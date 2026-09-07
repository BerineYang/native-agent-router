"""Minimal custom adapter example for a NON-ACP native runtime.

Copy, implement the contract, and register the class name in
adapters/base.py:build_adapter(). ~150 lines is typical.

Required semantics (see docs/ARCHITECTURE.md "Cancel & timeout correctness"):
- prompt() returns TerminalTurn with ok/reason; on timeout it must NOT clear its
  internal turn state so poll() can observe a late real terminal.
- cancel() confirms only from an observed terminal event or process exit, and
  sets last_cancel_note when the stop was ineffective.
- resume_session() returns protocol evidence (e.g. contextUsed), never vibes.
"""

from __future__ import annotations

import threading

from native_agent_router.adapters.base import Adapter, TerminalTurn


class MyVendorAdapter(Adapter):
    usage_quality = "real"          # or "partial" / "unknown" — be honest
    session_resume = True

    def __init__(self, spec, log_dir, permission_policy="deny"):
        super().__init__(spec, log_dir, permission_policy)
        self.proc = None
        self.last_cancel_note = None
        self._turns = {}
        self._lock = threading.Lock()

    def capabilities(self):
        return {"transport": "my vendor rpc over stdio", "session_resume": True,
                "usage_quality": self.usage_quality}

    def start(self, workspace):
        # spawn your runtime; wire your reader thread to dispatch:
        #   server->client requests  -> answer per self.permission_policy
        #   notifications           -> feed self._turns[session_id]
        raise NotImplementedError

    def create_session(self, workspace, mode):
        raise NotImplementedError  # return native_session_id

    def resume_session(self, native_session_id, workspace):
        # return e.g. {"resumed": True, "contextUsed": <from your protocol>}
        raise NotImplementedError

    def prompt(self, session_id, text, timeout, cancel_event, input_id=None):
        # send; loop until deadline observing terminal; on timeout RETURN
        # TerminalTurn(ok=False, reason="timeout", blocked=True) and KEEP the
        # turn registered so poll() can complete it later.
        raise NotImplementedError

    def poll(self, session_id):
        with self._lock:
            turn = self._turns.get(session_id)
        # return TerminalTurn when your protocol reports the real terminal,
        # else None (still running).
        return None

    def cancel(self, session_id):
        # send your stop; wait briefly; return True ONLY on observed terminal
        # or process exit; otherwise set last_cancel_note and return False.
        return False

    def close(self):
        pass
