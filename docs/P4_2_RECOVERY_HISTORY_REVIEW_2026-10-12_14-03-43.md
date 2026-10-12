# P4.2 历史解除事件补充审查与修复验证

审查基线：321ae1986c1308f3606c9c7c33f8eb3d48ff3409。以下结论来自本次源码检查、临时数据复现及实际测试输出。

## 已确认的问题

原 recovery_candidates() 将 recovery_verification 缺失或为 null 的已解除记录也作为候选；claim_recovery_verification() 会为这些记录现场创建任务。RecoveryWorker 首次扫描即可领取并调用注册业务探测。

原收据校验要求观测新鲜、晚于解除时间，并核对 Incident、服务、配置和租约，但没有区分历史解除记录与已经登记的待验证任务。最新 fingerprint 的历史记录因此可能把今天的业务成功关联到昨天的事件。

在临时 IncidentStore 中保存昨天已解除、没有恢复验证状态的记录，今天新建 Store 和 Worker。探测目标是隔离回环 HTTP 服务的 /order，服务返回满足业务契约的测试 JSON；没有调用真实 Demo 或数据库。

| 观测项 | 实际结果 |
| --- | --- |
| 解除时间 | 2026-10-11T03:48:44.300697+00:00 |
| 首次 Worker 扫描 | 历史 Incident 成为候选并被领取 |
| 实际网络请求 | GET /order，1 次 |
| 写入成功时间 | 2026-10-12T03:48:44.300697+00:00 |
| 两个时间的间隔 | 86400 秒 |
| 证据与状态 | recovery_evidence 新增 1 条，状态 verified |

修复前，新增的 8 项针对性检查实际失败；其中 v1/v2 存储格式与缺失/null 状态的四种组合均发出了真实 HTTP 请求。原有专项 82 项全部通过，说明原测试没有覆盖这次历史扫描边界。

## 最小修复

生产改动仅在 [incident_store.py](../incident_store.py)。[RecoveryWorker](../recovery/worker.py) 和 [RecoveryVerification](../models/recovery.py) 复用现有实现。

1. resolve_inactive_incidents() 在正常解除时，将解除时间和现有 RecoveryVerification 的 pending 状态放入同一次原子保存；仅在缺少状态时创建，不覆盖已有任务、预算或证据。
2. recovery_candidates() 只扫描已持久化的 pending、running 或待重试任务；缺少状态的历史记录默认不入队。
3. claim_recovery_verification() 同样拒绝没有持久化任务的记录，直接调用也不能隐式补验。首次领取一个 pending、没有尝试记录的任务时，才绑定并持久化可信 ServiceRegistry 策略。

不使用 Worker 启动时间或解除时间的固定窗口作为门槛，避免进程停机后误丢合法任务。没有新增公开接口、字段、schema version 或依赖；没有修改 Session 状态机、Remediation 审批、工具预算、Web API 或前端。

## 历史兼容性与时间含义

- v1/v2 历史记录仍可读取。缺少恢复验证状态的记录保持原样，重复扫描和直接领取均不补写成功时间或证据。
- 已有历史证据和已有 recovery_verified_at 保留，不删除，也不重新宣称符合新版收据校验。
- 升级时仍在 firing 的旧记录，之后由新代码正常解除时，可以登记 pending 并验证。
- 已有 running、失败待重试等持久化任务继续沿用原策略、租约、尝试次数及预算；崩溃后可在租约到期时接管，旧领取者不能写入成功。
- 新代码解除产生的 pending 任务，即使停机一天后才首次运行 Worker，仍可续跑。测试确认原 cleared_at 保持不变，receipt.completed_at 和 recovery_verified_at 记录探测当时的时间。

兼容性取舍：基线版本已经解除、但尚未领取且未留下持久化任务的事件，会被视为未登记的历史记录；即使解除时间很近，也不会根据时间猜测补验资格。本次没有新增历史补验策略或批量迁移。

恢复成功仅证明实际探测时满足注册业务契约，不证明原解除时刻已健康，也不推断业务真实恢复发生的时间。没有检查或修正真实数据中可能已由旧版本写入的历史成功记录。

## 测试与实际输出

环境：D:/conda/envs/agentlearn/python.exe，Python 3.12.14、Pydantic 2.13.5、Requests 2.34.2、pytest 9.1.1；前端使用 Node 24.15.0、npm 11.12.1。

| 验证阶段 | 实际结果 |
| --- | --- |
| 基线恢复验证专项 | 82 passed，11.94 秒 |
| 修复前新增边界检查 | 8 failed、85 deselected，4.06 秒；预期复现失败 |
| 相关回归：恢复验证、Incident 回归、多信号、Remediation 恢复 | 238 passed、1 skipped、35 subtests passed，51.76 秒 |
| 首轮完整 Python 回归 | 1 failed、846 passed、2 skipped、190 subtests passed，275.01 秒 |
| 修正新增测试同步方式后的恢复验证专项 | 96 passed，14.69 秒 |
| 最终完整 Python 回归 | 847 passed、2 skipped、190 subtests passed，252.20 秒 |
| 前端现有测试 | 4 个文件、38 项测试全部通过 |
| 前端类型检查与生产构建 | 通过 |

相关回归命令使用上述 Python 执行 -B -m pytest tests/test_recovery_verification.py tests/test_incident_regression.py tests/test_multi_signal.py tests/test_remediation_recovery.py -q --tb=short；最终专项执行 -B -m pytest tests/test_recovery_verification.py -q --tb=short。

最终完整测试在临时源码副本执行 -B -m pytest tests -q --disable-warnings。副本从上述基线提交导出，再覆盖本次两个源码/测试文件，排除工作区原有无关修改。运行前后及中断恢复后均核对两个文件哈希一致；这也是提交范围的业务代码快照。两个完整测试副本已自动清理。

两个跳过项为：仅在独立安装环境启用的固定依赖检查，以及需要 ALERT_RULES_PATH 的可选跨仓库告警规则检查。Windows 专用文件竞争测试已运行。

首轮完整测试唯一失败来自新增后台启动测试：测试用另一个 Store 实例在 Worker 原子替换文件时读取，出现 Windows PermissionError。测试改为通过重启后 Worker 所共享的 Store 实例读取，以使用现有线程锁；保留真实 HTTP、双 Worker、超时、请求次数和历史记录不变的断言。最终完整测试重新通过。

本次没有扩大生产 Store 的跨实例读取行为。线程锁仍以单个 Store 实例为范围；不能将这次测试同步修正解释为已经修复或验证了所有跨实例、跨进程读写窗口。

覆盖内容见 [恢复验证测试](../tests/test_recovery_verification.py)：昨天解除/今天首次扫描、缺失与 null 状态、v1/v2 格式、直接领取、正常解除原子登记、提交失败、跨日重启、后台双 Worker 启动、租约/崩溃接管、跨进程竞争与旧领取者迟到写入、重复轮询、重试次数、历史证据保留及已解除事件再次触发。

## 范围与限制

- 全部 Incident、Runtime、Remediation 测试数据均为临时数据；测试保护器拒绝打开项目真实 data 目录。没有运行真实 Monitor/Worker、故障注入、修复动作或模型调用。
- 真实网络验证仅指隔离回环 HTTP 传输；没有进行生产或 Demo 数据库联调。真实业务服务恢复情况未验证。
- 本次完整 diff 已检查，未引入新抽象、循环依赖或持久化迁移。旧的身份、配置、租约及证据新鲜度校验保留。
- 工作区原有 14 个修改或新增文件已与初始 SHA-256 核对一致；其他既有删除项保留，不纳入本次提交。README 等现有用户修改保持原样。
- 本报告记录本地验证；GitHub Actions 的最终结论未作为本次本地测试结果。
