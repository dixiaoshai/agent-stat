import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from unittest.mock import patch

from agent_stats.content import ContentError, ContentReader, default_content_config
from agent_stats.server import make_server


KEY = '["codex","local","session-a"]'


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.database = self.root / 'content.sqlite'
        self.config = self.root / 'content.json'
        self.blobs = self.root / 'blobs'
        self.blobs.mkdir()
        self.config.write_text(json.dumps({'schema_version': 1, 'capture_mode': 'content', 'database': 'content.sqlite',
                                          'content': {'blob_directory': str(self.blobs)},
                                          'collection': {'sources': [{'projects': [{'project_id': 'demo'}]}]}}))
        self.reader = ContentReader(self.config)
        self.db = sqlite3.connect(self.database)
        self.addCleanup(self.db.close)
        self.db.executescript('''PRAGMA user_version=1;
          CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
          INSERT INTO metadata VALUES('capture_mode','content'),('archive_id','archive-a');
          CREATE TABLE sessions(session_key TEXT PRIMARY KEY,project_id TEXT,deleted INTEGER);
          CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT,session_key TEXT,payload TEXT);
        ''')
        self.db.execute('INSERT INTO sessions VALUES(?,?,0)', (KEY, 'demo'))
        self.events = []
        self.event('session.started', 1, {'cwd': '/demo', 'created_at': '2026-11-14T00:00:00Z'})
        self.event('turn.started', 2, {'turn_id': 't1'})
        self.event('message.user', 3, {'title': 'request'}, {'text': 'first request'})
        self.event('tool.called', 4, {'call_id': 'c1', 'name': 'shell'}, {'arguments': 'echo safe'})
        self.event('tool.returned', 5, {'call_id': 'c1'}, {'output': 'safe'})
        self.event('reasoning.summary', 6, {}, {'text': 'summary'})
        self.event('message.assistant', 7, {}, {'text': 'first reply'})
        self.event('turn.completed', 8, {'turn_id': 't1'}, {'text': 'final'})
        # An older format can place the next user prompt before turn_context.
        self.event('message.user', 9, {'title': 'next'}, {'text': 'second request'})
        self.event('turn.context', 10, {'turn_id': 't2'})
        self.event('message.assistant', 11, {}, {'text': 'second reply'})

    def event(self, kind, line, data=None, body=None, path='/source/current.jsonl', stamp=None):
        event_id = hashlib.sha256((path + str(line)).encode()).hexdigest()
        event = {'schema_version': 1, 'event_id': event_id, 'runtime': 'codex', 'instance_id': 'local',
                 'session_id': 'session-a', 'session_key': KEY, 'project_id': 'demo',
                 'kind': kind, 'data': data or {}, 'body': body, 'raw': {'secret': 'raw-must-not-be-returned'},
                 'occurred_at': stamp or '2026-11-14T00:00:%02dZ' % line,
                 'source': {'path': path, 'line': line, 'record_hash': 'f' * 64}}
        self.db.execute('INSERT INTO events(event_id,session_key,payload) VALUES(?,?,?)', (event_id, KEY, json.dumps(event)))
        self.db.commit()
        self.events.append(event)
        return event

    def change(self, event, body):
        event['body'] = body
        self.db.execute('UPDATE events SET payload=? WHERE event_id=?', (json.dumps(event), event['event_id']))
        self.db.commit()

    def blob_event(self, data=b'large redacted output'):
        digest = hashlib.sha256(data).hexdigest()
        event = self.events[4]
        path = self.blobs / (event['event_id'] + '-' + digest + '.txt')
        path.write_bytes(data)
        self.change(event, {'output': {'content_ref': {'path': str(path), 'bytes': len(data), 'sha256': digest, 'media_type': 'text/plain'}}})
        return event, path

    def test_readonly_body_and_scoped_turns(self):
        before = self.database.stat().st_mtime_ns
        page = self.reader.page(KEY, 't1')
        self.assertEqual(page['status'], 'available')
        self.assertEqual([e['kind'] for e in page['events']], ['message.user', 'tool.called', 'tool.returned', 'reasoning.summary', 'message.assistant', 'turn.completed'])
        self.assertNotIn('raw-must-not-be-returned', json.dumps(page))
        self.assertTrue(all(e['turn_id'] == 't1' for e in page['events']))
        next_turn = self.reader.page(KEY, 't2')
        self.assertEqual(next_turn['events'][0]['body']['text'], 'second request')
        self.assertEqual(self.database.stat().st_mtime_ns, before)

    def test_pagination_context_and_archive_replacement(self):
        page = self.reader.page(KEY, 't1', limit=2)
        more = self.reader.page(KEY, 't1', after=page['next_cursor'], limit=2,
                                archive_id=page['archive_id'], revision=page['revision'])
        self.assertTrue(page['has_more'])
        self.assertTrue(all(e['turn_id'] == 't1' for e in more['events']))
        self.assertTrue(set(e['event_id'] for e in page['events']).isdisjoint(e['event_id'] for e in more['events']))
        with self.assertRaises(ContentError) as exc:
            self.reader.page(KEY, after=page['next_cursor'], archive_id='other')
        self.assertEqual(exc.exception.status, 409)
        for after, limit in [(-1, 50), (0, 0), (0, 101), ('no', 50)]:
            with self.assertRaises(ContentError):
                self.reader.page(KEY, after=after, limit=limit)

    def test_missing_configuration_no_creation_and_scope(self):
        self.assertEqual(ContentReader(None).page(KEY)['status'], 'not_configured')
        self.assertEqual(ContentReader(self.root / 'missing.json').page(KEY)['status'], 'unavailable')
        self.assertEqual(self.reader.page('other')['status'], 'empty')
        self.db.execute('UPDATE sessions SET project_id=?', ('outside',))
        self.db.commit()
        self.assertEqual(self.reader.page(KEY)['events'], [])
        self.db.execute('UPDATE sessions SET project_id=?,deleted=1', ('demo',))
        self.db.commit()
        self.assertEqual(self.reader.page(KEY)['events'], [])
        self.database.unlink()
        self.assertEqual(self.reader.page(KEY)['status'], 'unavailable')
        self.assertFalse(self.database.exists())

    def test_prefix_revision_rejects_middle_rewrite_but_allows_append(self):
        page = self.reader.page(KEY, limit=3)
        self.event('message.assistant', 12, {}, {'text': 'appended'})
        more = self.reader.page(KEY, after=page['next_cursor'], revision=page['revision'])
        self.assertTrue(more['events'])
        # A Trace rewrite deletes the old row and assigns a new seq, while the
        # first session.started record and its sequence remain unchanged.
        event = self.events[2]
        self.db.execute('DELETE FROM events WHERE event_id=?', (event['event_id'],))
        self.db.commit()
        self.event('message.user', 3, {'title': 'rewritten'}, {'text': 'changed request'})
        with self.assertRaises(ContentError) as exc:
            self.reader.page(KEY, after=page['next_cursor'], revision=page['revision'])
        self.assertEqual(exc.exception.status, 409)
        fresh = self.reader.page(KEY)
        self.assertEqual([e['source']['line'] for e in fresh['events']], [3, 4, 5, 6, 7, 8, 9, 11, 12])
        self.assertEqual(fresh['events'][0]['body']['text'], 'changed request')

    def test_latest_rollout_and_fork_history(self):
        self.event('session.started', 1, {'created_at': '2026-11-14T01:00:00Z', 'forked_from_session_id': 'parent'},
                   path='/new.jsonl', stamp='2026-11-14T01:00:00Z')
        self.event('message.assistant', 2, {}, {'text': 'copied history'}, path='/new.jsonl', stamp='2026-11-14T00:00:00Z')
        self.event('turn.started', 3, {'turn_id': 'new'}, path='/new.jsonl', stamp='2026-11-14T01:00:01Z')
        self.event('message.assistant', 4, {}, {'text': 'new revision'}, path='/new.jsonl', stamp='2026-11-14T01:00:02Z')
        page = self.reader.page(KEY)
        self.assertEqual([e['body']['text'] for e in page['events']], ['new revision'])
        self.assertEqual(self.reader.page(KEY, 't1')['events'], [])

    def test_verified_blob_and_tampering(self):
        event, path = self.blob_event()
        self.assertEqual(self.reader.blob(KEY, event['event_id'], '/output')['text'], 'large redacted output')
        path.write_text('large modified output')
        with self.assertRaises(ContentError):
            self.reader.blob(KEY, event['event_id'], '/output')
        with self.assertRaises(ContentError):
            self.reader.blob('other', event['event_id'], '/output')
        with self.assertRaises(ContentError):
            self.reader.blob(KEY, event['event_id'], '/missing')

    def test_blob_path_and_symlink_rejected(self):
        event, path = self.blob_event()
        content = path.read_bytes()
        path.unlink()
        outside = self.root / path.name
        outside.write_bytes(content)
        path.symlink_to(outside)
        with self.assertRaises(ContentError) as exc:
            self.reader.blob(KEY, event['event_id'], '/output')
        self.assertEqual(exc.exception.status, 403)
        ref = event['body']['output']['content_ref']
        ref['path'] = str(outside)
        self.change(event, event['body'])
        with self.assertRaises(ContentError) as exc:
            self.reader.blob(KEY, event['event_id'], '/output')
        self.assertEqual(exc.exception.status, 403)

    def test_oversized_blob_and_nested_pointer(self):
        event, path = self.blob_event()
        value = event['body']['output']
        self.change(event, {'a/b~c': [value]})
        self.assertEqual(self.reader.blob(KEY, event['event_id'], '/a~1b~0c/0')['text'], path.read_text())
        value['content_ref']['bytes'] = 17 * 1024 * 1024
        self.change(event, {'output': value})
        with self.assertRaises(ContentError) as exc:
            self.reader.blob(KEY, event['event_id'], '/output')
        self.assertEqual(exc.exception.status, 413)

    def test_defaults_use_explicit_environment(self):
        with patch.dict(os.environ, {'AGENT_STATS_CONTENT_CONFIG': str(self.config)}):
            self.assertEqual(default_content_config(), str(self.config))

    def test_http_session_validation_and_same_origin(self):
        class StubStore:
            def detail(_, identity, filters=None):
                return {'session': {'id': KEY}, 'turns': [{'native_id': 't1'}]} if identity == KEY else None

        server = make_server(StubStore(), [], 0, archive_only=True, content_reader=self.reader)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:%d' % server.server_port

        def request(params, headers=None, endpoint='/api/session-content'):
            try:
                with urlopen(Request(base + endpoint + '?' + urlencode(params), headers=headers or {}), timeout=3) as response:
                    return response.status, response.headers, json.load(response)
            except HTTPError as exc:
                return exc.code, exc.headers, json.load(exc)

        status, headers, body = request({'id': KEY, 'turn_id': 't1'})
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertTrue(body['events'])
        self.assertEqual(request({'id': 'other'})[0], 404)
        self.assertEqual(request({'id': KEY, 'turn_id': 'other'})[0], 404)
        self.assertEqual(request({'id': KEY, 'after': '-1'})[0], 400)
        self.assertEqual(request({'id': KEY}, {'Host': 'evil.invalid'})[0], 403)
        self.assertEqual(request({'id': KEY}, {'Origin': 'https://evil.invalid'})[0], 403)
        self.assertEqual(request({'id': KEY}, {'Origin': 'https://evil.invalid'}, '/api/session-content/blob')[0], 403)


if __name__ == '__main__':
    unittest.main()
