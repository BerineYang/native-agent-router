"""Scripted stub adapter for no-model tests. Emulates turns, resume state,
duplicate usage reports, failures, timeouts, unconfirmed cancels and crashes."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .base import Adapter, TerminalTurn


class StubAdapter(Adapter):
    usage_quality = "real"
    session_resume = True

    def __init__(self, spec, log_dir, permission_policy="deny"):
        super().__init__(spec, log_dir, permission_policy)
        scenario = spec.get("scenario")
        scenario_path = spec.get("scenario_path")
        if scenario is None and scenario_path:
            scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8"))
        self.scenario = scenario or []
        self.calls = 0
        self.started = False
        self.crashed = False
        self.prompted = 0
        self.cancellations = 0
        self._pending = None
        self.last_cancel_note = None
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "stub-calls.jsonl").open("a", encoding="utf-8").close()

    def capabilities(self) -> dict:
        return {"transport": "stub", "session_resume": True, "usage_quality": self.usage_quality}

    def start(self, workspace: str):
        self.started = True
        self.workspace = workspace

    def create_session(self, workspace: str, mode: str | None) -> str:
        return "sess-stub-0001"

    def resume_session(self, native_session_id: str, workspace: str) -> dict:
        return {"resumed": True, "contextUsed": self.prompted * 500}

    def _usage(self, raw: dict) -> dict:
        return {"input_tokens": raw.get("input_tokens", 0), "output_tokens": raw.get("output_tokens", 0),
                "total_tokens": raw.get("total_tokens", 0), "source": "stub"}

    def prompt(self, session_id: str, text: str, timeout: float, cancel_event: threading.Event,
               input_id: str | None = None) -> TerminalTurn:
        self.prompted += 1
        if self.crashed:
            raise RuntimeError("stub process crashed")
        i = min(self.calls, len(self.scenario) - 1)
        step = self.scenario[i] if self.scenario else {"kind": "turn", "text": "done"}
        self.calls += 1
        kind = step.get("kind")
        if kind == "crash":
            self.crashed = True
            raise RuntimeError("stub process crashed")
        if kind == "timeout":
            time.sleep(min(float(step.get("seconds", 2)), max(timeout, 1)))
            return TerminalTurn(ok=False, reason="timeout", text_tail=step.get("text", ""), blocked=True)
        if kind == "timeout_then_complete":
            time.sleep(min(float(step.get("seconds", 2)), max(timeout, 1)))
            self._pending = {"at": time.time() + float(step.get("complete_after", 3)),
                             "turn": TerminalTurn(ok=True, reason="end_turn", text_tail=step.get("text", "late finish"),
                                                  usage=self._usage(step["usage"]) if step.get("usage") else None)}
            return TerminalTurn(ok=False, reason="timeout", text_tail=step.get("text", ""), blocked=True)
        if kind == "hold":
            deadline = time.time() + float(step.get("seconds", 3))
            while time.time() < deadline:
                if cancel_event.is_set() and step.get("cancel_confirmed", True):
                    return TerminalTurn(ok=False, reason="cancelled", text_tail=step.get("text", ""))
                time.sleep(0.05)
            return TerminalTurn(ok=True, reason="end_turn", text_tail=step.get("text", "held"),
                                usage=self._usage(step["usage"]) if step.get("usage") else None)
        if kind == "fail":
            return TerminalTurn(ok=False, reason="failed", text_tail=step.get("text", "stub failure"))
        if step.get("write_file"):
            ws = getattr(self, "workspace", None)
            if ws:
                (Path(ws) / step["write_file"]).write_text("stub change\n", encoding="utf-8")
        usage = self._usage(step["usage"]) if step.get("usage") else None
        if step.get("duplicate_usage") and usage:
            usage = dict(usage)
            usage["duplicate_reported"] = True  # kernel must still count once
        return TerminalTurn(ok=True, reason="end_turn", text_tail=step.get("text", ""),
                            tools=step.get("tools", []), usage=usage)

    def poll(self, session_id: str) -> TerminalTurn | None:
        p = getattr(self, "_pending", None)
        if p and time.time() >= p["at"]:
            self._pending = None
            return p["turn"]
        return None

    def cancel(self, session_id: str) -> bool:
        self.cancellations += 1
        step = self.scenario[min(self.calls, len(self.scenario) - 1)]
        return bool(step.get("cancel_confirmed", True))
