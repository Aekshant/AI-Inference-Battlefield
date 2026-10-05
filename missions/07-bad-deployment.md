# Mission 07: Bad deployment (Attack #5)

v2 = `LATENCY_FACTOR=1.8`, `ORACLE_ACCURACY=0.85`, `COST=0.14` (v1: 1.0 / 0.95 / 0.10).

1. **Eval gate** (`make eval`, 40-question golden set × 3): v1 97.5% accuracy, p99 139 ms; v2 85.8%, p99 246 ms. `make canary-start` → **rejected**: accuracy below 0.90, p99 1.77× v1, cost 1.40× v1.
2. **Skip the eval** under live traffic: the canary health gates saw p99 TTFT 1.50× and cost per token 1.40× → **rolled back at 5%, about 10 s in**.
3. **Good v3:** promoted 5% → 25% → 50% → 100%.

**Bugs found on the way:**
- The gateway only understood `X-Model-Version: v2`, so the eval for "v3" silently ran against v1 and *passed*. Fix: route by the version each GPU reports, and return 404 for unknown versions.
- The gates used 1-minute windows that still held the previous canary's samples. Fix: query by `version` label, with a window that starts at the rollout.
- A good v3 was rolled back at 50% because one canary pod got half the traffic. **Canary capacity must scale with traffic share:** the controller now scales `gpu-canary` before each step (RBAC limited to that scale subresource).
- `histogram_quantile` returns NaN with no data, Go's JSON encoder refuses NaN, and `/rollouts/current` returned an empty 200. Fix: sanitize NaN to 0.
