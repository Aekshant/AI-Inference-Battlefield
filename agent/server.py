"""AI SRE agent API (used by the console).

    POST /incidents                 {"question": "Why did p99 TTFT jump 5x?"}
    GET  /incidents                 list
    GET  /incidents/{id}            full timeline, diagnosis, proposal, postmortem
    POST /incidents/{id}/decision   {"approved": true, "note": "...", "by": "kalyan"}
    GET  /digest                    the deterministic triage view right now

With AUTO_WATCH=1 the agent opens an incident by itself when a high-severity
signal or a firing alert appears and no incident is open.
"""

import asyncio
import os
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from langgraph.types import Command

import battlefield as bf
from graph import MODEL, build

app = FastAPI(title="ai-sre")
graph = build()
incidents: dict = {}  # id -> meta
AUTO_WATCH = os.getenv("AUTO_WATCH", "1") == "1"


def cfg(iid):
    return {"configurable": {"thread_id": iid}}


async def drive(iid, payload):
    meta = incidents[iid]
    try:
        out = await graph.ainvoke(payload, cfg(iid))
        if "__interrupt__" in (out or {}):
            meta["status"] = "awaiting_approval"
        else:
            meta["status"] = (out or {}).get("status", "done")
    except Exception as e:
        meta["status"] = "error"
        meta["error"] = repr(e)
    meta["updated"] = time.time()


async def open_incident(question, source="human"):
    iid = time.strftime("%H%M%S-") + uuid.uuid4().hex[:4]
    incidents[iid] = dict(id=iid, question=question, source=source, status="triage", created=time.time(),
                          updated=time.time())
    asyncio.create_task(drive(iid, {"id": iid, "question": question, "timeline": []}))
    return incidents[iid]


@app.post("/incidents")
async def create(request: Request):
    body = await request.json()
    return await open_incident(body.get("question") or "Production looks unhealthy. What is going on?")


@app.get("/incidents")
async def list_incidents():
    out = []
    for iid, meta in sorted(incidents.items(), reverse=True):
        st = (await graph.aget_state(cfg(iid))).values
        out.append(dict(meta, live_status=st.get("status"), root_cause=(st.get("diagnosis") or {}).get("root_cause"),
                        action=(st.get("proposal") or {}).get("action")))
    return out


@app.get("/incidents/{iid}")
async def get_incident(iid: str):
    if iid not in incidents:
        raise HTTPException(404, "unknown incident")
    snap = await graph.aget_state(cfg(iid))
    v = dict(snap.values)
    return dict(incidents[iid], state=v, waiting_on=list(snap.next))


@app.post("/incidents/{iid}/decision")
async def decide(iid: str, request: Request):
    if iid not in incidents:
        raise HTTPException(404, "unknown incident")
    if incidents[iid]["status"] != "awaiting_approval":
        raise HTTPException(409, f"incident is {incidents[iid]['status']}, not awaiting approval")
    body = await request.json()
    decision = dict(approved=bool(body.get("approved")), note=body.get("note", ""), by=body.get("by", "human"),
                    ts=time.time())
    incidents[iid]["status"] = "executing" if decision["approved"] else "closing"
    asyncio.create_task(drive(iid, Command(resume=decision)))
    return incidents[iid]


@app.get("/digest")
async def get_digest():
    return await asyncio.to_thread(bf.digest)


@app.get("/health")
async def health():
    return {"ok": True, "model": MODEL, "auto_watch": AUTO_WATCH}


async def watcher():
    """Open an incident when something high-severity shows up twice in a row."""
    strikes = 0
    while True:
        await asyncio.sleep(15)
        try:
            busy = any(m["status"] not in ("resolved", "mitigated", "rejected", "unresolved", "error", "done")
                       for m in incidents.values())
            if busy:
                continue
            recent = [m for m in incidents.values() if time.time() - m["created"] < 120]
            if recent:
                continue  # give the last fix time to work
            d = await asyncio.to_thread(bf.digest)
            high = [h for h in d["hypotheses"] if h["severity"] == "high"]
            strikes = strikes + 1 if (high or d["alerts"]) else 0
            if strikes >= 2:
                what = "; ".join([h["summary"] for h in high][:2] + [f"alert {a}" for a in d["alerts"]][:2])
                await open_incident(f"Automated page: {what}", source="watcher")
                strikes = 0
        except Exception as e:
            print("watcher:", e, flush=True)


@app.on_event("startup")
async def startup():
    if AUTO_WATCH:
        asyncio.create_task(watcher())
