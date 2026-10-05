# Mission 02: Destroy it with traffic (Attack #1)

**Setup:** `make spike` (1000 req/s, open loop).

| | ok | errors | p99 |
|---|---|---|---|
| naive | 12% | **88%** | 8.9 s |
| hardened | 10% (≈100 req/s, all the fleet can do) | **0%** | 2.3 s |

**Weapons:** admission control (token bucket, max in-flight = total GPU slots, a bounded queue with a 1 s deadline) and backpressure. Excess requests get a `429`/`503` with `Retry-After` in milliseconds instead of timing out after 9 s.

**Bugs this mission found in my own defenses:**
1. The long-prompt pool reserved "the last *eligible* GPU". Once one GPU was ejected, a different GPU got reserved and only one GPU served everything. Fix: reserve a fixed GPU per pool, and let short requests spill onto it while its KV cache is under 30% full.
2. Stale TTFT: an ejected GPU gets no traffic, so the gateway's EWMA for it never decays and it stays ejected forever. Fix: decay toward the GPU's self-reported TTFT while idle.
