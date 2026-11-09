"""Cache subtotals and token ratios keep the same sample grain and filters."""
import json
import tempfile
import unittest
from pathlib import Path

from agent_stats.core import Store
from agent_stats.usage import aggregate_cache


def turn(inp=100, cached=90, **overrides):
    return {'id': 'turn-a', 'input_tokens': inp, 'cached_input_tokens': cached,
            'token_total': inp + 5 if type(inp) is int else None, 'output_tokens': 5,
            'tools': [], 'model': 'model-a', 'started_at': '2026-11-08T00:00:00Z',
            **overrides}


class CacheUsageTests(unittest.TestCase):
    def test_ratio_is_weighted_by_input_and_does_not_add_cache_to_total(self):
        rows = [turn(), turn(900, 90)]
        cache = aggregate_cache(rows)
        self.assertEqual(cache['cached_input_known'], 180)
        self.assertEqual(cache['uncached_input_known'], 820)
        self.assertEqual(cache['input_known'], 1000)
        self.assertEqual(cache['hit_rate'], .18)
        self.assertTrue(cache['complete'])
        self.assertEqual(sum(t['token_total'] for t in rows), 1010)

    def test_partial_coverage_uses_paired_samples_even_if_output_is_missing(self):
        cache = aggregate_cache([turn(), turn(900, None), turn(None, 50),
                                 turn(100, 20, output_tokens=None, token_total=None)])
        self.assertEqual(cache['cached_input_known'], 110)
        self.assertEqual(cache['input_known'], 200)
        self.assertEqual(cache['hit_rate'], .55)
        self.assertEqual(cache['samples'], 2)
        self.assertEqual(cache['turns'], 4)
        self.assertFalse(cache['complete'])

    def test_zero_input_unknown_and_no_cache_are_distinct(self):
        for rows in ([], [turn(None, None)]):
            cache = aggregate_cache(rows)
            self.assertIsNone(cache['cached_input_known'])
            self.assertIsNone(cache['hit_rate'])
            self.assertFalse(cache['complete'])
        zero = aggregate_cache([turn(0, 0, cache_write_input_tokens=0)])
        self.assertEqual(zero['cached_input_known'], 0)
        self.assertEqual(zero['samples'], 1)
        self.assertTrue(zero['complete'])
        self.assertIsNone(zero['hit_rate'])
        self.assertEqual(zero['cache_write_known'], 0)
        self.assertEqual(zero['cache_write_samples'], 1)
        self.assertEqual(aggregate_cache([turn(100, 0)])['hit_rate'], 0)

    def test_invalid_categories_are_excluded_and_writes_are_separate(self):
        rows = [turn(100, True), turn(100, -1), turn(100, 101),
                turn(True, 0), turn(-1, 0), turn(100, 10, cache_write_input_tokens=25)]
        cache = aggregate_cache(rows)
        self.assertEqual(cache['samples'], 1)
        self.assertEqual(cache['cached_input_known'], 10)
        self.assertEqual(cache['cache_write_known'], 25)
        self.assertEqual(cache['cache_write_samples'], 1)
        self.assertEqual(cache['hit_rate'], .1)

    def test_store_scopes_cache_to_time_model_project_and_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'stats.sqlite3')
            self.addCleanup(store.db.close)
            second = turn(900, 90, id='turn-b', model='model-b', started_at='2026-11-09T00:00:00Z')
            parent = {'id': 'parent', 'native_id': 'parent', 'source': 'codex', 'instance': 'test',
                      'project': '/project-a', 'turns': [turn(), second]}
            child = {**parent, 'id': 'child', 'project': '/project-b',
                     'parent_id': 'parent', 'turns': [turn(100, 50)]}
            for data in (parent, child):
                store.db.execute('INSERT INTO sessions VALUES(?,?,?)', (data['id'], 0, json.dumps(data)))
            store.db.commit()
            result = store.query()
            cache = result['summary']['cache_usage']
            self.assertTrue(cache['aggregation_blocked'])
            self.assertEqual(cache['cached_input_known'], 230)
            self.assertEqual(cache['input_known'], 1100)
            self.assertTrue(result['groups'][0]['cache_usage']['aggregation_blocked'])
            self.assertTrue(result['trend'][0]['cache_usage']['aggregation_blocked'])
            self.assertFalse(result['sessions'][0]['cache_usage']['aggregation_blocked'])
            for filters in ({'model': 'model-b'}, {'start': '2026-11-09T00:00:00Z'}):
                scoped = store.query(filters)
                cache = scoped['summary']['cache_usage']
                self.assertFalse(cache['aggregation_blocked'])
                self.assertEqual(cache['cached_input_known'], 90)
                self.assertEqual(cache['hit_rate'], .1)
                self.assertEqual(scoped['sessions'][0]['cache_usage'], cache)
                self.assertEqual(scoped['groups'][0]['cache_usage'], cache)
                self.assertEqual(scoped['trend'][0]['cache_usage'], cache)
                self.assertEqual(store.detail('parent', filters)['session']['cache_usage'], cache)
            scoped = store.query({'project': '/project-b'})
            self.assertEqual(scoped['summary']['cache_usage']['hit_rate'], .5)
            self.assertEqual(scoped['summary']['token_total'], 105)

    def test_cumulative_cache_snapshots_and_reimports_do_not_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'rollout.jsonl'
            usage = {'input_tokens': 100, 'cached_input_tokens': 80,
                     'output_tokens': 5, 'total_tokens': 105}
            rows = [
                {'type': 'session_meta', 'payload': {'id': 'cache-session'}},
                {'type': 'turn_context', 'payload': {'turn_id': 'first', 'model': 'model-a'}},
                {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': usage}}},
                {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': usage}}},
                {'type': 'turn_context', 'payload': {'turn_id': 'second', 'model': 'model-a'}},
                {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {
                    'input_tokens': 1000, 'cached_input_tokens': 170, 'output_tokens': 10, 'total_tokens': 1010}}}},
            ]
            for index, row in enumerate(rows):
                row['timestamp'] = '2026-11-08T00:00:0%dZ' % index
            source.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            store = Store(root / 'stats.sqlite3')
            self.addCleanup(store.db.close)
            for _ in range(2):
                store.import_paths([source])
                result = store.query()
                self.assertEqual(result['summary']['cache_usage']['cached_input_known'], 170)
                self.assertEqual(result['summary']['cache_usage']['hit_rate'], .17)
                self.assertEqual(result['summary']['cache_usage']['samples'], 2)
                self.assertEqual(result['summary']['token_total'], 1010)


if __name__ == '__main__':
    unittest.main()
