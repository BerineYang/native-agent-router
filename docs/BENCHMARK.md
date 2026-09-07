# Benchmark & Token Accounting

## Accounting policy

Usage is recorded with explicit quality labels:

- `real` — reported by the agent's own protocol (ZCode turn.terminal /
  turn.completed / session.updated usage frames; deduped by seq and counted
  from exactly one source per turn).
- `partial` — some turns reported, some not.
- `estimated` — derived (e.g., char-count heuristics); NAR does not do this by default.
- `unknown` — agent doesn't surface usage (e.g., opencode over ACP today).

Per-model breakdowns are kept per source. Cache tokens follow the vendor's own
fields (ZCode reports cacheRead/cacheWrite when present) and are never
double-counted with input.

**We cannot claim full-chain totals**: orchestrator-side (Codex/opencode) token
counts are not visible to the kernel. Only worker-side usage + kernel-side
byte budgets are measured. Any total-cost statement must be labeled with this gap.

## Measured today (real, this machine)

| Run | Worker | Tokens (real) | Notes |
|---|---|---|---|
| zcode read-only reply | GLM-5.3, thought=low | 20,612 in / 4 out | fixed native system-prompt+tools overhead ≈20.6k per turn |
| zcode resume reply | same session | ≈20.7k in | history replay costs are vendor-side; we don't re-inject them |
| zcode trivial bugfix + verify | GLM-5.3 | 80,545 in / 235 out | multi-step tool loop; ~80k input reflects native harness growth |
| zcode permission-misread incident | GLM-5.3 | **227,095 in** | failure cost kept in the record; fixed by correct decision schema |
| opencode trivial reply | deepseek-v4-flash | unknown (0 reported) | ACP stream carries no usage |

**Small tasks are dominated by fixed native prompt overhead.** A 4-character
answer cost 20k+ worker tokens. Therefore NAR's default guidance: *do not
delegate* work whose orchestrator-side context is already cheaper than the
worker's fixed overhead (see SKILL.md rule 1). These are single observations,
not a savings ratio.

## Planned A/B/C protocol (designed, not yet run)

Fixed baseline commit, fixed model/provider, fixed acceptance script, same task
text; ≥5 repetitions per arm; cold-start and continued-session arms separate;
all failures and rework counted:

- **A**: orchestrator alone (Codex/opencode does the work itself).
- **B**: same worker directly (human gives the same prompt to ZCode/opencode).
- **C**: via NAR (adds `run`+`wait`+`inspect` round-trips, counted on the
  orchestrator side from its own stats).

Budget rule before any paid run: estimate from the tables above, state the cap
(`--budget`), and stop on breach. Smoke tests like today's are explicitly
**not** used to claim stable savings percentages.

## Deterministic scoring (zero tokens)

Every task ends with an objective score (0–100) from: verify first-try vs
after-repair vs failed, out-of-scope edits, blocked/timeout, unconfirmed cancel,
baseline auditability, usage quality. Aggregates per agent (`nar stats`,
`agents`) feed orchestrator routing decisions — high-score agents for hard
units, cheaper ones for mechanical units. No model is consulted to score.
