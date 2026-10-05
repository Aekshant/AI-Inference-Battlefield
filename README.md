# ⚔️ AI Inference Battlefield

A small LLM serving system built to be attacked. Each mission breaks it in a new way; you fight back with the gateway's defenses, a rollout controller, and finally an AI SRE agent running on a local Ollama model.

```
 users ─► attacker (load + chaos) ─► AI Gateway (Go/Gin) ─┬─► gpu-0 ┐
                                       admission, routing,  ├─► gpu-1 ├─ stable pool (model v1)
                                       breakers, retries,   ├─► gpu-2 ┘
                                       canary split, quotas └─► gpu-canary ── candidate (v2: worse)
 rollout controller (Go) ── eval gate + canary health gates + edge fleet hub ◄── 50 edge devices
 Prometheus + Grafana ◄── everything
 AI SRE (Python, LangGraph + MCP, qwen2.5:3b on host Ollama) ─► human approval ─► actions
 React console ─► all of the above
```

| Part | Stack | Where |
|---|---|---|
| AI Gateway | Go 1.27 + Gin | [gateway/](gateway/) |
| GPU worker (simulated GPU, optional real Ollama) | Python FastAPI | [worker/](worker/) |
| Attacker (open-loop load + chaos) | Python aiohttp | [attacker/](attacker/) |
| Rollout controller + fleet hub | Go + Gin | [controller/](controller/) |
| Edge fleet (50 devices) | Python | [edge/](edge/) |
| Eval gate (golden set) | Python, stdlib | [evals/](evals/) |
| AI SRE agent + MCP server | Python, LangGraph, MCP, Ollama | [agent/](agent/) |
| Console | React 19 + Vite + TS | [console/](console/) |
| Cluster | kind (1 control plane + 2 workers) | [deploy/](deploy/) |

## Run it

Prerequisites: Docker, Go, Node, kind, kubectl, Ollama with `qwen2.5:3b` pulled.

```bash
make up            # kind cluster + build + load + deploy (≈2 min after first image pulls)
make console       # http://127.0.0.1:5173
make agent         # AI SRE on 127.0.0.1:8090 (uses local Ollama)
```

Everything binds to `127.0.0.1` only: gateway :8080, attacker :8081, controller :8082, Prometheus :9090, Grafana :3000, agent :8090, console :5173.

Useful targets (`make help` lists all):

```bash
make status                   # gateway's view of every GPU
make spike DUR=60             # 1000 req/s      make baseline / long-prompts / noisy-tenant
make watch                    # client-side live view
make chaos-slow GPU=gpu-2     # also chaos-kv, chaos-errors, chaos-reset, kill-pod, pod-killer
make defenses-naive           # / defenses-hardened
make eval && make canary-start
make edge-partition DUR=60    # / fleet-rollout VERSION=edge-v2-bad
python3 scripts/experiment.py --defenses hardened --scenario spike --duration 40   # before/after numbers
```

## The design choice that makes this work on a laptop

There are no GPUs here. Each `gpu-N` pod is a **simulated GPU**: it enforces batch slots, a KV-cache token budget, prefill/decode speed, continuous-batching slowdown and KV-pressure slowdown, and exposes `/state` + `/metrics`. In `synthetic` mode it fakes the token stream, which is how 1000 req/s attacks are possible on CPU. In `real` mode (`MODE=real`) it proxies to Ollama — see *Real model mode* below.

## Results (measured on this machine)

| Mission | Attack | Naive | With the right defenses |
|---|---|---|---|
| 02 | 1000 req/s spike | 88% errors, p99 8.9 s | **0% errors**, p99 2.3 s, excess shed fast |
| 03 | gpu-2 TTFT → 4 s | p99 20 s, 10% errors | gpu-2 ejected in **3 s**, 0% errors |
| 04 | 6k-token prompt flood | 21% errors, p99 24 s | 0% errors, short prompts p50 0.92 s, only `kv_full` shed |
| 05 | kill a GPU pod / hang a GPU process | 19% errors (DNS lag) / unbounded p99 | **0% errors** — EndpointSlice discovery makes pod deletion free even in naive mode; hangs need timeouts + admission |
| 06 | batch-jobs floods 500 req/s | acme 22% ok, globex 15% ok | acme/globex **100%** ok, batch-jobs held to quota |
| 07 | ship v2 (+80% latency, −12% accuracy, +40% cost) | — | eval gate rejects; with eval skipped, canary **rolls back at 5%**; a good v3 promotes 5→25→50→100% |
| 08 | partition 40% of edge devices | — | devices keep serving, **280 late records synced, 0 dropped** |
| 09 | ring-roll a broken edge model | (would hit 40%+ of fleet) | halted at canary ring: **4 / 50 devices**, auto-rollback |
| 10 | silent slow GPU, nobody paging | — | AI SRE pages itself, diagnoses the GPU, proposes the drain, verifies on user SLOs |

Details, including the bugs each mission exposed in my own defenses, are in [missions/](missions/).

## Real model mode (needs your decision)

The serving workers can stream real tokens from Ollama (`MODE=real`), but pods inside kind can only reach the host's Ollama if it listens on more than `127.0.0.1`. Changing that was blocked during setup because it exposes a local service, so it's left to you. Two options:

- **Safer:** bind Ollama to the kind bridge only (`OLLAMA_HOST=172.22.0.1:11434`; check yours with `docker network inspect kind -f '{{range .IPAM.Config}}{{.Gateway}} {{end}}'`). Pods can reach it; your LAN can't. The agent then needs `OLLAMA_URL=http://172.22.0.1:11434` too, since Ollama no longer listens on localhost.
- **Simplest:** `OLLAMA_HOST=0.0.0.0:11434`, which also exposes it to your network unless firewalled.

```bash
systemctl edit ollama        # [Service]  Environment="OLLAMA_HOST=172.22.0.1:11434"
systemctl restart ollama
kubectl -n battlefield set env statefulset/gpu MODE=real OLLAMA_URL=http://172.22.0.1:11434 OLLAMA_MODEL=qwen2.5:0.5b
```

Real mode on CPU serves a few requests per second, so keep traffic attacks in synthetic mode. The AI SRE agent doesn't need any of this: it runs on the host and uses Ollama on `127.0.0.1`.

## Notes

- Host Docker uses cgroup v1, which Kubernetes ≥1.35 refuses by default; [deploy/kind/cluster.yaml](deploy/kind/cluster.yaml) sets `failCgroupV1: false`. kind warns this support will go away; moving the host to cgroup v2 is the long-term fix.
- Gateway state (defenses, tenants, canary weight, GPU weights and ejections) and controller state (model registry and evals, fleet desired versions) are saved to PersistentVolumes and survive restarts. Both stay single-replica on purpose.
- The gateway discovers GPUs from EndpointSlices (falling back to DNS outside a cluster), so a deleted pod stops getting traffic within about a second.
