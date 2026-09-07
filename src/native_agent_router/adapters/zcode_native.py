"""Native ZCode adapter (app-server, protocol 0.16.x, verified against CLI 0.16.5).

Protocol facts (see docs/PROTOCOL-ZCODE-016.md):
- line-delimited JSON over stdio, NO `jsonrpc` envelope field
- session/create uses workspace {workspacePath, workspaceKey}, not cwd
- events arrive via session/event notifications with a per-session `seq`
- server->client requests (interaction/*, session/requestRuntimePreferences)
  MUST be answered or the turn stalls
- terminal signals: turn.completed / turn.failed / turn.terminal (may overlap;
  usage is counted from exactly one source, see _pick_usage)

No credentials are injected: the ZCode backend reads ~/.zcode/v2/config.json
itself. Model, provider and billing channel stay untouched.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from .base import Adapter, AdapterError, TerminalTurn
from .jsonrpc import JsonRpcProcess, ProcessDied, RpcError

TEXT_TAIL_MAX = 8000
TOOLS_MAX = 60


def _load_zcode_config(preferred: str | None = None) -> dict | None:
    """Read the user's own ZCode config (v2 first, then cli) read-only."""
    candidates = []
    if preferred:
        candidates.append(Path(preferred))
    candidates += [Path.home() / ".zcode" / "v2" / "config.json",
                   Path.home() / ".zcode" / "cli" / "config.json"]
    for c in candidates:
        try:
            d = json.loads(c.read_text(encoding="utf-8-sig"))
            if isinstance(d, dict) and d.get("provider"):
                return d
        except (OSError, ValueError):
            continue
    return None


def build_runtime_model(provider: str | None = None, model: str | None = None,
                        thought_level: str | None = None,
                        config: dict | None = None, config_path: str | None = None) -> dict | None:
    """Build ZCode 0.16's `runtimeModel` payload from the user's own config.

    The headless app-server omits provider secrets from persisted workspace
    state, so session/resume of a cold session must re-supply this payload
    (verified live: without it, send fails -32031 ZCODE_RUNTIME_MODEL_UNAVAILABLE).
    The apiKey travels only inside this protocol payload — never written to
    disk, never logged, never exposed via MCP state. Provider, base URL and
    billing channel are exactly as configured by the user's ZCode app.
    (Pattern learned from codex-zcode-bridge/upstream/zcode_protocol.py.)
    """
    config = config if config is not None else _load_zcode_config(config_path)
    if not config:
        return None
    providers = config.get("provider") or {}
    if provider:
        picked_id = provider if isinstance(providers.get(provider), dict) else None
    else:
        # explicit selection from config file: {"model": "prov/id"}
        sel = config.get("model")
        sel_str = sel.get("main") if isinstance(sel, dict) else (sel if isinstance(sel, str) else None)
        if sel_str and "/" in sel_str:
            pid = sel_str.split("/", 1)[0]
            picked_id = pid if isinstance(providers.get(pid), dict) else None
            model = model or sel_str.split("/", 1)[1]
        else:
            picked = _v2_provider_pick(config)
            picked_id = picked[0] if picked else None
    if not picked_id:
        return None
    prov = providers[picked_id]
    models = prov.get("models") or {}
    model_id = model if model in models else next(iter(models), None)
    if not model_id:
        return None
    m = models[model_id] or {}
    opts = prov.get("options") or {}
    limits = m.get("limit") or {}
    modalities = m.get("modalities") or {}
    inputs = modalities.get("input") or []
    entry = {
        "modelId": model_id,
        "label": m.get("name") or model_id,
    }
    if limits.get("context"):
        entry["contextWindow"] = limits["context"]
    if limits.get("output"):
        entry["maxOutputTokens"] = limits["output"]
    if modalities:
        entry["supportsImages"] = "image" in inputs
        entry["supportsPdf"] = "pdf" in inputs
    reasoning = m.get("reasoning") if isinstance(m.get("reasoning"), dict) else None
    if reasoning:
        variants = reasoning.get("variants") or []
        r = {"enabled": bool(reasoning.get("enabled", True)),
             "levels": [{"value": str(v), "label": str(v)} for v in variants]}
        if reasoning.get("defaultVariant"):
            r["defaultLevel"] = reasoning["defaultVariant"]
        entry["reasoning"] = r
    provider_block = {
        "providerId": picked_id,
        "kind": prov.get("kind") or "anthropic",
        "label": prov.get("name") or picked_id,
        "source": "workspace",
        "models": [entry],
    }
    for tgt, src in (("baseURL", "baseURL"), ("apiFormat", "apiFormat"),
                     ("apiKeyRequired", "apiKeyRequired"), ("headers", "headers")):
        if opts.get(src) is not None:
            provider_block[tgt] = opts[src]
    if isinstance(opts.get("apiKey"), str) and opts["apiKey"]:
        provider_block["apiKey"] = {"source": "inline", "value": opts["apiKey"]}
    payload = {
        "revision": f"nar-{uuid.uuid4().hex}",
        "generatedAt": int(time.time() * 1000),
        "model": {"providerId": picked_id, "modelId": model_id},
        "provider": provider_block,
    }
    if thought_level:
        payload["thoughtLevel"] = thought_level
    return payload


def _v2_provider_pick(d: dict) -> tuple[str, dict] | None:
    enabled = {k: v for k, v in (d.get("provider") or {}).items() if v.get("enabled")}
    if not enabled:
        return None
    cache: set = set()
    try:
        c = json.loads((Path.home() / ".zcode" / "v2" / "coding-plan-cache.json").read_text(encoding="utf-8"))
        cache = {k for k, v in (c.get("entryStatus", {}).get("items") or {}).items()
                 if v.get("status") == "available"}
    except (OSError, ValueError):
        pass
    pick = next((k for k in enabled if k in cache), next(iter(enabled)))
    return pick, enabled[pick]


def zcode_env_auto(provider: str | None = None, model: str | None = None) -> dict:
    """Build subprocess env from the user's OWN ZCode configuration (read-only).

    The headless app-server refuses to start when no explicit model provider is
    configured (~/.zcode/cli/config.json). The ZCode desktop app stores the
    providers/keys in ~/.zcode/v2/config.json instead. When the headless config
    is absent, we pass the enabled provider's own values to the subprocess env.
    We never change provider, model, base URL or billing channel: these are the
    same credentials the desktop app itself uses. Values are never logged or
    written anywhere.

    Note: env-based config is enough for NEW sessions but resume cannot persist
    a model selection from it (ZCODE_RUNTIME_MODEL_UNAVAILABLE). For full
    session resume use config zcode_home="isolated" (see build_isolated_home).
    """
    cli_cfg = Path.home() / ".zcode" / "cli" / "config.json"
    if cli_cfg.is_file() and not provider and not model:
        try:
            cfg = json.loads(cli_cfg.read_text(encoding="utf-8-sig"))
            if cfg.get("provider") or cfg.get("model"):
                return {}
        except ValueError:
            pass
    try:
        d = json.loads((Path.home() / ".zcode" / "v2" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if provider:
        pv = (d.get("provider") or {}).get(provider)
        if not pv:
            return {}
        picked = (provider, pv)
    else:
        picked = _v2_provider_pick(d)
    if not picked:
        return {}
    opts = picked[1].get("options") or {}
    env = {
        "ZCODE_MODEL": model or next(iter(picked[1].get("models") or {}), "GLM-5.3"),
        "ZCODE_BASE_URL": opts.get("baseURL", ""),
    }
    if opts.get("apiKey"):
        env["ANTHROPIC_API_KEY"] = opts["apiKey"]
    return env


def build_isolated_home(target: Path, provider: str | None = None, model: str | None = None) -> Path:
    """Create a NAR-owned ZCode home with a headless model config so that the
    app-server can resolve AND persist model selections (required for
    session/resume — verified live on ZCode 0.16.5). The provider entry is
    copied from the user's own ~/.zcode/v2/config.json (same provider, base
    URL and billing channel; selection is explicit via config "provider"/
    "model" if set). The file contains the user's API key: written with
    owner-only permissions under the NAR home, regenerated as needed, never
    logged or copied elsewhere.
    """
    try:
        d = json.loads((Path.home() / ".zcode" / "v2" / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise AdapterError(f"isolated zcode home requires ~/.zcode/v2/config.json: {e}") from e
    if provider:
        pv = (d.get("provider") or {}).get(provider)
        if not pv:
            raise AdapterError(f"provider '{provider}' not found in ~/.zcode/v2/config.json")
        picked = (provider, pv)
    else:
        picked = _v2_provider_pick(d)
    if not picked:
        raise AdapterError("no enabled provider in ~/.zcode/v2/config.json")
    prov_id, prov = picked
    opts = prov.get("options") or {}
    models = {}
    for mid, m in (prov.get("models") or {}).items():
        limit = m.get("limit") or {}
        models[mid] = {k: v for k, v in {"contextWindow": limit.get("context"),
                                         "maxOutputTokens": limit.get("output")}.items() if v}
    if not models:
        models = {"GLM-5.3": {}}
    chosen_model = model if (model and model in models) else next(iter(models))
    entry = {
        "kind": prov.get("kind") or "anthropic",
        "name": prov.get("name") or prov_id,
        "options": {"baseURL": opts.get("baseURL", "")},
        "models": models,
    }
    if opts.get("apiKey"):
        entry["options"]["apiKey"] = opts["apiKey"]
    cli_dir = target / ".zcode" / "cli"
    cli_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cli_dir / "config.json"
    payload = {"provider": {prov_id: entry}, "model": f"{prov_id}/{chosen_model}"}
    cfg_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        os.chmod(cfg_path, 0o600)
    except OSError:
        pass
    return target


class _Turn:
    def __init__(self):
        self.event = threading.Event()
        self.text_chunks: list[str] = []
        self.text_len = 0
        self.tools: list[str] = []
        self.completed = None   # turn.completed payload
        self.failed = None      # turn.failed payload
        self.terminal = None    # turn.terminal payload
        self.last_session_usage = None
        self.turn_id = None
        self.cancel_requested = False


class _Session:
    def __init__(self):
        self.last_seq = 0
        self.turn: _Turn | None = None


class ZcodeNativeAdapter(Adapter):
    usage_quality = "real"
    session_resume = True

    def __init__(self, spec, log_dir, permission_policy="deny"):
        super().__init__(spec, log_dir, permission_policy)
        self.rpc: JsonRpcProcess | None = None
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()
        self._interactions: list[dict] = []
        self.last_cancel_note: str | None = None
        self.runtime_model: dict | None = None

    def capabilities(self) -> dict:
        d = self.spec.get("discovery") or {}
        return {
            "transport": "stdio app-server (zcode protocol 0.16)",
            "zcode_cjs": d.get("zcode_cjs"),
            "node": d.get("node"),
            "session_resume": True,
            "usage_quality": self.usage_quality,
            "modes": ["plan", "build", "edit", "yolo"],
        }

    def start(self, workspace: str):
        env = dict(self.spec.get("env") or {})
        provider = self.spec.get("provider")
        model = self.spec.get("model")
        self.runtime_model = build_runtime_model(
            provider=provider, model=model, thought_level=self.spec.get("thought_level"))
        zh = self.spec.get("zcode_home")
        if zh:
            from ..config import home_dir

            target = Path(zh) if zh not in ("isolated", True) else home_dir() / "zcode-home"
            build_isolated_home(target, provider=provider, model=model)
            env["USERPROFILE"] = str(target)
            env["HOME"] = str(target)
        elif self.runtime_model is None and self.spec.get("credentials", "auto") == "auto" and not (
                env.get("ANTHROPIC_API_KEY") or env.get("ZCODE_MODEL")):
            # fallback for machines where no provider config is resolvable
            env.update(zcode_env_auto(provider=provider, model=model))
        self.rpc = JsonRpcProcess(
            self.spec["argv"], cwd=workspace,
            raw_log=self.log_dir / "native-raw.jsonl",
            env=env or None,
        )
        self.rpc.on_request = self._on_request
        self.rpc.on_notification = self._on_notification

    def _session_params(self, base: dict, input_id: str | None = None, include_model: bool = False) -> dict:
        if self.runtime_model is not None:
            base["runtimeModel"] = self.runtime_model
            if include_model:
                base["model"] = self.runtime_model["model"]
        allow = self.spec.get("tool_allowlist")
        if allow:
            base["toolAllowlist"] = list(allow)
        if input_id:
            base["inputId"] = input_id
        return base

    # ---- server->client requests ----

    def _pick_option(self, options: list, allow: bool):
        for o in options:
            kind = str(o.get("kind") or "")
            if allow and kind.startswith("allow"):
                return o.get("optionId")
        for o in options:
            kind = str(o.get("kind") or "")
            if not allow and (kind.startswith("deny") or kind.startswith("reject")):
                return o.get("optionId")
        if allow:
            return options[0].get("optionId") if options else None
        return options[-1].get("optionId") if options else None

    def _on_request(self, method: str, params: dict, mid):
        if method == "interaction/requestPermission":
            # Verified live on 0.16.5: the broker expects a decision object
            # {decision:"allow"|"deny", reason}; returning an optionId alone is
            # treated as invalid and silently denied (which causes retry storms).
            allow = self.permission_policy == "allow"
            self._interactions.append({"method": method, "tool": params.get("toolName"),
                                       "decision": "allow" if allow else "deny", "policy": self.permission_policy})
            if allow:
                return {"decision": "allow", "reason": "nar: pre-authorized for this task"}
            return {"decision": "deny", "reason": "nar: permission policy is deny; stop attempting this side effect and report"}
        if method == "interaction/requestUserInput":
            # plan approval / AskUserQuestion: workers must not make product decisions
            self._interactions.append({"method": method, "outcome": "cancelled", "policy": self.permission_policy})
            return {"decision": "deny", "reason": "nar: user decision required; end the turn and report what you need"}
        if method == "session/requestRuntimePreferences":
            # 0.16 requires an answer within ~15s; Zod validates this field (verified live)
            return {"nativeSearchEnhancementsEnabled": False}
        raise LookupError(method)

    # ---- notifications ----

    def _on_notification(self, method: str, params: dict):
        if method != "session/event":
            return
        sid = params.get("sessionId")
        with self._lock:
            sess = self._sessions.get(sid)
        if sess is None:
            return
        seq = params.get("seq")
        if isinstance(seq, int) and seq <= sess.last_seq:
            return  # replay / snapshot overlap dedup
        if isinstance(seq, int):
            sess.last_seq = seq
        etype = params.get("type") or ""
        payload = params.get("payload") or {}
        turn = sess.turn
        if turn is None:
            if etype.startswith("turn."):
                return
            return
        if etype == "turn.started":
            turn.turn_id = payload.get("turnId")
        elif etype == "model.streaming":
            kind = payload.get("kind")
            if kind == "text_delta":
                delta = payload.get("delta") or ""
                turn.text_chunks.append(delta)
                turn.text_len += len(delta)
            elif kind == "tool_call":
                turn.tools.append(f"call:{payload.get('toolName') or payload.get('name')}")
        elif etype == "tool.updated":
            kind = payload.get("kind") or ""
            name = payload.get("toolName") or ""
            line = f"{kind}:{name}"
            if kind == "error":
                line += f" {str(payload.get('error') or payload.get('output'))[:200]}"
            turn.tools.append(line)
            if len(turn.tools) > TOOLS_MAX:
                del turn.tools[: len(turn.tools) - TOOLS_MAX]
        elif etype == "session.updated":
            u = payload.get("usage")
            if isinstance(u, dict):
                turn.last_session_usage = u
        elif etype == "turn.completed":
            if turn.completed is None:
                turn.completed = payload
            turn.event.set()
        elif etype == "turn.failed":
            if turn.failed is None:
                turn.failed = payload
            turn.event.set()
        elif etype == "turn.terminal":
            if turn.terminal is None:
                turn.terminal = payload
            turn.event.set()

    # ---- lifecycle ----

    def create_session(self, workspace: str, mode: str | None) -> str:
        w = {"workspacePath": workspace, "workspaceKey": workspace}
        params = self._session_params({
            "workspace": w, "mode": mode or "build",
            # saves one hidden model call per session; sessions durable at once
            "titleGenerationEnabled": False, "persistence": "immediate",
        }, include_model=True)
        r = self.rpc.request("session/create", params, timeout=90)
        sid = (r.get("session") or {}).get("sessionId")
        if not sid:
            raise AdapterError(f"session/create returned no sessionId: {r}")
        self._subscribe(sid)
        return sid

    def resume_session(self, native_session_id: str, workspace: str) -> dict:
        w = {"workspacePath": workspace, "workspaceKey": workspace}
        self.rpc.request("session/resume", self._session_params({"sessionId": native_session_id, "workspace": w}),
                         timeout=90)
        read = self.rpc.request("session/read", {"sessionId": native_session_id}, timeout=30)
        proj = read.get("projection") or {}
        self._subscribe(native_session_id)
        return {
            "resumed": True,
            "contextUsed": proj.get("contextUsed"),
            "contextWindow": proj.get("contextWindow"),
            "status": proj.get("status"),
        }

    def _subscribe(self, sid: str):
        with self._lock:
            self._sessions.setdefault(sid, _Session())
        self.rpc.request(
            "session/subscribe",
            {"sessionId": sid, "deliveryKind": "desktop-continuous", "includeSnapshot": True, "afterSeq": 0},
            timeout=30,
        )

    @staticmethod
    def _pick_usage(turn: _Turn) -> tuple[dict | None, str]:
        def norm(u: dict, source: str) -> dict:
            out = {"input_tokens": u.get("inputTokens") or u.get("input_tokens") or 0,
                   "output_tokens": u.get("outputTokens") or u.get("output_tokens") or 0,
                   "total_tokens": u.get("totalTokens") or u.get("total_tokens") or 0,
                   "source": source}
            for k_native, k_out in (("reasoningTokens", "reasoning_tokens"),
                                    ("cacheReadTokens", "cache_read_tokens"),
                                    ("cacheWriteTokens", "cache_write_tokens"),
                                    ("modelRequests", "model_requests")):
                if u.get(k_native) is not None:
                    out[k_out] = u[k_native]
            return out
        t = turn.terminal or {}
        if t.get("inputTokens") is not None or t.get("outputTokens") is not None or t.get("totalTokens") is not None:
            return (norm(t, "turn.terminal"), "turn.terminal")
        c = turn.completed or {}
        u = c.get("usage")
        if isinstance(u, dict):
            return (norm(u, "turn.completed"), "turn.completed")
        u = turn.last_session_usage
        if isinstance(u, dict):
            return (norm(u, "session.updated"), "session.updated")
        return (None, "none")

    def _begin_turn(self, session_id: str) -> tuple[_Session, _Turn]:
        with self._lock:
            sess = self._sessions.setdefault(session_id, _Session())
            turn = _Turn()
            sess.turn = turn
        return sess, turn

    def _build_turn(self, sess: _Session, turn: _Turn) -> TerminalTurn | None:
        """Return a TerminalTurn if the turn reached a terminal state, else None
        (still running — keep observing; the session/stop ack alone is never
        treated as confirmation because ZCode 0.16.5 may clear its cancel
        controller early while the task continues)."""
        if not turn.event.is_set():
            if self.rpc and not self.rpc.alive():
                return TerminalTurn(ok=False, reason="failed", text_tail=self._text(turn), tools=turn.tools)
            return None
        usage, usage_source = self._pick_usage(turn)
        if usage:
            usage["source"] = usage_source
        if turn.failed is not None:
            err = turn.failed.get("error") or {}
            tail = self._text(turn) or f"turn.failed: {err.get('code')} {err.get('message')}"
            return TerminalTurn(ok=False, reason="failed", text_tail=tail[:TEXT_TAIL_MAX],
                                tools=turn.tools, usage=usage)
        if turn.cancel_requested and not turn.completed and not turn.terminal:
            return TerminalTurn(ok=False, reason="cancelled", text_tail=self._text(turn),
                                tools=turn.tools, usage=usage)
        rt = (turn.terminal or {}).get("resultType") or (turn.completed or {}).get("resultType") or "success"
        return TerminalTurn(ok=rt in ("success", "end_turn"), reason="end_turn",
                            text_tail=self._text(turn), tools=turn.tools, usage=usage)

    def prompt(self, session_id: str, text: str, timeout: float, cancel_event: threading.Event,
               input_id: str | None = None) -> TerminalTurn:
        assert self.rpc
        sess, turn = self._begin_turn(session_id)
        try:
            self.rpc.request("session/send",
                             self._session_params({"sessionId": session_id, "content": text}, input_id),
                             timeout=60)
        except (RpcError, ProcessDied, TimeoutError) as e:
            with self._lock:
                sess.turn = None
            return TerminalTurn(ok=False, reason="failed", text_tail=f"session/send failed: {e}")
        deadline = time.time() + max(timeout, 5)
        while time.time() < deadline:
            if cancel_event.is_set() and not turn.cancel_requested:
                turn.cancel_requested = True
                self.cancel(session_id)
            result = self._build_turn(sess, turn)
            if result is not None:
                with self._lock:
                    if sess.turn is turn:
                        sess.turn = None
                return result
            time.sleep(0.15)
        # timeout: DO NOT clear the turn — a watcher can keep observing via poll()
        return TerminalTurn(ok=False, reason="timeout", text_tail=self._text(turn),
                            tools=turn.tools, blocked=True)

    def poll(self, session_id: str) -> TerminalTurn | None:
        """Check a previously timed-out turn for completion (watcher mode)."""
        with self._lock:
            sess = self._sessions.get(session_id)
            turn = sess.turn if sess else None
        if turn is None:
            return None
        result = self._build_turn(sess, turn)
        if result is not None:
            with self._lock:
                if sess.turn is turn:
                    sess.turn = None
        return result

    @staticmethod
    def _text(turn: _Turn) -> str:
        return "".join(turn.text_chunks)[-TEXT_TAIL_MAX:]

    def cancel(self, session_id: str) -> bool:
        """Send session/stop and judge effectiveness by OBSERVED events, never
        by the ack. Returns True only when the turn actually reached a terminal
        state. Records why a False happened (native early-cancel-controller
        clearance shows up as 'events continued after session/stop')."""
        if not self.rpc or not self.rpc.alive():
            self.last_cancel_note = "agent process not running"
            return False
        with self._lock:
            sess = self._sessions.get(session_id)
            turn = sess.turn if sess else None
            seq_before = sess.last_seq if sess else 0
        try:
            self.rpc.notify("session/stop", {"sessionId": session_id})
        except (ProcessDied, OSError):
            self.last_cancel_note = "failed to deliver session/stop"
            return False
        if turn is not None:
            turn.cancel_requested = True
        deadline = time.time() + 5
        while time.time() < deadline:
            if turn is not None and turn.event.is_set():
                self.last_cancel_note = "turn reached terminal state after stop"
                return True
            if not self.rpc.alive():
                self.last_cancel_note = "agent process exited after stop"
                return True
            time.sleep(0.1)
        with self._lock:
            seq_after = (self._sessions.get(session_id) or _Session()).last_seq
        if seq_after > seq_before:
            self.last_cancel_note = (f"events continued after session/stop (seq {seq_before} -> {seq_after}); "
                                     "native cancel ineffective — watcher keeps observing")
        else:
            self.last_cancel_note = "no terminal event within 5s grace (turn may still be running)"
        return False

    def close(self):
        if self.rpc:
            self.rpc.close()
            self.rpc = None

    def interaction_log(self) -> list[dict]:
        return self._interactions
