"""Release contract regressions using only fictional temporary source archives."""
from __future__ import annotations
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from agent_library.core import LibraryStore
from agent_library.handoff import context_pack, get_handoff, save_notes
from agent_library.importers import create_demo_source
from agent_library.ingest import import_source, snapshot
from agent_library import query


class IngestRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='agent-library-contract-')
        self.root = Path(self.temporary.name).resolve()
        self.store = LibraryStore(self.root / 'home')
        self.library = self.store.create('Synthetic contract library')
        self.ident = self.library['id']

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, name='source', messages=None, title='Synthetic task', updated='2026-01-03T00:00:00Z'):
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        if messages is None:
            messages = [
                {'uuid': 'user-1', 'sender': 'human', 'text': 'Keep the original files and preserve the source links.', 'created_at': '2026-01-01T00:00:00Z'},
                {'uuid': 'answer-1', 'sender': 'assistant', 'text': 'The source index is ready. A later correction is still pending.', 'created_at': '2026-01-02T00:00:00Z'},
                {'uuid': 'user-correction', 'sender': 'human', 'text': 'Important middle correction: exclude the hypothetical third source.', 'created_at': '2026-01-02T00:01:00Z'},
                {'uuid': 'answer-new', 'sender': 'assistant', 'text': 'Newest complete answer includes the correction.', 'created_at': '2026-01-03T00:00:00Z'},
            ]
        path = directory / 'conversations.json'
        path.write_text(json.dumps([{'uuid': 'shared-session', 'name': title, 'updated_at': updated, 'chat_messages': messages}]), encoding='utf-8')
        return directory

    def all_messages(self, library_id=None):
        with contextlib.closing(self.store.connect(library_id or self.ident)) as db:
            return [dict(row) for row in db.execute('SELECT * FROM messages ORDER BY ordinal')]

    def test_identical_replay_is_noop_and_keeps_generation(self):
        source = self.source()
        first = import_source(self.store, self.ident, source)
        active = self.store.generation(self.ident)
        second = import_source(self.store, self.ident, source)
        self.assertEqual(first['messages'], 4)
        self.assertTrue(second['unchanged'])
        self.assertEqual(self.store.generation(self.ident), active)
        self.assertEqual(len(self.all_messages()), 4)
        self.assertFalse(any(p.name.startswith('.staging-') for p in active.parent.iterdir()))

    def test_stale_smaller_export_does_not_delete_later_conversation(self):
        source = self.source()
        import_source(self.store, self.ident, source)
        original = self.all_messages()
        payload = json.loads((source / 'conversations.json').read_text())
        payload[0]['chat_messages'] = payload[0]['chat_messages'][:1]
        payload[0]['updated_at'] = '2026-01-01T00:00:00Z'
        payload[0]['name'] = 'Older title'
        (source / 'conversations.json').write_text(json.dumps(payload))
        import_source(self.store, self.ident, source)
        current = self.all_messages()
        self.assertEqual([m['text'] for m in current], [m['text'] for m in original])
        self.assertEqual([m['id'] for m in current], [m['id'] for m in original])
        with contextlib.closing(self.store.connect(self.ident)) as db:
            session = db.execute('SELECT title, updated_at FROM sessions').fetchone()
        self.assertEqual(session['title'], 'Synthetic task')
        self.assertEqual(session['updated_at'], '2026-01-03T00:00:00Z')

    def test_growing_snapshot_does_not_regress_to_shorter_reply(self):
        newest = [{'uuid': 'u', 'sender': 'human', 'text': 'Please answer'}, {'uuid': 'a', 'sender': 'assistant', 'text': 'Hello, this is the complete answer.'}]
        source = self.source(messages=newest)
        import_source(self.store, self.ident, source)
        older = json.loads((source / 'conversations.json').read_text())
        older[0]['chat_messages'][1]['text'] = 'Hello'
        older[0]['updated_at'] = '2026-01-01T00:00:00Z'
        (source / 'conversations.json').write_text(json.dumps(older))
        import_source(self.store, self.ident, source)
        self.assertEqual(self.all_messages()[1]['text'], 'Hello, this is the complete answer.')

    def test_auto_mixed_import_and_hidden_runtime_search(self):
        source = create_demo_source(self.root / 'mixed-source')
        import_source(self.store, self.ident, source)
        all_sessions = query.sessions(self.store, self.ident, kind='all')
        self.assertEqual({s['provider'] for s in all_sessions['items']}, {'claude', 'claude_code', 'cowork'})
        found = query.sessions(self.store, self.ident, q='preserve the public API')
        self.assertEqual(len(found['items']), 1)
        self.assertEqual(found['items'][0]['provider'], 'claude_code')
        self.assertIsNotNone(found['items'][0]['match_ordinal'])
        self.assertEqual(query.sessions(self.store, self.ident, q='Demo runtime details')['total'], 0)
        self.assertGreater(query.sessions(self.store, self.ident, q='虚构')['total'], 0)

    def test_context_zip_roundtrip_preserves_full_text_roles_and_images(self):
        source = create_demo_source(self.root / 'mixed-source')
        import_source(self.store, self.ident, source)
        with contextlib.closing(self.store.connect(self.ident)) as db:
            session = db.execute("SELECT * FROM sessions WHERE provider='claude' AND kind='main'").fetchone()
            original = [dict(r) for r in db.execute('SELECT * FROM messages WHERE session_id=? ORDER BY ordinal', (session['id'],))]
            media = {r['id']: dict(r) for r in db.execute('SELECT * FROM media')}
        packed = context_pack(self.store, self.ident, session['id'], lang='zh-CN')
        with zipfile.ZipFile(io.BytesIO(packed)) as archive:
            names = archive.namelist()
            self.assertIn('requirements.md', names)
            self.assertIn('conversation.json', names)
            checksums = json.loads(archive.read('checksums.json'))
            self.assertTrue(all(hashlib.sha256(archive.read(name)).hexdigest() == digest for name, digest in checksums.items()))
            for message in original:
                if message['text']:
                    self.assertIn(message['text'], archive.read('conversation.md').decode())
        destination = self.store.create('Restored specific conversation')
        pack_path = self.root / 'conversation-context.zip'
        pack_path.write_bytes(packed)
        import_source(self.store, destination['id'], pack_path)
        restored = self.all_messages(destination['id'])
        self.assertEqual([(r['role'], r['kind'], r['text']) for r in restored], [(r['role'], r['kind'], r['text']) for r in original])
        self.assertEqual([json.loads(r['media_json']) for r in restored], [json.loads(r['media_json']) for r in original])
        with contextlib.closing(self.store.connect(destination['id'])) as db:
            restored_media = {r['id']: dict(r) for r in db.execute('SELECT * FROM media')}
        expected_ids = {mid for r in original for mid in json.loads(r['media_json'])}
        self.assertEqual(set(restored_media), expected_ids)
        for mid, item in restored_media.items():
            self.assertEqual((self.store.generation(destination['id']) / item['relative_path']).read_bytes(), (self.store.generation(self.ident) / media[mid]['relative_path']).read_bytes())

    def test_context_pack_explicit_agent_library_provider(self):
        import_source(self.store, self.ident, self.source())
        session = query.sessions(self.store, self.ident)["items"][0]
        pack = self.root / 'specific-conversation.zip'
        pack.write_bytes(context_pack(self.store, self.ident, session['id']))
        other = self.store.create('Explicit context interchange')
        result = import_source(self.store, other['id'], pack, provider='agent_library')
        self.assertEqual(result['sessions'], 1)
        self.assertEqual([m['text'] for m in self.all_messages(other['id'])], [m['text'] for m in self.all_messages()])

    def test_context_pack_retains_reviewed_task_notes(self):
        import_source(self.store, self.ident, self.source())
        session = query.sessions(self.store, self.ident)['items'][0]
        notes = {'goal': 'A human-reviewed goal', 'constraints': 'Never overwrite the original.', 'decisions': 'Use the second source.', 'completed': 'Sources verified.', 'pending': 'Compare the two estimates.', 'files': '/demo/project'}
        save_notes(self.store, self.ident, session['id'], notes)
        pack = self.root / 'reviewed-context.zip'
        pack.write_bytes(context_pack(self.store, self.ident, session['id']))
        other = self.store.create('Reviewed context restored')
        import_source(self.store, other['id'], pack)
        restored = query.sessions(self.store, other['id'])['items'][0]
        handoff = get_handoff(self.store, other['id'], restored['id'])
        self.assertEqual(handoff['notes'], notes)
        self.assertTrue(handoff['edited'])

    def test_markdown_local_image_refs_survive_context_roundtrip(self):
        source = self.root / 'cowork-source'
        task = source / 'local-agent-mode-sessions/a/o/local_demo'
        task.mkdir(parents=True)
        task.with_suffix('.json').write_text(json.dumps({'title': 'Fictional local plot'}))
        row = {'type': 'assistant', 'uuid': 'plot-message', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': '![Plot](/mnt/user-data/outputs/plot.png)'}]}}
        (task / 'audit.jsonl').write_text(json.dumps(row) + '\n')
        image = task / 'outputs/plot.png'
        image.parent.mkdir()
        image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'fictional-test')
        import_source(self.store, self.ident, source)
        session = query.sessions(self.store, self.ident)['items'][0]
        message = next(m for m in query.session_messages(self.store, self.ident, session['id'])['items'] if m['kind'] == 'text')
        self.assertEqual(message['metadata']['image_references']['/mnt/user-data/outputs/plot.png'], message['media'][0]['id'])
        pack = self.root / 'plot-context.zip'
        pack.write_bytes(context_pack(self.store, self.ident, session['id']))
        other = self.store.create('Plot context restored')
        import_source(self.store, other['id'], pack)
        restored_session = query.sessions(self.store, other['id'])['items'][0]
        restored = next(m for m in query.session_messages(self.store, other['id'], restored_session['id'])['items'] if m['kind'] == 'text')
        self.assertEqual(restored['metadata']['image_references']['/mnt/user-data/outputs/plot.png'], restored['media'][0]['id'])

    def test_snapshot_excludes_credentials_and_symlinks_and_rejects_zip_escape(self):
        source = self.source()
        (source / '.credentials.json').write_text('{"secret":"fictional"}')
        (source / 'settings.json').write_text('{"token":"fictional"}')
        (source / 'plugins').mkdir()
        (source / 'plugins/private.json').write_text('{"private":"fictional"}')
        target = self.root / 'outside.txt'
        target.write_text('outside sentinel')
        (source / 'linked.txt').symlink_to(target)
        destination = self.root / 'snapshot'
        snapshot(source, destination)
        self.assertEqual([p.name for p in destination.iterdir()], ['conversations.json'])
        path = self.root / 'bad.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('../escape.json', '{}')
        active = self.store.generation(self.ident)
        with self.assertRaisesRegex(ValueError, 'Unsafe ZIP'):
            import_source(self.store, self.ident, path)
        self.assertEqual(self.store.generation(self.ident), active)
        self.assertFalse((self.root / 'escape.json').exists())
        self.assertEqual(target.read_text(), 'outside sentinel')

    def test_importing_parent_of_store_is_rejected(self):
        active = self.store.generation(self.ident)
        with self.assertRaisesRegex(ValueError, 'parent of the library'):
            import_source(self.store, self.ident, self.root)
        self.assertEqual(self.store.generation(self.ident), active)


if __name__ == '__main__':
    unittest.main()
