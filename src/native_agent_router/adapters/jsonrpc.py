"""Line-delimited JSON-RPC stdio process plumbing.

Shared by the ZCode app-server adapter (no `jsonrpc` envelope, server->client
requests must be answered) and the generic ACP adapter (JSON-RPC 2.0 over
stdio). Provides request/response futures, notification dispatch, a raw
protocol log for evidence, and clean process teardown.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from concurrent import futures
from pathlib import Path

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ProcessDied(Exception):
    pass


class JsonRpcProcess:
    def __init__(self, argv: list[str], cwd: str, raw_log: Path | None = None, env: dict | None = None):
        self.argv = argv
        self.proc: subprocess.Popen | None = None
        self._pending: dict = {}
        self._id_lock = threading.Lock()
        self._next_id = 1
        self._write_lock = threading.Lock()
        self.on_request = None      # callable(method, params, msg_id) -> result dict
        self.on_notification = None  # callable(method, params)
        self._raw_log = raw_log
        if raw_log:
            raw_log.parent.mkdir(parents=True, exist_ok=True)
            self._raw_fh = open(raw_log, "a", encoding="utf-8")
        else:
            self._raw_fh = None
        self.exit_code: int | None = None
        self._start(cwd, env)

    def _start(self, cwd: str, env: dict | None):
        full_env = None
        if env:
            import os

            full_env = os.environ.copy()
            full_env.update(env)
        self.proc = subprocess.Popen(
            self.argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            bufsize=1, env=full_env, creationflags=CREATE_NO_WINDOW,
        )
        self._t_out = threading.Thread(target=self._reader, daemon=True)
        self._t_err = threading.Thread(target=self._stderr_pump, daemon=True)
        self._t_out.start()
        self._t_err.start()

    def _log_raw(self, direction: str, obj):
        if self._raw_fh:
            try:
                self._raw_fh.write(json.dumps({"t": time.time(), "dir": direction, "msg": obj}, ensure_ascii=False) + "\n")
                self._raw_fh.flush()
            except (OSError, ValueError):
                pass

    def _reader(self):
        proc = self.proc
        assert proc and proc.stdout
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self._log_raw("in-bad", line[:2000])
                continue
            self._log_raw("in", msg)
            if not isinstance(msg, dict):
                continue
            mid = msg.get("id")
            method = msg.get("method")
            if mid is not None and method is None:
                fut = self._pending.pop(mid, None)
                if fut and not fut.done():
                    if "error" in msg and msg["error"] is not None:
                        fut.set_exception(RpcError(msg["error"].get("code"), msg["error"].get("message"), msg["error"].get("data")))
                    else:
                        fut.set_result(msg.get("result"))
            elif mid is not None and method is not None:
                if self.on_request:
                    try:
                        result = self.on_request(method, msg.get("params") or {}, mid)
                        self._send({"id": mid, "result": result if result is not None else {}})
                    except LookupError as e:
                        self._send({"id": mid, "error": {"code": -32601, "message": f"method not supported: {e}"}})
                    except Exception as e:
                        self._send({"id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}})
                else:
                    self._send({"id": mid, "error": {"code": -32601, "message": f"method not supported: {method}"}})
            elif method is not None:
                if self.on_notification:
                    try:
                        self.on_notification(method, msg.get("params") or {})
                    except Exception:
                        pass
        self.exit_code = proc.poll()
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ProcessDied(f"agent process exited (code {self.exit_code})"))
        self._pending.clear()

    def _stderr_pump(self):
        proc = self.proc
        assert proc and proc.stderr
        log = None
        if self._raw_log:
            log = open(str(self._raw_log) + ".stderr", "a", encoding="utf-8")
        try:
            for line in proc.stderr:
                if log:
                    log.write(line)
                    log.flush()
        except (OSError, ValueError):
            pass
        finally:
            if log:
                log.close()

    def _send(self, obj: dict):
        if not self.proc or not self.proc.stdin or self.proc.poll() is not None:
            raise ProcessDied("agent process is not running")
        line = json.dumps(obj, ensure_ascii=False)
        self._log_raw("out", obj)
        with self._write_lock:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 30.0):
        mid, fut = self.request_future(method, params)
        try:
            return fut.result(timeout=timeout)
        except futures.TimeoutError:
            self._pending.pop(mid, None)
            raise TimeoutError(f"no response to {method} within {timeout}s")

    def request_future(self, method: str, params: dict | None = None):
        """Send a request and return (id, Future) without waiting. The future
        is resolved whenever the response arrives — used to keep observing a
        turn after a first bounded wait expired (no hidden deadline caps)."""
        with self._id_lock:
            mid = self._next_id
            self._next_id += 1
        fut: futures.Future = futures.Future()
        self._pending[mid] = fut
        self._send({"id": mid, "method": method, "params": params or {}})
        return mid, fut

    def notify(self, method: str, params: dict | None = None):
        self._send({"method": method, "params": params or {}})

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self, grace: float = 5.0):
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        if self._raw_fh:
            self._raw_fh.close()
            self._raw_fh = None


class RpcError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(f"rpc error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data
