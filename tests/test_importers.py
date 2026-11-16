"""All fixtures are fictional, generated inside temporary directories."""
import base64
import json
from pathlib import Path
import tempfile
import unittest

from agent_library.importers import create_demo_source, parse_source


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='agent-library-synthetic-')
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def json(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def jsonl(self, name, rows):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(row) if not isinstance(row, str) else row for row in rows) + '\n', encoding='utf-8')
        return path

    @staticmethod
    def row(ident, text, role='assistant', **kw):
        return {'type': role, 'uuid': ident, 'timestamp': kw.pop('timestamp', '2026-01-01T00:00:00Z'), 'message': {'role': role, 'content': [{'type': 'text', 'text': text}], 'id': kw.pop('response_id', '')}, **kw}

    def test_demo_detects_three_providers_without_source_changes(self):
        create_demo_source(self.root)
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = parse_source(self.root)
        self.assertEqual(result['provider'], 'mixed')
        self.assertEqual({s['provider'] for s in result['sessions']}, {'claude', 'claude_code', 'cowork'})
        self.assertTrue(any(s['kind'] == 'history' for s in result['sessions']))
        self.assertTrue(any(s['kind'] == 'subagent' for s in result['sessions']))
        self.assertTrue(result['media'])
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_claude_project_role_parent_and_markdown_preserved(self):
        self.json('projects/p.json', {'uuid': 'p', 'name': 'Fictional project', 'docs': []})
        self.json('conversations.json', [{'uuid': 'c', 'project_uuid': 'p', 'chat_messages': [{'uuid': 'u', 'sender': 'human', 'text': '# Prompt'}, {'uuid': 'a', 'sender': 'assistant', 'text': '**Reply**', 'parent_message_uuid': 'u'}]}])
        result = parse_source(self.root, 'claude')
        session = next(s for s in result['sessions'] if s['id'] == 'c')
        self.assertEqual(session['project'], 'Fictional project')
        self.assertEqual(session['messages'][0]['role'], 'user')
        self.assertEqual(session['messages'][1]['text'], '**Reply**')
        self.assertEqual(session['messages'][1]['parent_id'], 'u')
        self.assertEqual(session['messages'][0]['source_path'], 'conversations.json')

    def test_streaming_snapshots_expand_and_audit_uuid_duplicates_drop(self):
        path = self.jsonl('session.jsonl', [self.row('a', 'Hel', response_id='response'), self.row('a', 'Hello', response_id='response'), self.row('copy', 'Hello', response_id='response')])
        messages = parse_source(path, 'claude_code')['sessions'][0]['messages']
        self.assertEqual([m['text'] for m in messages], ['Hello'])
        self.assertEqual(messages[0]['id'], 'a:0')
        self.assertEqual(messages[0]['source_line'], 1)

    def test_independent_repeated_user_prompts_survive(self):
        path = self.jsonl('session.jsonl', [self.row('u1', 'Same', 'user'), self.row('u2', 'Same', 'user')])
        self.assertEqual(len(parse_source(path)['sessions'][0]['messages']), 2)

    def test_tool_results_synthetic_and_thinking_are_not_user_prompts(self):
        rows = [self.row('meta', 'Runtime injection', 'user', isMeta=True), {'type': 'user', 'uuid': 'tool', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call', 'content': 'output'}]}}, {'type': 'assistant', 'uuid': 'think', 'message': {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'Hidden historical thought'}]}}]
        path = self.jsonl('session.jsonl', rows)
        messages = parse_source(path)['sessions'][0]['messages']
        self.assertEqual([(m['role'], m['kind']) for m in messages], [('system', 'metadata'), ('tool', 'tool'), ('assistant', 'thinking')])

    def test_non_prefix_revision_gets_unique_id(self):
        path = self.jsonl('session.jsonl', [self.row('a', 'First'), self.row('a', 'Revised')])
        messages = parse_source(path)['sessions'][0]['messages']
        self.assertEqual(len(messages), 2)
        self.assertNotEqual(messages[0]['id'], messages[1]['id'])

    def test_history_is_marked_input_only(self):
        path = self.jsonl('history.jsonl', [{'display': 'Only my prompt', 'sessionId': 'h', 'project': '/demo/project', 'timestamp': 1767225600000}])
        session = parse_source(path)['sessions'][0]
        self.assertEqual(session['kind'], 'history')
        self.assertTrue(session['metadata']['input_history_only'])
        self.assertEqual(session['messages'][0]['created_at'], '2026-01-01T00:00:00Z')

    def test_malformed_jsonl_row_reports_relative_line_and_continues(self):
        path = self.jsonl('session.jsonl', ['{bad', self.row('ok', 'Kept'), '[]'])
        result = parse_source(path)
        self.assertEqual(result['sessions'][0]['messages'][0]['text'], 'Kept')
        self.assertEqual([w['line'] for w in result['warnings']], [1, 3])
        self.assertTrue(all(w['file'] == 'session.jsonl' for w in result['warnings']))

    def test_cowork_audit_and_canonical_children_merge(self):
        base = 'local-agent-mode-sessions/account/org/local_fixture'
        self.json(base + '.json', {'title': 'Fictional task', 'userSelectedFolders': [{'path': '/demo/reading'}]})
        user = self.row('u', 'Review files', 'user')
        child = self.row('child', 'Child answer', parent_tool_use_id='tool-child')
        self.jsonl(base + '/.claude/projects/demo/main.jsonl', [user])
        self.jsonl(base + '/.claude/projects/demo/main/subagents/agent-review.jsonl', [child])
        self.jsonl(base + '/audit.jsonl', [user, child, self.row('main-answer', 'Done')])
        result = parse_source(self.root, 'cowork')
        self.assertEqual(len(result['sessions']), 2)
        main = next(s for s in result['sessions'] if s['kind'] == 'main')
        sub = next(s for s in result['sessions'] if s['kind'] == 'subagent')
        self.assertEqual(main['project'], '/demo/reading')
        self.assertEqual([m['text'] for m in main['messages']], ['Review files', 'Done'])
        self.assertEqual([m['text'] for m in sub['messages']], ['Child answer'])
        self.assertEqual(sub['parent_id'], main['id'])

    def test_cowork_metadata_only_not_complete_chat(self):
        path = self.json('local-agent-mode-sessions/a/o/local_missing.json', {'title': 'No transcript'})
        result = parse_source(self.root, 'cowork')
        self.assertEqual(result['sessions'][0]['kind'], 'metadata')
        self.assertTrue(result['sessions'][0]['metadata']['transcript_unavailable'])

    def test_unknown_format_and_invalid_provider_fail(self):
        path = self.json('unknown.json', {'unrelated': 'data'})
        with self.assertRaisesRegex(ValueError, 'No supported'):
            parse_source(path)
        with self.assertRaisesRegex(ValueError, 'Unsupported provider'):
            parse_source(path, 'other')

    def test_external_and_symlink_images_never_read(self):
        outside = self.root.parent / (self.root.name + '-outside.png')
        outside.write_bytes(b'private bytes')
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        (self.root / 'linked.png').symlink_to(outside)
        self.json('conversations.json', [{'uuid': 'c', 'chat_messages': [{'sender': 'human', 'content': [{'type': 'image', 'source': {'path': 'linked.png'}}, {'type': 'image', 'source': {'path': str(outside)}}, {'type': 'image', 'source': {'url': 'https://example.invalid/private.png'}}]}]}])
        result = parse_source(self.root)
        self.assertFalse(result['media'])
        self.assertTrue(all(m['metadata']['missing_media'] for m in result['sessions'][0]['messages']))

    def test_symlink_directories_skipped(self):
        other = self.root / 'other'
        other.mkdir()
        path = self.json('source/conversations.json', [{'uuid': 'c', 'chat_messages': [{'sender': 'human', 'text': 'safe'}]}])
        (other / 'alias').symlink_to(path.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'No supported'):
            parse_source(other)

    def test_canonical_interchange_reimports_and_preserves_original_provenance(self):
        payload = {'format': 'agent-library.conversation', 'version': 1, 'sessions': [{'id': 'neutral-1', 'provider': 'generic', 'title': 'Portable', 'messages': [{'id': 'm', 'role': 'user', 'kind': 'text', 'text': 'Continue this task', 'source_path': '../../old.json', 'source_line': 3}]}]}
        path = self.json('conversation.json', payload)
        result = parse_source(path)
        message = result['sessions'][0]['messages'][0]
        self.assertEqual(result['provider'], 'generic')
        self.assertEqual(message['source_path'], 'conversation.json')
        self.assertEqual(message['metadata']['original_provenance']['source_path'], '../../old.json')
        payload['version'] = 2
        path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, 'version'):
            parse_source(path)

    def test_canonical_invalid_roles_rejected(self):
        path = self.json('conversation.json', {'format': 'agent-library.conversation', 'version': 1, 'sessions': [{'id': 'c', 'messages': [{'role': 'execute', 'text': 'not a role'}]}]})
        with self.assertRaisesRegex(ValueError, 'role'):
            parse_source(path)

    def test_invalid_embedded_image_remains_missing(self):
        path = self.json('conversations.json', [{'uuid': 'c', 'chat_messages': [{'sender': 'human', 'content': [{'type': 'image', 'source': {'type': 'base64', 'data': base64.b64encode(b'not really an image').decode()}}]}]}])
        result = parse_source(path)
        self.assertFalse(result['media'])
        self.assertEqual(result['warnings'][0]['code'], 'invalid_embedded_image')

    def test_cowork_markdown_sandbox_image_resolves_only_snapshot(self):
        base = 'local-agent-mode-sessions/a/o/local_images'
        self.json(base + '.json', {'title': 'Fictional images'})
        self.jsonl(base + '/audit.jsonl', [self.row('a', '![plot](/mnt/user-data/outputs/plot.png)')])
        image = self.root / base / 'outputs/plot.png'
        image.parent.mkdir(parents=True)
        image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'fake-fixture')
        result = parse_source(self.root, 'cowork')
        message = next(m for m in result['sessions'][0]['messages'] if m['kind'] == 'text')
        self.assertEqual(len(message['media_keys']), 1)
        self.assertIn('/mnt/user-data/outputs/plot.png', message['metadata']['image_references'])
        self.assertEqual(result['media'][message['media_keys'][0]], base + '/outputs/plot.png')

    def test_neutral_interchange_retains_local_image(self):
        image = self.root / 'media/picture.png'
        image.parent.mkdir()
        image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'fictional')
        self.json('conversation.json', {'format': 'agent-library.conversation', 'version': 1, 'media': {'old-key': 'media/picture.png', 'remote-key': 'https://example.invalid/remote.png'}, 'sessions': [{'id': 'c', 'messages': [{'id': 'img', 'role': 'assistant', 'kind': 'image', 'text': '', 'media_keys': ['old-key', 'remote-key']}]}]})
        result = parse_source(self.root)
        self.assertEqual(len(result['media']), 1)
        self.assertEqual(len(result['sessions'][0]['messages'][0]['media_keys']), 1)

    def test_canonical_bad_metadata_fails_with_explanation(self):
        path = self.json('conversation.json', {'format': 'agent-library.conversation', 'version': 1, 'sessions': [{'id': 'c', 'metadata': 'bad', 'messages': []}]})
        with self.assertRaisesRegex(ValueError, 'metadata'):
            parse_source(path)

    def test_unrelated_local_named_code_root_remains_code(self):
        self.jsonl('local_project/session.jsonl', [self.row('a', 'A normal Code reply')])
        result = parse_source(self.root)
        self.assertEqual(result['provider'], 'claude_code')



if __name__ == '__main__':
    unittest.main()
