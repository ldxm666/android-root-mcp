"""Public, validated schemas shared by tools and persisted workflows."""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Selector(StrictModel):
    """All supplied predicates are ANDed. Ambiguous actions need an explicit nth."""

    text: Optional[str] = None
    text_contains: Optional[str] = None
    text_regex: Optional[str] = Field(default=None, max_length=256)
    resource_id: Optional[str] = None
    description: Optional[str] = None
    description_contains: Optional[str] = None
    class_name: Optional[str] = None
    package: Optional[str] = None
    hint: Optional[str] = None
    clickable: Optional[bool] = None
    enabled: Optional[bool] = None
    checked: Optional[bool] = None
    focused: Optional[bool] = None
    scrollable: Optional[bool] = None
    nth: Optional[int] = Field(default=None, ge=0, le=10000)
    within: Optional[tuple[int, int, int, int]] = None
    index: Optional[int] = Field(default=None, ge=0)
    snapshot_id: Optional[str] = None
    # Semantic addressing: derived from what the OS reported (role, label,
    # stable identity), never from pixels. Lets a caller re-find a control after
    # the layout moves, and locks a replay to a control rather than a coordinate.
    role: Optional[str] = None
    label_contains: Optional[str] = None
    semantic_key: Optional[str] = None

    @model_validator(mode="after")
    def validate_selector(self):
        fields = self.model_dump(exclude_none=True)
        if not set(fields) - {"nth", "snapshot_id", "within"}:
            raise ValueError("selector needs at least one matching predicate")
        if self.text_regex is not None:
            re.compile(self.text_regex)
        if self.index is not None and not self.snapshot_id:
            raise ValueError("index selectors require snapshot_id from a recent UI snapshot")
        if self.within and (self.within[2] <= self.within[0] or self.within[3] <= self.within[1]):
            raise ValueError("within must be [left, top, right, bottom]")
        return self


class Condition(StrictModel):
    kind: Literal["exists", "gone", "foreground", "shell", "stable"] = "exists"
    selector: Optional[Selector] = None
    package: Optional[str] = None
    activity: Optional[str] = None
    command: Optional[str] = None
    root: bool = False
    contains: Optional[str] = None
    exit_code: int = 0
    stable_samples: int = Field(default=2, ge=2, le=10)

    @model_validator(mode="after")
    def validate_condition(self):
        if self.kind in ("exists", "gone") and self.selector is None:
            raise ValueError(f"{self.kind} requires selector")
        if self.kind == "foreground" and not (self.package or self.activity):
            raise ValueError("foreground needs package and/or activity")
        if self.kind == "shell" and not self.command:
            raise ValueError("shell condition requires command")
        return self


class WorkflowStep(StrictModel):
    """A bounded, prevalidated workflow. Unknown actions/parameters are errors."""

    action: Literal[
        "launch", "tap", "long_click", "double_click", "input", "clear", "key",
        "swipe", "wait", "assert", "shell", "if", "repeat", "sleep", "read_text",
    ]
    label: str = Field(default="", max_length=100)
    selector: Optional[Selector] = None
    x: Optional[int] = Field(default=None, ge=0)
    y: Optional[int] = Field(default=None, ge=0)
    text: str = Field(default="", max_length=100000)
    clear: bool = True
    enter: bool = False
    key: str = "BACK"
    package: str = ""
    activity: str = ""
    direction: Literal["up", "down", "left", "right"] = "up"
    scale: float = Field(default=0.6, gt=0, le=0.9)
    duration_ms: int = Field(default=300, ge=1, le=10000)
    command: str = Field(default="", max_length=100000)
    root: bool = False
    condition: Optional[Condition] = None
    expect: Optional[Condition] = None
    timeout: float = Field(default=5, ge=0, le=300)
    ms: int = Field(default=0, ge=0, le=60000)
    on_error: Literal["stop", "continue"] = "stop"
    count: int = Field(default=1, ge=1, le=100)
    steps: list[WorkflowStep] = Field(default_factory=list, max_length=200)
    else_steps: list[WorkflowStep] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_step(self):
        if self.action in ("tap", "long_click", "double_click"):
            if self.selector is None and (self.x is None or self.y is None):
                raise ValueError("tap needs selector or x and y")
        if self.action in ("clear", "read_text") and not self.selector:
            raise ValueError(f"{self.action} needs selector")
        if self.action == "launch" and not self.package:
            raise ValueError("launch needs package")
        if self.action in ("wait", "assert", "if") and not self.condition:
            raise ValueError(f"{self.action} needs condition")
        if self.action == "shell" and not self.command:
            raise ValueError("shell needs command")
        if self.action in ("if", "repeat") and not self.steps:
            raise ValueError(f"{self.action} needs nonempty steps")
        if self.action not in ("if", "repeat") and (self.steps or self.else_steps):
            raise ValueError("nested steps only belong to if/repeat")
        return self


class Workflow(StrictModel):
    steps: list[WorkflowStep] = Field(min_length=1, max_length=200)
    timeout: float = Field(default=120, gt=0, le=1800)
    max_executed_steps: int = Field(default=500, ge=1, le=5000)
    return_ui: bool = True

    @model_validator(mode="after")
    def validate_depth(self):
        def visit(steps, depth=0):
            if depth > 8:
                raise ValueError("workflow nesting exceeds 8 levels")
            return sum(1 + visit(s.steps, depth + 1) + visit(s.else_steps, depth + 1) for s in steps)
        if visit(self.steps) > 500:
            raise ValueError("workflow definition exceeds 500 total steps")
        return self


class FormField(StrictModel):
    selector: Selector
    value: str = Field(max_length=100000)


class ShellCommand(StrictModel):
    command: str = Field(min_length=1, max_length=100000)
    label: str = Field(default="", max_length=100)


def normalize_legacy_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate v1 macro syntax without silently accepting misspelled actions."""
    converted = []
    aliases = {"click": "tap", "type": "input", "root_cmd": "shell", "wait_for": "wait"}
    selector_keys = {
        "index": "index", "snapshot_id": "snapshot_id", "id": "resource_id",
        "resource_id": "resource_id", "desc": "description_contains",
        "description": "description_contains", "text_contains": "text_contains",
    }
    for original in steps:
        s = dict(original)
        action = s.get("action", "")
        s["action"] = aliases.get(action, action)
        if action == "root_cmd":
            s["root"] = True
        if "cmd" in s:
            s["command"] = s.pop("cmd")
        if "value" in s:
            s["text"] = s.pop("value")
        if "code" in s:
            s["key"] = str(s.pop("code"))
        selector = dict(s.get("selector") or {})
        for key, dest in selector_keys.items():
            if key in s:
                selector[dest] = s.pop(key)
        if s["action"] in ("tap", "long_click", "double_click", "wait") and "text" in s:
            selector["text"] = s.pop("text")
        if "hint" in s:
            selector["hint"] = s.pop("hint")
        if selector:
            if s["action"] == "wait":
                s["condition"] = {"kind": "exists", "selector": selector}
            else:
                s["selector"] = selector
        if s.pop("long_click", False):
            s["action"] = "long_click"
        # v1 wait_ms is kept as an explicit following sleep, never a hidden delay.
        delay = s.pop("wait_ms", 0)
        converted.append(s)
        if delay:
            converted.append({"action": "sleep", "ms": delay})
    return converted
