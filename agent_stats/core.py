"""Source-independent normalized facts, persistence, and aggregation."""
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .codex import parse_rollout, timestamp

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
                key = json.dumps(['codex', instance, native_id], ensure_ascii=False, separators=(',', ':'))
                data['native_id'], data['id'] = native_id, key
                data['parent_id'] = json.dumps(['codex', instance, data['parent_id']], ensure_ascii=False, separators=(',', ':')) if data.get('parent_id') else None
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
                        skipped += 1
                        continue
                    if rank(data) == rank(old_data) and data['turns'] == old_data.get('turns'):
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
        turns, visible_sessions = [], []
        for s in sessions:
            subset = [dict(t, session_id=s['id'], project=s.get('project'), source=s['source']) for t in s['turns'] if _matches(t, s, f)]
            # Empty sessions remain visible when no turn-level filters are applied.
            session_match = all(not f.get(k) or f[k] == s.get(k) for k in ('source', 'project'))
            if not subset and (any(f.get(k) for k in ('start', 'end', 'model')) or not session_match):
                continue
            turns.extend(subset)
            total, known, samples = _sum_known(subset)
            models = sorted(set(t.get('model') or '未知' for t in subset))
            visible_sessions.append({**{k: s.get(k) for k in ('id', 'project', 'started_at', 'status', 'parent_id', 'source', 'instance')},
                                     'model': models[0] if len(models) == 1 else ('混合模型' if models else '未知'),
                                     'token_total': total, 'token_known': known, 'token_samples': samples,
                                     'turns': len(subset), 'tools': sum(len(t['tools']) for t in subset)})
            warnings.extend(s.get('warnings', []))
        ids = {s['id'] for s in visible_sessions}
        blocked = any(s.get('parent_id') in ids for s in visible_sessions)
        if blocked:
            warnings.append('父子用量包含关系未知：只展示各会话观测值，联合 Token 总量与已知小计不可计算。')
        total, known, samples = _sum_known(turns)
        valid = [t['token_total'] for t in turns if t.get('token_total') is not None]
        groups, trend = {}, {}
        for t in turns:
            groups.setdefault(t.get('model') or '未知', []).append(t)
            trend.setdefault((t.get('started_at') or '未知')[:10], []).append(t)
        def aggregate(label, rows, key):
            complete, subtotal, n = _sum_known(rows)
            return {key: label, 'turns': len(rows), 'token_total': None if blocked else complete,
                    'token_known': None if blocked else subtotal, 'token_samples': n}
        row = self.db.execute('SELECT body FROM metadata WHERE key=?', ('last_import',)).fetchone()
        report = json.loads(row[0]) if row else {}
        warnings.extend(report.get('errors', []))
        warnings.extend(report.get('warnings', []))
        return {'summary': {'sessions': len(visible_sessions), 'turns': len(turns),
                            'token_total': None if blocked else total, 'token_known': None if blocked else known,
                            'token_samples': samples, 'median': percentile(valid, .5), 'p90': percentile(valid, .9),
                            'tool_calls': sum(len(t['tools']) for t in turns) if turns else None, 'cost': None,
                            'aggregation_blocked': blocked},
                'sessions': sorted(visible_sessions, key=lambda s: s.get('started_at') or '', reverse=True),
                'turns': sorted(turns, key=lambda t: t.get('started_at') or ''),
                'groups': [aggregate(k, v, 'model') for k, v in sorted(groups.items())],
                'trend': [aggregate(k, v, 'date') for k, v in sorted(trend.items())],
                'diagnostics': [], 'coverage': {'token_turns': samples, 'turns': len(turns), 'cost': 0,
                    'percentile_method': 'Hyndman-Fan type 7; linear interpolation at (n-1)*p',
                    'time_basis': 'turn.started_at; UTC; [start,end)'},
                'warnings': list(dict.fromkeys(warnings)), 'filters': f, 'last_import': report}

    def detail(self, session_id, filters=None):
        sessions, warnings = self._all()
        s = next((s for s in sessions if s['id'] == session_id), None)
        if not s:
            return None
        f = _filters(filters)
        return {'session': {k: v for k, v in s.items() if k != 'turns'},
                'turns': [t for t in s['turns'] if _matches(t, s, f)],
                'warnings': warnings + s.get('warnings', []), 'filters': f}
