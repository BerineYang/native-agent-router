"""Model-free execution kernel: submit, bounded wait, fixed verification,
one pre-authorized targeted repair, cancellation with confirmation,
idempotent submission, workspace mutex, recovery and usage accounting.

Shared by the MCP server and the CLI. Never calls an LLM.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path

from ..adapters.base import Adapter, AdapterError, ResumeUnsupported, TerminalTurn, build_adapter
from ..config import Config, load_config
from ..diagnostics import classify_error
from .stats import AgentStats, compute_score
from .store import TaskStore, WorkspaceBusy, WorkspaceLock

TERMINAL_STATES = {"succeeded", "failed", "cancelled", "interrupted", "budget_exceeded"}
# "blocked" is NOT terminal: the watcher keeps observing and cancel/kill still apply.
ACTIVE_STATES = {"queued", "running", "cancel_requested", "blocked"}
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

VERIFY_TAIL_CHARS = 2500
PROMPT_TAIL_CHARS = 1500


class KernelError(Exception):
    pass


def canonical_workspace(workspace: str) -> str:
    """expanduser -> abspath -> normpath, validated once at the boundary so the
    task record, workspace lock and session_ref checks all share one form."""
    if workspace is None or not str(workspace).strip():
        raise KernelError("workspace is required")
    p = os.path.normpath(os.path.abspath(os.path.expanduser(str(workspace).strip())))
    if not Path(p).is_dir():
        raise KernelError(f"workspace does not exist or is not a directory: {p}")
    return p


class Kernel:
    def __init__(self, config: Config | None = None, store: TaskStore | None = None):
        self.config = config or load_config()
        self.store = store or TaskStore(self.config.home)
        self._cond = threading.Condition()
        self._active: dict[str, dict] = {}
        self._active_lock = threading.Lock()
        self.stats = AgentStats(self.config.home)
        self.store.mark_orphans_unknown()

    # ------------------------------------------------------------------ API

    def agents(self) -> list[dict]:
        out = []
        for aid in self.config.agent_ids():
            try:
                spec = self.config.agent_spec(aid)
                caps_probe = build_adapter(spec, Path(self.store.log_dir("probe")), "deny")
                caps = caps_probe.capabilities()
                err = None
            except Exception as e:  # discovery failure is reportable, not fatal
                spec, caps, err = None, {}, str(e)
            entry = {"agent_id": aid, "error": err, "stats": self.stats.summary(aid)}
            if spec:
                entry.update({
                    "adapter": spec["adapter"],
                    "argv": spec.get("argv"),
                    "model": spec.get("model"),
                    "default_mode": spec.get("default_mode"),
                    "default_permission_policy": spec.get("default_permission_policy"),
                    "capabilities": caps,
                })
            out.append(entry)
        return out

    def submit(self, agent_id: str, goal: str, workspace: str, *, scope_files=None, forbid=None,
               verify=None, mode=None, session_ref=None, timeout_sec=None, wait_sec=None,
               budget_tokens=None, permission_policy=None, repair_allowed=None,
               idempotency_key=None) -> dict:
        if not goal or not str(goal).strip():
            raise KernelError("goal is required")
        workspace = canonical_workspace(workspace)
        scope = [str(s) for s in (scope_files or [])]
        for s in scope:
            norm = s.replace("\\", "/")
            if os.path.isabs(s) or norm.startswith("/") or ".." in Path(norm).parts:
                raise KernelError(f"scope_files must be relative paths inside the workspace; got '{s}'")
        settings = self.config.settings
        spec_in = {
            "agent_id": agent_id,
            "goal": str(goal),
            "workspace": workspace,
            "scope_files": scope,
            "forbid": [str(s) for s in (forbid or [])],
            "verify": [v if isinstance(v, list) else str(v) for v in (verify or [])],
            "mode": mode,
            "permission_policy": permission_policy,
            "session_ref": session_ref,
            "timeout_sec": min(float(timeout_sec or settings["timeout_default_sec"]), float(settings["wait_max_sec"])),
            "budget_tokens": budget_tokens,
            "repair_allowed": settings["repair_default"] if repair_allowed is None else bool(repair_allowed),
            "idempotency_key": idempotency_key,
        }
        try:
            self.config.agent_spec(agent_id)
        except KeyError as e:
            raise KernelError(str(e)) from e
        if session_ref:
            self._validate_session_ref(agent_id, workspace, session_ref)
        task, duplicate = self.store.create(spec_in)
        if duplicate:
            snap = self.snapshot(task["task_id"])
            snap["duplicate"] = True
            return snap
        self.store.append_timeline(task["task_id"], "submitted")
        t = threading.Thread(target=self._execute, args=(dict(task),), daemon=True)
        with self._active_lock:
            self._active[task["task_id"]] = {"thread": t}
        t.start()
        snap = self.snapshot(task["task_id"])
        if wait_sec:
            return self.wait(task["task_id"], timeout_sec=wait_sec)
        return snap

    def wait(self, task_id: str, timeout_sec: float | None = None) -> dict:
        timeout = min(float(timeout_sec or self.config.settings["wait_default_sec"]),
                      float(self.config.settings["wait_max_sec"]))
        deadline = time.time() + timeout
        with self._cond:
            while True:
                snap = self.snapshot(task_id)
                if snap["status"] in TERMINAL_STATES or snap["status"] == "blocked":
                    return snap
                remaining = deadline - time.time()
                if remaining <= 0:
                    snap["wait_timed_out"] = True
                    return snap
                self._cond.wait(timeout=min(remaining, 0.5))

    def inspect(self, task_id: str, what: str = "status", offset: int = 0, limit: int | None = None) -> dict:
        task = self.store.get(task_id)
        if not task:
            raise KernelError(f"unknown task_id {task_id}")
        limit = limit or self.config.settings["log_tail_chars"]
        if what == "status":
            return self.snapshot(task_id)
        if what == "summary":
            return {"task_id": task_id, "status": task["status"], "result": task.get("result"),
                    "error": task.get("error")}
        if what == "usage":
            return {"task_id": task_id, "usage": task.get("usage"),
                    "note": "quality: real=agent-reported, partial/estimated/unknown see SECURITY.md"}
        if what == "diagnose":
            from ..diagnostics import build_diagnosis
            return build_diagnosis(self.store, task, self.config)
        if what in ("diff", "log", "verify", "raw"):
            path = self._inspect_path(task_id, what)
            if not path or not Path(path).is_file():
                return {"task_id": task_id, "what": what, "content": "", "note": "not available"}
            data = Path(path).read_text(encoding="utf-8", errors="replace")
            total = len(data)
            chunk = data[offset:offset + limit]
            trunc = offset + limit < total
            marker = f"\n...[showing chars {offset}-{offset + len(chunk)} of {total}; continue with offset={offset + limit}]" if trunc else ""
            return {"task_id": task_id, "what": what, "path": str(path), "offset": offset,
                    "content": chunk + marker, "total_chars": total, "truncated": trunc}
        raise KernelError(f"unknown inspect what='{what}' "
                          "(status|summary|diff|verify|log|raw|usage|diagnose)")

    def cancel(self, task_id: str) -> dict:
        task = self.store.get(task_id)
        if not task:
            raise KernelError(f"unknown task_id {task_id}")
        if task["status"] in TERMINAL_STATES:
            return {"task_id": task_id, "requested": False, "confirmed": True, "status": task["status"],
                    "note": "task already finished"}
        self.store.update(task_id, status="cancel_requested")
        self.store.append_timeline(task_id, "cancel_requested")
        with self._cond:
            self._cond.notify_all()
        with self._active_lock:
            act = self._active.get(task_id)
        confirmed = False
        adapter = None
        if act:
            ev = act.get("cancel_event")
            if ev:
                ev.set()
            adapter = act.get("adapter")
            sid = task.get("native_session_id")
            if adapter and sid:
                try:
                    confirmed = bool(adapter.cancel(sid))
                except Exception:
                    confirmed = False
            th = act.get("thread")
            if th:
                th.join(timeout=10)
        final = self.store.get(task_id) or {}
        status = final.get("status")
        note = getattr(adapter, "last_cancel_note", None) if adapter else None
        return {"task_id": task_id, "requested": True, "confirmed": bool(confirmed), "status": status,
                "cancel_note": note,
                "note": "confirmed=true means the native runtime stopped the turn in response to this request"}

    def kill(self, task_id: str) -> dict:
        """Explicit user override: force-terminate the agent process and release
        the workspace lock. For stuck/unconfirmed cancels only — the boundary
        decision belongs to the human/orchestrator, never to the kernel."""
        task = self.store.get(task_id)
        if not task:
            raise KernelError(f"unknown task_id {task_id}")
        if task["status"] in TERMINAL_STATES:
            return {"task_id": task_id, "killed": False, "status": task["status"],
                    "note": "already terminal"}
        with self._active_lock:
            act = dict(self._active.get(task_id) or {})
        self.store.update(task_id, status="interrupted", error="killed by user request")
        self.store.append_timeline(task_id, "killed")
        ev = act.get("cancel_event")
        if ev:
            ev.set()
        adapter = act.get("adapter")
        if adapter:
            try:
                adapter.close()
            except Exception:
                pass
        th = act.get("thread") or act.get("watcher")
        if th:
            th.join(timeout=8)
        lk = act.get("lock")
        if lk:
            try:
                lk.release()
            except Exception:
                pass
        with self._active_lock:
            self._active.pop(task_id, None)
        self._signal()
        return {"task_id": task_id, "killed": True, "status": "interrupted",
                "note": "agent process terminated and workspace lock released; "
                        "native session may still hold state — reconcile before reuse"}

    def list_tasks(self) -> list[dict]:
        return [{"task_id": t["task_id"], "status": t["status"], "agent_id": t["agent_id"],
                 "goal": (t.get("goal") or "")[:60], "created_at": t.get("created_at"),
                 "native_session_id": t.get("native_session_id")}
                for t in self.store.list_tasks()]

    def reconcile(self, task_id: str) -> dict:
        """For 'unknown' tasks: establish current facts without resubmitting."""
        task = self.store.get(task_id)
        if not task:
            raise KernelError(f"unknown task_id {task_id}")
        if task["status"] not in ("unknown", "running", "cancel_requested"):
            return self.snapshot(task_id)
        facts = self._current_facts(task)
        self.store.update(task_id, status="interrupted",
                          error="interrupted: owner process died; facts re-established, orchestrator decides next step")
        self.store.append_timeline(task_id, "reconciled", **{k: facts[k] for k in facts})
        return self.snapshot(task_id) | {"facts": facts}

    # ------------------------------------------------------------- internals

    def _inspect_path(self, task_id: str, what: str) -> str | None:
        d = self.store.log_dir(task_id)
        if what == "diff":
            p = d / "diff.patch"
        elif what == "log":
            p = d / "events.jsonl"
        elif what == "raw":
            p = d / "native-raw.jsonl"
        elif what == "verify":
            p = d / "verify-combined.log"
        else:
            return None
        return str(p)

    def snapshot(self, task_id: str) -> dict:
        task = self.store.get(task_id)
        if not task:
            raise KernelError(f"unknown task_id {task_id}")
        snap = {k: task.get(k) for k in ("task_id", "run_id", "status", "agent_id", "workspace", "goal",
                                         "native_session_id", "session_ref", "mode", "error",
                                         "usage", "result", "timeline", "created_at", "updated_at")}
        snap["ok"] = task["status"] == "succeeded"
        snap["code"] = (task.get("result") or {}).get("error_code") or classify_error(task.get("error") or "", task["status"])
        return snap

    def _signal(self):
        with self._cond:
            self._cond.notify_all()

    def _set_status(self, task_id: str, status: str, **fields):
        self.store.update(task_id, status=status, **fields)
        self._signal()

    def _compose_prompt(self, task: dict, repair_note: str | None = None) -> str:
        lines = [f"Goal: {task['goal']}"]
        if task.get("scope_files"):
            lines.append("Modifiable files (nothing else may change): " + ", ".join(task["scope_files"]))
        if task.get("forbid"):
            lines.append("Forbidden: " + "; ".join(task["forbid"]))
        lines.append("Constraints: do not touch files outside the modifiable list; never run git commit; "
                     "never modify tests or acceptance scripts.")
        if task.get("verify"):
            cmds = [" ".join(c) if isinstance(c, list) else c for c in task["verify"]]
            lines.append("Acceptance (executed externally after your work): " + " && ".join(cmds))
        if repair_note:
            lines.append("REPAIR DIRECTIVE: " + repair_note)
        return "\n".join(lines)

    def _git(self, workspace: str, *args: str, timeout: float = 60) -> tuple[int, str]:
        try:
            r = subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="replace",
                               creationflags=CREATE_NO_WINDOW)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except (OSError, subprocess.TimeoutExpired) as e:
            return -1, str(e)

    def _capture_baseline(self, task_id: str, task: dict) -> dict:
        code, out = self._git(task["workspace"], "rev-parse", "--is-inside-work-tree")
        if code == 0 and out.strip() == "true":
            head_code, head = self._git(task["workspace"], "rev-parse", "HEAD")
            baseline = {"git": True, "head": head.strip() if head_code == 0 else None}
        else:
            baseline = {"git": False}
        task["baseline"] = baseline
        self.store.update(task["task_id"], baseline=baseline)
        return baseline

    def _capture_diff(self, task: dict) -> dict:
        ws = task["workspace"]
        base = task.get("baseline") or {}
        d = self.store.log_dir(task["task_id"])
        if not base.get("git"):
            return {"git": False, "changed_files": None,
                    "note": "workspace is not a git repo; isolation of changes cannot be verified"}
        _, status = self._git(ws, "status", "--porcelain")
        files = sorted({ln[3:].strip() for ln in status.splitlines() if len(ln) > 3})
        # artifacts produced by running the verification itself (bytecode caches,
        # etc.) are not worker edits; keep them visible separately
        artifacts = [f for f in files if f.replace("\\", "/").startswith(
            ("__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/"))
            or f.endswith(".pyc")]
        files = [f for f in files if f not in artifacts]
        diff_code, diff = self._git(ws, "diff", "HEAD")
        (d / "diff.patch").write_text(diff or "", encoding="utf-8")
        scope = task.get("scope_files") or []
        out_of_scope = []
        if scope:
            scope_norm = {s.replace("\\", "/").lstrip("./") for s in scope}
            for f in files:
                fn = f.replace("\\", "/").lstrip("./")
                if fn not in scope_norm:
                    out_of_scope.append(f)
        return {"git": True, "changed_files": files, "artifacts": artifacts,
                "out_of_scope": out_of_scope, "diff_log": str(d / "diff.patch")}

    def _run_verify(self, task: dict) -> list[dict]:
        results = []
        d = self.store.log_dir(task["task_id"])
        combined = []
        timeout = float(self.config.settings["verify_timeout_sec"])
        allow_shell = bool(self.config.settings.get("verify_allow_shell"))
        for i, cmd in enumerate(task.get("verify") or []):
            log_path = d / f"verify-{i}.log"
            if isinstance(cmd, list):
                argv, use_shell = cmd, False
            elif allow_shell:
                argv, use_shell = cmd, True
            else:
                # no-shell default: split into argv so a verify string cannot
                # smuggle shell metacharacters (SECURITY.md)
                try:
                    argv, use_shell = shlex.split(cmd, posix=True), False
                except ValueError:
                    argv, use_shell = cmd, allow_shell
            try:
                r = subprocess.run(argv, cwd=task["workspace"], capture_output=True, text=True,
                                   timeout=timeout, encoding="utf-8", errors="replace",
                                   shell=use_shell,
                                   creationflags=CREATE_NO_WINDOW)
                out = (r.stdout or "") + (r.stderr or "")
                code = r.returncode
            except subprocess.TimeoutExpired as e:
                out = (e.stdout or "") if isinstance(e.stdout, str) else ""
                out += f"\n[verify timeout after {timeout}s]"
                code = 124
            except OSError as e:
                out = f"[failed to start: {e}]"
                code = 127
            log_path.write_text(out or "", encoding="utf-8")
            combined.append(f"=== verify[{i}]: {argv} exit={code} ===\n{out}")
            results.append({"cmd": argv if isinstance(argv, str) else " ".join(argv),
                            "exit_code": code, "ok": code == 0,
                            "log": str(log_path),
                            "tail": (out or "")[-VERIFY_TAIL_CHARS:]})
        if combined:
            (d / "verify-combined.log").write_text("\n".join(combined), encoding="utf-8")
        return results

    def _validate_session_ref(self, agent_id: str, workspace: str, ref: str):
        ref_task = self.store.get(ref)
        if not ref_task:
            raise KernelError(f"session_ref task not found: {ref}")
        if ref_task.get("agent_id") != agent_id:
            raise KernelError(
                f"session cross-use denied: {ref} belongs to agent '{ref_task.get('agent_id')}', task targets '{agent_id}'")
        if os.path.abspath(ref_task.get("workspace") or "") != os.path.abspath(workspace):
            raise KernelError(
                f"session cross-use denied: {ref} workspace {ref_task.get('workspace')} != {workspace}")
        if not ref_task.get("native_session_id"):
            raise KernelError(f"session_ref {ref} has no native session to continue")

    def _resolve_session(self, task: dict, adapter: Adapter) -> dict:
        """Returns {'native_session_id', 'resumed', 'evidence'}."""
        ref = task.get("session_ref")
        if not ref:
            sid = adapter.create_session(task["workspace"], task.get("mode"))
            self.store.update(task["task_id"], native_session_id=sid)
            return {"native_session_id": sid, "resumed": False, "evidence": "new native session"}
        ref_task = self.store.get(ref)
        if not ref_task:
            raise KernelError(f"session_ref task not found: {ref}")
        if ref_task.get("agent_id") != task["agent_id"]:
            raise KernelError(
                f"session cross-use denied: {ref} belongs to agent '{ref_task.get('agent_id')}', task targets '{task['agent_id']}'")
        if os.path.abspath(ref_task.get("workspace") or "") != os.path.abspath(task["workspace"]):
            raise KernelError(
                f"session cross-use denied: {ref} workspace {ref_task.get('workspace')} != {task['workspace']}")
        sid = ref_task.get("native_session_id")
        if not sid:
            raise KernelError(f"session_ref {ref} has no native session to continue")
        ev = adapter.resume_session(sid, task["workspace"])
        self.store.update(task["task_id"], native_session_id=sid)
        return {"native_session_id": sid, "resumed": True, "evidence": ev}

    def _register(self, task_id: str, **kw):
        with self._active_lock:
            act = self._active.setdefault(task_id, {})
            act.update(kw)

    def _execute(self, task: dict):
        task_id = task["task_id"]
        lock = WorkspaceLock(task["workspace"], task_id, self.config.home)
        cancel_event = threading.Event()
        adapter: Adapter | None = None
        handed_off = False
        try:
            self._set_status(task_id, "running")
            try:
                lock.acquire()
            except WorkspaceBusy as e:
                self._set_status(task_id, "failed", error=f"workspace_busy: {e}")
                return
            self.store.append_timeline(task_id, "workspace_locked")
            self._capture_baseline(task_id, task)
            base = task.get("baseline") or {}
            if self.config.settings.get("require_git_baseline") and not base.get("git"):
                self._set_status(task_id, "failed",
                                 error="require_git_baseline: refusing to run a worker in a non-git "
                                       "workspace (changes would be unauditable)")
                return
            spec = self.config.agent_spec(task["agent_id"])
            policy = (task.get("permission_policy")
                      or spec.get("default_permission_policy") or "deny")
            log_dir = self.store.log_dir(task_id)
            log_dir.mkdir(parents=True, exist_ok=True)
            adapter = build_adapter(spec, log_dir, policy)
            self._register(task_id, adapter=adapter, cancel_event=cancel_event, lock=lock)
            adapter.start(task["workspace"])
            session_info = self._resolve_session(task, adapter)
            self.store.append_timeline(task_id, "session_ready", **{
                "native_session_id": session_info["native_session_id"], "resumed": session_info["resumed"]})

            turn = adapter.prompt(session_info["native_session_id"],
                                  self._compose_prompt(task),
                                  task["timeout_sec"], cancel_event,
                                  input_id=f"{task_id}:t1")
            self._record_usage(task, adapter, turn)
            self.store.append_timeline(task_id, "turn_done", reason=turn.reason, ok=turn.ok)
            if turn.reason == "timeout":
                # never release the lock or kill the process on an unconfirmed
                # turn: hand off to a watcher that keeps observing the native
                # session until a real terminal state arrives.
                handed_off = True
                self._handoff_watch(task, adapter, session_info, lock, cancel_event)
                return
            self._post_turn(task, adapter, session_info, turn, lock, cancel_event)
        except ResumeUnsupported as e:
            self._finish(task_id, "failed", error=f"resume unsupported: {e}")
        except KernelError as e:
            self._finish(task_id, "failed", error=str(e))
        except Exception as e:  # adapter crash, spawn failure, ...
            self._finish(task_id, "failed", error=f"{type(e).__name__}: {e}")
        finally:
            if not handed_off:
                self._cleanup(task_id, adapter, lock)

    def _cleanup(self, task_id: str, adapter: Adapter | None, lock: WorkspaceLock):
        if adapter:
            try:
                adapter.close()
            except Exception:
                pass
        try:
            lock.release()
        except Exception:
            pass
        with self._active_lock:
            self._active.pop(task_id, None)
        self._signal()

    def _handoff_watch(self, task: dict, adapter: Adapter, session_info: dict,
                       lock: WorkspaceLock, cancel_event: threading.Event):
        task_id = task["task_id"]
        note = getattr(adapter, "last_cancel_note", None)
        self._set_status(task_id, "blocked",
                         error=f"turn timeout after {task['timeout_sec']}s; native session still running; "
                               "workspace lock held and watcher active; use cancel (with confirmation check) "
                               "or wait; never blindly resubmit side-effecting work")
        self.store.append_timeline(task_id, "blocked_watching")
        t = threading.Thread(target=self._watch, args=(dict(task), adapter, session_info, lock, cancel_event),
                             daemon=True)
        with self._active_lock:
            act = self._active.setdefault(task_id, {})
            act["watcher"] = t
        t.start()

    def _watch(self, task: dict, adapter: Adapter, session_info: dict,
               lock: WorkspaceLock, cancel_event: threading.Event):
        task_id = task["task_id"]
        sid = session_info["native_session_id"]
        try:
            while True:
                time.sleep(1.0)
                cur = self.store.get(task_id) or {}
                if cur.get("status") in ("interrupted",):
                    return  # killed by user; _kill already cleaned up
                turn = None
                try:
                    turn = adapter.poll(sid)
                except Exception as e:
                    self._finish(task_id, "failed", error=f"watcher lost the agent: {e}")
                    return
                if turn is not None:
                    self._record_usage(cur, adapter, turn, label="watched")
                    self.store.append_timeline(task_id, "watched_turn_done", reason=turn.reason, ok=turn.ok)
                    if turn.reason == "timeout":
                        continue
                    self._post_turn(cur, adapter, session_info, turn, lock, cancel_event)
                    return
                if hasattr(adapter, "rpc") and adapter.rpc is not None and not adapter.rpc.alive():
                    self._finish(task_id, "failed", error="agent process died during blocked turn")
                    return
        except Exception as e:
            self._finish(task_id, "failed", error=f"watcher error: {type(e).__name__}: {e}")
        finally:
            self._cleanup(task_id, None, lock)

    def _post_turn(self, task: dict, adapter: Adapter, session_info: dict,
                   turn: TerminalTurn, lock: WorkspaceLock, cancel_event: threading.Event):
        task_id = task["task_id"]
        cur = self.store.get(task_id) or task
        if cur.get("status") == "interrupted":
            return
        if not turn.ok:
            if turn.reason == "cancelled" or cur.get("status") == "cancel_requested":
                # a late completion must not resurrect a cancelled turn
                self._finish(task_id, "cancelled", error="turn cancelled", turn=turn)
            else:
                self._finish(task_id, "failed",
                             error=f"worker turn failed: {turn.reason}: {turn.text_tail[:500]}", turn=turn)
            return
        if cur.get("status") == "cancel_requested":
            self._finish(task_id, "cancelled", error="turn ended after cancel request; treat output as cancelled",
                         turn=turn)
            return
        diff = self._capture_diff(cur)
        verify = self._run_verify(cur) if cur.get("verify") else []
        if verify and not all(r["ok"] for r in verify):
            if cur.get("repair_allowed") and (cur.get("repair_used") or 0) < 1:
                self.store.update(task_id, repair_used=1)
                self.store.append_timeline(task_id, "repair_start")
                bad = [r for r in verify if not r["ok"]]
                note = ("acceptance verification failed. Excerpts: " +
                        " | ".join((r["tail"] or r["cmd"])[-800:] for r in bad) +
                        ". Fix only these problems; do not change files outside the allowed list; "
                        "do not modify tests or acceptance criteria.")
                turn2 = adapter.prompt(session_info["native_session_id"],
                                       self._compose_prompt(cur, repair_note=note),
                                       cur["timeout_sec"], cancel_event,
                                       input_id=f"{task_id}:repair")
                self._record_usage(cur, adapter, turn2, label="repair")
                self.store.append_timeline(task_id, "repair_done", reason=turn2.reason, ok=turn2.ok)
                if turn2.reason == "timeout":
                    self._handoff_watch(cur, adapter, session_info, lock, cancel_event)
                    return
                diff = self._capture_diff(cur)
                verify = self._run_verify(cur)
        ok = all(r["ok"] for r in verify)
        self._finish(task_id, "succeeded" if ok else "failed",
                     error=None if ok else "verification_failed after allowed repair",
                     turn=turn, diff=diff, verify=verify, blocked=False)

    def _record_usage(self, task: dict, adapter: Adapter, turn: TerminalTurn, label: str = "turn"):
        if not turn.usage:
            if adapter.usage_quality == "unknown" and not (task.get("usage") or {}).get("sources"):
                self.store.update(task["task_id"], usage={
                    "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                    "quality": "unknown", "sources": ["no-usage-reported"]})
            return
        u = turn.usage
        self.store.append_usage(
            task["task_id"], source=f"{task['agent_id']}:{label}:{u.get('source') or 'turn'}",
            input_tokens=u.get("input_tokens") or 0, output_tokens=u.get("output_tokens") or 0,
            total_tokens=u.get("total_tokens") or 0, quality=adapter.usage_quality)
        budget = task.get("budget_tokens")
        if budget:
            cur = (self.store.get(task["task_id"]) or {}).get("usage") or {}
            if (cur.get("total_tokens") or 0) > int(budget):
                raise KernelError(f"budget exceeded: {cur.get('total_tokens')} > {budget} tokens")

    def _finish(self, task_id: str, status: str, *, error: str | None = None,
                turn: TerminalTurn | None = None, diff: dict | None = None,
                verify: list | None = None, blocked: bool = False):
        task = self.store.get(task_id) or {}
        d = self.store.log_dir(task_id)
        events_log = d / "events.jsonl"
        worker_summary = (turn.text_tail[-PROMPT_TAIL_CHARS:] if turn else "") or ""
        usage = task.get("usage") or {}
        score = compute_score(
            status=status, verify=verify or [], repair_used=task.get("repair_used") or 0,
            out_of_scope=(diff or {}).get("out_of_scope") or [],
            diff_git=bool((diff or {}).get("git")),
            cancel_unconfirmed=(task.get("status") == "cancel_requested" and status != "cancelled"),
            blocked=blocked or status == "blocked",
            usage_quality=usage.get("quality", "unknown"))
        duration = time.time() - float(task.get("created_ts") or time.time())
        self.stats.record(task.get("agent_id") or "?", task_id, status, score,
                          usage.get("total_tokens", 0), duration)
        error_code = classify_error(error or "", status)
        result = {
            "worker_summary": worker_summary,
            "worker_summary_truncated": bool(turn and len(turn.text_tail) > PROMPT_TAIL_CHARS),
            "tools_seen": (turn.tools if turn else [])[-15:],
            "diff": diff,
            "verify": verify or [],
            "usage": usage,
            "score": score,
            "error_code": error_code,
            "duration_sec": round(duration, 1),
            "logs": {"dir": str(d), "events": str(events_log),
                     "read_more": "inspect(task_id, what='log'|'diff'|'verify'|'raw'|'diagnose', offset=N)"},
        }
        (d / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        self.store.update(task_id, result=result, error=error)
        self._set_status(task_id, status)

    def _current_facts(self, task: dict) -> dict:
        ws = task["workspace"]
        facts = {"status_was": task["status"], "native_session_id": task.get("native_session_id")}
        base = task.get("baseline") or {}
        if base.get("git"):
            _, status = self._git(ws, "status", "--porcelain")
            facts["uncommitted_changes_now"] = [ln[3:].strip() for ln in status.splitlines() if len(ln) > 3][:50]
            if task.get("verify"):
                facts["verify_now"] = self._run_verify(task)
        else:
            facts["note"] = "not a git repo; cannot establish change facts reliably"
        return facts
