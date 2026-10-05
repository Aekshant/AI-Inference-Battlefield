import { GW, get, post, type Backend, type GatewayState, type Point } from "../api";
import { Bar, Kpi, TrafficChart, useAction, usePoll } from "../components";

function sum(points: Point[], k: keyof Point) {
  return points.reduce((a, p) => a + (p[k] as number), 0);
}

export default function Battlefield() {
  const { data: st, error } = usePoll(() => get<GatewayState>(`${GW}/admin/state`), 1000);
  const { data: series } = usePoll(() => get<Point[]>(`${GW}/admin/stats?seconds=120`), 1000);
  const { data: events } = usePoll(() => get<{ ts: string; msg: string }[]>(`${GW}/admin/events?limit=60`), 2000);

  const last = (series ?? []).slice(-10);
  const total = sum(last, "rps");
  const pct = (k: keyof Point) => (total ? (100 * sum(last, k)) / total : 0);
  const p99 = Math.max(0, ...last.map((p) => p.p99_ms));
  const ttft = Math.max(0, ...last.map((p) => p.ttft_p99_ms));

  return (
    <div className="grid">
      {error && <div className="err">gateway unreachable: {error}</div>}
      <div className="kpis">
        <Kpi label="Requests / s (10s)" value={(total / 10).toFixed(0)} />
        <Kpi label="Success" value={`${pct("ok").toFixed(1)}%`} tone={pct("ok") > 95 || !total ? "ok" : pct("ok") > 70 ? "warn" : "bad"} />
        <Kpi label="Shed (fast fail)" value={`${pct("shed").toFixed(1)}%`} tone={pct("shed") > 20 ? "warn" : undefined} />
        <Kpi label="Errors" value={`${pct("errors").toFixed(1)}%`} tone={pct("errors") > 1 ? "bad" : "ok"} />
        <Kpi label="p99 latency" value={`${(p99 / 1000).toFixed(2)}s`} tone={p99 > 3000 ? "bad" : p99 > 1500 ? "warn" : "ok"} />
        <Kpi label="p99 TTFT" value={`${(ttft / 1000).toFixed(2)}s`} tone={ttft > 1500 ? "bad" : ttft > 600 ? "warn" : "ok"} />
      </div>

      <div className="panel">
        <h2>Traffic — last 2 minutes</h2>
        <TrafficChart points={series ?? []} />
      </div>

      <div className="panel">
        <div className="row" style={{ marginBottom: 8 }}>
          <h2 style={{ margin: 0 }}>GPU fleet</h2>
          <span className="spacer" />
          {st && (
            <span className="muted">
              routing <code>{String(st.defenses.routing)}</code> · canary {(st.canary_weight * 100).toFixed(0)}% ·
              gateway in-flight {st.admission.inflight} · queued {st.admission.queued}
            </span>
          )}
        </div>
        <div className="grid g4">
          {(st?.backends ?? []).map((b) => <GpuCard key={b.addr} b={b} />)}
        </div>
      </div>

      <div className="panel">
        <h2>Gateway events</h2>
        <div className="feed">
          {[...(events ?? [])].reverse().map((e, i) => (
            <div key={i}><span className="muted">{e.ts.slice(11, 19)}</span> {e.msg}</div>
          ))}
        </div>
      </div>
    </div>
  );
}

function GpuCard({ b }: { b: Backend }) {
  const s = b.state;
  const ttft = Math.max(s.ttft_ewma_ms ?? 0, b.gw_ttft_ewma_ms ?? 0);
  const ejected = b.manual_eject || b.outlier_eject;
  const { busy, msg, run } = useAction();
  const chaos = (body: object) => run("chaos", () => post(`${GW}/admin/chaos/${b.name}`, body));
  const status = b.gone ? <span className="pill bad">draining (pod gone)</span>
    : !b.healthy ? <span className="pill bad">down</span>
    : b.breaker !== "closed" ? <span className="pill warn">breaker {b.breaker}</span>
    : ejected ? <span className="pill warn">{b.outlier_eject ? "outlier-ejected" : "ejected"}</span>
    : <span className="pill ok">serving</span>;
  return (
    <div className={`panel gpu ${!b.healthy ? "down" : ""} ${ejected ? "ejected" : ""}`}>
      <div className="row">
        <h3 style={{ margin: 0 }}>{b.name}</h3>
        <span className="spacer" />
        {status}
      </div>
      <div className="muted" style={{ fontSize: 12 }}>{b.pool} · model {s.version || "?"} · weight {b.weight}</div>
      <dl>
        <dt>in-flight / slots</dt><dd>{b.inflight} / {s.slots ?? "?"}</dd>
        <dt>GPU queue</dt><dd className={s.queue_depth > (s.slots ?? 32) ? "bad" : ""}>{s.queue_depth ?? 0}</dd>
        <dt>TTFT (EWMA)</dt><dd className={ttft > 1000 ? "bad" : ttft > 400 ? "warn" : ""}>{ttft.toFixed(0)} ms</dd>
        <dt>tokens / s</dt><dd>{(s.tokens_per_sec ?? 0).toFixed(0)}</dd>
        <dt>KV cache</dt><dd>{((s.kv_util ?? 0) * 100).toFixed(0)}%</dd>
      </dl>
      <Bar v={s.kv_util ?? 0} />
      <div className="row" style={{ marginTop: 10 }}>
        <button className="btn small" disabled={busy} title="+4s time to first token" onClick={() => chaos({ slow_ttft_ms: 4000 })}>slow</button>
        <button className="btn small" disabled={busy} title="shrink KV cache to 6k tokens" onClick={() => chaos({ kv_budget: 6000 })}>squeeze KV</button>
        <button className="btn small" disabled={busy} title="50% of requests fail" onClick={() => chaos({ error_rate: 0.5 })}>errors</button>
        <button className="btn small danger" disabled={busy} title="process exits; Kubernetes restarts it" onClick={() => run("crash", () => post(`${GW}/admin/chaos/${b.name}/crash`))}>crash</button>
        <button className="btn small" disabled={busy} onClick={() => run("reset", () => post(`${GW}/admin/chaos/${b.name}/reset`))}>heal</button>
      </div>
      <div className="row" style={{ marginTop: 6 }}>
        {b.manual_eject
          ? <button className="btn small good" disabled={busy} onClick={() => run("restore", () => post(`${GW}/admin/backends/${b.name}/restore`))}>restore</button>
          : <button className="btn small" disabled={busy} onClick={() => run("eject", () => post(`${GW}/admin/backends/${b.name}/eject`))}>eject</button>}
        <button className="btn small" disabled={busy} onClick={() => run("weight", () => post(`${GW}/admin/backends/${b.name}/weight`, { weight: b.weight >= 1 ? 0.3 : 1 }))}>
          {b.weight >= 1 ? "weight → 0.3" : "weight → 1"}
        </button>
      </div>
      {msg && <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>{msg}</div>}
    </div>
  );
}
