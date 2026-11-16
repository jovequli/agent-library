"""Read-only adapters for supported Claude family archive formats.

Every returned record has archive-relative provenance. Importing never executes
historical commands, fetches remote assets, or follows a filesystem symlink.
Adapters deliberately distinguish complete chats, input history and metadata.
"""
from __future__ import annotations

import base64
import binascii
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any

PROVIDERS = ("claude", "claude_code", "cowork")
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp"}
_BINARY_KEYS = {"data", "base64", "image_base64"}
_MAX_EMBEDDED_IMAGE = 32 * 1024 * 1024


def _hash(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _date(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / (1000 if value > 1e11 else 1), timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return ""
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # A timezone-free source remains timezone-free; do not invent one.
        return date.isoformat().replace("+00:00", "Z")
    except ValueError:
        return str(value)


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[Embedded binary content retained in source]" if k in _BINARY_KEYS and isinstance(v, str) and len(v) > 256 else _clean(v)) for k, v in value.items() if k not in ("signature", "_audit_hmac")}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"data:(?:image|audio|video|application)/[^;,\s]+;base64,[A-Za-z0-9+/=\r\n]+", "[Embedded binary retained in source]", value)
    return value


def _dump(value: Any) -> str:
    return json.dumps(_clean(value), ensure_ascii=False, indent=2, default=str)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, list):
        return "\n\n".join(_text(item) for item in value if item is not None)
    if isinstance(value, dict) and value.get("type") in ("text", "input_text", "output_text"):
        return _text(value.get("text", ""))
    return _dump(value)


class _Reader:
    def __init__(self, selected: Path):
        self.selected = selected
        self.root = selected if selected.is_dir() else selected.parent
        self.warnings: list[dict] = []
        self.media: dict[str, str | bytes] = {}
        self.files = self._files()
        self.file_set = set(self.files)
        self._json_cache: dict[Path, Any] = {}

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def warn(self, code: str, path: Path, line: int = 0) -> None:
        self.warnings.append({"code": code, "file": self.relative(path), "line": line})

    def _files(self) -> list[Path]:
        if self.selected.is_symlink():
            raise ValueError("Source must not be a symlink")
        if self.selected.is_file():
            return [self.selected]
        files = []
        for directory, dirs, names in os.walk(self.selected, followlinks=False):
            parent = Path(directory)
            dirs[:] = sorted(name for name in dirs if name not in (".git", "__pycache__", "node_modules") and not (parent / name).is_symlink())
            for name in sorted(names):
                path = parent / name
                if path.is_file() and not path.is_symlink():
                    files.append(path)
        return sorted(files)

    def json(self, path: Path) -> Any:
        if path in self._json_cache:
            return self._json_cache[path]
        try:
            with path.open("r", encoding="utf-8-sig") as stream:
                data = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError):
            self.warn("invalid_json", path)
            data = None
        self._json_cache[path] = data
        return data

    def jsonl(self, path: Path):
        try:
            with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
                for line, raw in enumerate(stream, 1):
                    if not raw.strip():
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        self.warn("invalid_jsonl_row", path, line)
                        continue
                    if not isinstance(data, dict):
                        self.warn("non_object_jsonl_row", path, line)
                        continue
                    yield line, data
        except OSError:
            self.warn("source_read_error", path)

    def image(self, block: dict, path: Path, session_dir: Path | None = None) -> list[str]:
        source = block.get("source") or block.get("image_url") or block.get("url") or block
        if isinstance(source, dict):
            data = source.get("data") or source.get("base64")
            ref = source.get("url") or source.get("path") or source.get("file_path")
        else:
            data, ref = None, source
        if not data and isinstance(ref, str) and ref.startswith("data:image/"):
            try:
                _, data = ref.split(",", 1)
            except ValueError:
                data = None
        if isinstance(data, str):
            try:
                if len(data) > _MAX_EMBEDDED_IMAGE * 4 // 3 + 8:
                    raise ValueError("Image too large")
                payload = base64.b64decode(data, validate=True)
                # Do not publish arbitrary binary data as an image.
                if not _image_magic(payload):
                    raise ValueError("Unrecognized image format")
                key = _hash(payload)
                self.media[key] = payload
                return [key]
            except (ValueError, binascii.Error):
                self.warn("invalid_embedded_image", path)
        if isinstance(ref, str):
            local = self.local_image(ref, path, session_dir)
            if local:
                key = _hash(self.relative(local))
                self.media[key] = self.relative(local)
                return [key]
        return []

    def local_image(self, ref: str, path: Path, session_dir: Path | None = None) -> Path | None:
        # Absolute task paths and URLs are historical evidence, not permission
        # to read live disk locations or access a remote server.
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", ref) or "\x00" in ref:
            return None
        normalized = ref.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if ".." in pure.parts:
            return None
        if pure.is_absolute():
            # Cowork messages can mention old sandbox /mnt/.../outputs paths.
            # Resolve them only against preserved files in this task snapshot.
            if session_dir:
                for marker in ("outputs", "uploads"):
                    if marker in pure.parts:
                        tail = pure.parts[pure.parts.index(marker):]
                        matches = [p for p in self.files if session_dir in p.parents and len(p.parts) >= len(tail) and p.parts[-len(tail):] == tail and p.suffix.lower() in _IMAGE_EXTENSIONS]
                        if len(matches) == 1:
                            return matches[0]
            return None
        candidates = [path.parent / Path(*pure.parts), self.root / Path(*pure.parts)]
        if session_dir:
            candidates.append(session_dir / Path(*pure.parts))
            if pure.parts and pure.parts[0] == "mnt":
                candidates.append(session_dir / Path(*pure.parts[1:]))
        for candidate in candidates:
            try:
                relative = candidate.relative_to(self.root)
                cursor = self.root
                if any((self.root / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)):
                    continue
                candidate.relative_to(self.root)
                if candidate in self.file_set and candidate.suffix.lower() in _IMAGE_EXTENSIONS:
                    return candidate
            except (ValueError, OSError):
                pass
        return None


def _image_magic(data: bytes) -> bool:
    return data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM")) or (len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP") or (len(data) > 12 and data[4:8] == b"ftyp" and b"avif" in data[8:32])


def _session(ident: str, provider: str, title: str = "", project: str = "", kind: str = "main", parent_id: str | None = None) -> dict:
    return {"id": ident, "provider": provider, "title": title, "project": project, "kind": kind, "parent_id": parent_id, "created_at": "", "updated_at": "", "metadata": {}, "messages": []}


def _message(ident: str, role: str, kind: str, text: str, path: str, line: int = 0, timestamp: Any = "", parent_id: Any = None, media: list | None = None, metadata: dict | None = None) -> dict:
    return {"id": ident, "role": role, "kind": kind, "text": text, "source_path": path, "source_line": line, "created_at": _date(timestamp), "parent_id": parent_id, "media_keys": media or [], "metadata": metadata or {}}


def _blocks(reader: _Reader, content: Any, role: str, ident: str, path: Path, line: int = 0, timestamp: Any = "", parent_id: Any = None, synthetic: bool = False, response_id: str = "", session_dir: Path | None = None) -> list[dict]:
    blocks = content if isinstance(content, list) else [content]
    result = []
    for i, block in enumerate(blocks):
        kind, current_role, media = "text", role, []
        metadata = {"response_id": response_id, "block_index": i, "historical": True}
        if isinstance(block, str):
            text = _text(block)
        elif isinstance(block, dict):
            typ = str(block.get("type", ""))
            metadata["block_type"] = typ
            if typ in ("text", "input_text", "output_text"):
                text = _text(block.get("text", ""))
            elif typ in ("thinking", "redacted_thinking"):
                kind, text = "thinking", _text(block.get("thinking") or block.get("text") or "[Thinking content unavailable]")
            elif typ in ("tool_use", "server_tool_use"):
                kind, current_role = "tool", "assistant"
                metadata.update(tool_id=str(block.get("id", "")), tool_name=str(block.get("name", "tool")))
                text = metadata["tool_name"] + "\n" + _dump(block.get("input", {}))
            elif typ == "tool_result" or typ.endswith("_tool_result"):
                kind, current_role = "tool", "tool"
                metadata.update(tool_id=str(block.get("tool_use_id", "")), is_error=bool(block.get("is_error")))
                text = _text(block.get("content", block))
            elif typ in ("image", "image_url", "input_image"):
                kind = "image"
                media = reader.image(block, path, session_dir)
                metadata["missing_media"] = not bool(media)
                text = "" if media else "[Image bytes not present in this archive]"
            else:
                kind, text = "metadata", _dump(block)
        elif block is None:
            continue
        else:
            kind, text = "metadata", _dump(block)
        if synthetic and kind == "text":
            current_role, kind = "system", "metadata"
        if kind == "text" and text.startswith("This session is being continued from a previous conversation"):
            current_role, kind = "system", "context"
        if kind == "text":
            references = {}
            for match in re.finditer(r'!\[[^\]]*\]\((?:<([^>]+)>|([^\s)]+))(?:\s+["\'][^)]*)?\)', text):
                reference = match.group(1) or match.group(2)
                keys = reader.image({"source": {"path": reference}}, path, session_dir)
                if keys:
                    references[reference] = keys[0]
                    media.extend(key for key in keys if key not in media)
            if references:
                metadata["image_references"] = references
        if text or media:
            result.append(_message(f"{ident}:{i}", current_role, kind, text, reader.relative(path), line, timestamp, parent_id, media, metadata))
    return result


class _Collector:
    """Merge canonical/audit snapshots without merging independent user inputs."""
    def __init__(self):
        self.messages: list[dict] = []
        self.keys: dict[tuple, int] = {}
        self.slots: dict[tuple, int] = {}
        self.exact: set[tuple] = set()

    def add(self, message: dict):
        key = (message["id"], message["role"], message["kind"])
        old_index = self.keys.get(key)
        response = message["metadata"].get("response_id")
        block = message["metadata"].get("block_index")
        tool = message["metadata"].get("tool_id", "")
        slot = (response, message["role"], message["kind"], block, tool)
        exact = (response, message["role"], message["kind"], tool, message["text"], tuple(message["media_keys"]))
        if response and exact in self.exact:
            return
        if old_index is None and response:
            old_index = self.slots.get(slot)
        if old_index is not None:
            previous = self.messages[old_index]
            if previous["text"] == message["text"] and previous["media_keys"] == message["media_keys"]:
                return
            if message["kind"] in ("text", "thinking") and message["text"].startswith(previous["text"]):
                # Keep stable ID and canonical source location while expanding text.
                previous["text"] = message["text"]
                previous["media_keys"] = message["media_keys"]
                if not previous["created_at"]:
                    previous["created_at"] = message["created_at"]
                self.keys[key] = old_index
                if response:
                    self.exact.add(exact)
                return
            if message["kind"] in ("text", "thinking") and previous["text"].startswith(message["text"]):
                return
            # An actual revision, unlike a growing streaming snapshot, is retained.
            message["id"] += ":variant:" + _hash(message["text"] + _dump(message["media_keys"]))[:12]
            key = (message["id"], message["role"], message["kind"])
            if key in self.keys:
                return
        self.keys[key] = len(self.messages)
        if response:
            self.slots[slot] = len(self.messages)
            self.exact.add(exact)
        self.messages.append(message)

    def finish(self, preserve_order: bool = False) -> list[dict]:
        # Retain source chronology where timestamps are missing; sort only with
        # known/preceding timestamps. Never overwrite a source timestamp.
        if preserve_order:
            return list(self.messages)
        last = ""
        ordered = []
        for i, message in enumerate(self.messages):
            last = message["created_at"] or last
            ordered.append((last, i, message))
        return [m for _, _, m in sorted(ordered, key=lambda item: (item[0], item[1]))]


def _finish(session: dict, collector: _Collector | None = None) -> dict:
    if collector:
        session["messages"] = collector.finish(bool(session.get("metadata", {}).get("preserve_source_order")))
    dates = [m["created_at"] for m in session["messages"] if m.get("created_at")]
    session["created_at"] = session["created_at"] or (min(dates) if dates else "")
    session["updated_at"] = max([session["updated_at"], *dates]) if dates else session["updated_at"]
    if not session["title"]:
        session["title"] = next((re.sub(r"\s+", " ", m["text"]).strip()[:100] for m in session["messages"] if m["role"] == "user" and m["kind"] in ("text", "history")), "Untitled conversation")
    if session["kind"] == "main" and not any(m["role"] in ("user", "assistant") and m["kind"] in ("text", "image", "attachment") for m in session["messages"]):
        session["kind"] = "metadata"
        session["metadata"]["transcript_unavailable"] = True
    return session


def _log_records(reader: _Reader, row: dict, path: Path, line: int, session_dir: Path | None = None) -> list[dict]:
    typ = row.get("type", "")
    ident = str(row.get("uuid") or row.get("id") or f"{reader.relative(path)}:{line}")
    timestamp = row.get("timestamp") or row.get("_audit_timestamp") or ""
    parent = row.get("parentUuid") or row.get("logicalParentUuid")
    message = row.get("message")
    if typ in ("user", "assistant") and isinstance(message, dict):
        role = message.get("role", typ)
        if role not in ("user", "assistant", "tool", "system"):
            role = typ
        synthetic = bool(row.get("isMeta") or row.get("isSynthetic") or row.get("isCompactSummary") or row.get("sourceToolUseID") or row.get("sourceToolAssistantUUID"))
        records = _blocks(reader, message.get("content", ""), role, ident, path, line, timestamp, parent, synthetic, str(message.get("id") or ""), session_dir)
        for record in records:
            record["metadata"].update(raw_id=ident, is_sidechain=bool(row.get("isSidechain")))
        return records
    if typ == "stream_event":
        event = row.get("event") or {}
        delta = event.get("delta") or {} if isinstance(event, dict) else {}
        text = delta.get("text") or delta.get("thinking") or ""
        if not text:
            return []
        return [_message(ident, "system", "metadata", "[Unfinished streaming fragment]\n" + _text(text), reader.relative(path), line, timestamp, parent)]
    if typ in ("system", "summary", "result", "attachment", "continued-in", "started", "launched"):
        detail = {k: v for k, v in row.items() if k not in ("_audit_hmac", "uuid", "timestamp", "_audit_timestamp")}
        return [_message(ident, "system", "context" if typ in ("summary", "continued-in") else "metadata", _text(row.get("content")) if row.get("content") is not None else _dump(detail), reader.relative(path), line, timestamp, parent)]
    return []


def _read_log(reader: _Reader, path: Path, session: dict, collector: _Collector, session_dir: Path | None = None):
    for line, row in reader.jsonl(path):
        if row.get("cwd") and not session["project"]:
            session["project"] = str(row["cwd"])
        if row.get("customTitle") or row.get("aiTitle"):
            session["title"] = str(row.get("customTitle") or row.get("aiTitle"))
        for message in _log_records(reader, row, path, line, session_dir):
            collector.add(message)


def _claude(reader: _Reader) -> list[dict]:
    sessions = []
    candidates = [p for p in reader.files if p.suffix.lower() == ".json" and (p == reader.selected or "conversation" in p.name.lower() or any("conversation" in part.lower() for part in p.relative_to(reader.root).parts[:-1]))]
    projects: dict[str, str] = {}
    for path in reader.files:
        if path.suffix.lower() != ".json" or not ("project" in path.name.lower() or any("projects" in part.lower() for part in path.relative_to(reader.root).parts[:-1])):
            continue
        payload = reader.json(path)
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if isinstance(entry, dict) and entry.get("uuid") and entry.get("name") and ("docs" in entry or "prompt_template" in entry or "description" in entry):
                projects[str(entry["uuid"])] = str(entry["name"])
                context = _session("project:" + str(entry["uuid"]), "claude", "Project context · " + str(entry["name"]), str(entry["name"]), "context")
                for key in ("description", "prompt_template"):
                    if entry.get(key):
                        context["messages"].append(_message(key, "system", "context", f"[Historical project {key}; review before reuse]\n" + _text(entry[key]), reader.relative(path)))
                for i, document in enumerate(entry.get("docs", [])):
                    if isinstance(document, dict):
                        context["messages"].append(_message(str(document.get("uuid") or f"document:{i}"), "system", "context", f"[Project document: {document.get('filename', '')}]\n" + _text(document.get("content")), reader.relative(path)))
                if context["messages"]:
                    sessions.append(_finish(context))
    for path in candidates:
        payload = reader.json(path)
        if isinstance(payload, dict) and isinstance(payload.get("conversations"), list):
            entries = payload["conversations"]
        else:
            entries = payload if isinstance(payload, list) else [payload]
        for entry_index, entry in enumerate(entries):
            if not isinstance(entry, dict) or not isinstance(entry.get("chat_messages"), list):
                continue
            ident = str(entry.get("uuid") or entry.get("id") or "conversation:" + _hash(reader.relative(path) + str(entry_index))[:24])
            project = entry.get("project") or {}
            project_id = entry.get("project_uuid") or entry.get("project_id") or (project.get("uuid") if isinstance(project, dict) else "")
            project_name = project.get("name", "") if isinstance(project, dict) else str(project)
            session = _session(ident, "claude", str(entry.get("name") or entry.get("title") or ""), projects.get(str(project_id), "") or project_name)
            session["created_at"], session["updated_at"] = _date(entry.get("created_at")), _date(entry.get("updated_at"))
            session["metadata"]["project_id"] = project_id or ""
            collector = _Collector()
            if entry.get("summary"):
                collector.add(_message(ident + ":summary", "system", "context", "[Historical summary; verify current state]\n" + _text(entry["summary"]), reader.relative(path)))
            for index, message in enumerate(entry["chat_messages"]):
                if not isinstance(message, dict):
                    reader.warn("invalid_chat_message", path)
                    continue
                role = {"human": "user", "assistant": "assistant"}.get(message.get("sender"), "system")
                mid = str(message.get("uuid") or message.get("id") or f"{ident}:{index}")
                content = message.get("content") or message.get("text") or ""
                for record in _blocks(reader, content, role, mid, path, timestamp=message.get("created_at"), parent_id=message.get("parent_message_uuid")):
                    collector.add(record)
                attachments = message.get("attachments") or message.get("files") or []
                for j, attachment in enumerate(attachments):
                    if not isinstance(attachment, dict):
                        continue
                    ref = attachment.get("file_name") or attachment.get("filename") or attachment.get("path") or attachment.get("url") or ""
                    media = reader.image({"source": {"path": ref}}, path)
                    if media:
                        collector.add(_message(mid + f":file:{j}", role, "image", "", reader.relative(path), timestamp=message.get("created_at"), media=media, metadata={"name": str(ref)}))
                    else:
                        extracted = attachment.get("extracted_content")
                        collector.add(_message(mid + f":file:{j}", "system", "context" if extracted else "metadata", ("[Historical attachment; original bytes may be unavailable]\n" + _dump(attachment)), reader.relative(path), timestamp=message.get("created_at"), metadata={"missing_media": True}))
            sessions.append(_finish(session, collector))
    # Account memory is a dated source snapshot, never a current instruction.
    for path in reader.files:
        if path.suffix.lower() != ".json" or "memor" not in reader.relative(path).lower():
            continue
        payload = reader.json(path)
        if not isinstance(payload, dict) or not any(k in payload for k in ("conversations_memory", "memory_files")):
            continue
        context = _session("memory:" + _hash(reader.relative(path))[:20], "claude", "Historical account memory", kind="context")
        if payload.get("conversations_memory"):
            context["messages"].append(_message("conversation-memory", "system", "context", "[Historical account memory; review before reuse]\n" + _text(payload["conversations_memory"]), reader.relative(path)))
        for i, memory in enumerate(payload.get("memory_files", [])):
            if isinstance(memory, dict):
                context["messages"].append(_message(str(memory.get("path") or i), "system", "context", _text(memory.get("content")), reader.relative(path), timestamp=memory.get("updated_at"), metadata={"historical_path": memory.get("path", "")}))
        if context["messages"]:
            sessions.append(_finish(context))
    return sessions


def _cowork_roots(reader: _Reader) -> list[Path]:
    roots = set()
    evidence = {p.parent for p in reader.files if p.name.startswith("audit") and p.suffix == ".jsonl"}
    evidence.update(p.with_suffix("") for p in reader.files if p.suffix == ".json" and p.stem.startswith("local_") and p.with_suffix("").is_dir() and not p.with_suffix("").is_symlink())
    for path in reader.files:
        for directory in path.parents:
            if directory == reader.root.parent:
                break
            if directory.name.startswith("local_") and ("local-agent-mode-sessions" in directory.parts or directory in evidence):
                roots.add(directory)
                break
        if path.suffix == ".json" and path.stem.startswith("local_") and "local-agent-mode-sessions" in path.parts:
            # Metadata may survive even when its matching transcript dir does not.
            roots.add(path.with_suffix(""))
    if reader.selected.is_file() and reader.selected.name.startswith("audit") and reader.selected.suffix == ".jsonl":
        roots.add(reader.root)
    return sorted(roots)


def _cowork(reader: _Reader, roots: list[Path]) -> list[dict]:
    result = []
    for directory in roots:
        metadata_path = directory.with_suffix(".json")
        metadata = reader.json(metadata_path) if metadata_path in reader.file_set else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        ident = str(metadata.get("sessionId") or metadata.get("id") or directory.name)
        selected_folders = metadata.get("userSelectedFolders") or []
        folder_names = [str(x.get("path") or x.get("name") or "") if isinstance(x, dict) else str(x) for x in selected_folders] if isinstance(selected_folders, list) else [str(selected_folders)]
        project = str(metadata.get("cwd") or next(iter(folder_names), ""))
        session = _session(ident, "cowork", str(metadata.get("title") or ""), project)
        session["created_at"] = _date(metadata.get("createdAt"))
        session["updated_at"] = _date(metadata.get("lastActivityAt"))
        session["metadata"] = {"selected_folders": folder_names, "historical": True}
        files = [p for p in reader.files if directory in p.parents]
        if reader.selected.is_file() and directory == reader.root:
            files = reader.files
        logs = [p for p in files if p.suffix == ".jsonl" and "projects" in p.parts and ".claude" in p.parts]
        main_logs = [p for p in logs if "subagents" not in p.parts and not p.stem.startswith("agent-")]
        child_logs = [p for p in logs if p not in main_logs]
        collector = _Collector()
        for path in main_logs:
            _read_log(reader, path, session, collector, directory)
        children = []
        by_uuid = {}
        for path in child_logs:
            child = _session(ident + ":subagent:" + _hash(reader.relative(path))[:20], "cowork", "", project, "subagent", ident)
            cc = _Collector()
            _read_log(reader, path, child, cc, directory)
            children.append((child, cc))
            for message in cc.messages:
                raw_id = message["metadata"].get("raw_id")
                if raw_id:
                    by_uuid[raw_id] = (child, cc)
        audit_children = {}
        for path in [p for p in files if p.name.startswith("audit") and p.suffix == ".jsonl"]:
            for line, row in reader.jsonl(path):
                records = _log_records(reader, row, path, line, directory)
                if not records:
                    continue
                parent_tool = row.get("parent_tool_use_id")
                matching = by_uuid.get(str(row.get("uuid") or ""))
                if matching:
                    _, target = matching
                elif parent_tool:
                    key = str(parent_tool)
                    if key not in audit_children:
                        child = _session(ident + ":audit-agent:" + _hash(key)[:20], "cowork", "", project, "subagent", ident)
                        audit_children[key] = (child, _Collector())
                    _, target = audit_children[key]
                else:
                    target = collector
                for record in records:
                    target.add(record)
        initial = metadata.get("initialMessage")
        if isinstance(initial, str) and initial.strip() and not any(m["role"] == "user" and m["kind"] == "text" and initial.strip() == m["text"].strip() for m in collector.messages):
            collector.add(_message(ident + ":initial", "user", "text", initial, reader.relative(metadata_path), timestamp=session["created_at"], metadata={"origin": "metadata.initialMessage"}))
        if not collector.messages and metadata_path in reader.file_set:
            collector.add(_message(ident + ":metadata", "system", "metadata", "[Session metadata only; transcript bytes are unavailable]\n" + _dump(metadata), reader.relative(metadata_path)))
        # Restore all preserved outputs/uploads images into an optional gallery.
        known = set(k for m in collector.messages for k in m["media_keys"])
        for path in files:
            if path.suffix.lower() in _IMAGE_EXTENSIONS and any(part in ("outputs", "uploads") for part in path.relative_to(directory).parts):
                key = _hash(reader.relative(path))
                reader.media[key] = reader.relative(path)
                if key not in known:
                    collector.add(_message("artifact:" + key, "system", "image", path.name, reader.relative(path), media=[key], metadata={"artifact": True, "not_message_attachment": True}))
                    known.add(key)
        _attach_memory(reader, files, collector)
        result.append(_finish(session, collector))
        result.extend(_finish(child, cc) for child, cc in [*children, *audit_children.values()])
    return result


def _attach_memory(reader: _Reader, files: list[Path], collector: _Collector):
    for path in files:
        if "memory" in path.parts and path.suffix.lower() in (".md", ".txt"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                reader.warn("source_read_error", path)
                continue
            collector.add(_message("memory:" + _hash(reader.relative(path))[:20], "system", "context", "[Historical project memory; may postdate this conversation]\n" + _clean(text), reader.relative(path)))


def _code(reader: _Reader, cowork_roots: list[Path]) -> list[dict]:
    result = []
    main_ids = set()
    logs = [p for p in reader.files if p.suffix == ".jsonl" and not any(root in p.parents for root in cowork_roots)]
    for path in logs:
        if path.name == "history.jsonl" or path.name.startswith("audit"):
            continue
        rows = list(reader.jsonl(path))
        if not any(row.get("type") in ("user", "assistant", "system") for _, row in rows):
            continue
        child = "subagents" in path.parts or path.stem.startswith("agent-")
        parent_id = path.parent.parent.name if "subagents" in path.parts else None
        sid = path.stem
        if not child:
            sid = str(next((row.get("sessionId") or row.get("session_id") for _, row in rows if row.get("sessionId") or row.get("session_id")), sid))
            main_ids.add(sid)
        ident = (str(parent_id) + ":subagent:" + path.stem) if child else sid
        session = _session(ident, "claude_code", kind="subagent" if child else "main", parent_id=parent_id)
        collector = _Collector()
        for line, row in rows:
            if row.get("cwd") and not session["project"]:
                session["project"] = str(row["cwd"])
            if row.get("customTitle") or row.get("aiTitle"):
                session["title"] = str(row.get("customTitle") or row.get("aiTitle"))
            for message in _log_records(reader, row, path, line):
                collector.add(message)
        # Only attach memory from the same encoded project directory.
        if "projects" in path.parts:
            index = path.parts.index("projects")
            if len(path.parts) > index + 1:
                project_dir = Path(*path.parts[:index + 2])
                _attach_memory(reader, [p for p in reader.files if project_dir in p.parents], collector)
        result.append(_finish(session, collector))
    for path in [p for p in logs if p.name == "history.jsonl"]:
        groups = {}
        for line, row in reader.jsonl(path):
            if "display" not in row:
                continue
            sid = str(row.get("sessionId") or _hash(str(row.get("project", "")))[:20])
            if sid not in groups:
                session = _session("history:" + sid, "claude_code", "", str(row.get("project") or ""), "history", sid if sid in main_ids else None)
                session["metadata"]["input_history_only"] = True
                groups[sid] = (session, _Collector())
            session, collector = groups[sid]
            text = _text(row.get("display"))
            if row.get("pastedContents"):
                text += "\n\n[Preserved pasted content]\n" + _text(row["pastedContents"])
            if text:
                collector.add(_message("history:" + str(line), "user", "history", text, reader.relative(path), line, row.get("timestamp")))
        result.extend(_finish(session, collector) for session, collector in groups.values())
    # Metadata-only Desktop Code records must not be described as recovered chats.
    for path in reader.files:
        if path.suffix == ".json" and "claude-code-sessions" in path.parts:
            meta = reader.json(path)
            if not isinstance(meta, dict):
                continue
            sid = str(meta.get("sessionId") or path.stem)
            if sid in main_ids:
                continue
            session = _session(sid, "claude_code", str(meta.get("title") or "Desktop Code metadata"), str(meta.get("originCwd") or meta.get("cwd") or ""), "metadata")
            session["metadata"]["transcript_unavailable"] = True
            session["messages"] = [_message("metadata", "system", "metadata", "[Session metadata only; no transcript found]\n" + _dump(meta), reader.relative(path))]
            result.append(_finish(session))
    for path in reader.files:
        if path.name == "CLAUDE.md" and path.parent == reader.root:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                reader.warn("source_read_error", path)
                continue
            session = _session("global-context", "claude_code", "Historical global instructions", kind="context")
            session["messages"] = [_message("global-context", "system", "context", "[Historical global requirements; review applicability before reuse]\n" + _clean(text), reader.relative(path))]
            result.append(_finish(session))
    return result


def _portable(reader: _Reader) -> list[dict]:
    """Versioned neutral interchange used by downloadable context packs."""
    result = []
    roles = {"user", "assistant", "tool", "system"}
    kinds = {"text", "tool", "thinking", "metadata", "context", "image", "history", "attachment", "summary"}
    session_kinds = {"main", "subagent", "history", "context", "metadata"}
    for path in reader.files:
        if path.suffix.lower() != ".json" or not (path == reader.selected or path.name in ("conversation.json", "agent-library.json")):
            continue
        payload = reader.json(path)
        if not isinstance(payload, dict) or payload.get("format") != "agent-library.conversation":
            continue
        if payload.get("version") != 1:
            raise ValueError("Unsupported agent-library conversation version")
        if not isinstance(payload.get("sessions"), list):
            raise ValueError("Invalid agent-library conversation sessions")
        available_media = {}
        mapping = payload.get("media") or {}
        if not isinstance(mapping, dict):
            raise ValueError("Invalid agent-library media mapping")
        for key, reference in mapping.items():
            if not isinstance(key, str) or not isinstance(reference, str):
                continue
            local = reader.local_image(reference, path)
            if local:
                new_key = _hash(reader.relative(local))
                reader.media[new_key] = reader.relative(local)
                available_media[key] = new_key
        for index, raw in enumerate(payload["sessions"]):
            if not isinstance(raw, dict) or not isinstance(raw.get("messages"), list):
                raise ValueError("Invalid agent-library conversation session")
            origin = str(raw.get("provider") or "generic")
            if len(origin) > 80 or not re.fullmatch(r"[a-zA-Z0-9_-]+", origin):
                origin = "generic"
            kind = raw.get("kind", "main")
            session = _session(str(raw.get("native_id") or raw.get("id") or "portable:" + str(index)), origin, str(raw.get("title") or ""), str(raw.get("project") or ""), kind if kind in session_kinds else "main", raw.get("parent_id"))
            session["created_at"], session["updated_at"] = _date(raw.get("created_at")), _date(raw.get("updated_at"))
            if raw.get("metadata") is not None and not isinstance(raw["metadata"], dict):
                raise ValueError("Invalid agent-library session metadata")
            session["metadata"] = _clean(raw.get("metadata") or {})
            session["metadata"]["preserve_source_order"] = True
            attachments = session["metadata"].get("session_attachments", [])
            if isinstance(attachments, list):
                session["metadata"]["session_attachments"] = [{**item, "id": available_media[item["id"]]} for item in attachments
                    if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] in available_media]
            artifact_refs = session["metadata"].get("artifact_references", {})
            preserved_artifacts = {}
            if isinstance(artifact_refs, dict):
                from .core import safe_relative
                for key, reference in artifact_refs.items():
                    if key not in {"handoff", "requirements", "transcript"} or not isinstance(reference, str):
                        continue
                    candidate = safe_relative(reader.root, reference)
                    if candidate in reader.file_set and candidate.is_file():
                        preserved_artifacts[key] = reader.relative(candidate)
            if session["metadata"].get("legacy_artifacts"):
                session["metadata"]["original_artifact_paths"] = session["metadata"]["legacy_artifacts"]
            session["metadata"]["legacy_artifacts"] = preserved_artifacts
            collector = _Collector()
            for i, message in enumerate(raw["messages"]):
                if not isinstance(message, dict) or message.get("role") not in roles:
                    raise ValueError("Invalid agent-library message role")
                if message.get("kind", "text") not in kinds:
                    raise ValueError("Invalid agent-library message kind")
                if message.get("metadata") is not None and not isinstance(message["metadata"], dict):
                    raise ValueError("Invalid agent-library message metadata")
                if message.get("media_keys") is not None and not isinstance(message["media_keys"], list):
                    raise ValueError("Invalid agent-library message media keys")
                meta = _clean(message.get("metadata") or {})
                references = meta.get("image_references")
                if isinstance(references, dict):
                    meta["image_references"] = {ref: available_media[key] for ref, key in references.items() if isinstance(ref, str) and isinstance(key, str) and key in available_media}
                meta["original_provenance"] = {"source_path": str(message.get("source_path") or ""), "source_line": message.get("source_line", 0)}
                media = [available_media[k] for k in message.get("media_keys", []) if isinstance(k, str) and k in available_media]
                if message.get("media_keys") and not media:
                    meta["missing_media"] = True
                collector.add(_message(str(message.get("native_id") or message.get("id") or f"message:{i}"), message["role"], message.get("kind", "text"), _text(message.get("text")), reader.relative(path), timestamp=message.get("created_at"), parent_id=message.get("parent_id"), media=media, metadata=meta))
            result.append(_finish(session, collector))
    return result


def parse_source(path: Path, provider: str = "auto") -> dict:
    """Normalize supported source files without writing to the selected source.

    ``path`` is a directory, official conversations JSON, Code JSONL, or Cowork
    audit JSONL. ZIP extraction and immutable source snapshots belong to ingest.
    An auto import may discover several providers in one explicitly selected tree.
    """
    selected = Path(path).expanduser().absolute()
    if provider not in ("auto", "agent_library", *PROVIDERS):
        raise ValueError("Unsupported provider")
    if not selected.exists():
        raise ValueError("Source does not exist")
    if selected.is_symlink():
        raise ValueError("Source must not be a symlink")
    selected = selected.resolve()
    reader = _Reader(selected)
    roots = _cowork_roots(reader)
    sessions = _portable(reader) if provider in ("auto", "agent_library") else []
    if provider in ("auto", "claude"):
        sessions.extend(_claude(reader))
    if provider in ("auto", "cowork"):
        sessions.extend(_cowork(reader, roots))
    if provider in ("auto", "claude_code"):
        sessions.extend(_code(reader, roots))
    # Combining duplicate exports should not replace a newer copy arbitrarily.
    merged: dict[tuple[str, str], tuple[dict, _Collector]] = {}
    for session in sessions:
        key = (session["provider"], session["id"])
        if key not in merged:
            merged[key] = (session, _Collector())
        destination, collector = merged[key]
        for message in session["messages"]:
            collector.add(message)
        destination["created_at"] = min(filter(None, (destination["created_at"], session["created_at"])), default="")
        destination["updated_at"] = max(destination["updated_at"], session["updated_at"])
    sessions = [_finish(session, collector) for session, collector in merged.values()]
    if not sessions:
        raise ValueError("No supported conversation source found")
    found = sorted({session["provider"] for session in sessions})
    return {"sessions": sessions, "warnings": reader.warnings, "provider": found[0] if len(found) == 1 else "mixed", "media": reader.media}


def create_demo_source(path: Path) -> Path:
    """Write only fictional, reproducible examples for onboarding and tests."""
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    def write_json(relative: str, value: Any):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    def write_lines(relative: str, values: list[dict]):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(json.dumps(value, ensure_ascii=False) for value in values) + "\n", encoding="utf-8")
    # This tiny image is generated from a public format sample, not user media.
    tiny_png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jP1sAAAAASUVORK5CYII="
    write_json("claude/conversations.json", [{
        "uuid": "demo-claude-reading", "name": "Demo · A readable research conversation", "project_uuid": "demo-project", "created_at": "2026-01-01T09:00:00Z", "updated_at": "2026-01-01T09:04:00Z",
        "chat_messages": [
            {"uuid": "demo-c-u1", "sender": "human", "created_at": "2026-01-01T09:00:00Z", "content": [{"type": "text", "text": "请整理这个虚构的研究计划。Please keep the answer readable, preserve my requirements, and give me a small table."}]},
            {"uuid": "demo-c-a1", "sender": "assistant", "created_at": "2026-01-01T09:01:00Z", "parent_message_uuid": "demo-c-u1", "content": [{"type": "text", "text": "# Fictional river study\n\nThis is **synthetic demonstration data**. 这是虚构演示数据。\n\n| Task | Status |\n|:--|:--|\n| Read sources | Complete |\n| Compare results | Pending |\n\nThe inline formula is $E = mc^2$.\n\n$$\\frac{1}{n} \\sum_{i=1}^{n} x_i$$\n\n```python\nprint('Hello, archive')\n```\n\n- [x] Preserve the original conversation\n- [ ] Continue with another assistant"}]},
            {"uuid": "demo-c-u2", "sender": "human", "created_at": "2026-01-01T09:03:00Z", "content": [{"type": "text", "text": "下一步请比较两个方案。Keep the original source links and do not invent missing findings."}, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": tiny_png}}]},
            {"uuid": "demo-c-a2", "sender": "assistant", "created_at": "2026-01-01T09:04:00Z", "content": [{"type": "text", "text": "I have organized the plan. The next assistant should compare the two approaches and verify source evidence before drawing conclusions."}]},
        ]
    }])
    write_json("claude/projects/demo-project.json", {"uuid": "demo-project", "name": "Fictional research shelf", "description": "All content in this project is fictional.", "prompt_template": "Keep citations and mark unknowns explicitly.", "docs": []})
    rows = []
    for index in range(54):
        role = "user" if index % 2 == 0 else "assistant"
        rows.append({"type": role, "uuid": f"demo-code-{index}", "sessionId": "demo-code-main", "cwd": "/demo/projects/river-notes", "timestamp": f"2026-01-02T10:{index:02d}:00Z", "parentUuid": f"demo-code-{index-1}" if index else None, "message": {"role": role, "content": [{"type": "text", "text": (f"Demo request {index // 2 + 1}: review the fictional parser and preserve the public API. 虚构测试需求。" if role == "user" else f"Demo reply {index // 2 + 1}: the parser changes are documented; follow-up verification remains pending.")}]}})
    rows.insert(2, {"type": "system", "uuid": "demo-code-hook", "subtype": "hook_success", "timestamp": "2026-01-02T10:01:30Z", "content": "Demo runtime details are hidden in normal reading mode."})
    write_lines("claude_code/projects/-demo-projects-river-notes/demo-code-main.jsonl", rows)
    write_lines("claude_code/projects/-demo-projects-river-notes/demo-code-main/subagents/agent-review.jsonl", [{"type": "assistant", "uuid": "demo-code-child", "timestamp": "2026-01-02T10:03:00Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Synthetic subagent review: add malformed-input coverage."}]}}])
    write_lines("claude_code/history.jsonl", [{"display": "A preserved input is not a complete conversation.", "sessionId": "demo-history-only", "project": "/demo/projects/input-history", "timestamp": 1767348000000}])
    cowork_dir = "cowork/local-agent-mode-sessions/demo-account/demo-org/local_demo-task"
    write_json(cowork_dir + ".json", {"title": "Demo · Organize a reading folder", "createdAt": "2026-01-03T09:00:00Z", "lastActivityAt": "2026-01-03T09:03:00Z", "initialMessage": "Organize the fictional reading folder without deleting original files.", "userSelectedFolders": [{"path": "/demo/reading-folder"}]})
    cowork_rows = [
        {"type": "user", "uuid": "demo-cowork-u", "timestamp": "2026-01-03T09:00:00Z", "message": {"role": "user", "content": "Organize the fictional reading folder without deleting original files."}},
        {"type": "assistant", "uuid": "demo-cowork-a", "timestamp": "2026-01-03T09:01:00Z", "message": {"id": "demo-cowork-response", "role": "assistant", "content": [{"type": "text", "text": "## Reading folder\n\nI prepared a proposed structure. The originals remain available.\n\n> Please review the pending categories before continuing."}, {"type": "tool_use", "id": "demo-list", "name": "list_directory", "input": {"path": "/demo/reading-folder"}}]}},
        {"type": "user", "uuid": "demo-cowork-tool", "timestamp": "2026-01-03T09:02:00Z", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "demo-list", "content": "fictional-paper-a.pdf\nfictional-paper-b.pdf"}]}},
        {"type": "assistant", "uuid": "demo-cowork-done", "timestamp": "2026-01-03T09:03:00Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Two fictional papers are ready for human review. Pending: choose categories and approve the proposed folder names."}]}},
    ]
    write_lines(cowork_dir + "/.claude/projects/demo-folder/demo-cowork.jsonl", cowork_rows)
    write_lines(cowork_dir + "/audit.jsonl", cowork_rows)
    image = root / cowork_dir / "outputs/demo-pixel.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(base64.b64decode(tiny_png))
    return root
