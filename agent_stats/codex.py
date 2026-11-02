"""Version-aware, metadata-only Codex rollout adapter.

Never persist the input envelope. Accumulated snapshots are not requests.
"""
import json
from datetime import datetime, timezone

FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens')


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    except ValueError:
        return None


def usage(value):
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if k in FIELDS and type(v) is int and v >= 0}


def parse_rollout(path, instance='local'):
    warnings, events, meta = [], [], {}
    with open(path, encoding='utf-8', errors='strict') as stream:
        for number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or not isinstance(row.get('payload'), dict):
                    raise ValueError()
                p = row['payload']
                if row.get('type') == 'session_meta':
                    meta = {k: p.get(k) for k in ('id', 'session_id', 'cwd', 'timestamp', 'parent_id', 'source', 'cli_version')}
                # Select metadata immediately: never retain prompt/args/output bodies.
                clean = {k: p.get(k) for k in ('type', 'turn_id', 'root_turn_id', 'model', 'call_id', 'name')}
                if row.get('type') == 'event_msg' and p.get('type') == 'token_count':
                    info = p.get('info') if isinstance(p.get('info'), dict) else {}
                    clean['cumulative'] = usage(info.get('total_token_usage'))
                if row.get('type') == 'response_item':
                    for source, target in (('arguments', 'input_bytes'), ('input', 'input_bytes'), ('output', 'output_bytes')):
                        value = p.get(source)
                        if isinstance(value, (str, list, dict)):
                            clean[target] = len((value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)).encode('utf-8'))
                stamp = timestamp(row.get('timestamp'))
                if stamp is None:
                    warnings.append('缺少带时区事件时间: 行 %d' % number)
                events.append((stamp, number, row.get('type'), clean))
            except (ValueError, TypeError, UnicodeError):
                warnings.append('损坏或无效的 JSONL 行: %d' % number)
    sid = meta.get('id') or meta.get('session_id')
    if not isinstance(sid, str) or not sid:
        return None, warnings + ['缺少稳定 session_meta 身份']
    source = meta.get('source')
    parent = meta.get('parent_id')
    if isinstance(source, dict):
        agent = source.get('subagent')
        if isinstance(agent, dict):
            spawn = agent.get('thread_spawn')
            if isinstance(spawn, dict):
                parent = spawn.get('parent_thread_id') or parent
    turns, tools, high = {}, {}, {}
    active = None
    usage_seen = False
    gaps = {key: set() for key in FIELDS}

    def turn(tid, stamp, line, inferred=False):
        if not isinstance(tid, str) or not tid:
            tid = 'unassigned'
        if tid not in turns:
            turns[tid] = {'id': tid, 'started_at': stamp, 'ended_at': None, 'status': 'unknown', 'model': None,
                          'token_total': None, 'input_tokens': None, 'cached_input_tokens': None,
                          'output_tokens': None, 'reasoning_output_tokens': None, 'cache_write_input_tokens': None,
                          'tools': [], 'evidence': [], 'attribution': 'unassigned' if tid == 'unassigned' else ('inferred' if inferred else 'native'),
                          '_seen': set(), '_missing': set()}
        t = turns[tid]
        if stamp and (not t['started_at'] or stamp < t['started_at']):
            t['started_at'] = stamp
        if len(t['evidence']) < 100:
            t['evidence'].append({'line': line, 'timestamp': stamp})
        return t

    # Event time controls revisions; file position only breaks equal-time ties.
    events.sort(key=lambda e: (e[0] or '', e[1]))
    for stamp, line, kind, p in events:
        explicit = p.get('turn_id') or p.get('root_turn_id')
        if kind == 'turn_context':
            active = explicit
            t = turn(active, stamp, line, inferred=not bool(explicit))
            if isinstance(p.get('model'), str):
                if t['model'] and t['model'] != p['model']:
                    t['model'] = '混合模型'
                    warnings.append('同一轮次出现多个模型，未拆分模型用量')
                else:
                    t['model'] = p['model']
        elif kind == 'event_msg':
            subtype = p.get('type')
            if subtype == 'task_started':
                active = explicit
                turn(active, stamp, line, inferred=not bool(explicit))['status'] = 'in_progress'
            elif subtype in ('task_complete', 'task_completed', 'turn_aborted'):
                target = explicit or active
                if target:
                    t = turn(target, stamp, line)
                    t['ended_at'] = stamp
                    t['status'] = 'interrupted' if subtype == 'turn_aborted' else 'completed'
                if target == active:
                    active = None
            elif subtype == 'token_count':
                current = p.get('cumulative') or {}
                if not current:
                    warnings.append('用量事件缺少有效累计快照: 行 %d' % line)
                    continue
                if 'total_tokens' not in current and 'input_tokens' in current and 'output_tokens' in current:
                    current['total_tokens'] = current['input_tokens'] + current['output_tokens']
                if any(k in high and v < high[k] for k, v in current.items()):
                    warnings.append('累计快照回退或重置，未猜测增量: 行 %d' % line)
                    target = explicit or active
                    turn(target, stamp, line)['_missing'].update(FIELDS)
                    continue
                delta = {k: v - high.get(k, 0) for k, v in current.items()}
                # A replay contains no new usage, but a first explicit zero remains known zero.
                duplicate = usage_seen and all(k in high and high[k] == v for k, v in current.items())
                high.update(current)
                usage_seen = True
                if duplicate:
                    continue
                target = explicit or active
                t = turn(target, stamp, line, inferred=not bool(explicit))
                if not explicit and target:
                    t['attribution'] = 'inferred usage via turn_context'
                if not target:
                    warnings.append('用量无法可靠归属，单列未归属记录: 行 %d' % line)
                for key in FIELDS:
                    dest = 'token_total' if key == 'total_tokens' else key
                    if key in delta:
                        t[dest] = (t[dest] or 0) + delta[key]
                        t['_seen'].add(key)
                        if gaps[key] and gaps[key] <= {t['id']}:
                            t['_missing'].discard(key)
                        elif gaps[key]:
                            t['_missing'].add(key)
                            warnings.append('分类快照跨缺失轮次，无法完整拆分: ' + key)
                        gaps[key].clear()
                    else:
                        t['_missing'].add(key)
                        gaps[key].add(t['id'])
        elif kind == 'response_item':
            subtype, call = p.get('type'), p.get('call_id')
            if not isinstance(call, str) or subtype not in ('function_call', 'custom_tool_call', 'function_call_output', 'custom_tool_call_output'):
                continue
            is_call = subtype in ('function_call', 'custom_tool_call')
            if call not in tools:
                target = explicit or active
                t = turn(target, stamp, line, inferred=not bool(explicit))
                tools[call] = {'call_id': call, 'name': '未知工具', 'input_bytes': None, 'output_bytes': None,
                               'status': 'unknown', 'line': line, 'started_at': stamp if is_call else None,
                               'ended_at': None, 'attribution': 'explicit' if explicit else ('inferred turn_context' if active else 'unassigned')}
                t['tools'].append(tools[call])
            tool = tools[call]
            if is_call:
                if isinstance(p.get('name'), str):
                    tool['name'] = p['name']
                if p.get('input_bytes') is not None:
                    tool['input_bytes'] = p['input_bytes']
            else:
                tool['output_bytes'] = p.get('output_bytes')
                tool['ended_at'] = stamp
                tool['status'] = 'result_observed'  # result is not proof of success
    result_turns = list(turns.values())
    for t in result_turns:
        for key in t.pop('_missing'):
            t['token_total' if key == 'total_tokens' else key] = None
        t.pop('_seen')
        t['model'] = t['model'] or '未知'
        t['tools'].sort(key=lambda x: x['line'])
    if warnings:
        warnings = list(dict.fromkeys(warnings))
    return {'id': sid, 'source': 'codex', 'instance': instance, 'project': meta.get('cwd'),
            'started_at': timestamp(meta.get('timestamp')) or next((e[0] for e in events if e[0]), None),
            'status': 'unknown', 'parent_id': parent if isinstance(parent, str) else None,
            'cli_version': meta.get('cli_version'), 'turns': result_turns,
            'warnings': warnings, 'revision_at': max((e[0] for e in events if e[0]), default=''),
            'event_count': len(events)}, warnings
