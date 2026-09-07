"""Configuration loading, per-agent discovery and command resolution.

Config lookup order:
  1. explicit path (nar --config / NAR_CONFIG env)
  2. <home>/agents.json   (home = $NAR_HOME or ~/.native-agent-router)
  3. built-in defaults + auto-discovery
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

DEFAULT_SETTINGS = {
    "version": 1,
    "wait_default_sec": 120,
    "wait_max_sec": 3600,
    "timeout_default_sec": 1800,
    "verify_timeout_sec": 600,
    "max_result_chars": 12000,
    "log_tail_chars": 4000,
    "repair_default": True,
    "verify_allow_shell": False,
    "require_git_baseline": False,
}

AGENT_DEFAULTS = {
    "enabled": True,
    "adapter": None,
    "command": None,
    "cwd_arg": None,
    "default_mode": None,
    "modes": None,
    "default_permission_policy": "deny",
    "env": None,
    "credentials": "auto",
    "zcode_home": None,
    "model": None,
    "provider": None,
    "model_args": None,
    "thought_level": None,
    "tool_allowlist": None,
}

ACP_PRESETS = {
    "opencode": ["opencode", "acp"],
    "gemini": ["gemini", "--experimental-acp"],
}


def home_dir() -> Path:
    return Path(os.environ.get("NAR_HOME", str(Path.home() / ".native-agent-router")))


def _which(name: str):
    for cand in (name, f"{name}.cmd", f"{name}.exe", f"{name}.bat"):
        p = shutil.which(cand)
        if p:
            return p
    return None


def _registry_install_location(display_name: str):
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            base = winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")
        except OSError:
            continue
        with base:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(base, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(base, sub) as sk:
                        name, _ = winreg.QueryValueEx(sk, "DisplayName")
                        if isinstance(name, str) and display_name.lower() in name.lower():
                            loc, _ = winreg.QueryValueEx(sk, "InstallLocation")
                            if loc:
                                return loc
                except OSError:
                    continue
    return None


def find_zcode_cjs() -> str | None:
    env = os.environ.get("ZCODE_BIN")
    if env and Path(env).is_file():
        return str(Path(env).resolve())
    on_path = _which("zcode")
    if on_path:
        return on_path
    roots = []
    loc = _registry_install_location("zcode")
    if loc:
        roots.append(Path(loc))
    import string

    for var in ("LOCALAPPDATA", "PROGRAMFILES", "ProgramFiles(x86)", "APPDATA"):
        base = os.environ.get(var)
        if base:
            roots.append(Path(base) / "Programs" / "ZCode")
            roots.append(Path(base) / "ZCode")
    for letter in string.ascii_uppercase:
        roots.append(Path(f"{letter}:/Program Files/ZCode"))
        roots.append(Path(f"{letter}:/Program Files (x86)/ZCode"))
    for root in roots:
        cand = root / "resources" / "glm" / "zcode.cjs"
        if cand.is_file():
            return str(cand.resolve())
        direct = root / "zcode.cjs"
        if root.name.lower() == "glm" and direct.is_file():
            return str(direct.resolve())
    return None


def find_node() -> str | None:
    return _which("node")


def find_version(argv: list[str], timeout: float = 20.0) -> str | None:
    import subprocess

    try:
        r = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        line = line.strip()
        if line:
            return line[:60]
    return None


class Config:
    def __init__(self, data: dict, config_path: Path | None, home: Path):
        self.raw = data
        self.config_path = config_path
        self.home = home
        self.settings = dict(DEFAULT_SETTINGS)
        for k in DEFAULT_SETTINGS:
            if k in data:
                self.settings[k] = data[k]
        self.agents_raw: dict[str, dict] = {}
        for k, v in (data.get("agents") or {}).items():
            spec = dict(AGENT_DEFAULTS)
            spec.update(v or {})
            self.agents_raw[k] = spec
        if not self.agents_raw:
            for name, preset in ACP_PRESETS.items():
                if _which(preset[0]):
                    self.agents_raw[name] = {**AGENT_DEFAULTS, "adapter": "acp-generic"}
            cjs = find_zcode_cjs()
            if cjs:
                self.agents_raw["zcode"] = {**AGENT_DEFAULTS, "adapter": "zcode-native"}

    def agent_ids(self) -> list[str]:
        return sorted(self.agents_raw.keys())

    def agent_spec(self, agent_id: str) -> dict:
        spec = self.agents_raw.get(agent_id)
        if not spec:
            raise KeyError(f"unknown agent_id '{agent_id}'; configured: {self.agent_ids()}")
        if not spec.get("enabled", True):
            raise KeyError(f"agent '{agent_id}' is disabled in config")
        adapter = spec.get("adapter")
        if not adapter:
            if agent_id == "zcode":
                adapter = "zcode-native"
            elif agent_id in ACP_PRESETS:
                adapter = "acp-generic"
            else:
                raise KeyError(f"agent '{agent_id}' has no adapter; set \"adapter\" in config")
        resolved = {
            "agent_id": agent_id,
            "adapter": adapter,
            "cwd_arg": spec.get("cwd_arg"),
            "default_mode": spec.get("default_mode"),
            "modes": spec.get("modes"),
            "default_permission_policy": spec.get("default_permission_policy", "deny"),
            "env": spec.get("env") or {},
            "credentials": spec.get("credentials", "auto"),
            "zcode_home": spec.get("zcode_home"),
            "model": spec.get("model"),
            "provider": spec.get("provider"),
            "thought_level": spec.get("thought_level"),
            "tool_allowlist": spec.get("tool_allowlist"),
            "discovery": {},
            "scenario": spec.get("scenario"),
            "scenario_path": spec.get("scenario_path"),
        }
        if adapter == "zcode-native":
            cjs = find_zcode_cjs()
            node = find_node()
            resolved["discovery"] = {"zcode_cjs": cjs, "node": node}
            cmd = spec.get("command")
            if cmd:
                argv = [c.format(node=node or "node", zcode_cjs=cjs or "") for c in cmd]
            else:
                if not cjs:
                    raise KeyError(
                        "zcode CLI not found. Install the ZCode desktop app, put 'zcode' on PATH, "
                        'or set env ZCODE_BIN to resources/glm/zcode.cjs (see README "Configure ZCode").'
                    )
                if not node:
                    raise KeyError("Node.js >= 22 is required to run the ZCode CLI (see README).")
                argv = [node, cjs, "app-server"]
            resolved["argv"] = argv
        elif adapter == "acp-generic":
            cmd = spec.get("command") or ACP_PRESETS.get(agent_id)
            if not cmd:
                raise KeyError(f'agent \'{agent_id}\': acp-generic adapter requires "command" in config')
            argv = list(cmd)
            # Windows: npm shims are .cmd files; resolve the first token on PATH
            if argv and not Path(argv[0]).is_absolute():
                resolved0 = _which(argv[0])
                if resolved0:
                    argv[0] = resolved0
            model = spec.get("model")
            model_args = spec.get("model_args")
            if model and model_args:
                argv += [str(a).replace("{model}", model) for a in model_args]
            resolved["argv"] = argv
        elif adapter == "stub":
            resolved["argv"] = []
        else:
            raise KeyError(f"agent '{agent_id}': unknown adapter '{adapter}'")
        return resolved


def load_config(explicit: str | None = None, home: Path | None = None) -> Config:
    home = home or home_dir()
    path: Path | None = None
    if explicit:
        path = Path(explicit)
    elif os.environ.get("NAR_CONFIG"):
        path = Path(os.environ["NAR_CONFIG"])
    else:
        default = home / "agents.json"
        if default.is_file():
            path = default
    data: dict = {}
    if path and path.is_file():
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError(f"config {path} must be a JSON object")
    return Config(data, path if path and path.is_file() else None, home)
