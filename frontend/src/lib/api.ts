import type { Capabilities, Filters, ImportReport, Overview, SessionDetail } from '../types';

function query(filters: Filters, extra: Record<string, string> = {}): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value);
  for (const [key, value] of Object.entries(extra)) params.set(key, value);
  return params.toString();
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    const message =
      body && typeof body === 'object' && 'error' in body && typeof body.error === 'string'
        ? body.error
        : `请求失败（HTTP ${response.status}）`;
    throw new Error(message);
  }
  if (body === null) throw new Error('服务器返回了无效响应');
  return body as T;
}

export const getOverview = (filters: Filters, signal?: AbortSignal) =>
  request<Overview>(`/api/overview?${query(filters)}`, { signal });

export const getSession = (id: string, filters: Filters, signal?: AbortSignal) =>
  request<SessionDetail>(`/api/session?${query(filters, { id })}`, { signal });

export const getCapabilities = (signal?: AbortSignal) =>
  request<Capabilities>('/api/capabilities', { signal });

export const refreshImport = (signal?: AbortSignal) =>
  request<ImportReport>('/api/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
    signal,
  });
