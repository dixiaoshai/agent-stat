"""Source-independent normalized facts, persistence, and aggregation."""
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .codex import parse_rollout, timestamp
from .codex_metadata import metadata_home, project_name, read_project_metadata, read_session_titles
from .pricing import PRICING, aggregate_cost, estimate_turn
from .usage import aggregate_cache

SCHEMA_VERSION = 1


def _sum_known(turns):
    vals = [t['token_total'] for t in turns if t.get('token_total') is not None]
    return (sum(vals) if len(vals) == len(turns) and turns else None,
            sum(vals) if vals else None, len(vals))


def percentile(values, p):
    """Hyndman-Fan type 7: linear interpolation at (n-1)*p."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def _filters(filters):
    out = {k: v for k, v in (filters or {}).items() if v and k in ('start', 'end', 'source', 'project', 'model')}
    for key in ('start', 'end'):
        if key in out:
            normalized = timestamp(out[key])
            if not normalized:
                raise ValueError('时间必须是带时区的 ISO 8601: ' + key)
            out[key] = normalized
    if out.get('start') and out.get('end') and out['start'] >= out['end']:
        raise ValueError('开始时间必须早于结束时间')
    return out


def _matches(turn, session, filters):
    for key in ('source', 'project'):
        if key in filters and filters[key] != session.get(key):
            return False
    if 'model' in filters and filters['model'] != turn.get('model'):
        return False
    stamp = turn.get('started_at')
    if filters.get('start') and (not stamp or stamp < filters['start']):
        return False
    if filters.get('end') and (not stamp or stamp >= filters['end']):
        return False
    return True


def _parent_turn_fields(turn, session, sessions_by_id):
    """Resolve only a root ID verified inside this turn's direct parent session."""
    parent_id = session.get('parent_id')
    if not isinstance(parent_id, str) or not parent_id:
        parent_id = None
    result = {'parent_session_id': parent_id, 'parent_turn_id': None,
              'parent_turn_attribution': 'unknown'}
    parent = sessions_by_id.get(parent_id)
    root = turn.get('root_turn_id')
    if (not parent or parent_id == session.get('id') or
            not isinstance(root, str) or not root):
        return result
    matches = [t for t in parent['turns'] if t.get('native_id') == root and isinstance(t.get('id'), str)]
    if len(matches) == 1:
        result['parent_turn_id'] = matches[0]['id']
        result['parent_turn_attribution'] = 'root_turn_id'
    return result


class Store:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        os.chmod(str(self.path), 0o600)
        # DELETE journal mode avoids persistent metadata in broadly readable WAL files.
        self.db.execute('PRAGMA journal_mode=DELETE')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise ValueError('不支持的数据库版本')
        self.db.execute('CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, mtime REAL NOT NULL, body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, body TEXT NOT NULL)')
        self.db.execute('PRAGMA user_version=%d' % SCHEMA_VERSION)
        self.db.commit()

    def import_paths(self, paths, instance='local'):
        if not isinstance(instance, str) or not instance:
            raise ValueError('instance 不能为空')
        files, errors, warnings = set(), [], []
        for raw in paths:
            p = Path(raw).expanduser()
            if p.is_file():
                files.add(p)
            elif p.is_dir():
                files.update(p.rglob('*.jsonl'))
            else:
                errors.append('来源不存在: ' + str(p))
        imported = skipped = 0
        titles_by_home, projects_by_home = {}, {}
        for path in files:
            home = metadata_home(path)
            if home not in titles_by_home:
                titles_by_home[home], problems = read_session_titles(home)
                projects_by_home[home] = read_project_metadata(home)
                warnings.extend(problems)
        # Serialize revision checks with writes across independent importer processes.
        self.db.execute('BEGIN IMMEDIATE')
        for path in sorted(files):
            try:
                data, problems = parse_rollout(path, instance)
                warnings.extend(str(path) + ': ' + w for w in problems)
                if not data:
                    skipped += 1
                    continue
                # JSON tuple is collision-free for arbitrary instance/session strings.
                native_id = data['id']
                data['title'] = titles_by_home[metadata_home(path)].get(native_id)
                data['project_name'] = project_name(native_id, data.get('project'), projects_by_home[metadata_home(path)])
                key = json.dumps(['codex', instance, native_id], ensure_ascii=False, separators=(',', ':'))
                data['native_id'], data['id'] = native_id, key
                data['parent_id'] = json.dumps(['codex', instance, data['parent_id']], ensure_ascii=False, separators=(',', ':')) if data.get('parent_id') else None
                data['forked_from_id'] = json.dumps(['codex', instance, data['forked_from_id']], ensure_ascii=False, separators=(',', ':')) if data.get('forked_from_id') else None
                data['source_path'] = str(path)
                data['received_at'] = datetime.now(timezone.utc).isoformat()
                for turn in data['turns']:
                    turn['native_id'], turn['id'] = turn['id'], json.dumps([key, turn['id']], ensure_ascii=False, separators=(',', ':'))
                old = self.db.execute('SELECT body FROM sessions WHERE key=?', (key,)).fetchone()
                if old:
                    old_data = json.loads(old[0])
                    # Source event revision is authoritative; touching an old copy is not a new revision.
                    rank = lambda s: (s.get('revision_at', ''), s.get('event_count', 0))
                    if rank(data) < rank(old_data):
                        # An older copy must not replace newer usage, but project
                        # renames from the same Codex home remain authoritative.
                        old_source = old_data.get('source_path')
                        if old_source and metadata_home(old_source) == metadata_home(path):
                            name = project_name(native_id, old_data.get('project'), projects_by_home[metadata_home(path)])
                            if name != old_data.get('project_name'):
                                old_data['project_name'] = name
                                self.db.execute('UPDATE sessions SET body=? WHERE key=?',
                                                (json.dumps(old_data, ensure_ascii=False), key))
                                imported += 1
                                continue
                        skipped += 1
                        continue
                    if (rank(data) == rank(old_data) and data['turns'] == old_data.get('turns')
                            and data['warnings'] == old_data.get('warnings', [])
                            and data['title'] == old_data.get('title')
                            and data['project_name'] == old_data.get('project_name')
                            and data.get('usage_accounting') == old_data.get('usage_accounting')
                            and data.get('forked_from_id') == old_data.get('forked_from_id')):
                        skipped += 1
                        continue
                self.db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?,?)', (key, path.stat().st_mtime, json.dumps(data, ensure_ascii=False)))
                imported += 1
            except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
                errors.append(str(path) + ': ' + type(exc).__name__)
        report = {'scanned': len(files), 'imported': imported, 'skipped': skipped, 'errors': errors,
                  'warnings': warnings, 'received_at': datetime.now(timezone.utc).isoformat()}
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('last_import', json.dumps(report, ensure_ascii=False)))
        self.db.commit()
        return report

    def _all(self):
        sessions, warnings = [], []
        for key, body in self.db.execute('SELECT key,body FROM sessions ORDER BY key'):
            try:
                data = json.loads(body)
                if (not isinstance(data, dict) or not isinstance(data.get('id'), str)
                        or not isinstance(data.get('turns'), list)
                        or any(not isinstance(t, dict) or not isinstance(t.get('tools'), list) or 'token_total' not in t for t in data['turns'])):
                    raise ValueError()
                sessions.append(data)
            except (ValueError, TypeError):
                warnings.append('数据库中有损坏的规范化会话，已跳过')
        return sessions, warnings

    def query(self, filters=None):
        f = _filters(filters)
        sessions, warnings = self._all()
        sessions_by_id = {s['id']: s for s in sessions}
        turns, visible_sessions, visible_accounting = [], [], {}
        legacy_accounting = False
        for s in sessions:
            subset = [estimate_turn(dict(t, sequence=t.get('sequence') or sequence, session_id=s['id'], session_title=s.get('title'), session_native_id=s.get('native_id'), project=s.get('project'), project_name=s.get('project_name'), source=s['source'], **_parent_turn_fields(t, s, sessions_by_id))) for sequence, t in enumerate(s['turns'], 1) if _matches(t, s, f)]
            # Empty sessions remain visible when no turn-level filters are applied.
            session_match = all(not f.get(k) or f[k] == s.get(k) for k in ('source', 'project'))
            if not subset and (any(f.get(k) for k in ('start', 'end', 'model')) or not session_match):
                continue
            turns.extend(subset)
            visible_accounting[s['id']] = s.get('usage_accounting')
            # Old imports did not retain fork identities. They cannot be safely
            # combined until reparsed, even if no parent relation was stored.
            legacy_accounting |= bool(s.get('source_path') and 'usage_accounting' not in s)
            total, known, samples = _sum_known(subset)
            models = sorted(set(t.get('model') or '未知' for t in subset))
            visible_sessions.append({**{k: s.get(k) for k in ('id', 'native_id', 'title', 'project', 'project_name', 'started_at', 'status', 'parent_id', 'source', 'instance')},
                                     'model': models[0] if len(models) == 1 else ('混合模型' if models else '未知'),
                                     'token_total': total, 'token_known': known, 'token_samples': samples,
                                     **aggregate_cost(subset),
                                     'cache_usage': aggregate_cache(subset),
                                     'turns': len(subset), 'tools': sum(len(t['tools']) for t in subset)})
            prefix = s.get('source_path', '')
            warnings.extend((prefix + ': ' if prefix else '') + w for w in s.get('warnings', []))
        ids = {s['id'] for s in visible_sessions}
        blocked = legacy_accounting or any(s.get('parent_id') in ids and
                      (visible_accounting[s['id']] != 'per_session' or
                       visible_accounting[s['parent_id']] != 'per_session')
                      for s in visible_sessions)
        if blocked:
            warnings.append('部分会话计量口径尚未核实（或旧采集记录），请刷新采集；仍无法核实时保留观测小计。')
        total, known, samples = _sum_known(turns)
        valid = [t['token_total'] for t in turns if t.get('token_total') is not None]
        groups, trend = {}, {}
        for t in turns:
            groups.setdefault(t.get('model') or '未知', []).append(t)
            trend.setdefault((t.get('started_at') or '未知')[:10], []).append(t)
        def aggregate(label, rows, key):
            complete, subtotal, n = _sum_known(rows)
            return {key: label, 'turns': len(rows), 'token_total': None if blocked else complete,
                    'token_known': None if blocked else subtotal, 'token_samples': n,
                    **aggregate_cost(rows, blocked),
                    'cache_usage': aggregate_cache(rows, blocked),
                    'observed_token_total': complete, 'observed_token_known': subtotal}
        row = self.db.execute('SELECT body FROM metadata WHERE key=?', ('last_import',)).fetchone()
        report = json.loads(row[0]) if row else {}
        warnings.extend(report.get('errors', []))
        warnings.extend(report.get('warnings', []))
        return {'summary': {'sessions': len(visible_sessions), 'turns': len(turns),
                            'token_total': None if blocked else total, 'token_known': None if blocked else known,
                            'observed_token_total': total, 'observed_token_known': known,
                            'token_samples': samples, 'median': percentile(valid, .5), 'p90': percentile(valid, .9),
                            'tool_calls': sum(len(t['tools']) for t in turns) if turns else None, 'cost': None,
                            **aggregate_cost(turns, blocked),
                            'cache_usage': aggregate_cache(turns, blocked),
                            'aggregation_blocked': blocked},
                'sessions': sorted(visible_sessions, key=lambda s: s.get('started_at') or '', reverse=True),
                'turns': sorted(turns, key=lambda t: t.get('started_at') or ''),
                'groups': [aggregate(k, v, 'model') for k, v in sorted(groups.items())],
                'trend': [aggregate(k, v, 'date') for k, v in sorted(trend.items())],
                'pricing': PRICING,
                'diagnostics': [], 'coverage': {'token_turns': samples, 'turns': len(turns), 'cost': 0,
                    'estimated_cost_turns': sum(t['estimated_cost'] is not None for t in turns),
                    'percentile_method': 'Hyndman-Fan type 7; linear interpolation at (n-1)*p',
                    'time_basis': 'turn.started_at; UTC; [start,end)'},
                'warnings': list(dict.fromkeys(warnings)), 'filters': f, 'last_import': report}

    def detail(self, session_id, filters=None):
        sessions, warnings = self._all()
        s = next((s for s in sessions if s['id'] == session_id), None)
        if not s:
            return None
        f = _filters(filters)
        sessions_by_id = {row['id']: row for row in sessions}
        turns = [estimate_turn(dict(t, sequence=t.get('sequence') or sequence, **_parent_turn_fields(t, s, sessions_by_id))) for sequence, t in enumerate(s['turns'], 1) if _matches(t, s, f)]
        return {'session': {**{k: v for k, v in s.items() if k != 'turns'}, **aggregate_cost(turns),
                            'cache_usage': aggregate_cache(turns)},
                'turns': turns, 'pricing': PRICING,
                'warnings': warnings + s.get('warnings', []), 'filters': f}
