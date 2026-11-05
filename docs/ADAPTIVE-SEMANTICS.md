# Adaptive Semantics — operating a phone the way a person does

Status: layers 1–3 implemented and verified on a Xiaomi 2210132C (HyperOS 3.0.310,
Android 16 / SDK 36, 1080x2400, KernelSU). Layer 4 designed, not built.

The design principle throughout: **never guess what a control is.** Either the
operating system said what it is, or we watched what happened when we touched it,
or we say we don't know. No OCR, no vision, no invented labels.

---

## The measured problem this exists to solve

Audit across 5 real apps, 889 nodes, foreground-verified:

| app | clickable nodes | OS-named | bare |
|---|---|---|---|
| com.miui.home (launcher) | 15 | 93.3% desc | 6.7% |
| com.miui.notes | 7 | 100% | 0.0% |
| com.quark.browser | 24 | 41.7% | 58.3% |
| com.android.settings | 14 | 21.4% | 78.6% |
| com.taobao.taobao | 44 | 25.0% | 75.0% |
| **com.tencent.mm (WeChat)** | **0** | — | **100%** |

- **43.3 % of tappable controls carry any OS label. 56.7 % carry none.**
- **WeChat reports 44 nodes with zero `clickable="true"`** — it draws custom views
  and handles touch in `onTouchEvent` without `setClickable(true)`. Any design that
  enumerates controls by the `clickable` flag reports WeChat as an empty screen.
- Six richer accessibility fields never appear in the dump: `tooltip-text`,
  `state-description`, `pane-title`, `actions`/`action-list`, `input-type`,
  `important-for-accessibility`. `hint` appeared twice in 889 nodes.
- `dumpsys activity top` yields **no view ids and no text on this ROM** (verified:
  `mID=` / `mText` / `android:text` = 0 lines; WeChat's whole output is 776 bytes).
  It is not a fallback here.

## The measured latency problem

| path | measured |
|---|---|
| `adb shell input tap` | 95–114 ms |
| persistent root shell, same tap | 32.2 ms (floor is `input`'s own JVM) |
| `screencap -p` end to end | 1895 ms (1751 ms is on-device zlib) |
| raw screencap + host decode | 615 ms, decode 0.00 ms (zero-copy numpy) |
| `am start -W` cold | 656–1033 ms |
| full hierarchy snapshot | ~330 ms |

---

## Layer 1 — Perception: OS-derived semantics (`src/ui_semantics.py`)

Turns one hierarchy dump into typed controls. Per node:

- `role` — button / icon_button / text_input / search / password_input / toggle /
  tab / list_item / nav_back / nav_close / slider / container …
  Includes **actionability promotion**: a custom `ViewGroup` with a reported click
  and a label is a `button`, which is what launchers and most Chinese apps ship.
- `label` + `label_source` — `text` | `desc` | `hint` | `resource_name`. Provenance
  is mandatory; a control with no name gets `""`, never a fabricated one.
- `actions` — only capabilities the OS actually reported.
- `semantic_key` — stable identity, **contains no coordinates**:
  `com.miui.home|button|系统工具`. Survives layout changes; enables replay without
  storing a tap position.
- `region` — top_bar / bottom_nav / dialog / content / floating / system_bar /
  left_rail, calibrated for a 144 px display cutout (`TOP_BAR_RATIO 0.14`,
  `BOTTOM_NAV_RATIO 0.12`), with per-call threshold overrides.
- `interactive_source` — `flag` (the OS said so) vs `inferred` (deduced from
  geometry/class/hierarchy). Inferred confidence is capped and marked
  `needs_probe`, so a caller can always tell a system fact from an inference.

`interactive_mode="inferred"` (default) restores the controls the `clickable` flag
loses — a scrollable list of 9 bare rows yields 10 candidates instead of 0, while a
full-viewport node is excluded.

Cost: **51.1 ms for 2000 nodes**. Deterministic, byte-identical across runs.

Verified live on the launcher: 38 controls, **29 OS-named (76.3 %)**, 17 inferred;
`role="button"` query returns 14 named buttons (天气 / 系统工具 / 日历 / 笔记).

## Layer 2 — Action: native devnode injection + act-and-observe (`act`)

**Transport.** A resident root daemon (`native/pfdaemon`, `app_process`, uid 0,
`setsid`) writes packed `input_event` bursts straight to `/dev/input/event7`.
ABS maxima are read from the hardware at startup (`maxX=14399 maxY=31999`,
13.333×), not hardcoded. Fallbacks: `injectInputEvent` (binds as UID 2000, no root)
and the original uiautomator2 / `adb input` paths. Client: `src/native_input.py`;
opt out with `ANDROID_MCP_NATIVE_INPUT=0`.

Measured through the real tool paths on the target device:

| operation | before | after |
|---|---|---|
| tap | 95–114 ms (`adb input tap`) | **40.8 ms** |
| swipe 300 ms | 664 ms end-to-end | **310.9 ms** (300 ms gesture + 11 ms) |
| key BACK / HOME | 75–114 ms | **16.5–41 ms**, `route: devnode` |
| `run_workflow`, 5 steps, no UI return | 663 ms | **215.9 ms** |
| `run_workflow`, 5 steps with UI return | 1028 ms | **597.4 ms** |

`text` does NOT go through this daemon: HyperOS rejects key injection via
`injectInputEvent` (verified, `injectInputEvent returned false` even after fixing
the `deviceId` bug), the touchscreen node carries no keyboard usage codes, and
there is no root clipboard CLI on this ROM. Unicode text stays on the working
uiautomator2 `set_text` / `send_keys` path.

**Verb.** One round trip returns the action *and* its consequence:

```
act(action, selector | x/y, value, observe=True)
  -> { ..., "effect": "opened a different screen (activity changed to …)",
             "effect_detail": {...}, "snapshot_id": ..., "current_package": ... }
```

The comparison baseline is the snapshot already cached from the caller's last
observation, so reporting the consequence costs **one** capture, not two.
Measured `act + observe`: **231 ms** total.

Verified: `act(key="HOME")` → `navigation` effect, package `com.miui.home`.

## Layer 3 — Learning: probe and remember (`src/probe_engine.py`, `src/control_memory.py`)

This is the part that handles the 56.7 %. A person does not read an unlabeled row —
they tap it and remember. So:

1. `choose_probe_target` picks the highest-value unknown control (unlabeled, not yet
   learned, content/bottom_nav before system bars, deterministic tie-break).
2. `is_safe_to_probe` refuses anything destructive or financial — 删除 / 支付 /
   退出登录 / uninstall / clear data / password fields — and records the refusal.
   Absence of a `clickable` flag is **not** grounds for refusal; that is the
   condition being resolved.
3. `diff_screens` classifies the consequence: `navigation` / `dialog_appeared` /
   `dialog_dismissed` / `scrolled` / `selection_changed` / `content_changed` /
   `no_visual_change`. Any node added or removed is always reported —
   `no_visual_change` strictly means nothing detectable differs.
4. `display` note: `no_visual_change` is stored as an honest observation which
   explicitly does not prove the control is inert.
5. The result is persisted per `(package, activity, control identity)` and appears
   as `learned_effect` on later `ui_describe` calls.

Durability: atomic JSON writes, schema-versioned, corrupt-file tolerant, bounded
(400 controls/screen, 2000 screens), thread-safe.
Cost: `diff_screens` on 2000×2000 nodes = **9.45 ms**.

Known limitation, chosen deliberately: identity for controls *without* a
`semantic_key` is position-anchored (role + ~43 px bucket), so a list scrolled
further than that re-learns the row. Keying on document order would survive
scrolling but mis-attribute after any insertion — "learn it again" is the safer
failure. Controls with a `semantic_key` (~76 % on the launcher) are unaffected.

## Layer 4 — Deep View (designed, NOT built)

Layers 1–3 raise the ceiling from 43 % to "everything learnable by acting". The
remaining gap is controls that are invisible to *both* the accessibility dump and
behavioural probing, plus the six missing accessibility fields.

- **AccessibilityService** (needs an APK + user enablement): adds
  `TYPE_VIEW_CLICKED`/`SCROLLED` events, tooltips, `state-description`,
  `getActionList()`, `input-type`. Learns interactivity from real touches instead of
  probing. Caveat: it reads the same `AccessibilityNodeInfo`, so WeChat still
  reports non-clickable.
- **LSPosed module** (device already runs LSPosed + KernelSU) — the only path that
  reads the *live* View object: `getTag()`, `getContentDescription()`, and
  reflectively whether `ListenerInfo.mOnClickListener` is non-null. That is the
  decisive signal for WeChat's self-drawn rows, which have no id but do have
  listeners attached.

Do not build layer 4 before layers 1–3 are exercised on real tasks — the learned
memory is what makes the cost of an unnamed control acceptable.

---

## Non-goals

- OCR / template matching / vision. Explicitly rejected.
- Fabricating labels for unlabeled controls.
- `dumpsys activity top` as a semantics source — it produces nothing on this ROM.
- Shipping the persistent-shell accelerator: measured **slower** than plain adb on
  this host (106 ms vs 66 ms per command), so it is gated off behind
  `ANDROID_MCP_FASTPATH=1`.
