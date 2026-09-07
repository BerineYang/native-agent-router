"""Persistent state: task store, id/idempotency, workspace mutex, recovery.

All state lives under <home>/:
  tasks/<task_id>.json      task record (atomic writes)
  logs/<task_id>/           events.jsonl, native-raw.jsonl, verify-*.log, result.json
  locks/<hash>.lock         workspace mutex (pid-based staleness)
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

from ..config import home_dir


def atomic_write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        k = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if k.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def new_task_id() -> str:
    return "t-" + uuid.uuid4().hex[:12]


def workspace_lock_path(workspace: str, home: Path | None = None) -> Path:
    home = home or home_dir()
    h = hashlib.sha1(os.path.abspath(workspace).lower().encode("utf-8")).hexdigest()[:20]
    return home / "locks" / (h + ".lock")


class WorkspaceBusy(Exception):
    def __init__(self, holder: dict):
        super().__init__(
            f"workspace is locked by task {holder.get('task_id')} (pid {holder.get('pid')}); "
            "wait or cancel that task first"
        )
        self.holder = holder


class WorkspaceLock:
    """Exclusive per-workspace lock. Stolen only when the holder pid is dead."""

    def __init__(self, workspace: str, task_id: str, home: Path | None = None):
        self.workspace = os.path.abspath(workspace)
        self.task_id = task_id
        self.path = workspace_lock_path(workspace, home)
        self._held = False

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"pid": os.getpid(), "task_id": self.task_id, "ts": time.time(), "workspace": self.workspace}
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                self._held = True
                return
            except FileExistsError:
                holder = read_json(self.path)
                if holder and pid_alive(int(holder.get("pid") or 0)) and time.time() - float(holder.get("ts") or 0) < 86400:
                    raise WorkspaceBusy(holder)
                try:
                    self.path.unlink()
                except OSError:
                    pass

    def release(self):
        if not self._held:
            return
        self._held = False
        try:
            cur = read_json(self.path)
            if cur and cur.get("task_id") == self.task_id:
                self.path.unlink()
        except OSError:
            pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class TaskStore:
    def __init__(self, home: Path | None = None):
        self.home = home or home_dir()
        self.tasks_dir = self.home / "tasks"
        self.logs_dir = self.home / "logs"
        self._lock = threading.Lock()

    def task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def log_dir(self, task_id: str) -> Path:
        return self.logs_dir / task_id

    def create(self, spec: dict) -> tuple[dict, bool]:
        """Returns (task, duplicate). Idempotent on idempotency_key."""
        key = spec.get("idempotency_key")
        with self._lock:
            if key:
                for existing in self.list_tasks():
                    if existing.get("idempotency_key") == key and existing.get("agent_id") == spec.get("agent_id"):
                        return existing, True
            task_id = new_task_id()
            now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            task = {
                "task_id": task_id,
                "run_id": task_id + "-r1",
                "status": "queued",
                "created_at": now,
                "created_ts": time.time(),
                "updated_at": now,
                "pid": os.getpid(),
                "agent_id": spec["agent_id"],
                "workspace": os.path.abspath(spec["workspace"]),
                "goal": spec["goal"],
                "scope_files": spec.get("scope_files") or [],
                "forbid": spec.get("forbid") or [],
                "verify": spec.get("verify") or [],
                "mode": spec.get("mode"),
                "permission_policy": spec.get("permission_policy"),
                "session_ref": spec.get("session_ref"),
                "native_session_id": None,
                "timeout_sec": spec.get("timeout_sec"),
                "budget_tokens": spec.get("budget_tokens"),
                "repair_allowed": spec.get("repair_allowed"),
                "idempotency_key": key,
                "baseline": None,
                "repair_used": 0,
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "quality": "unknown", "sources": []},
                "result": None,
                "error": None,
                "timeline": [],
            }
            atomic_write_json(self.task_path(task_id), task)
            self.log_dir(task_id).mkdir(parents=True, exist_ok=True)
            return task, False

    def get(self, task_id: str) -> dict | None:
        return read_json(self.task_path(task_id))

    def update(self, task_id: str, **fields):
        with self._lock:
            task = self.get(task_id)
            if task is None:
                raise KeyError(task_id)
            task.update(fields)
            task["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            atomic_write_json(self.task_path(task_id), task)

    def append_timeline(self, task_id: str, event: str, **detail):
        with self._lock:
            task = self.get(task_id)
            if task is None:
                return
            entry = {"ts": time.strftime("%H:%M:%S"), "event": event}
            if detail:
                entry.update(detail)
            tl = task.get("timeline") or []
            tl.append(entry)
            task["timeline"] = tl[-80:]
            task["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            atomic_write_json(self.task_path(task_id), task)

    def append_usage(self, task_id: str, source: str, input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0, quality: str = "real"):
        with self._lock:
            task = self.get(task_id)
            if task is None:
                return
            u = task.get("usage") or {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "quality": "unknown", "sources": []}
            if source not in u["sources"]:
                u["sources"].append(source)
                u["input_tokens"] += int(input_tokens or 0)
                u["output_tokens"] += int(output_tokens or 0)
                u["total_tokens"] += int(total_tokens or 0)
                if u["quality"] == "unknown":
                    u["quality"] = quality
                elif u["quality"] != quality:
                    u["quality"] = "partial"
            atomic_write_json(self.task_path(task_id), task)

    def list_tasks(self) -> list[dict]:
        out = []
        if self.tasks_dir.is_dir():
            for p in sorted(self.tasks_dir.glob("t-*.json")):
                t = read_json(p)
                if t:
                    out.append(t)
        return out

    def mark_orphans_unknown(self) -> list[str]:
        """At startup: running tasks whose owner process is dead become 'unknown'."""
        marked = []
        for t in self.list_tasks():
            if t["status"] in ("queued", "running") and not pid_alive(int(t.get("pid") or 0)):
                self.update(t["task_id"], status="unknown", error="owner process died before completion; call inspect to reconcile")
                self.append_timeline(t["task_id"], "orphaned", detail="owner process no longer alive")
                marked.append(t["task_id"])
        return marked
