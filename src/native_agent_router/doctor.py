"""nar doctor: environment diagnostics + agent discovery. Teaches users what
to install or configure by printing concrete, machine-specific hints."""

from __future__ import annotations

import shutil
import sys

from . import __version__
from .config import find_node, find_version, find_zcode_cjs, load_config


def run_doctor(config_path: str | None = None):
    print(f"native-agent-router {__version__}")
    print(f"python: {sys.version.split()[0]} at {sys.executable}")
    node = find_node()
    print(f"node: {node or 'NOT FOUND (needed by zcode-native; >= 22)'}")
    git = shutil.which("git")
    print(f"git: {git or 'NOT FOUND (needed for diff auditing and verification baselines)'}")

    cfg = load_config(config_path)
    if cfg.config_path:
        print(f"config: {cfg.config_path}")
    else:
        print(f"config: none found; using auto-discovery defaults. Create one at {cfg.home / 'agents.json'}"
              " (see config.example.json / README 'Configuration').")
    print(f"home:   {cfg.home}")

    cjs = find_zcode_cjs()
    if cjs:
        print(f"zcode CLI: {cjs}")
        if node:
            v = find_version([node, cjs, "--version"])
            print(f"zcode version: {v or 'unknown (failed to run)'}")
            ok = v and "0.16" in v
            print(f"  protocol 0.16 app-server: {'supported' if ok else 'NOT verified - adapter targets 0.16.x'}")
        print("  how to use: agents.json -> {\"agents\": {\"zcode\": {\"adapter\": \"zcode-native\"}}}")
    else:
        print("zcode CLI: NOT FOUND")
        print("  fix: install the ZCode desktop app, or put 'zcode' on PATH, or set env ZCODE_BIN")
        print("       to the app's resources/glm/zcode.cjs, or set agents.json command explicitly.")

    for aid in cfg.agent_ids():
        try:
            spec = cfg.agent_spec(aid)
            argv = spec.get("argv") or []
            on_path = bool(shutil.which(argv[0])) if argv else False
            print(f"agent '{aid}': adapter={spec['adapter']} argv={argv} binary_on_path={on_path}")
        except Exception as e:
            print(f"agent '{aid}': NOT USABLE - {e}")
    print("done. See README.md 'Configuration' for the full agents.json reference.")
