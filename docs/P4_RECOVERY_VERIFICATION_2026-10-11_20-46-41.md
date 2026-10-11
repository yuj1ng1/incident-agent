# Incident Agent P4.1 / P4.2 复核与验证记录

本次范围是独立业务恢复验证；仅修改本地源码、测试并新增本报告。没有提交、推送、启动新的真实监控进程、调用付费模型、制造故障或修改真实审批与调查预算。

## 1. P4.1 已确认的事实

- 原 Monitor 通过完整告警快照判断候选解除，再校验当前规则、阈值语义、目标 up、抓取时效和实际指标。缺失、过期、异常指标不会被当作零。已有 can_clear / rules_are_current 的业务规则没有改变。
- resolve_inactive_incidents 将 Incident.status 写为 resolved、alert_state 写为 cleared，并记录 UTC cleared_at；原来没有独立业务探测。
- 原 record_recovery_verification 只要求带时区的时间、非空 Evidence 和 cleared 状态。本次在临时目录实际复现：它接受了早于解除时间七天、source 为 unrelated_health、detail 为 {} 的证据。它没有验证业务成功、目标、Incident 身份、时效或解除批次。
- 同一 fingerprint 解除后再次 firing 会创建新的 Incident。只检查旧 Incident.alert_state 不足以拒绝其正在进行的旧探测，需要识别同一 fingerprint 的后续记录。
- 另在临时目录复现了旧解除判断覆盖新 firing 观测的窗口：指标查询与 JSON 写入之间，另一 Monitor 更新了告警；原解除方法仍将其置为 resolved。现已增加观测一致性校验。
- 实际阅读了 [Demo app.py](../../prometheus-demo/app.py)：/order 只执行 SELECT id, item, quantity FROM orders ORDER BY id LIMIT 1，不写订单；数据库连接和查询成功后仍可能因回归开关返回 500。/health 只确认进程可达，不访问数据库。因此 /health=200 不能证明 /order 正常。业务查询仍会产生正常访问日志、指标和数据库负载；GET 方法本身不是无副作用的证明。
- 现有健康 Provider / ServiceClient 不检查业务契约，不能直接作为此验证器。Remediation 的 DemoVerifier 属于获批动作后的验证，绑定 Proposal，不能替代独立 Incident 恢复记录。
- Runtime 的 Worker、恢复、Checkpoint 和 SQLite 事务面向调查 Session；将独立 HTTP 探测塞入该流程会混淆模型预算与审批。此次没有修改 investigation/ 或 remediation/。
- Web API 使用明确字段白名单，已公开 cleared_at 与 recovery_verified_at；没有公开恢复失败原因或尝试明细。本次没有修改 web_api/ 或前端。

## 2. 最小架构与修改文件

复用 ServiceRegistry 的可信配置、IncidentStore 的 JSON 权威清单、现有进程锁/文件锁与 fsync 后的原子替换。解除记录本身作为可扫描的待办来源；无需新增数据库、迁移历史数据或另建调查 Runtime。

| 文件 | 实际变化 |
| --- | --- |
| [services/profiles.py](../services/profiles.py) | 新增可选 HTTPRecovery，Pydantic 校验批准标记、固定目标、服务身份、响应条件及次数/超时/时效上限 |
| [services/configuration.py](../services/configuration.py) | 仅为 order-service 注册 order_read_query；其他服务未配置即不会自动验证成功 |
| [services/registry.py](../services/registry.py) | 支持 recovery capability；描述仍不输出 URL 或完整配置 |
| [models/recovery.py](../models/recovery.py) | 新增有版本的 RecoveryPolicy、RecoveryVerification、RecoveryAttempt、RecoveryReceipt、RecoverySample 和检查结构 |
| [models/incident.py](../models/incident.py) | 添加可选 recovery_verification；原 Evidence 与 recovery_evidence 结构保持可读 |
| [recovery/verifier.py](../recovery/verifier.py) | 无 LLM 的固定目标 HTTP 业务探测及有限、脱敏的结构化收据 |
| [recovery/worker.py](../recovery/worker.py)、[recovery/__init__.py](../recovery/__init__.py) | 默认两个轻量后台消费者；持久化领取、调用验证器、提交结果与重启扫描 |
| [incident_store.py](../incident_store.py) | 新增候选查询、领取、失败完成；加强成功写入校验；保护恢复字段；增加解除观测校验及 Windows 文件提交保护 |
| [monitor/incident_monitor.py](../monitor/incident_monitor.py) | 解除时传入原观测快照，可信轮询后唤醒恢复消费者；轮询线程不执行业务探测 |
| [main.py](../main.py) | 管理恢复消费者的启动和停止，入口 import 仍无运行副作用 |
| [tests/test_recovery_verification.py](../tests/test_recovery_verification.py)、[tests/recovery_process_fixture.py](../tests/recovery_process_fixture.py) | 独立恢复回归及真实子进程验证 |
| [tests/test_multi_signal.py](../tests/test_multi_signal.py) | 旧“随意写一个 200 即成功”测试改为确认该输入被拒绝 |
| [tests/test_service_registry.py](../tests/test_service_registry.py) | 明确只有 order-service 新增可选恢复能力 |

没有新增第三方依赖，也没有修改 requirements、Agent 提示词、日志时间窗口、Session 状态机、修复白名单或正式审批机制。默认后台扫描仅读取热清单，不打开历史归档。JSON 清单本身仍由原 IncidentStore 管理；大规模历史的扫描性能尚未做容量压测。

## 3. 默认配置和成功条件

注册目标为 GET http://127.0.0.1:8001/order，可信标识 order_read_query。

一次尝试默认要求一次请求同时满足：

1. HTTP 状态精确为 200。
2. JSON service 精确为 order-service。
3. JSON dependency 精确为 order-db。
4. JSON order 为非空对象。

这些条件证明注册查询在采集时满足约定，不等于所有业务接口、所有实例和持续 SLO 都恢复。

| 参数 | 默认值 | 配置上限 |
| --- | --- | --- |
| 每次尝试要求的成功样本 | 1 | 5 |
| 每次解除的尝试次数 | 3 | 5 |
| 失败后的重试间隔 | 30 秒 | 300 秒 |
| 连接/读取 timeout | 各 2 秒 | 各 5 秒 |
| 写入时的证据最大年龄 | 60 秒 | 300 秒 |
| 原始响应体 | 最多 16 KiB | 固定上限 |
| 默认领取租约 | 14 秒 | 根据探测强度推导 |

租约先于网络操作持久化；崩溃也消耗一次已领取的尝试。失败按配置间隔重试；崩溃留下的领取在租约到期后可接管。次数不会因重复轮询、重建 Worker 或配置变更清零。缺少能力/有效解除时间、配置漂移、旧解除被替代时停止自动验证并保留原因。

配置目前仅支持明确 IP 的 HTTP/HTTPS origin、固定未编码路径和获批的 GET 业务查询；不支持任意主机名、认证配置、动态 URL、查询参数或模型给出的地址。现有 health / runtime 能力的主机名支持不受影响。JSON 条件只支持顶层字段精确匹配与非空对象，不执行自定义代码。

## 4. 核心流程和权威边界

1. Monitor 完成原有可信解除检查。
2. IncidentStore 在文件锁内重新核对 Incident ID、服务、告警观测和状态；新观测优先，旧解除判断不能覆盖它。
3. 解除记录持久化后，后台消费者扫描尚未验证的记录。
4. 在短文件锁内核对当前解除批次、同一 fingerprint 的后续记录、重试时机和预算；持久化随机领取令牌与租约。
5. 完全释放数据锁后，使用注册配置执行有限 HTTP 探测。
6. 成功写入前再次读取权威记录，检查当前领取令牌、租约、解除时间、Incident/服务/目标/配置身份、UTC 证据时间、样本数量与全部成功检查。
7. 同一原子提交写入恢复成功时间、结构化 Evidence 和完成状态。失败只写诊断/重试信息，不写成功时间，不改 firing 状态，不发起修复。

新增主要接口为 recovery_candidates、claim_recovery_verification、finish_recovery_verification；成功仍使用 record_recovery_verification，现要求有效 claim_token 和完整收据。resolve_inactive_incidents 增加可选 expected_incidents，Monitor 提供它以保护并发解除判断。

权威存储是 IncidentStore；本功能不维护第二份 SQLite 恢复队列。监控告警、业务恢复、调查 Session、正式修复审批仍为不同概念。

## 5. 并发、重启、重复执行与证据保证

- HTTP 不在 IncidentStore 文件锁、线程锁或本功能的 SQLite 事务中执行。
- 同一解除同时只有一个有效领取；旧领取的迟到结果不能覆盖新领取。
- 同一 fingerprint 新 Incident 出现后，即使它再次解除，也不能接受前一 Incident 的迟到成功。
- 同一 ID 的告警状态或 cleared_at 在探测中变化，也会拒绝旧成功。
- 普通 observation / Runtime 保存读取最新恢复字段，旧对象不能抹掉已保存的尝试和证据。
- 成功重复提交幂等，不追加第二份证据；失败保留原有历史恢复证据。
- 解除后未唤醒、领取后退出、网络后未提交，均能通过持久化清单再次发现。只有明确成功且可靠保存的结果才设 recovery_verified_at；不伪造崩溃前的探测结果。
- 不能保证每个物理 HTTP 请求恰好一次。租约接管可能重复获批的只读查询，但总尝试有界；已提交成功证据不会重复写入。
- 校验以当前已持久化的 Monitor 观测为准。Prometheus、HTTP 和本地 JSON 无跨系统原子事务，不宣称消除监控观测延迟。
- 配置变化不会重新授予本次解除的尝试预算；旧配置收据不再被接受。

Windows 并发回归实际复现了普通读句柄导致 os.replace 返回 WinError 5。最终方案使用原 RLock 协调同一 Store 的读取，仅对 Windows 错误 5/32 做最多三次本地提交尝试，两次间隔各 10ms；不重复网络探测。外部长期占用文件仍会明确失败，原文件保留、成功不落盘；释放占用后可重试。没有改用新的原生替换协议，也没有弱化文件锁。Windows 共享模式的定义见 [Microsoft CreateFileW 文档](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)。

## 6. HTTP 与证据保护

禁用环境代理和默认环境认证，保持 TLS 校验。原始 JSON 仅在内存中用于业务条件校验，不持久化响应头、Cookie、Token、密码或订单正文。持久化内容只有注册标识、配置摘要、UTC 时间、HTTP 状态、检查名称/布尔结果和受控原因码。

拒绝重定向、非 JSON、非对象 JSON、重复 JSON 字段、NaN、压缩正文、超限正文及不符合业务条件的 200。核对本机 Requests 2.34.2 源码并用隔离 HTTP 服务复现：allow_redirects=False 仍可能在准备 Response.next 时读取重定向正文。最终在 response hook 中先关闭并拒绝 3xx，避免库先消耗不受本验证器限制的响应体；真实 HTTP 回归确认未访问重定向目标。

Requests timeout 是连接/读取等待限制，不是整个下载的硬墙钟上限。本实现另检查正文读取经过时间并限制正文大小；未验证恶意持续滴流响应头的绝对退出时限，不能将领取租约当作强制杀死 HTTP 线程的机制。超时语义见 [Requests 官方说明](https://requests.readthedocs.io/en/latest/user/quickstart/#timeouts)。环境认证与代理开关见 [Requests Session 文档](https://requests.readthedocs.io/en/latest/api/#requests.Session.trust_env)。

## 7. 实际验证结果

环境：D:/conda/envs/agentlearn/python.exe，Python 3.12.14、Pydantic 2.13.5、Requests 2.34.2、pytest 9.1.1。

- 修改前相关基线：213 passed、1 skipped、51 subtests passed。
- 最终独立恢复回归：82 passed，13.55 秒。
- 最终完整 Python 回归：833 passed、2 skipped、190 subtests passed，418.74 秒（6 分 58 秒）；实际命令为 `D:/conda/envs/agentlearn/python.exe -B -m pytest -q`。
- 本次源码/测试语法检查及限定本次文件的 diff 空白检查通过。
- 开发中发现的能力集合旧断言、Windows 读写竞争和真实重定向路径均经过核实、修正并纳入最终测试；未将未完成或失败的中间运行当作验收通过。

完整回归的两个跳过项分别是依赖纯净安装检查（需 INCIDENT_AGENT_CLEAN_ENV）和可选跨仓库规则检查（需 ALERT_RULES_PATH）。本次没有触发远端 CI 或创建新的纯净安装环境。

新回归覆盖全部要求的十五类场景，包括 firing 不探测、500、符合/不符合契约的 200、连接失败与超时、缺少能力、非法目标、缺失/过期/未来时间、重复轮询、重建 Worker、子进程异常退出、同/不同 ID 再次 firing、双进程竞争领取、旧令牌跨进程迟到写入、保存重读、v1/v2 历史读取、失败保留证据和慢探测时继续监控其他 Incident。额外覆盖配置漂移、布尔值不接受整数、缺少成功检查、响应脱敏、真实本机 HTTP、真实重定向正文截断以及 Windows 跨进程读占用的有界失败和恢复。

验证层次：

- HTTP 状态/业务条件/多数错误路径使用 Mock 与临时数据。
- 并发领取、租约接管、异常退出、迟到提交和读占用使用真实 Python 子进程与临时 JSON 文件。
- 锁外探测验证包含真实隔离 SQLite 写事务；并不修改正式 Runtime。
- HTTP 客户端验证使用真实隔离回环服务器。
- 真实 Demo 验证：2026-10-11T12:10:24.079893Z，注册 /order 返回 200，四项条件全部满足，成功证据保存并重新读取于临时 Incident；没有写入真实 Incident 清单。这次实际只读业务请求会产生正常指标/日志，未制造故障、重启服务或改变业务数据。
- 未重跑此前“40 次请求”历史测试，也未执行新的真实模型调查。

## 8. 数据保护、未完成联调与启用

结束核对时，将任务开始保存的 126 份哈希与当前文件逐一比较：其中 15 份是原有工作区文件，111 份是运行数据。原有文件中 14 份一致；AGENTS.md 在任务期间变为新的排版版本（LastWriteTimeUtc 为 2026-10-11T12:41:32.7600016Z），本次没有编辑、还原或覆盖它，并已读取最新约定。运行数据中，仅 data/incidents.json 有变化，Runtime SQLite 和其他 110 份已核对的数据文件一致。此检查不代表整个运行目录所有文件的完整快照。

原有 main.py 监控进程 PID 3656 在任务开始前已启动（北京时间 2026-10-11 18:30:10），结束核对时仍在运行，会持续写入 incidents.json。本次测试和真实 Demo 证据提交均使用临时存储；没有直接修改、迁移或回填真实 Incident/Runtime/Remediation 数据，也没有为保持哈希而覆盖正在更新的文件。

暂存区仍为空，HEAD 仍为 6fdd1d93eaf0d8c35120e7bea5c560a82fd04c7b，分支仍为 main。本次新增和修改的 15 份源码/测试加本报告留在工作区；原有文档删除、归档、README、提示词、temp_test.py 等改动未纳入本次实现。

待真实环境确认的是：加载新代码后的 Monitor 可信解除 → 后台验证 → 原有真实 Incident → 只读 Web API 的完整链路，以及真实故障失败重试、真实重启接管、部署中的多个 Monitor 和长时间运行。此次临时 Incident 的真实 /order 成功不替代上述整链路验证。

现有运行中的 Monitor 不会自动加载新模块。检查代码后应整体更新监控进程，避免新旧版本同时写入同一清单；旧版序列化器不认识新恢复字段。没有在本次任务中停止或重启现有监控/业务进程，也没有自动修改历史记录来让恢复字段显得成功。

恢复证明是采集时的历史事实，不能将非空 recovery_verified_at 展示为永久实时健康。HTTP 探测会改变访问统计，应依据服务实际业务契约配置验证强度。

## 9. P4.3 接入建议

现有只读查询已经输出解除与验证时间。后续只需扩展 [web_api/schemas.py](../web_api/schemas.py) 和 [web_api/queries.py](../web_api/queries.py) 的白名单 DTO：

- 独立 recovery_status、注册 target_id、尝试次数/上限、最近尝试时间、next_retry_at 和受控失败原因。
- 有限 HTTP 状态与检查布尔摘要，不暴露领取令牌、原始 detail、完整配置、URL、正文或响应头。
- 明确区分“告警已解除”“业务已于某时验证”“验证失败”“证据不足/等待重试”和旧批次已失效。
- 对没有 P4 结构化收据的旧恢复记录保留历史标识，不默认为符合新的证据校验。
- API 请求和 Web 启动保持只读，不触发探测、重试、调查或审批；不增加写接口，不改变原 Session/Remediation 语义。

本次没有实现 P4.3 页面展示。没有 git commit、git push 或暂存本次修改；原有未提交改动保留供用户检查。
