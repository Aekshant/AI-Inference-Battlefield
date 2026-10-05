# Mission 04: KV cache explosion (Attack #3)

**Setup:** `make long-prompts`: 60 req/s of chat plus 12 req/s of 6000-token prompts from tenant `globex`.

| | ok | errors | p99 |
|---|---|---|---|
| naive | 61.5% | **21%** | 24 s |
| hardened, first try | 21% | 0% | 11.8 s ← *worse goodput* |
| hardened, fixed | **88%** | **0%** | 3.4 s (short prompts p50 0.92 s) |

**The cascade:** long prompts legitimately have high TTFT (1.5 s of prefill) and queue for KV space until they time out. The gateway treated both as *GPU failures*, so it ejected GPUs and tripped breakers, which pushed their load onto the remaining GPUs. That's a cascading failure caused by the defenses themselves.

**Fixes:**
- Separate **overload** (503 capacity, 504 timeout) from **breakage** (connection error, 500, 502). Overload means retry elsewhere; it does not trip breakers or eject GPUs.
- Only short prompts feed the TTFT EWMAs.
- The gateway accounts for KV tokens it has in flight per GPU, because 500 ms polls are too stale. If nothing fits, it sheds `kv_full` immediately.
