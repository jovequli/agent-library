"""Snapshot, parse and validate inputs before atomically updating a library."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import stat
import zipfile
from pathlib import Path, PurePosixPath

from . import __version__, schema
from .core import LibraryStore, now, safe_relative, write_json

MAX_FILES = 100_000
MAX_FILE = 2 * 1024**3
MAX_TOTAL = 20 * 1024**3
SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}
EXCLUDE_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "plugins", "cache", "caches", "debug", "telemetry", "statsig"}
EXCLUDE_FILES = {".credentials.json", "credentials.json", "auth.json", "settings.json", "settings.local.json", "config.json", ".env"}


def _eligible(parts) -> bool:
    return not any(p.lower() in EXCLUDE_DIRS for p in parts[:-1]) and parts[-1].lower() not in EXCLUDE_FILES


def extract_zip(source: Path, destination: Path) -> None:
    """Extract supported history files with strict cross-platform path checks."""
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES:
            raise ValueError("Archive contains too many files.")
        seen, total = set(), 0
        for item in entries:
            name = item.filename
            parts = PurePosixPath(name).parts
            if (not parts or name.startswith(("/", "\\")) or "\\" in name or ":" in name
                    or any(p in ("..", ".") for p in parts) or "\0" in name):
                raise ValueError("Unsafe ZIP path.")
            key = name.rstrip("/").casefold()
            if key in seen:
                raise ValueError("Duplicate ZIP paths are not supported.")
            seen.add(key)
            if stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError("ZIP symlinks are not supported.")
            total += item.file_size
            if item.file_size > MAX_FILE or total > MAX_TOTAL or (item.file_size > 1024**2 and item.file_size > max(item.compress_size, 1) * 1000):
                raise ValueError("Archive exceeds safe import limits.")
        for item in entries:
            parts = PurePosixPath(item.filename).parts
            if item.is_dir() or not _eligible(parts) or Path(item.filename).suffix.lower() not in SUFFIXES:
                continue
            target = safe_relative(destination, item.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)


def snapshot(source: Path, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    source = source.expanduser()
    if source.is_symlink():
        raise ValueError("Select the original source, not a symlink.")
    source = source.resolve(strict=True)
    if source.is_dir() and destination.resolve().is_relative_to(source):
        raise ValueError("Select a conversation export directory, not a parent of the library data directory.")
    skipped = 0
    if source.is_file() and source.suffix.lower() == ".zip":
        extract_zip(source, destination)
    else:
        files = []
        if source.is_file():
            files = [(source, source.name)]
        else:
            for current, directories, names in os.walk(source, followlinks=False):
                base = Path(current)
                directories[:] = sorted(d for d in directories if d.lower() not in EXCLUDE_DIRS and not (base / d).is_symlink())
                for name in sorted(names):
                    path = base / name
                    rel = path.relative_to(source)
                    if path.is_symlink() or not _eligible(rel.parts) or path.suffix.lower() not in SUFFIXES:
                        skipped += 1
                        continue
                    files.append((path, rel.as_posix()))
                    if len(files) > MAX_FILES:
                        raise ValueError("Source contains too many files.")
        total = 0
        for path, relative in files:
            if path.suffix.lower() not in SUFFIXES or not _eligible(Path(relative).parts):
                raise ValueError("Select supported JSON, JSONL, ZIP or a history directory.")
            size = path.stat().st_size
            total += size
            if size > MAX_FILE or total > MAX_TOTAL:
                raise ValueError("Source exceeds import size limits.")
            target = safe_relative(destination, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target, follow_symlinks=False)
            if target.is_symlink():
                target.unlink()
                raise ValueError("Source changed to a symlink during import.")
    digest, count, size = hashlib.sha256(), 0, 0
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            relative = path.relative_to(destination).as_posix()
            digest.update(relative.encode("utf-8") + b"\0")
            fdigest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    fdigest.update(chunk)
                    size += len(chunk)
            digest.update(fdigest.digest())
            count += 1
    return {"sha256": digest.hexdigest(), "files": count, "bytes": size, "skipped": skipped}


def image_type(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data.startswith(b"BM"):
        return "image/bmp", ".bmp"
    if len(data) > 16 and data[4:8] == b"ftyp" and b"avif" in data[8:32]:
        return "image/avif", ".avif"
    return None


def stable_id(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def merge_messages(old: list[dict], incoming: list[dict], stale: bool) -> tuple[list[dict], int]:
    """Union native blocks, retaining source order and expanded response snapshots."""
    existing = {m["native_id"]: m for m in old}
    order = [m["native_id"] for m in old]
    retained = 0
    cursor = None
    for index, message in enumerate(incoming):
        key = message["native_id"]
        previous = existing.get(key)
        if previous:
            older_snapshot = previous["text"].startswith(message["text"]) and len(previous["text"]) > len(message["text"])
            if older_snapshot or (stale and not message["text"].startswith(previous["text"])):
                retained += 1
            else:
                message["id"] = previous["id"]
                if previous["text"] != message["text"] and not message["text"].startswith(previous["text"]):
                    metadata = json.loads(message["metadata_json"])
                    versions = json.loads(previous["metadata_json"]).get("previous_versions", [])
                    metadata["previous_versions"] = [*versions[-15:], {"text": previous["text"], "source_path": previous["source_path"], "source_line": previous["source_line"]}]
                    message["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
                existing[key] = message
        else:
            if cursor is not None:
                position = order.index(cursor) + 1
            else:
                following = next((m["native_id"] for m in incoming[index + 1:] if m["native_id"] in existing), None)
                if following:
                    position = order.index(following)
                elif message["created_at"]:
                    position = next((i for i, k in enumerate(order) if existing[k]["created_at"] and existing[k]["created_at"] > message["created_at"]), len(order))
                else:
                    position = len(order)
            order.insert(position, key)
            existing[key] = message
        cursor = key
    result = [existing[k] for k in order]
    for ordinal, message in enumerate(result):
        message["ordinal"] = ordinal
    return result, retained


def import_source(store: LibraryStore, library_id: str, path: Path | str, provider: str = "auto", dataset: str = "default", progress=None) -> dict:
    from .importers import parse_source
    if provider not in {"auto", "claude", "claude_code", "cowork", "agent_library"}:
        raise ValueError("Unsupported source provider.")
    dataset = str(dataset or "default").strip()
    if not dataset or len(dataset) > 120:
        raise ValueError("Dataset label must contain 1 to 120 characters.")
    update = progress or (lambda phase, percent: None)
    with store.locked(library_id):
        active = store.generation(library_id)
        staging = store.stage(library_id)
        try:
            update("snapshot", 5)
            shutil.copytree(active, staging, dirs_exist_ok=True)
            incoming = staging / "sources" / ".incoming"
            details = snapshot(Path(path), incoming)
            receipts = json.loads((staging / "imports.json").read_text(encoding="utf-8"))
            if any(r.get("sha256") == details["sha256"] and r.get("dataset") == dataset and r.get("requested_provider") == provider for r in receipts):
                return {"sessions": 0, "messages": 0, "warnings": [], "unchanged": True}
            receipt_id = stable_id(dataset, provider, details["sha256"])[:32]
            source_dir = staging / "sources" / receipt_id
            if source_dir.exists():
                shutil.rmtree(incoming)
            else:
                incoming.rename(source_dir)
            update("parse", 25)
            selected = Path(path).expanduser()
            parse_path = source_dir / selected.name if selected.is_file() and selected.suffix.lower() != ".zip" else source_dir
            parsed = parse_source(parse_path, provider)
            sessions = parsed.get("sessions", [])
            if not sessions:
                raise ValueError("No supported conversations found in this source.")
            warnings = list(parsed.get("warnings", []))
            if details["skipped"]:
                warnings.append({"code": "files_skipped", "count": details["skipped"], "message": "Unrelated files, credentials and symlinks were excluded."})
            update("index", 55)
            db = sqlite3.connect(staging / "archive.sqlite3")
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            media_ids = {}
            try:
                for key, value in parsed.get("media", {}).items():
                    if isinstance(value, bytes):
                        data, name = value, str(key)
                    elif isinstance(value, str):
                        media_path = safe_relative(source_dir, value)
                        if not media_path.is_file() or media_path.stat().st_size > 64 * 1024**2:
                            warnings.append({"code": "missing_media", "file": value})
                            continue
                        data, name = media_path.read_bytes(), media_path.name
                    else:
                        continue
                    mime = image_type(data)
                    if not mime or len(data) > 64 * 1024**2:
                        warnings.append({"code": "unsupported_media", "file": name})
                        continue
                    media_id = hashlib.sha256(data).hexdigest()
                    relative = "media/" + media_id + mime[1]
                    (staging / relative).write_bytes(data)
                    db.execute("INSERT OR REPLACE INTO media VALUES (?,?,?,?,?)", (media_id, relative, mime[0], Path(name).name, len(data)))
                    media_ids[str(key)] = media_id
                inserted_messages = 0
                for session in sessions:
                    native = str(session["id"])
                    actual_provider = str(session.get("provider", parsed.get("provider", "claude")))
                    ident = stable_id(dataset, actual_provider, native)
                    parent = stable_id(dataset, actual_provider, str(session["parent_id"])) if session.get("parent_id") else None
                    old_session = db.execute("SELECT * FROM sessions WHERE id=?", (ident,)).fetchone()
                    old_messages = [dict(row) for row in db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY ordinal", (ident,))]
                    stale = bool(old_session and old_session["updated_at"] and session.get("updated_at") and session["updated_at"] < old_session["updated_at"])
                    old_metadata = json.loads(old_session["metadata_json"]) if old_session else {}
                    incoming_metadata = dict(session.get("metadata", {}))
                    if isinstance(incoming_metadata.get("session_attachments"), list):
                        incoming_metadata["session_attachments"] = [{**item, "id": media_ids[item["id"]]} for item in incoming_metadata["session_attachments"]
                            if isinstance(item, dict) and item.get("id") in media_ids]
                    if isinstance(incoming_metadata.get("legacy_artifacts"), dict):
                        incoming_metadata["legacy_artifacts"] = {key: "sources/" + receipt_id + "/" + value
                            for key, value in incoming_metadata["legacy_artifacts"].items() if isinstance(value, str) and safe_relative(source_dir, value).is_file()}
                    metadata = {**incoming_metadata, **old_metadata} if stale else {**old_metadata, **incoming_metadata}
                    db.execute("DELETE FROM messages WHERE session_id=?", (ident,))
                    db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                        ident, native, dataset, actual_provider, old_session["title"] if stale else str(session.get("title") or "Untitled conversation"),
                        old_session["project"] if stale else str(session.get("project") or ""), old_session["created_at"] if stale else str(session.get("created_at") or ""), max(str(session.get("updated_at") or ""), old_session["updated_at"] if old_session else ""),
                        old_session["kind"] if stale else str(session.get("kind") or "main"), parent, json.dumps(metadata, ensure_ascii=False)))
                    incoming_messages = []
                    seen_native = {}
                    for ordinal, message in enumerate(session.get("messages", [])):
                        role = str(message.get("role", "system"))
                        kind = str(message.get("kind", "text"))
                        if role not in {"user", "assistant", "tool", "system"}:
                            role = "system"
                        message_native = str(message.get("id") or ordinal)
                        occurrence = seen_native.get(message_native, 0)
                        seen_native[message_native] = occurrence + 1
                        if occurrence:
                            message_native += ":occurrence:" + str(occurrence)
                        source = str(message.get("source_path") or "")
                        source_relative = ""
                        if source:
                            safe_relative(source_dir, source)
                            source_relative = "sources/" + receipt_id + "/" + source
                        mids = list(dict.fromkeys(media_ids[k] for k in message.get("media_keys", []) if k in media_ids))
                        message_meta = dict(message.get("metadata", {}))
                        if isinstance(message_meta.get("image_references"), dict):
                            message_meta["image_references"] = {ref: media_ids[key] for ref, key in message_meta["image_references"].items() if key in media_ids}
                        incoming_messages.append(dict(id=stable_id(ident, message_native), session_id=ident, ordinal=ordinal, native_id=message_native,
                            role=role, kind=kind, text=str(message.get("text") or ""), created_at=str(message.get("created_at") or ""),
                            source_path=source_relative, source_line=int(message.get("source_line") or 0), parent_id=message.get("parent_id"),
                            media_json=json.dumps(mids), metadata_json=json.dumps(message_meta, ensure_ascii=False)))
                    merged, retained = merge_messages(old_messages, incoming_messages, stale)
                    if retained:
                        warnings.append({"code": "older_snapshots_retained", "session": native, "count": retained})
                    for message in merged:
                        db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(message[k] for k in (
                            "id", "session_id", "ordinal", "native_id", "role", "kind", "text", "created_at", "source_path", "source_line", "parent_id", "media_json", "metadata_json")))
                    inserted_messages += len(merged)
                schema.rebuild_search(db)
                db.commit()
                counts = {"total_sessions": db.execute("SELECT count(*) FROM sessions").fetchone()[0], "total_messages": db.execute("SELECT count(*) FROM messages").fetchone()[0]}
            finally:
                db.close()
            report = {"sessions": len(sessions), "messages": inserted_messages, "warnings": warnings,
                      "unchanged": False, "provider": parsed.get("provider"), **counts}
            receipts.append({"id": receipt_id, "dataset": dataset, "requested_provider": provider, "provider": parsed.get("provider"),
                             "imported_at": now(), "parser_version": __version__, **details})
            write_json(staging / "imports.json", receipts)
            write_json(staging / "report.json", report)
            update("validate", 90)
            schema.validate_database(staging / "archive.sqlite3")
            store.publish(library_id, staging)
            update("complete", 100)
            return report
        finally:
            if staging.exists():
                shutil.rmtree(staging)
