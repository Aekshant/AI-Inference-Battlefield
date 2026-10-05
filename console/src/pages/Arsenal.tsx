import { useState } from "react";
import { ATK, GW, get, post, type GatewayState, type Run } from "../api";
import { useAction, usePoll } from "../components";

const SCENARIOS: [string, string][] = [
  ["baseline", "80 req/s steady — what normal looks like"],
  ["spike", "1000 req/s — Attack #1"],
  ["ramp", "80 → 1000 req/s over the run"],
  ["long_prompts", "60 req/s + 12 req/s of 6k-token prompts — Attack #3"],
  ["noisy_tenant", "batch-jobs floods 500 req/s next to acme/globex"],
  ["mixed", "three tenants, some streaming"],
];

const DEFENSES: [string, string, string][] = [
  ["admission", "Admission control", "token bucket + max in-flight + bounded queue with deadline"],
  ["backpressure", "Backpressure", "shed instantly when every GPU is saturated"],
  ["kv_aware", "KV-aware routing", "skip GPUs whose KV cache can't fit the request; shed kv_full"],
  ["long_prompt_pool", "Long-prompt pool", "reserve one GPU for huge prompts"],
  ["outlier_ejection", "Outlier ejection", "eject GPUs with TTFT > 3x pool median"],
  ["health_checks", "Health checks", "skip GPUs that fail /state polls"],
  ["circuit_breaker", "Circuit breaker", "per-GPU closed → open → half-open"],
  ["retries", "Retries (budgeted)", "retry on another GPU before the first byte"],
  ["fallback", "Fallback", "cached or degraded answer instead of an error"],
  ["tenant_quotas", "Tenant quotas + fair queueing", "per-tenant limits, priorities, DRR"],
];

export default function Arsenal() {
  const { data: st } = usePoll(() => get<GatewayState>(`${GW}/admin/state`), 1500);
  const { data: runs } = usePoll(() => get<Run[]>(`${ATK}/attacks`), 1500);
  const [dur, setDur] = useState(60);
  const { busy, msg, run } = useAction();
  const d = st?.defenses ?? {};
  const active = (runs ?? []).filter((r) => r.running);

  return (
    <div className="grid g2">
      <div className="grid">
        <div className="panel">
          <h2>Attacks</h2>
          <div className="row" style={{ marginBottom: 8 }}>
            <label>duration <input type="number" value={dur} min={10} max={600} style={{ width: 80 }} onChange={(e) => setDur(+e.target.value)} /> s</label>
            <span className="spacer" />
            <button className="btn danger" disabled={busy} onClick={() => run("stop", () => post(`${ATK}/attacks/stop`))}>stop all traffic</button>
          </div>
          {SCENARIOS.map(([id, desc]) => (
            <div className="toggle" key={id}>
              <button className="btn small primary" disabled={busy} onClick={() => run(id, () => post(`${ATK}/attacks`, { scenario: id, duration_s: dur }))}>launch</button>
              <div><code>{id}</code><small>{desc}</small></div>
            </div>
          ))}
          <h2 style={{ marginTop: 14 }}>Infrastructure chaos</h2>
          <div className="row">
            <button className="btn danger" disabled={busy} onClick={() => run("kill pod", () => post(`${ATK}/chaos/kill-pod`, { random: true }))}>kill random GPU pod</button>
            <button className="btn danger" disabled={busy} onClick={() => run("pod killer", () => post(`${ATK}/chaos/pod-killer`, { interval_s: 20, duration_s: dur }))}>pod killer (every 20s)</button>
            <button className="btn" disabled={busy} onClick={() => run("heal all", () => post(`${GW}/admin/chaos/all/reset`))}>heal all GPUs</button>
          </div>
          {msg && <div className="muted" style={{ marginTop: 8 }}>{msg}</div>}
        </div>

        <div className="panel">
          <h2>What users feel (client side)</h2>
          {active.length === 0 && <div className="muted">no traffic running</div>}
          {active.map((r) => (
            <div key={r.id} style={{ marginBottom: 10 }}>
              <div className="row"><b>{r.scenario}</b><span className="muted">{r.elapsed_s.toFixed(0)}s / {r.duration_s}s</span></div>
              <div className="mono">
                last 10s: sent {r.last10s.sent} · ok {r.last10s.ok} · shed {r.last10s.shed} · <span className={r.last10s.error ? "bad" : ""}>err {r.last10s.error}</span> · p50 {r.last10s.p50_ms}ms · p99 {r.last10s.p99_ms}ms
              </div>
              {r.tenants && Object.keys(r.tenants).length > 1 && (
                <table style={{ marginTop: 6 }}>
                  <thead><tr><th>tenant</th><th>ok</th><th>shed</th><th>error</th><th>p99</th></tr></thead>
                  <tbody>
                    {Object.entries(r.tenants).map(([t, v]) => {
                      const tot = (v.ok ?? 0) + (v.shed ?? 0) + (v.error ?? 0) + (v.fallback ?? 0) || 1;
                      return <tr key={t}><td>{t}</td><td>{((100 * (v.ok ?? 0)) / tot).toFixed(0)}%</td><td>{((100 * (v.shed ?? 0)) / tot).toFixed(0)}%</td><td>{((100 * (v.error ?? 0)) / tot).toFixed(0)}%</td><td>{v.p99_ms}ms</td></tr>;
                    })}
                  </tbody>
                </table>
              )}
            </div>
          ))}
        </div>
      </div>

      <div className="grid">
        <div className="panel">
          <div className="row" style={{ marginBottom: 8 }}>
            <h2 style={{ margin: 0 }}>Defenses</h2>
            <span className="spacer" />
            <button className="btn small" disabled={busy} onClick={() => run("naive", () => post(`${GW}/admin/defenses/preset/naive`))}>all off (naive)</button>
            <button className="btn small good" disabled={busy} onClick={() => run("hardened", () => post(`${GW}/admin/defenses/preset/hardened`))}>all on (hardened)</button>
          </div>
          <div className="toggle">
            <span style={{ minWidth: 16 }} />
            <div style={{ flex: 1 }}>Routing <small>how a GPU is chosen</small></div>
            <select value={String(d.routing ?? "round_robin")} onChange={(e) => run("routing", () => post(`${GW}/admin/defenses`, { routing: e.target.value }))}>
              {["round_robin", "least_loaded", "p2c", "prefix"].map((r) => <option key={r}>{r}</option>)}
            </select>
          </div>
          {DEFENSES.map(([k, label, desc]) => (
            <label className="toggle" key={k}>
              <input type="checkbox" checked={!!d[k]} onChange={(e) => run(k, () => post(`${GW}/admin/defenses`, { [k]: e.target.checked }))} />
              <div>{label}<small>{desc}</small></div>
            </label>
          ))}
          <div className="row muted" style={{ marginTop: 8, fontSize: 12 }}>
            rate {String(d.rate_limit)}/s · max in-flight {String(d.max_inflight)} · queue {String(d.max_queue)} · queue timeout {String(d.queue_timeout_ms)}ms · attempt timeout {String(d.attempt_timeout_ms)}ms
          </div>
        </div>

        <div className="panel scroll">
          <h2>Tenants</h2>
          <table>
            <thead><tr><th>tenant</th><th>rate /s</th><th>weight</th><th>priority</th><th>queued</th></tr></thead>
            <tbody>
              {Object.values(st?.tenants ?? {}).map((t) => (
                <tr key={t.name}>
                  <td>{t.name}</td><td>{t.rate}</td><td>{t.weight}</td>
                  <td>{["interactive", "standard", "batch"][t.priority] ?? t.priority}</td>
                  <td>{st?.admission.queued_by_tenant?.[t.name] ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
