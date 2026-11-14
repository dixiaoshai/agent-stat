"""Version-aware Codex rollout adapter with bounded prompt display titles.

Never persist the input envelope. Accumulated snapshots are not requests.
"""
import json
import re
from datetime import datetime, timezone

FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens')
# Verified against local parent/child rollout snapshots, not a public format contract.
PER_SESSION_VERSIONS = {'0.166.0-alpha.5.1', '0.166.0-alpha.6'}


def prompt_title(value):
    """Keep only a short display label, excluding injected runtime context."""
    if not isinstance(value, str):
        return None
    value = re.sub(r'# AGENTS\.md instructions\s*<INSTRUCTIONS>.*?</INSTRUCTIONS>', '', value, flags=re.S)
    value = re.sub(
        r'<(environment_context|external_codex_apps_open_page|turn_aborted|INSTRUCTIONS)>.*?</\1>',
        '', value, flags=re.S,
    )
    value = ' '.join(value.split())
    return (value if len(value) <= 80 else value[:79] + '…') or None


def message_title(kind, payload):
    if kind == 'event_msg' and payload.get('type') == 'user_message':
        return prompt_title(payload.get('message'))
    if kind != 'response_item':
        return None
    subtype = payload.get('type')
    if not ((subtype == 'message' and payload.get('role') == 'user') or subtype == 'agent_message'):
        return None
    content = payload.get('content')
    if not isinstance(content, list):
        return None
    parts = [part['text'] for part in content if isinstance(part, dict)
             and part.get('type') in ('input_text', 'text') and isinstance(part.get('text'), str)]
    value = '\n'.join(parts)
    if subtype == 'agent_message':
        if not value.startswith('Message Type: NEW_TASK\n') or '\nPayload:\n' not in value:
            return None
        value = value.split('\nPayload:\n', 1)[1]
    return prompt_title(value)


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


def usage_total(value):
    result = usage(value)
    if 'total_tokens' not in result and 'input_tokens' in result and 'output_tokens' in result:
        result['total_tokens'] = result['input_tokens'] + result['output_tokens']
    return result


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
                    meta = {k: p.get(k) for k in ('id', 'session_id', 'cwd', 'timestamp', 'parent_id', 'forked_from_id', 'source', 'cli_version')}
                    meta['recorded_at'] = timestamp(row.get('timestamp'))
                # Select a bounded title immediately; never retain full input or tool bodies.
                clean = {k: p.get(k) for k in ('type', 'turn_id', 'root_turn_id', 'model', 'call_id', 'name')}
                clean['prompt_title'] = message_title(row.get('type'), p)
                clean['assistant_message'] = row.get('type') == 'response_item' and p.get('role') == 'assistant'
                if row.get('type') == 'event_msg' and p.get('type') == 'token_count':
                    info = p.get('info') if isinstance(p.get('info'), dict) else {}
                    clean['cumulative'] = usage_total(info.get('total_token_usage'))
                    clean['last_usage'] = usage_total(info.get('last_token_usage'))
                    # Codex also emits token_count for rate-limit updates without usage.
                    # Retain only this structural flag, never account limit details.
                    clean['rate_limit_only'] = ('info' in p and p['info'] is None
                                                and isinstance(p.get('rate_limits'), dict)
                                                and bool(p['rate_limits']))
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
    return summarize_events(events, meta, warnings, instance)


def summarize_events(events, meta, warnings=None, instance='local', runtime='codex'):
    """Aggregate sanitized observations without reading or retaining source bodies."""
    warnings = list(warnings or [])
    sid = meta.get('id') or meta.get('session_id')
    if not isinstance(sid, str) or not sid:
        return None, warnings + ['缺少稳定 session_meta 身份']
    source = meta.get('source')
    forked_from = meta.get('forked_from_id')
    forked_from = forked_from if isinstance(forked_from, str) and forked_from else None
    fork_started = timestamp(meta.get('timestamp')) or meta.get('recorded_at')
    fork_time_uncertain = bool(forked_from and (not fork_started or any(
        not stamp and kind != 'session_meta' for stamp, _, kind, _ in events)))
    if forked_from and not fork_started:
        warnings.append('分叉会话缺少有效创建时间，无法区分继承历史')
    elif fork_time_uncertain:
        warnings.append('分叉会话含无效事件时间，无法区分继承历史')
    parent = meta.get('parent_id')
    if isinstance(source, dict):
        agent = source.get('subagent')
        if isinstance(agent, dict):
            spawn = agent.get('thread_spawn')
            if isinstance(spawn, dict):
                parent = spawn.get('parent_thread_id') or parent
    turns, tools, high = {}, {}, {}
    # Root identities are observed per native turn, independently of whether
    # the events themselves create or update a normalized turn.
    roots_by_turn = {}
    active = None
    pending_title = None
    explicit_titles = {}
    usage_seen = False
    counter_reset = False
    accounting_evidence = 0
    accounting_missing = False
    accounting_conflict = False
    gaps = {key: set() for key in FIELDS}

    def turn(tid, stamp, line, inferred=False):
        if not isinstance(tid, str) or not tid:
            tid = 'unassigned'
        if tid not in turns:
            turns[tid] = {'id': tid, 'started_at': stamp, 'ended_at': None, 'status': 'unknown', 'model': None,
                          'title': explicit_titles.pop(tid, None),
                          'token_total': None, 'input_tokens': None, 'cached_input_tokens': None,
                          'output_tokens': None, 'reasoning_output_tokens': None, 'cache_write_input_tokens': None,
                          'recorded_cost_usd': None, 'recorded_cost_source': None,
                          'tools': [], 'evidence': [], 'attribution': 'unassigned' if tid == 'unassigned' else ('inferred' if inferred else 'native'),
                          '_seen': set(), '_missing': set(), '_unrecoverable': set(), '_cost_sources': set()}
        t = turns[tid]
        if stamp and (not t['started_at'] or stamp < t['started_at']):
            t['started_at'] = stamp
        if len(t['evidence']) < 100:
            t['evidence'].append({'line': line, 'timestamp': stamp})
        return t

    def apply_title(t):
        nonlocal pending_title
        if pending_title and not t['title']:
            t['title'] = pending_title
        pending_title = None

    # Event time controls revisions; file position only breaks equal-time ties.
    events.sort(key=lambda e: (e[0] or '', e[1]))
    for stamp, line, kind, p in events:
        # Forks may carry copied history. Only newly recorded work belongs here.
        if forked_from and (not stamp or (fork_started and stamp < fork_started)):
            continue
        native_turn, root_turn = p.get('turn_id'), p.get('root_turn_id')
        if (isinstance(native_turn, str) and native_turn and
                isinstance(root_turn, str) and root_turn):
            roots_by_turn.setdefault(native_turn, set()).add(root_turn)
        explicit = p.get('turn_id') or p.get('root_turn_id')
        if p.get('prompt_title'):
            # Wait for context/lifecycle before attributing a user message. Older
            # rollouts can place it before the next turn_context.
            if explicit:
                if explicit in turns:
                    if not turns[explicit]['title']:
                        turns[explicit]['title'] = p['prompt_title']
                else:
                    explicit_titles.setdefault(explicit, p['prompt_title'])
            else:
                pending_title = pending_title or p['prompt_title']
            continue
        is_work = (kind == 'response_item' and (p.get('assistant_message') or p.get('type') in (
            'reasoning', 'function_call', 'custom_tool_call', 'function_call_output', 'custom_tool_call_output')))
        if pending_title and (is_work or (kind == 'event_msg' and p.get('type') in (
                'task_complete', 'task_completed', 'turn_aborted'))):
            target = explicit or active
            if target in turns:
                apply_title(turns[target])
        if kind == 'turn_context':
            active = explicit
            t = turn(active, stamp, line, inferred=not bool(explicit))
            apply_title(t)
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
                t = turn(active, stamp, line, inferred=not bool(explicit))
                t['status'] = 'in_progress'
                apply_title(t)
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
                    if not p.get('rate_limit_only'):
                        warnings.append('用量事件缺少有效累计快照: 行 %d' % line)
                    continue
                if any(k in high and v < high[k] for k, v in current.items()):
                    warnings.append('累计快照回退或重置，未猜测增量: 行 %d' % line)
                    target = explicit or active
                    turn(target, stamp, line)['_unrecoverable'].update(FIELDS)
                    counter_reset = True
                    accounting_conflict = True
                    continue
                delta = {k: v - high.get(k, 0) for k, v in current.items()}
                # A replay contains no new usage, but a first explicit zero remains known zero.
                duplicate = usage_seen and all(k in high and high[k] == v for k, v in current.items())
                if duplicate:
                    continue
                last = p.get('last_usage') or {}
                unrecoverable = set()
                if forked_from:
                    # The first cumulative snapshot includes the source thread's
                    # history. Its last usage is the first new call, not that history.
                    # A category first appearing later also has no known baseline.
                    for key in current:
                        if key in high:
                            continue
                        if not usage_seen and key in last and last[key] <= current[key]:
                            delta[key] = last[key]
                        else:
                            delta.pop(key, None)
                            unrecoverable.add(key)
                            warnings.append('分叉会话缺少有效用量基线，无法扣除继承历史: ' + key)
                if 'total_tokens' not in last:
                    accounting_missing = True
                elif usage_seen or forked_from or current == last:
                    if all(value == last[key] for key, value in delta.items() if key in last):
                        accounting_evidence += 1
                    else:
                        accounting_conflict = True
                else:
                    # An unverified format may inherit an unmarked baseline;
                    # later matching calls do not explain that initial excess.
                    accounting_missing = True
                high.update(current)
                usage_seen = True
                target = explicit or active
                t = turn(target, stamp, line, inferred=not bool(explicit))
                t['_unrecoverable'].update(unrecoverable)
                if counter_reset:
                    # Crossing the old high-water mark does not restore a
                    # trustworthy epoch after a counter reset.
                    t['_unrecoverable'].update(FIELDS)
                # Pi has no turn_context record; assistant usage carries the
                # physical model instead.
                turn_model = p.get('model')
                if isinstance(turn_model, str) and turn_model:
                    if t['model'] and t['model'] != turn_model:
                        t['model'] = '混合模型'
                        warnings.append('同一轮次出现多个模型，未拆分模型用量')
                    else:
                        t['model'] = turn_model
                # Recorded cost belongs to this request (last_usage), not to the
                # cumulative snapshot. It survives replay detection above.
                request_cost = p.get('cost_usd')
                if request_cost is not None:
                    t['recorded_cost_usd'] = (t['recorded_cost_usd'] or 0) + request_cost
                    cost_source = p.get('cost_source')
                    if isinstance(cost_source, str) and cost_source:
                        t['_cost_sources'].add(cost_source)
                apply_title(t)
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
                        if key in unrecoverable:
                            # This snapshot establishes high for future deltas,
                            # but cannot recover this turn's inherited baseline.
                            gaps[key].clear()
                        else:
                            gaps[key].add(t['id'])
        elif kind == 'response_item':
            subtype, call = p.get('type'), p.get('call_id')
            if not isinstance(call, str) or subtype not in ('function_call', 'custom_tool_call', 'function_call_output', 'custom_tool_call_output'):
                continue
            is_call = subtype in ('function_call', 'custom_tool_call')
            if call not in tools:
                target = explicit or active
                t = turn(target, stamp, line, inferred=not bool(explicit))
                apply_title(t)
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
    for sequence, t in enumerate(result_turns, 1):
        t['sequence'] = sequence
        roots = roots_by_turn.get(t['id'], set())
        # Conflicting root identities do not establish a reliable parent.
        t['root_turn_id'] = next(iter(roots)) if len(roots) == 1 else None
        if fork_time_uncertain:
            t['_unrecoverable'].update(FIELDS)
        # Preserve positive write evidence even when incomplete later snapshots
        # make the total unknown. Pricing cannot then assume no paid writes.
        t['cache_write_observed'] = (t.get('cache_write_input_tokens') or 0) > 0
        cost_sources = t.pop('_cost_sources')
        # A turn can mix provider-reported and locally computed costs; do not
        # present a single basis when that happened.
        t['recorded_cost_source'] = (next(iter(cost_sources)) if len(cost_sources) == 1
                                     else ('mixed' if cost_sources else None))
        for key in t.pop('_missing') | t.pop('_unrecoverable'):
            t['token_total' if key == 'total_tokens' else key] = None
        t.pop('_seen')
        t['model'] = t['model'] or '未知'
        t['tools'].sort(key=lambda x: x['line'])
    if warnings:
        warnings = list(dict.fromkeys(warnings))
    if runtime == 'pi':
        # Pi usage is recorded per request; the adapter only synthesizes a
        # cumulative snapshot for consumers that need deltas.
        usage_accounting = 'per_session' if not fork_time_uncertain else 'unknown'
    else:
        usage_accounting = ('per_session' if not accounting_conflict and not fork_time_uncertain and
                            (meta.get('cli_version') in PER_SESSION_VERSIONS or
                             (accounting_evidence and not accounting_missing)) else 'unknown')
    return {'id': sid, 'source': runtime, 'instance': instance, 'project': meta.get('cwd'),
            'started_at': timestamp(meta.get('timestamp')) or next((e[0] for e in events if e[0]), None),
            'status': 'unknown', 'parent_id': parent if isinstance(parent, str) else None,
            'forked_from_id': forked_from,
            'usage_accounting': usage_accounting,
            'cli_version': meta.get('cli_version'), 'turns': result_turns,
            'warnings': warnings, 'revision_at': max((e[0] for e in events if e[0]), default=''),
            'event_count': len(events)}, warnings
