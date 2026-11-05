# pfdaemon — resident root input daemon

A long-lived `app_process` process that injects touch input without forking a JVM
per gesture. This exists because `adb shell input tap` costs **95–114 ms** on this
device, of which ~40–60 ms is the `input` binary's own `app_process` startup — a
cost no amount of shell reuse removes.

**Status: touch input route VERIFIED. Text/CJK route NOT delivered (see Gaps).**

---

## What bound

Measured on the target device (Xiaomi 2210132C, HyperOS, Android 16 / SDK 36,
arm64, 1080x2400, KernelSU):

| item | value |
|---|---|
| `ping` | `{"ok":true,"uid":0,"sdk":36,"route":"devnode","w":1080,"h":2400,"rot":0,"touch":"/dev/input/event7"}` |
| uid bound | **0 (root)** — started via `su -c` |
| primary route | **`devnode`** → `/dev/input/event7` ("fts") |
| secondary route | `injectInputEvent` via `android.hardware.input.InputManagerGlobal` — binds, but **rejects KeyEvents** (see Gaps) |
| ABS maxima | read from the device, not hardcoded: `maxX=14399 maxY=31999`, scale ≈ 13.333× |
| jar sha8 | `125d9be2` (rebuilt; adds the `KeyEvent` `deviceId` fix) |

Every gesture reply carries `"route":"devnode"` and `"raw_ms"` (the daemon's own
measured duration), so a caller can always tell which engine served it.

---

## Measured latency

Host-side wall clock around a socket round trip; `raw_ms` is the daemon's own
number. 20 taps / 10 swipes, medians.

| operation | host wall | daemon raw_ms | overhead |
|---|---|---|---|
| `tap` ×20 | **37.71 ms** (min 35.92, max 44.01) | 32 ms (min 32, max 36) | ≈5.7 ms after the deliberate 32 ms tap hold |
| `swipe 300 ms` ×10 | **306.02 ms** (min 303.93, max 312.08) | 300 ms | **+6.0 ms** |
| `key BACK` ×5 | **10.13 ms** | — | — |

For comparison, on the same device: `adb shell input tap` = 95–114 ms; a
persistent shell = 51.9 ms/tap; a persistent root shell = 32.2 ms/tap.

The swipe result is the important one: **a 300 ms gesture completes in 306 ms**,
i.e. the daemon adds ~6 ms on top of the gesture it was told to perform.

---

## Verification: the swipe really scrolls

Proven without screenshots. Method:

1. Focus `com.android.settings/com.android.settings.MiuiSettings`.
2. Capture the visible text set of the hierarchy (uiautomator2 RPC —
   `dumpsys activity top` returns no view tree on this ROM).
3. Send one `swipe 540 1800 540 600 300` through the daemon.
4. Capture the text set again and diff.

Result: the text set changed and the newly-appeared tokens were the
below-the-fold menu entries that a tap could not have revealed:

```
桌面  显示与亮度  声音与触感  指纹、面部与密码  隐私与安全  应用设置  省电与电池  健康使用手机
```

That is a scroll, not a tap.

## Verification: a stuck finger is released

A malformed gesture (`gesture [[100,100]] 500`) is rejected with
`{"ok":false,"err":"need_2_points"}`. Immediately after, a normal `tap` still
succeeds (`{"ok":true,"events":8,"route":"devnode","raw_ms":32}`) and `ping`
still answers. The daemon emits `ABS_MT_TRACKING_ID -1` + `SYN_REPORT` on every
error path, so a gesture that throws mid-swipe cannot leave the screen latched
down.

---

## Gaps — read before wiring this in

1. **Text / CJK is NOT delivered by this daemon.** Verified failures on this
   device, in order:
   - `injectInputEvent` returns `false` for every `KeyEvent`
     (`IllegalStateException: injectInputEvent returned false`), including after
     fixing the 10-arg `KeyEvent` constructor's `deviceId` from
     `KeyCharacterMap.VIRTUAL_KEYBOARD` (−1) to 0. Motion events inject fine;
     key events are rejected on HyperOS.
   - The devnode route cannot carry text: the touchscreen node has no keyboard
     usage codes.
   - There is no root clipboard CLI: `cmd clipboard set-text` →
     `"No shell command implementation"` (only the `clipboard` binder, service 114).
   - `input text 中文` hard-fails (`NullPointerException` in
     `InputShellCommand.sendText`).

   **Use the MCP server's existing uiautomator2 path for text** — `set_text` /
   `send_keys` handles CJK correctly and is already what the server calls. Do not
   route text through this daemon.

2. **`injectInputEvent` motion route is untested in my runs.** Every gesture I
   measured went over `devnode` because root was available. The inject path is
   the designed fallback for a non-root start (`run.py --no-root`) and it bound
   successfully, but I did not exercise it end-to-end here.

3. **`EV_SIZE = 24` is assumed, not read from a header.** There is no `python3`
   and no C toolchain on the device. It is supported empirically: taps land on
   the intended control and swipes scroll, which would not happen if the
   `struct input_event` layout were wrong.

4. **Rotation is only partly proven.** `toRaw()` maps logical→raw through the
   reported rotation and gives the correct result at rotation 0 (the only state
   this device was in). 90/180/270 are implemented but unverified.

5. **Failure modes not exercised:** lockscreen, display off, a gesture injected
   while a real finger is touching, ROM blocking `/dev/input/event7`. Injection
   during a real touch fights the hardware stream; the driver merges them, which
   can produce a visible jump.

---

## Start / stop / verify

Start (root, detached, surviving the caller):

```sh
adb -s $SERIAL shell "su -c 'cd /data/local/tmp/pfdaemon && \
  CLASSPATH=/data/local/tmp/pfdaemon/pfdaemon-<sha8>.jar \
  setsid app_process /system/bin com.pf.Daemon --port 47810 \
  >/data/local/tmp/pfdaemon/pfdaemon.log 2>&1 </dev/null &'"
adb -s $SERIAL forward tcp:0 tcp:47810      # prints the host port
```

Then talk line-JSON to `127.0.0.1:<host_port>`:

```
ping
tap 540 1200
swipe 540 1800 540 600 300
key 4
quit
```

Verify everything with the harness in this directory:

```sh
python verify.py ping
python verify.py bench --n 20
python verify.py scroll      # proves a swipe scrolls via hierarchy text diff
python verify.py stuck       # proves no stuck finger after an error
python verify.py cjk         # expected to FAIL - the daemon cannot type text
```

Stop — **never** `pkill -f com.pf.Daemon`; the pattern matches the adb shell's own
command line and kills the session before cleanup runs. Kill by pid found from
`/proc`:

```sh
adb -s $SERIAL shell "su -c 'for d in /proc/[0-9]*; do \
  c=\$(tr \"\\0\" \" \" < \$d/cmdline 2>/dev/null); \
  case \"\$c\" in *com.pf.Daemon*) kill -9 \"\${d#/proc/}\";; esac; done'"
```

Note `ps -A -o PID,NAME | grep com.pf.Daemon` returns **nothing even while the
daemon is running** on this ROM — a false negative. Use the `/proc` scan, or
`ping` over the forwarded port.

## Protocol

One command per line, one JSON reply per line:

```
ping | tap x y | long_press x y ms | swipe x1 y1 x2 y2 dur | gesture [[x,y],...] dur
     | pinch cx cy r1 r2 dur [rot] | key <code> | text <base64-utf8> | quit
```

Gesture replies include `route` and `raw_ms`. `text` accepts base64 and currently
fails at the injection layer — see Gaps.
