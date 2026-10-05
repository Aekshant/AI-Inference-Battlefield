#!/usr/bin/env python3
"""Offline eval gate: score a model version on the golden set, then register
the result with the rollout controller.

    run_eval.py --version v2          (pins traffic with X-Model-Version)

Accuracy = answer contains the expected string. In synthetic mode the worker's
"oracle" answers correctly with probability ORACLE_ACCURACY, so v2's
deliberately worse quality shows up here before any user sees it.
Stdlib only.
"""
import argparse
import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))


def ask(gw, version, item):
    body = json.dumps({"prompt": item["prompt"], "max_tokens": 16, "eval_expected": item["expected"],
                       "tenant": "eval"}).encode()
    req = urllib.request.Request(gw + "/v1/generate", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Model-Version": version,
                                          "X-Tenant": "eval"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.load(r)
        return dict(ok=True, latency=time.monotonic() - t0, text=out.get("text", ""), cost=out.get("cost", 0),
                    tokens=out.get("tokens", 0) + out.get("prompt_tokens", 0), expected=item["expected"],
                    version=out.get("version"))
    except Exception as e:
        return dict(ok=False, latency=time.monotonic() - t0, error=str(e), expected=item["expected"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://127.0.0.1:8080")
    ap.add_argument("--controller", default="http://127.0.0.1:8082")
    ap.add_argument("--version", default="v2")
    ap.add_argument("--repeat", type=int, default=3, help="passes over the golden set")
    ap.add_argument("--no-register", action="store_true")
    a = ap.parse_args()

    items = [json.loads(l) for l in open(os.path.join(HERE, "golden.jsonl"))] * a.repeat
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda it: ask(a.gateway, a.version, it), items))
    good = [r for r in res if r["ok"]]
    wrong_version = [r for r in good if r.get("version") not in (None, a.version)]
    if wrong_version:
        print("WARNING: %d answers came from %s, not %s" % (len(wrong_version), wrong_version[0]["version"], a.version))
    correct = sum(1 for r in good if r["expected"].lower() in r["text"].lower())
    lat = sorted(r["latency"] * 1000 for r in good) or [0]
    tokens = sum(r["tokens"] for r in good) or 1
    report = dict(
        accuracy=round(correct / max(1, len(res)), 4),
        p50_ms=round(lat[len(lat) // 2], 1),
        p99_ms=round(lat[min(len(lat) - 1, int(len(lat) * 0.99))], 1),
        cost_per_1k_tokens=round(sum(r["cost"] for r in good) / tokens * 1000, 5),
        n=len(res),
    )
    print("eval %s: %s (failed requests: %d)" % (a.version, json.dumps(report), len(res) - len(good)))
    if not a.no_register:
        req = urllib.request.Request("%s/registry/%s/eval" % (a.controller, a.version),
                                     data=json.dumps(report).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            print("registered:", json.load(r)["version"])


if __name__ == "__main__":
    main()
