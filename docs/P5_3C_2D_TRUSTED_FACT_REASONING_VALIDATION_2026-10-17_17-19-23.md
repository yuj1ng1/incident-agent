# P5.3c.2d 可信事实与模型推理分离验证

本轮完成独立预检入口中的 Python 权威事实展示、claims 输出契约、机械校验、分层报告与一次真实 DeepSeek 请求。**事实层通过，模型结构通过，系统安全检查通过；模型解释独立逐条审查失败，整体严格验收不通过。** 9 条 claim 中 4 条通过、5 条引用归属或覆盖不足。没有修改、删去或替换错误 claim，没有追加 Prompt 规则、重试或第二次付费请求。

## 基线、工作区与修改范围

项目 `D:\Incident Agent`，仓库 `yuj1ng1/incident-agent`。开始时 main 的 HEAD、实际远端 main 均为 `49993b35107f576c7e932723921ea5500cc91255`，暂存区为空。用户已有 README、生产 Prompt、temp_test.py、文档移动及未跟踪说明等改动，全部保留且不纳入本次提交。

本轮仅交付以下源码/测试与本报告：

- [scripts/preflight_real_model.py](../scripts/preflight_real_model.py)：新增 `build_authoritative_fact_view`、内部 `json_sha256`、`compose_preflight_report`；调整独立 TASK、`validate_model_answer`、Provider 的派生产物以及独立摘要/CLI 判定。
- [tests/test_real_model_preflight.py](../tests/test_real_model_preflight.py)：迁移独立 TASK 与审查需求标记的断言，保留既有 45 项安全覆盖。
- [tests/test_preflight_evidence_index.py](../tests/test_preflight_evidence_index.py)：保留索引/Runtime/原始记录核对，迁移旧事实复述契约，并覆盖事实投影、claims、错误语义、审查绑定及不完整响应，共 89 项。

复用现有 `build_evidence_index`、Agent/Worker/ModelClient/RuntimeStore、固定历史夹具及完整 metric_analysis。没有生产接口、数据库字段、持久化格式或新依赖；不修改生产 Prompt、Worker、状态机、工具权限、审批/修复逻辑或预算。安全常量与 `build_evidence_index`、`build_case`、`isolated_tool` 等九个函数的 AST 与基线一致，见[范围审计](../data/model-validation/p53c2d-ftie99sh/verification/scope-audit.json)。

生产 Prompt 仍使用用户任务前已有修改的版本：实际字符串 SHA-256=`94faddc0b0029a11be7f815d571f3fad47e0d2cad6305be0aa899752f8c7350a`，文件 SHA-256=`85484d80c42ce0c3efb3fbf74b4766ed165336775a8f1908893d42cbfc9b62a2`。本轮不是在干净基线 Prompt 上运行。离线与真实请求的 instructions 完全相同，且付费后未再修改源码或 TASK。

## A. Python 权威事实

事实来源链为：本 Session 已持久化原记录 → 与 Runtime observation 身份、参数、状态和采集时间核对 → 既有 evidence_index → 只读事实投影。Provider 在 bootstrap 版本记录持久化之后重新加载五条来源；不是仅对启动前的四条指标建索引。

`build_authoritative_fact_view` 深复制索引，保留全部 observation_id、工具、独立 collected_at、原状态、historical_only、data_missing 与样本。查询 `evaluation_time`、原顶层样本 `evaluated_at` 以及具体样本的 value/evaluated_at 使用 `{"provided":true,"value":原值}`；缺失为 `{"provided":false,"value":"未提供"}`。原字段存在且为 null 时仍是 provided=true，不与字段缺失混淆。

不根据邻项、查询目标或采集时间补齐缺失样本时间；不推断根因、当前健康或业务恢复。未知工具/结构沿用索引的 unsupported 标记，事实构建和付费入口明确拒绝。索引与原始记录不变。事实构建不读取模型 claim。

本轮原始 UTC 时间 T1=`2026-10-11T10:48:55+00:00`，T2=`2026-10-11T10:48:55.300000+00:00`：

| 来源 | collected_at | 查询 evaluation_time | 样本与 evaluated_at |
| --- | --- | --- | --- |
| case-a-t1 | 2026-10-14T02:00:00+00:00 | T1 | 请求率 0.41821223361699034、5xx 1.0、scrape 1.0；样本均 T1 |
| case-a-t2 | 2026-10-14T02:00:00+00:00 | T2 | 请求率 0.0、scrape 1.0 的样本均 T2；5xx=no_data，value/evaluated_at 未提供 |
| case-b-errors-t1 | 2026-10-14T02:00:00+00:00 | **未提供** | 5xx 1.0；样本 evaluated_at=T1 |
| case-b-rate-t2 | 2026-10-14T02:00:00+00:00 | **未提供** | 请求率 0.0；样本 evaluated_at=T2 |
| obs-07d9196395d14af6b3051a22d0e7e6e2 | 2026-10-17T09:11:50.566387+00:00 | **未提供** | get_running_version=no_data；无指标，evaluated_at 也未提供 |

`query_metrics` 原顶层 evaluated_at 属于该指标样本，事实视图保留原位置，不改名为查询 evaluation_time。版本的 historical_only=true 表示 Runtime 不允许作为当前状态证据，不把它分类成历史指标。有效零与 no_data 始终分开。

离线与真实请求各完成 **209 项**原记录→索引→事实展示逐字段核对，均一致；见[离线审计](../data/model-validation/p53c2d-ftie99sh/verification/offline-request-audit.json)与[真实审计](../data/model-validation/p53c2d-ftie99sh/verification/live-request-audit.json)。原四条夹具、完整 metric_analysis、原 Runtime input 和 Checkpoint records 均保留，派生封套未写回 Runtime 历史。五个不同 collected_at 通过 Mock 变体覆盖；没有为了测试修改真实固定夹具的四个相同采集时间。

## B. 模型解释契约与机械边界

模型只输出 `{"claims":[{"kind":"...","text":"...","observation_ids":["..."]}]}`，不再返回 sources 事实副本。kind 限 confirmed/limitation/hypothesis/next_check；1～10 条、建议最多 3 条、每条文本最多 600 字符、总文本最多 2400 字符。TASK 建议约 250～350 汉字，未硬编码本案例日期、来源 ID 或标准答案。人工 expected/grading/rubric 仍只用于本地审查，不进入请求。

`validate_model_answer` 检查 JSON、重复键、非有限数字、精确字段与类型、合法 kind、空文本、条数/长度、引用非空/不重复以及来源 ID 是否真实存在；响应 status 非 completed 即失败。它不证明文本中的时间、因果、全局缺口或引用支持关系。**模型 kind=confirmed 也只是待审查的模型声明，不属于 A 层权威事实。**

对自然语言推理保留 `manual_reasoning_review=required`。默认 report 的 semantic_review=pending_manual_review，推理与整体均 pending；不能把结构通过或 CLI exit 0 解释为推理通过。本次 9 条、3 个 next_check、合计 904 字符（其中 356 个汉字）符合硬限制；约 250～350 汉字是建议长度，不是机械验收硬门槛。前两条仍复述部分日期/指标，未完全达到只解释事实关系的意图。这类自然语言偏离未被机械检查自动识别。

`compose_preflight_report` 分开权威事实、原 claims/机械状态/审查状态和五项验收。事实 SHA 必须与基于索引生成的预期投影匹配；可选审查记录同时绑定 claims 与事实 SHA，完整覆盖全部 claim，包含具名审查方法、逐条理由和原引用。篡改/空事实、跨事实套用旧审查、缺项或错 hash 不能提升验收。此校验只验证审查记录的绑定和完整性，**不能证明审查内容本身正确**。human/独立审查通过与失败路径的 Mock 记录，不代表实际已有人工审批。

实现阶段的独立代码审查发现上述跨事实复用风险，已在付费前修复并加入回归；见[代码审查记录](../data/model-validation/p53c2d-ftie99sh/verification/code-review.json)。

## 离线验证与付费前安全门

全部使用 `D:\conda\envs\agentlearn\python.exe -B`，Python 3.12.14；OpenAI SDK 3.14.0、httpx2 2.13.0、Pydantic 2.13.5、python-dotenv 1.2.3、pytest 9.1.1。没有安装依赖。

| 实际验证 | 结果 |
| --- | --- |
| 既有独立入口定向测试 | 45 passed in 49.28s |
| 事实/claims 定向测试 | 88 passed in 202.66s；最后新增类型参数单独 1 passed in 43.00s，共 89 项 |
| 完整 `python -B -m pytest -q -rs -p no:cacheprovider --tb=short` | **1780 passed、2 skipped、190 subtests passed in 633.41s**，覆盖最终冻结源码和全部 134 项定向用例 |
| `python -B -m pip check` | No broken requirements found |
| 最终默认离线 CLI | Runtime COMPLETED；0 次真实模型请求，事实通过；占位回答不是 JSON，结构失败属于预期传输占位结果 |
| 原记录、索引与事实审计 | 离线/真实各 209 字段一致；每次请求 58,738 字节，小于原 98,304 上限 |
| 请求与范围审计 | 完整原输入/分析保留，rubric/密钥未泄露，预算与工具白名单未扩大，git diff --check 通过 |

2 项跳过分别为 CI/隔离虚拟环境的 clean-install 检查，以及未设置 ALERT_RULES_PATH 的可选跨仓库规则验证；不是已完成的集成验证。未改前端，未运行前端测试。定向测试全部禁网，live=True 注入 SDK 与 MockTransport 仍为 Mock；完整回归包含本地 SQLite/跨进程测试，不证明真实监控或业务服务可用。

初轮迁移曾因旧 summary 的 not_run/pending 断言与新 required 标记不一致产生 7 项失败，修改断言后既有 45 项全通过；新增文件的较早进程也曾加载旧 pending 断言而出现 1 项失败，最终 89 项及完整回归均通过。历史输出保留，没有将未执行测试写成通过。

完整回归、依赖、离线审计、源码冻结、原有改动/运行数据保护、暂存区及独立代码审查均通过后，[pre-live-gate.json](../data/model-validation/p53c2d-ftie99sh/verification/pre-live-gate.json) 记录放行，并以 exclusive-create 单次标记记录本轮执行，不自动重试。

## 唯一一次真实模型请求

实际执行：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B 'D:\Incident Agent\scripts\preflight_real_model.py' --run-live --approve-paid-model-run
```

调用于北京时间 2026-10-17 17:11，endpoint=`https://api.deepseek.com/responses`，模型名 deepseek-flash；max_output_tokens=2048、reasoning.effort=none、tools=[]、tool_choice=none；SDK 构造及 with_options 的 max_retries=0，原 30 秒请求/90 秒监督上限不变。独立 OS 临时目录与 SQLite，没有启动生产 main、Monitor、Scheduler 或 RecoveryWorker。

| 实际项目 | 结果 |
| --- | --- |
| response ID / status | `dd7e7e8f-58a0-4e90-afaa-811c2d0f568e` / completed |
| Provider 入口 / SDK create 入口 / Runtime 模型预留 | 1 / 1 / 1 |
| 本地 bootstrap 工具执行 / 模型工具调用 | 1 / 0 |
| input / cached / uncached tokens | 14923 / 6784 / 8139 |
| output / reasoning / total tokens | 650 / 0 / 15573 |
| CLI / Runtime | exit 0 / COMPLETED；仅表示执行与机械条件完成 |
| 入口自动验收 | 事实 passed、结构 passed、推理 pending、整体 pending |

一次工具执行仅为本地精确替代的 get_running_version(order-service) 返回 no_data。四条历史记录内的 request_executed=true 是夹具元数据，不是本轮在线请求。没有连接真实 Prometheus、Loki、业务 HTTP 或数据库，没有真实修复、故障注入或额外建议执行。SDK 入口计数不是独立账单证明；本轮确实收到一个真实响应。

### 模型实际原文

以下从 answer-original.txt 原样插入，原件、raw response、SDK 文本与 Checkpoint.answer 一致，未改写：

```json
{"claims":[{"kind":"confirmed","text":"历史指标显示 order-service 在 2026-10-11T10:48:55Z 评估时刻 http_5xx_rate 为 1.0，同期 http_request_rate 约 0.42、service_scrape_up 为 1.0；该证据仅覆盖该历史评估点。","observation_ids":["case-a-t1","case-b-errors-t1"]},{"kind":"confirmed","text":"2026-10-11T10:48:55.3Z 的另一评估点 http_request_rate 为 0.0，且 http_5xx_rate 无样本（no_data）；该时刻不能解释为零错误，两个评估点不可拼成同一时刻快照。","observation_ids":["case-a-t2","case-b-rate-t2"]},{"kind":"limitation","text":"现有证据全部为历史观测，均标记 use_for_current_state=false，且告警 starts_at、observed_at、window 缺失，因此无法证明当前 5xx 是否仍持续，也无法确定与告警时间窗的对应关系。","observation_ids":["case-a-t1","case-a-t2","case-b-errors-t1","case-b-rate-t2"]},{"kind":"limitation","text":"get_running_version 返回 no_data（historical_fixture_has_no_current_runtime_identity），无法确认当前运行版本；历史身份中的 commit 仅作参考，不能视为已部署认证。","observation_ids":["obs-07d9196395d14af6b3051a22d0e7e6e2"]},{"kind":"limitation","text":"未取得日志、健康检查、服务依赖健康或数据库指标证据；capabilities 虽声明 logs/health/dependencies，但本轮没有对应结果，证据缺口不为能力缺失。","observation_ids":["case-a-t1"]},{"kind":"hypothesis","text":"告警的 http_5xx_rate 3 成超过阈值可能对应历史观测到的接近全量 5xx 的应用失败，但具体责任组件（应用自身、order-db 依赖或网络）尚无法区分，因为缺少日志和依赖探测证据。","observation_ids":["case-a-t1","case-b-errors-t1"]},{"kind":"next_check","text":"在同一明确 UTC 评估时刻重新获取 http_request_rate、http_5xx_rate、service_scrape_up，以获得当前快照并核实是否仍有 5xx。","observation_ids":["case-a-t1","case-a-t2"]},{"kind":"next_check","text":"在告警实际窗口内查询 order-service 日志（确定 starts_at 后向前适当扩展），确认是否出现依赖 order-db 的连接错误或应用异常类型。","observation_ids":["case-b-errors-t1"]},{"kind":"next_check","text":"先获取服务依赖关系与 order-db 健康检查、postgres_up 指标，以区分应用自身故障、数据库不可达与网络链路问题。","observation_ids":["case-a-t1"]}]}
```

### 逐条独立语义复核

下表为 **Codex 独立证据复核**，并由主 Agent 对原请求和记录再次交叉检查；不是人类审查，也不是 Python 自动语义识别。编号按报告习惯从 1 起，JSON 审查文件的 claim_index 从 0 起。

| claim | 独立结果 | 证据依据与实际边界 |
| --- | --- | --- |
| 1 | 通过 | case-a-t1 的查询目标 evaluation_time 和三个样本 evaluated_at 都为该历史时点，值分别为 http_5xx_rate=1.0、http_request_rate=0.41821223361699034、service_scrape_up=1.0；约0.42是有依据的舍入。case-b-errors-t1 仅有同刻样本 evaluated_at，回答使用一般的评估时刻而未声称该B记录存在查询 evaluation_time。回答限制为历史评估点，未声称当前健康、抓取原子性或两条记录是独立佐证。 |
| 2 | 失败 | case-a-t2 支持查询目标T2、请求率0.0及5xx=no_data，缺失5xx没有样本 evaluated_at；case-b-rate-t2 支持同T2的请求率样本，因此零与缺失的主体解释正确，也没有给缺失样本补时间。但本claim末句比较两个评估点，所引两条均属于T2，没有引用T1来源，无法单凭本claim引用支持跨两个时点的比较。完整回答的前一claim有T1，不等于本条就近引用已经覆盖该比较；此处是引用范围不足，而非时间字面量造假。 |
| 3 | 失败 | 所引四条指标在Runtime封套中均为use_for_current_state=false，足以支持它们不能证明当前5xx或恢复。告警starts_at、observed_at、window确实在runtime-input的alert_event元数据中为null，但不属于所引四条观测的字段；本条把这些独立元数据事实归入指标引用，引用不能支持完整陈述。另外全部现有证据的表述未引用第五条版本记录。不是凭空编造告警字段，而是证据归属和引用覆盖不足。 |
| 4 | 失败 | 所引get_running_version原记录明确status=no_data且reason=historical_fixture_has_no_current_runtime_identity，支持当前运行版本未知；它只有unverified_instance身份，没有历史commit。历史commit_sha实际位于另外四条历史指标的identity中，本claim未引用它们。因此后半句的历史commit表述虽可在完整原记录中找到背景，不能由唯一所引版本观测支持。未将no_data当成服务不存在，也未虚构版本评估时间。 |
| 5 | 失败 | 完整五条来源确实没有日志、健康检查、依赖健康或数据库指标结果，但单条case-a-t1只能展示其自身三个HTTP指标，不能证明整个证据集合的缺口。logs/health/dependencies能力声明实际位于runtime-input的capabilities元数据，不在case-a-t1观测中。证据缺口不等于能力缺失这一原则正确；本条仍存在全局缺失结论与能力事实的引用不对应，不能因引用ID真实而通过。 |
| 6 | 失败 | 所引A/B历史指标支持5xx比例1.0；http_5xx_rate是错误比例，因而接近全量5xx有指标语义依据，不应误判为错误QPS或无基线的高流量。告警0.3与阈值0.2真实存在于alert_event元数据，order-db真实存在于capabilities.dependencies，但两类事实均不在本claim所引指标观测中；缺少日志/依赖结果也是全局集合判断。其可能对应已明确写为hypothesis，且未把数据库或网络确认为根因；然而告警与历史的时间对应尚不能核实，完整陈述缺少相应来源归属，所以严格引用支持审查失败。 |
| 7 | 通过 | case-a-t1提供完整历史同评估时刻的三个指标，case-a-t2提供另一历史目标时刻且缺5xx样本，因此重新取得同一明确评估时刻的当前指标有直接取证动机。本条是next_check，未宣称新数据已经取得、当前5xx持续或业务已经恢复，也未执行工具。相同评估时间仍不等于相同抓取时刻或HTTP请求原子性；本条未明确声称后两者。 |
| 8 | 通过 | case-b-errors-t1的历史5xx比例1.0可以支持继续寻找相关日志的取证方向。order-db是本次runtime-input可信capabilities.dependencies中已有的目标，不是依据名称新造的依赖；该元数据并非所引指标自身的拓扑证明。本条以确认是否出现表述待查日志，并先要求确定缺失的starts_at，没有伪造实际告警窗口、数据库错误或已取得的日志结果。此通过仅表示建议合理，不表示时间窗口已知或故障假设成立。 |
| 9 | 通过 | case-a-t1的历史5xx比例和抓取指标提供进一步调查服务与已登记依赖的动机；order-db在本次输入的可信配置依赖列表中。本条为next_check，先要求取得关系、健康和指标，没有声称order-db实际故障、postgres_up已有数值或网络是根因。后续执行仍需核对order-db的指标Profile及可用Provider，且健康或postgres_up单项不能独立区分数据库进程、认证、exporter和网络原因；此通过仅接受其作为待验证取证方向，不保证可执行性或完整因果区分。 |

关键区分：告警时间字段、capabilities、依赖和历史 commit 在完整 request 的元数据或其他记录中可以找到，不能把它们笼统称为凭空编造；问题是当前 claim 所引观测不支持完整陈述。当前 observation_id 空间覆盖五条工具来源，没有为 AlertEvent/capabilities 等上下文元数据提供相应可引用 ID，这也是本实验暴露的来源建模限制。

没有发现本轮再次将 B 的样本 evaluated_at 变成查询 evaluation_time，也没有把 no_data 补零、宣称当前恢复或把 hypothesis 确认为根因。http_5xx_rate 在 [MetricProfile](../services/metric_profiles.py) 中是 error_rate/total_rate，因此 1.0 的比例解释有依据；不能误判为错误 QPS 或无基线的高流量。即便这些方面正确，严格引用要求仍有五项失败。

[独立审查](../data/model-validation/p53c2d-ftie99sh/verification/independent-evidence-review.json)、[主 Agent 交叉检查](../data/model-validation/p53c2d-ftie99sh/verification/root-evidence-cross-check.json)与[审查后组合报告](../data/model-validation/p53c2d-ftie99sh/verification/preflight-report-reviewed.json)单独存档。原入口 preflight-report.json 的 pending 结果不覆盖，原始 sources/claims 不修改。

## C. 分层验收结果

| 验收层 | 实际结果 | 依据 |
| --- | --- | --- |
| Runtime 调查流程 | 通过 | COMPLETED，预算与结果持久化一致；不代表业务恢复 |
| 权威事实完整性 | 通过 | 五来源完整；209 字段对照无补时、虚构数值或来源丢失 |
| 模型输出结构 | 通过 | 合法 claims JSON，9 条、真实不重复引用、3 条建议；响应完整 |
| 模型推理/引用正确性 | **失败** | 独立逐条复核 4 通过、5 失败，错误保留 |
| 系统安全 | 通过本轮检查 | 单次付费、固定预算、隔离持久化、无真实外部工具或修复、原始数据保护 |
| 整体严格验收 | **失败** | 推理引用存在未解决的关键错误，机械通过不足以提升 |

这些结果只属于独立实验，不修改生产 Incident 状态或对外 API。本地测试 Incident 的 COMPLETED 是 Runtime 流程状态，不能作为生产恢复或推理正确证据。

## 费用与 P5.3c.2c 比较

已重新核对[DeepSeek 官方人民币价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)；按当前周末空闲时段的缓存输入 0.02、未缓存输入 1、输出 4 元/百万 tokens 估算：

`(6784 × 0.02 + 8139 × 1 + 650 × 4) / 1000000 = 0.01087468 元`，约 **0.01087 元**。同用量高峰口径为 0.02174936 元。未查询账户余额、账单或实际扣款，以上不是已核实扣款。见[价格快照](../data/model-validation/p53c2d-ftie99sh/verification/pricing-source.json)和[实际用量核对](../data/model-validation/p53c2d-ftie99sh/verification/live-inspection.json)。

| 比较项 | P5.3c.2c | P5.3c.2d 本轮 |
| --- | --- | --- |
| 权威事实职责 | Python 索引后，模型仍复述 sources | Python 直接生成事实展示；模型仅返回 claims |
| 模型事实字段校验 | B 两来源错误生成查询 evaluation_time，机械拒绝 | 不要求模型填写该字段；事实层准确，不再出现该错误的事实副本 |
| 模型解释审查 | 严格复核失败 | 机械通过、独立逐条审查仍失败，5/9 引用范围或归属不足 |
| input / cached / output tokens | 14924 / 6784 / 1269 | 14923 / 6784 / 650 |
| 估算空闲费用 | 0.01335168 元（前报告） | 0.01087468 元 |
| 严格验收 | 不通过 | 不通过 |

前轮事实字段错误已通过职责分离从权威展示中消除，这不是证明模型已经掌握所有时间语义；引用存在与引用支持之间的缺口仍在。新旧契约不同、仅单案例各一次请求、未控制模型采样；官方当前 deepseek-flash 别名列为 DeepSeek-V4.1-Flash-1017，响应仅给别名，不能据此确认前后底层版本完全相同。输出 tokens 下降也不等于一般推理能力提升。

## 原始产物、SHA-256 与保护核对

真实原目录：`C:\Users\zp1222\AppData\Local\Temp\incident-p53c2d-ftie99sh`。最终离线原目录：`C:\Users\zp1222\AppData\Local\Temp\incident-p53c2d-548id3wf`。本轮归档：`D:\Incident Agent\data\model-validation\p53c2d-ftie99sh`，包含真实原件、offline/ 与 verification/。原始运行文件共 37 份，复制前后 SHA-256 全相同；完整清单见[artifact-manifest.json](../data/model-validation/p53c2d-ftie99sh/artifact-manifest.json)，后续文档/交付审计会继续追加清单。

| 关键文件 | SHA-256 |
| --- | --- |
| [request.json](../data/model-validation/p53c2d-ftie99sh/request.json) | `11731768b8b5164004aa224d3444a2820cdccd8ac97c817b12f6be6534ccf1fe` |
| [request-source-records.json](../data/model-validation/p53c2d-ftie99sh/request-source-records.json) | `6604eb43e7d7869fbba7d8b7f15dc7f3a003acb296718655892b970ea3e71b30` |
| [evidence-index.json](../data/model-validation/p53c2d-ftie99sh/evidence-index.json) | `8d051d7929bb9e08da7eac2033be3abb9df2662dfa9d4305a6c20ad5441ce794` |
| [authoritative-facts.json](../data/model-validation/p53c2d-ftie99sh/authoritative-facts.json) | `102ded06a4f96904a7385ceb405995e52dda73b303b878ad5b94c23107915bb8` |
| [response.raw.json](../data/model-validation/p53c2d-ftie99sh/response.raw.json) | `b3da66d57abf3f2cf4ad21dfa5cb1bc3693be469189267be29cbad4facce32d0` |
| [answer-original.txt](../data/model-validation/p53c2d-ftie99sh/answer-original.txt) | `1534581882587ad967352a7b0b284886b176a501477ccc705ca7d12240adebad` |
| [checkpoint.json](../data/model-validation/p53c2d-ftie99sh/checkpoint.json) | `5e5dff7ef958e1a408fe60caa12b9958aceaf8c0dce00b599e6b1d7a6263d6b7` |
| [runtime.sqlite3](../data/model-validation/p53c2d-ftie99sh/runtime.sqlite3) | `9d0aba08490bb09f77164f94c7feedca07e8f4c0bff1ce6c3d5aea934ca8ba45` |
| [verification/independent-evidence-review.json](../data/model-validation/p53c2d-ftie99sh/verification/independent-evidence-review.json) | `c3e8209a72a318554008ae41e7856f073911fb59e00d8bec9838a77f9a189729` |
| [verification/preflight-report-reviewed.json](../data/model-validation/p53c2d-ftie99sh/verification/preflight-report-reviewed.json) | `8b62626bcd3014a44b34a66d2ce0cfa9020c48dec35b3939c9a1acb523996599` |

687 个原有受保护文件、四次历史归档和四个历史原临时目录均核对无变更；生产运行数据、密钥文件和用户已有改动保留，见[付费后保护核对](../data/model-validation/p53c2d-ftie99sh/verification/post-live-integrity.json)。没有结束无关进程、启用故障开关或留下本轮启动的业务服务。独立临时 SQLite 与原始产物按要求保留，不删除。数据归档受既有 /data/ 忽略规则保护，不加入 Git；这些本地证据链接在其他检出环境不会自动存在。

## 已知限制、下一阶段与交付

机械校验没有自动识别五条语义引用错误，也没有消除所有重复事实文本。事实视图只保证对本轮已核对输入的忠实展示，不认证真实后端可用性；当前输入是固定历史夹具。未验证生产调度、真实指标/日志/业务恢复、真实修复、一般故障推理稳定性或实际账单。

本轮按要求停止继续 Prompt 追加与同类重试。下一阶段建议先设计多案例评估和模型适用性分析：覆盖不同来源/时间缺失、有效零/no_data、版本身份变化和错误引用；梳理工具 observation 与 AlertEvent/配置元数据的可引用来源类型；用明确证据标准对不同模型进行逐 claim 盲审。不要把未建立来源身份的上下文事实强行归到无关工具 ID。上述仅为建议，本轮未实施下一阶段。

本报告正文完成后读取实际北京时间到秒命名；仅选择本轮脚本、两份测试与本报告提交推送。实际提交 SHA、目标分支和远端一致性在交付消息及本地 delivery.json 记录，不在待提交报告中提前声明成功。完成交付后停止，等待代码审查。
