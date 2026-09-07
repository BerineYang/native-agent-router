"""Deterministic task scoring and per-agent statistics.

Scores are computed from objective signals only (verify outcomes, repair use,
out-of-scope edits, timeouts, unconfirmed cancels, usage quality, baseline
auditability). NO model calls, so scoring itself costs zero tokens.
The orchestrator uses the stats surfaced by agents()/nar stats to decide
which agent (high- vs low-capability) should get the next task.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()


def compute_score(*, status: str, verify: list, repair_used: int, out_of_scope: list,
                  diff_git: bool, cancel_unconfirmed: bool, blocked: bool,
                  usage_quality: str) -> dict:
    score = 100
    factors = []
    if status == "succeeded" and verify and all(v["ok"] for v in verify):
        factors.append("verify_passed_first_try" if repair_used == 0 else "verify_passed_after_repair")
    if verify and not all(v["ok"] for v in verify):
        score -= 45
        factors.append("verify_failed")
    if repair_used:
        score -= 20
        factors.append("repair_needed")
    if out_of_scope:
        score -= 15
        factors.append(f"out_of_scope:{len(out_of_scope)}")
    if not diff_git:
        score -= 10
        factors.append("no_git_baseline_isolation_unverifiable")
    if blocked:
        score -= 30
        factors.append("timeout_or_blocked")
    if cancel_unconfirmed:
        score -= 20
        factors.append("cancel_unconfirmed")
    if status in ("failed", "cancelled"):
        score -= 30
        factors.append(f"status_{status}")
    if usage_quality == "unknown":
        score -= 3
        factors.append("usage_unreported")
    elif usage_quality in ("partial", "estimated"):
        score -= 1
        factors.append(f"usage_{usage_quality}")
    return {"score": max(0, min(100, score)), "factors": factors}


class AgentStats:
    def __init__(self, home: Path):
        self.dir = Path(home) / "stats"
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, agent_id: str) -> Path:
        return self.dir / f"{agent_id}.json"

    def load(self, agent_id: str) -> dict:
        try:
            return json.loads(self.path(agent_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"agent_id": agent_id, "runs": 0, "succeeded": 0, "failed": 0, "other": 0,
                    "score_sum": 0, "repair_runs": 0, "oos_runs": 0, "blocked_runs": 0,
                    "total_tokens": 0, "last_tasks": []}

    def record(self, agent_id: str, task_id: str, status: str, score: dict,
               usage_total: int, duration_sec: float):
        with _LOCK:
            s = self.load(agent_id)
            s["runs"] += 1
            if status == "succeeded":
                s["succeeded"] += 1
            elif status in ("failed", "cancelled"):
                s["failed"] += 1
            else:
                s["other"] += 1
            s["score_sum"] += int(score.get("score", 0))
            s["repair_runs"] += 1 if "repair_needed" in score.get("factors", []) else 0
            s["oos_runs"] += 1 if any(f.startswith("out_of_scope") for f in score.get("factors", [])) else 0
            s["blocked_runs"] += 1 if "timeout_or_blocked" in score.get("factors", []) else 0
            s["total_tokens"] += int(usage_total or 0)
            s.setdefault("durations", []).append(round(duration_sec, 1))
            s["durations"] = s["durations"][-30:]
            s["last_tasks"] = ([{"task_id": task_id, "status": status, "score": score.get("score"),
                                 "factors": score.get("factors", [])[:4], "ts": time.strftime("%m-%d %H:%M")}]
                               + s.get("last_tasks", []))[:15]
            tmp = self.path(agent_id).with_suffix(".tmp")
            tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path(agent_id))
            return s

    def summary(self, agent_id: str) -> dict:
        s = self.load(agent_id)
        runs = s.get("runs", 0)
        durs = s.get("durations", [])
        return {
            "runs": runs,
            "success_rate": round(s["succeeded"] / runs, 2) if runs else None,
            "avg_score": round(s["score_sum"] / runs, 1) if runs else None,
            "repair_rate": round(s["repair_runs"] / runs, 2) if runs else None,
            "out_of_scope_rate": round(s["oos_runs"] / runs, 2) if runs else None,
            "blocked_rate": round(s["blocked_runs"] / runs, 2) if runs else None,
            "avg_tokens": round(s["total_tokens"] / runs) if runs else None,
            "median_duration_sec": sorted(durs)[len(durs) // 2] if durs else None,
            "last_tasks": s.get("last_tasks", [])[:5],
        }
