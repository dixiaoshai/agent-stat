"""Source-to-query behavioral checks using only synthetic events."""
import json
from pathlib import Path
import tempfile
import unittest

from agent_stats.core import Store


def event(kind, payload, second=0):
    return {"timestamp": "2026-11-01T10:00:%02dZ" % second, "type": kind, "payload": payload}


def fixture(identity="synthetic-session", total=120):
    return [
        event("session_meta", {"id": identity, "cwd": "/synthetic/project"}),
        event("event_msg", {"type": "task_started", "turn_id": "synthetic-turn"}, 1),
        event("turn_context", {"turn_id": "synthetic-turn", "model": "synthetic-model"}, 1),
        event("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": total - 20, "output_tokens": 20, "total_tokens": total}}}, 2),
        event("event_msg", {"type": "task_complete", "turn_id": "synthetic-turn"}, 3),
    ]


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "stats.sqlite3")

    def write(self, events, name="source.jsonl"):
        path = self.root / name
        path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        return str(path)

    def test_cumulative_duplicate_late_revision_and_restart(self):
        events = fixture()
        events.insert(4, events[3])  # same snapshot replay
        path = self.write(events)
        self.store.import_paths([path])
        self.store.import_paths([path])
        self.assertEqual(self.store.query()["summary"]["token_total"], 120)
        events.append(event("event_msg", {"type": "token_count", "turn_id": "synthetic-turn", "info": {"total_token_usage": {"input_tokens": 130, "output_tokens": 20, "total_tokens": 150}}}, 4))
        self.write(events)
        self.store.import_paths([path])
        self.assertEqual(self.store.query()["summary"]["token_total"], 150)
        self.assertEqual(self.store.query()["summary"]["turns"], 1)
        self.assertEqual(Store(self.root / "stats.sqlite3").query()["summary"]["token_total"], 150)

    def test_unknown_is_not_zero_and_half_open_windows(self):
        zero = fixture("zero", 20)
        zero[3]["payload"]["info"]["total_token_usage"] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        unknown = fixture("unknown")
        unknown.pop(3)
        self.store.import_paths([self.write(zero, "zero.jsonl"), self.write(unknown, "unknown.jsonl")])
        turns = self.store.query()["turns"]
        self.assertEqual(sum(t["token_total"] is None for t in turns), 1)
        self.assertEqual(sum(t["token_total"] == 0 for t in turns), 1)
        before = self.store.query({"start": "2026-11-01T09:00:00Z", "end": "2026-11-01T10:00:01Z"})
        after = self.store.query({"start": "2026-11-01T10:00:01Z", "end": "2026-11-01T11:00:00Z"})
        self.assertEqual(before["summary"]["turns"], 0)
        self.assertEqual(after["summary"]["turns"], 2)
        self.assertIsNone(after["summary"]["cost"])

    def test_instances_do_not_collide(self):
        path = self.write(fixture())
        self.store.import_paths([path], instance="device-a")
        self.store.import_paths([path], instance="device-b")
        result = self.store.query()
        self.assertEqual(result["summary"]["sessions"], 2)
        self.assertEqual(len({s["id"] for s in result["sessions"]}), 2)

    def test_tools_are_not_repeated_across_turns_and_text_is_private(self):
        events = fixture()
        events.insert(3, event('response_item', {'type':'function_call', 'name':'shell', 'call_id':'call-1', 'arguments':'SECRET_SENTINEL'}, 1))
        events.insert(4, event('response_item', {'type':'function_call_output', 'call_id':'call-1', 'output':'SECRET_SENTINEL'}, 2))
        events.extend([event('turn_context', {'turn_id':'second', 'model':'other'}, 5)])
        self.store.import_paths([self.write(events)])
        result = self.store.query()
        self.assertEqual(result['summary']['tool_calls'], 1)
        self.assertEqual([len(t['tools']) for t in result['turns']], [1, 0])
        self.assertNotIn('SECRET_SENTINEL', (self.root/'stats.sqlite3').read_bytes().decode('utf-8', errors='ignore'))
        scoped = self.store.detail(result['sessions'][0]['id'], {'model':'other'})
        self.assertEqual(len(scoped['turns']), 1)
        self.assertEqual(scoped['turns'][0]['model'], 'other')

    def test_parent_child_unknown_and_older_copy(self):
        parent, child = fixture('parent', 140), fixture('child', 40)
        child[0]['payload']['parent_id'] = 'parent'
        self.store.import_paths([self.write(parent, 'parent.jsonl'), self.write(child, 'child.jsonl')])
        result = self.store.query()
        self.assertIsNone(result['summary']['token_total'])
        self.assertIsNone(result['summary']['token_known'])
        self.assertTrue(result['summary']['aggregation_blocked'])
        self.assertEqual(result['summary']['observed_token_total'], 180)
        self.assertEqual(result['summary']['observed_token_known'], 180)
        self.assertEqual(result['groups'][0]['observed_token_known'], 180)
        self.assertEqual(result['trend'][0]['observed_token_known'], 180)
        self.assertIsNone(result['groups'][0]['token_known'])
        self.assertIsNone(result['trend'][0]['token_known'])
        self.assertEqual(sorted(s['token_total'] for s in result['sessions']), [40,140])
        old = fixture('parent', 100)
        old[-1]['timestamp'] = '2026-11-01T10:00:02Z'
        self.store.import_paths([self.write(old, 'old-copy.jsonl')])
        self.assertIn(140, [s['token_total'] for s in self.store.query()['sessions']])

    def test_observed_subtotal_respects_model_and_time_filters(self):
        parent, child = fixture('parent', 140), fixture('child', 40)
        child[0]['payload']['parent_id'] = 'parent'
        child[2]['payload']['model'] = 'child-model'
        for row in child:
            row['timestamp'] = row['timestamp'].replace('2026-11-01', '2026-11-02')
        self.store.import_paths([self.write(parent, 'parent.jsonl'), self.write(child, 'child.jsonl')])
        for filters in ({'model': 'child-model'}, {'start': '2026-11-02T00:00:00Z'}):
            scoped = self.store.query(filters)
            self.assertEqual(scoped['summary']['sessions'], 1)
            self.assertEqual(scoped['summary']['observed_token_known'], 40)
            self.assertEqual(scoped['summary']['token_known'], 40)
            self.assertFalse(scoped['summary']['aggregation_blocked'])

    def test_observed_subtotal_preserves_missing_and_zero(self):
        missing = fixture('missing')
        missing.pop(3)
        path = self.write(missing)
        self.store.import_paths([path])
        result = self.store.query()
        self.assertIsNone(result['summary']['observed_token_known'])
        self.assertIsNone(result['trend'][0]['observed_token_known'])
        zero = fixture('zero')
        zero[3]['payload']['info']['total_token_usage'] = {'total_tokens': 0}
        self.store.import_paths([self.write(zero, 'zero.jsonl')])
        result = self.store.query()
        self.assertIsNone(result['summary']['observed_token_total'])
        self.assertEqual(result['summary']['observed_token_known'], 0)
        self.assertEqual(result['summary']['token_samples'], 1)

    def test_warning_has_one_source_qualified_occurrence(self):
        events = fixture()
        events.append(event('event_msg', {'type': 'token_count', 'info': {}}, 4))
        path = self.write(events)
        self.store.import_paths([path])
        warnings = self.store.query()['warnings']
        self.assertEqual(warnings, [path + ': 用量事件缺少有效累计快照: 行 6'])

    def test_rate_limit_only_event_does_not_claim_missing_usage(self):
        events = fixture()
        events.insert(1, event('event_msg', {'type': 'token_count', 'info': None,
                                           'rate_limits': {'primary': {'used_percent': 5}}}))
        report = self.store.import_paths([self.write(events)])
        self.assertEqual(report['warnings'], [])
        result = self.store.query()
        self.assertEqual(result['summary']['turns'], 1)
        self.assertEqual(result['summary']['token_total'], 120)

    def test_malformed_usage_is_not_hidden_by_rate_limits(self):
        for index, info in enumerate((None, {}, {'total_token_usage': {'total_tokens': -1}})):
            events = fixture('invalid-%d' % index)
            payload = {'type': 'token_count', 'info': info}
            if info is not None:
                payload['rate_limits'] = {'primary': {'used_percent': 5}}
            events.append(event('event_msg', payload, 4))
            report = self.store.import_paths([self.write(events, '%d.jsonl' % index)])
            self.assertEqual(len(report['warnings']), 1)

    def test_reimport_replaces_stale_parser_warning_without_usage_changes(self):
        path = self.write(fixture())
        self.store.import_paths([path])
        key, body = self.store.db.execute('SELECT key,body FROM sessions').fetchone()
        data = json.loads(body)
        data['warnings'] = ['obsolete parser warning']
        self.store.db.execute('UPDATE sessions SET body=? WHERE key=?', (json.dumps(data), key))
        self.store.db.commit()
        report = self.store.import_paths([path])
        self.assertEqual(report['imported'], 1)
        self.assertEqual(self.store.query()['warnings'], [])

    def test_corrupt_record_and_offset_time(self):
        path = self.write(fixture())
        with open(path, 'a') as stream: stream.write('{broken\n')
        report = self.store.import_paths([path])
        self.assertTrue(report['warnings'])
        self.assertTrue(self.store.query()['warnings'])
        self.assertEqual(self.store.query({'start':'2026-11-01T18:00:01+08:00'})['summary']['turns'], 1)
        self.assertEqual(self.store.query({'end':'2026-11-01T18:00:01+08:00'})['summary']['turns'], 0)
        self.store.db.execute("INSERT INTO sessions VALUES('broken',0,'{broken')")
        self.store.db.commit()
        self.assertEqual(self.store.query()['summary']['turns'],1)
        self.assertTrue(self.store.query()['warnings'])

    def test_late_missing_component_recovers_only_same_turn(self):
        events = fixture()
        events[3]['payload']['info']['total_token_usage'] = {'input_tokens':100,'total_tokens':120}
        events.append(event('event_msg', {'type':'token_count','turn_id':'synthetic-turn','info':{'total_token_usage':{'input_tokens':100,'output_tokens':20,'total_tokens':120}}},4))
        self.store.import_paths([self.write(events)])
        result = self.store.query()
        self.assertEqual(result['turns'][0]['output_tokens'],20)
        self.assertEqual(result['turns'][0]['token_total'],120)
        # A component first supplied in a different turn cannot all be attributed there.
        events[-1]['payload']['turn_id'] = 'second'
        self.store.import_paths([self.write(events)])
        result = self.store.query()
        self.assertTrue(all(t['output_tokens'] is None for t in result['turns']))

    def test_source_total_fallback_and_single_request_stream_not_double_counted(self):
        events = fixture()
        events[3]['payload']['info']['total_token_usage'] = {'input_tokens':100,'output_tokens':20}
        events.append(event('event_msg', {'type':'token_count','turn_id':'synthetic-turn','info':{'total_token_usage':{'input_tokens':100,'output_tokens':20,'total_tokens':120}}},4))
        events.append(event('token_usage_record', {'response_id':'synthetic-response','usage':{'total_tokens':120}},5))
        self.store.import_paths([self.write(events)])
        self.assertEqual(self.store.query()['summary']['token_total'],120)


if __name__ == "__main__":
    unittest.main()
