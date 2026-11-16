"""Verified, portable library backups. No network access or in-place restore.

A backup is private user data, not a source-code release archive. Only the active
immutable generation, handoff annotations, and library metadata are included.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile

FORMAT = "agent-library-backup"
VERSION = 1
MAX_FILES = 200_000
MAX_BYTES = 100 * 1024**3
MAX_SINGLE_FILE = 16 * 1024**3
MAX_MANIFEST = 32 * 1024**2
CHUNK = 1024 * 1024


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def _safe_name(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ValueError("Backup contains an unsafe path")
    parts = value.split("/")
    if value.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise ValueError("Backup contains an unsafe path")
    reserved = {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)), *("LPT" + str(i) for i in range(1, 10))}
    if any(":" in part or part.endswith((".", " ")) or part.split(".")[0].upper() in reserved for part in parts):
        raise ValueError("Backup contains a path that is unsafe on supported platforms")
    if PurePosixPath(value).as_posix() != value:
        raise ValueError("Backup contains an unsafe path")
    return value


def _validate_db(path: Path):
    # Keep one authoritative schema validator. It also rejects newer/older
    # database schemas rather than guessing a migration during restore.
    from .schema import validate_database
    validate_database(path)


def _files(root: Path, prefix: str):
    if root.is_symlink():
        raise ValueError("Library backup cannot follow symlinks")
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Library backup cannot include symlinks")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Library backup only supports regular files")
        yield prefix + "/" + path.relative_to(root).as_posix(), path


def _write_file(archive, name, path):
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as source, archive.open(name, "w", force_zip64=True) as dest:
        while block := source.read(CHUNK):
            size += len(block)
            if size > MAX_SINGLE_FILE:
                raise ValueError("A library file exceeds the backup safety limit")
            sha.update(block)
            dest.write(block)
    return {"path": name, "size": size, "sha256": sha.hexdigest()}


def backup_library(store, library_id: str, output: Path) -> dict:
    """Write a new ZIP with per-file SHA-256 checksums; never replace a file."""
    output = Path(output).expanduser().absolute()
    library = store.get(library_id)
    library_root = (store.home / "libraries" / library_id).resolve()
    # An output inside any library could be swept into a later generation or
    # backup and could overwrite a source selected by the user.
    libraries_root = (store.home / "libraries").resolve()
    if output.resolve().is_relative_to(libraries_root):
        raise ValueError("Choose a backup destination outside the libraries directory")
    if output.exists() or output.is_symlink():
        raise FileExistsError("Backup destination already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=".agent-library-backup-", suffix=".part", dir=output.parent)
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        with store.locked(library_id):
            generation = store.generation(library_id)
            _validate_db(generation / "archive.sqlite3")
            entries = list(_files(generation, "generation"))
            entries.extend(_files(library_root / "annotations", "annotations"))
            if len(entries) > MAX_FILES - 2:
                raise ValueError("Library has too many files for one backup")
            if sum(p.stat().st_size for _, p in entries) > MAX_BYTES:
                raise ValueError("Library exceeds the backup safety limit")
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
                metadata = _json({key: library[key] for key in ("id", "name", "created_at") if key in library})
                archive.writestr("library.json", metadata)
                records = [{"path": "library.json", "size": len(metadata), "sha256": hashlib.sha256(metadata).hexdigest()}]
                for name, path in entries:
                    records.append(_write_file(archive, _safe_name(name), path))
                manifest = {
                    "format": FORMAT, "version": VERSION, "schema_version": 1,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "library": json.loads(metadata), "files": records,
                }
                archive.writestr("manifest.json", _json(manifest))
        # Read back the completed file, including database consistency, before
        # presenting it to the caller as a usable backup.
        checked = verify_backup(temporary)
        if output.exists() or output.is_symlink():
            raise FileExistsError("Backup destination already exists")
        try:
            # Atomic and exclusive when the filesystem supports hard links.
            os.link(temporary, output)
        except FileExistsError:
            raise
        except OSError:
            # External FAT/exFAT volumes may not support hard links. Exclusive
            # creation still prevents overwriting an existing or concurrent file.
            created = False
            try:
                fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
                with os.fdopen(fd, "wb") as dest, temporary.open("rb") as source:
                    shutil.copyfileobj(source, dest, CHUNK)
                    dest.flush()
                    os.fsync(dest.fileno())
            except BaseException:
                if created:
                    output.unlink(missing_ok=True)
                raise
        digest = hashlib.sha256()
        with output.open("rb") as source:
            while block := source.read(CHUNK):
                digest.update(block)
        return {"path": str(output), "size": output.stat().st_size, "sha256": digest.hexdigest(), "files": checked["files"], "verified": True}
    finally:
        temporary.unlink(missing_ok=True)


def _manifest(archive):
    infos = archive.infolist()
    if not infos or len(infos) > MAX_FILES:
        raise ValueError("Backup has an invalid number of files")
    names = set()
    folded = set()
    total = 0
    for info in infos:
        name = _safe_name(info.filename)
        if name in names or name.casefold() in folded:
            raise ValueError("Backup contains duplicate or ambiguous paths")
        names.add(name)
        folded.add(name.casefold())
        mode = (info.external_attr >> 16) & 0xFFFF
        if info.is_dir() or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
            raise ValueError("Backup contains an unsupported file type")
        if info.flag_bits & 1:
            raise ValueError("Encrypted ZIP backups are not supported")
        if name != "manifest.json" and name != "library.json" and not name.startswith(("generation/", "annotations/")):
            raise ValueError("Backup contains unexpected top-level files")
        total += info.file_size
        if info.file_size > MAX_SINGLE_FILE or total > MAX_BYTES:
            raise ValueError("Backup exceeds the extraction safety limit")
        if info.file_size > 100 * 1024**2 and info.file_size > max(info.compress_size, 1) * 10000:
            raise ValueError("Backup compression ratio exceeds the safety limit")
    required = {"manifest.json", "library.json", "generation/archive.sqlite3", "generation/imports.json", "generation/report.json"}
    if not required <= names:
        raise ValueError("Backup is missing required files")
    if archive.getinfo("manifest.json").file_size > MAX_MANIFEST:
        raise ValueError("Backup manifest is too large")
    try:
        manifest = json.loads(archive.read("manifest.json"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Backup manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
        raise ValueError("Unsupported backup format or version")
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported library schema version")
    records = manifest.get("files")
    if not isinstance(records, list) or len(records) != len(names) - 1:
        raise ValueError("Backup manifest does not match the archive")
    expected = {}
    for row in records:
        if not isinstance(row, dict):
            raise ValueError("Backup manifest contains an invalid file record")
        name = _safe_name(row.get("path"))
        if name == "manifest.json" or name in expected or name not in names:
            raise ValueError("Backup manifest contains duplicate or missing files")
        digest = row.get("sha256")
        size = row.get("size")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Backup manifest contains an invalid checksum")
        if type(size) is not int or size < 0 or size != archive.getinfo(name).file_size:
            raise ValueError("Backup manifest contains an invalid size")
        expected[name] = row
    if set(expected) != names - {"manifest.json"}:
        raise ValueError("Backup manifest does not cover every file")
    return manifest, expected


def _validate_references(generation: Path, records: dict):
    """A checksummed archive still needs its referenced files to be complete."""
    from .schema import readonly
    try:
        receipts = json.loads((generation / "imports.json").read_text("utf-8"))
        report = json.loads((generation / "report.json").read_text("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Backup import metadata is invalid") from exc
    if not isinstance(receipts, list) or not isinstance(report, dict):
        raise ValueError("Backup import metadata is invalid")
    with closing(readonly(generation / "archive.sqlite3")) as db:
        for row in db.execute("SELECT relative_path,size FROM media"):
            relative = _safe_name(row["relative_path"])
            entry = records.get("generation/" + relative)
            if not relative.startswith("media/") or not entry or entry["size"] != row["size"]:
                raise ValueError("Backup is missing a referenced media file")
        for row in db.execute("SELECT DISTINCT source_path FROM messages WHERE source_path != ''"):
            relative = _safe_name(row["source_path"])
            if not relative.startswith("sources/") or "generation/" + relative not in records:
                raise ValueError("Backup is missing a referenced original source file")


def _extract_checked(archive_path: Path, destination: Path, keep_all: bool = True) -> dict:
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            manifest, records = _manifest(archive)
            for name, row in records.items():
                target = destination.joinpath(*name.split("/"))
                preserve = keep_all or name in ("library.json", "generation/archive.sqlite3", "generation/imports.json", "generation/report.json")
                if preserve:
                    target.parent.mkdir(parents=True, exist_ok=True)
                digest, count = hashlib.sha256(), 0
                with archive.open(name) as source:
                    output = target.open("xb") if preserve else None
                    try:
                        while block := source.read(CHUNK):
                            count += len(block)
                            if count > row["size"]:
                                raise ValueError("Backup file is larger than its manifest entry")
                            digest.update(block)
                            if output is not None:
                                output.write(block)
                    finally:
                        if output is not None:
                            output.close()
                if count != row["size"] or digest.hexdigest() != row["sha256"]:
                    raise ValueError("Backup checksum verification failed")
            try:
                metadata = json.loads((destination / "library.json").read_text("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Backup library metadata is invalid") from exc
            if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not metadata["name"].strip():
                raise ValueError("Backup library metadata is invalid")
            if metadata != manifest.get("library"):
                raise ValueError("Backup library metadata does not match its manifest")
            _validate_db(destination / "generation" / "archive.sqlite3")
            _validate_references(destination / "generation", records)
            return {"format": FORMAT, "version": VERSION, "library": metadata, "files": len(records), "bytes": sum(x["size"] for x in records.values()), "verified": True}
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, RuntimeError, NotImplementedError, sqlite3.Error) as exc:
        raise ValueError("Backup is corrupt or uses an unsupported ZIP feature") from exc


def verify_backup(archive: Path) -> dict:
    """Verify every file, ZIP CRC, manifest, format version, and database."""
    with tempfile.TemporaryDirectory(prefix="agent-library-verify-") as temporary:
        return _extract_checked(Path(archive).expanduser(), Path(temporary), keep_all=False)


def restore_library(store, archive: Path, name: str | None = None) -> dict:
    """Restore a verified archive into a new independent library, atomically."""
    if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 100):
        raise ValueError("Choose a library name between 1 and 100 characters")
    store.home.mkdir(parents=True, exist_ok=True)
    library = None
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=store.home) as temporary:
        extracted = Path(temporary)
        checked = _extract_checked(Path(archive).expanduser(), extracted)
        try:
            restored_name = name.strip() if name else checked["library"]["name"] + " (restored)"
            library = store.create(restored_name[:100])
            root = store.home / "libraries" / library["id"]
            with store.locked(library["id"]):
                staging = root / "generations" / (".restore-" + uuid.uuid4().hex)
                staging.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(extracted / "generation"), staging)
                annotations = extracted / "annotations"
                if annotations.exists():
                    shutil.copytree(annotations, root / "annotations", dirs_exist_ok=True)
                store.publish(library["id"], staging)
            result = dict(store.get(library["id"]))
            result.update({"restored": True, "verified": True, "files": checked["files"], "original_library_id": checked["library"].get("id")})
            return result
        except BaseException:
            if library is not None:
                shutil.rmtree(store.home / "libraries" / library["id"], ignore_errors=True)
            raise
