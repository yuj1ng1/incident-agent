# Runtime 稳定性与维护性整改验收

本记录基于当前源代码、隔离测试和实际 Git 状态。开始时本地 main、HEAD 和刷新后的 origin/main 均为 `f57d98b666db4fe3ec5d2eee5e012737dac4f68a`，暂存区为空，工作区有用户修改。普通报告文件名取正文完成后的 Asia/Shanghai 实际时间；业务数据继续使用带时区 UTC。本轮没有真实模型调用、真实监控轮询或故障注入。

## 1. 再调查规则与累计限制

| 原 Session | 自动轮询行为 | 人工入口 |
|---|---|---|
| COMPLETED | 完成投影后按既有 Policy、最小间隔、冷却/告警恶化及共享预算决定 | 只有原授权预算耗尽时允许 reopen |
| FAILED | 仅 model_empty_answer、model_invalid_call_ids 可在退避后有限再调查；未知错误/配置错误不自动重试 | 明确修正原因后 reopen |
| EXHAUSTED | 拒绝新建 Session，不清零预算 | reopen 明确重新授权固定上限 |
| CANCELLED | 不因轮询复活 | Incident 仍活动且实时 firing 才能 reopen |
| PAUSED / WAITING_HUMAN | 保留原 Session 和问题，不创建重复任务 | 原有 resume / answer 语义 |

429、临时 5xx、连接失败等继续使用同一 Session 内已有的 WAITING_RETRY，5/10 秒退避、有限尝试，耗尽后停止。FAILED 的新 Session 退避复用 AlertPolicy.retry_seconds，当前默认连续失败后等待 60/120 秒；复用 Incident 的 consecutive_investigation_failures 和 Policy 最大连续失败次数，没有另建失败计数器。

一次 Incident 调查授权包括原 Session 及其所有自动再调查 Session。新增可选 budget_group_id：旧 v1 Session 缺少此字段时默认为原授权组；自动新 Session 继承所属组；人工重开建立新组。预算按 incident_id 和授权组查询并汇总 Session 的权威计数，没有第二份持久化预算余额。

配置数值保持：每次尝试 7 次模型请求、13 次工具执行；一次授权最多 3 次尝试、21 次模型请求、39 次工具执行、8 次恢复、3 次提问、3600 秒。时间从该授权组最早 Session 创建起算，包括暂停/等待。每次外部调用前，在同一 SQLite 写事务中检查并预留累计预算；工具达到上限后仍允许剩余模型额度生成最终回答。第 N 次获准恢复可以运行，申请 N+1 时先拒绝再保持计数。普通 message、answer、resume 和新建自动 Session 均不能重新授权。

拒绝原因可通过 runtime_cli show-session 的 reinvestigation_refusal 查询；原因变更时持久化 reinvestigation_rejected 事件，重复同一拒绝不持续写入重复事件。

相关实现：[策略](../reinvestigation.py)、[Manager](../investigation/manager.py)、[Worker](../investigation/worker.py)、[预算判断](../investigation/state.py)、[Store 投影](../incident_store.py)。

## 2. 人工重开与审计

```powershell
python -B runtime_cli.py reopen ses-实际ID --actor 本地操作员 --reason '原因已修正，重新授权有限调查' --command-id 重开001
python -B runtime_cli.py show-session ses-实际ID
python -B runtime_cli.py show-events ses-实际ID --limit 1000
```

reopen 必须提供非空身份、理由和稳定命令 ID，可附 expected-version。命令先持久化再处理；相同 ID/内容返回原记录，不同内容冲突拒绝。只允许最新终态 Session 且上一投影完成。保留原 Session，不自动重放旧工具，不复用旧动态 Evidence。

处理前通过生产装配提供的只读 Prometheus verifier 获取完整当前告警快照，确认 fingerprint、服务、firing 状态和观测时效；进入提交事务后再次检查 Incident 尚未解除。缺失 verifier、缺失/过期/错误身份的告警、查询失败、已解除 Incident 均持久化拒绝。若提交后告警解除，Worker 在任何模型/工具执行前取消新任务。

investigation_reopened 与 budget_reauthorized 记录操作者、命令 ID、理由、原/新 Session、原错误/状态、之前累计计数、重新授权限制和告警观测时间。双进程重复同一重开命令仅产生一个命令和一次新授权。旧 IncidentStore.resume_investigation 的无审计清零入口已关闭，需提供 Manager/actor/reason 并委托持久化 reopen。

actor 仍是本地审计字段，不是真实身份认证。操作授权依赖当前本地账户对 CLI 和数据目录的访问权限；没有添加 Web 或公网认证。重新授权调查不解除全局模型认证/余额阻塞，也不批准或执行 Remediation。原审批系统及修复白名单未修改。

## 3. 启动和导入

main.py 只定义 main() 并在模块 guard 下调用。外部资源、dotenv、配置与工具初始化位于明确启动生命周期：先加载 dotenv，再导入配置/工具，构造客户端/Store，启动 Scheduler，运行 Monitor，退出时停止 Scheduler 并关闭模型客户端。保留 python main.py 的终端启动方式。

普通 import main 不读 dotenv、不加载工具/模型依赖、不构造 API 客户端、不开线程、不轮询、不创建/写入 Store。runtime_cli、remediation_cli 和两个受控验收脚本也消除了模块级业务依赖及 dotenv 读取。

隔离子进程用 import/audit/thread 拦截器拒绝网络、写文件、启动线程、子进程及模型依赖导入；检查哨兵数据校验和和线程数量。额外负向用例故意请求网络、线程、写入和模型导入，全部在副作用发生前被拒绝。生产装配测试使用真实 Agent/Manager/Monitor/Scheduler 对象，但资源路径、告警快照和线程启动均受隔离控制，验证两次受控轮询及关闭关系。没有直接非隔离 import 真实生产入口。

已有 GitHub 客户端按调用读取凭据的延迟机制已核对并保留，没有再次实现另一套初始化。

## 4. 权威数据、历史保留和恢复

| 数据 | 权威来源与保留 |
|---|---|
| Incident 身份、告警状态、active_by_fingerprint | incidents.json v2 |
| Session 状态、预算计数、执行权、命令、人类问题 | SQLite RuntimeStore v1 |
| 调查结论、已保存 observation/trace、恢复上下文 | 每个 Session 最新 Checkpoint；保留最近有限中间快照 |
| JSON 内 Runtime Evidence、InvestigationRecord、调查暂停/失败投影 | 从 SQLite 幂等投影；按 Session/observation ID 去重 |
| 没有 Runtime Session ID 的旧 Evidence/调查记录 | 只有原 JSON；继续保留，不推断 SQLite 可替代它们 |
| 修复审批、执行、验证与审计 | 原 RemediationStore，授权边界不变 |

不删除 Evidence 或调查历史。默认热文件保留最近 3 次 Runtime 调查及其关联 Evidence，旧内容完整归档到 incidents.json 同目录的 history/<sha256>.json，manifest 新增可选 history_archives 字段。先原子写入归档、fsync、校验身份/结构/校验和，再原子提交 manifest。归档写入失败保留旧热数据；manifest 提交失败可能留下未引用文件，原数据仍在，重试可复用相同归档。没有自动删除归档。

旧 JSON/v1 Session 可直接读取，不在启动时批量修改数据。历史只在对应 Incident 正常保存时归档。get_incident/get_active_incident/active_incidents 默认完整恢复历史，CLI 新增 show-incident；热路径显式 include_history=False。缺失或损坏归档报错，已归档内容不能被覆盖。另复现并修正了按时间展示的 Evidence 顺序与归档顺序不一致时误拒绝正常保存的问题。

Checkpoint 删除旧版本后，最新快照仍保留可靠累计观测、trace、结果；checkpoint_saved 事件补充 checkpoint ID 与 observation ID，可关联保存边界。已删除的每个中间模型请求原文、未保存响应不能精确恢复，不宣称完整原始推理历史全部存在。

归档限制的是热文件历史 payload，整个数据目录没有硬容量上限。归档引用、Session、命令及审计继续增长，后续容量管理需要保留完整备份和操作员策略。备份应在进程停止时包含完整 data 目录及 history/SQLite/Incident/Remediation 文件，不能只复制 incidents.json。

## 5. Runtime 扫描和事务

recover 按状态只查当前非终态任务，并单独查询 pending 命令、待投影工作；Scheduler 通知只查询待通知工作。list_sessions 一次 SELECT 返回需要的行，避免逐条 get 的 N+1 查询。预算汇总只查询对应 Incident/授权组。活跃 Incident 的补建检查仍保留，但用索引查询其最新 Session。

沿用 runtime_metadata 保存持久化待投影/待通知索引，在 Session/Checkpoint 写事务内更新。旧数据库首次启动一次回填；状态版本核对、防重复通知、投影失败重试、lease/fencing 与 WAITING_HUMAN/未消费消息均保留。不是以进程内缓存判断历史任务完成。

新增索引，SQLite 表、列及 user_version=1 保持兼容。写操作继续 BEGIN IMMEDIATE、synchronous=FULL、DELETE journal。读查询使用只读 URI/query_only 和 BEGIN 快照，不持有 RESERVED 写锁；只读连接尝试 UPDATE 会失败。没有通过降低安全持久化配置获取性能。

确定性测试：有 1 个已完成活跃 Incident，历史额外终态 Session 为 0 或 1000；通知/投影已完成后各执行 3 个空闲 tick，两组结果均为：

| 指标 | 0 个额外终态 | 1000 个额外终态 |
|---|---:|---:|
| 写事务调用 | 0 | 0 |
| 历史 Checkpoint 读取 | 0 | 0 |
| Session 反序列化次数 | 3 | 3 |

这是任务处理量和写事务次数的确定性测量，不是吞吐/毫秒延迟基准。首次数据库 quick_check 和一次性旧数据回填仍有历史成本；没有宣称所有读成本为零。

## 6. 审查发现核实

| 审查关注点 | 本轮核实与处理 |
|---|---|
| 持续 firing 的 FAILED/EXHAUSTED 无法再调查 | 已证实旧 enqueue 仅允许 COMPLETED 后建新 Session；改为有分类、有退避、有共享预算/人工授权的策略 |
| 新 Session 可能重置预算 | 保留原重要测试名称并扩展业务语义：允许分类 FAILED 有限重试，但总模型请求不得超过原 Incident 授权；另覆盖完成后再调查、429、人工重开和多 Incident 隔离 |
| main.py 导入即构造资源、运行轮询 | 源码证实；五个入口隔离导入回归通过 |
| 终态历史反复扫描且读查询争用写锁 | 源码证实；索引/pending 工作和只读快照修复，1000 个终态确定性测试通过 |
| legacy naive/aware 时间相减 | 已证实；统一将旧无时区时间按北京时间解释再转换 UTC，边界回归通过 |
| 恢复次数最后一次是否允许运行 | 当前 N 次恢复可运行的语义保留并明确验证；N+1 不增加计数，不擅自扩大原上限 |
| 未提交日志字段拼写 | 用户修改存在 inciden_id；按本轮明确范围修正为 incident_id |
| 仓库外 D:/prometheus-demo 测试路径 | 已证实原默认路径；改为仓库内契约夹具，跨仓库验证只有显式 ALERT_RULES_PATH 时运行，配置错误不静默跳过 |
| 自证式/恒真断言 | 证实关联测试的 all(...) 在空映射时可空集合成立；增加明确键集/数量与负向预期。规则测试增加独立五规则集合。AST 检查未发现 assertTrue(True) 或同表达式自相等；未获得独立审查原文，其他具体“自证”指控不能逐项确认 |
| temp_test.py 可直接删除 | 未证实：用户当前将其改为真实 Loki 查询脚本，仍有用途；原样保留，不导入或收集它，不纳入提交 |

当前固定文件名 Runtime 指南保留正文已有用户例外，仅修正上述已确认过时使用说明。旧验收记录和用户未提交 PROJECT_GUIDE 归档快照未改；旧快照中的同步入口/预算说明不代表当前版本，应以本记录及更新后的 [使用指南](DURABLE_AGENT_RUNTIME_GUIDE.md) 为准。

## 7. 实际验证

基线完整测试：474 passed、1 skipped、178 subtests passed，158.83 秒。

冻结暂存代码导出到不含 .env/真实数据/用户无关修改的独立临时目录，完整运行：

```text
python -B -m pytest tests -q --disable-warnings
504 passed, 2 skipped, 190 subtests passed in 221.03s
```

测试数据、模型响应、Prometheus/Loki 等外部返回均为隔离 Fixture/Mock；SQLite、文件归档和跨进程执行是真实的临时资源。包括六个独立进程退出恢复边界、claim/fencing、人类消息、双进程重复重开、另一进程 CLI 查询归档历史。两个跳过项为本地未启用干净依赖环境核验和未配置外部告警文件；仓库内规则契约始终运行。CI 的干净固定依赖环境由 GitHub Actions 实际检查。

本轮中间测试暴露并修正了新 Session 达到共享模型预算后错误分类、归档排序比较和受控脚本导入副作用。一次工作区完整测试在运行过程中扩展导入夹具后出现旧内存预期与新夹具不一致，结果为 1 failed / 503 passed；最终冻结快照完整重新执行通过，没有削弱原预算/审批/fencing 断言。

最终冻结导出后仅更新当前使用指南、README 与本验收记录，待提交可执行代码/测试保持冻结版本。指南另纠正 resume 自动解除全局模型阻塞的过时说法：必须显式 unblock-model，随后再恢复暂停任务。最新发布 SHA 和实际 CI 链接以交付回复及仓库 Actions 记录为准，不将尚未完成的 CI 写成通过。

## 8. 修改范围与未验证事项

复用现有 Manager、Worker、RuntimeStore、IncidentStore、Policy 和审计；新增的是已有模型的授权组字段、HumanCommand.reopen、HistoryArchive 结构、过滤查询及只读快照，没有新的 Manager/Service/框架、第三方依赖或 Store 数据库迁移。Remediation 工具注册、审批权限和不确定副作用重放规则未改变。

开始时已有 Prompt、temp_test.py、AGENTS.md、文档归档/删除及 README 索引修改。仅纳入本轮明确相关的 main 生命周期/dotenv 和日志拼写，其余原样保留，不提交。README 使用选择性暂存保留用户本地索引修改。真实 data/日志 299 个文件校验和逐一比对，未修改、未新增真实运行文件。

未执行真实 Prometheus/Loki/模型联调、生产 Monitor、Demo 故障注入或修复动作；因此不宣称这些外部系统当前可用。未测物理断电、磁盘满、网络共享盘或跨机器调度。归档不会自动清理，保留完整追溯有总磁盘增长成本；本地 actor 不构成公网认证。SQLite/JSON/Remediation 仍没有跨存储共同事务，沿用幂等投影及版本核对恢复。
