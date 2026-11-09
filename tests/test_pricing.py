"""Cost equivalents: cache categories, missing data, scope, and parent overlap."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_stats.core import Store
from agent_stats.pricing import PRICING, aggregate_cost, estimate_turn


def turn(**overrides):
    return {
        'id': 'turn-a', 'model': 'gpt-6.1-sol', 'input_tokens': 1000000,
        'cached_input_tokens': 900000, 'output_tokens': 100000,
        'reasoning_output_tokens': 70000, 'token_total': 1100000,
        'cache_write_input_tokens': None, 'tools': [],
        'started_at': '2026-11-08T01:00:00Z', **overrides,
    }


class PricingTests(unittest.TestCase):
    def test_cached_input_and_reasoning_are_not_double_charged(self):
        original = turn()
        result = estimate_turn(original)
        self.assertEqual(result['estimated_cost'], 1.29)
        self.assertEqual(result['cost_breakdown'], {'input': .2, 'cached_input': .09, 'output': 1})
        self.assertNotIn('estimated_cost', original)

    def test_unknown_models_and_incomplete_or_inconsistent_categories(self):
        cases = [
            {'model': 'codex-auto-review'}, {'model': 'gpt-6.1-sol-other'},
            {'model': '混合模型'}, {'cached_input_tokens': None},
            {'input_tokens': None}, {'output_tokens': -1}, {'input_tokens': True},
            {'cached_input_tokens': 1000001}, {'token_total': 100},
            {'cache_write_input_tokens': 10}, {'cache_write_input_tokens': -1},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                result = estimate_turn(turn(**overrides))
                self.assertIsNone(result['estimated_cost'])
                self.assertIsNone(result['cost_breakdown'])
                self.assertTrue(result['estimated_cost_reason'])

    def test_zero_and_small_values_remain_known(self):
        result = estimate_turn(turn(input_tokens=0, cached_input_tokens=0, output_tokens=0,
                                    token_total=0, cache_write_input_tokens=0))
        self.assertEqual(result['estimated_cost'], 0)
        self.assertIsNone(result['estimated_cost_reason'])
        tiny = estimate_turn(turn(model='gpt-6-luna', input_tokens=1,
                                  cached_input_tokens=1, output_tokens=0, token_total=1))
        self.assertEqual(tiny['estimated_cost'], .00000001)

    def test_aggregate_distinguishes_unknown_partial_and_observed(self):
        known, missing = estimate_turn(turn()), estimate_turn(turn(model='unknown'))
        complete = aggregate_cost([known])
        self.assertEqual(complete['estimated_cost'], 1.29)
        partial = aggregate_cost([known, missing])
        self.assertIsNone(partial['estimated_cost'])
        self.assertEqual(partial['estimated_cost_known'], 1.29)
        self.assertEqual(partial['cost_samples'], 1)
        for rows in ([], [missing]):
            self.assertIsNone(aggregate_cost(rows)['estimated_cost_known'])
        blocked = aggregate_cost([known], blocked=True)
        self.assertIsNone(blocked['estimated_cost'])
        self.assertIsNone(blocked['estimated_cost_known'])
        self.assertEqual(blocked['observed_estimated_cost_known'], 1.29)

    def test_store_estimates_each_model_and_respects_detail_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'stats.sqlite3')
            self.addCleanup(store.db.close)
            second = turn(id='turn-b', model='gpt-6-luna', started_at='2026-11-09T01:00:00Z')
            facts = {'id': 'parent', 'native_id': 'parent', 'source': 'codex', 'instance': 'test',
                     'project': '/synthetic', 'turns': [turn(), second]}
            child = {**facts, 'id': 'child', 'parent_id': 'parent', 'turns': [turn()]}
            for data in (facts, child):
                store.db.execute('INSERT INTO sessions VALUES(?,?,?)', (data['id'], 0, json.dumps(data)))
            store.db.commit()
            all_data = store.query()
            self.assertTrue(all_data['summary']['aggregation_blocked'])
            self.assertIsNone(all_data['summary']['cost'])  # Actual billing remains unknown.
            self.assertIsNone(all_data['summary']['estimated_cost_known'])
            self.assertEqual(all_data['summary']['observed_estimated_cost_known'], 2.649)
            self.assertEqual(all_data['summary']['cost_samples'], 3)
            filters = {'start': '2026-11-09T00:00:00Z'}
            scoped = store.query(filters)
            self.assertFalse(scoped['summary']['aggregation_blocked'])
            self.assertEqual(scoped['summary']['estimated_cost'], .069)
            self.assertEqual(scoped['sessions'][0]['estimated_cost'], .069)
            self.assertEqual(scoped['groups'][0]['estimated_cost'], .069)
            self.assertEqual(scoped['trend'][0]['estimated_cost'], .069)
            detail = store.detail('parent', filters)
            self.assertEqual(len(detail['turns']), 1)
            self.assertEqual(detail['turns'][0]['estimated_cost'], .069)
            self.assertEqual(detail['session']['estimated_cost'], .069)
            # Rates apply at query time, independent of persisted usage revisions.
            with patch.dict(PRICING['models'], {'gpt-6-luna': {'input': 1, 'cached_input': .1, 'output': 5}}):
                self.assertEqual(store.query(filters)['summary']['estimated_cost'], .69)
            stored = store.db.execute('SELECT body FROM sessions WHERE key=?', ('parent',)).fetchone()[0]
            self.assertNotIn('estimated_cost', stored)

    def test_missing_later_write_snapshot_keeps_positive_write_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'rollout.jsonl'
            rows = [
                {'type': 'session_meta', 'payload': {'id': 'writes'}},
                {'type': 'turn_context', 'payload': {'turn_id': 'turn-a', 'model': 'gpt-6.1-sol'}},
            ]
            for values in (
                {'input_tokens': 100, 'cached_input_tokens': 0, 'output_tokens': 10,
                 'total_tokens': 110, 'cache_write_input_tokens': 50},
                {'input_tokens': 200, 'cached_input_tokens': 0, 'output_tokens': 20,
                 'total_tokens': 220},
            ):
                rows.append({'type': 'event_msg', 'payload': {
                    'type': 'token_count', 'info': {'total_token_usage': values}}})
            for n, row in enumerate(rows):
                row['timestamp'] = '2026-11-08T00:00:0%dZ' % n
            source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            store = Store(root / 'stats.sqlite3')
            self.addCleanup(store.db.close)
            store.import_paths([source])
            result = store.query()
            parsed = result['turns'][0]
            self.assertIsNone(parsed['cache_write_input_tokens'])
            self.assertTrue(parsed['cache_write_observed'])
            self.assertIsNone(parsed['estimated_cost'])
            self.assertIn('TTL', parsed['estimated_cost_reason'])
            self.assertEqual(result['summary']['cost_samples'], 0)


if __name__ == '__main__':
    unittest.main()
