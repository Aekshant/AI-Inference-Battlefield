# Mission 05: Model server dies (Attack #4)

## Round 1: pod deleted (`make kill-pod GPU=gpu-1`, or `make pod-killer`)

| | errors |
|---|---|
| naive, DNS discovery | **19.4%**, which didn't recover within 30 s |
| hardened, DNS discovery | 0.00% (pod killer every 20 s: 0.00%, 4.9% shed) |
| **naive, EndpointSlice discovery** | **0.00%** |

**Gotcha:** CoreDNS caches records for 30 s. With DNS discovery the gateway kept round-robining to the dead pod's IP. Worse, that IP blackholes connections (2 s connect timeout), so the backend always had requests in flight and was never removed.

**Fix:** the gateway lists ready endpoints from the Kubernetes EndpointSlice API every second (read-only RBAC). A backend that leaves the list is **never routed to again, with or without defenses**. That's a membership fact, not a health check. It is kept only until its in-flight requests drain. A clean pod deletion now costs users nothing even in naive mode.

## Round 2: hung process (`curl -XPOST :8080/admin/chaos/gpu-1 -d '{"hang":true}'`)

The pod stays Running and liveness passes. Only `/ready` fails, so the endpoint is withdrawn about 4 s later, and requests already sent there hang.

| | errors | p99 |
|---|---|---|
| naive | 2.6% (stuck requests time out after 30 s) | climbing without bound: 4.9 s at t=40 s and rising |
| hardened | **0%** | bounded at 2.7 s (9% shed while capacity is down) |

Attempt timeouts, retries before the first byte, admission control and backpressure are what make a hang survivable.

## Also found: a slot leak in the worker

After the final-boss run, gpu-1 reported `active=3` with no traffic. The cause was a race: a queued request's timeout firing in the same tick a slot was granted to it. Fixing that naively caused **double releases**, because the timeout cancels the future and the grant code couldn't tell. Now the waiter records the grant explicitly. [worker/test_slots.py](../worker/test_slots.py) races 500 timeouts and 100 cancellations against grants and checks the accounting stays exact (`make test`).
