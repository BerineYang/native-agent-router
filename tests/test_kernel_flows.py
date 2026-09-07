import json
import subprocess
import sys
import time

import pytest

from conftest import make_kernel, stub_agent, wait_terminal


def test_success_verify_pass_usage_recorded_once(home, plain_ws):
    k = make_kernel(home, {"w1": stub_agent([
        {"kind": "turn", "text": "implemented module", "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}},
    ])})
    snap = k.submit("w1", "do the thing", str(plain_ws),
                    verify=[["python", "-c", "print('ok')"]], wait_sec=30)
    assert snap["status"] == "succeeded", snap
    assert snap["usage"]["total_tokens"] == 150
    assert snap["usage"]["quality"] == "real"
    assert snap["result"]["verify"][0]["ok"] is True
    assert "implemented module" in snap["result"]["worker_summary"]


def test_verify_fail_then_repair_succeeds(home, plain_ws):
    # first turn writes nothing -> verify fails; repair turn writes FIXED -> verify passes
    k = make_kernel(home, {"w1": stub_agent([
        {"kind": "turn", "text": "attempt 1", "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}},
        {"kind": "turn", "text": "fixed", "write_file": "FIXED",
         "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}},
    ])})
    checker = "import os,sys;sys.exit(0 if os.path.exists('FIXED') else 1)"
    snap = k.submit("w1", "make it pass", str(plain_ws), verify=["python -c \"%s\"" % checker], wait_sec=30)
    assert snap["status"] == "succeeded", snap
    assert snap.get("repair_used") == 1 or (k.store.get(snap["task_id"]) or {}).get("repair_used") == 1
    assert snap["usage"]["total_tokens"] == 45  # both turns counted exactly once


def test_verify_fails_after_repair(home, plain_ws):
    k = make_kernel(home, {"w1": stub_agent([
        {"kind": "turn", "text": "a"}, {"kind": "turn", "text": "b"},
    ])})
    snap = k.submit("w1", "doomed", str(plain_ws), verify=["python -c \"import sys;sys.exit(3)\""], wait_sec=30)
    assert snap["status"] == "failed"
    assert "verification_failed" in (snap.get("error") or "")


def test_repair_disabled(home, plain_ws):
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn", "text": "a"}])})
    snap = k.submit("w1", "doomed", str(plain_ws), verify=["python -c \"import sys;sys.exit(3)\""],
                    repair_allowed=False, wait_sec=30)
    assert snap["status"] == "failed"
    assert (k.store.get(snap["task_id"]) or {}).get("repair_used") == 0


def test_duplicate_submit_idempotent(home, plain_ws):
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn"}])})
    s1 = k.submit("w1", "g", str(plain_ws), idempotency_key="op-42", wait_sec=30)
    s2 = k.submit("w1", "g", str(plain_ws), idempotency_key="op-42")
    assert s2.get("duplicate") is True
    assert s1["task_id"] == s2["task_id"]


def test_workspace_mutex(home):
    ws = home.parent / "mutex-ws"
    ws.mkdir(exist_ok=True)
    k = make_kernel(home, {"w1": stub_agent([{"kind": "hold", "seconds": 3, "cancel_confirmed": True}])})
    s1 = k.submit("w1", "holder", str(ws))
    time.sleep(0.8)  # let the first task take the lock
    s2 = k.submit("w1", "intruder", str(ws), wait_sec=15)
    assert s2["status"] == "failed"
    assert "workspace_busy" in (s2.get("error") or "")
    assert "t-" in s2.get("error", "")  # holder task id is reported
    k.cancel(s1["task_id"])
    wait_terminal(k, s1["task_id"])


def test_session_cross_agent_denied(home, plain_ws):
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn"}]), "w2": stub_agent([{"kind": "turn"}])})
    t1 = k.submit("w1", "first", str(plain_ws), wait_sec=30)
    assert t1["native_session_id"]
    with pytest.raises(Exception, match="cross-use denied"):
        k.submit("w2", "second", str(plain_ws), session_ref=t1["task_id"])


def test_session_cross_workspace_denied(home, plain_ws, tmp_path):
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn"}])})
    t1 = k.submit("w1", "first", str(plain_ws), wait_sec=30)
    other = tmp_path / "other-ws"
    other.mkdir()
    with pytest.raises(Exception, match="cross-use denied"):
        k.submit("w1", "second", str(other), session_ref=t1["task_id"])


def test_session_resume_same_agent_workspace(home, plain_ws):
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn", "text": "one"}, {"kind": "turn", "text": "two"}])})
    t1 = k.submit("w1", "first", str(plain_ws), wait_sec=30)
    t2 = k.submit("w1", "second", str(plain_ws), session_ref=t1["task_id"], wait_sec=30)
    assert t2["status"] == "succeeded"
    assert t2["native_session_id"] == t1["native_session_id"]
    assert "resumed" in json.dumps(t2.get("timeline"))


def test_cancel_unconfirmed(home):
    ws = home.parent / "cancel-ws"
    ws.mkdir(exist_ok=True)
    k = make_kernel(home, {"w1": stub_agent([{"kind": "hold", "seconds": 5, "cancel_confirmed": False}])})
    t = k.submit("w1", "long", str(ws))
    time.sleep(0.8)
    res = k.cancel(t["task_id"])
    assert res["requested"] is True
    assert res["confirmed"] is False  # runtime did not confirm the stop
    final = wait_terminal(k, t["task_id"])
    # cancel was NOT confirmed by the runtime; a late completion after a cancel
    # request is reported as cancelled (never silently counted as success)
    assert final["status"] == "cancelled"


def test_cancel_confirmed(home):
    ws = home.parent / "cancel2-ws"
    ws.mkdir(exist_ok=True)
    k = make_kernel(home, {"w1": stub_agent([{"kind": "hold", "seconds": 30, "cancel_confirmed": True}])})
    t = k.submit("w1", "long", str(ws))
    time.sleep(0.8)
    res = k.cancel(t["task_id"])
    assert res["requested"] is True
    assert res["confirmed"] is True
    final = wait_terminal(k, t["task_id"])
    assert final["status"] == "cancelled"


def test_cancel_finished_task(home, plain_ws):
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn"}])})
    t = k.submit("w1", "quick", str(plain_ws), wait_sec=30)
    res = k.cancel(t["task_id"])
    assert res["requested"] is False and res["confirmed"] is True


def test_unknown_reconcile(home, plain_ws):
    from native_agent_router.kernel.store import TaskStore, atomic_write_json
    store = TaskStore(home)
    t, _ = store.create({"agent_id": "w1", "goal": "g", "workspace": str(plain_ws)})
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    store.update(t["task_id"], status="running", pid=p.pid)
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn"}])})  # init marks orphans
    snap = k.inspect(t["task_id"], "status")
    assert snap["status"] == "unknown"
    rec = k.reconcile(t["task_id"])
    assert rec["status"] == "interrupted"
    assert "facts" in rec
    # resubmission must NOT silently reuse the unknown task
    with pytest.raises(Exception, match="cross-use denied|not found|no native session"):
        k.submit("w1", "again", str(plain_ws), session_ref=t["task_id"])


def test_worker_crash_reports_failure(home, plain_ws):
    k = make_kernel(home, {"w1": stub_agent([{"kind": "crash"}])})
    snap = k.submit("w1", "g", str(plain_ws), wait_sec=30)
    assert snap["status"] == "failed"
    assert "crashed" in (snap.get("error") or "")


def test_budget_exceeded(home, plain_ws):
    k = make_kernel(home, {"w1": stub_agent([
        {"kind": "turn", "usage": {"input_tokens": 900, "output_tokens": 100, "total_tokens": 1000}}])})
    snap = k.submit("w1", "g", str(plain_ws), budget_tokens=500, wait_sec=30)
    assert snap["status"] == "failed"
    assert "budget" in (snap.get("error") or "")


def test_turn_timeout_blocked_not_resubmitted(home):
    ws = home.parent / "to-ws"
    ws.mkdir(exist_ok=True)
    k = make_kernel(home, {"w1": stub_agent([{"kind": "timeout", "seconds": 4}])})
    snap = k.submit("w1", "g", str(ws), timeout_sec=1, wait_sec=30)
    assert snap["status"] == "blocked"
    assert "timeout" in (snap.get("error") or "")
    assert "resubmit" in (snap.get("error") or "")


def test_long_verify_log_truncated_and_paged(home, plain_ws):
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn"}])})
    big = "python -c \"print('x'*100000)\""
    snap = k.submit("w1", "g", str(plain_ws), verify=[big], wait_sec=60)
    assert snap["status"] == "succeeded"
    page1 = k.inspect(snap["task_id"], "verify", offset=0, limit=1000)
    assert page1["truncated"] is True
    assert page1["total_chars"] > 90000
    assert len(page1["content"]) <= 1100
    page2 = k.inspect(snap["task_id"], "verify", offset=page1["total_chars"] - 500, limit=500)
    assert page2["content"]  # paging reaches the tail


def test_scope_audit_flags_out_of_scope(home, git_ws):
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn", "write_file": "sneaky.txt"}])})
    snap = k.submit("w1", "g", str(git_ws), scope_files=["app.py"], wait_sec=30)
    assert snap["status"] == "succeeded"  # worker finished, audit still flags
    diff = snap["result"]["diff"]
    assert diff["git"] is True
    assert "sneaky.txt" in diff["changed_files"]
    assert diff["out_of_scope"] == ["sneaky.txt"]


def test_no_git_workspace_reports_unverifiable_isolation(home, plain_ws):
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn"}])})
    snap = k.submit("w1", "g", str(plain_ws), wait_sec=30)
    assert snap["result"]["diff"]["git"] is False
    assert "cannot be verified" in snap["result"]["diff"]["note"]
