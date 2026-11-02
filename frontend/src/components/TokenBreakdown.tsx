import type { Overview } from '../types';
import { compactNumber, formatNumber } from '../lib/format';

export function TokenBreakdown({ data }: { data: Overview }) {
  const { summary, turns } = data;
  const classified = turns.filter(
    (t) =>
      t.input_tokens != null &&
      t.output_tokens != null &&
      t.token_total === t.input_tokens + t.output_tokens,
  );
  const input = classified.reduce((n, t) => n + (t.input_tokens ?? 0), 0);
  const output = classified.reduce((n, t) => n + (t.output_tokens ?? 0), 0);
  const available = classified.length > 0 && !summary.aggregation_blocked;
  const total = input + output;
  const fraction = total > 0 ? input / total : 0;
  const angle = fraction * Math.PI * 2 - Math.PI / 2;
  const point = `${60 + 60 * Math.cos(angle)},${60 + 60 * Math.sin(angle)}`;
  const sector = `M60,60 L60,0 A60,60 0 ${fraction > 0.5 ? 1 : 0},1 ${point} Z`;
  return (
    <article className="panel summary-card">
      <h3>累计 Token</h3>
      <div className="token-layout">
        <div className="token-amount">
          <strong className="accent" title={formatNumber(summary.token_known)}>
            {compactNumber(summary.token_known)}
          </strong>
          <p>{summary.token_total == null ? '已知小计' : '完整合计'}</p>
          <small>
            用量覆盖 {summary.token_samples}/{summary.turns} 轮
          </small>
        </div>
        <svg
          className="pie"
          viewBox="0 0 120 120"
          role="img"
          aria-label={
            available
              ? `可拆分 ${classified.length} 轮，输入 ${input}，输出 ${output} Token`
              : '分类构成未知'
          }
        >
          <circle cx="60" cy="60" r="60" fill={available && total > 0 ? '#8c806b' : '#ded9cf'} />
          {available &&
            total > 0 &&
            (fraction === 1 ? (
              <circle cx="60" cy="60" r="60" fill="#3c6dbb" />
            ) : fraction > 0 ? (
              <path d={sector} fill="#3c6dbb" />
            ) : null)}
        </svg>
        <div className="legend">
          <div className="legend-row">
            <i className="swatch" />
            <span>输入</span>
            <span className="legend-number">{available ? compactNumber(input) : '未知'}</span>
          </div>
          <div className="legend-row">
            <i className="swatch output" />
            <span>输出</span>
            <span className="legend-number">{available ? compactNumber(output) : '未知'}</span>
          </div>
          <small>
            {summary.aggregation_blocked
              ? '父子分类未合并'
              : `分类覆盖 ${classified.length}/${turns.length} 轮`}
          </small>
        </div>
      </div>
      <p className="card-note">
        {summary.aggregation_blocked
          ? '父子包含关系未知，联合用量不可计算。'
          : '饼图仅含分类完整样本；缓存与推理不重复相加。'}
      </p>
    </article>
  );
}
