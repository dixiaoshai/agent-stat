"""On-demand read-only access to an explicitly configured Trace content archive.

No raw rollout access, writes, network clients, or Knowledge dependency. Stats
continues to persist metadata only; response bodies live only for the request.
"""
import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from .archive import _rollout_result, _source_group

KINDS = {'message.user', 'message.assistant', 'reasoning.summary', 'tool.called',
         'tool.returned', 'turn.completed', 'context.compacted'}
MAX_PAGE_BYTES = 16 * 1024 * 1024
MAX_BLOB_BYTES = 16 * 1024 * 1024
MAX_SESSION_BYTES = 128 * 1024 * 1024


class ContentError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _result(status, message=None, archive_id=None, **extra):
    return dict(status=status, message=message, archive_id=archive_id,
                events=[], next_cursor=0, has_more=False, **extra)


def default_content_config():
    configured = os.environ.get('AGENT_STATS_CONTENT_CONFIG')
    if configured:
        return configured
    default = Path.home() / '.local/share/agent-trace/content.json'
    return str(default) if default.is_file() else None


def _integer(value, name, maximum=9007199254740991):
    if not re.fullmatch(r'[0-9]{1,16}', str(value)):
        raise ContentError('无效的正文分页参数：' + name)
    number = int(value)
    if not 0 <= number <= maximum:
        raise ContentError('正文分页参数超出范围：' + name)
    return number


def _fingerprint(events, through):
    # Only the prefix already delivered: new appends do not invalidate a page,
    # but deleted/replaced records and changed turn attribution do.
    prefix = [(e['seq'], e['event_id'], (e.get('source') or {}).get('record_hash'), e['turn_id'])
              for e in events[:through]]
    return hashlib.sha256(json.dumps(prefix, ensure_ascii=False).encode()).hexdigest()


def _assign_turns(events, start):
    """Use lifecycle/context in one selected rollout; never infer from time gaps."""
    assigned, pending, tools = {}, [], {}
    active = None
    forked = start['data'].get('forked_from_session_id')
    fork_start = start['data'].get('created_at') or start.get('occurred_at')

    def assign(event, target, explicit=False):
        assigned[event['event_id']] = (target, 'explicit' if explicit else 'lifecycle' if target else 'unassigned')

    # Match summarize_events' timestamp/line ordering, including equal timestamps.
    for event in sorted(events, key=lambda e: (e.get('occurred_at') or '', e['source']['line'])):
        if forked and (not event.get('occurred_at') or (fork_start and event['occurred_at'] < fork_start)):
            continue
        data, kind = event['data'], event['kind']
        explicit = data.get('turn_id') or data.get('root_turn_id')
        if kind == 'message.user' and not explicit:
            pending.append(event)
            continue
        if kind in ('turn.context', 'turn.started'):
            active = explicit
        target = explicit or active
        if kind == 'tool.returned' and not explicit:
            target = tools.get(data.get('call_id')) or target
        if pending and (kind in ('turn.context', 'turn.started', 'turn.completed', 'turn.interrupted',
                                'message.assistant', 'tool.called', 'tool.returned', 'reasoning.summary')):
            for user in pending:
                assign(user, target)
            pending = []
        assign(event, target, bool(explicit))
        if kind == 'tool.called' and data.get('call_id'):
            tools[data['call_id']] = target
        if kind in ('turn.completed', 'turn.interrupted') and target == active:
            active = None
    for user in pending:
        assign(user, None)
    return assigned


class ContentReader:
    def __init__(self, config_path):
        self.config_path = Path(config_path).expanduser() if config_path else None

    def _config(self):
        config = json.loads(self.config_path.read_text(encoding='utf-8'))
        if config.get('schema_version') != 1 or config.get('capture_mode') != 'content':
            raise ValueError('Expected a Trace content configuration')
        base = self.config_path.absolute().parent
        database = config.get('database')
        if not isinstance(database, str) or not database or database == ':memory:':
            raise ValueError('Content database required')
        database = (base / database).absolute()
        blob = config.get('content', {}).get('blob_directory')
        blob = (base / blob).absolute() if isinstance(blob, str) else database.parent / 'content-blobs'
        projects = {project['project_id'] for source in config['collection']['sources']
                    for project in source['projects']}
        if not projects or not all(isinstance(p, str) and p for p in projects):
            raise ValueError('Content project scope required')
        return database, blob, projects

    def _snapshot(self, session_key):
        database, blobs, projects = self._config()
        # A fresh read-only connection observes new commits without retaining a
        # stale snapshot or creating a missing database/directory.
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('Unsupported Trace archive schema')
            metadata = dict(db.execute('SELECT key,value FROM metadata'))
            if metadata.get('capture_mode') != 'content' or not metadata.get('archive_id'):
                raise ValueError('Not a content archive')
            archive_id = metadata['archive_id']
            session = db.execute('SELECT project_id,deleted FROM sessions WHERE session_key=?', (session_key,)).fetchone()
            if not session or session[1] or session[0] not in projects:
                return archive_id, blobs, [], None
            count, size = db.execute('SELECT count(*),coalesce(sum(length(payload)),0) FROM events WHERE session_key=?', (session_key,)).fetchone()
            if count > 100000 or size > MAX_SESSION_BYTES:
                raise ContentError('此会话归档过大，暂不能在详情中加载。', 413)
            rows = db.execute("SELECT seq,json_remove(payload,'$.raw') FROM events WHERE session_key=? ORDER BY seq", (session_key,)).fetchall()
        groups = {}
        for seq, payload in rows:
            event = json.loads(payload)
            if event.get('session_key') != session_key or event.get('project_id') != session[0]:
                raise ValueError('Content identity mismatch')
            event['seq'] = seq
            if event.get('source'):
                groups.setdefault(_source_group(event), []).append(event)
        chosen, selected = None, []
        for group in groups.values():
            candidate = _rollout_result(group)
            if candidate is not None and (chosen is None or candidate[0] > chosen[0]):
                chosen, selected = candidate, group
        if chosen is None:
            return archive_id, blobs, [], None
        assigned = _assign_turns(selected, chosen[1][1])
        visible = []
        for event in selected:
            if event['kind'] not in KINDS or event['event_id'] not in assigned:
                continue
            if event['kind'] == 'turn.completed' and not event.get('body'):
                continue
            turn, attribution = assigned[event['event_id']]
            visible.append({**{key: event.get(key) for key in ('event_id', 'seq', 'kind', 'occurred_at', 'source', 'data', 'body')},
                            'turn_id': turn, 'attribution': attribution})
        # SQLite seq is storage order, not chronology: a rewritten source line
        # is deleted/reinserted and receives a later seq. Page the logical order.
        visible.sort(key=lambda e: (e.get('occurred_at') or '', e['source']['line'], e['event_id']))
        return archive_id, blobs, visible, None

    def page(self, session_key, turn_id=None, after=0, limit=50, archive_id=None, revision=None):
        after, limit = _integer(after, 'after'), _integer(limit, 'limit', 100)
        if limit == 0:
            raise ContentError('正文页大小必须大于零')
        if not self.config_path:
            return _result('not_configured', '尚未配置正文归档。统计数据仍可查看。')
        try:
            archive, _, events, _ = self._snapshot(session_key)
            matching = [event for event in events if not turn_id or (event['turn_id'] or 'unassigned') == turn_id]
            if after and ((archive_id and archive_id != archive) or (revision and revision != _fingerprint(matching, after))):
                raise ContentError('已加载的正文归档有变更，请重新加载。', 409)
            if after > len(matching):
                raise ContentError('正文分页位置已失效，请重新加载。', 409)
            remaining = matching[after:]
            page, page_bytes = [], 0
            for event in remaining[:limit]:
                size = len(json.dumps(event, ensure_ascii=False).encode('utf-8'))
                if page_bytes + size > MAX_PAGE_BYTES:
                    if not page:
                        raise ContentError('单条正文过大，请通过归档工具查看。', 413)
                    break
                page.append(event)
                page_bytes += size
            return {'status': 'available' if matching else 'empty',
                    'message': None if matching else '此任务尚无可展示的归档正文，可能未纳入采集范围或尚未同步。',
                    'archive_id': archive, 'revision': _fingerprint(matching, after + len(page)), 'events': page,
                    'next_cursor': after + len(page), 'has_more': len(remaining) > len(page)}
        except ContentError:
            raise
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            return _result('unavailable', '正文归档暂不可用，请检查 content 配置、数据库或访问权限；统计仍可查看。')

    def blob(self, session_key, event_id, pointer):
        if not self.config_path:
            raise ContentError('尚未配置正文归档。', 404)
        if not isinstance(pointer, str) or not pointer.startswith('/') or len(pointer) > 4096:
            raise ContentError('无效的正文引用')
        try:
            _, directory, events, _ = self._snapshot(session_key)
            event = next((event for event in events if event['event_id'] == event_id), None)
            if not event:
                raise ContentError('正文事件不存在或不在当前范围内。', 404)
            value = event['body']
            for part in pointer[1:].split('/'):
                if re.search(r'~(?![01])', part):
                    raise ContentError('无效的正文引用')
                key = part.replace('~1', '/').replace('~0', '~')
                value = value[int(key)] if isinstance(value, list) and re.fullmatch(r'0|[1-9][0-9]*', key) else value[key]
            ref = value['content_ref']
            if (not isinstance(ref, dict) or ref.get('media_type') != 'text/plain'
                    or type(ref.get('bytes')) is not int or ref['bytes'] < 0
                    or not re.fullmatch(r'[a-f0-9]{64}', ref.get('sha256', ''))):
                raise ContentError('正文引用无效。')
            if ref['bytes'] > MAX_BLOB_BYTES:
                raise ContentError('外置正文超过 16 MiB，请通过归档工具查看。', 413)
            path = Path(ref['path'])
            # Trace stores flat, content-addressed files. Do not follow a ref
            # into another directory, a symlink, device, or arbitrary file.
            if (not path.is_absolute() or path.parent.absolute() != directory.absolute()
                    or path.name != event_id + '-' + ref['sha256'] + '.txt'
                    or path.is_symlink() or directory.resolve() != directory.absolute()):
                raise ContentError('正文引用不在允许的文件范围内。', 403)
            descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != ref['bytes']:
                    raise ContentError('正文文件大小校验失败。', 409)
                data = stream.read(MAX_BLOB_BYTES + 1)
            if len(data) != ref['bytes'] or hashlib.sha256(data).hexdigest() != ref['sha256']:
                raise ContentError('正文文件完整性校验失败。', 409)
            return {'text': data.decode('utf-8')}
        except ContentError:
            raise
        except (OSError, ValueError, TypeError, KeyError, IndexError, sqlite3.Error):
            raise ContentError('外置正文不可用，可能已清理或暂时无法读取。', 404)
