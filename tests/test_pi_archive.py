"""Pi metadata archive consumption: per-request usage, recorded cost and forks."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agent_stats.archive import import_batch
from agent_stats.core import Store


def digest(value):
    return hashlib.sha256(json.dumps(value, separators=(',', ':')).encode('utf-8')).hexdigest()


def record(seq, kind, data=None, sid='pi-session', runtime='pi', occurred=None, line=None):
    line = line or seq
    key = json.dumps([runtime, 'local', sid], separators=(',', ':'))
    return {
        'schema_version': 1, 'event_id': digest([runtime, sid, kind, line]),
        'archive_seq': seq, 'runtime': runtime, 'instance_id': 'local', 'session_id': sid,
        'session_key': key, 'project_id': 'demo', 'kind': kind,
        'occurred_at': occurred or '2026-01-01T10:00:%02dZ' % seq,
        'observed_at': '2026-01-01T10:01:00Z', 'native_type': kind,
        'source': None if kind == 'session.deleted' else {
            'path': '/demo/pi-session.jsonl', 'line': line, 'record_hash': format(seq + 40, '064x'),
        },
        'data': data or {}, 'body': None, 'raw': None,
    }


def page(start, end, events):
    return {'schema_version': 1, 'archive_id': 'archive-pi', 'view': 'metadata', 'after_cursor': start,
            'next_cursor': end, 'has_more': False, 'events': events}


def usage(turn, cumulative, last, model='deepseek-flash', cost=None, source='model_rates'):
    data = {'turn_id': turn, 'root_turn_id': None, 'model': model, 'cumulative': cumulative,
            'last_usage': last, 'rate_limit_only': False}
    if cost is not None:
        data.update(cost_usd=cost, cost_source=source)
    return data


class PiArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'stats.sqlite3')

    def test_per_request_usage_and_recorded_cost(self):
        first = {'input_tokens': 150, 'cached_input_tokens': 50, 'output_tokens': 20, 'total_tokens': 170}
        second = {'input_tokens': 210, 'cached_input_tokens': 10, 'output_tokens': 30, 'total_tokens': 240}
        rows = [
            record(1, 'session.started', {'cwd': '/demo', 'created_at': '2026-01-01T10:00:00Z', 'title': 'Pi demo'}),
            record(2, 'message.user', {'turn_id': 'u1', 'title': 'Hello'}),
            record(3, 'turn.started', {'turn_id': 'u1'}),
            record(4, 'usage.snapshot', usage('u1', first, first, cost=0.001)),
            record(5, 'tool.called', {'turn_id': 'u1', 'call_id': 'c1', 'name': 'bash', 'input_bytes': 10}),
            record(6, 'tool.returned', {'turn_id': 'u1', 'call_id': 'c1', 'output_bytes': 5, 'status': 'result_observed'}),
            record(7, 'usage.snapshot', usage('u1', {
                'input_tokens': 360, 'cached_input_tokens': 60, 'output_tokens': 50, 'total_tokens': 410,
            }, second, cost=0.002, source='provider')),
            record(8, 'turn.completed', {'turn_id': 'u1'}),
        ]
        self.assertEqual(import_batch(self.store, page(0, 8, rows))['cursor'], 8)
        data = self.store.query()
        summary = data['summary']
        self.assertEqual(summary['sessions'], 1)
        self.assertEqual(summary['turns'], 1)
        self.assertEqual(summary['token_total'], 410)
        self.assertEqual(summary['tool_calls'], 1)
        turn = data['turns'][0]
        self.assertEqual(turn['model'], 'deepseek-flash')
        self.assertEqual(turn['input_tokens'], 360)
        self.assertEqual(turn['cached_input_tokens'], 60)
        self.assertEqual(turn['output_tokens'], 50)
        self.assertEqual(turn['token_total'], 410)
        self.assertEqual(turn['status'], 'completed')
        # Recorded request costs are summed per turn and not re-estimated.
        self.assertAlmostEqual(turn['estimated_cost'], 0.003)
        self.assertEqual(turn['cost_basis'], 'recorded')
        self.assertEqual(turn['cost_source'], 'mixed')
        self.assertAlmostEqual(summary['recorded_cost_known'], 0.003)
        self.assertEqual(summary['recorded_cost_samples'], 1)
        self.assertEqual(summary['cost_basis'], 'recorded')
        session = data['sessions'][0]
        self.assertEqual(session['source'], 'pi')
        self.assertEqual(session['title'], 'Pi demo')
        detail = self.store.detail(session['id'])['session']
        self.assertEqual(detail['usage_accounting'], 'per_session')
        filtered = self.store.query({'source': 'pi'})
        self.assertEqual(filtered['summary']['sessions'], 1)
        self.assertEqual(self.store.query({'source': 'codex'})['summary']['sessions'], 0)

    def test_fork_skips_inherited_usage_and_cost(self):
        inherited = {'input_tokens': 1000, 'output_tokens': 100, 'total_tokens': 1100}
        current = {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}
        rows = [
            record(1, 'session.started', {'cwd': '/demo', 'created_at': '2026-01-01T10:05:00Z',
                                          'forked_from_session_id': 'parent'}, occurred='2026-01-01T10:05:00Z'),
            record(2, 'message.user', {'turn_id': 'old'}, occurred='2026-01-01T10:00:00Z'),
            record(3, 'usage.snapshot', usage('old', inherited, inherited, cost=9.0),
                   occurred='2026-01-01T10:00:01Z'),
            record(4, 'message.user', {'turn_id': 'u1'}, occurred='2026-01-01T10:05:01Z'),
            record(5, 'turn.started', {'turn_id': 'u1'}, occurred='2026-01-01T10:05:01Z'),
            # The adapter's cumulative still includes copied history; the
            # consumer trusts last_usage instead of charging the inherited part.
            record(6, 'usage.snapshot', usage('u1', {
                'input_tokens': 1100, 'output_tokens': 120, 'total_tokens': 1220,
            }, current, cost=0.5), occurred='2026-01-01T10:05:02Z'),
            record(7, 'turn.completed', {'turn_id': 'u1'}, occurred='2026-01-01T10:05:03Z'),
        ]
        import_batch(self.store, page(0, 7, rows))
        data = self.store.query()
        self.assertEqual(data['summary']['token_total'], 120)
        self.assertEqual(data['summary']['turns'], 1)
        self.assertEqual(json.loads(data['turns'][0]['id'])[-1], 'u1')
        self.assertAlmostEqual(data['turns'][0]['estimated_cost'], 0.5)
        self.assertAlmostEqual(data['summary']['recorded_cost_known'], 0.5)
        detail = self.store.detail(data['sessions'][0]['id'])['session']
        self.assertEqual(detail['parent_id'], None)
        self.assertIsNotNone(detail['forked_from_id'])

    def test_cost_fields_and_runtime_are_validated(self):
        base = [record(1, 'session.started', {'cwd': '/demo', 'created_at': '2026-01-01T10:00:00Z'})]
        bad_runtime = record(2, 'usage.snapshot', usage('u', {'input_tokens': 1, 'total_tokens': 1},
                                                        {'input_tokens': 1, 'total_tokens': 1}), runtime='deepseek')
        with self.assertRaises(ValueError):
            import_batch(self.store, page(0, 2, base + [bad_runtime]))

        negative = record(2, 'usage.snapshot', usage('u', {'input_tokens': 1, 'total_tokens': 1},
                                                     {'input_tokens': 1, 'total_tokens': 1}, cost=-1))
        with self.assertRaises(ValueError):
            import_batch(self.store, page(0, 2, base + [negative]))
        bad_source = record(2, 'usage.snapshot', usage('u', {'input_tokens': 1, 'total_tokens': 1},
                                                       {'input_tokens': 1, 'total_tokens': 1},
                                                       cost=0.1, source='invoice'))
        with self.assertRaises(ValueError):
            import_batch(self.store, page(0, 2, base + [bad_source]))
        misplaced = record(2, 'message.user', {'turn_id': 'u', 'cost_usd': 1})
        with self.assertRaises(ValueError):
            import_batch(self.store, page(0, 2, base + [misplaced]))
        # A pi or dsh event id is adapter-defined; the Codex line-number formula is not applied.
        ok = record(2, 'usage.snapshot', usage('u', {'input_tokens': 1, 'total_tokens': 1},
                                               {'input_tokens': 1, 'total_tokens': 1}, cost=0))
        import_batch(self.store, page(0, 2, base + [ok]))
        self.assertEqual(self.store.query()['summary']['token_total'], 1)

    def test_dsh_runtime_is_accepted_with_per_request_usage(self):
        rows = [
            record(1, 'session.started', {'cwd': '/demo', 'created_at': '2026-01-01T10:00:00Z'},
                   sid='session-demo', runtime='dsh'),
            record(2, 'turn.started', {'turn_id': '1'}, sid='session-demo', runtime='dsh'),
            record(3, 'usage.snapshot', usage('1', {'input_tokens': 10, 'total_tokens': 10},
                                              {'input_tokens': 10, 'total_tokens': 10}),
                   sid='session-demo', runtime='dsh'),
            record(4, 'turn.completed', {'turn_id': '1'}, sid='session-demo', runtime='dsh'),
        ]
        import_batch(self.store, page(0, 4, rows))
        data = self.store.query()
        self.assertEqual(data['summary']['token_total'], 10)
        self.assertEqual(data['sessions'][0]['source'], 'dsh')
        detail = self.store.detail(data['sessions'][0]['id'])['session']
        self.assertEqual(detail['usage_accounting'], 'per_session')

    def test_pi_deletion_removes_projection(self):
        rows = [
            record(1, 'session.started', {'cwd': '/demo', 'created_at': '2026-01-01T10:00:00Z'}),
            record(2, 'turn.started', {'turn_id': 'u'}),
            record(3, 'usage.snapshot', usage('u', {'input_tokens': 10, 'total_tokens': 10},
                                              {'input_tokens': 10, 'total_tokens': 10})),
        ]
        import_batch(self.store, page(0, 3, rows))
        deleted = record(4, 'session.deleted', sid='pi-session')
        import_batch(self.store, page(3, 4, [deleted]))
        self.assertEqual(self.store.query()['summary']['sessions'], 0)
        with self.assertRaises(ValueError):
            import_batch(self.store, page(3, 4, [record(5, 'usage.snapshot', usage(
                'u', {'input_tokens': 1, 'total_tokens': 1}, {'input_tokens': 1, 'total_tokens': 1}))]))


if __name__ == '__main__':
    unittest.main()
