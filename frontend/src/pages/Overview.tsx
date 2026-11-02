import type { Overview as OverviewData, Selection } from '../types';
import { compactNumber, formatDate, formatNumber } from '../lib/format';
import { MetricCard } from '../components/MetricCard';
import { TokenBreakdown } from '../components/TokenBreakdown';
import { TrendChart } from '../components/TrendChart';

export function Overview({
  data,
  onSelect,
  onSessions,
}: {
  data: OverviewData;
  onSelect: (selection: Selection) => void;
  onSessions: () => void;
}) {
  const s = data.summary;
  const coverage = s.turns > 0 ? (100 * s.token_samples) / s.turns : null;
  const maxGroup = Math.max(1, ...data.groups.map((g) => g.token_known ?? 0));
  return (
    <>
      <h2 className="section-label">概况</h2>
      <div className="summary-grid">
        <TokenBreakdown data={data} />
        <article className="panel summary-card">
          <h3>费用</h3>
          <div className="cost-layout">
            <div>
              <strong className="accent">未知</strong>
              <p>未核实来源费用</p>
            </div>
            <div className="unknown-pie" aria-hidden="true">
              —
            </div>
            <div className="cost-explanation">
              <p>
                来源报告费用 <span>未知</span>
              </p>
              <p>
                模型估算费用 <span>未启用</span>
              </p>
            </div>
          </div>
          <p className="card-note">不套近似模型价格，不将缺失金额记为零。</p>
        </article>
        <article className="panel summary-card">
          <h3>用量覆盖</h3>
          <strong>{coverage == null ? '未知' : `${coverage.toFixed(1)}%`}</strong>
          <p>
            {s.token_samples} / {s.turns} 轮有有效用量
          </p>
          <div className="coverage-track">
            <span style={{ width: `${coverage ?? 0}%` }} />
          </div>
          <p className="muted">有效用量轮次 / 当前范围总轮次</p>
          <button className="scope-button" onClick={onSessions}>
            查看构成轮次
          </button>
          <p className="card-note">未知样本不参与中位数与 P90。</p>
        </article>
      </div>
      <h2 className="section-label">单轮消耗与运行规模</h2>
      <div className="metrics">
        <MetricCard
          label="单轮 Token 中位数"
          value={compactNumber(s.median)}
          detail={`n = ${s.token_samples} / ${s.turns} 轮`}
          note="仅使用有效用量轮次"
        />
        <MetricCard
          label="单轮 Token P90"
          value={compactNumber(s.p90)}
          detail="尾部消耗分布"
          note="Type 7 线性插值"
        />
        <MetricCard
          label="观测会话"
          value={formatNumber(s.sessions)}
          detail="当前筛选范围"
          note="保留独立会话身份"
        />
        <MetricCard
          label="可识别轮次"
          value={formatNumber(s.turns)}
          detail="原生身份 / 推断归属"
          note="按轮次开始时间归窗"
        />
        <MetricCard
          label="工具调用"
          value={formatNumber(s.tool_calls)}
          detail="已观察到的调用记录"
          note="次数不等于模型请求数"
        />
        <MetricCard
          label="模型请求数"
          value="未知"
          detail="未确认逐请求身份"
          note="不由用量快照反推"
        />
      </div>
      <TrendChart data={data} />
      <div className="bottom-grid">
        <article className="panel model-panel">
          <h3>按模型 · 用量构成</h3>
          <div className="groups">
            {data.groups.length === 0 ? (
              <div className="empty">暂无模型数据</div>
            ) : (
              data.groups.map((g) => (
                <div className="group" key={g.model}>
                  <b>{g.model}</b>
                  <span>{g.turns} 轮</span>
                  <div className="bar">
                    <i style={{ width: `${(100 * (g.token_known ?? 0)) / maxGroup}%` }} />
                  </div>
                  <small>
                    {formatNumber(g.token_known)} tokens · {g.token_samples}/{g.turns} 有效轮次
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
            {data.sessions.length === 0 ? (
              <div className="empty">当前范围暂无会话</div>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>工作区</th>
                    <th>开始时间</th>
                    <th>轮次</th>
                    <th>Token 小计</th>
                    <th>详情</th>
                  </tr>
                </thead>
                <tbody>
                  {data.sessions.slice(0, 6).map((session) => (
                    <tr key={session.id}>
                      <td className="session-identity" title={session.project ?? '未知工作区'}>
                        {session.project ?? '未知工作区'}
                      </td>
                      <td>{formatDate(session.started_at)}</td>
                      <td>{session.turns}</td>
                      <td>
                        {compactNumber(session.token_known)}
                        <small className="sub">
                          {session.token_samples}/{session.turns} 有效轮次
                        </small>
                      </td>
                      <td>
                        <button
                          className="open"
                          aria-label={`查看会话 ${session.id}`}
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
        高消耗仅是进一步检查的线索。费用、模型请求数及父子包含关系未知时，不生成估算或确定的联合总量。
      </p>
    </>
  );
}
