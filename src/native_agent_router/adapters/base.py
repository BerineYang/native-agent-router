"""Pluggable adapter contract. Each adapter drives one native agent runtime."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TerminalTurn:
    ok: bool
    reason: str  # end_turn | failed | timeout | cancelled | unsupported
    text_tail: str = ""
    tools: list = field(default_factory=list)
    usage: dict | None = None
    blocked: bool = False


class AdapterError(Exception):
    pass


class ResumeUnsupported(AdapterError):
    pass


class Adapter:
    usage_quality = "unknown"   # real | partial | estimated | unknown
    session_resume = True

    def __init__(self, spec: dict, log_dir: Path, permission_policy: str = "deny"):
        self.spec = spec
        self.log_dir = log_dir
        self.permission_policy = (permission_policy or "deny").lower()

    def capabilities(self) -> dict:
        raise NotImplementedError

    def start(self, workspace: str):
        raise NotImplementedError

    def create_session(self, workspace: str, mode: str | None) -> str:
        raise NotImplementedError

    def resume_session(self, native_session_id: str, workspace: str) -> dict:
        raise NotImplementedError

    def prompt(self, session_id: str, text: str, timeout: float, cancel_event: threading.Event,
               input_id: str | None = None) -> TerminalTurn:
        raise NotImplementedError

    def poll(self, session_id: str) -> TerminalTurn | None:
        """Watcher hook: return the terminal turn if a previously timed-out
        turn has since completed; None if still running or unknown."""
        return None

    def cancel(self, session_id: str) -> bool:
        return False

    def close(self):
        pass


def build_adapter(spec: dict, log_dir: Path, permission_policy: str) -> Adapter:
    kind = spec["adapter"]
    if kind == "zcode-native":
        from .zcode_native import ZcodeNativeAdapter as A
    elif kind == "acp-generic":
        from .acp_generic import AcpGenericAdapter as A
    elif kind == "stub":
        from .stub import StubAdapter as A
    else:
        raise AdapterError(f"unknown adapter '{kind}'")
    return A(spec, log_dir, permission_policy)
