# P6.2b 第一轮受控 /error 故障实验报告

本轮已按新授权执行且只执行一次：本地 order-service 的 5 次 GET `/error` 均返回预期 HTTP 500，版本核验 10 次，helper 总耗时 35.469 秒并正常退出。真实后续观测确认 Counter 从 8 增至 13、Loki 返回 5 条本时段错误日志候选；采集时 order-service 告警为 inactive，5xx 比率和 P95 为 no_data。未捕获真实 firing，因此 Runtime 使用明确标注的合成告警与 Mock SDK。没有补跑故障、额外 `/order`、真实 DeepSeek 或修复。

本地完整原件根目录为 `data/p62b-first-error-20261018-01/`，不加入 Git。报告保留失败、时序缺口和未知项，不能将本轮表述为真实告警触发或真实模型根因调查成功。

## 1. 环境身份与授权记录

已阅读当前 `AGENTS.md`、实际代码及 [P6.2b 准备报告](P6_2B_PREPARATION_2026-10-18_18-04-33.md)，复用既有准备结论并以当前现场核验为准。Demo 根目录没有 AGENTS.md。准备报告中的旧 HEAD 与“停止发送即可恢复”属于历史表述；本报告明确区分当前 SHA、停止流量和业务恢复。

| 仓库 | 分支 | 开始时 HEAD / 本轮核实真实远端 SHA | origin |
| --- | --- | --- | --- |
| Incident Agent | main | 67ca549a6f1783ef5f1937315a6f546aa33a0a62 | https://github.com/yuj1ng1/incident-agent.git |
| prometheus-demo | codex/postgres-monitoring | 0e2f6f3220b9fb9f0baf8730de5ffaaac00d8c38 | https://github.com/yuj1ng1/prometheus-demo.git |

两个仓库的原有用户修改均保留。Incident Agent 的 README、生产 Prompt、temp_test.py、历史文档迁移/删除和其他原有未跟踪文件，以及 Demo README，均排除于本次提交。原暂存区为空。本轮只提交两个既有实验脚本的最小扩展、两个新实验脚本、两个测试文件和本报告。没有新依赖，没有改动生产 Prompt、Remediation、安全预算或监控阈值。

目标 PID **29476**，进程命令为 `D:\conda\envs\agentlearn\python.exe -u d:\prometheus-demo\app.py`，监听 `0.0.0.0:8001`；固定请求为 `GET http://localhost:8001/error`，没有改为 127.0.0.1。Windows 进程创建时间是 `2026-10-18T03:31:29.2333240Z`，应用 `/version` 的 `process_started_at=2026-10-18T03:31:32.744052Z` 是应用初始化时间，两者语义不同。`service=order-service`，两个运行 flags 一直为 false，`commit_sha=null`，所以运行代码 SHA 保持 **unknown**，不能以仓库 HEAD 代替。容器挂载和监控配置归属保存于 `data/p62b-host-audit-20261018/`。

用户先明确“同意授权”，随后确认“我全程没动，你自己按授权来就行”。在最后现场复核后生成本次授权 `authorization.json`：run_id 为 `p62b-first-error-20261018-01`，记录时间 `2026-10-18T11:52:16.428025+00:00`；这是授权落盘时间，不虚构为用户消息时间。preflight 完成于 `11:52:16.411085+00:00`。授权具体包含 PID29476/8001、固定 GET `/error`、预期 HTTP500/错误日志/指标更新、最多30次故障请求、helper最多60秒、非500/超时/身份或flags变化/目标不符/预算不足即停，以及结束后核对helper退出、Demo身份和只读Prometheus/Loki。此前 `/order` 授权未沿用。

并发核对依据为用户无操作声明、当时无运行中的 pytest 或实验命令、连接检查和业务计数未变。PID29888 为同解释器的无脚本名 Python，**其用途仍未独立查明**；授权记录保留此限制，不能声称绝对排除隐藏后台活动。此前出现的 pytest PID41700 已退出；8004 的 uvicorn 和其他进程未被终止。固定授权消费目录保存 `SHA-256(run_id).consumed.json`，同一授权复制或改名仍不能复用；本次标记已消费，不因指标缺失解除。

## 2. 实际请求数及 unknown 语义

监督器开始于 `2026-10-18T11:52:51.476315+00:00`，结束于 `11:53:26.952976+00:00`，monotonic 总耗时 **35.469秒**。helper PID43932，退出码0，`child_stopped`，未触发截止、未强杀。5个完整 `version_before -> fault -> version_after` 周期后，因下一轮完整最坏预算不足而停止，`stop_reason=p62b_insufficient_full_round_budget`。

| 本阶段归档 | 全部真实 HTTP | 版本核验 | /error | 其他只读 |
| --- | ---: | ---: | ---: | ---: |
| offline-preflight | 6 | 1 | 0 | 5 |
| authorized-preflight | 4 | 1 | 0 | 3 |
| 本次根目录/preflight | 2 | 1 | 0 | 1 |
| experiment/traffic | 15 | 10 | 5 | 0 |
| observations | 9 | 1 | 0 | 8 |
| 合计 | **36** | **14** | **5** | **17** |

表中前两项分别位于 `data/p62b-offline-preflight-20261018/` 和 `data/p62b-authorized-preflight-20261018/`。本次根目录范围合计26次HTTP，其中版本12次、故障5次、其他只读9次；不能把准备核验算成故障业务请求。真实 `/order`、DeepSeek、Remediation 均为 **0**。

`business_route_requests=5`、`identity_verification_requests=10` 和 `expected_500_responses=5` 均由完整最终子报告及原件校验，监督器计数状态为 `verified_child_report`，本次没有未知总数。代码和测试仍要求：截止中断、最终报告缺失或校验失败时，总数保持 `unknown/null`，已有完整原件仅证明下限。进入本地 Session.request 不独立证明服务端收到；本次500响应、日志和Counter只提供限定的交叉支持。

## 3. HTTP 500 原件和实现边界

新增 [p62b_error_experiment.py](../scripts/p62b_error_experiment.py)，默认输出 `offline_plan_only`，不产生真实请求。真实 CLI 必须显式 `--execute`、提供本次结构化授权和新输出目录。500 是此固定测试路由的预期状态；200、404、302等停止，不套用 `/order` 的200成功条件。

复用 [P6.2a受限监督器](../scripts/p62a_business_baseline.py)，父子进程共享绝对monotonic截止，包含helper启动开销；每轮保留前后身份和开关核验，串行、无重试、无重定向、无环境代理。开始新轮次前预留三请求24秒最坏预算及最终落盘2秒，轮间至少1秒。这避免临近截止启动无法完整收尾的周期，没有删除安全检查、改变地址或客户端连接方式。localhost约2秒的延迟根因仍未确认；未追加连接诊断。按当前约7秒/轮及保守预算，本轮只完成5次，不能承诺在60秒内达到20次门槛。

原件位于 `experiment/traffic/`。下表均为 **2026-10-18 UTC**，来源URL固定为 `http://localhost:8001/error`，每份body为21bytes：`Internal Server Error\n`。

| 原件ID | HTTP开始 | HTTP结束 | 状态 |
| --- | --- | --- | ---: |
| http-002 | 11:52:54.430057 | 11:52:56.445634 | 500 |
| http-005 | 11:53:01.501397 | 11:53:03.513664 | 500 |
| http-008 | 11:53:08.556101 | 11:53:10.562627 | 500 |
| http-011 | 11:53:15.762671 | 11:53:17.775888 | 500 |
| http-014 | 11:53:22.806964 | 11:53:24.815895 | 500 |

五份body的SHA-256均为 `e41656eb2ba6c6293bf6dd928e5a88cdbc50535cab661c1969e0f598e497ed62`。每条请求的JSON另存UTC起止、phase、请求/响应URL、状态、bytes和body哈希；完整清单包含版本核验原件。头部按白名单保存，不保存Set-Cookie。父监督器只可通过自己创建的Popen对象终止其helper，不扫描或终止其他进程。

## 4. Prometheus 指标与真实告警状态

已读取实际 `alerts.yml`：HighHttp5xxRate 使用1分钟窗口、请求增量至少20、5xx比率严格大于20%、scrape up、条件持续10秒；scrape/evaluation均为5秒。规则未修改，未为通过实验降低门槛或延长流量。

本轮只做一次后续只读采集，9次HTTP全部归档于 `observations/`。采集入口起止为 `2026-10-18T11:54:48.842673+00:00` 至 `11:54:56.306126+00:00`，实际首个HTTP请求为 `11:54:53.968607+00:00`。

**收尾操作存在时序缺口**：原计划停流后约15秒采集，但第一次PowerShell向Python `-c`传参时引号处理导致SyntaxError，在任何网络调用前失败；修正为stdin代码后才执行唯一一轮采集。采集入口晚于最后故障 **84.026778秒**，首个实际HTTP观测晚 **89.152712秒**。没有补跑故障，也没有第二轮采集。以下instant结果不能代表故障期间峰值或覆盖整个故障窗口。

| 指标 | 实际结果 | API evaluation UTC |
| --- | --- | --- |
| http_requests_total | ok，13.0；预检累计8，差值+5 | 11:54:54.056000 |
| http_request_rate | ok，0.0 | 11:54:54.070000 |
| service_scrape_up | ok，1.0 | 11:54:54.100000 |
| http_5xx_rate | no_data，原API空vector | 无有效样本，不编造时间和值 |
| http_latency_p95_seconds | no_data，原API空vector | 无有效样本，不编造时间和值 |

`http_requests_total` 是按service聚合，未按status拆分，不能冒称已取得500专属Counter增量或Histogram增量。5次故障不满足20次门槛，且实际采集已不与故障一分钟窗口重合；两项限制都保留。晚期request rate=0只表示该查询窗口观测，不代表业务恢复。

完整alerts快照中无order-service/HighHttp5xxRate，因此**采集时状态为inactive**；未捕获该服务pending或firing。不能据此断言整个实验从未pending/firing。实际rule返回：state=inactive、health=ok、duration=10、`lastEvaluation=2026-10-18T11:54:53.949805044Z`、`evaluationTime=0.000578247`；group `lastEvaluation=2026-10-18T11:54:53.949722837Z`。order-service未返回activeAt，不编造。rule-wide状态与单服务快照的适用范围分别保留。

原快照仍有 **inventory-service/ServiceDown firing**，其 `activeAt=2026-10-18T03:29:26.669063896Z`，不属于本轮order-service故障。完整原快照归档，没有删除该条来伪造单服务监控源。真实500不能直接宣称HighHttp5xxRate已触发。

## 5. Loki 错误日志

实际LogQL为 `{service="order-service"}`。HTTP原件确定故障窗口：`[2026-10-18T11:52:54.430057Z, 2026-10-18T11:53:24.815895Z)`。Loki查询范围为同一起点至 `11:53:34.815895Z`，末端固定延后10秒容纳摄取；两种窗口各自保留，没有把摄取时间当成请求发生时间。

实际返回5条，均为 `YYYY-MM-DD HH:MM:SS ERROR service=order-service status=500 request failed`。现场Windows为China Standard Time / UTC+08:00，按该已核实偏移转换正文时间，不使用会话Asia/Tokyo。

| 正文事件时间（UTC+08:00，2026-10-18） | Loki返回UTC（2026-10-18） |
| --- | --- |
| 19:52:56 | 11:52:56.890409532Z |
| 19:53:03 | 11:53:03.745373808Z |
| 19:53:10 | 11:53:10.803780307Z |
| 19:53:17 | 11:53:17.856416369Z |
| 19:53:24 | 11:53:24.918736582Z |

正文秒级时间按一秒区间与故障范围相交核对；Loki时间独立核对查询范围。5条均符合内容、服务及窗口，候选数5。Demo日志没有path或共享request_id，因此仅能作**时间、内容、数量的限定关联**，不能证明逐请求强身份匹配，也不能把历史日志或其他服务错误算入本次。没有以缺日志推导健康或恢复。

## 6. 时间关联与证据完整性

HTTP原件UTC、日志正文秒级时间、Loki纳秒时间、Prometheus evaluation及实际采集时间均保留，未将不同时间语义合并。Alloy配置无正文时间解析stage，故Loki时间不能替换业务事件时间。

关键响应body SHA-256如下，完整JSON/body及工具参数在各子目录清单中：

| 原件 | SHA-256 |
| --- | --- |
| observations/http-001.body，完整alerts | 11215b6992c7445495cf0479f54e42ee1f85b6d9ac43c8cf055fadf42ceb647c |
| observations/http-002.body，rules | 320f6512ce0050897e061d0f46513402dd3a27a150aedc25d99c30e801e9a406 |
| observations/http-008.body，Loki | 45029dae1b6fb30f2d4efb95ddcddd31b29e341d6bd3b121227f5b7a0bf115c3 |
| observations/http-009.body，version | 6beb998c0a9ccaf082157804955058459f81452b3eeb1c535128681e830509f9 |

离线 `_verified_child_report(..., 30)` 与 `verify_capture(observations)` 均通过：核对计数、manifest、文件集合、JSON/body哈希、字节数、请求顺序及时间包围关系；工具envelope与URL、PromQL、LogQL/范围、原始响应绑定，并重新解析。6份既有manifest分别核验35、32、19、5、7、3条，全部一致。根目录另保存收尾摘要、授权消费标记副本和整体sha256清单。哈希证明本地归档一致性，不独立证明外部真实性；真实原件不加入Git。

## 7. Runtime 调查链与 Mock/真实来源

新增 [p62b_evidence.py](../scripts/p62b_evidence.py) 复用原Prometheus/Loki工具、解析器及 [P6.1隔离入口](../scripts/p61_readonly_integration.py)，运行既有 IncidentMonitor -> InvestigationManager -> Worker -> RuntimeStore。Store创建于独立TemporaryDirectory，结束后归档于 `runtime-replay/runtime/stores/`；没有创建第二套Agent、Worker、Store或注册系统。生产main.py、RecoveryWorker、真实模型、数据库探测及Remediation均未启动。

隔离规则明确为 `labels.service == "order-service" AND labels.alertname == "HighHttp5xxRate" AND state == "firing"`。若真实快照满足才使用 `real_firing_alert_snapshot_replay`；否则保留真实快照并使用 `synthetic_alert_input`。本轮没有捕获order-service firing，**实际输入为合成告警**，`synthetic_fallback_reason=no_captured_order_service_firing`；inventory原始firing保存于完整快照，没有进入本次调查。

实际回放开始于 `2026-10-18T11:55:46.619383+00:00`，保存alerts快照原采集时间 `11:54:53.999316+00:00`。Session `ses-6058f2a930ed4df3a2e17806a8daa89c`、Incident `inc-c6814073`，Session为COMPLETED，**2次Mock模型轮次、4次工具执行**。模型来源固定为 `scripted_mock_sdk_not_real_deepseek`。COMPLETED仅表示这条模拟调查流程完成，未确认根因或恢复。

回放保留原件 `captured_at/source/captured_result`，外层历史证据标记no_data及 `use_for_current_state=false`，不能用Runtime的处理时间续期。真实指标no_data不补零，日志原件仍保留5条但不冒充当前状态；运行版本原件为commit_unknown；未采集的health保持 `observation_not_collected`。Mock最终文字明确未确认根因、真实模型能力及业务恢复。回放新增真实服务请求、真实DeepSeek和修复调用均0。

## 8. 测试与验证结果

解释器为 `D:\conda\envs\agentlearn\python.exe`：Python3.12.14、Pydantic2.13.5、pytest9.1.1、requests2.34.2、OpenAI3.14.0。已核对解释器与导入路径，没有大规模升级依赖或新增依赖。测试结果来自真实运行日志：

- 定向测试：**211 passed in61.33s**，`data/p62b-final-targeted-20261018.log`，覆盖P6.1采集/回放、P6.2a监督器、原件分析及本轮入口/证据。
- 受限完整回归：**2130 passed, 2 skipped, 8 deselected, 190 subtests passed in761.39s**，退出码0，`data/p62b-final-verified-regression-20261018.log`。
- Demo离线unittest：**21 tests in11.999s，OK**；Flask test client、Mock DB和临时日志，未请求运行中的Demo。
- `pip check`：**No broken requirements found**。
- 真实外部联调仅为本报告所列的一轮故障及限定只读观测；没有真实模型验证。

完整回归使用 `data/p62a-20261018T075858Z-719154a6/p62a_network_guard.py`，阻止未Mock网络，仅允许Windows asyncio内部socketpair唤醒。排除以下真实loopback测试函数，共8实例；它们未运行，不写成通过：

| 排除测试函数 | 实例数 |
| --- | ---: |
| test_first_worker_skips_yesterdays_legacy_clear_without_real_http | 4 |
| test_background_startup_only_probes_durable_jobs | 1 |
| test_real_http_transport_against_isolated_loopback_server | 1 |
| test_real_redirect_is_refused_before_library_drains_body | 1 |
| test_real_loopback_http_ignores_environment_proxy | 1 |

两项skip为 `DependencyTests.test_clean_install_has_all_pins_and_no_legacy_httpx`（未设置INCIDENT_AGENT_CLEAN_ENV=1）及 `test_external_rules_contract_when_explicitly_configured`（未设置ALERT_RULES_PATH）。实际告警配置另已直接读取核对，不将skip当作规则测试通过。

新增最小测试覆盖：未授权拒绝；预期500；200/404/302拒绝；超时、身份/flags/目标变化停止；30次/60秒上限；截止/缺报告计数unknown；只终止自建helper；授权跨路径防复用；禁止真实模型/修复；指标日志缺失不伪造；firing与合成输入区别；原件哈希、时间和历史回放不续期。

较早非最终回归有 `1 failed, 2113 passed, 2 skipped, 8 deselected, 190 subtests passed in915.67s`，失败为UTC的Z/+00:00字面串断言，已改为同一时刻语义；另一次为修复授权复用主动中断，exit -1，无最终结果。原日志保留，两次均不冒称全通过。最终测试后业务源码未再修改。

## 9. 故障停止和收尾核查

故障流量已停止，helper43932正常退出。`postcheck-host.json` 的 `11:54:45.0762715Z` 核查及最终 `12:02:48.4543645Z` 主机核查均显示helper不存在、原Demo PID29476/启动命令/创建时间和8001监听未变化。后续version原件也保留同一应用启动时间及false flags；运行SHA仍unknown。

8项保护文件哈希与任务前记录一致：Demo app/alerts/Prometheus/Alloy配置、Agent生产Prompt、temp_test.py以及两个README。没有修改故障开关、重启、停库或自动修复，没有终止未知进程。全工作区diff检查可见用户既有temp_test.py空白问题，任务范围检查不修改该文件。

**停止发送请求只表示停止故障流量，固定 `/error` 路由仍定义为500；没有进行业务恢复验证。** 晚期inactive或request rate=0不能解释为恢复。没有自动发送 `/order`，也未在证据不足时重跑。独立 `/order` 正常业务验证需要另列具体次数、时间预算和新的授权，不在本轮执行。

交付采用选择性提交推送，仅上述7个文件；本地运行数据、授权消费标记和用户原有修改均保留且不提交。推送后核对本地与真实远端SHA，并停止等待用户安排的独立代码审查。本轮内部校验不替代该审查。

## 10. 当前是否具备进入真实模型调查的条件

**尚不具备。** 已确认固定路由真实HTTP500、Counter变化及限定关联的错误日志，且既有Runtime的隔离Mock流程可运行；但没有捕获真实order-service firing，指标与故障窗口未对齐，5xx/P95仍no_data，运行代码SHA未知，独立代码审查尚未完成，连接延迟根因也未验证。不能把合成输入或Mock文字补成真实告警和DeepSeek结论。

本阶段真实DeepSeek上限和实际值始终为 **0次**。后续真实模型阶段仍需独立明确授权；本轮不以证据缺口为理由自动扩大请求、改变核验策略、地址、连接方式或告警门槛。
