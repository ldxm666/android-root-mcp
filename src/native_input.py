"""Client for the on-device native input daemon (root, /dev/input devnode route).

Why this exists: ``adb shell input tap`` costs 95-114 ms because the ``input``
binary starts its own JVM every time. A resident daemon writing packed
``input_event`` structs straight to the touchscreen node does the same work with
~6 ms of overhead. Measured on the target device: tap 37.7 ms, a 300 ms swipe
completes in 306 ms, key 10.1 ms.

Everything here is optional and failure-tolerant. If the daemon is absent, dead,
or the ROM blocks the devnode, :meth:`available` returns False and the caller
falls back to its previous transport. No method may block indefinitely: the
server serialises every tool behind one global lock, so a hung call would freeze
all 37 tools behind it.

The daemon speaks one JSON object per line over TCP on loopback, reached through
``adb forward``. Protocol: ``ping | tap x y | long_press x y ms | swipe x1 y1 x2
y2 ms | gesture [[x,y],...] ms | pinch ... | key <code> | text <b64> | quit``.
``text`` is known-broken on this ROM (HyperOS rejects key injection and the
devnode carries no keyboard codes), so this client does not send it.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from typing import Optional

_DEFAULT_DEVICE_PORT = 47810
_START_DIR = "/data/local/tmp/pfdaemon"
_LOCK = threading.Lock()
_SINGLETON: Optional["NativeInput"] = None


class NativeInputError(RuntimeError):
    """Raised when the daemon is reachable but rejected or broke a request."""


class NativeInput:
    """Lazily-connected client. One instance per process."""

    def __init__(self, adb_bin: str, serial: Optional[str],
                 device_port: int = _DEFAULT_DEVICE_PORT, connect_timeout: float = 2.0) -> None:
        self.adb_bin = adb_bin
        self.serial = serial
        self.device_port = int(device_port)
        self.connect_timeout = float(connect_timeout)
        self._sock: Optional[socket.socket] = None
        self._host_port: Optional[int] = None
        self._dead = True
        self._attempted = 0.0

    # -- discovery ---------------------------------------------------------
    def _adb(self, args: list[str], timeout: float = 8.0) -> tuple[int, str]:
        argv = [self.adb_bin] + (["-s", self.serial] if self.serial else []) + args
        try:
            done = subprocess.run(argv, capture_output=True, timeout=timeout, check=False,
                                  shell=False,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            return 1, ""
        return done.returncode, done.stdout.decode("utf-8", "replace")

    def _forward(self) -> Optional[int]:
        """Install a fresh host->device forward and return the chosen host port."""
        code, out = self._adb(["forward", "tcp:0", f"tcp:{self.device_port}"])
        if code != 0:
            return None
        text = out.strip().splitlines()[-1] if out.strip() else ""
        try:
            return int(text)
        except ValueError:
            return None

    def _open(self) -> bool:
        port = self._forward()
        if port is None:
            return False
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=self.connect_timeout)
        except OSError:
            return False
        sock.settimeout(self.connect_timeout)
        self._sock, self._host_port = sock, port
        reply = self._call("ping", timeout=self.connect_timeout)
        if not reply.get("ok"):
            self.close()
            return False
        self._dead = False
        return True

    def start_daemon(self, jar_glob: str = "pfdaemon*.jar") -> bool:
        """Best-effort start. Returns True only if a subsequent ping answers."""
        code, out = self._adb(["shell", "su", "-c",
                               f"ls {_START_DIR}/{jar_glob} 2>/dev/null | head -1"])
        jar = out.strip()
        if code != 0 or not jar:
            return False
        launch = (
            f"cd {_START_DIR} && CLASSPATH={jar} setsid app_process /system/bin "
            f"com.pf.Daemon --port {self.device_port} >{_START_DIR}/pfdaemon.log 2>&1 </dev/null &"
        )
        self._adb(["shell", "su", "-c", launch], timeout=10)
        for _ in range(12):
            time.sleep(0.25)
            if self._open():
                return True
        return False

    # -- transport ---------------------------------------------------------
    def _call(self, line: str, timeout: float = 6.0) -> dict:
        sock = self._sock
        if sock is None:
            raise NativeInputError("native input daemon is not connected")
        try:
            sock.settimeout(max(0.05, timeout))
            sock.sendall((line + "\n").encode("utf-8"))
            chunks = bytearray()
            deadline = time.monotonic() + max(0.05, timeout)
            while time.monotonic() < deadline:
                chunk = sock.recv(4096)
                if not chunk:
                    raise NativeInputError("daemon closed the connection")
                chunks.extend(chunk)
                if b"\n" in chunks:
                    break
        except (OSError, socket.timeout) as exc:
            self._dead = True
            raise NativeInputError(f"daemon transport failed: {exc}") from exc
        payload = bytes(chunks).split(b"\n", 1)[0].decode("utf-8", "replace").strip()
        try:
            reply = json.loads(payload)
        except (ValueError, TypeError) as exc:
            self._dead = True
            raise NativeInputError(f"daemon sent unparseable reply: {payload[:120]!r}") from exc
        if not isinstance(reply, dict):
            raise NativeInputError("daemon reply was not an object")
        return reply

    # -- public ------------------------------------------------------------
    def available(self) -> bool:
        """Cheap, non-raising readiness check. Never blocks longer than ~2 s."""
        if self._sock is not None and not self._dead:
            return True
        now = time.monotonic()
        if now - self._attempted < 5.0:      # back off so a missing daemon is cheap
            return False
        self._attempted = now
        with _LOCK:
            if self._sock is not None and not self._dead:
                return True
            return self._open()

    def health(self) -> dict:
        if not self.available():
            return {"ok": False, "connected": False}
        try:
            reply = self._call("ping", timeout=3)
        except NativeInputError as exc:
            return {"ok": False, "connected": False, "error": str(exc)}
        reply["connected"] = True
        reply["host_port"] = self._host_port
        return reply

    def tap(self, x: int, y: int, timeout: float = 6.0) -> dict:
        return self._require_ok(self._call(f"tap {int(x)} {int(y)}", timeout))

    def long_press(self, x: int, y: int, ms: int = 600, timeout: float = 8.0) -> dict:
        return self._require_ok(self._call(f"long_press {int(x)} {int(y)} {int(ms)}", timeout))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300,
              timeout: Optional[float] = None) -> dict:
        budget = timeout if timeout is not None else duration_ms / 1000.0 + 6.0
        return self._require_ok(self._call(
            f"swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}", budget))

    def gesture(self, points: list[tuple[int, int]], duration_ms: int = 300,
                timeout: Optional[float] = None) -> dict:
        budget = timeout if timeout is not None else duration_ms / 1000.0 + 6.0
        encoded = json.dumps([[int(a), int(b)] for a, b in points])
        return self._require_ok(self._call(f"gesture {encoded} {int(duration_ms)}", budget))

    def key(self, code: int | str, timeout: float = 5.0) -> dict:
        return self._require_ok(self._call(f"key {code}", timeout))

    @staticmethod
    def _require_ok(reply: dict) -> dict:
        if not reply.get("ok"):
            raise NativeInputError(str(reply.get("err") or "daemon rejected the request"))
        return reply

    def close(self) -> None:
        sock, self._sock = self._sock, None
        self._dead = True
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def instance(adb_bin: str, serial: Optional[str]) -> NativeInput:
    """Process-wide singleton bound to the first adb binary/serial seen."""
    global _SINGLETON
    with _LOCK:
        if _SINGLETON is None or _SINGLETON.adb_bin != adb_bin or _SINGLETON.serial != serial:
            if _SINGLETON is not None:
                _SINGLETON.close()
            _SINGLETON = NativeInput(adb_bin, serial)
        return _SINGLETON


def reset() -> None:
    """Drop the connection (device switch / shutdown)."""
    global _SINGLETON
    with _LOCK:
        if _SINGLETON is not None:
            _SINGLETON.close()
        _SINGLETON = None


def enabled() -> bool:
    """Kill switch, default ON: the daemon path is opt-out, not opt-in."""
    return os.environ.get("ANDROID_MCP_NATIVE_INPUT", "1").strip().lower() not in (
        "0", "off", "false", "no")
