# P6.5 第二次真实多轮调查实验：离线诊断与授权前准备

本轮没有发起真实 DeepSeek 请求，也没有发起新的 order-service 业务流量、Prometheus/Loki HTTP 或故障注入。实验准备未启动生产 main.py、生产调度/恢复流程，未执行真实业务 Remediation；回归中的 Scheduler、RecoveryWorker 和 Remediation 仅用于隔离测试及 Mock。没有创建新付费授权或消费记录，没有使用旧授权余量，没有进入 P6.6。

离线材料已冻结，但本次受限完整回归仍失败；不具备无条件真实付费实验准入。保留全部失败并停止，不再追加诊断或真实请求。

## 1. Git、环境与工作区基线

- 已读取当前根目录 [AGENTS.md](../AGENTS.md)。开始时本地 main HEAD、origin/main、`git ls-remote origin refs/heads/main` 均为 `6cf6f07044e1002991a9c264d4c97c3ccf2fb74c`，暂存区为空；最终核对见 [final-verification.json](../data/p65-second-preflight-20261019-01/final-verification.json)。未提交、未推送。
- 指定解释器 `D:\conda\envs\agentlearn\python.exe`，实际 Python 3.12.14，Windows 11 / build 26200；OpenAI SDK 3.14.0、Pydantic 2.13.5、httpx2 2.13.0、pytest 9.1.1、requests 2.34.2、psycopg 3.3.6。Runtime SQLite SCHEMA_VERSION=1，Checkpoint schema_version=1。没有安装、升级或新增依赖。实际 `pip check` 返回 `No broken requirements found.`
- [baseline.json](../data/p65-second-preflight-20261019-01/baseline.json)记录 5,355 个既有文件哈希及初始 Git 状态；仅记录凭据存在性和来源，不输出密钥内容。保留 README、prompts/agent_prompt.py、temp_test.py、原文档删除/归档、未跟踪文件和旧测试目录。没有 reset、clean、stash。
- 本轮修改 [tests/projection_process_fixture.py](../tests/projection_process_fixture.py) 与 [tests/runtime_process_fixture.py](../tests/runtime_process_fixture.py) 两个测试夹具的启动重试，新增 [tests/test_process_fixture_startup.py](../tests/test_process_fixture_startup.py) 的三项 Mock 边界测试。生产 Agent、Worker、ModelClient、Store、Checkpoint、工具权限、审批和 P6.5 三个脚本相对 HEAD 无修改。没有新增生产接口、数据结构或依赖。
- 当前用户 Prompt 有未提交修改，但 P6.5 使用显式 TASK；首轮真实发送体由冻结文件绑定，不读取这个用户 Prompt 作为实验指令。HEAD 并不单独代表整个脏工作区，实验同时绑定 18 项源码哈希；两个测试夹具修复单独记录，不冒充已提交版本。

## 2. 历史原件、失败 Session 与旧授权保护

复核 [原 P6.5 报告](P6_5_VALIDATION_2026-10-19_11-37-25.md)、[兼容修复报告](P6_5_PROTOCOL_COMPATIBILITY_VALIDATION_2026-10-19_14-47-37.md)、实际原 API/SDK JSON、请求、summary、SQLite 及授权消费。以当前原件和实现为依据，没有仅采信旧报告结论。

| 绑定对象 | 当前 SHA-256 |
| --- | --- |
| P6.2b 根 manifest（76 文件，绑定其余 75 项） | `350578d6914f7f09a530d85178f491da2366521b25b5f5fe4b5872bd58e5ae08` |
| 旧 P6.5 live manifest（绑定其余 12 项） | `9e810da91ccfe6dea8cf9d3ee045e72f9f1122b636b4854df381038d91ee0f14` |
| 原 response-01.raw.txt | `5de921df0aa7736fb7259e4a91cddef9168f250a1c913fc4eea294c94136772c` |
| 原 response-01.sdk.json | `e0e766a9faec7a0abb91a38cb0b408a53db3389abddd783dffe6c3ca715e95d2` |
| 已消费的旧授权记录 | `8a07f530670a7cb5b64c1b802ac7f5d6c4a5f0a90970711d1eeb42f06f626ed3` |

原 Session `ses-41e8e4e92f8b445ea62085ba48d2883e` 用只读 RuntimeStore 核对：仍为 FAILED、模型计数 1、工具执行 1。旧授权计划 SHA `d9efc7591dc5ba70f5c62a4f9d454fff759628e19ab03685cf998c26b2f6d41d` 的消费文件仍存在且内容未改。旧授权中的独立审查豁免只适用于旧实验，不能延伸至这次实验。

本地哈希证明封存一致性，不是 Provider 签名或计费证明。原始回答、失败终态及原授权均未改写。

## 3. 跨进程测试的有界诊断（3 次已用尽）

两项测试实际使用 subprocess.Popen，子进程执行指定 Python 和 `-B`；没有使用 multiprocessing.Process，因此不能把问题直接归为 Windows multiprocessing spawn。claim 测试 cwd 为项目根；双 writer 的 cwd 为独立 TEMP。夹具通过项目路径与继承的 PYTHONPATH 找到 runtime_support，实际 exe、cwd、sys.path、PID 都已记录。

原测试的 ready 分别在运行时/Store 初始化后写出。父测试仅轮询文件，在 finally 才 poll/communicate；子进程若在 ready 前退出，表面上可能仅呈现启动超时。锁文件存在本身不能证明锁被占用。

| 次数 | 实际执行 | 结果及证据范围 |
| --- | --- | --- |
| 1 | 原样运行两项指定测试，附加本地旁路 | `2 failed in 74.12s`。claim 在 ready 屏障失败；两个子进程随后 exit=0。writer 的 monitor 在 IncidentStore 初始化时发生真实 FileLockError、ready 未写、exit=1；worker exit=0、写 30 次。|
| 2 | 独立 TEMP 中由父进程持有实际 Windows 文件锁，再启动未修改的 writer 夹具 | 子进程 exit=1、stderr 明确 FileLockError，ready-monitor=false；复现脚本断言通过。该次是故意诱发的失败，不是生产 Store 测试通过。|
| 3 | 修复 writer 夹具后、最终 claim 夹具修复前，唯一一次受限完整回归，并继续旁路记录指定测试及共享夹具用例 | 1 failed, 2292 passed, 2 skipped, 8 deselected, 190 subtests passed in 897.85s (0:14:57) |

[diagnostic-summary.json](../data/p65-second-preflight-20261019-01/diagnostic-summary.json)汇总每个子进程的启动、ready 写入、atexit、父进程观测退出时间、退出码、stdout/stderr、文件状态、文件锁及 SQLite 阶段耗时。完整原始记录分别位于 diagnostic-1/2/3 日志；第 3 次另有逐进程 stdout/stderr 文件。所有运行有界，无循环重跑。

| 诊断/角色 | PID | Popen 返回 UTC | ready write-open UTC | 父进程观测退出 UTC / code |
| --- | --- | --- | --- | --- |
| diagnostic-1/原claim/one | 26432 | 2026-10-19T08:16:13.300199+00:00 | 2026-10-19T08:16:46.282118+00:00 | 2026-10-19T08:16:46.791091+00:00 / 0 |
| diagnostic-1/原claim/two | 12744 | 2026-10-19T08:16:13.377917+00:00 | 2026-10-19T08:16:46.561070+00:00 | 2026-10-19T08:16:46.967172+00:00 / 0 |
| diagnostic-1/monitor | 17860 | 2026-10-19T08:16:48.895149+00:00 | 未写 | 2026-10-19T08:17:18.929944+00:00 / 1 |
| diagnostic-1/worker | 13772 | 2026-10-19T08:16:48.939219+00:00 | 2026-10-19T08:17:18.394027+00:00 | 2026-10-19T08:17:21.734875+00:00 / 0 |
| diagnostic-3/monitor | 38860 | 2026-10-19T08:29:10.354855+00:00 | 2026-10-19T08:29:19.823279+00:00 | 2026-10-19T08:29:26.000963+00:00 / 0 |
| diagnostic-3/worker | 34776 | 2026-10-19T08:29:10.516584+00:00 | 2026-10-19T08:29:19.847509+00:00 | 2026-10-19T08:29:26.194824+00:00 / 0 |
| diagnostic-3/其他回归claim/one | 33776 | 2026-10-19T08:33:27.933206+00:00 | 未写 | 2026-10-19T08:33:57.990443+00:00 / 1 |
| diagnostic-3/其他回归claim/two | 12812 | 2026-10-19T08:33:27.973812+00:00 | 2026-10-19T08:33:33.681218+00:00 | 2026-10-19T08:33:58.360342+00:00 / 0 |
| diagnostic-3/原claim/one | 39680 | 2026-10-19T08:36:32.349663+00:00 | 2026-10-19T08:36:38.821051+00:00 | 2026-10-19T08:36:39.574560+00:00 / 0 |
| diagnostic-3/原claim/two | 17036 | 2026-10-19T08:36:32.410713+00:00 | 2026-10-19T08:36:38.855574+00:00 | 2026-10-19T08:36:39.738152+00:00 / 0 |
| diagnostic-2/持锁复现 | 41028 | 2026-10-19T08:18:36.055166+00:00 | 未写 | 2026-10-19T08:18:51.577093+00:00 / 1 |

时间记录使用 UTC；atexit 与父进程观测退出是时间边界，不冒充内核精确退出时间。ready 时间为 write-open 审计点，实际存在性另有退出时快照。FileLock 是非阻塞锁；None 返回与 traceback 表明失败。SQLite 的计时包含 connect、PRAGMA 和 BEGIN IMMEDIATE，不能称为纯锁等待时间。

前两次诊断对导入过程做了 Python profile，存在观察者效应风险；第 3 次改为 runtime_support 导入完成后再启用，减少对 SDK/Pydantic 导入的扰动。第 1 次约 33 秒的 claim ready 不能作为无旁路环境性能证据，更不能反向解释历史完整回归。子进程在 finally 放行后退出，也不代表原 claim/fencing 断言已执行。

### 确定性夹具缺陷及最小修复

确认 `projection_process_fixture.py` 的 `IncidentStore(...)` 初始化发生在 ready 前、也在原有写入锁重试之外；并发初始化可触发非阻塞 FileLockError，导致子进程退出。第 1 次捕获实际异常，第 2 次用父进程持锁复现。旧完整回归缺少对应 stderr，因此不能认定旧 writer 超时必然是同一原因。

最小复现旁路为 [reproduce_startup_lock.py](../data/p65-second-preflight-20261019-01/reproduce_startup_lock.py)，当时未修改的夹具另存为 [projection_process_fixture.before.py](../data/p65-second-preflight-20261019-01/projection_process_fixture.before.py)，与初始工作区哈希逐字节一致；原输出见 [diagnostic-2-reproduction.json](../data/p65-second-preflight-20261019-01/diagnostic-2-reproduction.json)。该复现脚本在第 2 次运行时使用当时的原测试路径；修复后不再重复该诊断。另封存 [runtime_process_fixture.before.py](../data/p65-second-preflight-20261019-01/runtime_process_fixture.before.py) 以绑定第 3 次失败发生时的共享夹具版本。

仅给该夹具的 Store 构造增加最多 10 秒、间隔 2ms、仅捕获 FileLockError 的启动重试，与其现有每次写入重试政策一致。父测试 15 秒 ready、30 秒 communicate 和全部写入/合并断言未改；claim 测试及其 30 秒屏障未改。生产 FileLock、SQLite timeout、lease、fencing 未改。持续持锁仍明确失败，不把配置或解析异常转成成功。

完整回归失败项：FAILED tests/test_review_quality.py::test_two_incidents_race_with_real_processes_and_fencing。其 PID=33776 在 IncidentStore 初始化时 FileLockError，ready-one 未写，尚未执行 claim；另一进程退出 0。指定原 claim/fencing 和双 writer 在该次完整回归中通过，但不能替代整套通过。回归结束后仅修复共享 claim 夹具的有界初始化重试，新增 3 项不启动子进程的 Mock 重试边界验证；该修复后没有实际跨进程复验或第二次完整回归。最多三次诊断已用尽，不追加重跑。

完整回归结束后，根据 PID=33776 的实际 FileLockError，又仅为共享 runtime_process_fixture.py 的 claim-contender 初始化增加同样最多 10 秒/2ms 的 FileLockError 重试；其他 crash/wait-model 阶段仍使用原 create_runtime，不改变其异常和中断语义。新增 Mock 检查临时锁重试、持续锁到期停止、非锁错误不重试，实际 **3 passed in 6.05s**；它们不启动子进程，不计为额外跨进程诊断，也不能证明修复后真实并发已经通过。相关源码和原测试断言未被弱化。

## 4. P6.5 已有能力复用与兼容复核

继续沿用 InvestigationManager → Worker → Agent → ModelClient → ToolManager → RuntimeStore；没有另写协议适配器或运行时。阅读并复核 p65_model_boundary.py、p65_readonly_investigation.py、p65_protocol_replay.py、Worker、Checkpoint、ModelClient 及其调用方。

| 边界 | 实际复核结果 |
| --- | --- |
| 限定 Schema 兼容 | 原 API missing 与 SDK null 均只在 usage.input_tokens_details.cache_write_tokens 放宽；其他 SDK 字段继续 strict=True 完整校验，错误类型和其他必需字段缺失拒绝。|
| 原件与 usage | 不补 0、不改写 usage；未知缓存写入记录 value_known=false。当前原件及原输出逐项校验通过。|
| 协议身份 | 原 function_call、参数字符串、call_id 和顺序保留；SDK 解析结果必须匹配冻结 SDK 文件。|
| 持久化绑定 | pending 必须清空，valid_chain 必须通过；每个输出需匹配保存的调用与实际记录结果，缺失、游离、重复、篡改或未记录输出均停止。|
| 预算 | Worker 在模型/工具 I/O 前事务预留；adapter 再核对总模型预留计数并独立拒绝第 4 次 SDK 入口。含探针最多 4 次实际工具执行，缓存命中不伪装为新执行。|
| 每轮敏感检查 | 每个出站 body 与每份返回序列化结果做字段/值和实际 credential 值检查；超 131072 字节停止。源投影白名单为主要防线；不声称任意自然语言秘密都可被规则识别。|
| 权限 | Unknown/Remediation/人类输入工具拒绝；跨服务由原 Worker 拒绝并停止后续出站；registry 仅保留三种回放函数，禁止 HTTP、数据库、子进程、线程与修复调用。|
| 不完整与恢复 | 不完整响应、Provider/参数/绑定错误保留原件并停止；单 Worker.run_once、max_attempts=1、max_resumes=0，无 fallback 或自动 resume。|
| 结果信任 | 模型 COMPLETED 仍为 pending_review、trusted=false，不视为人类批准或业务恢复。|

当前定向测试包含真实封存首轮离线回放：原首轮来自真实 Provider，后续为明确标记的 scripted Mock；它验证原 call/output 经过 Worker 和 Checkpoint，不证明真实第二轮 DeepSeek 推理。真实 SDK MockTransport 测试只使用内存 transport 与 TEMP 合成授权，不是实际付费请求。

本轮没有发现需要扩展兼容字段或修改生产安全边界的证据；这只覆盖当前代码、SDK 版本和已有样本/负测，不是任意未来 Provider 格式的兼容保证。

## 5. 第二次真实实验预算与停止条件

| 项目 | 冻结上限/约束 |
| --- | --- |
| Provider / model / endpoint | DeepSeek / deepseek-flash / https://api.deepseek.com/responses |
| Incident / Worker attempt | 1 个隔离 synthetic Incident，1 次 Worker.run_once |
| SDK create | 最多 3 次；真实第 2 轮是目标，第 3 轮非必需 |
| 重试 / resume / fallback / HumanRequest | SDK=0，应用=0，resume=0，无 fallback，无主动人工问答 |
| 只读工具 | 最多 4 次执行，包含自动 get_running_version 探针；仅 query_metrics、query_logs、get_running_version |
| 请求限制 | 每次 timeout=30 秒、max_output_tokens=2048、出站序列化体≤131072 字节；合计输出配置最多 6144 tokens |
| 服务与材料 | 仅 order-service，data/p62b-first-error-20261018-01；没有新采集 |
| 禁止事项 | main.py、Scheduler、RecoveryWorker、Remediation、新故障、新业务流量、Prometheus/Loki/业务 HTTP |

30 秒是 SDK timeout 配置，不宣称所有底层阶段合计的硬墙钟上限。字节限制不是输入 token 上限。未在线查价、未验证账户余额或 Provider 可用性，也没有已确认的人民币费用上限；后续历史链会增长，最多三次均可能收费，失败/超时不能视为免单。

若第一轮无 function_call，如实记录并结束，不制造第二轮。合法工具请求实际执行或明确拒绝，输出持久化后才可进入真实第二次请求。真实第二轮需确实收到原调用和对应输出，并能够据结果继续或结束。若第二轮因 Provider 格式、预算或其他边界失败，冻结证据后停止，不补跑。模型继续申请超预算工具时，同样保留真实 EXHAUSTED/FAILED，不追求三轮或固定工具顺序。

## 6. 新请求及历史证据外发范围

可审查原件为 [prepared/first-request.json](../data/p65-second-preflight-20261019-01/prepared/first-request.json)、[package.json](../data/p65-second-preflight-20261019-01/prepared/package.json)、[plan.json](../data/p65-second-preflight-20261019-01/prepared/plan.json)，以及 [new-experiment-scope.json](../data/p65-second-preflight-20261019-01/new-experiment-scope.json)。

| 绑定 | SHA-256 |
| --- | --- |
| 新计划 | `0fe9d7cbe071f146b711f5c75f782bd127b99ce574f39891f782ac781863b9be` |
| 新范围 | `cca779f946b1fcb1025f67f9770c666c906cd52ff3daabd25cec99e6910c5cb5` |
| 首轮计划请求 body | `825cfc43c6c4e60e1fcced85eea16d3680d94158170e11cbbb6c88f3efc6a79d` |
| 历史投影 package | `0c99f1bbdab631c2e04e7b41aa72c12d1a993c9067a139c664519a8e24b0c99a` |

首轮 body 与旧实验相同，但新计划的兼容修复代码哈希已变化；同一首轮请求 SHA 不等于可复用旧授权。此处是拟发请求，还没有本次真实发送。

首轮仅包含冻结 TASK、合成告警标记、manifest、历史窗口、证据缺口、冻结版本探针、工具 schema 和预算。后续允许原输入、完整实际 Provider 输出和真正已提交并绑定的 function_call_output；第 3 次请求关闭 tools。没有授权夹带整个工作区、.env、数据库行、任意文件、实时服务观测或未经绑定的历史消息。

工具只按冻结参数精确回放 5 项指标（http_5xx_rate、http_requests_total、http_request_rate、http_latency_p95_seconds、service_scrape_up）、日志和版本；其他参数返回无 captured_result 的明确 no_data。外层 historical_only=true、use_for_current_state=false，captured_result.status 才表示原采集状态。原 no_data 不转 0，未知 commit 不填 HEAD。

- 故障窗口：`2026-10-18T11:52:54.430057+00:00` 至 `2026-10-18T11:53:24.815895+00:00`。
- 精确冻结 Loki 参数：start=`2026-10-18T11:52:54.430057+00:00`，end=`2026-10-18T11:53:34.815895+00:00`。省略日志时间只表示已有固定采集窗口；显式参数必须精确匹配，不改变字符串或边界来制造命中。
- 原采集窗口：`2026-10-18T11:54:48.842673+00:00` 至 `2026-10-18T11:54:56.306126+00:00`；首个采集 HTTP 开始于 `11:54:53.968607+00:00`。
- 原指标/日志采集时间约 `2026-10-18T11:54:54Z`，版本采集为 `11:54:56.306126+00:00`，每条实际完整时间在 package 中。采集晚于故障，不能称为故障当时的即时健康状态。
- 5 个 Loki 纳秒时间保存在 package 原 logs 中；正文秒级事件时间仍独立保留，不混同 Loki 时间。每次真实模型调用时间将由新 request/completion 记录；现在不存在本次真实模型时间。

若模型再次请求旧故障窗口日志，将返回 outside_approved_frozen_evidence/no_data，无 captured_result、无旧日志内容、无实时 Loki。该受限结果仍须原样持久化并可被下一轮解释。

## 7. 新授权与消费记录设计

[authorization-requirements.json](../data/p65-second-preflight-20261019-01/authorization-requirements.json)只是字段及流程要求；新范围中的授权时间和授权正文均为 null。没有生成 explicitly_authorized=true 的新授权，没有创建或占用新的真实消费文件。

未来人类新授权须明确绑定代码 SHA、新 scope/plan SHA、根 manifest、首轮请求 SHA、DeepSeek/model、3 次 SDK/4 次工具、无重试/恢复、上述历史字段及同范围动态输入、费用风险，并保存实际授权原文和带时区时间。源消息无精确时间时，只能记录实际收到确认后的 UTC 及时间来源，不能伪造原消息时间。

继续使用现有 validate_approval 和全局单计划一次性 ledger：在获取凭据、构造 SDK 前，用 xb + fsync 独占创建 `<新plan_sha>.consumed.json`；失败后仍占用，不能把“未用完请求”当新授权或重新执行许可。旧 ledger 保留。

执行前还须重新核对真实远端/HEAD、18 项代码哈希、额外范围文件 hash、环境、准备包及历史 manifest、ledger 空缺、人类新授权。当前 validate_approval 直接校验 plan/request/manifest 和预算；额外 HEAD/scope 字段通过这项操作前核验绑定，不能谎称当前校验器自动检查所有附加字段。无需为此再开发协议适配器。

## 8. 实际验证与完整回归

| 验证层级 | 本轮实际结果 |
| --- | --- |
| 首次定向命令的收集错误 | 引用了不存在的 test_investigation_runtime.py；exit=4、no tests ran。保留原日志，纠正为实际 test_runtime.py 后运行下列批次。没有把收集错误算成测试通过。|
| P6.5 Schema/协议、Worker/Runtime、ResultTrust、P6.4 离线调查定向 | **195 passed，21 subtests passed in 159.41s**。|
| 指定跨进程原测试诊断 | 第 1 次 2 failed；第 2 次最小持锁复现符合预期；第 3 次结果详见完整回归。|
| 最终夹具修复后的 Mock 单元检查 | **3 passed in 6.05s**，无子进程；[fixture-startup-unit.txt](../data/p65-second-preflight-20261019-01/fixture-startup-unit.txt)。|
| 唯一一次受限完整回归（最终 claim 夹具修复前） | **1 failed, 2292 passed, 2 skipped, 8 deselected, 190 subtests passed in 897.85s (0:14:57)**。原始输出 [restricted-regression.txt](../data/p65-second-preflight-20261019-01/restricted-regression.txt)。|
| 依赖 | pip check 无破损依赖；没有新增依赖。|
| 静态/一致性 | 22 个相关源文件 AST 可解析；两项夹具 diff check 通过；18 项 prepare 代码哈希一致；5353 个既有文件未改，两项预期差异为测试夹具；新范围/准备包/ledger 状态复核通过。 |
| 真实外部服务与真实模型 | 均未执行，不以 Mock/内存 SDK/历史重放冒充。|

定向命令运行 test_p65_protocol_compatibility.py、test_p65_readonly_investigation.py、test_runtime.py、test_result_trust.py、test_p64_offline_investigation.py。完整回归命令、全部时间和退出码保存在 diagnostic-3-parent.jsonl。

完整回归排除的 8 项不计为通过：test_real_loopback_http_ignores_environment_proxy（1）、test_first_worker_skips_yesterdays_legacy_clear_without_real_http（4）、test_background_startup_only_probes_durable_jobs（1）、test_real_http_transport_against_isolated_loopback_server（1）、test_real_redirect_is_refused_before_library_drains_body（1）。两个 skip 为未启用 clean-install 独立环境检查、未设置 ALERT_RULES_PATH 的跨仓库规则检查；均未计为通过。

所有验证使用继承到子进程的 socket/DNS/UDP 审计 guard；仅放行 Windows 标准库 socketpair 的内部唤醒连接。完整回归额外禁止真实 psycopg.connect，测试自身 Mock 不受影响。拒绝尝试记录在 network-denials.jsonl，不等于真实服务请求。P6.5 内存 SDK 测试的计数字段只表示注入 transport 的 SDK 入口，不累计成付费请求。

## 9. 准入、未排除风险与独立审查建议

本轮未给出真实付费执行准入。已确认完整回归失败发生在共享测试夹具的 IncidentStore 初始化，尚未执行 claim；当前未发现 lease/fencing、模型预算、工具权限或审批失效的证据。P6.5 是单进程的一次 Worker.run_once，guard 禁止子进程和线程，与该并发启动路径不同。但共享夹具的最终修复没有实际跨进程复验，完整回归也仍保留失败，因此不能无条件确认全部准入条件，按本次任务第八节停止，交独立审查。新 scope/plan 只是可审查材料，不代表任何审批，也不在当前状态申请立即付费执行。

仍未验证：真正 DeepSeek 第 2 轮往返、当前 Provider 兼容/可用性、实际费用、所有未来输出排列形式、生产部署端到端行为及业务恢复。provider_verified 继续 false，生产 Worker 的 evidence_rebuild 策略不变。

建议独立审查者核对：两个测试夹具 diff、新增 Mock 边界测试与原有非阻塞锁契约；三次原始进程日志及观察者效应；P6.5 raw/SDK 双校验和 output→Checkpoint→record 绑定；新范围/请求/ledger 绑定；真实实验结束后逐条审查未经改写的模型原答。独立审查建议不是本轮已经完成独立审查的声明。

历史完整失败仍保留：首次 P6.5 为 `1 failed, 2232 passed, 2 skipped, 8 deselected, 190 subtests`（writer ready）；兼容修复阶段为 `1 failed, 2292 passed, 2 skipped, 8 deselected, 190 subtests`（claim ready）。本轮通过或单独复测都不能抹去它们，也不能宣称历史根因已经完全排除。

本阶段完成后停止。需要新的人类付费调用授权才能执行新实验；没有自动提交/推送，没有进入 P6.6。
