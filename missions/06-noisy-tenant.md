# Mission 06: One tenant starves everyone

**Setup:** `make noisy-tenant`: acme 25 req/s, globex 25 req/s, batch-jobs 500 req/s.

| | acme ok | globex ok | batch-jobs ok |
|---|---|---|---|
| naive | 22% | 15% | 20% |
| hardened, first try | 58% | 30% | 8% |
| hardened, fixed | **100%** | **100%** | 8% (= its 40 req/s quota) |

**The bug:** the global token bucket was checked *before* tenant quotas, so batch-jobs' rejected excess still burned the shared tokens. Fix: check the tenant quota first, then the global limit. Weighted fair queueing (deficit round robin) and priority classes (interactive > standard > batch, with batch evicted first when the queue is full) handle what's left.
