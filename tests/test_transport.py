import sys

import pytest

from src.transport import execute


def test_exit_code_and_both_streams():
    result = execute([sys.executable, "-c", "import sys; print('out'); print('err',file=sys.stderr); sys.exit(7)"])
    assert not result.success
    assert result.exit_code == 7
    assert result.text == "out"
    assert result.as_dict()["stderr"] == "err"


def test_binary_newlines_are_not_translated():
    result = execute([sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes([0,13,10,255]))"])
    assert result.stdout == b"\0\r\n\xff"


def test_output_is_bounded():
    result = execute([sys.executable, "-c", "print('x'*10000)"], max_bytes=30)
    assert result.success and result.truncated and len(result.stdout) == 30


def test_timeout_does_not_report_success():
    result = execute([sys.executable, "-c", "import time; time.sleep(3)"], timeout=0.05)
    assert result.timed_out and not result.success


def test_stream_artifact(tmp_path):
    path = tmp_path / "artifact.bin"
    result = execute([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'abc'*10000)"], output_file=str(path), max_bytes=3)
    assert result.success and not result.stdout
    assert path.stat().st_size == 30000


@pytest.mark.parametrize("exe", ["adb.bat", "ADB.CMD"])
def test_no_batch_host_shell(exe):
    result = execute([exe, "shell", "echo a&b"])
    assert not result.success and "native adb" in result.error


@pytest.mark.parametrize("kwargs", [{"timeout": 0}, {"timeout": 4000}, {"max_bytes": 0}, {"max_bytes": 40*1024*1024}])
def test_transport_limits(kwargs):
    with pytest.raises(ValueError):
        execute([sys.executable, "-c", "pass"], **kwargs)


def test_binary_shell_framing_preserves_bytes_and_exit_code(controller, monkeypatch):
    import base64
    import re
    from pathlib import Path

    from src.transport import CommandResult
    payload = b"\0\n\r\n\xffbinary"
    def adb(args, **kwargs):
        assert args[0] == "exec-out"
        token = re.search(r"__MCP_BINARY_[0-9a-f]+__", args[1]).group()
        wire = payload + f"\n{token}:RC:7\n".encode() + base64.b64encode(b"failure\n") + f"\n{token}:END\n".encode()
        Path(kwargs["output_file"]).write_bytes(wire)
        return CommandResult([], 0)
    monkeypatch.setattr(controller, "adb_result", adb)
    result = controller.shell_result("binary command", root=True, binary=True)
    assert result.stdout == payload and result.stderr == b"failure\n"
    assert result.exit_code == 7 and not result.success


def test_binary_shell_missing_trailer_is_not_success(controller, monkeypatch):
    from pathlib import Path

    from src.transport import CommandResult
    def adb(args, **kwargs):
        Path(kwargs["output_file"]).write_bytes(b"su permission denied")
        return CommandResult([], 0)
    monkeypatch.setattr(controller, "adb_result", adb)
    result = controller.shell_result("cat protected", root=True, binary=True)
    assert not result.success and "trailer" in result.error
