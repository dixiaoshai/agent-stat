export type Metric = number | null;
export interface CacheUsage {
  cached_input_known: Metric;
  uncached_input_known: Metric;
  input_known: Metric;
  hit_rate: Metric;
  samples: number;
  turns: number;
  complete: boolean;
  aggregation_blocked: boolean;
  cache_write_known: Metric;
  cache_write_samples: number;
}
export interface Filters {
  start?: string;
  end?: string;
  project?: string;
  model?: string;
  source?: string;
}
export interface ToolRecord {
  call_id: string;
  name: string;
  input_bytes: Metric;
  output_bytes: Metric;
  status: string;
  line: number;
  started_at: string | null;
  ended_at: string | null;
  attribution: string;
}
export interface Evidence {
  line: number;
  timestamp: string | null;
}
export interface Turn {
  id: string;
  native_id: string;
  parent_turn_id?: string | null;
  parent_session_id?: string | null;
  parent_turn_attribution?: string;
  title?: string | null;
  sequence?: number;
  session_id?: string;
  session_native_id?: string;
  session_title?: string | null;
  project?: string | null;
  project_name?: string | null;
  source?: string;
  started_at: string | null;
  ended_at: string | null;
  status: string;
  model: string;
  token_total: Metric;
  input_tokens: Metric;
  cached_input_tokens: Metric;
  cache_write_input_tokens?: Metric;
  output_tokens: Metric;
  reasoning_output_tokens: Metric;
  estimated_cost: Metric;
  estimated_cost_reason: string | null;
  cost_breakdown: { input: number; cached_input: number; output: number } | null;
  tools: ToolRecord[];
  evidence: Evidence[];
  attribution: string;
}
export interface Session {
  id: string;
  native_id?: string;
  title: string | null;
  project: string | null;
  project_name?: string | null;
  model: string;
  started_at: string | null;
  status: string;
  token_total: Metric;
  token_known: Metric;
  token_samples: number;
  cache_usage: CacheUsage;
  estimated_cost: Metric;
  estimated_cost_known: Metric;
  cost_samples: number;
  turns: number;
  tools: number;
  parent_id: string | null;
  source: string;
  instance: string;
}
export interface Summary {
  sessions: number;
  turns: number;
  token_total: Metric;
  token_known: Metric;
  observed_token_total?: Metric;
  observed_token_known?: Metric;
  token_samples: number;
  cache_usage: CacheUsage;
  median: Metric;
  p90: Metric;
  tool_calls: Metric;
  cost: Metric;
  estimated_cost: Metric;
  estimated_cost_known: Metric;
  observed_estimated_cost_known: Metric;
  cost_samples: number;
  aggregation_blocked: boolean;
}
export interface Pricing {
  currency: 'USD';
  checked_at: string;
  source_url: string;
  basis: string;
  assumptions: string[];
  models: Record<string, { input: number; cached_input: number; output: number }>;
}
export interface Bucket {
  turns: number;
  token_total: Metric;
  token_known: Metric;
  observed_token_total?: Metric;
  observed_token_known?: Metric;
  token_samples: number;
  cache_usage: CacheUsage;
}
export interface ImportReport {
  scanned: number;
  imported: number;
  skipped: number;
  errors: string[];
  warnings: string[];
  received_at: string;
}
export interface Overview {
  summary: Summary;
  pricing: Pricing;
  sessions: Session[];
  turns: Turn[];
  groups: (Bucket & { model: string })[];
  trend: (Bucket & { date: string })[];
  coverage: { token_turns: number; turns: number; percentile_method: string; time_basis: string };
  warnings: string[];
  filters: Filters;
  last_import: Partial<ImportReport>;
}
export interface SessionDetail {
  session: {
    id: string;
    title?: string | null;
    native_id?: string;
    project: string | null;
    project_name?: string | null;
    source_path?: string;
    parent_id?: string | null;
    source?: string;
    cache_usage: CacheUsage;
  };
  turns: Turn[];
  warnings: string[];
  filters: Filters;
}
export interface Capabilities {
  source: string;
  adapter_version: string;
  mode: string;
  items: { name: string; status: string; detail: string }[];
  documentation: string;
}
export interface Selection {
  sessionId: string;
  turnId?: string;
}
export type Page = 'overview' | 'sessions' | 'source';
