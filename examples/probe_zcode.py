"""Probe a ZCode app-server without any model call: verify the process starts,
handshake works and session/create is answerable. Useful before wiring nar.

Usage:
  python probe_zcode.py [zcode.cjs] [workspace] [--provider builtin:xxx]

--provider reads ~/.zcode/v2/config.json, picks that provider and injects
ZCODE_MODEL/ZCODE_BASE_URL/ANTHROPIC_API_KEY into the subprocess env only
(never printed, never stored).
Exit code 0 = protocol reachable.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time


def build_env_from_v2(provider_id):
    v2 = json.load(open(os.path.expanduser(r"~\.zcode\v2\config.json"), encoding="utf-8"))
    provs = v2.get("provider") or {}
    if provider_id:
        prov = provs[provider_id]
    else:
        enabled = {k: v for k, v in provs.items() if v.get("enabled")}
        cache = {}
        try:
            c = json.load(open(os.path.expanduser(r"~\.zcode\v2\coding-plan-cache.json"), encoding="utf-8"))
            for k, v in (c.get("entryStatus", {}).get("items") or {}).items():
                if v.get("status") == "available":
                    cache[k] = True
        except (OSError, ValueError):
            pass
        pick = next((k for k in enabled if cache.get(k)), None) or next(iter(enabled), None)
        if not pick:
            raise SystemExit("no enabled provider in v2 config")
        provider_id, prov = pick, enabled[pick]
    opts = prov.get("options") or {}
    env = {}
    env["ZCODE_MODEL"] = next(iter(prov.get("models") or {}), "GLM-5.3")
    env["ZCODE_BASE_URL"] = opts.get("baseURL", "")
    if opts.get("apiKey"):
        env["ANTHROPIC_API_KEY"] = opts["apiKey"]
    return provider_id, env


def main():
    args = [a for a in sys.argv[1:]]
    provider = None
    if "--provider" in args:
        i = args.index("--provider")
        provider = args[i + 1]
        del args[i:i + 2]
    cjs = args[0] if args else "zcode"
    ws = args[1] if len(args) > 1 else "."

    env = os.environ.copy()
    if provider is not None or not os.path.expanduser(r"~\.zcode\cli\config.json"):
        pid, inject = build_env_from_v2(provider)
        env.update(inject)
        print(f"credentials: injected from v2 config provider={pid} (values hidden)", flush=True)
    else:
        print("credentials: using existing cli config", flush=True)

    argv = (["node", cjs] if cjs.endswith(".cjs") else [cjs]) + ["app-server"]
    print(f"spawn: {argv} (cwd={ws})", flush=True)
    p = subprocess.Popen(argv, cwd=ws, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1, env=env)
    q = queue.Queue()
    threading.Thread(target=lambda: [q.put(l) for l in p.stdout], daemon=True).start()

    def request(rid, method, params, timeout=60):
        p.stdin.write(json.dumps({"id": rid, "method": method, "params": params}) + "\n")
        p.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = q.get(timeout=max(deadline - time.time(), 0.1))
            except queue.Empty:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") is not None and msg.get("method"):
                if msg["method"] == "session/requestRuntimePreferences":
                    p.stdin.write(json.dumps({"id": msg["id"], "result": {"nativeSearchEnhancementsEnabled": False}}) + "\n")
                    p.stdin.flush()
                    print("answered session/requestRuntimePreferences", flush=True)
                continue
            if msg.get("id") == rid:
                return msg
            print("event:", json.dumps(msg, ensure_ascii=False)[:240], flush=True)
        raise TimeoutError(method)

    try:
        r = request(1, "session/create", {"workspace": {"workspacePath": ws, "workspaceKey": ws}, "mode": "plan"}, 45)
        print("session/create ->", json.dumps(r, ensure_ascii=False)[:300], flush=True)
        sid = (r.get("result") or {}).get("session", {}).get("sessionId")
        if sid:
            r2 = request(2, "session/read", {"sessionId": sid})
            print("session/read   ->", json.dumps(r2, ensure_ascii=False)[:300], flush=True)
            print("PROBE OK, native session:", sid, flush=True)
    finally:
        try:
            p.terminate()
        except OSError:
            pass
    sys.exit(0 if sid else 1)


if __name__ == "__main__":
    main()
