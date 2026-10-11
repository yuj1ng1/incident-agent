# Runtime 收尾验证

核查起点为 `82a67504338a0b55f8e7c4b5c9cfbf1ee05c29ad`，分支 main，开始时与 origin/main 一致。已有未提交 Prompt、临时脚本、AGENTS.md、README 和文档整理改动保留，不包含在本次提交中。

## 已复现的问题与修复

1. 无 claim 投影先写 JSON、后校验版本。临时 SQLite/JSON、同步屏障和真实 Worker 的 Checkpoint 保存复现了旧快照仍进入 `apply_runtime_projection` 的问题。现在所有投影在短 `BEGIN IMMEDIATE` 事务内先检查版本，再写 JSON；Worker 仍检查 fencing/lease。独立连接验证写锁覆盖 JSON 提交，过期快照不写 Incident，较新 Checkpoint 保持待投影。历史 Evidence 保留采集时语义，未改变 TTL 或把历史观测提升为当前事实。
2. 补证失败出口绕过安全检查。确定性测试在补证 I/O 后同时提交 message、pause、cancel，返回 `tool_error` 时原程序错误完成。将最终安全检查放入 `_finish`，覆盖所有补证出口；失败仍保留已保存结果及证据缺口，控制命令在完成前处理。没有更改终态 Session 拒绝普通 message 的规则。
3. 两项旧 Runtime CLI 测试遗漏 `--remediations`，会使用真实数据默认路径。测试增加临时路径；新增自动 Store 构造保护，在打开项目真实 data 目录之前失败。未删除历史数据或验证快照。

## 实际测试

- 修复前，旧快照屏障测试失败；补证失败同时提交 message/pause/cancel 的三项测试失败，状态为错误的 COMPLETED。
- 最新 Runtime 修复的聚焦测试：17 passed，包含 pause/cancel/message、租约失效、cleared、主动让出，以及成功/失败补证两组。检查状态、审计事件、attempt/model/tool 计数和恢复；保存结果后恢复无需额外模型请求，message 后需要重新推理。
- 第一轮完整回归：569 passed、2 failed、2 skipped、190 subtests passed。两项失败均由新增真实数据保护拦截旧 CLI 测试，随后补齐临时路径；对应两项重跑为 2 passed。
- 较早的四个相关测试文件运行：83 passed、25 subtests passed；此运行早于最终补证失败出口修复，不能替代最终完整回归。
- 完整收尾版本会与只读 API 一起再次执行完整回归，并通过 GitHub Actions 检查实际提交。

## 修改边界与限制

复用现有 RuntimeStore、IncidentStore、Worker 和 outbox。保留 CAS、fencing、幂等投影与 JSON/SQLite 独立提交的崩溃恢复语义；不增加依赖，不改变 7/13 attempt 预算、状态机和正式修复审批。

JSON 写入期间会短暂持有 SQLite 写锁；模型/工具 I/O、提案生成和修复动作不进入该事务。这适用于当前本地单 Worker 架构，未进行断电、长时间压力或分布式验证。

本阶段没有真实模型调用、故障注入或修复执行。真实 `runtime.sqlite3`、`incidents.json`、`remediation.json` 的存在状态/哈希以及已有修改受到保护，验证结束后再次检查。
