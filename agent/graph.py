"""The AI SRE incident graph (LangGraph).

    triage -> investigate -> diagnose -> propose -> [human approval] -> execute -> verify -> postmortem
       ^                                                 \\ rejected ------------------------------/  |
       \\------------- users still hurting (max MAX_ATTEMPTS) ---------------------------------------/

A small local model (qwen2.5:3b via Ollama) makes the judgement calls inside
each step; the graph itself is fixed so the model can't wander off. Tools come
from the battlefield MCP server. Every LLM step has a deterministic fallback so
an unparseable answer degrades to "rule-based SRE", never to a wrong action.
"""

import asyncio
import json
import os
import re
import sys
import time
from typing import Any, Optional, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_ollama import ChatOllama
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

import battlefield as bf

MODEL = os.getenv("AGENT_MODEL", "qwen2.5:3b")
# Only the postmortem's short summary is LLM-written; the facts are templated.
# (qwen3:4b was tried here: on this CPU it ignored think=false and spent 14 min
# reasoning without producing a postmortem.)
POSTMORTEM_MODEL = os.getenv("POSTMORTEM_MODEL", MODEL)
OLLAMA = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
MAX_TOOL_CALLS = int(os.getenv("MAX_TOOL_CALLS", "4"))
VERIFY_WAIT_S = int(os.getenv("VERIFY_WAIT_S", "45"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))

SYSTEM = """You are an SRE on call for an LLM inference platform.
Architecture: users -> AI gateway (admission control, routing, circuit breakers, canary split, tenant quotas)
-> stable GPU pool (gpu-0..gpu-N, model v1) and a canary pool (candidate model). A rollout controller runs canaries;
an edge fleet of devices gets models ring by ring (canary, early, broad).
Be concise and factual. Only claim what the evidence shows. Prefer the smallest safe action.
Prefer the most specific explanation (a named GPU, tenant, canary or ring) over generic "overload",
but remember that removing capacity from an overloaded fleet makes things worse.
Never mention components (edge fleet, canary, tenants) unless the facts show a problem there. Units: TTFT values are seconds unless marked ms."""


class Incident(TypedDict, total=False):
    id: str
    question: str
    timeline: list
    digest: dict
    notes: str
    diagnosis: dict
    proposal: dict
    decision: dict
    result: str
    verification: dict
    attempts: list
    rejections: list
    postmortem: str
    status: str


def log(state, step, text, data=None):
    entry = dict(ts=time.time(), step=step, text=text)
    if data is not None:
        entry["data"] = data
    state.setdefault("timeline", []).append(entry)
    print(f"[{state.get('id')}] {step}: {text}", file=sys.stderr, flush=True)


def llm(json_mode=False, model=None):
    kw = dict(model=model or MODEL, base_url=OLLAMA, temperature=0, num_ctx=8192)
    if (model or MODEL).startswith("qwen3"):
        kw["reasoning"] = False  # no <think> transcript in the output
    if json_mode:
        kw["format"] = "json"
    return ChatOllama(**kw)


def parse_json(text):
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text or "", re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def tried(state):
    return [(a["action"], json.dumps(a["args"], sort_keys=True)) for a in state.get("attempts") or []]


def history_text(state):
    if not state.get("attempts"):
        return ""
    lines = ["PREVIOUS ACTIONS THAT DID NOT RESTORE THE SLO (do not repeat them):"]
    for a in state["attempts"]:
        v = a["verdict"]
        lines.append(f"- {a['action']} {json.dumps(a['args'])}: errors {v['errors_before']:.1%} -> {v['errors_after']:.1%}, "
                     f"p99 TTFT {v['ttft_p99_before']}s -> {v['ttft_p99_after']}s "
                     f"({'better but still breaching SLO' if v['improved'] else 'not better'})")
    return "\n".join(lines) + "\n\n"


def digest_text(d):
    lines = ["FACTS:"] + [f"- {f}" for f in d["facts"]]
    lines.append("RULE-BASED HYPOTHESES (may be incomplete or wrong):")
    if not d["hypotheses"]:
        lines.append("- none")
    for i, h in enumerate(d["hypotheses"], 1):
        lines.append(f"{i}. [{h['severity']}] {h['summary']} -> suggested {h['action']} {json.dumps(h['args'])}")
    return "\n".join(lines)


_mcp_tools = None


async def mcp_tools():
    """Battlefield tools served over MCP (stdio subprocess)."""
    global _mcp_tools
    if _mcp_tools is None:
        here = os.path.dirname(os.path.abspath(__file__))
        client = MultiServerMCPClient({"battlefield": {
            "command": sys.executable, "args": [os.path.join(here, "mcp_server.py")], "transport": "stdio",
            "env": dict(os.environ), "cwd": here}})
        _mcp_tools = {t.name: t for t in await client.get_tools()}
    return _mcp_tools


# ---------------------------------------------------------------- nodes

async def triage(state: Incident):
    if state.get("attempts"):
        log(state, "triage", f"attempt {len(state['attempts']) + 1}: previous fix did not help users, re-assessing")
    d = await asyncio.to_thread(bf.digest)
    state["digest"] = d
    log(state, "triage", f"{len(d['facts'])} facts, {len(d['hypotheses'])} hypotheses, alerts={d['alerts']}",
        dict(facts=d["facts"], hypotheses=d["hypotheses"]))
    state["status"] = "investigating"
    return state


async def investigate(state: Incident):
    tools = await mcp_tools()
    read_names = ["fleet_health", "traffic_summary", "firing_alerts", "recent_events", "rollout_status", "k8s_pods"]
    bound = llm().bind_tools([tools[n] for n in read_names if n in tools])
    msgs = [SystemMessage(SYSTEM), HumanMessage(
        f"Incident: {state['question']}\n\n{history_text(state)}{digest_text(state['digest'])}\n\n"
        f"Call at most {MAX_TOOL_CALLS} tools to confirm or refute the hypotheses (recent_events and "
        "fleet_health are usually most useful). Then reply with 3-5 bullet points of findings.")]
    calls = 0
    notes = ""
    try:
        for _ in range(MAX_TOOL_CALLS + 1):
            resp: AIMessage = await bound.ainvoke(msgs)
            msgs.append(resp)
            if not resp.tool_calls or calls >= MAX_TOOL_CALLS:
                notes = resp.content or ""
                break
            for tc in resp.tool_calls[: MAX_TOOL_CALLS - calls]:
                calls += 1
                t = tools.get(tc["name"])
                out = await t.ainvoke(tc["args"]) if t else f"unknown tool {tc['name']}"
                out = out if isinstance(out, str) else json.dumps(out, default=str)
                log(state, "tool", f"{tc['name']}({json.dumps(tc['args'])})", out[:1500])
                msgs.append(ToolMessage(out[:4000], tool_call_id=tc["id"]))
    except Exception as e:
        notes = f"(investigation LLM failed: {e})"
    state["notes"] = notes.strip() or "(no findings)"
    log(state, "investigate", state["notes"])
    return state


async def diagnose(state: Incident):
    prompt = (f"Incident: {state['question']}\n\n{history_text(state)}{digest_text(state['digest'])}\n\nInvestigation notes:\n{state['notes']}\n\n"
              'Return JSON: {"root_cause": str, "evidence": [str, ...], "blast_radius": str, '
              '"confidence": "high"|"medium"|"low", "hypothesis_id": str or null}. '
              "hypothesis_id is the id of the matching rule-based hypothesis if one fits, else null. "
              f"Hypothesis ids: {[h['id'] for h in state['digest']['hypotheses']]}")
    d = None
    try:
        r = await llm(json_mode=True).ainvoke([SystemMessage(SYSTEM), HumanMessage(prompt)])
        d = parse_json(r.content)
    except Exception as e:
        log(state, "diagnose", f"LLM failed: {e}")
    hyps = state["digest"]["hypotheses"]
    if not d or not d.get("root_cause"):
        top = hyps[0] if hyps else None
        d = dict(root_cause=top["summary"] if top else "no clear fault found", evidence=state["digest"]["facts"][:4],
                 blast_radius="unknown", confidence="low", hypothesis_id=top["id"] if top else None, fallback=True)
    state["diagnosis"] = d
    log(state, "diagnose", d["root_cause"], d)
    return state


async def propose(state: Incident):
    actions = {k: dict(args={a: t.__name__ for a, t in v[1].items()}, risk=v[2]) for k, v in bf.ACTIONS.items()}
    hyps = state["digest"]["hypotheses"]
    feedback = ""
    if state.get("decision") and not state["decision"].get("approved") and state["decision"].get("note"):
        feedback = (f"A HUMAN REJECTED your previous proposal {state['proposal']['action']} "
                    f"{json.dumps(state['proposal']['args'])} with this feedback: \"{state['decision']['note']}\". "
                    "Propose something that addresses the feedback.\n\n")
    prompt = (f"{feedback}Diagnosis: {json.dumps(state['diagnosis'])}\n\n{history_text(state)}{digest_text(state['digest'])}\n\n"
              f"Allowed actions: {json.dumps(actions)}\n"
              'Pick ONE action that mitigates the root cause with the least risk. Return JSON: '
              '{"action": str, "args": {...}, "rationale": str, "expected_effect": str}')
    p, err = None, None
    try:
        r = await llm(json_mode=True).ainvoke([SystemMessage(SYSTEM), HumanMessage(prompt)])
        p = parse_json(r.content)
    except Exception as e:
        err = f"LLM failed: {e}"
    if p:
        clean, err = await asyncio.to_thread(bf.validate_action, p.get("action"), p.get("args") or {})
        if clean is not None:
            p["args"] = clean
            if (p["action"], json.dumps(clean, sort_keys=True)) in tried(state):
                err = "that exact action was already tried and did not help"
    if not p or err:
        # Fall back to the hypothesis the diagnosis picked, else the top untried one.
        done = tried(state)
        fresh = [x for x in hyps if (x["action"], json.dumps(x["args"], sort_keys=True)) not in done]
        hid = state["diagnosis"].get("hypothesis_id")
        h = next((x for x in fresh if x["id"] == hid), fresh[0] if fresh else None)
        if err:
            log(state, "propose", f"model proposal rejected ({err}); using rule-based suggestion")
        if h:
            p = dict(action=h["action"], args=h["args"], rationale=h["summary"], expected_effect="mitigate " + h["id"],
                     fallback=True)
        else:
            p = dict(action="no_action", args={"reason": "no actionable fault found"}, rationale="system looks healthy",
                     expected_effect="none", fallback=True)
    p["risk"] = bf.ACTIONS[p["action"]][2]
    state["proposal"] = p
    state["status"] = "awaiting_approval"
    log(state, "propose", f"{p['action']} {json.dumps(p['args'])} - {p.get('rationale', '')}", p)
    return state


async def approval(state: Incident):
    if state["proposal"]["action"] == "no_action":
        state["decision"] = dict(approved=False, note="nothing to execute")
        return state
    decision = interrupt(dict(proposal=state["proposal"], diagnosis=state["diagnosis"]))
    state["decision"] = decision
    if not decision.get("approved") and decision.get("note"):
        state.setdefault("rejections", []).append(dict(proposal=state["proposal"], note=decision["note"]))
    log(state, "approval", ("APPROVED" if decision.get("approved") else "REJECTED") +
        (f" by {decision.get('by', 'human')}" ) + (f": {decision['note']}" if decision.get("note") else ""))
    return state


def after_approval(state: Incident):
    d = state.get("decision", {})
    if d.get("approved"):
        return "execute"
    # Rejected with feedback: let the agent try again (bounded).
    if d.get("note") and state["proposal"]["action"] != "no_action" and len(state.get("rejections") or []) <= 2:
        return "propose"
    return "postmortem"


async def execute(state: Incident):
    state["status"] = "executing"
    p = state["proposal"]
    tools = await mcp_tools()
    try:
        out = await tools["execute_action"].ainvoke({"action": p["action"], "args": p["args"]})
        out = out if isinstance(out, str) else json.dumps(out, default=str)
    except Exception as e:
        out = f"FAILED: {e}"
    state["result"] = out
    log(state, "execute", out[:500])
    return state


async def verify(state: Incident):
    state["status"] = "verifying"
    log(state, "verify", f"waiting {VERIFY_WAIT_S}s, then judging on user-facing SLOs (errors, p99 TTFT)")
    await asyncio.sleep(VERIFY_WAIT_S)
    after = await asyncio.to_thread(bf.digest)
    v = bf.slo_verdict(state["digest"], after)
    p = state["proposal"]
    state.setdefault("attempts", []).append(dict(action=p["action"], args=p["args"], verdict=v))
    state["verification"] = dict(v, facts_after=after["facts"])
    verdict = "WITHIN SLO" if v["within_slo"] else "BETTER BUT STILL BREACHING SLO" if v["improved"] else "USERS NOT BETTER"
    log(state, "verify", f"errors {v['errors_before']:.1%} -> {v['errors_after']:.1%}, p99 TTFT "
        f"{v['ttft_p99_before']}s -> {v['ttft_p99_after']}s: {verdict}", v)
    return state


def after_verify(state: Incident):
    if state["verification"]["within_slo"] or len(state.get("attempts") or []) >= MAX_ATTEMPTS:
        return "postmortem"
    return "triage"  # users still hurting: re-assess with the history of what didn't work


def follow_ups(state):
    """Rule-based follow-ups from what was actually done."""
    out = []
    for a in state.get("attempts") or []:
        act, args = a["action"], a["args"]
        if act in ("shift_traffic", "eject_backend") and args.get("weight", 0) == 0:
            out.append(f"{args['backend']} is out of rotation: find out why it degraded, then `restore_backend` it "
                       "(or replace the pod) - the fleet is running with reduced capacity.")
        if act == "enable_defense":
            out.append(f"`{args['name']}` was off in production. Make it part of the default gateway config.")
        if a["verdict"].get("shed_after", 0) > 0.05:
            out.append(f"{a['verdict']['shed_after']:.0%} of traffic is being shed: add capacity (`scale_gpus`) "
                       "or confirm that shedding at this load is acceptable.")
        if act == "set_canary_weight":
            out.append("The canary was rolled back: block its promotion until it passes the eval gate again.")
        if act == "fleet_rollback":
            out.append("Find out why the edge ring health gate didn't stop this before it spread.")
    if not out:
        out.append("No action was executed: confirm whether the system recovered on its own.")
    return list(dict.fromkeys(out))[:3]


async def postmortem(state: Incident):
    diag = state.get("diagnosis") or {}
    attempts = state.get("attempts") or []
    approved = state.get("decision", {}).get("approved")
    ver = state.get("verification") or {}
    if not approved and state.get("decision"):
        state["status"] = "rejected"
    elif ver.get("within_slo"):
        state["status"] = "resolved"
    elif ver.get("improved"):
        state["status"] = "mitigated"  # better, but a human should keep going
    else:
        state["status"] = "unresolved"

    rows = []
    for i, a in enumerate(attempts, 1):
        v = a["verdict"]
        verdict = "SLO met" if v["within_slo"] else "better, SLO still breached" if v["improved"] else "not better"
        rows.append(f"| {i} | `{a['action']} {json.dumps(a['args'])}` | {v['errors_before']:.1%} → {v['errors_after']:.1%} | "
                    f"{v['ttft_p99_before']:.1f} s → {v['ttft_p99_after']:.1f} s | {v['shed_before']:.1%} → {v['shed_after']:.1%} | {verdict} |")
    first = attempts[0]["verdict"] if attempts else None
    impact = (f"At the start of remediation users saw {first['errors_before']:.1%} errors and a p99 time-to-first-token of "
              f"{first['ttft_p99_before']:.1f} s (SLO: errors ≤ {bf.SLO_ERRORS:.0%}, p99 TTFT ≤ {bf.SLO_TTFT_S} s)."
              if first else "No remediation was executed, so no before/after measurement exists.")
    final = (f"Final state: {ver['errors_after']:.1%} errors, p99 TTFT {ver['ttft_p99_after']:.1f} s, "
             f"{ver['shed_after']:.1%} shed — **{state['status']}**." if ver else f"Status: **{state['status']}**.")

    facts = dict(question=state["question"], root_cause=diag.get("root_cause"), status=state["status"],
                 actions=[f"{a['action']} {json.dumps(a['args'])}" for a in attempts], final=final)
    summary = None
    try:
        r = await llm(model=POSTMORTEM_MODEL).ainvoke([SystemMessage(SYSTEM), HumanMessage(
            "In 2-3 plain sentences, summarize this incident for an engineering channel: what broke, what was done, "
            "how it ended. Use only these facts, no headings:\n" + json.dumps(facts))])
        summary = re.sub(r"<think>.*?</think>", "", r.content, flags=re.S).strip()
        if not summary or len(summary) > 700 or "#" in summary:
            summary = None  # off-format: use the template instead
    except Exception as e:
        log(state, "postmortem", f"summary LLM failed: {e}")
    if not summary:
        summary = (f"{diag.get('root_cause') or state['question']} "
                   f"{len(attempts)} action(s) were executed after human approval. {final}")

    ev = "\n".join(f"- {e}" for e in diag.get("evidence") or []) or "- (none recorded)"
    fu = "\n".join(f"- {x}" for x in follow_ups(state))
    table = ("| # | Action | User errors | p99 TTFT | Shed | Verdict |\n|---|---|---|---|---|---|\n" + "\n".join(rows)) if rows \
        else "_No action executed._"
    state["postmortem"] = f"""# Postmortem: {state['question']}

## Summary
{summary}

## Impact
{impact}

## Root cause
{diag.get('root_cause') or 'not determined'} _(confidence: {diag.get('confidence', 'unknown')})_

Evidence:
{ev}

## Actions taken
{table}

## Verification
{final} Each action was judged {VERIFY_WAIT_S} s after execution on user-facing SLOs, not on whether the alert cleared.

## Follow-ups
{fu}
"""
    log(state, "postmortem", f"incident {state['status']}")
    return state


def build():
    g = StateGraph(Incident)
    for name, fn in [("triage", triage), ("investigate", investigate), ("diagnose", diagnose),
                     ("propose", propose), ("approval", approval), ("execute", execute),
                     ("verify", verify), ("postmortem", postmortem)]:
        g.add_node(name, fn)
    g.set_entry_point("triage")
    g.add_edge("triage", "investigate")
    g.add_edge("investigate", "diagnose")
    g.add_edge("diagnose", "propose")
    g.add_edge("propose", "approval")
    g.add_conditional_edges("approval", after_approval,
                            {"execute": "execute", "propose": "propose", "postmortem": "postmortem"})
    g.add_edge("execute", "verify")
    g.add_conditional_edges("verify", after_verify, {"triage": "triage", "postmortem": "postmortem"})
    g.add_edge("postmortem", END)
    return g.compile(checkpointer=MemorySaver())
