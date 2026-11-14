"""Behavioral coverage for the shared metadata archive consumer."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest

from agent_stats.archive import import_batch
from agent_stats.core import Store


def record(seq, kind, data=None, sid='synthetic'):
    key = json.dumps(['codex', 'local', sid], separators=(',', ':'))
    native = json.dumps(['codex', 'local', sid, 'deleted' if kind == 'session.deleted' else seq], ensure_ascii=False, separators=(',', ':'))
    return {'schema_version': 1, 'event_id': hashlib.sha256(native.encode('utf-8')).hexdigest(), 'archive_seq': seq,
            'runtime': 'codex', 'instance_id': 'local', 'session_id': sid, 'session_key': key,
            'project_id': 'demo', 'kind': kind, 'occurred_at': '2026-01-01T10:00:%02dZ' % seq,
            'observed_at': '2026-01-01T10:01:00Z', 'native_type': kind,
            'source': None if kind == 'session.deleted' else {'path': '/demo/rollout.jsonl', 'line': seq, 'record_hash': format(seq + 30, '064x')},
            'data': data or {}, 'body': None, 'raw': None}


def page(start, end, events):
    return {'schema_version': 1, 'archive_id': 'archive-demo', 'view': 'metadata', 'after_cursor': start,
            'next_cursor': end, 'has_more': False, 'events': events}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'stats.sqlite3')
        self.rows = [
            record(1, 'session.started', {'cwd': '/demo', 'created_at': '2026-01-01T10:00:00Z'}),
            record(2, 'turn.started', {'turn_id': 'turn'}),
            record(3, 'turn.context', {'turn_id': 'turn', 'model': 'demo'}),
            record(4, 'usage.snapshot', {'turn_id': 'turn', 'cumulative': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120},
                                         'last_usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}}),
            record(5, 'tool.called', {'turn_id': 'turn', 'call_id': 'call', 'name': 'shell', 'input_bytes': 50}),
            record(6, 'tool.returned', {'turn_id': 'turn', 'call_id': 'call', 'output_bytes': 12, 'status': 'result_observed'}),
            record(7, 'turn.completed', {'turn_id': 'turn'}),
        ]

    def test_usage_matches_direct_source_and_replay(self):
        source = self.root / 'rollout.jsonl'
        native = [
            {'type': 'session_meta', 'payload': {'id': 'synthetic', 'cwd': '/demo', 'timestamp': '2026-01-01T10:00:00Z'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn'}},
            {'type': 'turn_context', 'payload': {'turn_id': 'turn', 'model': 'demo'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'turn_id': 'turn', 'info': {'total_token_usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}, 'last_token_usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}}}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'turn_id': 'turn', 'name': 'shell', 'call_id': 'call', 'arguments': 'X' * 50}},
            {'type': 'response_item', 'payload': {'type': 'function_call_output', 'turn_id': 'turn', 'call_id': 'call', 'output': 'X' * 12}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn'}},
        ]
        for i, row in enumerate(native, 1):
            row['timestamp'] = '2026-01-01T10:00:%02dZ' % i
        source.write_text(''.join(json.dumps(x) + '\n' for x in native))
        self.store.import_paths([source])
        expected = self.store.query()['summary']
        batch = page(0, 7, self.rows)
        self.assertEqual(import_batch(self.store, batch)['cursor'], 7)
        actual = self.store.query()['summary']
        self.assertEqual(actual['sessions'], 1)
        self.assertEqual(actual['token_total'], expected['token_total'])
        self.assertEqual(actual['tool_calls'], expected['tool_calls'])
        self.assertTrue(import_batch(self.store, batch)['replayed'])
        self.assertEqual(self.store.query()['summary']['token_total'], 120)

    def test_privacy_gap_and_conflict_are_atomic(self):
        import_batch(self.store, page(0, 3, self.rows[:3]))
        bad = record(4, 'message.user', {'title': 'safe'})
        bad['body'] = {'text': 'SECRET_SENTINEL'}
        with self.assertRaises(ValueError):
            import_batch(self.store, page(3, 4, [bad]))
        self.assertNotIn(b'SECRET_SENTINEL', (self.root / 'stats.sqlite3').read_bytes())
        with self.assertRaises(ValueError):
            import_batch(self.store, page(4, 7, self.rows[4:]))
        conflict = page(0, 3, self.rows[:3]); conflict['events'][2]['data']['model'] = 'other'
        with self.assertRaises(ValueError):
            import_batch(self.store, conflict)
        self.assertEqual(self.store.db.execute('SELECT cursor FROM archive_cursors').fetchone()[0], 3)

    def test_deletion_and_resurrection(self):
        import_batch(self.store, page(0, 7, self.rows))
        tombstone = record(8, 'session.deleted')
        import_batch(self.store, page(7, 8, [tombstone]))
        self.assertEqual(self.store.query()['summary']['sessions'], 0)
        with self.assertRaises(ValueError):
            import_batch(self.store, page(8, 9, [record(9, 'session.started')]))
        self.assertEqual(self.store.db.execute('SELECT cursor FROM archive_cursors').fetchone()[0], 8)

    def test_direct_source_cannot_restore_deleted_archive_session(self):
        source = self.root / 'rollout.jsonl'
        native = [
            {'timestamp': '2026-01-01T10:00:01Z', 'type': 'session_meta',
             'payload': {'id': 'synthetic', 'cwd': '/demo'}},
            {'timestamp': '2026-01-01T10:00:02Z', 'type': 'event_msg',
             'payload': {'type': 'task_started', 'turn_id': 'turn'}},
        ]
        source.write_text(''.join(json.dumps(row) + '\n' for row in native), encoding='utf-8')
        import_batch(self.store, page(0, 7, self.rows))
        # The same source cannot replace the owned archive projection either.
        report = self.store.import_paths([source])
        self.assertEqual(report['skipped'], 1)
        self.assertFalse(report['errors'])
        self.assertEqual(self.store.query()['summary']['token_total'], 120)
        import_batch(self.store, page(7, 8, [record(8, 'session.deleted')]))
        report = self.store.import_paths([source])
        self.assertEqual(report['scanned'], 1)
        self.assertEqual(report['skipped'], 1)
        self.assertFalse(report['errors'])
        self.assertEqual(self.store.query()['summary']['sessions'], 0)
        self.assertEqual(Store(self.root / 'stats.sqlite3').query()['summary']['sessions'], 0)

    def test_cli_import_archive_summary_and_replay(self):
        batch_file = self.root / 'batch.json'
        batch_file.write_text(json.dumps(page(0, 7, self.rows)), encoding='utf-8')
        db_file = self.root / 'cli.sqlite3'
        base = [sys.executable, '-m', 'agent_stats', '--db', str(db_file)]
        def run(*args):
            result = subprocess.run(base + list(args), capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        first = run('import-archive', '--input', str(batch_file))
        self.assertEqual(first['cursor'], 7)
        self.assertFalse(first['replayed'])
        self.assertEqual(run('summary')['summary']['token_total'], 120)
        second = run('import-archive', '--input', str(batch_file))
        self.assertTrue(second['replayed'])
        self.assertEqual(second['imported'], 0)
        summary = run('summary')['summary']
        self.assertEqual(summary['sessions'], 1)
        self.assertEqual(summary['token_total'], 120)
        self.assertEqual(summary['tool_calls'], 1)

    def test_archive_ownership_conflict_is_atomic(self):
        import_batch(self.store, page(0, 3, self.rows[:3]))
        competing = page(0, 1, [self.rows[0]])
        competing['archive_id'] = 'another-archive'
        with self.assertRaises(ValueError):
            import_batch(self.store, competing)
        self.assertIsNone(self.store.db.execute('SELECT cursor FROM archive_cursors WHERE archive_id=?', ('another-archive',)).fetchone())
        self.assertEqual(self.store.query()['summary']['sessions'], 1)

    def test_metadata_allowlist_and_resume(self):
        import_batch(self.store, page(0, 3, self.rows[:3]))
        bad = record(4, 'source.unknown', {'content': 'SECRET_SENTINEL'})
        with self.assertRaises(ValueError):
            import_batch(self.store, page(3, 4, [bad]))
        import_batch(self.store, page(3, 7, self.rows[3:]))
        self.assertEqual(self.store.query()['summary']['token_total'], 120)


    def test_session_title_is_kept_and_bounded(self):
        titled = json.loads(json.dumps(self.rows))
        titled[0]['data']['title'] = '共享会话名称'
        import_batch(self.store, page(0, 7, titled))
        self.assertEqual(self.store.query()['sessions'][0]['title'], '共享会话名称')

        bad = record(8, 'session.started', {'cwd': '/other', 'title': 'two\nlines'}, sid='other')
        with self.assertRaises(ValueError):
            import_batch(self.store, page(7, 8, [bad]))
        self.assertEqual(self.store.db.execute('SELECT cursor FROM archive_cursors').fetchone()[0], 7)

        too_long = record(8, 'session.started', {'cwd': '/other', 'title': 'x' * 81}, sid='other')
        with self.assertRaises(ValueError):
            import_batch(self.store, page(7, 8, [too_long]))

    def test_missing_title_stays_unknown(self):
        import_batch(self.store, page(0, 7, self.rows))
        self.assertIsNone(self.store.query()['sessions'][0]['title'])


    def test_folded_source_series_is_part_of_the_identifier(self):
        start = record(1, 'session.started', {'cwd': '/demo', 'created_at': '2026-01-01T10:00:00Z'})
        folded = record(2, 'turn.started', {'turn_id': 'turn'})
        folded['source']['series'] = 'child-identity'
        native = json.dumps(['codex', 'local', 'synthetic', 'child-identity', 2],
                            ensure_ascii=False, separators=(',', ':'))
        folded['event_id'] = hashlib.sha256(native.encode('utf-8')).hexdigest()
        import_batch(self.store, page(0, 2, [start, folded]))
        self.assertEqual(self.store.db.execute('SELECT cursor FROM archive_cursors').fetchone()[0], 2)

        bad = record(3, 'turn.started', {'turn_id': 'turn'})
        bad['source']['series'] = 'synthetic'
        with self.assertRaises(ValueError):
            import_batch(self.store, page(2, 3, [bad]))
        empty = record(3, 'turn.started', {'turn_id': 'turn'})
        empty['source']['series'] = ''
        with self.assertRaises(ValueError):
            import_batch(self.store, page(2, 3, [empty]))
        self.assertEqual(self.store.db.execute('SELECT cursor FROM archive_cursors').fetchone()[0], 2)

    def test_archive_rebuild_keeps_the_same_revision_as_direct_import(self):
        def event(seq, kind, data, series, occurred, path, sid='shared-session'):
            row = record(seq, kind, data, sid)
            row['occurred_at'] = occurred
            row['source'] = {'path': path, 'line': seq, 'record_hash': format(seq + 70, '064x'), 'series': series}
            native = json.dumps(['codex', 'local', sid, series, seq], ensure_ascii=False, separators=(',', ':'))
            row['event_id'] = hashlib.sha256(native.encode('utf-8')).hexdigest()
            return row

        def usage(cumulative, last):
            return {'turn_id': last, 'cumulative': {k: v for k, v in cumulative.items()},
                    'last_usage': dict(last_usage)}

        # Two rollouts of one session: the shorter one ends first, the longer
        # one has a later revision and an extra turn.
        short = [
            event(1, 'session.started', {'cwd': '/demo', 'created_at': '2026-01-01T10:00:00Z'}, 'short-file', '2026-01-01T10:00:00Z', '/demo/short.jsonl'),
            event(2, 'turn.started', {'turn_id': 't1'}, 'short-file', '2026-01-01T10:00:01Z', '/demo/short.jsonl'),
            event(3, 'turn.context', {'turn_id': 't1', 'model': 'demo'}, 'short-file', '2026-01-01T10:00:02Z', '/demo/short.jsonl'),
            event(4, 'usage.snapshot', {'turn_id': 't1', 'cumulative': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120},
                                        'last_usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}},
                  'short-file', '2026-01-01T10:00:03Z', '/demo/short.jsonl'),
            event(5, 'turn.completed', {'turn_id': 't1'}, 'short-file', '2026-01-01T10:00:04Z', '/demo/short.jsonl'),
        ]
        long = [
            event(1, 'session.started', {'cwd': '/demo', 'created_at': '2026-01-01T10:00:00Z'}, 'long-file', '2026-01-01T10:00:00Z', '/demo/long.jsonl'),
            event(2, 'turn.started', {'turn_id': 't1'}, 'long-file', '2026-01-01T10:00:01Z', '/demo/long.jsonl'),
            event(3, 'turn.context', {'turn_id': 't1', 'model': 'demo'}, 'long-file', '2026-01-01T10:00:02Z', '/demo/long.jsonl'),
            event(4, 'usage.snapshot', {'turn_id': 't1', 'cumulative': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120},
                                        'last_usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}},
                  'long-file', '2026-01-01T10:00:03Z', '/demo/long.jsonl'),
            event(5, 'turn.completed', {'turn_id': 't1'}, 'long-file', '2026-01-01T10:00:04Z', '/demo/long.jsonl'),
            event(6, 'turn.started', {'turn_id': 't2'}, 'long-file', '2026-01-01T10:05:00Z', '/demo/long.jsonl'),
            event(7, 'turn.context', {'turn_id': 't2', 'model': 'demo'}, 'long-file', '2026-01-01T10:05:01Z', '/demo/long.jsonl'),
            event(8, 'usage.snapshot', {'turn_id': 't2', 'cumulative': {'input_tokens': 150, 'output_tokens': 20, 'total_tokens': 170},
                                        'last_usage': {'input_tokens': 50, 'output_tokens': 0, 'total_tokens': 50}},
                  'long-file', '2026-01-01T10:05:02Z', '/demo/long.jsonl'),
            event(9, 'turn.completed', {'turn_id': 't2'}, 'long-file', '2026-01-01T10:05:03Z', '/demo/long.jsonl'),
        ]
        for index, row in enumerate(long, start=6):
            row['archive_seq'] = index
            row['source']['line'] = index
            native_id = json.dumps(['codex', 'local', 'shared-session', 'long-file', index],
                                   ensure_ascii=False, separators=(',', ':'))
            row['event_id'] = hashlib.sha256(native_id.encode('utf-8')).hexdigest()
        import_batch(self.store, page(0, 5, short))
        import_batch(self.store, page(5, 14, long))

        native = [
            {'type': 'session_meta', 'payload': {'id': 'shared-session', 'cwd': '/demo', 'timestamp': '2026-01-01T10:00:00Z'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 't1'}},
            {'type': 'turn_context', 'payload': {'turn_id': 't1', 'model': 'demo'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'turn_id': 't1', 'info': {'total_token_usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}, 'last_token_usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}}}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 't1'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 't2'}},
            {'type': 'turn_context', 'payload': {'turn_id': 't2', 'model': 'demo'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'turn_id': 't2', 'info': {'total_token_usage': {'input_tokens': 150, 'output_tokens': 20, 'total_tokens': 170}, 'last_token_usage': {'input_tokens': 50, 'output_tokens': 0, 'total_tokens': 50}}}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 't2'}},
        ]
        for index, row in enumerate(native, 1):
            row['timestamp'] = '2026-01-01T10:0%d:0%dZ' % (index, index)
        source = self.root / 'long.jsonl'
        source.write_text(''.join(json.dumps(row) + '\n' for row in native), encoding='utf-8')
        direct = Store(self.root / 'direct.sqlite3')
        self.addCleanup(direct.db.close)
        direct.import_paths([source])
        expected = direct.query()['summary']
        actual = self.store.query()['summary']
        self.assertEqual(expected['token_total'], 170)
        self.assertEqual(actual['token_total'], expected['token_total'])
        self.assertEqual(actual['token_known'], expected['token_known'])
        self.assertEqual(actual['turns'], expected['turns'])
        self.assertEqual(self.store.query()['sessions'][0]['turns'], 2)
