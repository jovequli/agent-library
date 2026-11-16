"""Release boundaries: synthetic payloads only, never a user's archive."""
from __future__ import annotations

import hashlib
import importlib.util
import io
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest
import zipfile

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('release_boundary_check', SCRIPTS / 'check_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleasePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent-library-release-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_approved_public_source_and_generated_metadata_are_allowed(self):
        for path in ('README.md', 'src/agent_library/core.py', 'tests/test_fictional.py',
                     'docs/ARCHITECTURE.md', '.github/workflows/ci.yml'):
            self.assertIsNone(release.inspect_entry(path, b'Fictional public source', 'source'), path)
        for path in ('PKG-INFO', 'setup.cfg', 'src/agent_history_library.egg-info/SOURCES.txt'):
            self.assertTrue(release.approved_release_path(path, 'sdist'))
            self.assertFalse(release.approved_release_path(path, 'source'))
        for path in ('agent_library/cli.py', 'agent_history_library-0.1.0.dist-info/METADATA',
                     'agent_history_library-0.1.0.dist-info/licenses/LICENSE',
                     'agent_history_library-0.1.0.dist-info/licenses/src/agent_library/web/vendor/katex.LICENSE'):
            self.assertIsNone(release.inspect_entry(path, b'Public license or program', 'wheel'), path)

    def test_unapproved_nested_files_and_runtime_exports_are_rejected(self):
        for path in ('docs/conversation.json', 'examples/private-notes.txt', 'src/agent_library/export.json',
                     'docs/unreviewed.jpg', 'tests/real-history.jsonl', 'src/agent_library/data/history.md',
                     'docs/migration_reports/report.md', 'agent_library/hidden.json',
                     'src/agent_library/web/vendor/unreviewed.min.js',
                     'src/agent_library/web/vendor/fonts/unreviewed.ttf',
                     'agent_history_library-0.1.0.dist-info/licenses/private/LICENSE'):
            self.assertIsNotNone(release.inspect_entry(path, b'not inspected as public source'), path)

    def test_credentials_and_database_sidecars_cannot_hide_under_allowed_roots(self):
        for path in ('docs/.env', 'examples/.env.production', 'src/auth.json',
                     'scripts/secret.pem', 'docs/signing.key', 'tests/archive.SQLITE',
                     'docs/archive.sqlite3-wal', 'docs/copy.db-shm'):
            self.assertIsNotNone(release.inspect_entry(path, b'Fictional data'), path)
        self.assertIsNotNone(release.inspect_entry('docs/safe.md', b'SQLite format 3\0fictional'))

    def test_personal_paths_include_root_ending_and_json_escaped_windows_paths(self):
        paths = [('/' + 'Users' + '/fictional-owner/private').encode(),
                 ('/' + 'home' + '/fictional-owner').encode(),
                 ('C:' + '\\' + 'Users' + '\\fictional-owner\\private').encode(),
                 ('C:' + '\\\\' + 'Users' + '\\\\fictional-owner\\\\private').encode()]
        for data in paths:
            failure = release.inspect_entry('docs/example.md', data)
            self.assertIn('home path', failure)
            self.assertNotIn('fictional-owner', failure)
        self.assertIsNone(release.inspect_entry('docs/example.md', b'/path/to/library and ~/.agent-library'))

    def test_secret_markers_report_only_path_not_matched_value(self):
        payloads = [b'sk-' + b'proj-' + b'A' * 60,
                    b'gh' + b'p_' + b'B' * 40,
                    b'AK' + b'IA' + b'C' * 16,
                    b'-----BEGIN ' + b'PRIVATE KEY-----',
                    b'Authorization: Bearer ' + b'D' * 48]
        for payload in payloads:
            failure = release.inspect_entry('docs/example.md', payload)
            self.assertIsNotNone(failure)
            self.assertNotIn(payload.decode(), failure)
            self.assertIn('docs/example.md', failure)
        self.assertIsNone(release.inspect_entry('docs/example.md', b'sk-your-key is a placeholder; SHA-256: ' + b'a' * 64))

    def test_only_reviewed_fictional_screenshot_hash_is_accepted(self):
        path = Path(__file__).resolve().parents[1] / 'docs/preview-zh-CN.jpg'
        self.assertIsNone(release.inspect_entry('docs/preview-zh-CN.jpg', path.read_bytes()))
        self.assertIn('synthetic media changed', release.inspect_entry('docs/preview-zh-CN.jpg', b'fictional replacement'))

    def test_source_tree_ignores_dev_dirs_but_not_nested_secret_files(self):
        (self.root / 'docs').mkdir()
        (self.root / 'docs/private.json').write_text('{}')
        (self.root / 'README.md').write_text('Public sample')
        (self.root / 'node_modules').mkdir()
        (self.root / 'node_modules/private.json').write_text('{}')
        rows = list(release.source_entries(self.root))
        self.assertEqual({name for name, _, _ in rows}, {'README.md', 'docs/private.json'})
        self.assertTrue(next(error for name, _, error in rows if name == 'docs/private.json'))

    def test_zip_links_duplicate_paths_and_escape_paths_are_rejected(self):
        for index, names in enumerate([('../escape.md',), ('docs/a.md', 'docs/A.md'), ('docs//a.md',)]):
            path = self.root / f'unsafe-{index}.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                for name in names:
                    archive.writestr(name, 'Fictional data')
            with self.assertRaises(ValueError):
                list(release.archive_entries(path))
        path = self.root / 'link.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            info = zipfile.ZipInfo('docs/link.md')
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, 'outside')
        with self.assertRaisesRegex(ValueError, 'Non-regular ZIP'):
            list(release.archive_entries(path))

    def test_tar_hardlinks_and_archive_limits_are_checked_before_payload_read(self):
        path = self.root / 'link.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            item = tarfile.TarInfo('agent_history_library-0.1.0/docs/link.md')
            item.type = tarfile.LNKTYPE
            item.linkname = 'outside'
            archive.addfile(item)
        with self.assertRaisesRegex(ValueError, 'Non-regular'):
            list(release.archive_entries(path))
        path = self.root / 'oversize.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('README.md', b'A' * 200)
        previous = release.MAX_FILE_BYTES
        try:
            release.MAX_FILE_BYTES = 100
            with self.assertRaisesRegex(ValueError, 'limits'):
                list(release.archive_entries(path))
        finally:
            release.MAX_FILE_BYTES = previous

    def test_archive_layout_normalization_enforces_approved_paths(self):
        path = self.root / 'source.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name in ('README.md', 'docs/private.json', 'src/agent_history_library.egg-info/PKG-INFO'):
                payload = b'Public fictional fixture'
                item = tarfile.TarInfo('agent_history_library-0.1.0/' + name)
                item.size = len(payload)
                archive.addfile(item, io.BytesIO(payload))
        rows = list(release.archive_entries(path))
        self.assertEqual([name for name, _, _ in rows], ['README.md', 'docs/private.json', 'src/agent_history_library.egg-info/PKG-INFO'])
        self.assertTrue(rows[1][2])
        self.assertIsNone(rows[0][2])
        self.assertIsNone(rows[2][2])


if __name__ == '__main__':
    unittest.main()
