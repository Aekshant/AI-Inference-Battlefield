#!/usr/bin/env python3
"""Run one battlefield experiment and print a before/after-friendly summary.

    experiment.py --defenses hardened --scenario spike --duration 40
    experiment.py --defenses naive --scenario baseline --chaos-at 10 \
        --chaos 'gpu-2:{"slow_ttft_ms":4000}'
    experiment.py --patch '{"routing":"p2c"}' ...     (apply on top of preset)
    experiment.py ... --kill-at 15 --kill gpu-1

Stdlib only so it runs on the host's Python 3.8.
"""
import argparse
import json
import subprocess
import time
import urllib.request

GW = "http://127.0.0.1:8080"
ATK = "http://127.0.0.1:8081"


def call(url, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def share(stats, seconds):
    by = {}
    for p in stats[-seconds:]:
        for k, v in (p.get("by_node") or {}).items():
            by[k] = by.get(k, 0) + v
    tot = sum(by.values()) or 1
    return {k: "%d%%" % round(100 * v / tot) for k, v in sorted(by.items())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--defenses", default="naive")
    ap.add_argument("--patch", default=None, help="JSON merged into defenses after preset")
    ap.add_argument("--scenario", default="baseline")
    ap.add_argument("--streams", default=None, help="custom streams JSON")
    ap.add_argument("--duration", type=float, default=40)
    ap.add_argument("--chaos", action="append", default=[], help='node:{"json"}')
    ap.add_argument("--chaos-at", type=float, default=0)
    ap.add_argument("--kill", default=None)
    ap.add_argument("--kill-at", type=float, default=0)
    ap.add_argument("--sample", type=float, default=10)
    ap.add_argument("--no-reset", action="store_true")
    a = ap.parse_args()

    call(ATK + "/attacks/stop", {})
    if not a.no_reset:
        call(GW + "/admin/chaos/all/reset", {})
    call(GW + "/admin/defenses/preset/" + a.defenses, {})
    if a.patch:
        call(GW + "/admin/defenses", json.loads(a.patch))
    time.sleep(3)
    body = {"scenario": a.scenario, "duration_s": a.duration}
    if a.streams:
        body["streams"] = json.loads(a.streams)
    run = call(ATK + "/attacks", body)
    print("== %s | defenses=%s %s | streams=%s" % (a.scenario, a.defenses, a.patch or "", run["streams"]))
    t0 = time.time()
    chaos_done = not a.chaos
    kill_done = not a.kill
    next_sample = a.sample
    while True:
        el = time.time() - t0
        if not chaos_done and el >= a.chaos_at:
            for c in a.chaos:
                node, js = c.split(":", 1)
                call(GW + "/admin/chaos/" + node, json.loads(js))
                print("   t=%4.0fs  CHAOS %s %s" % (el, node, js))
            chaos_done = True
        if not kill_done and el >= a.kill_at:
            subprocess.run(["kubectl", "-n", "battlefield", "delete", "pod", a.kill, "--grace-period=0", "--force"],
                           capture_output=True)
            print("   t=%4.0fs  KILLED pod %s" % (el, a.kill))
            kill_done = True
        if el >= next_sample:
            r = [x for x in call(ATK + "/attacks") if x["id"] == run["id"]][0]
            l = r["last10s"]
            stats = call(GW + "/admin/stats?seconds=12")
            print("   t=%4.0fs  sent=%-5d ok=%-5d shed=%-5d err=%-5d p50=%6.0fms p99=%6.0fms  share=%s" % (
                el, l["sent"], l["ok"], l["shed"], l["error"], l["p50_ms"], l["p99_ms"], share(stats, 10)))
            next_sample += a.sample
        if el >= a.duration + 1:
            break
        time.sleep(0.5)
    time.sleep(2)
    r = [x for x in call(ATK + "/attacks") if x["id"] == run["id"]][0]
    t = r["totals"]
    sent = max(1, t["sent"])
    print("   TOTAL sent=%d ok=%.1f%% shed=%.1f%% error=%.1f%% fallback=%.1f%%" % (
        t["sent"], 100 * t["ok"] / sent, 100 * t["shed"] / sent, 100 * t["error"] / sent, 100 * t.get("fallback", 0) / sent))
    if len(r.get("tenants", {})) > 1:
        for tn, v in sorted(r["tenants"].items()):
            tot = max(1, sum(x for k, x in v.items() if k != "p99_ms"))
            print("     tenant %-11s ok=%5.1f%% shed=%5.1f%% err=%5.1f%% p99=%6.0fms" % (
                tn, 100 * v.get("ok", 0) / tot, 100 * v.get("shed", 0) / tot, 100 * v.get("error", 0) / tot, v["p99_ms"]))
    if not a.no_reset:
        call(GW + "/admin/chaos/all/reset", {})


if __name__ == "__main__":
    main()
