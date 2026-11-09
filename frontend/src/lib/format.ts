import type { Metric, Turn } from '../types';

export const turnName = (turn: Turn) =>
  turn.title || (turn.sequence ? `第 ${turn.sequence} 轮 · 无可读 Prompt` : '无可读 Prompt 的轮次');
export const sessionName = (session: { title?: string | null; parent_id?: string | null }) =>
  session.title || (session.parent_id ? '子代理会话' : '未命名会话');
export const projectName = (project: { project?: string | null; project_name?: string | null }) =>
  project.project_name ||
  project.project
    ?.replace(/[\\/]+$/, '')
    .split(/[\\/]/)
    .pop() ||
  project.project ||
  '未知工作区';

export const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
export const formatNumber = (n: Metric | undefined) =>
  n == null ? '未知' : n.toLocaleString('zh-CN', { maximumFractionDigits: 1 });
export const formatPercent = (n: Metric | undefined) =>
  n == null ? '未知' : `${(n * 100).toFixed(1)}%`;
export const validCachePair = (input: Metric | undefined, cached: Metric | undefined) =>
  input != null &&
  cached != null &&
  Number.isInteger(input) &&
  Number.isInteger(cached) &&
  input >= 0 &&
  cached >= 0 &&
  cached <= input;
export const cacheRate = (input: Metric | undefined, cached: Metric | undefined): Metric =>
  validCachePair(input, cached) && input! > 0 ? cached! / input! : null;
export const uncachedInput = (input: Metric | undefined, cached: Metric | undefined): Metric =>
  validCachePair(input, cached) ? input! - cached! : null;
export const formatCost = (n: Metric | undefined) => {
  if (n == null || !Number.isFinite(n)) return '未知';
  if (n === 0) return '$0';
  if (n > 0 && n < 0.0001) return '<$0.0001';
  return `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: n < 0.01 ? 4 : 2 })}`;
};
export const compactNumber = (n: Metric | undefined) =>
  n == null
    ? '未知'
    : n >= 1e6
      ? `${(n / 1e6).toFixed(1)}M`
      : n >= 1e3
        ? `${(n / 1e3).toFixed(1)}k`
        : formatNumber(n);
export const formatDate = (value: string | null | undefined) =>
  value ? new Date(value).toLocaleString('zh-CN', { timeZone, hour12: false }) : '未知';
export const attributionLabel = (value: string) =>
  ({
    native: '原生轮次身份',
    explicit: '事件明确提供',
    inferred: '推断归组',
    unassigned: '未归属',
    'inferred turn_context': '依据轮次上下文推断',
    'inferred usage via turn_context': '用量依据轮次上下文推断',
  })[value] ||
  value ||
  '未知';
export const statusLabel = (value: string) =>
  ({ unknown: '状态未知', completed: '已结束', in_progress: '进行中', interrupted: '已中断' })[
    value
  ] || value;
