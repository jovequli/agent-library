"""Portable storage/search regressions; all history is fictional and temporary."""
from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from agent_library import query, schema
from agent_library.core import LibraryStore
from agent_library.ingest import import_source


class StorageWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='agent-library-storage-test-')
        self.root = Path(self.temporary.name).resolve()
        self.store = LibraryStore(self.root / 'private-home')
        self.library_id = self.store.create('Fictional storage workflow')['id']

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, directory='input', messages=None, title='Fictional conversation', updated='2026-01-03T00:00:00Z'):
        source = self.root / directory
        source.mkdir(parents=True, exist_ok=True)
        if messages is None:
            messages = [{'uuid': 'u', 'sender': 'human', 'text': 'Please keep source evidence.', 'created_at': '2026-01-01T00:00:00Z'}, {'uuid': 'a', 'sender': 'assistant', 'text': 'Evidence is retained in the fictional archive.', 'created_at': '2026-01-03T00:00:00Z'}]
        (source / 'conversations.json').write_text(json.dumps([{'uuid': 'shared-native-session', 'name': title, 'updated_at': updated, 'chat_messages': messages}]), encoding='utf-8')
        return source

    def rows(self, sql='SELECT * FROM messages ORDER BY ordinal', params=(), library_id=None):
        with contextlib.closing(self.store.connect(library_id or self.library_id)) as db:
            return [dict(row) for row in db.execute(sql, params)]

    @staticmethod
    def tree_digest(directory):
        return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob('*') if p.is_file()}

    def test_future_schema_rejected_by_open_and_validator_without_rewrite(self):
        generation = self.store.generation(self.library_id)
        path = generation / 'archive.sqlite3'
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute(f'PRAGMA user_version={schema.VERSION + 1}')
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Unsupported schema'):
            self.store.connect(self.library_id)
        with self.assertRaisesRegex(ValueError, 'Unsupported archive schema'):
            schema.validate_database(path)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.store.generation(self.library_id), generation)

    def test_explicit_json_file_detected_without_filename_assumptions(self):
        original = self.source() / 'conversations.json'
        renamed = original.with_name('renamed-export.json')
        original.rename(renamed)
        import_source(self.store, self.library_id, renamed)
        self.assertEqual(query.sessions(self.store, self.library_id, q='source evidence')['total'], 1)

    def test_neutral_provider_is_searchable_and_available_as_a_filter(self):
        source = self.root / 'neutral-export.json'
        source.write_text(json.dumps({'format': 'agent-library.conversation', 'version': 1, 'sessions': [{
            'id': 'fictional-other-agent', 'provider': 'other_agent', 'title': 'Independent agent history',
            'messages': [{'id': 'user-1', 'role': 'user', 'kind': 'text', 'text': 'Continue the fictional shared work.'}]}]}), encoding='utf-8')
        import_source(self.store, self.library_id, source)
        result = query.sessions(self.store, self.library_id, q='shared work', provider='other_agent')
        self.assertEqual(result['total'], 1)
        self.assertIn({'id': 'other_agent', 'name': 'other_agent'}, result['providers'])

    def test_zip_device_path_is_rejected_without_publishing(self):
        source = self.root / 'unsafe.zip'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('CON.json', '[]')
        before = self.store.generation(self.library_id)
        with self.assertRaises(ValueError):
            import_source(self.store, self.library_id, source)
        self.assertEqual(self.store.generation(self.library_id), before)

    def test_malformed_import_keeps_previous_generation_and_receipts(self):
        import_source(self.store, self.library_id, self.source())
        generation = self.store.generation(self.library_id)
        before = self.tree_digest(generation)
        invalid = self.root / 'bad-input'
        invalid.mkdir()
        (invalid / 'conversations.json').write_text('{not valid json')
        with self.assertRaisesRegex(ValueError, 'No supported'):
            import_source(self.store, self.library_id, invalid)
        self.assertEqual(self.store.generation(self.library_id), generation)
        self.assertEqual(self.tree_digest(generation), before)
        self.assertFalse(any(p.name.startswith('.staging-') for p in generation.parent.iterdir()))

    def test_late_index_failure_rolls_back_full_generation(self):
        source = self.source()
        import_source(self.store, self.library_id, source)
        generation = self.store.generation(self.library_id)
        before = self.tree_digest(generation)
        updated = [{'uuid': 'u', 'sender': 'human', 'text': 'A changed fictional request.'}, {'uuid': 'new', 'sender': 'assistant', 'text': 'New reply cannot be published after index failure.'}]
        self.source(messages=updated, updated='2026-01-04T00:00:00Z')
        with patch('agent_library.ingest.schema.rebuild_search', side_effect=sqlite3.OperationalError('Synthetic index failure')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'Synthetic index failure'):
                import_source(self.store, self.library_id, source)
        self.assertEqual(self.store.generation(self.library_id), generation)
        self.assertEqual(self.tree_digest(generation), before)
        self.assertFalse(any(p.name.startswith('.staging-') for p in generation.parent.iterdir()))
        self.assertEqual(query.sessions(self.store, self.library_id, q='A changed fictional request')['total'], 0)

    def test_same_native_ids_remain_independent_between_dataset_labels(self):
        one = self.source('account-one', messages=[{'uuid': 'u', 'sender': 'human', 'text': 'Distinctive amber account requirement.'}])
        two = self.source('account-two', messages=[{'uuid': 'u', 'sender': 'human', 'text': 'Distinctive cobalt account requirement.'}])
        import_source(self.store, self.library_id, one, dataset='account-one')
        import_source(self.store, self.library_id, two, dataset='account-two')
        sessions = self.rows('SELECT * FROM sessions ORDER BY dataset')
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0]['native_id'], sessions[1]['native_id'])
        self.assertNotEqual(sessions[0]['id'], sessions[1]['id'])
        self.assertEqual(query.sessions(self.store, self.library_id, q='amber')['items'][0]['dataset'], 'account-one')
        self.assertEqual(query.sessions(self.store, self.library_id, q='cobalt')['items'][0]['dataset'], 'account-two')
        self.assertTrue(import_source(self.store, self.library_id, one, dataset='account-one')['unchanged'])

    def test_new_middle_message_preserves_existing_message_ids_and_order(self):
        original = [{'uuid': 'u', 'sender': 'human', 'text': 'First request', 'created_at': '2026-01-01T00:00:00Z'}, {'uuid': 'a', 'sender': 'assistant', 'text': 'Final answer', 'created_at': '2026-01-03T00:00:00Z'}]
        source = self.source(messages=original)
        import_source(self.store, self.library_id, source)
        old = {m['native_id']: m['id'] for m in self.rows()}
        middle = {'uuid': 'correction', 'sender': 'human', 'text': 'Preserve this recovered middle correction.', 'created_at': '2026-01-02T00:00:00Z'}
        self.source(messages=[original[0], middle, original[1]], updated='2026-01-04T00:00:00Z')
        import_source(self.store, self.library_id, source)
        rows = self.rows()
        self.assertEqual([r['text'] for r in rows], ['First request', middle['text'], 'Final answer'])
        self.assertEqual([r['ordinal'] for r in rows], [0, 1, 2])
        self.assertEqual({r['native_id']: r['id'] for r in rows if r['native_id'] in old}, old)
        self.assertEqual(query.sessions(self.store, self.library_id, q='middle correction')['items'][0]['match_ordinal'], 1)

    def test_quote_queries_and_short_chinese_are_literal_and_safe(self):
        source = self.source(messages=[{'uuid': 'u', 'sender': 'human', 'text': '中文测试 includes "quoted phrase" and apostrophe \' safely. 100%_done\\folder'}])
        import_source(self.store, self.library_id, source)
        for term in ('中文', '测', '"quoted phrase"', 'apostrophe \'', '100%_done\\folder'):
            with self.subTest(term=term):
                self.assertEqual(query.sessions(self.store, self.library_id, q=term)['total'], 1)
        self.assertEqual(query.sessions(self.store, self.library_id, q='" OR 1=1 --')['total'], 0)

    def test_like_fallback_escapes_wildcards_and_backslash(self):
        source = self.root / 'literal-search'
        source.mkdir()
        entries = [
            {'uuid': 'literal', 'name': 'Fictional literal', 'chat_messages': [{'uuid': 'l', 'sender': 'human', 'text': 'A literal 100%_done\\folder value 中文。'}]},
            {'uuid': 'decoy', 'name': 'Fictional decoy', 'chat_messages': [{'uuid': 'd', 'sender': 'human', 'text': 'A decoy 100Xdone folder value English.'}]},
        ]
        (source / 'conversations.json').write_text(json.dumps(entries))
        import_source(self.store, self.library_id, source)
        with contextlib.closing(sqlite3.connect(self.store.generation(self.library_id) / 'archive.sqlite3')) as db, db:
            db.execute("UPDATE archive_info SET value='like' WHERE key='search'")
        for term in ('%', '_', '\\', '100%_done\\folder', '中文'):
            with self.subTest(term=term):
                found = query.sessions(self.store, self.library_id, q=term)
                self.assertEqual(found['total'], 1)
                self.assertEqual(found['items'][0]['native_id'], 'literal')

    def test_schema_without_trigram_support_still_imports_and_searches(self):
        real_connect = sqlite3.connect
        class NoFTSConnection:
            def __init__(self, database, *args, **kwargs):
                self.connection = real_connect(database, *args, **kwargs)
            def execute(self, statement, *args, **kwargs):
                if statement.startswith('CREATE VIRTUAL TABLE'):
                    raise sqlite3.OperationalError('Synthetic SQLite without trigram support')
                return self.connection.execute(statement, *args, **kwargs)
            def __getattr__(self, name):
                return getattr(self.connection, name)
        with patch('agent_library.schema.sqlite3.connect', side_effect=NoFTSConnection):
            library = self.store.create('Fictional fallback library')
        import_source(self.store, library['id'], self.source())
        with contextlib.closing(self.store.connect(library['id'])) as db:
            self.assertEqual(db.execute("SELECT value FROM archive_info WHERE key='search'").fetchone()[0], 'like')
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='message_search'").fetchone())
        self.assertEqual(query.sessions(self.store, library['id'], q='fictional archive')['total'], 1)

    def test_all_records_search_marks_assistant_thinking_as_hidden(self):
        path = self.root / 'conversation.json'
        path.write_text(json.dumps({'format': 'agent-library.conversation', 'version': 1, 'sessions': [{'id': 'hidden', 'provider': 'generic', 'messages': [{'id': 'u', 'role': 'user', 'kind': 'text', 'text': 'A normal fictional request.'}, {'id': 'think', 'role': 'assistant', 'kind': 'thinking', 'text': 'Hidden synthetic deliberation phrase.'}]}]}))
        import_source(self.store, self.library_id, path)
        self.assertEqual(query.sessions(self.store, self.library_id, q='synthetic deliberation')['total'], 0)
        result = query.sessions(self.store, self.library_id, q='synthetic deliberation', kind='all')
        self.assertEqual(result['total'], 1)
        self.assertTrue(result['items'][0]['match_hidden'])
        self.assertEqual(result['items'][0]['match_ordinal'], 1)


if __name__ == '__main__':
    unittest.main()
