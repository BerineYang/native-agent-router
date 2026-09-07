"""Masked, token-cheap error diagnosis for a task.

build_diagnosis() returns a small JSON-safe dict (bounded size) that explains
the likely cause from objective evidence: task timeline, adapter stderr tail,
compact protocol frames, verify tails, interaction records. All user content is
redacted first (secrets, emails, long blobs, absolute paths outside the
workspace basename), so the report is safe to paste into a chat or an issue.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

MAX_BYTES = 4200
LINE_MAX = 160

_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|credential)\s*[\"']?\s*[:=]\s*[\"']?([^\s\"',}]{4,})")
_SK_STYLE = re.compile(r"\b(sk|pk|pat|ghp|xox[bap])-[A-Za-z0-9_\-]{8,}\b")
_B64ISH = re.compile(r"\b[A-Za-z0-9+/]{48,}={0,2}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_URL_CRED = re.compile(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@")


def mask_text(text: str) -> str:
    if not text:
        return ""
    t = _SECRET_KEY.sub(lambda m: f"{m.group(1)}=<REDACTED>", text)
    t = _SK_STYLE.sub("<REDACTED>", t)
    t = _B64ISH.sub("<REDACTED_BLOB>", t)
    t = _URL_CRED.sub(r"\1<user>:<pw>@", t)
    t = _EMAIL.sub("<email>", t)
    out = []
    for line in t.splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) > LINE_MAX:
            line = line[:LINE_MAX] + "..."
        out.append(line)
    return "\n".join(out[-40:])


def _tail_file(path: Path, lines: int) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(data[-lines:])


def _compact_frames(path: Path, n: int = 24) -> list:
    frames = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return frames
    for ln in lines[-n * 3:]:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        msg = rec.get("msg")
        if not isinstance(msg, dict):
            continue
        f = {"dir": rec.get("dir")}
        if msg.get("method"):
            f["method"] = msg["method"]
        if msg.get("type"):
            f["type"] = msg["type"]
        if msg.get("error") is not None:
            err = msg["error"]
            f["error"] = {"code": err.get("code"), "message": mask_text(str(err.get("message") or ""))[:200]}
        if msg.get("result") is not None and isinstance(msg.get("result"), dict):
            keys = sorted(msg["result"].keys())[:6]
            f["result_keys"] = keys
        frames.append(f)
        if len(frames) >= n:
            break
    return frames


HINTS = [
    (r"RUNTIME_MODEL_UNAVAILABLE|Model config is missing|no loaded config file",
     "zcode headless model config: set agents.json zcode option \"zcode_home\": \"isolated\" "
     "(NAR generates a private headless config from your own ~/.zcode/v2/config.json; needed for session resume)."),
    (r"requestRuntimePreferences",
     "ZCode 0.16 requires answering session/requestRuntimePreferences within ~15s; the adapter does; "
     "if you see this, check for a custom adapter override."),
    (r"workspace_busy", "Another NAR task holds the workspace lock; wait for it, cancel it, or use a different worktree."),
    (r"budget", "token budget exceeded; raise budget_tokens or split the task into a smaller work unit."),
    (r"timeout|blocked", "turn exceeded timeout_sec: the worker may still be running; use inspect/cancel, "
     "never blindly resubmit side-effecting work."),
    (r"429|1302|Too Many Requests|rate limit|请求过于频繁",
     "provider rate limit: retry later or lower concurrency; NAR serializes per workspace but not across agents."),
    (r"session/load|ResumeUnsupported", "this ACP agent does not implement session/load; continuation starts a new native session."),
    (r"initialize failed|ACP initialize", "ACP handshake failed: check the command in agents.json and run the agent manually."),
    (r"node|ENOENT", "Node.js >= 22 not found on PATH (required by the ZCode CLI)."),
    (r"verification_failed", "verification commands failed after the allowed repair: inspect(what='verify') and decide; do not lower the bar."),
    (r"permission|requestPermission", "worker requested a permission that was denied by policy: rerun with permission_policy='allow' only in a sandbox you trust."),
    (r"process exited|ProcessDied", "agent process died: inspect(what='raw') stderr tail shows why (crash, OOM, auth)."),
]


ERROR_TEMPLATES = [
    ("workspace_busy", r"workspace_busy|workspace is locked", "another NAR task holds the workspace lock"),
    ("require_git_baseline", r"require_git_baseline", "refused: non-git workspace"),
    ("invalid_workspace", r"workspace does not exist|workspace is required|scope_files must be relative", "bad workspace/scope argument"),
    ("model_unavailable", r"RUNTIME_MODEL_UNAVAILABLE|Model config is missing|no loaded config file|model config", "ZCode model/provider config rejected"),
    ("runtime_prefs_unanswered", r"requestRuntimePreferences", "server->client request not answered in time"),
    ("permission_denied", r"permission policy is deny|requestPermission", "worker side effect denied by policy"),
    ("verification_failed", r"verification_failed|acceptance verification", "output failed the fixed acceptance checks"),
    ("budget_exceeded", r"budget", "token budget exceeded"),
    ("turn_timeout", r"turn timeout|timeout after", "turn exceeded timeout; native may still run"),
    ("cancel_unconfirmed", r"cancel|stop", "stop requested but not confirmed by the runtime"),
    ("rate_limited", r"429|1302|Too Many Requests|rate limit|请求过于频繁", "provider rate limit"),
    ("resume_unsupported", r"session/load|resume unsupported|ResumeUnsupported", "agent cannot resume sessions"),
    ("acp_initialize", r"initialize failed|ACP initialize", "ACP handshake failed"),
    ("node_missing", r"node|ENOENT", "Node.js >= 22 missing (ZCode CLI)"),
    ("process_died", r"process exited|ProcessDied|crashed|agent process died", "agent process crashed"),
]


def classify_error(text: str, status: str = "") -> str:
    """Map an error string (and status) to a stable, clusterable code."""
    t = (text or "").lower()
    if not t:
        return "ok" if status in ("succeeded",) else ("none" if not status else status)
    for code, pat, _tmpl in ERROR_TEMPLATES:
        if re.search(pat, text or "", re.I):
            return code
    if status == "failed":
        return "failed_other"
    return "unknown"


def build_diagnosis(store, task: dict, config) -> dict:
    task_id = task["task_id"]
    d = store.log_dir(task_id)
    stderr_tail = mask_text(_tail_file(Path(str(d / "native-raw.jsonl.stderr")), 25))
    frames = _compact_frames(d / "native-raw.jsonl")
    verify = (task.get("result") or {}).get("verify") or []
    verify_tails = [mask_text(v.get("tail") or "")[-400:] for v in verify if not v.get("ok")][:3]
    timeline = (task.get("timeline") or [])[-12:]
    combined = " ".join(filter(None, [str(task.get("error") or ""), stderr_tail,
                                      json.dumps(frames[-6:], ensure_ascii=False)]))
    hints = [hint for pat, hint in HINTS if re.search(pat, combined, re.I)]
    code = classify_error(task.get("error") or "", task.get("status") or "")
    diag = {
        "task_id": task_id,
        "code": code,
        "template_id": code,
        "status": task.get("status"),
        "agent_id": task.get("agent_id"),
        "mode": task.get("mode"),
        "permission_policy": task.get("permission_policy"),
        "workspace": Path(task.get("workspace") or "").name,
        "error": mask_text(str(task.get("error") or ""))[:600],
        "timeline_tail": timeline,
        "stderr_tail": stderr_tail,
        "protocol_frames": frames[-14:],
        "verify_failures": verify_tails,
        "usage": task.get("usage"),
        "likely_cause": next((tmpl for c, _p, tmpl in ERROR_TEMPLATES if c == code), None),
        "hints": hints[:4],
        "sanitized": True,
    }
    blob = json.dumps(diag, ensure_ascii=False)
    if len(blob.encode("utf-8")) > MAX_BYTES:
        diag["stderr_tail"] = diag["stderr_tail"][:800]
        diag["protocol_frames"] = diag["protocol_frames"][-6:]
        diag["timeline_tail"] = diag["timeline_tail"][-6:]
        diag["truncated"] = True
    return diag
