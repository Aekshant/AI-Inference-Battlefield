#!/usr/bin/env python3
"""Tiny stdlib-only CLI for the battlefield (works on the host's Python 3.8).

    bf.py status            gateway's view of every GPU
    bf.py watch             live client-side view of running attacks
    bf.py attack spike 60   start a scenario
    bf.py events            recent gateway events
"""
import json
import sys
import time
import urllib.request

GW = "http://127.0.0.1:8080"
ATK = "http://127.0.0.1:8081"


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)


def post(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def status():
    d = get(GW + "/admin/state")
    on = [k for k, v in d["defenses"].items() if v is True]
    print("defenses on: %s | routing=%s | canary=%.0f%% | gw inflight=%d queued=%d" % (
        ", ".join(on) or "none", d["defenses"]["routing"], d["canary_weight"] * 100,
        d["admission"]["inflight"], d["admission"]["queued"]))
    print("%-28s %-7s %-7s %5s %5s %6s %9s %-9s %5s %s" % (
        "GPU", "pool", "healthy", "infl", "queue", "kv", "ttft", "breaker", "w", "flags"))
    for b in d["backends"]:
        s = b["state"]
        flags = []
        if b["outlier_eject"]:
            flags.append("OUTLIER")
        if b["manual_eject"]:
            flags.append("EJECTED")
        chaos = {k: v for k, v in (s.get("chaos") or {}).items() if v and k != "kv_budget" and v != 1.0}
        if chaos:
            flags.append("chaos=" + json.dumps(chaos))
        print("%-28s %-7s %-7s %5d %5d %6.2f %7.0fms %-9s %5.2f %s" % (
            b["name"], b["pool"], b["healthy"], b["inflight"], s.get("queue_depth", 0),
            s.get("kv_util", 0), max(s.get("ttft_ewma_ms", 0), b["gw_ttft_ewma_ms"]),
            b["breaker"], b["weight"], " ".join(flags)))


def watch():
    while True:
        for r in get(ATK + "/attacks"):
            if not r["running"]:
                continue
            l = r["last10s"]
            print("%s %-13s %5.0fs  sent/10s=%-6d ok=%-6d shed=%-6d err=%-6d p50=%6.0fms p99=%6.0fms" % (
                r["id"], r["scenario"], r["elapsed_s"], l["sent"], l["ok"], l["shed"], l["error"],
                l["p50_ms"], l["p99_ms"]))
        time.sleep(2)


def events():
    for e in get(GW + "/admin/events?limit=40"):
        print(e["ts"][11:19], e["msg"])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        status()
    elif cmd == "watch":
        try:
            watch()
        except KeyboardInterrupt:
            pass
    elif cmd == "events":
        events()
    elif cmd == "attack":
        r = post(ATK + "/attacks", {"scenario": sys.argv[2], "duration_s": float(sys.argv[3]) if len(sys.argv) > 3 else 60})
        print("run", r["id"], r["scenario"], r["streams"])
    else:
        print(__doc__)
