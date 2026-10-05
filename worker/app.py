"""Simulated GPU inference worker.

Each pod pretends to be one GPU serving an LLM. It enforces the limits a real
GPU has (concurrent batch slots, a KV-cache token budget, prefill/decode speed)
and exposes the signals a smart gateway needs (/state, /metrics). Faults can be
injected through /chaos so the gateway, rollout controller and AI SRE agent have
something real to fight.

Modes:
  synthetic  fake token stream with realistic timing (used for 1000 req/s attacks)
  real       proxy to Ollama (OLLAMA_URL) and stream real tokens
"""

import asyncio
import hashlib
import json
import os
import random
import time
from collections import OrderedDict

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

NODE_ID = os.getenv("NODE_ID") or os.getenv("HOSTNAME", "gpu-local")
POOL = os.getenv("POOL", "stable")
MODEL_VERSION = os.getenv("MODEL_VERSION", "v1")
MODE = os.getenv("MODE", "synthetic")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b")

SLOTS = int(os.getenv("SLOTS", "32"))
MAX_QUEUE = int(os.getenv("MAX_QUEUE", "256"))
KV_BUDGET = int(os.getenv("KV_BUDGET_TOKENS", "65536"))
BASE_TTFT_MS = float(os.getenv("BASE_TTFT_MS", "120"))
PREFILL_TPS = float(os.getenv("PREFILL_TPS", "4000"))  # prompt tokens/sec
DECODE_TPS = float(os.getenv("DECODE_TPS", "60"))  # output tokens/sec per request
# Model-quality knobs (used to simulate a bad v2 deployment)
LATENCY_FACTOR = float(os.getenv("LATENCY_FACTOR", "1.0"))
ORACLE_ACCURACY = float(os.getenv("ORACLE_ACCURACY", "0.95"))
COST_PER_1K_TOKENS = float(os.getenv("COST_PER_1K_TOKENS", "0.10"))

WORDS = ("the gateway routes tokens through healthy gpus while the queue drains and "
         "latency stays inside the budget so users never notice the attack").split()

# ---------------------------------------------------------------- metrics
L = ["node", "pool", "version"]
LV = dict(node=NODE_ID, pool=POOL, version=MODEL_VERSION)
m_requests = Counter("worker_requests_total", "Requests by outcome", L + ["outcome"])
m_ttft = Histogram("worker_ttft_seconds", "Time to first token", L,
                   buckets=(.05, .1, .2, .3, .5, .75, 1, 1.5, 2, 3, 5, 8, 13, 20))
m_latency = Histogram("worker_request_seconds", "End-to-end request time", L,
                      buckets=(.1, .25, .5, 1, 2, 3, 5, 8, 13, 20, 30, 60))
m_tokens = Counter("worker_output_tokens_total", "Generated tokens", L)
m_prompt_tokens = Counter("worker_prompt_tokens_total", "Prompt tokens", L)
m_cost = Counter("worker_cost_dollars_total", "Simulated serving cost", L)
m_queue = Gauge("worker_queue_depth", "Requests waiting for a slot", L)
m_active = Gauge("worker_active_requests", "Requests holding a slot", L)
m_slots = Gauge("worker_slots", "Batch slots", L)
m_kv_used = Gauge("worker_kv_used_tokens", "KV cache tokens in use", L)
m_kv_budget = Gauge("worker_kv_budget_tokens", "KV cache capacity", L)
m_kv_util = Gauge("worker_kv_utilization", "KV cache utilization 0..1", L)
m_tps = Gauge("worker_tokens_per_second", "Aggregate decode throughput (EWMA)", L)
m_ttft_ewma = Gauge("worker_ttft_ewma_seconds", "TTFT EWMA", L)
m_prefix_hits = Counter("worker_prefix_cache_hits_total", "Prefix cache hits", L)
m_chaos = Gauge("worker_chaos_active", "1 when a fault is injected", L + ["fault"])


class Chaos:
    def __init__(self):
        self.reset()

    def reset(self):
        self.slow_ttft_ms = 0.0
        self.decode_factor = 1.0  # <1 = slower decode
        self.error_rate = 0.0
        self.kv_budget = KV_BUDGET
        self.hang = False

    def as_dict(self):
        return dict(slow_ttft_ms=self.slow_ttft_ms, decode_factor=self.decode_factor,
                    error_rate=self.error_rate, kv_budget=self.kv_budget, hang=self.hang)


class GPU:
    """Slot + KV accounting. Waiters are served FIFO when capacity frees up."""

    def __init__(self):
        self.active = 0
        self.kv_used = 0
        self.waiters: "OrderedDict[int, tuple]" = OrderedDict()
        self._seq = 0
        self.ttft_ewma = BASE_TTFT_MS / 1000
        self.last_ttft = 0.0  # monotonic time of the last first-token observation
        self.tps_ewma = 0.0
        self._tok_window = 0
        self.prefix_cache: "OrderedDict[str, None]" = OrderedDict()

    def _fits(self, kv):
        return self.active < SLOTS and self.kv_used + kv <= chaos.kv_budget

    async def acquire(self, kv, timeout):
        if kv > chaos.kv_budget:
            raise HTTPException(413, "request larger than KV cache")
        if not self.waiters and self._fits(kv):
            self._take(kv)
            return
        if len(self.waiters) >= MAX_QUEUE:
            raise HTTPException(503, "worker queue full")
        # waiter = [kv, future, granted]; "granted" is the only reliable record of
        # slot ownership - the future may be cancelled by a timeout in the same tick.
        w = [kv, asyncio.get_running_loop().create_future(), False]
        self._seq += 1
        key = self._seq
        self.waiters[key] = w
        try:
            await asyncio.wait_for(asyncio.shield(w[1]), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            self.waiters.pop(key, None)
            if w[2]:
                self.release(kv)  # slot was granted as we gave up: hand it back
            if isinstance(e, asyncio.CancelledError):
                raise
            raise HTTPException(503, "timed out waiting for GPU slot")

    def _take(self, kv):
        self.active += 1
        self.kv_used += kv

    def release(self, kv):
        self.active -= 1
        self.kv_used -= kv
        self.pump()

    def pump(self):
        while self.waiters:
            key, w = next(iter(self.waiters.items()))
            if not self._fits(w[0]):
                break
            self.waiters.pop(key)
            self._take(w[0])
            w[2] = True
            if not w[1].done():
                w[1].set_result(True)

    def kv_util(self):
        return min(1.0, self.kv_used / max(1, chaos.kv_budget))

    def prefix_hit(self, prompt):
        key = hashlib.sha1(prompt[:512].encode()).hexdigest()
        hit = key in self.prefix_cache
        self.prefix_cache[key] = None
        self.prefix_cache.move_to_end(key)
        while len(self.prefix_cache) > 2048:
            self.prefix_cache.popitem(last=False)
        return hit


chaos = Chaos()
gpu = GPU()
app = FastAPI(title=f"gpu-worker {NODE_ID}")
http = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=3))


def est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def decode_rate():
    """Per-request decode speed. Continuous batching shares compute, and a nearly
    full KV cache forces preemption/swapping, which slows everyone down."""
    batch_penalty = 1 + 0.02 * max(0, gpu.active - 1)
    kv = gpu.kv_util()
    kv_penalty = 1 + (kv - 0.85) * 12 if kv > 0.85 else 1
    return DECODE_TPS * chaos.decode_factor / batch_penalty / kv_penalty / LATENCY_FACTOR


def answer_tokens(body, n):
    expected = body.get("eval_expected")
    if expected is not None:
        # Simulated model quality: the "oracle" answers correctly with ORACLE_ACCURACY
        ans = expected if random.random() < ORACLE_ACCURACY else random.choice(WORDS)
        return [ans]
    return [random.choice(WORDS) for _ in range(n)]


async def synthetic_stream(body, prompt_tokens, max_tokens):
    hit = gpu.prefix_hit(body["prompt"])
    if hit:
        m_prefix_hits.labels(**LV).inc()
    prefill = prompt_tokens / PREFILL_TPS * (0.2 if hit else 1.0)
    ttft = (BASE_TTFT_MS / 1000 + prefill) * LATENCY_FACTOR + chaos.slow_ttft_ms / 1000
    ttft *= random.uniform(0.9, 1.1)
    await asyncio.sleep(ttft)
    yield None  # marks first token time
    for i, tok in enumerate(answer_tokens(body, max_tokens)):
        if i:
            await asyncio.sleep(1 / decode_rate())
        yield tok + " "


async def real_stream(body, prompt_tokens, max_tokens):
    if chaos.slow_ttft_ms:
        await asyncio.sleep(chaos.slow_ttft_ms / 1000)
    first = True
    req = {"model": OLLAMA_MODEL, "prompt": body["prompt"], "stream": True,
           "options": {"num_predict": max_tokens}}
    async with http.stream("POST", f"{OLLAMA_URL}/api/generate", json=req) as r:
        if r.status_code != 200:
            raise HTTPException(502, f"ollama {r.status_code}")
        async for line in r.aiter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if first:
                yield None
                first = False
            if chunk.get("response"):
                yield chunk["response"]
            if chunk.get("done"):
                break


@app.post("/v1/generate")
async def generate(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    max_tokens = int(body.get("max_tokens", 32))
    stream = bool(body.get("stream", False))
    deadline = float(body.get("queue_timeout_s", 10))
    prompt_tokens = est_tokens(prompt)
    kv = prompt_tokens + max_tokens
    t0 = time.monotonic()

    if chaos.hang:
        await asyncio.sleep(3600)
    if chaos.error_rate and random.random() < chaos.error_rate:
        m_requests.labels(**LV, outcome="error").inc()
        raise HTTPException(500, "injected fault")

    try:
        await gpu.acquire(kv, deadline)
    except HTTPException as e:
        m_requests.labels(**LV, outcome="rejected").inc()
        raise e

    gen = (real_stream if MODE == "real" else synthetic_stream)(body, prompt_tokens, max_tokens)
    meta = {"node": NODE_ID, "pool": POOL, "version": MODEL_VERSION}

    async def run():
        """Yields (kind, value) events and always releases the slot."""
        n = 0
        ttft = None
        try:
            async for tok in gen:
                if tok is None:
                    ttft = time.monotonic() - t0
                    m_ttft.labels(**LV).observe(ttft)
                    if prompt_tokens < 500:  # long-prompt prefill isn't sickness
                        gpu.ttft_ewma = 0.8 * gpu.ttft_ewma + 0.2 * ttft
                        gpu.last_ttft = time.monotonic()
                    yield "first", ttft
                    continue
                n += 1
                gpu._tok_window += 1
                yield "token", tok
            total = time.monotonic() - t0
            m_latency.labels(**LV).observe(total)
            m_tokens.labels(**LV).inc(n)
            m_prompt_tokens.labels(**LV).inc(prompt_tokens)
            m_cost.labels(**LV).inc((n + prompt_tokens) / 1000 * COST_PER_1K_TOKENS)
            m_requests.labels(**LV, outcome="ok").inc()
            yield "done", dict(meta, ttft_ms=round((ttft or total) * 1000, 1),
                               total_ms=round(total * 1000, 1), tokens=n,
                               prompt_tokens=prompt_tokens,
                               cost=round((n + prompt_tokens) / 1000 * COST_PER_1K_TOKENS, 6))
        except Exception:
            m_requests.labels(**LV, outcome="error").inc()
            raise
        finally:
            gpu.release(kv)

    if stream:
        async def sse():
            async for kind, val in run():
                if kind == "token":
                    yield f"data: {json.dumps({'token': val})}\n\n"
                elif kind == "done":
                    yield f"data: {json.dumps(dict(val, done=True))}\n\n"
                else:
                    yield ": first-token\n\n"
        return StreamingResponse(sse(), media_type="text/event-stream",
                                 headers={"X-Node": NODE_ID})

    text = []
    done = {}
    async for kind, val in run():
        if kind == "token":
            text.append(val)
        elif kind == "done":
            done = val
    return JSONResponse(dict(done, text="".join(text).strip()), headers={"X-Node": NODE_ID})


@app.get("/state")
async def state():
    if chaos.hang:
        await asyncio.sleep(30)  # a hung process doesn't answer health polls either
    return dict(node=NODE_ID, pool=POOL, version=MODEL_VERSION, mode=MODE,
                slots=SLOTS, active=gpu.active, queue_depth=len(gpu.waiters),
                kv_used=gpu.kv_used, kv_budget=chaos.kv_budget, kv_util=round(gpu.kv_util(), 3),
                ttft_ewma_ms=round(gpu.ttft_ewma * 1000, 1), tokens_per_sec=round(gpu.tps_ewma, 1),
                chaos=chaos.as_dict())


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/ready")
async def ready():
    if chaos.hang:
        raise HTTPException(503, "hung")
    return {"ok": True}


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/chaos")
async def set_chaos(request: Request):
    body = await request.json()
    for k in ("slow_ttft_ms", "decode_factor", "error_rate"):
        if k in body:
            setattr(chaos, k, float(body[k]))
    if "kv_budget" in body:
        chaos.kv_budget = int(body["kv_budget"])
        gpu.pump()
    if "hang" in body:
        chaos.hang = bool(body["hang"])
    return chaos.as_dict()


@app.post("/chaos/reset")
async def reset_chaos():
    chaos.reset()
    gpu.pump()
    return chaos.as_dict()


@app.post("/chaos/crash")
async def crash():
    asyncio.get_running_loop().call_later(0.1, os._exit, 1)
    return {"crashing": NODE_ID}


async def sampler():
    """Refresh gauges once per second."""
    while True:
        await asyncio.sleep(1)
        gpu.tps_ewma = 0.7 * gpu.tps_ewma + 0.3 * gpu._tok_window
        gpu._tok_window = 0
        m_queue.labels(**LV).set(len(gpu.waiters))
        m_active.labels(**LV).set(gpu.active)
        m_slots.labels(**LV).set(SLOTS)
        m_kv_used.labels(**LV).set(gpu.kv_used)
        m_kv_budget.labels(**LV).set(chaos.kv_budget)
        m_kv_util.labels(**LV).set(gpu.kv_util())
        m_tps.labels(**LV).set(gpu.tps_ewma)
        m_ttft_ewma.labels(**LV).set(gpu.ttft_ewma)
        m_chaos.labels(**LV, fault="slow").set(1 if chaos.slow_ttft_ms or chaos.decode_factor < 1 else 0)
        m_chaos.labels(**LV, fault="errors").set(1 if chaos.error_rate else 0)
        m_chaos.labels(**LV, fault="kv_squeeze").set(1 if chaos.kv_budget < KV_BUDGET else 0)
        m_chaos.labels(**LV, fault="hang").set(1 if chaos.hang else 0)
        # No fresh observations (idle, drained or ejected): decay toward what a new
        # request would see now, so a recovered GPU can earn its traffic back.
        if time.monotonic() - gpu.last_ttft > 3:
            gpu.ttft_ewma = 0.7 * gpu.ttft_ewma + 0.3 * (BASE_TTFT_MS / 1000 + chaos.slow_ttft_ms / 1000)


@app.on_event("startup")
async def startup():
    asyncio.create_task(sampler())
