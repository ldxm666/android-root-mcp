import threading
import time

import pytest
from pydantic import ValidationError

from src.automation import AutomationError
from src.models import Condition, Selector, Workflow, normalize_legacy_steps
from src.transport import CommandResult


def test_selector_and_semantics(controller):
    found = controller.find_elements(Selector(text="确定", resource_id="ok"))
    assert found["count"] == 1
    assert found["elements"][0]["resource_id"] == "com.example.app:id/ok"


def test_ambiguous_actions_fail_before_click(controller):
    with pytest.raises(AutomationError, match="several"):
        controller.element_action(Selector(text="确定"), return_ui=False)
    assert not controller._u2_device.clicks


def test_explicit_nth(controller):
    result = controller.element_action(Selector(text="确定", nth=1), return_ui=False)
    assert result["success"]
    assert controller._u2_device.clicks == [(550, 200)]


def test_disabled(controller):
    with pytest.raises(AutomationError, match="disabled"):
        controller.element_action(Selector(resource_id="disabled"), return_ui=False)


def test_index_requires_snapshot_id():
    with pytest.raises(ValidationError):
        Selector(index=1)


def test_stale_snapshot_fails(controller):
    snapshot = controller.ui_snapshot()
    controller._u2_device.xml = controller._u2_device.xml.replace("搜索", "Changed")
    with pytest.raises(AutomationError) as exc:
        controller.element_action(Selector(index=1, snapshot_id=snapshot["snapshot_id"]), return_ui=False)
    assert exc.value.code == "STALE_SNAPSHOT"


def test_legacy_dirty_index_is_not_remapped(controller):
    controller.ui_snapshot()
    controller.invalidate_ui()
    result = controller.tap_target(index=1, return_ui_after=False)
    assert not result["success"] and not controller._u2_device.clicks


def test_filter_does_not_renumber_indices(controller):
    all_nodes = controller.ui_snapshot(only_interactive=False)["elements"]
    filtered = controller.ui_snapshot(keyword="ok2")["elements"]
    assert filtered[0]["index"] == next(n["index"] for n in all_nodes if n["id"] == "ok2")


def test_snapshot_diff(controller):
    before = controller.ui_snapshot()
    assert controller.ui_snapshot(since_snapshot_id=before["snapshot_id"])["diff"]["unchanged"]
    controller._u2_device.xml = controller._u2_device.xml.replace("搜索", "new")
    diff = controller.ui_snapshot(since_snapshot_id=before["snapshot_id"])["diff"]
    assert len(diff["changed"]) == 1 and not diff["added"] and not diff["removed"]


def test_expired_snapshot_diff(controller):
    assert not controller.ui_snapshot(since_snapshot_id="unknown")["diff"]["available"]


@pytest.mark.parametrize("selector,count", [
    ({"text_regex": "^确"}, 2), ({"within": [0, 0, 350, 280], "text": "确定"}, 1),
    ({"enabled": False}, 1), ({"checked": True}, 1), ({"class_name": "android.widget.EditText"}, 1),
    ({"text": "确定", "resource_id": "query"}, 0),
])
def test_predicates(controller, selector, count):
    assert controller.find_elements(Selector(**selector))["count"] == count


def test_unicode_settext_and_append(controller):
    selector = Selector(resource_id="query")
    assert controller.element_action(selector, "set_text", "中文🚀'&", return_ui=False)["success"]
    assert controller._u2_device.value == "中文🚀'&"
    assert controller.element_action(selector, "append_text", "ABC", return_ui=False)["success"]
    assert controller._u2_device.value == "中文🚀'&ABC"


def test_input_target_missing_never_types_elsewhere(controller):
    result = controller.input_text_fast("secret", resource_id="missing", return_ui_after=False)
    assert not result["success"] and controller._u2_device.value == "old"


def test_check_idempotent(controller):
    result = controller.element_action(Selector(resource_id="toggle"), "check", return_ui=False)
    assert result["success"] and not result["changed"] and not controller._u2_device.clicks


def test_gone_does_not_succeed_when_ui_failed(controller, monkeypatch):
    def fail():
        raise AutomationError("transport missing")
    monkeypatch.setattr(controller, "_capture_ui", fail)
    result = controller.wait_condition(Condition(kind="gone", selector=Selector(text="whatever")), timeout=0)
    assert not result["success"]


def test_stable_wait(controller):
    result = controller.wait_condition(Condition(kind="stable"), timeout=1, poll_interval=0.05)
    assert result["success"] and result["attempts"] == 2


def test_workflow_branches_and_loops(controller, monkeypatch):
    executed = []
    def shell(command, **kwargs):
        executed.append(command)
        return CommandResult([], 0, command.encode())
    monkeypatch.setattr(controller, "shell_result", shell)
    result = controller.run_workflow(Workflow(steps=[
        {"action": "if", "condition": {"kind": "exists", "selector": {"text": "absent"}},
         "steps": [{"action": "shell", "command": "wrong"}],
         "else_steps": [{"action": "repeat", "count": 3, "steps": [{"action": "shell", "command": "ok"}]}]},
        {"action": "assert", "condition": {"kind": "exists", "selector": {"resource_id": "query"}}},
    ], return_ui=False))
    assert result["success"] and executed == ["ok", "ok", "ok"]
    assert result["executed_steps"] == 6


def test_fail_fast_exit_code(controller, monkeypatch):
    commands = []
    def shell(command, **kwargs):
        commands.append(command)
        return CommandResult([], 5, b"failed")
    monkeypatch.setattr(controller, "shell_result", shell)
    result = controller.run_workflow(Workflow(steps=[{"action": "shell", "command": "bad"}, {"action": "shell", "command": "never"}], return_ui=False))
    assert not result["success"] and commands == ["bad"]


def test_explicit_continue_still_reports_failed_run(controller, monkeypatch):
    monkeypatch.setattr(controller, "shell_result", lambda *a, **k: CommandResult([], 2))
    result = controller.run_workflow(Workflow(steps=[{"action": "shell", "command": "bad", "on_error": "continue"}, {"action": "sleep", "ms": 0}], return_ui=False))
    assert not result["success"] and result["executed_steps"] == 2 and result["failed_steps"] == ["0"]


def test_unknown_late_action_validates_before_effects(controller):
    with pytest.raises(ValidationError):
        controller.execute_batch_actions([{"action": "tap", "text": "搜索"}, {"action": "typo"}], False)
    assert not controller._u2_device.clicks


def test_workflow_step_budget(controller):
    result = controller.run_workflow(Workflow(steps=[{"action": "repeat", "count": 100, "steps": [{"action": "sleep", "ms": 0}]}], max_executed_steps=3, return_ui=False))
    assert not result["success"] and result["code"] == "STEP_BUDGET_EXCEEDED"


def test_workflow_deadline(controller):
    result = controller.run_workflow(Workflow(steps=[{"action": "sleep", "ms": 500}], timeout=0.05, return_ui=False))
    assert not result["success"] and result["code"] == "WORKFLOW_TIMEOUT"
    assert controller._operation_deadline is None


def test_cooperative_cancel(controller):
    returned = []
    worker = threading.Thread(target=lambda: returned.append(controller.run_workflow(Workflow(steps=[{"action": "sleep", "ms": 5000}, {"action": "tap", "x": 1, "y": 1}], return_ui=False))))
    worker.start()
    for _ in range(100):
        if controller.workflow_control()["active"]:
            break
        time.sleep(0.005)
    assert controller.workflow_control("cancel")["success"]
    worker.join(3)
    assert not worker.is_alive() and returned[0]["code"] == "CANCELLED"
    assert not controller._u2_device.clicks


def test_saved_workflows_and_path_traversal(controller):
    workflow = Workflow(steps=[{"action": "sleep", "ms": 0}], return_ui=False)
    controller.saved_workflow("save", "test", workflow)
    assert controller.saved_workflow()["names"] == ["test"]
    assert controller.saved_workflow("run", "test")["success"]
    assert controller.saved_workflow("load", "test")["workflow"]["steps"][0]["action"] == "sleep"
    with pytest.raises(ValueError):
        controller.saved_workflow("save", "../escape", workflow)
    controller.saved_workflow("delete", "test")
    assert not controller.saved_workflow()["names"]


def test_legacy_conversion():
    data = normalize_legacy_steps([{"action": "root_cmd", "cmd": "id"}, {"action": "wait_for", "id": "q"}, {"action": "input", "text": "中文", "id": "q", "wait_ms": 5}])
    assert data[0] == {"action": "shell", "command": "id", "root": True}
    assert data[1]["condition"]["selector"]["resource_id"] == "q"
    assert data[2]["selector"]["resource_id"] == "q" and data[3]["ms"] == 5


def test_branch_transport_failure_does_not_take_else(controller, monkeypatch):
    def fail():
        raise AutomationError("UI offline")
    monkeypatch.setattr(controller, "_capture_ui", fail)
    result = controller.run_workflow(Workflow(steps=[{
        "action": "if", "condition": {"kind": "exists", "selector": {"text": "button"}},
        "steps": [{"action": "tap", "x": 1, "y": 1}], "else_steps": [{"action": "tap", "x": 2, "y": 2}],
    }], return_ui=False))
    assert not result["success"] and result["code"] == "CONDITION_OBSERVATION_FAILED"
    assert not controller._u2_device.clicks


def test_exact_full_id_uses_native_rpc_without_ui_dump(controller, monkeypatch):
    d = controller._u2_device
    d.jsonrpc = object()
    d.count = 1
    d.info = {"bounds": {"left": 30, "top": 150, "right": 300, "bottom": 250},
              "className": "android.widget.Button", "resourceName": "com.example.app:id/ok", "enabled": True}
    def no_dump():
        raise AssertionError("native selector should not dump the entire UI")
    monkeypatch.setattr(controller, "_capture_ui", no_dump)
    result = controller.element_action(Selector(resource_id="com.example.app:id/ok"), return_ui=False)
    assert result["success"] and result["element"]["engine"] == "uiautomator2_selector"
    assert d.clicks == [(165, 200)]


def test_native_ambiguity_does_not_click(controller):
    d = controller._u2_device
    d.jsonrpc = object()
    d.count = 2
    with pytest.raises(AutomationError, match="several"):
        controller.element_action(Selector(text="duplicate"), return_ui=False)
    assert not d.clicks


def test_native_missing_window_falls_back_to_full_xml(controller):
    d = controller._u2_device
    d.jsonrpc = object()
    d.count = 0
    result = controller.element_action(Selector(resource_id="com.example.app:id/ok"), return_ui=False)
    assert result["success"]
    assert d.clicks == [(165, 200)]
