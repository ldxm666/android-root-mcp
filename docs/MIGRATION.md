# 从 v1 升级到 v2

19 个原工具名和主要入参保留。新增能力通过 34 个新工具提供；建议新的自动化优先使用 `run_workflow` 和完整 resource-id 选择器。

| 原行为 | v2 行为 / 迁移方式 |
| --- | --- |
| 命令失败却返回 `success=true` | 返回真实退出码、stdout/stderr 和失败状态；调用方需要处理失败 |
| 未知批处理动作直接跳过 | 在第一个动作前校验整段工作流，拼写错误会直接拒绝 |
| 多个选择器条件任一个匹配即点击 | 条件按 AND 匹配；多目标须限定条件或显式 nth |
| UI 过滤后重新生成编号 | 编号来自完整树的顺序，过滤不重编号，可能不连续 |
| 过期 index 自动刷新后继续按数字点 | 拒绝旧编号；新选择器传 snapshot_id，或改用语义条件 |
| `batch_ui_actions` 可直接传旧 index | 若使用 index，需在该步传 snapshot_id；一段宏优先用不会过期的语义选择器 |
| 每次 tap/key 固定 sleep | 默认等待改成 0；显式 wait_after_ms 仍支持，但推荐条件等待 |
| `get_ui_tree` 只有摘要 | 保留 ui_elements_summary 别名，增加 summary、snapshot_id 等字段；完整节点用 ui_snapshot(compact=false) |
| 字符/IME 不可用也尝试空剪贴板粘贴 | 明确返回输入不可用，不会把其他剪贴板内容误当作目标文本 |
| 以路径存在判断 push/pull 成功 | 检查命令状态；现有文件原子替换并保留 UID/mode，新文件才应用 file_mode |
| `grant_all_permissions` 连特殊权限也默认打开 | 仅处理声明的危险运行时权限并逐条报告；特殊 appop 用 app_permissions 显式操作 |
| optimize 关闭时一律写动画 1、常亮 0 | 保存原值并返回 profile_id；恢复用 agent_profile(action=restore) |
| deploy 清空全局 logcat | 保留其他开发者日志，按本次启动时间收集；就绪判断采用条件等待 |
| 固定某个客户端与绝对解释器路径 | install.ps1 生成当前项目真实 venv/服务器路径；默认不改全局客户端配置 |

v1 返回值仍为 JSON 文本；v2 新工具还提供 MCP structuredContent。错误包含 success、code、error 和可用的 details。`post_ui.success` 描述最终观察是否成功；要求业务结果成立时应写 `assert` / `expect`，不要仅凭“点按命令发出”判断任务已完成。

并发请求会串行访问设备，避免一次操作过程被切换设备/改写缓存。`workflow_control` 可独立检查/取消运行中的工作流；后台命令请使用 `device_job`，而不是长时间占住普通 Shell 请求。

高层数据写入与恢复工具会保留备份路径，并把应用留在停止状态。检查后再用 app_action(launch/restart) 启动。MCP 不会自动回滚已经执行的 Root/UI 操作。
