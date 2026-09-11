"""MCP server exposing the kernel through five stable tools:
agents / run / wait / inspect / cancel.

Run:  nar-mcp          (stdio transport, for MCP client configs)
New agent backends are added by editing agents.json, not by adding tools.

Error semantics (deliberate): a tool that CANNOT perform its job raises, which
FastMCP turns into an MCP result with isError=True (distinct from a successful
call whose business status field says "failed"). Business outcomes (succeeded /
failed / blocked / cancelled / interrupted) are returned as normal structured dicts with an
`ok` field and a stable `code`. This lets orchestrators drive retry policy from
`code`/`status` without guessing whether a payload is an error or a result.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import __version__
from .diagnostics import classify_error
from .kernel.kernel import Kernel, KernelError
from .kernel.store import TaskStore
from .config import load_config

mcp = FastMCP("native-agent-router")
_kernel: Kernel | None = None


class ToolFailure(Exception):
    """Raised for genuine tool failures -> MCP isError=True."""


def kernel() -> Kernel:
    global _kernel
    if _kernel is None:
        _kernel = Kernel(load_config(), TaskStore())
    return _kernel


def _run(fn, *a, **k):
    """Call a kernel op; bad-input / not-found become isError, business states
    stay normal returns."""
    try:
        return fn(*a, **k)
    except KernelError as e:
        code = classify_error(str(e))
        raise ToolFailure(f"[{code}] {e}") from e


@mcp.tool()
def agents() -> list[dict]:
    """List configured worker agents and their capabilities (adapter type,
    transport, native session resume support, usage accounting quality) plus
    per-agent SCORE STATS (avg_score, success_rate, repair_rate,
    out_of_scope_rate, blocked_rate, avg_tokens, median_duration_sec,
    last_tasks). Use the stats to route work: high-score agents for hard
    units, cheaper/lower-score agents for well-specified mechanical units.
    Use this before run() to pick a valid agent_id."""
    return _run(kernel().agents)


@mcp.tool()
def run(agent_id: str, goal: str, workspace: str,
        scope_files: list[str] | None = None, forbid: list[str] | None = None,
        verify: list[str] | None = None, mode: str | None = None,
        session_ref: str | None = None, timeout_sec: float | None = None,
        wait_sec: float | None = None, budget_tokens: int | None = None,
        permission_policy: str | None = None, repair_allowed: bool | None = None,
        idempotency_key: str | None = None) -> dict:
    """Submit a complete work unit to a worker agent and return its task_id.

    Requirements:
    - agent_id: exact id from agents(); every task is explicitly bound to one agent.
    - goal: full objective, constraints and context in ONE prompt (complete work
      unit; do not ping-pong per file/line).
    - workspace: directory path (canonicalized to an absolute path by the kernel).
    - scope_files: RELATIVE paths the worker may modify (absolute or escaping
      paths are rejected; enforced by diff audit).
    - forbid: extra prohibitions.
    - verify: commands executed by the kernel after the turn; the worker cannot
      pass by editing them. Prefer list form; string form is arg-split (no shell)
      unless config verify_allow_shell is on. Exit code 0 = pass.
    - mode: adapter-specific permission mode (zcode: plan|build|edit|yolo).
    - session_ref: task_id of a previous task on the SAME agent+workspace to
      continue its native session. Cross-agent/workspace reuse is rejected.
    - timeout_sec: per-turn timeout; on timeout the task becomes blocked (never
      silently resubmitted; the lock is held and a watcher keeps observing).
    - wait_sec: bounded block until finish (default: return immediately).
    - budget_tokens: hard token budget; exceeded -> status failed with code budget_exceeded.
    - permission_policy: "deny" (default) or "allow" (auto-allow within this task).
    - repair_allowed: one pre-authorized targeted repair attempt (default true).
    - idempotency_key: resubmitting the same key returns the original task.

    Returns a snapshot dict with ok/status/code. A run() success only means
    SUBMITTED, not done — follow with wait()/inspect().
    """
    return _run(kernel().submit, agent_id, goal, workspace, scope_files=scope_files,
                forbid=forbid, verify=verify, mode=mode, session_ref=session_ref,
                timeout_sec=timeout_sec, wait_sec=wait_sec, budget_tokens=budget_tokens,
                permission_policy=permission_policy, repair_allowed=repair_allowed,
                idempotency_key=idempotency_key)


@mcp.tool()
def wait(task_id: str, timeout_sec: float = 120) -> dict:
    """Bounded blocking wait for a task to reach a terminal or blocked state.
    Does not busy-poll. Returns the snapshot dict; if it still runs when the
    timeout expires, wait_timed_out=true — call again later. Terminal states are
    succeeded/failed/cancelled/interrupted. blocked is observable but nonterminal:
    the watcher continues to hold the lock and observe the native session."""
    return _run(kernel().wait, task_id, timeout_sec)


@mcp.tool()
def inspect(task_id: str, what: str = "status", offset: int = 0, limit: int = 4000) -> dict:
    """Read task details on demand (logs/diffs stay on disk; read slices here).
    what: status | summary | diff | verify | log | raw | usage | diagnose.
    - diagnose returns a compact SANITIZED report (secrets/emails/blobs redacted,
      size-bounded) with a stable `code` and actionable `hints` — safe to share.
    """
    return _run(kernel().inspect, task_id, what, offset, limit)


@mcp.tool()
def cancel(task_id: str) -> dict:
    """Request cancellation and report whether the stop was CONFIRMED by the
    native runtime (confirmed=true only on an observed terminal/exit). A cancel
    request alone is not a stop."""
    return _run(kernel().cancel, task_id)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
