#!/usr/bin/env python3
# run.py - control the osbridge app_process daemon: start / stop / status / bench
#
#   python run.py start            # push-check, allocate a free port, start detached, health check
#   python run.py stop             # graceful quit, then kill if still alive
#   python run.py status           # pid, ports, uptime, route map
#   python run.py ping
#   python run.py send "focus"     # one raw command through the daemon
#   python run.py --bench          # median daemon-vs-adb table (see BENCH.md)
#
# Everything the daemon needs lives in /data/local/tmp. Nothing is installed on the device.

import argparse
import json
import os
import random
import re
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "_state.json"

DEF_ADB = r"D:\Tools\platform-tools\adb.exe"
DEF_SERIAL = "975aaf17"
DEVICE_DIR = "/data/local/tmp"
JAR_NAME = "osbridge.jar"
DEV_JAR = f"{DEVICE_DIR}/{JAR_NAME}"
DEV_LOG = f"{DEVICE_DIR}/osbridge.log"
MAIN_CLASS = "com.pf.os.OsBridge"

ADB = os.environ.get("OSBRIDGE_ADB", DEF_ADB)
SERIAL = os.environ.get("OSBRIDGE_SERIAL", DEF_SERIAL)


# --------------------------------------------------------------------------- adb plumbing

def adb(*args, timeout=30, binary=False):
    cmd = [ADB, "-s", SERIAL] + [str(a) for a in args]
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if binary:
        return p.returncode, p.stdout, p.stderr
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    return p.returncode, out, err


def sh(cmd, timeout=30):
    """Run a device shell command as one string (avoids argv quoting surprises)."""
    return adb("shell", cmd, timeout=timeout)


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(st):
    STATE.write_text(json.dumps(st, indent=2), encoding="utf-8")


def clear_state():
    try:
        STATE.unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------- daemon client

class Client:
    """Line protocol: one command per line, one JSON reply per line, over the forwarded TCP port."""

    def __init__(self, host="127.0.0.1", port=None, timeout=15.0):
        st = load_state()
        self.host = host
        self.port = int(port if port is not None else st.get("host_port", 0))
        self.timeout = timeout

    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return s

    def send(self, line, timeout=None) -> dict:
        t = timeout or self.timeout
        s = self._connect()
        try:
            s.settimeout(t)
            s.sendall((line.rstrip("\n") + "\n").encode("utf-8"))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            text = buf.decode("utf-8", "replace").strip()
            if not text:
                return {"ok": False, "error": "empty reply (daemon died?)"}
            return json.loads(text)
        finally:
            try:
                s.close()
            except Exception:
                pass

    # convenience wrappers used by selftest / bench
    def ping(self):
        return self.send("ping")


def timed_send(client, line):
    t0 = time.perf_counter()
    r = client.send(line)
    return (time.perf_counter() - t0) * 1000.0, r


# --------------------------------------------------------------------------- liveness / discovery

def is_up(host_port=None, timeout=2.0):
    """Liveness is decided by the daemon itself answering `ping`, never by a ps heuristic."""
    st = load_state()
    port = int(host_port or st.get("host_port") or 0)
    if not port:
        return None
    try:
        r = Client(port=port, timeout=timeout).ping()
        return r if r.get("ok") else None
    except Exception:
        return None


def daemon_pids():
    """Best-effort PID list. Cosmetic only: stop() works without it, status() trusts ping.

    NOTE (measured on this device): `ps -A -o PID,PPID,NAME` returned empty even while the
    daemon held a LISTEN socket on 127.0.0.1, so a ps-based "not running" verdict must never be
    trusted on its own. Three shapes are tried, and [] is a legal answer.
    """
    for cmd in ("pidof com.pf.os.OsBridge",
                "ps -A | grep -F com.pf.os.OsBridge | grep -v grep",
                "ps -A | grep -F app_process | grep -v grep"):
        try:
            rc, out, _ = sh(cmd)
        except Exception:
            continue
        pids = []
        for ln in out.splitlines():
            for tok in ln.split():
                if tok.isdigit():
                    pids.append(int(tok))
                    break
        if pids:
            return pids
    return []


# --------------------------------------------------------------------------- lifecycle

def push_jar(force=False):
    if force or not os.path.exists(HERE / JAR_NAME):
        build = HERE / "build.ps1"
        if not build.exists():
            raise SystemExit(f"{JAR_NAME} missing and build.ps1 not found")
        print(f"[push] building via {build.name} ...")
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(build)],
            check=True,
        )
        return
    rc, out, err = sh(f"ls -l {DEV_JAR} 2>/dev/null")
    if rc != 0 or DEV_JAR not in out:
        print(f"[push] {DEV_JAR} missing on device, pushing")
        rc, out, err = adb("push", str(HERE / JAR_NAME), DEV_JAR)
        if rc != 0:
            raise SystemExit(f"adb push failed: {err.strip()}")
        adb("shell", f"chmod 644 {DEV_JAR}")


def start(device_port=None, quiet=False):
    push_jar()
    st = load_state()

    # stop anything already running
    if is_up(st.get("host_port")) or daemon_pids():
        stop(quiet=True)

    dev_port = device_port or 0
    attempts = [dev_port] if dev_port else []
    if not attempts:
        base = random.randint(27100, 27600)
        attempts = list(range(base, base + 12))

    last_err = None
    for port in attempts:
        # A busy port is detected by the bind failing, not by a /proc/net/tcp pre-check:
        # the daemon logs the bind error, ping never answers, and the next port is tried.
        launch = (
            f"cd {DEVICE_DIR} && "
            f"CLASSPATH={DEV_JAR} "
            f"setsid nohup app_process /system/bin {MAIN_CLASS} --port {port} "
            f"> {DEV_LOG} 2>&1 < /dev/null &"
        )
        try:
            # -T: no pty. Measured 139 ms wall for this launch; the daemon keeps running detached.
            adb("shell", "-T", launch, timeout=15)
        except subprocess.TimeoutExpired:
            # a hung launch is not fatal: the detached child may still be coming up, so fall
            # through to the ping health check rather than declaring failure here.
            last_err = f"launch shell timed out (port {port})"

        # forward host tcp:0 -> device tcp:port; adb picks the free host port and prints it
        old = st.get("host_port")
        if old:
            adb("forward", "--remove", f"tcp:{old}")
        rc, fout, ferr = adb("forward", "tcp:0", f"tcp:{port}")
        host_port = None
        if rc == 0:
            m = re.search(r"(\d+)\s*$", fout.strip())
            if m:
                host_port = int(m.group(1))
        if host_port is None:
            last_err = f"adb forward failed: {ferr.strip() or fout.strip()}"
            continue

        st = {
            "device_port": port,
            "host_port": host_port,
            "started_at": time.time(),
            "jar": DEV_JAR,
            "log": DEV_LOG,
        }
        save_state(st)

        # health check: boot is ~55 ms, so 12 s is generous slack, not a poll storm
        ok = None
        deadline = time.time() + 12
        while time.time() < deadline:
            ok = is_up(host_port, timeout=2.0)
            if ok:
                break
            time.sleep(0.15)
        if ok:
            pids = daemon_pids()
            st["pid"] = pids[0] if pids else None
            save_state(st)
            if not quiet:
                print(f"[start] ok  device_port={port} host_port={host_port} pid={st['pid']} "
                      f"boot_ms={ok.get('boot_ms')} log={DEV_LOG}")
            return st

        # failed on this port: clean up whatever half-started, then try the next one
        try:
            adb("shell", "-T", "pkill -f com.pf.os.OsBridge", timeout=10)
        except Exception:
            pass
        adb("forward", "--remove", f"tcp:{host_port}")

    raise SystemExit(f"start failed: {last_err}")


def stop(quiet=False):
    st = load_state()
    # graceful first: `quit` replies ok and then the daemon halts itself
    if st.get("host_port"):
        try:
            Client(port=st["host_port"], timeout=2.0).send("quit")
            time.sleep(0.25)
        except Exception:
            pass
    # single validated sweep as backstop (this exact command was run on the device)
    try:
        adb("shell", "-T", "pkill -f com.pf.os.OsBridge", timeout=15)
    except Exception:
        pass
    if st.get("host_port"):
        adb("forward", "--remove", f"tcp:{st['host_port']}")
    clear_state()
    if not quiet:
        print("[stop] daemon stopped, forward removed")


def status():
    st = load_state()
    up = is_up(st.get("host_port"))
    if not up:
        print(f"status: not answering on host_port={st.get('host_port')} (device_port={st.get('device_port')})")
        return 1
    print(f"status: running device_port={st.get('device_port')} host_port={st.get('host_port')}")
    print(f"  uid={up.get('uid')} sdk={up.get('sdk')} pid={up.get('pid')} boot_ms={up.get('boot_ms')}")
    try:
        r = Client(port=st.get("host_port"), timeout=20).send("diag")
        print("  routes:")
        for k, v in (r.get("routes") or {}).items():
            print(f"    {k:26s} <- {v}")
        for n in (r.get("notes") or []):
            print(f"    note: {n}")
    except Exception as e:
        print(f"  diag failed: {e}")
    return 0


# --------------------------------------------------------------------------- bench

def _median(vals):
    return statistics.median(vals) if vals else float("nan")


def _fmt(v):
    return f"{v:8.1f}" if v == v else "     n/a"


def bench(runs=5):
    """Median wall time per operation: daemon line-protocol vs the adb-shell equivalent.

    What is measured is what an agent actually pays: for the daemon, open a TCP socket to the
    forwarded port, write a line, read a line. For adb, spawn the adb client and wait for the
    shell command to finish (which includes a fresh app_process JVM for am/pm/settings/dumpsys).
    """
    st = start(quiet=True)
    cli = Client(port=st["host_port"], timeout=30)

    # ---- first-call latency (JIT + first binder touch on each path), reported separately
    first = {}
    t0 = time.perf_counter()
    cli.send("ping")
    first["ping"] = (time.perf_counter() - t0) * 1000
    for cmd, label in (("focus", "focus"), ("packages --filter com.android.settings", "packages")):
        t0 = time.perf_counter()
        cli.send(cmd)
        first[label] = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    cli.send("setting_get system screen_brightness")
    first["setting_get"] = (time.perf_counter() - t0) * 1000

    def adb_time(shell_cmd):
        t0 = time.perf_counter()
        subprocess.run([ADB, "-s", SERIAL, "shell", shell_cmd],
                       capture_output=True, timeout=60)
        return (time.perf_counter() - t0) * 1000

    def daemon_time(line):
        t0 = time.perf_counter()
        cli.send(line)
        return (time.perf_counter() - t0) * 1000

    # adb shell baseline: pure client+daemon overhead, no JVM work
    adb_baseline = statistics.median([adb_time("true") for _ in range(runs)])

    cases = [
        # label,              daemon line,                              adb shell equivalent
        ("focus",             "focus",                                 "dumpsys window | grep mCurrentFocus"),
        ("launch (--wait)",   "launch com.android.settings --wait",     "am start -W com.android.settings"),
        ("packages",          "packages --filter com.android",          "pm list packages com.android"),
        ("setting_get",       "setting_get system screen_brightness",   "settings get system screen_brightness"),
        ("pkg_info",          "pkg_info com.android.settings",          "dumpsys package com.android.settings"),
    ]

    rows = []
    for label, dline, acmd in cases:
        # warm one shot first so we measure steady state, not page faults
        cli.send(dline)
        d = [daemon_time(dline) for _ in range(runs)]
        a = [adb_time(acmd) for _ in range(runs)]
        dm, am = _median(d), _median(a)
        rows.append({
            "op": label,
            "daemon_ms": dm,
            "adb_ms": am,
            "speedup": (am / dm) if dm else float("nan"),
            "daemon_all": d,
            "adb_all": a,
        })
        print(f"  {label:18s} daemon {_fmt(dm)} ms   adb {_fmt(am)} ms   x{am/dm:5.1f}")

    print()
    print(f"  adb shell baseline ('adb shell true'): {adb_baseline:.1f} ms per call")
    print()
    print("| operation | daemon median | adb median | speedup |")
    print("|---|---:|---:|---:|")
    for r in rows:
        print(f"| {r['op']} | {r['daemon_ms']:.1f} ms | {r['adb_ms']:.1f} ms | {r['speedup']:.1f}x |")
    print()
    print("first-call latency (daemon):")
    for k, v in first.items():
        print(f"  {k:12s} {v:.1f} ms")

    out = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "serial": SERIAL,
        "runs": runs,
        "adb_shell_baseline_ms": adb_baseline,
        "rows": rows,
        "first_call_ms": first,
    }
    (HERE / "bench_results.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwrote {HERE / 'bench_results.json'}")
    return out


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="osbridge daemon control")
    ap.add_argument("action", nargs="?", default="status",
                    choices=["start", "stop", "restart", "status", "ping", "send", "diag", "bench"])
    ap.add_argument("cmd", nargs="?", help="command line for `send`")
    ap.add_argument("--port", type=int, default=0, help="force a device port")
    ap.add_argument("--bench", action="store_true", help="shorthand for action=bench")
    ap.add_argument("--runs", type=int, default=5, help="bench repetitions per case (default 5)")
    args = ap.parse_args()

    if args.bench:
        args.action = "bench"

    if args.action == "start":
        start(device_port=args.port)
    elif args.action == "stop":
        stop()
    elif args.action == "restart":
        stop(quiet=True)
        start(device_port=args.port)
    elif args.action == "status":
        return status()
    elif args.action == "ping":
        r = Client(timeout=5).ping()
        print(json.dumps(r, indent=2))
        return 0 if r.get("ok") else 1
    elif args.action == "diag":
        print(json.dumps(Client(timeout=20).send("diag"), indent=2))
    elif args.action == "send":
        if not args.cmd:
            raise SystemExit("usage: run.py send \"<command>\"")
        print(json.dumps(Client(timeout=60).send(args.cmd), indent=2))
    elif args.action == "bench":
        bench(runs=args.runs)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
