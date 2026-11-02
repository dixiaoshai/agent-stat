import type { Overview } from '../types';
import { compactNumber, formatNumber } from '../lib/format';

export function TrendChart({ data }: { data: Overview }) {
  const max = Math.max(1, ...data.trend.map((t) => t.token_known ?? 0));
  return (
    <article className="panel trend-panel">
      <div className="panel-head">
        <h3>趋势</h3>
        <span className="muted">指标：Token 已知小计</span>
      </div>
      <p className="chart-description">
        按轮次开始时间归入 UTC 日期。蓝：输入 ·
        褐：输出；分类不完整时以蓝柱展示未拆分小计，斜线表示未知。
      </p>
      {data.trend.length === 0 ? (
        <div className="empty">当前范围暂无活动，请调整时间或筛选条件。</div>
      ) : (
        <div className="chart" role="img" aria-label="每日 Token 用量趋势，可悬停查看小计与覆盖">
          <div className="plot">
            <div className="plot-lines" aria-hidden="true">
              {[max, max / 2, 0].map((n, i) => (
                <div key={i}>
                  <span>{compactNumber(n)}</span>
                </div>
              ))}
            </div>
            <div className="bars">
              {data.trend.map((bucket) => {
                const known = data.turns.filter(
                  (t) =>
                    (t.started_at ?? '未知').slice(0, 10) === bucket.date && t.token_total != null,
                );
                const split =
                  known.length > 0 &&
                  known.every(
                    (t) =>
                      t.input_tokens != null &&
                      t.output_tokens != null &&
                      t.token_total === t.input_tokens + t.output_tokens,
                  );
                const output = split ? known.reduce((n, t) => n + (t.output_tokens ?? 0), 0) : 0;
                const height = bucket.token_known == null ? 1 : (100 * bucket.token_known) / max;
                const percent =
                  split && (bucket.token_known ?? 0) > 0 ? (100 * output) / bucket.token_known! : 0;
                const label = `${bucket.date} · Token ${formatNumber(bucket.token_known)} · 有效 ${bucket.token_samples}/${bucket.turns} 轮${split ? ' · 输入/输出分类完整' : ' · 未拆分'}`;
                return (
                  <div className="bar-col" key={bucket.date} title={label}>
                    <div
                      className={`bar-stack${bucket.token_known == null ? ' unknown' : ''}`}
                      style={{ height: `${height}%` }}
                    >
                      <span className="segment input" style={{ height: `${100 - percent}%` }} />
                      <span className="segment output" style={{ height: `${percent}%` }} />
                    </div>
                    <small>{bucket.date === '未知' ? '未知日期' : bucket.date.slice(5)}</small>
                  </div>
                );
              })}
            </div>
          </div>
          <p className="axis-caption">时间（UTC）</p>
        </div>
      )}
    </article>
  );
}
