# v2.1.0 验证记录

验证日期：2026-11-02（v2.0.0 记录），2026-11-05 按 v2.1.0 修订。此记录说明实际验证范围，不能据此认为所有 ROM、应用和特权操作均已真机覆盖。

> **v2.1.0 修订说明**：本文件原写有「89 项测试全部通过」。该数字来自 v2.0.0 的旧环境，在 v2.1.0 提交上无法复现——本次修订环境未安装 pytest，因此**没有重新运行完整测试套件**，也不再声明任何通过数量。已实测复核的只有两项：工具注册数为 **57**（进程内导入 `src.server` 后 `list_tools()` 计数），以及 `pytest` 与 `jsonschema` 的可用性（`jsonschema` 已装，`pytest` 缺失，已补进 `pyproject.toml` 的 `dev` 依赖）。下方「覆盖点」描述的是测试**试图**覆盖的范围，不代表当前提交的通过状态；要确认请按文末步骤在装好 `dev` 依赖的环境中自行运行。

## 环境

- Windows、Python 3.14.6、原生 ADB 37.0.0。
- MCP Python SDK 1.30.0、UIAutomator2 3.7.1、Pydantic 2.14.2、Pillow 12.3.0、websocket-client 1.9.2。
- pytest 9.2.0、Ruff 0.17.3。
- 一台经 USB 调试授权的 Android 16 / SDK 36、arm64 设备；通过 `su -c id` 实际确认 UID 0，设备使用 KernelSU。
- 项目声明 Python 3.10+。v2.0.0 记录时的语法解析检查不代表 v2.1.0 的当前状态；本次修订只对改动文件执行了 `py_compile`。运行环境版本见上，未声称在所有支持版本上运行过。

## 离线和协议检查

测试覆盖点（范围说明，非当前提交的通过声明）：

- 本机子进程真实退出码、stdout/stderr、超时、二进制字节、输出截断和文件输出。
- AND 选择器、歧义、disabled、旧快照、过滤后索引、差异、原生 RPC 路径与 XML 回退。
- Unicode 输入、聚焦失败、幂等勾选、条件观察失败、稳定检测、分支/循环/断言、步骤预算、截止时间、取消、宏保存和预验证。
- Shell/Intent/Provider 参数与引用、错误检测、SQLite WAL/只读/BLOB/受控写回、Preferences 类型、设置原值恢复、归档路径与链接检查。
- 57 个工具的注册和 JSON Schema；能力资源与提示模板；无设备时的工具行为。
- **真实 stdio 子进程和官方 MCP ClientSession**：initialize、list_tools、structuredContent、无设备工作流、非法动作拒绝，以及另一个请求在工作流运行中查询并取消。

协议检查曾暴露 SDK 同步回调阻塞事件循环的问题，已将设备调用放入工作线程；只在控制器级串行化设备操作。工作流控制使用独立短锁，不等待设备长任务。

其他已执行检查：

- `ruff check src tests scripts` 通过。
- `pip check` 无依赖冲突。
- 示例工作流通过实际 Pydantic 模型校验。
- 从真实注册结果生成 `docs/tool-schema.json`，共 57 个工具。
- 安装器在隔离的客户端配置文件上执行：保留已有 MCP 条目和其他配置，新增本项目条目并生成原文件备份。
- setuptools 构建 wheel 和 sdist 成功。
- 发布 ZIP 采用明确目录/文件白名单、CRC 检查及 SHA-256；排除虚拟环境、运行状态、日志和真机采集内容。

## 13 组隔离真机检查

使用 `scripts/live_smoke.py`；全部通过。仅执行只读系统/UI 探测，以及唯一 `/data/local/tmp/mcp_suite_smoke_<随机值>` 目录和本测试创建的后台任务操作。退出时清理测试文件、任务和自身端口映射。

| 检查组 | 实际检查内容 |
| --- | --- |
| root_identity | 实际 UID 0、SDK |
| exit_codes | 退出码 7、stdout/stderr 分离、不误报成功 |
| root_files | NUL、CR/LF、0xFF、中文和引号路径二进制往返；原子替换、640 权限、备份、SHA 不匹配拒绝 |
| directory_transfer | 隔离目录 push/pull |
| sqlite_wal | 手机没有 sqlite3 时，本机读取测试库 WAL 中的数据；只读模式拒绝写入 |
| batch_error_handling | 首错停止、显式继续但总状态仍失败 |
| batch_timing | 20 条简单 Root 命令逐条执行与单次批量执行 |
| remote_timeout | 手机端 timeout 终止测试 sleep，返回超时 |
| workflows | 条件、分支、三次循环、断言；非法后续动作在任何副作用前被拒绝 |
| workflow_cancel | 取消后不执行后面的测试写入 |
| background_jobs | Root/普通 Shell 后台任务、退出码、中文和换行、增量偏移、取消自建进程组 |
| port_forward | 动态本地端口创建、列举、移除 |
| ui_read_and_diff | 只读界面树、差异、完整 resource-id 的真实原生选择器查询；不点击界面 |

新增原生查询检查时发现：完整 XML 可以含有 UiSelector 查询不到的其他窗口节点。原生查询无结果时现已回退 XML；不把这个差异误判为目标一定不存在。

## 性能样本

最后一次完整真机检查的样本：

| 操作 | 测量结果 |
| --- | --- |
| 20 条 `printf` Root 命令，逐次请求 | 3475.6 ms |
| 同样 20 条命令，单次 `shell_batch` | 1333.9 ms，约 2.61 倍 |
| 本次首个 UI 快照（含本地连接初始化） | 1030.1 ms |
| 随后的 UI 快照 | 506.4 ms |
| 原生选择器结果引擎 | `uiautomator2_selector` |

早期针对这台手机的定位测量中，单独的 `u2.app_current()` 查询约 5.4 秒，是旧 UI 读取约 5.8 秒的主要开销；已从每次快照中移除。其他热读取样本约 0.26–0.53 秒。前台内容和节点数会变化，因此这些数字不是固定 SLA，也不能推导所有任务都能获得相同加速。

主要节省还来自减少 AI/MCP 往返：整段工作流、条件立即满足即继续、批量 Shell、最后一次 UI、后台直接数据通道。未对模型自身推理延迟或网络客户端进行统一基准测试。

## 本次未在真实用户数据上执行的操作

没有对设备执行实际应用安装/卸载/清空数据、用户应用数据恢复、真实 Preferences/数据库写回、权限授予、系统设置改写、重启、点击或输入。上述相关逻辑通过模型、构造命令及本机/模拟对象测试覆盖，仍需要在你自己的测试应用上验证业务效果。

没有为了测试强制开启第三方应用的 WebView 调试；CDP 动作与录屏完整产物未在本次真机检查中覆盖。WebView 必须由目标应用开放调试，厂商命令、特殊权限、加密数据库和安全图层可能不受支持；失败会返回错误。

## 复现

在安装开发依赖后运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe scripts/export_tool_schema.py
# 显式填入自己的设备序列号；会启动/连接 UIAutomator2 辅助服务
.\.venv\Scripts\python.exe scripts/live_smoke.py --serial 设备序列号 --report .artifacts/live.json
.\.venv\Scripts\python.exe scripts/package_release.py
```

真机详细报告保存在本地 `.artifacts/`，不纳入发布 ZIP；其中可能含本机路径或诊断内容。
