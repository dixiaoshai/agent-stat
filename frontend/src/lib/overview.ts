import type { Metric, Turn } from '../types';

export const knownValue = (value: Metric | undefined): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0;
const count = (value: Metric | undefined): value is number =>
  knownValue(value) && Number.isInteger(value);

export function tokenParts(turn: Turn) {
  const input = turn.input_tokens;
  const cached = turn.cached_input_tokens;
  const output = turn.output_tokens;
  if (!count(input) || !count(cached) || !count(output) || cached > input) return null;
  if (turn.token_total !== input + output) return null;
  const write = turn.cache_write_input_tokens ?? null;
  if (write != null && (!count(write) || write > input - cached)) return null;
  return { uncached: input - cached - (write ?? 0), cached, write, output, total: input + output };
}

export function statistics(values: (Metric | undefined)[]) {
  const sorted = values.filter(knownValue).sort((a, b) => a - b);
  const percentile = (p: number): Metric => {
    if (!sorted.length) return null;
    const position = (sorted.length - 1) * p;
    const low = Math.floor(position);
    return sorted[low] + (sorted[Math.ceil(position)] - sorted[low]) * (position - low);
  };
  return {
    median: percentile(0.5),
    p90: percentile(0.9),
    p10: percentile(0.1),
    mean: sorted.length ? sorted.reduce((sum, value) => sum + value, 0) / sorted.length : null,
    count: sorted.length,
  };
}

export type ConcentrationMetric = 'cost' | 'tokens' | 'toolBytes';

export function toolOutputBytes(turn: Turn): Metric {
  if (!turn.tools.length || !turn.tools.every((tool) => count(tool.output_bytes))) return null;
  return turn.tools.reduce((total, tool) => total + (tool.output_bytes ?? 0), 0);
}

export function rankConcentration(turns: Turn[], metric: ConcentrationMetric) {
  const rows = turns
    .map((turn) => ({
      turn,
      value:
        metric === 'cost'
          ? turn.estimated_cost
          : metric === 'tokens'
            ? turn.token_total
            : toolOutputBytes(turn),
    }))
    .filter((row): row is { turn: Turn; value: number } => knownValue(row.value))
    .sort((a, b) => b.value - a.value || a.turn.id.localeCompare(b.turn.id));
  const total = rows.length ? rows.reduce((sum, row) => sum + row.value, 0) : null;
  const selected = rows.slice(0, Math.ceil(rows.length * 0.1));
  return {
    rows: selected,
    samples: rows.length,
    total,
    share:
      total != null && total > 0 ? selected.reduce((sum, row) => sum + row.value, 0) / total : null,
  };
}
