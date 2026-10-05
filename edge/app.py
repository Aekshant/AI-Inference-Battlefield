"""Edge fleet simulator: many small devices in one pod.

Each device runs a local model (simulated), produces telemetry every 2s and
uploads it to the fleet hub. When the network is partitioned the device keeps
serving locally and buffers telemetry (bounded); when the network returns it
syncs the backlog. Devices pull the desired model version for their ring and
install it (takes a few seconds). A model whose name contains "bad" fails ~30%
of local inferences.

Chaos:
    POST /chaos/partition {"fraction": 0.4, "duration_s": 60}
    POST /chaos/network   {"latency_ms": 800, "loss": 0.2}
    POST /chaos/reset
"""

import asyncio
import hashlib
import os
import random
import socket
import time
from collections import deque

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

HUB = os.getenv("HUB_URL", "http://controller.battlefield:8082")
POD = os.getenv("POD_NAME", socket.gethostname())
DEVICES = int(os.getenv("DEVICES", "25"))
BUFFER_MAX = int(os.getenv("BUFFER_MAX", "300"))

m_buffered = Gauge("edge_buffered_records", "Telemetry waiting to be uploaded", ["pod"])
m_partitioned = Gauge("edge_partitioned_devices", "Devices currently cut off", ["pod"])
m_dropped = Counter("edge_dropped_records_total", "Telemetry lost to full buffers", ["pod"])
m_uploads = Counter("edge_uploads_total", "Upload attempts", ["pod", "result"])
m_infer = Counter("edge_inferences_total", "Local inferences", ["pod", "version", "ok"])


def ring_of(dev_id):
    h = int(hashlib.sha1(dev_id.encode()).hexdigest(), 16) % 100
    return "canary" if h < 10 else "early" if h < 40 else "broad"


class Net:
    latency_ms = 0.0
    loss = 0.0


class Device:
    def __init__(self, i):
        self.id = f"{POD}-d{i:02d}"
        self.ring = ring_of(self.id)
        self.version = "edge-v1"
        self.installing = False
        self.partitioned_until = 0.0
        self.buffer: deque = deque()
        self.dropped = 0

    @property
    def partitioned(self):
        return time.time() < self.partitioned_until

    def infer(self):
        bad = "bad" in self.version
        ok = random.random() > (0.3 if bad else 0.01)
        latency = random.gauss(180 if bad else 60, 15)
        m_infer.labels(POD, self.version, str(ok).lower()).inc()
        rec = dict(ts=time.time(), latency_ms=round(max(5, latency), 1), ok=ok, version=self.version)
        if len(self.buffer) >= BUFFER_MAX:
            self.buffer.popleft()  # bounded buffer: oldest telemetry is sacrificed
            self.dropped += 1
            m_dropped.labels(POD).inc()
        self.buffer.append(rec)

    async def upload(self, session):
        if self.partitioned or not self.buffer:
            return
        if Net.latency_ms:
            await asyncio.sleep(Net.latency_ms / 1000)
        if random.random() < Net.loss:
            m_uploads.labels(POD, "lost").inc()
            return
        batch = list(self.buffer)[:200]
        try:
            async with session.post(f"{HUB}/fleet/telemetry", json=dict(
                    device=self.id, ring=self.ring, version=self.version, dropped=self.dropped,
                    buffered=len(self.buffer), records=batch)) as r:
                if r.status != 200:
                    m_uploads.labels(POD, "error").inc()
                    return
                body = await r.json()
            for _ in batch:
                self.buffer.popleft()
            m_uploads.labels(POD, "ok").inc()
            desired = body.get("desired")
            if desired and desired != self.version and not self.installing:
                asyncio.create_task(self.install(desired))
        except Exception:
            m_uploads.labels(POD, "error").inc()

    async def install(self, version):
        self.installing = True
        await asyncio.sleep(random.uniform(2, 6))  # download + swap model
        if not self.partitioned:
            self.version = version
        self.installing = False


devices = [Device(i) for i in range(DEVICES)]
app = FastAPI(title=f"edge-fleet {POD}")


async def run_device(d: Device, session):
    await asyncio.sleep(random.uniform(0, 3))
    tick = 0
    while True:
        d.infer()
        if tick % 2 == 0:
            await d.upload(session)
        tick += 1
        await asyncio.sleep(2)


async def gauges():
    while True:
        m_buffered.labels(POD).set(sum(len(d.buffer) for d in devices))
        m_partitioned.labels(POD).set(sum(1 for d in devices if d.partitioned))
        await asyncio.sleep(2)


@app.on_event("startup")
async def startup():
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
    for d in devices:
        asyncio.create_task(run_device(d, session))
    asyncio.create_task(gauges())


@app.get("/devices")
async def list_devices():
    return [dict(id=d.id, ring=d.ring, version=d.version, partitioned=d.partitioned,
                 buffered=len(d.buffer), dropped=d.dropped) for d in devices]


@app.post("/chaos/partition")
async def partition(request: Request):
    body = await request.json()
    frac, dur = float(body.get("fraction", 0.4)), float(body.get("duration_s", 60))
    victims = random.sample(devices, max(1, int(len(devices) * frac)))
    for d in victims:
        d.partitioned_until = time.time() + dur
    return {"partitioned": [d.id for d in victims], "duration_s": dur}


@app.post("/chaos/network")
async def network(request: Request):
    body = await request.json()
    Net.latency_ms = float(body.get("latency_ms", Net.latency_ms))
    Net.loss = float(body.get("loss", Net.loss))
    return {"latency_ms": Net.latency_ms, "loss": Net.loss}


@app.post("/chaos/reset")
async def reset():
    Net.latency_ms, Net.loss = 0.0, 0.0
    for d in devices:
        d.partitioned_until = 0
    return {"ok": True}


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
async def health():
    return {"ok": True}
