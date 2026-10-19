# P6.5 协议兼容修复与冻结真实响应离线复验

本轮解决了已确认的 cache_write_tokens 元数据兼容阻断。首次真实响应中的 commentary 和三个 function_call 原样经过现有 Worker，三个工具结果提交后进入下一轮输入；下一轮仅为脚本化 Mock。**这证明离线工程路径能够处理该冻结响应，不代表 P6.5 真实多轮调查通过。**

本轮新增真实 DeepSeek 请求、业务请求、Prometheus/Loki HTTP、故障注入和真实 Remediation 均为 **0**，没有启动生产 main.py、Scheduler 或 RecoveryWorker，没有创建新真实付费授权，没有使用旧实验剩余额度，没有进入 P6.6。受限完整回归仍有一项未解释的跨进程屏障失败，不能报告完整回归通过。

## 1. 基线与原件复核

- 已读取实际根目录 [AGENTS.md](../AGENTS.md)。开始时本地 main HEAD、origin/main 与 `git ls-remote origin refs/heads/main` 均为 `b8848485e51271954453d2cc25f86c3728c39dff`；提交前再次查询真实远端，仍为该 SHA。开始及暂存前的索引均为空。
- 使用 `D:\conda\envs\agentlearn\python.exe`，本机实际版本为 OpenAI SDK 3.14.0、Pydantic 2.13.5、httpx2 2.13.0。没有安装、升级或新增依赖。
- 已核对原 [P6.5 报告](P6_5_VALIDATION_2026-10-19_11-37-25.md)、原 API JSON、SDK JSON、请求、completion、summary、manifest、原授权与消费记录，并阅读 P6.4 离线测试、Worker、Checkpoint、ToolManager 和 ModelClient 的实际实现。
- 原运行目录 `data/p65-live-20261019-01/` 共 13 个文件，manifest 绑定其余 12 项，逐项重算通过。API 原文、SDK 文件在最终回放目录的副本与原件逐字节一致。原运行 manifest SHA-256 仍为 `9e810da91ccfe6dea8cf9d3ee045e72f9f1122b636b4854df381038d91ee0f14`。
- 原请求 body SHA-256 仍为 `825cfc43c6c4e60e1fcced85eea16d3680d94158170e11cbbb6c88f3efc6a79d`；最终回放使用同一请求范围、同一历史证据投影和同一自动探针结果。
- 原消费文件 SHA-256 为 `8a07f530670a7cb5b64c1b802ac7f5d6c4a5f0a90970711d1eeb42f06f626ed3`，仍保持占用；原 Session `ses-41e8e4e92f8b445ea62085ba48d2883e` 保持 FAILED、模型请求 1、工具执行 1。没有改写原失败终态或原报告。
- [保护复核](../data/p65-protocol-validation-20261019-01/preservation-final.json)确认 6,946 个既有文件哈希无差异，用户原有 README、Prompt、temp_test、文档删除/迁移及未跟踪文件状态不变。没有 reset、clean、stash 或清理旧数据。

| 封存文件 | SHA-256 |
| --- | --- |
| response-01.raw.txt | `5de921df0aa7736fb7259e4a91cddef9168f250a1c913fc4eea294c94136772c` |
| response-01.sdk.json | `e0e766a9faec7a0abb91a38cb0b408a53db3389abddd783dffe6c3ca715e95d2` |

这些哈希证明本地封存一致性，不是 Provider 签名。原文文件保存的是此前封存的 UTF-8 响应文本，不将其扩大描述为独立认证的底层 HTTP 传输字节。

## 2. 修改前的最小禁网复现

[reproduction-before-change.json](../data/p65-protocol-validation-20261019-01/reproduction-before-change.json)保存修改前的实际复算。SDK 的实际 `construct_type` 解析路径对冻结 API JSON 构造出的对象，其完整 model_dump 与封存 SDK JSON 相同；没有调用 responses.create，没有网络请求。

| 数据 | 原字段情况 | 完整 Response.model_validate(strict=True) 的唯一错误 |
| --- | --- | --- |
| 原 API JSON | input_tokens_details 只有 cached_tokens=0，cache_write_tokens 不存在 | usage.input_tokens_details.cache_write_tokens：missing / Field required |
| SDK 序列化 JSON | cache_write_tokens 已由原 SDK 解析表现为 null | 同一路径：int_type / Input should be a valid integer |

原 Provider 自报 input_tokens=2375、output_tokens=218、total_tokens=2593、cached_tokens=0、reasoning_tokens=0；未报告 cache_write_tokens，不能推断为 0。这也不是独立核实的账单。两份原件的 usage 均没有被修改。

该真实样本的其他字段在本机严格 Schema 下未产生第二项错误。四项 output 均完整：一条 phase=commentary 的 assistant message，随后三个 function_call；参数字符串、调用名、call_id、输出项 ID 和顺序均保留。此结论只覆盖本次实际冻结样本。

## 3. 最小修复及拒绝边界

| 文件 | 实际修改 |
| --- | --- |
| [p65_model_boundary.py](../scripts/p65_model_boundary.py) | 增加局部 validate_response_schema；明确未知元数据记录；冻结首轮入口及来源标记；收紧未知工具和未提交/无法绑定输出的停止行为。 |
| [p65_readonly_investigation.py](../scripts/p65_readonly_investigation.py) | 可传入 offline_first_response；付费模式与冻结重放互斥；摘要记录每轮来源。保留原 Runtime 上限和执行链。 |
| [p65_protocol_replay.py](../scripts/p65_protocol_replay.py) | 新增仅离线 CLI；复核原 manifest、原请求及已消费记录，调用已有隔离 prepare/run，随后只提供标记明确的 Mock 最终响应。没有 --execute 或授权创建功能。 |
| [test_p65_protocol_compatibility.py](../tests/test_p65_protocol_compatibility.py) | 新增结构负测、原件校验、真实首轮重放、绑定与预算负测。 |
| [test_p65_readonly_investigation.py](../tests/test_p65_readonly_investigation.py) | 更新已确认 usage 兼容预期；将非法/跨目标调用的停止行为拆分验证，保留注入、预算、缓存、恢复和消费防重放覆盖。 |

校验先执行完整 SDK Response 的 strict=True 校验。只有所有错误都严格位于 `usage.input_tokens_details.cache_write_tokens`，且原值缺失或为 None，才采用继承原 SDK 类型的局部 Schema：仅该字段允许 None。其余 InputTokensDetails、ResponseUsage 和 Response 定义仍继承原 SDK，随后再次进行完整严格校验。

这不是忽略 ValidationError，也不是无条件接受 SDK 构造对象。SDK 的 construct_type 只用于重现实际解析行为；进入 Worker 前必须通过 Schema 和语义校验。原 API JSON 不补 0、不补 null、不删 usage；局部对象中的 None 表示未知，不写回原件。完成记录明确保存 `provider_field_not_reported`、原始 missing 与 SDK null 两种表现、value_known=false；原 usage 和原文哈希可追溯。

实际 SDK 返回时，API 原 JSON 与 SDK 序列化结果都要经过校验，防止 SDK 构造默认值掩盖其他必需字段缺失。其他必需字段缺失、错误类型、错误模型、空或非法类型 ID、非 completed、error/incomplete_details、非法参数 JSON、重复/空 call_id、未知工具、不支持输出项、敏感内容、模型额度耗尽仍停止。跨目标调用先由原 Worker 拒绝，实验适配器不再将该拒绝输出发给下一轮模型。

同一 adapter 已接收真实调用后，如果 Checkpoint 仍 pending、链不完整、输出重复/游离、调用被改变，或输出不能绑定持久化记录，就停止；不再把丢失输出退化成普通历史消息继续同一轮。独立恢复入口原有 saved-records 路径仍保留，付费入口 max_resumes=0 没有改变。

未修改生产 ModelClient、Worker、Agent、RuntimeStore、Checkpoint、工具实现、审批权限、Prompt 或任何生产预算；没有新的 Manager、Worker 或 Runtime，也没有全局修改 SDK/Pydantic。

## 4. 冻结真实首轮的实际离线执行

最终产物为 [replay-audit.json](../data/p65-real-response-offline-replay-20261019-02/replay-audit.json)、[summary.json](../data/p65-real-response-offline-replay-20261019-02/runtime/summary.json)、[worker-boundaries.json](../data/p65-real-response-offline-replay-20261019-02/runtime/worker-boundaries.json)与实际 SQLite。此前 `...-01/` 是保留的中间回放，没有覆盖；本报告以最终 `...-02/` 及最终源码哈希为准。

实际链仍为 InvestigationManager → Worker → Agent → ModelClient → ToolManager → RuntimeStore。新 Session 通过 Manager.enqueue 和一次 Worker.run_once 产生，没有直接写成功 Session 或 Checkpoint。第一轮标记为 **real_provider_first_response_offline_replay**，使用实际原 API 内容和 SDK 解析，不重新编写三个调用。

| 调用 | 实际 Python 执行及结果 |
| --- | --- |
| 自动 get_running_version 探针 | 执行 1 次。外层 no_data，原采集亦 no_data / commit_unknown；未使用当前 HEAD 补版本。 |
| call_00_2sRC2gGqrurnl7HSJBCO4223：query_metrics(order-service, http_5xx_rate) | 执行 1 次。外层历史 no_data；内层原采集 no_data，没有补 0。 |
| call_01_scG2F5JTluTxed9NwXVG6584：query_metrics(order-service, http_requests_total) | 执行 1 次。外层历史 no_data；内层原采集 ok / 13.0，采样时间仍为历史时间，不能证明当前状态。 |
| call_02_Yr1Q0kHxw1ktaNmXFW175209：query_logs(order-service, 原故障窗口) | 执行受限回放函数 1 次，范围不匹配，返回 no_data / outside_approved_frozen_evidence；没有 captured_result 或日志内容，没有执行 Loki HTTP。 |

“执行”指实际调用受控 Python 回放函数；不代表执行了外部 HTTP，也不代表取得有效证据。以上四项 record.success=true 表示 Python 执行完成，result.status 均为 no_data，原 Runtime 因此保存 tool_failed 事件；不能把该事件名混同于 Python 异常，也不能把执行完成说成取证成功。

模型日志参数完整保留 `2026-10-18T11:52:54.430057Z` 至 `2026-10-18T11:53:24.815895Z`。原冻结 Loki 查询 end 为 `2026-10-18T11:53:34.815895+00:00`，晚 10 秒且字符串格式不同；现有精确匹配规则未改。没有扩大窗口、规范化后冒充匹配，或把那 5 条冻结日志作为本次查询结果。故障窗口、Loki 查询窗口、Loki 时间戳与正文事件时间继续区分。

## 5. 预算、持久化与多轮协议

[最终只读证明](../data/p65-protocol-validation-20261019-01/replay-final-proof.json)同时核对 Runtime 计数、四次 tool_before_io、四次 tool_after_io、四条 tool_reserved 事件及四条 observation record。**合计 4 次：探针 1、模型原选工具 3；缓存命中 0、Worker 工具拒绝 0。** 日志的范围 no_data 是已执行函数的返回，不是缓存或未执行。

三个原 call_id 逐字一致，Checkpoint 的调用参数与原字符串一致，每个 function_call_output 都绑定实际已保存 record.result。构建第二轮输入时 pending 已清空；请求保存的 checkpoint_sha256 与 model_before_io 读取的已提交 Checkpoint 完全一致。独立只读打开 SQLite，还能在保留的一个 model_started Checkpoint 中读到三个完整输出；原 Store 仍仅保留最近四份 Checkpoint，所有边界的已提交快照另由 worker-boundaries 保存，没有擅改保留策略。

第二轮实际输入为首轮原 input + **完整原 response.output** + 三个已提交 function_call_output。逐项比较确认 commentary、四项 output 的顺序和内容与原 API JSON 相同，没有删除或重排。commentary 本来就在三个调用之前，因此当前 valid_chain 可以接受；没有第二个独立协议阻断，也没有调整生产 valid_chain。未验证所有可能的 Provider 交错输出形式。

第二轮响应明确标为 **scripted_mock_followup**，只提供脚本化最终文字，不新增主回放工具。新 Session=COMPLETED，Runtime 模型入口计数=2，真实 SDK create 计数=0；此计数不能描述为两次真实 DeepSeek 推理。ResultTrust 仍为 semantic_status=pending_review、trusted=false、human_approval=not_recorded。

独立预算负测在三个原选工具后让 Mock 继续提出第五次执行，Runtime 停在 EXHAUSTED，执行数及 records 仍为 4，没有第五次执行记录或第三轮请求。未提交、被篡改、游离、重复或无记录支撑的输出负测均停止，未伪造工具成功。

## 6. 实际测试与完整回归失败

所有测试使用独立 TEMP Store 和本地网络 guard。Python audit hook 阻断真实 socket/DNS/UDP；唯一例外是 Windows 标准库 socketpair 的内部唤醒连接。P6.5 运行入口还阻断 HTTP、数据库、子进程、线程和修复执行；完整回归额外禁止真实 psycopg.connect，允许测试自身 Mock。SDK MockTransport 测试只访问内存 transport，合成授权仅在 TEMP 中，不是真实付费授权或真实模型请求。拒绝尝试保存在本地 network-denials.txt，不计为实际服务连接。

| 检查 | 实际结果 |
| --- | --- |
| 初次协议/P6.5 定向 | [101 passed in 52.42s](../data/p65-protocol-validation-20261019-01/targeted-initial.txt)。随后补充两个负测，由下列最终批次覆盖。 |
| 最终协议、P6.5、Worker、Checkpoint、Runtime、ResultTrust、P6.1/P6.4 相关批次 | [366 passed, 58 subtests passed in 280.02s](../data/p65-protocol-validation-20261019-01/runtime-targeted.txt)。 |
| 受限完整回归 | **[1 failed, 2292 passed, 2 skipped, 8 deselected, 190 subtests passed in 670.46s](../data/p65-protocol-validation-20261019-01/restricted-regression.txt)**，未通过。 |
| 失败项单独诊断复测 | [1 passed in 9.80s](../data/p65-protocol-validation-20261019-01/claim-race-isolated.txt)，不替代完整回归失败。 |
| pip check | [No broken requirements found](../data/p65-protocol-validation-20261019-01/pip-check.txt)。 |
| 其他核查 | 五个 Python 文件 AST 可解析，原件哈希/请求范围/SQLite 绑定通过；生产核心、失败测试及其子进程夹具相对基线无 diff。 |

完整回归失败项为 `tests/test_runtime_quality.py::ProcessQualityTests::test_two_process_claim_race_and_old_process_fencing`。在第 628 行等待 ready-one/ready-two 时，第 625 行原有 30 秒屏障报 `child barrier timed out`；尚未进入后续 claim/fencing 断言。完整 traceback 原样保留。该次测试自身在 finally 消费了子进程输出，原完整失败运行没有额外捕获其 stdout/stderr；不能将诊断复测的输出冒充当次输出。

独立复测只增加本地诊断旁路，没有改测试或生产超时、断言、租约或 fencing。两个子进程均 exit=0、stdout/stderr 为空，ready、claim、fence 文件状态保存在 [claim-race-diagnostics.jsonl](../data/p65-protocol-validation-20261019-01/claim-race-diagnostics.jsonl)。目前无法确定原完整运行未及时 ready 的原因，不能断言由机器负载、锁、防病毒或本次改动造成，也不能据单测通过宣称已修复。

上一报告唯一失败的双 writer 用例在**本次完整回归中通过**，monitor/worker 均 exit=0、各写入 30 次，见 [writer-diagnostics.jsonl](../data/p65-protocol-validation-20261019-01/writer-diagnostics.jsonl)。这不解释上次失败根因；本次完整回归的另一个失败仍独立保留。

以下 8 项真实 loopback 用例继续排除，不计为通过：test_real_loopback_http_ignores_environment_proxy（1）、test_first_worker_skips_yesterdays_legacy_clear_without_real_http（4）、test_background_startup_only_probes_durable_jobs（1）、test_real_http_transport_against_isolated_loopback_server（1）、test_real_redirect_is_refused_before_library_drains_body（1）。两个 skip 分别为未启用 clean-install 独立环境检查、未设置 ALERT_RULES_PATH 的跨仓库规则检查。本机冻结原件测试均实际执行；其他没有本地原件的工作区会明确 skip 对应测试，不能宣称复验了真实原件。

## 7. 已知限制、下一阶段及提交边界

- 已完成此真实样本的限定兼容修复和 Worker 离线协议路径证明；没有真实第二轮输入/输出往返，更没有真实最终调查结论或业务恢复证明。
- provider_verified 继续为 false，生产 Worker 的 evidence_rebuild 策略未改；本次完整 SDK 输入链由原 P6.5 实验适配器投影，不能据此认证生产 Provider 路径。
- SDK 私有解析函数只在已核验的本机版本用于冻结复算，并受完整 Schema 校验约束；未声称未来 SDK 或其他 Provider 输出形态兼容。
- **尚不能确认满足重新付费实验的全部条件**：工程材料已可提交独立审查，但受限完整回归仍失败，需先审查并解释或处置该并发验证问题。之后如申请真实多轮实验，仍须重新冻结最终代码/请求方案并取得新的独立明确授权；旧授权不可复用。本轮没有创建这类授权。
- 按用户追加要求，仅提交本次五个 Python 文件和本报告；原件、日志、数据、授权、消费记录、网络 guard、用户已有差异不提交。实际提交说明、完整 SHA 与远端核对结果由最终交付回执和本地 publication-receipt.json 记录，不在提交正文中预填自身 SHA。

本阶段工作结束后停止，等待独立审查，不继续真实请求或 P6.6。
