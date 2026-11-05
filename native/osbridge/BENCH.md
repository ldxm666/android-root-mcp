# osbridge — measured numbers

Two rules for this file: every number here was observed on the device, and anything not observed is
labelled **unmeasured** rather than estimated.

Device: Xiaomi 2210132C, Android 16 / SDK 36, arm64, shell uid 2000, serial `<your-device-serial>`.
Host: Windows, `adb` from `ADB_PATH` (or whatever is on `PATH`), JDK 21, build-tools 36.0.0.

---

## 1. What IS measured

### 1.1 Daemon cold start — the cost we pay once instead of per call

The daemon's own startup timer, printed into `/data/local/tmp/osbridge.log` as `boot_ms`. It covers
JVM bring-up, `ActivityThread.systemMain()`, all five binder service binds, and the listening socket.

| run | boot_ms | source |
|---|---:|---|
| 1 | 62 | `osbridge up: uid=2000 sdk=36 port=27507 boot_ms=62` |
| 2 | 54 | `osbridge up: uid=2000 sdk=36 port=27521 boot_ms=54` |
| 3 | 52 | `osbridge up: uid=2000 sdk=36 port=27529 boot_ms=52` (diag run) |

So the entire one-time startup is **~55 ms**. Compare with a single `adb shell am start -W`, which
pays a fresh `app_process` JVM of the same order *for every call*.

### 1.2 Launch command wall time

| what | measured |
|---|---:|
| `adb shell -T "CLASSPATH=... app_process ... &"` (the detached start command, host wall clock) | 139 ms |

The daemon is serving ~55 ms after that command is issued; the remaining ~85 ms is the adb client.

### 1.3 Binder routes that actually bound (from `diag`, three runs, identical)

| service | bound route on SDK 36 |
|---|---|
| IActivityTaskManager | `ActivityTaskManager.getService()` |
| IActivityManager | `ActivityManager.getService()` |
| IPackageManager | `AppGlobals.getPackageManager()` |
| IWindowManager | `WindowManagerGlobal.getWindowManagerService()` |
| IClipboard | `IClipboard$Stub.asInterface(ServiceManager.getService("clipboard"))` |
| system Context | `ActivityThread.systemMain().getSystemContext()` — **requires `Looper.prepareMainLooper()` first** |

Nothing had to be abandoned: every service in the protocol bound on its first candidate route.

---

## 2. What is NOT measured (yet)

**No per-command latency number exists.** The daemon bound every service and printed its route map,
but the line protocol was never driven end-to-end through an `adb forward` before the device was
taken offline for a thermal/jank report. Any table with milliseconds in it here would be fiction.

| operation | daemon median | adb-shell equivalent | speedup |
|---|---:|---:|---:|
| `focus` | unmeasured | unmeasured (`dumpsys window \| grep mCurrentFocus`) | — |
| `launch <pkg> --wait` | unmeasured | unmeasured (`am start -W <pkg>`) | — |
| `packages` | unmeasured | unmeasured (`pm list packages`) | — |
| `setting_get` | unmeasured | unmeasured (`settings get system screen_brightness`) | — |
| `pkg_info` | unmeasured | unmeasured (`dumpsys package <pkg>`) | — |
| `adb shell true` baseline | — | unmeasured | — |

First-call latency (JIT warm-up on each path) is also unmeasured and must be reported separately
from steady state, exactly as it is a different phenomenon: it is paid once per daemon lifetime.

---

## 3. How to fill the table

```powershell
cd D:\Tools\android-root-mcp\native\osbridge
python run.py --bench            # 5 runs per case, median, writes bench_results.json
python selftest.py               # PASS/FAIL per command, with cross-checks against adb
```

`run.py --bench` measures what an agent actually pays:

- **daemon**: open a TCP socket to the forwarded host port, write one line, read one line.
  No process is spawned; the forward is created once at `start`.
- **adb**: spawn `adb.exe` and wait for the shell command to exit. For `am`/`pm`/`settings`/`dumpsys`
  that includes a fresh `app_process` JVM (~55 ms floor, measured above), plus adb client overhead.

It also prints `adb shell true` as a baseline so the client-side overhead is separated from the JVM.
`bench_results.json` carries the raw per-run samples, not just the medians.

### Benchmarking etiquette on this device

The phone thermally throttles: a benchmark sweep was measured at 11.19 % janky frames / 93 ms p90
while several tasks were hitting it at once. Run `--bench` alone, on a cool device, and never in
parallel with another adb sweep — the numbers will be noise otherwise.
