# Incident Agent 持久化边界与调度修复验证

基准：`87a092c1091151bc345a02ecdb7724acd762e533`。开始时本地 HEAD、分支 main 和远端 main 一致，暂存区为空。已有文档整理、README、提示词、temp_test.py 和未跟踪 AGENTS.md 均保留，不纳入本次提交。

## 核查结论与最小触发条件

| 事项 | 结论 | 最小触发条件与修复 |
| --- | --- | --- |
| `_failure()` 的 LostClaim 导致 Scheduler 停机 | 已证实 | Worker 的只读调用或模型请求失败时租约已过期，并被另一个 Worker 接管；失败处理的 guard 抛出 LostClaim，原保护范围不能覆盖。现在整个执行、失败处理和投影边界均区分执行权丢失，停止旧 Worker 发布并记录审计事件。 |
| `_run()` 直接 LostClaim 已有保护 | 原行为已确认 | 原代码已经捕获此路径，不把它计为新增停机缺陷；补充丢失执行权的审计，并用两个实际 Worker 验证无旧证据写入。 |
| `sync_incident()` 普通版本竞争必然抛 LostClaim | 未证实该前提 | 当前基准已有 state_version 比较与提前返回。实际并发保存测试确认新 Checkpoint 不被 ack 或覆盖；保留此保护并新增 superseded 事件。Worker 发起的本地 JSON 发布增加 fencing 校验与保护。 |
| CAS 与租约丢失混用 LostClaim | 已证实 | 对过期 state_version 执行 Store.save 会被原来视为 LostClaim。现在意外 CAS 冲突使用 StateVersionConflict，属于一致性错误，保持 fail-closed。HumanCommand 的 expected_version 拒绝仍沿用原业务处理。 |
| `_run()` 数据库错误被转成普通调查失败 | 已证实 | 执行期间数据库错误落入宽泛 Exception 分支并调用 `_failure()`。现在 SQLite、Runtime 完整性、持久化校验错误向上传播；隔离 SQLite 在模型响应边界实际损坏时仍使 Scheduler 停机，且原字节不被替换。 |
| Incident 缺失却被 ack | 已证实 | 待投影 Session 对应的 Incident 不存在，apply_runtime_projection 原来返回 None。现在返回 MISSING，Checkpoint 保持 projected=False，进入可查询、可审计的隔离 outbox，不热重试。 |
| 归档 Session 未核实历史内容便被 ack | 已证实 | Session ID 出现在归档 manifest 时原来直接返回。现在核查归档文件、checksum、身份、结果和成功 Observation；缺失、损坏或内容冲突均 fail-closed。 |
| 重新授权快照 A 覆盖 Monitor 的 B | 已证实 | 新授权 Session 尚未开始 attempt，reopen_alert=A 的投影延后至 Monitor 已保存 B。现在保留 B 的值、时间戳、状态与 fingerprint；A 仅保存在 Checkpoint 和预算授权审计中。 |
| 无新业务内容仍重复投影 | 已证实 | RUNNING Session 已投影后只修改 phase/history 或租约记账，原写入重新置 projected=False，并通过 updated_at 触发 JSON 保存。增加业务内容水位线，保留终态、错误、新 Observation、人类上下文和修复补证变化的投影。 |
| A3 save_observation 缺锁并发读改写 | 缺锁前提不成立 | 原方法读取已位于 @locked_update 内。两个独立进程同时进行 30 次 Monitor 写入和 30 次 Worker 证据写入，最终保留全部 30 个 Observation 和最后监控字段；不修改锁或此方法。锁竞争按原 FileLockError 语义做有界重试。 |
| A7 FixtureAgent 无条件 success=True | 已证实 | 夹具返回 tool_error 或抛出 Python 异常时仍返回 True。现按真实 ToolManager 的失败语义返回 False，另用真实 ToolManager 执行失败函数验证预算已扣、失败结果已保存且不成为有效证据。no_data 的 Python 执行成功与证据无效仍严格区分。 |

这些测试证明上述具体边界，不把测试场景数量当作独立缺陷数量，也不宣称排除所有可能的并发风险。

## 最小实现及持久化影响

- 沿用 Worker、Scheduler、InvestigationManager、RuntimeStore 与 IncidentStore；没有新增第三方依赖或替换现有架构。
- RuntimeIntegrityError / StateVersionConflict 与 LostClaim 分离。丢失执行权只允许记录错误审计，不保存旧 Checkpoint、工具证据或 Incident。
- IncidentStore 明确返回 APPLIED、UNCHANGED、ARCHIVED、MISSING。只有前三种可靠结果才能使 projected=True；None 或未知结果不能被确认。
- 缺失 Incident 以 `blocked_projection:<session_id>`、Checkpoint 的 projection_blocked 和 incident_projection_blocked 事件保留隔离记录；从自动 pending 队列移出不代表投影成功。CLI workflow 可查询原因。解除隔离需人工核对数据后使用 Store 接口；本次未增加自动造数据、自动重试或解除隔离命令。
- Worker 发起的本地 JSON 发布在短 SQLite 事务中核对 fencing，防止接管与旧发布并发。模型、工具、Remediation 提案和外部动作均不在该保护事务中；JSON 提交和 SQLite ack 仍为两个提交点，崩溃通过现有 outbox 幂等对账。
- 水位线覆盖业务状态、错误、attempt、成功 Observation、终态结果及预算/trace、人类消息、授权快照和补证缺口；phase、Responses history、lease 和纯记账更新不会单独要求重写 Incident。Remediation pending 仍独立触发原有提案恢复路径。
- Session 状态枚举及合法转换表、7/13 attempt 预算、Incident 生命周期、HumanRequest 边界、Remediation 独立审批/白名单/UNKNOWN 禁止自动重试规则均保持原有定义。
- SQLite user_version=1、Checkpoint schema_version=1、Incident JSON version=2 和归档 schema_version=1 保持不变。Checkpoint 增加可选 projection_digest 与默认 False 的 projection_blocked，runtime_metadata 增加隔离 key；旧 Checkpoint 可由新代码读取，无需清空或重写真实数据。
- 旧程序对额外 Checkpoint 字段采用 extra=forbid，不能把此次字段扩展描述为可直接降级兼容；回退程序前需评估/恢复兼容的运行时快照。本次没有对真实库执行迁移或回退。

授权验证快照是授权证据，Monitor 快照是监控状态。这两个来源的时间与用途不同，不能通过覆盖其中一个来制造一致性。

## 重复投影计数

隔离数据库、可控时钟；先完成一次 RUNNING 状态投影，再进行 5 次仅 phase/history/lease 记账的 Checkpoint 写入。表内不计预热与创建 Session。

| 工作负载 | Checkpoint 写入 | 实际投影尝试 | Incident 完整 JSON 保存 | 新增 Observation |
| --- | ---: | ---: | ---: | ---: |
| 基准代码，5 次纯记账写入 | 10 | 5 | 5 | 0 |
| 修复代码，同样 5 次写入 | 5 | 0 | 0 | 0 |
| 修复代码，执行 1 次成功的只读工具并投影 | 3 | 1 | 1 | 1 |

Checkpoint 写入包括工具预约、工具结果和真正成功的投影确认。优化未略过终态失败、HumanRequest 答复、调查最终结果和 Remediation 所需数据。

## 验证记录

全部本地 pytest 使用 `D:\conda\envs\agentlearn\python.exe`（Python 3.12.14、Pydantic 2.13.5、pytest 9.1.1），启用 PYTHON_DOTENV_DISABLED=1、禁用字节码与 pytest cache，日志由原 conftest 隔离至临时目录。

命令：`python -B -m pytest tests -q --disable-warnings -p no:cacheprovider`，与现有 CI 的完整 tests/ 范围一致；不运行用户的独立手工 temp_test.py。

| 验证 | 实际结果 |
| --- | --- |
| 修复前原完整套件 | 504 passed, 2 skipped, 190 subtests passed，185.97s |
| 修复前第一组 16 项边界测试 | 12 failed, 4 passed；失败涉及已复现缺陷与缺失的审计/结果语义 |
| 首轮边界修复与崩溃夹具适配 | 22 passed，21.18s |
| 最终工作区完整套件 | 528 passed, 2 skipped, 190 subtests passed，186.54s；24 项新增测试全部通过 |
| 单独导出的提交代码快照完整套件 | 528 passed, 2 skipped, 190 subtests passed in 193.77s (0:03:13) |

本地两项 skip 原因：clean-install 的锁定依赖检查只在 CI/独立干净环境开启；跨仓库 AlertRules 检查需要显式配置 ALERT_RULES_PATH。本次没有启用 Demo 或真实外部服务来消除 skip。CI 结果按最终提交核实并随交付给出。

新增测试覆盖：两个实际并发 Worker 的执行权交接、失败处理失权、独立 SQLite 连接的投影版本竞争、真实 SQLite 文件损坏、CAS 与状态版本异常、Incident 缺失隔离与重启、有效归档确认、归档缺失/损坏/内容冲突、授权 A / Monitor B 时序、进程在 JSON 提交后直接退出、幂等恢复不调用模型、纯记账与真实 Observation 的四类计数、人类回答/最终结果、真实 ToolManager 失败及两个实际进程的并发字段保护。

既有测试全部保留。旧测试仅适配：CAS 预期类型从 LostClaim 改为 StateVersionConflict；投影包装夹具返回真实 outcome；归档冲突从静默跳过改为预期 fail-closed。另修正新测试把项目实际 investigating 状态误写成 open 的断言。这些调整没有弱化原有不丢数据或幂等目标。

未验证：真实模型/付费 API、真实外部系统故障、真实修复动作、真实运行库迁移、断电/磁盘故障、长期压力及其他同字段监控写入排序。本次没有启动 main.py、停止 Demo 服务、修改运行数据或读取/打印 .env 密钥；测试均使用临时存储、模拟模型和隔离进程。

## 相关源码与测试

[Worker](../investigation/worker.py)、[Scheduler](../investigation/scheduler.py)、[Manager](../investigation/manager.py)、[RuntimeStore](../investigation/store.py)、[Checkpoint 水位线](../investigation/checkpoint.py)、[IncidentStore](../incident_store.py)、[新增边界测试](../tests/test_persistence_boundaries.py)、[并发与崩溃子进程](../tests/projection_process_fixture.py)。
