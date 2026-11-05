import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from src.transport import CommandResult

PREF = b'<?xml version="1.0"?><map><string name="keep">original</string><boolean name="flag" value="false"/></map>'


def test_preferences_typed_updates_preserve_others(controller, monkeypatch):
    stopped, written = [], []
    monkeypatch.setattr(controller, "require_shell", lambda command, **kwargs: stopped.append(command))
    monkeypatch.setattr(controller, "read_remote_bytes", lambda *a, **k: PREF)
    monkeypatch.setattr(controller, "write_remote_bytes", lambda path, data, **kwargs: (written.append((path, data, kwargs)) or {"success": True}))
    result = controller.preferences("com.example.app", "settings", "set", values={
        "flag": {"type": "boolean", "value": True},
        "text": {"type": "string", "value": "中文<&🚀"},
        "long": {"type": "long", "value": 2**40},
        "set": {"type": "set", "value": ["a", "b", "a"]},
    })
    root = ET.fromstring(written[0][1])
    nodes = {n.get("name"): n for n in root}
    assert result["success"] and result["app_left_stopped"] and len(stopped) == 1
    assert nodes["keep"].text == "original" and nodes["flag"].get("value") == "true"
    assert nodes["text"].text == "中文<&🚀" and [n.text for n in nodes["set"]] == ["a", "b"]
    assert written[0][2]["expected_sha256"] == hashlib.sha256(PREF).hexdigest()


@pytest.mark.parametrize("entry", [
    {"type": "boolean", "value": "false"}, {"type": "int", "value": True},
    {"type": "int", "value": 2**40}, {"type": "long", "value": 2**70},
    {"type": "float", "value": float("nan")}, {"type": "set", "value": [1]},
])
def test_invalid_preference_has_no_device_effect(controller, monkeypatch, entry):
    commands = []
    monkeypatch.setattr(controller, "require_shell", lambda *a, **k: commands.append(a))
    with pytest.raises(ValueError):
        controller.preferences("com.example.app", "settings", "set", values={"bad": entry})
    assert not commands


def test_preferences_delete(controller, monkeypatch):
    written = []
    monkeypatch.setattr(controller, "require_shell", lambda *a, **k: "")
    monkeypatch.setattr(controller, "read_remote_bytes", lambda *a, **k: PREF)
    monkeypatch.setattr(controller, "write_remote_bytes", lambda path, data, **kwargs: (written.append(data) or {"success": True}))
    controller.preferences("com.example.app", "settings.xml", "delete", keys=["flag"])
    root = ET.fromstring(written[0])
    assert [n.get("name") for n in root] == ["keep"]


def test_profile_restores_exact_values_and_missing_keys(controller, monkeypatch):
    before = {"window_animation_scale": "0.5", "transition_animation_scale": "2", "animator_duration_scale": "null", "stay_on_while_plugged_in": "7"}
    calls = []
    monkeypatch.setattr(controller, "require_shell", lambda command, **kwargs: before[command.rsplit(" ", 1)[-1]])
    monkeypatch.setattr(controller, "shell_batch", lambda commands, **kwargs: (calls.append(commands) or {"success": True}))
    result = controller.agent_profile()
    assert result["before"] == before
    restored = controller.agent_profile("restore", result["profile_id"])
    assert restored["success"]
    strings = [r["command"] for r in calls[1]]
    assert "settings delete global animator_duration_scale" in strings
    assert "settings put global window_animation_scale 0.5" in strings
    assert "settings put global stay_on_while_plugged_in 7" in strings
    assert not controller.agent_profile("restore", result["profile_id"])["changed"]


def test_profile_other_device_rejected(controller):
    path = controller.state_dir / "profiles" / (("b"*32)+".json")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"serial": "other"}), encoding="utf-8")
    with pytest.raises(ValueError, match="different device"):
        controller.agent_profile("restore", "b"*32)


def test_sqlite_checkpointed_write_preserves_original_backup(controller, monkeypatch, tmp_path):
    local = tmp_path / "original.db"
    with sqlite3.connect(local) as db:
        db.execute("CREATE TABLE t(value TEXT)")
        db.execute("INSERT INTO t VALUES('old')")
    original = local.read_bytes()
    commands, replaced = [], []
    def require(command, **kwargs):
        commands.append(command)
        if command.startswith("sha256sum"):
            return hashlib.sha256(original).hexdigest() + " main.db"
        return ""
    def shell(command, **kwargs):
        if command.startswith("test -f") and ("-wal" in command or "-shm" in command):
            return CommandResult([], 1)
        if kwargs.get("output_file"):
            Path(kwargs["output_file"]).write_bytes(original)
        return CommandResult([], 0)
    def write(path, data, **kwargs):
        replaced.append((data, kwargs))
        return {"success": True, "sha256": hashlib.sha256(data).hexdigest()}
    monkeypatch.setattr(controller, "require_shell", require)
    monkeypatch.setattr(controller, "shell_result", shell)
    monkeypatch.setattr(controller, "write_remote_bytes", write)
    result = controller.sqlite("/data/user/0/com.example.app/databases/test.db", "UPDATE t SET value=?", ["中文"],
                               write=True, package="com.example.app", stop_app=True)
    assert result["success"] and result["app_left_stopped"] and result["backup_path"]
    updated = tmp_path / "updated.db"
    updated.write_bytes(replaced[0][0])
    with sqlite3.connect(updated) as db:
        assert db.execute("SELECT value FROM t").fetchone()[0] == "中文"
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert replaced[0][1]["expected_sha256"] == hashlib.sha256(original).hexdigest()
    assert any("cp -p" in cmd and "mcp-backup" in cmd for cmd in commands)
    assert local.read_bytes() == original


def test_webview_socket_validation(controller):
    with pytest.raises(ValueError):
        controller.webview("pages", socket="/tmp/evil;id")


def test_record_screen_scales_file_limit(controller, monkeypatch):
    options = []
    monkeypatch.setattr(controller, "start_job", lambda command, **kwargs: (options.append(kwargs) or {"success": True, "directory": "/tmp/fake"}))
    assert controller.record_screen(seconds=180, bit_rate=4_000_000)["success"]
    assert options[0]["max_output_mb"] > 90
    with pytest.raises(ValueError, match="256"):
        controller.record_screen(seconds=180, bit_rate=100_000_000)
