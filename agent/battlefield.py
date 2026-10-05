"""Read tools, allowlisted actions and the evidence digest for the AI SRE.

The digest does the number-crunching in plain Python and hands the small model
~20 lines of facts plus rule-based hypotheses. The model reasons over evidence;
it never sees raw time series or the injected-chaos labels (that would be
cheating - a real SRE doesn't get a "this GPU was sabotaged" flag).
"""

import json
import os
import statistics
import subprocess
import time

import httpx

GW = os.getenv("GATEWAY_URL", "http://127.0.0.1:8080")
PROM = os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090")
CTL = os.getenv("CONTROLLER_URL", "http://127.0.0.1:8082")
NS = os.getenv("NAMESPACE", "battlefield")

http = httpx.Client(timeout=8)


def _get(url):
    r = http.get(url)
    r.raise_for_status()
    return r.json()


def _post(url, body=None):
    r = http.post(url, json=body or {})
    r.raise_for_status()
    return r.json()


def prom(expr):
    """Instant query -> list of (labels, float)."""
    try:
        d = _get(f"{PROM}/api/v1/query?" + httpx.QueryParams(query=expr).__str__())
        out = []
        for r in d["data"]["result"]:
            v = float(r["value"][1])
            if v == v and abs(v) != float("inf"):  # drop NaN/Inf
                out.append((r["metric"], v))
        return out
    except Exception:
        return []


def prom1(expr, default=0.0):
    r = prom(expr)
    return r[0][1] if r else default


def kubectl(*args):
    p = subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True, timeout=20)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[:300])
    return p.stdout


# ---------------------------------------------------------------- read tools

def fleet_health() -> str:
    """Per-GPU view from the gateway: health, load, KV cache, TTFT, breaker, weight."""
    st = _get(f"{GW}/admin/state")
    rows = []
    for b in st["backends"]:
        s = b["state"]
        rows.append(dict(gpu=b["name"], pool=b["pool"], version=s.get("version"), healthy=b["healthy"],
                         inflight=b["inflight"], queue=s.get("queue_depth"), slots=s.get("slots"),
                         kv_util=s.get("kv_util"), ttft_ms=round(max(s.get("ttft_ewma_ms", 0), b["gw_ttft_ewma_ms"])),
                         breaker=b["breaker"], weight=b["weight"],
                         ejected=b["manual_eject"] or b["outlier_eject"]))
    on = sorted(k for k, v in st["defenses"].items() if v is True)
    return json.dumps(dict(gpus=rows, defenses_on=on, routing=st["defenses"]["routing"],
                           canary_weight=st["canary_weight"], gateway_queue=st["admission"]["queued"]))


def traffic_summary() -> str:
    """Last-minute gateway traffic: request rate, outcomes, shed reasons, p99 TTFT/latency per pool, per-tenant shed ratio."""
    out = dict(
        rps={m.get("outcome"): round(v, 1) for m, v in prom('sum by (outcome) (rate(gw_requests_total[1m]))')},
        shed_reasons={m.get("reason"): round(v, 1) for m, v in prom('sum by (reason) (rate(gw_shed_total[1m]))')},
        ttft_p99_s={m.get("pool"): round(v, 3) for m, v in prom("gw:ttft_p99:1m")},
        latency_p99_s={m.get("pool"): round(v, 3) for m, v in prom("gw:latency_p99:1m")},
        user_error_ratio=round(prom1("user:error_ratio:1m"), 3),
        tenants={},
    )
    tot = {m.get("tenant"): v for m, v in prom('sum by (tenant) (rate(gw_requests_total[1m]))')}
    shed = {m.get("tenant"): v for m, v in prom('sum by (tenant) (rate(gw_shed_total[1m]))')}
    for t, v in tot.items():
        if v > 0.5:
            out["tenants"][t] = dict(rps=round(v, 1), shed_ratio=round(shed.get(t, 0) / v, 2))
    return json.dumps(out)


def firing_alerts() -> str:
    """Prometheus alerts currently firing or pending."""
    try:
        alerts = _get(f"{PROM}/api/v1/alerts")["data"]["alerts"]
    except Exception as e:
        return json.dumps({"error": str(e)})
    return json.dumps([dict(name=a["labels"]["alertname"], state=a["state"],
                            summary=a["annotations"].get("summary", ""),
                            labels={k: v for k, v in a["labels"].items() if k not in ("alertname", "severity")})
                       for a in alerts])


def _age(iso):
    """Seconds since a Go RFC3339 timestamp (nanosecond fraction stripped)."""
    from datetime import datetime, timezone
    import re
    try:
        t = datetime.fromisoformat(re.sub(r"\.\d+", "", iso).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds()
    except ValueError:
        return 0


def recent_events(limit: int = 15) -> str:
    """Recent gateway and controller events (ejections, breakers, rollouts, config changes)."""
    ev = []
    try:
        ev += [dict(src="gateway", ts=e["ts"][11:19], msg=e["msg"], _age=_age(e["ts"]))
               for e in _get(f"{GW}/admin/events?limit={limit}")]
    except Exception:
        pass
    try:
        ev += [dict(src="controller", ts=e["ts"][11:19], msg=e["msg"], _age=_age(e["ts"]))
               for e in _get(f"{CTL}/events")[-limit:]]
    except Exception:
        pass
    ev = [e for e in ev if not e["msg"].startswith("chaos")]  # no peeking at the attacker
    ev = [e for e in ev if e.pop("_age", 0) < 600]  # only the last 10 minutes matter
    return json.dumps(sorted(ev, key=lambda e: e["ts"])[-limit:])


def rollout_status() -> str:
    """Model canary rollout and edge fleet ring rollout status."""
    out = {}
    for name, url in (("model_rollout", f"{CTL}/rollouts/current"), ("fleet", f"{CTL}/fleet")):
        try:
            d = _get(url)
            if name == "model_rollout":
                last = d["checks"][-1]["values"] if d.get("checks") else {}
                d = dict(candidate=d["candidate"], status=d["status"], weight=d["weight"], reason=d.get("reason"),
                         last_check={k: round(v, 4) for k, v in last.items()})
            else:
                d = dict(rings=d["rings"], desired=d["desired"],
                         rollout=(d["rollout"] or {}) and {k: d["rollout"].get(k) for k in ("version", "status", "ring", "reason")})
            out[name] = d
        except Exception as e:
            out[name] = f"unavailable: {e}"
    return json.dumps(out)


def k8s_pods() -> str:
    """Kubernetes pods in the battlefield namespace: phase, readiness, restarts, age."""
    d = json.loads(kubectl("get", "pods", "-o", "json"))
    rows = []
    now = time.time()
    for p in d["items"]:
        cs = (p["status"].get("containerStatuses") or [{}])[0]
        started = p["status"].get("startTime")
        age = now - time.mktime(time.strptime(started, "%Y-%m-%dT%H:%M:%SZ")) + time.timezone if started else 0
        rows.append(dict(pod=p["metadata"]["name"], phase=p["status"].get("phase"), ready=cs.get("ready"),
                         restarts=cs.get("restartCount", 0), age_s=int(age)))
    return json.dumps(rows)


def pod_logs(pod: str, lines: int = 30) -> str:
    """Last log lines of one pod."""
    return kubectl("logs", pod, f"--tail={min(lines, 100)}")[-3000:]


READ_TOOLS = [fleet_health, traffic_summary, firing_alerts, recent_events, rollout_status, k8s_pods, pod_logs]


# ---------------------------------------------------------------- actions (allowlist)

def act_shift_traffic(backend: str, weight: float) -> str:
    """Set a GPU's routing weight (0 = drain, 1 = normal)."""
    return json.dumps(_post(f"{GW}/admin/backends/{backend}/weight", {"weight": max(0.0, min(1.0, float(weight)))}))


def act_eject_backend(backend: str) -> str:
    """Stop routing to a GPU entirely."""
    return json.dumps(_post(f"{GW}/admin/backends/{backend}/eject"))


def act_restore_backend(backend: str) -> str:
    """Put an ejected GPU back into rotation (and weight 1)."""
    _post(f"{GW}/admin/backends/{backend}/weight", {"weight": 1})
    return json.dumps(_post(f"{GW}/admin/backends/{backend}/restore"))


def act_set_canary_weight(weight: float) -> str:
    """Reduce canary traffic share; 0 rolls the canary back (increases are refused)."""
    try:
        _post(f"{CTL}/rollouts/current/abort")
    except Exception:
        pass
    return json.dumps(_post(f"{GW}/admin/canary", {"weight": max(0.0, min(1.0, float(weight)))}))


def act_scale_gpus(replicas: int) -> str:
    """Scale the stable GPU pool (3..6 replicas)."""
    n = max(3, min(6, int(replicas)))
    return kubectl("scale", "statefulset/gpu", f"--replicas={n}").strip()


def act_restart_pod(pod: str) -> str:
    """Delete a pod so its controller recreates it."""
    if not pod.startswith(("gpu-", "gpu-canary")):
        raise ValueError("only GPU pods may be restarted")
    return kubectl("delete", "pod", pod, "--wait=false").strip()


def act_set_tenant_rate(tenant: str, rate: float) -> str:
    """Change one tenant's request-rate quota (req/s)."""
    t = _get(f"{GW}/admin/tenants").get(tenant) or dict(name=tenant, burst=10, weight=1, priority=2)
    t["rate"] = max(1.0, float(rate))
    t["burst"] = max(1, int(t["rate"] / 2))
    _post(f"{GW}/admin/defenses", {"tenant_quotas": True})
    return json.dumps(_post(f"{GW}/admin/tenants", t))


DEFENSES = {"admission", "backpressure", "kv_aware", "outlier_ejection", "long_prompt_pool", "health_checks",
            "circuit_breaker", "retries", "fallback", "tenant_quotas"}


def act_enable_defense(name: str) -> str:
    """Turn on one gateway defense."""
    if name not in DEFENSES:
        raise ValueError(f"unknown defense {name}; choose from {sorted(DEFENSES)}")
    patch = {name: True}
    if name in ("outlier_ejection", "kv_aware"):
        patch["routing"] = "p2c"
    return json.dumps({k: v for k, v in _post(f"{GW}/admin/defenses", patch).items() if k in patch})


def act_fleet_rollback(version: str = "edge-v1") -> str:
    """Point every edge ring back at a known-good model version."""
    return json.dumps(_post(f"{CTL}/fleet/rollback", {"version": version}))


def act_no_action(reason: str = "") -> str:
    """Do nothing (system is healthy or the fix is outside the allowlist)."""
    return "no action: " + reason


ACTIONS = {
    "shift_traffic": (act_shift_traffic, {"backend": str, "weight": float}, "low"),
    "eject_backend": (act_eject_backend, {"backend": str}, "low"),
    "restore_backend": (act_restore_backend, {"backend": str}, "low"),
    "set_canary_weight": (act_set_canary_weight, {"weight": float}, "low"),
    "scale_gpus": (act_scale_gpus, {"replicas": int}, "medium"),
    "restart_pod": (act_restart_pod, {"pod": str}, "medium"),
    "set_tenant_rate": (act_set_tenant_rate, {"tenant": str, "rate": float}, "medium"),
    "enable_defense": (act_enable_defense, {"name": str}, "low"),
    "fleet_rollback": (act_fleet_rollback, {"version": str}, "low"),
    "no_action": (act_no_action, {"reason": str}, "none"),
}


def validate_action(name, args):
    """Returns (clean_args, error)."""
    if name not in ACTIONS:
        return None, f"action {name!r} is not in the allowlist {sorted(ACTIONS)}"
    _, schema, _ = ACTIONS[name]
    clean = {}
    for k, typ in schema.items():
        if k not in (args or {}):
            if name in ("no_action", "fleet_rollback"):
                continue
            return None, f"missing argument {k!r}"
        try:
            clean[k] = typ(args[k])
        except (TypeError, ValueError):
            return None, f"argument {k!r} must be {typ.__name__}"
    if name in ("shift_traffic", "eject_backend", "restore_backend"):
        gpus = {g["gpu"]: g for g in json.loads(fleet_health())["gpus"]}
        g = gpus.get(clean["backend"])
        if not g:
            return None, f"unknown GPU {clean['backend']!r}; known: {sorted(gpus)}"
        # No-op guard: a small model sometimes picks the wrong target with the right intent.
        if name == "shift_traffic" and abs(g["weight"] - clean["weight"]) < 1e-6:
            return None, f"no-op: {clean['backend']} already has weight {g['weight']}"
        if name == "eject_backend" and g["ejected"]:
            return None, f"no-op: {clean['backend']} is already ejected"
    if name == "set_canary_weight":
        cw = json.loads(fleet_health())["canary_weight"]
        # Policy: the agent may only roll a canary back. Sending more traffic to an
        # unproven model is a promotion, and promotions go through the rollout
        # controller's eval + health gates - never through an incident bot.
        if clean["weight"] >= cw:
            return None, (f"policy: agent may only reduce canary weight (current {cw}); "
                          "promotions go through the rollout controller")
    return clean, None


def execute_action(name, args):
    fn, _, _ = ACTIONS[name]
    return fn(**args)


# ---------------------------------------------------------------- evidence digest

def digest():
    """Deterministic triage: facts + hypotheses (each with a suggested action)."""
    facts, hyps = [], []
    fh = json.loads(fleet_health())
    ts = json.loads(traffic_summary())
    gpus = [g for g in fh["gpus"] if g["pool"] == "stable"]
    canary = [g for g in fh["gpus"] if g["pool"] == "canary"]
    healthy_ttft = [g["ttft_ms"] for g in gpus if g["healthy"]]
    med = statistics.median(healthy_ttft) if healthy_ttft else 0

    rps = ts["rps"]
    total = sum(rps.values()) or 0
    facts.append(f"gateway traffic {total:.0f} req/s: " + ", ".join(f"{k}={v}" for k, v in sorted(rps.items())))
    if ts["shed_reasons"]:
        facts.append("shed reasons (req/s): " + ", ".join(f"{k}={v}" for k, v in ts["shed_reasons"].items()))
    facts.append(f"p99 TTFT by pool: {ts['ttft_p99_s']}; user-visible error ratio {ts['user_error_ratio']:.1%}")
    facts.append(f"defenses on: {', '.join(fh['defenses_on']) or 'NONE'}; routing={fh['routing']}")

    for g in gpus:
        flags = []
        if not g["healthy"]:
            flags.append("UNREACHABLE")
        if g["breaker"] != "closed":
            flags.append(f"breaker {g['breaker']}")
        if g["ejected"]:
            flags.append("ejected")
        if med and g["ttft_ms"] > 3 * med and g["ttft_ms"] > 800:
            flags.append(f"TTFT {g['ttft_ms']}ms = {g['ttft_ms'] / med:.1f}x fleet median")
        if g["kv_util"] and g["kv_util"] > 0.85:
            flags.append(f"KV cache {g['kv_util']:.0%}")
        if g["queue"] and g["slots"] and g["queue"] > g["slots"]:
            flags.append(f"queue {g['queue']} > {g['slots']} slots")
        if g["weight"] < 1:
            flags.append(f"weight {g['weight']}")
        facts.append(f"{g['gpu']}: ttft={g['ttft_ms']}ms inflight={g['inflight']} queue={g['queue']} "
                     f"kv={g['kv_util']:.0%} {'; '.join(flags) if flags else 'ok'}")

        if any(f.startswith("TTFT") for f in flags) and g["weight"] > 0:
            hyps.append(dict(id=f"slow-{g['gpu']}", severity="high",
                             summary=f"{g['gpu']} is degraded: TTFT {g['ttft_ms']}ms vs fleet median {med:.0f}ms",
                             action="shift_traffic", args={"backend": g["gpu"], "weight": 0.0}))
        if not g["healthy"]:
            hyps.append(dict(id=f"down-{g['gpu']}", severity="high", summary=f"{g['gpu']} is unreachable",
                             action="restart_pod", args={"pod": g["gpu"]}))
        if g["kv_util"] and g["kv_util"] > 0.9 and "kv_aware" not in fh["defenses_on"]:
            hyps.append(dict(id="kv-pressure", severity="medium",
                             summary=f"KV cache exhausted on {g['gpu']} ({g['kv_util']:.0%}); router ignores KV usage",
                             action="enable_defense", args={"name": "kv_aware"}))

    errors = rps.get("error", 0)
    if total > 5 and errors / total > 0.05 and "admission" not in fh["defenses_on"]:
        hyps.append(dict(id="overload-no-admission", severity="high",
                         summary=f"{errors / total:.0%} of requests fail with admission control off: the fleet is overloaded and queues grow unbounded",
                         action="enable_defense", args={"name": "admission"}))
    shed = rps.get("shed", 0)
    busy = [g for g in gpus if g["healthy"] and g["inflight"] >= 0.8 * (g["slots"] or 32)]
    if total > 5 and shed / total > 0.2 and len(busy) == len([g for g in gpus if g["healthy"] and not g["ejected"]]):
        hyps.append(dict(id="capacity", severity="medium",
                         summary=f"{shed / total:.0%} of traffic shed and every serving GPU is near its slot limit",
                         action="scale_gpus", args={"replicas": min(6, len(gpus) + 1)}))

    tenants = ts["tenants"]
    if len(tenants) > 1:
        top = max(tenants.items(), key=lambda kv: kv[1]["rps"])
        others_shed = [t for t, v in tenants.items() if t != top[0] and v["shed_ratio"] > 0.2]
        if top[1]["rps"] > 0.6 * sum(v["rps"] for v in tenants.values()):
            facts.append(f"tenants: " + ", ".join(f"{t} {v['rps']}rps shed={v['shed_ratio']:.0%}" for t, v in tenants.items()))
            if others_shed or "tenant_quotas" not in fh["defenses_on"]:
                hyps.append(dict(id=f"noisy-{top[0]}", severity="high",
                                 summary=f"tenant {top[0]} sends {top[1]['rps']} req/s ({top[1]['rps'] / max(1, sum(v['rps'] for v in tenants.values())):.0%} of traffic); other tenants are being starved",
                                 action="set_tenant_rate", args={"tenant": top[0], "rate": 30}))

    if fh["canary_weight"] > 0 and canary:
        c = canary[0]
        s_ttft = ts["ttft_p99_s"].get("stable", 0)
        c_ttft = ts["ttft_p99_s"].get("canary", 0)
        facts.append(f"canary {c['version']} weight={fh['canary_weight']:.0%} p99 TTFT {c_ttft}s vs stable {s_ttft}s")
        if s_ttft and c_ttft > 1.3 * s_ttft:
            hyps.append(dict(id="canary-regression", severity="high",
                             summary=f"canary {c['version']} p99 TTFT is {c_ttft / s_ttft:.1f}x stable",
                             action="set_canary_weight", args={"weight": 0.0}))

    try:
        fleet = json.loads(rollout_status()).get("fleet", {})
        for ring, v in (fleet.get("rings") or {}).items():
            offline = v["devices"] - v["online"]
            if offline or v["online_error_rate"] > 0.05:
                facts.append(f"edge ring {ring}: {v['online']}/{v['devices']} online, versions {v['versions']}, error rate {v['online_error_rate']:.0%}")
            if v["online_error_rate"] > 0.1:
                bad = [k for k in v["versions"] if k != "edge-v1"]
                hyps.append(dict(id=f"edge-{ring}", severity="high",
                                 summary=f"edge ring {ring} error rate {v['online_error_rate']:.0%} on versions {v['versions']}",
                                 action="fleet_rollback", args={"version": "edge-v1"}))
    except Exception:
        pass

    alerts = [a for a in json.loads(firing_alerts()) if isinstance(a, dict) and a.get("state") == "firing"]
    for a in alerts:
        facts.append(f"ALERT {a['name']}: {a['summary']}")

    hyps.sort(key=lambda h: {"high": 0, "medium": 1}.get(h["severity"], 2))
    kpis = recent_kpis()
    return dict(ts=time.time(), facts=facts, hypotheses=hyps, alerts=[a["name"] for a in alerts], kpis=kpis)


def recent_kpis(seconds=15):
    """Short-window KPIs for verification: the gateway's per-second stats plus the
    synthetic user probe over 30s. 1-minute Prometheus windows lag too much to
    judge a fix that is 30 seconds old."""
    pts = [p for p in _get(f"{GW}/admin/stats?seconds={seconds + 1}") if p["rps"]][-seconds:]
    total = sum(p["rps"] for p in pts) or 0
    err = sum(p["errors"] for p in pts)
    shed = sum(p["shed"] for p in pts)
    ttfts = sorted(p["ttft_p99_ms"] for p in pts if p["ttft_p99_ms"])
    lat = sorted(p["p99_ms"] for p in pts if p["p99_ms"])
    pick = lambda v: round(v[int(len(v) * 0.9)] / 1000, 3) if v else 0.0  # 90th pct of per-second p99s
    user = prom1('sum(rate(attacker_requests_total{outcome=~"error|dropped"}[30s])) / '
                 'clamp_min(sum(rate(attacker_requests_total[30s])), 0.001)')
    return dict(user_error_ratio=round(user, 3), gw_error_ratio=round(err / total, 3) if total else 0.0,
                shed_ratio=round(shed / total, 3) if total else 0.0, ttft_p99_s=pick(ttfts),
                latency_p99_s=pick(lat), rps=round(total / max(1, len(pts)), 1))


SLO_ERRORS = float(os.getenv("SLO_ERRORS", "0.02"))  # user-visible error ratio
SLO_TTFT_S = float(os.getenv("SLO_TTFT_S", "2.0"))   # p99 time to first token


def slo_verdict(before, after):
    """Did users actually get better? Judged on user-facing SLOs, not on whether
    our own hypotheses went away (draining a slow GPU makes its hypothesis vanish
    even when it overloads everyone else)."""
    b, a = before["kpis"], after["kpis"]
    bad_b = max(b["user_error_ratio"], b["gw_error_ratio"])  # same failures seen from two sides: max, not sum
    bad_a = max(a["user_error_ratio"], a["gw_error_ratio"])
    errors_ok = bad_a <= 0.01 or bad_a <= 0.7 * bad_b
    ttft_ok = a["ttft_p99_s"] <= 1.5 or (b["ttft_p99_s"] and a["ttft_p99_s"] <= 0.7 * b["ttft_p99_s"])
    worse = bad_a > bad_b * 1.2 + 0.01
    within = bad_a <= SLO_ERRORS and a["ttft_p99_s"] <= SLO_TTFT_S
    return dict(improved=bool(errors_ok and ttft_ok and not worse), within_slo=bool(within), errors_before=round(bad_b, 3),
                errors_after=round(bad_a, 3), ttft_p99_before=b["ttft_p99_s"], ttft_p99_after=a["ttft_p99_s"],
                shed_before=b["shed_ratio"], shed_after=a["shed_ratio"])
