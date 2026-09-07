import json
import sys
import threading

import pytest

from conftest import STUBS

from native_agent_router.adapters.acp_generic import AcpGenericAdapter
from native_agent_router.adapters.base import ResumeUnsupported


def make_adapter(tmp_path, scenario, policy="deny"):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    monkey = pytest.MonkeyPatch()
    monkey.setenv("FAKE_SCENARIO", scenario)
    monkey.setenv("FAKE_TRACE", str(tmp_path / "trace.jsonl"))
    spec = {"adapter": "acp-generic", "argv": [sys.executable, str(STUBS / "fake_acp_agent.py")],
            "cwd_arg": None, "env": None}
    a = AcpGenericAdapter(spec, tmp_path / "logs", policy)
    a.start(str(ws))
    a._monkey = monkey
    return a, ws, tmp_path / "trace.jsonl"


def teardown(a):
    a.close()
    a._monkey.undo()


def test_initialize_handshake(tmp_path):
    a, ws, _ = make_adapter(tmp_path, "ok")
    try:
        caps = a.capabilities()
        assert caps["loadSession"] is True
        assert caps["transport"] == "stdio ACP"
    finally:
        teardown(a)


def test_prompt_streams_updates_and_usage(tmp_path):
    a, ws, _ = make_adapter(tmp_path, "ok")
    try:
        sid = a.create_session(str(ws), None)
        turn = a.prompt(sid, "do it", 30, threading.Event())
        assert turn.ok and turn.reason == "end_turn"
        assert "worked part 0." in turn.text_tail
        assert any("Read file" in t for t in turn.tools)
        assert turn.usage["total_tokens"] == 150
        assert turn.usage["source"] == "_meta.tokenUsage"
    finally:
        teardown(a)


def test_prompt_failure_stop_reason(tmp_path):
    a, ws, _ = make_adapter(tmp_path, "fail")
    try:
        sid = a.create_session(str(ws), None)
        turn = a.prompt(sid, "do it", 30, threading.Event())
        assert not turn.ok
        assert turn.reason == "refusal"
    finally:
        teardown(a)


def test_permission_allow_and_deny(tmp_path):
    for policy, expected in (("allow", {"optionId": "allow"}), ("deny", {"optionId": "reject"})):
        a, ws, trace = make_adapter(tmp_path, "permission", policy)
        try:
            sid = a.create_session(str(ws), None)
            turn = a.prompt(sid, "run it", 30, threading.Event())
            assert turn.ok
            answers = [json.loads(l)["result"] for l in trace.read_text(encoding="utf-8").splitlines()
                       if json.loads(l).get("event") == "client_response"]
            assert expected in answers
        finally:
            teardown(a)


def test_resume_via_session_load(tmp_path):
    a, ws, _ = make_adapter(tmp_path, "ok")
    try:
        sid = a.create_session(str(ws), None)
        a.prompt(sid, "first", 30, threading.Event())
        ev = a.resume_session(sid, str(ws))
        assert ev["resumed"] is True
    finally:
        teardown(a)


def test_resume_unsupported_mapped_clearly(tmp_path):
    a, ws, _ = make_adapter(tmp_path, "nores")
    try:
        sid = a.create_session(str(ws), None)
        with pytest.raises(ResumeUnsupported):
            a.resume_session(sid, str(ws))
    finally:
        teardown(a)


def test_kernel_acp_end_to_end(home, plain_ws):
    from conftest import make_kernel
    import os

    k = make_kernel(home, {"acp1": {"adapter": "acp-generic",
                                    "command": [sys.executable, str(STUBS / "fake_acp_agent.py")]}})
    snap = k.submit("acp1", "work", str(plain_ws), wait_sec=30)
    assert snap["status"] == "succeeded", snap
    assert snap["usage"]["total_tokens"] == 150
    assert snap["usage"]["quality"] == "unknown"  # honest label: ACP usage quality
