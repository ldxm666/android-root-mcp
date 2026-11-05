"""v2 controller: compatibility with v1 tools plus reliable execution primitives."""

from __future__ import annotations

import base64
import importlib.metadata
import os
import re
import shlex
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .android_controller import AndroidRootController
from .automation import AutomationError, AutomationMixin
from .data_ops import DataMixin
from .device_ops import DeviceOpsMixin
from .jobs import JobsMixin
from .transport import execute, reset_fast_transport, try_fast_shell
from .visual import VisualMixin
from .webview import WebViewMixin


class DeviceError(RuntimeError):
    def __init__(self, message, code="DEVICE_ERROR", **details):
        super().__init__(message)
        self.code = code
        self.details = details


class AdvancedAndroidController(AutomationMixin, DataMixin, DeviceOpsMixin, JobsMixin, WebViewMixin, VisualMixin, AndroidRootController):
    def __init__(self, serial=None, state_dir=None):
        super().__init__(serial)
        self.state_dir = Path(state_dir or os.environ.get("ANDROID_MCP_STATE_DIR", Path.home() / ".android-root-mcp"))
        self.lock = threading.RLock()
        self._u2_retry_at = 0
        self._u2_error = ""
        self._operation_deadline = None
        self._device_timeout_available = None
        self.init_automation()
        self.init_jobs()
        self._owned_forwards = {}

    @staticmethod
    def _resolve_adb_executable():
        configured = os.environ.get("ADB_PATH")
        candidates = [configured] if configured else []
        for name in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
            if os.environ.get(name):
                candidates.append(os.path.join(os.environ[name], "platform-tools", "adb.exe" if os.name == "nt" else "adb"))
        candidates += [os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe")]
        which = shutil.which("adb")
        if which:
            if which.lower().endswith((".bat", ".cmd")):
                candidates.append(os.path.normpath(os.path.join(os.path.dirname(which), "..", "platform-tools", "adb.exe")))
            else:
                candidates.append(which)
        try:
            import adbutils
            candidates.append(adbutils.adb_path())
        except ImportError:
            pass
        for candidate in candidates:
            if candidate and os.path.isfile(candidate) and not candidate.lower().endswith((".bat", ".cmd")):
                return str(candidate)
        return configured or "adb"

    def adb_result(self, args, timeout=20, max_bytes=1_048_576, host=False, stdin=None, output_file=None):
        if not host:
            self.ensure_device()
        prefix = [self.adb_bin] + ([] if host else ["-s", self.serial])
        if self._operation_deadline:
            remaining = self._operation_deadline - time.monotonic()
            if remaining <= 0:
                raise AutomationError("Workflow deadline reached", "WORKFLOW_TIMEOUT")
            timeout = min(timeout, max(0.1, remaining))
        return execute(prefix + list(args), timeout=timeout, max_bytes=max_bytes, stdin=stdin, output_file=output_file)

    def ensure_device(self):
        if not self.serial:
            devices = self.list_devices()
            online = [d["serial"] for d in devices["devices"] if d["state"] == "device"]
            if len(online) != 1:
                raise DeviceError("Select an online device with device_connection(action='select', serial=...)",
                                  "DEVICE_SELECTION_REQUIRED", devices=devices["devices"])
            self.serial = online[0]

    def list_devices(self):
        r = self.adb_result(["devices", "-l"], host=True)
        if not r.success:
            raise DeviceError("Cannot list ADB devices", result=r.as_dict())
        devices = []
        for line in r.text.splitlines():
            parts = line.split()
            if len(parts) < 2 or line.startswith(("List of devices", "*")):
                continue
            details = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
            devices.append({"serial": parts[0], "state": parts[1], **details})
        return {"success": True, "selected_serial": self.serial, "devices": devices}

    def select_device(self, serial):
        if self._active_workflow:
            raise DeviceError("Cannot change device during a workflow", "WORKFLOW_BUSY")
        devices = self.list_devices()["devices"]
        if not any(d["serial"] == serial and d["state"] == "device" for d in devices):
            raise DeviceError("Requested device is not online/authorized", devices=devices)
        self.serial = serial
        self.reset_u2()
        # The warm pipe is bound to the previous serial; drop it so a stale
        # session can never answer for the newly selected phone.
        reset_fast_transport()
        try:
            from . import native_input as _native
            _native.reset()
        except Exception:
            pass
        self.init_automation()
        self._device_timeout_available = None
        self._screen_size = (1080, 2400)
        return {"success": True, "serial": serial, "ui_cache_cleared": True}

    def device_connection(self, action="list", serial="", address="", pairing_code=""):
        if action == "list":
            return self.list_devices()
        if action == "select":
            return self.select_device(serial)
        if action in ("connect", "disconnect", "pair"):
            if not re.fullmatch(r"(?:[A-Za-z0-9_.-]+|\[[0-9a-fA-F:]+\]):\d{1,5}", address):
                raise ValueError("address must be an explicit host:port (IPv6 in brackets)")
            args = [action, address]
            if action == "pair":
                if not re.fullmatch(r"\d{6}", pairing_code):
                    raise ValueError("pairing_code must contain six digits")
                args.append(pairing_code)
            r = self.adb_result(args, host=True, timeout=20).as_dict()
            # Some ADB host commands report errors on stdout with exit status 0.
            if re.search(r"failed|unable|cannot|error", r["stdout"], re.I):
                r["success"] = False
            if action == "disconnect" and address == self.serial and r["success"]:
                self.serial = None
                self.reset_u2()
                self.init_automation()
            return r
        raise ValueError("action must be list/select/connect/disconnect/pair")

    def shell_result(self, command, root=False, timeout=20, max_bytes=1_048_576, output_file=None, binary=False):
        if not command or "\0" in command:
            raise ValueError("command must be nonempty and contain no NUL")
        if not 0 < timeout <= 3600:
            raise ValueError("timeout must be in (0, 3600]")
        if not 1 <= max_bytes <= 32*1024*1024:
            raise ValueError("max_bytes must be between 1 and 32 MiB")
        self.ensure_device()
        if self._device_timeout_available is None:
            probe = self.adb_result(["shell", "-T", "command -v timeout"], timeout=5)
            self._device_timeout_available = probe.success and bool(probe.text.strip())
        if self._operation_deadline:
            timeout = min(timeout, max(0.1, self._operation_deadline - time.monotonic()))
        # Some OEM shells inject CR before LF even with adb shell -T. Binary
        # transfers MUST use exec-out, with an independently framed exit status.
        if binary or output_file:
            return self._binary_shell_result(command, root, timeout, max_bytes, output_file)
        # Wrap inside su so timeout and descendants share the requested UID.
        script = "sh -c " + shlex.quote(command)
        if self._device_timeout_available:
            script = f"timeout -s KILL {timeout:.3f} " + script
        if root:
            script = "su -c " + shlex.quote(script)
        # Warm-pipe branch: the script is already fully formed (device-side
        # timeout, su wrapper), so the same bytes run on a persistent shell and
        # every caller above us inherits the saving with no call-site change.
        # Declines cleanly when the accelerator is unavailable.
        served = try_fast_shell(script, timeout=timeout, max_bytes=max_bytes,
                                args=[self.adb_bin, "-s", self.serial or "", "shell", "-T", script])
        if served is not None:
            if served.exit_code in (124, 137):
                served.timed_out = True
                served.error = "Device-side command timeout (or process killed)"
            return served
        r = self.adb_result(["shell", "-T", script], timeout=min(3600, timeout + 3),
                            max_bytes=max_bytes, output_file=output_file)
        if r.exit_code in (124, 137):
            r.timed_out = True
            r.error = "Device-side command timeout (or process killed)"
        return r

    def _binary_shell_result(self, command, root, timeout, max_bytes, output_file):
        token = "__MCP_BINARY_" + uuid.uuid4().hex + "__"
        error_path = "/data/local/tmp/mcp_stderr_" + uuid.uuid4().hex
        qe = shlex.quote(error_path)
        payload = "sh -c " + shlex.quote(command)
        if self._device_timeout_available:
            payload = f"timeout -s KILL {timeout:.3f} " + payload
        wrapper = (
            "umask 077\n" + payload + f" 2>{qe}\n"
            "mcp_rc=$?\n"
            f"printf '\\n{token}:RC:%s\\n' \"$mcp_rc\"\n"
            f"head -c 32768 {qe} 2>/dev/null | base64\n"
            f"printf '\\n{token}:END\\n'\n"
            f"rm -f {qe} 2>/dev/null\n"
        )
        script = ("su -c " if root else "sh -c ") + shlex.quote(wrapper)
        with tempfile.TemporaryDirectory(prefix="mcp_wire_") as temporary:
            wire = Path(temporary) / "stdout"
            r = self.adb_result(["exec-out", script], timeout=min(3600, timeout+3), output_file=str(wire))
            if not wire.exists():
                return r
            size = wire.stat().st_size
            with wire.open("r+b") as stream:
                tail_start = max(0, size-65536)
                stream.seek(tail_start)
                tail = stream.read()
                prefix = ("\n"+token+":RC:").encode()
                footer = ("\n"+token+":END\n").encode()
                position = tail.rfind(prefix)
                if position < 0 or not tail.endswith(footer):
                    r.error = r.error or "Binary transport missing exit-status trailer (device disconnected, su failed or command timed out)"
                    r.exit_code = r.exit_code if r.exit_code else None
                    stream.seek(0)
                    r.stdout = stream.read(min(max_bytes, size))
                    r.truncated = size > max_bytes
                    return r
                metadata = tail[position+len(prefix):-len(footer)]
                code, _, errors = metadata.partition(b"\n")
                try:
                    actual_code = int(code)
                    stderr = base64.b64decode(b"".join(errors.split()), validate=True)
                except (ValueError, base64.binascii.Error):
                    r.error = "Invalid binary transport metadata"
                    r.exit_code = None
                    return r
                payload_size = tail_start + position
                stream.truncate(payload_size)
                if not output_file:
                    stream.seek(0)
                    r.stdout = stream.read(max_bytes)
                    r.truncated = payload_size > max_bytes
            if r.success:
                r.exit_code = actual_code
            r.stderr = (r.stderr + stderr)[:max_bytes]
            if actual_code in (124, 137):
                r.timed_out = True
                r.error = "Device-side binary command timeout (or process killed)"
            if output_file and r.success:
                destination = Path(output_file).expanduser().resolve()
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(wire, destination)
            return r

    def require_shell(self, command, root=False, timeout=20, max_bytes=1_048_576):
        r = self.shell_result(command, root=root, timeout=timeout, max_bytes=max_bytes)
        if not r.success:
            raise DeviceError("Device command failed", "COMMAND_FAILED", result=r.as_dict())
        return r.text

    def run_adb(self, args, timeout=15.0, binary=False):
        r = self.adb_result(args, timeout=timeout, max_bytes=32*1024*1024 if binary else 1_048_576)
        if not r.success:
            if binary:
                raise DeviceError("Binary ADB operation failed", result=r.as_dict())
            return f"[ADB Error code={r.exit_code}] {r.error or r.stderr.decode('utf-8', 'replace') or r.text}"
        return r.stdout if binary else r.text.strip()

    def run_shell(self, shell_cmd, timeout=15):
        return self.require_shell(shell_cmd, timeout=timeout)

    def run_root(self, root_cmd, timeout=20):
        return self.require_shell(root_cmd, root=True, timeout=timeout)

    @property
    def u2(self):
        if self._u2_device is not None:
            return self._u2_device
        if time.monotonic() < self._u2_retry_at:
            return None
        try:
            self.ensure_device()
            import uiautomator2
            d = uiautomator2.connect(self.serial)
            d.settings["wait_timeout"] = 0
            info = d.info
            self._screen_size = (info.get("displayWidth", 1080), info.get("displayHeight", 2400))
            self._u2_device = d
            self._u2_error = ""
            return d
        except Exception as exc:
            self._u2_error = str(exc)
            self._u2_retry_at = time.monotonic() + 15
            return None

    def reset_u2(self):
        self._u2_device = None
        self._u2_retry_at = 0

    def foreground(self):
        r = self.shell_result("dumpsys activity activities | grep -E 'topResumedActivity|mResumedActivity'", timeout=5, max_bytes=16384)
        found = re.search(r"\bu\d+\s+([\w.]+)/([\w.$]+)", r.text)
        if not found:
            r = self.shell_result("dumpsys window windows | grep mCurrentFocus", timeout=5, max_bytes=16384)
            found = re.search(r"\bu\d+\s+([\w.]+)/([\w.$]+)", r.text)
        if not found:
            raise DeviceError("Cannot determine foreground activity", result=r.as_dict())
        pkg, activity = found.groups()
        return {"package": pkg, "activity": pkg + activity if activity.startswith(".") else activity}

    def shell_batch(self, commands, root=False, stop_on_error=True, timeout=30, max_output_bytes=1_048_576):
        if not 1 <= len(commands) <= 200:
            raise ValueError("commands must contain 1..200 entries")
        token = "__MCP_" + uuid.uuid4().hex + "__"
        script = []
        for i, entry in enumerate(commands):
            cmd = entry.command if hasattr(entry, "command") else entry["command"]
            if not cmd or "\0" in cmd:
                raise ValueError("Every command must be nonempty with no NUL")
            script += [f"printf '\\n{token}:{i}:BEGIN\\n'",
                       "( eval " + shlex.quote(cmd) + " ) 2>&1", "mcp_rc=$?",
                       f"printf '\\n{token}:{i}:END:%s\\n' \"$mcp_rc\""]
            if stop_on_error:
                script.append('[ "$mcp_rc" -eq 0 ] || exit "$mcp_rc"')
        r = self.shell_result("\n".join(script), root=root, timeout=timeout, max_bytes=max_output_bytes)
        results = []
        for m in re.finditer(re.escape(token) + r":(\d+):BEGIN\r?\n(.*?)\r?\n" + re.escape(token) + r":\1:END:(\d+)", r.text, re.S):
            index, output, code = m.groups()
            item = commands[int(index)]
            label = item.label if hasattr(item, "label") else item.get("label", "")
            results.append({"index": int(index), "label": label, "exit_code": int(code), "success": int(code) == 0, "output": output})
        self.invalidate_ui()
        return {"success": r.success and len(results) == len(commands) and all(x["success"] for x in results),
                "results": results, "executed": len(results), "requested": len(commands),
                "exit_code": r.exit_code, "timed_out": r.timed_out, "truncated": r.truncated,
                "elapsed_ms": r.elapsed_ms, "stderr": r.stderr.decode("utf-8", "replace"),
                **({"error": r.error} if r.error else {})}

    def doctor(self, initialize_u2=False):
        self.ensure_device()
        commands = [
            {"command": "getprop ro.product.model; getprop ro.build.version.release; getprop ro.build.version.sdk; getprop ro.product.cpu.abi", "label": "android"},
            {"command": "id", "label": "adb_identity"},
            {"command": "for c in timeout tar base64 setsid screenrecord perfetto sqlite3 restorecon; do if p=$(command -v \"$c\"); then printf '%s=%s\\n' \"$c\" \"$p\"; else printf '%s=unavailable\\n' \"$c\"; fi; done", "label": "binaries"},
        ]
        basic = self.shell_batch(commands, stop_on_error=False)
        root = self.shell_result("id", root=True, timeout=8).as_dict()
        root["uid_zero"] = bool(re.search(r"\buid=0\b", root["stdout"]))
        versions = {}
        for pkg in ("mcp", "uiautomator2", "Pillow", "pydantic", "websocket-client"):
            try:
                versions[pkg] = importlib.metadata.version(pkg)
            except importlib.metadata.PackageNotFoundError:
                versions[pkg] = None
        if initialize_u2:
            self.reset_u2()
            u2_ready = self.u2 is not None
        else:
            u2_ready = self._u2_device is not None
        return {"success": basic["success"], "serial": self.serial, "adb_path": self.adb_bin,
                "adb_version": self.adb_result(["version"], host=True).text, "probes": basic["results"],
                "root": root, "packages": versions, "u2_connected": u2_ready, "u2_error": self._u2_error,
                "device_timeout_supported": self._device_timeout_available,
                "notes": ["Root does not bypass TEE/StrongBox, encrypted locked-user storage, DRM, or every ROM/SELinux restriction.",
                          "UI hierarchy does not expose arbitrary Canvas/game pixels. Use WebView DOM or screenshots when needed."]}

    def saved_workflow(self, action="list", name="", workflow=None):
        folder = self.state_dir / "workflows"
        if action == "list":
            return {"success": True, "names": sorted(p.stem for p in folder.glob("*.json")) if folder.exists() else []}
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
            raise ValueError("name must contain 1..80 letters, digits, '_' or '-'")
        path = folder / (name + ".json")
        if action == "save":
            if workflow is None:
                raise ValueError("save requires workflow")
            from .models import Workflow
            model = workflow if isinstance(workflow, Workflow) else Workflow.model_validate(workflow)
            folder.mkdir(parents=True, exist_ok=True)
            tmp = folder / (name + "." + uuid.uuid4().hex + ".tmp")
            tmp.write_text(model.model_dump_json(indent=2), encoding="utf-8")
            os.replace(tmp, path)
            return {"success": True, "name": name, "path": str(path)}
        if action == "delete":
            path.unlink()
            return {"success": True, "deleted": name}
        if action not in ("load", "run"):
            raise ValueError("action must be list/save/load/run/delete")
        from .models import Workflow
        model = Workflow.model_validate_json(path.read_text(encoding="utf-8"))
        return self.run_workflow(model) if action == "run" else {"success": True, "name": name, "workflow": model.model_dump(exclude_none=True)}

    def read_smart_logcat(self, package="", tag="", grep_keyword="", level="I", lines=120,
                          crash_only=False, clear_first=False, since=""):
        from .data_ops import package_name
        if not 1 <= lines <= 10000 or level not in "VDIWEFS" or len(level) != 1:
            raise ValueError("invalid logcat lines/level")
        if package:
            package_name(package)
        if clear_first:
            return {**self.shell_result("logcat -c").as_dict(), "action": "cleared_logcat"}
        args = ["logcat", "-d", "-v", "threadtime", "-t", str(min(lines*5, 50000))]
        if crash_only:
            args += ["-b", "crash"]
        if since:
            args += ["-T", since]
        if tag:
            args += ["-s", f"{tag}:{level}"]
        else:
            args += [f"*:{level}"]
        r = self.shell_result(" ".join(shlex.quote(x) for x in args), timeout=15, max_bytes=2*1024*1024)
        pids = set()
        if package and not crash_only:
            p = self.shell_result("pidof " + shlex.quote(package), timeout=5)
            pids = {s for s in p.text.split() if s.isdigit()}
        selected = []
        for line in r.text.splitlines():
            if line.startswith("--------- beginning of"):
                continue
            if package and not crash_only:
                m = re.match(r"\d\d-\d\d\s+[\d:.]+\s+(\d+)\s+", line)
                if (pids and (not m or m[1] not in pids)) or (not pids and package.casefold() not in line.casefold()):
                    continue
            if grep_keyword and grep_keyword.casefold() not in line.casefold():
                continue
            selected.append(line)
        metadata = r.as_dict()
        metadata.pop("stdout", None)
        return {**metadata, "logs": "\n".join(selected[-lines:]), "matched_lines": min(len(selected), lines),
                "package": package, "resolved_pids": sorted(pids), "buffer": "crash" if crash_only else "default"}

    def inspect_lsposed_and_ksu(self, keyword="", lines=120, include_verbose=False):
        if not 1 <= lines <= 10000:
            raise ValueError("lines must be 1..10000")
        pattern = "verbose_*.log" if include_verbose else "modules_*.log"
        cmd = (f"LATEST=$(ls -1t /data/adb/lspd/log/{pattern} 2>/dev/null | head -n 1); "
               "printf 'LOG_FILE=%s\\n' \"$LATEST\"; "
               f"if [ -n \"$LATEST\" ]; then tail -n {min(lines*4, 20000)} \"$LATEST\"; fi")
        r = self.shell_result(cmd, root=True, max_bytes=2*1024*1024)
        raw = r.text.splitlines()
        path = raw[0].removeprefix("LOG_FILE=") if raw else ""
        logs = [line for line in raw[1:] if not keyword or keyword.casefold() in line.casefold()][-lines:]
        modules = self.shell_result(
            'if [ -d /data/adb/modules ]; then for d in /data/adb/modules/*; do '
            '[ -d "$d" ] || continue; echo "=== $d ==="; '
            'if [ -f "$d/disable" ]; then echo DISABLED=true; fi; '
            'cat "$d/module.prop" 2>/dev/null || true; done; fi', root=True, max_bytes=131072)
        return {"success": r.success and modules.success, "lspd_available": bool(path), "lspd_log_file": path,
                "lspd_logs": "\n".join(logs), "matched_lines": len(logs), "installed_root_modules": modules.text,
                "truncated": r.truncated or modules.truncated}

    def deploy_and_verify_apk(self, apk_path, target_package, launch_activity="", wait_seconds=3.5, log_keyword=""):
        from .automation import compact_ui
        from .data_ops import package_name
        from .device_ops import component_name
        from .models import Condition
        package_name(target_package)
        if launch_activity:
            component_name(target_package, launch_activity)
        if not 0 <= wait_seconds <= 300 or not Path(apk_path).is_file():
            raise ValueError("APK must be a local file and wait_seconds must be 0..300")
        t0 = time.monotonic()
        installed = self.app_action("install", apk_paths=[apk_path], allow_downgrade=True)
        if not installed["success"]:
            return {"success": False, "stage": "install", "result": installed}
        since = self.require_shell("date '+%m-%d %H:%M:%S.000'").strip()
        self.require_shell("am force-stop " + shlex.quote(target_package), root=True)
        launched = self.launch_app(target_package, launch_activity, timeout=max(1, wait_seconds))
        if not launched["success"]:
            return {"success": False, "stage": "launch", "result": launched}
        ready = self.wait_condition(Condition(kind="foreground", package=target_package), timeout=wait_seconds)
        ui = self.dump_ui_elements(max_elements=100)
        crashes = self.read_smart_logcat(crash_only=True, lines=100, since=since)
        app = self.read_smart_logcat(package=target_package, grep_keyword=log_keyword, lines=100, since=since)
        lspd = self.inspect_lsposed_and_ksu(keyword=log_keyword or target_package, lines=100)
        target_crashed = bool(crashes.get("logs") and target_package in crashes["logs"])
        return {"success": ready["success"] and ui["success"] and not target_crashed,
                "elapsed_sec": round(time.monotonic()-t0, 2), "installed": installed,
                "launched": launched, "ready": ready, "has_crash": target_crashed,
                "crash_logs": crashes.get("logs", ""), "app_logs": app.get("logs", ""),
                "lsposed_logs": lspd.get("lspd_logs", ""), "post_ui": compact_ui(ui),
                "note": "Global logcat buffers were preserved. Verification covers the observed interval only."}

    def close(self):
        # Background jobs intentionally survive the server; only mappings owned
        # by this process are removed. Never remove another debugger's forwards.
        for (serial, _), port in list(self._owned_forwards.items()):
            execute([self.adb_bin, "-s", serial, "forward", "--remove", f"tcp:{port}"], timeout=5)
        self._owned_forwards.clear()
        reset_fast_transport()
        try:
            from . import native_input as _native
            _native.reset()
        except Exception:
            pass

    def get_device_status(self):
        self.ensure_device()
        info = self.u2.info if self.u2 is not None else {}
        fields = self.shell_batch([
            {"command": "getprop ro.product.model", "label": "model"},
            {"command": "getprop ro.build.version.release", "label": "android"},
            {"command": "getprop ro.build.version.sdk", "label": "sdk"},
            {"command": "wm size", "label": "screen"},
        ])
        root = self.shell_result("id", root=True, timeout=8)
        current = self.foreground()
        return {"success": fields["success"], "serial": self.serial, "adb_path": self.adb_bin,
                "u2_connected": bool(info), "screen_on": info.get("screenOn"),
                "display_size": list(self._screen_size), "current_package": current.get("package"),
                "current_activity": current.get("activity"), "root_available": root.success and "uid=0(" in root.text,
                "root_identity": root.text, "root_error": root.error or root.stderr.decode("utf-8", "replace"),
                **{r["label"]: r["output"].strip() for r in fields["results"]}}
