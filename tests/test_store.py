import json
import os

from native_agent_router.kernel.store import (
    TaskStore,
    WorkspaceBusy,
    WorkspaceLock,
    atomic_write_json,
    pid_alive,
)


def test_idempotent_create(home):
    store = TaskStore(home)
    spec = {"agent_id": "w", "goal": "g", "workspace": ".", "idempotency_key": "K1"}
    t1, dup1 = store.create(spec)
    t2, dup2 = store.create(spec)
    assert not dup1 and dup2
    assert t1["task_id"] == t2["task_id"]


def test_idempotency_scoped_by_agent(home):
    store = TaskStore(home)
    spec = {"agent_id": "w", "goal": "g", "workspace": ".", "idempotency_key": "K"}
    t1, _ = store.create(spec)
    t2, dup = store.create({**spec, "agent_id": "other"})
    assert not dup and t1["task_id"] != t2["task_id"]


def test_atomic_update(home):
    store = TaskStore(home)
    t, _ = store.create({"agent_id": "w", "goal": "g", "workspace": "."})
    store.update(t["task_id"], status="running")
    assert store.get(t["task_id"])["status"] == "running"


def test_orphan_marking(home):
    store = TaskStore(home)
    t, _ = store.create({"agent_id": "w", "goal": "g", "workspace": "."})
    store.update(t["task_id"], status="running", pid=0)  # pid 0 is never a valid owner
    marked = store.mark_orphans_unknown()
    assert t["task_id"] in marked
    assert store.get(t["task_id"])["status"] == "unknown"


def test_pid_alive_guards():
    assert not pid_alive(0)
    assert not pid_alive(-5)


def test_lock_exclusive(home):
    ws = os.getcwd()
    l1 = WorkspaceLock(ws, "t-1", home)
    l1.acquire()
    l2 = WorkspaceLock(ws, "t-2", home)
    with __import__("pytest").raises(WorkspaceBusy) as e:
        l2.acquire()
    assert e.value.holder["task_id"] == "t-1"
    l1.release()
    l2.acquire()
    l2.release()


def test_lock_steals_from_dead_owner(home):
    ws = os.getcwd()
    lock_path = home / "locks" / "stale.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(lock_path, {"pid": 0, "task_id": "t-dead", "ts": 0, "workspace": ws})
    l = WorkspaceLock(ws, "t-2", home)
    l.acquire()
    l.release()
