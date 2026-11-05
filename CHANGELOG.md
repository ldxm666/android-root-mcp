# Changelog

## 2.1.0 — 2026-11-05

工具总数 53 → 57。本版把「操作手机」从截图猜位置改成读系统数据加动手学习。

- 新增 `src/ui_semantics.py`：一次层级 dump 解析成有类型的控件——`role`、带来源标注的 `label`（text/desc/hint/resource_name）、`actions`、不含坐标的稳定身份 `semantic_key`、`region`，以及区分「系统说的」与「推断的」的 `interactive_source`。2000 节点 51.1 ms。没有名字的控件 `label` 为空，不编造。
- 新增 `src/probe_engine.py` 与 `src/control_memory.py`：对系统不肯命名的控件逐个试探，拒绝删除/支付/退出登录/卸载/密码类目标并记录拒绝原因，比对前后界面，按（包名, Activity, 控件身份）落盘，之后由 `ui_describe` 以 `learned_effect` 带出。2000×2000 节点比对 9.45 ms。
- 新增 `src/native_input.py` 与 `native/pfdaemon`：常驻 root 守护进程向 `/dev/input/event7` 写打包的 `input_event`，绕开每次都要起 JVM 的 `input` 二进制。实测点击 95–114 ms → 40.8 ms，300 ms 滑动 664 ms → 310.9 ms，按键 → 16.5–41 ms，五步工作流 663 ms → 215.9 ms。
- 新增四个工具：`ui_describe`（按系统语义读屏）、`ui_query`（按语义查找控件）、`act`（一次往返返回动作与结果，实测 231 ms）、`learn`（试探并记住无名控件的实际作用）。
- 选择器新增 `role` / `label_contains` / `semantic_key` 三个语义条件，回放不再依赖坐标。
- 动作类工具不再默认在每次操作后重抓整棵 UI 树（`return_ui` 默认 false），并新增 `ui_snapshot(since_snapshot_id=…)` 差异观察。
- 截图与控件树改为并发抓取；UI 快照缓存显示尺寸，滑动手势不再每次查询窗口大小。
- 修正 `element_action` 等工具在长列表中每次重抓全树造成的上下文与耗时浪费。
- 已知限制：中文输入不走原生 daemon（HyperOS 拒绝按键注入），仍用 uiautomator2 `set_text`；无 `semantic_key` 的控件身份依赖位置；非 0 旋转未验证；LSPosed 深读模块仅设计未实现。

## 2.0.0 — 2026-11-02

- 工具从实际 19 个扩展为 53 个（v2.0.0 时；v2.1.0 起为 57 个）；新增 capability 资源和开发任务提示模板。
- 新增原生精确选择器通道、AND 条件、歧义检查、快照编号/差异、旧编号失效检查。
- 去掉 UI 快照中昂贵的 `u2.app_current()` / `dumpsys activity top` 前台探测。
- 增加条件等待、多字段表单、预验证工作流、断言、分支、有限循环、后置条件、取消、持久化宏。
- 新增单次 ADB/Root Shell 批量执行；保存逐条退出码，修复忽略错误和未知动作仍报成功的问题。
- 二进制传输使用 exec-out 与独立退出状态帧，修复 OEM Shell 插入 CR 导致的文件损坏。
- 增加原子文件写入、权限保留、备份/哈希检查、目录推拉、类型化 Preferences、SQLite WAL 与受控写回、应用数据归档。
- 增加应用/权限/split APK、类型化 Intent、ContentProvider CRUD、设备设置/属性/控制、可恢复调优配置。
- 增加后台任务与日志增量、PID 身份检查、录屏、诊断报告、端口转发、WebView DOM/CDP、视觉差异比较。
- 重写安装器，使用项目内 venv 和生成配置；客户端 JSON 合并需要显式参数并先备份。
- 新增离线回归、实际 stdio MCP 协议测试、隔离真机检查与中文文档。
- 设备操作在线程执行，保持 MCP 事件循环可响应运行中状态/取消；原生选择器遗漏其他窗口时回退完整 XML。

## 1.0.0

输入压缩包中的版本，包含 19 个 MCP 工具。原 README 中“16 个工具”的标题与实际数量不一致。
