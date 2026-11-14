"""Follow agent-trace's loopback API and apply metadata pages locally.

The worker is the only owner of its progress: agent-trace never tracks delivery
or acknowledges pages. Each page is applied with ``archive.import_batch`` so the
stats projection and the archive cursor commit in one transaction; the extra
``follow_state`` row only records identity, health and the single-worker lease.

Nothing here reads Codex source logs. ``follow`` and the dashboard are separate
processes that share the stats database.
"""
import hashlib
import http.client
import json
import os
import random
import re
import signal
import socket
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .archive import import_batch
from .core import Store

VIEW = 'metadata'
LOOPBACK = ('127.0.0.1', '::1', 'localhost')
TOKEN_ENV = re.compile(r'^[A-Z][A-Z0-9_]*$')
DEFAULT_PAGE_SIZE = 500
DEFAULT_IDLE_SECONDS = 15
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_BACKOFF_SECONDS = 300
MAX_RESPONSE_BYTES = 40 * 1024 * 1024
LEASE_SECONDS = 30
LEASE_CHUNK_SECONDS = 10
CONFIG_KEYS = {'schema_version', 'trace_url', 'token_env', 'database', 'page_size',
               'idle_seconds', 'request_timeout_seconds', 'max_backoff_seconds'}

_STATE_DDL = """
CREATE TABLE IF NOT EXISTS follow_state (
  follow_id TEXT PRIMARY KEY,
  trace_url TEXT NOT NULL,
  archive_id TEXT,
  view TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_attempt_at TEXT,
  last_success_at TEXT,
  last_error_code TEXT,
  last_error_class TEXT,
  last_error_at TEXT,
  lease_owner TEXT,
  lease_expires_at REAL
)
"""


def _at():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


class FollowError(Exception):
    """Classified sync failure: ``transient`` retries, ``permanent`` stops."""

    def __init__(self, code, klass, message, retry_after=None):
        super().__init__(message)
        self.code = code
        self.klass = klass
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class FollowConfig:
    path: Path
    trace_url: str
    token_env: str
    database: Path
    page_size: int = DEFAULT_PAGE_SIZE
    idle_seconds: int = DEFAULT_IDLE_SECONDS
    request_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_backoff_seconds: int = DEFAULT_MAX_BACKOFF_SECONDS


def _invalid(field, message):
    raise FollowError('INVALID_CONFIG', 'permanent', 'config.%s: %s' % (field, message))


def _bounded_int(raw, field, low, high):
    value = raw if raw is not None else None
    if type(value) is not int or not low <= value <= high:
        _invalid(field, 'must be an integer %d..%d' % (low, high))
    return value


def parse_trace_url(raw):
    if not isinstance(raw, str) or not raw:
        _invalid('trace_url', 'must be a non-empty string')
    parts = urlsplit(raw)
    if parts.scheme != 'http':
        _invalid('trace_url', 'must use http')
    if parts.username or parts.password:
        _invalid('trace_url', 'must not contain credentials')
    if parts.path not in ('', '/'):
        _invalid('trace_url', 'must not contain a path')
    if parts.query or parts.fragment:
        _invalid('trace_url', 'must not contain a query or fragment')
    host = parts.hostname
    if host not in LOOPBACK:
        _invalid('trace_url', 'host must be 127.0.0.1, ::1 or localhost')
    try:
        port = parts.port
    except ValueError:
        _invalid('trace_url', 'port is invalid')
    if port is None or not 1 <= port <= 65535:
        _invalid('trace_url', 'must include the port agent-trace listens on')
    netloc = ('[%s]:%d' % (host, port)) if ':' in host else ('%s:%d' % (host, port))
    return urlunsplit(('http', netloc, '', '', ''))


def load_config(path, db_override=None):
    """Strictly parse a follow config; relative paths resolve against the file."""
    config_path = Path(path).expanduser()
    try:
        raw = json.loads(config_path.read_text(encoding='utf-8'))
    except OSError:
        raise FollowError('INVALID_CONFIG', 'permanent', 'config: cannot read file %s' % config_path)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise FollowError('INVALID_CONFIG', 'permanent', 'config: file is not valid JSON')
    if not isinstance(raw, dict):
        _invalid('', 'top level must be an object')
    unknown = sorted(set(raw) - CONFIG_KEYS)
    if unknown:
        _invalid('', 'unknown field(s): ' + ', '.join(unknown))
    if raw.get('schema_version') != 1:
        _invalid('schema_version', 'must be 1')
    for field in ('trace_url', 'token_env', 'database'):
        if field not in raw:
            _invalid(field, 'is required')
    trace_url = parse_trace_url(raw['trace_url'])
    token_env = raw['token_env']
    if not isinstance(token_env, str) or not TOKEN_ENV.match(token_env):
        _invalid('token_env', 'must be an environment variable name such as AGENT_TRACE_STATS_TOKEN')
    database = raw['database']
    if not isinstance(database, str) or not database:
        _invalid('database', 'must be a non-empty string')
    if database.startswith('~'):
        _invalid('database', 'must not use ~; use an absolute path or a path relative to the config file')
    idle_seconds = _bounded_int(raw.get('idle_seconds', DEFAULT_IDLE_SECONDS), 'idle_seconds', 1, 3600)
    page_size = _bounded_int(raw.get('page_size', DEFAULT_PAGE_SIZE), 'page_size', 1, 1000)
    request_timeout_seconds = _bounded_int(raw.get('request_timeout_seconds', DEFAULT_TIMEOUT_SECONDS),
                                           'request_timeout_seconds', 1, 120)
    max_backoff_seconds = _bounded_int(raw.get('max_backoff_seconds', DEFAULT_MAX_BACKOFF_SECONDS),
                                       'max_backoff_seconds', idle_seconds, 3600)
    if db_override:
        # CLI override keeps the existing CLI convention: relative to cwd.
        target = Path(db_override).expanduser()
    else:
        target = Path(database)
        if not target.is_absolute():
            target = config_path.parent / target
    return FollowConfig(path=config_path, trace_url=trace_url, token_env=token_env,
                        database=target, page_size=page_size, idle_seconds=idle_seconds,
                        request_timeout_seconds=request_timeout_seconds,
                        max_backoff_seconds=max_backoff_seconds)


def read_token(config):
    value = os.environ.get(config.token_env)
    if not isinstance(value, str) or not value or value != value.strip():
        raise FollowError('TOKEN_MISSING', 'permanent',
                          'environment variable %s must hold the bearer token' % config.token_env)
    return value


# -- HTTP client ------------------------------------------------------------

class _NoRedirect(HTTPRedirectHandler):
    """Refuse redirects so a loopback URL cannot be bounced to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TraceClient:
    def __init__(self, base_url, token, timeout, page_size=DEFAULT_PAGE_SIZE):
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.timeout = timeout
        self.page_size = page_size
        self.opener = build_opener(_NoRedirect())

    def status(self):
        data = self._get('/v1/status')
        return validate_status(data, self.page_size)

    def events(self, after, limit):
        return self._get('/v1/events', {'after': str(after), 'limit': str(limit), 'view': VIEW})

    def _get(self, path, query=None):
        url = self.base_url + path
        if query:
            url += '?' + urlencode(query)
        request = Request(url, headers={'Authorization': 'Bearer ' + self.token,
                                        'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise _http_error(exc)
        except URLError as exc:
            raise _network_error(exc.reason)
        except (socket.timeout, TimeoutError):
            raise FollowError('TIMEOUT', 'transient', 'request timed out')
        except http.client.IncompleteRead:
            raise FollowError('RESPONSE_TRUNCATED', 'transient', 'response was truncated')
        except OSError as exc:
            raise _network_error(exc)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise FollowError('PAGE_TOO_LARGE', 'permanent',
                              'response exceeds the local size limit; reduce page_size and restart')
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise FollowError('INVALID_PAGE', 'permanent', 'response is not valid JSON')


def _network_error(reason):
    text = str(reason or 'connection failed')
    if isinstance(reason, (socket.timeout, TimeoutError)) or 'timed out' in text.lower():
        return FollowError('TIMEOUT', 'transient', 'request timed out')
    return FollowError('CONNECTION_ERROR', 'transient', text)


def _http_error(exc):
    code, message = None, None
    try:
        payload = json.loads(exc.read(65536) or b'null')
        if isinstance(payload, dict) and isinstance(payload.get('error'), dict):
            code = payload['error'].get('code')
            message = payload['error'].get('message')
    except (OSError, ValueError):
        pass
    if not isinstance(code, str) or not code:
        code = {401: 'UNAUTHORIZED', 403: 'FORBIDDEN', 404: 'NOT_FOUND', 405: 'METHOD_NOT_ALLOWED',
                413: 'PAGE_TOO_LARGE', 414: 'QUERY_TOO_LARGE'}.get(exc.code, 'HTTP_%d' % exc.code)
    if not isinstance(message, str) or not message:
        message = 'agent-trace returned HTTP %d' % exc.code
    transient = exc.code in (408, 429) or 500 <= exc.code <= 599
    retry_after = None
    header = exc.headers.get('Retry-After') if exc.headers else None
    if isinstance(header, str) and header.strip().isdigit():
        retry_after = int(header.strip())
    return FollowError(code, 'transient' if transient else 'permanent', message, retry_after)


def validate_status(data, page_size):
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise FollowError('PROTOCOL', 'permanent', 'status response does not follow protocol v1')
    protocol = data.get('protocol')
    archive = data.get('archive')
    if not isinstance(protocol, dict) or protocol.get('schema_version') != 1:
        raise FollowError('PROTOCOL', 'permanent', 'status response is missing the v1 protocol block')
    max_events = protocol.get('max_page_events')
    if type(max_events) is not int or max_events < 1:
        raise FollowError('PROTOCOL', 'permanent', 'status response does not report max_page_events')
    if page_size > max_events:
        raise FollowError('PAGE_TOO_LARGE', 'permanent',
                          'page_size %d exceeds the server page limit %d; lower page_size and restart'
                          % (page_size, max_events))
    if not isinstance(archive, dict) or not isinstance(archive.get('archive_id'), str) or not archive['archive_id']:
        raise FollowError('PROTOCOL', 'permanent', 'status response does not report an archive_id')
    return {'archive_id': archive['archive_id'], 'max_page_events': max_events}


def validate_page(batch, after, page_size, archive_id):
    keys = {'schema_version', 'archive_id', 'view', 'after_cursor', 'next_cursor', 'has_more', 'events'}
    if not isinstance(batch, dict) or set(batch) != keys:
        raise FollowError('INVALID_PAGE', 'permanent', 'page envelope is not a v1 batch')
    if batch['schema_version'] != 1 or batch['view'] != VIEW:
        raise FollowError('INVALID_PAGE', 'permanent', 'page is not a v1 %s batch' % VIEW)
    if not isinstance(batch['archive_id'], str) or not batch['archive_id']:
        raise FollowError('INVALID_PAGE', 'permanent', 'page is missing archive_id')
    if batch['archive_id'] != archive_id:
        raise FollowError('ARCHIVE_CHANGED', 'permanent',
                          'stored archive_id differs from the Trace response')
    if batch['after_cursor'] != after:
        raise FollowError('INVALID_PAGE', 'permanent', 'page does not start at the committed cursor')
    if type(batch['next_cursor']) is not int or batch['next_cursor'] < after:
        raise FollowError('INVALID_PAGE', 'permanent', 'page cursor does not advance')
    if type(batch['has_more']) is not bool or not isinstance(batch['events'], list):
        raise FollowError('INVALID_PAGE', 'permanent', 'page envelope has an invalid type')
    if len(batch['events']) > page_size:
        raise FollowError('INVALID_PAGE', 'permanent', 'page returned more events than requested')
    if batch['has_more'] and batch['next_cursor'] == after:
        raise FollowError('INVALID_PAGE', 'permanent', 'page claims more data without advancing')
    for event in batch['events']:
        if not isinstance(event, dict) or not isinstance(event.get('archive_seq'), int):
            raise FollowError('INVALID_PAGE', 'permanent', 'page contains an invalid event')
        if not after < event['archive_seq'] <= batch['next_cursor']:
            raise FollowError('INVALID_PAGE', 'permanent', 'page contains an out-of-range event')
    return batch


# -- local sync state -------------------------------------------------------

def follow_id(database, view=VIEW):
    """Stable identity of one target database + fixed view."""
    key = _canonical(['agent-stats-follow', view, str(Path(database).expanduser().resolve())])
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


def _ensure_state(db, identifier, trace_url):
    db.execute(_STATE_DDL)
    db.execute('CREATE TABLE IF NOT EXISTS archive_cursors (archive_id TEXT PRIMARY KEY, cursor INTEGER NOT NULL)')
    now = _at()
    db.execute('INSERT OR IGNORE INTO follow_state (follow_id,trace_url,view,created_at,updated_at) VALUES(?,?,?,?,?)',
               (identifier, trace_url, VIEW, now, now))
    db.commit()
    return _state_row(db, identifier)


def _state_row(db, identifier):
    row = db.execute('SELECT * FROM follow_state WHERE follow_id=?', (identifier,)).fetchone()
    if row is None:
        return None
    names = [column[0] for column in db.execute('SELECT * FROM follow_state LIMIT 0').description]
    return dict(zip(names, row))


def _owner():
    return '%s:%d:%s' % (socket.gethostname(), os.getpid(), os.urandom(4).hex())


def _lease_alive(owner, expires_at, now):
    if not owner or not isinstance(expires_at, (int, float)) or expires_at <= now:
        return False
    host, _, rest = owner.partition(':')
    if host == socket.gethostname():
        pid_text = rest.split(':', 1)[0]
        if pid_text.isdigit():
            try:
                os.kill(int(pid_text), 0)
            except ProcessLookupError:
                return False
            except OSError:
                pass
    return True


def acquire_lease(db, identifier, owner):
    now = time.time()
    db.execute('BEGIN IMMEDIATE')
    try:
        row = _state_row(db, identifier)
        if row and row['lease_owner'] and row['lease_owner'] != owner:
            if _lease_alive(row['lease_owner'], row['lease_expires_at'], now):
                raise FollowError('FOLLOW_LOCKED', 'permanent',
                                  'another follow worker holds the lease for this database')
        db.execute('UPDATE follow_state SET lease_owner=?, lease_expires_at=? WHERE follow_id=?',
                   (owner, now + LEASE_SECONDS, identifier))
        db.commit()
    except Exception:
        db.rollback()
        raise


def renew_lease(db, identifier, owner):
    db.execute('BEGIN IMMEDIATE')
    try:
        cursor = db.execute('UPDATE follow_state SET lease_expires_at=? WHERE follow_id=? AND lease_owner=?',
                            (time.time() + LEASE_SECONDS, identifier, owner))
        if cursor.rowcount != 1:
            raise FollowError('FOLLOW_LOCKED', 'permanent',
                              'follow lease was lost; another worker may have taken over')
        db.commit()
    except Exception:
        db.rollback()
        raise


def release_lease(db, identifier, owner):
    try:
        db.execute('UPDATE follow_state SET lease_owner=NULL, lease_expires_at=NULL WHERE follow_id=? AND lease_owner=?',
                   (identifier, owner))
        db.commit()
    except sqlite3.Error:
        pass


def classify(exc):
    """Normalize local failures for the error report."""
    if isinstance(exc, FollowError):
        return exc
    if isinstance(exc, sqlite3.OperationalError):
        return FollowError('LOCAL_DB_BUSY', 'transient', 'target database is busy: ' + str(exc))
    if isinstance(exc, sqlite3.IntegrityError):
        return FollowError('LOCAL_CONFLICT', 'permanent', 'target database rejected the page: ' + str(exc))
    if isinstance(exc, sqlite3.Error):
        return FollowError('LOCAL_DB', 'permanent', 'target database failed: ' + str(exc))
    if isinstance(exc, ValueError):
        # import_batch rejects invalid pages and conflicting event identities.
        return FollowError('INVALID_PAGE', 'permanent', str(exc))
    return FollowError('INTERNAL', 'permanent', type(exc).__name__)


def _emit_stdout(event):
    print(json.dumps(event, ensure_ascii=False, separators=(',', ':')), flush=True)


def _stderr_error(error):
    payload = {'error': {'code': error.code, 'class': error.klass, 'message': error.message}}
    print(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), file=sys.stderr, flush=True)


class FollowWorker:
    """Single-target follower; one page per cycle, cursor committed with the page."""

    def __init__(self, config, store, token, client=None, emit=None):
        self.config = config
        self.store = store
        self.db = store.db
        self.client = client or TraceClient(config.trace_url, token, config.request_timeout_seconds,
                                            config.page_size)
        self.emit = emit or _emit_stdout
        self.stop_event = threading.Event()
        self.follow_id = follow_id(config.database)
        self.owner = _owner()
        self.archive_id = None
        self.cursor = 0
        self.failures = 0
        self.needs_probe = True
        self.fatal = None
        self.last_error = None

    # -- control -----------------------------------------------------------

    def request_stop(self):
        self.stop_event.set()

    def run(self):
        if not self._start():
            return 1
        reason = 'signal'
        status = 0
        try:
            while not self.stop_event.is_set() and self.fatal is None:
                try:
                    caught_up = self._cycle()
                except (FollowError, sqlite3.Error, ValueError) as exc:
                    error = classify(exc)
                    self._record_error(error)
                    self._emit_state()
                    delay = None if error.klass == 'permanent' else self._backoff(error)
                    self.emit({'type': 'follow.error', 'at': _at(), 'class': error.klass,
                               'code': error.code, 'message': error.message,
                               'retry_in_seconds': delay, 'cursor': self.cursor})
                    if error.klass == 'permanent':
                        self.last_error = error
                        reason = 'error'
                        status = 2
                        break
                    self.needs_probe = True
                    if self._wait(delay):
                        break
                    continue
                self.failures = 0
                if caught_up:
                    continue
                self.emit({'type': 'follow.idle', 'at': _at(), 'cursor': self.cursor,
                           'idle_seconds': self.config.idle_seconds})
                self._wait(self.config.idle_seconds)
            if self.fatal is not None and status == 0:
                error = classify(self.fatal)
                self._record_error(error)
                self.emit({'type': 'follow.error', 'at': _at(), 'class': error.klass,
                           'code': error.code, 'message': error.message,
                           'retry_in_seconds': None, 'cursor': self.cursor})
                self.last_error = error
                reason = 'error'
                status = 2
        finally:
            self._emit_state()
            release_lease(self.db, self.follow_id, self.owner)
            self.emit({'type': 'follow.stopped', 'at': _at(), 'reason': reason, 'cursor': self.cursor})
        if self.last_error is not None:
            _stderr_error(self.last_error)
        return status

    # -- lifecycle ---------------------------------------------------------

    def _start(self):
        try:
            _ensure_state(self.db, self.follow_id, self.config.trace_url)
            acquire_lease(self.db, self.follow_id, self.owner)
            row = _state_row(self.db, self.follow_id)
            self.archive_id = row['archive_id'] if row else None
            self.cursor = self._read_cursor()
        except (FollowError, sqlite3.Error, OSError) as exc:
            _stderr_error(classify(exc))
            return False
        self.emit({'type': 'follow.started', 'at': _at(), 'trace_url': self.config.trace_url,
                   'view': VIEW, 'archive_id': self.archive_id, 'cursor': self.cursor})
        self._emit_state()
        return True

    def _emit_state(self):
        row = None
        try:
            row = _state_row(self.db, self.follow_id)
        except sqlite3.Error:
            pass
        row = row or {}
        self.emit({'type': 'follow.state', 'at': _at(), 'archive_id': self.archive_id,
                   'cursor': self.cursor, 'last_success_at': row.get('last_success_at'),
                   'last_error': ({'code': row.get('last_error_code'), 'class': row.get('last_error_class'),
                                   'at': row.get('last_error_at')}
                                  if row.get('last_error_code') else None)})

    # -- one page ----------------------------------------------------------

    def _cycle(self):
        if self.needs_probe:
            self._probe()
        page = self.client.events(self.cursor, self.config.page_size)
        validate_page(page, self.cursor, self.config.page_size, self.archive_id)
        self._touch_attempt()
        result = import_batch(self.store, page)
        self.cursor = result['cursor']
        self._record_success()
        if page['events'] or page['next_cursor'] > page['after_cursor']:
            self.emit({'type': 'follow.page', 'at': _at(), 'after': page['after_cursor'],
                       'next_cursor': self.cursor, 'events': len(page['events']),
                       'has_more': page['has_more']})
        return page['has_more']

    def _probe(self):
        status = self.client.status()
        archive_id = status['archive_id']
        if self.archive_id and archive_id != self.archive_id:
            raise FollowError('ARCHIVE_CHANGED', 'permanent',
                              'stored archive_id differs from the Trace response')
        if not self.archive_id:
            self._bind(archive_id)
        self.needs_probe = False
        self.cursor = self._read_cursor()
        self._record_trace_url()

    def _bind(self, archive_id):
        now = _at()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('UPDATE follow_state SET archive_id=?, updated_at=? WHERE follow_id=?',
                            (archive_id, now, self.follow_id))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.archive_id = archive_id

    def _read_cursor(self):
        if not self.archive_id:
            return 0
        row = self.db.execute('SELECT cursor FROM archive_cursors WHERE archive_id=?',
                              (self.archive_id,)).fetchone()
        return row[0] if row else 0

    def _record_trace_url(self):
        self.db.execute('UPDATE follow_state SET trace_url=?, updated_at=? WHERE follow_id=?',
                       (self.config.trace_url, _at(), self.follow_id))
        self.db.commit()

    def _touch_attempt(self):
        self.db.execute('UPDATE follow_state SET last_attempt_at=?, updated_at=? WHERE follow_id=?',
                       (_at(), _at(), self.follow_id))
        self.db.commit()

    def _record_success(self):
        now = _at()
        self.db.execute('UPDATE follow_state SET archive_id=?, last_success_at=?, updated_at=?, '
                        'last_error_code=NULL, last_error_class=NULL, last_error_at=NULL '
                        'WHERE follow_id=?', (self.archive_id, now, now, self.follow_id))
        self.db.commit()

    def _record_error(self, error):
        now = _at()
        try:
            self.db.execute('UPDATE follow_state SET last_error_code=?, last_error_class=?, '
                            'last_error_at=?, updated_at=? WHERE follow_id=?',
                            (error.code, error.klass, now, now, self.follow_id))
            self.db.commit()
        except sqlite3.Error:
            pass

    # -- pacing ------------------------------------------------------------

    def _backoff(self, error):
        base = self.config.idle_seconds * (2 ** min(self.failures, 10))
        delay = min(self.config.max_backoff_seconds, base) * random.uniform(0.9, 1.1)
        delay = max(delay, float(self.config.idle_seconds))
        if error.retry_after is not None:
            delay = min(float(error.retry_after), float(self.config.max_backoff_seconds))
            delay = max(delay, float(self.config.idle_seconds))
        delay = min(delay, float(self.config.max_backoff_seconds))
        self.failures += 1
        self.last_delay = round(delay, 2)
        return self.last_delay

    def _wait(self, seconds):
        """Sleep in lease-sized chunks; returns True when a stop was requested."""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self.stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.stop_event.wait(min(remaining, LEASE_CHUNK_SECONDS))
            if self.stop_event.is_set():
                break
            try:
                renew_lease(self.db, self.follow_id, self.owner)
            except (FollowError, sqlite3.Error) as exc:
                self.fatal = exc
                return True
        return self.stop_event.is_set()


def run_follow(config_path, db_override=None):
    """CLI entry: validate, acquire, follow until stopped or a permanent error."""
    try:
        config = load_config(config_path, db_override)
        token = read_token(config)
    except FollowError as exc:
        _stderr_error(exc)
        return 1
    try:
        store = Store(config.database)
    except (sqlite3.Error, OSError, ValueError) as exc:
        _stderr_error(FollowError('LOCAL_DB', 'permanent',
                                  'cannot open target database: ' + type(exc).__name__))
        return 1
    worker = FollowWorker(config, store, token)

    def _stop(_signum, _frame):
        worker.request_stop()

    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass
    try:
        return worker.run()
    finally:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        store.db.close()
