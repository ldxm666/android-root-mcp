"""Semantic UI snapshots, selectors, conditions and bounded workflow execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections import OrderedDict
from typing import Optional

from .models import Condition, FormField, Selector, Workflow, WorkflowStep, normalize_legacy_steps


class AutomationError(RuntimeError):
    def __init__(self, message: str, code: str = "UI_ERROR", **details):
        super().__init__(message)
        self.code = code
        self.details = details


def compact_ui(ui: dict) -> dict:
    return {k: ui[k] for k in (
        "success", "error", "snapshot_id", "engine", "elapsed_ms", "current_package",
        "current_activity", "total_elements", "shown_elements", "summary",
    ) if k in ui}


try:  # optional: OS-derived semantics engine
    from . import ui_semantics as _semantics
except Exception:  # pragma: no cover - engine is additive, never required
    _semantics = None


def enrich_nodes(nodes: list) -> list:
    """Attach OS-derived semantics to every node in place.

    ``role`` / ``label`` / ``actions`` / ``semantic_key`` are derived from what
    the operating system reported (view class, resource-id name, accessibility
    description, reported action flags) - never from pixels. Purely additive and
    failure-tolerant: if the engine is missing or throws, nodes come back
    untouched and every existing tool behaves exactly as before.
    """
    if _semantics is None or not nodes:
        return nodes
    try:
        for node in nodes:
            info = _semantics.describe_control(node)
            node["role"] = info.get("role", "")
            node["label"] = info.get("label", "")
            node["label_source"] = info.get("label_source", "")
            node["actions"] = info.get("actions", [])
            node["semantic_key"] = info.get("semantic_key", "")
    except Exception:
        pass
    return nodes


class AutomationMixin:
    def init_automation(self):
        self._snapshots = OrderedDict()
        self._current_snapshot = None
        self._ui_dirty = True
        self._screen_size_known = False
        self._workflow_guard = threading.Lock()
        self._active_workflow = None
        self._workflow_history = OrderedDict()

    def invalidate_ui(self):
        self._ui_dirty = True

    def _read_hierarchy(self) -> tuple[str, str, dict]:
        d = self.u2
        if d is not None:
            try:
                xml = d.dump_hierarchy(compressed=False)
                if xml and "<hierarchy" in xml:
                    try:
                        # u2.app_current() may call `dumpsys activity top`, which
                        # blocks while the foreground app serializes its full view
                        # tree. Use the small activity-manager state dump instead.
                        current = self.foreground()
                    except Exception:
                        current = {}
                    return xml, "uiautomator2", current
            except Exception:
                self.reset_u2()
        # /dev/tty is not available on all ROMs. Use a unique temporary file.
        path = f"/data/local/tmp/mcp_ui_{uuid.uuid4().hex}.xml"
        from shlex import quote
        cmd = (
            f"uiautomator dump {quote(path)} >/dev/null && cat {quote(path)}; "
            f"rc=$?; rm -f {quote(path)}; exit $rc"
        )
        r = self.shell_result(cmd, timeout=15, max_bytes=8 * 1024 * 1024)
        if not r.success or r.truncated or "<hierarchy" not in r.text:
            raise AutomationError("Unable to read UI hierarchy", "UI_UNAVAILABLE", result=r.as_dict())
        return r.text[r.text.find("<?xml") if "<?xml" in r.text else r.text.find("<hierarchy"):], "adb", self.foreground()

    def _capture_ui(self) -> dict:
        t0 = time.monotonic()
        xml, engine, current = self._read_hierarchy()
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            self.invalidate_ui()
            raise AutomationError(f"Malformed UI XML: {exc}", "UI_PARSE_ERROR") from exc
        nodes = []

        def walk(element, path="0", parent=None):
            next_parent = parent
            if element.tag == "node":
                a = element.attrib
                match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", a.get("bounds", ""))
                if match:
                    left, t, r, b = map(int, match.groups())
                    if r > left and b > t:
                        cls = a.get("class", "")
                        states = {k: a.get(k, "false") == "true" for k in (
                            "clickable", "long-clickable", "scrollable", "checkable",
                            "checked", "focused", "selected", "password",
                        )}
                        states["enabled"] = a.get("enabled", "true") == "true"
                        flags = [short for key, short in (
                            ("clickable", "click"), ("long-clickable", "long_click"),
                            ("scrollable", "scroll"), ("checked", "checked"), ("focused", "focused"),
                        ) if states[key]]
                        editable = "EditText" in cls or a.get("editable") == "true"
                        if editable:
                            flags.append("input")
                        if not states["enabled"]:
                            flags.append("disabled")
                        node = {
                            "index": len(nodes), "key": path, "parent": parent,
                            "class": cls.rsplit(".", 1)[-1], "full_class": cls,
                            "text": a.get("text", ""), "desc": a.get("content-desc", ""),
                            "hint": a.get("hint", ""), "resource_id": a.get("resource-id", ""),
                            "id": a.get("resource-id", "").rsplit("/", 1)[-1],
                            "package": a.get("package", ""), "bounds": [left, t, r, b],
                            "center": [(left + r) // 2, (t + b) // 2],
                            "flags": flags, "editable": editable, **states,
                        }
                        nodes.append(node)
                        next_parent = node["index"]
            for i, child in enumerate(element):
                walk(child, f"{path}.{i}", next_parent)

        walk(root)
        enrich_nodes(nodes)
        digest = hashlib.sha256(((self.serial or "") + "\0" + xml).encode("utf-8")).hexdigest()[:24]
        snapshot = {
            "success": True, "snapshot_id": digest, "engine": engine,
            "current_package": current.get("package", ""), "current_activity": current.get("activity", ""),
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            "elements": nodes, "xml": xml,
        }
        self._last_elements = nodes
        self._last_dump_time = time.time()
        self._current_snapshot = snapshot
        self._ui_dirty = False
        self._snapshots[digest] = snapshot
        self._snapshots.move_to_end(digest)
        while len(self._snapshots) > 8:
            self._snapshots.popitem(last=False)
        return snapshot

    @staticmethod
    def _summarize(nodes) -> str:
        lines = []
        for n in nodes:
            role = n.get("role") or n["class"]
            label = n.get("label") or ""
            head = f"[#{n['index']}] {role}"
            if label:
                head += " " + json.dumps(label[:2000], ensure_ascii=False)
            parts = [head]
            for key in ("text", "desc", "hint", "id"):
                if n[key] and n[key] != label:
                    # Escape untrusted newlines/quotes so node text cannot forge summary rows.
                    parts.append(f"{key}={json.dumps(n[key][:2000], ensure_ascii=False)}")
            actions = n.get("actions") or []
            if actions:
                parts.append(f"do={','.join(actions)}")
            parts.extend([f"center={tuple(n['center'])}", f"flags={','.join(n['flags'])}"])
            lines.append(" ".join(parts))
        return "\n".join(lines)

    def ui_snapshot(self, only_interactive=True, keyword="", max_elements=100, include_xml=False, since_snapshot_id=None) -> dict:
        if not 1 <= max_elements <= 2000:
            raise ValueError("max_elements must be between 1 and 2000")
        previous = self._snapshots.get(since_snapshot_id) if since_snapshot_id else None
        snapshot = self._capture_ui()
        nodes = snapshot["elements"]
        filtered = [n for n in nodes if (
            not only_interactive or n["text"] or n["desc"] or n["hint"] or n["flags"] or n["checkable"]
        ) and (not keyword or keyword.casefold() in " ".join(str(n[k]) for k in (
            "text", "desc", "hint", "resource_id", "full_class",
        )).casefold())]
        visible = filtered[:max_elements]
        result = {k: v for k, v in snapshot.items() if k not in ("xml", "elements")}
        result.update(
            total_elements=len(nodes), matched_elements=len(filtered), shown_elements=len(visible),
            truncated=len(filtered) > max_elements, summary=self._summarize(visible), elements=visible,
        )
        if include_xml:
            result["xml"] = snapshot["xml"]
        if since_snapshot_id:
            if previous is None:
                result["diff"] = {"available": False, "reason": "Snapshot expired; use this full snapshot"}
            else:
                old = {n["key"]: n for n in previous["elements"]}
                new = {n["key"]: n for n in nodes}
                changes = [
                    {"key": k, "before": old[k], "after": new[k]} for k in new.keys() & old.keys() if old[k] != new[k]
                ]
                result["diff"] = {
                    "available": True, "unchanged": previous["snapshot_id"] == snapshot["snapshot_id"],
                    "added": [new[k] for k in sorted(new.keys() - old.keys())][:max_elements],
                    "removed": [old[k] for k in sorted(old.keys() - new.keys())][:max_elements],
                    "changed": sorted(changes, key=lambda n: n["key"])[:max_elements],
                    "truncated": max(len(new.keys()-old.keys()), len(old.keys()-new.keys()), len(changes)) > max_elements,
                }
        return result

    def dump_ui_elements(self, only_interactive=True, keyword="", max_elements=120):
        try:
            return self.ui_snapshot(only_interactive, keyword, max_elements)
        except (AutomationError, RuntimeError, ValueError) as exc:
            return {"success": False, "error": str(exc), "code": getattr(exc, "code", "UI_ERROR")}

    @staticmethod
    def _matches(node: dict, selector: Selector) -> bool:
        predicates = selector.model_dump(exclude_none=True)
        fields = {"text": "text", "description": "desc", "class_name": "full_class", "package": "package", "hint": "hint"}
        for key, expected in predicates.items():
            if key in fields and node.get(fields[key]) != expected:
                return False
            if key == "text_contains" and expected.casefold() not in node["text"].casefold():
                return False
            if key == "description_contains" and expected.casefold() not in node["desc"].casefold():
                return False
            if key == "text_regex" and not re.search(expected, node["text"]):
                return False
            if key == "resource_id" and expected not in (node["resource_id"], node["id"]):
                return False
            if key in ("clickable", "enabled", "checked", "focused", "scrollable", "index") and node[key] != expected:
                return False
            if key == "role" and expected != node.get("role"):
                return False
            if key == "label_contains" and expected.casefold() not in str(node.get("label") or "").casefold():
                return False
            if key == "semantic_key" and expected != node.get("semantic_key"):
                return False
            if key == "within":
                left, t, r, b = expected
                x, y = node["center"]
                if not (left <= x < r and t <= y < b):
                    return False
        return True

    def find_elements(self, selector: Selector, fresh=True, max_results=100) -> dict:
        snapshot = self._capture_ui() if fresh or self._current_snapshot is None else self._current_snapshot
        if selector.index is not None and selector.snapshot_id != snapshot["snapshot_id"]:
            raise AutomationError("UI changed: refresh snapshot or use a semantic selector", "STALE_SNAPSHOT", snapshot_id=snapshot["snapshot_id"])
        matched = [n for n in snapshot["elements"] if self._matches(n, selector)]
        if selector.nth is not None:
            matched = matched[selector.nth:selector.nth+1]
        return {
            "success": True, "snapshot_id": snapshot["snapshot_id"], "count": len(matched),
            "elements": matched[:max_results], "truncated": len(matched) > max_results,
        }

    def _require_semantics(self):
        if _semantics is None:
            raise AutomationError(
                "Semantics engine unavailable (src/ui_semantics.py missing or failed to import)",
                "SEMANTICS_UNAVAILABLE",
            )
        return _semantics

    def _control_memory(self):
        if getattr(self, "_cm", None) is None:
            try:
                from .control_memory import ControlMemory
                self._cm = ControlMemory(os.path.join(str(self.state_dir), "control_memory.json"))
            except Exception:
                self._cm = None
        return self._cm

    def learn(self, budget=2, settle_ms=250, allow_restore=True) -> dict:
        """Probe unlabeled controls, record what each one actually caused, persist it.

        This is how the agent learns apps whose controls the OS refuses to name.
        Every round: observe -> pick the most valuable unknown control -> refuse it
        if it looks destructive -> tap -> observe again -> store the observed
        effect under (package, activity, control identity). Nothing is ever
        inferred about a control's meaning beyond the delta we actually saw.
        """
        from . import probe_engine as _probe
        memory = self._control_memory()
        if memory is None:
            raise AutomationError("Control memory unavailable", "MEMORY_UNAVAILABLE")
        engine = self._require_semantics()
        rounds: list = []
        for _ in range(max(1, min(int(budget), 5))):
            before = self._capture_ui()
            package, activity = before["current_package"], before["current_activity"]
            controls = engine.describe_screen(
                before["elements"], tuple(self._screen_size))["controls"]
            target = _probe.choose_probe_target(controls, memory)
            if not target:
                rounds.append({"skipped": True, "reason": "no probe-worthy control left on this screen"})
                break
            index = target.get("index")
            if index is None or index >= len(before["elements"]):
                rounds.append({"skipped": True, "reason": "target disappeared before probing"})
                break
            node = before["elements"][index]
            allowed, reason = _probe.is_safe_to_probe(node, package, set())
            if not allowed:
                memory.record(package, activity, node, {"kind": "refused", "summary": reason})
                rounds.append({"skipped": target.get("semantic_key"), "reason": reason})
                continue
            self._click_coordinates(*node["center"])
            if settle_ms:
                time.sleep(max(0, min(settle_ms, 5000)) / 1000)
            after = self._capture_ui()
            effect = _probe.diff_screens(before, after)
            sentence = _probe.summarize_effect(effect)
            try:
                _probe.record_round(before, node, after, memory)
            except Exception:
                memory.record(package, activity, node, effect)
            rounds.append({"semantic_key": target.get("semantic_key"),
                           "label": node.get("label") or "", "effect": sentence,
                           "kind": effect.get("classification") or effect.get("kind")})
            if allow_restore and (effect.get("classification") or effect.get("kind")) == "navigation":
                # A probe that navigated should put the screen back, or the next
                # round would learn the wrong screen.
                try:
                    self.press("BACK")
                    time.sleep(0.35)
                except Exception:
                    pass
        return {"success": True, "rounds": rounds, "memory": memory.stats()}

    def act(self, action="tap", selector=None, value="", x=None, y=None, direction="up",
            duration_ms=None, wait_timeout=3.0, observe=True) -> dict:
        """One human-shaped verb: do the thing, then report what changed.

        Efficiency contract: the comparison baseline is the snapshot still cached
        from the caller's last observation, so observing the result costs ONE
        capture instead of two. The agent gets input and consequence in a single
        round trip and does not need a follow-up perceive call.
        """
        before = self._current_snapshot
        if observe and before is None:
            before = self._capture_ui()
        repeatable = ("tap", "long_click", "double_click", "set_text", "append_text",
                      "clear", "check", "uncheck", "read")
        if action in repeatable:
            if x is not None and y is not None:
                if action in ("set_text", "append_text", "clear"):
                    result = self.input_text_fast(
                        value, clear_first=action != "append_text", return_ui_after=False)
                else:
                    self._click_coordinates(int(x), int(y), action,
                                            int(duration_ms) if duration_ms else 600)
                    result = {"success": True, "action": action, "coords": [int(x), int(y)]}
            elif selector is not None:
                result = self.element_action(
                    selector, action, value, wait_timeout, False,
                    int(duration_ms) if duration_ms else 600)
            else:
                raise ValueError("act requires a selector or explicit x/y coordinates")
        elif action == "scroll":
            result = self.swipe_screen(direction, duration_ms=int(duration_ms) if duration_ms else 300,
                                       return_ui_after=False)
        elif action == "key":
            result = self.press(value or "BACK")
        else:
            raise ValueError(
                "action must be one of tap/long_click/double_click/set_text/append_text/"
                "clear/check/uncheck/read/scroll/key")

        if not observe:
            return result
        after = self._capture_ui()
        effect, sentence = None, ""
        try:
            from . import probe_engine as _probe
            if before is not None:
                effect = _probe.diff_screens(before, after)
                sentence = _probe.summarize_effect(effect)
        except Exception:
            effect, sentence = None, ""
        result["effect"] = sentence
        if effect is not None:
            result["effect_detail"] = effect
        result["snapshot_id"] = after["snapshot_id"]
        result["current_package"] = after["current_package"]
        result["current_activity"] = after["current_activity"]
        return result

    def ui_describe(self, max_controls=40, role=None, region=None, include_containers=False) -> dict:
        """Describe the current screen as OS-reported controls, not pixels.

        Every field comes from the accessibility/view hierarchy: class, resource
        name, content description, reported action flags. No OCR, no vision.
        """
        engine = self._require_semantics()
        snapshot = self._capture_ui()
        model = engine.describe_screen(
            snapshot["elements"], tuple(self._screen_size),
            max_controls=max_controls, include_containers=include_containers,
        )
        controls = model.get("controls") or []
        if role:
            wanted = role.casefold()
            controls = [c for c in controls if str(c.get("role", "")).casefold() == wanted]
        if region:
            engine_model = engine.build_spatial_model(snapshot["elements"], tuple(self._screen_size))
            members = set(engine_model.get("regions", {}).get(region, []))
            controls = [c for c in controls if c.get("index") in members]
        memory = self._control_memory()
        if memory is not None:
            try:
                controls = memory.annotate(snapshot["current_package"],
                                           snapshot["current_activity"], controls)
                learned = sum(1 for c in controls if c.get("learned_effect"))
                if learned:
                    model["learned_controls"] = learned
            except Exception:
                pass
        model["controls"] = controls
        summary = str(model.get("summary") or "")
        if not summary or "unknown-package" in summary:
            # The engine works from nodes alone and cannot know which app it is
            # looking at; the caller does, so name it here.
            parts = [snapshot["current_package"] or "unknown package"]
            activity = str(snapshot["current_activity"] or "").rsplit(".", 1)[-1]
            if activity:
                parts.append(activity)
            parts.append(f"{len(controls)} controls")
            structure = model.get("structure") or {}
            nav = structure.get("bottom_nav_labels") or []
            if nav:
                parts.append("nav=" + "/".join(str(n) for n in nav[:6]))
            primary = (structure.get("primary_action") or {}).get("label")
            if primary:
                parts.append(f"primary={primary}")
            model["summary"] = ": ".join(parts[:2]) + " | " + ", ".join(parts[2:])
        model.update({
            "success": True, "snapshot_id": snapshot["snapshot_id"], "engine": snapshot["engine"],
            "current_package": snapshot["current_package"],
            "current_activity": snapshot["current_activity"],
            "total_elements": len(snapshot["elements"]),
        })
        if role or region:
            model["filtered_controls"] = len(controls)
        return model

    def ui_query(self, role=None, label_contains=None, text_contains=None, semantic_key=None,
                 region=None, enabled_only=True, max_results=25) -> dict:
        """Find controls by OS-derived semantics so a replay needs no coordinates."""
        engine = self._require_semantics()
        snapshot = self._capture_ui()
        matches = engine.find_by_semantics(
            snapshot["elements"], role=role, label_contains=label_contains,
            text_contains=text_contains, semantic_key=semantic_key, region=region,
            screen_size=tuple(self._screen_size), enabled_only=enabled_only,
        )
        return {
            "success": True, "snapshot_id": snapshot["snapshot_id"], "count": len(matches),
            "elements": matches[:max_results], "truncated": len(matches) > max_results,
            "current_package": snapshot["current_package"],
            "current_activity": snapshot["current_activity"],
        }

    def resolve_selector(self, selector: Selector, timeout=3, cancel=None) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            if cancel and cancel.is_set():
                raise AutomationError("Workflow cancelled", "CANCELLED")
            found = self._native_find_one(selector)
            if found is None:
                found = self.find_elements(selector)
            if found["count"] > 1:
                raise AutomationError("Selector matches several elements; refine it or supply nth", "AMBIGUOUS_SELECTOR", candidates=found["elements"][:10])
            if found["count"] == 1:
                node = found["elements"][0]
                if not node["enabled"]:
                    raise AutomationError("Matched element is disabled", "ELEMENT_DISABLED")
                return node
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AutomationError("No matching element before timeout", "ELEMENT_NOT_FOUND")
            if cancel:
                cancel.wait(min(0.15, remaining))
            else:
                time.sleep(min(0.15, remaining))

    def _native_find_one(self, selector: Selector):
        """Exact selectors use native JSON-RPC instead of serializing the UI tree.

        Queries/diffs still use complete snapshots. Short IDs, regex, region,
        hint and index selectors deliberately take the XML path so semantics do
        not change between Java's UiSelector and the local XML matcher.
        """
        fields = selector.model_dump(exclude_none=True)
        mapping = {"text": "text", "resource_id": "resourceId", "description": "description",
                   "class_name": "className", "package": "packageName", "clickable": "clickable",
                   "enabled": "enabled", "checked": "checked", "focused": "focused", "scrollable": "scrollable"}
        # A bare id ("search_box") used to be rejected here, which pushed every
        # short-id selector onto the slow XML path. Trying the native UiSelector
        # first is free when it works and harmless when it does not: a zero match
        # already falls through to XML, which remains the only path that sees
        # secondary windows. Do NOT resolve the package name here to qualify the
        # id - foreground() costs ~130 ms and would tax every selector.
        if set(fields) - set(mapping) - {"nth"}:
            return None
        d = self.u2
        if d is None or not hasattr(d, "jsonrpc"):
            return None
        try:
            obj = d(**{mapping[k]: v for k, v in fields.items() if k in mapping})
            count = obj.count
            if count == 0:
                # UiSelector can omit secondary windows which dump_hierarchy
                # exposes (for example SystemUI overlays). Keep XML fallback.
                return None
            if selector.nth is not None:
                if selector.nth >= count:
                    return None
                obj = obj[selector.nth]
                count = 1
            if count != 1:
                return {"count": count, "elements": []}
            info = obj.info
            bounds = info.get("visibleBounds") or info["bounds"]
            left, top, right, bottom = (bounds[k] for k in ("left", "top", "right", "bottom"))
            if right <= left or bottom <= top:
                return None
            cls = info.get("className", "")
            node = {"index": None, "engine": "uiautomator2_selector", "text": info.get("text") or "",
                    "desc": info.get("contentDescription") or "", "hint": "",
                    "resource_id": info.get("resourceName") or selector.resource_id or "",
                    "id": (info.get("resourceName") or selector.resource_id or "").rsplit("/", 1)[-1],
                    "full_class": cls, "class": cls.rsplit(".", 1)[-1], "package": info.get("packageName", ""),
                    "bounds": [left, top, right, bottom], "center": [(left+right)//2, (top+bottom)//2],
                    "enabled": info.get("enabled", True), "checked": info.get("checked", False),
                    "checkable": info.get("checkable", False)}
            return {"count": 1, "elements": [node]}
        except Exception:
            return None

    def resolve_element_target(self, index=None, text=None, text_contains=None, resource_id=None,
                               description=None, x=None, y=None, wait_timeout=3.0):
        if x is not None and y is not None:
            return int(x), int(y), f"coords({x},{y})"
        fields = {"index": index, "text": text, "text_contains": text_contains,
                  "resource_id": resource_id, "description_contains": description}
        if index is not None:
            if self._ui_dirty or not self._current_snapshot:
                return None, None, "Element index is stale; obtain a UI snapshot first"
            fields["snapshot_id"] = self._current_snapshot["snapshot_id"]
        try:
            node = self.resolve_selector(Selector(**{k: v for k, v in fields.items() if v is not None}), timeout=wait_timeout)
            return *node["center"], f"#{node['index']} {node['text'] or node['desc'] or node['id']}"
        except (ValueError, AutomationError) as exc:
            return None, None, str(exc)

    _KEY_CODES = {
        "HOME": 3, "BACK": 4, "CALL": 5, "ENDCALL": 6, "MENU": 82, "ENTER": 66,
        "DEL": 67, "TAB": 61, "SPACE": 62, "ESCAPE": 111, "POWER": 26,
        "VOLUME_UP": 24, "VOLUME_DOWN": 25, "APP_SWITCH": 187, "RECENTS": 187,
        "WAKEUP": 224, "SLEEP": 223, "SEARCH": 84, "CAMERA": 27,
    }

    def native_input(self):
        """Return the resident root input daemon, or None when it is unusable."""
        try:
            from . import native_input as _native
            if not _native.enabled():
                return None
            return _native.instance(self.adb_bin, self.serial)
        except Exception:
            return None

    def _click_coordinates(self, x, y, action="tap", duration_ms=600):
        if min(x, y) < 0:
            raise ValueError("negative coordinates are not actionable")
        # Native devnode injection first: `input tap` spawns a JVM per call
        # (95-114 ms measured); the resident daemon does the same tap in ~38 ms.
        # double_click stays on uiautomator2, which produces a real double-tap
        # gesture that two programmatic taps do not reliably reproduce.
        if action in ("tap", "long_click"):
            native = self.native_input()
            if native is not None and native.available():
                try:
                    if action == "long_click":
                        native.long_press(x, y, int(duration_ms))
                    else:
                        native.tap(x, y)
                    self.invalidate_ui()
                    return
                except Exception:
                    pass
        d = self.u2
        if d is not None:
            if action == "long_click":
                d.long_click(x, y, duration=duration_ms / 1000)
            elif action == "double_click":
                d.double_click(x, y, duration=0.1)
            else:
                d.click(x, y)
        else:
            cmd = f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} {int(duration_ms)}" if action == "long_click" else f"input tap {int(x)} {int(y)}"
            if action == "double_click":
                cmd += "; " + cmd
            self.require_shell(cmd)
        self.invalidate_ui()

    def element_action(self, selector: Selector, action="tap", value="", timeout=3, return_ui=False,
                       duration_ms=600, press_enter=False, cancel=None) -> dict:
        if action not in ("tap", "long_click", "double_click", "set_text", "append_text", "clear", "check", "uncheck", "read"):
            raise ValueError(f"Unknown element action: {action}")
        node = self.resolve_selector(selector, timeout, cancel)
        if action == "read":
            return {"success": True, "element": node}
        if action in ("check", "uncheck") and node["checked"] == (action == "check"):
            result = {"success": True, "changed": False, "element": node}
        elif action in ("set_text", "append_text", "clear"):
            self._click_coordinates(*node["center"])
            result = self.input_text_fast(value if action != "clear" else "",
                                          clear_first=action != "append_text", press_enter=press_enter,
                                          return_ui_after=False)
        else:
            if action in ("check", "uncheck") and not node["checkable"]:
                raise AutomationError("Element is not checkable", "NOT_CHECKABLE")
            self._click_coordinates(*node["center"], action=action, duration_ms=duration_ms)
            result = {"success": True, "changed": True, "element": node}
        if return_ui:
            result["post_ui"] = compact_ui(self.dump_ui_elements(max_elements=100))
        else:
            result["observe"] = "ui_snapshot(since_snapshot_id=...) returns only the delta"
        return result

    def tap_target(self, index=None, text=None, text_contains=None, resource_id=None, description=None,
                   x=None, y=None, long_click=False, duration_ms=600, wait_timeout=3.0,
                   wait_after_ms=0, return_ui_after=False):
        cx, cy, label = self.resolve_element_target(index, text, text_contains, resource_id, description, x, y, wait_timeout)
        if cx is None:
            return {"success": False, "error": label}
        self._click_coordinates(cx, cy, "long_click" if long_click else "tap", duration_ms)
        if wait_after_ms:
            time.sleep(min(wait_after_ms, 60000) / 1000)
        result = {"success": True, "target": label, "coords": [cx, cy]}
        if return_ui_after:
            result["post_ui"] = compact_ui(self.dump_ui_elements(max_elements=100))
        else:
            result["observe"] = "ui_snapshot(since_snapshot_id=...) returns only the delta"
        return result

    def input_text_fast(self, text, index=None, resource_id=None, hint_or_text=None, clear_first=True,
                        press_enter=False, return_ui_after=False):
        if index is not None or resource_id is not None or hint_or_text is not None:
            cx, cy, label = self.resolve_element_target(index=index, resource_id=resource_id,
                                                       text_contains=hint_or_text, wait_timeout=2)
            if cx is None:
                return {"success": False, "error": label}
            self._click_coordinates(cx, cy)
        d = self.u2
        if d is None:
            # ADB input text does not support arbitrary Unicode. Never pretend a paste
            # succeeded when no clipboard/IME transport is available.
            if not text.isascii() or "\n" in text or "%s" in text or clear_first:
                return {"success": False, "code": "UNICODE_INPUT_UNAVAILABLE",
                        "error": "Unicode, literal %s and reliable clearing require uiautomator2; initialize it with device_doctor"}
            from shlex import quote
            self.require_shell("input text " + quote(text.replace(" ", "%s")))
            method = "adb_ascii"
        else:
            try:
                target = d(focused=True)
                value = text if clear_first else target.get_text(timeout=0) + text
                result = target.set_text(value, timeout=0)
                if result is False:
                    raise RuntimeError("UIAutomator rejected setText")
                method = "uiautomator2_setText"
            except Exception:
                orig_ime = self.require_shell("settings get secure default_input_method").strip()
                try:
                    d.send_keys(text, clear=clear_first)
                except Exception as exc:
                    return {"success": False, "error": f"Unicode input failed: {exc}"}
                finally:
                    from shlex import quote
                    if orig_ime and orig_ime != "null":
                        self.shell_result("ime set " + quote(orig_ime), timeout=5)
                method = "uiautomator2_ime"
        self.invalidate_ui()
        if press_enter:
            self.press("ENTER")
        result = {"success": True, "method": method, "characters": len(text), "press_enter": press_enter}
        if return_ui_after:
            result["post_ui"] = compact_ui(self.dump_ui_elements(max_elements=100))
        return result

    def press(self, key):
        key = str(key)
        if not re.fullmatch(r"(?:KEYCODE_)?[A-Z0-9_]+|\d+", key.upper()):
            raise ValueError("key must be a keycode number or name")
        normalized = key.upper().removeprefix("KEYCODE_")
        code = None
        if normalized.isdigit():
            code = int(normalized)
        else:
            code = self._KEY_CODES.get(normalized)
        if code is not None:
            native = self.native_input()
            if native is not None and native.available():
                try:
                    native.key(code)
                    self.invalidate_ui()
                    return {"success": True, "key": key, "route": "devnode", "keycode": code}
                except Exception:
                    pass
        self.require_shell("input keyevent " + key.upper())
        self.invalidate_ui()
        return {"success": True, "key": key}

    def swipe_screen(self, direction="up", scale=0.6, x1=None, y1=None, x2=None, y2=None,
                     duration_ms=300, scroll_to_text=None, max_scrolls=5, return_ui_after=False):
        if direction not in ("up", "down", "left", "right"):
            raise ValueError("direction must be up/down/left/right")
        if not 0 < scale <= 0.9 or not 1 <= duration_ms <= 10000 or not 0 <= max_scrolls <= 100:
            raise ValueError("invalid swipe scale/duration/max_scrolls")
        d = self.u2
        if d is not None:
            # Refresh the display size only when we do not already know it; a
            # window_size round trip costs several hundred ms and the geometry
            # does not change between calls on a single device.
            if not getattr(self, "_screen_size_known", False):
                try:
                    self._screen_size = tuple(d.window_size())
                    self._screen_size_known = True
                except Exception:
                    pass
        else:
            if not getattr(self, "_screen_size_known", False):
                m = re.findall(r"(\d+)x(\d+)", self.require_shell("wm size"))
                if m:
                    self._screen_size = tuple(map(int, m[-1]))
                    self._screen_size_known = True
        w, h = self._screen_size
        cx, cy = w // 2, h // 2
        dx, dy = int(w * scale / 2), int(h * scale / 2)
        defaults = {"up": (cx, cy+dy, cx, cy-dy), "down": (cx, cy-dy, cx, cy+dy),
                    "left": (cx+dx, cy, cx-dx, cy), "right": (cx-dx, cy, cx+dx, cy)}
        points = (x1, y1, x2, y2)
        if any(p is not None for p in points) and not all(p is not None for p in points):
            raise ValueError("provide all four coordinates")
        coords = points if x1 is not None else defaults[direction]
        count = 0
        last_id = None
        limit = max_scrolls if scroll_to_text else 1
        while True:
            if scroll_to_text:
                found = self.find_elements(Selector(text_contains=scroll_to_text))
                if found["count"]:
                    return {"success": True, "found": True, "scrolls_performed": count, "elements": found["elements"]}
                if count >= limit or last_id == found["snapshot_id"]:
                    return {"success": False, "found": False, "scrolls_performed": count,
                            "error": "Target not found before scroll limit/end of content"}
                last_id = found["snapshot_id"]
            if count >= limit:
                break
            native = self.native_input()
            routed = False
            if native is not None and native.available():
                try:
                    native.swipe(int(coords[0]), int(coords[1]), int(coords[2]), int(coords[3]),
                                 int(duration_ms))
                    routed = True
                except Exception:
                    routed = False
            if not routed:
                self.require_shell("input swipe " + " ".join(str(int(v)) for v in (*coords, duration_ms)))
            self.invalidate_ui()
            count += 1
            if not scroll_to_text:
                break
        result = {"success": True, "from": list(coords[:2]), "to": list(coords[2:]), "scrolls_performed": count}
        if return_ui_after:
            result["post_ui"] = compact_ui(self.dump_ui_elements(max_elements=100))
        return result

    def wait_condition(self, condition: Condition, timeout=10, poll_interval=0.2, cancel=None) -> dict:
        if not 0 <= timeout <= 1800 or not 0.05 <= poll_interval <= 10:
            raise ValueError("invalid timeout/poll_interval")
        deadline = time.monotonic() + timeout
        previous, stable, attempts = None, 0, 0
        last = {}
        while True:
            if cancel and cancel.is_set():
                return {"success": False, "code": "CANCELLED", "attempts": attempts}
            attempts += 1
            try:
                if condition.kind in ("exists", "gone"):
                    last = self.find_elements(condition.selector)
                    satisfied = last["count"] > 0 if condition.kind == "exists" else last["count"] == 0
                elif condition.kind == "foreground":
                    last = self.foreground()
                    satisfied = (not condition.package or last.get("package") == condition.package) and (
                        not condition.activity or last.get("activity") == condition.activity)
                elif condition.kind == "shell":
                    r = self.shell_result(condition.command, root=condition.root,
                                          timeout=max(0.1, min(5, deadline-time.monotonic())), max_bytes=32768)
                    last = r.as_dict()
                    satisfied = r.exit_code == condition.exit_code and not r.timed_out and not r.error and (
                        condition.contains is None or condition.contains in r.text)
                else:
                    ui = self._capture_ui()
                    fingerprint = ui["snapshot_id"]
                    stable = stable + 1 if previous == fingerprint else 1
                    previous = fingerprint
                    last = {"snapshot_id": fingerprint, "stable_samples": stable}
                    satisfied = stable >= condition.stable_samples
            except AutomationError as exc:
                # Failure to observe is NOT proof that an element is gone.
                last = {"error": str(exc), "code": exc.code}
                satisfied = False
            if satisfied:
                return {"success": True, "attempts": attempts, "observed": last}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"success": False, "code": "CONDITION_TIMEOUT", "attempts": attempts, "observed": last}
            if cancel:
                cancel.wait(min(poll_interval, remaining))
            else:
                time.sleep(min(poll_interval, remaining))

    def fill_form(self, fields: list[FormField], submit: Optional[Selector] = None, return_ui=False):
        if not 1 <= len(fields) <= 100:
            raise ValueError("fields must contain 1..100 items")
        trace = []
        for i, field in enumerate(fields):
            result = self.element_action(field.selector, "set_text", field.value, return_ui=False)
            trace.append({"field": i, **result})
            if not result.get("success"):
                return {"success": False, "failed_field": i, "trace": trace,
                        "post_ui": compact_ui(self.dump_ui_elements())}
        if submit:
            result = self.element_action(submit, return_ui=False)
            if not result.get("success"):
                return {"success": False, "stage": "submit", "trace": trace, "result": result}
        return {"success": True, "fields_filled": len(trace),
                **({"post_ui": compact_ui(self.dump_ui_elements())} if return_ui else {})}

    def workflow_control(self, action="status", run_id=None):
        with self._workflow_guard:
            active = self._active_workflow
            if action == "cancel":
                if not active or (run_id and run_id != active["run_id"]):
                    return {"success": False, "error": "No matching active workflow"}
                active["cancel"].set()
            elif action != "status":
                raise ValueError("action must be status or cancel")
            return {"success": True, "active": {k: v for k, v in active.items() if k != "cancel"} if active else None,
                    "recent": list(self._workflow_history.values())[-10:]}

    def run_workflow(self, workflow: Workflow) -> dict:
        run_id = uuid.uuid4().hex
        cancel = threading.Event()
        with self._workflow_guard:
            if self._active_workflow:
                raise AutomationError("A workflow is already running on this controller", "WORKFLOW_BUSY")
            self._active_workflow = {"run_id": run_id, "serial": self.serial, "started_at": time.time(), "executed_steps": 0, "cancel": cancel}
        t0 = time.monotonic()
        deadline = t0 + workflow.timeout
        trace, executed, failures = [], 0, []

        def budget():
            if cancel.is_set():
                raise AutomationError("Workflow cancelled; no further steps executed", "CANCELLED")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AutomationError("Workflow deadline reached", "WORKFLOW_TIMEOUT")
            return remaining

        def run_steps(steps, parent=""):
            nonlocal executed
            for i, step in enumerate(steps):
                remaining = budget()
                if executed >= workflow.max_executed_steps:
                    raise AutomationError("Workflow step budget reached", "STEP_BUDGET_EXCEEDED")
                executed += 1
                with self._workflow_guard:
                    self._active_workflow["executed_steps"] = executed
                path = f"{parent}.{i}" if parent else str(i)
                started = time.monotonic()
                record = {"step": path, "action": step.action, "label": step.label}
                trace.append(record)
                try:
                    result = run_step(step, path, min(step.timeout, remaining))
                    if not result.get("success", False):
                        raise AutomationError(result.get("error", "Step failed"), result.get("code", "STEP_FAILED"), result=result)
                    if step.expect:
                        post = self.wait_condition(step.expect, min(step.timeout, budget()), cancel=cancel)
                        if not post["success"]:
                            raise AutomationError("Step postcondition failed", "POSTCONDITION_FAILED", observed=post)
                        result["postcondition"] = post
                    record.update(success=True, result=result)
                except Exception as exc:
                    code = getattr(exc, "code", "STEP_ERROR")
                    record.update(success=False, error=str(exc), code=code, details=getattr(exc, "details", {}))
                    failures.append(path)
                    if step.on_error == "stop" or code in ("CANCELLED", "WORKFLOW_TIMEOUT", "STEP_BUDGET_EXCEEDED"):
                        raise
                finally:
                    record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)

        def run_step(s: WorkflowStep, path, timeout):
            if s.action == "launch":
                return self.launch_app(s.package, s.activity, timeout=max(0.1, timeout))
            if s.action in ("tap", "long_click", "double_click"):
                if s.selector:
                    return self.element_action(s.selector, s.action, timeout=timeout, duration_ms=s.duration_ms, return_ui=False, cancel=cancel)
                self._click_coordinates(s.x, s.y, s.action, s.duration_ms)
                return {"success": True, "coordinates": [s.x, s.y]}
            if s.action in ("input", "clear"):
                if s.selector:
                    return self.element_action(s.selector, "clear" if s.action == "clear" else "set_text" if s.clear else "append_text", s.text,
                                               timeout=timeout, press_enter=s.enter, return_ui=False, cancel=cancel)
                return self.input_text_fast(s.text, clear_first=s.clear, press_enter=s.enter, return_ui_after=False)
            if s.action == "key":
                return self.press(s.key)
            if s.action == "swipe":
                return self.swipe_screen(s.direction, s.scale, duration_ms=s.duration_ms, return_ui_after=False)
            if s.action in ("wait", "assert"):
                return self.wait_condition(s.condition, timeout=timeout if s.action == "wait" else 0, cancel=cancel)
            if s.action == "shell":
                self.invalidate_ui()
                return self.shell_result(s.command, root=s.root, timeout=max(0.1, timeout), max_bytes=65536).as_dict()
            if s.action == "sleep":
                seconds = s.ms / 1000
                if seconds > budget():
                    raise AutomationError("Sleep exceeds remaining workflow budget", "WORKFLOW_TIMEOUT")
                cancel.wait(seconds)
                budget()
                return {"success": True, "slept_ms": s.ms}
            if s.action == "read_text":
                return self.element_action(s.selector, "read", timeout=timeout, return_ui=False, cancel=cancel)
            if s.action == "if":
                test = self.wait_condition(s.condition, timeout=0, cancel=cancel)
                if test.get("observed", {}).get("error"):
                    raise AutomationError("Cannot observe branch condition", "CONDITION_OBSERVATION_FAILED", observed=test)
                run_steps(s.steps if test["success"] else s.else_steps, path + (".then" if test["success"] else ".else"))
                return {"success": True, "branch": "then" if test["success"] else "else"}
            if s.action == "repeat":
                for n in range(s.count):
                    budget()
                    if s.condition:
                        check = self.wait_condition(s.condition, timeout=0, cancel=cancel)
                        if check.get("observed", {}).get("error"):
                            raise AutomationError("Cannot observe repeat condition", "CONDITION_OBSERVATION_FAILED", observed=check)
                        if check["success"]:
                            return {"success": True, "iterations": n, "until_satisfied": True}
                    run_steps(s.steps, f"{path}.repeat[{n}]")
                if s.condition:
                    done = self.wait_condition(s.condition, timeout=0, cancel=cancel)
                    return {"success": done["success"], "iterations": s.count, "until_satisfied": done["success"], "code": "REPEAT_LIMIT"}
                return {"success": True, "iterations": s.count}
            raise ValueError(f"Unimplemented action {s.action}")

        result = {"run_id": run_id, "success": False}
        previous_deadline = self._operation_deadline
        self._operation_deadline = deadline
        try:
            run_steps(workflow.steps)
            budget()
            result["success"] = not failures
        except Exception as exc:
            result.update(error=str(exc), code=getattr(exc, "code", "WORKFLOW_ERROR"))
        finally:
            self._operation_deadline = previous_deadline
            result.update(executed_steps=executed, failed_steps=failures, trace=trace,
                          elapsed_ms=round((time.monotonic() - t0) * 1000, 1))
            if workflow.return_ui:
                result["post_ui"] = compact_ui(self.dump_ui_elements(max_elements=100))
            with self._workflow_guard:
                self._workflow_history[run_id] = {k: result[k] for k in ("run_id", "success", "executed_steps", "elapsed_ms")}
                while len(self._workflow_history) > 20:
                    self._workflow_history.popitem(last=False)
                self._active_workflow = None
        return result

    def execute_batch_actions(self, steps, return_ui_after=False):
        # Validate the ENTIRE macro, including late/nested steps, before the first tap.
        model = Workflow(steps=normalize_legacy_steps(steps), return_ui=return_ui_after)
        return self.run_workflow(model)
