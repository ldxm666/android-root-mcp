#!/usr/bin/env python3
"""
native/fallback.py — degraded pure-shell gesture path. Strategy C.

Purpose: never hard-fail. If there is no dexer, the daemon crashes, or the ROM blocks
both injection engines, this still moves a finger.

Two sub-strategies, both needing root (the node is crw-rw---- root:input and SELinux
input_device denies group write despite shell being in `input`):

  sendevent  (default) — one `sendevent` per event, but streamed down ONE persistent
             `su` shell instead of one adb call per event. That is the whole point:
             a fork per *adb call* measured 25ms/event; a fork down a live pipe is
             the fork cost only. Still the slowest path.

  --bulk     — pack struct input_event[] host-side, base64 it, and let the device
             write the whole burst to /dev/input/eventN with a single `base64 -d >`.
             One fork + one write per sample instead of ~6. Much closer to the
             daemon's devnode engine without needing a dexer.

Neither is a substitute for the daemon. Both are measured and labelled as degraded.

Touchscreen discovery is dynamic: parse `getevent -pl`, find the device exposing
ABS_MT_POSITION_X, read its ABS maxima, and scale from the logical display. No
hardcoded node, no hardcoded 13.333.
"""

import shutil
import argparse
import base64
import json
import os
import re
import struct
import subprocess
import sys
import time

DEFAULT_ADB = os.environ.get("ADB_PATH") or shutil.which("adb") or "adb"

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0x00
ABS_MT_SLOT = 0x2f
ABS_MT_TOUCH_MAJOR = 0x30
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39
ABS_MT_PRESSURE = 0x3a
BTN_TOUCH = 0x14a
BTN_TOOL_FINGER = 0x145

STEP_MS = 8


class Device:
    def __init__(self, adb_path, serial):
        self.adb = [adb_path] + (["-s", serial] if serial else [])
        self.shell_proc = None
        self.node = None
        self.max_x = 14399
        self.max_y = 31999
        self.w = 1080
        self.h = 2400

    # ---- plumbing -------------------------------------------------------

    def run(self, cmd, timeout=30):
        return subprocess.run(self.adb + ["shell", cmd],
                              capture_output=True, text=True, timeout=timeout)

    def root_shell(self):
        """One long-lived `su` we can stream into; avoids a fork per adb call."""
        if self.shell_proc is None:
            self.shell_proc = subprocess.Popen(
                self.adb + ["shell", "su"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, text=True, bufsize=1)
        return self.shell_proc

    def write_line(self, line):
        p = self.root_shell()
        p.stdin.write(line + "\n")
        p.stdin.flush()

    def close(self):
        if self.shell_proc:
            try:
                self.shell_proc.stdin.write("exit\n")
                self.shell_proc.stdin.flush()
                self.shell_proc.wait(timeout=5)
            except Exception:
                try:
                    self.shell_proc.kill()
                except Exception:
                    pass
            self.shell_proc = None

    # ---- discovery ------------------------------------------------------

    def discover(self):
        r = self.run("getevent -pl 2>/dev/null")
        out = r.stdout or ""
        dev, name, in_touch = None, "?", False
        for line in out.splitlines():
            if line.startswith("add device"):
                dev = line.split(":", 1)[1].strip()
                in_touch = False
            elif line.strip().startswith("name:"):
                name = line.split(":", 1)[1].strip().strip('"')
            elif "ABS_MT_POSITION_X" in line:
                in_touch = True
                self.max_x = self._last_int(line, "max") or self.max_x
            elif "ABS_MT_POSITION_Y" in line and in_touch:
                self.max_y = self._last_int(line, "max") or self.max_y
                if dev:
                    self.node = dev
                    self.name = name
        if not self.node:
            raise RuntimeError("no ABS_MT_POSITION_X device found in `getevent -pl`")

        r = self.run("wm size")
        for line in (r.stdout or "").splitlines():
            m = re.search(r"(\d+)x(\d+)", line)
            if m:
                self.w, self.h = int(m.group(1)), int(m.group(2))
        return self

    @staticmethod
    def _last_int(line, key):
        m = re.search(re.escape(key) + r"\s+(-?\d+)", line)
        return int(m.group(1)) if m else None

    def to_raw(self, x, y):
        rx = round(x * self.max_x / max(1, self.w - 1))
        ry = round(y * self.max_y / max(1, self.h - 1))
        return max(0, min(self.max_x, int(rx))), max(0, min(self.max_y, int(ry)))

    # ---- emit -----------------------------------------------------------

    def ev(self, etype, code, value):
        if self.bulk:
            self.buf.append(struct.pack("<qqHHi", 0, 0, etype, code, value))
        else:
            self.write_line("sendevent %s %d %d %d" % (self.node, etype, code, value))

    def flush(self):
        if not self.bulk:
            return
        if not self.buf:
            return
        blob = b"".join(self.buf)
        self.buf = []
        b64 = base64.b64encode(blob).decode()
        # single write(2) into the node, no per-event fork
        self.write_line("echo %s | base64 -d > %s" % (b64, self.node))

    def reset(self):
        for slot in range(10):
            self.ev(EV_ABS, ABS_MT_SLOT, slot)
            self.ev(EV_ABS, ABS_MT_TRACKING_ID, -1)
        self.ev(EV_ABS, ABS_MT_SLOT, 0)
        self.ev(EV_KEY, BTN_TOUCH, 0)
        self.ev(EV_KEY, BTN_TOOL_FINGER, 0)
        self.ev(EV_SYN, SYN_REPORT, 0)
        self.flush()

    # ---- gestures -------------------------------------------------------

    def tap(self, x, y, hold_ms=32):
        rx, ry = self.to_raw(x, y)
        self.ev(EV_ABS, ABS_MT_SLOT, 0)
        self.ev(EV_ABS, ABS_MT_TRACKING_ID, 1)
        self.ev(EV_ABS, ABS_MT_POSITION_X, rx)
        self.ev(EV_ABS, ABS_MT_POSITION_Y, ry)
        self.ev(EV_ABS, ABS_MT_TOUCH_MAJOR, 6)
        self.ev(EV_KEY, BTN_TOUCH, 1)
        self.ev(EV_KEY, BTN_TOOL_FINGER, 1)
        self.ev(EV_SYN, SYN_REPORT, 0)
        self.flush()
        time.sleep(hold_ms / 1000.0)
        self.ev(EV_ABS, ABS_MT_SLOT, 0)
        self.ev(EV_ABS, ABS_MT_TRACKING_ID, -1)
        self.ev(EV_KEY, BTN_TOUCH, 0)
        self.ev(EV_KEY, BTN_TOOL_FINGER, 0)
        self.ev(EV_SYN, SYN_REPORT, 0)
        self.flush()

    def swipe(self, x1, y1, x2, y2, dur_ms):
        steps = max(2, dur_ms // STEP_MS)
        rx, ry = self.to_raw(x1, y1)
        self.ev(EV_ABS, ABS_MT_SLOT, 0)
        self.ev(EV_ABS, ABS_MT_TRACKING_ID, 1)
        self.ev(EV_ABS, ABS_MT_POSITION_X, rx)
        self.ev(EV_ABS, ABS_MT_POSITION_Y, ry)
        self.ev(EV_ABS, ABS_MT_TOUCH_MAJOR, 6)
        self.ev(EV_KEY, BTN_TOUCH, 1)
        self.ev(EV_KEY, BTN_TOOL_FINGER, 1)
        self.ev(EV_SYN, SYN_REPORT, 0)
        self.flush()

        t0 = time.time()
        for s in range(1, steps + 1):
            target = t0 + (dur_ms / 1000.0) * s / steps
            remain = target - time.time()
            if remain > 0:
                time.sleep(remain)
            x = x1 + (x2 - x1) * s / steps
            y = y1 + (y2 - y1) * s / steps
            rx, ry = self.to_raw(x, y)
            self.ev(EV_ABS, ABS_MT_POSITION_X, rx)
            self.ev(EV_ABS, ABS_MT_POSITION_Y, ry)
            self.ev(EV_SYN, SYN_REPORT, 0)
            self.flush()

        self.ev(EV_ABS, ABS_MT_SLOT, 0)
        self.ev(EV_ABS, ABS_MT_TRACKING_ID, -1)
        self.ev(EV_KEY, BTN_TOUCH, 0)
        self.ev(EV_KEY, BTN_TOOL_FINGER, 0)
        self.ev(EV_SYN, SYN_REPORT, 0)
        self.flush()


def main():
    ap = argparse.ArgumentParser(description="sendevent fallback (degraded path)")
    ap.add_argument("--adb", default=DEFAULT_ADB)
    ap.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL"))
    ap.add_argument("--bulk", action="store_true",
                    help="packed input_event[] bursts written in one syscall (faster)")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("-n", type=int, default=5)
    sub = ap.add_subparsers(dest="op")
    p = sub.add_parser("tap"); p.add_argument("x", type=float); p.add_argument("y", type=float)
    p = sub.add_parser("swipe")
    for a in ("x1", "y1", "x2", "y2"):
        p.add_argument(a, type=float)
    p.add_argument("dur", type=int, nargs="?", default=300)
    sub.add_parser("probe")
    args = ap.parse_args()

    dev = Device(args.adb, args.serial)
    dev.bulk = args.bulk
    dev.buf = []
    dev.discover()
    print("node=%s name=%s max=(%d,%d) display=%dx%d"
          % (dev.node, getattr(dev, "name", "?"), dev.max_x, dev.max_y, dev.w, dev.h),
          file=sys.stderr)

    try:
        dev.reset()
        if args.op == "tap":
            t = time.perf_counter(); dev.tap(args.x, args.y)
            print(json.dumps({"ok": True, "route": "sendevent" if not args.bulk else "sendevent-bulk",
                              "raw_ms": round((time.perf_counter() - t) * 1000)}))
        elif args.op == "swipe":
            t = time.perf_counter()
            dev.swipe(args.x1, args.y1, args.x2, args.y2, args.dur)
            print(json.dumps({"ok": True, "route": "sendevent" if not args.bulk else "sendevent-bulk",
                              "raw_ms": round((time.perf_counter() - t) * 1000)}))
        elif args.op == "probe":
            print(json.dumps({"ok": True, "node": dev.node, "max_x": dev.max_x, "max_y": dev.max_y}))
        else:
            ap.print_help()
    finally:
        dev.reset()
        dev.close()


if __name__ == "__main__":
    sys.exit(main())
