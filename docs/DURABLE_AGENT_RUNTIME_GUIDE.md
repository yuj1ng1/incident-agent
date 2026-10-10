# Durable Agent Runtime 使用指南

本次按用户指定保留固定文件名。运行数据中的时间统一为带时区的 UTC；本文不属于业务模型提示词。

## 1. 架构与权威数据

```text
Prometheus → IncidentMonitor → InvestigationManager → SQLite RuntimeStore
                                  ↓                     ↑
                         Scheduler → Worker → Agent → ToolManager → 只读客户端
                                  ↑
                      runtime_cli / 未来的 Web 应用服务

Worker 的有效证据 → 原有 RemediationPlanner → RemediationStore
                                              ↓
                            remediation_cli approve/reject/execute/verify/audit
```

Incident 表示告警与调查历史；InvestigationSession 表示一次具有累计预算的可恢复任务。把任务独立出来后，等待人类、暂时失败和进程退出都不必丢弃调查或停止监控。

IncidentMonitor 继续负责 AlertEvent、AlertPolicy、Fingerprint、候选关联与告警解除，只持久化观测和提交任务。旧同步调查入口已经移除。Worker 负责执行时序和持久化，Agent 继续负责模型选择、提示词、响应解释；ServiceRegistry 和既有工具保持服务能力的唯一配置来源。

SQLite 是本地任务、命令、Checkpoint、问题、预算和执行权的权威来源。原有 IncidentStore JSON 仍是 Incident 的权威来源，RemediationStore JSON 仍是审批、动作和审计的权威来源。没有把整个 Incident 再复制到 SQLite。没有新增第三方生产依赖。

同一 Incident 只能有一个非终止 Session。COMPLETED 完成投影后按既有 Policy、最小间隔、冷却和恶化条件再调查；FAILED 仅允许已分类的 `model_empty_answer`、`model_invalid_call_ids` 自动再调查，退避使用 Policy 的 retry_seconds 按连续失败次数倍增，且受原有失败上限约束。其他 FAILED、EXHAUSTED、CANCELLED 必须人工 `reopen`；PAUSED、WAITING_HUMAN 等状态继续使用原 Session。告警解除后再触发由原有 IncidentStore 创建新的 Incident，历史保持。

同一 Incident 的自动 Session 共享一次授权预算，计数从 Session 的权威字段汇总。只有持久化的人工 `reopen` 可以建立新的授权范围；COMPLETED 只有原授权预算耗尽时才能人工重开。拒绝原因保存在 `reinvestigation_refusal`，变更时记录 `reinvestigation_rejected` 事件，重复轮询不重复写入同一拒绝事件。

## 2. 文件职责

| 文件 | 职责 |
|---|---|
| `../investigation/models.py` | 严格 Session、预算、命令、问题、工具观测、Checkpoint 校验 |
| `../investigation/state.py` | 状态转换、预算和错误代码 |
| `../investigation/store.py` | SQLite schema、事务、CAS、claim、Checkpoint 与事件 |
| `../investigation/checkpoint.py` | 体积限制、敏感内容保护、消息链校验与恢复策略 |
| `../investigation/evidence_policy.py` | 服务/实例隔离、缓存键和证据 TTL |
| `../investigation/manager.py` | 入队、消息、命令、HumanRequest、恢复对账、工作流查询 |
| `../investigation/worker.py` | 单 Worker、调用前预算、调用后保存、安全点与有限重试 |
| `../investigation/scheduler.py` | 独立后台调度与协作式退出 |
| `../runtime_cli.py` | 本地操作员入口 |
| `../storage_lock.py` | 两个 JSON Store 共用的 OS 文件锁 |
| `../scripts/validate_durable_runtime.py` | 显式启用的本地真实模型/工具验收 |

直接执行 `main.py` 时，由 `main()` 启动一个 Scheduler 线程和原有监控循环，退出时停止 Scheduler 并关闭模型客户端；不引入分布式队列。普通导入 `main`、`runtime_cli`、`remediation_cli` 不加载 dotenv 或业务依赖、不构造客户端、不开线程、不读写 Store。dotenv 在明确启动后、配置与工具导入前加载；已有客户端按调用延迟读取凭据的机制保留。`Agent.request_durable_response` 与 `ModelClient.call_once` 提供受控的单次请求入口，旧 `Agent.investigate` 已移除。Evidence 的采集时间、Session 和 observation ID、InvestigationRecord 的 Session ID 均为可选字段，旧 JSON 可以继续读取。

## 3. SQLite 与事务

默认路径为 `data/runtime.sqlite3`，数据库及 journal/WAL/SHM 均被 Git 忽略。schema version 使用 `PRAGMA user_version=1`；只允许新的空数据库从 0 初始化。未知高版本、存在表但没有版本、损坏数据库均报错，不重置文件。

| 表 | 唯一性与用途 |
|---|---|
| investigation_sessions | Session 主键；非终止状态的 incident_id 部分唯一索引 |
| investigation_checkpoints | Checkpoint 主键与 Session 外键；保留最近有限快照 |
| investigation_commands | command_id 唯一；sequence 提供持久化提交顺序 |
| human_messages | command_id 唯一；明确 consumed_at |
| human_requests | request_id 唯一；每个 Session 最多一个 pending |
| investigation_events | event_id 唯一；sequence 提供事件顺序 |
| runtime_metadata | 全局模型阻塞、再调查拒绝及待投影/待通知索引；业务状态仍以 Session/Checkpoint 为准 |

Session 的 JSON 数据经过 Pydantic 校验；status/state_version 为索引投影，读取时检查它们与正文一致。执行权和预算放在 Session 内，不另建重复状态表。

每次写事务使用独立连接、`BEGIN IMMEDIATE`、外键、5000ms busy timeout、`synchronous=FULL` 和参数化 SQL。第一版使用 DELETE journal，没有默认启用 WAL。网络调用发生在事务之外，CLI 不必等待模型完成才能写消息。状态、预算、Checkpoint、消息消费和相关事件在各自事务内一起提交。

只读查询使用 `mode=ro`、`query_only=ON` 和短 `BEGIN` 快照，不申请 RESERVED 写锁。`list_sessions` 一次 SELECT 返回所需行，支持状态与 Incident 条件；恢复只查询非终态 Session、pending 命令及待投影工作，通知只查询待通知工作。旧 v1 数据库首次启动会回填这两类持久化索引，此后不在每个 tick 重读所有历史 Checkpoint。添加索引及 metadata，不修改表、列或 schema version；DELETE journal 与 FULL 持久化设置保持。当前只支持同机单 Worker，SQLite 文件不要放在网络共享盘。

## 4. Session 状态机

| 状态 | 含义及主要出口 |
|---|---|
| CREATED | 已建立任务；进入 QUEUED 或取消/失败 |
| QUEUED | 等待 claim；可运行、暂停、取消、耗尽或失败 |
| RUNNING | 唯一有效 Worker 执行；可等待、终止，或安全释放回队列 |
| WAITING_RETRY | 等待有限退避时间，到期入队 |
| WAITING_HUMAN | 等待一个持久化问题的有效回答，不调用模型 |
| PAUSED | 人工暂停、模型认证/余额阻塞或问题过期；显式 resume |
| COMPLETED | 调查正常完成，不代表业务恢复或修复已经执行 |
| EXHAUSTED | 累计/尝试预算耗尽 |
| FAILED | 当前调查失败；错误分类、Policy 和共享预算决定是否允许新的有限调查 |
| CANCELLED | 停止后续调查，保留 Incident、证据和审计 |

最后四种是终止状态，不允许 resume。普通 resume 只接受 PAUSED；WAITING_HUMAN 通过有效 answer 恢复，WAITING_RETRY 由调度器到期恢复。暂停期间可以回答仍未过期的问题，答案先保存，任务仍保持 PAUSED，直到显式 resume。

`reopen` 是另一条明确的人工授权命令，必须提供操作者、非空理由和稳定命令 ID。它保留原终态 Session，建立一次有上限的新调查授权，不重用旧证据。处理前重新读取完整的 Prometheus 告警快照，核对 fingerprint、服务、firing 状态和观测时效，并在提交前再次核对 Incident 活动状态。校验失败会持久化拒绝；重复相同命令返回原结果，冲突拒绝。原/新 Session 的审计包含命令、理由、先前错误和计数、重新授权限制与当前告警观测时间。本地 actor 是审计身份字段，实际访问权限依赖操作系统；没有新增公网认证。

Remediation 的 pending/approved/executing 等状态属于原有独立工作流。Session 可以 COMPLETED，同时 Proposal 为 pending。

## 5. Checkpoint 和六个崩溃边界

Checkpoint 保存版本、执行阶段、已完成工具观测（采集时间、参数、call_id、缓存键、实例身份）、工具 trace、必要模型消息、未执行调用、已交付的人类线索和预算预留信息。它不保存修复执行权限。

| 崩溃位置 | 恢复语义 |
|---|---|
| 模型调用前 | 已预留预算仍计入；不知道是否发出请求，不把它当完成 |
| 收到模型响应但未保存 | 响应无法可靠恢复；预算保留，重建上下文 |
| 模型请求工具但工具未执行 | 未执行调用可放弃并记录；不补造 output |
| 工具返回但未保存 | 不声明成功；只读调用可以在剩余预算内重查 |
| 工具结果已保存、模型未继续 | 保留 observation；新模型上下文使用仍有效证据，缓存可避免重查 |
| Checkpoint 已提交后立即退出 | 从已提交快照恢复；可靠保存的结果不丢失 |

默认采用 at-least-once 和有限重放。每次外部调用前预留预算，无法确认的调用可能保守多计一次；预算是可靠的上界，不宣称外部 HTTP exactly-once。未知工具结果没有 observation，更不会伪造成功。

## 6. Responses 上下文恢复

实际环境使用 OpenAI Python SDK 3.14.0、SQLite 3.53.4 和 `deepseek-flash`。不同供应商的 Responses 兼容性必须独立检查。DeepSeek 文档说明服务端无状态、`previous_response_id` 不支持，且 reasoning 支持方式与 OpenAI 不完全相同：[DeepSeek 官方兼容说明](https://api-docs.deepseek.com/zh-cn/guides/responses_api/)。工具 output 必须对应真实 call_id；OpenAI 推理模型的工具消息链还需要保留相应 reasoning items：[OpenAI 官方 function calling](https://developers.openai.com/api/docs/guides/function-calling)。

实现了两种策略：

- A：`valid_chain` 检查顺序、唯一 call_id、JSON 参数、完整 output 配对；只有 `Worker(provider_verified=True)` 且消息链完整、动态证据仍有效时才使用完整链。默认关闭，不把“供应商支持 Responses”当作每个 Checkpoint 都可精确续接。
- B：默认开启。使用当前 AlertEvent、Policy、Registry、有效工具观测、最近调查摘要和 human-provided context 重建模型轮次。未执行的只读调用会记录为放弃，不向新输入塞入缺少 output 的 function_call。B 不是原推理的精确继续。

程序内同时记录消息链供审计，正常轮次默认也用 B，避免语法上完整但已经过期的工具结果继续冒充当前事实。输入做体积限制，历史结论不是新证据。

## 7. Inbox、安全点与主动提问

HumanCommand 包含 command_id、Session、actor、类型、payload、UTC 创建/处理时间、处理状态、结果和可选 expected_version。先落库，再处理。相同 ID 和相同内容返回原结果，ID 内容冲突拒绝。

运行中的命令由 Worker 在模型返回后、下一工具前、工具完成后和下一模型轮次前交付。已发出的 API 请求不能被后来消息改写。改变调查方向时可以放弃尚未开始的只读调用，保留已保存有效证据，重新构建 B 上下文。

同一批 pending 命令中 cancel 优先，其余按提交 sequence 处理。取消后其他命令拒绝。CLI 可用 expected_version 拒绝过期状态操作。消息消费、Checkpoint 和消费事件同事务提交；崩溃后已消费消息仍在 Checkpoint，不会消失。

人类消息和答案始终标记为 `human_provided_unverified_context`。它们不进入 Evidence，也不能增加工具权限、预算或批准修复。actor 只是本地审计字段，本阶段没有公网身份认证。

模型可调用 Runtime 专用 `request_human_input(question)`，此入口不注册到通用修复系统。问题、Checkpoint 和 WAITING_HUMAN 同一事务保存，Worker 释放执行权。默认同一 Incident 授权范围最多 3 个问题、每 Session 同时最多 1 个 pending、1800 秒过期；过期后 PAUSED，旧答案不能恢复任务。answer 需要有效 request_id，重复新 ID 回答拒绝，相同 command_id 可安全重试；cancel 使未完成请求失效。

## 8. 预算、重试与公平调度

单次尝试仍为 7 次模型请求、13 次实际工具执行。Runtime 的 SDK 调用关闭 SDK 内部重试，不叠加原有 ModelClient 的 legacy 重试。请求失败、超时、不完整响应和实际失败工具均计入预算，缓存命中单独计数。

集中配置位于 `../config.py` 的 RUNTIME_LIMITS：3 次尝试、21 次模型请求、39 次工具执行、8 次恢复、3600 秒总时长，数值未改变。同一 Incident 一次授权范围内的所有自动 Session 共用这些限制，时间从该范围最早 Session 创建时起算。暂停、等待、进程退出和自动新建 Session 都不会清零计数。第 8 次获准恢复仍可以运行，第 9 次恢复在增加计数之前被拒绝。问题也有独立提问上限。人工 `reopen` 明确重新授权同样的固定上限，不接受任意预算数值，不授权修复动作；模型全局阻塞不会因此解除。

401/402/403 持久化为 PAUSED 和全局模型阻塞，停止其他自动模型请求；操作员处理原因后，先显式 `unblock-model --actor 操作员` 解除全局阻塞，再对暂停 Session 执行 resume。resume、cancel、reopen 本身都不解除全局阻塞。429、临时 5xx、连接失败和 incomplete 有限进入 WAITING_RETRY，5/10 秒指数退避，最多 3 次尝试。非法输入/工具调用不能无限重试。工具 no_data 是证据缺口，模型可以选择其他工具或在剩余预算内重新查询；没有额外隐藏的客户端重试层。

调度使用持久化 queued_at：通常 Critical 优先、同级 FIFO；排队达到 120 秒的任务进入最老优先队列，避免 Warning 永久饥饿。调度不抢占已经发出的请求。等待人类、暂停、等待重试均不占 Worker。

claim 在 SQLite 事务内建立 owner token、单调 fencing token 和 120 秒 lease；所有 Worker 写入检查当前 owner/fence/lease。跨 Session 也只允许一个未过期 Worker claim。过期旧 Worker 即便拿到外部响应也不能覆盖新结果。安全点续租；模型 SDK 设置 60 秒超时，既有 HTTP 工具保持 5 秒等本来超时。超时不是任意第三方网络故障下的硬实时保证。

## 9. 证据时效与审批边界

缓存键包括 Session 服务、工具、参数、可信 Profile 配置及必要的 SHA/进程启动身份；依赖 Profile 也进入缓存键。不同服务严格隔离。恢复时强制重新探测服务运行身份；运行身份无法确认时不继续沿用旧动态身份。源码和 diff 只有参数为完整 40 位 SHA 才稳定。

| 观测 | TTL |
|---|---:|
| HTTP/PostgreSQL 指标、健康、依赖健康、运行版本 | 15 秒 |
| 近期日志 | 30 秒 |
| 最近提交 | 60 秒 |
| 能力/依赖配置描述 | 300 秒 |
| 完整 SHA 的源码/diff | 不按动态 TTL 过期；当前因果判断仍须匹配运行 SHA |

no_data 和失败不缓存为成功证据。过期观测保留在历史记录，但从当前事实中排除。正常完成前，如果原有确定性 Planner 能形成历史候选且还有预算，Worker 刷新运行身份及过期的关键动态观测；预算不足时记录证据缺口，不创建不安全 Proposal。

正式提案仍由原有 Planner 和 RemediationStore 创建、去重。普通 message、answer、调查恢复都不能 approve 或 execute。唯一固定动作仍为 `disable_demo_regression`，没有 shell、restart、rollback 通用工具。执行继续检查审批 digest、目标、进程、版本、参数、过期和审计。已有目标身份去重防止相关 Incident 重复执行。

调查 JSON 投影和修复提案写入采用幂等 outbox：SQLite 保存结果 → JSON 按 Session/observation ID 合并 → SQLite 确认。JSON 先成功而确认失败时，下次合并不会重复追加调查。修复存储暂时失败记录事件并有限延迟对账，不能阻塞其他调查；过期证据不会因此补造提案。

## 10. 启动、操作与崩溃恢复

在项目目录使用已验证的 agentlearn 环境：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B main.py
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py list-sessions
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py unblock-model --actor 本地操作员
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py show-session ses-实际ID
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py show-incident inc-实际ID
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py show-events ses-实际ID --limit 1000
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py send-message ses-实际ID --actor 本地操作员 --command-id 调查方向001 --text '先检查数据库连接情况。'
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py pause ses-实际ID --actor 本地操作员 --command-id 暂停001
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py resume ses-实际ID --actor 本地操作员 --command-id 继续001
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py cancel ses-实际ID --actor 本地操作员 --command-id 取消001
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py reopen ses-实际ID --actor 本地操作员 --reason '故障原因已处理，重新授权有限调查' --command-id 重开001
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py list-human-requests
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py answer-human-request ses-实际ID --request-id req-实际ID --actor 本地操作员 --command-id 回答001 --text '部署背景线索，仍需要工具验证。'
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py list-pending-remediations
```

`--store`、`--incidents`、`--remediations` 是显式的本地操作员路径选项，放在子命令前；模型不能选择这些路径。原有 `remediation_cli.py approve/reject/execute` 语义不变。批准不等于执行。

重启 `main.py` 即加载任务；未过期 claim 不被强夺，崩溃后最多等待剩余 lease。启动对账检查 Incident 是否仍存在/告警是否已解除、预算、问题过期、已提交结果和未消费命令，也补回“Incident 已存但任务尚未建立”的合法调查。不要删除 SQLite 来解除暂停或重置预算。告警解除仅取消不需要继续的任务，不宣称业务已恢复。

Ctrl+C 停止监控并让 Worker 在下一个安全点保存后释放回队列；不会杀死未知线程/进程，也不会撤销已发生的外部操作或独立修复事务。

## 11. 数据保护与保留策略

单份 Checkpoint 最大 4 MiB，工具 payload 最大 64 KiB，超出返回明确 no_data；单字符串限制 65536 字符，当前证据上下文约束在 60 KiB，最近调查摘要限 3 条。每 Session 最近 4 个 Checkpoint 自动轮替；已有可靠观测保存在最新快照和 Incident 中。命令最多 100 条、人类文本总量最多 64 KiB。

凭据字段、配置中实际凭据值、Bearer 和敏感日志字段脱敏；源码脱敏保留变量引用与语法，不把变量名误认成密码值。脱敏不是对任意自由文本所有秘密的形式化证明，本地数据目录仍应限制 OS 访问权限。完整 trace 不进入 Git。

IncidentStore 的告警生命周期字段及 active_by_fingerprint 是权威状态；Session 状态/计数和 Checkpoint 在 SQLite 中权威保存，JSON 内调查状态、失败次数及 Runtime Evidence 是投影。没有 Session ID 的旧 Evidence/调查记录只有 JSON 保留，继续留在热文件，不根据推测删除。

写入时默认只在 `incidents.json` 保留最近 3 次 Runtime 调查及其证据，更早内容完整写入同目录 `history/<sha256>.json`，热文件保留归档引用。归档先原子写入、fsync、校验身份/结构/校验和，再提交 JSON manifest；失败保留原记录，崩溃可能留下未引用的归档文件，不自动清理。旧 JSON 不在启动时批量迁移，仅在正常保存对应 Incident 时归档。`get_incident` 等公开查询默认恢复全部历史，`runtime_cli show-incident` 同样包含归档；监控和调查上下文显式使用热记录。缺失或损坏归档会报错，不返回伪造的空历史。修改已归档内容被拒绝。

Checkpoint 轮替删除的是中间快照，最新快照仍保存累计 observations、trace 和可靠结果；事件补充 checkpoint ID 与 observation ID 以关联历史。不能恢复已经删除的每一个中间模型请求原文，也不能重建未保存响应。历史查询以保留的最新 Session 快照及 JSON/归档为依据。

Session、命令、问题、结构化事件和归档文件默认长期保留，不自动删除调查/审批历史。此策略限制热文件的证据体积，并不限制整个数据目录总容量；归档引用及审计仍随历史增长。备份须包含完整 data 目录（尤其 incidents.json、history、runtime.sqlite3 与 Remediation 文件），并在进程停止时取得一致快照。没有按天自动清除审计或归档的功能。数据库不能通过自动清空来恢复损坏。

## 12. 未来 Web 接入与当前限制

Web 可直接复用 `InvestigationManager.enqueue/submit/get_command/requests/workflow/recover/sync_incident`，以及 RuntimeStore 的 Session 与 events 查询；Worker/Scheduler 独立运行。workflow 同时查询真实 RemediationStore，不建立第二套审批记录。未来必须添加实际认证、授权和请求限流，再把 Web 身份映射到 actor；不要通过 CLI 子进程控制业务。

当前没有 Web、跨机器调度、跨存储原子事务、强制终止正在发出的模型请求、exactly-once 外部 HTTP 或自动预算扩展。事故响应仍依靠有效证据与独立人类审批。测试范围与实际未验证事项见 [验收记录](DURABLE_AGENT_RUNTIME_VALIDATION.md)。
