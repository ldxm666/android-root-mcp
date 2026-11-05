import hashlib
import io
import json
import shlex
import sqlite3
import tarfile
from pathlib import Path

import pytest

from src.advanced_controller import DeviceError
from src.data_ops import package_name, remote_path, validate_archive
from src.transport import CommandResult


@pytest.mark.parametrize("bad", ["bad;id", "com.a/../../", "", "com.a b", "../com.app"])
def test_package_validation(bad):
    with pytest.raises(ValueError):
        package_name(bad)


@pytest.mark.parametrize("bad", ["relative", "/", "/a\0b", "/a\nb"])
def test_path_validation(bad):
    with pytest.raises(ValueError):
        remote_path(bad)


@pytest.mark.parametrize("name,kind", [("../evil", "file"), ("/evil", "file"), ("C:/evil", "file"), ("link", "symlink"), ("link", "hardlink")])
def test_unsafe_tar(name, kind):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo(name)
        if kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "/outside"
        elif kind == "hardlink":
            member.type = tarfile.LNKTYPE
            member.linkname = "../outside"
        archive.addfile(member)
    buffer.seek(0)
    with tarfile.open(fileobj=buffer) as archive:
        with pytest.raises(ValueError):
            validate_archive(archive)


def test_intent_quotes_and_full_activity(controller, monkeypatch):
    recorded = []
    monkeypatch.setattr(controller, "shell_result", lambda cmd, **kwargs: (recorded.append(cmd) or CommandResult([], 0, b"Starting: Intent")))
    value = "中文'; touch /data/local/tmp/NOT_EXECUTED; echo '"
    result = controller.send_intent(package="com.example.app", activity="com.example.app.MainActivity", extras={"value": value, "long": 2**40, "boolean": True}, user_id=10)
    assert result["success"]
    args = shlex.split(recorded[0])
    assert args[args.index("-n")+1] == "com.example.app/com.example.app.MainActivity"
    assert value in args and "--el" in args and "--ez" in args and "10" in args


def test_am_stdout_error_is_not_success(controller, monkeypatch):
    monkeypatch.setattr(controller, "shell_result", lambda *a, **k: CommandResult([], 0, b"Error type 3\nActivity does not exist"))
    assert not controller.send_intent(package="com.example.app", activity=".Missing")["success"]


def test_provider_requires_where(controller):
    with pytest.raises(ValueError, match="where"):
        controller.provider("delete", "content://test/items")


def test_provider_values_quote(controller, monkeypatch):
    called = []
    monkeypatch.setattr(controller, "shell_result", lambda cmd, **k: (called.append(shlex.split(cmd)) or CommandResult([], 0)))
    controller.provider("update", "content://test/items", where="name='x'", values={"body": "hi'; echo hacked; #", "enabled": True})
    assert "body:s:hi'; echo hacked; #" in called[0]


def test_shell_batch_failure_and_not_executed(controller, monkeypatch):
    def shell(script, **kwargs):
        import re
        token = re.search(r"__MCP_[a-f0-9]+__", script).group()
        output = f"\n{token}:0:BEGIN\nfirst\n{token}:0:END:0\n\n{token}:1:BEGIN\nfailed\n{token}:1:END:7\n"
        return CommandResult([], 7, output.encode())
    monkeypatch.setattr(controller, "shell_result", shell)
    result = controller.shell_batch([{"command": "echo first"}, {"command": "exit 7"}, {"command": "never"}])
    assert not result["success"] and result["executed"] == 2
    assert result["results"][1]["exit_code"] == 7


def test_shell_batch_truncation_no_false_success(controller, monkeypatch):
    monkeypatch.setattr(controller, "shell_result", lambda *a, **k: CommandResult([], 0, b"partial", truncated=True))
    assert not controller.shell_batch([{"command": "echo big"}])["success"]


def test_multiple_devices_require_selection(controller, monkeypatch):
    controller.serial = None
    monkeypatch.setattr(controller, "list_devices", lambda: {"devices": [{"serial": "a", "state": "device"}, {"serial": "b", "state": "device"}]})
    with pytest.raises(DeviceError) as exc:
        controller.ensure_device()
    assert exc.value.code == "DEVICE_SELECTION_REQUIRED"


def test_job_other_device_is_rejected(controller):
    path = controller._job_path("a"*32)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"serial": "other"}), encoding="utf-8")
    with pytest.raises(ValueError, match="different device"):
        controller.device_job("status", "a"*32)


def test_sqlite_write_requires_stop_before_any_command(controller):
    with pytest.raises(ValueError, match="stop_app"):
        controller.sqlite("/data/private.db", "DELETE FROM t", write=True)


def test_sqlite_wal_readonly_and_blob_limit(controller, tmp_path, monkeypatch):
    db = tmp_path / "source.db"
    connection = sqlite3.connect(db)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE t(id INTEGER, data BLOB)")
    connection.executemany("INSERT INTO t VALUES(?,?)", [(1, b"\0\xff"), (2, b"two")])
    connection.commit()
    remote_files = {"": db.read_bytes(), "-wal": Path(str(db)+"-wal").read_bytes(), "-shm": Path(str(db)+"-shm").read_bytes()}

    def require(command, **kwargs):
        if command.startswith("sha256sum"):
            return hashlib.sha256(remote_files[""]).hexdigest() + " main.db"
        return ""

    def shell(command, **kwargs):
        if command.startswith("cat ") and kwargs.get("output_file"):
            suffix = "-wal" if "main.db-wal" in command else "-shm" if "main.db-shm" in command else ""
            Path(kwargs["output_file"]).write_bytes(remote_files[suffix])
        return CommandResult([], 0)

    monkeypatch.setattr(controller, "require_shell", require)
    monkeypatch.setattr(controller, "shell_result", shell)
    result = controller.sqlite("/private/source.db", "SELECT * FROM t WHERE id > ?", [0], max_rows=1)
    assert result["success"] and result["truncated"] and result["rows"] == [[1, {"base64": "AP8=", "bytes": 2}]]
    with pytest.raises(sqlite3.OperationalError):
        controller.sqlite("/private/source.db", "DELETE FROM t")
    connection.close()
