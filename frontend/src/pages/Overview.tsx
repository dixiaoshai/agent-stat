import { useRef, useState } from 'react';
import type { Overview as OverviewData, Selection } from '../types';
import {
  cacheRate,
  compactNumber,
  formatCost,
  formatDate,
  formatNumber,
  formatPercent,
  projectName,
  sessionName,
  turnName,
  uncachedInput,
  validCachePair,
} from '../lib/format';
import {
  knownValue,
  rankConcentration,
  statistics,
  type ConcentrationMetric,
} from '../lib/overview';
import { MetricCard } from '../components/MetricCard';
import { TokenBreakdown } from '../components/TokenBreakdown';
import { TrendChart } from '../components/TrendChart';
import { BreakdownPie } from '../components/BreakdownPie';

const colors = { input: '#3c6dbb', cached: '#b8c2d1', output: '#8c806b' };

export function Overview({
  data,
  onSelect,
  onSessions,
}: {
  data: OverviewData;
  onSelect: (selection: Selection) => void;
  onSessions: () => void;
}) {
  const { summary: s, turns } = data;
  const tokens = statistics(turns.map((t) => t.token_total));
  const cost = statistics(turns.map((t) => t.estimated_cost));
  const hits = statistics(turns.map((t) => cacheRate(t.input_tokens, t.cached_input_tokens)));
  const input = statistics(turns.map((t) => uncachedInput(t.input_tokens, t.cached_input_tokens)));
  const cache = statistics(
    turns.map((t) =>
      validCachePair(t.input_tokens, t.cached_input_tokens) ? t.cached_input_tokens : null,
    ),
  );
  const output = statistics(turns.map((t) => t.output_tokens));
  const groupValue = (g: OverviewData['groups'][number]) =>
    s.aggregation_blocked ? (g.observed_token_known ?? g.token_known) : g.token_known;
  const maxGroup = Math.max(1, ...data.groups.map((g) => groupValue(g) ?? 0));

  return (
    <div className="overview-page">
      <h2 className="section-label">概况</h2>
      <div className="summary-grid overview-summary">
        <TokenBreakdown data={data} />
        <CostSummary data={data} />
        <ConcentrationCard data={data} onSelect={onSelect} />
      </div>
      <h2 className="section-label">单轮消耗（中位数）</h2>
      <div className="metrics overview-metrics">
        <MetricCard
          label="单轮 Token 中位数"
          value={compactNumber(tokens.median)}
          detail={`P90 ${compactNumber(tokens.p90)} · n=${tokens.count}`}
          note={`有效 ${tokens.count}/${s.turns} 轮 · 未知样本不参与统计`}
        />
        <MetricCard
          label="单轮估算费用"
          value={formatCost(cost.median)}
          detail={`P90 ${formatCost(cost.p90)} · 均值 ${formatCost(cost.mean)}`}
          note={`USD · 可估算 ${cost.count}/${s.turns} 轮 · 非账单`}
        />
        <MetricCard
          label="缓存占输入 / 轮"
          value={formatPercent(hits.median)}
          detail={`P10 ${formatPercent(hits.p10)} · n=${hits.count}`}
          note="各轮 Token 比例的中位数；非请求命中率"
        />
        <MetricCard
          label="未缓存输入 / 轮"
          value={compactNumber(input.median)}
          detail={`P90 ${compactNumber(input.p90)} · n=${input.count}`}
          note="输入减缓存读取；未知写入保留在输入中"
        />
        <MetricCard
          label="Cache / 轮"
          value={compactNumber(cache.median)}
          detail={`P90 ${compactNumber(cache.p90)} · n=${cache.count}`}
          note="缓存读取 Token；仅有效输入 / 缓存配对"
        />
        <MetricCard
          label="Output / 轮"
          value={compactNumber(output.median)}
          detail={`P90 ${compactNumber(output.p90)} · n=${output.count}`}
          note="输出 Token；含来源已计入的推理输出"
        />
      </div>
      <TrendChart data={data} />

      <details className="panel overview-support cache-details">
        <summary>
          缓存与数据覆盖 · 缓存读取 {compactNumber(s.cache_usage.cached_input_known)} · 占输入{' '}
          {formatPercent(s.cache_usage.hit_rate)}
        </summary>
        <div className="cache-metrics">
          <div>
            <span>缓存输入 Token</span>
            <b>{formatNumber(s.cache_usage.cached_input_known)}</b>
          </div>
          <div>
            <span>未缓存输入 Token</span>
            <b>{formatNumber(s.cache_usage.uncached_input_known)}</b>
          </div>
          <div>
            <span>缓存占输入（整体）</span>
            <b>{formatPercent(s.cache_usage.hit_rate)}</b>
          </div>
          <div>
            <span>缓存写入 Token</span>
            <b>{formatNumber(s.cache_usage.cache_write_known)}</b>
          </div>
        </div>
        <p>
          缓存配对 {s.cache_usage.samples}/{s.turns} 轮 · 写入字段{' '}
          {s.cache_usage.cache_write_samples}/{s.turns} 轮 · 总量字段 {s.token_samples}/{s.turns}{' '}
          轮有效。
        </p>
        <p>
          整体比例为同一批有效样本的缓存读取总和 /
          输入总和；区别于上方单轮比例的中位数。缓存已包含在输入中，不重复相加。
          {s.aggregation_blocked && '父子观测值未去重，可能重复。'}
        </p>
        <p>
          {s.sessions} 个会话 · {formatNumber(s.tool_calls)} 条工具记录 · 模型请求数未知。
        </p>
      </details>
      <details className="panel overview-support pricing-details">
        <summary>估算依据与模型单价 · {data.pricing.checked_at}</summary>
        <p>
          {data.pricing.basis} ·{' '}
          <a href={data.pricing.source_url} target="_blank" rel="noreferrer">
            OpenAI 官方价格
          </a>
        </p>
        {data.pricing.assumptions.map((text) => (
          <p key={text}>{text}</p>
        ))}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>模型</th>
                <th>输入</th>
                <th>缓存输入</th>
                <th>输出</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(data.pricing.models).map(([model, price]) => (
                <tr key={model}>
                  <td>{model}</td>
                  <td>{formatCost(price.input)}</td>
                  <td>{formatCost(price.cached_input)}</td>
                  <td>{formatCost(price.output)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p>单价单位：USD / 百万 Token。</p>
      </details>

      <div className="bottom-grid">
        <article className="panel model-panel">
          <h3>按模型 · {s.aggregation_blocked ? '观测小计（未去重）' : '用量构成'}</h3>
          <div className="groups">
            {data.groups.length === 0 ? (
              <div className="empty">暂无模型数据</div>
            ) : (
              data.groups.map((g) => (
                <div className="group" key={g.model}>
                  <b>{g.model}</b>
                  <span>{g.turns} 轮</span>
                  <div className="bar">
                    <i style={{ width: `${(100 * (groupValue(g) ?? 0)) / maxGroup}%` }} />
                  </div>
                  <small>
                    {formatNumber(groupValue(g))} Token · {g.token_samples}/{g.turns} 轮有效
                  </small>
                  <small>
                    缓存 {formatNumber(g.cache_usage.cached_input_known)} · 占输入{' '}
                    {formatPercent(g.cache_usage.hit_rate)} · 配对 {g.cache_usage.samples}/
                    {g.cache_usage.turns} 轮{g.cache_usage.aggregation_blocked ? ' · 未去重' : ''}
                  </small>
                </div>
              ))
            )}
          </div>
        </article>
        <article className="panel recent-panel">
          <div className="panel-head">
            <h3>最近会话</h3>
            <button className="text-button" onClick={onSessions}>
              查看全部
            </button>
          </div>
          <div className="table-wrap">
            {!data.sessions.length ? (
              <div className="empty">当前范围暂无会话</div>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>会话</th>
                    <th>开始时间</th>
                    <th>轮次</th>
                    <th>Token 小计</th>
                    <th>详情</th>
                  </tr>
                </thead>
                <tbody>
                  {data.sessions.slice(0, 6).map((session) => (
                    <tr key={session.id}>
                      <td>
                        <span
                          className="session-identity"
                          title={`会话 ID：${session.native_id || session.id}`}
                        >
                          {sessionName(session)}
                        </span>
                        <small className="sub" title={session.project || undefined}>
                          {projectName(session)}
                        </small>
                      </td>
                      <td>{formatDate(session.started_at)}</td>
                      <td>{session.turns}</td>
                      <td>
                        {compactNumber(session.token_known)}
                        <small className="sub">
                          {session.token_samples}/{session.turns} 轮有效
                        </small>
                      </td>
                      <td>
                        <button
                          className="open"
                          aria-label={`查看会话 ${sessionName(session)}`}
                          onClick={() => onSelect({ sessionId: session.id })}
                        >
                          详情
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </article>
      </div>
      <p className="footnote">
        仅统计当前筛选范围的已知样本。费用为 API 等价估算，非实际账单。
        {s.aggregation_blocked && '父子会话观测值未去重，不代表实际联合用量。'}
      </p>
    </div>
  );
}

function CostSummary({ data }: { data: OverviewData }) {
  const { summary: s } = data;
  const value = s.aggregation_blocked ? s.observed_estimated_cost_known : s.estimated_cost_known;
  const rows = data.turns.filter(
    (t) =>
      t.cost_breakdown &&
      knownValue(t.estimated_cost) &&
      Object.values(t.cost_breakdown).every(knownValue) &&
      Math.abs(Object.values(t.cost_breakdown).reduce((sum, v) => sum + v, 0) - t.estimated_cost) <
        1e-8,
  );
  const parts = [
    {
      label: '未缓存输入',
      value: rows.reduce((n, t) => n + t.cost_breakdown!.input, 0),
      color: colors.input,
    },
    {
      label: '缓存读取',
      value: rows.reduce((n, t) => n + t.cost_breakdown!.cached_input, 0),
      color: colors.cached,
    },
    {
      label: '输出',
      value: rows.reduce((n, t) => n + t.cost_breakdown!.output, 0),
      color: colors.output,
    },
  ];
  const total = parts.reduce((n, p) => n + p.value, 0);
  return (
    <article className="panel summary-card cost-summary">
      <h3>估算费用</h3>
      <div className="breakdown-layout">
        <div className="breakdown-amount">
          <strong className="accent" title={formatCost(value)}>
            {formatCost(value)}
          </strong>
          <p>USD · 非账单</p>
          <small>
            可估算 {s.cost_samples}/{s.turns} 轮
          </small>
        </div>
        <BreakdownPie
          className="breakdown-chart"
          segments={parts}
          label={rows.length ? `费用构成，${rows.length} 轮可拆分` : '费用构成未知'}
        />
        <div className="breakdown-legend">
          {parts.map((p) => (
            <div className="breakdown-row" key={p.label}>
              <i className="swatch" style={{ background: p.color }} />
              <span>{p.label}</span>
              <span className="legend-number">
                {rows.length ? formatCost(p.value) : '未知'} ·{' '}
                {formatPercent(total > 0 ? p.value / total : null)}
              </span>
            </div>
          ))}
        </div>
      </div>
      <p className="card-note">
        {s.aggregation_blocked ? '观测小计未去重 · ' : ''}
        {s.cost_samples < s.turns ? '已知小计 · ' : ''}构成覆盖 {rows.length}/{s.turns}{' '}
        轮；仅按已估算分类拆分。
      </p>
    </article>
  );
}

function ConcentrationCard({
  data,
  onSelect,
}: {
  data: OverviewData;
  onSelect: (s: Selection) => void;
}) {
  const [metric, setMetric] = useState<ConcentrationMetric>('cost');
  const ref = useRef<HTMLDialogElement>(null);
  const rank = rankConcentration(data.turns, metric);
  const label = metric === 'cost' ? '估算费用' : metric === 'tokens' ? '总 Token' : '工具输出字节';
  const amount = (value: number) => (metric === 'cost' ? formatCost(value) : compactNumber(value));
  return (
    <article className="panel summary-card concentration-card">
      <h3>消耗集中度</h3>
      <strong>{formatPercent(rank.share)}</strong>
      <p>前 10% 轮次 / {label}贡献</p>
      <div className="metric-toggle" role="group" aria-label="集中度指标">
        {(
          [
            ['cost', '费用'],
            ['tokens', '总 Token'],
            ['toolBytes', '工具输出字节'],
          ] as const
        ).map(([key, text]) => (
          <button
            key={key}
            aria-pressed={metric === key}
            className={metric === key ? 'active' : ''}
            onClick={() => setMetric(key)}
          >
            {text}
          </button>
        ))}
      </div>
      <button
        className="scope-button"
        disabled={rank.share == null}
        onClick={() => ref.current?.showModal()}
      >
        查看这 {rank.rows.length} 轮
      </button>
      <p className="card-note">
        入选 {rank.rows.length} 轮 · 有效 {rank.samples}/{data.turns.length} 轮
        {data.summary.aggregation_blocked ? ' · 未去重' : ''}
      </p>
      <dialog
        ref={ref}
        className="detail concentration-dialog"
        aria-labelledby="concentration-title"
      >
        <div className="panel-head">
          <h2 id="concentration-title">前 10% 轮次 · {label}</h2>
          <button onClick={() => ref.current?.close()}>关闭 ×</button>
        </div>
        <p className="metadata">
          当前筛选范围 · 已知样本内排序 · 贡献 {formatPercent(rank.share)}
          {data.summary.aggregation_blocked ? ' · 观测值未去重' : ''}
        </p>
        <div className="detail-table-wrap">
          <table>
            <thead>
              <tr>
                <th>轮次</th>
                <th>模型</th>
                <th>{label}</th>
                <th>详情</th>
              </tr>
            </thead>
            <tbody>
              {rank.rows.map(({ turn, value }) => (
                <tr key={turn.id}>
                  <td>{turnName(turn)}</td>
                  <td>{turn.model}</td>
                  <td>{amount(value)}</td>
                  <td>
                    <button
                      className="open"
                      disabled={!turn.session_id}
                      onClick={() => {
                        ref.current?.close();
                        onSelect({ sessionId: turn.session_id!, turnId: turn.id });
                      }}
                    >
                      详情 →
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </dialog>
    </article>
  );
}
