import { useState } from "react";
import { AGENT, get, post } from "../api";
import { useAction, usePoll } from "../components";

type Step = { ts: number; step: string; text: string; data?: unknown };
type Proposal = { action: string; args: Record<string, unknown>; rationale?: string; expected_effect?: string; risk?: string; fallback?: boolean };
type Diagnosis = { root_cause: string; evidence?: string[]; blast_radius?: string; confidence?: string; fallback?: boolean };
type Attempt = { action: string; args: Record<string, unknown>; verdict: Record<string, number | boolean> };
type IncidentSummary = { id: string; question: string; source: string; status: string; created: number; root_cause?: string; action?: string };
type IncidentDetail = IncidentSummary & {
  state: { timeline?: Step[]; diagnosis?: Diagnosis; proposal?: Proposal; attempts?: Attempt[]; postmortem?: string; status?: string; digest?: { facts: string[] } };
};

const QUESTIONS = [
  "Why did p99 TTFT jump 5x?",
  "GPU utilization looks fine but users are timing out. Why?",
  "One customer says they are being throttled. What is going on?",
  "Production is broken.",
];

const tone = (s: string) => (s === "resolved" ? "ok" : s === "awaiting_approval" || s === "mitigated" ? "warn" : ["unresolved", "error", "rejected"].includes(s) ? "bad" : "");

export default function Sre() {
  const { data: incidents, error } = usePoll(() => get<IncidentSummary[]>(`${AGENT}/incidents`), 2000);
  const { data: health } = usePoll(() => get<{ model: string; auto_watch: boolean }>(`${AGENT}/health`), 10000);
  const [sel, setSel] = useState<string | null>(null);
  const [q, setQ] = useState(QUESTIONS[0]);
  const { busy, msg, run } = useAction();
  const current = sel ?? incidents?.[0]?.id ?? null;

  return (
    <div className="grid g2" style={{ gridTemplateColumns: "minmax(280px, 1fr) minmax(0, 2fr)" }}>
      <div className="grid" style={{ alignContent: "start" }}>
        <div className="panel">
          <h2>Page the AI SRE</h2>
          {error && <div className="err">agent offline — run <code>make agent</code></div>}
          {health && <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>model <code>{health.model}</code> (local Ollama) · auto-watch {health.auto_watch ? "on" : "off"}</div>}
          <textarea value={q} onChange={(e) => setQ(e.target.value)} />
          <div className="row" style={{ marginTop: 6 }}>
            {QUESTIONS.map((x) => <button key={x} className="btn small" onClick={() => setQ(x)}>{x.slice(0, 26)}…</button>)}
          </div>
          <button className="btn primary" style={{ marginTop: 8 }} disabled={busy}
            onClick={() => run("incident opened", async () => { const r = await post<IncidentSummary>(`${AGENT}/incidents`, { question: q }); setSel(r.id); })}>
            open incident
          </button>
          {msg && <div className="muted">{msg}</div>}
        </div>
        <div className="panel">
          <h2>Incidents</h2>
          {(incidents ?? []).map((i) => (
            <div key={i.id} className="toggle" style={{ cursor: "pointer", background: i.id === current ? "var(--panel-2)" : undefined }} onClick={() => setSel(i.id)}>
              <div style={{ flex: 1 }}>
                <div className="row"><code>{i.id}</code><span className={`pill ${tone(i.status)}`}>{i.status}</span>{i.source === "watcher" && <span className="pill">auto</span>}</div>
                <small>{i.question}</small>
                {i.root_cause && <small>→ {i.root_cause}</small>}
              </div>
            </div>
          ))}
        </div>
      </div>
      {current ? <IncidentView id={current} /> : <div className="panel muted">No incidents yet.</div>}
    </div>
  );
}

function IncidentView({ id }: { id: string }) {
  const { data } = usePoll(() => get<IncidentDetail>(`${AGENT}/incidents/${id}`), 1500, [id]);
  const [note, setNote] = useState("");
  const { busy, msg, run } = useAction();
  if (!data) return <div className="panel">loading…</div>;
  const s = data.state;
  const p = s.proposal;
  const decide = (approved: boolean) => run(approved ? "approved" : "rejected", () =>
    post(`${AGENT}/incidents/${id}/decision`, { approved, note, by: "console" }).then(() => setNote("")));

  return (
    <div className="grid" style={{ alignContent: "start" }}>
      <div className="panel">
        <div className="row"><h3 style={{ margin: 0 }}>{data.question}</h3><span className="spacer" /><span className={`pill ${tone(data.status)}`}>{data.status}</span></div>
        {s.diagnosis && (
          <div style={{ marginTop: 10 }}>
            <h2>Diagnosis {s.diagnosis.fallback && <span className="pill warn">rule-based fallback</span>} <span className="pill">{s.diagnosis.confidence} confidence</span></h2>
            <div>{s.diagnosis.root_cause}</div>
            <ul>{(s.diagnosis.evidence ?? []).map((e, i) => <li key={i} className="muted">{e}</li>)}</ul>
            {s.diagnosis.blast_radius && <div className="muted">blast radius: {s.diagnosis.blast_radius}</div>}
          </div>
        )}
      </div>

      {p && data.status === "awaiting_approval" && (
        <div className="panel proposal">
          <h2>Proposed action — needs a human</h2>
          <div className="row"><code style={{ fontSize: 15 }}>{p.action}({Object.entries(p.args).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ")})</code><span className="pill">{p.risk} risk</span>{p.fallback && <span className="pill warn">rule-based</span>}</div>
          <p>{p.rationale}</p>
          {p.expected_effect && <p className="muted">expected: {p.expected_effect}</p>}
          <input type="text" placeholder="optional note (a rejection with a note makes the agent re-propose)" value={note} onChange={(e) => setNote(e.target.value)} style={{ width: "100%", marginBottom: 8 }} />
          <div className="row">
            <button className="btn good" disabled={busy} onClick={() => decide(true)}>approve & execute</button>
            <button className="btn danger" disabled={busy} onClick={() => decide(false)}>reject</button>
          </div>
          {msg && <div className="muted">{msg}</div>}
        </div>
      )}

      {(s.attempts ?? []).length > 0 && (
        <div className="panel scroll">
          <h2>Actions & verification (judged on user SLOs)</h2>
          <table>
            <thead><tr><th>action</th><th>errors</th><th>p99 TTFT</th><th>shed</th><th>verdict</th></tr></thead>
            <tbody>
              {s.attempts!.map((a, i) => (
                <tr key={i}>
                  <td><code>{a.action} {JSON.stringify(a.args)}</code></td>
                  <td>{pct(a.verdict.errors_before)} → {pct(a.verdict.errors_after)}</td>
                  <td>{a.verdict.ttft_p99_before}s → {a.verdict.ttft_p99_after}s</td>
                  <td>{pct(a.verdict.shed_before)} → {pct(a.verdict.shed_after)}</td>
                  <td className={a.verdict.within_slo ? "ok" : a.verdict.improved ? "warn" : "bad"}>{a.verdict.within_slo ? "SLO met" : a.verdict.improved ? "better, SLO breached" : "not better"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {s.postmortem && (
        <div className="panel">
          <h2>Postmortem (drafted by the agent)</h2>
          <div className="md">{s.postmortem.replace(/^```(markdown)?\n?|```$/gm, "")}</div>
        </div>
      )}

      <div className="panel">
        <h2>Timeline</h2>
        <div className="timeline">
          {(s.timeline ?? []).map((e, i) => (
            <div className="step" key={i}>
              <b>{e.step}</b> <span className="muted">{new Date(e.ts * 1000).toLocaleTimeString()}</span>
              <div>{e.text}</div>
              {e.step === "tool" && e.data != null && <details><summary className="muted">result</summary><pre className="wrap">{String(e.data)}</pre></details>}
              {e.step === "triage" && e.data != null && <details><summary className="muted">evidence digest</summary><pre className="wrap">{JSON.stringify(e.data, null, 2)}</pre></details>}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function pct(v: number | boolean) {
  return typeof v === "number" ? `${(v * 100).toFixed(1)}%` : String(v);
}
