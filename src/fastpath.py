"""Persistent, low-overhead fast path for Android control.

Why this exists: the stock server spawns one ``adb.exe`` per action. A trivial
``shell_execute`` costs ~122 ms and ``get_device_status`` ~778 ms, almost all of
it process creation. This module keeps the transports open instead:

* **One long-lived ``adb shell`` process** per privilege level. Commands are
  framed with a unique sentinel so N commands can be pipelined in a single
  write. No host process is created per action.
* **uiautomator2's HTTP server**, spoken over JSON-RPC 2.0 on an
  ``adb forward tcp:0`` port. No third-party client, so no version coupling.
  On this device the server is ``com.wetest.uia2.Main`` (jsonrpc4j), which takes
  **positional** params (``params: [...]``) - object params are rejected with
  ``-32602``. The method set is narrower than the upstream Python client
  suggests; :meth:`FastPath.health` reports what actually answered.
* **``adb exec-out``** for binary payloads only (raw ``screencap``). One spawn,
  no temp files, no PNG on either side.

Every public method takes a ``timeout`` and raises :class:`FastPathTimeout`
instead of blocking, because the MCP server serialises tools behind one global
lock - a hang here freezes everything. Locks are per-shell and are never held
across a caller-visible wait.

Importable on its own; no imports from the rest of the package::

    python src/fastpath.py --bench        # self-benchmark against the device
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Callable, Sequence

__all__ = ["FastPath", "FastPathTimeout", "get", "warm"]

__version__ = "1.0.0"

#: Prefix for every on-device temp file. Never a fixed name: concurrent tools
#: share /data/local/tmp and a fixed path would race.
_TMP_PREFIX = "/data/local/tmp/.dsh_fp_"

#: Ports the uiautomator2 server is known to listen on, most likely first. The
#: device is also asked which port is actually open, so this is an ordering hint
#: that keeps discovery to one forward attempt in the common case.
_U2_PORT_HINTS = (9008, 7912, 6790, 4444, 8888, 5555)

#: Never try more forwards than this: each miss costs a forward plus a refused
#: connection, and a pathological port list must not become a hang.
_U2_MAX_CANDIDATES = 12

_KEYCODES = {
    "HOME": 3,
    "BACK": 4,
    "CALL": 5,
    "ENDCALL": 6,
    "0": 7, "1": 8, "2": 9, "3": 10, "4": 11,
    "5": 12, "6": 13, "7": 14, "8": 15, "9": 16,
    "STAR": 17, "POUND": 18,
    "DPAD_UP": 19, "DPAD_DOWN": 20, "DPAD_LEFT": 21, "DPAD_RIGHT": 22,
    "DPAD_CENTER": 23, "VOLUME_UP": 24, "VOLUME_DOWN": 25, "POWER": 26,
    "CAMERA": 27, "CLEAR": 28, "A": 29, "B": 30, "C": 31, "D": 32, "E": 33,
    "F": 34, "G": 35, "H": 36, "I": 37, "J": 38, "K": 39, "L": 40, "M": 41,
    "N": 42, "O": 43, "P": 44, "Q": 45, "R": 46, "S": 47, "T": 48, "U": 49,
    "V": 50, "W": 51, "X": 52, "Y": 53, "Z": 54,
    "COMMA": 55, "PERIOD": 56, "ALT_LEFT": 57, "ALT_RIGHT": 58,
    "SHIFT_LEFT": 59, "SHIFT_RIGHT": 60, "TAB": 61, "SPACE": 62, "ENTER": 66,
    "DEL": 67, "BACKSPACE": 67, "GRAVE": 68, "MINUS": 69, "EQUALS": 70,
    "LEFT_BRACKET": 71, "RIGHT_BRACKET": 72, "BACKSLASH": 73, "SEMICOLON": 74,
    "APOSTROPHE": 75, "SLASH": 76, "AT": 77, "NUM": 78, "HEADSETHOOK": 79,
    "FOCUS": 80, "PLUS": 81, "MENU": 82, "NOTIFICATION": 83, "SEARCH": 84,
    "MEDIA_PLAY_PAUSE": 85, "MEDIA_STOP": 86, "MEDIA_NEXT": 87,
    "MEDIA_PREVIOUS": 88, "MEDIA_REWIND": 89, "MEDIA_FAST_FORWARD": 90,
    "MUTE": 91, "PAGE_UP": 92, "PAGE_DOWN": 93, "PICTSYMBOLS": 94,
    "SWITCH_CHARSET": 95, "BUTTON_A": 96, "BUTTON_B": 97, "BUTTON_C": 98,
    "BUTTON_X": 99, "BUTTON_Y": 100, "BUTTON_Z": 101, "BUTTON_L1": 102,
    "BUTTON_R1": 103, "BUTTON_L2": 104, "BUTTON_R2": 105, "BUTTON_THUMBL": 106,
    "BUTTON_THUMBR": 107, "BUTTON_START": 108, "BUTTON_SELECT": 109,
    "BUTTON_MODE": 110, "ESCAPE": 111, "FORWARD_DEL": 112,
    "CTRL_LEFT": 113, "CTRL_RIGHT": 114, "CAPS_LOCK": 115, "SCROLL_LOCK": 116,
    "META_LEFT": 117, "META_RIGHT": 118, "FUNCTION": 119, "SYSRQ": 120,
    "BREAK": 121, "MOVE_HOME": 122, "MOVE_END": 123, "INSERT": 124,
    "FORWARD": 125, "MEDIA_PLAY": 126, "MEDIA_PAUSE": 127, "MEDIA_CLOSE": 128,
    "MEDIA_EJECT": 129, "MEDIA_RECORD": 130, "F1": 131, "F2": 132, "F3": 133,
    "F4": 134, "F5": 135, "F6": 136, "F7": 137, "F8": 138, "F9": 139,
    "F10": 140, "F11": 141, "F12": 142, "NUMPAD_0": 144, "NUMPAD_1": 145,
    "NUMPAD_2": 146, "NUMPAD_3": 147, "NUMPAD_4": 148, "NUMPAD_5": 149,
    "NUMPAD_6": 150, "NUMPAD_7": 151, "NUMPAD_8": 152, "NUMPAD_9": 153,
    "NUMPAD_DIVIDE": 154, "NUMPAD_MULTIPLY": 155, "NUMPAD_SUBTRACT": 156,
    "NUMPAD_ADD": 157, "NUMPAD_DOT": 158, "NUMPAD_COMMA": 159,
    "NUMPAD_ENTER": 160, "NUMPAD_EQUALS": 161, "NUMPAD_LEFT_PAREN": 162,
    "NUMPAD_RIGHT_PAREN": 163, "VOLUME_MUTE": 164, "INFO": 165,
    "CHANNEL_UP": 166, "CHANNEL_DOWN": 167, "ZOOM_IN": 168, "ZOOM_OUT": 169,
    "TV": 170, "WAKEUP": 224, "SLEEP": 223, "SOFT_SLEEP": 276,
    "ASSIST": 219, "BRIGHTNESS_UP": 221, "BRIGHTNESS_DOWN": 220,
    "MEDIA_AUDIO_TRACK": 222, "SETTINGS": 176, "APP_SWITCH": 187,
    "CUT": 277, "COPY": 278, "PASTE": 279, "SYSTEM_NAVIGATION_LEFT": 282,
    "SYSTEM_NAVIGATION_RIGHT": 283, "ALL_APPS": 284, "REFRESH": 285,
}


class FastPathTimeout(RuntimeError):
    """A fast-path operation exceeded its deadline.

    Raised instead of blocking: the MCP server holds one global lock while a
    tool runs, so a stuck pipe or an unresponsive device must surface as an
    exception the caller can fall back from.
    """


# --------------------------------------------------------------------------- #
# persistent shell
# --------------------------------------------------------------------------- #
class _Shell:
    """One long-lived ``adb shell`` process with sentinel-framed commands.

    Transport: ``adb -s <serial> shell [su -c sh]`` with piped stdio. A reader
    thread drains stdout into a bytearray (binary safe, never decoded) and a
    condition variable wakes the caller when its sentinel lands.

    Frame written for command *i*::

        { <cmd> ; } 2><errfile>; printf '\\001<tok>:<rc>\\001'; cat <errfile>; \\
            printf '\\002'; rm -f <errfile>

    so the byte order on the pipe is ``stdout … \\001tok:rc\\001 stderr \\002``.
    With ``sep_err=False`` stderr is merged with ``2>&1`` and no temp file is
    created - the hot path for gestures, where only the exit status matters.
    """

    def __init__(self, argv: Sequence[str], tag: str, root: bool) -> None:
        self.argv = list(argv)
        self.tag = tag
        self.root = root
        self._proc: subprocess.Popen[bytes] | None = None
        self._buf = bytearray()
        self._pos = 0
        self._cv = threading.Condition()
        self._io = threading.Lock()
        self._dead = True
        self._reader: threading.Thread | None = None
        self._token = uuid.uuid4().hex[:8]

    # -- lifecycle ---------------------------------------------------------
    @property
    def alive(self) -> bool:
        """Cheap liveness read for :meth:`FastPath.available` - no I/O."""
        proc = self._proc
        return not self._dead and proc is not None and proc.poll() is None

    def open(self, timeout: float = 10.0) -> None:
        """Spawn adb and wait until the shell answers ``id``/``echo``."""
        self.close()
        self._buf.clear()
        self._pos = 0
        self._proc = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._dead = False
        self._reader = threading.Thread(
            target=self._drain, name=f"fastpath-shell-{self.tag}", daemon=True
        )
        self._reader.start()
        rc, out, _err = self.run("id", timeout=timeout, sep_err=False)
        if rc != 0:
            self.close()
            raise FastPathTimeout(f"shell {self.tag!r} failed its liveness probe: {out!r}")
        if self.root and "uid=0" not in out:
            self.close()
            raise FastPathTimeout(f"shell {self.tag!r} is not root: {out.strip()!r}")

    def close(self) -> None:
        """Kill the process and wake every waiter."""
        proc, self._proc = self._proc, None
        self._dead = True
        with self._cv:
            self._cv.notify_all()
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

    # -- reader ------------------------------------------------------------
    def _drain(self) -> None:
        """Append stdout bytes to the buffer until EOF, then mark dead."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        fd = proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 1 << 16)
            except OSError:
                chunk = b""
            if not chunk:
                break
            with self._cv:
                self._buf.extend(chunk)
                self._cv.notify_all()
        with self._cv:
            self._dead = True
            self._cv.notify_all()

    # -- framing -----------------------------------------------------------
    def _frame(self, cmd: str, idx: int, sep_err: bool) -> str:
        mark = f"\\001{self._token}:%d\\001"
        if not sep_err:
            return f"{{ {cmd} ; }} 2>&1; printf '{mark}' $?; printf '\\002'\n"
        errfile = f"{_TMP_PREFIX}{self.tag}_{self._token}_{idx}.err"
        return (
            f"{{ {cmd} ; }} 2>{errfile}; printf '{mark}' $?; "
            f"cat {errfile} 2>/dev/null; printf '\\002'; rm -f {errfile}\n"
        )

    def _write(self, payload: bytes) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or self._dead:
            raise FastPathTimeout(f"shell {self.tag!r} is not running")
        fd = proc.stdin.fileno()
        view = memoryview(payload)
        while view:
            try:
                written = os.write(fd, view)
            except OSError as exc:
                raise FastPathTimeout(f"shell {self.tag!r} write failed: {exc}") from exc
            if written <= 0:
                raise FastPathTimeout(f"shell {self.tag!r} write stalled")
            view = view[written:]

    def _take(self, deadline: float, n_frames: int) -> list[tuple[int, bytes, bytes]]:
        """Wait for *n_frames* complete frames, or raise :class:`FastPathTimeout`."""
        token = self._token.encode()
        open_mark = b"\x01" + token + b":"
        results: list[tuple[int, bytes, bytes]] = []
        with self._cv:
            while len(results) < n_frames:
                while len(results) < n_frames:
                    start = self._buf.find(open_mark, self._pos)
                    if start < 0:
                        break
                    close = self._buf.find(b"\x01", start + len(open_mark))
                    if close < 0:
                        break
                    raw_rc = bytes(self._buf[start + len(open_mark):close])
                    if not raw_rc.lstrip(b"-").isdigit():
                        self._pos = start + 1
                        continue
                    tail = self._buf.find(b"\x02", close)
                    if tail < 0:
                        break
                    results.append(
                        (
                            int(raw_rc),
                            bytes(self._buf[self._pos:start]),
                            bytes(self._buf[close + 1:tail]),
                        )
                    )
                    self._pos = tail + 1
                if len(results) >= n_frames:
                    break
                if self._dead:
                    raise FastPathTimeout(f"shell {self.tag!r} died mid-command")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FastPathTimeout(f"shell {self.tag!r} timed out")
                self._cv.wait(min(remaining, 0.25))
            consumed = self._pos
            if consumed > (1 << 20):
                del self._buf[:consumed]
                self._pos = 0
        return results

    # -- public ------------------------------------------------------------
    def run(self, cmd: str, timeout: float = 20.0, sep_err: bool = True) -> tuple[int, str, str]:
        """Run one command; returns ``(rc, stdout, stderr)`` as text."""
        return self.run_many([cmd], timeout=timeout, sep_err=sep_err)[0]

    def run_many(
        self, cmds: Sequence[str], timeout: float = 20.0, sep_err: bool = True
    ) -> list[tuple[int, str, str]]:
        """Write every command in ONE write, then collect every sentinel."""
        if not cmds:
            return []
        payload = "".join(
            self._frame(cmd, i, sep_err) for i, cmd in enumerate(cmds)
        ).encode()
        deadline = time.monotonic() + max(timeout, 0.001)
        with self._io:
            self._write(payload)
            raw = self._take(deadline, len(cmds))
        out: list[tuple[int, str, str]] = []
        for rc, so, se in raw:
            out.append(
                (
                    rc,
                    so.decode("utf-8", "replace").rstrip("\r\n"),
                    se.decode("utf-8", "replace").rstrip("\r\n"),
                )
            )
        return out


# --------------------------------------------------------------------------- #
# uiautomator2 JSON-RPC client
# --------------------------------------------------------------------------- #
class _U2:
    """Minimal JSON-RPC 2.0 client for the on-device uiautomator2 server.

    Transport: ``adb forward tcp:0 tcp:<device port>`` then HTTP POST to
    ``/jsonrpc/0``. Ports are discovered, never hardcoded; the forward is
    re-created if the server restarts elsewhere. Params are positional because
    this build's jsonrpc4j rejects named params with ``-32602``.
    """

    def __init__(self, adb: str, serial: str) -> None:
        self.adb = adb
        self.serial = serial
        self.port: int | None = None
        self.device_port: int | None = None
        self._id = 0
        self._lock = threading.Lock()
        self.last_ms = 0.0

    # -- forward management ------------------------------------------------
    def _adb(self, args: Sequence[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.adb, "-s", self.serial, *args],
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def forwards(self) -> list[tuple[str, str]]:
        """Every existing ``adb forward`` mapping for this device."""
        out = self._adb(["forward", "--list"], timeout=6)
        rows = []
        for line in out.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == self.serial:
                rows.append((parts[1], parts[2]))
        return rows

    def attach(self, device_port: int, timeout: float = 6.0) -> int:
        """Create a fresh ``tcp:0`` forward to *device_port*; returns local port."""
        self.detach()
        out = self._adb(["forward", "tcp:0", f"tcp:{device_port}"], timeout=timeout)
        text = out.stdout.decode("utf-8", "replace").strip().splitlines()
        if out.returncode != 0 or not text:
            raise FastPathTimeout(f"adb forward failed: {out.stderr.decode('utf-8', 'replace')}")
        self.port = int(text[-1])
        self.device_port = device_port
        return self.port

    def detach(self) -> None:
        """Remove our forward so a restarting server can rebind cleanly."""
        if self.port is not None:
            try:
                self._adb(["forward", "--remove", f"tcp:{self.port}"], timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.port = None

    # -- transport ---------------------------------------------------------
    def call(
        self, method: str, params: Sequence[Any] | None = None, timeout: float = 8.0
    ) -> Any:
        """POST one JSON-RPC request; returns ``result`` or raises."""
        port = self.port
        if port is None:
            raise FastPathTimeout("uiautomator2 is not attached")
        with self._lock:
            self._id += 1
            rid = self._id
        body = json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": list(params or [])}
        ).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/jsonrpc/0",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
        except (urllib.error.URLError, OSError) as exc:
            raise FastPathTimeout(f"u2 {method} transport failed: {exc}") from exc
        finally:
            self.last_ms = round((time.perf_counter() - t0) * 1000, 1)
        try:
            doc = json.loads(payload)
        except ValueError as exc:
            raise FastPathTimeout(f"u2 {method} returned non-JSON") from exc
        if "error" in doc:
            err = doc["error"] or {}
            raise FastPathTimeout(
                f"u2 {method} rejected: {err.get('code')} {err.get('message')}"
            )
        return doc.get("result")

    def call_soft(
        self, method: str, params: Sequence[Any] | None = None, timeout: float = 8.0
    ) -> tuple[bool, Any]:
        """Like :meth:`call` but returns ``(ok, result)`` instead of raising."""
        try:
            return True, self.call(method, params, timeout=timeout)
        except FastPathTimeout:
            return False, None


# --------------------------------------------------------------------------- #
# the fast path
# --------------------------------------------------------------------------- #
class FastPath:
    """Lazily-initialised, thread-safe facade over the two persistent transports.

    Construction is free; the first call (or :meth:`warm`) opens the shells and
    the forward. All public methods take a ``timeout`` and never spawn a host
    process - except :meth:`exec_out`, which uses one ``adb exec-out`` for
    binary payloads, and the one-off ``adb forward`` management calls.
    """

    def __init__(
        self,
        adb: str | None = None,
        serial: str | None = None,
        u2_ports: Sequence[int] | None = None,
    ) -> None:
        self.adb = adb or os.environ.get("FASTPATH_ADB") or _find_adb()
        self.serial = serial or os.environ.get("FASTPATH_SERIAL", "")
        self.u2_ports = tuple(u2_ports or _U2_PORT_HINTS)
        self.u2 = _U2(self.adb, self.serial)
        self._shells: dict[bool, _Shell] = {}
        self._shell_lock = threading.RLock()
        self._init_lock = threading.Lock()
        self._ready = False
        self._u2_ok = False
        self._u2_ms = 0.0
        self._display = [0, 0, 0]
        self._text_mode = "unknown"
        self._gesture_route = "shell"
        self._shot_route = "none"
        self._u2_pid = 0
        self._last_shot_ms = 0.0
        self._root_argv: list[str] | None = None
        self._notes: list[str] = []

    # -- lifecycle ---------------------------------------------------------
    def _lazy(self) -> None:
        """Open everything once. Safe to call from any thread, any number of times."""
        if self._ready:
            return
        with self._init_lock:
            if self._ready:
                return
            if not self.serial:
                self.serial = _detect_serial(self.adb)
                self.u2.serial = self.serial
            self._probe_u2()
            self._ready = True

    def warm(self, timeout: float = 15.0) -> dict:
        """Pre-open both shells and the u2 forward so the first real call is fast.

        From another module::

            from fastpath import warm
            warm()

        Idempotent and safe to call at server start-up.
        """
        self._lazy()
        t0 = time.perf_counter()
        self._shell(False, timeout=timeout)
        if self._root_argv is not None:
            self._shell(True, timeout=timeout)
        else:
            self._open_root(timeout=timeout)
        if self.u2.port is not None:
            ok, info = self.u2.call_soft("deviceInfo", [], timeout=timeout)
            if ok and isinstance(info, dict):
                self._absorb_device_info(info)
        return {
            "warmed": True,
            "ms": round((time.perf_counter() - t0) * 1000, 1),
            "serial": self.serial,
            "u2_port": self.u2.port,
            "text_mode": self._text_mode,
        }

    def close(self) -> None:
        """Tear everything down; the next call re-opens lazily."""
        with self._shell_lock:
            for shell in self._shells.values():
                shell.close()
            self._shells.clear()
        self.u2.detach()
        self._ready = False

    def available(self) -> bool:
        """Cheap readiness check for a caller choosing fast path vs fallback.

        No I/O, no locks, never raises, returns in microseconds - it reads two
        flags maintained by the reader threads.
        """
        try:
            shell = self._shells.get(False)
            return bool(self._ready and shell is not None and shell.alive)
        except Exception:  # noqa: BLE001 - contract says never raise
            return False

    # -- adb discovery -----------------------------------------------------
    def _probe_u2(self) -> None:
        """Find the uiautomator2 port, forward it, and verify with a real call."""
        orders: list[int] = []
        for port in list(self.u2_ports) + self._device_u2_ports():
            if port not in orders:
                orders.append(port)
        for port in orders[:_U2_MAX_CANDIDATES]:
            try:
                self.u2.attach(port, timeout=6)
                ok, info = self.u2.call_soft("ping", [], timeout=3)
                if ok:
                    ok2, info2 = self.u2.call_soft("deviceInfo", [], timeout=6)
                    if ok2 and isinstance(info2, dict):
                        self._u2_ok = True
                        self._u2_ms = self.u2.last_ms
                        self._absorb_device_info(info2)
                        return
            except (FastPathTimeout, ValueError, OSError):
                pass
            self.u2.detach()
        self._u2_ok = False
        self._notes.append("uiautomator2 server not reachable; using shell only")

    def _device_u2_ports(self) -> list[int]:
        """Ports the device itself reports as LISTEN, u2-looking ones first."""
        found: list[int] = []
        try:
            shell = self._shell(False, timeout=8)
            rc, out, _ = shell.run(
                "ss -tlnp 2>/dev/null | grep -E 'uiautomator|wetest|atx' ; "
                "ss -tln 2>/dev/null | awk 'NR>1{print $4}' | "
                "grep -oE '[0-9]+$' | sort -u | head -40",
                timeout=8,
                sep_err=False,
            )
            if rc == 0:
                for token in re.findall(r":(\d{2,5})\b", out):
                    port = int(token)
                    if port not in found:
                        found.append(port)
        except (FastPathTimeout, OSError):
            pass
        return found

    def _absorb_device_info(self, info: dict) -> None:
        w, h = info.get("displayWidth"), info.get("displayHeight")
        rot = info.get("displayRotation", 0)
        if isinstance(w, int) and isinstance(h, int) and w and h:
            self._display = [w, h, int(rot or 0)]
        self._u2_ms = self.u2.last_ms

    # -- shells ------------------------------------------------------------
    def _shell_argv(self, root: bool) -> list[str]:
        if root:
            if self._root_argv is None:
                raise FastPathTimeout("no root shell available")
            return self._root_argv
        return [self.adb, "-s", self.serial, "shell"]

    def _open_root(self, timeout: float = 15.0) -> None:
        """Try the known ways to get a persistent root shell; keeps the first that works."""
        candidates = [
            [self.adb, "-s", self.serial, "shell", "su", "-c", "sh"],
            [self.adb, "-s", self.serial, "shell", "su", "0", "sh"],
            [self.adb, "-s", self.serial, "shell", "su"],
        ]
        for argv in candidates:
            shell = _Shell(argv, tag="root", root=True)
            try:
                shell.open(timeout=timeout)
            except (FastPathTimeout, OSError):
                shell.close()
                continue
            with self._shell_lock:
                self._shells[True] = shell
            self._root_argv = argv
            return
        self._notes.append("root shell unavailable; root=True falls back to su -c")

    def _shell(self, root: bool, timeout: float = 15.0) -> _Shell:
        """Return a live shell, opening (or repairing) it on demand."""
        with self._shell_lock:
            shell = self._shells.get(root)
        if shell is not None and shell.alive:
            return shell
        if root and self._root_argv is None:
            self._open_root(timeout=timeout)
            with self._shell_lock:
                shell = self._shells.get(True)
            if shell is None:
                raise FastPathTimeout("root shell unavailable")
            return shell
        shell = _Shell(self._shell_argv(root), tag="root" if root else "shell", root=root)
        shell.open(timeout=timeout)
        with self._shell_lock:
            old = self._shells.get(root)
            self._shells[root] = shell
        if old is not None:
            old.close()
        return shell

    def _with_retry(
        self, fn: Callable[[_Shell], Any], root: bool, timeout: float
    ) -> Any:
        """Run *fn* on the shell; on a dead shell, rebuild once and retry."""
        shell = self._shell(root, timeout=timeout)
        try:
            return fn(shell)
        except FastPathTimeout:
            if shell.alive:
                raise
            with self._shell_lock:
                if self._shells.get(root) is shell:
                    self._shells.pop(root, None)
            shell.close()
            retry = self._shell(root, timeout=timeout)
            return fn(retry)

    # -- shells: public ----------------------------------------------------
    def shell(self, cmd: str, timeout: float = 20, root: bool = False) -> tuple[int, str, str]:
        """Run one command on the persistent shell.

        Returns ``(rc, stdout, stderr)`` with trailing newlines stripped, which
        is exactly what ``transport.execute(...).text``/``.stderr`` expose, so a
        caller cannot tell which path served the request. stderr is captured
        separately, as adb's shell protocol does.
        """
        self._lazy()
        return self._with_retry(
            lambda sh: sh.run(cmd, timeout=timeout, sep_err=True), root, timeout
        )

    def _lean(self, cmd: str, timeout: float = 8) -> int:
        """Run a command with stderr merged and NO temp file, returning rc.

        Gestures are the hot path: the two extra device forks a separated-stderr
        frame costs (`cat` + `rm`) are ~10 ms each, which would be a fifth of a
        tap. Callers here only need the exit status.
        """
        self._lazy()
        rc, _out, _err = self._with_retry(
            lambda sh: sh.run(cmd, timeout=timeout, sep_err=False), False, timeout
        )
        if rc != 0:
            raise FastPathTimeout(f"{cmd.split()[0]} rc={rc}")
        return rc

    def shell_many(
        self, cmds: list[str], timeout: float = 20, root: bool = False
    ) -> list[tuple[int, str, str]]:
        """Run N commands in a SINGLE round trip.

        All frames go out in one write and all N sentinels are parsed from the
        single stream - this is the whole point of the persistent shell. A
        12-command batch lands in one device round trip instead of twelve
        process spawns.
        """
        self._lazy()
        return self._with_retry(
            lambda sh: sh.run_many(cmds, timeout=timeout, sep_err=True), root, timeout
        )

    # -- binary ------------------------------------------------------------
    def exec_out(self, args: list[str], timeout: float = 30) -> bytes:
        """Run ``adb exec-out`` and return raw bytes.

        The only place this module spawns adb per call, and only because binary
        payloads cannot ride the text shell safely. No temp files either side.
        """
        self._lazy()
        proc = subprocess.Popen(
            [self.adb, "-s", self.serial, "exec-out", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise FastPathTimeout(f"exec-out {args[0]} exceeded {timeout:g}s") from None
        if proc.returncode != 0:
            raise FastPathTimeout(
                f"exec-out {args[0]} rc={proc.returncode}: "
                f"{err.decode('utf-8', 'replace')[:200]}"
            )
        return out

    def shot(self, timeout: float = 20) -> dict:
        """Capture the screen as a RAW RGBA buffer - no PNG on either side.

        ``screencap`` without ``-p`` emits a ``(width, height, format[, colorspace])``
        little-endian header followed by the pixel buffer, so there is no encode,
        no decode and no file. The header length is derived from the payload size
        (12 bytes pre-Android 9, 16 bytes after) instead of assumed.

        Route: u2 ``screenshot`` when the server exposes it (this build does not -
        it answers ``-32601``), otherwise one ``adb exec-out screencap``.
        ``shot_route`` in the result says which one served the call. ``-p`` (PNG)
        is never used here: it costs ~900 ms more because the device zlib-compresses
        10 MB of pixels.
        """
        self._lazy()
        t0 = time.perf_counter()
        raw: bytes | None = None
        route = "exec_out_raw"
        if self._u2_ready():
            ok, data = self.u2.call_soft("screenshot", [""], timeout=timeout)
            if ok and isinstance(data, str) and len(data) > 64:
                try:
                    raw = base64.b64decode(data, validate=False)
                    route = "u2_screenshot"
                except (ValueError, TypeError):
                    raw = None
        if raw is None:
            raw = self.exec_out(["screencap"], timeout=timeout)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        self._shot_route = route
        self._last_shot_ms = ms
        if len(raw) < 12:
            raise FastPathTimeout(f"screencap returned {len(raw)} bytes")
        width = int.from_bytes(raw[0:4], "little")
        height = int.from_bytes(raw[4:8], "little")
        fmt = int.from_bytes(raw[8:12], "little")
        expect = width * height * 4
        header = len(raw) - expect
        if header not in (12, 16):
            # size does not line up: trust the 12-byte header and report the gap
            header = 12
        pixels = raw[header:]
        return {
            "w": width,
            "h": height,
            "fmt": "rgba",
            "format_code": fmt,
            "header": header,
            "bytes": len(pixels),
            "expected": expect,
            "raw": pixels,
            "ms": ms,
            "route": route,
        }

    # -- gestures ----------------------------------------------------------
    def _u2_ready(self) -> bool:
        return self._u2_ok and self.u2.port is not None

    def set_gesture_route(self, route: str) -> str:
        """Choose the gesture transport: ``'shell'``, ``'u2'`` or ``'auto'``.

        ``'auto'`` keeps the shell (measured faster on this device: the u2
        ``click`` RPC settles for ~120 ms after injecting, while a warm-shell
        ``input tap`` is ~52 ms) and falls back to u2 when the shell errors.
        """
        route = route.lower()
        if route not in ("shell", "u2", "auto"):
            raise ValueError("route must be 'shell', 'u2' or 'auto'")
        self._gesture_route = "shell" if route == "auto" else route
        return self._gesture_route

    def tap(self, x: int, y: int, timeout: float = 8) -> None:
        """Tap at absolute pixels.

        Route ``shell`` (default): ``input tap`` on the warm shell - one device
        fork pair, no host spawn, ~52 ms measured. Route ``u2``: the ``click``
        RPC in the resident uiautomator process, ~170 ms measured because it
        waits for the window to settle after injecting.
        """
        self._lazy()
        if self._gesture_route == "u2" and self._u2_ready():
            try:
                self.u2.call("click", [x, y], timeout=timeout)
                return
            except FastPathTimeout:
                pass
        self._lean(f"input tap {x} {y}", timeout=timeout)

    def long_press(self, x: int, y: int, ms: int = 600, timeout: float = 10) -> None:
        """Press and hold for ``ms``.

        ``input swipe`` with an identical start and end is the only press-and-hold
        primitive the ``input`` binary exposes, and it holds for exactly ``ms``.
        """
        self._lazy()
        self._lean(f"input swipe {x} {y} {x} {y} {int(ms)}", timeout=timeout)

    def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
        timeout: float = 15,
    ) -> None:
        """Straight-line drag.

        ``input swipe`` interpolates in the input dispatcher and honours
        ``duration_ms`` exactly, with no host process. The u2 alternative
        (``swipePoints``) takes explicit steps and runs ~170 ms slower here, so it
        is only used by :meth:`gesture` for paths the shell cannot express.
        """
        self._lazy()
        self._lean(f"input swipe {x1} {y1} {x2} {y2} {int(duration_ms)}", timeout=timeout)

    def gesture(
        self,
        points: list[tuple[int, int]],
        duration_ms: int = 300,
        timeout: float = 20,
    ) -> None:
        """Interpolated multi-point path.

        u2 ``swipePoints(int[] flat, int steps)`` injects MotionEvents directly,
        so the path is smooth at pointer-event resolution - the shell has no
        equivalent. Steps are derived from the requested duration (~60 Hz).
        Falls back to a chain of ``input swipe`` segments when u2 is absent.
        """
        self._lazy()
        pts = [(int(x), int(y)) for x, y in points]
        if len(pts) < 2:
            raise ValueError("gesture needs at least two points")
        steps = max(2, min(200, int(duration_ms / 16)))
        if self._u2_ready():
            try:
                flat: list[int] = []
                for x, y in pts:
                    flat.extend((x, y))
                self.u2.call("swipePoints", [flat, steps], timeout=timeout)
                return
            except FastPathTimeout:
                pass
        cmds = [
            f"input swipe {a[0]} {a[1]} {b[0]} {b[1]} "
            f"{max(1, int(duration_ms / (len(pts) - 1)))}"
            for a, b in zip(pts, pts[1:])
        ]
        self.shell_many(cmds, timeout=timeout)

    def pinch(
        self,
        cx: int,
        cy: int,
        start_radius: int,
        end_radius: int,
        duration_ms: int = 300,
        rotate_deg: float = 0.0,
        timeout: float = 25,
    ) -> None:
        """Two-finger pinch about ``(cx, cy)``.

        u2's ``gesture(Selector, p1, p2, p3, p4, steps)`` drives
        ``performTwoPointerGesture``, the only real two-pointer injection this
        server exposes; ``swipePoints`` is single-pointer. The selector is only
        used to obtain a ``UiObject`` handle - the coordinates are absolute.
        """
        self._lazy()
        steps = max(4, min(100, int(duration_ms / 16)))
        starts = _radial(cx, cy, start_radius, rotate_deg)
        ends = _radial(cx, cy, end_radius, rotate_deg)
        if self._u2_ready():
            for sel in _selector_candidates():
                try:
                    self.u2.call(
                        "gesture",
                        [
                            sel,
                            {"x": starts[0][0], "y": starts[0][1]},
                            {"x": starts[1][0], "y": starts[1][1]},
                            {"x": ends[0][0], "y": ends[0][1]},
                            {"x": ends[1][0], "y": ends[1][1]},
                            steps,
                        ],
                        timeout=timeout,
                    )
                    return
                except FastPathTimeout:
                    continue
        # last resort: two single-pointer drags in opposite directions
        self.shell_many(
            [
                f"input swipe {starts[0][0]} {starts[0][1]} {ends[0][0]} {ends[0][1]} "
                f"{duration_ms}",
                f"input swipe {starts[1][0]} {starts[1][1]} {ends[1][0]} {ends[1][1]} "
                f"{duration_ms}",
            ],
            timeout=timeout,
        )

    def key(self, code: str | int, timeout: float = 8) -> None:
        """Press a key by name (``'BACK'``, ``'HOME'``, ``'ENTER'``) or keycode.

        Warm-shell ``input keyevent``: no host spawn, ~52 ms measured, against
        ~66 ms for the u2 ``pressKeyCode`` RPC on this build.
        """
        self._lazy()
        if isinstance(code, str):
            name = code.strip().upper()
            if name.isdigit():
                code = int(name)
            elif name in _KEYCODES:
                code = _KEYCODES[name]
            elif re.fullmatch(r"KEYCODE_\w+", name):
                code = name
            else:
                raise ValueError(f"unknown key name {code!r}")
        self._lean(f"input keyevent {code}", timeout=timeout)

    # -- text --------------------------------------------------------------
    def text(self, s: str, timeout: float = 15) -> None:
        """Type text into the focused field, CJK and emoji included.

        u2's ``setClipboard(label, text)`` + ``pasteClipboard()`` carries any
        Unicode through the system clipboard (~25 ms, zero spawns); the on-device
        ``input text`` command cannot represent non-ASCII and mangles spaces, so
        it is only the ASCII fallback. Which one actually served the call is
        recorded in :meth:`health` as ``text_mode``.
        """
        self._lazy()
        if not s:
            return
        if self._u2_ready():
            try:
                self.u2.call("setClipboard", ["dshfastpath", s], timeout=timeout)
                self.u2.call("pasteClipboard", [], timeout=timeout)
                self._text_mode = "u2_clipboard"
                return
            except FastPathTimeout:
                pass
        if all(ord(ch) < 128 for ch in s):
            escaped = s.replace(" ", "%s")
            self.shell(f"input text {escaped}", timeout=timeout)
            self._text_mode = "input_text_ascii"
            return
        raise FastPathTimeout(
            "no CJK-safe text path: uiautomator2 clipboard unavailable and "
            "`input text` cannot carry non-ASCII"
        )

    # -- ui introspection --------------------------------------------------
    def dump(self, compact: bool = True, only_interactive: bool = False) -> dict:
        """Normalised accessibility tree.

        Transport: u2 ``dumpWindowHierarchy(false)`` returns the XML string in
        one call (~95 ms, 46 KB here) with no device-side process; parsing and
        filtering happen in Python. Falls back to a grep-bounded
        ``dumpsys activity top`` view hierarchy when the RPC is unavailable;
        ``uiautomator dump`` is never used because it dies on this device.
        """
        self._lazy()
        t0 = time.perf_counter()
        xml_text = ""
        err = ""
        if self._u2_ready():
            try:
                xml_text = self.u2.call("dumpWindowHierarchy", [False], timeout=15) or ""
            except FastPathTimeout as exc:
                err = str(exc)
        if not xml_text:
            nodes, err = self._dump_via_dumpsys(err)
            if only_interactive:
                nodes = [
                    n
                    for n in nodes
                    if n.get("clickable") or n.get("scrollable") or n.get("text")
                ]
            out: dict[str, Any] = {
                "nodes": [_compact(n, bounds=False) for n in nodes] if compact else nodes,
                "ts": time.time(),
                "rotation": self._display[2] if len(self._display) > 2 else 0,
                "source": "dumpsys_activity_top",
                "ms": round((time.perf_counter() - t0) * 1000, 1),
            }
            if err:
                out["error"] = err
            return out
        nodes, rotation = _parse_hierarchy(xml_text)
        if only_interactive:
            nodes = [
                n
                for n in nodes
                if n.get("clickable")
                or n.get("scrollable")
                or n.get("long_clickable")
                or n.get("text")
                or n.get("desc")
            ]
        if compact:
            nodes = [_compact(n) for n in nodes]
        out = {
            "nodes": nodes,
            "ts": time.time(),
            "rotation": rotation,
            "source": "u2_rpc",
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        }
        if err:
            out["error"] = err
        return out

    def _dump_via_dumpsys(self, prior: str) -> tuple[list[dict], str]:
        """Degrade to one grep-bounded ``dumpsys activity top`` hierarchy.

        ``uiautomator dump`` is deliberately NOT used: on this device the stale
        uiautomator process makes it die (exit 137) after ~330 ms on every
        attempt, so retrying it is pure latency. ``dumpsys activity top`` never
        dies and carries the view hierarchy, though without pixel bounds.
        """
        try:
            rc, out, err = self.shell(
                "dumpsys activity top | sed -n '/View Hierarchy:/,/Looper/p' | head -c 300000",
                timeout=25,
            )
        except FastPathTimeout as exc:
            return [], f"{prior}; {exc}".strip("; ")
        if rc != 0 or "View Hierarchy" not in out:
            return [], f"{prior}; dumpsys fallback unusable: {err or out[:120]}".strip("; ")
        nodes: list[dict] = []
        for line in out.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("View Hierarchy"):
                continue
            depth = (len(line) - len(line.lstrip(" "))) // 2
            node = {
                "depth": depth,
                "cls": stripped.split("{", 1)[0].strip(),
                "text": "",
                "desc": "",
                "id": "",
                "pkg": "",
                "bounds": [0, 0, 0, 0],
                "clickable": "clickable" in stripped,
                "long_clickable": False,
                "scrollable": "scrollable" in stripped,
                "checkable": False,
                "checked": False,
                "enabled": True,
                "focused": False,
                "selected": False,
            }
            hit = re.search(r"\bid/([A-Za-z0-9_]+)", stripped)
            if hit:
                node["id"] = f"id/{hit.group(1)}"
            text = re.search(r'text="([^"]*)"', stripped)
            if text:
                node["text"] = text.group(1)
            nodes.append(node)
        return nodes[:2000], ""

    def current_focus(self, timeout: float = 12) -> dict:
        """``{'package','activity'}`` for the resumed activity.

        Package comes from u2 ``deviceInfo`` (35 ms); the activity needs one
        ``dumpsys activity`` line, which is grep-limited on the device so only a
        few bytes cross the wire. No full dumpsys parse.
        """
        self._lazy()
        package = ""
        if self._u2_ready():
            try:
                info = self.u2.call("deviceInfo", [], timeout=timeout)
                if isinstance(info, dict):
                    package = info.get("currentPackageName") or ""
                    self._absorb_device_info(info)
            except FastPathTimeout:
                pass
        activity = ""
        try:
            rc, out, _ = self.shell(
                "dumpsys activity activities | grep -m1 -E "
                "'topResumedActivity|mResumedActivity|ResumedActivity'",
                timeout=timeout,
            )
            if rc == 0 and out:
                found = re.search(r"([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)", out)
                if found:
                    package = package or found.group(1)
                    activity = found.group(2)
                    if activity.startswith("."):
                        activity = package + activity
        except FastPathTimeout:
            pass
        if not package:
            rc, out, _ = self.shell("dumpsys window | grep -m1 mCurrentFocus", timeout=timeout)
            found = re.search(r"([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)", out or "")
            if found:
                package, activity = found.group(1), found.group(2)
        return {"package": package, "activity": activity}

    def wait_idle(self, timeout: float = 3.0, quiet_ms: int = 120) -> bool:
        """Wait until window content settles. Cheap: never screenshots.

        Polls u2 ``waitForIdle(0)`` (~12 ms) plus the current package; content is
        called stable once the accessibility dispatcher reports idle AND the
        foreground package has not changed for ``quiet_ms``.
        """
        self._lazy()
        deadline = time.monotonic() + max(0.0, timeout)
        quiet = max(0, quiet_ms) / 1000.0
        last_change = time.monotonic()
        last_pkg: str | None = None
        while True:
            idle = False
            pkg = last_pkg
            if self._u2_ready():
                ok, _ = self.u2.call_soft("waitForIdle", [0], timeout=2)
                idle = ok
                ok2, info = self.u2.call_soft("deviceInfo", [], timeout=2)
                if ok2 and isinstance(info, dict):
                    pkg = info.get("currentPackageName") or pkg
            else:
                ok, focus = True, self.current_focus(timeout=3)
                pkg = focus.get("package") or pkg
                idle = ok
            now = time.monotonic()
            if pkg != last_pkg:
                last_pkg = pkg
                last_change = now
            if idle and (now - last_change) >= quiet:
                return True
            if now >= deadline:
                return False
            time.sleep(0.03)

    # -- health / bench ----------------------------------------------------
    def health(self) -> dict:
        """One cheap snapshot: what is actually reachable right now.

        Budget 300 ms. Probed, never assumed: ``u2_ok`` is set only after a real
        ``deviceInfo`` answers, and ``u2_ms`` is that call's measured round-trip.
        The shell probe picks up the uiautomator process id in the same round
        trip. No dumpsys, no screenshots.
        """
        t0 = time.perf_counter()
        shell_ok = False
        u2_pid = self._u2_pid
        try:
            rc, out, _ = self.shell(
                "echo fp-ok; ps -A -o PID,ARGS 2>/dev/null "
                "| grep -m1 -E 'wetest|uiautomator' | awk '{print $1}'",
                timeout=6,
            )
            lines = out.splitlines()
            shell_ok = rc == 0 and bool(lines) and "fp-ok" in lines[0]
            for line in lines[1:]:
                if line.strip().isdigit():
                    u2_pid = int(line.strip())
                    break
        except (FastPathTimeout, OSError):
            shell_ok = False
        self._u2_pid = u2_pid
        u2_ok = False
        if self.u2.port is not None:
            ok, info = self.u2.call_soft("deviceInfo", [], timeout=5)
            if ok and isinstance(info, dict):
                u2_ok = True
                self._u2_ok = True
                self._absorb_device_info(info)
            else:
                self._u2_ok = False
        return {
            "shell_ok": shell_ok,
            "u2_ok": u2_ok,
            "u2_ms": round(self.u2.last_ms, 1) if u2_ok else 0.0,
            "u2_pid": u2_pid,
            "forward_port": self.u2.port,
            "device_port": self.u2.device_port,
            "daemon_ok": False,
            "daemon_note": "no native daemon wired; transports are adb-shell + u2 RPC",
            "serial": self.serial,
            "display": list(self._display),
            "root": self._root_argv is not None,
            "text_mode": self._text_mode,
            "gesture_route": self._gesture_route if u2_ok else "shell",
            "shot_route": self._shot_route,
            "shot_ms": self._last_shot_ms,
            "version": __version__,
            "ms": round((time.perf_counter() - t0) * 1000, 1),
            "notes": list(self._notes),
        }

    def bench(self, runs: int = 3, taps: int = 6, heavy: bool = False) -> dict:
        """Self-benchmark against the live device; returns the numbers as a dict.

        Light by default: a phone being tapped while you measure it is a phone
        whose frame times you just ruined, so the long loops only run with
        ``heavy=True``. The "before" side (spawned adb per action) is not
        reproduced here - this module never spawns adb per action, that is the
        entire point - those numbers come from the caller's own measurement.
        """
        self._lazy()
        if heavy:
            runs, taps = max(runs, 5), max(taps, 20)
        out: dict[str, Any] = {
            "serial": self.serial,
            "runs": runs,
            "taps": taps,
            "heavy": heavy,
            "display": list(self._display),
        }

        def median_ms(fn: Callable[[], Any], n: int) -> float:
            samples = []
            for _ in range(n):
                t0 = time.perf_counter()
                try:
                    fn()
                except FastPathTimeout:
                    return -1.0
                samples.append((time.perf_counter() - t0) * 1000)
            return round(statistics.median(samples), 1) if samples else -1.0

        cx = self._display[0] // 2 or 540
        cy = self._display[1] // 2 or 1200

        self._gesture_route = "shell"
        shell_tap = median_ms(lambda: self.tap(cx, cy, timeout=10), taps)
        u2_tap = -1.0
        if self._u2_ready():
            self._gesture_route = "u2"
            u2_tap = median_ms(lambda: self.tap(cx, cy, timeout=10), taps)
        self._gesture_route = "shell"
        out["tap"] = {
            "shell_lean_ms": shell_tap,
            "u2_click_ms": u2_tap,
            "chosen": "shell" if shell_tap >= 0 else "u2",
        }
        out["swipe_ms"] = median_ms(lambda: self.swipe(cx, cy + 150, cx, cy - 150, 200), 3)
        out["key_home_ms"] = median_ms(lambda: self.key("HOME", timeout=10), 2)

        out["shell_single_ms"] = median_ms(lambda: self.shell("echo one"), runs)
        batch = [
            "getprop ro.product.model",
            "getprop ro.build.version.sdk",
            "id -u",
            "echo batch",
            "date +%s",
            "ls /proc/self >/dev/null && echo ok",
            "getprop ro.serialno",
            "echo two",
            "true",
            "false",
            "printf 'x\n'",
            "echo end",
        ]
        out["shell_many_12_ms"] = median_ms(lambda: self.shell_many(batch), runs)
        out["shell_12_separate_ms"] = median_ms(
            lambda: [self.shell(c) for c in batch], max(1, runs // 2)
        )

        first_dump = self.dump(compact=True)
        out["dump"] = {
            "ms": first_dump.get("ms", -1),
            "nodes": len(first_dump.get("nodes", [])),
            "source": first_dump.get("source"),
        }
        out["dump_ms"] = median_ms(lambda: self.dump(compact=True), runs)
        first_shot = self.shot()
        out["shot_raw"] = {
            "ms": first_shot["ms"],
            "bytes": first_shot["bytes"],
            "route": first_shot["route"],
            "header": first_shot["header"],
        }
        out["shot_raw_ms"] = median_ms(lambda: self.shot(), runs)
        if heavy:
            png = self.exec_out(["screencap", "-p"])
            out["shot_png"] = {
                "bytes": len(png),
                "ms": median_ms(lambda: self.exec_out(["screencap", "-p"]), 2),
            }
        out["focus_ms"] = median_ms(lambda: self.current_focus(), runs)
        out["wait_idle_ms"] = median_ms(lambda: self.wait_idle(timeout=3.0), runs)
        out["health_ms"] = median_ms(lambda: self.health(), runs)
        return out


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _find_adb() -> str:
    """Locate adb: env, the conventional Windows install, then PATH."""
    env = os.environ.get("ADB_PATH")
    if env and os.path.exists(env):
        return env
    for candidate in (
        r"C:\platform-tools\adb.exe",
        "/usr/bin/adb",
        "/usr/local/bin/adb",
    ):
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("adb")
    return found or "adb"


def _detect_serial(adb: str) -> str:
    """First device serial adb reports, or a single attached device's serial."""
    try:
        out = subprocess.run(
            [adb, "devices"],
            capture_output=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in out.stdout.decode("utf-8", "replace").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            return parts[0]
    return ""


def _radial(cx: int, cy: int, radius: int, rotate_deg: float) -> list[tuple[int, int]]:
    """Two points mirrored about ``(cx, cy)`` at *radius*, rotated by *rotate_deg*."""
    import math

    a = math.radians(rotate_deg)
    r = max(1, int(radius))
    return [
        (int(cx + r * math.cos(a)), int(cy + r * math.sin(a))),
        (int(cx - r * math.cos(a)), int(cy - r * math.sin(a))),
    ]


def _selector_candidates() -> list[dict]:
    """Selectors used only to obtain a UiObject handle for ``gesture``."""
    return [
        {"className": "android.widget.FrameLayout"},
        {"className": "android.view.View"},
        {"className": "android.widget.LinearLayout"},
    ]


def _parse_hierarchy(xml_text: str) -> tuple[list[dict], int]:
    """Flatten uiautomator's XML into an ordered node list plus rotation."""
    if not xml_text.strip():
        return [], 0
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return [], 0
    rotation = int(root.attrib.get("rotation", 0) or 0)
    nodes: list[dict] = []
    stack: list[tuple[ET.Element, int]] = [(root, -1)]
    while stack:
        el, depth = stack.pop()
        attrib = el.attrib
        if "bounds" in attrib or "class" in attrib:
            nodes.append(
                {
                    "depth": depth,
                    "cls": attrib.get("class", ""),
                    "text": attrib.get("text", ""),
                    "desc": attrib.get("content-desc", ""),
                    "id": attrib.get("resource-id", ""),
                    "pkg": attrib.get("package", ""),
                    "bounds": _bounds(attrib.get("bounds", "")),
                    "clickable": attrib.get("clickable") == "true",
                    "long_clickable": attrib.get("long-clickable") == "true",
                    "scrollable": attrib.get("scrollable") == "true",
                    "checkable": attrib.get("checkable") == "true",
                    "checked": attrib.get("checked") == "true",
                    "enabled": attrib.get("enabled") == "true",
                    "focused": attrib.get("focused") == "true",
                    "selected": attrib.get("selected") == "true",
                }
            )
        for child in reversed(list(el)):
            stack.append((child, depth + 1))
    return nodes, rotation


def _bounds(spec: str) -> list[int]:
    """``'[x1,y1][x2,y2]'`` -> ``[x1, y1, x2, y2]``."""
    nums = re.findall(r"-?\d+", spec or "")
    if len(nums) != 4:
        return [0, 0, 0, 0]
    return [int(n) for n in nums]


def _compact(node: dict, bounds: bool = True) -> dict:
    """Drop empty/false fields and shorten the attribute names."""
    out: dict[str, Any] = {
        "i": node["depth"],
        "c": node["cls"].rsplit(".", 1)[-1] if node["cls"] else "",
    }
    if bounds:
        out["b"] = node["bounds"]
    for key, short in (
        ("text", "t"),
        ("desc", "d"),
        ("id", "r"),
        ("pkg", "p"),
    ):
        value = node.get(key)
        if value:
            out[short] = value
    for key in ("clickable", "long_clickable", "scrollable", "checked", "focused", "selected"):
        if node.get(key):
            out[key] = True
    return out


_SINGLETON: FastPath | None = None
_SINGLETON_LOCK = threading.Lock()


def get() -> FastPath:
    """Return the process-wide :class:`FastPath` singleton."""
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = FastPath()
    return _SINGLETON


def warm(timeout: float = 15.0) -> dict:
    """Pre-open both shells and the u2 forward on the singleton.

    ``from fastpath import warm; warm()`` at server start-up, then every later
    call finds live pipes and an established forward.
    """
    return get().warm(timeout=timeout)


def _main(argv: Sequence[str]) -> int:
    """``python fastpath.py --bench`` prints the measured table as JSON."""
    import argparse

    parser = argparse.ArgumentParser(description="Android fast path probe")
    parser.add_argument("--bench", action="store_true", help="run the self-benchmark")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--taps", type=int, default=20)
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--heavy", action="store_true", help="long tap/png loops")
    parser.add_argument("--serial", default="")
    args = parser.parse_args(argv)

    fp = FastPath(serial=args.serial or None)
    warm_info = fp.warm()
    print(json.dumps({"warm": warm_info}, indent=2))
    if args.health or not args.bench:
        print(json.dumps(fp.health(), indent=2, ensure_ascii=False))
    if args.bench:
        print(json.dumps(fp.bench(runs=args.runs, taps=args.taps, heavy=args.heavy),
                        indent=2, ensure_ascii=False))
    fp.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(__import__("sys").argv[1:]))
