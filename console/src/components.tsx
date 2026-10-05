import { useEffect, useRef, useState } from "react";
import type { Point } from "./api";

// Poll a loader every `ms`; keeps the last good value and surfaces errors.
export function usePoll<T>(loader: () => Promise<T>, ms: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef(loader);
  ref.current = loader;
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const v = await ref.current();
        if (alive) { setData(v); setError(null); }
      } catch (e) {
        if (alive) setError(String(e));
      }
    };
    tick();
    const id = setInterval(tick, ms);
    return () => { alive = false; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error };
}

export function Kpi({ label, value, tone }: { label: string; value: string; tone?: "ok" | "warn" | "bad" }) {
  return (
    <div className="panel kpi">
      <div className="label">{label}</div>
      <div className={`value ${tone ?? ""}`}>{value}</div>
    </div>
  );
}

export function Bar({ v, tone }: { v: number; tone?: string }) {
  const color = tone ?? (v > 0.9 ? "var(--bad)" : v > 0.7 ? "var(--warn)" : "var(--accent)");
  return <div className="bar"><i style={{ width: `${Math.min(100, Math.max(0, v * 100))}%`, background: color }} /></div>;
}

// Stacked outcome bars per second + p99 latency line on a second axis.
export function TrafficChart({ points, height = 180 }: { points: Point[]; height?: number }) {
  const w = 800, h = height, pad = 28;
  const maxRps = Math.max(10, ...points.map((p) => p.rps));
  const maxLat = Math.max(1000, ...points.map((p) => p.p99_ms));
  const bw = (w - pad * 2) / Math.max(1, points.length);
  const y = (v: number) => h - pad - (v / maxRps) * (h - pad * 2);
  const yl = (v: number) => h - pad - (v / maxLat) * (h - pad * 2);
  const series: [keyof Point, string][] = [["ok", "var(--ok)"], ["fallback", "var(--warn)"], ["shed", "var(--shed)"], ["errors", "var(--bad)"]];
  const line = points.map((p, i) => `${i ? "L" : "M"}${pad + i * bw + bw / 2},${yl(p.p99_ms)}`).join(" ");
  return (
    <div>
      <svg viewBox={`0 0 ${w} ${h}`} width="100%" role="img" aria-label="requests per second by outcome and p99 latency">
        <line x1={pad} x2={w - pad} y1={h - pad} y2={h - pad} stroke="var(--border)" />
        <text x={2} y={pad} fontSize="10" fill="var(--muted)">{maxRps.toFixed(0)}/s</text>
        <text x={w - pad + 2} y={pad} fontSize="10" fill="var(--muted)">{(maxLat / 1000).toFixed(1)}s</text>
        {points.map((p, i) => {
          let acc = 0;
          return series.map(([k, c]) => {
            const v = p[k] as number;
            if (!v) return null;
            const y0 = y(acc), y1 = y(acc + v);
            acc += v;
            return <rect key={`${i}${k}`} x={pad + i * bw} y={y1} width={Math.max(1, bw - 1)} height={y0 - y1} fill={c} opacity={0.85} />;
          });
        })}
        <path d={line} fill="none" stroke="var(--text)" strokeWidth={1.5} />
      </svg>
      <div className="legend">
        <span><i style={{ background: "var(--ok)" }} />ok</span>
        <span><i style={{ background: "var(--shed)" }} />shed</span>
        <span><i style={{ background: "var(--bad)" }} />errors</span>
        <span><i style={{ background: "var(--warn)" }} />fallback</span>
        <span><i style={{ background: "var(--text)", height: 2 }} />p99 latency</span>
      </div>
    </div>
  );
}

export function useAction() {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const run = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(true); setMsg(null);
    try { await fn(); setMsg(`✓ ${label}`); } catch (e) { setMsg(`✗ ${label}: ${e}`); }
    setBusy(false);
  };
  return { busy, msg, run };
}
