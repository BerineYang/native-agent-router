"""MCP tool-layer test: tools now return dicts and RAISE on genuine failure
(FastMCP turns a raise into isError=True), which is the whole point of the
error-semantics fix."""

import json
from pathlib import Path

import pytest

from conftest import STUBS


def setup_config(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / "agents.json").write_text(json.dumps({
        "version": 1,
        "agents": {"w1": {"adapter": "stub", "scenario": [{"kind": "turn", "text": "done via mcp",
                                                           "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}]}},
    }), encoding="utf-8")


def test_mcp_tools_end_to_end(home, plain_ws):
    setup_config(home)
    import native_agent_router.mcp_server as m

    m._kernel = None
    ags = m.agents()
    assert isinstance(ags, list) and any(a["agent_id"] == "w1" for a in ags)

    res = m.run(agent_id="w1", goal="mcp flow", workspace=str(plain_ws),
                verify=["python -c \"print(1)\""], wait_sec=30)
    assert isinstance(res, dict)
    assert res["ok"] is True and res["status"] == "succeeded" and res["code"] == "ok"
    task_id = res["task_id"]
    assert res["usage"]["total_tokens"] == 15

    status = m.inspect(task_id=task_id, what="status")
    assert status["status"] == "succeeded"
    summary = m.inspect(task_id=task_id, what="summary")
    assert "done via mcp" in summary["result"]["worker_summary"]
    usage = m.inspect(task_id=task_id, what="usage")
    assert usage["usage"]["quality"] == "real"

    cancel = m.cancel(task_id=task_id)
    assert cancel["requested"] is False  # already finished; honest structured result

    # genuine failure must RAISE (-> MCP isError), not hide in a payload
    with pytest.raises(Exception):
        m.run(agent_id="ghost", goal="g", workspace=str(plain_ws))
    with pytest.raises(Exception):
        m.inspect(task_id="t-doesnotexist", what="status")


def test_mcp_wait_and_inspect_paging(home, plain_ws):
    setup_config(home)
    import native_agent_router.mcp_server as m

    m._kernel = None
    res = m.run(agent_id="w1", goal="paging", workspace=str(plain_ws),
                verify=["python -c \"print('y'*50000)\""], wait_sec=30)
    page = m.inspect(task_id=res["task_id"], what="verify", offset=0, limit=500)
    assert page["truncated"] is True
    w = m.wait(task_id=res["task_id"], timeout_sec=5)
    assert w["status"] == "succeeded"
