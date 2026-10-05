"""Attacker: open-loop traffic generator + chaos orchestrator.

It measures what *users* experience (client-side latency, errors, sheds), which
is the number that matters when the gateway claims everything is fine.

    POST /attacks            {"scenario": "spike", "duration_s": 60}
    GET  /attacks            live stats for every run
    POST /attacks/stop       stop everything
    POST /chaos/kill-pod     {"pod": "gpu-1"} or {"random": true}
    POST /chaos/pod-killer   {"interval_s": 20, "duration_s": 120}
"""

import asyncio
import os
import random
import ssl
import string
import time
import uuid
from collections import deque

import aiohttp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

GATEWAY = os.getenv("GATEWAY_URL", "http://gateway:8080")
NAMESPACE = os.getenv("NAMESPACE", "battlefield")
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "4000"))

m_req = Counter("attacker_requests_total", "Client-side outcomes", ["scenario", "tenant", "outcome"])
m_lat = Histogram("attacker_latency_seconds", "Client-side latency of successful requests",
                  ["scenario", "tenant"], buckets=(.1, .25, .5, .75, 1, 1.5, 2, 3, 5, 8, 13, 20, 30))
m_kills = Counter("attacker_pod_kills_total", "Pods deleted by chaos", ["pod"])

# Shared system prompts give prefix-aware routing something to reuse.
SYSTEM_PROMPTS = [f"You are assistant #{i}. Policy: " + " ".join(
    random.Random(i).choice(["be brief", "cite sources", "use json", "be kind", "think step by step"])
    for _ in range(40)) for i in range(16)]

# A stream is one traffic source: rate, tenant and request shape.
SCENARIOS = {
    "baseline": [dict(rate=80)],
    "spike": [dict(rate=1000)],
    "ramp": [dict(rate=80, ramp_to=1000)],
    "long_prompts": [dict(rate=60), dict(rate=12, prompt_chars=24000, max_tokens=64, tenant="globex")],
    "noisy_tenant": [dict(rate=25, tenant="acme"), dict(rate=25, tenant="globex"),
                     dict(rate=500, tenant="batch-jobs")],
    "mixed": [dict(rate=60, tenant="acme"), dict(rate=40, tenant="globex", stream=True),
              dict(rate=20, tenant="batch-jobs", max_tokens=64)],
}

app = FastAPI(title="attacker")
runs: dict = {}
session: aiohttp.ClientSession = None
sem: asyncio.Semaphore = None
killer_task = None
kill_log: deque = deque(maxlen=50)


def make_prompt(chars):
    sys_p = random.choice(SYSTEM_PROMPTS)
    user = "".join(random.choices(string.ascii_lowercase + " ", k=max(16, chars - len(sys_p))))
    return (sys_p + "\nUser: " + user)[:max(chars, 40)]


class Run:
    def __init__(self, scenario, streams, duration):
        self.id = uuid.uuid4().hex[:8]
        self.scenario = scenario
        self.streams = streams
        self.duration = duration
        self.started = time.time()
        self.stopped = False
        self.seconds: deque = deque(maxlen=300)  # per-second buckets
        self.totals = dict(sent=0, ok=0, shed=0, error=0, fallback=0, dropped=0)
        self.by_tenant: dict = {}
        self.tenant_lat: dict = {}
        self.tasks = []

    def bucket(self):
        now = int(time.time())
        if not self.seconds or self.seconds[-1]["t"] != now:
            self.seconds.append(dict(t=now, sent=0, ok=0, shed=0, error=0, fallback=0, lat=[]))
        return self.seconds[-1]

    def record(self, outcome, tenant, latency=None):
        b = self.bucket()
        b[outcome] = b.get(outcome, 0) + 1
        self.totals[outcome] = self.totals.get(outcome, 0) + 1
        bt = self.by_tenant.setdefault(tenant, {})
        bt[outcome] = bt.get(outcome, 0) + 1
        if latency is not None:
            tl = self.tenant_lat.setdefault(tenant, deque(maxlen=2000))
            tl.append(latency)
        m_req.labels(self.scenario, tenant, outcome).inc()
        if latency is not None:
            m_lat.labels(self.scenario, tenant).observe(latency)
            if len(b["lat"]) < 5000:
                b["lat"].append(latency)

    def summary(self):
        def pct(v, p):
            if not v:
                return 0
            v = sorted(v)
            return round(v[min(len(v) - 1, int(len(v) * p))] * 1000, 1)
        recent = list(self.seconds)[-61:-1]
        lat = [x for s in recent[-10:] for x in s["lat"]]
        return dict(
            id=self.id, scenario=self.scenario, streams=self.streams,
            elapsed_s=round(time.time() - self.started, 1), duration_s=self.duration,
            running=not self.stopped, totals=self.totals,
            tenants={t: dict(v, p99_ms=pct(list(self.tenant_lat.get(t, [])), .99))
                     for t, v in self.by_tenant.items()},
            last10s=dict(p50_ms=pct(lat, .5), p99_ms=pct(lat, .99),
                         ok=sum(s["ok"] for s in recent[-10:]), shed=sum(s["shed"] for s in recent[-10:]),
                         error=sum(s["error"] for s in recent[-10:]), sent=sum(s["sent"] for s in recent[-10:])),
            series=[dict(t=s["t"], sent=s["sent"], ok=s["ok"], shed=s["shed"], error=s["error"],
                         fallback=s["fallback"], p99_ms=pct(s["lat"], .99)) for s in recent],
        )


async def one_request(run: Run, st: dict):
    tenant = st.get("tenant", "default")
    body = dict(prompt=make_prompt(st.get("prompt_chars", 400)), max_tokens=st.get("max_tokens", 32),
                stream=st.get("stream", False), tenant=tenant)
    if st.get("sessions"):
        body["session"] = f"s{random.randrange(st['sessions'])}"
    t0 = time.monotonic()
    run.bucket()["sent"] += 1
    run.totals["sent"] += 1
    try:
        async with session.post(f"{GATEWAY}/v1/generate", json=body,
                                headers={"X-Tenant": tenant}) as r:
            await r.read()
            dt = time.monotonic() - t0
            if r.status == 200:
                run.record("fallback" if r.headers.get("X-Fallback") else "ok", tenant,
                           None if r.headers.get("X-Fallback") else dt)
            elif r.status in (429, 503) and "shed" in (await r.text()):
                run.record("shed", tenant)
            else:
                run.record("error", tenant)
    except Exception:
        run.record("error", tenant)
    finally:
        sem.release()


async def drive(run: Run, st: dict):
    """Open-loop arrivals: requests fire on schedule whether or not the system keeps up."""
    acc = 0.0
    tick = 0.01
    t_start = time.monotonic()
    while not run.stopped and time.monotonic() - t_start < run.duration:
        frac = (time.monotonic() - t_start) / run.duration
        rate = st["rate"] + (st.get("ramp_to", st["rate"]) - st["rate"]) * frac
        acc += rate * tick
        while acc >= 1:
            acc -= 1
            if sem.locked():
                run.record("dropped", st.get("tenant", "default"))
                continue
            await sem.acquire()
            asyncio.create_task(one_request(run, st))
        await asyncio.sleep(tick)


async def supervise(run: Run):
    await asyncio.gather(*[drive(run, st) for st in run.streams])
    run.stopped = True


@app.on_event("startup")
async def startup():
    global session, sem
    conn = aiohttp.TCPConnector(limit=MAX_CONCURRENCY, ttl_dns_cache=10)
    session = aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=30))
    sem = asyncio.Semaphore(MAX_CONCURRENCY)


@app.get("/scenarios")
async def scenarios():
    return SCENARIOS


@app.post("/attacks")
async def start(request: Request):
    body = await request.json()
    name = body.get("scenario", "baseline")
    streams = body.get("streams") or SCENARIOS.get(name)
    if not streams:
        raise HTTPException(404, f"unknown scenario; have {list(SCENARIOS)}")
    mult = float(body.get("rate_multiplier", 1))
    streams = [dict(s, rate=s["rate"] * mult, **({"ramp_to": s["ramp_to"] * mult} if "ramp_to" in s else {}))
               for s in streams]
    run = Run(name, streams, float(body.get("duration_s", 60)))
    runs[run.id] = run
    for old in list(runs)[:-20]:
        runs.pop(old)
    run.tasks.append(asyncio.create_task(supervise(run)))
    return run.summary()


@app.get("/attacks")
async def list_runs():
    return [r.summary() for r in runs.values()]


@app.post("/attacks/stop")
async def stop_all():
    for r in runs.values():
        r.stopped = True
    return {"stopped": len(runs)}


# ---------------------------------------------------------------- k8s chaos

def k8s():
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    if not os.path.exists(token_path):
        raise HTTPException(500, "not running in-cluster")
    token = open(token_path).read()
    ctx = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    return "https://kubernetes.default.svc", {"Authorization": f"Bearer {token}"}, ctx


async def list_gpu_pods():
    base, headers, ctx = k8s()
    url = f"{base}/api/v1/namespaces/{NAMESPACE}/pods?labelSelector=app%3Dgpu"
    async with session.get(url, headers=headers, ssl=ctx) as r:
        data = await r.json()
    return [p["metadata"]["name"] for p in data.get("items", [])
            if p["status"].get("phase") == "Running" and not p["metadata"].get("deletionTimestamp")]


async def kill(pod):
    base, headers, ctx = k8s()
    url = f"{base}/api/v1/namespaces/{NAMESPACE}/pods/{pod}?gracePeriodSeconds=0"
    async with session.delete(url, headers=headers, ssl=ctx) as r:
        if r.status >= 300:
            raise HTTPException(r.status, await r.text())
    m_kills.labels(pod).inc()
    kill_log.append(dict(ts=time.time(), pod=pod))
    return pod


@app.post("/chaos/kill-pod")
async def kill_pod(request: Request):
    body = await request.json()
    pod = body.get("pod")
    if body.get("random") or not pod:
        pods = await list_gpu_pods()
        if not pods:
            raise HTTPException(409, "no running gpu pods")
        pod = random.choice(pods)
    return {"killed": await kill(pod)}


@app.post("/chaos/pod-killer")
async def pod_killer(request: Request):
    global killer_task
    body = await request.json()
    interval, duration = float(body.get("interval_s", 20)), float(body.get("duration_s", 120))

    async def loop():
        end = time.monotonic() + duration
        while time.monotonic() < end:
            pods = await list_gpu_pods()
            if pods:
                await kill(random.choice(pods))
            await asyncio.sleep(interval)

    if killer_task and not killer_task.done():
        killer_task.cancel()
    killer_task = asyncio.create_task(loop())
    return {"pod_killer": dict(interval_s=interval, duration_s=duration)}


@app.post("/chaos/edge/{action}")
async def edge_chaos(action: str, request: Request):
    """Fan a chaos call (partition | network | reset) out to every edge pod."""
    if action not in ("partition", "network", "reset"):
        raise HTTPException(404, "partition | network | reset")
    body = await request.json() if action != "reset" else {}
    base, headers, ctx = k8s()
    url = f"{base}/api/v1/namespaces/{NAMESPACE}/pods?labelSelector=app%3Dedge"
    async with session.get(url, headers=headers, ssl=ctx) as r:
        pods = [(p["metadata"]["name"], p["status"].get("podIP")) for p in (await r.json()).get("items", [])
                if p["status"].get("podIP")]
    out = {}
    for name, ip in pods:
        try:
            async with session.post(f"http://{ip}:8000/chaos/{action}", json=body) as r:
                out[name] = await r.json()
        except Exception as e:
            out[name] = str(e)
    return out


@app.get("/chaos/kills")
async def kills():
    return list(kill_log)


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
async def health():
    return {"ok": True}
