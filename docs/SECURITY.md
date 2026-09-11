# Security

## Threat model (honest scope)

NAR is a local orchestration layer for agents you already authorized. It is
**not a sandbox**: a worker with `--policy allow` and write modes can modify
anything the user account can. Protections are: explicit per-task agent binding,
workspace mutex, scope audit (git diff vs allowlist), fixed external verification,
permission-policy defaults (`deny`), budgets, and full raw evidence trails.
File whitelists in prompts and git worktrees are discipline, not isolation.
If your threat model needs isolation, run workers in a container/VM — NAR will
report honestly when it cannot verify changes (no-git baseline →
`isolation unverifiable`).

## Credentials

- In the default `credentials:"auto"` mode, NAR never logs, persists, or
  uploads an API key. It sends the key only to the local ZCode app-server in the
  protocol payload. `nar doctor` and all task records omit it.
- **ZCode**: `credentials:"auto"` reads (read-only) the provider you configured
  in the ZCode desktop app (`~/.zcode/v2/config.json`) and passes the same
  provider/model/base-URL/key inside the **protocol payload** (`runtimeModel`)
  to the local app-server process. No provider switch, no billing-channel
  change, no auth bypass, no plan-limit circumvention.
- `zcode_home:"isolated"` (opt-in) additionally writes a private headless config
  containing the key under `<NAR home>/zcode-home/.zcode/cli/config.json`
  (owner-only chmod 600) so sessions and the desktop app stay fully separated.
  Trade-off: one extra on-disk copy of your key — delete with the home dir.
  Default is `null` (no isolated home; runtimeModel path only).
- ACP agents keep their own auth (`opencode` uses its own config; nothing is
  injected).
- Secrets never enter: task records, stats, MCP tool results, `diagnose` output
  (regex-masked: key/token/secret/password/authorization fields, `sk-*` style
  strings, long base64, URL credentials, emails; absolute paths reduced to
  workspace basename).

## Boundary operations (always human)

Login/logout, purchasing quota, switching billing channels, privilege
escalation, force-killing (`nar kill --yes` — explicit flag required), and any
irreversible change are never performed autonomously by the kernel.

## Execution safety defaults

- `default_mode: "plan"` + `default_permission_policy: "deny"` → read-only
  posture unless a task opts up.
- `tool_allowlist` restricts native tool sets server-side (blocks Bash/Write/
  subagents/WebSearch for review-style tasks — the WebSearch lesson: a denied
  question turned into a 200k-token search detour).
- `session_ref` cross-agent/cross-workspace reuse is rejected synchronously.
- Idempotency keys prevent double submission; duplicate submit returns the
  original task.
- Cancellation is confirmation-based, never ack-based; unconfirmed stops keep
  the lock and stay observable.

## State & logs

Logs contain prompts and code excerpts from YOUR tasks (that's their purpose):
`<home>/logs/` is user-owned, never uploaded, and `diagnose` is the masked,
shareable view. To wipe everything: delete `<home>` (see ROLLBACK.md).

## MCP client trust boundary

`nar-mcp` speaks stdio only, no network listener. `verify` commands execute
with your user privileges — do not expose the server to untrusted clients or
multi-tenant use. Hardening now default:

- **verify runs without a shell**: list commands run as argv; string commands are
  `shlex.split` (posix) into argv, so shell metacharacters (`&&`, `|`, `;`,
  backticks) in a verify string are NOT interpreted (covered by a test). Set
  `"verify_allow_shell": true` only if you truly need shell semantics, and never
  build verify strings from untrusted input.
- **scope_files must be relative** inside the workspace; absolute or `..` paths
  are rejected at submit (no path-escape confusion in the audit).
- **require_git_baseline** (config, default false; recommended true for write
  tasks): the kernel refuses to run a worker in a non-git workspace so changes
  are always auditable/revertable; a violation fails closed with code
  `require_git_baseline`.
- **workspace is canonicalized once** (expanduser→abspath→normpath) at submit,
  so the task record, workspace lock and session_ref checks can never diverge
  under relative paths or concurrent submits.

## Tool error semantics (observability)

A tool that cannot do its job raises → MCP `isError=true` (distinct from a
successful call whose business `status` is `failed`). Every success payload
carries `ok` and a stable `code` (e.g. `ok`, `workspace_busy`,
`verification_failed`, `model_unavailable`, `turn_timeout`, `budget_exceeded`,
`cancel_unconfirmed`, `require_git_baseline`), and `inspect diagnose` returns the
same `code`/`template_id` with a redacted, size-bounded report — so retry/routing
policy is driven by `code`, not by parsing prose.

## Scope is audit, not isolation (honest limit)

`scope_files` is enforced by post-execution git-diff audit, not by a filesystem
jail: it detects out-of-scope edits (and penalizes the score) but cannot
absolutely prevent them. For hard isolation run the worker in a container/VM.
`tool_allowlist` (zcode) restricts the native tool set server-side and is the
closest thing to pre-execution enforcement NAR can apply without a sandbox.

