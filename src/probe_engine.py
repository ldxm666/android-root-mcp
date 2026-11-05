"""Learn what an unlabelled control does by acting on it and diffing the screen.

The perception layer names only a minority of controls. On this project's target
device 56.7% of tappable nodes carry no text, no description and no resource id,
and WeChat reports zero ``clickable`` flags because it handles touch in
``onTouchEvent``. For those controls the only honest source of meaning is
*observation*: tap it, look at what changed, remember it.

This module is deliberately device-free. It takes two hierarchy snapshots that
somebody else captured, says exactly what changed between them, and turns that
into a durable observation. Nothing here presses anything: the caller decides to
act, and :func:`is_safe_to_probe` exists so that decision can never reach a
control that deletes data or spends money.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

#: How many concrete pieces of evidence to keep. Enough to explain the change,
#: small enough that one noisy screen cannot bloat a prompt.
MAX_EVIDENCE = 12
#: y-quantum for matching nodes across two snapshots, in pixels. A 4 px shift
#: must not read as a different node; a real scroll moves far more than this.
MATCH_QUANTUM = 24
#: A matched population below this share of the smaller snapshot means the
#: screen was replaced rather than edited.
MATCHED_SHARE_FLOOR = 0.5
#: Confidence above which a control is considered learned and no longer worth
#: spending a probe on.
LEARNED_ENOUGH = 0.7

#: Regions ordered by how much a probe there teaches us. Content first: system
#: bars are usually framework chrome, not the app's own behaviour.
_REGION_RANK = {"content": 0, "bottom_nav": 1, "floating": 2, "dialog": 3,
                "left_rail": 4, "top_bar": 5, "system_bar": 9, "unknown": 8}

#: Never probe these. Destructive, financial, or identity-ending.
DENY_TOKENS: tuple[str, ...] = (
    "delete", "remove", "erase", "wipe", "format", "uninstall", "clear data",
    "logout", "log out", "sign out", "reset", "restore defaults",
    "pay", "payment", "purchase", "buy", "checkout", "place order",
    "call", "dial", "emergency",
    "删除", "移除", "清空", "格式化", "卸载", "恢复出厂", "重置", "注销",
    "登出", "退出登录", "退订", "支付", "付款", "购买", "下单", "结算",
    "拨号", "呼叫", "紧急",
)

#: Refused by default but permitted when the caller explicitly opts in, because
#: these are usually benign yet sit one step away from committed state.
SOFT_DENY_TOKENS: tuple[str, ...] = (
    "confirm", "submit", "send",
    "确定", "确认", "发送", "提交",
)

#: Wording that marks a bounded mid-screen panel as a dialog/permission prompt.
#: Many Chinese apps build these from plain containers with no class hint, so a
#: class-name test alone misses the most common case.
_DIALOG_TOKENS: tuple[str, ...] = (
    "permission", "allow", "deny", "ok", "cancel", "dialog", "alert", "popup", "modal",
    "权限", "允许", "拒绝", "确定", "取消", "对话框", "提示", "确认",
)

#: Roles that are interactive by construction even without a click flag.
_INTERACTIVE_ROLES = frozenset({
    "button", "icon_button", "tab", "list_item", "checkbox", "radio", "switch",
    "slider", "menu_item", "link", "text_input", "search", "password_input",
})
_NAV_ROLES = frozenset({"nav_back", "nav_close", "nav_home", "nav_menu", "nav_more"})
_INPUT_ROLES = frozenset({"text_input", "search", "password_input"})


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _bounds(node: Mapping[str, Any]) -> tuple[int, int, int, int]:
    raw = node.get("bounds")
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            return tuple(int(v) for v in raw)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return (0, 0, 0, 0)
    return (0, 0, 0, 0)


def _text_of(node: Mapping[str, Any]) -> str:
    for key in ("label", "text", "desc"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _elements(snapshot: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(snapshot, Mapping):
        return []
    nodes = snapshot.get("elements")
    if isinstance(nodes, (list, tuple)):
        return [n for n in nodes if isinstance(n, Mapping)]
    return []


def _screen_extent(nodes: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    """Infer the panel size from the widest/tallest bounds present."""
    width = height = 0
    for node in nodes:
        left, top, right, bottom = _bounds(node)
        width = max(width, right, left)
        height = max(height, bottom, top)
    return width or 1080, height or 2400


def _base_signature(node: Mapping[str, Any]) -> str:
    """Identity of a node ignoring absolute y.

    y is excluded on purpose: a scroll must still pair rows, and the shift is
    then measured rather than baked into the key. x, width and height are kept
    so genuinely different columns do not merge.
    """
    key = node.get("semantic_key")
    if isinstance(key, str) and key.strip():
        return "k:" + key.strip()
    left, top, right, bottom = _bounds(node)
    return "g:{}|{}|{}|{}".format(
        str(node.get("role") or ""), _text_of(node), left, max(0, bottom - top)
    )


def _identify(nodes: Sequence[Mapping[str, Any]]) -> list[str]:
    """Per-node ids, disambiguated by occurrence in document order.

    Lists frequently hold several rows that are identical in role, label and
    geometry. Without an occurrence counter they collapse into one identity and
    a scroll reads as "everything vanished". Document order is stable across a
    scroll, so ``row|#3`` still refers to the same row afterwards.
    """
    counts: dict[str, int] = {}
    ids: list[str] = []
    for node in nodes:
        base = _base_signature(node)
        occurrence = counts.get(base, 0)
        counts[base] = occurrence + 1
        ids.append("{}|#{}".format(base, occurrence))
    return ids


def _dialog_like(node: Mapping[str, Any], extent: tuple[int, int]) -> bool:
    """A dialog is a centred, bounded panel rather than a full-bleed screen.

    Two independent ways to qualify, because requiring a class name would miss
    most real prompts: the class/id mentions a dialog, OR the node is a bounded
    mid-screen region whose own wording looks like a prompt.
    """
    blob = " ".join(str(node.get(k) or "") for k in ("full_class", "resource_id", "id", "role"))
    lowered = blob.casefold()
    if "dialog" in lowered or "popup" in lowered or "modal" in lowered or "alert" in lowered:
        return True
    width, height = extent
    left, top, right, bottom = _bounds(node)
    if width <= 0 or height <= 0 or right <= left or bottom <= top:
        return False
    w_share = (right - left) / float(width)
    h_share = (bottom - top) / float(height)
    centre_share = ((left + right) / 2.0) / float(width)
    # Permission sheets are frequently full-width, so width is capped at 1.0
    # rather than requiring an inset panel; the height band and the wording
    # check are what keep ordinary full-screen content out.
    bounded_middle = (0.30 <= w_share <= 1.0 and 0.08 <= h_share <= 0.92
                      and abs(centre_share - 0.5) <= 0.10)
    if not bounded_middle:
        return False
    words = _blob(node)
    return any(token in words for token in _DIALOG_TOKENS)


def _evidence_of(node: Mapping[str, Any]) -> str:
    """One compact description of a node, for the evidence trail."""
    label = _text_of(node)
    role = str(node.get("role") or node.get("class") or "node")
    ident = node.get("resource_id") or node.get("id") or ""
    bits = [role]
    if label:
        bits.append(repr(label[:60]))
    if ident:
        bits.append(str(ident)[:60])
    return " ".join(bits)


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #
def diff_screens(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Classify what an action actually changed between two snapshots.

    Returns a dict with a primary ``kind`` (strongest supported claim), every
    supported ``kinds`` entry, counts, and a capped ``evidence`` list. The caller
    records ``kind`` and ``summary``; nothing here guesses a control's purpose.
    """
    old_nodes = _elements(before)
    new_nodes = _elements(after)
    extent = _screen_extent(list(old_nodes) + list(new_nodes))

    old_map: dict[str, Mapping[str, Any]] = dict(zip(_identify(old_nodes), old_nodes))
    new_map: dict[str, Mapping[str, Any]] = dict(zip(_identify(new_nodes), new_nodes))

    common = [k for k in new_map if k in old_map]
    appeared = [k for k in new_map if k not in old_map]
    disappeared = [k for k in old_map if k not in new_map]

    old_pkg = str((before or {}).get("current_package") or "")
    new_pkg = str((after or {}).get("current_package") or "")
    old_act = str((before or {}).get("current_activity") or "")
    new_act = str((after or {}).get("current_activity") or "")
    navigation = bool((old_pkg and new_pkg and old_pkg != new_pkg)
                      or (old_act and new_act and old_act != new_act))

    kinds: list[str] = []
    evidence: list[str] = []
    detail: dict[str, Any] = {}

    # -- 1. navigation outranks everything: the screen is not comparable. -----
    if navigation:
        kinds.append("navigation")
        detail["from"] = {"package": old_pkg, "activity": old_act}
        detail["to"] = {"package": new_pkg, "activity": new_act}
        changed_activity = new_act or new_pkg
        evidence.append("activity {!r} -> {!r}".format(old_act or old_pkg, new_act or new_pkg))

    # -- 2. dialogs: a panel appeared or vanished. ----------------------------
    appeared_dialogs = [new_map[k] for k in appeared if _dialog_like(new_map[k], extent)]
    gone_dialogs = [old_map[k] for k in disappeared if _dialog_like(old_map[k], extent)]
    if appeared_dialogs:
        kinds.append("dialog_appeared")
        detail["dialog_appeared"] = [_text_of(n) for n in appeared_dialogs[:3]]
        evidence.extend("appeared: " + _evidence_of(n) for n in appeared_dialogs[:MAX_EVIDENCE // 2])
    if gone_dialogs and not navigation:
        kinds.append("dialog_dismissed")
        detail["dialog_dismissed"] = [_text_of(n) for n in gone_dialogs[:3]]
        evidence.extend("vanished: " + _evidence_of(n) for n in gone_dialogs[:MAX_EVIDENCE // 2])

    # -- 3. state and text edits on nodes present in both. -------------------
    selection_changes: list[str] = []
    text_changes: list[str] = []
    for key in common:
        old, new = old_map[key], new_map[key]
        for flag in ("checked", "selected"):
            if bool(old.get(flag)) != bool(new.get(flag)):
                selection_changes.append("{} {} {} -> {}".format(
                    flag, _evidence_of(new), bool(old.get(flag)), bool(new.get(flag))))
        if _text_of(old) != _text_of(new) and not navigation:
            text_changes.append("{!r} -> {!r}".format(_text_of(old)[:40], _text_of(new)[:40]))
    if selection_changes:
        kinds.append("selection_changed")
        evidence.extend(selection_changes[:MAX_EVIDENCE // 2])
    if text_changes:
        kinds.append("text_changed")
        evidence.extend(text_changes[:MAX_EVIDENCE // 2])

    # -- 4. scroll: same population, consistent vertical shift. -------------
    deltas = []
    for key in common:
        old, new = old_map[key], new_map[key]
        dy = _bounds(new)[1] - _bounds(old)[1]
        if abs(dy) >= MATCH_QUANTUM:
            deltas.append(dy)
    moved = 0.0
    if deltas and len(deltas) >= max(2, len(common) // 3):
        moved = sorted(deltas)[len(deltas) // 2]
        same_sign = sum(1 for d in deltas if (d > 0) == (moved > 0))
        if same_sign >= 0.6 * len(deltas) and not text_changes and not selection_changes:
            kinds.append("scrolled")
            detail["scroll_delta"] = moved
            evidence.append("content shifted {:+.0f} px".format(moved))

    # -- 5. wholesale replacement. ------------------------------------------
    smaller = min(len(old_nodes), len(new_nodes)) or 1
    common_share = len(common) / float(smaller)
    replaced = bool((appeared or disappeared) and common_share < MATCHED_SHARE_FLOOR
                    and not navigation and "dialog_appeared" not in kinds)
    if replaced:
        kinds.append("content_replaced")
        evidence.append("{} nodes replaced by {}".format(len(disappeared), len(appeared)))

    # -- 6. structural add/remove that no stronger claim explains. ----------
    # `no_visual_change` must mean strictly nothing detectable differs, so an
    # added or removed node is always reported even when it is not a dialog.
    if not kinds and (appeared or disappeared):
        kinds.append("content_changed")
        for key in appeared[:MAX_EVIDENCE // 2]:
            evidence.append("added: " + _evidence_of(new_map[key]))
        for key in disappeared[:MAX_EVIDENCE // 2]:
            evidence.append("removed: " + _evidence_of(old_map[key]))

    # -- 7. nothing detectable at all. --------------------------------------
    if not kinds:
        kinds.append("no_visual_change")

    # Order the claims by strength so the primary kind is the strongest one.
    order = ["navigation", "dialog_appeared", "dialog_dismissed", "selection_changed",
             "text_changed", "scrolled", "content_replaced", "content_changed",
             "no_visual_change"]
    kinds.sort(key=lambda k: order.index(k) if k in order else len(order))

    return {
        "kind": kinds[0],
        "kinds": kinds,
        "changed_count": len(appeared) + len(disappeared)
                          + len(text_changes) + len(selection_changes),
        "appeared": len(appeared),
        "disappeared": len(disappeared),
        "matched": len(common),
        "matched_share": round(common_share, 3),
        "navigation": {"package": [old_pkg, new_pkg], "activity": [old_act, new_act]}
                      if navigation else None,
        "scroll_delta": detail.get("scroll_delta"),
        "dialog_labels": detail.get("dialog_appeared") or detail.get("dialog_dismissed") or [],
        "evidence": evidence[:MAX_EVIDENCE],
    }


def summarize_effect(diff: Mapping[str, Any]) -> str:
    """One short sentence a model or a human can read. Proves, never guesses."""
    kind = str(diff.get("kind") or "no_visual_change")
    if kind == "navigation":
        nav = diff.get("navigation") or {}
        act = nav.get("activity") or ["", ""]
        target = act[1] or ((nav.get("package") or ["", ""])[1])
        return "opened a different screen (activity changed to {})".format(target or "unknown")[:200]
    if kind == "dialog_appeared":
        labels = [l for l in (diff.get("dialog_labels") or []) if l]
        if labels:
            return "dialog appeared: {}".format(labels[0])[:200]
        return "a dialog-like panel appeared (unlabelled)"
    if kind == "dialog_dismissed":
        labels = [l for l in (diff.get("dialog_labels") or []) if l]
        if labels:
            return "dialog dismissed: {}".format(labels[0])[:200]
        return "a dialog-like panel disappeared"
    if kind == "selection_changed":
        return "a selection state toggled ({})".format(
            "; ".join(diff.get("evidence") or []) or "checked/selected changed")[:200]
    if kind == "text_changed":
        return "text content changed on {} control(s)".format(
            sum(1 for e in (diff.get("evidence") or []) if "->" in e))
    if kind == "scrolled":
        return "content scrolled by {:+.0f} px".format(float(diff.get("scroll_delta") or 0))
    if kind == "content_replaced":
        return "the visible content was replaced ({} gone, {} new)".format(
            diff.get("disappeared"), diff.get("appeared"))
    if kind == "content_changed":
        bits = [e for e in (diff.get("evidence") or []) if e.startswith(("added:", "removed:"))]
        detail = "; ".join(bits[:3])
        return ("the screen changed ({} added, {} removed){}".format(
            diff.get("appeared"), diff.get("disappeared"),
            ": " + detail if detail else ""))[:200]
    return "no visible change (this does not prove the control is inert)"


# --------------------------------------------------------------------------- #
# safety
# --------------------------------------------------------------------------- #
def _blob(control: Mapping[str, Any]) -> str:
    parts = [control.get("label"), control.get("role"), control.get("resource_id"),
             control.get("id"), control.get("text"), control.get("desc")]
    return " ".join(str(p) for p in parts if p).casefold()


def is_safe_to_probe(
    control: Mapping[str, Any],
    package: str | None = None,
    denylist: Sequence[str] | None = None,
    allow_soft_deny: bool = False,
    allow_nav: bool = False,
    allow_input: bool = False,
) -> tuple[bool, str]:
    """Decide whether activating this control could destroy state or cost money.

    Returns ``(safe, reason)``. Conservative by design: a refused probe costs one
    unknown, an unsafe one costs the user's data or money.
    """
    role = str(control.get("role") or "").casefold()
    region = str(control.get("region") or "").casefold()
    blob = _blob(control)

    for token in (denylist or ()):
        if token and str(token).casefold() in blob:
            return False, "caller denylist matched {!r}".format(token)

    for token in DENY_TOKENS:
        if token in blob:
            return False, "destructive or financial wording {!r}".format(token)

    if role == "password_input" or bool(control.get("password")):
        return False, "password field: probing it cannot teach a safe meaning"

    if role in _INPUT_ROLES and not allow_input:
        return False, "input field: typing changes state without proving behaviour"

    if region == "system_bar":
        return False, "system bar: framework chrome, not app behaviour"

    if role in _NAV_ROLES and not allow_nav:
        return False, "navigation control: probing it loses the current screen"

    if not allow_soft_deny:
        for token in SOFT_DENY_TOKENS:
            if token in blob:
                return False, "committing wording {!r}; pass allow_soft_deny=True to override".format(token)

    if not control.get("enabled", True):
        return False, "control is disabled"

    if not _is_interactive(control):
        return False, "not reported as an interactive control"

    return True, "harmless content control"


def _is_interactive(control: Mapping[str, Any]) -> bool:
    """Is this node a control at all?

    Deliberately generous: the whole point of probing is the *inferred* opaque
    row that reports no click flag, so requiring `clickable` would refuse
    exactly the controls worth learning. Absence of a flag is not evidence of
    being inert - it is the condition we are trying to resolve.
    """
    if str(control.get("interactive_source") or "").casefold() in ("flag", "inferred"):
        return True
    if control.get("needs_probe"):
        return True
    if _has_click(control):
        return True
    if control.get("editable"):
        return True
    if str(control.get("role") or "").casefold() in _INTERACTIVE_ROLES:
        return True
    if control.get("interactive") is True:
        return True
    return False


def _has_click(control: Mapping[str, Any]) -> bool:
    actions = control.get("actions")
    if isinstance(actions, (list, tuple)) and "click" in [str(a) for a in actions]:
        return True
    return bool(control.get("clickable"))


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def _region_rank(control: Mapping[str, Any]) -> int:
    return _REGION_RANK.get(str(control.get("region") or "unknown").casefold(), 8)


def choose_probe_target(
    controls: Sequence[Mapping[str, Any]],
    memory: Any = None,
    package: str | None = None,
    activity: str | None = None,
    screen_size: Sequence[int] | None = None,
    allow_soft_deny: bool = False,
    allow_nav: bool = False,
) -> Mapping[str, Any] | None:
    """Pick the single most informative control to learn next, or None.

    Preference: unknown controls over known ones, bare over labelled, content
    region over chrome, then a deterministic tie-break so repeated calls agree.
    """
    candidates: list[tuple[tuple, Mapping[str, Any]]] = []
    for control in controls:
        if not isinstance(control, Mapping):
            continue
        safe, _reason = is_safe_to_probe(control, package, None, allow_soft_deny, allow_nav)
        if not safe:
            continue
        bare = control.get("needs_probe") or not _text_of(control)
        learned = 0.0
        if memory is not None and package is not None:
            record = memory.lookup(package, activity, control, screen_size)
            if record:
                learned = float(record.get("confidence") or 0.0)
        if learned >= LEARNED_ENOUGH:
            continue
        # Unknown (0.0) sorts before partially learned; bare sorts before labelled.
        rank = (_region_rank(control),
                0 if bare else 1,
                round(learned, 3),
                str(control.get("semantic_key") or ""),
                int(control.get("index") or 0))
        candidates.append((rank, control))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


def plan_probe_round(
    controls: Sequence[Mapping[str, Any]],
    memory: Any = None,
    package: str | None = None,
    activity: str | None = None,
    budget: int = 1,
    screen_size: Sequence[int] | None = None,
    allow_soft_deny: bool = False,
    allow_nav: bool = False,
) -> dict[str, Any]:
    """Plan one learning round: which control, why, and how to get back.

    ``budget`` is advisory: this returns the next target only. A caller wanting
    several rounds loops and re-snapshots between them, because the screen must
    be re-observed after each action for the diff to mean anything.
    """
    if budget < 1:
        return {"target": None, "refused": True, "reason": "budget is zero"}
    target = choose_probe_target(controls, memory, package, activity,
                                 screen_size, allow_soft_deny, allow_nav)
    if target is None:
        return {
            "target": None,
            "refused": True,
            "reason": "no safe unlearned control on this screen"
                      + ("" if controls else " (no controls supplied)"),
        }
    safe, reason = is_safe_to_probe(target, package, None, allow_soft_deny, allow_nav)
    if not safe:  # defensive: choose_probe_target already filtered
        return {"target": None, "refused": True, "reason": reason}
    return {
        "target": dict(target),
        "refused": False,
        "reason": "best unlearned {} control in region {!r}".format(
            target.get("role") or "unknown", target.get("region") or "unknown"),
        "restore_hint": "record package/activity ({}/{}) before tapping; if the activity "
                        "changed, one BACK should return, otherwise re-open the app".format(
                            package or "?", activity or "?"),
        "budget": budget,
    }


def record_round(
    before: Mapping[str, Any] | None,
    target: Mapping[str, Any],
    after: Mapping[str, Any] | None,
    memory: Any,
    package: str | None = None,
    activity: str | None = None,
    screen_size: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Diff, summarise and persist one observation in a single call.

    ``package``/``activity`` default to the *before* snapshot's own identity, so
    a control is filed under the screen it was actually tapped on.
    """
    diff = diff_screens(before, after)
    summary = summarize_effect(diff)
    pkg = package if package is not None else str((before or {}).get("current_package") or "")
    act = activity if activity is not None else str((before or {}).get("current_activity") or "")
    effect = {
        "kind": diff["kind"],
        "summary": summary,
        "detail": "; ".join(diff.get("evidence") or [])[:300],
    }
    record = memory.record(
        pkg, act, target, effect,
        screen_size=screen_size,
        snapshot_before=str((before or {}).get("snapshot_id") or "") or None,
        snapshot_after=str((after or {}).get("snapshot_id") or "") or None,
    )
    return {"diff": diff, "summary": summary, "effect": effect,
            "screen": {"package": pkg, "activity": act}, "record": record}
