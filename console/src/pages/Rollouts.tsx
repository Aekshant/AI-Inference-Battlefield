import { useState } from "react";
import { ATK, CTL, get, post } from "../api";
import { useAction, usePoll } from "../components";

type Eval = { accuracy: number; p50_ms: number; p99_ms: number; cost_per_1k_tokens: number; n: number; passed?: boolean; reasons?: string[] };
type Model = { version: string; pool: string; status: string; eval?: Eval };
type Rollout = {
  id: string; candidate: string; status: string; step: number; weight: number; steps: number[]; reason?: string;
  skip_eval: boolean; checks: { ts: string; weight: number; values: Record<string, number>; failed?: string[] }[];
};
type Ring = { devices: number; online: number; versions: Record<string, number>; online_error_rate: number };
type Fleet = { rings: Record<string, Ring>; desired: Record<string, string>; rollout: null | { version: string; status: string; ring: string; reason?: string } };

const statusTone = (s: string) => (s === "promoted" || s === "completed" || s === "serving" ? "ok" : s === "running" ? "warn" : "bad");

export default function Rollouts() {
  const { data: reg } = usePoll(() => get<Record<string, Model>>(`${CTL}/registry`), 3000);
  const { data: ro } = usePoll(() => get<Rollout>(`${CTL}/rollouts/current`).catch(() => null), 2000);
  const { data: fleet } = usePoll(() => get<Fleet>(`${CTL}/fleet`), 2000);
  const { data: events } = usePoll(() => get<{ ts: string; msg: string }[]>(`${CTL}/events`), 3000);
  const [cand, setCand] = useState("v2");
  const [skipEval, setSkipEval] = useState(false);
  const [edgeVersion, setEdgeVersion] = useState("edge-v2-bad");
  const { busy, msg, run } = useAction();

  return (
    <div className="grid g2">
      <div className="grid">
        <div className="panel scroll">
          <h2>Model registry · offline eval gate</h2>
          <table>
            <thead><tr><th>version</th><th>pool</th><th>status</th><th>accuracy</th><th>p99</th><th>$/1k tok</th><th>gate</th></tr></thead>
            <tbody>
              {Object.values(reg ?? {}).map((m) => (
                <tr key={m.version}>
                  <td><code>{m.version}</code></td><td>{m.pool}</td>
                  <td><span className={`pill ${statusTone(m.status)}`}>{m.status}</span></td>
                  <td>{m.eval ? (m.eval.accuracy * 100).toFixed(1) + "%" : "—"}</td>
                  <td>{m.eval ? m.eval.p99_ms.toFixed(0) + "ms" : "—"}</td>
                  <td>{m.eval ? m.eval.cost_per_1k_tokens.toFixed(3) : "—"}</td>
                  <td title={(m.eval?.reasons ?? []).join("\n")}>{m.eval?.passed === undefined ? "—" : m.eval.passed ? "pass" : "FAIL"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted" style={{ fontSize: 12 }}>Register evals with <code>make eval</code> (runs the golden set against v1 and v2).</p>
        </div>

        <div className="panel">
          <h2>Canary rollout</h2>
          <div className="row">
            <label>candidate <input type="text" value={cand} onChange={(e) => setCand(e.target.value)} style={{ width: 70 }} /></label>
            <label className="row"><input type="checkbox" checked={skipEval} onChange={(e) => setSkipEval(e.target.checked)} /> skip eval gate (YOLO)</label>
            <button className="btn primary" disabled={busy} onClick={() => run("rollout", () => post(`${CTL}/rollouts`, { candidate: cand, skip_eval: skipEval, step_seconds: 30 }))}>start</button>
            <button className="btn danger" disabled={busy} onClick={() => run("abort", () => post(`${CTL}/rollouts/current/abort`))}>abort</button>
          </div>
          <p className="muted" style={{ fontSize: 12 }}>Launch <code>baseline</code> traffic first — health gates need canary requests to judge.</p>
          {ro && (
            <div>
              <div className="row" style={{ margin: "8px 0" }}>
                <b>{ro.candidate}</b><span className={`pill ${statusTone(ro.status)}`}>{ro.status}</span>
                <span className="muted">canary {(ro.weight * 100).toFixed(0)}%</span>
              </div>
              <div className="row">
                {ro.steps.map((s, i) => (
                  <span key={i} className={`pill ${i < ro.step || ro.status === "promoted" ? "ok" : i === ro.step ? (ro.status === "running" ? "warn" : "bad") : ""}`}>{(s * 100).toFixed(0)}%</span>
                ))}
              </div>
              {ro.reason && <p className={ro.status === "promoted" ? "ok" : "bad"}>{ro.reason}</p>}
              {ro.checks.length > 0 && (() => {
                const c = ro.checks[ro.checks.length - 1].values;
                return <div className="mono muted">last check: canary reqs {c.canary_requests?.toFixed(0)} · p99 TTFT {c.canary_ttft_p99?.toFixed(3)}s vs {c.stable_ttft_p99?.toFixed(3)}s · err {(100 * (c.canary_err_ratio ?? 0)).toFixed(1)}% · $/tok ratio {c.stable_cost_tok ? (c.canary_cost_tok / c.stable_cost_tok).toFixed(2) : "—"}</div>;
              })()}
            </div>
          )}
          {msg && <div className="muted">{msg}</div>}
        </div>
      </div>

      <div className="grid">
        <div className="panel">
          <h2>Edge fleet · ring rollouts</h2>
          <div className="grid g3">
            {Object.entries(fleet?.rings ?? {}).map(([ring, r]) => (
              <div key={ring} className="panel" style={{ background: "var(--panel-2)" }}>
                <h3>{ring}</h3>
                <div>{r.online}/{r.devices} online {r.online < r.devices && <span className="pill warn">{r.devices - r.online} partitioned</span>}</div>
                <div>desired <code>{fleet?.desired[ring]}</code></div>
                <div className={r.online_error_rate > 0.05 ? "bad" : "muted"}>error rate {(r.online_error_rate * 100).toFixed(1)}%</div>
                <div className="mono" style={{ fontSize: 11 }}>{Object.entries(r.versions).map(([v, n]) => `${v}: ${n}`).join(" · ")}</div>
              </div>
            ))}
          </div>
          {fleet?.rollout && (
            <p>rollout <code>{fleet.rollout.version}</code> <span className={`pill ${statusTone(fleet.rollout.status)}`}>{fleet.rollout.status}</span> {fleet.rollout.ring && <>ring <b>{fleet.rollout.ring}</b></>} <span className="bad">{fleet.rollout.reason}</span></p>
          )}
          <div className="row" style={{ marginTop: 8 }}>
            <input type="text" value={edgeVersion} onChange={(e) => setEdgeVersion(e.target.value)} style={{ width: 130 }} />
            <button className="btn primary" disabled={busy} onClick={() => run("fleet rollout", () => post(`${CTL}/fleet/rollouts`, { version: edgeVersion, hold_s: 20 }))}>roll out to rings</button>
            <button className="btn" disabled={busy} onClick={() => run("fleet rollback", () => post(`${CTL}/fleet/rollback`, { version: "edge-v1" }))}>rollback all</button>
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            <button className="btn danger" disabled={busy} onClick={() => run("partition", () => post(`${ATK}/chaos/edge/partition`, { fraction: 0.4, duration_s: 60 }))}>partition 40% for 60s</button>
            <button className="btn danger" disabled={busy} onClick={() => run("bad network", () => post(`${ATK}/chaos/edge/network`, { latency_ms: 800, loss: 0.2 }))}>800ms + 20% loss</button>
            <button className="btn" disabled={busy} onClick={() => run("heal edge", () => post(`${ATK}/chaos/edge/reset`))}>heal network</button>
          </div>
        </div>
        <div className="panel">
          <h2>Controller events</h2>
          <div className="feed">
            {[...(events ?? [])].reverse().map((e, i) => <div key={i}><span className="muted">{e.ts.slice(11, 19)}</span> {e.msg}</div>)}
          </div>
        </div>
      </div>
    </div>
  );
}
