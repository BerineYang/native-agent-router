"""nar doctor: environment diagnostics + agent discovery."""

from __future__ import annotations

import re
import shutil
import sys

from . import __version__
from .config import find_node, find_version, find_zcode_cjs, load_config


def _status(ok: bool) -> str:
    return "[OK]" if ok else "[X]"


def _major(version: str | None) -> int | None:
    if not version:
        return None
    match = re.search(r"(?:^|\s)v?(\d+)(?:\.|$)", version)
    return int(match.group(1)) if match else None


def run_doctor(config_path: str | None = None):
    print(f"[i] native-agent-router {__version__}")
    python_ok = sys.version_info >= (3, 10)
    print(f"{_status(python_ok)} python: {sys.version.split()[0]} at {sys.executable}")
    if not python_ok:
        print("[i]   fix: install Python 3.10 or newer.")

    node = find_node()
    node_version = find_version([node, "--version"]) if node else None
    node_major = _major(node_version)
    node_ok = bool(node and node_major is not None and node_major >= 22)
    if node:
        print(f"{_status(node_ok)} node: {node} ({node_version or 'version unknown'})")
        if not node_ok:
            print("[i]   fix: install Node.js 22 or newer for the zcode-native adapter.")
    else:
        print("[X] node: NOT FOUND (Node.js >= 22 is needed by zcode-native)")
        print("[i]   fix: install Node.js 22 or newer and restart the terminal.")

    git = shutil.which("git")
    print(f"{_status(bool(git))} git: {git or 'NOT FOUND (needed for diff auditing and verification baselines)'}")
    if not git:
        print("[i]   fix: install Git and restart the terminal.")

    cfg = load_config(config_path)
    if cfg.config_path:
        print(f"[OK] config: {cfg.config_path}")
    else:
        print(f"[i] config: none found; using auto-discovery defaults. Create one at {cfg.home / 'agents.json'}"
              " (see config.example.json / README 'Configuration').")
    print(f"[i] home: {cfg.home}")

    cjs = find_zcode_cjs()
    if cjs:
        print(f"[OK] zcode CLI: {cjs}")
        if node:
            version = find_version([node, cjs, "--version"])
            print(f"{_status(bool(version))} zcode version: {version or 'unknown (failed to run)'}")
            protocol_ok = bool(version and "0.16" in version)
            protocol = "0.16 app-server supported" if protocol_ok else "not verified; adapter targets 0.16.x"
            print(f"{_status(protocol_ok)} zcode protocol: {protocol}")
        print("[i]   use: agents.json -> {\"agents\": {\"zcode\": {\"adapter\": \"zcode-native\"}}}")
    else:
        print("[X] zcode CLI: NOT FOUND")
        print("[i]   fix: install the ZCode desktop app, put 'zcode' on PATH, or set ZCODE_BIN")
        print("[i]        to the app's resources/glm/zcode.cjs, or set agents.json command explicitly.")

    for agent_id in cfg.agent_ids():
        try:
            spec = cfg.agent_spec(agent_id)
            argv = spec.get("argv") or []
            binary = argv[0] if argv else None
            available = bool(binary and shutil.which(binary))
            print(f"{_status(available)} agent '{agent_id}': adapter={spec['adapter']} "
                  f"argv={argv} binary_available={available}")
        except Exception as exc:
            print(f"[X] agent '{agent_id}': NOT USABLE - {exc}")
    print("[i] done. [X] items require attention only when you use the related adapter.")
    print("[i] See README.md 'Configuration' for the full agents.json reference.")
