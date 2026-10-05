// Thin fetch helpers. Every backend is proxied same-origin by vite (see vite.config.ts).
export const GW = "/gw";
export const ATK = "/atk";
export const CTL = "/ctl";
export const AGENT = "/agent";

export async function get<T = any>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export async function post<T = any>(url: string, body: unknown = {}): Promise<T> {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await r.text();
  if (!r.ok) throw new Error(`${r.status} ${text}`);
  return text ? JSON.parse(text) : ({} as T);
}

export type WorkerState = {
  node: string; pool: string; version: string; slots: number; active: number; queue_depth: number;
  kv_used: number; kv_budget: number; kv_util: number; ttft_ewma_ms: number; tokens_per_sec: number;
  chaos: Record<string, number | boolean>;
};

export type Backend = {
  name: string; addr: string; pool: string; healthy: boolean; inflight: number; kv_inflight: number;
  state: WorkerState; gw_ttft_ewma_ms: number; gw_error_rate: number; weight: number;
  manual_eject: boolean; outlier_eject: boolean; gone?: boolean; breaker: string; score: number;
};

export type Defenses = Record<string, boolean | number | string>;

export type GatewayState = {
  defenses: Defenses; backends: Backend[]; canary_weight: number;
  admission: { inflight: number; queued: number; queued_by_tenant: Record<string, number> };
  tenants: Record<string, { name: string; rate: number; burst: number; weight: number; priority: number }>;
};

export type Point = {
  t: number; rps: number; ok: number; shed: number; errors: number; fallback: number;
  p50_ms: number; p99_ms: number; ttft_p99_ms: number; by_pool: Record<string, number>; by_node: Record<string, number>;
};

export type Run = {
  id: string; scenario: string; elapsed_s: number; duration_s: number; running: boolean;
  totals: Record<string, number>; last10s: Record<string, number>;
  tenants?: Record<string, Record<string, number>>;
  streams: Record<string, unknown>[];
};
