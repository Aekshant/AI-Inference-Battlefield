# Mission 10: Give it to the AI SRE

**Agent:** LangGraph state machine. Each step runs the local `qwen2.5:3b`, and its tools come from an MCP server ([agent/mcp_server.py](../agent/mcp_server.py)).

```
triage → investigate (≤4 MCP tool calls) → diagnose → propose → HUMAN APPROVAL → execute → verify ─┐
  ▲                                                     └─ rejected with feedback → re-propose      │
  └──────────── users still outside SLO (≤3 attempts) ◄─────────────────────────────────────────────┘
```

**Design for a 3B model:** a deterministic evidence digest (about 20 lines of facts plus rule-based hypotheses), a fixed graph, JSON-mode outputs, and a rule-based fallback at every LLM step. The digest hides injected-chaos labels so the agent can't cheat.

## Runs and what each one taught

| Run | What happened | Fix |
|---|---|---|
| 1 | Correct action (drain gpu-2), marked "resolved"... but user errors went **13% → 28%**: draining a GPU from an overloaded fleet with no admission control made things worse. The verifier only counted open hypotheses, and draining made the "slow gpu-2" hypothesis disappear. | Verify on **user-facing SLOs**; loop back to triage if users still hurt |
| 2 | The model's diagnosis was right, but it proposed `shift_traffic(gpu-0, 1.0)`, a no-op aimed at the wrong GPU | **No-op guard**, then fall back to the rule-based action; rejecting with a note makes the agent re-propose |
| 3 | Drain → not better → re-triage → `enable_defense(admission)` → errors 50% → 18%, but marked "not better" because 1-minute windows still held pre-fix samples | Verify on 15 s gateway stats plus a 30 s user probe |
| 4 | Auto-watch paged itself. The **model's own** proposal was right (drain gpu-1). Errors 21% → 12%, and it was marked "resolved". | "Resolved" now requires the **absolute SLO** (≤2% errors, p99 TTFT ≤2 s); merely better = `mitigated`, keep going |
| 5 | In round 2 the model proposed `set_canary_weight(1.0)`, which would **send 100% of traffic to the untested v2**. My test harness auto-approved it. | **Policy:** the agent may only *reduce* canary weight; promotions belong to the rollout controller's gates |

| **6 (final)** | Paged itself 121 s after the fault. Round 1: the model's own proposal, drain gpu-1 → users **not** better (0% → 5.8% errors, the two remaining GPUs were overloaded). Round 2: the model again tried `set_canary_weight(1.0)`, the **policy blocked it**, and the agent fell back to `enable_defense(admission)` → errors 5.8% → **0.0%**, p99 TTFT 7.0 s → **0.8 s** (16% shed). **Resolved, within SLO.** | — |

**Postmortems:** qwen2.5:3b's free-form postmortems mixed up cause and effect. I tried `qwen3:4b` for that step: on this CPU it ignored `think: false`, generated at 12 tokens/s, and after **14 minutes** had produced a reasoning dump with no postmortem. The fix wasn't a bigger model. Every fact in a postmortem (impact, root cause, evidence, each action with its before/after, the verdict) is already known exactly, so it's **templated from data**, and follow-ups come from rules ("gpu-1 is out of rotation: restore it once healthy", "admission was off in production: make it default", "16% shed: add capacity"). The LLM writes only a 2–3 sentence summary, with a template fallback. That takes 18 s, and every number matches the run.

Lessons: the small model is good at *reading* evidence and noticeably worse at choosing *arguments*. Guardrails go in code, not in the prompt: the allowlist, argument validation, no-op detection, policy limits and SLO-based verification. Human approval caught what the guardrails didn't yet cover.
