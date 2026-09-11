# Native Agent Runtime Router (NAR)

English | [简体中文](README.zh-CN.md)

**Route coding tasks from an orchestrator agent (Codex / opencode / Claude Code) to
native CLI worker agents (ZCode, any ACP-compatible agent) through a small,
deterministic, model-free kernel.**

The kernel never calls a model. It handles task state, bounded waiting, workspace
locking, fixed verification, one pre-authorized repair, cancellation with
confirmation, budgets, recovery, usage accounting and per-agent scoring — so the
orchestrator spends its tokens on judgment, not on polling and log-shuffling.

```
Orchestrator (Codex / opencode / Claude Code)
        |  MCP (5 stable tools)  or  CLI (`nar`)
        v
  Model-free kernel: tasks, locks, verification, repair, budget, recovery
        v
  Pluggable native adapters
   ├── zcode-native   (ZCode app-server protocol 0.16, verified live on 0.16.5)
   ├── acp-generic    (Agent Client Protocol, verified live on opencode 1.18.15)
   └── your adapter   (small class, see docs/BACKENDS.md)
```

More native agents are being supported…

- MCP: `nar-mcp` exposes exactly five tools: `agents`, `run`, `wait`, `inspect`, `cancel`.
  New backends are added by **config**, not by new tools.
- CLI: `nar` shares the same kernel (debugging, scripting, humans).
- Token discipline: compact results by default; full logs/diffs on disk, paged
  via `inspect`; deterministic scoring per task (zero tokens) feeds routing.

## Install

Requires Python >= 3.10. For the ZCode adapter: Node.js >= 22 and the ZCode
desktop app (or its CLI on `PATH`). For ACP agents: any ACP server binary.

Install from the official GitHub source:

```bash
git clone https://github.com/BerineYang/native-agent-router.git
cd native-agent-router
python -m pip install .
```

If `git clone` is unreliable, download the official
[`v1.0.0` branch source ZIP](https://github.com/BerineYang/native-agent-router/archive/refs/heads/v1.0.0.zip),
extract it, open a terminal in the extracted directory, and run:

```bash
python -m pip install .
```

For development, replace the install command with
`python -m pip install -e .`. In the repository directory, `pipx install .`
also works.

On Windows, verify the installation with the module form first. It works even
when the Python scripts directory is missing from `PATH`:

```bash
python -m native_agent_router doctor
```

If the scripts directory is on `PATH`, the shorter command is equivalent:

```bash
nar doctor
```

The doctor command uses `[OK]`, `[X]`, and `[i]` markers for the ZCode bundle,
Node.js, Git, and configuration checks. It never prints credentials. A missing
ZCode entry matters only when you intend to use the `zcode-native` adapter.

## Quickstart

1. Create the config (optional — auto-discovery works without it):

```bash
mkdir -p ~/.native-agent-router
cp config.example.json ~/.native-agent-router/agents.json
```

2. List agents, submit a complete work unit, wait, inspect:

```bash
nar agents
nar run zcode "Implement parse_flag() in flags.py per the docstring; only modify flags.py" \
    /abs/path/to/project --mode build --policy allow --scope flags.py --verify "python -m pytest -q" --wait 600
nar wait <task_id>
nar inspect <task_id> summary
nar inspect <task_id> diagnose      # sanitized, shareable error report
```

3. Continue the SAME native session for follow-up work on the same module:

```bash
nar run zcode "Now handle the --verbose edge case we discussed" /abs/path/to/project \
    --session-ref <previous_task_id> --mode build --policy allow --verify "python -m pytest -q"
```

## Configuration (the teaching section)

Config file lookup order: `--config` flag → `NAR_CONFIG` env →
`<home>/agents.json` (home = `NAR_HOME` env or `~/.native-agent-router`) →
built-in defaults + auto-discovery.

### Top-level settings

| Key | Default | Meaning |
|---|---|---|
| `wait_default_sec` | 120 | default bounded block for `wait` |
| `wait_max_sec` | 3600 | cap on a single `wait` call (does NOT cap task timeouts) |
| `timeout_default_sec` | 1800 | default per-turn timeout |
| `verify_timeout_sec` | 600 | timeout per verification command |
| `max_result_chars` | 12000 | reserved for configuration compatibility; current result fields have their own fixed bounds |
| `log_tail_chars` | 4000 | default page size for `inspect` when no limit is supplied |
| `repair_default` | true | one pre-authorized targeted repair per task |
| `verify_allow_shell` | false | verify commands run without a shell (argv / `shlex.split`); set true only if you need shell semantics |
| `require_git_baseline` | false | fail-closed: refuse to run a worker in a non-git workspace so changes are always auditable |

### Agents

Every task must name an `agent_id` explicitly. There is **no global
"selected backend"** that other calls could silently change.

| Key | Applies to | Meaning |
|---|---|---|
| `adapter` | all | `zcode-native`, `acp-generic`, or `stub` (tests) |
| `enabled` | all | set false to hide an agent |
| `command` | acp-generic | argv that starts the ACP server, e.g. `["opencode","acp"]` |
| `cwd_arg` | acp-generic | some ACP servers want `--cwd <ws>`; set the flag name here |
| `model` | both | model selection (see below) |
| `model_args` | acp-generic | CLI-arg model injection template, e.g. `["--model","{model}"]` |
| `provider` | zcode-native | ZCode provider id from your own `~/.zcode/v2/config.json` |
| `thought_level` | zcode-native | e.g. `low` / `high` / `max` (per model capabilities) |
| `default_mode` | both | defaults to `plan`; ZCode also supports `build` / `edit` / `yolo` |
| `default_permission_policy` | both | `deny` (default, honest) or `allow` (auto-approve within one task) |
| `tool_allowlist` | zcode-native | native tool-set restriction, e.g. `["Read","Grep","Glob"]` |
| `zcode_home` | zcode-native | `"isolated"` = keep NAR's ZCode sessions in a private home (see docs/SECURITY.md) |
| `credentials` | zcode-native | `auto` (default): reuse the user's own ZCode config; see below |
| `env` | both | extra environment variables for the agent subprocess; values are stored in `agents.json` |

### Model selection

- **ZCode (`zcode-native`)**: set `provider` + `model` (+ optional
  `thought_level`). NAR builds ZCode 0.16's `runtimeModel` payload from **your
  own** `~/.zcode/v2/config.json` and passes it inside the protocol only.
  No credential is ever copied to disk by default, no provider/base URL/billing
  channel is changed, and cold `session/resume` works (verified live on 0.16.5).
- **ACP agents (`acp-generic`)**: NAR first tries the ACP-native path —
  `session/set_config_option` / `session/set_model` (works with opencode, whose
  `session/new` advertises a `model` config option) — then falls back to
  `model_args` CLI substitution (e.g. `gemini --experimental-acp --model X`).

### ZCode discovery (how `zcode.cjs` is found)

Order: `ZCODE_BIN` env → `zcode` on PATH → Windows registry uninstall entries
(InstallLocation) → `%LOCALAPPDATA%\Programs\ZCode` → `Program Files\ZCode` on
every drive. If none match, `nar doctor` tells you exactly what to set. The
desktop app does not add its CLI to PATH — that is normal.

### Credentials, honestly

`credentials: "auto"` reads (read-only) the provider you already configured in
the ZCode desktop app and passes the same provider/model/base-URL/key to the
headless app-server **in the protocol payload only**. NAR never logs keys,
never uploads them anywhere, never switches billing channels, and never
bypasses authentication or plan limits. If you prefer a private session store,
set `zcode_home: "isolated"` (documents trade-offs in docs/SECURITY.md).

## MCP integration

Run `nar-mcp` over stdio. Examples:

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.native-agent-router]
command = "nar-mcp"
args = []
```

**opencode** (`opencode.json` in your project or `~/.config/opencode/`):

```json
{ "mcp": { "native-agent-router": { "type": "local", "command": ["nar-mcp"], "enabled": true } } }
```

**Claude Code** (`.mcp.json`):

```json
{ "mcpServers": { "native-agent-router": { "command": "nar-mcp" } } }
```

Then ask your orchestrator, in one sentence:
*"Run `agents`, then `run` this task to `zcode` with scope and verify, and
`wait` for it."* See `skill/native-agent-router/SKILL.md` for the delegation
rules you can drop into any agent's skill folder.

## The five MCP tools

| Tool | Purpose |
|---|---|
| `agents` | configured agents + capabilities + **score stats** (routing data) |
| `run` | submit one complete work unit (goal/scope/forbid/verify/budget/mode/session_ref) |
| `wait` | bounded block until terminal/blocked; never busy-polls |
| `inspect` | paged on-demand reads: status/summary/diff/verify/log/raw/usage/**diagnose** |
| `cancel` | request stop and report whether it was **confirmed** |

`run` returning means *submitted*, not *done*. Terminal states are
`succeeded|failed|cancelled|interrupted`. `blocked` is observable but nonterminal:
the kernel keeps the workspace lock and watches the native session until a real
terminal event arrives (or you `cancel`/`kill`). A token budget violation uses
status `failed` with code `budget_exceeded`.

## CLI reference

```
nar agents | doctor | list
nar run <agent_id> <goal> <workspace> [--scope ...] [--verify "cmd"] [--mode build]
        [--policy allow|deny] [--session-ref TASK] [--timeout N] [--wait N]
        [--budget N] [--idempotency-key K] [--no-repair]
nar wait <task_id> [--timeout N]
nar inspect <task_id> [status|summary|diff|verify|log|raw|usage|diagnose] [--offset N] [--limit N]
nar cancel <task_id>
nar kill <task_id> --yes        # force: terminate agent + release lock (user boundary)
nar stats [agent_id]            # deterministic per-agent score statistics
```

## Tests

```bash
pip install -e ".[dev]"
pytest -q                 # 67 tests, no model calls, no network
pytest -q -m real         # opt-in: hits your real installed agents (spends tokens!)
```

## Docs

[ARCHITECTURE.md](docs/ARCHITECTURE.md) ·
[BACKENDS.md](docs/BACKENDS.md) ·
[VERIFY.md](docs/VERIFY.md) ·
[BENCHMARK.md](docs/BENCHMARK.md) ·
[SECURITY.md](docs/SECURITY.md) ·
[ROLLBACK.md](docs/ROLLBACK.md) ·
[Skill](skill/native-agent-router/SKILL.md)

## Status & honesty

- Verified live on this machine: ZCode 0.16.5 (read-only, native resume,
  edit+verify, permission handling, real usage), opencode 1.18.15 over ACP
  (handshake, model switch, resume). Evidence paths in docs/VERIFY.md.
- Not yet verified here: Gemini CLI (not installed), Claude Code as ACP
  (installed but currently unusable per user), Codex as orchestrator client.
- No claim of "optimal" or fixed savings percentage: see BENCHMARK.md for what
  was measured and what wasn't.
- MIT licensed. Independent community project; not affiliated with Z.AI/ZCode,
  OpenAI Codex, or SST/opencode.
