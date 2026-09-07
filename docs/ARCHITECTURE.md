# Architecture

## Layers

```
Orchestrator (Codex / opencode / Claude Code)   <- judgment only
   |  MCP stdio (nar-mcp)  or  CLI (nar)        <- same kernel, no duplicated logic
   v
Kernel (zero model calls)
   task store (atomic JSON) | workspace mutex | idempotency | bounded wait
   fixed verification | one pre-authorized repair | budget | recovery/reconcile
   usage accounting (real/partial/estimated/unknown) | deterministic scoring
   v
Adapter contract (adapters/base.py)
   start | create_session | resume_session | prompt | poll | cancel | close
   v
Native agent processes (their own harness, prompts, tools, billing)
   zcode-native  -> `node zcode.cjs app-server` (protocol 0.16)
   acp-generic   -> any ACP stdio server (`opencode acp`, `gemini --experimental-acp`, ...)
```

## Identity model

- `task_id` — NAR task (stable across repairs/watching)
- `run_id` — execution attempt of the task (`<task_id>-rN`)
- `native_session_id` — the agent's own session id (ZCode `sess_...`, opencode `ses_...`)
- `native_turn_id` — the agent's turn id when the protocol exposes one

Session continuation is bound by `(agent_id, workspace, native_session_id)`.
`session_ref` (a previous task_id) is validated **synchronously at submit**:
different agent or different workspace is rejected ("session cross-use denied").

## State machine

```
queued -> running -> succeeded | failed | cancelled | blocked | interrupted
                 \-> cancel_requested -> cancelled | (late completion -> cancelled)
blocked -> (watcher sees real terminal) -> succeeded | failed | cancelled
unknown (orphan at startup) -> reconcile -> interrupted (+ current facts)
```

`blocked` is deliberately **not** terminal: on turn timeout the kernel keeps the
workspace lock and the agent process alive, and a watcher thread polls the
adapter (`poll()`) until a genuine terminal event arrives. Resources are never
released on an unconfirmed stop.

## Cancel & timeout correctness (audit against known bridge defects)

A community bridge exhibited four stacked defects; NAR's design responds to each:

1. *Native ZCode clears its cancel controller before the completion Promise
   resolves, so `session/stop` acks success without stopping.*
   → NAR never trusts the ack: `cancel()` confirms only via an observed terminal
   event or process exit; if protocol events keep flowing after stop, it reports
   `events continued after session/stop; native cancel ineffective` (tested with
   a scripted stub reproducing the defect).
2. *Bridge releases the lease ~10s after timeout without stop confirmation,
   causing state contradictions.*
   → NAR holds the lock and process on timeout (watcher mode). The only way to
   force-release is user-invoked `nar kill --yes`, which marks the task
   `interrupted` and warns that the native session may still hold state.
3. *Second wait didn't extend the deadline (ms/s confusion, hidden 600s cap).*
   → All timeouts are seconds end-to-end; `wait_max_sec` bounds only the `wait`
   tool's block, never the task's own `timeout_sec` (tested: 900s survives).
4. *Logs showed work continuing after stop.*
   → NAR's raw protocol log per task (`native-raw.jsonl`) is the evidence base;
   `inspect what='raw'` and `diagnose` surface it compactly.

## Token-efficiency mechanisms (design, not marketing)

- Compact-by-default results: status + changed files + verify outcomes + bounded
  worker summary + usage; everything else (full logs, diffs, raw frames) stays on
  disk behind paged `inspect`.
- `titleGenerationEnabled:false` on ZCode session create — skips one hidden model
  call per session (learned from codex-zcode-bridge; adopted).
- `tool_allowlist` — native tool-set restriction to stop exploratory detours that
  burn tokens (observed live: a model that couldn't ask a question went WebSearch).
- Deterministic scoring + stats (zero tokens) give the orchestrator routing data
  instead of a summarizer model.
- Session reuse via `session_ref` for follow-ups on the same module (native
  history beats re-explaining context).
- No scheduler model, no summarizer model, no recursive delegation, no default
  multi-model discussion.

## Tool error semantics

`nar-mcp` distinguishes two kinds of outcome: a tool that cannot do its job
raises (bad agent_id, unknown task_id, invalid workspace/scope) → MCP
`isError=true`; a successful call always returns a structured dict with `ok`
and a stable `code` (`ok`, `workspace_busy`, `verification_failed`,
`model_unavailable`, `turn_timeout`, `budget_exceeded`, `cancel_unconfirmed`,
`require_git_baseline`, ...). `inspect diagnose` carries the same `code` in a
redacted, size-bounded report. Orchestrators drive retry/routing off `code`,
never off prose parsing.

## Persistence & recovery

- `<home>/tasks/<task_id>.json` — atomic (tmp+rename); safe to read while running.
- `<home>/logs/<task_id>/` — events.jsonl (kernel), native-raw.jsonl (protocol
  frames), verify-*.log, diff.patch, result.json, stderr.
- `<home>/locks/<sha1(workspace)>.lock` — pid-checked; stale locks (dead pid)
  are stolen; live holders block with `workspace_busy` + holder id.
- `<home>/stats/<agent>.json` — score history & aggregates.
- Startup `mark_orphans_unknown()` + `reconcile()` re-establish facts (verify +
  diff re-run) without ever blindly resubmitting side-effecting work.
- No secrets in task records, stats, or logs; `diagnose` output is masked.
