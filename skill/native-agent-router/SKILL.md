---
name: native-agent-router
description: Delegate well-specified coding work units to native worker agents (ZCode/ACP) via the nar MCP tools; accept by fixed verification; escalate judgment calls back to you.
---

# Delegating with native-agent-router

Use when a subtask is spec-clear and independently verifiable. Otherwise DO IT
YOURSELF — delegation overhead (worker fixed prompt ≈20k+ tokens) exceeds the
saving for small/ambiguous tasks.

## Rules

1. **Don't delegate when**: <1 file of judgment, exploratory debugging, or the
   handoff would cost more than doing it. Check `agents` stats first: route hard
   units to high-`avg_score` agents, mechanical units to cheap ones.
2. **One complete work unit per `run`**: goal + constraints + scope
   (`scope_files`) + prohibitions (`forbid`) + acceptance (`verify` commands the
   worker cannot edit). Do not ping-pong per file.
3. **Continue, don't re-explain**: follow-ups on the same module use
   `session_ref=<previous task_id>` (same agent+workspace enforced).
4. **Wait, don't poll**: `run(..., wait_sec=...)` or `wait`. On `blocked`, do
   NOT resubmit — `inspect status`, then `cancel` (check `confirmed`) or keep waiting.
5. **Accept by evidence**: `summary` shows changed_files/out_of_scope/verify/
   usage/score. Never trust "submitted" as done; never trust a worker that
   deleted or weakened tests (verify runs externally; score penalizes).
6. **Read big things on demand**: `inspect diff|verify|log|raw --offset N`.
   Keep your context small; the kernel truncates results with read-more handles.
7. **Escalate on**: permission denials, cross-session rejections, budget/
   timeout blocks, score < 60, out-of-scope edits. These are judgment calls —
   yours, not the kernel's.
8. **Safety**: default `policy=deny`+`mode=plan`. Writes need
   `mode=build policy=allow` only inside a git repo you can diff/revert.
   Login/billing/kill are human boundaries.

## One-liner pattern

> "agents → run(zcode, goal, ws, --scope, --verify 'pytest -q', --wait 600) →
> wait → inspect summary; if blocked: inspect diagnose; then accept or escalate."
