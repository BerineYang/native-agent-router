"""Generic ACP (Agent Client Protocol) adapter for any ACP-compatible agent.

Speaks JSON-RPC 2.0 over stdio: initialize -> session/new -> session/prompt,
session/update notifications streamed back, session/request_permission answered
according to the task permission policy. Verified live against
`opencode acp` (opencode 1.18.15).

Agents are added by config only, e.g.:
  {"agents": {"gemini": {"adapter": "acp-generic",
                          "command": ["gemini", "--experimental-acp"]}}}
"""

from __future__ import annotations

import threading
import time

from .base import Adapter, AdapterError, ResumeUnsupported, TerminalTurn
from .jsonrpc import JsonRpcProcess, ProcessDied, RpcError

TEXT_TAIL_MAX = 8000
TOOLS_MAX = 60


class _Turn:
    def __init__(self):
        self.event = threading.Event()
        self.text_chunks: list[str] = []
        self.tools: list[str] = []
        self.usage = None
        self.permission_answers: list[dict] = []


class AcpGenericAdapter(Adapter):
    usage_quality = "unknown"
    session_resume = True

    def __init__(self, spec, log_dir, permission_policy="deny"):
        super().__init__(spec, log_dir, permission_policy)
        self.rpc: JsonRpcProcess | None = None
        self._turns: dict[str, _Turn] = {}
        self._pending_futs: dict[str, object] = {}
        self._lock = threading.Lock()
        self.last_cancel_note: str | None = None
        self.model_set: str | None = None
        self.model_set_error: str | None = None
        self.mode_set: str | None = None
        self.mode_set_error: str | None = None
        self._init_result: dict | None = None

    def capabilities(self) -> dict:
        caps = {}
        if self._init_result:
            agent_caps = self._init_result.get("agentCapabilities") or {}
            caps = {
                "loadSession": bool(agent_caps.get("loadSession")),
                "promptCapabilities": agent_caps.get("promptCapabilities"),
                "authMethods": [m.get("name") or m.get("id") for m in self._init_result.get("authMethods") or []],
            }
        return {"transport": "stdio ACP", "command": self.spec["argv"], "session_resume": bool(caps.get("loadSession")),
                "usage_quality": self.usage_quality, **caps}

    def start(self, workspace: str):
        argv = list(self.spec["argv"])
        cwd_arg = self.spec.get("cwd_arg")
        if cwd_arg:
            argv += [cwd_arg, workspace]
        self.rpc = JsonRpcProcess(argv, cwd=workspace, raw_log=self.log_dir / "native-raw.jsonl",
                                  env=self.spec.get("env") or None)
        self.rpc.on_request = self._on_request
        self.rpc.on_notification = self._on_notification
        try:
            self._init_result = self.rpc.request("initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            }, timeout=60)
        except (RpcError, ProcessDied, TimeoutError) as e:
            raise AdapterError(f"ACP initialize failed: {e}") from e
        pv = (self._init_result or {}).get("protocolVersion")
        if pv not in (1, "1"):
            raise AdapterError(f"unexpected ACP protocolVersion: {pv}")

    def _on_request(self, method: str, params: dict, mid):
        if method == "session/request_permission":
            options = params.get("options") or []
            allow = self.permission_policy == "allow"
            chosen = None
            if allow:
                for o in options:
                    if str(o.get("kind") or "").startswith("allow"):
                        chosen = o.get("optionId")
                        break
                chosen = chosen or (options[0].get("optionId") if options else None)
            else:
                for o in options:
                    if str(o.get("kind") or "").startswith(("deny", "reject")):
                        chosen = o.get("optionId")
                        break
                chosen = chosen or (options[-1].get("optionId") if options else None)
            with self._lock:
                for t in self._turns.values():
                    t.permission_answers.append({"toolCall": params.get("toolCall"), "chosen": chosen, "policy": self.permission_policy})
            return {"optionId": chosen}
        raise LookupError(method)

    def _on_notification(self, method: str, params: dict):
        if method != "session/update":
            return
        sid = params.get("sessionId")
        with self._lock:
            turn = self._turns.get(sid)
        if turn is None:
            return
        upd = params.get("update") or {}
        kind = upd.get("sessionUpdate") or ""
        if kind == "agent_message_chunk":
            chunk = (upd.get("content") or {}).get("text") or ""
            turn.text_chunks.append(chunk)
        elif kind in ("tool_call", "tool_call_update"):
            title = upd.get("title") or upd.get("kind") or kind
            turn.tools.append(f"{upd.get('status') or kind}:{title}")
            if len(turn.tools) > TOOLS_MAX:
                del turn.tools[: len(turn.tools) - TOOLS_MAX]
        meta = upd.get("_meta") or params.get("_meta") or {}
        if isinstance(meta, dict):
            for key in ("tokenUsage", "usage", "tokens"):
                v = meta.get(key)
                if isinstance(v, dict):
                    turn.usage = {
                        "input_tokens": v.get("input") or v.get("inputTokens") or v.get("input_tokens") or 0,
                        "output_tokens": v.get("output") or v.get("outputTokens") or v.get("output_tokens") or 0,
                        "total_tokens": v.get("total") or v.get("totalTokens") or v.get("total_tokens") or 0,
                        "source": f"_meta.{key}",
                    }
                elif isinstance(v, (int, float)):
                    turn.usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": int(v), "source": f"_meta.{key}"}

    def create_session(self, workspace: str, mode: str | None) -> str:
        r = self.rpc.request("session/new", {"cwd": workspace, "mcpServers": []}, timeout=120)
        sid = r.get("sessionId")
        if not sid:
            raise AdapterError(f"session/new returned no sessionId: {r}")
        model = self.spec.get("model")
        if model:
            self._set_model(sid, model)
        if mode:
            self._set_mode(sid, mode)
        return sid

    def _set_model(self, sid: str, model: str):
        for method, params in (
            ("session/set_config_option", {"sessionId": sid, "configId": "model", "value": model}),
            ("session/set_config_option", {"sessionId": sid, "optionId": "model", "value": model}),
            ("session/set_model", {"sessionId": sid, "modelId": model}),
        ):
            try:
                self.rpc.request(method, params, timeout=30)
                self.model_set = model
                return
            except RpcError as e:
                self.model_set_error = f"{method}: {e.code} {e.message}"
            except (ProcessDied, TimeoutError) as e:
                self.model_set_error = f"{method}: {e}"
        raise AdapterError(f"agent rejected model '{model}' via every known method; last: {self.model_set_error}")

    def _set_mode(self, sid: str, mode: str):
        for method, params in (
            ("session/set_mode", {"sessionId": sid, "modeId": mode}),
            ("session/set_config_option", {"sessionId": sid, "configId": "mode", "value": mode}),
        ):
            try:
                self.rpc.request(method, params, timeout=30)
                self.mode_set = mode
                return
            except RpcError:
                continue
        self.mode_set_error = f"agent does not accept mode '{mode}' (using its default)"

    def resume_session(self, native_session_id: str, workspace: str) -> dict:
        try:
            self.rpc.request("session/load", {"sessionId": native_session_id, "cwd": workspace, "mcpServers": []}, timeout=120)
        except RpcError as e:
            if e.code == -32601:
                raise ResumeUnsupported(f"agent does not implement session/load: {e.message}") from e
            raise AdapterError(f"session/load failed: {e.message}") from e
        except (ProcessDied, TimeoutError) as e:
            raise AdapterError(f"session/load failed: {e}") from e
        return {"resumed": True, "evidence": "session/load accepted by agent"}

    def prompt(self, session_id: str, text: str, timeout: float, cancel_event: threading.Event,
               input_id: str | None = None) -> TerminalTurn:
        assert self.rpc
        turn = _Turn()
        with self._lock:
            self._turns[session_id] = turn
        try:
            mid, fut = self.rpc.request_future(
                "session/prompt",
                {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
            )
        except (ProcessDied, OSError) as e:
            with self._lock:
                self._turns.pop(session_id, None)
            return TerminalTurn(ok=False, reason="failed", text_tail=f"send failed: {e}")
        with self._lock:
            self._pending_futs[session_id] = fut
        deadline = time.time() + max(timeout, 30)
        while time.time() < deadline:
            if fut.done():
                return self._finish_prompt(session_id, turn, fut)
            if cancel_event.is_set():
                self.cancel(session_id)
            time.sleep(0.15)
        # timeout: the future stays registered so poll() can observe completion
        return TerminalTurn(ok=False, reason="timeout", text_tail=self._text(turn),
                            tools=turn.tools, blocked=True)

    def _finish_prompt(self, session_id: str, turn: _Turn, fut) -> TerminalTurn:
        with self._lock:
            self._turns.pop(session_id, None)
            self._pending_futs.pop(session_id, None)
        try:
            r = fut.result()
        except TimeoutError:
            return TerminalTurn(ok=False, reason="timeout", text_tail=self._text(turn), tools=turn.tools, blocked=True)
        except (RpcError, ProcessDied) as e:
            detail = getattr(e, "message", None) or str(e)
            return TerminalTurn(ok=False, reason="failed", text_tail=self._text(turn) or f"session/prompt error: {detail}",
                                tools=turn.tools)
        usage = turn.usage
        stop = (r or {}).get("stopReason") or "end_turn"
        if stop == "cancelled":
            return TerminalTurn(ok=False, reason="cancelled", text_tail=self._text(turn), tools=turn.tools, usage=usage)
        return TerminalTurn(ok=stop == "end_turn", reason=stop, text_tail=self._text(turn),
                            tools=turn.tools, usage=usage)

    def poll(self, session_id: str) -> TerminalTurn | None:
        """Watcher mode: check whether a timed-out turn has since completed."""
        with self._lock:
            fut = self._pending_futs.get(session_id)
            turn = self._turns.get(session_id)
        if fut is None:
            return None
        if not fut.done():
            if self.rpc and not self.rpc.alive():
                with self._lock:
                    self._pending_futs.pop(session_id, None)
                return TerminalTurn(ok=False, reason="failed", text_tail=self._text(turn) if turn else "",
                                    blocked=False)
            return None
        return self._finish_prompt(session_id, turn or _Turn(), fut)

    @staticmethod
    def _text(turn: _Turn) -> str:
        return "".join(turn.text_chunks)[-TEXT_TAIL_MAX:]

    def cancel(self, session_id: str) -> bool:
        """Send session/cancel; confirm ONLY if the in-flight prompt actually
        returned (ACP gives us a real response, unlike a fire-and-forget stop)."""
        if not self.rpc or not self.rpc.alive():
            self.last_cancel_note = "agent process not running"
            return False
        try:
            self.rpc.notify("session/cancel", {"sessionId": session_id})
        except (ProcessDied, OSError):
            self.last_cancel_note = "failed to deliver session/cancel"
            return False
        with self._lock:
            fut = self._pending_futs.get(session_id)
        if fut is None:
            with self._lock:
                active = session_id in self._turns
            if not active:
                self.last_cancel_note = "no turn in flight"
                return True
        deadline = time.time() + 5
        while time.time() < deadline:
            with self._lock:
                fut = self._pending_futs.get(session_id)
            if fut is not None and fut.done():
                self.last_cancel_note = "prompt response arrived after cancel"
                return True
            if not self.rpc.alive():
                self.last_cancel_note = "agent exited after cancel"
                return True
            time.sleep(0.1)
        self.last_cancel_note = "no prompt response within 5s after cancel (still running?)"
        return False

    def close(self):
        if self.rpc:
            self.rpc.close()
            self.rpc = None
