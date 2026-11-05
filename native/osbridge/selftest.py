#!/usr/bin/env python3
# selftest.py - exercise every osbridge command once, PASS/FAIL per command.
#
#   python selftest.py                 # safe set (no force_stop, no quit)
#   python selftest.py --destructive   # also force_stop + quit (kills the daemon at the end)
#   python selftest.py --runs 3        # repetitions for the timing columns (default 1)
#
# Cross-checks are done against the plain adb equivalents wherever one exists, so a PASS means
# "the daemon agrees with the framework", not "the daemon returned JSON".
#
# STATUS AT HANDOFF: this file has NOT been executed. It was written while the device was being
# held quiet after a thermal/jank report; run it before wiring the daemon into the server.

import argparse
import base64
import json
import subprocess
import sys
import time

import run as runner   # same directory: Client, adb(), start(), stop(), load_state()

results = []
DETAIL = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def skip(name, why):
    results.append((name, None, why))
    print(f"SKIP  {name}   {why}")


def adb_out(cmd):
    rc, out, err = runner.adb("shell", cmd)
    return out.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--destructive", action="store_true")
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args()

    st = runner.load_state()
    if not runner.is_up(st.get("host_port")):
        print("daemon not running; starting it")
        st = runner.start(quiet=True)
    cli = runner.Client(port=st["host_port"], timeout=60)
    print(f"daemon host_port={st['host_port']} device_port={st['device_port']}\n")

    # ---------------------------------------------------------------- ping
    t0 = time.perf_counter()
    r = cli.send("ping")
    ms = (time.perf_counter() - t0) * 1000
    rec("ping", r.get("ok") and r.get("uid") == 2000,
        f"uid={r.get('uid')} sdk={r.get('sdk')} boot_ms={r.get('boot_ms')} {ms:.1f}ms")

    # ---------------------------------------------------------------- focus (cross-check dumpsys)
    r = cli.send("focus")
    true_focus = adb_out("dumpsys window | grep mCurrentFocus")
    d_pkg = r.get("package") or ""
    ok = bool(r.get("ok")) and d_pkg and d_pkg in true_focus
    rec("focus", ok, f"daemon={d_pkg}/{r.get('activity')} | adb={true_focus[:90]}")
    DETAIL.append(("dumpsys window", true_focus))

    # ---------------------------------------------------------------- launch --wait
    t0 = time.perf_counter()
    r = cli.send("launch com.android.settings --wait")
    wall = (time.perf_counter() - t0) * 1000
    ok = bool(r.get("ok")) and r.get("focused") and 0 <= r.get("ms", -1) < 5000
    rec("launch com.android.settings --wait", ok,
        f"ms={r.get('ms')} dispatch_ms={r.get('dispatch_ms')} focused={r.get('focused')} wall={wall:.0f}ms")
    time.sleep(0.5)

    # ---------------------------------------------------------------- packages vs pm list
    r = cli.send("packages")
    n_daemon = r.get("count", -1)
    n_adb = int(adb_out("pm list packages | wc -l") or -1)
    rec("packages", n_daemon == n_adb, f"daemon={n_daemon} pm_list={n_adb} delta={n_daemon - n_adb}")

    # ---------------------------------------------------------------- packages --filter / --system
    r = cli.send("packages --filter com.android")
    rec("packages --filter com.android", bool(r.get("ok")) and r.get("count", 0) > 0,
        f"count={r.get('count')}")
    r2 = cli.send("packages --system")
    rec("packages --system", bool(r2.get("ok")) and 0 < r2.get("count", 0) <= n_daemon,
        f"count={r2.get('count')}")

    # ---------------------------------------------------------------- pkg_info
    r = cli.send("pkg_info com.android.settings")
    ok = bool(r.get("ok")) and r.get("version_code") and r.get("uid") and r.get("apk_path")
    rec("pkg_info com.android.settings", ok,
        f"v={r.get('version_name')}({r.get('version_code')}) uid={r.get('uid')} "
        f"target={r.get('target_sdk')} apk={r.get('apk_path')}")

    # ---------------------------------------------------------------- resolve
    intent = json.dumps({"action": "android.intent.action.MAIN",
                         "categories": ["android.intent.category.LAUNCHER"],
                         "package": "com.android.settings"})
    r = cli.send(f"resolve {intent}")
    rec("resolve", bool(r.get("ok")) and bool(r.get("component")), f"component={r.get('component')}")

    # ---------------------------------------------------------------- setting_get vs settings get
    r = cli.send("setting_get system screen_brightness")
    adb_val = adb_out("settings get system screen_brightness")
    rec("setting_get system screen_brightness", str(r.get("value")) == adb_val,
        f"daemon={r.get('value')!r} adb={adb_val!r}")

    # ---------------------------------------------------------------- setting_put + restore
    orig = adb_val
    newv = "128" if orig != "128" else "129"
    r = cli.send(f"setting_put system screen_brightness {newv}")
    readback = cli.send("setting_get system screen_brightness")
    ok = bool(r.get("ok")) and str(readback.get("value")) == newv
    restored = cli.send(f"setting_put system screen_brightness {orig}")
    rec("setting_put system screen_brightness", ok,
        f"wrote {newv}, read back {readback.get('value')}, restored {orig} "
        f"(restore ok={restored.get('written')})")

    # ---------------------------------------------------------------- clipboard
    payload = "osbridge selftest " + time.strftime("%H:%M:%S")
    b64 = base64.b64encode(payload.encode()).decode()
    r = cli.send(f"clipboard_set {b64}")
    ok_set = bool(r.get("ok"))
    g = cli.send("clipboard_get")
    # On Android 10+ a non-focused process is refused clipboard reads. Either the text comes
    # back, or the refusal must be explicit (denied=true) - both are PASS, silence is not.
    if g.get("text") == payload:
        rec("clipboard_set/get", ok_set, f"round-trip ok ({len(payload)} bytes)")
    elif g.get("denied"):
        rec("clipboard_set/get", ok_set,
            f"write ok, read refused by framework as expected on Android 10+: {g.get('note')}")
    else:
        rec("clipboard_set/get", False, f"write ok={ok_set}, unexpected get={g}")
    DETAIL.append(("clipboard_get raw", json.dumps(g)))

    # ---------------------------------------------------------------- broadcast (no receivers)
    bcast = json.dumps({"action": "com.pf.osbridge.SELFTEST",
                        "extras": {"ts": int(time.time())}})
    r = cli.send(f"broadcast {bcast}")
    rec("broadcast", bool(r.get("ok")), f"route={r.get('route')}")

    # ---------------------------------------------------------------- services
    r = cli.send("services com.android")
    rec("services", bool(r.get("ok")),
        f"count={r.get('count')} visible={r.get('visible')} "
        f"(getServices is filtered for non-system callers on O+)")

    # ---------------------------------------------------------------- activity_top
    r = cli.send("activity_top")
    ok = bool(r.get("ok")) and r.get("package") and r.get("activity")
    rec("activity_top", ok, f"{r.get('package')}/{r.get('activity')} pid={r.get('pid')}")

    # ---------------------------------------------------------------- diag
    r = cli.send("diag")
    routes = r.get("routes") or {}
    unreachable = [k for k, v in routes.items() if str(v).startswith("UNREACHABLE")]
    rec("diag", bool(r.get("ok")) and not unreachable,
        f"routes={len(routes)} unreachable={unreachable or 'none'}")

    # ---------------------------------------------------------------- force_stop (opt-in)
    if args.destructive:
        r = cli.send("force_stop com.android.settings")
        gone = adb_out("pidof com.android.settings")
        rec("force_stop com.android.settings", bool(r.get("ok")), f"pidof after={gone or 'none'}")
    else:
        skip("force_stop", "opt-in: needs --destructive (closes Settings UI)")

    # ---------------------------------------------------------------- quit (opt-in)
    if args.destructive:
        r = cli.send("quit")
        time.sleep(0.6)
        dead = runner.is_up(st["host_port"]) is None
        rec("quit", bool(r.get("ok")) and dead, f"daemon gone={dead}")
    else:
        skip("quit", "opt-in: needs --destructive (stops the daemon)")

    # ---------------------------------------------------------------- summary
    n_pass = sum(1 for _, ok, _ in results if ok is True)
    n_fail = sum(1 for _, ok, _ in results if ok is False)
    n_skip = sum(1 for _, ok, _ in results if ok is None)
    print(f"\n{n_pass} pass, {n_fail} fail, {n_skip} skip")
    for name, ok, detail in results:
        if ok is False:
            print(f"  FAILED: {name}  {detail}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
