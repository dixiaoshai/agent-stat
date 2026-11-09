import { useMemo, useState } from 'react';
import type { Overview, Turn } from '../types';
import { compactNumber, formatCost, formatNumber, formatPercent } from '../lib/format';
import { knownValue, tokenParts, toolOutputBytes } from '../lib/overview';

type Metric = 'tokens' | 'cost' | 'cache' | 'tools';
type Segment = { label: string; value: number; color: string; className: string };
type ChartRow = { date: string; value: number | null; segments: Segment[]; label: string };

const blue = '#3c6dbb';
const cache = '#b8c2d1';
const output = '#8c806b';
const unknown = '#d6d1c7';

const specs: Record<Metric, { label: string; unit: string; description: string }> = {
  tokens: {
    label: 'Token 总量',
    unit: 'Token',
    description: '蓝：未缓存输入 · 灰蓝：缓存读取和已知写入 · 褐：输出 · 斜线：未拆分。',
  },
  cost: {
    label: '估算费用',
    unit: 'USD',
    description: 'API 标准短上下文单价估算。蓝：未缓存输入 · 灰蓝：缓存输入 · 褐：输出。',
  },
  cache: {
    label: '缓存输入',
    unit: 'Token',
    description: '缓存读取 Token 已知小计；缓存属于输入，不与 Token 总量重复相加。',
  },
  tools: {
    label: '工具输出字节',
    unit: '字节',
    description: '仅统计有工具且各工具输出大小均已观察到的轮次；字节数不换算为 Token。',
  },
};

const makeSegment = (label: string, value: number, color: string, className: string): Segment => ({
  label,
  value,
  color,
  className,
});

function tokenRow(bucket: Overview['trend'][number], turns: Turn[], observed: boolean): ChartRow {
  const value = observed ? (bucket.observed_token_known ?? bucket.token_known) : bucket.token_known;
  let uncached = 0;
  let cached = 0;
  let out = 0;
  let unclassified = 0;
  let writesMissing = false;
  for (const turn of turns) {
    if (!knownValue(turn.token_total)) continue;
    const parts = tokenParts(turn);
    if (!parts) {
      unclassified += turn.token_total;
      continue;
    }
    uncached += parts.uncached;
    cached += parts.cached + (parts.write ?? 0);
    out += parts.output;
    writesMissing ||= parts.write == null;
  }
  const total = uncached + cached + out + unclassified;
  // If a backend bucket cannot be reconciled with its turns, avoid inventing a split.
  if (value != null && Math.abs(value - total) > 0.000001) {
    uncached = 0;
    cached = 0;
    out = 0;
    unclassified = value;
  }
  const cacheUsage = bucket.cache_usage;
  const label = `${bucket.date} · Token ${formatNumber(value)}${observed ? '（观测小计，未去重）' : ''} · 有效 ${bucket.token_samples}/${bucket.turns} 轮 · 缓存输入 ${formatNumber(cacheUsage.cached_input_known)} · 占输入 ${formatPercent(cacheUsage.hit_rate)} · 缓存配对 ${cacheUsage.samples}/${cacheUsage.turns} 轮${unclassified ? ' · 含未拆分 Token' : ''}${writesMissing ? ' · 写入未知时归入未缓存输入' : ''}`;
  return {
    date: bucket.date,
    value,
    segments: [
      makeSegment('未缓存输入', uncached, blue, 'input'),
      makeSegment('缓存读取 / 已知写入', cached, cache, 'cached'),
      makeSegment('输出', out, output, 'output'),
      makeSegment('未拆分', unclassified, unknown, 'unclassified'),
    ],
    label,
  };
}

function costRow(bucket: Overview['trend'][number], turns: Turn[], observed: boolean): ChartRow {
  const usable = turns.filter((turn) => knownValue(turn.estimated_cost));
  const value = usable.length ? usable.reduce((sum, turn) => sum + turn.estimated_cost!, 0) : null;
  let uncached = 0;
  let cached = 0;
  let out = 0;
  let unclassified = 0;
  for (const turn of usable) {
    const amount = turn.estimated_cost!;
    const parts = turn.cost_breakdown;
    if (!parts || ![parts.input, parts.cached_input, parts.output].every(knownValue)) {
      unclassified += amount;
      continue;
    }
    const subtotal = parts.input + parts.cached_input + parts.output;
    if (Math.abs(subtotal - amount) > 0.00000001 || (subtotal === 0 && amount !== 0)) {
      unclassified += amount;
      continue;
    }
    // Source components and total are independently rounded to 12 decimal places.
    const scale = subtotal ? amount / subtotal : 1;
    uncached += parts.input * scale;
    cached += parts.cached_input * scale;
    out += parts.output * scale;
  }
  const label = `${bucket.date} · 估算费用 ${formatCost(value)}${observed ? '（观测小计，未去重）' : ''} · 可估算 ${usable.length}/${bucket.turns} 轮 · 非实际账单${unclassified ? ' · 含未拆分估算费用' : ''}`;
  return {
    date: bucket.date,
    value,
    segments: [
      makeSegment('未缓存输入', uncached, blue, 'input'),
      makeSegment('缓存输入', cached, cache, 'cached'),
      makeSegment('输出', out, output, 'output'),
      makeSegment('未拆分', unclassified, unknown, 'unclassified'),
    ],
    label,
  };
}

function cacheRow(bucket: Overview['trend'][number], observed: boolean): ChartRow {
  const c = bucket.cache_usage;
  const value = c.cached_input_known;
  return {
    date: bucket.date,
    value,
    segments: [makeSegment('缓存输入', value ?? 0, cache, 'cached')],
    label: `${bucket.date} · 缓存输入 ${formatNumber(value)}${observed ? '（观测小计，未去重）' : ''} · 占输入 ${formatPercent(c.hit_rate)} · 配对 ${c.samples}/${c.turns} 轮${!c.complete && c.samples ? ' · 已知小计' : ''}`,
  };
}

function toolsRow(bucket: Overview['trend'][number], turns: Turn[], observed: boolean): ChartRow {
  const known = turns.map(toolOutputBytes).filter(knownValue);
  const value = known.length ? known.reduce((sum, bytes) => sum + bytes, 0) : null;
  return {
    date: bucket.date,
    value,
    segments: [makeSegment('工具输出', value ?? 0, blue, 'input')],
    label: `${bucket.date} · 工具输出 ${formatNumber(value)} 字节${observed ? '（观测小计，未去重）' : ''} · 已知输出 ${known.length}/${bucket.turns} 轮${known.length && known.length < bucket.turns ? ' · 已知小计' : ''}`,
  };
}

export function TrendChart({ data }: { data: Overview }) {
  const [metric, setMetric] = useState<Metric>('tokens');
  const observed = data.summary.aggregation_blocked;
  const rows = useMemo(() => {
    const byDate = new Map<string, Turn[]>();
    for (const turn of data.turns) {
      const key = (turn.started_at ?? '未知').slice(0, 10);
      const group = byDate.get(key);
      if (group) group.push(turn);
      else byDate.set(key, [turn]);
    }
    return data.trend.map((bucket) => {
      const turns = byDate.get(bucket.date) ?? [];
      if (metric === 'cost') return costRow(bucket, turns, observed);
      if (metric === 'cache') return cacheRow(bucket, observed);
      if (metric === 'tools') return toolsRow(bucket, turns, observed);
      return tokenRow(bucket, turns, observed);
    });
  }, [data.trend, data.turns, metric, observed]);
  const max = Math.max(0, ...rows.map((row) => row.value ?? 0)) || 1;
  const spec = specs[metric];
  const format = metric === 'cost' ? formatCost : compactNumber;
  return (
    <article className="panel trend-panel">
      <div className="panel-head">
        <h3>趋势</h3>
        <label>
          指标{' '}
          <select
            aria-label="趋势指标"
            value={metric}
            onChange={(e) => setMetric(e.target.value as Metric)}
          >
            {Object.entries(specs).map(([key, item]) => (
              <option key={key} value={key}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="chart-description">
        按轮次开始时间归入 UTC 日期。{spec.description}
        {metric === 'tokens' && ' 写入未知时保留在未缓存输入内。'}
        {observed && ' 观测小计未去重，可能包含重叠用量。'}
      </p>
      {rows.length === 0 ? (
        <div className="empty">当前范围暂无活动，请调整时间或筛选条件。</div>
      ) : (
        <div
          className="chart"
          role="group"
          aria-label={`每日${spec.label}趋势，可聚焦或悬停查看详情`}
        >
          <div className="plot">
            <div className="plot-lines" aria-hidden="true">
              {[max, max / 2, 0].map((n, i) => (
                <div key={i}>
                  <span>{format(n)}</span>
                </div>
              ))}
            </div>
            <div className="bars">
              {rows.map((row) => (
                <div
                  className="bar-col"
                  key={row.date}
                  title={row.label}
                  role="img"
                  aria-label={row.label}
                  tabIndex={0}
                >
                  <div
                    className={`bar-stack${row.value == null ? ' unknown' : ''}`}
                    style={{ height: `${row.value == null ? 1 : (100 * row.value) / max}%` }}
                    aria-hidden="true"
                  >
                    {row.segments.map((segment) => (
                      <span
                        className={`segment ${segment.className}`}
                        key={segment.label}
                        style={{
                          height: `${row.value ? (100 * segment.value) / row.value : 0}%`,
                          background:
                            segment.className === 'unclassified'
                              ? `repeating-linear-gradient(45deg, ${unknown}, ${unknown} 3px, #f5f2ec 3px, #f5f2ec 6px)`
                              : segment.color,
                        }}
                      />
                    ))}
                  </div>
                  <small>{row.date === '未知' ? '未知日期' : row.date.slice(5)}</small>
                </div>
              ))}
            </div>
          </div>
          <p className="axis-caption">时间（UTC） · {spec.unit}</p>
        </div>
      )}
    </article>
  );
}
