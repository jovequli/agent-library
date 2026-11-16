"""Lossless migration of the original normalized conversation archive.

This adapter reads only explicitly selected local roots. It never imports Python
from an archive or executes historical content. Private sources, reports and
legacy generated artifacts remain in the destination data home.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3

from . import __version__, schema
from .core import now, safe_relative, write_json
from .ingest import image_type, stable_id

_REQUIRED = {
    "sessions": {"id", "source", "title", "created_at", "updated_at", "project", "kind", "parent_id", "source_paths", "message_count", "handoff_path", "transcript_path"},
    "messages": {"id", "session_id", "seq", "role", "kind", "text", "created_at", "uuid", "source_path", "line"},
}
_CACHE_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "cache", "caches"}
_PROVIDERS = {"chat": "claude", "design": "claude", "context": "claude", "claude_code": "claude_code", "desktop_code": "claude_code", "cowork": "cowork"}
_DATASET = "legacy-archive"
_CHUNK = 1024 * 1024


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _stat(path: Path) -> tuple:
    value = path.stat()
    return value.st_size, value.st_mtime_ns, value.st_ino


def _relative_source(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise ValueError("Legacy source path is not a valid relative path.")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Legacy source path escapes the selected source root.")
    result = root / relative
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Legacy source references a symlink.")
    if not result.resolve().is_relative_to(root):
        raise ValueError("Legacy source path escapes the selected source root.")
    return result


def _json(path: Path, default):
    if not path.exists():
        return default
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, type(default)):
        raise ValueError("Legacy metadata has an incompatible structure.")
    return value


def _validate_legacy(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA query_only=ON")
    for table, required in _REQUIRED.items():
        info = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (table,)).fetchone()
        if not info or info[0] != "table" or "VIRTUAL TABLE" in (info[1] or "").upper():
            raise ValueError("Legacy archive requires concrete sessions and messages tables.")
        present = {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}
        if not required <= present:
            raise ValueError("Legacy archive is missing required record fields.")
    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("Legacy archive database failed integrity validation.")
    if db.execute("SELECT 1 FROM messages m LEFT JOIN sessions s ON s.id=m.session_id WHERE s.id IS NULL LIMIT 1").fetchone():
        raise ValueError("Legacy archive contains orphaned messages.")
    if db.execute("SELECT 1 FROM messages GROUP BY session_id,seq HAVING count(*)>1 LIMIT 1").fetchone():
        raise ValueError("Legacy archive has ambiguous message sequence numbers.")


class _Snapshots:
    def __init__(self, source_root: Path, staging: Path, update):
        self.source_root, self.staging, self.update = source_root, staging, update
        self.files: dict[str, dict] = {}
        self.skipped = []
        self.source_map = {}
        self.records = []
        self.trees = []

    def add(self, path: Path, proposed: str) -> None:
        if path.is_symlink():
            self.skipped.append({"source_path": path.relative_to(self.source_root).as_posix(), "reason": "symlink"})
            return
        if not path.is_file():
            return
        if not path.resolve().is_relative_to(self.source_root):
            raise ValueError("Selected legacy files must stay inside the explicit source root.")
        relative = path.relative_to(self.source_root).as_posix()
        try:
            safe_relative(self.staging, proposed)
        except ValueError:
            extension = path.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,12}", path.suffix) else ".bin"
            proposed = "sources/legacy_mapped/" + hashlib.sha256(relative.encode()).hexdigest() + extension
        previous = self.files.get(relative)
        if previous and previous["destination"] != proposed:
            return
        self.files[relative] = {"path": path, "destination": proposed, "stat": _stat(path)}
        self.source_map[relative] = proposed

    def tree(self, root: Path, prefix: str) -> None:
        if not root.exists():
            return
        if root.is_symlink():
            self.skipped.append({"source_path": root.relative_to(self.source_root).as_posix(), "reason": "symlink"})
            return
        for current, directories, names in os.walk(root, followlinks=False):
            base = Path(current)
            keep = []
            for name in sorted(directories):
                path = base / name
                if name.lower() in _CACHE_DIRS or path.is_symlink():
                    self.skipped.append({"source_path": path.relative_to(self.source_root).as_posix(), "reason": "cache" if name.lower() in _CACHE_DIRS else "symlink"})
                else:
                    keep.append(name)
            directories[:] = keep
            for name in sorted(names):
                path = base / name
                self.add(path, prefix + "/" + path.relative_to(root).as_posix())
        self.trees.append((root, {relative for relative, item in self.files.items() if item["path"].is_relative_to(root)}))

    def copy(self, database_source: Path, database_snapshot: Path) -> None:
        total = sum(item["stat"][0] for item in self.files.values()) or 1
        copied = 0
        for relative, item in sorted(self.files.items()):
            path = item["path"]
            _relative_source(self.source_root, relative)
            if _stat(path) != item["stat"]:
                raise ValueError("Legacy source changed while preparing its snapshot.")
            target = safe_relative(self.staging, item["destination"])
            target.parent.mkdir(parents=True, exist_ok=True)
            if path == database_source:
                original_digest = _sha(path)
                # SQLite's online backup API produces a coherent standalone
                # snapshot; its bytes need not equal a WAL-mode original file.
                shutil.copyfile(database_snapshot, target)
                digest = _sha(target)
            else:
                hasher = hashlib.sha256()
                with path.open("rb") as source, target.open("xb") as destination:
                    for block in iter(lambda: source.read(_CHUNK), b""):
                        hasher.update(block)
                        destination.write(block)
                digest = hasher.hexdigest()
                original_digest = digest
                if _sha(target) != digest:
                    raise ValueError("A legacy source copy failed its checksum.")
            if _stat(path) != item["stat"]:
                raise ValueError("Legacy source changed while copying its snapshot.")
            item["sha256"] = original_digest
            record = {"source_path": relative, "destination": item["destination"], "size": target.stat().st_size, "sha256": digest}
            if original_digest != digest:
                record["original_sha256"] = original_digest
            self.records.append(record)
            copied += item["stat"][0]
            self.update("snapshot", 10 + int(copied * 45 / total))

    def verify_unchanged(self) -> None:
        for root, expected in self.trees:
            actual = set()
            for current, directories, names in os.walk(root, followlinks=False):
                base = Path(current)
                directories[:] = [name for name in directories if name.lower() not in _CACHE_DIRS and not (base / name).is_symlink()]
                actual.update((base / name).relative_to(self.source_root).as_posix() for name in names if (base / name).is_file() and not (base / name).is_symlink())
            if actual != expected:
                raise ValueError("The original archive file inventory changed during migration.")
        total = sum(item["stat"][0] for item in self.files.values()) or 1
        checked = 0
        for item in self.files.values():
            path = item["path"]
            if path.is_symlink() or not path.is_file() or _stat(path) != item["stat"] or _sha(path) != item["sha256"]:
                raise ValueError("The original archive changed during migration; no new library was activated.")
            checked += item["stat"][0]
            self.update("verify_source", 94 + int(checked * 4 / total))


def _payload(digest, session_id, row):
    value = [session_id, row["id"], row["seq"], row["role"], row["kind"], row["text"], row["created_at"], row["uuid"], row["source_path"], row["line"]]
    digest.update(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")


def migrate_legacy_archive(store, archive_root: Path, source_root: Path, name="Claude Database", progress=None) -> dict:
    """Copy an explicitly selected legacy archive into a new independent library.

    No source file is rewritten. All normalized records, raw backup directories,
    media and generated legacy handoff evidence are preserved in private storage.
    """
    last_progress = None

    def update(phase, percent):
        nonlocal last_progress
        value = (phase, int(percent))
        if progress and value != last_progress:
            progress(*value)
        last_progress = value

    archive_root = Path(archive_root).expanduser()
    source_root = Path(source_root).expanduser()
    if archive_root.is_symlink() or source_root.is_symlink():
        raise ValueError("Select original legacy directories, not symlinks.")
    archive_root, source_root = archive_root.resolve(strict=True), source_root.resolve(strict=True)
    if not (archive_root / "data/archive.sqlite").is_file() and (archive_root / "record_library/data/archive.sqlite").is_file():
        archive_root /= "record_library"
    if not archive_root.is_relative_to(source_root):
        raise ValueError("Legacy library must be inside the explicitly selected source root.")
    if store.home.is_relative_to(source_root) or source_root.is_relative_to(store.home):
        raise ValueError("The private destination and original source roots must be separate.")
    database_source = _relative_source(source_root, (archive_root / "data/archive.sqlite").relative_to(source_root).as_posix())
    if not database_source.is_file():
        raise ValueError("Legacy data/archive.sqlite was not found.")
    update("inspect", 1)
    # Validate before creating a destination, then use a coherent snapshot for
    # all metadata and message reads. No legacy modules are imported.
    with closing(schema.readonly(database_source)) as original:
        _validate_legacy(original)
    library = store.create(name)
    staging = store.stage(library["id"])
    snapshot_db = staging / ".legacy-snapshot.sqlite"
    try:
        with store.locked(library["id"]):
            original_stat = _stat(database_source)
            original_hash = _sha(database_source)
            with closing(schema.readonly(database_source)) as original, closing(sqlite3.connect(snapshot_db)) as copy:
                original.backup(copy)
            if _stat(database_source) != original_stat or _sha(database_source) != original_hash:
                raise ValueError("Legacy database changed while creating its snapshot.")
            update("snapshot", 10)
            sources = _Snapshots(source_root, staging, update)
            for folder in ("extracted", "local_backup"):
                sources.tree(source_root / folder, "sources/legacy_raw/" + folder)
            sources.tree(archive_root / "data", "sources/legacy_archive/data")
            sources.tree(archive_root / "transcripts", "sources/legacy_archive/transcripts")
            sources.tree(archive_root / "handoffs", "sources/legacy_archive/handoffs")
            sources.tree(archive_root / "media", "sources/legacy_archive/media")
            for path in sorted(archive_root.iterdir()):
                if path.is_file() and path.suffix.lower() in {".md", ".txt"}:
                    sources.add(path, "sources/legacy_archive/" + path.name)
            with closing(schema.readonly(snapshot_db)) as legacy:
                _validate_legacy(legacy)
                sessions = [dict(row) for row in legacy.execute("SELECT * FROM sessions ORDER BY id")]
                by_id = {row["id"]: row for row in sessions}
                counts = {"sessions": len(sessions), "messages": legacy.execute("SELECT count(*) FROM messages").fetchone()[0]}
                if not sessions:
                    raise ValueError("Legacy archive contains no sessions.")
                warnings = []
                for row in legacy.execute("SELECT DISTINCT source_path FROM messages WHERE source_path != ''"):
                    relative = row[0]
                    path = _relative_source(source_root, relative)
                    if path.is_file() and relative not in sources.source_map:
                        sources.add(path, "sources/legacy_reference/" + hashlib.sha256(relative.encode()).hexdigest() + (path.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,12}", path.suffix) else ".bin"))
                    elif not path.is_file():
                        warnings.append({"code": "legacy_source_missing", "source_path": relative})
                initial_media = _json(archive_root / "data/media_index.json", {})
                initial_assets = initial_media.get("assets", {})
                if not isinstance(initial_assets, dict):
                    raise ValueError("Legacy media assets are invalid.")
                for asset in initial_assets.values():
                    if not isinstance(asset, dict):
                        raise ValueError("Legacy media assets are invalid.")
                    relative = asset.get("path", "")
                    path = _relative_source(source_root, relative)
                    if not path.is_file():
                        raise ValueError("Legacy media is missing.")
                    if relative not in sources.source_map:
                        sources.add(path, "sources/legacy_reference/" + hashlib.sha256(relative.encode()).hexdigest() + (path.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,12}", path.suffix) else ".bin"))
                for session in sessions:
                    for relative in (session["handoff_path"], session["transcript_path"], (Path(session["handoff_path"]).parent / "用户要求.md").as_posix() if session["handoff_path"] else ""):
                        if not relative or relative in sources.source_map:
                            continue
                        path = _relative_source(source_root, relative)
                        if path.is_file():
                            sources.add(path, "sources/legacy_reference/" + hashlib.sha256(relative.encode()).hexdigest() + (path.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,12}", path.suffix) else ".bin"))
                sources.copy(database_source, snapshot_db)
                source_db_relative = sources.source_map[database_source.relative_to(source_root).as_posix()]
                def copied_metadata(filename):
                    relative = (archive_root / "data" / filename).relative_to(source_root).as_posix()
                    mapped = sources.source_map.get(relative)
                    return _json(staging / mapped, {}) if mapped else {}

                catalog = copied_metadata("session_catalog.json").get("sessions", {})
                if not isinstance(catalog, dict):
                    raise ValueError("Legacy catalog sessions are invalid.")
                media_index = copied_metadata("media_index.json")
                assets = media_index.get("assets", {})
                occurrences = media_index.get("sessions", {})
                missing = media_index.get("missing", {})
                bindings = media_index.get("message_bindings", {})
                if not all(isinstance(value, dict) for value in (assets, occurrences, missing, bindings)):
                    raise ValueError("Legacy media index is invalid.")
                update("media", 57)
                (staging / "media").mkdir()
                media_records = {}
                for image_id, asset in assets.items():
                    if not isinstance(asset, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(image_id)):
                        raise ValueError("Legacy media has an invalid asset identity.")
                    mapped = sources.source_map.get(asset.get("path", ""))
                    if not mapped:
                        raise ValueError("Legacy media has no preserved source snapshot.")
                    path = safe_relative(staging, mapped)
                    if not path.is_file() or _sha(path) != image_id:
                        raise ValueError("Legacy media is missing or its checksum has changed.")
                    with path.open("rb") as stream:
                        magic = image_type(stream.read(64))
                    if not magic:
                        raise ValueError("Legacy media is not a supported preserved raster image.")
                    relative = "media/" + image_id + magic[1]
                    shutil.copyfile(path, staging / relative)
                    if _sha(staging / relative) != image_id:
                        raise ValueError("A migrated media file failed checksum verification.")
                    media_records[image_id] = {"id": image_id, "relative_path": relative, "mime": magic[0], "name": Path(asset["path"]).name, "size": path.stat().st_size}
                message_media = defaultdict(list)
                session_media = defaultdict(list)
                message_missing = defaultdict(list)
                exact_count = attachment_count = missing_count = 0
                for sid, items in occurrences.items():
                    if sid not in by_id or not isinstance(items, list):
                        raise ValueError("Legacy media references an unknown session.")
                    for item in items:
                        if not isinstance(item, dict) or item.get("id") not in media_records:
                            raise ValueError("Legacy media references an unknown asset.")
                        value = dict(item)
                        mid = item.get("message_id")
                        if mid is not None and item.get("display") == "message":
                            row = legacy.execute("SELECT id,session_id,uuid,source_path,line,role,kind FROM messages WHERE id=?", (mid,)).fetchone()
                            if row is None or row["session_id"] != sid:
                                raise ValueError("Legacy image is bound to an invalid message.")
                            expected = bindings.get(str(mid))
                            if expected and any(row[key] != expected[key] for key in ("id", "session_id", "uuid", "source_path", "line", "role", "kind") if key in expected):
                                raise ValueError("Legacy image binding does not match its preserved message.")
                            message_media[mid].append(item["id"])
                            exact_count += 1
                        else:
                            value["original_source_path"] = value.get("source_path", "")
                            value["source_path"] = sources.source_map.get(value["original_source_path"], media_records[item["id"]]["relative_path"])
                            session_media[sid].append(value)
                            attachment_count += 1
                for sid, items in missing.items():
                    if sid not in by_id or not isinstance(items, list):
                        raise ValueError("Missing legacy media references an unknown session.")
                    for item in items:
                        if not isinstance(item, dict):
                            raise ValueError("Missing legacy media metadata is invalid.")
                        mid = item.get("message_id")
                        if mid is not None:
                            row = legacy.execute("SELECT session_id FROM messages WHERE id=?", (mid,)).fetchone()
                            if row is None or row[0] != sid:
                                raise ValueError("Missing legacy media has an invalid message binding.")
                            message_missing[mid].append(item)
                        missing_count += 1
                update("import", 65)
                database = schema.create_database(staging / "archive.sqlite3")
                provider_counts, kind_counts = Counter(), Counter()
                digest = hashlib.sha256()
                mapped_ids = {sid: stable_id(_DATASET, _PROVIDERS.get(row["source"], row["source"]), sid) for sid, row in by_id.items()}
                try:
                    for asset in media_records.values():
                        database.execute("INSERT INTO media VALUES (?,?,?,?,?)", tuple(asset[k] for k in ("id", "relative_path", "mime", "name", "size")))
                    for index, session in enumerate(sessions):
                        sid, source = session["id"], session["source"]
                        classification = catalog.get(sid, {})
                        if not isinstance(classification, dict):
                            raise ValueError("Legacy session catalog entry is invalid.")
                        original_project = session["project"] or ""
                        if classification:
                            project = str(classification.get("display_project") or "") if classification.get("catalog_group_type") == "project" else ""
                        else:
                            project = original_project if not original_project.startswith("/sessions/") else ""
                        artifacts = {}
                        for key, original in (("handoff", session["handoff_path"]), ("transcript", session["transcript_path"])):
                            if original in sources.source_map:
                                artifacts[key] = sources.source_map[original]
                        if session["handoff_path"]:
                            requirement = (Path(session["handoff_path"]).parent / "用户要求.md").as_posix()
                            if requirement in sources.source_map:
                                artifacts["requirements"] = sources.source_map[requirement]
                        metadata = {"legacy_session_id": sid, "legacy_source": source, "legacy_kind": session["kind"], "legacy_parent_id": session["parent_id"],
                            "legacy_project": original_project, "legacy_catalog": classification,
                            "legacy_source_paths": json.loads(session["source_paths"] or "[]"), "legacy_artifacts": artifacts,
                            "task_paths": classification.get("task_paths", []), "execution_cwd": classification.get("execution_cwd", original_project),
                            "session_attachments": session_media.get(sid, []), "missing_media_count": len(missing.get(sid, [])),
                            "missing_media": [item for item in missing.get(sid, []) if item.get("message_id") is None]}
                        provider = _PROVIDERS.get(source, source)
                        database.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)", (mapped_ids[sid], sid, _DATASET, provider,
                            session["title"] or "", project, session["created_at"] or "", session["updated_at"] or "", session["kind"] or "main",
                            mapped_ids.get(session["parent_id"]), json.dumps(metadata, ensure_ascii=False)))
                        provider_counts[provider] += 1
                        kind_counts[session["kind"] or "main"] += 1
                        for message in legacy.execute("SELECT * FROM messages WHERE session_id=? ORDER BY seq,id", (sid,)):
                            if message["role"] is None or message["kind"] is None or message["text"] is None:
                                raise ValueError("Legacy message has missing required text/role/kind fields.")
                            original = message["source_path"] or ""
                            copied_source = sources.source_map.get(original, source_db_relative if original else "")
                            message_metadata = {"legacy_message_id": message["id"], "legacy_seq": message["seq"], "legacy_uuid": message["uuid"],
                                "legacy_source_path": original, "legacy_line": message["line"], "missing_media": message_missing.get(message["id"], [])}
                            if original and original not in sources.source_map:
                                message_metadata["original_source_missing"] = True
                            mids = list(dict.fromkeys(message_media.get(message["id"], [])))
                            if mids:
                                aliases = {}
                                for item in occurrences.get(sid, []):
                                    if item.get("message_id") == message["id"] and item.get("id") in mids:
                                        for field in ("source_url", "source_path"):
                                            if item.get(field):
                                                aliases[item[field]] = item["id"]
                                message_metadata["image_references"] = aliases
                            database.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                                stable_id(mapped_ids[sid], "legacy-row", str(message["id"])), mapped_ids[sid], message["seq"], "legacy-row:" + str(message["id"]),
                                message["role"], message["kind"], message["text"], message["created_at"] or "", copied_source, message["line"] or 0,
                                None, json.dumps(mids), json.dumps(message_metadata, ensure_ascii=False)))
                            _payload(digest, sid, message)
                        update("import", 65 + int((index + 1) * 20 / len(sessions)))
                    update("index", 86)
                    schema.rebuild_search(database)
                    database.commit()
                    if database.execute("SELECT count(*) FROM sessions").fetchone()[0] != counts["sessions"] or database.execute("SELECT count(*) FROM messages").fetchone()[0] != counts["messages"]:
                        raise ValueError("Legacy migration record counts did not match.")
                    actual = hashlib.sha256()
                    database.row_factory = sqlite3.Row
                    for row in database.execute("SELECT s.native_id,m.* FROM messages m JOIN sessions s ON s.id=m.session_id ORDER BY s.native_id,m.ordinal"):
                        meta = json.loads(row["metadata_json"])
                        original = {"id": meta["legacy_message_id"], "seq": row["ordinal"], "role": row["role"], "kind": row["kind"], "text": row["text"],
                            "created_at": row["created_at"], "uuid": meta["legacy_uuid"], "source_path": meta["legacy_source_path"], "line": meta["legacy_line"]}
                        _payload(actual, row["native_id"], original)
                    if actual.hexdigest() != digest.hexdigest():
                        raise ValueError("Legacy message content did not pass exact verification.")
                finally:
                    database.close()
            # Windows requires the snapshot reader to close before deletion or
            # the generation directory is renamed during publication.
            snapshot_db.unlink()
            update("validate", 91)
            schema.validate_database(staging / "archive.sqlite3")
            sources.verify_unchanged()
            source_manifest = {"format": "agent-library.legacy-source-manifest", "version": 1, "created_at": now(),
                "files": sources.records, "skipped": sources.skipped, "unchanged_source": True}
            manifest_relative = "sources/legacy_source_manifest.json"
            write_json(staging / manifest_relative, source_manifest)
            receipt = {"id": stable_id(original_hash, digest.hexdigest()), "dataset": _DATASET, "provider": "mixed", "requested_provider": "legacy_archive",
                "parser_version": __version__, "imported_at": now(), "sha256": original_hash, "payload_sha256": digest.hexdigest(), "source_manifest": manifest_relative}
            write_json(staging / "imports.json", [receipt])
            report = {**counts, "media": len(media_records), "exact_media_occurrences": exact_count,
                "session_attachments": attachment_count, "missing_media_references": missing_count,
                "providers": dict(provider_counts), "kinds": dict(kind_counts), "source_files": len(sources.records),
                "source_bytes": sum(item["size"] for item in sources.records), "source_manifest": manifest_relative,
                "payload_sha256": digest.hexdigest(), "original_database_sha256": original_hash, "warnings": warnings,
                "verified": True, "unchanged_source": True}
            write_json(staging / "report.json", report)
            update("publish", 99)
            store.publish(library["id"], staging)
            active = store.generation(library["id"])
            result = {**library, "library_id": library["id"], **report, "report_path": str(active / "report.json")}
            update("complete", 100)
            return result
    except BaseException:
        shutil.rmtree(store.root(library["id"]), ignore_errors=True)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
