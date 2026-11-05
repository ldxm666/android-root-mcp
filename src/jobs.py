"""Device-owned background tasks with persistent handles and bounded logs."""

from __future__ import annotations

import base64
import json
import re
import shlex
import time
import uuid


class JobsMixin:
    def init_jobs(self):
        self._job_guard = __import__("threading").Lock()

    def _job_path(self, job_id):
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("invalid job_id")
        return self.state_dir / "jobs" / (job_id + ".json")

    def _load_job(self, job_id):
        self.ensure_device()
        meta = json.loads(self._job_path(job_id).read_text(encoding="utf-8"))
        if meta["serial"] != self.serial:
            raise ValueError("job belongs to a different device; select that device first")
        return meta

    def start_job(self, command, root=False, timeout=300, max_output_mb=16, label=""):
        if not command or "\0" in command:
            raise ValueError("command must be nonempty and contain no NUL")
        if not 0 <= timeout <= 86400 or not 1 <= max_output_mb <= 256:
            raise ValueError("timeout must be 0..86400s and max_output_mb 1..256")
        self.ensure_device()
        job_id = uuid.uuid4().hex
        base = "/data/local/tmp/android-root-mcp-jobs-" + ("root" if root else "shell")
        directory = base + "/" + job_id
        q = shlex.quote(directory)
        qb = shlex.quote(base)
        self.require_shell(f"command -v setsid >/dev/null && [ ! -L {qb} ] && mkdir -p {qb} && "
                           f"[ \"$(stat -c %u {qb})\" = \"$(id -u)\" ] && chmod 700 {qb} && mkdir -m 700 {q}", root=root)
        actual = "sh -c " + shlex.quote(command)
        if timeout:
            if not self._device_timeout_available:
                raise ValueError("device timeout command required for bounded jobs (or explicitly use timeout=0)")
            actual = f"timeout -s KILL {timeout:g} " + actual
        wrapper = (
            f"cd {q} || exit 1\n"
            "echo $$ > pid\n"
            "awk '{print $22}' /proc/$$/stat > start_ticks\n"
            f"(ulimit -f {max_output_mb*2048}; {actual}) >stdout 2>stderr\n"
            "mcp_rc=$?\n"
            "printf '%s' \"$mcp_rc\" > exit_code.tmp\n"
            "mv exit_code.tmp exit_code\n"
        )
        launch = f"setsid sh -c {shlex.quote(wrapper)} </dev/null >/dev/null 2>&1 & echo $!"
        r = self.shell_result(launch, root=root, timeout=5)
        if not r.success or not re.fullmatch(r"\d+", r.text.strip()):
            return {**r.as_dict(), "error": "Cannot launch detached job"}
        meta = {"job_id": job_id, "serial": self.serial, "root": root, "directory": directory,
                "launched_pid": int(r.text.strip()), "command": command, "label": label,
                "created_at": time.time(), "timeout": timeout, "max_output_mb": max_output_mb}
        path = self._job_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"success": True, **meta, "note": "Use job_id to poll/output/cancel; jobs survive MCP restarts. No automatic restart after device reboot."}

    def job_status(self, job_id):
        meta = self._load_job(job_id)
        q = shlex.quote(meta["directory"])
        command = (
            f"cd {q} || exit $?; "
            "if [ -f exit_code ]; then printf 'EXIT='; cat exit_code; echo; fi; "
            "if [ -f pid ] && [ -f start_ticks ]; then "
            "p=$(cat pid); t=$(cat start_ticks); echo PID=$p; echo START=$t; "
            "if [ -r /proc/$p/stat ]; then "
            "echo NOW=$(awk '{print $22}' /proc/$p/stat); "
            "echo GROUP=$(awk '{print $5}' /proc/$p/stat); "
            "echo STATE=$(awk '{print $3}' /proc/$p/stat); fi; fi; "
            "for f in stdout stderr; do if [ -f \"$f\" ]; then echo SIZE_$f=$(stat -c %s \"$f\"); fi; done"
        )
        r = self.shell_result(command, root=meta["root"], timeout=5)
        if not r.success:
            return {**r.as_dict(), "job_id": job_id, "state": "unavailable"}
        fields = dict(line.split("=", 1) for line in r.text.splitlines() if "=" in line)
        identity_matches = fields.get("START") and fields.get("START") == fields.get("NOW")
        live = bool(identity_matches and fields.get("STATE") not in ("Z", "X"))
        code = int(fields["EXIT"]) if fields.get("EXIT", "").lstrip("-").isdigit() else None
        state = "finished" if code is not None else "running" if live else "starting" if time.time()-meta["created_at"] < 3 else "interrupted"
        return {"success": True, "job_id": job_id, "serial": meta["serial"], "state": state,
                "exit_code": code, "command_success": code == 0 if code is not None else None,
                "pid": int(fields["PID"]) if fields.get("PID", "").isdigit() else None,
                "identity_matches": bool(identity_matches), "process_group": fields.get("GROUP"),
                "stdout_bytes": int(fields.get("SIZE_stdout", 0)), "stderr_bytes": int(fields.get("SIZE_stderr", 0)),
                "label": meta.get("label", ""), "directory": meta["directory"]}

    def device_job(self, action="list", job_id="", command="", root=False, timeout=300,
                   max_output_mb=16, label="", stream="stdout", offset=0, limit=32768, encoding="utf-8"):
        if action == "start":
            return self.start_job(command, root, timeout, max_output_mb, label)
        if action == "list":
            self.ensure_device()
            paths = (self.state_dir / "jobs").glob("*.json")
            entries = []
            for path in sorted(paths):
                try:
                    meta = json.loads(path.read_text(encoding="utf-8"))
                    if meta["serial"] == self.serial:
                        entries.append({k: meta[k] for k in ("job_id", "serial", "label", "created_at", "directory")})
                except (ValueError, KeyError, OSError):
                    continue
            return {"success": True, "jobs": entries}
        meta = self._load_job(job_id)
        if action == "status":
            return self.job_status(job_id)
        if action == "output":
            if stream not in ("stdout", "stderr") or not 0 <= offset <= 2**40 or not 1 <= limit <= 1024*1024:
                raise ValueError("invalid output stream/offset/limit")
            if encoding not in ("utf-8", "base64"):
                raise ValueError("encoding must be utf-8/base64")
            path = shlex.quote(meta["directory"] + "/" + stream)
            r = self.shell_result(f"test -f {path} || exit $?; tail -c +{offset+1} {path} | head -c {limit}", root=meta["root"], max_bytes=limit, binary=True)
            content = base64.b64encode(r.stdout).decode("ascii") if encoding == "base64" else r.stdout.decode("utf-8", "replace")
            metadata = r.as_dict()
            metadata.pop("stdout", None)
            return {**metadata, "content": content, "encoding": encoding, "offset": offset,
                    "next_offset": offset + len(r.stdout), "bytes": len(r.stdout), "job_id": job_id}
        if action == "cancel":
            status = self.job_status(job_id)
            if status.get("state") in ("finished", "interrupted"):
                return {"success": True, "changed": False, "status": status}
            if not status.get("identity_matches") or not status.get("pid"):
                return {"success": False, "error": "Cannot verify job PID/start-time; refusing to signal an unrelated process", "status": status}
            pid = status["pid"]
            # Recheck identity at the point of signaling, preventing PID-reuse accidents.
            q = shlex.quote(meta["directory"])
            target = f"-{pid}" if str(pid) == status.get("process_group") else str(pid)
            cmd = (f"cd {q} && [ \"$(awk '{{print $22}}' /proc/{pid}/stat)\" = \"$(cat start_ticks)\" ] && "
                   f"kill -TERM -- {target}")
            return {**self.shell_result(cmd, root=meta["root"], timeout=5).as_dict(), "job_id": job_id, "signal": "TERM"}
        if action == "cleanup":
            status = self.job_status(job_id)
            if status.get("state") not in ("finished", "interrupted"):
                raise ValueError("cancel and wait for job termination before cleanup")
            r = self.shell_result("rm -rf " + shlex.quote(meta["directory"]), root=meta["root"])
            if r.success:
                self._job_path(job_id).unlink()
            return {**r.as_dict(), "job_id": job_id}
        raise ValueError("action must be list/start/status/output/cancel/cleanup")

    def record_screen(self, action="start", job_id="", seconds=30, bit_rate=4_000_000, local_path=""):
        if action == "start":
            if not 1 <= seconds <= 180 or not 100000 <= bit_rate <= 100_000_000:
                raise ValueError("seconds must be 1..180; bit_rate 100000..100000000")
            import math
            file_limit = max(16, math.ceil(seconds*bit_rate/8/1048576*1.2+4))
            if file_limit > 256:
                raise ValueError("requested recording exceeds the 256 MiB per-file job limit; reduce time/bit_rate")
            result = self.start_job(f"screenrecord --time-limit {seconds} --bit-rate {bit_rate} record.mp4",
                                    root=True, timeout=seconds+10, max_output_mb=file_limit, label="screenrecord")
            if result["success"]:
                result["device_path"] = result["directory"] + "/record.mp4"
            return result
        if action == "status":
            return self.job_status(job_id)
        if action == "stop":
            # screenrecord needs SIGINT to finalize the MP4; TERM can corrupt it.
            meta = self._load_job(job_id)
            if meta.get("label") != "screenrecord":
                raise ValueError("job is not a screenrecord task")
            status = self.job_status(job_id)
            if status.get("state") == "finished":
                return {"success": True, "changed": False, "status": status}
            if not status.get("identity_matches") or str(status.get("pid")) != status.get("process_group"):
                return {"success": False, "error": "Cannot verify screenrecord process group"}
            pid = status["pid"]
            q = shlex.quote(meta["directory"])
            cmd = (f"cd {q} && [ \"$(awk '{{print $22}}' /proc/{pid}/stat)\" = \"$(cat start_ticks)\" ] && "
                   f"kill -INT -- -{pid}")
            return self.shell_result(cmd, root=meta["root"]).as_dict()
        if action == "pull":
            if not local_path:
                raise ValueError("pull requires local_path")
            meta = self._load_job(job_id)
            if meta.get("label") != "screenrecord":
                raise ValueError("job is not a screenrecord task")
            if self.job_status(job_id).get("state") not in ("finished", "interrupted"):
                raise ValueError("stop or wait for screenrecord to finish before pulling")
            return self.transfer("pull", meta["directory"] + "/record.mp4", local_path)
        raise ValueError("action must be start/status/stop/pull")
