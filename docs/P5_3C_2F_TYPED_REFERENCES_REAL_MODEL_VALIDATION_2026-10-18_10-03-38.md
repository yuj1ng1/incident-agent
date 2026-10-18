# P5.3c.2f 首次 Typed References 真实模型验证报告

## 结论与阶段边界

**整体严格验收失败：9 条原始 claims 中 7 条 passed、2 条 failed（claim_index=3、5，编号从 0 起）。** Runtime、Python 权威事实与 JSON/claims 结构通过；独立语义审查仍发现身份来源错绑和全局缺口引用不足。原答案未修补、删改或重请求。相比 c.2d 的 9 条中 5 条失败，本案例出现可观察的引用改善，但不是稳定通过率或长期可靠性的证明。

本阶段是独立真实模型验证，没有开发生产功能。复用现有 `scripts/preflight_real_model.py`、`scripts/preflight_evidence_refs.py`、Agent、Worker、Runtime 与本地测试夹具；无新增 Manager、Store、Provider、接口、依赖、schema、权限、工具白名单或预算变更。没有修改生产 Prompt、生产模块或已有报告。

本次实际运行发生一次真实 SDK 请求。模型与参数固定；没有因任何错误或验收失败追加请求。没有启动 main.py、Monitor、Scheduler、RecoveryWorker、实际业务服务、真实 Prometheus/Loki/PostgreSQL 查询、故障注入或修复。结束后没有本任务遗留的 Python 预检/pytest 进程；独立 SQLite 与临时目录按要求保留。

## 基线、真实远端及原有工作区

先读取本地 [AGENTS.md](../AGENTS.md)，然后实际检查 Git、入口与 SDK。阶段开始及付费前的分支均为 `main`；HEAD 为 `4b8dbbb220b5dafa9a6e2141d4f777251819e793`（c.2e）。真实执行 `git ls-remote --heads origin main`，返回同一 SHA；远端 URL 是 `https://github.com/yuj1ng1/incident-agent.git`，没有把本地 origin/main 缓存当远端确认。

暂存区初始为空。11 个关键入口、Runtime 与夹具文件的规范化 Git blob 与 c.2e 相同；目标关键代码没有无法解释的工作区差异。完整记录见[初始清单](../data/model-validation/p53c2f-3uskvil0/verification/initial-state.json)、[关键代码审查](../data/model-validation/p53c2f-3uskvil0/verification/static-safety-review.json)和[付费安全门](../data/model-validation/p53c2f-3uskvil0/verification/pre-live-gate.json)。

原工作区已有 README.md、prompts/agent_prompt.py、temp_test.py 的修改，6 个既有文档删除，以及 12 个未跟踪说明文件/归档文件。本轮保留这些内容和所有既有运行数据，没有把它们加入提交。最终保护检查覆盖 822 个原有文件（含缺失状态、运行数据及密钥文件哈希），无变化；[核对记录](../data/model-validation/p53c2f-3uskvil0/verification/post-live-integrity.json)保留证据。

生产 Prompt **实际字符串** SHA-256 为 `94faddc0b0029a11be7f815d571f3fad47e0d2cad6305be0aa899752f8c7350a`，与历史真实测试基线一致。Git HEAD 内 Prompt 字符串哈希为 `a4462e66dc90587fb6eb998566f558bd4b5da25bd0b2a8107b851c8fa4e71da2`；差异是已有未提交的第 52 条预算说明。本次实际验证的是 c.2e 关键代码加该既有工作区 Prompt，不能把新提交的纯 Git 检出误称为完全相同的请求快照。完整请求正文已本地保留，本轮不恢复、不覆盖、不提交该 Prompt。

本阶段 10 月 17 日启动后曾被用户中断，10 月 18 日恢复。首次定向与完整回归日志没有完成摘要，不计入通过结果；原日志另存 `*-interrupted.log`。恢复时无相关 Python 后台进程，822 个保护文件不变，重新运行离线测试。中断前真实请求为 0。

## 离线安全门与实际测试

全部使用 `D:\conda\envs\agentlearn\python.exe -B`，Python 3.12.14；实际依赖为 openai 3.14.0、httpx2/httpcore2 2.13.0、pydantic 2.13.5、python-dotenv 1.2.3、pytest 9.1.1，未安装依赖。

| 验证 | 本轮实际结果 |
| --- | --- |
| 三份现有定向测试 | **182 passed in 178.68s**，exit 0 |
| 完整 Python 回归 | **1828 passed、2 skipped、190 subtests passed in 572.69s**，exit 0 |
| pip check | `No broken requirements found.` |
| 默认离线 CLI | exit 0；Runtime COMPLETED；真实 SDK/网络模型请求 0 |
| 离线占位回答 | 原始 Mock `resp-fixture`，结构 failed、语义 pending、整体 failed；属于预期，不冒充模型语义通过 |
| 离线产物/来源审计 | 5 条 observation、9 条 context，完整 Runtime 分析、原始记录、索引和事实可重建 |
| 付费前保护与次数检查 | 822 个原文件不变，暂存区为空；阶段内 paid 记录 0，exclusive-create 标记未存在 |

实际命令：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest -q -rs -p no:cacheprovider --tb=short tests/test_real_model_preflight.py tests/test_preflight_evidence_index.py tests/test_preflight_typed_references.py
& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest -q -rs -p no:cacheprovider --tb=short
& 'D:\conda\envs\agentlearn\python.exe' -B -m pip check
& 'D:\conda\envs\agentlearn\python.exe' -B 'D:\Incident Agent\scripts\preflight_real_model.py'
```

两项跳过分别是 tests/test_dependencies.py 的 CI/独立虚拟环境 clean-install 检查，及未设置 ALERT_RULES_PATH 的可选跨仓库告警规则校验。它们不是已验证事项。没有前端改动，未运行前端验证。

定向测试使用禁网 fixture；其中 `live=True` 注入 Mock SDK/MockTransport 的用例是离线测试，其局部 `real_model_api_requests=1` 仅表示注入入口，不是付费请求。默认离线 CLI 同样没有网络 SDK。完整回归包含本地持久化和跨进程测试，不代表真实外部业务服务已联通。

本轮分别核对了以下安全门：

1. 五个来源是四条导入历史指标和一次本地受控版本探测，保留原 observation_id；不是五次本轮新工具调用。
2. 告警与能力来自实际 Runtime 首段上下文，身份来自 Checkpoint 原记录/快照，集合来自请求前同一 SQLite 事务读取的完整 records。
3. 原记录、Runtime 摘要、v2 索引和 Python 事实一致。复核 26 项时间/样本字段存在性及值，保存缺失标记；`evaluation_time`、`evaluated_at`、`collected_at` 分开，不给 no_data 补值或样本时间。
4. 有限 `0.0` 保留为实际观测；case-a-t2 的 5xx=no_data 未变为 0。B 两条仅提供样本 evaluated_at，不新造查询 evaluation_time。
5. 原始完整 Runtime 输入与 metric_analysis 保留，追加 raw_records 和 v2 index；没有人工裁剪为标准答案，没有把旧答案或人工评分 rubric 放入请求。
6. paid 与 offline 均使用精确本地 isolated_tool，只允许 get_running_version(order-service) 返回 no_data；模型 tools=[]，任何意外工具输出拒绝。provider_verified=False，无修复 Planner。
7. SDK 构造与 with_options 均 max_retries=0、timeout=30，传输默认 retries=0；禁用代理继承与重定向。Runtime max_attempts=1、max_model_requests=1、max_resumes=0、max_human_requests=0；未启动后续调度。
8. CLI 自行创建空的 OS 临时目录并监督自己的子进程，90 秒墙钟上限；入口原参数未修改。
9. 请求意图、原始 response、SDK 序列化响应、原答案、usage、快照、Checkpoint 和异常分类可留存，密钥值扫描无匹配，无请求头保存。
10. SDK 与代码版本、真实远端、工作区和原有数据在付费前重新核对，安全门全部实际通过后才启动。

[官方 OpenAI Python SDK 文档](https://developers.openai.com/api/reference/python)说明 max_retries=0 可禁用 SDK 自动重试；本轮同时检查本机安装版本源码的重试循环。此文档仅解释 SDK，不替代对第三方 DeepSeek 返回行为的实测。

## 唯一一次真实模型请求

实际执行原入口，未换入口、增加开关或重跑：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B 'D:\Incident Agent\scripts\preflight_real_model.py' --run-live --approve-paid-model-run
```

启动北京时间 `2026-10-18T09:57:07.293841+08:00`，完成记录 `2026-10-18T09:57:21.207115+08:00`。使用 `https://api.deepseek.com/responses`、deepseek-flash、max_output_tokens=2048、reasoning.effort=none、tools=[]、tool_choice=none；请求超时 30 秒，监督上限 90 秒；应用自动重试关闭。

| 项目 | 实际结果 |
| --- | --- |
| response ID / status | `5cbaf0e3-8488-4aa7-9caf-000d2b14f309` / `completed` |
| CLI / Runtime | exit 0 / COMPLETED |
| Provider 入口 / SDK create 入口 / Runtime 模型预留 | 1 / 1 / 1 |
| 本地 bootstrap 工具 / 模型工具调用 | 1 / 0 |
| 输入 / cached / uncached tokens | 17825 / 6912 / 10913 |
| 输出 / reasoning / 总 tokens | 1208 / 0 / 19033 |
| 入口初始验收 | 事实 passed、结构 passed、推理 pending、整体 pending |
| 原始异常分类 | null；无 provider-error 文件 |
| 入口序列化请求大小 | 68,737 字节，小于 98,304 上限；不是独立抓包测得的 HTTP 大小 |

次数定义是本地 SDK create 入口，不能单凭它证明服务器收件或实际扣款；本轮另有一个真实响应原件证明实际返回。未查询账户余额或账单，不估算实际扣款。传入模型名和响应 model 均为 deepseek-flash，未证明其底层版本与 c.2d 完全相同。

原件目录为 `C:\Users\zp1222\AppData\Local\Temp\incident-p53c2e-q8hgqk1f`。目录前缀沿用 c.2e 入口代码的既有名称，本轮阶段仍是 c.2f。审查前冻结 20 份真实运行文件，副本逐字节哈希相同；原始响应文本、SDK output_text、answer-original.txt 与 Checkpoint.answer 完全相同。

审计辅助脚本首次用整个 usage 字典逐键相等作断言，因 SDK model_dump 额外填入可选 `input_tokens_details.cache_write_tokens=null` 而失败。随后保持原件不变，逐项核对 ID、所有实际 usage 数值及答案均一致；该断言前提过严不是 API/Runtime 失败，也没有触发第二次请求，见[审计说明](../data/model-validation/p53c2f-3uskvil0/verification/audit-assumption-note.json)。

## 原始 claims 与引用

下面是原文及原始引用值的逐条展示；原始 JSON 字节保留于[answer-original.txt](../data/model-validation/p53c2f-3uskvil0/live/answer-original.txt)，SHA-256：`9a900f99336de4b8102a1712a52bdba056a3a89bba90942f856553e045ff15df`。没有添加缺失引用、删除错误 claim 或改写模型措辞。

| claim_index | kind | 原始 text | 原始 observation_ids | 原始 context_refs |
| --- | --- | --- | --- | --- |
| 0 | confirmed | 告警为 order-service 的 http_5xx_rate 数值型信号，值 0.3 超阈值 0.2，状态 firing。 | `[]` | `[{"type": "alert_event", "id": "context:alert_event:3f88d474308b05c839b4c627626c89b812e042aab1160e26552a96cd1ffc03b3"}]` |
| 1 | confirmed | 历史某评估时刻 order-service 对齐查询显示 http_5xx_rate=1.0、service_scrape_up=1，另一时刻请求率降至 0；这些均为过期历史窗口观测，不能证明当前状态。 | `["case-a-t1", "case-a-t2", "case-b-errors-t1", "case-b-rate-t2"]` | `[{"type": "evidence_collection_scope", "id": "context:evidence_collection_scope:0a7032ce8e51731c7c6cde8132442ab853003024152c61017c0f16e0aa3a7ba8"}]` |
| 2 | confirmed | order-service 具备 health、logs、metrics、dependencies、runtime、source 等能力，注册依赖 order-db，但能力声明不证明后端可达或依赖健康。 | `[]` | `[{"type": "capability_context", "id": "context:capability_context:69d032ed43b66b7e2f7d2b15f4ce5fda1153e5935c0b64ba02f03f727d2c1565"}]` |
| 3 | limitation | 运行版本探测返回 no_data（historical_fixture_has_no_current_runtime_identity），持久化 identity 中的 commit_sha 仅为历史关联，不能确认当前运行版本或已部署内容。 | `["obs-3fb46f167f8f41ab9997040bdf69882a"]` | `[{"type": "identity_context", "id": "context:identity_context:412fdc0e9a0c8ac624c7bd6e6357b97fa722bacea6de08b9a62abe754684a752"}]` |
| 4 | limitation | 某时刻 http_5xx_rate 返回 no_data，且不同指标评估时刻不一致，缺失不能视为零值或健康，也不能拼成同一时刻快照。 | `["case-a-t2", "case-b-errors-t1", "case-b-rate-t2"]` | `[{"type": "evidence_collection_scope", "id": "context:evidence_collection_scope:0a7032ce8e51731c7c6cde8132442ab853003024152c61017c0f16e0aa3a7ba8"}]` |
| 5 | hypothesis | 故障可能位于 order-service 到 order-db 的依赖路径或订单自身应用逻辑；当前无日志、依赖探测、健康检查或数据库指标证据，无法区分这两种假设，也未确认任何根因。 | `["case-a-t1"]` | `[{"type": "capability_context", "id": "context:capability_context:69d032ed43b66b7e2f7d2b15f4ce5fda1153e5935c0b64ba02f03f727d2c1565"}]` |
| 6 | next_check | 在同一明确评估时刻获取告警窗口内 http_5xx_rate、http_request_rate、service_scrape_up，并查询该时段 order-service 日志以确认错误类型。 | `["case-a-t1", "case-a-t2"]` | `[{"type": "alert_event", "id": "context:alert_event:3f88d474308b05c839b4c627626c89b812e042aab1160e26552a96cd1ffc03b3"}]` |
| 7 | next_check | 检查 order-service 当前 health 与 order-db 依赖健康，若能确认依赖不可达再沿数据库方向取证。 | `[]` | `[{"type": "capability_context", "id": "context:capability_context:69d032ed43b66b7e2f7d2b15f4ce5fda1153e5935c0b64ba02f03f727d2c1565"}]` |
| 8 | next_check | 确认当前实际运行版本（需可用的 runtime/部署证据）后，若日志显示应用级异常，再检查近期源码与提交 diff；不得据历史 commit_sha 推测已部署。 | `["obs-3fb46f167f8f41ab9997040bdf69882a"]` | `[{"type": "capability_context", "id": "context:capability_context:69d032ed43b66b7e2f7d2b15f4ce5fda1153e5935c0b64ba02f03f727d2c1565"}]` |

与已确认问题相关的实际上下文如下。身份 ID 有效、类型正确、hash 可重建，但它不包含历史 SHA，这是本轮不能机械放行的具体例子。

| 类型 | 实际 reference_id | 实际 source_location | 实际 value |
| --- | --- | --- | --- |
| `alert_event` | `context:alert_event:3f88d474308b05c839b4c627626c89b812e042aab1160e26552a96cd1ffc03b3` | `runtime_input/incident_context/alert_event` | `{"service": "order-service", "severity": "critical", "alertname": "legacy", "signal_type": "numeric", "source": "legacy", "state": "firing", "metric": "http_5xx_rate", "value": 0.3, "threshold": 0.2, "unit": null, "direction": null, "labels": {}, "identity_labels": [], "starts_at": null, "observed_at": null, "window": null, "minimum_requests": null}` |
| `capability_context` | `context:capability_context:69d032ed43b66b7e2f7d2b15f4ce5fda1153e5935c0b64ba02f03f727d2c1565` | `runtime_input/incident_context/capabilities` | `{"status": "ok", "service": "order-service", "capabilities": ["health", "logs", "metrics", "dependencies", "runtime", "source", "recovery"], "missing_capabilities": [], "providers": {"health": "http", "logs": "loki", "metrics": "prometheus", "runtime": "http_version", "source": "github", "recovery": "http_business"}, "dependencies": ["order-db"], "remediation": ["disable_demo_regression"], "metric_profile": "http_service", "supported_metrics": ["http_requests_total", "http_request_rate", "http_5xx_rate", "http_latency_p95_seconds", "service_scrape_up"], "source_paths": ["app.py", "http_observability.py"]}` |
| `identity_context` | `context:identity_context:412fdc0e9a0c8ac624c7bd6e6357b97fa722bacea6de08b9a62abe754684a752` | `source_snapshot/records/4/identity` | `{"unverified_instance": "b22843e72a824cc9a5fbf5e31278b180"}` |

## 独立逐条语义审查

此处为 Codex 基于冻结原件的独立证据审查，单一审查者；不是人类评分、不是新的模型 API 请求，也没有把审查结论送回模型。每条同时检查事实存在、引用支持、来源类型与遗漏、时间归属、历史/当前、配置/执行、集合范围、根因不确定性及建议动机。

| claim_index | 判定 | 逐条依据 | 错误类型 |
| --- | --- | --- | --- |
| 0 | passed | alert_event 原来源明确给出 service=order-service、signal_type=numeric、metric=http_5xx_rate、value=0.3、threshold=0.2、state=firing。本条只陈述输入告警元数据；引用正确，不以告警证明当前服务健康或根因。 | 无 |
| 1 | passed | case-a-t1 的 evaluation_time=T1，样本 http_5xx_rate=1.0、service_scrape_up=1.0，同时请求率为0.41821223361699034；case-a-t2 在T2=T1+300ms请求率0.0，两个B观测分别补充T1的5xx与T2的请求率。本条就近引用四条指标及集合范围，覆盖所比较两个时点。降至0仅描述两个历史观测值的变化，不证明故障趋势、因果或当前状态；这些指被讨论的四条指标，不将版本探测说成指标。未把共同查询时间解释成共同抓取时间或原子性。 | 无 |
| 2 | passed | capability_context 的 capabilities 含 health/logs/metrics/dependencies/runtime/source，dependencies=[order-db]。文本后半句明确限制为能力声明，不能证明后端可达或依赖健康；没有声称日志、健康或依赖工具已经执行。 | 无 |
| 3 | failed | 版本 observation 的 no_data 与 reason 确实支持当前运行版本未知；但本条唯一 identity_context 的 source_location 为 source_snapshot/records/4/identity，value 只有 unverified_instance，没有 commit_sha。历史 commit_sha 实际在 records/0..3/identity，属于其他身份引用，本条没有引用它们。历史SHA存在于完整背景，不能说成凭空造假；错误是所引身份来源不支持后半句。没有把no_data解释为服务不存在或部署失败。 | identity_source_misattribution, insufficient_citation_support |
| 4 | passed | case-a-t2 的查询目标T2对应http_5xx_rate=no_data，缺失项没有value或evaluated_at；case-b-errors-t1给出T1的5xx样本，case-b-rate-t2给出T2的请求率样本。因此跨指标不同样本时刻的比较有T1/T2两侧引用。某时刻返回no_data没有声称缺失样本本身具有evaluated_at，也未将A2内部成功样本错误判作不对齐；不把缺失补0，不拼为同刻，不宣称恢复。 | 无 |
| 5 | failed | case-a-t1的历史5xx观测可支持进一步调查的动机，capability_context能支持已登记order-db依赖；依赖路径或应用逻辑被清楚标为假设，未确认为根因。但当前无日志、依赖探测、健康检查或数据库指标证据是涉及完整输入集合的缺口判断，仅引用一条指标和能力配置不能证明该集合缺失；本条没有引用实际可用的evidence_collection_scope。原始五记录快照确实没有这些结果，所以这不是虚构数据库故障，也不能据该句确认现实没有日志。 | missing_evidence_collection_scope_reference, global_evidence_gap_citation_incomplete |
| 6 | passed | 两个A观测分别提供历史5xx异常与另一查询目标缺样本，alert_event提供告警指标和阈值，足以支持重新取同一评估时刻的指标及查询日志的建议。该句属于next_check，没有声称已取得新指标/日志，也没有给出虚构窗口数值。执行前置条件仍未满足：实际alert_event的starts_at、observed_at、window均为null，原建议没有明确列出先补齐告警窗口的步骤，须在执行计划中核对；本次只接受其为有动机的取证方向，不把窗口当已知，也不替模型补写答案。共同评估时刻不等于共同抓取时刻。 | 无 |
| 7 | passed | capability_context明确登记order-service的health能力和order-db依赖，足以支持检查方向。若能确认依赖不可达为未来条件，不断言依赖当前不可达、数据库故障或已经执行探测。后续必须核对依赖的受控Provider及分阶段结果，单一健康检查不认证根因。 | 无 |
| 8 | passed | 版本observation的no_data支持先确认当前版本，capability_context中runtime/source/logs能力支持条件性后续取证方向。若日志显示为尚未发生的条件，没有虚构应用异常、已执行源码调查或近期已部署提交。末句不得据历史commit_sha推测已部署是一般证据约束，不是在断言所引版本identity含commit_sha，也没有给出具体SHA；区别于claim_index=3明确声称持久化identity中存在commit_sha。建议仅支持方向，执行时仍需确认实际运行SHA和源码可用性。 | 无 |

第 3 条和第 5 条的背景事实可以在完整输入中找到；失败原因是**原引用不足以支持完整陈述**，不能笼统报为模型凭空编造事实。没有发现本轮将 no_data 补 0、将 B 样本时间转成查询时间、把不同时点拼成同刻、宣称当前恢复或确认数据库故障/代码回归。

第 6 条作为建议有证据动机，未声称窗口已知或调查已执行，因此本项通过；但真实执行仍缺告警窗口。窗口为 null 的前置条件不能被省略为已经满足。第 8 条末尾是一般证据约束，不同于第 3 条对特定身份内容的事实断言。本审查保留两项执行限制，不借此补写原答案，也不主观增加额外失败项。

[完整逐项审查 JSON](../data/model-validation/p53c2f-3uskvil0/verification/independent-claim-review.json)保留原 text、kind、原引用、verdict、逐条依据及多个 error_types；[审查绑定](../data/model-validation/p53c2f-3uskvil0/verification/reasoning-review-attestation.json)绑定本次 facts、claims 与逐项引用；[组合审查结果](../data/model-validation/p53c2f-3uskvil0/verification/preflight-report-reviewed.json)另存。入口原始 pending 报告保持不变。

## 五个核心验收维度与严格结论

| 维度 | 结果 | 依据及边界 |
| --- | --- | --- |
| Runtime 流程 | passed | COMPLETED、预算和结果持久化一致；不是业务恢复 |
| Python authoritative facts 完整性 | passed | 5 来源、9 context 可由原始外部输入重建，缺失/零/时间投影忠实；原始 SQLite 只读 integrity_check=ok，请求前及最终 Checkpoint 与导出一致 |
| JSON / claims 结构 | passed | completed 响应，9 条合法 claims，3 条 next_check，引用存在/类型/哈希机械检查通过 |
| 来源与引用支持绑定 | **failed** | claim 3 错绑身份；claim 5 缺集合范围引用；不是指目录本身被篡改 |
| 自然语言语义与证据支持充分性 | **failed** | 7 passed、2 failed；未发现明确数值/时间/因果造假，但两条陈述不能由原引用充分支持 |
| 整体严格验收 | **failed** | 任一必要维度或 claim 失败即不通过，不能用 CLI=0、COMPLETED、合法 ID 或测试通过提升结论 |

## 与 c.2d 的实际比较

对照[历史 c.2d 报告](P5_3C_2D_TRUSTED_FACT_REASONING_VALIDATION_2026-10-17_17-19-23.md)及其原答案和审查文件，旧答案 SHA-256 与报告中的 `1534581882587ad967352a7b0b284886b176a501477ccc705ca7d12240adebad` 相同，9 条中确有 5 条失败。本轮也返回 9 条是实际结果，不是预先假定。

| 项目 | c.2d 实际情况 | c.2f 本轮实际情况 |
| --- | --- | --- |
| 告警元数据归属 | 告警值、阈值及缺失时间字段混入工具引用 | claim 0 正确使用 alert_event；未再陈述缺失告警时间字段，不能据此证明所有时间元数据能力都改善 |
| 能力/依赖归属 | 能力声明及 order-db 背景未有对应元数据引用 | claim 2 正确引用 capability_context，并明确配置不等于执行或健康；claim 5 的依赖名也有配置引用 |
| 身份信息归属 | 历史 commit 陈述错误归到版本 no_data 观测 | 新 identity_context 已使用，却选中了只含 unverified_instance 的身份；问题未解决 |
| 集合缺口判断 | 单一指标 ID 支持全局无日志等缺口，失败 | 其他条使用 scope，但 claim 5 全局缺口仍漏 scope；未解决 |
| 跨时刻引用覆盖 | 一条比较 T1/T2 但只引用 T2，失败 | claim 1 和 4 分别同时覆盖 T1/T2，观察到引用覆盖改善 |
| claims passed / failed | 4 / 5 | 7 / 2 |
| input / output / total tokens | 14923 / 650 / 15573 | 17825 / 1208 / 19033 |
| 整体严格验收 | failed | failed |

该比较只有同一固定历史案例各一次调用。引用契约和输入长度不同，模型采样及别名背后的版本未固定，没有控制其他变量；不能把失败项减少归结为 typed references 的一般因果效果，也不能换算稳定成功率。新的引用目录解决了“缺少可引用来源”的部分建模问题，但本次证明模型仍可能选错或漏用来源。

## 产物、SHA-256 与保护

本阶段 OS 任务目录为 `C:\Users\zp1222\AppData\Local\Temp\incident-p53c2f-3uskvil0`；专用归档为 `D:\Incident Agent\data\model-validation\p53c2f-3uskvil0`。包含 live/ 真实 SDK 原件、offline/ 明确标记的 Mock 原件、verification/ 测试日志/安全门/冻结/usage/审查/交付记录。原始目录继续保留，不覆盖历史归档。

[artifact-manifest.json](../data/model-validation/p53c2f-3uskvil0/artifact-manifest.json)记录新增文件的字节 SHA-256 与分类，清单自身另有校验文件；后续交付记录会追加到本阶段归档。以下为关键不可变文件字节哈希，不与审查绑定用的入口 json_sha256 混用。

| 文件 | SHA-256 |
| --- | --- |
| [live/request.json](../data/model-validation/p53c2f-3uskvil0/live/request.json) | `7289bfaad9e4020f8ce9b3babf9ec15b52b8792661966be3b2b701f1f4201843` |
| [live/runtime-input.json](../data/model-validation/p53c2f-3uskvil0/live/runtime-input.json) | `2bcdc3fefb72c377e336e8b4ebb0f4839eb8d893a5c5a7cdd3a11e4842cdb7d7` |
| [live/request-source-snapshot.json](../data/model-validation/p53c2f-3uskvil0/live/request-source-snapshot.json) | `bd27a2a6ec2b7a1867e0de3a6943d4279bb2e9c0d6f2bef3cb969b175dfc3b59` |
| [live/evidence-index.json](../data/model-validation/p53c2f-3uskvil0/live/evidence-index.json) | `dbc70bfca9e4d34b50613c25818e5dc7c19f302e6f9eda99f43f0f52409a0a47` |
| [live/authoritative-facts.json](../data/model-validation/p53c2f-3uskvil0/live/authoritative-facts.json) | `4abc14b58f19299c5358e9c2c445cd056852a3a8541e24017c309d1a63c46800` |
| [live/response.raw.json](../data/model-validation/p53c2f-3uskvil0/live/response.raw.json) | `b231a30bff567bdf7c73b01b55e0c3a211abbaa10a87d4f0d8f085a40628141a` |
| [live/response.sdk.json](../data/model-validation/p53c2f-3uskvil0/live/response.sdk.json) | `94166aea08ac34f2a1b49a9c6d9cd1c5527b120c2911d2addf2d1d44ac55931a` |
| [live/answer-original.txt](../data/model-validation/p53c2f-3uskvil0/live/answer-original.txt) | `9a900f99336de4b8102a1712a52bdba056a3a89bba90942f856553e045ff15df` |
| [live/checkpoint.json](../data/model-validation/p53c2f-3uskvil0/live/checkpoint.json) | `435a3ff3275acae8ea6148c4ee5411e21ebd667b68f2daa37256d30c1db3d55d` |
| [live/runtime.sqlite3](../data/model-validation/p53c2f-3uskvil0/live/runtime.sqlite3) | `ea4d588cb6025524d5ae0a78582f42e0cf9034719a80a6abfd42db2f2419273f` |
| [verification/independent-claim-review.json](../data/model-validation/p53c2f-3uskvil0/verification/independent-claim-review.json) | `c0a206400827ae4cc4094e241874f02ac300444e41b698d18056b61e14593379` |
| [verification/reasoning-review-attestation.json](../data/model-validation/p53c2f-3uskvil0/verification/reasoning-review-attestation.json) | `f1ee9ed935398212ca495a7dafbdf7262bcfd6ce4907b90f62912b4df62d35e6` |
| [verification/preflight-report-reviewed.json](../data/model-validation/p53c2f-3uskvil0/verification/preflight-report-reviewed.json) | `804d86b6129705f6ee43ca19b0d4343e624047c3ce06d1726be3fcb9953047c4` |

归档受原有 `/data/` 忽略规则保护，不提交运行数据；这些本地证据链接在其他检出环境不保证存在。没有输出密钥、请求头、完整环境变量或真实敏感凭据。原有 README、Prompt、temp_test.py、删除/归档内容及运行数据保持原样。

## 已确认问题、疑点、未验证项与下一阶段

已确认问题有两项：模型在 identity_context 类型正确且 ID 合法时仍错用具体来源；全局集合缺口陈述仍漏掉 evidence_collection_scope。机械校验通过不足以自动认证自然语言支持充分性。两项问题本阶段只报告，不修改代码或 Prompt。

需要在后续设计中关注的执行风险：告警 starts_at、observed_at、window 均为 null，而 next_check 没有显式先补窗口；尚不能直接构造可执行的告警时窗查询。该建议不是已执行调查，也没有虚构具体时间。本轮不启动真实工具来补缺口。对真实依赖健康 Provider、模型别名底层版本及多审查者一致性仍待验证。

尚未验证生产调度、真实监控/业务服务、真实日志与数据库取证、恢复、修复执行、跨案例稳定性、跨模型可迁移性、实际账单及两项跳过测试。单个 historical fixture 的请求成功不证明真实故障根因推理能力。

下一阶段最小建议是先审查本轮原始失败与判据，再决定建立多个离线证据快照的引用效果评估，或设计能够显式表达身份与集合支持范围的最小约束。需要用户决定：是否允许不确定引用时省略陈述/标记待验证，以及取证建议在窗口缺失时是否必须显式先请求时间线。不要靠增加同例重试或逐句答案式 Prompt 提高通过率。本轮不自动进入开发或第二次真实调用。

按用户最后明确的提交与推送要求，仅选择本报告提交；生产代码、Prompt、既有修改与运行数据不入本次提交。实际提交标题、完整 SHA、目标分支和远端复核在最终交付消息及本地 delivery.json 记录，本报告不提前声称推送成功。完成交付后停止，等待代码审查。
