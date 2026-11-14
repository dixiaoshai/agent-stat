import { useEffect, useMemo, useState } from 'react';
import type { Overview, Selection, Session, Turn } from '../types';
import { buildTurnTree, type TurnNode } from '../lib/turnTree';
import { workspaceColor } from '../lib/workspaceColor';
import {
  cacheRate,
  formatCost,
  formatDate,
  formatNumber,
  formatPercent,
  projectName,
  sessionName,
  statusLabel,
  turnName,
} from '../lib/format';

type Unit = 'turns' | 'sessions';
type Sort = 'time' | 'tokens' | 'turns' | 'cost' | 'cache';

type TurnEntry = { node: TurnNode<Turn>; depth: number };
type SessionGroup = { session: Session; roots: TurnNode<Turn>[] };
type SessionEntry = { kind: 'session'; group: SessionGroup } | ({ kind: 'turn' } & TurnEntry);

function flattenTurns(tree: TurnNode<Turn>[], expanded?: Set<string>, depth = 0): TurnEntry[] {
  const entries: TurnEntry[] = [];
  const pending = tree.map((node) => ({ node, depth })).reverse();
  while (pending.length) {
    const entry = pending.pop()!;
    entries.push(entry);
    if (!expanded || expanded.has(entry.node.turn.id)) {
      for (let i = entry.node.children.length - 1; i >= 0; i -= 1) {
        pending.push({ node: entry.node.children[i], depth: entry.depth + 1 });
      }
    }
  }
  return entries;
}

export function Sessions({ data, onSelect }: { data: Overview; onSelect: (s: Selection) => void }) {
  const [unit, setUnit] = useState<Unit>('sessions');
  const [sort, setSort] = useState<Sort>('time');
  const [search, setSearch] = useState('');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const rows = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    if (unit === 'sessions') {
      const promptMatches = new Set(
        data.turns
          .filter((turn) => query && turn.title?.toLocaleLowerCase().includes(query))
          .map((turn) => turn.session_id),
      );
      return data.sessions
        .filter(
          (s) =>
            !query ||
            promptMatches.has(s.id) ||
            `${s.title ?? ''} ${s.native_id ?? ''} ${s.project_name ?? ''} ${s.project} ${s.model} ${s.id}`
              .toLocaleLowerCase()
              .includes(query),
        )
        .slice()
        .sort((a, b) => compare(a, b, sort));
    }
    return data.turns
      .filter(
        (t) =>
          !query ||
          `${t.title ?? ''} ${t.session_title ?? ''} ${t.project_name ?? ''} ${t.project} ${t.model} ${t.native_id} ${t.session_id}`
            .toLocaleLowerCase()
            .includes(query),
      )
      .slice()
      .sort((a, b) => compare(a, b, sort));
  }, [data, unit, sort, search]);
  const turnTree = useMemo(
    () =>
      buildTurnTree(
        data.turns.slice().sort((a, b) => compare(a, b, sort)),
        search.trim()
          ? new Set(
              unit === 'sessions'
                ? data.turns
                    .filter((turn) => rows.some((row) => row.id === turn.session_id))
                    .map((turn) => turn.id)
                : rows.map((row) => row.id),
            )
          : undefined,
      ),
    [data.turns, rows, unit, sort, search],
  );
  const allTurnEntries = useMemo(() => flattenTurns(turnTree), [turnTree]);
  const sessionGroups = useMemo(() => {
    const rootsBySession = new Map<string, TurnNode<Turn>[]>();
    for (const node of turnTree) {
      const id = node.turn.session_id || '';
      const group = rootsBySession.get(id) || [];
      group.push(node);
      rootsBySession.set(id, group);
    }
    return data.sessions
      .slice()
      .sort((a, b) => compare(a, b, sort))
      .filter(
        (session) =>
          rootsBySession.has(session.id) ||
          (!session.turns && (!search.trim() || rows.some((row) => row.id === session.id))),
      )
      .map((session) => ({ session, roots: rootsBySession.get(session.id) || [] }));
  }, [data.sessions, turnTree, rows, sort, search]);
  const visibleSessionEntries = useMemo(
    () =>
      sessionGroups.flatMap((group): SessionEntry[] =>
        group.session.parent_id && group.roots.length
          ? flattenTurns(group.roots, expanded).map((entry) => ({
              kind: 'turn' as const,
              ...entry,
            }))
          : [
              { kind: 'session', group },
              ...(expanded.has(group.session.id)
                ? flattenTurns(group.roots, expanded, 1).map((entry) => ({
                    kind: 'turn' as const,
                    ...entry,
                  }))
                : []),
            ],
      ),
    [sessionGroups, expanded],
  );
  const visibleTurnEntries = useMemo(() => flattenTurns(turnTree, expanded), [turnTree, expanded]);
  useEffect(() => {
    if (!search.trim()) return;
    // Keep matching descendants visible together with their ancestor context.
    setExpanded(
      (previous) =>
        new Set([
          ...previous,
          ...sessionGroups.map(({ session }) => session.id),
          ...allTurnEntries
            .filter(({ node }) => node.children.length)
            .map(({ node }) => node.turn.id),
        ]),
    );
  }, [search, allTurnEntries, sessionGroups]);

  return (
    <section className="view">
      <div className="toolbar">
        <h2 className="section-label">各轮明细</h2>
        <label>
          查看单位{' '}
          <select value={unit} onChange={(e) => setUnit(e.target.value as Unit)}>
            <option value="sessions">会话</option>
            <option value="turns">轮次</option>
          </select>
        </label>
        <label>
          排序{' '}
          <select value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
            <option value="time">时间 · 降序</option>
            <option value="tokens">Token · 降序</option>
            <option value="cache">缓存输入 · 降序</option>
            <option value="cost">估算费用 · 降序</option>
            <option value="turns">工具 / 轮次数 · 降序</option>
          </select>
        </label>
        <label>
          搜索{' '}
          <input
            aria-label="搜索会话、Prompt、工作区或模型"
            placeholder="会话、Prompt、工作区或模型"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <>
          <button
            onClick={() =>
              setExpanded(
                (previous) =>
                  new Set([
                    ...previous,
                    ...sessionGroups
                      .filter(({ roots }) => roots.length)
                      .map(({ session }) => session.id),
                    ...allTurnEntries
                      .filter(({ node }) => node.children.length)
                      .map(({ node }) => node.turn.id),
                  ]),
              )
            }
            disabled={!rows.length}
          >
            全部展开
          </button>
          <button onClick={() => setExpanded(new Set())} disabled={!expanded.size}>
            全部折叠
          </button>
        </>
        <span className="muted" id="row-count">
          {unit === 'sessions'
            ? `${sessionGroups.length} 组 · ${rows.length} 个${search.trim() ? '匹配' : ''}会话`
            : `${rows.length} 条`}{' '}
          · 当前样本 {data.summary.token_samples}/{data.summary.turns} 轮有用量
        </span>
      </div>
      <div className="panel detail-table">
        <div className="table-wrap">
          {unit === 'turns' ? (
            <TurnTable
              rows={visibleTurnEntries}
              sessions={data.sessions}
              expanded={expanded}
              onToggle={(id) =>
                setExpanded((previous) => {
                  const next = new Set(previous);
                  if (next.has(id)) next.delete(id);
                  else next.add(id);
                  return next;
                })
              }
              onSelect={onSelect}
            />
          ) : (
            <SessionTable
              rows={visibleSessionEntries}
              sessions={data.sessions}
              expanded={expanded}
              onToggle={(id) =>
                setExpanded((previous) => {
                  const next = new Set(previous);
                  if (next.has(id)) next.delete(id);
                  else next.add(id);
                  return next;
                })
              }
              onSelect={onSelect}
            />
          )}
        </div>
      </div>
      <p className="footnote">
        费用按当前 API 标准短上下文单价估算，非实际账单；未记录缓存写入时按无单独计费写入估算，
        未还原请求级长上下文加价或工具附加费。未知模型或用量不参与估算。缓存和推理不重复计入 Token
        总量；模型请求数未知。时间按 {Intl.DateTimeFormat().resolvedOptions().timeZone} 显示。
      </p>
    </section>
  );
}

function compare(a: Turn | Session, b: Turn | Session, sort: Sort): number {
  if (sort === 'cache') {
    const cache = (row: Turn | Session) =>
      ('cache_usage' in row ? row.cache_usage.cached_input_known : row.cached_input_tokens) ?? -1;
    return cache(b) - cache(a);
  }
  if (sort === 'cost') {
    const cost = (row: Turn | Session) =>
      ('estimated_cost_known' in row ? row.estimated_cost_known : row.estimated_cost) ?? -1;
    return cost(b) - cost(a);
  }
  if (sort === 'tokens')
    return (
      (b.token_total ?? ('token_known' in b ? b.token_known : null) ?? -1) -
      (a.token_total ?? ('token_known' in a ? a.token_known : null) ?? -1)
    );
  if (sort === 'turns')
    return ('turns' in b ? b.turns : b.tools.length) - ('turns' in a ? a.turns : a.tools.length);
  return String(b.started_at ?? '').localeCompare(String(a.started_at ?? ''));
}

function TurnIdentity({
  entry,
  session,
  expanded,
  onToggle,
  promptLabel = false,
}: {
  entry: TurnEntry;
  session?: Session;
  expanded: Set<string>;
  onToggle: (id: string) => void;
  promptLabel?: boolean;
}) {
  const turn = entry.node.turn;
  const subagent = Boolean(turn.parent_session_id || session?.parent_id);
  const agents = new Set(entry.node.children.map(({ turn }) => turn.session_id)).size;
  return (
    <>
      {entry.node.children.length ? (
        <button
          className="session-toggle"
          aria-label={`${expanded.has(turn.id) ? '折叠' : '展开'}第 ${turn.sequence ?? '?'} 轮的子代理`}
          aria-expanded={expanded.has(turn.id)}
          onClick={() => onToggle(turn.id)}
        >
          {expanded.has(turn.id) ? '▾' : '▸'}
        </button>
      ) : (
        <span className="turn-branch" aria-hidden="true">
          ↳
        </span>
      )}
      <span className={`tag ${subagent ? 'subagent-badge' : 'turn-badge'}`}>
        {subagent ? '子代理' : '轮次'}
      </span>{' '}
      <span className="session-identity" title={`轮次 ID：${turn.native_id}`}>
        {subagent
          ? `${session ? sessionName(session) : turn.session_title || '子代理'} · 第 ${turn.sequence ?? '?'} 轮`
          : promptLabel
            ? turnName(turn)
            : `第 ${turn.sequence ?? '?'} 轮`}
      </span>
      {agents > 0 && <small className="sub session-child-count">{agents} 个子代理</small>}
      {(entry.node.parentMissing || (subagent && !turn.parent_turn_id)) && (
        <small className="sub session-parent-note">
          {turn.parent_turn_id ? '父轮次不在当前范围' : '所属父轮次未知'}
        </small>
      )}
    </>
  );
}

function TurnTable({
  rows,
  sessions,
  expanded,
  onToggle,
  onSelect,
}: {
  rows: TurnEntry[];
  sessions: Session[];
  expanded: Set<string>;
  onToggle: (id: string) => void;
  onSelect: (s: Selection) => void;
}) {
  if (!rows.length) return <div className="empty">没有匹配轮次，请调整时间或筛选。</div>;
  return (
    <table>
      <thead>
        <tr>
          {[
            '轮次 / 会话',
            '模型',
            '工作区',
            '时间',
            '输入 Token',
            '总 Token',
            '缓存输入',
            '输出',
            '估算费用 USD',
            '模型调用',
            '状态',
            '详情',
          ].map((x) => (
            <th key={x}>{x}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((entry) => {
          const t = entry.node.turn;
          const session = sessions.find((session) => session.id === t.session_id);
          return (
            <tr
              key={t.id}
              className={`workspace-row session-turn-row${t.parent_session_id ? ' subagent-row' : ''}`}
              style={workspaceColor(t)}
              data-turn-id={t.id}
              data-session-id={t.session_id}
              data-parent-turn-id={t.parent_turn_id || undefined}
              data-depth={entry.depth}
            >
              <td style={{ paddingLeft: 8 + Math.min(entry.depth, 8) * 16 }}>
                <TurnIdentity
                  entry={entry}
                  session={session}
                  expanded={expanded}
                  onToggle={onToggle}
                  promptLabel
                />
                <small className="sub" title={`会话 ID：${t.session_native_id || t.session_id}`}>
                  会话：{t.session_title || '未命名会话'}
                </small>
                <small className="sub" title={t.native_id}>
                  轮次 ID：{t.native_id}
                </small>
              </td>
              <td>{t.model}</td>
              <td>
                <span className="project-name" title={t.project || undefined}>
                  {projectName(t)}
                </span>
              </td>
              <td>{formatDate(t.started_at)}</td>
              <td>{formatNumber(t.input_tokens)}</td>
              <td>{formatNumber(t.token_total)}</td>
              <td>
                {formatNumber(t.cached_input_tokens)}
                <small className="sub">
                  占输入 {formatPercent(cacheRate(t.input_tokens, t.cached_input_tokens))}
                </small>
              </td>
              <td>{formatNumber(t.output_tokens)}</td>
              <td
                title={
                  t.cost_basis === 'recorded'
                    ? 'pi 记录的请求成本，非实际账单'
                    : t.estimated_cost_reason || '按 API 标准短上下文单价估算'
                }
              >
                {formatCost(t.estimated_cost)}
              </td>
              <td>未知</td>
              <td>
                <span className="tag">{statusLabel(t.status)}</span>
              </td>
              <td>
                <button
                  className="open"
                  aria-label={`查看轮次 ${turnName(t)}`}
                  onClick={() => onSelect({ sessionId: t.session_id ?? '', turnId: t.id })}
                >
                  详情 →
                </button>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function SessionTable({
  rows,
  sessions,
  expanded,
  onToggle,
  onSelect,
}: {
  rows: SessionEntry[];
  sessions: Session[];
  expanded: Set<string>;
  onToggle: (id: string) => void;
  onSelect: (s: Selection) => void;
}) {
  const bySession = useMemo(
    () => new Map(sessions.map((session) => [session.id, session])),
    [sessions],
  );
  if (!rows.length) return <div className="empty">没有匹配会话，请调整时间或筛选。</div>;
  return (
    <table className="session-table">
      <thead>
        <tr>
          {[
            '会话 / Agent 轮次',
            '开始时间',
            '模型',
            '工作区',
            'Prompt',
            '轮次',
            '工具记录',
            'Token 已知小计',
            '缓存输入',
            '估算费用 USD',
            '状态',
            '详情',
          ].map((label) => (
            <th key={label}>{label}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((entry) => {
          if (entry.kind === 'session') {
            const { session: s, roots } = entry.group;
            return (
              <tr
                key={s.id}
                className={`workspace-row session-row${expanded.has(s.id) ? ' is-expanded' : ''}`}
                style={workspaceColor(s)}
                data-session-id={s.id}
              >
                <td className="session-name-cell">
                  <button
                    className="session-toggle"
                    aria-label={`${expanded.has(s.id) ? '折叠' : '展开'}会话 ${sessionName(s)}`}
                    aria-expanded={expanded.has(s.id)}
                    onClick={() => onToggle(s.id)}
                    disabled={!roots.length}
                  >
                    {expanded.has(s.id) ? '▾' : '▸'}
                  </button>
                  <span className={s.parent_id ? 'tag subagent-badge' : 'badge'}>
                    {s.parent_id ? '子代理' : 'CX'}
                  </span>
                  <span className="session-identity" title={sessionName(s)}>
                    {sessionName(s)}
                  </span>
                  {s.parent_id && (
                    <small className="sub session-parent-note">
                      {roots.some(({ turn }) => turn.parent_turn_id)
                        ? '父轮次不在当前范围'
                        : '所属父轮次未知'}
                    </small>
                  )}
                </td>
                <td>{formatDate(s.started_at)}</td>
                <td>{s.model}</td>
                <td>
                  <span className="project-name" title={s.project || undefined}>
                    {projectName(s)}
                  </span>
                </td>
                <td className="prompt-cell">
                  <span className="muted">—</span>
                </td>
                <td>{formatNumber(s.turns)}</td>
                <td>
                  {formatNumber(s.tools)}
                  <small className="sub">已观察记录</small>
                </td>
                <td>
                  {formatNumber(s.token_known)}
                  <small className="sub">
                    {s.token_samples}/{s.turns} 轮有效
                  </small>
                </td>
                <td className="cache-cell">
                  {formatNumber(s.cache_usage.cached_input_known)}
                  <small className="sub">
                    占输入 {formatPercent(s.cache_usage.hit_rate)} · 配对 {s.cache_usage.samples}/
                    {s.cache_usage.turns} 轮
                    {!s.cache_usage.complete && s.cache_usage.samples > 0 ? ' · 已知小计' : ''}
                  </small>
                </td>
                <td className="cost-cell">
                  {formatCost(s.estimated_cost_known)}
                  <small className="sub">
                    {s.cost_samples}/{s.turns} 轮
                    {s.cost_basis === 'recorded'
                      ? '记录成本'
                      : s.cost_basis === 'mixed'
                        ? '记录/估算成本'
                        : '可估算'}
                    {s.cost_samples < s.turns && s.cost_samples > 0 ? ' · 已知小计' : ''}
                  </small>
                </td>
                <td>
                  <span className="tag">{statusLabel(s.status)}</span>
                </td>
                <td>
                  <button
                    className="open"
                    aria-label={`查看会话 ${sessionName(s)}`}
                    onClick={() => onSelect({ sessionId: s.id })}
                  >
                    详情 →
                  </button>
                </td>
              </tr>
            );
          }
          const turn = entry.node.turn;
          const session = bySession.get(turn.session_id || '');
          return (
            <tr
              key={turn.id}
              className={`workspace-row session-turn-row${turn.parent_session_id ? ' subagent-row' : ''}`}
              style={workspaceColor(turn)}
              data-session-id={turn.session_id}
              data-turn-id={turn.id}
              data-parent-turn-id={turn.parent_turn_id || undefined}
              data-depth={entry.depth}
            >
              <td
                className="turn-name-cell"
                style={{ paddingLeft: 8 + Math.min(entry.depth, 8) * 16 }}
              >
                <TurnIdentity
                  entry={entry}
                  session={session}
                  expanded={expanded}
                  onToggle={onToggle}
                />
              </td>
              <td>{formatDate(turn.started_at)}</td>
              <td>{turn.model}</td>
              <td>
                <span className="project-name" title={turn.project || undefined}>
                  {projectName(turn)}
                </span>
              </td>
              <td className="prompt-cell">
                <span title={turnName(turn)}>{turnName(turn)}</span>
              </td>
              <td className="muted">—</td>
              <td>
                {formatNumber(turn.tools.length)}
                <small className="sub">已观察记录</small>
              </td>
              <td>{formatNumber(turn.token_total)}</td>
              <td className="cache-cell">
                {formatNumber(turn.cached_input_tokens)}
                <small className="sub">
                  占输入 {formatPercent(cacheRate(turn.input_tokens, turn.cached_input_tokens))}
                </small>
              </td>
              <td
                title={
                  turn.cost_basis === 'recorded'
                    ? 'pi 记录的请求成本，非实际账单'
                    : turn.estimated_cost_reason || '按 API 标准短上下文单价估算'
                }
              >
                {formatCost(turn.estimated_cost)}
              </td>
              <td>
                <span className="tag">{statusLabel(turn.status)}</span>
              </td>
              <td>
                <button
                  className="open"
                  aria-label={`查看轮次 ${turnName(turn)}`}
                  onClick={() => onSelect({ sessionId: turn.session_id || '', turnId: turn.id })}
                >
                  详情 →
                </button>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
