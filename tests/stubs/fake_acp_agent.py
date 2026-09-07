"""Fake ACP agent (JSON-RPC 2.0 over stdio) for adapter conformance tests.
No model calls. FAKE_SCENARIO env: ok | fail | permission. FAKE_TRACE jsonl."""

import json
import os
import sys
import threading

scenario = os.environ.get("FAKE_SCENARIO", "ok")
trace_path = os.environ.get("FAKE_TRACE")
_out_lock = threading.Lock()
state = {"sends": 0, "permissions": [], "cancelled": False}


def send(obj):
    with _out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def trace(event, **kw):
    if trace_path:
        with open(trace_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": event, **kw}) + "\n")


def push_updates(sid):
    n = state["sends"] - 1
    send({"method": "session/update", "params": {"sessionId": sid, "update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "worked part %d. " % n}}}})
    send({"method": "session/update", "params": {"sessionId": sid, "update": {
        "sessionUpdate": "tool_call", "toolCallId": f"call_{n}", "title": "Read file", "status": "in_progress"}}})
    send({"method": "session/update", "params": {"sessionId": sid, "update": {
        "sessionUpdate": "tool_call_update", "toolCallId": f"call_{n}", "status": "completed",
        "_meta": {"tokenUsage": {"input": 120, "output": 30, "total": 150}}}}})


def request_permission(sid):
    send({"id": 7001, "method": "session/request_permission",
          "params": {"sessionId": sid, "toolCall": {"toolCallId": "call_x", "title": "Bash"},
                     "options": [{"optionId": "allow", "kind": "allow_once", "name": "Allow"},
                                 {"optionId": "reject", "kind": "reject_once", "name": "Reject"}]}})


def handle(method, params, mid):
    if method == "initialize":
        send({"id": mid, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True},
                                    "authMethods": []}})
    elif method == "session/new":
        send({"id": mid, "result": {"sessionId": "sess_acp_fake_1"}})
    elif method == "session/load":
        if scenario == "nores":
            send({"id": mid, "error": {"code": -32601, "message": "method not found: session/load"}})
        else:
            send({"id": mid, "result": {}})
    elif method == "session/prompt":
        sid = params.get("sessionId")
        state["sends"] += 1
        if scenario == "permission" and state["sends"] == 1:
            request_permission(sid)
            threading.Thread(target=prompt_after_permission, args=(sid, mid), daemon=True).start()
        else:
            push_updates(sid)
            stop = "refusal" if scenario == "fail" else "end_turn"
            send({"id": mid, "result": {"stopReason": stop}})
    elif method == "session/cancel":
        state["cancelled"] = True
        trace("cancel_received")
    else:
        send({"id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})


def prompt_after_permission(sid, mid):
    import time
    time.sleep(0.3)
    push_updates(sid)
    send({"id": mid, "result": {"stopReason": "end_turn"}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid = msg.get("id")
        method = msg.get("method")
        if mid is not None and method:
            handle(method, msg.get("params") or {}, mid)
        elif mid is not None:
            trace("client_response", id=mid, result=msg.get("result"))
            state["permissions"].append(msg.get("result"))


if __name__ == "__main__":
    main()
