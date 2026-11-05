"""Opt-in smoke tests. Only mutate a unique /data/local/tmp fixture and own jobs.

No app install/uninstall, user-data writes, settings changes, reboot or UI clicks.
Usage: python scripts/live_smoke.py --serial SERIAL --report .artifacts/live.json
"""

import argparse
import hashlib
import json
import shlex
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError

from src.advanced_controller import AdvancedAndroidController
from src.models import Selector, Workflow


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("--report", default=".artifacts/live.json")
    parser.add_argument("--skip-ui", action="store_true")
    args = parser.parse_args()
    report_path = Path(args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    reports = []
    c = AdvancedAndroidController(args.serial, state_dir=report_path.parent / "test-state")
    remote = "/data/local/tmp/mcp_suite_smoke_" + uuid.uuid4().hex
    tracked_jobs = []

    def check(name, fn):
        start = time.monotonic()
        try:
            details = fn()
            record = {"name": name, "passed": True, "elapsed_ms": round((time.monotonic()-start)*1000, 1), "details": details}
        except Exception as exc:
            record = {"name": name, "passed": False, "elapsed_ms": round((time.monotonic()-start)*1000, 1), "error": str(exc), "details": getattr(exc, "details", {})}
        reports.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)

    def expect(value, message="assertion failed"):
        if not value:
            raise AssertionError(message)

    def wait_job(job, seconds=5):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            status = c.job_status(job)
            if status.get("state") in ("finished", "interrupted"):
                return status
            time.sleep(0.1)
        raise AssertionError(f"job did not complete: {status}")

    try:
        c.require_shell("mkdir -m 700 " + shlex.quote(remote), root=True)

        def identities():
            r = c.shell_result("id", root=True)
            expect(r.success and "uid=0(" in r.text, r.as_dict())
            return {"root_uid_verified": True, "sdk": c.require_shell("getprop ro.build.version.sdk")}
        check("root_identity", identities)

        def exit_codes():
            r = c.shell_result("printf out; printf err >&2; exit 7", root=True)
            expect(r.exit_code == 7 and r.text == "out" and r.stderr == b"err" and not r.success, r.as_dict())
            return {"exit_code": r.exit_code, "stdout_stderr_separate": True}
        check("exit_codes", exit_codes)

        def binary():
            payload = b"\0\1\xff\n\r\n" + "中文🚀\"';&|$()".encode()
            path = remote + "/引号'中文.bin"
            result = c.write_remote_bytes(path, payload, mode="640")
            expect(result["success"], result)
            expect(c.read_remote_bytes(path) == payload, "binary transfer altered bytes")
            old_hash = hashlib.sha256(payload).hexdigest()
            replaced = c.write_remote_bytes(path, b"changed", expected_sha256=old_hash)
            expect(replaced["success"], replaced)
            expect(c.read_remote_bytes(replaced["backup_path"]) == payload)
            mode = c.require_shell("stat -c %a " + shlex.quote(path), root=True).strip()
            expect(mode == "640", mode)
            wrong = c.write_remote_bytes(path, b"must not write", expected_sha256="0"*64)
            expect(not wrong["success"] and c.read_remote_bytes(path) == b"changed")
            return {"unicode_quoted_path": True, "binary_roundtrip": True, "mode_preserved": mode, "backup_and_hash_guard": True}
        check("root_files", binary)

        def tree_transfer():
            with tempfile.TemporaryDirectory() as temp:
                local = Path(temp) / "source"
                (local / "nested").mkdir(parents=True)
                (local / "nested" / "中文.txt").write_bytes(b"abc\0\n\r\n")
                pushed = c.transfer("push", remote + "/tree", str(local))
                expect(pushed["success"], pushed)
                pulled = c.transfer("pull", remote + "/tree", str(Path(temp)/"received"))
                expect(pulled["success"], pulled)
                expect((Path(temp)/"received/nested/中文.txt").read_bytes() == b"abc\0\n\r\n")
            return {"directory_push_pull": True}
        check("directory_transfer", tree_transfer)

        def sqlite_wal():
            with tempfile.TemporaryDirectory() as temp:
                db = Path(temp)/"fixture.db"
                connection = sqlite3.connect(db)
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("CREATE TABLE test(id INTEGER, text TEXT, data BLOB)")
                    connection.execute("INSERT INTO test VALUES(?,?,?)", (1, "中文🚀", b"\0\xff"))
                    connection.commit()
                    for suffix in ("", "-wal", "-shm"):
                        result = c.transfer("push", remote + "/fixture.db" + suffix, str(db)+suffix)
                        expect(result["success"], result)
                    result = c.sqlite(remote + "/fixture.db", "SELECT id,text,data FROM test WHERE id=?", [1])
                    expect(result["success"] and result["rows"] == [[1, "中文🚀", {"base64": "AP8=", "bytes": 2}]], result)
                    try:
                        c.sqlite(remote + "/fixture.db", "DELETE FROM test")
                    except sqlite3.OperationalError:
                        pass
                    else:
                        raise AssertionError("read-only SQLite accepted a DELETE")
                finally:
                    connection.close()
            return {"wal_rows_visible_without_phone_sqlite3": True, "read_only_enforced": True}
        check("sqlite_wal", sqlite_wal)

        def batch_behavior():
            result = c.shell_batch([{"command": "printf first"}, {"command": "exit 7"}, {"command": "echo should_not_run"}], root=True)
            expect(not result["success"] and result["executed"] == 2 and result["results"][1]["exit_code"] == 7, result)
            continued = c.shell_batch([{"command": "exit 3"}, {"command": "printf last"}], stop_on_error=False)
            expect(not continued["success"] and continued["executed"] == 2, continued)
            return {"fail_fast": True, "continue_reports_failure": True}
        check("batch_error_handling", batch_behavior)

        def batch_speed():
            commands = [{"command": f"printf {i}", "label": str(i)} for i in range(20)]
            start = time.monotonic()
            for command in commands:
                expect(c.shell_result(command["command"], root=True).success)
            serial_ms = (time.monotonic()-start)*1000
            start = time.monotonic()
            result = c.shell_batch(commands, root=True)
            batch_ms = (time.monotonic()-start)*1000
            expect(result["success"] and result["executed"] == 20, result)
            return {"commands": 20, "separate_ms": round(serial_ms, 1), "one_batch_ms": round(batch_ms, 1), "measured_ratio": round(serial_ms/batch_ms, 2)}
        check("batch_timing", batch_speed)

        def deadline():
            start = time.monotonic()
            result = c.shell_result("sleep 5", root=True, timeout=0.3)
            elapsed = time.monotonic()-start
            expect(not result.success and result.timed_out and elapsed < 3, result.as_dict())
            return {"timed_out": True, "wall_seconds": round(elapsed, 2)}
        check("remote_timeout", deadline)

        def workflows():
            result = c.run_workflow(Workflow(steps=[
                {"action": "if", "condition": {"kind": "shell", "command": "test -d " + remote},
                 "steps": [{"action": "repeat", "count": 3, "steps": [{"action": "shell", "command": "printf x >> " + remote + "/counter", "root": True}]}],
                 "else_steps": [{"action": "shell", "command": "exit 9"}]},
                {"action": "assert", "condition": {"kind": "shell", "command": "test $(wc -c < " + remote + "/counter) -eq 3", "root": True}},
            ], return_ui=False))
            expect(result["success"], result)
            try:
                c.execute_batch_actions([{"action": "root_cmd", "cmd": "touch " + remote + "/never"}, {"action": "typo"}], False)
            except ValidationError:
                pass
            else:
                raise AssertionError("unknown step not rejected")
            expect(c.shell_result("test ! -e " + remote + "/never", root=True).success)
            return {"condition_branch_repeat_assert": True, "whole_plan_prevalidation": True}
        check("workflows", workflows)

        def cancel_workflow():
            returned = []
            worker = threading.Thread(target=lambda: returned.append(c.run_workflow(Workflow(steps=[{"action": "sleep", "ms": 5000}, {"action": "shell", "command": "touch " + remote + "/after_cancel", "root": True}], return_ui=False))))
            worker.start()
            deadline = time.monotonic()+2
            while not c.workflow_control()["active"] and time.monotonic()<deadline:
                time.sleep(0.01)
            expect(c.workflow_control("cancel")["success"])
            worker.join(3)
            expect(not worker.is_alive() and returned[0].get("code") == "CANCELLED", returned)
            expect(c.shell_result("test ! -e " + remote + "/after_cancel", root=True).success)
            return {"cancellation_stops_later_steps": True}
        check("workflow_cancel", cancel_workflow)

        def jobs():
            result = c.start_job("printf '中文'; sleep 0.2; printf done", root=True, timeout=5, label="smoke")
            expect(result["success"], result)
            job = result["job_id"]
            tracked_jobs.append(job)
            status = wait_job(job)
            expect(status["exit_code"] == 0, status)
            output = c.device_job("output", job_id=job)
            expect(output["content"] == "中文done", output)
            expect(c.device_job("output", job_id=job, offset=output["next_offset"])["bytes"] == 0)
            ordinary = c.start_job("printf 'normal-shell\\n'", root=False, timeout=5, label="nonroot-smoke")
            expect(ordinary["success"], ordinary)
            tracked_jobs.append(ordinary["job_id"])
            expect(wait_job(ordinary["job_id"])["exit_code"] == 0)
            expect(c.device_job("output", job_id=ordinary["job_id"])["content"] == "normal-shell\n")
            other = c.start_job("sleep 30", root=True, timeout=40, label="cancel-smoke")
            expect(other["success"], other)
            tracked_jobs.append(other["job_id"])
            time.sleep(0.1)
            expect(c.device_job("cancel", job_id=other["job_id"])["success"])
            wait_job(other["job_id"])
            return {"background_exit_and_incremental_output": True, "cancel_own_process_group": True}
        check("background_jobs", jobs)

        def forwards():
            result = c.port_forward("add", "tcp:0", "tcp:1")
            expect(result["success"] and result["stdout"].isdigit(), result)
            port = result["stdout"]
            try:
                expect("tcp:"+port in c.port_forward()["stdout"])
            finally:
                expect(c.port_forward("remove", "tcp:"+port)["success"])
            return {"dynamic_forward_and_cleanup": True}
        check("port_forward", forwards)

        if not args.skip_ui:
            def ui():
                first = c.ui_snapshot(max_elements=10)
                second = c.ui_snapshot(max_elements=10, since_snapshot_id=first["snapshot_id"])
                expect(first["success"] and second["success"])
                from collections import Counter
                identifiers = Counter(n["resource_id"] for n in c._current_snapshot["elements"] if n["resource_id"])
                native = {}
                candidates = [n for n in c._current_snapshot["elements"] if n["resource_id"]
                              and identifiers[n["resource_id"]] == 1 and n["enabled"]
                              and n["package"] == second["current_package"]]
                for candidate in sorted(candidates, key=lambda n: not n["clickable"])[:30]:
                    found = c._native_find_one(Selector(resource_id=candidate["resource_id"]))
                    if found and found["count"] == 1:
                        native = found["elements"][0]
                        break
                return {"engine": second["engine"], "nodes": second["total_elements"],
                        "first_capture_ms": first["elapsed_ms"], "warm_capture_ms": second["elapsed_ms"],
                        "diff_available": second["diff"]["available"], "native_selector_engine": native.get("engine", "no_compatible_target_in_current_window")}
            check("ui_read_and_diff", ui)
    finally:
        for job in tracked_jobs:
            try:
                c.device_job("cancel", job_id=job)
                wait_job(job)
                c.device_job("cleanup", job_id=job)
            except Exception as exc:
                reports.append({"name": "cleanup_job", "passed": False, "error": str(exc)})
        try:
            c.require_shell("rm -rf " + shlex.quote(remote), root=True)
        except Exception as exc:
            reports.append({"name": "cleanup_fixture", "passed": False, "error": str(exc)})
        report = {"passed": all(item["passed"] for item in reports), "checks": reports,
                  "device_sdk": "observed per check", "finished_at_unix": time.time(),
                  "scope": "read-only device probes + isolated /data/local/tmp files/jobs; no app/user-data/settings/UI mutations"}
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
