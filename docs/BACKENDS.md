# Backends

## zcode-native (ZCode app-server, protocol 0.16)

Verified live against ZCode CLI **0.16.5** (bundled in the desktop app at
`<install>/resources/glm/zcode.cjs`; not on PATH by default — NAR auto-discovers
via `ZCODE_BIN`, PATH, registry InstallLocation, and Program Files on every drive).

Protocol facts confirmed on this machine (beyond the community docs):

- Line-delimited JSON, **no `jsonrpc` field**; `id`+`method` = request (both
  directions), `id`+no-method = response, `method`-only = notification.
- `session/create` params: `{workspace:{workspacePath,workspaceKey}, mode,
  titleGenerationEnabled:false, persistence:"immediate", runtimeModel, model}`.
- **`session/requestRuntimePreferences` must be answered within ~15s** or
  `session/create` fails `-32022`. Zod-validated answer shape (verified):
  `{"nativeSearchEnhancementsEnabled": <bool>}`. (An empty `{}` is rejected.)
- **`interaction/requestPermission` expects a decision object**
  `{"decision":"allow"|"deny", "reason":...}` — NOT an `optionId`-only result.
  Wrong shape is silently treated as denial → the worker retries every write
  path and burns tokens (observed: 227k tokens on one trivial fix). The correct
  shape resolved all attempts on first try.
- `session/send` accepts `runtimeModel`/`toolAllowlist`/`inputId` but **rejects
  a top-level `model` key** (`-32602 Unrecognized key: "model"`).
- Cold resume across processes requires re-supplying `runtimeModel` (the
  app-server deliberately omits provider secrets from persisted workspace
  state). Without it: `session/send` → `-32031 ZCODE_RUNTIME_MODEL_UNAVAILABLE`.
  NAR builds the payload from your own `~/.zcode/v2/config.json` (same provider,
  model, base URL, billing channel; key travels only inside the protocol).
- Terminal events: `turn.completed` / `turn.failed` / `turn.terminal` may
  overlap; NAR counts usage from exactly one source (priority terminal >
  completed > session.updated) and dedups by per-session `seq`.
- Usage fields captured when present: input/output/total, reasoning, cache
  read/write, modelRequests — quality `real`.

Modes: `plan` (read-only), `build`, `edit`, `yolo`. Default `plan` + policy
`deny` is the safe posture; write tasks need `--mode build --policy allow`
(permission requests then auto-allow within that task only).

## acp-generic (Agent Client Protocol over stdio)

Verified live against **opencode 1.18.15** (`opencode acp`):

- `initialize` (protocolVersion 1) → capabilities incl. `loadSession:true`.
- `session/new {cwd, mcpServers:[]}` → `sessionId` **plus `configOptions`**:
  a `model` select (dozens of values) and a `mode` select (`build`/`plan`).
- Model selection: NAR tries `session/set_config_option` (configId/optionId
  variants) then `session/set_model`; falls back to `model_args` CLI
  substitution for servers that take flags (e.g. `gemini --experimental-acp`).
- Turn: `session/prompt` request/response; `session/update` notifications
  (agent_message_chunk, tool_call, tool_call_update, available_commands_update).
  Usage is NOT surfaced in the ACP stream by opencode → NAR labels quality
  `unknown` honestly (never guesses).
- Resume: `session/load` (opencode supports; cold-resume answered "OCOK" from
  the previous turn — protocol-verified continuation).
- Permissions: `session/request_permission` answered per task policy by option
  `kind` prefix (allow*/deny*/reject*); cancel = `session/cancel` notification,
  confirmed only when the in-flight prompt response actually arrives.

## Adding a third agent

### ACP-compatible (config only, no code)

```json
{ "agents": { "gemini": {
    "adapter": "acp-generic",
    "command": ["gemini", "--experimental-acp"],
    "model": "gemini-2.5-pro",
    "model_args": ["--model", "{model}"],
    "default_permission_policy": "deny"
} } }
```

`nar doctor` then shows it; `nar run gemini ...` works. Tasks are bound to
`agent_id` explicitly; nothing else changes.

### Non-ACP native runtime (small adapter)

Implement the contract in `adapters/base.py` (~150 lines typical) and register
the name in `build_adapter()`. Required semantics:

- `start(workspace)` spawn process; raw-frame logging to `log_dir`
- `create_session` / `resume_session` (return **protocol evidence**, e.g.
  contextUsed, not vibes)
- `prompt(..., input_id)` → `TerminalTurn(ok, reason, text_tail, tools, usage)`
- `poll(session_id)` → late terminal (watcher mode)
- `cancel(session_id)` → **confirmed only by observed terminal/exit**; set
  `last_cancel_note` explaining any unconfirmed stop
- `close()` graceful-then-kill

See `examples/custom_adapter_example.py`.

## What NAR deliberately does NOT do

- No reimplementation of any agent's harness: native system prompts, tools,
  models, providers, credentials and billing stay exactly as the vendor ships.
- No Pi+GLM or raw-API impersonation of ZCode.
- No auth/plan-limit bypass; login, purchases, billing switches and privilege
  elevation are user-boundary operations.
- No scheduler/summarizer model inside the kernel.
