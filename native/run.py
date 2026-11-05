#!/usr/bin/env python3
"""
native/run.py — lifecycle helper for the pfdaemon input daemon.

  python run.py --start            # start (root, devnode engine) -- idempotent
  python run.py --start --no-root  # start as plain shell UID (inject engine only)
  python run.py --status           # ping the live daemon
  python run.py --stop             # stop; safe when nothing is running
  python run.py --bench            # latency comparison (daemon vs `input`)
  python run.py --cmd "tap 540 1200"

Idempotency: --start checks a host state file, then verifies with a real `ping` over
the forwarded socket. A live daemon is reused, never duplicated. A dead one is
reaped and replaced. Concurrent starts serialise on a host-side lock file.

Device-side artefacts all live under /data/local/tmp/pfdaemon/ and are named
pfdaemon-<sha8>.{jar,log,pid} so they cannot collide with other tools sharing
/data/local/tmp.

NOTE: the devnode path requires root (the node is crw-rw---- root:input and SELinux
input_device denies even group write, although shell is in the `input` group).
--no-root falls back to injectInputEvent, which genuinely works as UID 2000.
"""

import argparse
import hashlib
import json
import os
import socket
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
JAR_LOCAL = os.path.join(HERE, "pfdaemon", "pfdaemon.jar")
STATE = os.path.join(HERE, "pfdaemon", "state.json")
LOCK = os.path.join(HERE, "pfdaemon", "state.lock")
DDIR = "/data/local/tmp/pfdaemon"

DEFAULT_ADB = os.environ.get("ADB_PATH") or shutil.which("adb") or "adb"
PKG = "com.pf.Daemon"

START_SH = """#!/system/bin/sh
# pfdaemon launcher — args: <jar> <port> <log>
mkdir -p /data/local/tmp/pfdaemon 2>/dev/null
setsid sh -c "CLASSPATH=$1 app_process /system/bin com.pf.Daemon --port $2 >$3 2>&1" </dev/null >/dev/null 2>&1 &
echo $!
"""


class Adb:
    def __init__(self, path, serial):
        self.path = path
        self.serial = serial

    def _base(self):
        a = [self.path]
        if self.serial:
            a += ["-s", self.serial]
        return a

    def shell(self, cmd, root=False, timeout=30):
        if root:
            # single-quote the payload so the device shell does not re-split it
            cmd = "su -c " + json.dumps(cmd)
        return subprocess.run(self._base() + ["shell", cmd],
                              capture_output=True, text=True, timeout=timeout)

    def push(self, local, remote):
        return subprocess.run(self._base() + ["push", local, remote],
                              capture_output=True, text=True, timeout=120)

    def forward(self, remote_port):
        r = subprocess.run(self._base() + ["forward", "tcp:0", "tcp:%d" % remote_port],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError("adb forward failed: " + r.stderr.strip())
        return int(r.stdout.strip())

    def unforward(self, local_port):
        subprocess.run(self._base() + ["forward", "--remove", "tcp:%d" % local_port],
                       capture_output=True, text=True, timeout=30)


class Client:
    def __init__(self, port, timeout=20):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.f = self.sock.makefile("rwb")
        self.timeout = timeout

    def cmd(self, line):
        self.f.write((line + "\n").encode())
        self.f.flush()
        raw = self.f.readline()
        if not raw:
            raise RuntimeError("daemon closed the connection")
        return json.loads(raw.decode("utf-8"))

    def close(self):
        try:
            self.f.close()
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------- state / lock

def read_state():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return None


def write_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=2)
    os.replace(tmp, STATE)


def clear_state():
    try:
        os.remove(STATE)
    except FileNotFoundError:
        pass


class HostLock:
    """Exclusive across processes; stale after 60s so a crashed run cannot wedge us."""

    def __init__(self, path, timeout=60):
        self.path = path
        self.timeout = timeout
        self.held = False

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > 60:
                        os.remove(self.path)
                        continue
                except FileNotFoundError:
                    continue
                time.sleep(0.1)
        raise RuntimeError("could not acquire %s within %ss" % (self.path, self.timeout))

    def __exit__(self, *exc):
        if self.held:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass


# ------------------------------------------------------------------- helpers

def jar_tag():
    with open(JAR_LOCAL, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:8]


def device_paths(tag):
    return {
        "dir": DDIR,
        "jar": "%s/pfdaemon-%s.jar" % (DDIR, tag),
        "log": "%s/pfdaemon-%s.log" % (DDIR, tag),
        "pid": "%s/pfdaemon-%s.pid" % (DDIR, tag),
        "sh": "%s/start-%s.sh" % (DDIR, tag),
    }


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def ping(port, timeout=2.0):
    try:
        c = Client(port, timeout)
        r = c.cmd("ping")
        c.close()
        return r
    except Exception:
        return None


def is_alive(st):
    if not st or "lport" not in st:
        return None
    r = ping(st["lport"])
    if r and r.get("ok"):
        return r
    return None


def deploy(adb, tag, root):
    paths = device_paths(tag)
    adb.shell("mkdir -p " + DDIR + " 2>/dev/null")
    r = adb.push(JAR_LOCAL, paths["jar"])
    if r.returncode != 0:
        raise RuntimeError("push failed: " + r.stderr.strip())
    adb.shell("chmod 644 " + paths["jar"])

    # launcher script avoids nested su/setsid quoting entirely
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n") as fh:
        fh.write(START_SH)
        tmp = fh.name
    try:
        adb.push(tmp, paths["sh"])
    finally:
        os.unlink(tmp)
    adb.shell("chmod 755 " + paths["sh"])
    return paths


def start(args):
    if not os.path.exists(JAR_LOCAL):
        sys.exit("pfdaemon.jar missing — run build.ps1 first")
    adb = Adb(args.adb, args.serial)

    with HostLock(LOCK):
        st = read_state()
        live = is_alive(st)
        if live:
            print("already running: port=%s uid=%s route=%s  (reused)"
                  % (st["lport"], live.get("uid"), live.get("route")))
            return 0
        if st:
            stop_inner(adb, st, quiet=True)   # stale state: reap, then replace

        tag = jar_tag()
        paths = deploy(adb, tag, args.root)

        port = free_port()
        adb.shell("rm -f %s %s" % (paths["log"], paths["pid"]))
        r = adb.shell("sh %s %s %d %s" % (paths["sh"], paths["jar"], port, paths["log"]),
                      root=args.root)
        pid = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""

        # wait for the $READY handshake in the log
        ready = None
        deadline = time.time() + 10
        while time.time() < deadline:
            rr = adb.shell("cat %s 2>/dev/null" % paths["log"])
            out = rr.stdout or ""
            if "$READY" in out:
                ready = out
                break
            if "no_engine" in out or "listen_failed" in out:
                print(out.strip(), file=sys.stderr)
                return 4
            time.sleep(0.25)
        if ready is None:
            rr = adb.shell("cat %s 2>/dev/null" % paths["log"])
            print("daemon did not become ready.\n--- log ---\n%s" % (rr.stdout or "(empty)"),
                  file=sys.stderr)
            return 5

        route = "unknown"
        for line in ready.splitlines():
            if line.startswith("$READY"):
                parts = line.split()
                if len(parts) >= 3:
                    route = parts[2]

        lport = adb.forward(port)
        p = ping(lport, 5)
        if not p:
            print("forward established but ping failed", file=sys.stderr)
            return 6

        write_state({
            "tag": tag, "port": port, "lport": lport, "pid": pid,
            "jar": paths["jar"], "log": paths["log"],
            "route": p.get("route"), "uid": p.get("uid"),
            "root": bool(args.root), "serial": args.serial,
        })
        print("started: uid=%s route=%s device_port=%d local_port=%d pid=%s"
              % (p.get("uid"), p.get("route"), port, lport, pid or "?"))
        return 0


def stop_inner(adb, st, quiet=False):
    pid = st.get("pid")
    root = st.get("root", False)
    if pid:
        adb.shell("kill %s 2>/dev/null" % pid, root=root)
    # belt and braces: never leave a finger down / a listener behind
    adb.shell("pkill -f %s 2>/dev/null" % PKG, root=root)
    if st.get("lport"):
        adb.unforward(st["lport"])
    clear_state()
    if not quiet:
        print("stopped")


def stop(args):
    adb = Adb(args.adb, args.serial)
    st = read_state()
    if not st:
        print("not running")
        return 0
    stop_inner(adb, st)
    return 0


def status(args):
    st = read_state()
    if not st:
        print("not running (no state file)")
        return 1
    p = is_alive(st)
    if not p:
        print("state file present but daemon is dead (pid=%s)" % st.get("pid"))
        return 1
    print(json.dumps(p))
    return 0


def send(args):
    st = read_state()
    p = is_alive(st) if st else None
    if not p:
        print("daemon not running — python run.py --start", file=sys.stderr)
        return 1
    c = Client(st["lport"], 60)
    r = c.cmd(args.cmd)
    print(json.dumps(r))
    c.close()
    return 0 if r.get("ok") else 2


def bench(args):
    """Median wall-clock per op. Runs the same op through `adb shell input` for contrast."""
    st = read_state()
    if not st or not is_alive(st):
        print("daemon not running — start it first", file=sys.stderr)
        return 1
    adb = Adb(args.adb, args.serial)
    c = Client(st["lport"], 60)
    n = args.n

    # first call pays ART warmup; report it separately from the steady state
    t0 = time.perf_counter()
    c.cmd("ping")
    first_ping = (time.perf_counter() - t0) * 1000

    def med(fn, count):
        xs = []
        for _ in range(count):
            t = time.perf_counter()
            fn()
            xs.append((time.perf_counter() - t) * 1000)
        return xs

    rows = []
    xs = med(lambda: c.cmd("tap 540 1200"), n)
    rows.append(("daemon tap", xs))
    xs = med(lambda: c.cmd("swipe 540 1700 540 900 300"), max(3, n // 2))
    rows.append(("daemon swipe 300ms", xs))
    xs = med(lambda: adb.shell("input tap 540 1200"), max(3, n // 2))
    rows.append(("adb shell input tap", xs))
    xs = med(lambda: adb.shell("input swipe 540 1700 540 900 300"), max(3, n // 2))
    rows.append(("adb shell input swipe", xs))

    print("first ping (cold): %.1f ms" % first_ping)
    print("%-26s %8s %8s %8s" % ("op", "median", "p90", "n"))
    for name, xs in rows:
        s = sorted(xs)
        p90 = s[min(len(s) - 1, int(len(s) * 0.9))]
        print("%-26s %8.1f %8.1f %8d" % (name, statistics.median(xs), p90, len(xs)))
    c.close()
    return 0


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--start", action="store_true")
    g.add_argument("--stop", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--bench", action="store_true")
    g.add_argument("--cmd")

    ap.add_argument("--no-root", dest="no_root", action="store_true",
                    help="run as plain shell UID (inject engine only, no devnode)")
    ap.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL"))
    ap.add_argument("--adb", default=DEFAULT_ADB)
    ap.add_argument("-n", type=int, default=10)
    args = ap.parse_args()
    args.root = not args.no_root

    if args.start:
        return start(args)
    if args.stop:
        return stop(args)
    if args.status:
        return status(args)
    if args.bench:
        return bench(args)
    if args.cmd:
        return send(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
