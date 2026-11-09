"""Parent/child aggregation respects the confidence of normalized usage facts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_stats.core import Store


def turn(identity, inp, cached, output):
    return {'id': identity, 'started_at': '2026-11-08T10:00:00Z',
            'model': 'gpt-6.1-sol', 'input_tokens': inp,
            'cached_input_tokens': cached, 'output_tokens': output,
            'token_total': inp + output, 'tools': []}


class ParentUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'stats.sqlite3')
        self.addCleanup(self.store.db.close)

    def insert(self, data):
        self.store.db.execute('INSERT INTO sessions VALUES(?,?,?)',
                              (data['id'], 0, json.dumps(data)))
        self.store.db.commit()

    def facts(self, identity, **kwargs):
        return {'id': identity, 'native_id': identity, 'source': 'codex',
                'instance': 'test', 'project': '/project', **kwargs}

    def test_parent_and_child_independent_usage_is_summed(self):
        parent = self.facts('parent', usage_accounting='per_session',
                            turns=[turn('p', 1000000, 900000, 100000)])
        child = self.facts('child', usage_accounting='per_session', parent_id='parent',
                           turns=[turn('c', 500000, 400000, 50000)])
        self.insert(parent)
        self.insert(child)
        result = self.store.query()
        summary = result['summary']
        self.assertFalse(summary['aggregation_blocked'])
        self.assertEqual(summary['token_total'], 1650000)
        self.assertEqual(summary['estimated_cost'], 2.03)
        self.assertEqual(summary['cache_usage']['input_known'], 1500000)
        self.assertEqual(summary['cache_usage']['cached_input_known'], 1300000)
        self.assertFalse(summary['cache_usage']['aggregation_blocked'])
        for aggregate in (result['groups'][0], result['trend'][0]):
            self.assertEqual(aggregate['token_total'], 1650000)
            self.assertEqual(aggregate['estimated_cost'], 2.03)
            self.assertEqual(aggregate['cache_usage']['cached_input_known'], 1300000)
            self.assertFalse(aggregate['cache_usage']['aggregation_blocked'])
        self.assertFalse(result['warnings'])

    def test_imported_verified_version_parent_and_child_are_summed(self):
        paths = []
        for identity, total, parent_id in [('parent', 110, None), ('child', 55, 'parent')]:
            meta = {'id': identity, 'cwd': '/project', 'cli_version': '0.166.0-alpha.6'}
            if parent_id:
                meta['source'] = {'subagent': {'thread_spawn': {'parent_thread_id': parent_id}}}
            usage = {'input_tokens': total - 10, 'cached_input_tokens': 0,
                     'output_tokens': 10, 'total_tokens': total}
            rows = [
                {'type': 'session_meta', 'payload': meta},
                {'type': 'turn_context', 'payload': {'turn_id': 't', 'model': 'gpt-6.1-sol'}},
                {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                    'total_token_usage': usage, 'last_token_usage': usage}}},
            ]
            for index, row in enumerate(rows):
                row['timestamp'] = '2026-11-08T00:00:0%dZ' % index
            source = Path(self.temp.name) / (identity + '.jsonl')
            source.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
            paths.append(source)
        report = self.store.import_paths(paths)
        self.assertFalse(report['errors'])
        result = self.store.query()
        self.assertFalse(result['summary']['aggregation_blocked'])
        self.assertEqual(result['summary']['token_total'], 165)
        self.assertEqual(result['summary']['cache_usage']['input_known'], 145)
        self.assertFalse(result['warnings'])

    def test_missing_marker_on_either_side_blocks_only_visible_pair(self):
        parent = self.facts('parent', usage_accounting='per_session',
                            turns=[turn('p', 100, 50, 10)])
        child = self.facts('child', parent_id='parent', project='/child',
                           turns=[turn('c', 200, 100, 20)])
        self.insert(parent)
        self.insert(child)
        for unknown in ('child', 'parent'):
            with self.subTest(unknown=unknown):
                data = {'parent': parent.copy(), 'child': child.copy()}
                data['parent']['usage_accounting'] = 'per_session'
                data['child']['usage_accounting'] = 'per_session'
                data[unknown].pop('usage_accounting')
                for row in data.values():
                    self.store.db.execute('UPDATE sessions SET body=? WHERE key=?',
                                          (json.dumps(row), row['id']))
                self.store.db.commit()
                result = self.store.query()
                self.assertTrue(result['summary']['aggregation_blocked'])
                self.assertIsNone(result['summary']['token_total'])
                self.assertIsNone(result['summary']['estimated_cost'])
                self.assertTrue(result['summary']['cache_usage']['aggregation_blocked'])
                self.assertEqual(result['summary']['observed_token_total'], 330)
                self.assertIn('刷新采集', result['warnings'][0])
                scoped = self.store.query({'project': '/child'})
                self.assertFalse(scoped['summary']['aggregation_blocked'])
                self.assertEqual(scoped['summary']['token_total'], 220)

    def test_same_revision_reimport_upgrades_facts_and_namespaces_fork(self):
        source = Path(self.temp.name) / 'one.jsonl'
        source.write_text('{}\n', encoding='utf-8')
        parsed = {'id': 'child', 'source': 'codex', 'instance': 'device-a',
                  'project': '/project', 'parent_id': 'parent',
                  'forked_from_id': 'origin', 'usage_accounting': 'per_session',
                  'revision_at': '2026-11-08T10:00:00Z', 'event_count': 1,
                  'turns': [turn('c', 100, 50, 10)], 'warnings': []}
        with patch('agent_stats.core.parse_rollout', return_value=(parsed, [])):
            self.assertEqual(self.store.import_paths([source], instance='device-a')['imported'], 1)
            key, body = self.store.db.execute('SELECT key,body FROM sessions').fetchone()
            data = json.loads(body)
            self.assertEqual(json.loads(data['parent_id']), ['codex', 'device-a', 'parent'])
            self.assertEqual(json.loads(data['forked_from_id']), ['codex', 'device-a', 'origin'])
            # A stored row created by the old parser has the same event revision
            # and turns, but lacks both the accounting marker and fork identity.
            data.pop('usage_accounting')
            data.pop('forked_from_id')
            self.store.db.execute('UPDATE sessions SET body=? WHERE key=?', (json.dumps(data), key))
            self.store.db.commit()
            # import_paths mutates the parser return; return a fresh native fact.
            fresh = dict(parsed, id='child', parent_id='parent', forked_from_id='origin',
                         turns=[turn('c', 100, 50, 10)])
            with patch('agent_stats.core.parse_rollout', return_value=(fresh, [])):
                report = self.store.import_paths([source], instance='device-a')
            self.assertEqual(report['imported'], 1)
            upgraded = json.loads(self.store.db.execute('SELECT body FROM sessions').fetchone()[0])
            self.assertEqual(upgraded['usage_accounting'], 'per_session')
            self.assertEqual(json.loads(upgraded['forked_from_id']), ['codex', 'device-a', 'origin'])

    def test_legacy_import_without_fork_metadata_requires_refresh(self):
        # Before the parser upgrade a fork was indistinguishable from a root.
        self.insert(self.facts('old-fork', source_path='/synthetic/old.jsonl',
                               turns=[turn('t', 1000, 900, 100)]))
        result = self.store.query()
        self.assertTrue(result['summary']['aggregation_blocked'])
        self.assertIsNone(result['summary']['token_total'])
        self.assertIsNone(result['summary']['estimated_cost'])
        self.assertIn('刷新采集', result['warnings'][0])
        self.assertFalse(self.store.query({'project': '/other'})['summary']['aggregation_blocked'])


if __name__ == '__main__':
    unittest.main()
