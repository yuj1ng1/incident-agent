# Runtime 小范围质量修复验证记录

本记录针对 2026-10-09 的实际代码、独立审查发现及真实 Incident 观测。文件名时间取正文写入完成后的 Asia/Shanghai 时间；业务观测仍使用带时区 UTC。不修改历史报告及其原始测试结果。

## 基准、范围与保护

- 实际起始分支 `main`，HEAD 为 `8939e5591bca0fc545e61d9e99565c7d7a09e12d`；开始时及提交前获取 `origin/main`，均无分歧。
- 原有暂存区为空。工作区已有 AGENTS.md、Prompt、README 文档索引、文档归档、main.py 的 dotenv 调整、Monitor 日志字段修改及 temp_test.py。
- 本次只暂存质量修复。AGENTS.md 仍未跟踪；Prompt、历史文档整理和临时测试文件未纳入。10 个可用原始文件与开始前备份逐字节一致；重叠文件的原有修改仍留在工作区，未复制进本次提交。
- 没有修改告警阈值、监控恢复语义、服务配置、业务数据、修复白名单或审批权限。没有清空数据库、重置工作区、强制推送、启动故障注入或进行收费模型请求。

## 核实结果

| 问题 | 判断与触发依据 | 最小处理 |
| --- | --- | --- |
| 单次工具额度不足中断整个调查 | 已证实。旧 `_reserve` 抛出普通 RuntimeError，`_failure` 将其列为 transient，进入 WAITING_RETRY；多调用的后续输出不完整 | 独立的软限制异常，仅拒绝未执行调用，保留 call_id、已保存观测和模型剩余额度 |
| 首次运行身份探测后多声明 1 次工具预算 | 已证伪。13 次包含首次探测，探测后 Python 允许 12 次，Prompt 同样声明 12 次；失败探测也预留 1 次 | 不增加计数特例；模型看到尝试余额与 Session 余额的最小值 |
| 最后模型轮数耗尽仍重试新尝试 | 已证实。旧代码把 attempt_budget_exhausted 当作 transient | 正常轮数耗尽安全结束，不通过新尝试扩张正常调查预算 |
| check_scrape_target 遗漏 TTL / 未知工具 age=0 被认为 fresh | 已证实。旧默认 TTL=0，未知工具恰好零年龄可被接受 | 明确策略、未知工具 fail closed，Worker 装配及测试校验注册表完整性 |
| 所有源码永久有效 | 部分错误前提。完整 40 位 SHA 原本已具有不可变条件；短 SHA/Branch 原本并非 stable | 补齐可变 ref 的 60 秒缓存策略，保留不可变 SHA 条件 |
| 固定 SHA 与运行 SHA 不同仍是当前证据 | 已证实。旧 effective_records 的 stable 分支跳过运行版本关联 | 固定 SHA 可缓存并作为版本参考，只有与运行 SHA 相同才进入当前状态证据 |
| 配置变化后复用依赖 service 查询结果 | 已证实缺口。旧缓存只纳入 dependency 参数的目标配置 | 同时覆盖 service 参数指向的依赖配置；保留主服务及原 dependency 缓存键格式 |
| 上下文重建丢失过期观测字段 | 已证实。旧上下文仅保留状态摘要，过期成功结果无法供模型引用 | 有界历史摘要和失败原因，明确采集时间、来源和禁止用于当前状态 |
| 相同参数的先后调用混用观测身份 | 最终自审发现并回归验证 | 用 call_id / 缓存 observation_id 关联实际保存结果；非法参数拒绝也可安全重建 |
| 补证错误污染最终回答 | 已证实。旧补证未隔离临时失败，旧 `_failure` 会无条件替换结果 | 保存回答，记录补证缺口，禁止证据不足的提案；LostClaim 始终传播 |
| 单个历史 JSON 投影写失败阻塞其他 Session | 已证实。旧 recover 在 claim 前逐个同步，OSError 向外传播 | 隔离单 Session 写入/锁错误，5 秒起步、最高 300 秒退避，保留未完成投影和审计 |
| recover 中存在嵌套 SQLite 连接 | 未复现，当前旧代码相关 Store 调用已传 db；不能仅凭默认参数断言问题 | 增加事务复用回归。JSON 投影移至事务外，按 state_version 只确认已投影快照 |
| 状态预算比较符全部存在差 1 | 未证实。预留前 >= 拒绝第 N+1 次；resume_count > max 的作用是允许第 N 次恢复继续执行 | 不机械替换比较符，验证第 N 次、恢复及已保存结果的边界 |
| 两套调查执行与重试实现 | 已证实。生产 main 已用 Runtime，Agent/Monitor 同步循环仍由旧测试和一个验证脚本调用 | 删除失去用途的循环、缓存、证据去重和 ModelClient 重试；迁移调用与测试 |
| 终端缺少最终回答与 HumanRequest | 已证实。真实 Incident 已保存完整回答，但原终端只显示模型循环日志 | 在持久化后展示结果/问题；CLI 复用 workflow 查询保存数据 |
| no_data 被日志记为成功 | 已证实。旧 ToolManager 在正常 Python 返回后统一打印成功 | 按 status 分类，记录安全原因、时长、执行/缓存标志和可靠的 Session ID |
| get_running_version no_data 是客户端 Bug | 对该真实 Incident 未成立；两次持久化记录都是 commit_unknown | 保留 no_data，细分原因，不放宽 SHA、服务身份、UTC 或开关校验 |
| base 环境导入 field_validator 失败 | 已证实环境问题：base Pydantic 1.10.8，项目要求 2.x | 明确 agentlearn，增加项目依赖闭包锁及 Windows/Python 3.12 CI 配置 |
| 认证错误后取消暂停任务导致全局阻塞无恢复入口 | 已证实独立审查发现：model_blocked 保留，取消后不能 resume 该任务来解除 | 显式本地 unblock-model；不把取消等同于认证修复，不恢复已取消任务 |
| 跨进程崩溃测试断言不足、缺少独立领取竞争 | 已证实审查指出的验证缺口 | 六个崩溃边界验证实际工具 I/O、预留、Checkpoint 和结果；两个真实进程竞争领取与旧进程 fencing |

## 权威路径及接口调整

实际生产装配为 `IncidentMonitor → InvestigationManager → Scheduler/Worker → Agent → ToolManager → Tool/Client`。

[Agent](../agent/agent.py) 保留 Prompt 构建、FunctionCall 解析、单轮模型请求和回答解释；删除 investigate、handle_tool_call、add_evidence、evidence_exists、poll_tool_cache 及关联状态。[Monitor](../monitor/incident_monitor.py) 只保存监控观测和入队，构造器必须传 InvestigationManager。[ModelClient](../model_client/model_client.py) 只进行 call_once，SDK 重试为 0；重试由 Runtime 统一计数，旧 retry.py 和失去用途的 MODEL_RETRIES 删除。

所有仓内入口、验证脚本和重要旧测试已迁移。生产装配测试检查 main 的 Monitor 与 Scheduler 使用同一 Manager，且 Agent 没有旧 investigate 方法。没有把旧循环复制进 Worker，也没有引入另一套 EvidenceStore。未发现仓内必要兼容调用方；仓外使用旧 Agent/Monitor 构造接口的代码须迁移，不能声称已检查未知外部调用方。

重要修改文件：

- 正确性：`investigation/worker.py`、`state.py`、`evidence_policy.py`、`models.py`、`manager.py`。
- 唯一执行路径：`agent/agent.py`、`model_client/model_client.py`、删除 `model_client/retry.py`、`monitor/incident_monitor.py`、`main.py`、`config.py`、两个 validate 脚本。
- 可观测性：`investigation/scheduler.py`、`runtime_cli.py`、`tools/tool_manager.py`、`tools/version.py`、`agent_logger/logger_config.py`。
- 测试：`tests/test_runtime_quality.py`、`conftest.py`、`runtime_support.py`、`runtime_process_fixture.py`，以及 code_evidence、incident_regression、multi_signal、remediation、runtime、service_registry 原测试的迁移/补强。
- 可复现性和说明：`requirements.lock.txt`、`.github/workflows/runtime-tests.yml`、README 和本记录。

## 预算语义

默认限制未增加：每次尝试 7 次模型请求、13 次工具预留；Session 3 次尝试、21 次模型请求、39 次工具预留、8 次恢复、3600 秒。

实际 I/O 之前通过 SQLite 事务预留。初次或恢复时的版本探测成功、no_data 或执行失败均消耗一次预留；恢复不重置尝试和累计计数。缓存命中及预算/权限拒绝不增加工具预留。崩溃在“已预留但尚未 I/O”窗口时，预留不会退回；这是安全的保守计数，不能把这个计数描述为崩溃后已知确实发生的工具执行数。

模型看到的剩余工具次数为 `min(13-current_attempt_tool_calls, 39-total_tool_executions)`。模型轮数也取尝试余额与 Session 余额的最小值，并明确包含本轮。最后模型轮不提供工具 schema；若供应商仍返回工具请求，Python 的硬预算仍生效，不能发起额外模型请求。

每个软额度拒绝都有合法 function_call_output 和原 call_id，内容为 `status=tool_limit_reached / reason=attempt_tool_budget_exhausted`。单次工具不足不会重试 Session；硬累计额度、持续时间及 LostClaim 不转换为正常工具成功。已经保存的最终回答不会被合成失败文本覆盖。

## 证据与上下文

[evidence_policy.py](../investigation/evidence_policy.py) 为全部 11 个只读工具声明策略：指标、健康、抓取目标、运行版本 15 秒；日志 30 秒；近期代码变更、可变源码 ref/diff 60 秒；配置能力/依赖 300 秒。完整 40 位不可变 SHA 可长期缓存，短 SHA 和 Branch 没有不可变待遇。

当前证据必须同时满足：实际保存的成功 ok 观测、时效、目标配置和必要的运行身份。固定 SHA 源码还必须与当前运行 SHA 一致；其他版本仅是参考。不同服务、参数、依赖配置和运行身份继续隔离。

每轮上下文分为当前有效证据、历史成功观测、失败/缺失观测和未经验证的人类线索。历史摘要包含工具、目标、参数、采集时间、状态、关键字段和 observation_id，并带 `use_for_current_state=false`；例如只能说明“采集时数据库可达”，不能证明当前可达。原 ToolObservation 保留在 Checkpoint，摘要不是第二个权威存储。

当前证据块有 60000 字节选择预算，历史/失败摘要 20000 字节，近期调用结果摘要 16000 字节。单摘要限制嵌套、字段、日志条目和文本长度，截断明确标记；这不是整个 Prompt 的总大小承诺。人类线索沿用 64000 字节限制，Checkpoint 沿用 4 MiB 上限。默认供应商使用有界结构化重建；完整消息链仅在验证过供应商、所有成功观测仍当前且调用配对完整时启用。

version 的 no_data/tool_error 仅允许 5 秒内复用失败原因，永不成为 Evidence。启动、恢复、修复补证的 force 探测仍执行实际请求。

## 调查完成、补证与审批

模型回答先保存。随后补证遇到额度/持续时间竞争、no_data 或规划暂时错误，只记录 proposal_evidence_gap，保留回答、观测和零连续调查失败；没有足够当前证据就不生成 Proposal。旧 Worker 丢失 claim 后不得继续任何工具或状态写入。

RemediationPlanner、RemediationStore、ActionExecutor 的审批及执行逻辑没有修改。HumanRequest 答复、普通消息、unblock-model 与恢复都不构成修复授权；已有独立审批测试继续通过。历史摘要不会传入 manager.propose 的当前证据集；历史候选仅用于决定是否值得补证。

## 恢复、事务与持久化

SQLite 继续使用 schema version 1、BEGIN IMMEDIATE、CAS、Lease、fencing token、外键和 synchronous=FULL；不修改真实库 schema，不清空记录。

单 Session 的 JSON 写入/锁失败记录错误类别、次数和 retry_at，最高间隔 300 秒；不把失败投影标记为完成。JSON 和提案文件 I/O 移至 SQLite 事务外；只有 state_version 与实际处理快照一致时确认，竞争保存的新 Checkpoint 保持待对账。损坏数据、未知 schema 和 SQLite 一致性错误传播并使 Scheduler 停止，而非忽略后继续。

Checkpoint 新增可选字段 proposal_evidence_gap、projection_attempts、projection_retry_at、projection_error_category；新代码可读取旧 schema 1 Checkpoint（包括真实历史数据）。旧程序的 extra=forbid 未承诺能读取这些新字段，回滚程序前需要考虑新 Checkpoint 的兼容性。IncidentStore 格式和 RemediationStore 授权数据格式未改。

## 真实 Incident 与版本原因

对 `inc-dfbfc980` / `ses-fba3d990d04944eeaa258f70cd82c7e9` 使用 SQLite 只读连接制作临时副本，在副本上运行真实 CLI。状态 COMPLETED，4 次模型请求、13 次工具预留；CLI 完整回答与 Checkpoint 相同，为 6786 字符。验证前后真实 SQLite 和 Incident JSON 的 SHA-256 未变化，没有修改原报告的“根因未确认”结论。

两次历史 get_running_version 原始状态均为 no_data / commit_unknown。Demo 启动时从 APP_COMMIT_SHA 读取并严格检查 40 位 SHA；缺失、空或非法值会暴露 commit_sha=null。能确认当时没有合法版本声明，不能仅凭记录确定当时环境变量究竟是未设置还是被设置为非法值，也不能把这种情况归为连接超时。

当前只读探测时，8001/8002 无监听，未发现对应运行 Demo Python 实例。实际工具返回 version_connection_refused，localhost 请求约 4 秒；127.0.0.1 的拒绝请求约 2 秒。耗时不能单独证明 timeout，没有更改地址或 5 秒超时配置来制造成功。

工具原因现在区分 timeout、connection refused、其他 connection error、request error、HTTP 错误、响应结构错误、缺少版本字段、commit_unknown、非法 SHA、服务身份不确认、非法 UTC 进程时间和开关；其余执行异常由 ToolManager 记录安全异常类别。不放宽任何身份/SHA 校验。

## 终端与 CLI

[Scheduler](../investigation/scheduler.py) 在 SQLite 已提交后输出完成回答、Incident/Session/Service/状态/计数；等待时输出 Request ID 和问题；暂停/失败/耗尽输出状态与安全原因。状态、回答和 HumanRequest 查询使用同一事务快照，终端 I/O 不持有 SQLite 锁。打印失败不影响调查保存，之后可以重试。

SQLite metadata 保存简明展示签名，防止每轮或重启重复展示相同状态；状态、错误原因或待回答 Request 改变会重新通知。打印成功与标记提交之间的崩溃仍可能导致一次重复，未声称终端 exactly-once。

在项目目录使用已验证解释器：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py show-session ses-fba3d990d04944eeaa258f70cd82c7e9
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py list-human-requests
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py answer-human-request ses-替换为真实ID --request-id req-替换为真实ID --actor 本地操作员 --text '回答内容'
```

show-session 包含 investigation_result.answer、预算计数/限制、HumanRequest、错误和投影重试信息。模型认证/余额问题修复后显式 `unblock-model --actor 本地操作员`；暂停任务仍需 resume，取消任务不复活。actor 是本地审计信息，不是 Web 身份认证。

ToolManager 单条日志区分 Python 执行、ok、no_data、tool_error、缓存和拒绝；记录可靠 Session ID、工具、目标服务、安全 reason、时长及 executed/cached，不打印完整参数或异常正文。测试通过受信任日志路径覆盖及临时目录隔离，子进程继承同一测试日志路径。

## 环境和测试结果

agentlearn 实测：Python 3.12.14、Pydantic 2.13.5、OpenAI SDK 3.14.0、requests 2.34.2、python-dotenv 1.2.3、psycopg 3.3.6、PyYAML 6.0.3、pytest 9.1.1、SQLite 3.53.4。base 为 Python 3.11.5 / Pydantic 1.10.8，不是项目测试环境。

requirements.txt 的 Pydantic 2 约束原本正确。本次 requirements.lock.txt 锁定项目及 pytest 的 29 个依赖闭包，没有导出 Conda 的无关包、没有新增生产框架或运行时依赖。

| 验证 | 实际结果 |
| --- | --- |
| 修改前完整基线 | 418 passed，129 subtests，79.20 秒 |
| 第二阶段完整回归 | 432 passed，140 subtests，127.08 秒 |
| 最终冻结工作区完整回归 | 447 passed，151 subtests，142.24 秒 |
| 排除原有修改的独立提交代码快照 | 447 passed，151 subtests，112.44 秒 |
| 六个独立进程崩溃边界 | 全部通过；全量测试同时检查实际 I/O、保守预留、观测/回答保存与重采/缓存 |
| 两个真实进程领取竞争及旧 Worker 写入 | 仅一个获得 claim；过期并重新领取后旧进程 guard 被拒绝 |
| 真实历史 CLI 副本查询 | 完整回答及 4/13 计数与保存结果一致；原数据哈希不变 |
| pip check | No broken requirements found |
| pip install --dry-run --ignore-installed -r requirements.lock.txt | 29 个固定依赖解析成功；未在新环境实际安装 |
| 暂存 diff / 秘钥及运行文件检查 | diff --check 通过；未包含配置凭据或运行数据，原有修改保留 |

实际运行命令：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest tests -q --disable-warnings
# 同样命令在 .git/quality-fix-release 中独立运行拟提交代码快照
& 'D:\conda\envs\agentlearn\python.exe' -B -m pip check
& 'D:\conda\envs\agentlearn\python.exe' -B -m pip install --dry-run --ignore-installed -r requirements.lock.txt
```

447 项是隔离自动化总数，包含 Mock/Fixture、真实 SQLite 事务及跨进程测试，不能等同于 447 项真实外部联调。中间全量曾因旧通用原因码断言、一次新增测试参数拼写失败；两者已修正，最终冻结快照没有失败。没有删去审批、fencing、崩溃、旧数据和重要业务行为覆盖来使结果通过；废弃 poll 共享缓存测试迁移为 Runtime 同 Session 缓存与跨 Session 观测隔离。

## 未验证与维护边界

- 未发起真实模型请求，未重跑真实模型 HITL/修复闭环；本次没有收费请求、审批或执行修复。
- Demo 当前未运行，因此未进行健康 Demo /version=ok 的新联调；真实历史 no_data 原因不能进一步还原当时环境变量值。
- 没有故障注入、实际断电/磁盘损坏实验或长期并发压力测试。只读工具在“实际 I/O 后但 Checkpoint 前”崩溃可在预算内重采，不承诺外部调用 exactly-once。
- CI 配置已加入，GitHub 上的新运行结果未在本记录中验证；干净环境安装仅做依赖解析，未假装为完整新环境验证。
- 历史摘要有截断和采样；不能用于修复当前证据。现有单 Worker、全 Session 轮询、SQLite 与 JSON 幂等对账设计保持，不引入事件总线或新的通用 Manager。
- Worker/Manager 仍是复杂职责的主要维护点；本次局部调整，没有以行数为理由拆出额外层级。未知仓外旧接口使用者需要自行迁移。
- 原有文档整理仍未提交，本次只新增符合 docs 命名规范的记录并补 README 链接。最终 Commit SHA 和推送结果以交付回复及 git log 为准，避免文档包含自引用提交 SHA。
