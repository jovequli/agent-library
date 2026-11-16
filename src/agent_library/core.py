"""Independent libraries with immutable, atomically activated generations."""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import schema


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe_relative(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ValueError("Invalid relative archive path.")
    relative = Path(value)
    reserved = {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)), *("LPT" + str(i) for i in range(1, 10))}
    if (relative.is_absolute() or any(p in ("..", ".", "") for p in value.split("/")) or ":" in value
            or any(p.endswith((".", " ")) or p.split(".")[0].upper() in reserved for p in relative.parts)):
        raise ValueError("Archive path must stay inside its library.")
    result = root / relative
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError("Archive path escaped its library.")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Symlinks are not allowed inside an archive.")
    return result


class LibraryStore:
    def __init__(self, home: Path | str):
        self.home = Path(home).expanduser().resolve()
        self.libraries = self.home / "libraries"
        self.libraries.mkdir(parents=True, exist_ok=True)

    def root(self, library_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", library_id or ""):
            raise ValueError("Unknown library ID.")
        root = self.libraries / library_id
        if root.is_symlink():
            raise ValueError("Library roots cannot be symlinks.")
        return root

    def create(self, name: str) -> dict:
        name = str(name).strip()
        if not name or len(name) > 100:
            raise ValueError("Library name must contain 1 to 100 characters.")
        ident = uuid.uuid4().hex
        root = self.root(ident)
        root.mkdir(mode=0o700)
        metadata = {"id": ident, "name": name, "created_at": now(), "format_version": 1}
        try:
            write_json(root / "library.json", metadata)
            (root / "generations").mkdir()
            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=root / "generations"))
            db = schema.create_database(staging / "archive.sqlite3")
            db.close()
            (staging / "sources").mkdir()
            (staging / "media").mkdir()
            write_json(staging / "imports.json", [])
            write_json(staging / "report.json", {"sessions": 0, "messages": 0, "warnings": []})
            self.publish(ident, staging)
            return metadata
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise

    def list(self) -> list[dict]:
        result = []
        for path in sorted(self.libraries.iterdir()):
            if path.is_dir() and not path.is_symlink() and re.fullmatch(r"[0-9a-f]{32}", path.name):
                try:
                    result.append(self.get(path.name))
                except (OSError, ValueError, KeyError):
                    continue
        return sorted(result, key=lambda x: (x["created_at"], x["id"]))

    def get(self, library_id: str) -> dict:
        root = self.root(library_id)
        try:
            result = json.loads((root / "library.json").read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError("Unknown library ID.") from exc
        if result.get("id") != library_id or result.get("format_version") != 1:
            raise ValueError("Unsupported library metadata.")
        return result

    def generation(self, library_id: str) -> Path:
        self.get(library_id)
        root = self.root(library_id)
        value = (root / "CURRENT").read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise ValueError("Library has an invalid generation pointer.")
        result = safe_relative(root / "generations", value)
        if not result.is_dir():
            raise ValueError("Library active generation is missing.")
        return result

    def connect(self, library_id: str) -> sqlite3.Connection:
        path = self.generation(library_id) / "archive.sqlite3"
        db = schema.readonly(path)
        if db.execute("PRAGMA user_version").fetchone()[0] != schema.VERSION:
            db.close()
            raise ValueError("Unsupported schema; upgrade Agent Library before opening this library.")
        return db

    @contextlib.contextmanager
    def locked(self, library_id: str):
        self.get(library_id)
        db = sqlite3.connect(self.root(library_id) / ".write-lock.sqlite3", timeout=30)
        try:
            db.execute("BEGIN EXCLUSIVE")
            yield
        finally:
            db.rollback()
            db.close()

    def stage(self, library_id: str) -> Path:
        parent = self.root(library_id) / "generations"
        return Path(tempfile.mkdtemp(prefix=".staging-", dir=parent))

    def publish(self, library_id: str, staging: Path) -> None:
        root = self.root(library_id)
        parent = root / "generations"
        if staging.parent.resolve() != parent.resolve() or staging.is_symlink():
            raise ValueError("Generation must be staged inside its library.")
        schema.validate_database(staging / "archive.sqlite3")
        for path in staging.rglob("*"):
            if path.is_symlink():
                raise ValueError("Generation cannot contain symlinks.")
        ident = uuid.uuid4().hex
        os.replace(staging, parent / ident)
        atomic_write(root / "CURRENT", (ident + "\n").encode("ascii"))

    def doctor(self) -> dict:
        result = []
        for library in self.list():
            try:
                generation = self.generation(library["id"])
                schema.validate_database(generation / "archive.sqlite3")
                with contextlib.closing(self.connect(library["id"])) as db:
                    for row in db.execute("SELECT relative_path,size FROM media"):
                        media = safe_relative(generation, row["relative_path"])
                        if not media.is_file() or media.stat().st_size != row["size"]:
                            raise ValueError("A preserved media file is missing or has changed.")
                result.append({"id": library["id"], "name": library["name"], "ok": True})
            except (OSError, ValueError, sqlite3.Error) as exc:
                result.append({"id": library["id"], "name": library["name"], "ok": False, "error": str(exc)})
        return {"schema_version": schema.VERSION, "sqlite_version": sqlite3.sqlite_version,
                "libraries": result, "ok": all(item["ok"] for item in result)}
