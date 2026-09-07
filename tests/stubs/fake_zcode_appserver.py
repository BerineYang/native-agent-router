"""Fake ZCode app-server (protocol 0.16 shape) for adapter conformance tests.
No model calls. Scripted via FAKE_SCENARIO env: ok | fail | permission | dup_usage.
Recorded interactions are written to FAKE_TRACE (jsonl)."""

import json
import os
import sys
import threading
import time

state = {"context_used": 0, "sends": 0, "permissions": [], "stopped": False}
lock = threading.Lock()
trace_path = os.environ.get("FAKE_TRACE")
scenario = os.environ.get("FAKE_SCENARIO", "ok")
_out_lock = threading.Lock()


def send(obj):
    with _out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def trace(event, **kw):
    if trace_path:
        with open(trace_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": event, **kw}) + "\n")


def push_events(sid):
    with lock:
        state["sends"] += 1
        n = state["sends"]
    if scenario == "cancel_stuck":
        # emulate the native defect: after session/stop the work keeps running;
        # only real terminal comes much later (or never within test window)
        def spam():
            s = 500
            while s < 560:
                s += 1
                send({"method": "session/event", "params": {"sessionId": sid, "seq": s, "type": "tool.updated",
                                                            "payload": {"kind": "progress", "toolCallId": "call_stuck",
                                                                        "toolName": "Bash"}}})
                time.sleep(0.2)
        threading.Thread(target=spam, daemon=True).start()
        return
    seq = 100
    send({"method": "session/event", "params": {"sessionId": sid, "seq": seq, "type": "turn.started", "payload": {"turnId": f"turn_{n}"}}})
    seq += 1
    send({"method": "session/event", "params": {"sessionId": sid, "seq": seq, "type": "model.streaming",
                                                "payload": {"kind": "text_delta", "delta": "I did the work part %d. " % n}}})
    seq += 1
    send({"method": "session/event", "params": {"sessionId": sid, "seq": seq, "type": "model.streaming",
                                                "payload": {"kind": "text_delta", "delta": "Done."}}})
    seq += 1
    send({"method": "session/event", "params": {"sessionId": sid, "seq": seq, "type": "tool.updated",
                                                "payload": {"kind": "started", "toolCallId": "call_1", "toolName": "Read"}}})
    if scenario == "fail" and n == 1:
        seq += 1
        send({"method": "session/event", "params": {"sessionId": sid, "seq": seq, "type": "turn.failed",
                                                    "payload": {"error": {"code": 1302, "message": "rate limited (fake)"}}}})
        return
    # completed AND terminal both carry usage -> client must count once
    seq += 1
    send({"method": "session/event", "params": {"sessionId": sid, "seq": seq, "type": "turn.completed",
                                                "payload": {"resultType": "success", "usage": {"totalTokens": 150}}}})
    seq += 1
    send({"method": "session/event", "params": {"sessionId": sid, "seq": seq, "type": "turn.terminal",
                                                "payload": {"kind": "turn.terminal", "status": "success", "resultType": "end_turn",
                                                            "inputTokens": 100, "outputTokens": 50, "totalTokens": 150}}})


def handle(method, params, mid):
    global state
    if method == "session/create":
        sid = "sess_fake_1"
        send({"id": mid, "result": {"session": {"sessionId": sid, "title": "", "traceId": "trace_1"}}})
    elif method == "session/subscribe":
        send({"id": mid, "result": {"eventSeq": 0, "snapshot": {"projection": {"status": "idle"}}}})
    elif method == "session/send":
        sid = params.get("sessionId")
        with lock:
            state["context_used"] += 500
        send({"id": mid, "result": {"accepted": True}})
        if scenario == "permission" and state["sends"] == 0:
            # exercise interaction/requestPermission before finishing the turn
            perm_id = 9001
            send({"id": perm_id, "method": "interaction/requestPermission",
                  "params": {"requestId": "req_1", "sessionId": sid, "toolCallId": "call_9", "toolName": "Bash",
                             "reason": "run command", "input": {"command": "echo hi"},
                             "options": [{"optionId": "allow", "kind": "allow_once", "name": "Allow once"},
                                         {"optionId": "deny", "kind": "deny_once", "name": "Deny"}]}})
            threading.Thread(target=wait_permission_then_push, args=(perm_id, sid), daemon=True).start()
        else:
            threading.Thread(target=push_events, args=(sid,), daemon=True).start()
    elif method == "session/read":
        send({"id": mid, "result": {"projection": {"status": "idle", "contextUsed": state["context_used"],
                                                   "contextWindow": 200000, "totalTokenCount": state["context_used"]}}})
    elif method == "session/resume":
        send({"id": mid, "result": {"session": {"sessionId": params.get("sessionId")}}})
    elif method == "session/stop":
        with lock:
            state["stopped"] = True
        sid = params.get("sessionId")
        send({"method": "session/event", "params": {"sessionId": sid, "seq": 999, "type": "turn.terminal",
                                                    "payload": {"status": "cancelled", "resultType": "cancelled"}}})
        trace("stop_received")
    else:
        send({"id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})


def wait_permission_then_push(perm_id, sid):
    # The client's answer will arrive as a normal response; we just push events after it.
    import time
    time.sleep(0.2)
    push_events(sid)


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
        elif mid is None and method == "session/stop":
            with lock:
                state["stopped"] = True
            trace("stop_received")
            if scenario != "cancel_stuck":
                sid = (msg.get("params") or {}).get("sessionId")
                send({"method": "session/event", "params": {"sessionId": sid, "seq": 999, "type": "turn.terminal",
                                                            "payload": {"status": "cancelled", "resultType": "cancelled"}}})
        elif mid is not None:
            trace("client_response", id=mid, result=msg.get("result"))
            with lock:
                state["permissions"].append(msg.get("result"))


if __name__ == "__main__":
    main()
