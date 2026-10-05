# Mission 03: One GPU becomes slow (Attack #2)

**Setup:** baseline traffic, then `make chaos-slow GPU=gpu-2` (+4 s TTFT).

| | errors | p99 |
|---|---|---|
| naive (round robin) | 10.4% | **20.4 s** and climbing |
| hardened | **0%** | 2.7 s |

gpu-2 was outlier-ejected **3 s** after the fault and re-ejected every 15 s while it stayed slow.

**Cost:** two GPUs can't carry 80 req/s, so about 15% was shed. Ejection removes capacity; the real fix is to scale out, which becomes the AI SRE's follow-up in Mission 10.

**Routing modes available:** `round_robin`, `least_loaded`, `p2c` (power of two choices on a load score), and `prefix` (consistent hashing on the prompt prefix with bounded load, for prefix/KV cache reuse).
