"""Fork history is not new usage, including its cache and cost categories."""
import json
import tempfile
import unittest
from pathlib import Path

from agent_stats.codex import parse_rollout
from agent_stats.core import Store


def event(kind, payload, second):
    return {'timestamp': '2026-11-08T00:00:%02dZ' % second,
            'type': kind, 'payload': payload}


def counts(inp, cached, out):
    return {'input_tokens': inp, 'cached_input_tokens': cached,
            'cache_write_input_tokens': 0, 'output_tokens': out,
            'reasoning_output_tokens': 0, 'total_tokens': inp + out}


def snapshot(total, last, second):
    return event('event_msg', {'type': 'token_count', 'info': {
        'total_token_usage': total, 'last_token_usage': last}}, second)


def context(tid, second):
    return event('turn_context', {'turn_id': tid, 'model': 'gpt-6.1-sol'}, second)


class ForkUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'stats.sqlite3')
        self.addCleanup(self.store.db.close)

    def write(self, identity, rows, fork='origin', version='0.166.0-alpha.6'):
        meta = {'id': identity, 'cli_version': version,
                'timestamp': '2026-11-08T00:00:10Z', 'forked_from_id': fork}
        path = self.root / (identity + '.jsonl')
        path.write_text(''.join(json.dumps(r) + '\n' for r in
                                [event('session_meta', meta, 10)] + rows))
        return path

    def test_fork_deducts_every_category_in_all_query_views_and_reimports(self):
        original = self.write('origin', [context('original', 11),
                              snapshot(counts(1000, 800, 100), counts(1000, 800, 100), 12)], fork=None)
        fork = self.write('fork', [context('new-a', 13),
                          snapshot(counts(1100, 850, 120), counts(100, 50, 20), 14),
                          # Replayed snapshots must not add last usage twice.
                          snapshot(counts(1100, 850, 120), counts(100, 50, 20), 15),
                          context('new-b', 16),
                          snapshot(counts(1300, 950, 160), counts(200, 100, 40), 17)])
        self.store.import_paths([original, fork])
        data = self.store.query()
        self.assertEqual(data['warnings'], [])
        self.assertEqual(data['summary']['token_total'], 1460)
        self.assertEqual(data['summary']['cache_usage']['input_known'], 1300)
        self.assertEqual(data['summary']['cache_usage']['cached_input_known'], 950)
        self.assertAlmostEqual(data['summary']['estimated_cost'], .002395)
        fork_session = next(s for s in data['sessions'] if s['native_id'] == 'fork')
        detail = self.store.detail(fork_session['id'])
        self.assertEqual([t['token_total'] for t in detail['turns']], [120, 240])
        self.assertEqual(detail['session']['forked_from_id'], json.dumps(
            ['codex', 'local', 'origin'], separators=(',', ':')))
        self.assertEqual(data['groups'][0]['token_total'], 1460)
        self.assertEqual(data['trend'][0]['token_total'], 1460)
        scoped = self.store.query({'start': '2026-11-08T00:00:16Z'})
        self.assertEqual(scoped['summary']['token_total'], 240)
        self.assertEqual(self.store.import_paths([original, fork])['imported'], 0)
        self.assertEqual(self.store.query()['summary']['token_total'], 1460)

    def test_copied_pre_fork_turns_tools_and_usage_are_excluded(self):
        path = self.write('fork', [context('copied', 1),
                          event('response_item', {'type': 'function_call', 'call_id': 'old', 'name': 'shell'}, 2),
                          snapshot(counts(1000, 800, 100), counts(1000, 800, 100), 3),
                          context('new', 11),
                          snapshot(counts(1100, 850, 120), counts(100, 50, 20), 12)])
        session, warnings = parse_rollout(path)
        self.assertEqual(warnings, [])
        self.assertEqual([(t['id'], t['token_total']) for t in session['turns']], [('new', 120)])
        self.assertEqual(session['turns'][0]['tools'], [])

    def test_missing_or_invalid_first_last_usage_never_counts_inherited_tokens(self):
        for identity, last in [('missing', None), ('invalid', counts(2000, 1800, 200))]:
            with self.subTest(identity=identity):
                path = self.write(identity, [context('new', 11),
                                  snapshot(counts(1100, 850, 120), last, 12),
                                  snapshot(counts(1300, 950, 160), counts(200, 100, 40), 13)])
                session, warnings = parse_rollout(path)
                t = session['turns'][0]
                self.assertIsNone(t['token_total'])
                self.assertIsNone(t['input_tokens'])
                self.assertIsNone(t['cached_input_tokens'])
                self.assertTrue(any('基线' in w for w in warnings))
                self.store.import_paths([path])
                detail = self.store.detail(json.dumps(['codex', 'local', identity], separators=(',', ':')))
                self.assertIsNone(detail['turns'][0]['estimated_cost'])

    def test_missing_baseline_does_not_prevent_later_separate_turn_from_being_known(self):
        path = self.write('fork', [context('unknown', 11),
                          snapshot(counts(1100, 850, 120), None, 12),
                          context('known', 13),
                          snapshot(counts(1300, 950, 160), counts(200, 100, 40), 14)])
        session, _ = parse_rollout(path)
        self.assertIsNone(session['turns'][0]['token_total'])
        self.assertEqual(session['turns'][1]['token_total'], 240)

    def test_category_first_seen_after_fork_is_unknown_for_that_turn(self):
        first = counts(1100, 850, 120)
        del first['cached_input_tokens']
        path = self.write('fork', [context('new', 11),
                          snapshot(first, counts(100, 50, 20), 12),
                          snapshot(counts(1300, 950, 160), counts(200, 100, 40), 13)])
        session, warnings = parse_rollout(path)
        self.assertEqual(session['turns'][0]['token_total'], 360)
        self.assertIsNone(session['turns'][0]['cached_input_tokens'])
        self.assertTrue(any('cached_input_tokens' in w for w in warnings))

    def test_new_version_requires_consistent_per_call_evidence(self):
        for version in (None, 'unverified-version'):
            with self.subTest(version=version):
                path = self.write('new', [context('own', 11),
                                  snapshot(counts(100, 50, 20), counts(100, 50, 20), 12),
                                  snapshot(counts(300, 150, 60), counts(200, 100, 40), 13)],
                                  fork=None, version=version)
                session, _ = parse_rollout(path)
                self.assertEqual(session['usage_accounting'], 'per_session')
        path = self.write('conflicting', [context('own', 11),
                          snapshot(counts(100, 50, 20), counts(100, 50, 20), 12),
                          snapshot(counts(400, 250, 60), counts(200, 100, 40), 13)], fork=None)
        session, _ = parse_rollout(path)
        self.assertEqual(session['usage_accounting'], 'unknown')

    def test_unexplained_initial_excess_does_not_verify_a_new_version(self):
        path = self.write('new', [context('own', 11),
                          snapshot(counts(1100, 850, 120), counts(100, 50, 20), 12),
                          snapshot(counts(1300, 950, 160), counts(200, 100, 40), 13)],
                          fork=None, version='unverified-version')
        session, _ = parse_rollout(path)
        self.assertEqual(session['usage_accounting'], 'unknown')

    def test_fork_creation_time_falls_back_to_metadata_event_time(self):
        path = self.write('fork', [context('copied', 1),
                          snapshot(counts(1000, 800, 100), counts(1000, 800, 100), 2),
                          context('own', 11),
                          snapshot(counts(1100, 850, 120), counts(100, 50, 20), 12)])
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        del rows[0]['payload']['timestamp']
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        session, warnings = parse_rollout(path)
        self.assertEqual(warnings, [])
        self.assertEqual([t['token_total'] for t in session['turns']], [120])
        # With neither timestamp available even a plausible last usage cannot
        # distinguish a copied call from new work.
        del rows[0]['timestamp']
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        session, warnings = parse_rollout(path)
        self.assertTrue(all(t['token_total'] is None for t in session['turns']))
        self.assertTrue(any('创建时间' in w for w in warnings))

    def test_unstamped_fork_usage_cannot_be_counted_as_new_work(self):
        copied = snapshot(counts(1000, 800, 100), counts(1000, 800, 100), 2)
        del copied['timestamp']
        path = self.write('fork', [context('copied', 1), copied,
                          context('own', 11),
                          snapshot(counts(1100, 850, 120), counts(100, 50, 20), 12)])
        session, warnings = parse_rollout(path)
        self.assertEqual(session['usage_accounting'], 'unknown')
        self.assertEqual([t['id'] for t in session['turns']], ['own'])
        self.assertIsNone(session['turns'][0]['token_total'])
        self.assertIsNone(session['turns'][0]['input_tokens'])
        self.assertIsNone(session['turns'][0]['cached_input_tokens'])
        self.assertTrue(any('继承历史' in w for w in warnings))

    def test_reset_does_not_restore_known_usage_when_crossing_old_high_water(self):
        path = self.write('fork', [context('first', 11),
                          snapshot(counts(1100, 850, 120), counts(100, 50, 20), 12),
                          context('reset', 13),
                          snapshot(counts(50, 0, 5), counts(50, 0, 5), 14),
                          context('after', 15),
                          snapshot(counts(1200, 900, 130), counts(1150, 900, 125), 16)])
        session, warnings = parse_rollout(path)
        self.assertEqual([t['token_total'] for t in session['turns']], [120, None, None])
        self.assertEqual(session['usage_accounting'], 'unknown')
        self.assertTrue(any('重置' in w for w in warnings))


if __name__ == '__main__':
    unittest.main()
