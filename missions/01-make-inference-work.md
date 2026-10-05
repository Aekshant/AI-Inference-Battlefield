# Mission 01: Make inference work

**Setup:** `make baseline`. 80 req/s, round robin, all defenses off.

**Result:** 100% success, p50 0.88 s, p99 0.91 s, an even 33% traffic share per GPU.

**Learned:** with `max_tokens=32` the per-request service time is about 0.9 s (120 ms TTFT plus decode slowed by continuous batching). So the fleet's real capacity is about **100 req/s**, not the 150 that slot math suggests. Every later number depends on knowing this.
