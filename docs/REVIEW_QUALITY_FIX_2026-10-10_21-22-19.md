# 独立审查后的局部质量修复与运行语义

核查基准：`f07862ccb92e8f53813c43c40eb5530a52194fb7`，开始时本地 HEAD、分支 main 和远端 main 一致。
本次根据现有代码、隔离复现和真实子进程测试核实审查结论；审查报告自身属于静态审查，没有实际运行测试。

## 审查结论核实

| 审查判断 | 实际核实结果与处理 |
| --- | --- |
| 提案生成失败缺少计数、类别、查询与通知 | 成立。增加 Checkpoint 字段、workflow/CLI 展示、有限重试和通知。修复 Store 读取失败会遮蔽整个 workflow 的问题。 |
| 永久每 30 秒重试 | 推导不成立。原版本证据 TTL 为 15 秒；原代码在 31 秒后的复现中清除了 `remediation_pending`，但调查仍为 COMPLETED、回答仍成功。这是失败被静默清掉的实际缺陷。 |
| `_finish()` 提前返回必然误报 LostClaim，并额外扣 attempt | 被六条边界测试证伪。正常让出、取消与 cleared 没有错误的 LostClaim；真实租约失效仍记录 LostClaim。保存最终回答后恢复完成，attempt_count 仍为 1，模型与工具计数没有新增。Worker、guard、lease、fencing 和 CAS 保持原实现。 |
| `already_remediated` 没有进行验证，应该把验证字段改成 False | 被真实 Executor 控制流与隔离测试证伪。该分支不停止或重启目标，但仍调用独立 verifier。补充准确的 CLI 字段与返回语义。 |
| 多 Incident 并发与唯一索引缺少针对性测试 | 成立。复用已有子进程 fixture，验证两个不同 Incident 竞争全局执行权、过期后的 fencing、旧进程写入被拒绝，以及数据库唯一索引拒绝同 Incident 第二个非终态 Session。 |
| 完成时重复调用 `plan()` | 实测为两次、版本工具调用两次、工具总量 5 次。前一次判断历史候选是否值得补证，后一次在重新确认实例后重新校验有效证据。没有实测瓶颈依据；本次保留两次规划。 |
| 版本 TTL 未过期就可以省略强制重采 | 不采纳。测试在同一时刻改变 SHA、进程启动身份或故障开关，均阻止旧证据产生提案。TTL 不能替代创建提案前的实例确认。 |
| data 存在与 gitignore 矛盾 | 不成立。`git ls-files data` 为空，只跟踪 `.env.example`；被忽略文件仍然可以存在。本次保留现有快照、历史报告和运行数据。 |

## 实际修改位置与行为

- [Checkpoint](../investigation/models.py)：新增可选的 `remediation_attempts`、`remediation_error_category`、`remediation_blocked`、`remediation_blocked_reason`，具有兼容旧记录的默认值。
- [Manager](../investigation/manager.py)：提案尝试次数、错误分类、退避和待处理状态在同一次 SQLite 确认事务中保存；workflow 查询即使修复 Store 损坏，也返回已保存的调查结论及明确的 Store 错误类别。
- [RuntimeStore](../investigation/store.py)：已完成业务投影但提案被阻塞的 Session 退出自动投影队列，待处理状态继续保存在 Checkpoint，不重复高频唤醒。
- [Scheduler](../investigation/scheduler.py)：复用现有通知队列。首次失败、达到三次失败、进入 blocked 和实际恢复时展示相应状态；相同状态跨重启去重。输出失败仍保留待通知记录。
- [Runtime CLI](../runtime_cli.py)：`show-session` 展示完整结果、错误类别、累计提案尝试、重试时间和阻塞原因；`list-sessions` 增加 `remediation` 概要。查询返回 0 表示查询成功，不表示提案生成或修复成功。
- [Executor](../remediation/executor.py) 与 [Remediation CLI](../remediation_cli.py)：保持 ActionResult 返回接口，补充本次调用的验证 ID，CLI 根据该 ID 查询已保存的 VerificationResult，避免把后来的人工核查当作本次验证。
- [pytest.ini](../pytest.ini)：只设置 `testpaths = tests`，从根目录默认收集当前测试，保留显式指定其他路径的 pytest 能力。

### 提案失败与调查完成分别呈现

调查结果继续通过 `session.status = COMPLETED`、`investigation_result` 和 Incident 历史展示。提案失败不写入 Session 的 `last_error_category`，不增加调查连续失败次数，不清除已保存回答。

提案状态另行展示：

- `remediation_status = waiting_retry`：提案尚未完成，下一次重试时间可见。
- `remediation_status = blocked`：提案尚未完成，需要人工核查，自动队列不再重试。
- `remediation_status = not_pending`：没有待处理的提案生成任务；是否已有 Proposal 以 `remediations` 及其状态为准，该值不承诺修复已成功。
- `remediation_store_error_category`：本次读取修复 Store 失败的安全类别；此时空的 `remediations` 不能作为“没有 Proposal”的证据。

只持久化异常类名或固定白名单错误码，不写入原始异常内容。`remediation_error_category` 保留最后一次提案生成错误，阻塞原因单独展示。

临时锁或写入故障使用 `min(300, retry_base_seconds * 2 ** (attempts - 1))` 的退避。当前基础间隔为 5 秒，最多 6 次提案尝试；成功后清除失败与待重试状态，累计次数保留。权限错误、无效数据、损坏或无法读取的 Store，以及未知受控错误进入人工处理。

版本证据过期时阻塞为 `evidence_expired`；重新规划已不满足提案门槛时阻塞为 `evidence_no_longer_eligible`。不通过延长 TTL、复用旧实例身份或额外模型调用完成重试。实际证据有效期可能先于次数上限阻止重试。

blocked 的含义是人工待处理，当前查询与通知不会给已完成 Session 自动补证或重新授权预算。操作员应核查 Store 锁、权限、数据完整性和目标当前状态；取得新且有效的证据后，仍须遵守正式提案和审批流程。直接编辑 Checkpoint 或重置计数不能代替这些步骤。

### `_finish()` 的六条边界路径

| 补证期间事件 | 当前返回状态与事件 | Checkpoint 与下一次 Worker |
| --- | --- | --- |
| 租约失效 | RUNNING，`worker_claim_lost`，等待合法 recovery | 保存的最终回答保留；未可靠保存的补证结果不写入。恢复后完成，attempt_count 仍为 1。 |
| HumanMessage 到达 | QUEUED，`human_message_applied`、`human_context_changed_before_completion` | 保存人类线索及已有观测；旧结论因上下文变化失效，下一 Worker 重新形成结论，继续同一 attempt。 |
| 用户取消 | CANCELLED，`investigation_cancelled` | 已保存结果保留，不再 claim。 |
| Incident cleared | CANCELLED，`alert_cleared` | 已保存结果保留，不再 claim。 |
| 主动让出 | QUEUED，`worker_shutdown_at_safe_point` | 已保存最终回答保留，下一 Worker 完成，无新增模型请求。 |
| 已保存回答但补证尚未保存 | 真实过期时仍为 RUNNING，遵守 LostClaim | 后续 recovery 使用已保存结果，不伪造补证输出，不退还已经预留的工具预算。 |

## Remediation CLI 返回语义

`execute` 的退出码是流程结果，不能代替动作与验证字段：

- **0**：已获批流程已满足。本次验证通过，或该 Proposal 历史已经 SUCCEEDED，本次幂等跳过。
- **1**：动作失败或验证失败，流程没有满足。
- **2**：拒绝执行、无效命令/输入、Store 错误或本次验证记录无法确认。不能据此推断先前外部副作用不存在。

其他查询、审批、拒绝、人工核查和收口命令的 0 表示命令完成或记录/查询成功，具体健康检查结果仍读取其返回字段。参数解析失败沿用 argparse 的退出码 2。

| 本次 execute 情况 | action_performed | verification_performed | historical_proposal_succeeded | current_target_verified | 退出码 |
| --- | --- | --- | --- | --- | --- |
| `already_completed` | false | false | true | null | 0 |
| `already_remediated`，独立验证通过 | false | true | false | true | 0 |
| 实际动作与独立验证均成功 | true | true | false | true | 0 |
| 动作失败，明确未执行 | false | false | false | null | 1 |
| 动作结果 unknown | null | false | false | null | 1 |
| 实际动作完成，验证失败/异常 | true | true | false | false | 1 |
| `already_remediated`，验证失败 | false | true | false | false | 1 |

`execution_outcome` 保留 `executed / not_executed / unknown` 三态。`action_performed = null` 表示不能确认，不能按 false 处理。`target_already_remediated` 仅在本次命中 already_remediated 且独立验证通过时为 true。历史 SUCCEEDED 不证明当前健康。

返回的 ActionResult.details 包含本次验证引用，物理动作与独立验证仍分别存放在既有 actions / verifications 记录中。人工核查、历史成功、动作成功和验证成功不能互相替代。

## 恢复、预算和能力边界

恢复默认是**证据级上下文重建**：从持久化的结构化观测、工具结果及人类线索重建上下文，不承诺恢复未保存的模型响应或模型内部推理过程。未完成的只读调用可在新的调查路径中按需要重采，不保证原 pending 队列逐项续跑；不会伪造丢失的 function_call_output。

模型和工具预算在外部 I/O 前预留并持久化。崩溃、超时、响应未保存或租约失效后，已消耗/已预留预算不会自动退还；重新请求或重采仍会占用预算。普通租约恢复不会自动增加 attempt，真正的新尝试仍由现有规则决定。

单次 7 次模型请求、13 次工具调用，同 Incident 自动累计 3 次尝试、21 次模型请求、39 次工具执行及原有恢复/时间限制不变。本次只限制提案生成/写入重试，不改变修复动作执行、人工审批或已有人工重试预算。

目前自动提案与受控动作仅支持固定 Demo 场景：order-service、指定 prometheus-demo 仓库/app.py、固定故障开关及异常证据、固定 disable_demo_regression 动作。这不是通用自动修复框架；Proposal 始终需要正式人工审批，Runtime 不批准或执行动作。

SQLite `PRAGMA user_version` 和 Checkpoint.schema_version 均保持 1，没有表、列或索引结构迁移。四个新增 Checkpoint 字段都有默认值，新程序能读取旧记录；读取不自动重写旧 Checkpoint。旧程序使用 `extra=forbid`，因此不保证旧程序可以读取新程序写入的扩展记录，回滚时须考虑这一限制。Remediation 记录 schema 未修改。

## 实际验证

所有 Store、日志、锁和测试资源位于独立临时目录；模型、工具和物理修复动作使用 Fixture/Mock。并发验证使用真实子进程、真实 SQLite、文件屏障与实际唯一索引，没有调用收费模型或真实工具服务。

| 验证 | 实际结果 |
| --- | --- |
| 修改前完整 tests 回归 | 528 passed，2 skipped，190 subtests passed，193.46 秒 |
| 修改前六条 finish 路径、规划/身份及并发断言 | 12 passed；新增可观测性期望复现 2 failed（字段缺失、workflow 被坏 Store 阻断） |
| 新增局部回归 | 32 passed，35.74 秒 |
| 根目录默认收集 | 562 项，全部位于 tests；不收集 temp_test 或 data 中旧快照 |
| 当前工作区完整回归 | 560 passed，2 skipped，190 subtests passed，240.34 秒 |
| 基准 HEAD 加本次文件的独立提交快照 | 560 passed，2 skipped，190 subtests passed，236.47 秒 |

两项跳过分别是 CI/隔离安装专用的依赖检查，以及需要显式 ALERT_RULES_PATH 的可选跨仓库规则验证。持续失败/退避上限测试仅在 Fixture 中延长动态 TTL，以隔离调度逻辑；另有生产 TTL 原值下证据过期的回归，生产 TTL 没有修改。

回归入口：`& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest -q -rs --disable-warnings`。新增测试见 [审查核实及 Runtime 回归](../tests/test_review_quality.py)、[CLI 语义回归](../tests/test_remediation_cli_semantics.py)。

未运行真实故障注入、真实修复动作、真实模型调用、生产服务变更、断电实验或长期压力测试。现有审批、unknown、幂等、事务和预算安全测试全部继续通过。

Git 开始时无暂存改动；用户原有 18 个修改、未跟踪或删除记录逐文件校验保持不变，包括 Prompt、README、AGENTS.md、临时脚本、文档整理与独立审查报告。历史文件没有改写或清理。本次没有新增依赖、服务、事件总线、调度框架或 Manager/Worker 拆分。

本报告记录本地与独立快照的实测结果。最终提交的 GitHub Actions 结果以该提交实际 push run 为准，独立安装检查由现有 Windows / Python 3.12 CI 执行。
