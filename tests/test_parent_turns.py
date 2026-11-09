"""Parent-turn attribution from synthetic, namespaced rollout evidence."""
import json
import tempfile
import unittest
from pathlib import Path

from agent_stats.core import Store


class ParentTurnTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'stats.sqlite3')
        self.addCleanup(self.store.db.close)

    def write(self, identity, day, turns, parent=None):
        meta = {'id': identity, 'cwd': '/synthetic', 'cli_version': '0.166.0-alpha.6'}
        if parent:
            meta['source'] = {'subagent': {'thread_spawn': {'parent_thread_id': parent}}}
        rows = [{'timestamp': f'2026-11-{day:02d}T00:00:00Z', 'type': 'session_meta', 'payload': meta}]
        for i, (turn_id, root_id, model) in enumerate(turns, 1):
            payload = {'type': 'task_started', 'turn_id': turn_id}
            if root_id is not None:
                payload['root_turn_id'] = root_id
            rows.append({'timestamp': f'2026-11-{day:02d}T00:00:{i:02d}Z', 'type': 'event_msg', 'payload': payload})
            rows.append({'timestamp': f'2026-11-{day:02d}T00:00:{i:02d}Z', 'type': 'turn_context',
                         'payload': {'turn_id': turn_id, 'root_turn_id': root_id, 'model': model}})
        path = self.root / f'{identity}.jsonl'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        return path

    def sample(self):
        parent = self.write('parent', 1, [('parent-a', 'parent-a', 'parent-model'),
                                          ('parent-b', 'parent-b', 'parent-model')])
        child = self.write('child', 2, [('child-a', 'parent-a', 'child-model'),
                                        ('child-b', 'parent-b', 'child-model')], 'parent')
        return parent, child

    def test_reused_subagent_turns_bind_to_distinct_parent_turns(self):
        parent, child = self.sample()
        self.store.import_paths([parent, child], instance='one')
        result = self.store.query()
        sessions = {s['native_id']: s for s in result['sessions']}
        turns = {t['native_id']: t for t in result['turns']}
        for suffix in ('a', 'b'):
            sub = turns['child-' + suffix]
            self.assertEqual(sub['parent_session_id'], sessions['parent']['id'])
            self.assertEqual(sub['parent_turn_id'], turns['parent-' + suffix]['id'])
            self.assertEqual(sub['parent_turn_attribution'], 'root_turn_id')
        self.assertIsNone(turns['parent-a']['parent_turn_id'])
        detail = self.store.detail(sessions['child']['id'])
        for t in detail['turns']:
            overview = turns[t['native_id']]
            self.assertEqual(t['parent_turn_id'], overview['parent_turn_id'])
            self.assertEqual(t['parent_session_id'], overview['parent_session_id'])

    def test_parent_turn_is_resolved_before_time_or_model_filters(self):
        parent, child = self.sample()
        self.store.import_paths([parent, child])
        full = self.store.query()
        expected = {t['native_id']: t['id'] for t in full['turns'] if t['session_title'] is None
                    and t['native_id'].startswith('parent-')}
        for filters in ({'model': 'child-model'}, {'start': '2026-11-02T00:00:00Z'}):
            result = self.store.query(filters)
            self.assertEqual(len(result['sessions']), 1)
            self.assertEqual({t['parent_turn_id'] for t in result['turns']}, set(expected.values()))
            detail = self.store.detail(result['sessions'][0]['id'], filters)
            self.assertEqual({t['parent_turn_id'] for t in detail['turns']}, set(expected.values()))

    def test_same_native_ids_do_not_cross_instance_boundaries(self):
        parent, child = self.sample()
        self.store.import_paths([parent, child], instance='one')
        self.store.import_paths([child], instance='two')
        result = self.store.query()
        second = next(s for s in result['sessions'] if s['instance'] == 'two')
        for t in result['turns']:
            if t['session_id'] == second['id']:
                self.assertIsNone(t['parent_turn_id'])
                self.assertEqual(t['parent_turn_attribution'], 'unknown')
                self.assertEqual(json.loads(t['parent_session_id']), ['codex', 'two', 'parent'])

    def test_missing_wrong_and_non_direct_root_are_not_guessed(self):
        parent, child = self.sample()
        other = self.write('grandchild', 3, [('grandchild-a', 'parent-a', 'grandchild-model'),
                                             ('grandchild-b', None, 'grandchild-model'),
                                             ('grandchild-c', 'missing', 'grandchild-model')], 'child')
        self.store.import_paths([parent, child, other])
        result = self.store.query()
        grandchild = next(s for s in result['sessions'] if s['native_id'] == 'grandchild')
        scoped = [t for t in result['turns'] if t['session_id'] == grandchild['id']]
        self.assertEqual(len(scoped), 3)
        self.assertTrue(all(t['parent_turn_id'] is None for t in scoped))
        self.assertTrue(all(t['parent_turn_attribution'] == 'unknown' for t in scoped))

    def test_same_revision_reimport_populates_new_root_facts(self):
        parent, child = self.sample()
        self.store.import_paths([parent, child])
        session = next(s for s in self.store.query()['sessions'] if s['native_id'] == 'child')
        record = json.loads(self.store.db.execute('SELECT body FROM sessions WHERE key=?', (session['id'],)).fetchone()[0])
        for t in record['turns']:
            t.pop('root_turn_id')
        self.store.db.execute('UPDATE sessions SET body=? WHERE key=?', (json.dumps(record), session['id']))
        self.store.db.commit()
        self.assertEqual(self.store.import_paths([child])['imported'], 1)
        self.assertTrue(all(t['parent_turn_id'] for t in self.store.detail(session['id'])['turns']))

    def test_older_copy_does_not_replace_newer_parent_links(self):
        parent, child = self.sample()
        self.store.import_paths([parent, child])
        original = {t['native_id']: t['parent_turn_id'] for t in self.store.query()['turns']
                    if t['native_id'].startswith('child-')}
        stale = self.root / 'older-copy.jsonl'
        stale.write_text('\n'.join(child.read_text(encoding='utf-8').splitlines()[:3]) + '\n', encoding='utf-8')
        self.assertEqual(self.store.import_paths([stale])['skipped'], 1)
        restored = {t['native_id']: t['parent_turn_id'] for t in self.store.query()['turns']
                    if t['native_id'].startswith('child-')}
        self.assertEqual(restored, original)

    def test_conflicting_roots_for_one_turn_are_unknown(self):
        parent, child = self.sample()
        with child.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'timestamp': '2026-11-02T00:00:03Z', 'type': 'turn_context',
                                     'payload': {'turn_id': 'child-a', 'root_turn_id': 'parent-b', 'model': 'child-model'}}) + '\n')
        self.store.import_paths([parent, child])
        turns = {t['native_id']: t for t in self.store.query()['turns']}
        self.assertIsNone(turns['child-a']['root_turn_id'])
        self.assertIsNone(turns['child-a']['parent_turn_id'])
        self.assertIsNotNone(turns['child-b']['parent_turn_id'])


if __name__ == '__main__':
    unittest.main()
