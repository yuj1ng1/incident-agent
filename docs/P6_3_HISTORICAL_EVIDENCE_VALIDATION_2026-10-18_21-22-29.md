# P6.3 真实故障证据与模型调查验证：单次真实模型验证报告

本阶段先完成历史原件复核、脱敏请求冻结、默认离线入口与 Mock 验收，再在用户明确回复“授权”后执行了 **1 次真实 DeepSeek 请求，零重试、零工具、零业务请求、零修复**。返回 completed、格式检查通过，但两名 Agent 逐条复核确认 **2 条事实错误、3 条局部引用支持不足，语义严格验收失败**；人工审查仍 required，未伪装成人类签字。原始回答及哈希完整保留，没有改写回答、追加请求或补跑。

本阶段范围为 `real_model_historical_evidence_analysis`，没有验证 `live_alert_to_real_model_autonomous_investigation`。下文保留授权前真实离线结果及完整回归的一次失败，补入唯一真实调用和逐条语义意见；提交推送后停止等待独立审查。

## 1. Git 与环境基线

已实际读取根目录 [AGENTS.md](../AGENTS.md)。开始时分支 `main`、本地 HEAD 和 `git ls-remote origin refs/heads/main` 的真实远端 SHA 均为 `c54bc775b49640eab6d344281e58358a6ba72235`；origin 为 `https://github.com/yuj1ng1/incident-agent.git`，暂存区为空。

离线准备已提交为 `64e66bf45ce71663e2c3de0b5c1a3bbf21636b37`。本次获授权执行前再次核对，本地与实际远端 main 均为该 SHA、暂存区为空，原有用户差异未变化；[本次付费前核查](../data/p63-live-preflight-20261018-01.json) 记录了最终输入、预算、SDK版本及保护路径。授权后没有修改任何 Python 代码或请求内容，本次提交仅更新并重命名同用途报告；本地运行及审查产物不加入 Git。

原有 README、prompts/agent_prompt.py、temp_test.py 修改、6 项文档删除、未跟踪文档及此前 pytest 临时产物均保留并排除于提交。本阶段仅新增两个脚本、一个测试文件和本报告，没有修改生产代码、现有公开接口、配置、阈值、权限、Runtime 预算或依赖文件。保护清单另存 [p63-protected-baseline-20261018.json](../data/p63-protected-baseline-20261018.json)，涵盖 575 个原有路径的哈希/缺失状态和选定 Demo 文件；最终核对结果见收尾验证。

实际解释器 `D:\conda\envs\agentlearn\python.exe`：Python 3.12.14、openai 3.14.0、httpx2 2.13.0、Pydantic 2.13.5、requests 2.34.2、pytest 9.1.1。离线 inspect 确认 SDK 构造及 with_options 支持 max_retries/timeout，Responses.create 支持 max_output_tokens/reasoning/tools/tool_choice；SDK 默认重试不是 0，因此入口在构造和请求处均显式禁用。没有升级依赖。

## 2. 历史原件完整性与独立复核

唯一实验根目录 [p62b-first-error-20261018-01](../data/p62b-first-error-20261018-01/) 的根清单覆盖 75 项，SHA-256 为 `350578d6914f7f09a530d85178f491da2366521b25b5f5fe4b5872bd58e5ae08`。已重新读取原件并校验哈希、文件集合、路径、HTTP 元数据/body 绑定及解析结果；拒绝链接、目录逃逸、缺件或篡改。另复核 6 份嵌套清单：experiment 35 项、traffic 32 项、observations 19 项、preflight 5 项、runtime-replay 7 项、内层 runtime 3 项。

历史 Runtime 内层 seal 早于最终 agent.log 和 incidents.json.lock，且部分历史清单使用 Windows 分隔符；本阶段只在这两个固定 Runtime 清单中兼容原有分隔符，两份晚写文件仍由根清单校验，未冒称内层 seal 覆盖它们。哈希一致仅证明本地冻结一致性，不提供外部真实性认证。机器记录见 [完整性审计](../data/p63-integrity-audit-20261018.json)。

复用了 [P6.2b 子报告验证](../scripts/p62b_error_experiment.py) 的 `_verified_child_report()` 和 [原件回放验证](../scripts/p62b_evidence.py) 的 `verify_capture()`，在离线 guards 下重新解析历史响应。P6.2a 的 `p62b_analyze_capture.py` 固定 `/order` 与 business phase，不能拿来假称已验证本次 `/error`；本阶段没有调用它的错误适用路径，也未重新执行故障。

已阅读 [P6.2b 中文报告](P6_2B_ERROR_EXPERIMENT_2026-10-18_20-06-08.md)、[P6.1 验证](P6_1_READONLY_VALIDATION_2026-10-18_14-54-49.md)、[P6.2a 验证](P6_2A_BUSINESS_BASELINE_VALIDATION_2026-10-18_16-34-56.md)、[P5.3c.2f 真实验证](P5_3C_2F_TYPED_REFERENCES_REAL_MODEL_VALIDATION_2026-10-18_10-03-38.md)及 [P5.3c.2g 支持约束](P5_3C_2G_REFERENCE_SUPPORT_CONSTRAINTS_VALIDATION_2026-10-18_10-45-59.md)；结论以上述原件重算为准。报告所引 [历史主机审计](../data/p62b-host-audit-20261018/sha256.json) 的 6 项原件也重新核验通过，清单 SHA 为 `f86d4ac4744b3c52f3681fb898b67316248b789c2504eafc0a6505ef2f0ded65`；其中 timezone.json 确认为 UTC+08:00、不使用夏令时，只用于核对既有日志解释，未发送主机详情。

| 项目 | 本阶段依据原件重新确认的事实与边界 |
| --- | --- |
| 故障设计与 HTTP | 冻结授权将 GET `/error` 定义为受控、预期 500 测试；traffic/http-002、005、008、011、014 各有完整 HTTP500 与 body 原件。实际故障窗口为 2026-10-18 UTC `[11:52:54.430057, 11:53:24.815895)`。 |
| body 差异 | 五份 body 均为 21 字节 `Internal Server Error`，**没有换行**，SHA-256 均为 `e41656eb2ba6c6293bf6dd928e5a88cdbc50535cab661c1969e0f598e497ed62`。P6.2b 报告示例写有 `\n`，本轮按原件纠正说明，未改写历史报告。 |
| 运行身份 | 实验前后 10 条 `/version` 原始 JSON 的 service、process_started_at、flags 一致，两 flags 均 false；这是应用自报身份未观察到变化。commit_sha/short_sha 为 null，运行 SHA 仍 unknown，不以仓库 HEAD 或当前 Demo 源码替代。冻结集合未保存部署源码，不能认证具体运行代码。 |
| Counter | preflight/http-002.body 中按 service 聚合得 8；后续 observations/http-004.body 的总量为 13。差值 +5；没有取得后续独立 500 Counter 对比，数量相同也不能排除并发流量或证明排他因果。 |
| 指标 | http_request_rate 有效值 0.0，service_scrape_up 有效值 1.0；5xx 比率及 P95 原 API vector 为空、解析为 no_data，不补 value/evaluated_at。有效零是样本，no_data 是缺少可用结果。 |
| 时间缺口 | 采集入口 11:54:48.842673，完成 11:54:56.306126；入口晚于末次故障 84.026778 秒，首 HTTP 晚 89.152712 秒。每项实际 collected_at/evaluated_at 分别保留，不能代表故障期间峰值或当前健康。 |
| Loki | 保存 5 条限定 service、正文、时间窗口的 ERROR/status=500 日志；保留正文事件时间、UTC+08:00 解释、Loki 时间及额外 10 秒摄取窗口。日志没有路由和共享 request ID，只能作时间/内容/数量候选关联，不能逐请求绑定或推出数据库故障。 |
| 告警 | 完整快照中未捕获 order-service firing；当次状态依据完整快照缺席为 inactive，HighHttp5xxRate rule 为 inactive/health=ok。原快照有 inventory-service/ServiceDown firing，不归入本实验。晚期快照不覆盖整个故障期，不能推出从未告警。 |
| 历史 Runtime | 原件交叉绑定为 synthetic_alert_input + scripted_mock_sdk_not_real_deepseek；Mock Session COMPLETED、2 轮 Mock 模型、4 次历史回放工具流程，不是真实模型定位。 |
| 恢复与修复 | 集合内没有 `/order` 恢复验证或修复执行；历史记录 business_recovery_verified=false。不能用停止流量、inactive、request_rate=0、Mock COMPLETED 证明业务恢复，也不声称现实中永远不存在其他证据。 |

## 3. 最小实现、复用范围与架构

[p63_evidence.py](../scripts/p63_evidence.py) 只处理本案例的固定冻结布局，建立 17 条 allowlist 投影及相对原件路径/字节哈希：实验设计 1、HTTP500 5、版本组 1、基线 Counter 1、后续指标 5、日志 1、告警快照 1、版本 no_data 1、历史 Mock Runtime 1。计数为 17 条引用；分组内仍保留各次实际时间。所有引用都明确 historical_only；timestamp_basis 区分设计记录时间、HTTP 完成、最后版本核验、工具 envelope 采集与 Mock 完成。

引用命名空间为 `derived_frozen_evidence_references_not_runtime_observation_ids`。适配字段名 observation_id 仅用于复用既有 claims 合约，不能冒充真实 Runtime observation 或本轮新工具执行。`extraction_status=supported` 只表示本案例投影可提取，不表示生产 P5 Tool 新增支持或中文语义通过。

[p63_model_validation.py](../scripts/p63_model_validation.py) 提供 `prepare()`、`run_prepared()` 及默认离线 CLI；一个局部 SDK 适配器经现有 `ModelClient.call_once()` 调用 Responses。它不创建通用模型客户端、调查状态机、Store、审批或证据引擎，不实例化 Agent/Worker/InvestigationManager，不启动 Monitor、Scheduler、RecoveryWorker 或生产 main.py。现有 Worker 可能先探测版本并在完成后进入修复提案投影，这不是本次单轮历史分析所需，因此没有接入或修改该链路。

复用 [P5 validate_model_answer](../scripts/preflight_real_model.py) 校验 claims 结构、kind、条数/长度及引用存在和去重。结果只能标 `trusted_index_internal_integrity_only`，没有伪装为 `passed_external_rebuild`。P5.3c.2g 的 typed source/field sidecar 依赖实际 Runtime 输入和一致快照，不能直接认证本阶段 HTTP/Loki 目录，因此未强接或重写它；其具体实例、字段支持、集合范围、时间和语义审查原则保留在逐条人工审查中。

## 4. 实际发送的冻结输入与脱敏范围

实际发送的是 [冻结 request.json](../data/p63-prepared-20261018-02/request.json)，对应 [context.json](../data/p63-prepared-20261018-02/context.json)、[授权前预检摘要](../data/p63-prepared-20261018-02/summary.json) 与 [准备产物清单](../data/p63-prepared-20261018-02/sha256.json)。真实调用保存的 [request.json](../data/p63-live-20261018-01/request.json) 与它逐字节相同；准备摘要的 authorization_required 保留为授权前历史状态，不回写它冒充当时已有授权。**请求 37,234 字节，SHA-256：**

```text
a81dc82b0df040fc115dad0b04baca3eca6279edf4ee264fc337faeeac7d039f
```

这是保存的最终 JSON 请求内容字节哈希；SDK 传输可能重新序列化 JSON，不将它冒称 HTTP wire bytes 哈希。调用前从原件重新构建 context 和请求，进行逐字节与哈希比较，原件不重新采集、不刷新历史时间。预备包和历史目录拒绝被输出目录覆盖。较早 `p63-prepared-20261018-01` 是补充时间语义前的离线中间包，不是最终授权对象；未删除或冒充已发送。

仅投影上述来源的必要状态、时间、数值、固定错误正文、日志白名单正文、来源标识及原件哈希；没有发送完整环境变量、API key、数据库业务行、无关日志、请求头、主机进程清单或绝对数据路径。校验原件哈希时可读取历史 SQLite 文件字节，但没有打开查询业务数据库或将数据库内容放入请求。敏感字段/credential 形状检查是白名单之外的第二道检查，不声称可以识别任意自由文本秘密。未授权时不调用凭据读取函数或构造 SDK 客户端；批准后才从项目现有凭据路径读取 key，不把 key 写入请求/日志。

模型任务覆盖用户提出的现象、证据支持、监控状态、机制、真实业务故障、firing、no_data、恢复和进一步取证问题。回答要求 confirmed/hypothesis/limitation/next_check 分类，并逐条引用，不放入旧模型答案或人工评分结果。实验设计和禁止推断的边界是输入约束，不是通过验收的模型回答。

## 5. 新授权、调用预算与实际执行

| 参数 | 冻结值 |
| --- | --- |
| 模式 | 单次 `real_model_historical_evidence_analysis` |
| Provider / 模型 | 固定 https://api.deepseek.com，deepseek-flash；不读取替代模型或中转站地址 |
| 请求次数 | 新的明确授权后最多 1 次；实际已执行 1 次，本次额度已消费 |
| SDK / 应用重试 | 构造和 with_options 均 max_retries=0；无应用循环、fallback 或自动补跑 |
| 输出 | max_output_tokens=2048、reasoning.effort=none |
| 工具与延续 | tools=[]、tool_choice=none，无工具 dispatch、previous_response_id 或自动下一轮 |
| 超时 | SDK timeout=30 秒，按 SDK 各操作生效；本入口没有另设硬墙钟监督器，不承诺总进程耗时必定小于30秒 |
| 传输 | httpx2.Client(trust_env=False, follow_redirects=False)，不继承环境代理、不跟随重定向 |
| 审批绑定 | purpose、明确同意、最终请求 SHA、原件根清单 SHA、模型、1次/0重试及带时区记录时间 |
| 一次消费 | 正常 CLI 固定 data/p63-paid-consumption，按最终请求 SHA exclusive-create；复制目录、失败后重跑均不能重置。预留后初始化失败也不自动返还额度。可注入 SDK/临时 ledger 仅供显式离线测试。 |

用户在本聊天针对上一轮最终请求和明确预算直接回复“授权”。随后实际写入 [授权记录](../data/p63-paid-authorization-20261018-01.json)，记录时间为 `2026-10-18T13:12:34.463842+00:00`，明确这是授权后的落盘时间，不虚构为用户消息时间，也不声称人类独立审查已签字。复核原件、请求及 SDK 参数后，正常 CLI 在固定账本中为上述请求哈希 exclusive-create [消费标记](../data/p63-paid-consumption/a81dc82b0df040fc115dad0b04baca3eca6279edf4ee264fc337faeeac7d039f.consumed.json)，随后才进入真实 SDK。没有复用 P5 或 P6.2b 授权。

唯一真实执行命令为：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B scripts/p63_model_validation.py --execute --prepared-dir data/p63-prepared-20261018-02 --authorization data/p63-paid-authorization-20261018-01.json --output-dir data/p63-live-20261018-01
```

| 实际调用记录 | 值 |
| --- | --- |
| 返回模型 / response ID | deepseek-flash / `61ae4d5a-4804-40c8-8377-bcc6fee0c3f4` |
| SDK create 开始 UTC | `2026-10-18T13:12:48.836913+00:00` |
| SDK create 完成 UTC | `2026-10-18T13:13:00.098502+00:00`，间隔 11.261589 秒 |
| Provider 状态 / CLI退出码 | completed / 0；仅代表返回及结构流程完成 |
| 本地 SDK create / SDK重试 / 应用重试 | 1 / 0 / 0 |
| 输入 / 输出 / 总 token | 10368 / 1587 / 11955 |
| reasoning / cached token | 0 / 0；cache_write_tokens 为 null，原样保留 |
| 业务请求 / 工具执行 / 修复 | 0 / 0 / 0 |

实际参数仍为2048输出上限、reasoning.effort=none、tools=[]、tool_choice=none、SDK操作timeout30、max_retries=0；没有第二轮或 fallback。本地计数指进入 SDK create，不是独立计费记录；Provider usage 已保存，实际账单金额未核实，不估算费用。单次授权已消费，语义失败不会返还或自动追加请求。

已读本机 SDK 签名/实现及 P5.3c.2f 历史 request/raw 原件确认该调用方式曾得到 DeepSeek 响应；这仅是历史兼容证据，不证明第三方当前可用或与 OpenAI API 完全等价。[OpenAI Responses 官方参数文档](https://developers.openai.com/api/reference/python/resources/responses/methods/create)用于核对 SDK 参数含义，不能替代第三方实际行为验证。

## 6. 实际离线验证与本次检查范围

下列测试是本阶段授权前实际完成的运行；本次只执行既有冻结入口和报告更新，没有代码/依赖变更，因此未重复整套测试，也不把此前输出冒充本次新运行。调用前额外实际执行了原件/请求重建、SDK签名与版本检查；调用后再次核验返回原文和封存清单。

| 验证层级 | 实际结果与记录 |
| --- | --- |
| 新增离线定向 | **38 passed in 39.83s**，exit 0，无 skipped/deselected；[真实输出副本](../data/p63-targeted-20261018.log)明确标记 copy_from_tool_output，未为生成日志重跑测试。 |
| 受限完整 Python 回归 | **2167 passed、1 failed、2 skipped、8 deselected、190 subtests passed，786.53s**，exit 1；[完整原始日志](../data/p63-regression-20261018.log)。没有改写为全通过。 |
| 唯一失败的单独复测 | **1 passed in 10.51s**，exit 0；[复测日志](../data/p63-race-recheck-20261018.log)。未重跑整套来覆盖首次失败。 |
| pip check | **No broken requirements found.** |
| 默认 prepare / Mock CLI | 两者均 exit 0；真实网络模型/业务/修复均 0。 |
| 内部源码复查 | 两轮只读审查；首轮发现动态 capture 副本输出保护及 SDK 错误正文留存问题，已修复并加回归；第二轮未发现影响当前固定案例和正常 CLI 的新确认缺陷。这不替代交付后的独立审查。 |

定向命令为指定 Python `-B -m pytest tests/test_p63_model_validation.py -q -p no:cacheprovider --basetemp <系统TEMP中的独立目录>`。受限完整回归通过 `pytest.main()` 加载已存在的 `data/p62a-20261018T075858Z-719154a6/p62a_network_guard.py`，参数 `-q -rs -p no:cacheprovider -p p62a_network_guard --basetemp <系统TEMP中的独立目录>`，并以 -k 排除下列函数。guard 文件 SHA-256 为 `7cdf41e2f3a9fb91d190bd663daaf009d4ec04a40698806395fb8de67449a5e3`，阻止未 Mock 的网络，仅允许 Windows asyncio 标准库 socketpair 内部唤醒。

| 未运行的真实 loopback 函数 | 实例数 |
| --- | ---: |
| test_first_worker_skips_yesterdays_legacy_clear_without_real_http | 4 |
| test_background_startup_only_probes_durable_jobs | 1 |
| test_real_http_transport_against_isolated_loopback_server | 1 |
| test_real_redirect_is_refused_before_library_drains_body | 1 |
| test_real_loopback_http_ignores_environment_proxy | 1 |

两项实际 skip 是 `tests/test_dependencies.py` 的 clean-install 检查（需 CI/isolated venv），以及 `tests/test_multi_signal.py` 的跨仓库规则检查（未设置 ALERT_RULES_PATH）；这些场景本轮未验证。无前端改动，未运行前端构建；没有重跑 Demo 测试或访问 Demo。

完整回归唯一失败为 `tests/test_review_quality.py::test_two_incidents_race_with_real_processes_and_fencing`，在第 217 行等待 ready-one/ready-two 的 30 秒 barrier 超时；失败目录只留 ready-one，而没有 ready-two。该测试、runtime_process_fixture 和生产 investigation 模块相对指定基线没有改动，子进程路径没有 P6.3 入口。既有失败处理未把子进程 stderr 落入日志，无法据现有记录确定第二个子进程未就绪的具体原因，**不把机器负载或 SQLite 锁推定为已确认根因**。同一解释器、同一禁网 guard 下仅复测该函数通过，说明未稳定复现；首次失败和并发稳定性限制仍需独立审查。本阶段未改生产 Runtime、延长测试超时或弱化断言。

新增测试覆盖：无/错误授权的 0 SDK；原件与预备包哈希；敏感额外字段剔除及敏感投影拒绝；故障/采集/样本时间、no_data 与 0.0；真实快照与合成告警；请求重建不续期；禁止额外业务、数据库、进程、工具、修复；一次 SDK 与复制授权防复用；API异常脱敏原文和哈希；不完整、空回答、错误模型、意外工具输出、缺失引用保持失败；usage/UTC请求起止以及人工待审状态。

测试中的 live=True 分支全部注入 Mock SDK，并封锁外部网络、业务工具和修复；其中局部 real_model_sdk_requests=1 表示注入 SDK 的本地 create 入口，**不是实际付费请求**。没有借测试调用 DeepSeek。新增案例依赖本地冻结原件的部分在没有该忽略目录的环境会 skip，本工作区原件存在；不把这些真实原件加入 Git。另有无需本地原件的授权拦截测试。

早期两次定向运行分别有一个夹具失败：Mock response 默认 model=fixture 被实际模型名校验拦住，以及本机 SDK usage 结构要求 cache_write_tokens。修复的是测试夹具，没有弱化生产校验；最终结果单独记录。离线 CLI 真实运行 `prepare` 与 `--mock` 成功；Mock 回答只写“离线Mock占位；未验证真实模型分析能力”，结构 passed、strict pending、人工审查 required，产物见 [Mock 审查](../data/p63-mock-20261018-01/claim-review.json)。

## 7. 真实回答封存与逐条证据支持

完整原始回答为 [answer-original.txt](../data/p63-live-20261018-01/answer-original.txt)，API原文为 [response.raw.json](../data/p63-live-20261018-01/response.raw.json)，SDK序列化为 [response.sdk.json](../data/p63-live-20261018-01/response.sdk.json)，usage、参数、UTC起止及计数见 [原始 summary.json](../data/p63-live-20261018-01/summary.json)。原始产物共11文件（含清单），[sha256.json](../data/p63-live-20261018-01/sha256.json)覆盖其余10项，已全部重新核验。未回显凭据，credential_redacted=false。

| 冻结产物 | SHA-256 |
| --- | --- |
| 请求 request.json | `a81dc82b0df040fc115dad0b04baca3eca6279edf4ee264fc337faeeac7d039f` |
| API原文 response.raw.json | `4e03340b3427d3fc8e0c540aa54bf13c31f4ae30eb50bc6864283c82ffd942c5` |
| SDK response.sdk.json | `83250c2e3267c8f01691475fd6866f2ee83ca12418f61b95a96e9b609d6fb8d5` |
| 原始答案 answer-original.txt | `f35bf0377b544e254be67bb33e41cf9e1f30d190bd3859c3023ae9d8e3a28581` |
| 原始产物清单 sha256.json | `48b23d95de6b95b68b73837f696c962657f89d7193160182cd22dcb70fbfd81d` |

回答实际含10条claims。现有P5校验给出 output_structure=passed、errors=[]、reference_source_check=trusted_index_internal_integrity_only，原始 [claim-review.json](../data/p63-live-20261018-01/claim-review.json) 仍保留 strict_acceptance=pending、manual_reasoning_review=required。这些初始机械结果没有被覆写。

随后 root 与独立 Agent 分别直接阅读原答、冻结输入与本地原件，结论一致：**语义严格验收失败，人工仍待审**。单独的 [semantic-review.json](../data/p63-live-review-20261018-01/semantic-review.json) 保存每条原始 claim、原引用、审查意见及反例原件路径；[审查清单](../data/p63-live-review-20261018-01/sha256.json) 绑定其字节。它是两名 Agent 的审查，不是模型自评或人类签字，没有关键词自动评分、改写原答、补引用或删除错误条目。

下表编号从1开始，对应 sidecar claim_index=编号减1。“支持”只表示 Agent 在所述范围内的证据意见，人工审批状态均未完成。

| 编号 / kind | 原文关键陈述 | 实际引用与逐条意见 |
| --- | --- | --- |
| 1 confirmed | “捕获到6次完整HTTP响应”，随后只列5个完成时刻 | **事实错误。** 引用 http-002/005/008/011/014 共5个实例，原件和监督器均确认5条500；原答自己列出的时刻也只有5个。状态、响应体和路由范围正确，数量错误不能因引用ID有效而通过。 |
| 2 confirmed | “受控合成的HTTP 500测试……实验设计而非已部署源码或业务故障证明” | **支持。** 引用 design；正确区分设计意图、期望效应与真实部署/业务认证，没有把预期直方图更新当已取得观测。 |
| 3 confirmed | “日志查询返回5条……Loki时间戳落于故障窗口内” | **含确定时间错误。** 引用 logs；最后 Loki 时间 `11:53:24.918736582Z` 晚于故障窗口末 `11:53:24.815895Z` **0.102841582秒**，仍在延后10秒的Loki查询范围内。正文秒级事件时间 `11:53:24Z` 是窗口内候选，不能与Loki时间混用。数量、服务、内容、无route/shared ID及不能逐请求绑定的限制正确。反例在 observations/http-008.body。 |
| 4 confirmed | “10次身份检查均报告……flags均false……commit_sha为null……不能以仓库HEAD补齐” | **支持。** 引用 versions 与 running-version，正确限定自报身份、no_data/commit_unknown及未知部署源码。 |
| 5 confirmed | “聚合值为13.0（前后对比8.0→13.0）……no_data……0.0” | **局部引用不足。** 本条只引5个后续metric实例，没有 counter-before；这些引用不能单独支持基线8和变化。基线确实存在，属于漏引而非伪造数据。指标值、有效零与no_data区分、scrape不证明业务健康均正确。 |
| 6 confirmed | “单一迟到快照……order-service无选中告警……历史inactive不能排除故障窗口内曾告警” | **按本案语境支持。** 引用 alerts；规则评估时间和单快照覆盖限制正确。末句“不能声称观测到真实firing”须限定本次order-service；原件确有inventory-service真实ServiceDown firing，不能泛化为全快照不存在真实firing。 |
| 7 limitation | “/error按设计返回500，并伴随……服务聚合请求计数上升” | **局部引用不足。** 引用只有design、http-002、logs，未引前后Counter实例；设计中的预期更新不能支持实际增长。测试路由、真实500、有限日志关联及不能断言部署代码/数据库/业务根因的限制正确。 |
| 8 hypothesis | “计数从8.0升至13.0与5次捕获的500……不能排除并发流量或证明唯一归因” | **局部引用不足。** counter-before与metric-total支持聚合变化，非排他归因及假设标记正确；但HTTP只引最后一个http-014，不能在本条支持5次数量，也与#1的6次矛盾。Counter只定位到故障前采样至故障后采样之间增长；本条故障窗口内增长仍属未验证假设。 |
| 9 limitation | “no_data不能等于0.0……Mock……不能作为定位或恢复证据” | **支持并保留假设边界。** 引用5xx、P95、request-rate与runtime；缺失样本、有效零、历史范围与合成/Mock边界正确。“可能是查询窗口、指标命名或采集范围问题”只属一般可能，命名错误并无直接证据，不是已确认原因。 |
| 10 next_check | “取得故障窗口内对齐的5xx……部署源码……关联日志……/order业务健康与恢复验证” | **作为取证建议支持，未执行。** 引用5xx、running-version与logs，与输入缺口一致。先检索已保存的历史材料；日后的/order健康检查不能反向证明本次历史业务恢复。“以上均需新采集”不能充当本轮新业务请求授权。 |

整体上，没有发现本答虚构数据库故障、生产代码回归、订单业务瘫痪、工具执行、实际修复或业务恢复；也未把合成告警/Mock COMPLETED升级为真实模型自主调查。已确认问题是#1数量、#3精确时间边界，以及#5/#7/#8的局部引用支持。存在这些缺陷，因此不能把completed、格式通过或部分正确的边界说明当作本次语义验证成功。

## 8. 是否具备进一步真实模型 Agent 调查条件

已经完成一次真实历史材料分析并取得原答及缺陷证据，**没有足够依据直接进入或宣称完成完整真实模型自主调查**。本轮语义验收失败、人工独立审查未完成，完整回归的既有跨进程超时也须保留；原实验仍缺对齐的5xx/P95、连续告警史、已认证部署SHA、真实业务故障与恢复验证。

下一步最小工作是人工独立复核原答和10条审查意见，先决定如何处理数量、时间边界和就近引用问题；保留失败原文，现有单次额度不补跑。本阶段没有自动调整Prompt、扩建引用引擎或继续请求。任何模型复测、新采集、业务验证或 live alert → Agent 工具调查都需后续明确范围及新授权，本轮不据此再次注入故障或执行修复。

## 9. 收尾与提交边界

授权前最终核查见 [离线最终核查](../data/p63-final-verification-20261018.json)；本次调用后的原件、冻结输入、API原答、审查sidecar和保护路径复核见 [真实调用后核查](../data/p63-live-final-verification-20261018-01.json)。报告实质更新完成后使用实际北京时间重命名同一用途文档，原离线版本仍可在上一提交查看，不另造重复报告。没有遗留本轮故障开关或待恢复的受控服务，因为本轮没有注入或更改它们；既有服务与未知进程未被终止。没有批量结束Python。

真实调用后重新核实：本次付费前571个仓库保护路径均未变；原离线清单含选定Demo文件的575个路径也全部保持。原件75项、API封存10项及审查清单校验一致，实际密钥值扫描无匹配；原回答字节未修改，3份P6.3 Python文件、ModelClient和P5验证入口均与执行前HEAD一致，报告本地链接可解析。本次仅为数据与文档交付，没有重跑或改写此前测试结论。

本阶段没有修改 Demo、调整告警、启动生产 main.py、增加 `/order` 或 `/error` 请求、停止容器/数据库或执行修复。原有文档迁移及其他用户差异保持原状：离线提交为三个新Python文件和报告，本次授权后提交仅更新并重命名报告；data 下原件、请求、API返回、测试日志、授权消费和审查产物不加入Git。提交与推送使用实际北京时间生成中文说明，最终完整SHA和远端一致性由交付消息记录，避免在提交内部虚构自引用SHA。
