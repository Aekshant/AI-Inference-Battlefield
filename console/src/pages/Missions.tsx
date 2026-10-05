import { useState } from "react";
import { AGENT, ATK, CTL, GW, post } from "../api";
import { useAction } from "../components";

type Mission = {
  n: string; title: string; brief: string; win: string; weapons: string[];
  setup: () => Promise<unknown>;
};

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const naive = () => post(`${GW}/admin/defenses/preset/naive`);
const heal = () => post(`${GW}/admin/chaos/all/reset`);
const traffic = (scenario: string, duration_s = 300) => post(`${ATK}/attacks`, { scenario, duration_s });
const fresh = async () => { await post(`${ATK}/attacks/stop`); await heal(); await naive(); await post(`${GW}/admin/canary`, { weight: 0 }); };

const MISSIONS: Mission[] = [
  {
    n: "01", title: "Make inference work", brief: "Normal day. 80 req/s through the gateway to 3 GPUs.",
    win: "Battlefield shows ~100% success, p99 < 1s, traffic split evenly.", weapons: ["round robin"],
    setup: async () => { await fresh(); await traffic("baseline"); },
  },
  {
    n: "02", title: "🚨 Destroy it with traffic", brief: "1000 req/s arrives. Queues grow on every GPU until requests time out.",
    win: "Errors ≈ 0%. Excess load is shed fast (429/503) instead of timing out; p99 of admitted requests < 3s.",
    weapons: ["admission", "backpressure"], setup: async () => { await fresh(); await traffic("spike", 180); },
  },
  {
    n: "03", title: "🚨 One GPU becomes slow", brief: "Under normal traffic, one GPU's TTFT goes from 120ms to 4s. Round robin keeps feeding it.",
    win: "The slow GPU gets ~0% of traffic within seconds; errors ≈ 0%. Bonus: it gets traffic back after you heal it.",
    weapons: ["outlier_ejection", "routing=p2c", "health_checks"],
    setup: async () => { await fresh(); await traffic("baseline"); await sleep(10000); await post(`${GW}/admin/chaos/gpu-2`, { slow_ttft_ms: 4000 }); },
  },
  {
    n: "04", title: "🚨 KV cache explosion", brief: "6k-token prompts start arriving next to normal chat traffic. KV caches fill; everyone slows down.",
    win: "Short-prompt users keep p50 < 1s; only oversized requests are shed (kv_full); no GPU wrongly ejected.",
    weapons: ["kv_aware", "long_prompt_pool", "admission"], setup: async () => { await fresh(); await traffic("long_prompts"); },
  },
  {
    n: "05", title: "🚨 Model server dies", brief: "GPU pods get killed every 20 seconds. Users shouldn't notice.",
    win: "User-visible errors < 0.5% while pods die and come back.", weapons: ["health_checks", "retries", "circuit_breaker", "fallback"],
    setup: async () => { await fresh(); await traffic("baseline"); await sleep(8000); await post(`${ATK}/chaos/pod-killer`, { interval_s: 20, duration_s: 120 }); },
  },
  {
    n: "06", title: "🚨 One tenant is starving everyone", brief: "batch-jobs sends 500 req/s next to acme and globex (25 req/s each).",
    win: "acme and globex at ~100% success; batch-jobs held to its quota. (Arsenal → client-side per-tenant table)",
    weapons: ["tenant_quotas", "admission"], setup: async () => { await fresh(); await traffic("noisy_tenant"); },
  },
  {
    n: "07", title: "🚨 Bad deployment", brief: "v2 is +80% latency, −10% accuracy, +40% cost. Ship it — or stop it.",
    win: "Eval gate rejects v2 (run `make eval`), or with skip-eval the canary rolls back automatically at 5%.",
    weapons: ["eval gate", "canary health gates", "rollback"],
    setup: async () => { await fresh(); await post(`${GW}/admin/defenses/preset/hardened`); await traffic("baseline"); await sleep(5000); await post(`${CTL}/rollouts`, { candidate: "v2", skip_eval: true, step_seconds: 30 }); },
  },
  {
    n: "08", title: "🚨 Edge devices go dark", brief: "40% of the edge fleet loses its uplink for 60 seconds.",
    win: "Partitioned devices keep serving, buffer telemetry, and sync it all when the network heals (late records > 0, dropped = 0).",
    weapons: ["bounded buffers", "store & forward"], setup: async () => { await post(`${ATK}/chaos/edge/partition`, { fraction: 0.4, duration_s: 60 }); },
  },
  {
    n: "09", title: "🚨 Broken model to the fleet", brief: "Someone ring-rolls edge-v2-bad (30% local failures) to the edge fleet.",
    win: "The canary ring health gate halts the rollout and rolls every ring back; blast radius ≈ 10% of devices.",
    weapons: ["rings", "health gates", "auto-halt"], setup: async () => { await post(`${CTL}/fleet/rollouts`, { version: "edge-v2-bad", hold_s: 20 }); },
  },
  {
    n: "10", title: "🤖 Final boss: give it to the AI SRE", brief: "Naive defenses, normal traffic, a silently degraded GPU. You may only approve or reject.",
    win: "The agent finds the GPU, proposes a fix, verifies users actually got better — and tries again if they didn't.",
    weapons: ["AI SRE", "MCP tools", "human approval"],
    setup: async () => {
      await fresh(); await traffic("baseline", 900); await sleep(10000);
      await post(`${GW}/admin/chaos/gpu-1`, { slow_ttft_ms: 4000 }); await sleep(30000);
      await post(`${AGENT}/incidents`, { question: "Production is broken. Users report timeouts." });
    },
  },
];

export default function Missions({ go }: { go: (tab: string) => void }) {
  const { busy, msg, run } = useAction();
  const [active, setActive] = useState<string | null>(null);
  return (
    <div className="grid">
      <div className="panel">
        <div className="row">
          <h2 style={{ margin: 0 }}>Missions</h2>
          <span className="spacer" />
          <button className="btn" disabled={busy} onClick={() => run("stand down", fresh)}>stand down (stop traffic, heal, naive)</button>
        </div>
        <p className="muted" style={{ margin: "6px 0 0" }}>
          Each mission resets to <b>naive</b> defenses and stages the attack. Watch it hurt on the Battlefield, then fight back from the Arsenal.
        </p>
        {msg && <div className="muted">{msg}</div>}
      </div>
      <div className="grid g3">
        {MISSIONS.map((m) => (
          <div key={m.n} className="panel" style={active === m.n ? { borderColor: "var(--accent)" } : undefined}>
            <div className="muted mono">MISSION {m.n}</div>
            <h3>{m.title}</h3>
            <p>{m.brief}</p>
            <p><b>Win:</b> {m.win}</p>
            <div className="row" style={{ marginBottom: 8 }}>{m.weapons.map((w) => <span key={w} className="pill">{w}</span>)}</div>
            <button className="btn primary" disabled={busy} onClick={() => run(`mission ${m.n} deployed`, async () => {
              setActive(m.n);
              const t = m.n === "10" ? "sre" : m.n === "07" || m.n === "08" || m.n === "09" ? "rollouts" : "battlefield";
              go(t);
              await m.setup();
            })}>deploy mission</button>
          </div>
        ))}
      </div>
    </div>
  );
}
