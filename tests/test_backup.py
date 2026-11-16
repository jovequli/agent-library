"""Backup tests use only generated empty libraries and fictional files."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from agent_library.backup import backup_library, restore_library, verify_backup
from agent_library.core import LibraryStore


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.store = LibraryStore(self.base / "home")
        self.library = self.store.create("Fictional collection")
        self.id = self.library["id"]
        self.generation = self.store.generation(self.id)
        (self.generation / "sources" / "sample").mkdir(parents=True, exist_ok=True)
        (self.generation / "sources" / "sample" / "conversation.json").write_text('{"fictional": true}', "utf-8")
        (self.generation / "media").mkdir(exist_ok=True)
        (self.generation / "media" / "sample.png").write_bytes(b"fictional image bytes")
        self.root = self.store.home / "libraries" / self.id
        (self.root / "annotations").mkdir(exist_ok=True)
        (self.root / "annotations" / "sample.json").write_text('{"goal": "Fictional goal"}', "utf-8")
        self.backup = self.base / "backup.zip"

    def make_backup(self):
        return backup_library(self.store, self.id, self.backup)

    def rewrite(self, transform):
        with zipfile.ZipFile(self.backup) as archive:
            payload = {info.filename: archive.read(info.filename) for info in archive.infolist()}
        transform(payload)
        result = self.base / "changed.zip"
        with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in payload.items():
                archive.writestr(name, data)
        return result

    def test_roundtrip_is_independent_and_preserves_sources_media_notes(self):
        result = self.make_backup()
        self.assertTrue(result["verified"])
        self.assertEqual(result["size"], self.backup.stat().st_size)
        self.assertEqual(result["sha256"], hashlib.sha256(self.backup.read_bytes()).hexdigest())
        restored = restore_library(self.store, self.backup, "A second collection")
        self.assertNotEqual(restored["id"], self.id)
        self.assertEqual(restored["name"], "A second collection")
        new = self.store.generation(restored["id"])
        self.assertEqual((new / "sources/sample/conversation.json").read_bytes(), b'{"fictional": true}')
        self.assertEqual((new / "media/sample.png").read_bytes(), b"fictional image bytes")
        note = self.store.home / "libraries" / restored["id"] / "annotations/sample.json"
        self.assertEqual(json.loads(note.read_text())["goal"], "Fictional goal")
        self.assertEqual(self.store.generation(self.id), self.generation)
        self.assertEqual(len(self.store.list()), 2)

    def test_restore_in_different_home_requires_no_original_directory(self):
        self.make_backup()
        other_store = LibraryStore(self.base / "other-home")
        restored = restore_library(other_store, self.backup)
        self.assertTrue((other_store.generation(restored["id"]) / "sources/sample/conversation.json").exists())

    def test_only_active_generation_is_backed_up(self):
        stale = self.root / "generations" / "stale-fictional"
        stale.mkdir()
        (stale / "not-active.txt").write_text("Not the active snapshot")
        self.make_backup()
        with zipfile.ZipFile(self.backup) as archive:
            self.assertFalse(any("not-active" in name or "stale-fictional" in name for name in archive.namelist()))

    def test_existing_output_and_inside_library_are_rejected(self):
        self.make_backup()
        original = self.backup.read_bytes()
        with self.assertRaises(FileExistsError):
            self.make_backup()
        self.assertEqual(self.backup.read_bytes(), original)
        with self.assertRaises(ValueError):
            backup_library(self.store, self.id, self.generation / "bad.zip")

    def test_checksum_mismatch_fails_without_creating_library(self):
        self.make_backup()
        changed = self.rewrite(lambda payload: payload.__setitem__("generation/media/sample.png", b"tampered image bytes"))
        with self.assertRaises(ValueError):
            restore_library(self.store, changed)
        self.assertEqual(len(self.store.list()), 1)
        self.assertFalse(any(self.store.home.glob(".restore-*")))

    def test_unsupported_manifest_version_rejected(self):
        self.make_backup()
        def change(payload):
            manifest = json.loads(payload["manifest.json"])
            manifest["version"] = 999
            payload["manifest.json"] = json.dumps(manifest).encode()
        with self.assertRaisesRegex(ValueError, "version"):
            verify_backup(self.rewrite(change))

    def test_unsupported_database_schema_rejected_even_with_updated_checksum(self):
        self.make_backup()
        import sqlite3
        db = self.base / "future.sqlite3"
        db.write_bytes((self.generation / "archive.sqlite3").read_bytes())
        connection = sqlite3.connect(db)
        try:
            connection.execute("PRAGMA user_version=999")
        finally:
            connection.close()
        def change(payload):
            data = db.read_bytes()
            path = "generation/archive.sqlite3"
            payload[path] = data
            manifest = json.loads(payload["manifest.json"])
            record = next(row for row in manifest["files"] if row["path"] == path)
            record.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
            payload["manifest.json"] = json.dumps(manifest).encode()
        with self.assertRaises(ValueError):
            verify_backup(self.rewrite(change))

    def test_traversal_and_backslash_paths_rejected(self):
        self.make_backup()
        for name in ("generation/../../escape", "generation/../escape", "/absolute", "generation\\escape", "generation/C:/escape"):
            with self.subTest(name=name):
                changed = self.rewrite(lambda payload: payload.__setitem__(name, b"untrusted"))
                with self.assertRaises(ValueError):
                    restore_library(self.store, changed)
        self.assertFalse((self.base / "escape").exists())

    def test_duplicate_members_and_symlinks_rejected(self):
        self.make_backup()
        duplicated = self.base / "duplicate.zip"
        duplicated.write_bytes(self.backup.read_bytes())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(duplicated, "a") as archive:
                archive.writestr("library.json", b"{}")
        with self.assertRaises(ValueError):
            verify_backup(duplicated)
        linked = self.base / "linked.zip"
        linked.write_bytes(self.backup.read_bytes())
        with zipfile.ZipFile(linked, "a") as archive:
            member = zipfile.ZipInfo("generation/link")
            member.create_system = 3
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(member, "../../outside")
        with self.assertRaises(ValueError):
            verify_backup(linked)

    def test_unlisted_payload_and_size_limit_rejected(self):
        self.make_backup()
        changed = self.rewrite(lambda payload: payload.__setitem__("generation/unlisted.txt", b"unknown"))
        with self.assertRaises(ValueError):
            verify_backup(changed)
        with patch("agent_library.backup.MAX_BYTES", 100):
            with self.assertRaises(ValueError):
                verify_backup(self.backup)

    def test_missing_referenced_media_rejected(self):
        import sqlite3
        db = sqlite3.connect(self.generation / "archive.sqlite3")
        try:
            db.execute("INSERT INTO media VALUES (?,?,?,?,?)", ("fictional-missing", "media/missing.png", "image/png", "missing.png", 12))
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(ValueError, "referenced media"):
            self.make_backup()
        self.assertFalse(self.backup.exists())

    def test_bad_zip_leaves_no_library_or_partial_output(self):
        self.backup.write_bytes(b"not a zip file")
        with self.assertRaises(ValueError):
            restore_library(self.store, self.backup)
        self.assertEqual(len(self.store.list()), 1)
        self.assertFalse(list(self.store.home.glob(".restore-*")))
        self.assertFalse(list(self.base.glob("*.part")))


    def test_backup_on_filesystem_without_hardlinks(self):
        with patch("agent_library.backup.os.link", side_effect=OSError("No hard links")):
            result = self.make_backup()
        self.assertTrue(result["verified"])
        self.assertTrue(verify_backup(self.backup)["verified"])

    def test_cross_platform_device_paths_rejected(self):
        self.make_backup()
        for name in ("generation/CON", "generation/com1.txt", "generation/name.", "generation/name "):
            with self.subTest(name=name):
                changed = self.rewrite(lambda payload: payload.__setitem__(name, b"invalid"))
                with self.assertRaises(ValueError):
                    verify_backup(changed)

    def test_backup_refuses_source_symlink(self):
        path = self.generation / "sources" / "link"
        try:
            path.symlink_to(self.base / "outside", target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks unavailable on this platform")
        with self.assertRaises(ValueError):
            self.make_backup()
        self.assertFalse(self.backup.exists())
        self.assertFalse(list(self.base.glob(".agent-library-backup-*")))


if __name__ == "__main__":
    unittest.main()
