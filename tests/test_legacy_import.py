"""Legacy archive migrations use fictional data; no private archive is required."""
from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_library import query, schema
from agent_library.backup import backup_library, restore_library
from agent_library.core import LibraryStore
from agent_library.handoff import context_pack
from agent_library.ingest import import_source
from agent_library.legacy import migrate_legacy_archive


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aAZsAAAAASUVORK5CYII=')


class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent-library-legacy-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'old-export'
        self.archive = self.source / 'record_library'
        (self.archive / 'data').mkdir(parents=True)
        (self.archive / 'media').mkdir()
        self.store = LibraryStore(self.root / 'new-private-home')
        self.sid = 'chat:fictional-main'
        self.image_id = hashlib.sha256(PNG).hexdigest()
        self.other_image = PNG + b'fictional-session-artifact'
        self.other_id = hashlib.sha256(self.other_image).hexdigest()
        raw = self.source / 'extracted/conversations-000/conversations.json'
        raw.parent.mkdir(parents=True)
        raw.write_text('[{"uuid":"fictional-main","chat_messages":[]}]', encoding='utf-8')
        code = self.source / 'local_backup/claude_code/projects/fictional/task.jsonl'
        code.parent.mkdir(parents=True)
        code.write_text('{"type":"system","content":"fictional source"}\n', encoding='utf-8')
        (self.archive / 'media' / (self.image_id + '.png')).write_bytes(PNG)
        (self.archive / 'media' / (self.other_id + '.png')).write_bytes(self.other_image)
        self._make_database()
        assets = {ident: {'id': ident, 'path': 'record_library/media/' + ident + '.png', 'mime': 'image/png', 'width': 1, 'height': 1} for ident in (self.image_id, self.other_id)}
        media = {'version': 1, 'assets': assets, 'sessions': {self.sid: [
            {'id': self.image_id, 'session_id': self.sid, 'message_id': 4, 'source_path': assets[self.image_id]['path'], 'origin': 'embedded', 'display': 'message', 'mime': 'image/png', 'width': 1, 'height': 1},
            {'id': self.other_id, 'session_id': self.sid, 'message_id': None, 'source_path': assets[self.other_id]['path'], 'origin': 'outputs', 'display': 'attachment', 'mime': 'image/png', 'width': 1, 'height': 1},
        ]}, 'missing': {self.sid: [{'message_id': 6, 'reason': 'only_reference_or_missing_image_bytes'}]},
            'message_bindings': {'4': {'id': 4, 'session_id': self.sid, 'uuid': 'image-block', 'source_path': 'extracted/conversations-000/conversations.json', 'line': 0, 'role': 'user', 'kind': 'attachment'}, '6': {'id': 6, 'session_id': self.sid, 'uuid': 'missing-image', 'source_path': 'extracted/conversations-000/conversations.json', 'line': 0, 'role': 'user', 'kind': 'attachment'}}}
        (self.archive / 'data/media_index.json').write_text(json.dumps(media), encoding='utf-8')
        self.catalog = {'display_project': '/fictional/selected-folder', 'task_paths': ['/fictional/selected-folder'], 'catalog_group': 'fictional-group', 'catalog_group_label': 'selected-folder', 'catalog_group_type': 'project', 'month': '2026-01', 'dialogue_count': 5, 'execution_cwd': '/sessions/fictional/runtime'}
        (self.archive / 'data/session_catalog.json').write_text(json.dumps({'version': 2, 'sessions': {self.sid: self.catalog}}), encoding='utf-8')

    def _make_database(self):
        path = self.archive / 'data/archive.sqlite'
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''
            CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,title TEXT,created_at TEXT,updated_at TEXT,project TEXT,kind TEXT,parent_id TEXT,source_paths TEXT,message_count INTEGER,handoff_path TEXT,transcript_path TEXT);
            CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT NOT NULL,seq INTEGER NOT NULL,role TEXT,kind TEXT,text TEXT,created_at TEXT,uuid TEXT,source_path TEXT,line INTEGER);
            ''')
            rows = [
                (1, self.sid, 0, 'user', 'text', 'Fictional requirement one. Preserve it exactly.', '2026-01-01T00:00:00Z', 'duplicate-native-uuid', 'extracted/conversations-000/conversations.json', 0),
                (2, self.sid, 2, 'user', 'text', 'Fictional requirement two is an independent turn.', '2026-01-01T00:01:00Z', 'duplicate-native-uuid', 'extracted/conversations-000/conversations.json', 0),
                (3, self.sid, 3, 'assistant', 'text', '# Fictional reply\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n$E=mc^2$', '2026-01-01T00:02:00Z', 'assistant-block', 'extracted/conversations-000/conversations.json', 0),
                (4, self.sid, 4, 'user', 'attachment', '[image: preserved source]', '2026-01-01T00:03:00Z', 'image-block', 'extracted/conversations-000/conversations.json', 0),
                (5, self.sid, 5, 'system', 'summary', 'A fictional compact summary is evidence, not current instruction.', '', 'summary-block', 'local_backup/claude_code/projects/fictional/task.jsonl', 1),
                (6, self.sid, 6, 'user', 'attachment', '[image: original bytes not retained]', '', 'missing-image', 'extracted/conversations-000/conversations.json', 0),
                (7, 'context:fictional', 0, 'system', 'context', 'Fictional project instructions require confirmation before reuse.', '', 'context-block', 'local_backup/claude_code/projects/fictional/task.jsonl', 1),
            ]
            for sid, source, kind, count in [(self.sid, 'chat', 'main', 6), ('context:fictional', 'context', 'context', 1), ('claude_code:empty', 'claude_code', 'metadata', 0), ('claude_code:child', 'claude_code', 'subagent', 0)]:
                safe = sid.replace(':', '_')
                handoff = self.archive / 'handoffs' / safe / '接续说明.md'
                transcript = self.archive / 'transcripts' / (safe + '.md')
                handoff.parent.mkdir(parents=True, exist_ok=True)
                transcript.parent.mkdir(parents=True, exist_ok=True)
                handoff.write_text('Fictional continuation guide. Read original requirements.', encoding='utf-8')
                handoff.with_name('用户要求.md').write_text('Fictional original user requirements.', encoding='utf-8')
                transcript.write_text('Fictional retained transcript.', encoding='utf-8')
                db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (sid, source, 'Fictional archive session', '2026-01-01T00:00:00Z', '2026-01-01T00:03:00Z', '/sessions/fictional/runtime', kind, self.sid if sid.endswith(':child') else None, json.dumps(['extracted/conversations-000/conversations.json']), count, handoff.relative_to(self.source).as_posix(), transcript.relative_to(self.source).as_posix()))
            db.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)', rows)

    @staticmethod
    def tree(root):
        return {p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in root.rglob('*') if p.is_file()}

    def migrate(self):
        return migrate_legacy_archive(self.store, self.archive, self.source, name='Fictional migrated collection')

    def test_exact_rows_duplicate_native_ids_and_empty_sessions_survive(self):
        before = self.tree(self.source)
        result = self.migrate()
        self.assertEqual(result['sessions'], 4)
        self.assertEqual(result['messages'], 7)
        with closing(self.store.connect(result['id'])) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 4)
            sid = db.execute('SELECT id FROM sessions WHERE native_id=?', (self.sid,)).fetchone()[0]
            rows = db.execute('SELECT * FROM messages WHERE session_id=? ORDER BY ordinal', (sid,)).fetchall()
            self.assertEqual([row['ordinal'] for row in rows], [0, 2, 3, 4, 5, 6])
            self.assertNotEqual(rows[0]['native_id'], rows[1]['native_id'])
            self.assertEqual(json.loads(rows[0]['metadata_json'])['legacy_uuid'], json.loads(rows[1]['metadata_json'])['legacy_uuid'])
            self.assertEqual(rows[0]['native_id'], 'legacy-row:1')
            self.assertNotEqual(rows[0]['id'], rows[1]['id'])
            self.assertNotEqual(rows[0]['text'], rows[1]['text'])
            self.assertEqual(rows[4]['kind'], 'summary')
            self.assertEqual(db.execute("SELECT count(*) FROM sessions WHERE kind='metadata'").fetchone()[0], 1)
        self.assertEqual(self.tree(self.source), before)

    def test_provenance_catalog_artifacts_and_parent_links_are_preserved(self):
        result = self.migrate()
        generation = self.store.generation(result['id'])
        with closing(self.store.connect(result['id'])) as db:
            session = db.execute('SELECT * FROM sessions WHERE native_id=?', (self.sid,)).fetchone()
            metadata = json.loads(session['metadata_json'])
            self.assertEqual(metadata['legacy_session_id'], self.sid)
            self.assertEqual(metadata['legacy_catalog'], self.catalog)
            self.assertEqual(metadata['task_paths'], self.catalog['task_paths'])
            self.assertEqual(metadata['execution_cwd'], self.catalog['execution_cwd'])
            for relative in metadata['legacy_artifacts'].values():
                self.assertTrue((generation / relative).is_file())
            child = db.execute("SELECT * FROM sessions WHERE native_id='claude_code:child'").fetchone()
            self.assertEqual(child['parent_id'], session['id'])
            row = db.execute('SELECT * FROM messages WHERE session_id=? AND ordinal=5', (session['id'],)).fetchone()
            meta = json.loads(row['metadata_json'])
            self.assertEqual(meta['legacy_message_id'], 5)
            self.assertEqual(meta['legacy_seq'], 5)
            self.assertEqual(meta['legacy_uuid'], 'summary-block')
            self.assertEqual(meta['legacy_source_path'], 'local_backup/claude_code/projects/fictional/task.jsonl')
            self.assertEqual(meta['legacy_line'], 1)
            self.assertEqual(row['source_line'], 1)
            self.assertEqual((generation / row['source_path']).read_text(encoding='utf-8'), '{"type":"system","content":"fictional source"}\n')

    def test_images_bind_only_to_exact_messages_and_no_turns_are_invented(self):
        result = self.migrate()
        self.assertEqual(result['media'], 2)
        with closing(self.store.connect(result['id'])) as db:
            session = db.execute('SELECT * FROM sessions WHERE native_id=?', (self.sid,)).fetchone()
            rows = db.execute('SELECT * FROM messages WHERE session_id=? ORDER BY ordinal', (session['id'],)).fetchall()
            self.assertEqual(json.loads(rows[3]['media_json']), [self.image_id])
            self.assertTrue(json.loads(rows[5]['metadata_json'])['missing_media'])
            session_media = json.loads(session['metadata_json'])['session_attachments']
            ids = [item['id'] if isinstance(item, dict) else item for item in session_media]
            self.assertIn(self.other_id, ids)
            self.assertNotIn(self.image_id, ids)
            for item in db.execute('SELECT * FROM media'):
                data = (self.store.generation(result['id']) / item['relative_path']).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), item['id'])
        visible = query.session_messages(self.store, result['id'], session['id'], limit=100)['items']
        self.assertEqual(len(visible), 5)
        self.assertTrue(any(m['kind'] == 'attachment' and m['media'] for m in visible))

    def test_backup_restore_and_handoff_keep_the_migrated_evidence(self):
        result = self.migrate()
        archive = self.root / 'private-backup.zip'
        self.assertTrue(backup_library(self.store, result['id'], archive)['verified'])
        restored_store = LibraryStore(self.root / 'relocated-home')
        restored = restore_library(restored_store, archive)
        schema.validate_database(restored_store.generation(restored['id']) / 'archive.sqlite3')
        with closing(restored_store.connect(restored['id'])) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0], 7)
            session = db.execute('SELECT id FROM sessions WHERE native_id=?', (self.sid,)).fetchone()[0]
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(context_pack(restored_store, restored['id'], session))) as pack:
            text = pack.read('conversation.md').decode('utf-8')
            self.assertIn('Fictional requirement one. Preserve it exactly.', text)
            self.assertIn('Fictional requirement two is an independent turn.', text)
            self.assertIn('A fictional compact summary is evidence, not current instruction.', text)
            self.assertTrue(any(name.endswith(self.image_id + '.png') for name in pack.namelist()))
            self.assertTrue(any(name.endswith(self.other_id + '.png') for name in pack.namelist()))

    def test_portable_roundtrip_preserves_source_order_and_duplicate_old_uuid(self):
        # Source chronology can differ from captured timestamps after compaction
        # or a branch merge. Portable exports must preserve the captured order.
        with closing(sqlite3.connect(self.archive / 'data/archive.sqlite')) as db, db:
            db.execute("UPDATE messages SET created_at='2025-12-31T23:00:00Z' WHERE id=2")
        result = self.migrate()
        with closing(self.store.connect(result['id'])) as db:
            sid = db.execute('SELECT id FROM sessions WHERE native_id=?', (self.sid,)).fetchone()[0]
            expected = [(row['native_id'], row['role'], row['kind'], row['text'], row['created_at']) for row in db.execute('SELECT * FROM messages WHERE session_id=? ORDER BY ordinal', (sid,))]
        exported = self.root / 'fictional-context.zip'
        exported.write_bytes(context_pack(self.store, result['id'], sid))
        new_library = self.store.create('Portable fictional library')
        import_source(self.store, new_library['id'], exported)
        with closing(self.store.connect(new_library['id'])) as db:
            imported = db.execute('SELECT id FROM sessions').fetchone()[0]
            actual = [(row['native_id'], row['role'], row['kind'], row['text'], row['created_at']) for row in db.execute('SELECT * FROM messages WHERE session_id=? ORDER BY ordinal', (imported,))]
        self.assertEqual(actual, expected)

    def test_publish_failure_keeps_old_archive_and_leaves_no_partial_library(self):
        before = self.tree(self.source)
        initial = self.store.create('Existing fictional collection')
        generation = self.store.generation(initial['id'])
        # create() also publishes its initial empty generation. Fail only when
        # the migration tries to publish a nonempty result.
        original_publish = self.store.publish
        def fail_nonempty(library_id, staging):
            with closing(sqlite3.connect(staging / 'archive.sqlite3')) as db:
                count = db.execute('SELECT count(*) FROM sessions').fetchone()[0]
            if count:
                raise RuntimeError('Synthetic publication failure')
            return original_publish(library_id, staging)
        with patch.object(self.store, 'publish', side_effect=fail_nonempty):
            with self.assertRaisesRegex(RuntimeError, 'Synthetic publication failure'):
                self.migrate()
        self.assertEqual(self.tree(self.source), before)
        self.assertEqual([item['id'] for item in self.store.list()], [initial['id']])
        self.assertEqual(self.store.generation(initial['id']), generation)


if __name__ == '__main__':
    unittest.main()
