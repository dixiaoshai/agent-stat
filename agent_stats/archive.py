"""Consume versioned, metadata-only archive exports without opening source logs."""
import hashlib
import json
from datetime import datetime, timezone

from .codex import FIELDS, summarize_events, timestamp, usage_total

_COMMON = {'turn_id', 'root_turn_id', 'model'}
_FIELDS = {
    'session.started': {'cwd', 'parent_session_id', 'forked_from_session_id', 'created_at', 'cli_version', 'title'},
    'turn.context': set(), 'turn.started': set(), 'turn.completed': set(), 'turn.interrupted': set(),
    'message.user': {'title'}, 'message.assistant': set(), 'reasoning.summary': {'available'},
    'tool.called': {'call_id', 'name', 'input_bytes'},
    'tool.returned': {'call_id', 'output_bytes', 'status'},
    'usage.snapshot': {'cumulative', 'last_usage', 'rate_limit_only'},
    'context.compacted': set(), 'source.unknown': set(), 'session.deleted': set(),
}
_ENVELOPE = {'schema_version', 'event_id', 'archive_seq', 'runtime', 'instance_id', 'session_id',
             'session_key', 'project_id', 'kind', 'occurred_at', 'observed_at', 'native_type',
             'source', 'data', 'body', 'raw'}
_HEX = set('0123456789abcdef')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _id(value):
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX for c in value)


def _integer(value):
    return type(value) is int and 0 <= value <= 9007199254740991


def _validate_event(event, after, end):
    if not isinstance(event, dict) or set(event) != _ENVELOPE or event['schema_version'] != 1:
        raise ValueError('invalid archive event envelope')
    if event['body'] is not None or event['raw'] is not None:
        raise ValueError('metadata events must not contain body or raw content')
    if not _id(event['event_id']) or not _integer(event['archive_seq']) or not after < event['archive_seq'] <= end:
        raise ValueError('invalid event identity or sequence')
    for key in ('runtime', 'instance_id', 'session_id', 'project_id', 'kind', 'native_type'):
        if not isinstance(event[key], str) or not event[key] or len(event[key]) > 1000:
            raise ValueError('invalid archive event field: ' + key)
    key = json.dumps([event['runtime'], event['instance_id'], event['session_id']], ensure_ascii=False, separators=(',', ':'))
    if event['session_key'] != key:
        raise ValueError('invalid session key')
    source = event['source']
    if event['kind'] == 'session.deleted':
        if source is not None or event['data'] != {}:
            raise ValueError('deletion source must be null and metadata must be empty')
    elif (not isinstance(source, dict) or set(source) != {'path', 'line', 'record_hash'}
          or not isinstance(source['path'], str) or not _integer(source['line']) or source['line'] < 1
          or not _id(source['record_hash'])):
        raise ValueError('invalid source reference')
    if event['runtime'] == 'codex':
        native_identity = [event['runtime'], event['instance_id'], event['session_id'],
                           'deleted' if event['kind'] == 'session.deleted' else source['line']]
        native_json = json.dumps(native_identity, ensure_ascii=False, separators=(',', ':'))
        if hashlib.sha256(native_json.encode('utf-8')).hexdigest() != event['event_id']:
            raise ValueError('invalid Codex event ID')
    for name in ('occurred_at', 'observed_at'):
        if event[name] is not None and timestamp(event[name]) is None:
            raise ValueError('invalid timestamp')
    if event['observed_at'] is None:
        raise ValueError('observation timestamp is required')
    data = event['data']
    allowed = _COMMON | _FIELDS.get(event['kind'], set())
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError('unsupported metadata fields')
    for name, value in data.items():
        if name in ('cumulative', 'last_usage'):
            if not isinstance(value, dict) or set(value) - set(FIELDS) or any(not _integer(v) for v in value.values()):
                raise ValueError('invalid usage')
        elif name in ('input_bytes', 'output_bytes'):
            if value is not None and not _integer(value):
                raise ValueError('invalid byte count')
        elif name in ('available', 'rate_limit_only'):
            if value is not None and type(value) is not bool:
                raise ValueError('invalid boolean')
        elif value is not None and (not isinstance(value, str) or len(value) > (80 if name == 'title' else 4096)):
            raise ValueError('invalid metadata value: ' + name)
    if isinstance(data.get('title'), str) and ('\n' in data['title'] or '\r' in data['title']):
        raise ValueError('title must be one line')
    if event['kind'] == 'tool.returned' and data.get('status') not in (None, 'result_observed'):
        raise ValueError('invalid tool status')
    return key


def _observation(event):
    kind, data = event['kind'], event['data']
    clean = {name: data.get(name) for name in _COMMON}
    mapping = {
        'turn.context': ('turn_context', None), 'turn.started': ('event_msg', 'task_started'),
        'turn.completed': ('event_msg', 'task_complete'), 'turn.interrupted': ('event_msg', 'turn_aborted'),
        'usage.snapshot': ('event_msg', 'token_count'), 'message.user': ('event_msg', 'user_message'),
        'tool.called': ('response_item', 'function_call'), 'tool.returned': ('response_item', 'function_call_output'),
        'reasoning.summary': ('response_item', 'reasoning'),
        'message.assistant': ('response_item', 'message'),
    }
    native, subtype = mapping.get(kind, ('archive_unknown', None))
    clean['type'] = subtype
    if kind == 'message.user':
        clean['prompt_title'] = data.get('title')
    if kind == 'message.assistant':
        clean['assistant_message'] = True
    if kind in ('tool.called', 'tool.returned'):
        clean.update({name: data.get(name) for name in ('call_id', 'name', 'input_bytes', 'output_bytes')})
    if kind == 'usage.snapshot':
        clean.update(cumulative=usage_total(data.get('cumulative')), last_usage=usage_total(data.get('last_usage')),
                     rate_limit_only=data.get('rate_limit_only', False))
    return (timestamp(event['occurred_at']), event['source']['line'], native, clean)


def _rebuild(db, archive_id, session_key):
    rows = db.execute('SELECT body FROM archive_events WHERE archive_id=? AND session_key=? ORDER BY seq',
                      (archive_id, session_key)).fetchall()
    events = [json.loads(row[0]) for row in rows]
    start = next((e for e in events if e['kind'] == 'session.started'), None)
    if not start:
        return
    data = start['data']
    meta = {'id': start['session_id'], 'cwd': data.get('cwd'), 'timestamp': data.get('created_at'),
            'parent_id': data.get('parent_session_id'), 'forked_from_id': data.get('forked_from_session_id'),
            'cli_version': data.get('cli_version'), 'recorded_at': timestamp(start['occurred_at'])}
    observations = [_observation(e) for e in events if e['kind'] != 'session.deleted']
    result, _ = summarize_events(observations, meta, instance=start['instance_id'])
    if not result:
        raise ValueError('session cannot be summarized')
    result['native_id'], result['id'] = result['id'], session_key
    for field in ('parent_id', 'forked_from_id'):
        if result.get(field):
            result[field] = json.dumps(['codex', start['instance_id'], result[field]], ensure_ascii=False, separators=(',', ':'))
    for turn in result['turns']:
        turn['native_id'], turn['id'] = turn['id'], json.dumps([session_key, turn['id']], ensure_ascii=False, separators=(',', ':'))
    result['project_name'] = start['project_id']
    result['title'] = data.get('title')
    result['source_path'] = start['source']['path']
    result['received_at'] = datetime.now(timezone.utc).isoformat()
    db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?)', (session_key, 0, json.dumps(result, ensure_ascii=False)))


def import_batch(store, batch):
    """Apply exactly one metadata page and cursor in one transaction."""
    keys = {'schema_version', 'archive_id', 'view', 'after_cursor', 'next_cursor', 'has_more', 'events'}
    if (not isinstance(batch, dict) or set(batch) != keys or batch['schema_version'] != 1
            or batch['view'] != 'metadata' or not isinstance(batch['archive_id'], str) or not batch['archive_id']
            or not _integer(batch['after_cursor']) or not _integer(batch['next_cursor'])
            or batch['next_cursor'] < batch['after_cursor'] or type(batch['has_more']) is not bool
            or not isinstance(batch['events'], list)):
        raise ValueError('invalid archive batch')
    after, end, archive_id = batch['after_cursor'], batch['next_cursor'], batch['archive_id']
    previous = after
    for event in batch['events']:
        _validate_event(event, after, end)
        if event['runtime'] != 'codex':
            raise ValueError('unsupported runtime')
        if event['archive_seq'] <= previous:
            raise ValueError('events must be ordered')
        previous = event['archive_seq']
    if batch['has_more'] and end == after:
        raise ValueError('non-advancing page')
    fingerprint = _digest(batch)
    db = store.db
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('CREATE TABLE IF NOT EXISTS archive_events (archive_id TEXT NOT NULL, seq INTEGER NOT NULL, event_id TEXT NOT NULL, session_key TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(archive_id,seq), UNIQUE(archive_id,event_id))')
        db.execute('CREATE TABLE IF NOT EXISTS archive_cursors (archive_id TEXT PRIMARY KEY, cursor INTEGER NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS archive_pages (archive_id TEXT NOT NULL, after_cursor INTEGER NOT NULL, next_cursor INTEGER NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(archive_id,after_cursor,next_cursor))')
        db.execute('CREATE TABLE IF NOT EXISTS archive_deleted (archive_id TEXT NOT NULL, session_key TEXT NOT NULL, PRIMARY KEY(archive_id,session_key))')
        db.execute('CREATE TABLE IF NOT EXISTS archive_sessions (session_key TEXT PRIMARY KEY, archive_id TEXT NOT NULL, project_id TEXT NOT NULL)')
        row = db.execute('SELECT cursor FROM archive_cursors WHERE archive_id=?', (archive_id,)).fetchone()
        cursor = row[0] if row else 0
        if after != cursor:
            page = db.execute('SELECT fingerprint FROM archive_pages WHERE archive_id=? AND after_cursor=? AND next_cursor=?', (archive_id, after, end)).fetchone()
            if end <= cursor and page and page[0] == fingerprint:
                db.commit()
                return {'archive_id': archive_id, 'cursor': cursor, 'imported': 0, 'replayed': True}
            raise ValueError('archive cursor gap, overlapping page or conflicting replay')
        affected = set()
        for event in batch['events']:
            key = event['session_key']
            owner = db.execute('SELECT archive_id,project_id FROM archive_sessions WHERE session_key=?', (key,)).fetchone()
            if owner and owner != (archive_id, event['project_id']):
                raise ValueError('session already belongs to another archive or project')
            db.execute('INSERT OR IGNORE INTO archive_sessions VALUES(?,?,?)', (key, archive_id, event['project_id']))
            if event['kind'] == 'session.deleted':
                if db.execute('SELECT 1 FROM archive_deleted WHERE archive_id=? AND session_key=?', (archive_id, key)).fetchone():
                    raise ValueError('duplicate deletion event')
                db.execute('DELETE FROM archive_events WHERE archive_id=? AND session_key=?', (archive_id, key))
                db.execute('INSERT OR IGNORE INTO archive_deleted VALUES(?,?)', (archive_id, key))
                db.execute('DELETE FROM sessions WHERE key=?', (key,))
                affected.discard(key)
                continue
            if db.execute('SELECT 1 FROM archive_deleted WHERE archive_id=? AND session_key=?', (archive_id, key)).fetchone():
                raise ValueError('deleted session cannot be resurrected')
            try:
                db.execute('INSERT INTO archive_events VALUES(?,?,?,?,?)',
                           (archive_id, event['archive_seq'], event['event_id'], key, _canonical(event)))
            except Exception as exc:
                raise ValueError('conflicting archive event identity or sequence') from exc
            affected.add(key)
        for key in affected:
            _rebuild(db, archive_id, key)
        db.execute('INSERT OR REPLACE INTO archive_cursors VALUES(?,?)', (archive_id, end))
        db.execute('INSERT OR REPLACE INTO archive_pages VALUES(?,?,?,?)', (archive_id, after, end, fingerprint))
        db.commit()
        return {'archive_id': archive_id, 'cursor': end, 'imported': len(batch['events']), 'replayed': False}
    except Exception:
        db.rollback()
        raise
