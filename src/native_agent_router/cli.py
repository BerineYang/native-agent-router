"""Command-line client. Shares the same Kernel as the MCP server.

  nar agents                       list configured agents
  nar doctor                       environment + discovery diagnostics
  nar run ...                      submit a task (see README for examples)
  nar wait TID [--timeout N]       bounded wait
  nar inspect TID [what]           status|summary|diff|verify|log|raw|usage
  nar cancel TID                   request cancel, report confirmation
  nar list                         list tasks
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .config import load_config
from .kernel.kernel import Kernel
from .kernel.store import TaskStore


def _kernel(args) -> Kernel:
    return Kernel(load_config(getattr(args, "config", None)), TaskStore())


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def main(argv=None):
    p = argparse.ArgumentParser(prog="nar", description="native-agent-router CLI")
    p.add_argument("--config", help="path to agents.json")
    p.add_argument("--version", action="version", version=f"nar {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("agents")
    sub.add_parser("doctor")
    sub.add_parser("list")

    pr = sub.add_parser("run")
    pr.add_argument("agent_id")
    pr.add_argument("goal")
    pr.add_argument("workspace")
    pr.add_argument("--scope", nargs="*", default=None, help="modifiable files (relative)")
    pr.add_argument("--forbid", nargs="*", default=None)
    pr.add_argument("--verify", action="append", default=None, help="verification command (repeatable)")
    pr.add_argument("--mode", default=None)
    pr.add_argument("--session-ref", default=None, help="task_id to continue its native session")
    pr.add_argument("--timeout", type=float, default=None)
    pr.add_argument("--wait", type=float, default=None)
    pr.add_argument("--budget", type=int, default=None, help="token budget")
    pr.add_argument("--policy", choices=["allow", "deny"], default=None)
    pr.add_argument("--no-repair", action="store_true")
    pr.add_argument("--idempotency-key", default=None)

    pw = sub.add_parser("wait")
    pw.add_argument("task_id")
    pw.add_argument("--timeout", type=float, default=120)

    pi = sub.add_parser("inspect")
    pi.add_argument("task_id")
    pi.add_argument("what", nargs="?", default="status",
                    choices=["status", "summary", "diff", "verify", "log", "raw", "usage", "diagnose"])
    pi.add_argument("--offset", type=int, default=0)
    pi.add_argument("--limit", type=int, default=4000)

    pc = sub.add_parser("cancel")
    pc.add_argument("task_id")

    pk = sub.add_parser("kill")
    pk.add_argument("task_id")
    pk.add_argument("--yes", action="store_true", help="required confirmation for force-kill")

    ps = sub.add_parser("stats")
    ps.add_argument("agent_id", nargs="?", default=None)

    args = p.parse_args(argv)

    if args.cmd == "doctor":
        from .doctor import run_doctor
        run_doctor(getattr(args, "config", None))
        return 0

    k = _kernel(args)
    if args.cmd == "agents":
        _print(k.agents())
    elif args.cmd == "list":
        _print(k.list_tasks())
    elif args.cmd == "run":
        _print(k.submit(
            args.agent_id, args.goal, args.workspace,
            scope_files=args.scope, forbid=args.forbid, verify=args.verify,
            mode=args.mode, session_ref=args.session_ref, timeout_sec=args.timeout,
            wait_sec=args.wait, budget_tokens=args.budget,
            permission_policy=args.policy,
            repair_allowed=False if args.no_repair else None,
            idempotency_key=args.idempotency_key))
    elif args.cmd == "wait":
        _print(k.wait(args.task_id, args.timeout))
    elif args.cmd == "inspect":
        _print(k.inspect(args.task_id, args.what, args.offset, args.limit))
    elif args.cmd == "cancel":
        _print(k.cancel(args.task_id))
    elif args.cmd == "kill":
        if not args.yes:
            print("refusing to force-kill without --yes (this terminates the agent process and releases the lock)")
            return 2
        _print(k.kill(args.task_id))
    elif args.cmd == "stats":
        if args.agent_id:
            _print({args.agent_id: k.stats.summary(args.agent_id)})
        else:
            _print({a: k.stats.summary(a) for a in k.config.agent_ids()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
