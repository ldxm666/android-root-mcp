import io
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple, Union

from PIL import Image, ImageDraw

logger = logging.getLogger("android_root_mcp.controller")


class AndroidRootController:
    """
    Android Root & UI Automation 双擎控制器 (u2 RPC + ADB Root Shell)
    专为消除 Agent "单步截图盲点的人机感" 以及强化 Android 逆向/LSPosed/应用开发调试设计。
    """

    def __init__(self, serial: Optional[str] = None):
        self.serial = serial or os.environ.get("ANDROID_SERIAL")
        self.adb_bin = self._resolve_adb_executable()
        self._u2_device = None
        self._last_elements: List[Dict[str, Any]] = []
        self._last_dump_time: float = 0.0
        self._screen_size: Tuple[int, int] = (1080, 2400)

    # =========================================================================
    # 底层连接管理：ADB / Root Shell / uiautomator2 RPC
    # =========================================================================

    @staticmethod
    def _resolve_adb_executable() -> str:
        """
        优先解析原生 adb.exe 路径，避开 Windows 下 adb.bat 批处理对引号、分号和管道符的转义破坏，
        并确保在无 PATH 的 MCP 子进程环境中也能稳定找到 adb.exe。
        """
        candidates = [
            os.environ.get("ADB_PATH", ""),
            os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
        ]
        which_adb = shutil.which("adb")
        if which_adb:
            if which_adb.lower().endswith(".bat"):
                bat_dir = os.path.dirname(os.path.abspath(which_adb))
                real_exe = os.path.normpath(os.path.join(bat_dir, "..", "platform-tools", "adb.exe"))
                candidates.insert(1, real_exe)
            candidates.append(which_adb)

        try:
            import adbutils
            candidates.append(adbutils.adb_path())
        except Exception:
            pass

        for c in candidates:
            if c and os.path.isfile(c):
                return c
        return "adb"

    def _adb_base_cmd(self) -> List[str]:
        cmd = [self.adb_bin]
        if self.serial:
            cmd.extend(["-s", self.serial])
        return cmd

    def run_adb(self, args: List[str], timeout: float = 15.0, binary: bool = False) -> Union[str, bytes]:
        """执行标准 ADB 命令"""
        cmd = self._adb_base_cmd() + args
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if binary:
                return res.stdout
            stdout = res.stdout.decode("utf-8", errors="replace").strip()
            stderr = res.stderr.decode("utf-8", errors="replace").strip()
            if res.returncode != 0 and not stdout and stderr:
                return f"[ADB Error code={res.returncode}] {stderr}"
            return stdout
        except subprocess.TimeoutExpired:
            return f"[ADB Timeout after {timeout}s] {' '.join(cmd)}"
        except Exception as e:
            return f"[ADB Exception] {e}"

    def run_shell(self, shell_cmd: str, timeout: float = 15.0) -> str:
        """执行普通 adb shell 命令"""
        out = self.run_adb(["shell", shell_cmd], timeout=timeout, binary=False)
        return out if isinstance(out, str) else out.decode("utf-8", errors="replace")

    def run_root(self, root_cmd: str, timeout: float = 20.0) -> str:
        """
        通过 su -c 执行 Root 特权命令。
        使用单引号安全转义内部命令，避免复杂管道、重定向或变量被宿主机/普通 shell 截断。
        """
        escaped = root_cmd.replace("'", "'\\''")
        full_shell = f"su -c '{escaped}'"
        return self.run_shell(full_shell, timeout=timeout)

    @property
    def u2(self):
        """懒加载连接 uiautomator2 高速 RPC 服务"""
        if self._u2_device is None:
            try:
                import uiautomator2 as u2
                self._u2_device = u2.connect(self.serial) if self.serial else u2.connect()
                info = self._u2_device.info
                w = info.get("displayWidth", 1080)
                h = info.get("displayHeight", 2400)
                if w and h:
                    self._screen_size = (int(w), int(h))
            except Exception as e:
                logger.warning(f"uiautomator2 连接失败，将使用原生 ADB Root 降级模式: {e}")
                self._u2_device = None
        return self._u2_device

    def reset_u2(self):
        self._u2_device = None

    # =========================================================================
    # 模块 1：设备状态与当前焦点感知
    # =========================================================================

    def get_device_status(self) -> Dict[str, Any]:
        """获取设备全面状态（型号、系统版本、Root/KernelSU/LSPosed状态、当前应用与Activity、屏幕尺寸）"""
        status: Dict[str, Any] = {}
        try:
            d = self.u2
            if d:
                info = d.info
                curr = d.app_current()
                status["u2_connected"] = True
                status["screen_on"] = info.get("screenOn", True)
                status["display_size"] = f"{info.get('displayWidth')}x{info.get('displayHeight')}"
                status["sdk_int"] = info.get("sdkInt")
                status["current_package"] = curr.get("package")
                status["current_activity"] = curr.get("activity")
                status["current_pid"] = curr.get("pid")
                self._screen_size = (int(info.get("displayWidth", 1080)), int(info.get("displayHeight", 2400)))
        except Exception as e:
            status["u2_connected"] = False
            status["u2_error"] = str(e)

        # 补充系统与 Root 环境信息
        probe_script = (
            "echo MODEL=$(getprop ro.product.marketname); "
            "echo ANDROID=$(getprop ro.build.version.release); "
            "echo ID=$(id); "
            "echo KSU=$(/data/adb/ksud --version 2>/dev/null || echo none); "
            "echo LSPD=$(ls -1t /data/adb/lspd/log/modules_*.log 2>/dev/null | head -n 1); "
            "echo ANIM=$(settings get global transition_animation_scale)"
        )
        root_out = self.run_root(probe_script, timeout=8.0)
        for line in root_out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                status[k.strip().lower()] = v.strip()

        if "current_package" not in status:
            focus = self.run_shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
            status["window_focus"] = focus

        return status

    # =========================================================================
    # 模块 2：毫秒级语义 UI 树解析 (240ms u2 RPC + 缓存索引)
    # =========================================================================

    @staticmethod
    def _parse_bounds(bounds_str: str) -> Optional[Tuple[int, int, int, int]]:
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str or "")
        if not m:
            return None
        return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))

    def dump_ui_elements(
        self,
        only_interactive: bool = True,
        keyword: str = "",
        max_elements: int = 120,
    ) -> Dict[str, Any]:
        """
        高速提取当前屏幕的 UI 语义控件树，过滤无意义容器，生成带 #index 编号的结构化列表。
        单次耗时仅 ~240ms，完全替代传统的慢速截图识图。
        """
        t0 = time.time()
        xml_str = ""
        engine = "u2"

        try:
            if self.u2:
                xml_str = self.u2.dump_hierarchy(compressed=False)
        except Exception as e:
            logger.warning(f"u2 dump_hierarchy 异常，回退 adb uiautomator: {e}")
            self.reset_u2()

        if not xml_str or "<hierarchy" not in xml_str:
            engine = "adb"
            raw = self.run_adb(["exec-out", "uiautomator", "dump", "/dev/tty"], timeout=10.0)
            if isinstance(raw, str) and "<hierarchy" in raw:
                start = raw.find("<?xml")
                if start == -1:
                    start = raw.find("<hierarchy")
                end = raw.rfind("</hierarchy>")
                if end != -1:
                    xml_str = raw[start : end + len("</hierarchy>")]

        if not xml_str or "<hierarchy" not in xml_str:
            return {
                "success": False,
                "error": "无法获取当前屏幕 UI 树（可能处于锁屏或过渡动画中）",
                "elapsed_ms": round((time.time() - t0) * 1000, 1),
            }

        root = ET.fromstring(xml_str)
        elements: List[Dict[str, Any]] = []
        kw_lower = keyword.strip().lower()

        for node in root.iter("node"):
            text = (node.attrib.get("text") or "").strip()
            desc = (node.attrib.get("content-desc") or "").strip()
            hint = (node.attrib.get("hint") or "").strip()
            res_id = (node.attrib.get("resource-id") or "").strip()
            cls_name = (node.attrib.get("class") or "").strip()
            pkg = (node.attrib.get("package") or "").strip()

            clickable = node.attrib.get("clickable") == "true"
            long_clickable = node.attrib.get("long-clickable") == "true"
            scrollable = node.attrib.get("scrollable") == "true"
            checkable = node.attrib.get("checkable") == "true"
            checked = node.attrib.get("checked") == "true"
            focused = node.attrib.get("focused") == "true"
            enabled = node.attrib.get("enabled", "true") == "true"
            is_edit = "EditText" in cls_name

            bounds_tuple = self._parse_bounds(node.attrib.get("bounds", ""))
            if not bounds_tuple:
                continue
            x1, y1, x2, y2 = bounds_tuple
            w, h = x2 - x1, y2 - y1
            if w <= 2 or h <= 2:
                continue

            has_meaning = bool(text or desc or hint or clickable or long_clickable or scrollable or checkable or is_edit)
            if only_interactive and not has_meaning:
                continue

            if kw_lower:
                combined = f"{text} {desc} {hint} {res_id} {cls_name}".lower()
                if kw_lower not in combined:
                    continue

            short_cls = cls_name.split(".")[-1] if cls_name else "View"
            short_id = res_id.split("/")[-1] if "/" in res_id else res_id

            flags = []
            if clickable:
                flags.append("click")
            if long_clickable:
                flags.append("long_click")
            if is_edit:
                flags.append("input")
            if scrollable:
                flags.append("scroll")
            if checked:
                flags.append("checked")
            if focused:
                flags.append("focused")
            if not enabled:
                flags.append("disabled")

            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            item = {
                "index": len(elements),
                "class": short_cls,
                "full_class": cls_name,
                "text": text,
                "desc": desc,
                "hint": hint,
                "id": short_id,
                "resource_id": res_id,
                "package": pkg,
                "bounds": [x1, y1, x2, y2],
                "center": [cx, cy],
                "flags": flags,
            }
            elements.append(item)

        self._last_elements = elements
        self._last_dump_time = time.time()

        # 格式化高信息密度摘要文本供 LLM 极速阅读
        lines = []
        for el in elements[:max_elements]:
            parts = [f"[#{el['index']}] <{el['class']}>"]
            if el["text"]:
                parts.append(f'text="{el["text"]}"')
            if el["desc"]:
                parts.append(f'desc="{el["desc"]}"')
            if el["hint"]:
                parts.append(f'hint="{el["hint"]}"')
            if el["id"]:
                parts.append(f'id="{el["id"]}"')
            parts.append(f"center=({el['center'][0]},{el['center'][1]})")
            parts.append(f"bounds=[{el['bounds'][0]},{el['bounds'][1]},{el['bounds'][2]},{el['bounds'][3]}]")
            if el["flags"]:
                parts.append(f"flags={','.join(el['flags'])}")
            lines.append(" ".join(parts))

        app_info = {}
        try:
            if self.u2:
                app_info = self.u2.app_current()
        except Exception:
            pass

        elapsed = round((time.time() - t0) * 1000, 1)
        return {
            "success": True,
            "engine": engine,
            "elapsed_ms": elapsed,
            "current_package": app_info.get("package", ""),
            "current_activity": app_info.get("activity", ""),
            "total_elements": len(elements),
            "shown_elements": min(len(elements), max_elements),
            "summary": "\n".join(lines),
            "elements": elements[:max_elements],
        }

    # =========================================================================
    # 模块 3：Android 逆向/开发专用 — 原生 View 树与 16 进制资源 ID 透视
    # =========================================================================

    def inspect_native_view_hierarchy(
        self,
        keyword: str = "",
        visibility_filter: str = "ALL",
        max_lines: int = 200,
    ) -> Dict[str, Any]:
        """
        通过 `dumpsys activity top` 提取原生 Android View 树（耗时仅 ~230ms）。
        相比 uiautomator，它保留了：
        1. 混淆/真实的自定义 View 类名（如 com.baidu.BaiduMap...）
        2. 16 进制资源锚点（如 #7f0a03fc app:id/search_box）—— 写 Xposed/LSPosed 钩子必备！
        3. GONE (G) / INVISIBLE (I) / VISIBLE (V) 隐藏视图状态！
        """
        t0 = time.time()
        raw = self.run_shell("dumpsys activity top", timeout=10.0)
        lines = raw.splitlines()

        in_hierarchy = False
        parsed_views = []
        current_task_activity = ""

        # View 行正则: class{hash flags l,t-r,b #hexId pkg:id/name}
        view_re = re.compile(
            r"^(\s*)([\w\.\$]+)\{([0-9a-fA-F]+)\s+([VIG]\.[ED\.][F\.][E\.][H\.][C\.][L\.][\.D])\s+([^\s]+)\s+(-?\d+,-?\d+--?\d+,-?\d+)(?:\s+#([0-9a-fA-F]+)(?:\s+([^\}]+))?)?\}"
        )

        kw_lower = keyword.strip().lower()
        vis_upper = visibility_filter.strip().upper()

        for line in lines:
            if "TASK " in line or "ACTIVITY " in line:
                if "ACTIVITY " in line:
                    current_task_activity = line.strip()
            if "View Hierarchy:" in line:
                in_hierarchy = True
                continue
            if in_hierarchy:
                if line.strip().startswith("Looper (") or line.strip().startswith("Local FragmentActivity"):
                    in_hierarchy = False
                    continue

                m = view_re.match(line)
                if m:
                    indent_str, cls_name, vhash, vflags, _, rect_str, hex_id, res_name = m.groups()
                    depth = len(indent_str) // 2
                    vis_char = vflags[0]  # V, I, G
                    vis_label = {"V": "VISIBLE", "I": "INVISIBLE", "G": "GONE"}.get(vis_char, vis_char)

                    if vis_upper in ("VISIBLE", "V") and vis_char != "V":
                        continue
                    if vis_upper in ("GONE", "G") and vis_char != "G":
                        continue
                    if vis_upper in ("INVISIBLE", "I") and vis_char != "I":
                        continue

                    entry_str = (
                        f"{'  ' * min(depth, 12)}[{vis_label[0]}] {cls_name} "
                        f"rect=({rect_str})"
                    )
                    if hex_id:
                        entry_str += f" hex=0x{hex_id}"
                    if res_name:
                        entry_str += f" id={res_name.strip()}"

                    if kw_lower and kw_lower not in entry_str.lower():
                        continue

                    parsed_views.append({
                        "depth": depth,
                        "visibility": vis_label,
                        "class": cls_name,
                        "flags": vflags,
                        "rect": rect_str,
                        "hex_id": f"0x{hex_id}" if hex_id else None,
                        "resource_name": res_name.strip() if res_name else None,
                        "formatted": entry_str,
                    })

        elapsed = round((time.time() - t0) * 1000, 1)
        return {
            "success": True,
            "elapsed_ms": elapsed,
            "activity_header": current_task_activity,
            "total_matched_views": len(parsed_views),
            "shown_views": min(len(parsed_views), max_lines),
            "hierarchy_text": "\n".join(v["formatted"] for v in parsed_views[:max_lines]),
            "views": parsed_views[:max_lines],
        }

    # =========================================================================
    # 模块 4：智能交互引擎（点击 / 中文输入 / 滑动 / 按键 / 批处理宏）
    # =========================================================================

    def resolve_element_target(
        self,
        index: Optional[int] = None,
        text: Optional[str] = None,
        text_contains: Optional[str] = None,
        resource_id: Optional[str] = None,
        description: Optional[str] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        wait_timeout: float = 3.0,
    ) -> Tuple[Optional[int], Optional[int], str]:
        """解析点击或输入的目标坐标，支持缓存索引、文本、ID、描述或直接坐标"""
        if x is not None and y is not None:
            return int(x), int(y), f"coords({x},{y})"

        # 1. 如果指定了缓存 index
        if index is not None:
            if not self._last_elements or (time.time() - self._last_dump_time > 30):
                self.dump_ui_elements(only_interactive=True)
            for el in self._last_elements:
                if el["index"] == index:
                    cx, cy = el["center"]
                    label = el["text"] or el["desc"] or el["id"] or el["class"]
                    return cx, cy, f"#[{index}] {label} ({cx},{cy})"
            return None, None, f"未找到索引 #{index} 对应的元素"

        # 2. 如果使用语义条件，支持轮询等待元素出现
        deadline = time.time() + max(0.1, wait_timeout)
        while True:
            self.dump_ui_elements(only_interactive=False)
            for el in self._last_elements:
                if text and el["text"] == text:
                    return el["center"][0], el["center"][1], f'text="{text}" ({el["center"][0]},{el["center"][1]})'
                if text_contains and text_contains.lower() in (el["text"] or "").lower():
                    return el["center"][0], el["center"][1], f'text_contains="{text_contains}" ({el["center"][0]},{el["center"][1]})'
                if description and description.lower() in (el["desc"] or "").lower():
                    return el["center"][0], el["center"][1], f'desc="{description}" ({el["center"][0]},{el["center"][1]})'
                if resource_id and (el["resource_id"] == resource_id or el["id"] == resource_id):
                    return el["center"][0], el["center"][1], f'id="{resource_id}" ({el["center"][0]},{el["center"][1]})'

            if time.time() >= deadline:
                break
            time.sleep(0.35)

        return None, None, f"未在屏幕找到匹配元素 (text={text}, text_contains={text_contains}, id={resource_id}, desc={description})"

    def tap_target(
        self,
        index: Optional[int] = None,
        text: Optional[str] = None,
        text_contains: Optional[str] = None,
        resource_id: Optional[str] = None,
        description: Optional[str] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        long_click: bool = False,
        duration_ms: int = 600,
        wait_timeout: float = 3.0,
        wait_after_ms: int = 550,
        return_ui_after: bool = True,
    ) -> Dict[str, Any]:
        """执行语义化或坐标点击，并在点击后自动返回最新 UI 树，省去额外回合"""
        cx, cy, desc_info = self.resolve_element_target(
            index=index,
            text=text,
            text_contains=text_contains,
            resource_id=resource_id,
            description=description,
            x=x,
            y=y,
            wait_timeout=wait_timeout,
        )
        if cx is None or cy is None:
            return {"success": False, "error": desc_info}

        try:
            if self.u2:
                if long_click:
                    self.u2.long_click(cx, cy, duration=duration_ms / 1000.0)
                else:
                    self.u2.click(cx, cy)
            else:
                if long_click:
                    self.run_shell(f"input swipe {cx} {cy} {cx} {cy} {duration_ms}")
                else:
                    self.run_shell(f"input tap {cx} {cy}")
        except Exception as e:
            return {"success": False, "error": f"点击执行失败: {e}"}

        if wait_after_ms > 0:
            time.sleep(wait_after_ms / 1000.0)

        result: Dict[str, Any] = {
            "success": True,
            "action": "long_click" if long_click else "tap",
            "target": desc_info,
            "coords": [cx, cy],
        }
        if return_ui_after:
            ui = self.dump_ui_elements(only_interactive=True, max_elements=80)
            result["post_ui"] = {
                "package": ui.get("current_package"),
                "activity": ui.get("current_activity"),
                "total_elements": ui.get("total_elements"),
                "summary": ui.get("summary"),
            }
        return result

    def input_text_fast(
        self,
        text: str,
        index: Optional[int] = None,
        resource_id: Optional[str] = None,
        hint_or_text: Optional[str] = None,
        clear_first: bool = True,
        press_enter: bool = False,
        return_ui_after: bool = True,
    ) -> Dict[str, Any]:
        """
        极速中文/Unicode/符号输入。
        支持先自动点击聚焦目标输入框，再通过 u2 FastInputIME 或系统剪贴板粘贴注入文字，并在结束后恢复原输入法。
        """
        focused_target = "current_focus"
        if index is not None or resource_id is not None or hint_or_text is not None:
            cx, cy, desc_info = self.resolve_element_target(
                index=index,
                resource_id=resource_id,
                text_contains=hint_or_text,
                wait_timeout=2.0,
            )
            if cx is not None and cy is not None:
                focused_target = desc_info
                if self.u2:
                    self.u2.click(cx, cy)
                else:
                    self.run_shell(f"input tap {cx} {cy}")
                time.sleep(0.25)

        orig_ime = self.run_shell("settings get secure default_input_method").strip()
        method_used = "u2_send_keys"

        try:
            if self.u2:
                self.u2.send_keys(text, clear=clear_first)
            else:
                raise RuntimeError("u2 未就绪，使用剪贴板粘贴")
        except Exception:
            method_used = "clipboard_paste"
            if clear_first:
                # 移动到末尾并回退清理或全选删除
                self.run_shell("input keyevent KEYCODE_MOVE_END")
            if self.u2:
                self.u2.clipboard = text
            self.run_shell("input keyevent 279")  # KEYCODE_PASTE
        finally:
            if orig_ime and "whatinput" not in orig_ime.lower() and "fastinput" not in orig_ime.lower():
                self.run_shell(f"ime set {orig_ime}")

        if press_enter:
            time.sleep(0.15)
            self.run_shell("input keyevent 66")  # KEYCODE_ENTER

        time.sleep(0.4)
        res: Dict[str, Any] = {
            "success": True,
            "text": text,
            "target": focused_target,
            "method": method_used,
            "press_enter": press_enter,
        }
        if return_ui_after:
            ui = self.dump_ui_elements(only_interactive=True, max_elements=80)
            res["post_ui"] = {
                "package": ui.get("current_package"),
                "activity": ui.get("current_activity"),
                "summary": ui.get("summary"),
            }
        return res

    def swipe_screen(
        self,
        direction: Optional[str] = "up",
        scale: float = 0.6,
        x1: Optional[int] = None,
        y1: Optional[int] = None,
        x2: Optional[int] = None,
        y2: Optional[int] = None,
        duration_ms: int = 350,
        scroll_to_text: Optional[str] = None,
        max_scrolls: int = 5,
        return_ui_after: bool = True,
    ) -> Dict[str, Any]:
        """支持按方向滑动（up=向上滑看下方内容）、自定义坐标滑动、或自动滚动直到找到指定文字"""
        w, h = self._screen_size
        cx, cy = w // 2, h // 2
        span_x = int(w * scale * 0.5)
        span_y = int(h * scale * 0.5)

        def _calc_dir_coords(d_str: str) -> Tuple[int, int, int, int]:
            d_str = (d_str or "up").lower()
            if d_str == "up":
                return cx, cy + span_y, cx, cy - span_y
            elif d_str == "down":
                return cx, cy - span_y, cx, cy + span_y
            elif d_str == "left":
                return cx + span_x, cy, cx - span_x, cy
            elif d_str == "right":
                return cx - span_x, cy, cx + span_x, cy
            return cx, cy + span_y, cx, cy - span_y

        if scroll_to_text:
            for attempt in range(max_scrolls):
                ui = self.dump_ui_elements(only_interactive=False)
                for el in ui.get("elements", []):
                    if scroll_to_text.lower() in (el.get("text", "") + " " + el.get("desc", "")).lower():
                        return {
                            "success": True,
                            "found": True,
                            "scrolls_performed": attempt,
                            "element": el,
                            "post_ui": {"summary": ui.get("summary")},
                        }
                sx1, sy1, sx2, sy2 = _calc_dir_coords(direction or "up")
                self.run_shell(f"input swipe {sx1} {sy1} {sx2} {sy2} {duration_ms}")
                time.sleep(0.5)

            ui = self.dump_ui_elements(only_interactive=True, max_elements=80)
            return {
                "success": False,
                "found": False,
                "error": f"滚动 {max_scrolls} 次后仍未找到文字 '{scroll_to_text}'",
                "post_ui": {"summary": ui.get("summary")},
            }

        if x1 is not None and y1 is not None and x2 is not None and y2 is not None:
            sx1, sy1, sx2, sy2 = x1, y1, x2, y2
        else:
            sx1, sy1, sx2, sy2 = _calc_dir_coords(direction or "up")

        self.run_shell(f"input swipe {sx1} {sy1} {sx2} {sy2} {duration_ms}")
        time.sleep(0.5)

        res: Dict[str, Any] = {
            "success": True,
            "from": [sx1, sy1],
            "to": [sx2, sy2],
            "duration_ms": duration_ms,
        }
        if return_ui_after:
            ui = self.dump_ui_elements(only_interactive=True, max_elements=80)
            res["post_ui"] = {
                "package": ui.get("current_package"),
                "activity": ui.get("current_activity"),
                "summary": ui.get("summary"),
            }
        return res

    def execute_batch_actions(self, steps: List[Dict[str, Any]], return_ui_after: bool = True) -> Dict[str, Any]:
        """
        批量执行多步手机动作序列（消灭一问一答的卡顿感）。
        每个 step 支持：
        - {"action": "launch", "package": "com.xxx", "activity": "..."}
        - {"action": "tap", "text": "...", "index": 0, "id": "...", "x": 100, "y": 200}
        - {"action": "input", "text": "搜索词", "clear": true, "enter": true}
        - {"action": "swipe", "direction": "up"}
        - {"action": "key", "key": "BACK"}
        - {"action": "wait_for", "text": "...", "timeout": 5}
        - {"action": "sleep", "ms": 500}
        - {"action": "root_cmd", "cmd": "..."}
        """
        trace = []
        t0 = time.time()

        for i, step in enumerate(steps):
            act = (step.get("action") or "").lower().strip()
            try:
                if act == "launch":
                    pkg = step.get("package", "")
                    act_name = step.get("activity", "")
                    if act_name:
                        out = self.run_root(f"am start -n {pkg}/{act_name}")
                    else:
                        out = self.run_shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
                    time.sleep(step.get("wait_ms", 1000) / 1000.0)
                    trace.append({"step": i, "action": "launch", "status": "ok", "detail": out[:120]})

                elif act in ("tap", "click", "long_click"):
                    r = self.tap_target(
                        index=step.get("index"),
                        text=step.get("text"),
                        text_contains=step.get("text_contains"),
                        resource_id=step.get("id") or step.get("resource_id"),
                        description=step.get("desc") or step.get("description"),
                        x=step.get("x"),
                        y=step.get("y"),
                        long_click=(act == "long_click" or step.get("long_click", False)),
                        wait_timeout=float(step.get("timeout", 3.0)),
                        wait_after_ms=int(step.get("wait_ms", 450)),
                        return_ui_after=False,
                    )
                    if not r.get("success"):
                        ui = self.dump_ui_elements(only_interactive=True, max_elements=60)
                        return {
                            "success": False,
                            "failed_step": i,
                            "step_config": step,
                            "error": r.get("error"),
                            "trace": trace,
                            "current_ui": ui.get("summary"),
                        }
                    trace.append({"step": i, "action": act, "target": r.get("target")})

                elif act in ("input", "type"):
                    r = self.input_text_fast(
                        text=step.get("text") or step.get("value", ""),
                        index=step.get("index"),
                        resource_id=step.get("id") or step.get("resource_id"),
                        hint_or_text=step.get("hint"),
                        clear_first=step.get("clear", True),
                        press_enter=step.get("enter", False),
                        return_ui_after=False,
                    )
                    trace.append({"step": i, "action": "input", "text": step.get("text")})

                elif act == "swipe":
                    self.swipe_screen(
                        direction=step.get("direction", "up"),
                        scale=float(step.get("scale", 0.6)),
                        x1=step.get("x1"),
                        y1=step.get("y1"),
                        x2=step.get("x2"),
                        y2=step.get("y2"),
                        duration_ms=int(step.get("duration_ms", 350)),
                        scroll_to_text=step.get("scroll_to_text"),
                        return_ui_after=False,
                    )
                    trace.append({"step": i, "action": "swipe", "direction": step.get("direction", "up")})

                elif act == "key":
                    key = step.get("key") or step.get("code", "BACK")
                    self.run_shell(f"input keyevent {key}")
                    time.sleep(step.get("wait_ms", 350) / 1000.0)
                    trace.append({"step": i, "action": "key", "key": key})

                elif act == "wait_for":
                    cx, cy, info = self.resolve_element_target(
                        text=step.get("text"),
                        text_contains=step.get("text_contains"),
                        resource_id=step.get("id") or step.get("resource_id"),
                        description=step.get("desc"),
                        wait_timeout=float(step.get("timeout", 6.0)),
                    )
                    if cx is None:
                        ui = self.dump_ui_elements(only_interactive=True, max_elements=60)
                        return {
                            "success": False,
                            "failed_step": i,
                            "error": info,
                            "trace": trace,
                            "current_ui": ui.get("summary"),
                        }
                    trace.append({"step": i, "action": "wait_for", "found": info})

                elif act in ("sleep", "wait"):
                    ms = int(step.get("ms", 500))
                    time.sleep(ms / 1000.0)
                    trace.append({"step": i, "action": "sleep", "ms": ms})

                elif act == "root_cmd":
                    out = self.run_root(step.get("cmd", ""))
                    trace.append({"step": i, "action": "root_cmd", "output": out[:200]})

                else:
                    trace.append({"step": i, "action": act, "warning": "未知动作类型，已跳过"})

            except Exception as e:
                ui = self.dump_ui_elements(only_interactive=True, max_elements=60)
                return {
                    "success": False,
                    "failed_step": i,
                    "error": str(e),
                    "trace": trace,
                    "current_ui": ui.get("summary"),
                }

        res: Dict[str, Any] = {
            "success": True,
            "total_steps": len(steps),
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
            "trace": trace,
        }
        if return_ui_after:
            ui = self.dump_ui_elements(only_interactive=True, max_elements=80)
            res["post_ui"] = {
                "package": ui.get("current_package"),
                "activity": ui.get("current_activity"),
                "total_elements": ui.get("total_elements"),
                "summary": ui.get("summary"),
            }
        return res

    # =========================================================================
    # 模块 5：Set-of-Mark (SoM) 标注截图引擎（仅用于视觉兜底与界面审查）
    # =========================================================================

    def capture_screenshot(
        self,
        annotate_elements: bool = True,
        max_width: int = 720,
        save_path: Optional[str] = None,
    ) -> Tuple[bytes, str, Dict[str, Any]]:
        """
        获取手机屏幕截图。
        开启 annotate_elements=True 时，自动将可交互元素的 [#index] 编号与红框画在图上（Set-of-Mark），
        让多模态模型无需猜像素坐标，直接看编号调用 tap_element(index=N)！
        """
        t0 = time.time()
        img: Optional[Image.Image] = None

        try:
            if self.u2:
                img = self.u2.screenshot()
        except Exception as e:
            logger.warning(f"u2 截图失败，回退 root screencap: {e}")

        if img is None:
            raw_png = self.run_adb(["exec-out", "su", "-c", "screencap -p"], timeout=10.0, binary=True)
            if isinstance(raw_png, bytes) and len(raw_png) > 100:
                img = Image.open(io.BytesIO(raw_png))
            else:
                raise RuntimeError("截图失败，请检查设备连接状态")

        orig_w, orig_h = img.size
        self._screen_size = (orig_w, orig_h)

        ui_meta = {}
        if annotate_elements:
            ui_meta = self.dump_ui_elements(only_interactive=True, max_elements=80)
            img = img.convert("RGB")
            draw = ImageDraw.Draw(img)
            for el in self._last_elements[:80]:
                if not any(f in el["flags"] for f in ("click", "input", "long_click")):
                    continue
                x1, y1, x2, y2 = el["bounds"]
                idx_str = f"#{el['index']}"
                draw.rectangle([x1, y1, x2, y2], outline=(255, 40, 40), width=3)
                # 绘制角标标签背景
                tag_w = max(36, len(idx_str) * 14 + 10)
                tag_h = 26
                ty1 = max(0, y1 - tag_h)
                draw.rectangle([x1, ty1, x1 + tag_w, ty1 + tag_h], fill=(220, 20, 20))
                draw.text((x1 + 5, ty1 + 4), idx_str, fill=(255, 255, 255))

        if save_path:
            os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
            img.save(save_path)

        # 缩放压缩以加速传输并节省视觉 Token
        if max_width and orig_w > max_width:
            ratio = max_width / float(orig_w)
            new_h = int(orig_h * ratio)
            img_resized = img.resize((max_width, new_h), Image.Resampling.LANCZOS)
        else:
            img_resized = img

        buf = io.BytesIO()
        img_resized.convert("RGB").save(buf, format="JPEG", quality=82)
        jpeg_bytes = buf.getvalue()

        meta = {
            "original_size": f"{orig_w}x{orig_h}",
            "Annotated": annotate_elements,
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
            "saved_to": save_path,
            "elements_summary": ui_meta.get("summary", "") if annotate_elements else "",
        }
        return jpeg_bytes, "image/jpeg", meta

    # =========================================================================
    # 模块 6：Root 应用数据穿透（SQLite 数据库 & SharedPreferences 零界面读写）
    # =========================================================================

    def inspect_app_data(
        self,
        package: str,
        mode: str = "list",
        target_file: str = "",
        sql_query: str = "",
        pref_key: str = "",
    ) -> Dict[str, Any]:
        """
        利用 Root 权限直接穿透读取或查询 /data/data/<package>/ 与 /data/user_de/0/<package>/ 下的 SharedPreferences 和 SQLite 数据库。
        SQLite 查询采用 Root 镜像拉取 + 本机 Python sqlite3 引擎，兼容无 /system/bin/sqlite3 的 Android 14/15/16 ROM！
        """
        ce_dir = f"/data/data/{package}"
        de_dir = f"/data/user_de/0/{package}"
        if mode == "list":
            cmd = (
                f"echo '=== CE (/data/data) SHARED_PREFS ==='; ls -la {ce_dir}/shared_prefs/ 2>/dev/null; "
                f"echo '=== DE (/data/user_de/0) SHARED_PREFS ==='; ls -la {de_dir}/shared_prefs/ 2>/dev/null; "
                f"echo '=== DATABASES ==='; ls -la {ce_dir}/databases/ {de_dir}/databases/ 2>/dev/null; "
                f"echo '=== FILES ==='; ls -la {ce_dir}/files/ 2>/dev/null | head -n 40"
            )
            return {"success": True, "package": package, "listing": self.run_root(cmd)}

        elif mode == "read_prefs":
            if target_file:
                if not target_file.endswith(".xml"):
                    target_file += ".xml"
                xml_content = self.run_root(
                    f"cat '{ce_dir}/shared_prefs/{target_file}' 2>/dev/null || cat '{de_dir}/shared_prefs/{target_file}' 2>/dev/null"
                )
            else:
                xml_content = self.run_root(
                    f"head -n 200 {ce_dir}/shared_prefs/*.xml {de_dir}/shared_prefs/*.xml 2>/dev/null"
                )

            if pref_key:
                filtered = [
                    line for line in xml_content.splitlines()
                    if pref_key.lower() in line.lower() or "==>" in line
                ]
                return {"success": True, "package": package, "filtered_key": pref_key, "content": "\n".join(filtered)}
            return {"success": True, "package": package, "content": xml_content[:15000]}

        elif mode == "sql_query":
            if not target_file:
                return {"success": False, "error": "sql_query 模式必须指定 target_file (数据库文件名，如 data.db)"}
            remote_ce = f"{ce_dir}/databases/{target_file}"
            remote_de = f"{de_dir}/databases/{target_file}"
            tmp_remote_dir = f"/data/local/tmp/mcp_db_{int(time.time())}"
            local_tmp_dir = tempfile.mkdtemp(prefix="mcp_android_db_")

            try:
                # 拷贝主库及可能的 -wal / -shm 日志以确保读取最新未 checkpoint 数据
                prep_cmd = (
                    f"mkdir -p {tmp_remote_dir} && "
                    f"(cp {remote_ce}* {tmp_remote_dir}/ 2>/dev/null || cp {remote_de}* {tmp_remote_dir}/ 2>/dev/null) && "
                    f"chmod -R 644 {tmp_remote_dir}"
                )
                self.run_root(prep_cmd)
                self.run_adb(["pull", f"{tmp_remote_dir}/.", local_tmp_dir], timeout=15.0)
                self.run_root(f"rm -rf {tmp_remote_dir}")

                local_db_path = os.path.join(local_tmp_dir, os.path.basename(target_file))
                if not os.path.exists(local_db_path):
                    return {"success": False, "error": f"数据库文件不存在或无法拉取: {remote_ce}"}

                conn = sqlite3.connect(local_db_path)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()

                query = sql_query.strip() or "SELECT name, type, sql FROM sqlite_master WHERE type IN ('table', 'view');"
                cur.execute(query)
                rows = [dict(r) for r in cur.fetchall()[:200]]
                conn.close()
                return {
                    "success": True,
                    "package": package,
                    "database": target_file,
                    "query": query,
                    "row_count": len(rows),
                    "rows": rows,
                }
            except Exception as e:
                return {"success": False, "error": f"SQLite 查询失败: {e}"}
            finally:
                shutil.rmtree(local_tmp_dir, ignore_errors=True)

        return {"success": False, "error": f"不支持的 mode: {mode}"}

    # =========================================================================
    # 模块 7：Android 开发、LSPosed 模块调试与逆向工程闭环
    # =========================================================================

    def read_smart_logcat(
        self,
        package: str = "",
        tag: str = "",
        grep_keyword: str = "",
        level: str = "I",
        lines: int = 120,
        crash_only: bool = False,
        clear_first: bool = False,
    ) -> Dict[str, Any]:
        """智能读取 Logcat，支持按应用包名自动解析 PID、过滤崩溃缓冲池 (crash buffer)、按 Tag 或关键词筛选"""
        if clear_first:
            self.run_shell("logcat -c")
            return {"success": True, "action": "cleared_logcat"}

        if crash_only:
            out = self.run_shell(f"logcat -b crash -d -t {lines}")
            return {"success": True, "buffer": "crash", "logs": out}

        cmd_parts = [f"logcat -d -t {max(lines * 4, 500)}"]
        pid = ""
        if package:
            pid = self.run_shell(f"pidof {package}").strip().split(" ")[0]
            if pid.isdigit():
                cmd_parts.append(f"--pid={pid}")

        if tag:
            cmd_parts.append(f"-s '{tag}:{level}'")
        elif level in ("W", "E", "F"):
            cmd_parts.append(f"*:{level}")

        raw_log = self.run_shell(" ".join(cmd_parts), timeout=10.0)
        log_lines = raw_log.splitlines()

        if package and not pid.isdigit():
            log_lines = [line for line in log_lines if package.lower() in line.lower()]
        if grep_keyword:
            kw = grep_keyword.lower()
            log_lines = [line for line in log_lines if kw in line.lower()]

        tail_lines = log_lines[-lines:]
        return {
            "success": True,
            "package": package,
            "resolved_pid": pid,
            "matched_lines": len(tail_lines),
            "logs": "\n".join(tail_lines),
        }

    def inspect_lsposed_and_ksu(
        self,
        keyword: str = "",
        lines: int = 120,
        include_verbose: bool = False,
    ) -> Dict[str, Any]:
        """
        直接通过 Root 读取 LSPosed 最新模块日志 (/data/adb/lspd/log/modules_*.log) 与 KernelSU/Zygisk 模块状态。
        专为 Xposed/LSPosed 钩子开发排错设计！
        """
        log_type = "verbose_*.log" if include_verbose else "modules_*.log"
        cmd = (
            f"LATEST=$(ls -1t /data/adb/lspd/log/{log_type} 2>/dev/null | head -n 1); "
            f"echo \"LOG_FILE=$LATEST\"; "
            f"if [ -n \"$LATEST\" ]; then tail -n {max(lines * 3, 300)} \"$LATEST\"; fi"
        )
        raw = self.run_root(cmd, timeout=10.0)
        out_lines = raw.splitlines()
        log_file = ""
        content_lines = []
        for line in out_lines:
            if line.startswith("LOG_FILE="):
                log_file = line.split("=", 1)[1]
            else:
                content_lines.append(line)

        if keyword:
            kw = keyword.lower()
            content_lines = [line for line in content_lines if kw in line.lower()]

        mods_cmd = "for d in /data/adb/modules/*; do [ -d \"$d\" ] && echo \"=== $(basename $d) ===\" && cat \"$d/module.prop\" 2>/dev/null | head -n 6; done"
        ksu_modules = self.run_root(mods_cmd, timeout=8.0)

        return {
            "success": True,
            "lspd_log_file": log_file,
            "matched_lines": len(content_lines[-lines:]),
            "lspd_logs": "\n".join(content_lines[-lines:]),
            "installed_root_modules": ksu_modules,
        }

    def deploy_and_verify_apk(
        self,
        apk_path: str,
        target_package: str,
        launch_activity: str = "",
        wait_seconds: float = 3.5,
        log_keyword: str = "",
    ) -> Dict[str, Any]:
        """
        Android / LSPosed 开发者一键部署验证闭环：
        1. 安装 APK (adb install -r -d)
        2. 清空旧 logcat 缓冲池
        3. 强杀重启目标宿主应用 (target_package)
        4. 等待应用初始化与钩子注入
        5. 自动采集目标应用 UI 树 + 崩溃堆栈 + Logcat + LSPosed 最新日志！
        """
        if not os.path.exists(apk_path):
            return {"success": False, "error": f"APK 文件不存在: {apk_path}"}

        t0 = time.time()
        install_out = self.run_adb(["install", "-r", "-d", apk_path], timeout=60.0)
        if "Success" not in str(install_out):
            return {"success": False, "stage": "install", "output": install_out}

        self.run_shell("logcat -c")
        self.run_root(f"am force-stop {target_package}")
        time.sleep(0.3)

        if launch_activity:
            launch_out = self.run_root(f"am start -n {target_package}/{launch_activity}")
        else:
            launch_out = self.run_shell(f"monkey -p {target_package} -c android.intent.category.LAUNCHER 1")

        time.sleep(max(1.0, wait_seconds))

        # 收集验证证据
        crash_log = self.read_smart_logcat(crash_only=True, lines=40).get("logs", "")
        app_log = self.read_smart_logcat(package=target_package, grep_keyword=log_keyword, lines=60).get("logs", "")
        lspd_log = self.inspect_lsposed_and_ksu(keyword=log_keyword or target_package, lines=50).get("lspd_logs", "")
        ui = self.dump_ui_elements(only_interactive=True, max_elements=60)

        return {
            "success": True,
            "elapsed_sec": round(time.time() - t0, 2),
            "install_output": install_out,
            "launch_output": launch_out[:200],
            "has_crash": bool(crash_log.strip()),
            "crash_logs": crash_log,
            "app_logs": app_log,
            "lsposed_logs": lspd_log,
            "post_ui": {
                "package": ui.get("current_package"),
                "activity": ui.get("current_activity"),
                "total_elements": ui.get("total_elements"),
                "summary": ui.get("summary"),
            },
        }
