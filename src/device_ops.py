"""Typed Android app, system, provider, networking and diagnostics operations."""

from __future__ import annotations

import json
import re
import shlex
import time
import uuid
from pathlib import Path

from .data_ops import package_name


def checked_am_result(result):
    data = result.as_dict()
    # Android's am/content commands sometimes print a Java exception and exit 0.
    if re.search(r"(?:^|\n)(?:Error\b|Exception|java\.[\w.]*Exception|SecurityException|Failure \[)", data["stdout"]):
        data["success"] = False
        data["error"] = "Android reported an operation error in stdout"
    return data


def component_name(package, activity):
    if "/" in activity:
        if not re.fullmatch(r"[\w.]+/[\w.$]+", activity) or activity.split("/", 1)[0] != package:
            raise ValueError("component must be package/activity for the specified package")
        return activity
    if not re.fullmatch(r"[\w.$]+", activity):
        raise ValueError("invalid Activity/component name")
    return f"{package}/{activity}"


class DeviceOpsMixin:
    def launch_app(self, package, activity="", timeout=15, user_id=0):
        package = package_name(package)
        if not 0 <= user_id <= 10000:
            raise ValueError("invalid user_id")
        if activity:
            component = component_name(package, activity)
        else:
            r = self.shell_result(f"cmd package resolve-activity --brief --user {user_id} -c android.intent.category.LAUNCHER " + shlex.quote(package), timeout=min(10, timeout))
            candidates = re.findall(r"^([\w.]+/[\w.$]+)$", r.text, re.M)
            if not r.success or not candidates:
                return {"success": False, "error": "No launcher Activity found; specify activity explicitly", "probe": r.as_dict()}
            component = candidates[-1]
        self.invalidate_ui()
        return {**checked_am_result(self.shell_result(f"am start -W --user {user_id} -n " + shlex.quote(component), root=True, timeout=timeout)), "component": component}

    def send_intent(self, kind="activity", package="", activity="", action="", data_uri="", mime_type="",
                    extras=None, categories=None, flags=None, root=True, user_id=0, wait=True):
        kinds = {"activity": "start", "broadcast": "broadcast", "service": "startservice",
                 "foreground_service": "start-foreground-service", "stop_service": "stopservice"}
        if kind not in kinds or not 0 <= user_id <= 10000:
            raise ValueError("invalid intent kind/user_id")
        if package:
            package_name(package)
        if activity and not package:
            raise ValueError("activity requires package")
        if not (package or action or data_uri):
            raise ValueError("intent needs package, action or data_uri")
        parts = ["am", kinds[kind], "--user", str(user_id)]
        if kind == "activity" and wait:
            parts.append("-W")
        if activity:
            parts += ["-n", component_name(package, activity)]
        elif package:
            parts += ["-p", package]
        if action:
            parts += ["-a", action]
        if data_uri:
            parts += ["-d", data_uri]
        if mime_type:
            parts += ["-t", mime_type]
        for category in categories or []:
            parts += ["-c", category]
        if flags is not None:
            if isinstance(flags, bool) or not 0 <= flags <= 0xFFFFFFFF:
                raise ValueError("flags must be a nonnegative 32-bit integer")
            parts += ["-f", str(flags)]
        for key, value in (extras or {}).items():
            if not isinstance(key, str):
                raise ValueError("Intent extra keys must be strings")
            if value is None:
                parts += ["--esn", key]
            elif isinstance(value, bool):
                parts += ["--ez", key, str(value).lower()]
            elif isinstance(value, int):
                if not -(2**63) <= value < 2**63:
                    raise ValueError("Intent integer extra exceeds signed 64-bit range")
                parts += ["--ei" if -(2**31) <= value < 2**31 else "--el", key, str(value)]
            elif isinstance(value, float):
                import math
                if not math.isfinite(value):
                    raise ValueError("extra floats must be finite")
                parts += ["--ef", key, str(value)]
            elif isinstance(value, str):
                parts += ["--es", key, value]
            else:
                raise ValueError("Intent extras support string, boolean, integer, float or null")
        command = " ".join(shlex.quote(v) for v in parts)
        self.invalidate_ui()
        return {**checked_am_result(self.shell_result(command, root=root, timeout=30)), "command": command}

    def app_action(self, action="list", package="", apk_paths=None, activity="", include_system=False,
                   user_id=0, grant_permissions=False, allow_downgrade=False, allow_test=False):
        if not 0 <= user_id <= 10000:
            raise ValueError("invalid user_id")
        if action == "list":
            cmd = f"pm list packages {'-f' if include_system else '-3 -f'} --user {user_id}"
            return self.shell_result(cmd).as_dict()
        if action in ("install", "install_multiple"):
            paths = [str(Path(p).expanduser().resolve(strict=True)) for p in apk_paths or []]
            if not paths or (action == "install" and len(paths) != 1):
                raise ValueError("install needs one APK; install_multiple needs all base/split APK paths")
            if any(not Path(p).is_file() for p in paths):
                raise ValueError("every APK must be a regular local file")
            args = ["install" if action == "install" else "install-multiple", "-r", "--user", str(user_id)]
            if grant_permissions:
                args.append("-g")
            if allow_downgrade:
                args.append("-d")
            if allow_test:
                args.append("-t")
            r = self.adb_result(args + paths, timeout=180).as_dict()
            r["success"] = r["success"] and "Success" in r["stdout"]
            return r
        package = package_name(package)
        q = shlex.quote(package)
        if action == "launch":
            return self.launch_app(package, activity, user_id=user_id)
        if action == "restart":
            self.require_shell(f"am force-stop --user {user_id} {q}", root=True)
            return self.launch_app(package, activity, user_id=user_id)
        commands = {
            "info": f"dumpsys package {q}", "paths": f"pm path --user {user_id} {q}",
            "stop": f"am force-stop --user {user_id} {q}",
            "clear": f"pm clear --user {user_id} {q}",
            "enable": f"pm enable --user {user_id} {q}",
            "disable": f"pm disable-user --user {user_id} {q}",
            "uninstall": f"pm uninstall --user {user_id} {q}",
        }
        if action not in commands:
            raise ValueError("unknown app action")
        self.invalidate_ui()
        result = checked_am_result(self.shell_result(commands[action], root=True, timeout=60))
        if action in ("clear", "uninstall"):
            result["success"] = result["success"] and "Success" in result["stdout"]
        return {**result, "package": package, "action": action}

    def export_apks(self, package, output_dir):
        package = package_name(package)
        paths = self.require_shell("pm path " + shlex.quote(package)).splitlines()
        apk_paths = [line.split(":", 1)[1] for line in paths if line.startswith("package:")]
        if not apk_paths:
            return {"success": False, "error": "Package not installed/no APK paths found"}
        output = Path(output_dir).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        results = []
        for i, path in enumerate(apk_paths):
            name = ("base.apk" if i == 0 else f"split_{i}_{Path(path).name}")
            results.append(self.transfer("pull", path, str(output / name)))
        return {"success": all(r["success"] for r in results), "package": package, "files": results}

    def permissions(self, package, action="list", permission="", mode="allow", user_id=0):
        package = package_name(package)
        if not 0 <= user_id <= 10000:
            raise ValueError("invalid user_id")
        q = shlex.quote(package)
        if action == "list":
            return self.shell_batch([
                {"label": "permissions", "command": f"dumpsys package {q}"},
                {"label": "appops", "command": f"cmd appops get --user {user_id} {q}"},
            ], root=True, stop_on_error=False)
        if action == "grant_runtime_all":
            raw = self.require_shell(f"dumpsys package {q}")
            runtime = self.require_shell("pm list permissions -d")
            dangerous = set(re.findall(r"permission:([\w.]+)", runtime))
            declared = set(re.findall(r"\b(android\.permission\.[A-Z_0-9]+)", raw))
            commands = [{"label": p, "command": f"pm grant --user {user_id} {q} {shlex.quote(p)}"} for p in sorted(dangerous & declared)]
            if not commands:
                return {"success": True, "granted": [], "note": "No declared dangerous runtime permissions found"}
            return self.shell_batch(commands, root=True, stop_on_error=False, timeout=60)
        if not re.fullmatch(r"[\w.]+", permission):
            raise ValueError("permission/op name required")
        if action in ("grant", "revoke"):
            cmd = f"pm {action} --user {user_id} {q} {shlex.quote(permission)}"
        elif action == "appop":
            if mode not in ("allow", "ignore", "deny", "default", "foreground"):
                raise ValueError("invalid appop mode")
            cmd = f"cmd appops set --user {user_id} {q} {shlex.quote(permission)} {mode}"
        else:
            raise ValueError("action must be list/grant/revoke/grant_runtime_all/appop")
        return checked_am_result(self.shell_result(cmd, root=True))

    def settings(self, action="get", namespace="global", key="", value="", user_id=0):
        if namespace not in ("global", "system", "secure") or action not in ("get", "put", "delete", "list"):
            raise ValueError("invalid settings namespace/action")
        if not 0 <= user_id <= 10000:
            raise ValueError("invalid user_id")
        if action != "list" and not re.fullmatch(r"[\w.:-]+", key):
            raise ValueError("invalid settings key")
        args = ["settings", "--user", str(user_id), action, namespace]
        if action != "list":
            args.append(key)
        if action == "put":
            args.append(str(value))
        return self.shell_result(" ".join(shlex.quote(x) for x in args), root=action in ("put", "delete")).as_dict()

    def agent_profile(self, action="apply", profile_id="", zero_animation=True, stay_awake=True):
        self.ensure_device()
        folder = self.state_dir / "profiles"
        if action == "list":
            profiles = []
            if folder.exists():
                for path in folder.glob("*.json"):
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                        if data["serial"] == self.serial:
                            profiles.append({"profile_id": path.stem, "restored": data.get("restored", False), "created_at": data["created_at"]})
                    except (ValueError, KeyError, OSError):
                        continue
            return {"success": True, "profiles": profiles}
        if action == "apply":
            wanted = {}
            if zero_animation:
                wanted.update({k: "0" for k in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale")})
            if stay_awake:
                wanted["stay_on_while_plugged_in"] = "3"
            if not wanted:
                return {"success": True, "changed": False}
            original = {key: self.require_shell("settings get global " + key).strip() for key in wanted}
            profile_id = uuid.uuid4().hex
            data = {"serial": self.serial, "created_at": time.time(), "before": original, "applied": wanted, "restored": False}
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (profile_id + ".json")
            # Save the original values BEFORE any mutation, including partial failures.
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            result = self.shell_batch([{"command": f"settings put global {k} {v}", "label": k} for k, v in wanted.items()], root=True)
            return {**result, "profile_id": profile_id, "before": original, "applied": wanted}
        if action != "restore" or not re.fullmatch(r"[0-9a-f]{32}", profile_id):
            raise ValueError("restore needs a valid profile_id")
        path = folder / (profile_id + ".json")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["serial"] != self.serial:
            raise ValueError("profile belongs to a different device")
        if data.get("restored"):
            return {"success": True, "changed": False, "profile_id": profile_id}
        commands = [{"label": k, "command": f"settings delete global {shlex.quote(k)}" if v == "null" else f"settings put global {shlex.quote(k)} {shlex.quote(v)}"} for k, v in data["before"].items()]
        result = self.shell_batch(commands, root=True, stop_on_error=False)
        if result["success"]:
            data["restored"] = True
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return {**result, "profile_id": profile_id, "restored_values": data["before"]}

    def device_control(self, action, value="", root=True):
        boolean = {"on": True, "off": False, "true": True, "false": False, "1": True, "0": False}
        if action in ("wifi", "bluetooth", "mobile_data", "airplane_mode"):
            enabled = boolean.get(str(value).lower())
            if enabled is None:
                raise ValueError("value must be on/off")
            commands = {
                "wifi": f"svc wifi {'enable' if enabled else 'disable'}",
                "bluetooth": f"cmd bluetooth_manager {'enable' if enabled else 'disable'}",
                "mobile_data": f"svc data {'enable' if enabled else 'disable'}",
                "airplane_mode": f"cmd connectivity airplane-mode {'enable' if enabled else 'disable'}",
            }
            cmd = commands[action]
        elif action == "brightness":
            brightness = int(value)
            if not 0 <= brightness <= 255:
                raise ValueError("brightness must be in 0..255 (ROM may scale it differently)")
            cmd = f"settings put system screen_brightness_mode 0 && settings put system screen_brightness {brightness}"
        elif action == "rotation":
            if value == "auto":
                cmd = "settings put system accelerometer_rotation 1"
            elif str(value) in ("0", "1", "2", "3"):
                cmd = "settings put system accelerometer_rotation 0 && settings put system user_rotation " + str(value)
            else:
                raise ValueError("rotation must be auto/0/1/2/3")
        elif action == "screen_size":
            if value != "reset" and not re.fullmatch(r"[1-9]\d{1,4}x[1-9]\d{1,4}", value):
                raise ValueError("screen size must be WIDTHxHEIGHT or reset")
            cmd = "wm size " + value
        elif action == "density":
            if value != "reset" and not (str(value).isdigit() and 72 <= int(value) <= 1000):
                raise ValueError("density must be 72..1000 or reset")
            cmd = "wm density " + str(value)
        elif action == "volume":
            level = int(value)
            if not 0 <= level <= 150:
                raise ValueError("volume must be a nonnegative stream index (device limits apply)")
            cmd = f"cmd media_session volume --stream 3 --set {level}"
        else:
            commands = {
                "wake": "input keyevent KEYCODE_WAKEUP", "sleep": "input keyevent KEYCODE_SLEEP",
                "dismiss_keyguard": "wm dismiss-keyguard", "notifications": "cmd statusbar expand-notifications",
                "quick_settings": "cmd statusbar expand-settings", "collapse_panels": "cmd statusbar collapse",
                "reboot": "reboot", "recovery": "reboot recovery", "bootloader": "reboot bootloader",
                "shutdown": "reboot -p",
            }
            if action not in commands:
                raise ValueError("unknown device control action")
            cmd = commands[action]
        self.invalidate_ui()
        return {**self.shell_result(cmd, root=root).as_dict(), "action": action,
                **({"note": "dismiss-keyguard cannot bypass PIN/password/biometric authentication"} if action == "dismiss_keyguard" else {})}

    def system_property(self, action="get", name="", value="", resetprop=False):
        if action not in ("get", "set") or (name and not re.fullmatch(r"[\w.-]+", name)):
            raise ValueError("invalid property action/name")
        if action == "get":
            return self.shell_result("getprop " + shlex.quote(name) if name else "getprop").as_dict()
        if not name:
            raise ValueError("set requires name")
        cmd = f"{'resetprop' if resetprop else 'setprop'} {shlex.quote(name)} {shlex.quote(value)}"
        return self.shell_result(cmd, root=True).as_dict()

    def provider(self, action="query", uri="", projection=None, where="", sort="", values=None,
                 method="", arg="", user_id=0, root=True, max_output_bytes=65536):
        if action not in ("query", "insert", "update", "delete", "call", "gettype"):
            raise ValueError("invalid provider action")
        if not uri.startswith("content://") or "\0" in uri or not 0 <= user_id <= 10000:
            raise ValueError("requires content:// URI and valid user_id")
        if action in ("update", "delete") and not where:
            raise ValueError("update/delete require an explicit where clause; use '1' intentionally for all rows")
        parts = ["content", action, "--user", str(user_id), "--uri", uri]
        if projection:
            parts += ["--projection", ":".join(projection)]
        if where:
            parts += ["--where", where]
        if sort:
            parts += ["--sort", sort]
        for key, value in (values or {}).items():
            if not re.fullmatch(r"[\w.]+", key):
                raise ValueError("invalid provider bind name")
            if value is None:
                binding = f"{key}:n:"
            elif isinstance(value, bool):
                binding = f"{key}:b:{str(value).lower()}"
            elif isinstance(value, int):
                binding = f"{key}:l:{value}"
            elif isinstance(value, float):
                import math
                if not math.isfinite(value):
                    raise ValueError("provider floats must be finite")
                binding = f"{key}:d:{value}"
            elif isinstance(value, str):
                binding = f"{key}:s:{value}"
            else:
                raise ValueError("provider values must be scalar")
            parts += ["--extra" if action == "call" else "--bind", binding]
        if action == "call":
            if not method:
                raise ValueError("call needs method")
            parts += ["--method", method]
            if arg:
                parts += ["--arg", arg]
        return checked_am_result(self.shell_result(" ".join(shlex.quote(p) for p in parts), root=root, max_bytes=max_output_bytes))

    def port_forward(self, action="list", local="", remote="", reverse=False):
        kind = "reverse" if reverse else "forward"
        if action == "list":
            return self.adb_result([kind, "--list"]).as_dict()
        if action not in ("add", "remove"):
            raise ValueError("action must be list/add/remove")
        def validate(spec):
            if not re.fullmatch(r"(?:tcp:\d{1,5}|localabstract:[A-Za-z0-9_.:/-]+|localfilesystem:/[^\x00\r\n]+|jdwp:\d+)", spec):
                raise ValueError("invalid ADB endpoint")
            if spec.startswith("tcp:") and int(spec[4:]) > 65535:
                raise ValueError("TCP port out of range")
        validate(local)
        if action == "add":
            validate(remote)
            args = [kind, "--no-rebind", local, remote]
        else:
            args = [kind, "--remove", local]
        return self.adb_result(args).as_dict()

    def diagnostics(self, kind="summary", package="", output_dir=""):
        if package:
            package_name(package)
        pkg = shlex.quote(package)
        commands = {
            "summary": "getprop ro.product.model; getprop ro.build.version.release; uptime; df -h /data; dumpsys battery; dumpsys thermalservice",
            "memory": "dumpsys meminfo " + pkg,
            "cpu": "top -b -n 1 | head -n 80",
            "graphics": "dumpsys gfxinfo " + pkg,
            "network": "ip addr; ip route; dumpsys connectivity",
            "crashes": "logcat -b crash -d -t 120",
            "activity": "dumpsys activity activities",
            "services": "dumpsys activity services " + pkg,
            "power": "dumpsys power; dumpsys battery; dumpsys thermalservice",
        }
        if kind == "bundle":
            chosen = commands
        elif kind in commands:
            chosen = {kind: commands[kind]}
        else:
            raise ValueError("invalid diagnostic kind")
        result = self.shell_batch([{"label": k, "command": v} for k, v in chosen.items()], root=True,
                                  stop_on_error=False, timeout=90, max_output_bytes=2*1024*1024)
        if output_dir:
            directory = Path(output_dir).expanduser().resolve()
            directory.mkdir(parents=True, exist_ok=True)
            for item in result["results"]:
                (directory / (item["label"] + ".txt")).write_text(item["output"], encoding="utf-8")
            (directory / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            result["output_dir"] = str(directory)
        return result

    def gesture(self, action, points=None, selector=None, percent=50, duration=0.5):
        if not 0.05 <= duration <= 10 or not 1 <= percent <= 100:
            raise ValueError("invalid gesture duration/percent")
        d = self.u2
        if d is None:
            return {"success": False, "error": "Multi-point/pinch gestures require uiautomator2"}
        if action == "path":
            if not points or not 2 <= len(points) <= 100 or any(len(p) != 2 or min(p) < 0 for p in points):
                raise ValueError("path needs 2..100 nonnegative [x,y] points")
            d.swipe_points(points, duration=duration)
        elif action in ("pinch_in", "pinch_out"):
            if selector is None:
                raise ValueError("pinch requires selector")
            node = self.resolve_selector(selector)
            # Bounds selector is supported by uiautomator2's XPath engine. Pinch
            # uses a UiObject selected by the element's resource/class identity.
            target = d(resourceId=node["resource_id"], className=node["full_class"]) if node["resource_id"] else d(className=node["full_class"])
            if target.count != 1:
                raise ValueError("pinch target must have a unique resource-id/class")
            getattr(target, action)(percent=percent, steps=max(2, int(duration*100)))
        else:
            raise ValueError("action must be path/pinch_in/pinch_out")
        self.invalidate_ui()
        return {"success": True, "action": action}
