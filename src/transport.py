"""ADB transport with real exit status, bounded output and binary-safe I/O."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class CommandResult:
    args: list[str]
    exit_code: Optional[int]
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    truncated: bool = False
    elapsed_ms: float = 0
    error: str = ""

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.error

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace").rstrip("\r\n")

    def as_dict(self) -> dict:
        return {
            "success": self.success,
            "exit_code": self.exit_code,
            "stdout": self.text,
            "stderr": self.stderr.decode("utf-8", "replace").rstrip("\r\n"),
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms,
            **({"error": self.error} if self.error else {}),
        }


_FAST = None
_FAST_PROBED = False
_FAST_LOCK = threading.Lock()


def fast_transport():
    """Bind the optional zero-spawn fast path once. Never raises, never blocks.

    ``src/fastpath.py`` is an optional accelerator. It is OFF unless
    ``ANDROID_MCP_FASTPATH=1`` is set, because measurement on this host showed
    the warm pipe costing ~106 ms per command against ~66 ms for a plain adb
    spawn - shipping it enabled would be a regression. Keep the seam so a
    future fixed implementation can be switched on without touching callers.
    """
    global _FAST, _FAST_PROBED
    if os.environ.get("ANDROID_MCP_FASTPATH", "").strip().lower() not in ("1", "on", "true", "yes"):
        return None
    if _FAST_PROBED:
        return _FAST
    with _FAST_LOCK:
        if _FAST_PROBED:
            return _FAST
        _FAST_PROBED = True
        try:
            from . import fastpath  # noqa: WPS433 - optional dependency by design
        except Exception:
            try:
                import fastpath  # type: ignore[no-redef]
            except Exception:
                return None
        for factory in ("instance", "get_fastpath", "shared"):
            try:
                candidate = getattr(fastpath.FastPath, factory)()
            except Exception:
                continue
            if candidate is not None:
                _FAST = candidate
                return _FAST
        try:
            _FAST = fastpath.FastPath()
        except Exception:
            _FAST = None
        return _FAST


def try_fast_shell(script: str, timeout: float = 20, max_bytes: int = 1_048_576,
                   args: Optional[list[str]] = None) -> Optional[CommandResult]:
    """Serve one device shell script through the warm pipe, or decline.

    Returns None whenever the fast path is unavailable or unhappy, which makes
    the caller's fallback to ``execute()`` automatic. The script is passed
    through untouched, so the device-side ``timeout -s KILL`` wrapper and any
    ``su -c`` prefix keep working exactly as they do over plain adb.
    """
    fast = fast_transport()
    if fast is None or not script or "\0" in script:
        return None
    try:
        if not fast.available():
            return None
    except Exception:
        return None
    t0 = time.monotonic()
    try:
        code, out, err = fast.shell(script, timeout=max(0.1, float(timeout)), root=False)
    except Exception:
        return None
    if not isinstance(code, int):
        return None
    raw_out = out.encode("utf-8", "replace") if isinstance(out, str) else bytes(out or b"")
    raw_err = err.encode("utf-8", "replace") if isinstance(err, str) else bytes(err or b"")
    result = CommandResult(
        args=args or ["<fast-shell>", script],
        exit_code=code,
        stdout=raw_out[:max_bytes],
        stderr=raw_err[:max_bytes],
        truncated=len(raw_out) > max_bytes or len(raw_err) > max_bytes,
    )
    result.elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
    return result


def reset_fast_transport() -> None:
    """Drop the warm pipe. Used on device switch so it cannot follow the old phone."""
    global _FAST, _FAST_PROBED
    with _FAST_LOCK:
        previous, _FAST, _FAST_PROBED = _FAST, None, False
    if previous is None:
        return
    for method in ("close", "shutdown", "reset"):
        closer = getattr(previous, method, None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
            return


def execute(
    args: list[str],
    timeout: float = 20,
    max_bytes: int = 1_048_576,
    stdin: Optional[bytes] = None,
    output_file: Optional[str] = None,
) -> CommandResult:
    """Never invoke a host shell. Spool output to disk instead of unbounded RAM.

    ``output_file`` streams stdout to a local artifact (screenshots/archives).
    The ordinary stdout/stderr returned to MCP are independently bounded.
    A timeout kills the local ADB process; shell callers additionally install
    a device-side timeout, since killing ADB alone does NOT kill remote work.
    """
    if not 0 < timeout <= 3600:
        raise ValueError("timeout must be in (0, 3600]")
    if not 1 <= max_bytes <= 32 * 1024 * 1024:
        raise ValueError("max_bytes must be between 1 and 32 MiB")
    t0 = time.monotonic()
    result = CommandResult(args=args, exit_code=None)
    if args[0].lower().endswith((".bat", ".cmd")):
        result.error = "Use a native adb.exe via ADB_PATH, not a .bat/.cmd wrapper"
        return result
    with tempfile.TemporaryFile() as errors:
        if output_file:
            os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
            output = open(output_file, "wb")
        else:
            output = tempfile.TemporaryFile()
        try:
            try:
                completed = subprocess.run(
                    args, input=stdin, stdout=output, stderr=errors,
                    timeout=timeout, check=False, shell=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                result.exit_code = completed.returncode
            except subprocess.TimeoutExpired:
                result.timed_out = True
                result.error = f"ADB operation exceeded {timeout:g}s"
            except OSError as exc:
                result.error = str(exc)
            if not output_file:
                output.seek(0)
                raw = output.read(max_bytes + 1)
                result.stdout = raw[:max_bytes]
                result.truncated = len(raw) > max_bytes
            errors.seek(0)
            raw = errors.read(max_bytes + 1)
            result.stderr = raw[:max_bytes]
            result.truncated = result.truncated or len(raw) > max_bytes
        finally:
            output.close()
    result.elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
    return result
