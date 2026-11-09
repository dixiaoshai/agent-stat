"""Synthetic coverage for prompt-derived turn names and identity."""
import json
import tempfile
import unittest
from pathlib import Path

from agent_stats.codex import parse_rollout
from agent_stats.core import Store


def event(kind, payload, second):
    return {
        'timestamp': '2026-11-08T00:00:%02dZ' % second,
        'type': kind,
        'payload': payload,
    }


def message(text, second):
    return event('event_msg', {'type': 'user_message', 'message': text}, second)


def response(parts, second):
    return event('response_item', {'type': 'message', 'role': 'user', 'content': parts}, second)


def start(identity, second):
    return event('event_msg', {'type': 'task_started', 'turn_id': identity}, second)


def end(identity, second):
    return event('event_msg', {'type': 'task_complete', 'turn_id': identity}, second)


class TurnTitleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'rollout.jsonl'

    def write(self, rows):
        rows = [event('session_meta', {'id': 'session-a', 'cwd': '/synthetic'}, 0)] + rows
        self.path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
        return self.path

    def test_messages_follow_lifecycle_and_no_title_leaks_to_other_turns(self):
        self.write([
            message('First\n  prompt', 1),
            start('turn-a', 2),
            message('Additional instruction', 3),
            end('turn-a', 4),
            start('turn-b', 5),
            message('Second prompt', 6),
            end('turn-b', 7),
            start('turn-c', 8),
            end('turn-c', 9),
        ])
        session, _ = parse_rollout(self.path)
        self.assertEqual([(t['id'], t['title'], t['sequence']) for t in session['turns']], [
            ('turn-a', 'First prompt', 1),
            ('turn-b', 'Second prompt', 2),
            ('turn-c', None, 3),
        ])

    def test_user_response_ignores_injected_context_and_encrypted_text(self):
        self.write([
            response([
                {'type': 'input_text', 'text': '# AGENTS.md instructions\n\n<INSTRUCTIONS>\nInjected instructions\n</INSTRUCTIONS>'},
                {'type': 'input_text', 'text': '<environment_context>\nPrivate environment\n</environment_context>'},
                {'type': 'input_text', 'text': '<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>'},
                {'type': 'input_text', 'text': '  A real\n prompt  '},
            ], 1),
            start('turn-a', 2),
            end('turn-a', 3),
            start('turn-b', 4),
            response([{'type': 'encrypted_content', 'encrypted_content': 'ciphertext'}], 5),
            end('turn-b', 6),
        ])
        session, _ = parse_rollout(self.path)
        self.assertEqual([(t['id'], t['title']) for t in session['turns']], [
            ('turn-a', 'A real prompt'), ('turn-b', None),
        ])

    def test_new_task_plaintext_is_used_but_other_communications_are_not(self):
        def agent_message(text, second, extra=None):
            content = [{'type': 'input_text', 'text': text}]
            if extra:
                content.append(extra)
            return event('response_item', {'type': 'agent_message', 'content': content}, second)

        self.write([
            start('turn-a', 1),
            agent_message('Message Type: NEW_TASK\nTask name: /root/review\nSender: /root\nPayload:\nReview the changes', 2),
            agent_message('Message Type: MESSAGE\nPayload:\nIgnore this update', 3),
            end('turn-a', 4),
            start('turn-b', 5),
            agent_message('Message Type: NEW_TASK\nPayload:\n', 6,
                          {'type': 'encrypted_content', 'encrypted_content': 'ciphertext'}),
            agent_message('Message Type: MESSAGE\nPayload:\nDo not use this', 7),
            end('turn-b', 8),
        ])
        session, _ = parse_rollout(self.path)
        self.assertEqual([(t['id'], t['title']) for t in session['turns']], [
            ('turn-a', 'Review the changes'), ('turn-b', None),
        ])

    def test_explicit_usage_first_establishes_the_turn_for_pending_prompt(self):
        self.write([
            message('Prompt A', 1),
            event('event_msg', {'type': 'token_count', 'turn_id': 'turn-a',
                                'info': {'total_token_usage': {'total_tokens': 42}}}, 2),
        ])
        session, _ = parse_rollout(self.path)
        self.assertEqual([(t['id'], t['title'], t['token_total']) for t in session['turns']], [
            ('turn-a', 'Prompt A', 42),
        ])

    def test_rate_limit_update_does_not_attach_prompt_to_previous_turn(self):
        self.write([
            event('turn_context', {'turn_id': 'turn-a', 'model': 'synthetic'}, 1),
            message('Prompt B', 2),
            event('event_msg', {'type': 'token_count', 'info': None,
                                'rate_limits': {'primary': {'used_percent': 5}}}, 3),
            event('turn_context', {'turn_id': 'turn-b', 'model': 'synthetic'}, 4),
            event('event_msg', {'type': 'token_count', 'info': {'total_token_usage': {
                'total_tokens': 42}}}, 5),
        ])
        session, warnings = parse_rollout(self.path)
        self.assertEqual([(t['id'], t['title'], t['token_total']) for t in session['turns']], [
            ('turn-a', None, None), ('turn-b', 'Prompt B', 42),
        ])
        self.assertEqual(warnings, [])

    def test_repeated_contexts_without_lifecycle_keep_prompt_on_next_turn(self):
        self.write([
            message('Prompt A', 1),
            event('turn_context', {'turn_id': 'turn-a', 'model': 'synthetic'}, 2),
            event('event_msg', {'type': 'token_count', 'info': {'total_token_usage': {
                'total_tokens': 20}}}, 3),
            message('Prompt B', 4),
            event('turn_context', {'turn_id': 'turn-b', 'model': 'synthetic'}, 5),
            event('event_msg', {'type': 'token_count', 'info': {'total_token_usage': {
                'total_tokens': 42}}}, 6),
        ])
        session, _ = parse_rollout(self.path)
        self.assertEqual([(t['id'], t['title'], t['token_total']) for t in session['turns']], [
            ('turn-a', 'Prompt A', 20), ('turn-b', 'Prompt B', 22),
        ])

    def test_store_preserves_ids_and_limits_persisted_prompt(self):
        long_prompt = 'A' * 100 + ' SECRET_PROMPT_TAIL'
        self.write([
            start('turn-a', 1),
            message(long_prompt, 2),
            event('turn_context', {'turn_id': 'turn-a', 'model': 'synthetic'}, 3),
            event('response_item', {'type': 'function_call', 'call_id': 'call-a',
                                    'name': 'shell', 'arguments': 'SECRET_TOOL_BODY'}, 4),
            event('response_item', {'type': 'message', 'role': 'assistant',
                                    'content': [{'type': 'output_text', 'text': 'SECRET_ASSISTANT_BODY'}]}, 5),
            event('event_msg', {'type': 'token_count', 'info': {'total_token_usage': {
                'input_tokens': 20, 'output_tokens': 10, 'total_tokens': 30}}}, 6),
            end('turn-a', 7),
        ])
        store = Store(self.root / 'stats.sqlite3')
        self.addCleanup(store.db.close)
        store.import_paths([self.path])
        overview = store.query()
        turn = overview['turns'][0]
        session = overview['sessions'][0]
        self.assertEqual(session['native_id'], 'session-a')
        self.assertEqual(turn['native_id'], 'turn-a')
        self.assertEqual(turn['session_id'], session['id'])
        self.assertEqual(turn['title'], 'A' * 79 + '…')
        self.assertEqual(turn['sequence'], 1)
        self.assertEqual(turn['token_total'], 30)
        detail_turn = store.detail(session['id'])['turns'][0]
        self.assertEqual((detail_turn['id'], detail_turn['title'], detail_turn['sequence']),
                         (turn['id'], turn['title'], turn['sequence']))
        stored = store.path.read_bytes()
        for secret in (b'SECRET_PROMPT_TAIL', b'SECRET_TOOL_BODY', b'SECRET_ASSISTANT_BODY'):
            self.assertNotIn(secret, stored)


if __name__ == '__main__':
    unittest.main()
