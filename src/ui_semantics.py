"""Turn an Android hierarchy dump into semantic, actionable controls.

Purely derived from what the OS reported. No OCR, no vision, no guessing.

Provenance rule
---------------
Every field that is *read from the system* says so in ``basis`` / ``label_source``.
Every field that is *inferred* says so too. A caller must always be able to tell
"the OS said this" (``label_source == "text"``) from "we derived this"
(``label_source == "resource_name"``). When nothing usable is reported, ``label``
is the empty string -- we never invent placeholder names like "unknown button 3".

The one thing a hierarchy dump genuinely cannot tell us is the keyboard/input
type of a text field. That field is returned as ``None`` with a note rather than
being guessed.

Determinism: identical input produces a byte-identical result. No set iteration
reaches the output, every list is explicitly ordered.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "describe_control",
    "classify_region",
    "build_spatial_model",
    "find_by_semantics",
    "describe_screen",
    "resolve_resource_hint",
    "ROLES",
    "REGIONS",
    "ACTIONS",
]

# --------------------------------------------------------------------------- #
# Geometry thresholds.
#
# These are CALIBRATION, not physics. They were tuned against a real 1080x2400
# panel that reports a 144 px display cutout (``DisplayCutout insets=Rect(0,144,0,0)``).
# A bar band must therefore start below the status bar, not at y=0. Every value
# here can be overridden per call via the ``thresholds`` argument, so a caller on
# a different panel can retune without editing this module.
# --------------------------------------------------------------------------- #
TOP_BAR_RATIO = 0.14
"""Top app bar: 336 px on a 2400 px screen = 144 px cutout + a 96-192 px app bar."""

BOTTOM_NAV_RATIO = 0.12
"""Bottom navigation starts at y >= 2112 px on a 2400 px screen."""

SYSTEM_BAR_RATIO = 0.02
"""The bottom 2% is the gesture/system strip: a pill or a nav-bar backdrop."""

LEFT_RAIL_RATIO = 0.12
"""A tall node inside the leftmost 12% is a rail (tablet/landscape nav)."""

MIN_BAR_ITEM_W_RATIO = 0.02
"""A bar item narrower than 2% of the width is a dot, not a control."""

BAR_BOUND_TOLERANCE = 0.05
"""A bar item may not sit further outside the screen than 5% of its width."""

DIALOG_MIN_W = 0.40
DIALOG_MAX_W = 0.95
DIALOG_MIN_H = 0.20
DIALOG_MAX_H = 0.90
DIALOG_CENTER_TOLERANCE = 0.15
"""A bounded, roughly centred, non-full-bleed node is a dialog."""

FLOATING_MAX_W = 0.25
FLOATING_MAX_H = 0.15

INTERACTIVE_MODES = ("strict", "inferred")
"""``strict`` trusts only reported flags. ``inferred`` also accepts deduced controls.

Measured reason this exists: WeChat renders touch handling in ``onTouchEvent``
without ever calling ``setClickable(true)``, so a 44-node WeChat screen reports
ZERO clickable nodes while the pixels are perfectly tappable. A strict-only
engine answers "empty screen", which is worse than an honest guess.
"""

FULL_VIEWPORT_AREA_RATIO = 0.92
"""A node covering this much of the screen is a root/scroll host, never a control."""

INFERRED_MIN_AREA_RATIO = 0.002
"""Below this share of the screen a node is a dot or a spacer, not a control."""

INFERRED_CONFIDENCE_CAP = 0.60
"""An inferred control may never claim the confidence of an OS-confirmed one."""

DEFAULT_THRESHOLDS: dict[str, float] = {
    "top_bar": TOP_BAR_RATIO,
    "bottom_nav": BOTTOM_NAV_RATIO,
    "system_bar": SYSTEM_BAR_RATIO,
    "left_rail": LEFT_RAIL_RATIO,
    "min_bar_item_w": MIN_BAR_ITEM_W_RATIO,
    "bar_bound_tolerance": BAR_BOUND_TOLERANCE,
    "dialog_min_w": DIALOG_MIN_W,
    "dialog_max_w": DIALOG_MAX_W,
    "dialog_min_h": DIALOG_MIN_H,
    "dialog_max_h": DIALOG_MAX_H,
    "dialog_center": DIALOG_CENTER_TOLERANCE,
    "floating_max_w": FLOATING_MAX_W,
    "floating_max_h": FLOATING_MAX_H,
}


def resolve_thresholds(overrides: Mapping[str, float] | None = None) -> dict[str, float]:
    """Merge caller overrides over the calibrated defaults.

    Unknown keys are rejected rather than silently ignored, so a typo in a
    retune cannot quietly leave the default in force.
    """
    merged = dict(DEFAULT_THRESHOLDS)
    if not overrides:
        return merged
    for key, value in overrides.items():
        if key not in merged:
            raise ValueError(f"unknown threshold {key!r}; known: {sorted(merged)}")
        merged[key] = float(value)
    return merged

ROLES = (
    "button", "icon_button", "toggle", "checkbox", "radio", "switch",
    "text_input", "search", "password_input", "tab", "list_item", "link",
    "menu_item", "slider", "image", "label", "container",
    "nav_back", "nav_close", "nav_menu", "nav_home", "nav_more", "unknown",
)

REGIONS = (
    "top_bar", "bottom_nav", "left_rail", "content", "dialog", "floating",
    "system_bar",
)

ACTIONS = (
    "click", "long_click", "set_text", "clear", "check", "uncheck", "scroll",
    "focus", "read",
)

NAV_ROLES = ("nav_back", "nav_close", "nav_menu", "nav_home", "nav_more")

_INPUT_ROLES = ("text_input", "search", "password_input")

# --------------------------------------------------------------------------- #
# Role inference tables
# --------------------------------------------------------------------------- #
_FRAMEWORK_PREFIXES = (
    "btn", "iv", "tv", "et", "ib", "rl", "ll", "fl", "img", "icon", "ic",
)

# Simple class name -> role. "generic" roles may be refined by id tokens.
_CLASS_ROLE: dict[str, str] = {
    "button": "button",
    "materialbutton": "button",
    "appcompatbutton": "button",
    "imagebutton": "icon_button",
    "floatingactionbutton": "icon_button",
    "fab": "icon_button",
    "edittext": "text_input",
    "textinputedittext": "text_input",
    "autocompletetextview": "text_input",
    "searchview": "search",
    "searchautocompletetextview": "search",
    "checkbox": "checkbox",
    "checkedtextview": "checkbox",
    "radiobutton": "radio",
    "switch": "switch",
    "switchcompat": "switch",
    "togglebutton": "toggle",
    "seekbar": "slider",
    "textview": "label",
    "imageview": "image",
    "tab": "tab",
    "recyclerview": "container",
    "listview": "container",
    "gridview": "container",
    "scrollview": "container",
    "nestedscrollview": "container",
    "horizontalscrollview": "container",
    "viewpager": "container",
    "linearlayout": "container",
    "framelayout": "container",
    "relativelayout": "container",
    "constraintlayout": "container",
    "coordinatorlayout": "container",
    "cardview": "container",
    "webview": "container",
    "toolbar": "container",
    "viewgroup": "container",
    "view": "container",
}

# Roles a resource-id token is allowed to refine. A specific class-derived role
# (checkbox, switch, slider, tab, ...) is trusted over the id token.
_GENERIC_ROLES = frozenset({"button", "icon_button", "label", "image", "container", "unknown"})

_TOKEN_ROLE: dict[str, str] = {
    "btn": "button",
    "button": "button",
    "iv": "icon_button",
    "img": "icon_button",
    "image": "icon_button",
    "icon": "icon_button",
    "ic": "icon_button",
    "et": "text_input",
    "edit": "text_input",
    "input": "text_input",
    "edittext": "text_input",
    "field": "text_input",
    "cb": "checkbox",
    "checkbox": "checkbox",
    "rb": "radio",
    "radio": "radio",
    "sw": "switch",
    "switch": "switch",
    "toggle": "toggle",
    "tab": "tab",
    "search": "search",
    "slider": "slider",
    "seek": "slider",
    "back": "nav_back",
    "close": "nav_close",
    "menu": "nav_menu",
    "home": "nav_home",
    "more": "nav_more",
}

_VERSION_SUFFIXES = frozenset({"v1", "v2", "v3", "v4"})

# Classes that are interactive by nature even when no flag was reported.
_WIDGET_CLASSES = frozenset({
    "edittext", "textinputedittext", "autocompletetextview", "button",
    "materialbutton", "appcompatbutton", "imagebutton", "checkbox",
    "radiobutton", "switch", "switchcompat", "togglebutton", "seekbar", "tab",
    "checkedtextview",
})

# Layout classes that become controls only when they are a rendered leaf.
_GENERIC_CONTAINERS = frozenset({
    "linearlayout", "framelayout", "relativelayout", "constraintlayout",
    "coordinatorlayout", "viewgroup", "view", "cardview",
})

# Parents that make a leaf container a strong control candidate.
_SCROLLER_CLASSES = frozenset({"recyclerview", "listview", "viewpager", "gridview"})


def _empty_context() -> dict:
    return {"screen_area": 0.0, "by_index": {}, "parents": set(), "screen_size": (0, 0)}


def build_context(
    nodes: Sequence[Mapping[str, Any]], screen_size: Sequence[int]
) -> dict:
    """Precompute the parent/child and area facts inference needs.

    Built once per screen so interactivity inference stays O(1) per node
    instead of scanning for children.
    """
    width = int(screen_size[0]) if len(screen_size) > 0 else 0
    height = int(screen_size[1]) if len(screen_size) > 1 else 0
    by_index: dict[int, Mapping[str, Any]] = {}
    parents: set[int] = set()
    for position, node in enumerate(nodes):
        index = _index_of(node, position)
        by_index[index] = node
        parent = node.get("parent")
        if isinstance(parent, int) and not isinstance(parent, bool):
            parents.add(parent)
    return {
        "screen_area": float(width * height),
        "by_index": by_index,
        "parents": parents,
        "screen_size": (width, height),
    }


# --------------------------------------------------------------------------- #
# Small tolerant accessors -- every key in the input may be missing
# --------------------------------------------------------------------------- #
def _get(node: Mapping[str, Any], key: str, default: Any = None) -> Any:
    value = node.get(key, default)
    return default if value is None else value


def _bounds(node: Mapping[str, Any]) -> tuple[int, int, int, int]:
    raw = node.get("bounds")
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            left, top, right, bottom = (int(v) for v in raw)
            return left, top, right, bottom
        except (TypeError, ValueError):
            pass
    return 0, 0, 0, 0


def _area(node: Mapping[str, Any]) -> int:
    left, top, right, bottom = _bounds(node)
    return max(0, right - left) * max(0, bottom - top)


def _center(node: Mapping[str, Any]) -> tuple[int, int]:
    raw = node.get("center")
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            return int(raw[0]), int(raw[1])
        except (TypeError, ValueError):
            pass
    left, top, right, bottom = _bounds(node)
    return (left + right) // 2, (top + bottom) // 2


def _flags(node: Mapping[str, Any]) -> list[str]:
    raw = node.get("flags")
    if isinstance(raw, (list, tuple)):
        return [str(f) for f in raw]
    return []


def _has_flag(node: Mapping[str, Any], name: str) -> bool:
    if bool(node.get(name)):
        return True
    return name in _flags(node)


def _index_of(node: Mapping[str, Any], fallback: int = -1) -> int:
    raw = node.get("index")
    if isinstance(raw, bool):
        return fallback
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return fallback


def _simple_class(node: Mapping[str, Any]) -> str:
    full = str(_get(node, "full_class", ""))
    simple = str(_get(node, "class", ""))
    source = full or simple
    if not source:
        return ""
    return source.rsplit(".", 1)[-1].lower()


# --------------------------------------------------------------------------- #
# 6. Resource id parsing
# --------------------------------------------------------------------------- #
def resolve_resource_hint(resource_id: str | None) -> dict:
    """Split an Android resource id into package, name, tokens and a human name.

    Handles ``pkg:id/name``, a bare ``name``, and opaque numeric ids such as
    ``0x7f0a03fc`` (for which no name is recoverable, so ``tokens`` is empty and
    ``humanized`` is empty rather than invented).

    Framework prefixes (``btn_``, ``iv_``, ``ic_`` ...) are stripped when
    humanizing but preserved in ``prefix`` so callers can still reason about the
    widget family. Trailing version noise (``_v2``, ``_2``) is dropped from the
    humanized form only.
    """
    empty = {"package": "", "name": "", "tokens": [], "prefix": "", "humanized": ""}
    if not resource_id:
        return empty
    raw = str(resource_id).strip()
    if not raw:
        return empty

    lowered = raw.lower()
    if lowered.startswith("0x") or raw.isdigit():
        # Opaque id: the dump carries no recoverable name.
        return empty

    package = ""
    name = raw
    if "/" in raw:
        head, _, tail = raw.partition("/")
        package = head
        if ":" in package:
            package = package.split(":", 1)[0]
        name = tail
    if not name:
        return {**empty, "package": package}

    tokens = [t for t in name.lower().split("_") if t]
    # A leading token is a prefix when it names a widget family (btn_, et_, ...)
    # or a semantic role (tab_, nav_, search_). Either way it is noise in a
    # human label, so it is stripped from the label and preserved in `prefix`.
    prefix = tokens[0] if tokens and (tokens[0] in _FRAMEWORK_PREFIXES or tokens[0] in _TOKEN_ROLE) else ""

    body = tokens[1:] if prefix else list(tokens)
    while body and body[-1] in _VERSION_SUFFIXES:
        body.pop()
    while len(body) > 1 and body[-1].isdigit():
        body.pop()

    if body:
        humanized = " ".join(body)
    elif len(tokens) == 1:
        # Nothing survived stripping; the bare token is the best real name.
        humanized = tokens[0]
    else:
        humanized = ""

    return {
        "package": package,
        "name": name,
        "tokens": tokens,
        "prefix": prefix,
        # Non-ASCII names pass through untouched; lower() is a no-op for CJK.
        "humanized": humanized if any(ord(c) > 127 for c in name) else humanized,
    }


def _token_role(tokens: Sequence[str]) -> str:
    """Role implied by an id, considering only its leading token(s).

    Scanning every token produces false positives -- an id like
    ``home_address`` would otherwise read as a navigation button because it
    happens to contain "home". Android id conventions put the role marker first
    (``btn_``, ``et_``, ``tab_``, ``nav_back``), optionally behind one widget
    prefix, so only those two positions are consulted.
    """
    if not tokens:
        return ""
    first = _TOKEN_ROLE.get(tokens[0])
    if first:
        return first
    if tokens[0] in _FRAMEWORK_PREFIXES and len(tokens) > 1:
        return _TOKEN_ROLE.get(tokens[1], "")
    return ""


# --------------------------------------------------------------------------- #
# 1. describe_control
# --------------------------------------------------------------------------- #
def _promote_actionable(
    node: Mapping[str, Any], role: str, label: str, basis: list[str]
) -> str:
    """Promote a clickable, labelled generic node to an actionable role.

    Launchers and most Chinese apps build buttons from custom ViewGroup /
    FrameLayout subclasses plus a click flag, never ``android.widget.Button``.
    Trusting the class alone collapses every one of them to ``container`` and
    makes ``role="button"`` return nothing on a real screen. Promotion is
    confined to generic roles and requires the OS's own ``click`` capability:
    a ``long_click``-only or ``scroll``-only node is never promoted.
    """
    if role not in ("container", "image", "label"):
        return role
    if not _has_flag(node, "clickable"):
        return role
    if label:
        basis.append(f"promoted_from_{role}_by_click")
        return "button"
    left, top, right, bottom = _bounds(node)
    width, height = right - left, bottom - top
    if width > 0 and height > 0 and 0.6 <= width / height <= 1.67:
        # Square-ish, clickable, unnamed: an icon. Still tappable, and
        # needs_probe tells the caller the only way to learn more is to act.
        basis.append(f"promoted_from_{role}_by_click_iconish")
        return "icon_button"
    return role


def _derive_role(
    node: Mapping[str, Any], tokens: Sequence[str], label: str = ""
) -> tuple[str, list[str]]:
    role, basis = _derive_role_base(node, tokens)
    return _promote_actionable(node, role, label, basis), basis


def _derive_role_base(
    node: Mapping[str, Any], tokens: Sequence[str]
) -> tuple[str, list[str]]:
    """Role plus the ordered system facts it was derived from.

    Precedence, documented because it is a judgement call: a password field and
    a nav_* id are *more specific* than the widget class, so they win; otherwise
    the class is trusted first, then id tokens, then flags.
    """
    basis: list[str] = []
    simple = _simple_class(node)
    cls_role = _CLASS_ROLE.get(simple, "")
    if simple:
        basis.append(f"class={simple}" if cls_role else f"class={simple}(unmapped)")

    token_role = _token_role(tokens)
    if token_role:
        basis.append(f"id_token={token_role}")

    if _has_flag(node, "password"):
        basis.append("flag=password")
        return "password_input", basis

    if token_role in NAV_ROLES:
        # Only a control-shaped node may claim a nav role from its id alone;
        # otherwise a text field named "home_address" would read as a Home key.
        nav_plausible = (
            _has_flag(node, "clickable")
            or cls_role in ("icon_button", "button", "image")
            or simple in ("imageview", "imagebutton", "button")
        )
        if nav_plausible:
            return token_role, basis
        basis.append(f"id_token={token_role}(ignored:not-control-shaped)")

    if cls_role and cls_role not in _GENERIC_ROLES:
        return cls_role, basis

    if token_role:
        return token_role, basis

    if bool(node.get("editable")) or _has_flag(node, "editable"):
        basis.append("flag=editable")
        return "text_input", basis
    if _has_flag(node, "checkable"):
        basis.append("flag=checkable")
        return "checkbox", basis
    if _has_flag(node, "scrollable") and cls_role in ("", "container"):
        basis.append("flag=scrollable")
        return "container", basis

    if cls_role:
        return cls_role, basis
    return "unknown", basis


def _derive_actions(node: Mapping[str, Any], role: str) -> list[str]:
    """Only capabilities the OS actually reported, plus ``read`` (always free)."""
    actions: list[str] = []
    if _has_flag(node, "clickable"):
        actions.append("click")
    if _has_flag(node, "long-clickable"):
        actions.append("long_click")
    editable = bool(node.get("editable")) or _has_flag(node, "editable") or role in _INPUT_ROLES
    if editable:
        actions.append("set_text")
        actions.append("clear")
    if _has_flag(node, "checkable"):
        actions.append("uncheck" if bool(node.get("checked")) else "check")
    if _has_flag(node, "scrollable"):
        actions.append("scroll")
    if _has_flag(node, "focusable"):
        actions.append("focus")
    # Reading an element needs no capability at all.
    actions.append("read")
    return actions


def _derive_label(node: Mapping[str, Any], hint: Mapping[str, Any]) -> tuple[str, str]:
    """Best real name plus its provenance. Never invents text."""
    for source in ("text", "desc", "hint"):
        value = str(node.get(source) or "")
        if value.strip():
            return value, source
    if hint.get("humanized"):
        return str(hint["humanized"]), "resource_name"
    return "", ""


def _parent_path(node: Mapping[str, Any]) -> str:
    key = node.get("key")
    if isinstance(key, str) and key:
        return key
    return ""


def _ordinal_key(node: Mapping[str, Any], siblings: Sequence[Mapping[str, Any]] | None) -> str:
    """Stable tiebreaker that does not depend on ``index`` ordering.

    Ranks this node among siblings sharing the same (role-ish) class and bounds
    signature by (top, left). Bounds do not change when the dump renumbers
    ``index``, so the result survives a re-render.
    """
    if not siblings:
        return ""
    signature = lambda n: (  # noqa: E731 - local, tight
        _simple_class(n),
        _bounds(n),
    )
    mine = signature(node)
    peers = sorted(
        (signature(other) for other in siblings),
        key=lambda s: (s[1][1], s[1][0], s[0]),
    )
    try:
        position = peers.index(mine)
    except ValueError:
        return ""
    return str(position)


def describe_control(
    node: Mapping[str, Any],
    siblings: Sequence[Mapping[str, Any]] | None = None,
    interactive_mode: str = "strict",
    context: Mapping[str, Any] | None = None,
) -> dict:
    """Describe one hierarchy node as a semantic control.

    ``siblings`` is only used to build a stable identity when a node has no
    resource id; passing the sibling list is what keeps ``semantic_key`` stable
    across re-renders in that case.

    ``interactive_mode="inferred"`` additionally reports controls the OS failed
    to flag, marked ``interactive_source="inferred"`` and capped below the
    confidence of an OS-confirmed control. ``context`` comes from
    :func:`build_context`; without it inference can only use per-node facts.
    """
    hint = resolve_resource_hint(_get(node, "resource_id", ""))
    # Label first: actionability promotion needs the name before it can choose
    # between button and icon_button.
    label, label_source = _derive_label(node, hint)
    role, basis = _derive_role(node, hint["tokens"], label)
    actions = _derive_actions(node, role)

    package = str(_get(node, "package", ""))
    name = hint["name"]
    path = _parent_path(node)

    if name:
        semantic_key = f"{package}|{role}|{name}"
        key_source = "resource_id"
    elif path:
        semantic_key = f"{package}|{role}|{label}|{path}"
        key_source = "label_path"
    else:
        ordinal = _ordinal_key(node, siblings)
        semantic_key = f"{package}|{role}|{label}|#{ordinal}"
        key_source = "role_ordinal"

    # Provenance of the interactivity claim itself, before confidence is scored.
    source, infer_reason = _interactive_provenance(node, role, interactive_mode, context)

    # Confidence: how much of this description the OS actually handed us.
    confidence = 0.0
    if label_source in ("text", "desc", "hint"):
        confidence += 0.50
        basis.append(f"label_source={label_source}")
    elif label_source == "resource_name":
        confidence += 0.25
        basis.append("label_source=resource_name(inferred)")
    else:
        basis.append("label_source=none")
    if role not in ("unknown", "container"):
        confidence += 0.25
    if _has_flag(node, "clickable") or bool(node.get("editable")):
        confidence += 0.15
        basis.append("flag=clickable")
    if key_source == "resource_id":
        confidence += 0.10
    elif key_source == "role_ordinal":
        confidence -= 0.15
        basis.append("identity=ordinal(inferred, weaker)")
    confidence = round(max(0.0, min(1.0, confidence)), 2)
    if source == "inferred":
        # An inferred control may never look as trustworthy as an OS-confirmed
        # one, and the basis must say why we believe it is a control at all.
        confidence = min(confidence, INFERRED_CONFIDENCE_CAP)
        confidence = round(confidence, 2)
        reason = infer_reason
        basis.append(f"interactive=inferred({reason})")

    state = {
        "enabled": bool(_get(node, "enabled", True)),
        "checked": bool(node.get("checked")),
        "selected": bool(node.get("selected")),
        "focused": bool(node.get("focused")),
        "password": bool(node.get("password")),
    }
    state_description = node.get("state_description") or node.get("stateDescription")
    if isinstance(state_description, str) and state_description.strip():
        state["state_description"] = state_description

    result: dict[str, Any] = {
        "role": role,
        "label": label,
        "label_source": label_source,
        "actions": actions,
        "confidence": confidence,
        "basis": basis,
        "state": state,
        "semantic_key": semantic_key,
        "semantic_key_source": key_source,
        "region": "",
    }

    # Provenance of interactivity, always populated so a caller reading the
    # controls projection never has to distinguish "absent" from "unknown".
    result["interactive_source"] = source
    if source == "inferred" and not label:
        # The only way to learn what this does is to act and diff.
        result["needs_probe"] = True

    if role in _INPUT_ROLES:
        multiline = bool(node.get("scrollable")) or "multiline" in _simple_class(node)
        result["text_input"] = {
            "multiline": multiline,
            "obscured": bool(node.get("password")) or role == "password_input",
            "max_hint": str(_get(node, "hint", "")),
            # A hierarchy dump carries no InputType. Saying so beats guessing.
            "input_type": None,
            "input_type_note": "not derivable from a hierarchy dump",
        }
    return result


# --------------------------------------------------------------------------- #
# 2. classify_region
# --------------------------------------------------------------------------- #
def classify_region(
    node: Mapping[str, Any],
    screen_size: Sequence[int],
    thresholds: Mapping[str, float] | None = None,
) -> str:
    """Classify where a node sits on screen.

    ``thresholds`` overrides any calibrated constant for this call, so a caller
    on a different panel can retune without editing this module.
    """
    tune = resolve_thresholds(thresholds)
    width = int(screen_size[0]) if len(screen_size) > 0 else 0
    height = int(screen_size[1]) if len(screen_size) > 1 else 0
    if width <= 0 or height <= 0:
        return "content"
    left, top, right, bottom = _bounds(node)
    if right <= left or bottom <= top:
        return "content"

    node_w = right - left
    node_h = bottom - top

    # A bar item must actually live on the screen and be wider than a dot.
    tolerance = width * tune["bar_bound_tolerance"]
    on_screen = left >= -tolerance and right <= width + tolerance
    bar_worthy = on_screen and node_w >= width * tune["min_bar_item_w"]

    if bar_worthy and bottom <= height * tune["top_bar"]:
        return "top_bar"

    # The system strip only claims a full-bleed backdrop or a centred gesture
    # pill. A nav icon parked hard against the bottom edge must NOT be swallowed
    # here, so this test deliberately fails open to the nav band below.
    if bar_worthy and top >= height * (1.0 - tune["system_bar"]):
        cx, _cy = _center(node)
        centered = abs(cx - width / 2.0) <= width * 0.20
        if node_w >= width * 0.90 or centered:
            return "system_bar"

    if bar_worthy and top >= height * (1.0 - tune["bottom_nav"]):
        return "bottom_nav"

    if right <= width * tune["left_rail"] and node_h > height * 0.30:
        return "left_rail"

    cx, _cy = _center(node)
    centered = abs(cx - width / 2.0) <= width * tune["dialog_center"]
    bounded_w = tune["dialog_min_w"] <= node_w / width <= tune["dialog_max_w"]
    bounded_h = tune["dialog_min_h"] <= node_h / height <= tune["dialog_max_h"]
    if centered and bounded_w and bounded_h:
        return "dialog"

    if node_w <= width * tune["floating_max_w"] and node_h <= height * tune["floating_max_h"]:
        return "floating"
    return "content"


# --------------------------------------------------------------------------- #
# 3. build_spatial_model
# --------------------------------------------------------------------------- #
_REGION_ORDER = {
    "top_bar": 0,
    "dialog": 1,
    "content": 2,
    "left_rail": 3,
    "floating": 4,
    "bottom_nav": 5,
    "system_bar": 6,
}


def build_spatial_model(
    nodes: Sequence[Mapping[str, Any]],
    screen_size: Sequence[int],
    thresholds: Mapping[str, float] | None = None,
    interactive_mode: str = "inferred",
) -> dict:
    """Structure the screen into regions and named control groups.

    Single pass, list-based, no nested scans: this runs on every screen the
    agent looks at, so it must stay linear in node count.
    """
    tune = resolve_thresholds(thresholds)
    context = build_context(nodes, screen_size)
    width = int(screen_size[0]) if len(screen_size) > 0 else 0
    height = int(screen_size[1]) if len(screen_size) > 1 else 0

    regions: dict[str, list[int]] = {name: [] for name in REGIONS}
    top_bar: list[int] = []
    bottom_nav: list[tuple[int, int]] = []  # (x centre, index) for left->right
    nav_controls: dict[str, int] = {}
    scroll_containers: list[int] = []
    dialogs: list[int] = []
    content_buttons: list[tuple[int, int, str, int]] = []  # (-area, dist, key, index)

    screen_cx, screen_cy = width // 2, height // 2

    for position, node in enumerate(nodes):
        index = _index_of(node, position)
        region = classify_region(node, (width, height), tune)
        regions[region].append(index)

        if region == "top_bar":
            top_bar.append(index)
        if region == "bottom_nav":
            cx, _cy = _center(node)
            bottom_nav.append((cx, index))

        tokens = resolve_resource_hint(_get(node, "resource_id", ""))["tokens"]
        role, _basis = _derive_role(node, tokens)
        if role in NAV_ROLES and role not in nav_controls:
            nav_controls[role] = index
        if _has_flag(node, "scrollable"):
            scroll_containers.append(index)
        if region == "dialog":
            dialogs.append(index)

        if (
            role == "button"
            and bool(_get(node, "enabled", True))
            # A primary call-to-action is legitimately either in the content
            # flow or floating over it (a FAB is the canonical example). What it
            # cannot be is chrome: bars and dialogs are excluded.
            and region in ("content", "floating")
        ):
            cx, cy = _center(node)
            distance = abs(cx - screen_cx) + abs(cy - screen_cy)
            semantic_key = describe_control(node, interactive_mode=interactive_mode,
                                            context=context)["semantic_key"]
            content_buttons.append((-_area(node), distance, semantic_key, index))

    bottom_nav.sort(key=lambda pair: (pair[0], pair[1]))
    content_buttons.sort()

    primary_index = content_buttons[0][3] if content_buttons else None
    primary_label = ""
    if primary_index is not None:
        for node in nodes:
            if _index_of(node) == primary_index:
                primary_label = describe_control(node, interactive_mode=interactive_mode,
                                                 context=context)["label"]
                break

    return {
        "regions": {name: regions[name] for name in REGIONS},
        "region_names": list(REGIONS),
        "top_bar_controls": top_bar,
        "bottom_nav_controls": [index for _x, index in bottom_nav],
        "bottom_nav_order": [{"index": index, "x": x} for x, index in bottom_nav],
        "primary_action": {
            "index": primary_index,
            "label": primary_label,
        },
        "dialogs": dialogs,
        "scroll_containers": scroll_containers,
        "nav_controls": {role: nav_controls[role] for role in NAV_ROLES if role in nav_controls},
        "screen_size": [width, height],
    }


# --------------------------------------------------------------------------- #
# 4. find_by_semantics
# --------------------------------------------------------------------------- #
def find_by_semantics(
    nodes: Sequence[Mapping[str, Any]],
    role: str | None = None,
    label_contains: str | None = None,
    text_contains: str | None = None,
    semantic_key: str | None = None,
    region: str | None = None,
    screen_size: Sequence[int] | None = None,
    enabled_only: bool = True,
    interactive_mode: str = "inferred",
) -> list[dict]:
    """Semantic query over controls. Every supplied filter must match (AND).

    ``interactive_mode`` decides whether deduced controls are reported and
    whether their interactivity is marked as inferred. Filtering by role/label
    is unchanged in either mode.
    """
    needle_label = label_contains.lower() if label_contains else None
    needle_text = text_contains.lower() if text_contains else None
    context = build_context(nodes, screen_size) if screen_size else None

    matches: list[dict] = []
    for position, node in enumerate(nodes):
        index = _index_of(node, position)
        if enabled_only and not bool(_get(node, "enabled", True)):
            continue
        described = describe_control(node, interactive_mode=interactive_mode, context=context)
        if role is not None and described["role"] != role:
            continue
        if semantic_key is not None and described["semantic_key"] != semantic_key:
            continue
        label = described["label"]
        text = str(node.get("text") or "")
        if needle_label is not None and needle_label not in label.lower():
            continue
        if needle_text is not None and needle_text not in text.lower():
            continue
        if region is not None:
            if screen_size is None:
                raise ValueError("region filter requires screen_size")
            if classify_region(node, screen_size) != region:
                continue
        match = {
            "index": index,
            "center": list(_center(node)),
            "semantic_key": described["semantic_key"],
            "role": described["role"],
            "label": label,
            "confidence": described["confidence"],
        }
        if "interactive_source" in described:
            match["interactive_source"] = described["interactive_source"]
        if "needs_probe" in described:
            match["needs_probe"] = True
        matches.append(match)
    matches.sort(key=lambda m: (m["index"], m["semantic_key"]))
    return matches


# --------------------------------------------------------------------------- #
# 5. describe_screen
# --------------------------------------------------------------------------- #
def _reported_interactive(node: Mapping[str, Any]) -> bool:
    """True when the OS itself reported an interactivity capability."""
    for flag in ("clickable", "long-clickable", "checkable", "scrollable", "focusable"):
        if _has_flag(node, flag):
            return True
    return bool(node.get("editable"))


def _infer_reason(
    node: Mapping[str, Any], role: str, context: Mapping[str, Any] | None
) -> str:
    """Why geometry/class/hierarchy suggest this node is a control.

    Returns "" when it should not be treated as one. Every returned reason is an
    INFERENCE, never an OS fact, and is surfaced as such by the caller.
    """
    ctx = context or _empty_context()
    left, top, right, bottom = _bounds(node)
    if right <= left or bottom <= top:
        return ""
    area = float((right - left) * (bottom - top))
    screen_area = float(ctx.get("screen_area") or 0.0)
    width, height = ctx.get("screen_size") or (0, 0)
    if width and (left >= width or top >= height):
        return ""
    if screen_area and area >= screen_area * FULL_VIEWPORT_AREA_RATIO:
        # A root or scroll host: its children are the controls, not it.
        return ""
    if screen_area and area < screen_area * INFERRED_MIN_AREA_RATIO:
        # A dot, a divider or a spacer.
        return ""

    simple = _simple_class(node)
    if simple in _WIDGET_CLASSES or simple.endswith("itemview"):
        return f"class={simple}"

    index = _index_of(node)
    has_children = index in (ctx.get("parents") or ())
    if has_children:
        # A wrapper is not a control; its children are reported instead, so we
        # never list both the row and everything inside it.
        return ""

    named = any(str(node.get(key) or "").strip() for key in ("text", "desc", "hint"))
    if named:
        return "rendered_with_name"

    if simple in _GENERIC_CONTAINERS:
        parent = (ctx.get("by_index") or {}).get(node.get("parent"))
        if parent is not None:
            parent_cls = _simple_class(parent)
            if _has_flag(parent, "scrollable") or parent_cls in _SCROLLER_CLASSES:
                return "leaf_container_in_scroller"
        return "leaf_container"
    return ""


def _interactive_provenance(
    node: Mapping[str, Any],
    role: str,
    mode: str = "strict",
    context: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """``(source, reason)``: source is "flag"/"inferred"/"", reason is why inferred.

    Returned together so callers that need both do not pay for inference twice;
    the reason is the expensive part on a large screen.
    """
    if role in NAV_ROLES or role in _INPUT_ROLES:
        return "flag", ""
    if _reported_interactive(node):
        return "flag", ""
    if mode != "inferred":
        return "", ""
    reason = _infer_reason(node, role, context)
    return ("inferred", reason) if reason else ("", "")


def _interactive_source(
    node: Mapping[str, Any],
    role: str,
    mode: str = "strict",
    context: Mapping[str, Any] | None = None,
) -> str:
    """``"flag"`` (the OS said so), ``"inferred"`` (we deduced), or ``""``."""
    return _interactive_provenance(node, role, mode, context)[0]


def _is_interactive(
    node: Mapping[str, Any],
    described: Mapping[str, Any],
    mode: str = "strict",
    context: Mapping[str, Any] | None = None,
) -> bool:
    return bool(_interactive_source(node, described["role"], mode, context))


def _package_label(package: str) -> str:
    """Use the package as reported. We refuse to invent an application name."""
    return package or "unknown-package"


def describe_screen(
    nodes: Sequence[Mapping[str, Any]],
    screen_size: Sequence[int],
    max_controls: int = 40,
    include_containers: bool = False,
    thresholds: Mapping[str, float] | None = None,
    interactive_mode: str = "inferred",
) -> dict:
    """Token-efficient, LLM-facing view of one screen.

    Never truncates silently: when ``max_controls`` clips the list the exact
    number dropped is reported as ``omitted``. ``thresholds`` is forwarded to
    every region decision so a non-standard panel can be retuned per call.

    ``interactive_mode`` defaults to ``"inferred"`` because real apps (WeChat)
    report zero clickable nodes while being fully tappable; a strict-only
    answer there is an empty screen dressed up as a fact. Pass ``"strict"`` to
    see only OS-confirmed controls.
    """
    if interactive_mode not in INTERACTIVE_MODES:
        raise ValueError(f"interactive_mode must be one of {INTERACTIVE_MODES}")
    tune = resolve_thresholds(thresholds)
    context = build_context(nodes, screen_size)
    width, height = context["screen_size"]

    rows: list[tuple[tuple[int, int, int, int], dict]] = []
    package_counts: dict[str, int] = {}
    input_count = 0
    nav_count = 0
    system_labeled = 0
    inferred_labeled = 0
    unlabeled = 0
    interactive_total = 0
    inferred_interactive = 0

    for position, node in enumerate(nodes):
        index = _index_of(node, position)
        described = describe_control(node, interactive_mode=interactive_mode,
                                     context=context)
        role = described["role"]
        source = described["interactive_source"]
        if not source:
            continue
        # No wrapper guard is needed here: _infer_reason already refuses any node
        # that has children, so a deduced container is always a rendered leaf
        # (a list row or a grid cell) and is exactly what we want to report.

        interactive_total += 1
        if source == "inferred":
            inferred_interactive += 1
        region = classify_region(node, (width, height), tune)
        if role in _INPUT_ROLES:
            input_count += 1
        if role in NAV_ROLES or region == "bottom_nav":
            nav_count += 1
        if described["label_source"] in ("text", "desc", "hint"):
            system_labeled += 1
        elif described["label_source"] == "resource_name":
            inferred_labeled += 1
        else:
            unlabeled += 1

        package = str(_get(node, "package", ""))
        if package:
            package_counts[package] = package_counts.get(package, 0) + 1

        left, top, _right, _bottom = _bounds(node)
        # OS-confirmed controls sort ahead of deduced ones at equal region.
        source_rank = 0 if source == "flag" else 1
        rank = (_REGION_ORDER.get(region, 2), source_rank, top, left)
        rows.append((rank, _control_row(index, described, node, region, source)))

    rows.sort(key=lambda pair: pair[0])

    omitted = max(0, len(rows) - max(0, int(max_controls)))
    visible = [row for _rank, row in rows[: max(0, int(max_controls))]]

    spatial = build_spatial_model(nodes, (width, height), tune)
    # Only real labels belong in a labels list. An internal "#41" index is not a
    # name and must never be presented as one; unlabelled tabs are counted in
    # bottom_nav_unlabeled instead so the information is not lost.
    bottom_labels: list[str] = []
    bottom_unlabeled = 0
    by_index = {_index_of(node, position): node for position, node in enumerate(nodes)}
    for index in spatial["bottom_nav_controls"]:
        node = by_index.get(index)
        if node is None:
            continue
        label = describe_control(node)["label"]
        if label:
            bottom_labels.append(label)
        else:
            bottom_unlabeled += 1

    primary = spatial["primary_action"]
    main_package = ""
    if package_counts:
        # Deterministic: highest count, then alphabetical.
        main_package = sorted(package_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

    summary_bits = [
        f"{_package_label(main_package)}: {len(rows)} controls",
        f"{input_count} input" + ("s" if input_count != 1 else ""),
        f"{nav_count} nav",
    ]
    if inferred_interactive:
        summary_bits.append(f"{inferred_interactive} inferred")
    if primary["label"]:
        summary_bits.append(f"primary={primary['label']}")

    denominator = interactive_total or 1
    return {
        "summary": ", ".join(summary_bits),
        "controls": visible,
        "structure": {
            "regions": {
                name: len(spatial["regions"][name])
                for name in REGIONS
                if spatial["regions"][name]
            },
            "top_bar": len(spatial["top_bar_controls"]),
            "bottom_nav": len(spatial["bottom_nav_controls"]),
            "bottom_nav_labels": bottom_labels,
            "bottom_nav_unlabeled": bottom_unlabeled,
            "nav": [role for role in NAV_ROLES if role in spatial["nav_controls"]],
            "dialogs": len(spatial["dialogs"]),
            "scroll_containers": len(spatial["scroll_containers"]),
            "primary_action": primary,
        },
        "coverage": {
            "total": interactive_total,
            "system_labeled": system_labeled,
            "inferred_labeled": inferred_labeled,
            "unlabeled": unlabeled,
            "inferred_interactive": inferred_interactive,
            "system_ratio": round(system_labeled / denominator, 3),
            "nothing_reported_ratio": round(unlabeled / denominator, 3),
            # Share of every node on screen that we treated as a control.
            "interactive_ratio": round(interactive_total / (len(nodes) or 1), 3),
        },
        "interactive_mode": interactive_mode,
        "omitted": omitted,
        "screen_size": [width, height],
    }


def _control_row(
    index: int,
    described: Mapping[str, Any],
    node: Mapping[str, Any],
    region: str,
    source: str = "",
) -> dict:
    """Compact control row: empty fields are omitted, never padded."""
    row: dict[str, Any] = {"index": index, "role": described["role"]}
    if described["label"]:
        row["label"] = described["label"]
        row["label_source"] = described["label_source"]
    # Always a list, never null: a caller must be able to iterate unconditionally.
    row["actions"] = [a for a in described["actions"] if a != "read"]
    row["semantic_key"] = described["semantic_key"]
    row["center"] = list(_center(node))
    state = {
        k: v
        for k, v in described["state"].items()
        if k in ("checked", "selected", "focused") and v
    }
    if not described["state"]["enabled"]:
        state["enabled"] = False
    if state:
        row["state"] = state
    row["region"] = region
    if described["confidence"] < 0.5:
        row["confidence"] = described["confidence"]
    # Always present: "flag" (the OS said so) or "inferred" (we deduced it).
    row["interactive_source"] = described.get("interactive_source") or source
    if described.get("needs_probe"):
        row["needs_probe"] = True
    return row


# --------------------------------------------------------------------------- #
# Internal helper for callers that already hold a described control list
# --------------------------------------------------------------------------- #
def fingerprint(nodes: Sequence[Mapping[str, Any]]) -> str:
    """Stable hash of a screen's semantic identity, ignoring ``index`` churn.

    Useful as a cheap "did the screen actually change" check that does not
    depend on coordinates or document order.
    """
    keys = []
    for node in nodes:
        described = describe_control(node)
        if not _is_interactive(node, described):
            continue
        keys.append(described["semantic_key"])
    payload = "\n".join(sorted(keys)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]
