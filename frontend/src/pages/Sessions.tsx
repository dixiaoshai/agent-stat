import { useMemo, useState } from 'react';
import type { Overview, Selection, Session, Turn } from '../types';
import { formatDate, formatNumber, statusLabel } from '../lib/format';

type Unit = 'turns' | 'sessions';
type Sort = 'time' | 'tokens' | 'turns';

export function Sessions({ data, onSelect }: { data: Overview; onSelect: (s: Selection) => void }) {
  const [unit, setUnit] = useState<Unit>('turns');
  const [sort, setSort] = useState<Sort>('time');
  const [search, setSearch] = useState('');
  const rows = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    if (unit === 'sessions') {
      return data.sessions
        .filter(
          (s) => !query || `${s.project} ${s.model} ${s.id}`.toLocaleLowerCase().includes(query),
        )
        .slice()
        .sort((a, b) => compare(a, b, sort));
    }
    return data.turns
      .filter(
        (t) =>
          !query ||
          `${t.project} ${t.model} ${t.native_id} ${t.session_id}`
            .toLocaleLowerCase()
            .includes(query),
      )
      .slice()
      .sort((a, b) => compare(a, b, sort));
  }, [data, unit, sort, search]);

  return (
    <section className="view">
      <div className="toolbar">
        <h2 className="section-label">各轮明细</h2>
        <label>
          查看单位{' '}
          <select value={unit} onChange={(e) => setUnit(e.target.value as Unit)}>
            <option value="turns">轮次</option>
            <option value="sessions">会话</option>
          </select>
        </label>
        <label>
          排序{' '}
          <select value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
            <option value="time">时间 · 降序</option>
            <option value="tokens">Token · 降序</option>
            <option value="turns">工具 / 轮次数 · 降序</option>
          </select>
        </label>
        <label>
          搜索{' '}
          <input
            aria-label="搜索工作区或模型"
            placeholder="工作区、模型或身份"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <span className="muted" id="row-count">
          {rows.length} 条 · 当前样本 {data.summary.token_samples}/{data.summary.turns} 轮有用量
        </span>
      </div>
      <div className="panel detail-table">
        <div className="table-wrap">
          {unit === 'turns' ? (
            <TurnTable rows={rows as Turn[]} onSelect={onSelect} />
          ) : (
            <SessionTable rows={rows as Session[]} onSelect={onSelect} />
          )}
        </div>
      </div>
      <p className="footnote">
        费用与模型请求数保持未知。分类仅按来源字段展示；缓存和推理不重复计入 Token 总量。时间按{' '}
        {Intl.DateTimeFormat().resolvedOptions().timeZone} 显示。
      </p>
    </section>
  );
}

function compare(a: Turn | Session, b: Turn | Session, sort: Sort): number {
  if (sort === 'tokens')
    return (
      (b.token_total ?? ('token_known' in b ? b.token_known : null) ?? -1) -
      (a.token_total ?? ('token_known' in a ? a.token_known : null) ?? -1)
    );
  if (sort === 'turns')
    return ('turns' in b ? b.turns : b.tools.length) - ('turns' in a ? a.turns : a.tools.length);
  return String(b.started_at ?? '').localeCompare(String(a.started_at ?? ''));
}

function TurnTable({ rows, onSelect }: { rows: Turn[]; onSelect: (s: Selection) => void }) {
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
            '费用',
            '模型调用',
            '状态',
            '详情',
          ].map((x) => (
            <th key={x}>{x}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((t) => (
          <tr key={t.id}>
            <td>
              <span className="badge">CX</span>
              <span className="session-identity" title={t.native_id}>
                {t.native_id}
              </span>
              <small className="sub" title={t.session_id}>
                {t.session_id}
              </small>
            </td>
            <td>{t.model}</td>
            <td>
              <span className="project-name">{t.project || '未知工作区'}</span>
            </td>
            <td>{formatDate(t.started_at)}</td>
            <td>{formatNumber(t.input_tokens)}</td>
            <td>{formatNumber(t.token_total)}</td>
            <td>{formatNumber(t.cached_input_tokens)}</td>
            <td>{formatNumber(t.output_tokens)}</td>
            <td>未知</td>
            <td>未知</td>
            <td>
              <span className="tag">{statusLabel(t.status)}</span>
            </td>
            <td>
              <button
                className="open"
                aria-label={`查看轮次 ${t.native_id}`}
                onClick={() => onSelect({ sessionId: t.session_id ?? '', turnId: t.id })}
              >
                详情 →
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SessionTable({ rows, onSelect }: { rows: Session[]; onSelect: (s: Selection) => void }) {
  if (!rows.length) return <div className="empty">没有匹配会话，请调整时间或筛选。</div>;
  return (
    <table>
      <thead>
        <tr>
          {[
            '会话',
            '模型',
            '工作区',
            '开始时间',
            '轮次',
            '工具记录',
            'Token 已知小计',
            '费用',
            '状态',
            '详情',
          ].map((x) => (
            <th key={x}>{x}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((s) => (
          <tr key={s.id}>
            <td>
              <span className="badge">CX</span>
              <span className="session-identity" title={s.id}>
                {s.id}
              </span>
            </td>
            <td>{s.model}</td>
            <td>
              <span className="project-name">{s.project || '未知工作区'}</span>
            </td>
            <td>{formatDate(s.started_at)}</td>
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
            <td>未知</td>
            <td>
              <span className="tag">{statusLabel(s.status)}</span>
            </td>
            <td>
              <button
                className="open"
                aria-label={`查看会话 ${s.id}`}
                onClick={() => onSelect({ sessionId: s.id })}
              >
                详情 →
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
