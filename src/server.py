import asyncio
import functools
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Annotated, Any, Dict, List, Optional, Union

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image as MCPImage
from mcp.types import TextContent
from pydantic import Field

sys.dont_write_bytecode = True

if __package__:
    from .advanced_controller import AdvancedAndroidController
    from .automation import compact_ui
    from .extended_tools import register_extended_tools
else:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.advanced_controller import AdvancedAndroidController
    from src.automation import compact_ui
    from src.extended_tools import register_extended_tools

# 配置 Windows 环境下的 UTF-8 编码，避免 stdio 传输乱码
if sys.platform == "win32" and os.environ.get("PYTHONIOENCODING") is None:
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            try:
                _reconfigure(encoding="utf-8")
            except Exception:
                pass


def _build_log_handlers() -> list:
    handlers = [logging.StreamHandler(sys.stderr)]
    candidates = [os.environ["ANDROID_MCP_LOG_FILE"]] if os.environ.get("ANDROID_MCP_LOG_FILE") else []
    for path in candidates:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            handlers.append(
                RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8")
            )
            break
        except OSError:
            continue
    return handlers


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=_build_log_handlers(),
)
logger = logging.getLogger("android_root_mcp")

mcp = FastMCP(
    "android-root-mcp",
    instructions=(
        "Android Root MCP v2：语义交互、可保存条件工作流、单次 ADB 批处理、Root 数据/系统管理、后台任务与 WebView DOM。\n"
        "核心铁律：\n"
        "1. 严禁像传统人机一样『走一步截一张图算像素坐标』！优先调用 `get_ui_tree`（实际耗时依设备而定）获取屏幕控件列表、snapshot_id 与 `#index` 编号。\n"
        "2. 连续操作（如：打开应用 -> 点击搜索框 -> 输入中文 -> 按回车）优先调用 `run_workflow` 或兼容的 `batch_ui_actions` 一次性批量完成，用条件等待替代固定 sleep。\n"
        "3. `tap_element`、`input_text`、`swipe_screen` 执行后默认自动返回最新 `post_ui` 控件树，无需在操作后重复调用 `get_ui_tree`。\n"
        "4. 在进行 Android 逆向或 LSPosed 钩子开发时，优先使用 `inspect_view_hierarchy` 获取真实自定义 View 类名、GONE/INVISIBLE 状态与 `0x7f...` 16进制资源锚点。"
    ),
)

controller = AdvancedAndroidController()


def _json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def locked_tool(fn):
    """Serialize mutations and UI caches; leave workflow_control independently callable."""
    def run_blocking(*args, **kwargs):
        try:
            with controller.lock:
                return fn(*args, **kwargs)
        except Exception as exc:
            logger.warning("Tool %s failed: %s", fn.__name__, exc)
            data = {"success": False, "code": getattr(exc, "code", "OPERATION_FAILED"),
                    "error": str(exc), "details": getattr(exc, "details", {})}
            if fn.__annotations__.get("return") is str:
                return _json_text(data)
            if fn.__annotations__.get("return") is list:
                return [TextContent(type="text", text=_json_text(data))]
            return data

    @functools.wraps(fn)
    async def invoke(*args, **kwargs):
        # FastMCP executes sync callbacks on its event loop. Offload blocking ADB,
        # RPC and workflow waits so status/cancel requests remain responsive.
        return await asyncio.to_thread(run_blocking, *args, **kwargs)

    return mcp.tool()(invoke)


# =============================================================================
# 一、 毫秒级屏幕感知与 Android 逆向视图透视
# =============================================================================

@locked_tool
def get_device_status() -> str:
    """
    获取当前连接的 Android 设备状态：设备型号、Android/SDK 版本、实际 Root UID、
    当前前台包名与 Activity、屏幕分辨率及 UIAutomator2 可提供的亮屏状态。
    """
    return _json_text(controller.get_device_status())


@locked_tool
def get_ui_tree(
    only_interactive: Annotated[
        bool,
        Field(description="是否只返回有文本或可交互（可点击/可输入/可滚动）的控件。默认 True 过滤无意义布局容器"),
    ] = True,
    keyword: Annotated[
        str,
        Field(description="可选过滤关键词（匹配 text、desc、resource-id 或类名），留空返回全部"),
    ] = "",
    max_elements: Annotated[
        int,
        Field(description="最多返回的元素数量，默认 100"),
    ] = 100,
) -> str:
    """
    【核心感知工具 - 替代慢速截图】通过高速 RPC 提取当前屏幕的语义化 UI 控件树。
    返回每个元素的 `[#index]` 编号、类名、文本 (`text`)、描述 (`desc`)、`id`、中心坐标与可交互标志 (`flags`)。
    后续可直接调用 `tap_element(index=N)` 或 `tap_element(text="...")` 精准操作！
    """
    res = controller.dump_ui_elements(only_interactive, keyword, max_elements)
    result = compact_ui(res)
    result["ui_elements_summary"] = res.get("summary", res.get("error", ""))
    return _json_text(result)


@locked_tool
def inspect_view_hierarchy(
    keyword: Annotated[
        str,
        Field(description="可选过滤关键词（如自定义 View 类名、0x7f 资源号、id 名称），留空返回顶层起全部视图"),
    ] = "",
    visibility_filter: Annotated[
        str,
        Field(description="可见性过滤：'ALL'（全部，含 GONE/INVISIBLE 隐藏广告锚点）、'VISIBLE'（仅可见）、'GONE'（仅隐藏）"),
    ] = "ALL",
    max_lines: Annotated[
        int,
        Field(description="最大返回视图行数，默认 180"),
    ] = 180,
) -> str:
    """
    【仅用于已知能吐出 View 树的 ROM】通过 `dumpsys activity top` 取 Activity/Task 记录。
    注意：本机 HyperOS（Android 16）实测该命令**不返回任何 View 层级**——`mID=`、`mText`、
    `android:text` 均为 0 行，微信整份输出只有 776 字节。所以在该机型上它拿不到控件信息，
    需要理解界面请直接用 `ui_describe`（OS 语义，非 OCR）。
    在确实支持 AOSP 式 `dumpsys activity top` 的 ROM 上，本工具可能额外给出：
    1. 真实的自定义 View 类名；2. 16 进制资源 ID 锚点；3. 被隐藏的 GONE/INVISIBLE 节点。
    """
    res = controller.inspect_native_view_hierarchy(
        keyword=keyword,
        visibility_filter=visibility_filter,
        max_lines=max_lines,
    )
    return _json_text({
        "success": res.get("success"),
        "elapsed_ms": res.get("elapsed_ms"),
        "activity_header": res.get("activity_header"),
        "total_matched_views": res.get("total_matched_views"),
        "hierarchy_text": res.get("hierarchy_text"),
    })


@locked_tool
def screenshot_annotated(
    annotate_elements: Annotated[
        bool,
        Field(description="是否在截图上自动绘制可点击元素的红色边框与 [#index] 编号（Set-of-Mark）。默认 True"),
    ] = True,
    max_width: Annotated[
        int,
        Field(description="图片最大宽度（等比缩放以加速传输），默认 720"),
    ] = 720,
    save_path: Annotated[
        Optional[str],
        Field(description="可选：将截图保存到本地磁盘的绝对路径"),
    ] = None,
) -> list:
    """
    【视觉兜底与界面审查工具】获取手机屏幕截图。安全图层/DRM 等限制仍可能导致空白或拒绝。
    默认开启 Set-of-Mark (`annotate_elements=True`)，在图上直接标出 `[#0]`, `[#1]` 红框编号，
    看图后无需猜像素坐标，直接调用 `tap_element(index=编号)` 即可！
    注意：日常操作请优先使用 `get_ui_tree`，仅在 Canvas/地图渲染/游戏或视觉验证时调用本工具。
    """
    jpeg_bytes, mime, meta = controller.capture_screenshot(
        annotate_elements=annotate_elements,
        max_width=max_width,
        save_path=save_path,
    )
    img_obj = MCPImage(data=jpeg_bytes, format="jpeg")
    return [
        img_obj.to_image_content(),
        TextContent(type="text", text=_json_text(meta)),
    ]


# =============================================================================
# 二、 丝滑流式交互与批处理宏（自动携带操作后最新 UI 状态，砍掉多余回合）
# =============================================================================

@locked_tool
def tap_element(
    index: Annotated[
        Optional[int],
        Field(description="来自 get_ui_tree 或标注截图的元素编号（如 0, 1, 2）"),
    ] = None,
    text: Annotated[
        Optional[str],
        Field(description="精确匹配元素文本（如 '设置'、'确定'）"),
    ] = None,
    text_contains: Annotated[
        Optional[str],
        Field(description="模糊包含匹配元素文本"),
    ] = None,
    resource_id: Annotated[
        Optional[str],
        Field(description="匹配元素的 resource-id 或短 id（如 'search_box' 或 'com.xxx:id/btn'）"),
    ] = None,
    description: Annotated[
        Optional[str],
        Field(description="匹配元素的 content-desc 无障碍描述"),
    ] = None,
    x: Annotated[Optional[int], Field(description="兜底直接点击的横坐标 X")] = None,
    y: Annotated[Optional[int], Field(description="兜底直接点击的纵坐标 Y")] = None,
    long_click: Annotated[bool, Field(description="是否长按，默认 False")] = False,
    wait_timeout: Annotated[
        float,
        Field(description="若按文本/ID查找，最多等待元素出现的秒数，默认 3.0 秒"),
    ] = 3.0,
    wait_after_ms: Annotated[
        int,
        Field(description="点击后等待界面响应的毫秒数，默认 0ms；需要等待请用条件"),
    ] = 0,
    return_ui_after: Annotated[
        bool,
        Field(description="点击后是否附带完整新界面 UI 树。默认 False——这次重抓是全流程里最贵的一步（全树约 200ms + 大量上下文）；连续点击时保持 False，需要看变化用 ui_snapshot(since_snapshot_id=...) 只取差异"),
    ] = False,
) -> str:
    """
    智能语义点击工具。支持按 `#index`、`text`、`text_contains`、`resource_id`、`description` 或 `(x, y)` 点击。
    点击即返回确认与耗时，不默认回抓整棵树——观察交给 ui_snapshot 的差异模式，节奏才跟得上手。
    """
    return _json_text(
        controller.tap_target(
            index=index,
            text=text,
            text_contains=text_contains,
            resource_id=resource_id,
            description=description,
            x=x,
            y=y,
            long_click=long_click,
            wait_timeout=wait_timeout,
            wait_after_ms=wait_after_ms,
            return_ui_after=return_ui_after,
        )
    )


@locked_tool
def input_text(
    text: Annotated[str, Field(description="要输入的文本；UIAutomator2 通道支持中文和 Emoji，不可用时明确返回失败")],
    index: Annotated[
        Optional[int],
        Field(description="可选：输入前先点击聚焦的输入框元素编号 #index"),
    ] = None,
    resource_id: Annotated[
        Optional[str],
        Field(description="可选：输入前先点击聚焦的输入框 resource-id"),
    ] = None,
    hint_or_text: Annotated[
        Optional[str],
        Field(description="可选：输入前先点击聚焦的输入框提示词或文本"),
    ] = None,
    clear_first: Annotated[bool, Field(description="输入前是否清空原有内容，默认 True")] = True,
    press_enter: Annotated[bool, Field(description="输入完成后是否自动按下回车/搜索键，默认 False")] = False,
    return_ui_after: Annotated[bool, Field(description="输入后是否附带完整 UI 树。默认 False——每次重抓全树约 200ms 并吃掉大量上下文；要看结果用 ui_snapshot(since_snapshot_id=...) 只取差异")] = False,
) -> str:
    """
    极速中文/Unicode 文本输入工具。
    可一步完成『聚焦输入框 -> 清空旧内容 -> 注入中文字符串 -> 按回车提交 -> 返回新界面 UI 树』，并自动保护系统原输入法不被篡改。
    """
    return _json_text(
        controller.input_text_fast(
            text=text,
            index=index,
            resource_id=resource_id,
            hint_or_text=hint_or_text,
            clear_first=clear_first,
            press_enter=press_enter,
            return_ui_after=return_ui_after,
        )
    )


@locked_tool
def swipe_screen(
    direction: Annotated[
        Optional[str],
        Field(description="滑动方向：'up'（手指上滑浏览下方内容）、'down'、'left'、'right'"),
    ] = "up",
    scale: Annotated[float, Field(description="滑动幅度比例 (0.2 ~ 0.9)，默认 0.6")] = 0.6,
    scroll_to_text: Annotated[
        Optional[str],
        Field(description="可选：自动连续滚动屏幕直到出现包含该文本的元素为止！"),
    ] = None,
    x1: Annotated[Optional[int], Field(description="自定义起点 X")] = None,
    y1: Annotated[Optional[int], Field(description="自定义起点 Y")] = None,
    x2: Annotated[Optional[int], Field(description="自定义终点 X")] = None,
    y2: Annotated[Optional[int], Field(description="自定义终点 Y")] = None,
    duration_ms: Annotated[int, Field(description="滑动耗时毫秒数，默认 350")] = 350,
    return_ui_after: Annotated[bool, Field(description="滑动后是否附带完整 UI 树。默认 False——连续滑动时每步重抓全树会拖慢节奏；滚屏找元素请直接用 scroll_to_text")] = False,
) -> str:
    """
    智能滑动与滚动查找工具。
    支持四向滑动、自定义坐标拖拽，或设置 `scroll_to_text="目标文字"` 让工具自动滚屏寻找目标！
    """
    return _json_text(
        controller.swipe_screen(
            direction=direction,
            scale=scale,
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            duration_ms=duration_ms,
            scroll_to_text=scroll_to_text,
            return_ui_after=return_ui_after,
        )
    )


@locked_tool
def press_key(
    key: Annotated[
        str,
        Field(description="按键名称或代码：'BACK'、'HOME'、'APP_SWITCH'、'ENTER'、'DEL'、'VOLUME_UP'、'VOLUME_DOWN'、'POWER'、'WAKEUP'、'SLEEP'"),
    ] = "BACK",
    wait_after_ms: Annotated[int, Field(description="按键后等待毫秒数，默认 0")] = 0,
    return_ui_after: Annotated[bool, Field(description="按键后是否附带完整 UI 树。默认 False——按返回键后通常只需知道动作已送达，需要时再 ui_snapshot")] = False,
) -> str:
    """发送系统物理/虚拟按键（返回、主页、多任务、回车、电源唤醒等），并自动返回最新界面结构。"""
    if not 0 <= wait_after_ms <= 60000:
        raise ValueError("wait_after_ms must be in 0..60000")
    result = controller.press("APP_SWITCH" if key.upper() == "RECENTS" else key)
    if wait_after_ms:
        time.sleep(wait_after_ms / 1000)
    if return_ui_after:
        result["post_ui"] = compact_ui(controller.dump_ui_elements())
    return _json_text(result)


@locked_tool
def batch_ui_actions(
    steps: Annotated[
        List[Dict[str, Any]],
        Field(
            description=(
                "要连续执行的动作数组。支持动作类型：\n"
                "1. {'action': 'launch', 'package': 'com.xxx', 'activity': '可选'}\n"
                "2. {'action': 'wait_for', 'text': '搜索', 'timeout': 5}\n"
                "3. {'action': 'tap', 'text': '...', 'text_contains': '...', 'id': '...', 'index': 0, 'x': 100, 'y': 200}\n"
                "4. {'action': 'input', 'text': '要输入的中文', 'hint': '搜索框提示词', 'clear': True, 'enter': True}\n"
                "5. {'action': 'swipe', 'direction': 'up', 'scroll_to_text': '可选滚动查找文字'}\n"
                "6. {'action': 'key', 'key': 'BACK'}\n"
                "7. {'action': 'sleep', 'ms': 500}\n"
                "8. {'action': 'root_cmd', 'cmd': '...'}"
            )
        ),
    ],
    return_ui_after: Annotated[bool, Field(description="批量执行完毕后是否附带最终 UI 树。默认 False——流水线跑完再由调用方决定是否观察；流水线中途失败仍会返回当时页面供纠错")] = False,
) -> str:
    """
    【王牌去人机感工具】单次调用连续执行多步手机操作流水线！
    当能够预判接下来的 2~6 步操作时（例如：启动应用 -> 等待首页 -> 点击搜索框 -> 输入关键词 -> 按回车），
    必须使用本工具一次性下发，彻底告别『点一下停几秒等下一轮』的卡顿体验！
    若中途某一步失败，会自动中断并返回当前停留页面的 UI 树供纠错。
    """
    return _json_text(controller.execute_batch_actions(steps=steps, return_ui_after=return_ui_after))


# =============================================================================
# 三、 Root 特权穿透、私有组件直达与零界面数据操控
# =============================================================================

@locked_tool
def root_shell(
    command: Annotated[str, Field(description="要在手机上以 uid=0(root) 执行的 Linux / Android Shell 命令")],
    timeout: Annotated[float, Field(description="命令超时秒数，默认 20.0")] = 20.0,
) -> str:
    """
    【Root 上帝模式直通车】通过 `su -c` 在手机上执行任意 Root 特权 Shell 命令。
    可执行诸如修改系统属性 (`resetprop`/`setprop`)、查看内核/挂载信息、操作 `/data/` 私有目录、管理防火墙等任意底层指令。
    """
    controller.invalidate_ui()
    return _json_text(controller.shell_result(command, root=True, timeout=timeout).as_dict())


@locked_tool
def start_activity_root(
    package: Annotated[str, Field(description="应用包名（如 'com.example.app' 或 'com.android.settings'）")] = "",
    activity: Annotated[
        str,
        Field(description="目标 Activity 名称（支持未导出的私有 Activity，如 '.map.main.MainActivity'），留空则启动应用默认主页"),
    ] = "",
    data_uri: Annotated[
        str,
        Field(description="可选：DeepLink URI / Scheme（如 'baidumap://map/direction?...' 或 'https://...'）"),
    ] = "",
    action: Annotated[str, Field(description="可选：Intent Action（如 'android.intent.action.VIEW'）")] = "",
    extras: Annotated[
        Optional[Dict[str, Union[str, int, bool]]],
        Field(description="可选：Intent Extras 键值对（自动识别 str/int/bool 转换为 --es/--ei/--ez）"),
    ] = None,
    force_stop_first: Annotated[bool, Field(description="启动前是否先强行停止该应用以确保冷启动，默认 False")] = False,
    return_ui_after: Annotated[bool, Field(description="启动后是否附带界面 UI 树。默认 False——启动本身有耗时，重抓全树会让等待翻倍；需要确认落地页再 ui_snapshot")] = False,
) -> str:
    """
    【Root 页面直达工具】利用 Root 权限强行穿透拉起任意内部私有 Activity (`exported=false`) 或 DeepLink Scheme，
    直接一步跨越繁琐的开屏广告与多级菜单跳转！
    """
    if force_stop_first and package:
        stopped = controller.app_action("stop", package)
        if not stopped["success"]:
            return _json_text(stopped)
    if package and not activity and not data_uri and not action and not extras:
        result = controller.launch_app(package)
    else:
        result = controller.send_intent(package=package, activity=activity, action=action,
                                        data_uri=data_uri, extras=extras)
    if return_ui_after:
        result["post_ui"] = compact_ui(controller.dump_ui_elements())
    return _json_text(result)


@locked_tool
def manage_app(
    package: Annotated[str, Field(description="目标应用包名")],
    action: Annotated[
        str,
        Field(
            description=(
                "操作类型：\n"
                "- 'info': 查看版本名、versionCode、APK路径、UID、进程PID、签名与组件信息\n"
                "- 'stop': 强行停止应用 (am force-stop)\n"
                "- 'restart': 强杀并重新冷启动应用\n"
                "- 'clear_data': 清空应用数据与缓存 (pm clear)\n"
                "- 'grant_all_permissions': 尝试授予已声明的危险运行时权限，逐项返回结果；特殊权限需 app_permissions 明确设置"
            )
        ),
    ] = "info",
) -> str:
    """应用生命周期与权限管理工具（信息、停止、重启、清空数据、逐项尝试授予运行时权限）。"""
    if action == "grant_all_permissions":
        return _json_text(controller.permissions(package, "grant_runtime_all"))
    mapped = {"clear_data": "clear"}.get(action, action)
    result = controller.app_action(mapped, package)
    if action == "restart" and result["success"]:
        result["post_ui"] = compact_ui(controller.dump_ui_elements())
    return _json_text(result)


@locked_tool
def inspect_app_data(
    package: Annotated[str, Field(description="目标应用包名（如 'com.example.app'）")],
    mode: Annotated[
        str,
        Field(
            description=(
                "操作模式：\n"
                "- 'list': 列出应用私有目录下的 shared_prefs、databases 和 files\n"
                "- 'read_prefs': 读取 SharedPreferences XML 配置内容\n"
                "- 'sql_query': 对应用私有 SQLite 数据库执行 SQL 查询"
            )
        ),
    ] = "list",
    target_file: Annotated[
        str,
        Field(description="当 mode 为 'read_prefs' 时填 XML 文件名（留空读全部）；当 mode 为 'sql_query' 时填数据库文件名（如 'data.db'）"),
    ] = "",
    sql_query: Annotated[
        str,
        Field(description="当 mode='sql_query' 时执行的 SQL 语句（留空默认查询所有表名与表结构）"),
    ] = "",
    pref_key: Annotated[
        str,
        Field(description="当 mode='read_prefs' 时可选过滤的配置键名关键词"),
    ] = "",
) -> str:
    """
    【零界面数据穿透工具】直接通过 Root 读取或查询 `/data/data/<package>/` 下的 SharedPreferences 配置与 SQLite 数据库！
    无需打开应用界面，直接核查开关状态、缓存数据与内部表记录。
    """
    return _json_text(
        controller.inspect_app_data(
            package=package,
            mode=mode,
            target_file=target_file,
            sql_query=sql_query,
            pref_key=pref_key,
        )
    )


@locked_tool
def query_system_provider(
    source: Annotated[
        str,
        Field(
            description=(
                "数据源类型：\n"
                "- 'notifications': 抓取当前系统通知栏所有未读通知详情（含验证码、推送标题与正文）\n"
                "- 'sms': 直接读取短信收件箱最新消息\n"
                "- 'clipboard_get': 读取当前手机剪贴板文本\n"
                "- 'clipboard_set': 写入文本到手机剪贴板\n"
                "- 'custom_uri': 查询自定义 ContentProvider URI"
            )
        ),
    ] = "notifications",
    value_or_uri: Annotated[
        str,
        Field(description="当 source='clipboard_set' 时为要写入的文本；当 source='custom_uri' 时为 content://... 路径"),
    ] = "",
) -> str:
    """
    【后台零界面取证工具】无需下拉通知栏或打开短信 App，直接通过 Root 提取通知栏消息、短信验证码或读写系统剪贴板。
    """
    if source == "notifications":
        return _json_text(controller.shell_result("dumpsys notification --noredact", root=True, max_bytes=65536).as_dict())
    if source == "sms":
        return _json_text(controller.provider(uri="content://sms/inbox", projection=["address", "body", "date"], sort="date DESC"))
    if source == "custom_uri":
        return _json_text(controller.provider(uri=value_or_uri))
    if source in ("clipboard_get", "clipboard_set"):
        device = controller.u2
        if device is None:
            return _json_text({"success": False, "error": "Clipboard requires uiautomator2"})
        if source == "clipboard_set":
            device.clipboard = value_or_uri
            return _json_text({"success": True, "characters": len(value_or_uri)})
        return _json_text({"success": True, "clipboard": device.clipboard})
    raise ValueError("unknown provider source")


# =============================================================================
# 四、 Android 逆向工程、Logcat 崩溃诊断与 LSPosed 一键联调
# =============================================================================

@locked_tool
def read_logcat_smart(
    package: Annotated[str, Field(description="按应用包名自动解析 PID 过滤日志（如 'com.example.app'）")] = "",
    tag: Annotated[str, Field(description="按 Log Tag 过滤（如 'MapAdKiller'、'AndroidRuntime'）")] = "",
    grep_keyword: Annotated[str, Field(description="按正文中包含的关键词过滤")] = "",
    level: Annotated[str, Field(description="最低日志级别：'V', 'D', 'I', 'W', 'E', 'F'，默认 'I'")] = "I",
    lines: Annotated[int, Field(description="返回的最新日志行数，默认 100")] = 100,
    crash_only: Annotated[bool, Field(description="是否仅读取崩溃缓冲池 (logcat -b crash)，查闪退必开！默认 False")] = False,
    clear_first: Annotated[bool, Field(description="是否清空旧日志缓冲池 (logcat -c)，默认 False")] = False,
) -> str:
    """
    【智能 Logcat 诊断工具】支持按包名自动绑定进程 PID、一键提取闪退堆栈 (`crash_only=True`)、按 Tag 或关键词精准过滤日志。
    """
    return _json_text(
        controller.read_smart_logcat(
            package=package,
            tag=tag,
            grep_keyword=grep_keyword,
            level=level,
            lines=lines,
            crash_only=crash_only,
            clear_first=clear_first,
        )
    )


@locked_tool
def inspect_lsposed(
    keyword: Annotated[str, Field(description="可选：过滤 LSPosed 日志中的关键词（如模块名、宿主包名、Hook 标记）")] = "",
    lines: Annotated[int, Field(description="返回最新日志行数，默认 100")] = 100,
    include_verbose: Annotated[bool, Field(description="是否读取 LSPosed 框架底层 verbose 日志，默认 False 读模块日志")] = False,
) -> str:
    """
    【LSPosed / Xposed / KernelSU 专属调试工具】
    直接通过 Root 读取 `/data/adb/lspd/log/modules_*.log` 最新模块日志与已安装的 KernelSU/Zygisk 模块列表，
    快速验证 Hook 是否生效、是否有 StackOverflowError / ClassNotFoundException 等异常！
    """
    return _json_text(
        controller.inspect_lsposed_and_ksu(
            keyword=keyword,
            lines=lines,
            include_verbose=include_verbose,
        )
    )


@locked_tool
def deploy_and_verify_apk(
    apk_path: Annotated[str, Field(description="本地电脑上编译好的 APK 绝对路径")],
    target_package: Annotated[
        str,
        Field(description="安装后需要强杀并冷启动验证的目标包名（如果是调试 LSPosed 模块，填宿主包名如 'com.example.app'；如果是普通 App，填自身包名）"),
    ],
    launch_activity: Annotated[str, Field(description="可选：指定启动的具体 Activity")] = "",
    wait_seconds: Annotated[float, Field(description="启动后等待钩子执行与页面加载的秒数，默认 3.5 秒")] = 3.5,
    log_keyword: Annotated[str, Field(description="可选：过滤提取日志的关键词")] = "",
) -> str:
    """
    【Android / LSPosed 开发一键联调闭环】
    单次调用自动完成：
    1. 安装本地 APK (`adb install -r -d`)
    2. 记录日志起始时间，保留全局 Logcat 缓冲池
    3. 强杀并冷启动目标宿主应用 (`am force-stop` + 启动)
    4. 等待页面渲染与 Hook 注入
    5. 自动汇总返回：是否有 Crash 堆栈 + 目标应用 Logcat + LSPosed 模块最新日志 + 当前屏幕 UI 控件树！
    """
    return _json_text(
        controller.deploy_and_verify_apk(
            apk_path=apk_path,
            target_package=target_package,
            launch_activity=launch_activity,
            wait_seconds=wait_seconds,
            log_keyword=log_keyword,
        )
    )


@locked_tool
def file_transfer_root(
    action: Annotated[str, Field(description="'pull'（从手机任意 Root 路径拉取到电脑）或 'push'（从电脑推送到手机任意 Root 路径）")],
    device_path: Annotated[str, Field(description="手机端路径（支持 /data/data/... 或 /data/adb/... 等受保护路径）")],
    local_path: Annotated[str, Field(description="电脑本地绝对路径")],
    file_mode: Annotated[str, Field(description="push 时赋予的目标文件权限，默认 '644'")] = "644",
) -> str:
    """
    【Root 级跨端文件推拉】利用 `/data/local/tmp` 中转与 Root 提权，支持直接拉取或替换 `/data/data/`、`/data/adb/` 等任意私有目录文件。
    """
    return _json_text(controller.transfer(action, device_path, local_path, file_mode))


@locked_tool
def optimize_device_for_agent(
    zero_animation: Annotated[
        bool,
        Field(description="是否将系统过渡动画缩放设为 0.0；修改前保存原值，恢复用返回的 profile_id。默认 True"),
    ] = True,
    stay_awake_while_plugged: Annotated[
        bool,
        Field(description="是否开启 USB 连接时保持屏幕常亮不息屏，默认 True"),
    ] = True,
    wake_and_unlock: Annotated[
        bool,
        Field(description="是否立即唤醒并解除锁屏遮挡，默认 True"),
    ] = True,
) -> str:
    """
    【设备极速模式调优】一键将手机调校为最适合 AI Agent 高速操控与调试的状态：
    1. 关闭系统窗口与过渡动画 (`animation_scale = 0`)，消除页面切换等待时间
    2. 开启连接电源时屏幕常亮 (`stay_on_while_plugged_in`)
    3. 自动唤醒屏幕并滑开锁屏层 (`wm dismiss-keyguard`)
    """
    result = controller.agent_profile("apply", zero_animation=zero_animation,
                                      stay_awake=stay_awake_while_plugged)
    if wake_and_unlock and result["success"]:
        result["wake"] = controller.device_control("wake")
        result["keyguard"] = controller.device_control("dismiss_keyguard")
    return _json_text(result)


register_extended_tools(mcp, controller, locked_tool)


def main():
    logger.info("启动 Android Root MCP 服务器 (stdio)")
    try:
        mcp.run(transport="stdio")
    finally:
        controller.close()


if __name__ == "__main__":
    main()
