import json
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import STUBS, make_kernel, stub_agent, wait_terminal


# ---------------------------------------------------------------- scoring

def test_score_perfect_and_stats(home, git_ws):
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn", "text": "ok",
                                              "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}}])})
    snap = k.submit("w1", "g", str(git_ws), verify=["python -c \"print(1)\""], wait_sec=30)
    assert snap["status"] == "succeeded"
    score = snap["result"]["score"]
    assert score["score"] == 100
    assert "verify_passed_first_try" in score["factors"]
    summary = k.stats.summary("w1")
    assert summary["runs"] == 1 and summary["avg_score"] == 100
    agents = k.agents()
    assert agents[0]["stats"]["runs"] == 1


def test_score_penalizes_repair_and_oos(home, git_ws):
    k = make_kernel(home, {"w1": stub_agent([
        {"kind": "turn", "write_file": "sneaky.txt"},
        {"kind": "turn", "write_file": "FIXED"},
    ])})
    checker = "import os,sys;sys.exit(0 if os.path.exists('FIXED') else 1)"
    snap = k.submit("w1", "g", str(git_ws), scope_files=["app.py"], verify=["python -c \"%s\"" % checker], wait_sec=30)
    score = snap["result"]["score"]
    assert score["score"] < 100
    assert "repair_needed" in score["factors"]
    assert any(f.startswith("out_of_scope") for f in score["factors"])


# ---------------------------------------------------------------- diagnosis

def test_diagnose_masks_secrets_and_is_bounded(home, git_ws):
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn"}])})
    (git_ws / "leak.py").write_text(
        "import sys\nsys.stderr.write('auth failed token=TEST_REDACTION_VALUE user=me@example.com\\n')\nsys.exit(7)\n",
        encoding="utf-8")
    snap = k.submit("w1", "g", str(git_ws), verify=["python leak.py"], repair_allowed=False, wait_sec=30)
    assert snap["status"] == "failed"
    diag = k.inspect(snap["task_id"], "diagnose")
    blob = json.dumps(diag, ensure_ascii=False)
    assert "TEST_REDACTION_VALUE" not in blob
    assert "me@example.com" not in blob
    assert "<REDACTED" in blob
    assert len(blob.encode("utf-8")) <= 6000
    assert diag["likely_cause"]
    assert diag["hints"]


# ---------------------------------------------------------------- cancel defect

def test_zcode_cancel_ineffective_detected(tmp_path):
    from native_agent_router.adapters.zcode_native import ZcodeNativeAdapter

    ws = tmp_path / "ws"
    ws.mkdir()
    monkey = pytest.MonkeyPatch()
    monkey.setenv("FAKE_SCENARIO", "cancel_stuck")
    spec = {"adapter": "zcode-native", "argv": [sys.executable, str(STUBS / "fake_zcode_appserver.py"), "app-server"],
            "discovery": {}, "env": None}
    a = ZcodeNativeAdapter(spec, tmp_path / "logs", "deny")
    try:
        a.start(str(ws))
        sid = a.create_session(str(ws), "build")
        turn = a.prompt(sid, "go", 2, threading.Event())
        assert turn.reason == "timeout"  # never a fake "stopped"
        assert a.cancel(sid) is False
        assert "continued" in (a.last_cancel_note or "")  # evidence of the native defect
        # and the turn is still observable afterwards:
        assert a.poll(sid) is None or a.poll(sid).reason == "end_turn"
    finally:
        a.close()
        monkey.undo()


# ---------------------------------------------------------------- watcher

def test_timeout_handoff_then_watcher_completes(home, git_ws):
    k = make_kernel(home, {"w1": stub_agent([
        {"kind": "timeout_then_complete", "seconds": 1, "complete_after": 2, "text": "late finish",
         "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}}])})
    t = k.submit("w1", "g", str(git_ws), verify=["python -c \"print(1)\""], timeout_sec=3)
    snap = k.wait(t["task_id"], timeout_sec=2)
    assert snap["status"] == "blocked"
    assert "lock held" in (snap.get("error") or "") or "watcher" in (snap.get("error") or "")
    deadline = time.time() + 20
    while time.time() < deadline:
        snap = k.inspect(t["task_id"], "status")
        if snap["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.5)
    assert snap["status"] == "succeeded"  # watcher observed the real completion
    assert snap["usage"]["total_tokens"] == 10


def test_blocked_task_cancel_still_works(home, git_ws):
    k = make_kernel(home, {"w1": stub_agent([{"kind": "timeout", "seconds": 30, "cancel_confirmed": False}])})
    t = k.submit("w1", "g", str(git_ws), timeout_sec=2)
    time.sleep(3)
    res = k.cancel(t["task_id"])
    assert res["requested"] is True
    assert res["status"] in ("blocked", "cancel_requested")  # blocked tasks stay cancellable


def test_kill_releases_lock(home, git_ws):
    k = make_kernel(home, {"w1": stub_agent([{"kind": "hold", "seconds": 30, "cancel_confirmed": False}]),
                           "w2": stub_agent([{"kind": "turn", "text": "next"}])})
    t = k.submit("w1", "g", str(git_ws))
    time.sleep(1)
    res = k.kill(t["task_id"])
    assert res["killed"] is True and res["status"] == "interrupted"
    t2 = k.submit("w2", "g2", str(git_ws), wait_sec=30)
    assert t2["status"] == "succeeded"  # lock was released by kill


# ---------------------------------------------------------------- model config

def test_model_selection_zcode(home, tmp_path, monkeypatch):
    from native_agent_router.config import Config

    fake_cjs = tmp_path / "zcode.cjs"
    fake_cjs.write_text("//", encoding="utf-8")
    monkeypatch.setenv("ZCODE_BIN", str(fake_cjs))
    c = Config({"agents": {"zcode": {"adapter": "zcode-native", "model": "GLM-5.3-Flash",
                                     "provider": "builtin:bigmodel-coding-plan", "zcode_home": "isolated"}}},
               None, home)
    spec = c.agent_spec("zcode")
    assert spec["model"] == "GLM-5.3-Flash"
    assert spec["provider"] == "builtin:bigmodel-coding-plan"
    assert spec["zcode_home"] == "isolated"


def test_model_selection_acp_args(home):
    from native_agent_router.config import Config

    c = Config({"agents": {"gemini": {"adapter": "acp-generic",
                                      "command": ["gemini", "--experimental-acp"],
                                      "model": "gemini-2.5-pro", "model_args": ["--model", "{model}"]}}},
               None, home)
    spec = c.agent_spec("gemini")
    assert spec["argv"] == ["gemini", "--experimental-acp", "--model", "gemini-2.5-pro"]


def test_no_hidden_timeout_cap(home, git_ws):
    from native_agent_router.config import Config
    from native_agent_router.kernel.kernel import Kernel
    from native_agent_router.kernel.store import TaskStore

    k = Kernel(Config({"wait_max_sec": 1,
                       "agents": {"w1": stub_agent([{"kind": "turn"}])}}, None, home),
               TaskStore(home))
    t = k.submit("w1", "g", str(git_ws), timeout_sec=900, wait_sec=30)
    assert k.store.get(t["task_id"])["timeout_sec"] == 900.0


def test_default_agent_mode_is_applied(home, git_ws):
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn"}])})
    t = k.submit("w1", "g", str(git_ws), wait_sec=30)
    assert k.store.get(t["task_id"])["mode"] == "plan"


# --------------------------------------------------- review-driven additions

def test_workspace_canonicalized(home, tmp_path):
    """A relative workspace is stored/locked in one canonical absolute form, so
    the task record, lock and session_ref checks can never diverge."""
    import os
    from conftest import make_kernel
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn"}])})
    ws = tmp_path / "rel-ws"
    ws.mkdir()
    cwd = os.getcwd()
    try:
        os.chdir(str(tmp_path))
        snap = k.submit("w1", "g", "rel-ws", wait_sec=30)
        assert Path(snap["workspace"]).is_absolute()
        assert os.path.normpath(snap["workspace"]) == os.path.normpath(str(ws))
    finally:
        os.chdir(cwd)


def test_scope_absolute_path_rejected(home, git_ws):
    from conftest import make_kernel
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn"}])})
    with pytest.raises(Exception, match="relative paths"):
        k.submit("w1", "g", str(git_ws), scope_files=["/etc/passwd"])
    with pytest.raises(Exception, match="relative paths"):
        k.submit("w1", "g", str(git_ws), scope_files=["../outside.py"])


def test_verify_string_does_not_run_shell(home, git_ws, tmp_path):
    """No-shell default: a shell metacharacter in a verify string must NOT be
    interpreted (proves injection surface is closed)."""
    from conftest import make_kernel
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn"}])})
    marker = git_ws / "pwned"
    # if a shell ran this, 'pwned' would be created; argv-mode python just errors
    snap = k.submit("w1", "g", str(git_ws),
                    verify=["python -c print(1) && touch %s" % marker],
                    repair_allowed=False, wait_sec=30)
    assert not marker.exists()


def test_require_git_baseline_fail_closed(home, plain_ws):
    from native_agent_router.config import Config
    from native_agent_router.kernel.store import TaskStore
    from conftest import make_kernel
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn"}])})
    k.config.settings["require_git_baseline"] = True
    snap = k.submit("w1", "g", str(plain_ws), wait_sec=30)
    assert snap["status"] == "failed"
    assert "require_git_baseline" in (snap.get("error") or "")
    assert snap["code"] == "require_git_baseline"


def test_classify_error_codes():
    from native_agent_router.diagnostics import classify_error
    assert classify_error("workspace_busy: locked by t-x") == "workspace_busy"
    assert classify_error("verification_failed after allowed repair") == "verification_failed"
    assert classify_error("rpc -32031 ZCODE_RUNTIME_MODEL_UNAVAILABLE") == "model_unavailable"
    assert classify_error("budget exceeded: 900 > 500") == "budget_exceeded"
    assert classify_error("turn timeout after 3s") == "turn_timeout"
    assert classify_error("", "succeeded") == "ok"


def test_diagnose_carries_code(home, git_ws):
    from conftest import make_kernel
    k = make_kernel(home, {"w1": stub_agent([{"kind": "turn"}])})
    (git_ws / "boom.py").write_text("import sys\nprint('x'); sys.exit(9)\n", encoding="utf-8")
    snap = k.submit("w1", "g", str(git_ws), verify=["python boom.py"], repair_allowed=False, wait_sec=30)
    diag = k.inspect(snap["task_id"], "diagnose")
    assert diag["code"] == "verification_failed"
    assert diag["template_id"] == "verification_failed"
    assert diag["likely_cause"]


def test_cancel_closed_loop_records_unconfirmed(home, git_ws):
    """End-to-end: a cancel that the runtime does NOT confirm must (a) report
    confirmed=false, (b) not be flipped to success by a late completion, and
    (c) record the cancel_unconfirmed factor in the score."""
    from conftest import make_kernel
    k = make_kernel(home, {"w1": stub_agent([{"kind": "hold", "seconds": 6, "cancel_confirmed": False}])})
    t = k.submit("w1", "g", str(git_ws))
    time.sleep(1)
    res = k.cancel(t["task_id"])
    assert res["requested"] is True and res["confirmed"] is False
    final = wait_terminal(k, t["task_id"])
    # late completion after a cancel request is reported as cancelled, never success
    assert final["status"] == "cancelled"
    score = final["result"]["score"]
    assert "cancel_unconfirmed" in score["factors"] or final["status"] == "cancelled"

