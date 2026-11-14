"""Behavioral coverage for the agent-trace follow worker.

Every test uses a synthetic loopback Trace stub, a temporary database and no
Codex source logs. The worker must never touch source logs; the dashboard may
read the same database concurrently.
"""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from agent_stats.core import Store
from agent_stats.follow import (FollowError, FollowWorker, acquire_lease, follow_id,
                                load_config, read_token, validate_page, validate_status)
from agent_stats.server import make_server


def record(seq, kind, data=None, sid='synthetic', project='demo'):
    key = json.dumps(['codex', 'local', sid], separators=(',', ':'))
    native = json.dumps(['codex', 'local', sid, 'deleted' if kind == 'session.deleted' else seq],
                        ensure_ascii=False, separators=(',', ':'))
    return {'schema_version': 1, 'event_id': hashlib.sha256(native.encode('utf-8')).hexdigest(),
            'archive_seq': seq, 'runtime': 'codex', 'instance_id': 'local', 'session_id': sid,
            'session_key': key, 'project_id': project, 'kind': kind,
            'occurred_at': '2026-01-01T10:00:%02dZ' % seq,
            'observed_at': '2026-01-01T10:01:00Z', 'native_type': kind,
            'source': None if kind == 'session.deleted' else
            {'path': '/demo/rollout.jsonl', 'line': seq, 'record_hash': format(seq + 30, '064x')},
            'data': data or {}, 'body': None, 'raw': None}


def session_events(start_seq, sid, total_tokens=120):
    return [
        record(start_seq, 'session.started', {'cwd': '/demo', 'created_at': '2026-01-01T10:00:00Z'}, sid),
        record(start_seq + 1, 'turn.started', {'turn_id': 'turn'}, sid),
        record(start_seq + 2, 'turn.context', {'turn_id': 'turn', 'model': 'demo'}, sid),
        record(start_seq + 3, 'usage.snapshot', {
            'turn_id': 'turn',
            'cumulative': {'input_tokens': total_tokens - 20, 'output_tokens': 20, 'total_tokens': total_tokens},
            'last_usage': {'input_tokens': total_tokens - 20, 'output_tokens': 20, 'total_tokens': total_tokens},
        }, sid),
        record(start_seq + 4, 'tool.called', {'turn_id': 'turn', 'call_id': 'call', 'name': 'shell',
                                             'input_bytes': 50}, sid),
        record(start_seq + 5, 'tool.returned', {'turn_id': 'turn', 'call_id': 'call',
                                               'output_bytes': 12, 'status': 'result_observed'}, sid),
        record(start_seq + 6, 'turn.completed', {'turn_id': 'turn'}, sid),
    ]


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class FakeTrace:
    """Minimal v1 event API: deterministic paging, injectable failures."""

    def __init__(self, events=(), archive_id='archive-1', token='test-token', watermark=None,
                 delay=0.0, fail_events=0, corrupt=None, max_page_events=1000):
        self.events = sorted(events, key=lambda event: event['archive_seq'])
        self.archive_id = archive_id
        self.token = token
        self.watermark = watermark if watermark is not None else (
            self.events[-1]['archive_seq'] if self.events else 0)
        self.delay = delay
        self.fail_events = fail_events
        self.corrupt = corrupt
        self.max_page_events = max_page_events
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.headers.get('Authorization') != 'Bearer ' + outer.token:
                    return outer.send(self, 401, {'error': {'code': 'UNAUTHORIZED',
                                                            'message': 'Missing bearer token'}})
                parts = urlsplit(self.path)
                if parts.path == '/v1/status':
                    return outer.send(self, 200, outer.status_body())
                if parts.path != '/v1/events':
                    return outer.send(self, 404, {'error': {'code': 'NOT_FOUND', 'message': 'Unknown path'}})
                query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
                if outer.delay:
                    time.sleep(outer.delay)
                if outer.fail_events:
                    outer.fail_events -= 1
                    return outer.send(self, 500, {'error': {'code': 'INTERNAL',
                                                            'message': 'synthetic failure'}})
                after = int(query.get('after', '0'))
                limit = int(query.get('limit', '500'))
                view = query.get('view', 'metadata')
                outer.requests.append((after, limit, view))
                if view != 'metadata':
                    return outer.send(self, 403, {'error': {'code': 'FORBIDDEN_VIEW',
                                                            'message': 'View is not authorized'}})
                if after > outer.watermark:
                    return outer.send(self, 400, {'error': {'code': 'INVALID_CURSOR',
                                                            'message': 'Cursor is beyond archive history'}})
                remaining = [event for event in outer.events
                             if after < event['archive_seq'] <= outer.watermark]
                selected = remaining[:limit]
                has_more = len(remaining) > limit
                next_cursor = selected[-1]['archive_seq'] if has_more else outer.watermark
                batch = {'schema_version': 1, 'archive_id': outer.archive_id, 'view': 'metadata',
                         'after_cursor': after, 'next_cursor': next_cursor, 'has_more': has_more,
                         'events': json.loads(json.dumps(selected))}
                if outer.corrupt == 'after':
                    batch['after_cursor'] = after + 1
                elif outer.corrupt == 'archive':
                    batch['archive_id'] = 'archive-other'
                elif outer.corrupt == 'body' and batch['events']:
                    batch['events'][0]['body'] = {'text': 'SECRET_SENTINEL'}
                elif outer.corrupt == 'gap' and batch['events']:
                    batch['events'] = batch['events'][1:]
                return outer.send(self, 200, batch)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return 'http://127.0.0.1:%d' % self.server.server_port

    def status_body(self):
        return {
            'schema_version': 1,
            'protocol': {'schema_version': 1, 'default_page_events': 500,
                         'max_page_events': self.max_page_events,
                         'max_page_bytes': 33554432, 'views': ['metadata', 'content']},
            'archive': {'archive_id': self.archive_id, 'capture_mode': 'metadata',
                        'high_watermark': self.watermark},
            'collection': {'enabled': True, 'state': 'idle',
                           'started_at': '2026-01-01T00:00:00.000Z',
                           'heartbeat_at': '2026-01-01T00:01:00.000Z',
                           'scan_interval_seconds': 30, 'last_scan_started_at': None,
                           'last_scan_finished_at': None, 'last_scan': None,
                           'last_error': None, 'sources': []},
        }

    def send(self, handler, status, payload):
        body = json.dumps(payload).encode()
        handler.send_response(status)
        handler.send_header('Content-Type', 'application/json; charset=utf-8')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class FollowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.traces = []
        self.workers = []

    def tearDown(self):
        for worker in self.workers:
            worker.request_stop()
        for trace in self.traces:
            trace.stop()

    def trace(self, *args, **kwargs):
        trace = FakeTrace(*args, **kwargs)
        self.traces.append(trace)
        return trace

    def config(self, trace, **overrides):
        payload = {'schema_version': 1, 'trace_url': trace.url, 'token_env': 'TEST_TRACE_TOKEN',
                   'database': 'stats.sqlite3', 'page_size': 2, 'idle_seconds': 1,
                   'request_timeout_seconds': 2, 'max_backoff_seconds': 2}
        payload.update(overrides)
        payload = {key: value for key, value in payload.items() if value is not None}
        path = self.root / 'follow.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        return load_config(path)

    def worker(self, config, store=None, emit=None):
        store = store or Store(config.database)
        lines = emit if emit is not None else []
        worker = FollowWorker(config, store, 'test-token', emit=lines.append)
        self.workers.append(worker)
        return worker, store, lines

    def start(self, worker):
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        return thread

    def stop_worker(self, worker, thread):
        worker.request_stop()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), 'follow worker did not stop')

    def cursor(self, config, archive_id='archive-1'):
        """Read the committed cursor from a separate connection (worker-owned
        connections must stay single-threaded)."""
        try:
            connection = sqlite3.connect(str(config.database), timeout=10)
            try:
                row = connection.execute('SELECT cursor FROM archive_cursors WHERE archive_id=?',
                                         (archive_id,)).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            return None
        return row[0] if row else None

    def state(self, config):
        store = Store(config.database)
        self.addCleanup(store.db.close)
        return store.db.execute('SELECT * FROM follow_state').fetchone()


class ConfigTests(FollowTests):
    def test_defaults_and_relative_path(self):
        trace = self.trace()
        config = self.config(trace, page_size=None, idle_seconds=None,
                             request_timeout_seconds=None, max_backoff_seconds=None)
        self.assertEqual(config.page_size, 500)
        self.assertEqual(config.idle_seconds, 15)
        self.assertEqual(config.request_timeout_seconds, 20)
        self.assertEqual(config.max_backoff_seconds, 300)
        self.assertEqual(config.database, self.root / 'stats.sqlite3')
        self.assertEqual(config.trace_url, trace.url)

    def test_override_and_rejections(self):
        trace = self.trace()
        config = self.config(trace)
        overridden = load_config(self.root / 'follow.json', db_override='explicit.sqlite3')
        self.assertEqual(overridden.database, Path('explicit.sqlite3'))
        with self.assertRaises(FollowError) as caught:
            validate_status({'schema_version': 1}, 500)
        self.assertEqual(caught.exception.code, 'PROTOCOL')

    def test_example_config_is_usable(self):
        example = Path(__file__).resolve().parent.parent / 'agent-stats.follow.example.json'
        previous = os.environ.get('AGENT_TRACE_STATS_TOKEN')
        os.environ['AGENT_TRACE_STATS_TOKEN'] = 'example-token'
        try:
            config = load_config(example)
            self.assertEqual(config.trace_url, 'http://127.0.0.1:8766')
            self.assertEqual(config.page_size, 500)
            self.assertEqual(read_token(config), 'example-token')
            self.assertEqual(config.database, example.parent / '.agent-stats/stats.sqlite3')
        finally:
            if previous is None:
                os.environ.pop('AGENT_TRACE_STATS_TOKEN', None)
            else:
                os.environ['AGENT_TRACE_STATS_TOKEN'] = previous

    def test_strict_config_validation(self):
        trace = self.trace()
        valid = {'schema_version': 1, 'trace_url': trace.url, 'token_env': 'TEST_TRACE_TOKEN',
                 'database': 'stats.sqlite3'}
        cases = {
            'unknown key': {**valid, 'extra': 1},
            'bad schema': {**valid, 'schema_version': 2},
            'https url': {**valid, 'trace_url': trace.url.replace('http://', 'https://')},
            'lan host': {**valid, 'trace_url': 'http://192.168.1.5:8766'},
            'url path': {**valid, 'trace_url': trace.url + '/v1'},
            'no port': {**valid, 'trace_url': 'http://127.0.0.1'},
            'credentials': {**valid, 'trace_url': trace.url.replace('http://', 'http://a:b@')},
            'lower token env': {**valid, 'token_env': 'lowercase'},
            'tilde db': {**valid, 'database': '~/stats.sqlite3'},
            'missing db': {key: value for key, value in valid.items() if key != 'database'},
            'page size zero': {**valid, 'page_size': 0},
            'page size too big': {**valid, 'page_size': 1001},
            'bool page size': {**valid, 'page_size': True},
            'idle zero': {**valid, 'idle_seconds': 0},
            'backoff below idle': {**valid, 'idle_seconds': 30, 'max_backoff_seconds': 10},
            'timeout too big': {**valid, 'request_timeout_seconds': 121},
        }
        for label, payload in cases.items():
            path = self.root / 'bad-follow.json'
            path.write_text(json.dumps(payload), encoding='utf-8')
            with self.subTest(label):
                with self.assertRaises(FollowError) as caught:
                    load_config(path)
                self.assertEqual(caught.exception.code, 'INVALID_CONFIG')
                self.assertEqual(caught.exception.klass, 'permanent')

    def test_token_comes_from_environment_only(self):
        trace = self.trace()
        config = self.config(trace)
        previous = os.environ.pop('TEST_TRACE_TOKEN', None)
        try:
            with self.assertRaises(FollowError) as caught:
                read_token(config)
            self.assertEqual(caught.exception.code, 'TOKEN_MISSING')
            os.environ['TEST_TRACE_TOKEN'] = 'test-token'
            self.assertEqual(read_token(config), 'test-token')
        finally:
            os.environ.pop('TEST_TRACE_TOKEN', None)
            if previous is not None:
                os.environ['TEST_TRACE_TOKEN'] = previous


class WorkerTests(FollowTests):
    def setUp(self):
        super().setUp()
        os.environ['TEST_TRACE_TOKEN'] = 'test-token'
        self.addCleanup(os.environ.pop, 'TEST_TRACE_TOKEN', None)

    def test_pages_commit_cursor_and_restart_resumes(self):
        trace = self.trace(session_events(1, 'synthetic'))
        config = self.config(trace)
        worker, store, lines = self.worker(config)
        thread = self.start(worker)
        self.assertTrue(wait_until(lambda: self.cursor(config) == 7), lines)
        self.stop_worker(worker, thread)
        summary = store.query()['summary']
        self.assertEqual((summary['sessions'], summary['token_total'], summary['tool_calls']), (1, 120, 1))

        pages = [line for line in lines if line['type'] == 'follow.page']
        self.assertEqual([line['after'] for line in pages], [0, 2, 4, 6])
        self.assertEqual([line['next_cursor'] for line in pages], [2, 4, 6, 7])
        self.assertFalse(any(line['has_more'] for line in pages[-1:]))
        self.assertTrue(any(line['type'] == 'follow.started' for line in lines))
        self.assertEqual(lines[-1]['type'], 'follow.stopped')
        self.assertEqual(lines[-1]['reason'], 'signal')
        self.assertEqual(trace.requests[0][2], 'metadata')
        # Body/raw never travel through the worker or land in the database.
        self.assertNotIn(b'SECRET_SENTINEL', Path(config.database).read_bytes())

        # Restart catches up from the committed cursor, not from zero.
        trace.events.extend(session_events(8, 'second'))
        trace.watermark = 14
        restarted, store2, lines2 = self.worker(config, Store(config.database))
        seen = len(trace.requests)
        thread2 = self.start(restarted)
        self.assertTrue(wait_until(lambda: self.cursor(config) == 14), lines2)
        self.stop_worker(restarted, thread2)
        self.assertEqual(trace.requests[seen][0], 7)
        self.assertEqual(store2.query()['summary']['sessions'], 2)

    def test_second_instance_is_locked_out(self):
        trace = self.trace(session_events(1, 'synthetic'))
        config = self.config(trace)
        worker, store, lines = self.worker(config)
        thread = self.start(worker)
        self.assertTrue(wait_until(lambda: self.cursor(config) == 7), lines)
        second, store2, lines2 = self.worker(config, Store(config.database))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = second.run()
        self.assertEqual(code, 1)
        self.assertIn('FOLLOW_LOCKED', errors.getvalue())
        self.assertFalse(any(line['type'] == 'follow.started' for line in lines2))
        self.stop_worker(worker, thread)

    def test_stale_lease_from_dead_process_is_taken_over(self):
        import socket
        trace = self.trace(session_events(1, 'synthetic'))
        config = self.config(trace)
        store = Store(config.database)
        self.addCleanup(store.db.close)
        identifier = follow_id(config.database)
        store.db.execute(
            'CREATE TABLE follow_state (follow_id TEXT PRIMARY KEY, trace_url TEXT NOT NULL, '
            'archive_id TEXT, view TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, '
            'last_attempt_at TEXT, last_success_at TEXT, last_error_code TEXT, last_error_class TEXT, '
            'last_error_at TEXT, lease_owner TEXT, lease_expires_at REAL)')
        dead_owner = '%s:999999:abc' % socket.gethostname()
        store.db.execute('INSERT INTO follow_state VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (identifier, trace.url, None, 'metadata', 'now', 'now', None, None,
                          None, None, None, dead_owner, time.time() + 300))
        store.db.commit()
        acquire_lease(store.db, identifier, 'me:1:x')
        row = store.db.execute('SELECT lease_owner FROM follow_state').fetchone()
        self.assertEqual(row[0], 'me:1:x')

    def test_invalid_page_is_rejected_without_moving_cursor(self):
        trace = self.trace(session_events(1, 'synthetic'), corrupt='body')
        config = self.config(trace)
        worker, store, lines = self.worker(config)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = worker.run()
        self.assertEqual(code, 2)
        self.assertIn('INVALID_PAGE', errors.getvalue())
        self.assertIsNone(self.cursor(config))
        self.assertEqual(store.query()['summary']['sessions'], 0)
        self.assertNotIn(b'SECRET_SENTINEL', Path(config.database).read_bytes())

    def test_cursor_gap_is_rejected(self):
        trace = self.trace(session_events(1, 'synthetic'), corrupt='after')
        config = self.config(trace)
        worker, store, lines = self.worker(config)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = worker.run()
        self.assertEqual(code, 2)
        self.assertIn('INVALID_PAGE', errors.getvalue())
        self.assertIsNone(self.cursor(config))

    def test_archive_identity_change_pauses(self):
        trace = self.trace(session_events(1, 'synthetic'))
        config = self.config(trace)
        worker, store, lines = self.worker(config)
        thread = self.start(worker)
        self.assertTrue(wait_until(lambda: self.cursor(config) == 7), lines)
        self.stop_worker(worker, thread)

        trace.archive_id = 'archive-2'
        trace.events = session_events(1, 'synthetic')
        restarted, store2, lines2 = self.worker(config, Store(config.database))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = restarted.run()
        self.assertEqual(code, 2)
        self.assertIn('ARCHIVE_CHANGED', errors.getvalue())
        self.assertEqual(self.cursor(config), 7)
        self.assertEqual(store2.query()['summary']['sessions'], 1)

    def test_transient_failure_backs_off_then_catches_up(self):
        trace = self.trace(session_events(1, 'synthetic'), fail_events=1)
        config = self.config(trace)
        worker, store, lines = self.worker(config)
        thread = self.start(worker)
        self.assertTrue(wait_until(lambda: self.cursor(config) == 7), lines)
        self.stop_worker(worker, thread)
        errors = [line for line in lines if line['type'] == 'follow.error']
        self.assertEqual(len(errors), 1)
        self.assertEqual((errors[0]['class'], errors[0]['code']), ('transient', 'INTERNAL'))
        self.assertGreaterEqual(errors[0]['retry_in_seconds'], 1)
        self.assertEqual(store.query()['summary']['token_total'], 120)

    def test_unauthorized_token_is_permanent(self):
        trace = self.trace(session_events(1, 'synthetic'))
        config = self.config(trace)
        store = Store(config.database)
        self.addCleanup(store.db.close)
        worker = FollowWorker(config, store, 'wrong-token', emit=(lines := []).append)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = worker.run()
        self.assertEqual(code, 2)
        self.assertIn('UNAUTHORIZED', errors.getvalue())
        self.assertNotIn('wrong-token', errors.getvalue())

    def test_empty_tail_page_advances_cursor(self):
        trace = self.trace(session_events(1, 'synthetic'), watermark=10)
        config = self.config(trace)
        worker, store, lines = self.worker(config)
        thread = self.start(worker)
        self.assertTrue(wait_until(lambda: self.cursor(config) == 10), lines)
        self.stop_worker(worker, thread)
        self.assertEqual(store.query()['summary']['sessions'], 1)

    def test_tombstone_clears_projection_and_stays_deleted(self):
        trace = self.trace(session_events(1, 'synthetic') + [record(8, 'session.deleted')])
        config = self.config(trace)
        worker, store, lines = self.worker(config)
        thread = self.start(worker)
        self.assertTrue(wait_until(lambda: self.cursor(config) == 8), lines)
        self.stop_worker(worker, thread)
        self.assertEqual(store.query()['summary']['sessions'], 0)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)

        # A direct source refresh is skipped while the archive owns the session.
        source = self.root / 'rollout.jsonl'
        native = [
            {'timestamp': '2026-01-01T10:00:01Z', 'type': 'session_meta',
             'payload': {'id': 'synthetic', 'cwd': '/demo'}},
            {'timestamp': '2026-01-01T10:00:02Z', 'type': 'event_msg',
             'payload': {'type': 'task_started', 'turn_id': 'turn'}},
        ]
        source.write_text(''.join(json.dumps(row) + '\n' for row in native), encoding='utf-8')
        store2 = Store(config.database)
        self.addCleanup(store2.db.close)
        report = store2.import_paths([source])
        self.assertEqual(report['skipped'], 1)
        self.assertEqual(store2.query()['summary']['sessions'], 0)

    def test_dashboard_reads_during_follow(self):
        trace = self.trace(session_events(1, 'synthetic') + session_events(8, 'second'),
                           delay=0.05)
        config = self.config(trace)
        worker, store, lines = self.worker(config)
        thread = self.start(worker)
        # The dashboard uses its own connection, like a separate process.
        server = make_server(Store(config.database), [], 0)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            from urllib.request import urlopen
            base = 'http://127.0.0.1:%d' % server.server_port
            observed = []
            for _ in range(25):
                with urlopen(base + '/api/overview', timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    observed.append(json.loads(response.read())['summary']['sessions'])
                time.sleep(0.02)
            self.assertTrue(set(observed) <= {0, 1, 2})
            self.assertTrue(wait_until(lambda: self.cursor(config) == 14), lines)
            with urlopen(base + '/api/overview', timeout=3) as response:
                self.assertEqual(json.loads(response.read())['summary']['sessions'], 2)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
            self.stop_worker(worker, thread)


class ArchiveOnlyModeTests(unittest.TestCase):
    class StrictStore:
        def query(self, filters):
            return {'summary': {'sessions': 0}}

        def detail(self, identity, filters=None):
            return None

        def import_paths(self, paths, instance):
            raise AssertionError('archive-only mode must not read source logs')

    def setUp(self):
        self.server = make_server(self.StrictStore(), ['/not/allowed'], 0, archive_only=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:%s' % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, data=None, headers=None):
        from urllib.request import Request, urlopen
        request = Request(self.base + path, data=data, headers=headers or {})
        with urlopen(request, timeout=3) as response:
            return response.status, response.read()

    def test_refresh_only_reads_received_data(self):
        status, body = self.request('/api/refresh', b'{}', {'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        report = json.loads(body)
        self.assertEqual(report['mode'], 'archive_only')
        self.assertEqual((report['scanned'], report['imported']), (0, 0))
        self.assertTrue(report['warnings'])
        self.assertIn('received_at', report)

    def test_capabilities_reports_archive_only(self):
        status, body = self.request('/api/capabilities')
        self.assertEqual(status, 200)
        self.assertIn('no Codex, pi or dsh source access', json.loads(body)['mode'])


class CliTests(FollowTests):
    def test_follow_cli_with_sigterm_stops_cleanly(self):
        trace = self.trace(session_events(1, 'synthetic'))
        config = self.config(trace)
        env = dict(os.environ)
        env['TEST_TRACE_TOKEN'] = 'test-token'
        process = subprocess.Popen(
            [sys.executable, '-m', 'agent_stats', 'follow', '--config', str(config.path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            cwd=str(Path(__file__).resolve().parent.parent))
        try:
            self.assertTrue(wait_until(lambda: self.cursor(config) == 7, timeout=20))
            process.terminate()
            out, err = process.communicate(timeout=20)
        finally:
            if process.poll() is None:
                process.kill()
        self.assertEqual(process.returncode, 0, err)
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual(events[0]['type'], 'follow.started')
        self.assertEqual(events[-1]['type'], 'follow.stopped')
        self.assertEqual(events[-1]['reason'], 'signal')
        self.assertEqual(events[-1]['cursor'], 7)
        self.assertNotIn('test-token', out + err)

    def test_follow_cli_rejects_bad_config_with_json_error(self):
        path = self.root / 'follow.json'
        path.write_text(json.dumps({'schema_version': 1}), encoding='utf-8')
        result = subprocess.run([sys.executable, '-m', 'agent_stats', 'follow', '--config', str(path)],
                                capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent.parent))
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stderr.strip().splitlines()[-1])
        self.assertEqual(payload['error']['code'], 'INVALID_CONFIG')
        self.assertEqual(payload['error']['class'], 'permanent')

    def test_follow_cli_reports_missing_token(self):
        trace = self.trace()
        path = self.root / 'follow.json'
        path.write_text(json.dumps({'schema_version': 1, 'trace_url': trace.url,
                                    'token_env': 'UNSET_TRACE_TOKEN_FOR_TEST',
                                    'database': 'stats.sqlite3'}), encoding='utf-8')
        result = subprocess.run([sys.executable, '-m', 'agent_stats', 'follow', '--config', str(path)],
                                capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent.parent),
                                env={key: value for key, value in os.environ.items()
                                     if key != 'UNSET_TRACE_TOKEN_FOR_TEST'})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stderr.strip().splitlines()[-1])['error']['code'], 'TOKEN_MISSING')


if __name__ == '__main__':
    unittest.main()
