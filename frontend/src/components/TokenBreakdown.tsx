import type { Overview } from '../types';
import { compactNumber, formatNumber } from '../lib/format';
import { tokenParts } from '../lib/overview';
import { BreakdownPie } from './BreakdownPie';

const colors = { uncached: '#3c6dbb', cached: '#b8c2d1', write: '#8d9bad', output: '#8c806b' };

export function TokenBreakdown({ data }: { data: Overview }) {
  const { summary, turns } = data;
  const observed = summary.aggregation_blocked;
  const known = observed
    ? (summary.observed_token_known ?? summary.token_known)
    : summary.token_known;
  const parts = turns.map(tokenParts).filter((part) => part != null);
  const uncached = parts.reduce((sum, part) => sum + part.uncached, 0);
  const cached = parts.reduce((sum, part) => sum + part.cached, 0);
  const output = parts.reduce((sum, part) => sum + part.output, 0);
  const writeSamples = parts.filter((part) => part.write != null);
  const write = writeSamples.reduce((sum, part) => sum + (part.write ?? 0), 0);
  const total = parts.reduce((sum, part) => sum + part.total, 0);
  const writeMissing = writeSamples.length < parts.length;
  const segments = [
    { label: '未缓存输入', value: uncached, color: colors.uncached, available: parts.length > 0 },
    { label: '缓存读取', value: cached, color: colors.cached, available: parts.length > 0 },
    {
      label: writeMissing ? '已知缓存写入' : '缓存写入',
      value: write,
      color: colors.write,
      available: writeSamples.length > 0,
    },
    { label: '输出', value: output, color: colors.output, available: parts.length > 0 },
  ];
  const ariaLabel = parts.length
    ? `${parts.length} 轮 Token 构成：未缓存输入 ${uncached}，缓存读取 ${cached}，${writeSamples.length ? `已知缓存写入 ${write}` : '缓存写入未知'}，输出 ${output}${observed ? '；观测值未去重' : ''}`
    : 'Token 分类构成未知';
  return (
    <article className="panel summary-card token-summary">
      <h3>{observed ? '会话观测 Token' : '累计 Token'}</h3>
      <div className="breakdown-layout">
        <div className="breakdown-amount">
          <strong className="accent" title={formatNumber(known)}>
            {compactNumber(known)}
          </strong>
          <p>
            {summary.turns} 轮 · {summary.sessions} 个会话
          </p>
          <small>
            {observed
              ? '观测小计（未去重）'
              : summary.token_total == null
                ? '已知小计'
                : '完整合计'}
          </small>
        </div>
        <BreakdownPie className="breakdown-chart" segments={segments} label={ariaLabel} />
        <div className="breakdown-legend">
          {segments.map((segment) => (
            <div className="breakdown-row" key={segment.label}>
              <i className="swatch" style={{ background: segment.color }} />
              <span>{segment.label}</span>
              <span
                className="legend-number"
                title={formatNumber(segment.available ? segment.value : null)}
              >
                {segment.available ? compactNumber(segment.value) : '未知'}
                {segment.available && total > 0
                  ? ` · ${((segment.value / total) * 100).toFixed(1)}%`
                  : ''}
              </span>
            </div>
          ))}
        </div>
      </div>
      <p className="card-note">
        分类覆盖 {parts.length}/{turns.length} 轮{writeMissing && '；写入未知时保留在未缓存输入内'}
        。{observed && ' 观测小计可能重复。'}
      </p>
    </article>
  );
}
