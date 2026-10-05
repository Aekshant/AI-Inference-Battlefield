#!/usr/bin/env python3
"""Builds docs/AI-Inference-Battlefield.pdf (HLD + LLD + results) from HTML with
inline SVG diagrams, printed by headless Chrome.

    python3 docs/build_pdf.py

Stdlib only (runs on the host's Python 3.8).
"""
import html
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_HTML = os.path.join(HERE, "design.html")
OUT_PDF = os.path.join(HERE, "AI-Inference-Battlefield.pdf")

# ---------------------------------------------------------------- SVG helpers

PAL = {
    "edge": ("#eef3fb", "#2f6fdb"),    # gateway / core
    "gpu": ("#eaf6ef", "#1f8a4c"),
    "ctl": ("#f4effb", "#7a4fc9"),
    "ops": ("#fdf3e6", "#b7791f"),
    "user": ("#f1f2f4", "#5d6673"),
    "bad": ("#fdecec", "#c53030"),
    "ai": ("#e8f6f8", "#0f7c8c"),
}


def esc(s):
    return html.escape(str(s))


class Svg:
    def __init__(self, w, h):
        self.w, self.h, self.parts = w, h, []

    def box(self, x, y, w, h, title, sub="", kind="edge", r=8, dashed=False):
        fill, stroke = PAL[kind]
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}" stroke-width="1.4"{dash}/>')
        lines = sub.split("\n") if sub else []
        ty = y + h / 2 - (len(lines) * 6.5) + (4 if not lines else 0)
        self.parts.append(f'<text x="{x + w / 2}" y="{ty}" text-anchor="middle" class="bt" fill="{stroke}">{esc(title)}</text>')
        for i, l in enumerate(lines):
            self.parts.append(f'<text x="{x + w / 2}" y="{ty + 14 + i * 12.5}" text-anchor="middle" class="bs">{esc(l)}</text>')
        return (x, y, w, h)

    def group(self, x, y, w, h, label, kind="user"):
        _, stroke = PAL[kind]
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="none" stroke="{stroke}" stroke-width="1" stroke-dasharray="3 3"/>')
        self.parts.append(f'<text x="{x + 8}" y="{y + 14}" class="gl" fill="{stroke}">{esc(label)}</text>')

    def arrow(self, x1, y1, x2, y2, label="", dashed=False, color="#444", lx=None, ly=None, both=False):
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        start = ' marker-start="url(#ah-s)"' if both else ""
        self.parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="1.3"{dash} marker-end="url(#ah)"{start}/>')
        if label:
            lx = (x1 + x2) / 2 if lx is None else lx
            ly = (y1 + y2) / 2 - 4 if ly is None else ly
            self.parts.append(f'<text x="{lx}" y="{ly}" text-anchor="middle" class="al">{esc(label)}</text>')

    def path(self, d, label="", lx=0, ly=0, dashed=False, color="#444"):
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        self.parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.3"{dash} marker-end="url(#ah)"/>')
        if label:
            self.parts.append(f'<text x="{lx}" y="{ly}" text-anchor="middle" class="al">{esc(label)}</text>')

    def text(self, x, y, s, cls="al", anchor="middle"):
        self.parts.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" class="{cls}">{esc(s)}</text>')

    def render(self, caption=""):
        defs = ('<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                '<path d="M0,0 L10,5 L0,10 z" fill="#444"/></marker>'
                '<marker id="ah-s" viewBox="0 0 10 10" refX="1" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                '<path d="M0,0 L10,5 L0,10 z" fill="#444"/></marker></defs>')
        svg = (f'<svg viewBox="0 0 {self.w} {self.h}" width="100%" xmlns="http://www.w3.org/2000/svg">{defs}'
               + "".join(self.parts) + "</svg>")
        cap = f"<figcaption>{caption}</figcaption>" if caption else ""
        return f'<figure class="diagram">{svg}{cap}</figure>'


# ---------------------------------------------------------------- diagrams

def d_architecture():
    s = Svg(900, 560)
    s.group(10, 10, 170, 250, "Traffic & humans")
    s.box(25, 40, 140, 60, "Attacker", "open-loop load\n+ chaos engine", "user")
    s.box(25, 115, 140, 50, "Console", "React · :5173", "user")
    s.box(25, 180, 140, 60, "Human on call", "approves agent\nactions", "user")

    s.group(200, 10, 690, 400, "Kubernetes · kind (namespace battlefield)", "edge")
    s.box(220, 70, 170, 120, "AI Gateway", "Go · Gin · :8080\nadmission · fair queue\nrouting · breakers\nretries · canary split", "edge")
    s.group(430, 35, 200, 215, "stable pool · model v1", "gpu")
    s.box(450, 60, 160, 40, "gpu-0", "", "gpu")
    s.box(450, 110, 160, 40, "gpu-1", "", "gpu")
    s.box(450, 160, 160, 40, "gpu-2", "", "gpu")
    s.text(530, 225, "StatefulSet · simulated GPUs", "bs")
    s.group(650, 35, 220, 95, "canary pool · candidate", "bad")
    s.box(670, 62, 180, 50, "gpu-canary", "Deployment · autoscaled\nby rollout share", "bad")
    s.box(650, 150, 220, 85, "Rollout controller", "Go · :8082 · registry\neval gate · canary gates\nedge fleet hub", "ctl")
    s.box(650, 275, 220, 60, "Edge fleet", "2 pods × 25 devices\nrings: canary/early/broad", "ctl")
    s.box(430, 290, 200, 60, "Prometheus + Grafana", "rules · 8 alerts · dashboard", "ops")
    s.box(220, 290, 170, 60, "Kubernetes API", "EndpointSlices · scale\npod delete (chaos)", "user")

    s.group(200, 425, 690, 125, "Host (outside the cluster)", "ai")
    s.box(220, 455, 170, 75, "AI SRE agent", "Python · LangGraph\n:8090 · auto-watch", "ai")
    s.box(450, 455, 160, 75, "MCP server", "7 read tools\n+ guarded actions", "ai")
    s.box(670, 455, 180, 75, "Ollama", "qwen2.5:3b (CPU)\n127.0.0.1:11434", "ai")

    s.arrow(165, 70, 220, 105, "requests")
    s.arrow(165, 140, 220, 140)
    s.arrow(390, 110, 450, 80)
    s.arrow(390, 125, 450, 130, "load-aware", lx=420, ly=118)
    s.arrow(390, 140, 450, 180)
    s.path("M330,70 C380,4 800,0 835,60", "canary %", 600, 20)
    s.arrow(305, 190, 305, 290, "ready endpoints (1 s)", dashed=True, lx=355, ly=245)
    s.path("M650,225 C560,282 420,282 372,192", "set canary weight", 445, 286)
    s.arrow(760, 275, 760, 235, "telemetry", both=True, lx=800, ly=260)
    s.arrow(530, 290, 530, 250, "scrape", dashed=True, lx=580, ly=270)
    s.arrow(650, 210, 630, 300, "health gates", dashed=True, lx=670, ly=262)
    s.arrow(305, 455, 305, 350, "", color="#0f7c8c")
    s.arrow(390, 492, 450, 492)
    s.arrow(610, 492, 670, 492, "")
    s.path("M530,455 C530,410 530,380 530,352", "", color="#0f7c8c")
    s.path("M560,455 C620,400 700,380 740,337", "", color="#0f7c8c")
    s.text(440, 418, "reads + approved actions via MCP", "al")
    s.arrow(95, 240, 220, 480, "approve / reject", lx=130, ly=380)
    return s.render("Figure 1 — High-level architecture. Solid arrows are request/command paths; dashed arrows are observation paths.")


def d_deployment():
    s = Svg(900, 330)
    s.group(10, 10, 880, 250, "kind cluster “battlefield” · Kubernetes 1.37 · 3 Docker containers", "user")
    s.box(30, 40, 260, 200, "control-plane node", "API server · etcd · scheduler\n\nNodePorts → 127.0.0.1 only:\n30080 → 8080 gateway\n30081 → 8081 attacker\n30082 → 8082 controller\n30090 → 9090 Prometheus\n30030 → 3000 Grafana", "user")
    s.box(320, 40, 270, 200, "worker node", "pods scheduled by Kubernetes\n(example placement)\n\ngpu-0 · gpu-1 · attacker\ngrafana · controller · edge\n\nPVC: gateway-state,\ncontroller-state (local-path)", "gpu")
    s.box(610, 40, 260, 200, "worker2 node", "gpu-2 · gpu-canary\ngateway · prometheus · edge\n\nall pods: requests/limits set;\ngateway & controller:\nRecreate strategy (RWO volume)", "gpu")
    s.box(30, 275, 260, 45, "Host: console (vite) :5173", "", "user")
    s.box(320, 275, 270, 45, "Host: AI SRE container :8090 (host net)", "", "ai")
    s.box(610, 275, 260, 45, "Host: Ollama 127.0.0.1:11434", "", "ai")
    return s.render("Figure 2 — Deployment topology. Nothing is reachable from the LAN: every port binds to 127.0.0.1.")


def d_request():
    s = Svg(900, 300)
    y = 60
    steps = [("Parse", "tenant · pool\n(canary %)", "user"), ("Tenant bucket", "429 tenant_quota", "edge"),
             ("Global bucket", "429 rate_limited", "edge"), ("Fair queue", "priority + DRR\n≤1 s · 503 queue_*", "edge"),
             ("Router", "filter → score\np2c / prefix", "edge"), ("Breaker", "skip open GPUs", "edge"),
             ("Attempt", "TTFT timeout\nKV in-flight ±", "gpu")]
    x = 15
    pos = []
    for t, sub, k in steps:
        pos.append(s.box(x, y, 112, 70, t, sub, k))
        x += 125
    for i in range(len(pos) - 1):
        a, b = pos[i], pos[i + 1]
        s.arrow(a[0] + a[2], y + 35, b[0], y + 35)
    s.box(15, 190, 230, 70, "Shed fast", "429/503 + Retry-After\nbefore any GPU work", "bad")
    s.box(330, 190, 230, 70, "Backpressure / kv_full", "503 when every GPU is saturated\nor no GPU has KV room", "bad")
    s.box(640, 190, 245, 70, "Retry / fallback", "other GPU before 1st byte (budget 20%)\nelse cache → degraded answer", "ops")
    s.path("M200,130 C200,160 140,170 130,190", "", dashed=True, color="#c53030")
    s.path("M560,130 C560,160 470,170 445,190", "", dashed=True, color="#c53030")
    s.path("M820,130 C820,160 790,170 765,190", "", dashed=True, color="#b7791f")
    s.text(450, 30, "Every stage can shed; the user always gets an answer in bounded time.", "gl")
    return s.render("Figure 3 — Gateway request pipeline (hardened mode).")


def d_breaker():
    s = Svg(900, 225)
    s.box(60, 70, 170, 60, "CLOSED", "traffic flows", "gpu")
    s.box(365, 70, 170, 60, "OPEN", "no traffic · 10 s", "bad")
    s.box(670, 70, 170, 60, "HALF-OPEN", "exactly 1 probe", "ops")
    s.arrow(230, 90, 365, 90, "5 consecutive hard failures")
    s.arrow(535, 90, 670, 90, "cooldown elapsed")
    s.path("M755,130 C755,190 145,190 145,132", "probe succeeds", 450, 165)
    s.path("M700,70 C650,20 520,20 470,68", "probe fails", 585, 32)
    s.text(450, 215, "hard failure = connect error, 500, 502 · overload (503/504) never trips it · client cancel releases the probe", "al")
    return s.render("Figure 4 — Per-GPU circuit breaker.")


def d_rollout():
    s = Svg(900, 250)
    s.box(15, 90, 120, 60, "Eval gate", "golden set ×3", "ctl")
    xs = [175, 310, 445, 580]
    for x, p in zip(xs, ["5%", "25%", "50%", "100%"]):
        s.box(x, 90, 105, 60, "canary " + p, "scale pool first", "gpu" if p != "100%" else "edge")
    s.arrow(135, 120, 175, 120, "pass", ly=113)
    for a, b in zip(xs, xs[1:]):
        s.arrow(a + 105, 120, b, 120)
    s.box(730, 90, 155, 60, "PROMOTED", "weight 1.0", "edge")
    s.arrow(685, 120, 730, 120)
    s.box(250, 190, 330, 50, "ROLLED BACK · weight 0 · pool → 1 pod", "", "bad")
    for x in xs[:3]:
        s.arrow(x + 52, 150, min(max(x + 52, 270), 560), 190, dashed=True, color="#c53030")
    s.box(15, 190, 140, 50, "REJECTED", "", "bad")
    s.arrow(75, 150, 75, 190, "fail", lx=95, ly=172)
    s.text(450, 40, "Health gate every 5 s (window starts at rollout, filtered by version, ≥20 canary requests):", "gl")
    s.text(450, 62, "canary p99 TTFT ≤ 1.3× stable   ·   canary errors ≤ 2%   ·   canary $/token ≤ 1.25× stable   ·   each step holds 30 s", "al")
    return s.render("Figure 6 — Model rollout state machine (rollout controller).")


def d_agent():
    s = Svg(900, 260)
    nodes = [("triage", "deterministic\ndigest", 15), ("investigate", "≤4 MCP\ntool calls", 140), ("diagnose", "JSON +\nhypothesis id", 265),
             ("propose", "allowlist +\nvalidation", 390)]
    for t, sub, x in nodes:
        s.box(x, 60, 110, 65, t, sub, "ai")
    for (_, _, a), (_, _, b) in zip(nodes, nodes[1:]):
        s.arrow(a + 110, 92, b, 92)
    s.box(530, 55, 110, 75, "HUMAN", "approve /\nreject (+note)", "ops")
    s.arrow(500, 92, 530, 92)
    s.box(670, 60, 100, 65, "execute", "via MCP", "ai")
    s.arrow(640, 92, 670, 92, "approve", lx=655, ly=50)
    s.box(790, 60, 100, 65, "verify", "user SLOs\nafter 45 s", "ai")
    s.arrow(770, 92, 790, 92)
    s.box(670, 180, 220, 55, "postmortem", "templated facts + LLM summary", "ai")
    s.arrow(840, 125, 800, 180, "SLO met", lx=860, ly=158)
    s.path("M840,60 C840,0 70,0 70,58", "users still hurting → re-triage with history (≤3 attempts)", 450, 14)
    s.path("M585,130 C585,165 470,165 445,127", "reject + note → re-propose", 515, 172)
    s.arrow(570, 130, 680, 190, "reject", lx=600, ly=175)
    return s.render("Figure 7 — AI SRE incident graph (LangGraph).")


def d_worker():
    s = Svg(900, 220)
    s.box(15, 70, 150, 70, "request", "kv = prompt/4\n+ max_tokens", "user")
    s.box(220, 50, 200, 110, "acquire()", "fits? slots < 32 and\nkv_used + kv ≤ budget\nelse FIFO waiter\n[kv, future, granted]", "gpu")
    s.box(480, 50, 190, 110, "generate", "TTFT = (base + prefill)\n× factor + chaos\ndecode = 60 tok/s ÷\nbatch ÷ KV penalty", "gpu")
    s.box(730, 70, 155, 70, "release()", "pump(): grant\nwaiters that fit", "gpu")
    s.arrow(165, 105, 220, 105)
    s.arrow(420, 105, 480, 105)
    s.arrow(670, 105, 730, 105)
    s.path("M807,70 C807,10 320,10 320,48", "granted=True set before the future resolves", 560, 22)
    s.text(450, 190, "timeout/cancel: pop waiter; if granted → release (no leak); never release otherwise (no double free)", "al")
    return s.render("Figure 5 — Simulated GPU: slot and KV-cache accounting.")


# ---------------------------------------------------------------- content helpers

def table(head, rows, cls=""):
    th = "".join(f"<th>{h}</th>" for h in head)
    trs = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>'


def code(s):
    return f"<pre>{esc(s.strip())}</pre>"


SECTIONS = []


def section(num, title, body, brk=True):
    SECTIONS.append((num, title))
    cls = "section brk" if brk else "section"
    return f'<section class="{cls}" id="s{num}"><h1><span class="num">{num}</span>{title}</h1>{body}</section>'


def sub(title, body):
    return f"<h2>{title}</h2>{body}"


# ---------------------------------------------------------------- document

def build():
    parts = []

    parts.append(section("1", "Executive summary", f"""
<p class="lead">AI Inference Battlefield is an LLM serving platform designed to be attacked. A load generator and chaos engine
run realistic production incidents against a Kubernetes-hosted inference fleet. Each attack is measured with defenses off
and on. The final stage hands operations to an AI SRE agent, built on an open-source model, that detects, diagnoses,
remediates (with human approval) and verifies against user-facing SLOs.</p>
<div class="kpis">
  <div><b>88% → 0%</b><span>errors under a 10× spike</span></div>
  <div><b>3 s</b><span>to eject a degraded GPU</span></div>
  <div><b>5%</b><span>exposure of a bad model</span></div>
  <div><b>4 / 50</b><span>edge devices hit by a broken rollout</span></div>
</div>
{sub("What it demonstrates", '''<ul>
<li><b>Inference traffic engineering:</b> admission control, backpressure, load/KV/prefix-aware routing, outlier ejection,
circuit breakers, budgeted retries, fallback, multi-tenant fair queueing.</li>
<li><b>Progressive delivery for models:</b> offline eval gate, canary with metric health gates and automatic rollback,
canary capacity scaling, ring rollouts to an edge fleet.</li>
<li><b>Resilience at the edge:</b> devices that keep serving through network partitions and sync buffered telemetry.</li>
<li><b>Agentic operations done safely:</b> a small local LLM inside a fixed state machine, with an allowlist, argument
validation, policy limits, human approval and SLO-based verification.</li>
<li><b>Engineering honesty:</b> every number is measured, and the document records the bugs the attacks found in the
defenses themselves.</li></ul>''')}
{sub("Technology", table(["Layer", "Choice"], [
        ["Gateway, rollout controller", "Go 1.27, Gin, Prometheus client_golang"],
        ["GPU workers, attacker, edge fleet, evals", "Python 3.12, FastAPI, aiohttp"],
        ["AI SRE", "LangGraph 1.2, Model Context Protocol (MCP) 1.30, langchain-ollama, Ollama"],
        ["Model", "qwen2.5:3b (agent), qwen2.5:0.5b (optional real serving), on CPU"],
        ["Console", "React 19, Vite 7, TypeScript"],
        ["Platform", "Kubernetes 1.37 on kind, Prometheus 3.5, Grafana 12"],
    ]))}
"""))

    parts.append(section("2", "Problem and goals", f"""
<p>LLM inference fails differently from ordinary web services. Requests are long (seconds), expensive, and heterogeneous:
a 6,000-token prompt costs about 50× a chat turn in KV-cache memory. GPUs fail <i>grey</i>: they get slow, not dead. A
model release can be fast and wrong. GPU utilization says little about user experience. Generic L7 load balancing
(round robin, HTTP health checks) is the wrong tool.</p>
{sub("Goals", '''<ol>
<li>Build the smallest system that reproduces the real failure modes of LLM serving, on a laptop without GPUs.</li>
<li>For each failure, measure the naive behaviour, then the behaviour with a specific defense, using the same load.</li>
<li>Make releases safe: no bad model should reach more than a small, measurable fraction of users or devices.</li>
<li>Give the whole system to an AI agent and keep it safe: humans approve, code enforces policy, users' SLOs decide success.</li></ol>''')}
{sub("Non-goals", '''<ul><li>Absolute GPU performance numbers (GPUs are simulated; dynamics are the point).</li>
<li>High availability of the control plane (single replicas with persisted state).</li></ul>''')}
{sub("Service level objectives", table(["SLO", "Target", "Measured where"], [
        ["User-visible error ratio", "≤ 2%", "client side (attacker) and gateway"],
        ["p99 time to first token (TTFT)", "≤ 2 s", "gateway, short windows for verification"],
        ["Shed traffic", "explicit 429/503 in milliseconds, never a timeout", "gateway"],
    ]))}
"""))

    parts.append(section("3", "High-level design (HLD)", f"""
{d_architecture()}
{sub("Components", table(["Component", "Responsibility", "Interfaces"], [
        ["<b>AI Gateway</b> (Go)", "Only path users → GPUs. Ten defenses that can each be switched at runtime, canary split, tenant quotas, persisted state.", "<code>POST /v1/generate</code>, <code>/admin/*</code>, <code>/metrics</code>"],
        ["<b>GPU worker</b> (Python)", "One pod = one simulated GPU (slots, KV budget, prefill/decode); optional Ollama proxy; fault injection.", "<code>/v1/generate</code>, <code>/state</code>, <code>/chaos</code>, <code>/metrics</code>"],
        ["<b>Attacker</b> (Python)", "Open-loop load scenarios; client-side measurement; pod kills; edge chaos fan-out.", "<code>/attacks</code>, <code>/chaos/*</code>"],
        ["<b>Rollout controller</b> (Go)", "Registry + eval gate, canary steps with Prometheus gates, canary autoscaling, edge fleet hub with ring rollouts.", "<code>/registry</code>, <code>/rollouts</code>, <code>/fleet/*</code>"],
        ["<b>Edge fleet</b> (Python)", "50 devices: local inference, bounded store-and-forward telemetry, versions pulled by ring.", "<code>/devices</code>, <code>/chaos/*</code>"],
        ["<b>Eval harness</b> (Python)", "Golden set against a pinned version; posts accuracy, p99, cost per 1k tokens.", "CLI → <code>/registry/:v/eval</code>"],
        ["<b>AI SRE</b> (Python)", "LangGraph incident graph; MCP tools; human approval; SLO verification; auto-watch.", "<code>/incidents</code>, <code>/digest</code>"],
        ["<b>Console</b> (React)", "Missions, live battlefield, attack and defense controls, rollouts and fleet, incident approval.", "same-origin proxy to all APIs"],
        ["<b>Prometheus + Grafana</b>", "Pod-annotation scraping, recording rules, 8 alerts, generated 22-panel dashboard.", ":9090, :3000"],
    ]))}
{sub("Key flows", '''<ol>
<li><b>Data path:</b> attacker/console → gateway → (admission → router → attempt) → GPU → streamed tokens.</li>
<li><b>Discovery path:</b> gateway lists ready EndpointSlices every 1 s and polls each GPU's <code>/state</code> every 500 ms.</li>
<li><b>Release path:</b> eval → controller sets canary weight on the gateway → Prometheus gates → promote or roll back.</li>
<li><b>Edge path:</b> device → telemetry batch → hub returns desired version for its ring → device installs.</li>
<li><b>Operations path:</b> Prometheus alerts + digest → agent → MCP → human approval → action → SLO verification.</li></ol>''')}
{d_deployment()}
"""))

    parts.append(section("4", "LLD — AI Gateway", f"""
<p>Packages: <code>config</code> (defenses, tenants, store), <code>admission</code>, <code>router</code>,
<code>backend</code> (registry, discovery, polling, breaker), <code>stats</code> (per-second ring for the console),
<code>metrics</code>, and <code>cmd/gateway</code> (HTTP, proxy, retries, fallback, persistence).</p>
{d_request()}
{sub("4.1 Admission control", '''<ul>
<li><b>Order matters:</b> tenant token bucket first, then the global bucket. If the global bucket came first, a noisy
tenant's rejected traffic would still burn shared tokens.</li>
<li><b>Bounded in-flight</b> (<code>max_inflight</code> = total GPU slots, 96). Beyond that, a bounded queue (200) with a
deadline (1 s hardened).</li>
<li><b>Scheduling:</b> best priority class first (0 interactive, 1 standard, 2 batch), then deficit round robin across
tenants weighted by share. When the queue is full, the newest waiter of a strictly lower priority is evicted.</li>
<li>Every outcome is explicit: <code>tenant_quota</code>, <code>rate_limited</code>, <code>queue_full</code>,
<code>queue_timeout</code>, <code>evicted_by_priority</code>.</li></ul>''')}
{sub("4.2 Routing", '''<p><b>Eligibility filter</b> (in order): right pool · not excluded by an earlier attempt · not manually ejected ·
<b>not gone from EndpointSlices</b> (membership, applied even with every defense off) · weight &gt; 0 · healthy* · not
outlier-ejected* · breaker not open* · KV fits* · long-prompt split* (* = when that defense is on).</p>''' +
        code('''score(gpu) = ( (inflight + queue_depth) / slots
              + 0.5 * kv_util                  # when kv_aware
              + 0.25 * ttft_ewma_ms / 300
              + 2 * error_ewma ) / weight

kv_fits   = max(reported kv_used, gateway kv_inflight) + request_kv <= kv_budget
saturated = every eligible GPU has inflight >= 2*slots or queue >= slots   -> 503 backpressure''') +
        table(["Mode", "Algorithm", "Use"], [
            ["round_robin", "weighted rotation", "baseline that fails Attack #2"],
            ["least_loaded", "min score", "small fleets"],
            ["p2c", "power of two random choices on score", "default hardened: O(1), avoids herding"],
            ["prefix", "consistent hashing on session or prompt[:256], 64 virtual nodes per GPU, bounded load ≤ 1.25 × mean", "prefix/KV cache reuse"],
        ]) + '''<p><b>Long-prompt pool:</b> with ≥ 3 GPUs, the last GPU by name is reserved for prompts ≥ 8,000 chars. Short
requests may borrow it while its KV utilization is below 30%.</p>''')}
{sub("4.3 Health", '''<ul>
<li><b>Polling:</b> <code>/state</code> every 500 ms (400 ms timeout); 2 failures → unhealthy.</li>
<li><b>Discovery:</b> EndpointSlices via the Kubernetes API every 1 s (read-only RBAC); DNS fallback outside a cluster.
A backend that leaves the list but still has requests in flight is marked <i>draining</i> and never routed to again.</li>
<li><b>Outlier ejection:</b> each second, per pool: eject for 15 s if TTFT &gt; 3× the pool's lower median and &gt; 800 ms, or the
error EWMA is above 0.5, provided another GPU remains.</li>
<li><b>Overload ≠ breakage:</b> only connection errors, 500 and 502 count as hard failures; 503/504 are load signals.
TTFT EWMAs ignore prompts over 2,000 chars, so prefill doesn't look like sickness.</li></ul>''')}
{d_breaker()}
{sub("4.4 Retries, fallback and persistence", '''<ul>
<li>Up to 3 attempts, each on a different GPU, only before the first byte reaches the client. Retry budget: retries over
the last 10 s ≤ 20% of requests + 3.</li>
<li>Attempt timeout covers time to first token (6 s hardened); a stream may then run until it finishes.</li>
<li>Fallback: canary pool empty → stable pool; otherwise the last good answer for the same prompt from a 2,000-entry LRU
cache; otherwise a degraded 200 marked <code>X-Fallback</code>.</li>
<li>State (defenses, tenants, canary weight, per-GPU weights and ejections keyed by pod name) is written atomically to
<code>/data/gateway.json</code> on a PersistentVolume after every admin change and restored at startup.</li></ul>''')}
<div class="two"><div>{sub("4.5 Defense presets", table(["Setting", "naive", "hardened"], [
        ["admission / backpressure", "off", "on"], ["rate limit / burst", "120 / 60", "150 / 75"],
        ["in-flight / queue / deadline", "96/200/2s", "96/200/1s"],
        ["routing", "round_robin", "p2c"], ["kv_aware, long_prompt_pool, outlier_ejection", "off", "on"],
        ["health_checks, circuit_breaker, retries, fallback", "off", "on"],
        ["attempt timeout", "30 s", "6 s"], ["tenant_quotas", "off", "on"],
    ]))}</div><div>
{sub("4.6 Tenants", table(["Tenant", "Rate", "Burst", "Wt", "Priority"], [
        ["default", "150", "75", "2", "standard"], ["acme", "60", "30", "3", "interactive"],
        ["globex", "60", "30", "2", "standard"], ["batch-jobs", "40", "20", "1", "batch"]]))}</div></div>
{sub("4.7 Gateway API", table(["Method & path", "Purpose"], [
        ["<code>POST /v1/generate</code>", "inference: <code>{prompt, max_tokens, stream, tenant, session}</code>; headers <code>X-Tenant</code>, <code>X-Model-Version</code>; response headers <code>X-Node</code>, <code>X-Pool</code>, <code>X-Attempts</code>, <code>X-Fallback</code>"],
        ["<code>GET /admin/state</code>", "defenses, every backend snapshot with score, canary weight, admission queue, tenants"],
        ["<code>GET /admin/stats?seconds=N</code>", "per-second rps, ok/shed/errors/fallback, p50/p99 latency, p99 TTFT, traffic per pool and GPU"],
        ["<code>GET /admin/events</code>", "ejections, breaker transitions, discovery, config changes (last 500)"],
        ["<code>POST /admin/defenses</code> · <code>/defenses/preset/:name</code>", "partial JSON merge, or the naive / hardened preset"],
        ["<code>POST /admin/tenants</code>", "create or update a tenant quota, weight and priority"],
        ["<code>POST /admin/backends/:gpu/{weight|eject|restore}</code>", "operator overrides, persisted by pod name"],
        ["<code>POST /admin/canary</code>", "canary traffic share 0..1 (used by the rollout controller)"],
        ["<code>POST /admin/chaos/:gpu[/reset|/crash]</code>", "fault injection forwarded to one GPU or <code>all</code>"],
        ["<code>GET /metrics</code> · <code>/healthz</code>", "Prometheus metrics, liveness"],
    ]))}
"""))

    parts.append(section("5", "LLD — Simulated GPU worker", f"""
<p>Each pod models one GPU serving one model. The gateway cannot tell it apart from a real inference server: it
streams Server-Sent Events, enforces capacity, and reports the same signals (slots, queue depth, KV utilization, TTFT, tokens/s).</p>
{d_worker()}
{code('''est_tokens(text)      = len(text) / 4
kv(request)           = prompt_tokens + max_tokens                (budget 65,536 tokens)
prefill_s             = prompt_tokens / 4000  (x0.2 on prefix-cache hit; LRU of 2,048 prefixes)
ttft_s                = (0.120 + prefill_s) * LATENCY_FACTOR + chaos.slow_ttft  (x U[0.9, 1.1])
decode_tok_per_s      = 60 * decode_factor / (1 + 0.02*(active-1)) / kv_penalty / LATENCY_FACTOR
kv_penalty            = 1 + (kv_util - 0.85) * 12   when kv_util > 0.85   (preemption/swapping)''')}
{sub("Slot accounting (race-safe)", '''<p>A waiter is <code>[kv, future, granted]</code>. <code>pump()</code> sets <code>granted</code> and takes the
slot <i>before</i> resolving the future. On timeout or cancellation the waiter is removed; if <code>granted</code> is
set, the slot is released. This fixed a leak (timeout racing a grant). The first fix had caused double releases,
because <code>wait_for</code> cancels the future. <code>worker/test_slots.py</code> races 500 timeouts and 100
cancellations against grants and asserts exact accounting.</p>''')}
<div class="two"><div>{sub("Fault injection", table(["Fault", "API", "Effect"], [
        ["Slow GPU", '<code>{"slow_ttft_ms": 4000}</code>', "adds latency before the first token"],
        ["Slow decode", '<code>{"decode_factor": 0.3}</code>', "slower tokens/s"],
        ["Errors", '<code>{"error_rate": 0.5}</code>', "500s"],
        ["KV squeeze", '<code>{"kv_budget": 6000}</code>', "tiny KV cache"],
        ["Hang", '<code>{"hang": true}</code>', "requests and <code>/state</code> hang; <code>/ready</code> fails"],
        ["Crash", "<code>POST /chaos/crash</code>", "process exits; restarted"],
    ]))}</div><div>
{sub("Model versions", table(["Version", "Latency ×", "Accuracy", "$/1k tok"], [
        ["v1 stable", "1.0", "0.95", "0.10"], ["v2 bad", "1.8", "0.85", "0.14"], ["v3 good", "1.0", "0.96", "0.09"]]))}</div></div>
<p class="note">Synthetic mode answers eval questions with an "oracle" that is right with probability ORACLE_ACCURACY, so model
quality is measurable without a real model. Real mode (<code>MODE=real</code>) proxies to Ollama.</p>
"""))

    parts.append(section("6", "LLD — Rollout controller and edge fleet", f"""
{d_rollout()}
{sub("6.1 Eval gate", '''<p>The harness pins traffic with <code>X-Model-Version</code> (the gateway maps versions to pools from what GPUs report;
unknown versions return 404). It runs 40 questions × 3 and posts accuracy, p50/p99 and cost per 1k tokens. Gate:
accuracy ≥ 0.90, accuracy drop ≤ 3 points vs baseline, p99 ≤ 1.3× baseline, cost ≤ 1.25× baseline.</p>''')}
{sub("6.2 Canary health gates", code('''window = clamp(time since rollout start, 15s, 60s)        # no samples from earlier candidates
canary_requests = sum(increase(worker_requests_total{version="<cand>"}[window]))      # must be >= 20
ttft_p99(v)     = histogram_quantile(0.99, sum by (le)(rate(worker_ttft_seconds_bucket{version="<v>"}[window])))
cost_tok(v)     = rate(worker_cost_dollars_total) / (rate(output_tokens) + rate(prompt_tokens))
error_ratio     = rate(gw_requests_total{pool="canary",outcome="error"}) / rate(gw_requests_total{pool="canary"})''') +
        '''<p>NaN/Inf from Prometheus is sanitized to 0 (Go's JSON encoder rejects NaN). Before each step the controller
scales <code>gpu-canary</code> to <code>max(1, ceil(weight × stable_replicas × 1.2))</code> and waits for readiness.
RBAC allows only get/patch on the scale subresource of <code>gpu</code> and <code>gpu-canary</code>.</p>''')}
{sub("6.3 Edge fleet", '''<ul>
<li>Ring = <code>sha1(device_id) mod 100</code>: &lt;10 canary, &lt;40 early, else broad (4 / 17 / 29 devices).</li>
<li>Each device infers every 2 s and uploads every ~4 s (batches of ≤ 200). Buffer is capped at 300 records; the oldest
is dropped and counted. Records older than 15 s on arrival are counted as late.</li>
<li>A device is online if seen in the last 12 s. Ring rollout: set the ring's desired version → hold 20 s → judge only
online devices already on the new version (≥ 2) → error rate &gt; 5% halts and points every ring back to the previous version.</li>
<li>Chaos: partition a fraction of devices for N seconds, add uplink latency and loss.</li></ul>''')}
{sub("6.4 Persistence", '''<p>Registry (with evals) and fleet desired versions are snapshotted every 2 s when changed (atomic rename) to
<code>/data/controller.json</code>. A rollout in progress is not resumed after a restart.</p>''')}
"""))

    parts.append(section("7", "LLD — AI SRE agent", f"""
{d_agent()}
{sub("7.1 Why this shape", '''<p>A 3B model on CPU is good at reading evidence and unreliable at choosing arguments. The graph is fixed; the model
makes bounded decisions inside each node; every LLM step has a deterministic fallback; and all safety lives in code.</p>''')}
{sub("7.2 Evidence digest (triage)", '''<p>Plain Python turns gateway state, Prometheus and the controller into ~20 lines of facts plus ranked hypotheses, each
with a suggested action. Examples: GPU TTFT &gt; 3× fleet median → <code>shift_traffic(gpu, 0)</code>; errors &gt; 5% with
admission off → <code>enable_defense(admission)</code>; one tenant &gt; 60% of traffic with others shed →
<code>set_tenant_rate</code>; canary TTFT &gt; 1.3× stable → <code>set_canary_weight(0)</code>; edge ring errors &gt; 10% →
<code>fleet_rollback</code>. Injected-chaos labels and attacker events are hidden; events older than 10 minutes are dropped.</p>''')}
{sub("7.3 MCP tools and guarded actions", table(["Read tools (MCP)", "Allowlisted actions (validated)"], [
        ["fleet_health · traffic_summary · firing_alerts · recent_events · rollout_status · k8s_pods · pod_logs · evidence_digest",
         "shift_traffic · eject_backend · restore_backend · set_canary_weight (decrease only) · scale_gpus (3–6) · restart_pod (GPU pods only) · set_tenant_rate · enable_defense · fleet_rollback · no_action"]]) +
        '''<p>Validation: typed arguments, known targets, <b>no-op detection</b> (e.g. weight already 1), <b>policy</b> (the agent can never
increase canary traffic), and no repeat of an action that already failed. A rejected model proposal falls back to the
highest-ranked untried hypothesis.</p>''')}
{sub("7.4 Verification", code('''kpis(window=15s)  : gateway per-second stats + 30s client-probe error ratio
errors            = max(user_error_ratio, gateway_error_ratio)
improved          = (errors_after <= 1% or <= 0.7 x before) and (ttft_after <= 1.5s or <= 0.7 x before)
                    and not (errors_after > 1.2 x before + 1%)
within_slo        = errors_after <= 2% and p99_ttft_after <= 2s
next              = within_slo ? postmortem : (attempts < 3 ? triage : postmortem)
status            = resolved | mitigated (better, SLO breached) | unresolved | rejected'''))}
{sub("7.5 Operation", '''<ul><li><b>Auto-watch:</b> every 15 s; opens an incident after 2 consecutive checks with a high-severity hypothesis or firing
alert; skips while an incident is open or within 120 s of the last one.</li>
<li><b>Human loop:</b> LangGraph <code>interrupt()</code> pauses at approval; the console resumes it. A rejection with a note
re-enters <code>propose</code> with that feedback (≤ 2 times).</li>
<li><b>Postmortem:</b> impact, root cause, evidence, actions table and follow-ups are generated from data; the LLM writes only
a 2–3 sentence summary (template fallback). 18 s end to end.</li>
<li>Runs on the host network so it can reach Ollama on 127.0.0.1; the API binds to 127.0.0.1:8090.</li></ul>''')}
"""))

    parts.append(section("8", "Observability", f"""
{sub("Metrics (selected)", table(["Source", "Metrics"], [
        ["Gateway", "gw_requests_total{outcome,code,pool,tenant} · gw_shed_total{reason,tenant} · gw_ttft_seconds · gw_request_seconds · gw_retries_total · gw_fallbacks_total · gw_backend_* (healthy, breaker, weight, ejected, inflight, ttft) · gw_canary_weight · gw_defense_enabled"],
        ["Worker", "worker_ttft_seconds · worker_request_seconds · worker_queue_depth · worker_active_requests · worker_kv_utilization · worker_tokens_per_second · worker_cost_dollars_total · worker_prefix_cache_hits_total"],
        ["Attacker (users)", "attacker_requests_total{scenario,tenant,outcome} · attacker_latency_seconds"],
        ["Controller / edge", "rollout_canary_weight · rollout_outcomes_total · model_eval_* · fleet_devices · fleet_ring_error_rate · fleet_telemetry_late_total · edge_buffered_records"],
    ]))}
{sub("Alerts", table(["Alert", "Condition"], [
        ["HighTTFT", "stable p99 TTFT > 1.5 s for 30 s"], ["GPUSlow", "GPU TTFT EWMA > 3× fleet median and > 0.8 s"],
        ["KVCachePressure", "KV utilization > 90% for 20 s"], ["GPUUnreachable", "gateway cannot poll a GPU for 10 s"],
        ["HighErrorRate", "pool error ratio > 2% for 20 s"], ["HeavyLoadShedding", "> 20% shed for 30 s"],
        ["TenantStarvation", "a tenant has > 50% of its requests shed"], ["CanaryRegression", "canary p99 TTFT > 1.3× stable while canary weight > 0"],
    ]))}
<p>Per-backend gauges are reset every second so pods that no longer exist stop alerting. The Grafana dashboard is generated
from code (<code>deploy/observability/gen_grafana.py</code>): users, gateway, GPU fleet, rollout and tenants rows.</p>
"""))

    parts.append(section("9", "Deployment and security", f"""
{table(["Resource", "Kind", "Notes"], [
        ["gpu", "StatefulSet (3) + headless Service", "stable names gpu-0..2 for targeted chaos; readiness /ready, liveness /health"],
        ["gpu-canary", "Deployment (1–4) + headless Service", "scaled by the controller"],
        ["gateway", "Deployment (1, Recreate) + NodePort 30080 + PVC", "SA: list EndpointSlices"],
        ["controller", "Deployment (1, Recreate) + NodePort 30082 + PVC", "SA: get deployments/statefulsets, patch the two scale subresources"],
        ["attacker", "Deployment + NodePort 30081", "SA: get/list/delete pods (chaos)"],
        ["edge", "Deployment (2 × 25 devices)", "talks to the controller hub"],
        ["prometheus / grafana", "Deployments + NodePorts", "SA: get/list/watch pods, endpoints, services"],
    ])}
<ul>
<li><b>Least privilege:</b> namespaced Roles only, scoped to the verbs and (where possible) resource names each component needs.</li>
<li><b>No LAN exposure:</b> every host port binds to 127.0.0.1; Ollama stays on loopback.</li>
<li><b>Images:</b> Go binaries on distroless/static (nonroot); Python on python:3.12-slim; pinned dependency versions.</li>
<li><b>Platform note:</b> the host runs cgroup v1, so the kind config sets <code>failCgroupV1: false</code> for Kubernetes 1.37.</li></ul>
"""))

    parts.append(section("10", "Missions and measured results", f"""
{table(["#", "Attack", "Defenses off", "Defenses on"], [
        ["02", "1000 req/s spike (10× capacity)", "88% errors, p99 8.9 s", "<b>0% errors</b>, p99 2.3 s, fast 429s"],
        ["03", "GPU TTFT → 4 s", "p99 20 s, 10% errors", "ejected in <b>3 s</b>, 0% errors"],
        ["04", "6k-token prompt flood", "21% errors, p99 24 s", "0% errors; short prompts p50 0.92 s; only kv_full shed"],
        ["05a", "GPU pod killed", "19% errors (DNS lag)", "<b>0%</b> even with defenses off (EndpointSlices)"],
        ["05b", "GPU process hangs", "2.6% errors, p99 unbounded", "0% errors, p99 2.7 s"],
        ["06", "Tenant floods 500 req/s", "others 15–22% success", "others <b>100%</b>; noisy tenant at quota"],
        ["07", "Bad model (+80% latency, −12% accuracy, +40% cost)", "—", "eval gate rejects; if bypassed, rollback at <b>5%</b>; a good v3 promotes 5→100%"],
        ["08", "40% of edge devices partitioned", "—", "keep serving; <b>280 late records synced, 0 lost</b>"],
        ["09", "Broken edge model rollout", "—", "halted at the canary ring: <b>4 / 50</b> devices"],
        ["10", "Silent GPU degradation, nobody paging", "—", "agent pages itself, 2 attempts, <b>0% errors, p99 TTFT 0.8 s</b>"],
    ])}
{sub("Final-boss timeline (Mission 10)", table(["t", "Event"], [
        ["0 s", "gpu-1 silently gains +4 s TTFT; defenses are all off"],
        ["121 s", "auto-watch opens an incident; the model diagnoses gpu-1 and proposes draining it; human approves"],
        ["+45 s", "verification: users <b>not</b> better (errors 0% → 5.8%: the remaining GPUs are overloaded) → re-triage"],
        ["", "model proposes sending 100% of traffic to the canary → <b>refused by policy</b> → fallback: enable admission control"],
        ["+45 s", "errors 5.8% → 0%, p99 TTFT 7.0 s → 0.8 s, 16% shed → <b>resolved within SLO</b>; templated postmortem with follow-ups"],
    ]))}
"""))

    parts.append(section("11", "Lessons learned", table(["#", "What the measurement showed", "Change"], [
        ["1", "Breakers and ejection treated long-prompt prefill and capacity timeouts as failures, ejected busy GPUs, and cascaded load (goodput 21%).", "Separate overload (503/504) from breakage; TTFT EWMAs from short prompts only."],
        ["2", "Global limit before tenant quotas let a noisy tenant starve others.", "Tenant quota first, then global."],
        ["3", "An eval of “v3” silently ran on v1 and passed.", "Route by reported version; unknown version → 404."],
        ["4", "A good canary was rolled back at 50%: one pod, half the traffic.", "Scale canary capacity with traffic share."],
        ["5", "Health gates mixed in the previous candidate's samples.", "Version-filtered queries, window from rollout start."],
        ["6", "NaN from Prometheus broke the status API (empty 200).", "Sanitize NaN/Inf."],
        ["7", "DNS caching kept a dead pod in rotation for 30 s+.", "EndpointSlice membership, applied regardless of defenses."],
        ["8", "Slots leaked on a timeout/grant race; the first fix double-released.", "Explicit grant flag + race test."],
        ["9", "Draining a sick GPU without admission control made users worse; verifier counted alerts.", "Verify on user SLOs; retry loop; mitigated ≠ resolved."],
        ["10", "Agent proposed 100% traffic to an untested model; test harness approved it.", "Policy: agent may only reduce canary weight."],
        ["11", "qwen3:4b spent 14 min reasoning, no postmortem.", "Template facts; LLM writes only the summary (18 s)."],
    ])))

    parts.append(section("12", "Operations, limitations and roadmap", f"""
{sub("Run", code('''make up            # kind cluster + build + load + deploy
make console       # http://127.0.0.1:5173
make agent         # AI SRE (local Ollama)
make spike DUR=60  # baseline | ramp | long-prompts | noisy-tenant
make chaos-slow GPU=gpu-2 ; make kill-pod GPU=gpu-1 ; make pod-killer
make defenses-naive | defenses-hardened
make eval && make canary-start ; make fleet-rollout VERSION=edge-v2-bad
make test          # Go unit tests (race-clean) + worker slot-accounting race test
python3 scripts/experiment.py --defenses hardened --scenario spike --duration 40'''))}
{sub("Limitations", '''<ul><li>GPUs are simulated; real-token mode exists but isn't load-tested on CPU.</li>
<li>Single-replica gateway and controller (state persisted, not HA).</li>
<li>qwen2.5:3b on CPU: ~2 minutes from page to proposal; sometimes wrong targets (guardrails and the human catch them).</li>
<li>Host on cgroup v1 (kind workaround in place).</li></ul>''')}
{sub("Roadmap", '''<ul><li>Shared gateway state (Redis/etcd) and multiple gateway replicas.</li>
<li>Real serving engine (vLLM) behind the same worker contract on a GPU node; real KV metrics.</li>
<li>Autoscaling the stable pool from queue depth/TTFT (KEDA) and letting the agent propose capacity plans.</li>
<li>Agent evaluation suite: inject each mission N times, score diagnosis accuracy and time to mitigate per model.</li>
<li>Tracing (OpenTelemetry) from gateway to GPU for per-request breakdowns.</li></ul>''')}
"""))

    toc = "".join(f'<li><span class="tn">{n}</span><a href="#s{n}">{t}</a></li>' for n, t in SECTIONS)
    cover = f"""
<section class="cover">
  <div class="tag">Design document · HLD + LLD</div>
  <h1>AI Inference Battlefield</h1>
  <p class="subtitle">An LLM serving platform built to be attacked, plus an AI SRE that defends it</p>
  <div class="stack">Go · Gin · Python · LangGraph · MCP · Ollama · React · Kubernetes · Prometheus</div>
  <div class="kpis cover-kpis">
    <div><b>88% → 0%</b><span>errors under a 10× spike</span></div>
    <div><b>3 s</b><span>to eject a degraded GPU</span></div>
    <div><b>5%</b><span>exposure of a bad model</span></div>
    <div><b>0%</b><span>errors after the AI SRE's fix</span></div>
  </div>
  <div class="meta">Author: Kalyan Dahake · October 2026 · all results measured on a single CPU-only workstation</div>
</section>
<section class="toc brk"><h1>Contents</h1><ol>{toc}</ol></section>"""

    css = """
@page { size: A4; margin: 16mm 15mm 18mm 15mm; }
@page :first { margin: 0; }
* { box-sizing: border-box; }
body { font-family: 'Ubuntu', 'Liberation Sans', sans-serif; font-size: 10pt; line-height: 1.5; color: #1d232b; margin: 0; }
h1 { font-size: 19pt; margin: 0 0 10pt; color: #14213d; letter-spacing: -0.01em; }
h1 .num { display: inline-block; min-width: 28pt; color: #2f6fdb; }
h2 { font-size: 12pt; margin: 14pt 0 5pt; color: #14213d; }
p { margin: 0 0 7pt; }
ul, ol { margin: 0 0 8pt; padding-left: 16pt; } li { margin: 2pt 0; }
code { font-family: 'Ubuntu Mono', monospace; font-size: 9pt; background: #f0f2f5; padding: 0 3px; border-radius: 3px; }
pre { font-family: 'Ubuntu Mono', monospace; font-size: 8.6pt; line-height: 1.35; background: #f5f7fa; border: 1px solid #dde1e7;
      border-left: 3px solid #2f6fdb; padding: 7pt 9pt; border-radius: 4px; white-space: pre-wrap; break-inside: avoid; margin: 4pt 0 9pt; }
table { width: 100%; border-collapse: collapse; margin: 4pt 0 10pt; font-size: 8.8pt; break-inside: auto; }
th { background: #14213d; color: #fff; text-align: left; padding: 4pt 6pt; font-weight: 500; }
td { padding: 3.5pt 6pt; border-bottom: 1px solid #e3e6ea; vertical-align: top; }
tr { break-inside: avoid; } tbody tr:nth-child(even) td { background: #f8f9fb; }
.section.brk, .brk { break-before: page; }
.two { display: grid; grid-template-columns: 1.15fr 1fr; gap: 12pt; break-inside: avoid; }
.two h2 { margin-top: 8pt; }
.lead { font-size: 11pt; color: #33404f; }
.note { font-size: 9pt; color: #5d6673; border-left: 3px solid #b7791f; padding-left: 8pt; }
.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8pt; margin: 10pt 0 12pt; }
.kpis div { border: 1px solid #dde1e7; border-radius: 6px; padding: 8pt; background: #f8faff; }
.kpis b { display: block; font-size: 15pt; color: #2f6fdb; }
.kpis span { font-size: 8.5pt; color: #5d6673; }
figure.diagram { margin: 8pt 0 12pt; break-inside: avoid; border: 1px solid #e3e6ea; border-radius: 6px; padding: 8pt; }
figcaption { font-size: 8.5pt; color: #5d6673; margin-top: 4pt; text-align: center; }
svg .bt { font: 600 12px 'Ubuntu', sans-serif; }
svg .bs { font: 10.5px 'Ubuntu', sans-serif; fill: #3d4652; }
svg .al { font: 10px 'Ubuntu', sans-serif; fill: #3d4652; }
svg .gl { font: 600 10.5px 'Ubuntu', sans-serif; }
.cover { width: 210mm; height: 297mm; display: flex; flex-direction: column; justify-content: center; padding: 0 22mm;
         background: linear-gradient(160deg, #14213d 0%, #1f3a6e 60%, #2f6fdb 100%); color: #fff; margin: 0;
         -webkit-print-color-adjust: exact; print-color-adjust: exact; }
th, .kpis div, pre, tbody tr:nth-child(even) td { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.cover h1 { color: #fff; font-size: 34pt; margin: 6pt 0; }
.cover .tag { text-transform: uppercase; letter-spacing: 0.14em; font-size: 9pt; color: #a9c4ff; }
.cover .subtitle { font-size: 14pt; color: #dbe6ff; max-width: 150mm; }
.cover .stack { margin-top: 10pt; font-family: 'Ubuntu Mono', monospace; color: #a9c4ff; }
.cover-kpis div { background: rgba(255,255,255,0.08); border-color: rgba(255,255,255,0.25); }
.cover-kpis b { color: #fff; } .cover-kpis span { color: #c9d8f5; }
.cover .meta { margin-top: 26pt; font-size: 9pt; color: #c9d8f5; }
.toc ol { list-style: none; padding: 0; font-size: 12pt; }
.toc li { padding: 6pt 0; border-bottom: 1px solid #e3e6ea; }
.toc .tn { display: inline-block; width: 28pt; color: #2f6fdb; font-weight: 600; }
.toc a { color: #1d232b; text-decoration: none; }
"""
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>AI Inference Battlefield — Design</title>
<style>{css}</style></head><body>{cover}{''.join(parts)}</body></html>"""
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(doc)


def render():
    chrome = next((c for c in ("google-chrome", "chromium", "chromium-browser") if subprocess.call(
        ["which", c], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0), None)
    if not chrome:
        sys.exit("need google-chrome or chromium to print the PDF (HTML written to %s)" % OUT_HTML)
    subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                    "--print-to-pdf=" + OUT_PDF, "file://" + OUT_HTML],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("wrote", OUT_PDF)


if __name__ == "__main__":
    build()
    render()
