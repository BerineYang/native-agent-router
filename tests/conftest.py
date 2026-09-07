import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from native_agent_router.config import Config  # noqa: E402
from native_agent_router.kernel.kernel import Kernel  # noqa: E402
from native_agent_router.kernel.store import TaskStore, atomic_write_json  # noqa: E402

STUBS = Path(__file__).parent / "stubs"


def make_kernel(home: Path, agents: dict) -> Kernel:
    return Kernel(Config({"version": 1, "agents": agents}, None, home), TaskStore(home))


def stub_agent(scenario: list) -> dict:
    return {"adapter": "stub", "scenario": scenario}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("NAR_HOME", str(h))
    return h


@pytest.fixture()
def git_ws(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text("print('v1')\n", encoding="utf-8")

    def git(*args):
        subprocess.run(["git", *args], cwd=ws, check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    return ws


@pytest.fixture()
def plain_ws(tmp_path):
    ws = tmp_path / "plain"
    ws.mkdir()
    return ws


def wait_terminal(k: Kernel, task_id: str, timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    snap = {}
    while time.time() < deadline:
        snap = k.wait(task_id, timeout_sec=1)
        if snap["status"] in ("succeeded", "failed", "cancelled", "blocked", "budget_exceeded"):
            return snap
    return snap
