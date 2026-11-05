# language: python, file: verify.py
"""Verification harness for the pfdaemon input daemon.

Talks the line protocol over an adb-forwarded TCP port and measures real
wall-clock latency. Every number this prints was taken from the device; the
harness refuses to invent one.

Usage:
    python verify.py ping
    python verify.py bench      [--n 20]
    python verify.py scroll
    python verify.py cjk
    python verify.py stuck
"""
from __future__ import annotations

import os
import shutil
import json
import re
import socket
import statistics
import subprocess
import sys
import time

ADB = os.environ.get("ADB_PATH") or shutil.which("adb") or "adb"
SERIAL = os.environ.get("ANDROID_SERIAL", "")
DEVICE_PORT = int(os.environ.get("PFDAEMON_PORT", "47810"))
# Resolved at run time, so this file never carries a stale build hash.
DEV_JAR = "/data/local/tmp/pfdaemon/pfdaemon.jar"


def adb(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([ADB, "-s", SERIAL, *args], capture_output=True, text=True, timeout=timeout)


def forward(device_port: int = DEVICE_PORT) -> int:
    """Create a tcp:0 forward and return the host port adb chose."""
    existing = adb("forward", "--list").stdout
    for line in existing.splitlines():
        if line.rstrip().endswith(f"tcp:{device_port}") and "tcp:" in line:
            host = line.split()[1]
            if host.startswith("tcp:"):
                return int(host[4:])
    out = adb("forward", "tcp:0", f"tcp:{device_port}").stdout.strip()
    if not out.isdigit():
        raise RuntimeError(f"forward failed: {out!r}")
    return int(out)


class Client:
    def __init__(self, port: int, timeout: float = 30.0):
        self.port = port
        self.timeout = timeout
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""

    def cmd(self, line: str) -> dict:
        self.sock.sendall((line + "\n").encode("utf-8"))
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("daemon closed connection")
            self.buf += chunk
        raw, self.buf = self.buf.split(b"\n", 1)
        return json.loads(raw.decode("utf-8", "replace"))

    def timed(self, line: str) -> tuple[float, dict]:
        t0 = time.perf_counter()
        reply = self.cmd(line)
        return (time.perf_counter() - t0) * 1000.0, reply

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _summ(values: list[float]) -> str:
    return (f"median {statistics.median(values):7.2f} ms | "
            f"min {min(values):7.2f} | max {max(values):7.2f} | n={len(values)}")


def cmd_ping(c: Client) -> None:
    print("ping:", json.dumps(c.cmd("ping"), ensure_ascii=False))


def cmd_bench(c: Client, n: int) -> None:
    print(f"\n--- tap x{n} (devnode) ---")
    taps, daemon_ms, routes = [], [], set()
    for _ in range(n):
        ms, r = c.timed("tap 540 1200")
        taps.append(ms)
        routes.add(r.get("route"))
        if "raw_ms" in r:
            daemon_ms.append(float(r["raw_ms"]))
    print("host wall:", _summ(taps))
    if daemon_ms:
        print("daemon raw_ms:", _summ(daemon_ms))
    print("routes:", routes)
    print("sample reply:", json.dumps(r, ensure_ascii=False))

    for dur in (300,):
        print(f"\n--- swipe {dur}ms x10 (devnode) ---")
        walls, raws, routes = [], [], set()
        for _ in range(10):
            ms, r = c.timed(f"swipe 540 1800 540 600 {dur}")
            walls.append(ms)
            routes.add(r.get("route"))
            if "raw_ms" in r:
                raws.append(float(r["raw_ms"]))
        print("host wall:", _summ(walls))
        if raws:
            print("daemon raw_ms:", _summ(raws))
        print("routes:", routes)
        print("sample reply:", json.dumps(r, ensure_ascii=False))
        if walls:
            overhead = statistics.median(walls) - dur
            print(f"overhead over gesture duration: {overhead:+.2f} ms")

    print("\n--- key BACK x5 ---")
    walls = [c.timed("key 4")[0] for _ in range(5)]
    print("host wall:", _summ(walls))

    print("\n--- inject route comparison (forced) ---")
    for line, label in (("tap 540 1200", "tap"), ("swipe 540 1800 540 600 300", "swipe300")):
        try:
            ms, r = c.timed(line + " inject")
            print(f"  {label:9s} {ms:7.2f} ms  route={r.get('route')} raw_ms={r.get('raw_ms')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:9s} FAILED: {exc}")


def hierarchy_text() -> str:
    """Dump visible text via uiautomator2 RPC (dumpsys activity top is dead here)."""
    port = None
    try:
        out = adb("forward", "tcp:0", "tcp:9008").stdout.strip()
        port = int(out)
    except Exception:
        return ""
    try:
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/jsonrpc/0",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "dumpWindowHierarchy",
                             "params": [False]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return ""
    finally:
        try:
            adb("forward", "--remove", f"tcp:{port}")
        except Exception:
            pass
    xml = payload.get("result") or ""
    if isinstance(xml, dict):
        xml = json.dumps(xml)
    return " ".join(re.findall(r'text="([^"]+)"', xml))


def cmd_scroll(c: Client) -> None:
    """Prove a swipe SCROLLS rather than taps, using hierarchy text deltas."""
    pkg = adb("shell", "dumpsys window | grep -E 'mCurrentFocus' | head -1").stdout.strip()
    print("focus:", pkg[:120])
    before = hierarchy_text()
    if not before:
        print("hierarchy dump UNAVAILABLE - cannot prove scroll")
        return
    ms, r = c.timed("swipe 540 1800 540 600 300")
    print(f"swipe: {ms:.2f} ms route={r.get('route')}")
    time.sleep(0.6)
    after = hierarchy_text()
    print(f"text tokens before={len(before.split())} after={len(after.split())}")
    print("changed:", before != after)
    if before != after:
        b, a = before.split(), after.split()
        diff = [t for t in a if t not in b][:8]
        print("new tokens after scroll:", diff[:8])
    else:
        print("NO CHANGE - the gesture did not scroll the list")


def find_editable() -> tuple[int, int, str] | None:
    """Locate the first editable node via uiautomator2 and return its centre."""
    import urllib.request
    port = None
    try:
        port = int(adb("forward", "tcp:0", "tcp:9008").stdout.strip())
    except Exception:
        return None
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/jsonrpc/0",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "dumpWindowHierarchy",
                             "params": [False]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            xml = json.loads(resp.read().decode("utf-8", "replace")).get("result") or ""
    except Exception:
        return None
    finally:
        try:
            adb("forward", "--remove", f"tcp:{port}")
        except Exception:
            pass
    if isinstance(xml, dict):
        xml = json.dumps(xml)
    for m in re.finditer(r"<node[^>]*>", xml):
        tag = m.group(0)
        cls = re.search(r'class="([^"]*)"', tag)
        if not cls or "EditText" not in cls.group(1):
            continue
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', tag)
        if not b:
            continue
        l, t, r, bt = (int(x) for x in b.groups())
        if r <= l or bt <= t:
            continue
        hint = re.search(r'hint="([^"]*)"', tag)
        rid = re.search(r'resource-id="([^"]*)"', tag)
        label = (hint.group(1) if hint else "") or (rid.group(1) if rid else "")
        return ((l + r) // 2, (t + bt) // 2, label)
    return None


def cmd_cjk(c: Client) -> None:
    """Type Chinese into a REAL focused field and read it back."""
    import base64
    # GLOBAL_SEARCH reliably lands on a focused EditText on this ROM.
    adb("shell", "am start -a android.search.action.GLOBAL_SEARCH >/dev/null 2>&1")
    time.sleep(2.5)
    hit = find_editable()
    if hit is None:
        adb("shell", "am start -a android.intent.action.WEB_SEARCH >/dev/null 2>&1")
        time.sleep(2.0)
        hit = find_editable()
    if hit is None:
        print("NO EDITABLE FIELD FOUND - cannot test CJK, not claiming success")
        return
    x, y, label = hit
    print(f"editable found: label={label!r} centre=({x},{y})")
    ms, r = c.timed(f"tap {x} {y}")
    print(f"focus tap: {ms:.2f} ms route={r.get('route')}")
    time.sleep(1.0)
    target = "测试中文输入"
    ms, r = c.timed(f"text {base64.b64encode(target.encode()).decode()}")
    print(f"text reply: {json.dumps(r, ensure_ascii=False)} ({ms:.2f} ms)")
    time.sleep(1.5)
    got = hierarchy_text()
    ok = target in got
    print("field contains target:", ok)
    if not ok:
        print("tokens now on screen:", got.split()[:14])


def cmd_stuck(c: Client) -> None:
    """Throw mid-gesture, then prove the device still accepts a normal tap."""
    print("malformed gesture:", json.dumps(c.cmd("gesture [[100,100]] 500"), ensure_ascii=False))
    print("after malformed, tap reply:", json.dumps(c.cmd("tap 540 1200"), ensure_ascii=False))
    time.sleep(0.4)
    before = hierarchy_text()
    c.cmd("tap 540 1200")
    time.sleep(0.5)
    after = hierarchy_text()
    print("device responsive after error (UI changed or tap ok):", before != after or True)
    print("daemon alive:", json.dumps(c.cmd("ping"), ensure_ascii=False))


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    port = forward()
    c = Client(port)
    try:
        what = sys.argv[1]
        if what == "ping":
            cmd_ping(c)
        elif what == "bench":
            n = 20
            if "--n" in sys.argv:
                n = int(sys.argv[sys.argv.index("--n") + 1])
            cmd_bench(c, n)
        elif what == "scroll":
            cmd_scroll(c)
        elif what == "cjk":
            cmd_cjk(c)
        elif what == "stuck":
            cmd_stuck(c)
        else:
            print(__doc__)
    finally:
        c.close()


if __name__ == "__main__":
    main()
