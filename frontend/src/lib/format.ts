import type { Metric } from '../types';

export const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
export const formatNumber = (n: Metric | undefined) =>
  n == null ? '未知' : n.toLocaleString('zh-CN', { maximumFractionDigits: 1 });
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
