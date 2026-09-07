# Verification Record

All initial tests used independent, non-sensitive temp Git projects under
`%TEMP%\nar-verify\*`. No production design projects, PDKs or real work trees
were touched. Evidence lives under `G:\opencode\ad\native-agent-router\.nar-home\logs\<task_id>\`.

## Automated (no model, no network) — 64 tests green

`pytest -q` covers: misrouting (unknown/disabled agent), session cross-agent &
cross-workspace rejection, duplicate submit (idempotency), workspace mutex +
busy report, crash→failed, orphan→unknown→reconcile, cancel confirmed vs
unconfirmed (incl. native-defect reproduction stub), late-completion-after-cancel
guard, turn timeout→blocked with lock retention + watcher completion, hidden
timeout-cap regression (900s survives), long verify-log truncation & paging,
usage dedup (turn.completed + turn.terminal counted once; seq-replay ignored),
verification failure → one repair → pass / fail, repair-disabled, budget
exceeded, out-of-scope audit, no-git honesty, scope artifact exclusion, masked
diagnosis, scoring determinism, model selection (zcode provider/model/thought;
ACP model_args), MCP tool-layer end-to-end, ACP/ZCode protocol conformance
against spec-shaped fake servers.

## Real verification (this machine, 2026-09-07)

### ZCode 0.16.5 via `zcode-native` — REAL, model calls made

| Step | Task | Native session | Outcome |
|---|---|---|---|
| read-only | `t-a75a145562cd` | `sess_60c7aefd-090a-4494-92c6-25bd185fb61d` | replied exactly `RCOK`; usage real 20612/4 (thought_level=low) |
| native resume | `t-16ba6f8d1544` | same id, `resumed:true` | answered `RCOK` from previous turn; `-32031` fixed by runtimeModel on resume+send |
| edit+verify | `t-068c89b8a6b0` | `sess_ff1d0ac6-...` | fixed `app.py` (a−b→a+b); `python check.py` exit 0; diff audit clean; score 85 (see artifact note) |
| permission path | same run | — | 8× `interaction/requestPermission` answered `{decision:"allow"}`; edits landed |
| honest failure | `t-459c61a60f61` | `sess_d45c5f4f-...` | BEFORE the permission-schema fix: deny-misread caused retry storm, **227,095 tokens burned**, verify failed, score 0 — kept as evidence |

Protocol-level resume evidence: `session/resume` accepted + `session/read`
`contextUsed>0` + identical `native_session_id` across separate processes —
not "the model remembers".

### opencode 1.18.15 via `acp-generic` — REAL, model calls made

| Step | Task | Native session | Outcome |
|---|---|---|---|
| handshake+read-only | `t-192f574b4135` | `ses_f85192c03ffeOorrNsaehImqbO` | initialize→session/new→configOptions(model)→`session/set_config_option` to `opencode-go/deepseek-v4-flash`→`OCOK` |
| native resume | `t-ab971da8a954` | same id, `resumed:true` | answered `OCOK` via `session/load` |
| usage honesty | same | — | quality `unknown` (opencode ACP stream carries no token usage) |
| honest failure | `t-dcc33dfdbfd9` | `ses_f852472c3ffe...` | default model `scnet/Kimi-K3` hung 238s server-side with 0 tokens; CLI exit → orphan `unknown` → `diagnose` shows timeline+frames (masked) |

## Stub-only (protocol-shaped fakes, clearly labeled)

- `tests/stubs/fake_zcode_appserver.py` / `fake_acp_agent.py`: conformance of
  our client (seq dedup, terminal priority, permission decision shapes,
  cancel-ineffective detection, resume evidence). These prove our code follows
  the documented protocol; they do NOT substitute for the real runs above.

## Not verified (and why)

- **Gemini CLI**: not installed on this machine.
- **Claude Code**: installed (2.1.197) but user reports it currently unusable →
  no real acceptance claimed.
- **Codex as MCP orchestrator client**: Codex is installed (`~/.codex`) but this
  session's orchestrator is opencode; the MCP config snippet is provided but the
  Codex-side hookup was not exercised.
- **Billing-stop proof**: `native idle` ≠ provider-side billing stopped; the
  provider exposes no stop/usage endpoint to us. Stated as a limitation.
- **A/B/C benchmark** (orchestrator-alone vs worker-direct vs via-NAR):
  designed in BENCHMARK.md, NOT yet run with controlled budgets.
