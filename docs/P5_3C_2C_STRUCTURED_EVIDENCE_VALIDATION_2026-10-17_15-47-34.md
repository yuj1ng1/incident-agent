# P5.3c.2c 结构化证据交付与一次真实模型验证

本轮完成独立验证入口的确定性 evidence_index、结构化回答校验、Mock 回归和唯一一次真实 DeepSeek 请求。**结构化事实验收失败，调查推理严格复核也失败，P5.3c.2 严格验收不通过。** 回答完整，没有重试、提高预算或用 Python 修正模型答案。原始索引无字段归属错误；模型将两条 B 组指标的样本 evaluated_at 复制成原始不存在的查询 evaluation_time。

## 基线与实际修改范围

项目 `D:\Incident Agent`，仓库 `yuj1ng1/incident-agent`。开始时分支 main，本地 HEAD 和实际远端 main 均为 `7f9befe93baa07c59f00bfb8f21321781764b2c0`，暂存区为空。README、生产 Prompt、temp_test.py、文档整理和未跟踪说明均是用户原有修改，未覆盖、未纳入本次提交；前三次真实模型验证产物也保留。

实际交付文件仅为：

- [scripts/preflight_real_model.py](../scripts/preflight_real_model.py)：精简独立 TASK；增加 Runtime 封套读取、确定性索引构建和模型 JSON 校验；在独立 Provider 边界追加派生数据并保存原输入、原记录、索引及校验结果；真实模式结构失败时 CLI 返回 2。
- [tests/test_real_model_preflight.py](../tests/test_real_model_preflight.py)：将旧十条 TASK 文本断言改为两部分 JSON 的格式和边界要求，保留原有安全测试。
- [tests/test_preflight_evidence_index.py](../tests/test_preflight_evidence_index.py)：新增 54 项全 Mock 的字段、来源、缺失、异常及人工审查边界测试。
- 本报告。

复用现有 Worker、Agent、ModelClient、RuntimeStore、固定历史夹具和 metric_analysis。没有修改生产 Worker、生产 Prompt、确定性指标分析器、状态机、Checkpoint schema、数据库表、工具权限、审批边界、服务配置或模型预算，没有新增第三方依赖；Decimal 来自 Python 标准库。未涉及前端，因此没有重复前端回归。

生产 Prompt 沿用任务前已有未提交内容，实际 SHA-256 为 `94faddc0b0029a11be7f815d571f3fad47e0d2cad6305be0aa899752f8c7350a`。不能把本次说成在干净基线 Prompt 上运行。真实与离线 instructions 完全一致，完整内容保存在 request.json。

## 索引生成路径与字段含义

实际调用顺序已核对：Worker 先 bootstrap 本地 get_running_version，追加并提交 observation；随后构造上下文、持久化模型预算，再经 Agent/ModelClient 进入 BoundedProvider.create。Provider 的 load_records 回调此时重读本 Session 当前已持久化的 Checkpoint，拿到四条历史指标和本轮动态版本记录，未在启动前只对四条夹具建索引。

仅在独立 Provider 内深复制请求并追加一个普通 user 封套：`context_origin=preflight_deterministic_derived`、`scope=independent_entry_only_not_runtime_history`，包含完整 raw_records 和 evidence_index。原 input 原样保存为 runtime-input.json，实际 request.input 除最后封套外与它逐项相等；新封套未写回 Checkpoint 历史。没有伪造 function_call/function_call_output，也没有改变 Worker 计数。

索引是 schema_version=1 的独立派生对象，sources 按持久化原记录顺序构建。每项包含 observation_id、工具 name、collected_at、原 result.status、historical_only、data_missing、extraction_status 和 samples；仅当原 result 实际存在时保留 evaluation_time/evaluated_at。samples 中的 metric、status、value、evaluated_at 同样按原字段存在性提取，不从查询或邻项填补。

| 概念 | 本轮含义与约束 |
| --- | --- |
| collected_at | 原记录采集/保存时间，不等于查询或样本评估时间，也不证明当前健康 |
| evaluation_time | 仅复制原工具明确提供的查询目标时点；字段不存在就不在索引中生成 |
| evaluated_at | 归属于具体样本；instant 原顶层字段也保留，但不改名成查询时间 |
| historical_only | 复用实际 Runtime use_for_current_state=false；包括缺失版本证据，不表示版本记录是“历史指标” |
| data_missing | 本轮原顶层 no_data 或子样本 no_data 标记；不会补零或确认服务不存在 |
| extraction_status | 仅支持本实验 query_metrics/query_metrics_aligned 结构与本地 no_data 版本结果；未知工具、不支持的顶层状态或结构显式 unsupported，并在 SDK 入口前拒绝 |
| 索引省略字段 | 原字段不存在；索引没有用 null 假装原始存在。模型回答中缺失字段要求写 null，二者语义由格式说明区分 |

没有重算、替换或裁剪 metric_analysis。四条原始历史记录、动态版本完整结果和完整分析均在请求中保留，人工 expected、grading、rubric 及测试合成回答未进入请求。

## 原始记录到索引的逐字段核验

离线及真实请求分别完成 **116 项字段核对**，包括存在性、原值、工具、状态、采集时间、每个子指标、历史用途及缺失标记；均一致。完整逐字段 actual/raw 对照见[离线审计](../data/model-validation/p53c2c-t8d8w94y/verification/offline-request-audit.json)和[真实审计](../data/model-validation/p53c2c-t8d8w94y/verification/live-request-audit.json)。所有 ID 与真实来源一一对应，来源数为 5。

以下全部时间来自真实请求对应的原始记录，使用带时区 UTC；T1=`2026-10-11T10:48:55+00:00`，T2=`2026-10-11T10:48:55.300000+00:00`。

| observation_id | 工具 / 顶层状态 | collected_at | 原查询 evaluation_time | 子指标样本及时间 | data_missing |
| --- | --- | --- | --- | --- | --- |
| case-a-t1 | query_metrics_aligned / ok | 2026-10-14T02:00:00+00:00 | T1 | 请求率 0.41821223361699034、5xx 1.0、scrape_up 1.0；三条 evaluated_at=T1 | false |
| case-a-t2 | query_metrics_aligned / incomplete | 2026-10-14T02:00:00+00:00 | T2 | 请求率 0.0、scrape_up 1.0 的 evaluated_at=T2；5xx=no_data，原 value/evaluated_at 均不存在 | true |
| case-b-errors-t1 | query_metrics / ok | 2026-10-14T02:00:00+00:00 | **不存在** | 5xx 1.0，样本 evaluated_at=T1 | false |
| case-b-rate-t2 | query_metrics / ok | 2026-10-14T02:00:00+00:00 | **不存在** | 请求率 0.0，样本 evaluated_at=T2 | false |
| obs-0da63e742d3d496f91fe3c66e89c5f01 | get_running_version / no_data | 2026-10-17T06:21:49.238519+00:00 | **不存在** | 无指标；原 evaluated_at 也不存在 | true |

完整 raw_records 与 Checkpoint.records 相等，前四条与 case.json.records 相等；所有原 value/status/时间和工具其他字段均未改变。metric_analysis selected_record_count=4、各 excluded_record_counts=0、status=partial；完整 reports 与现有分析器对原记录的结果相等，并与最终离线请求相等。历史分析仍 use_for_current_state=false。

## 输出格式与机械校验边界

TASK 由十条规则改为一个紧凑 JSON 的两部分：sources 逐来源事实和 conclusion 简短解释。sources 明确区分 has_evaluation_time、evaluation_time、具体 samples 的 value/evaluated_at；conclusion 包含 confirmed、time_comparison、missing_vs_zero、unconfirmed、next_checks，各条带 text 和 observation_ids，建议最多三项。不要求内部推理过程，也没有写入固定正确日期或具体来源 ID。

validate_model_answer 检查 JSON 可解析性、重复键、非有限数、来源集合/重复/伪造、字段存在性和类型、采集/查询时间、状态、子指标集合、样本值和样本时间，以及结论基本结构和引用 ID 是否存在。Decimal 精确解析防止 `1e-999` 下溢成零；布尔值不能冒充数值，大整数不会导致有限性检查溢出。离线独立审查发现的这两个数值问题均在付费前修复并覆盖回归。

**引用存在、字段正确、JSON 合法或 response.completed 均不能证明解释正确。** 机械通过时 strict_acceptance 仍是 pending、manual_reasoning_review=required；字段错误则明确 failed。校验不修订回答，不重试，不替模型产生结论。即使 Runtime 完成保存流程也不改变业务 Session 状态来伪装推理成功；真实 CLI 额外因结构验收失败返回 2。

## Mock 测试、完整回归与请求审计

解释器均为 `D:\conda\envs\agentlearn\python.exe`，Python 3.12.14；OpenAI SDK 3.14.0、httpx2 2.13.0、Pydantic 2.13.5、python-dotenv 1.2.3、pytest 9.1.1。所有定向测试均禁网，注入的 live=True SDK 也是 Mock 或 SDK MockTransport，不能当作真实调用。

| 实际命令/检查 | 结果 |
| --- | --- |
| `python -B -m pytest tests/test_real_model_preflight.py -q -p no:cacheprovider --tb=short` | 45 passed in 31.80s |
| 联合定向 `python -B -m pytest tests/test_real_model_preflight.py tests/test_preflight_evidence_index.py -q -p no:cacheprovider --tb=short` | 98 passed in 82.96s；执行期间测试文件又补充最后一项 |
| 对最终新增重复键用例及加强后的第一项补验 | 2 passed in 8.02s；其中一项与联合测试重叠，不合计成 100 项 |
| `python -B -m pytest -q -rs -p no:cacheprovider --tb=short` | **1745 passed、2 skipped、190 subtests passed in 515.87s**；覆盖最终两文件全部 99 项（45 既有＋54 新增） |
| `python -B -m pip check` | No broken requirements found. |
| 最终默认离线 CLI | COMPLETED，真实请求 0；回答明确为占位文本，因此结构检查 failed 是预期，不代表模型推理结果 |
| 离线/真实请求审计 | 每次 116 字段核对一致；请求 58,746 字节，小于原 98,304 上限；原输入、原记录、完整分析保留；密钥及 rubric 检查通过 |

两个 skipped 分别是 tests/test_dependencies.py:28 的 CI/隔离 venv clean-install 检查，和 tests/test_multi_signal.py:111 未设置 ALERT_RULES_PATH 的可选跨仓库验证。未修改跳过条件，没有把跳过计为通过。测试数量不与定向结果相加。未重装干净环境或运行无关前端检查。

新增测试覆盖 A 五来源完整性、B 跨日期不继承、C 版本无评估时间、D aligned 查询目标与缺失子样本区别、E 原记录/分析/历史链和 rubric 隔离、F 正确/错误日期/补时间/补零/漏 ID/造 ID/非法 JSON/不完整结构。另覆盖重复 ID/子指标、非法引用、重复 JSON 键、非有限数、布尔值、大整数、数值下溢和来源不一致。字段正确但自然语言谬误的 Mock 回答原样保存且仍要求人工审查，不能自动通过严格验收。

## 一次真实请求与运行安全

定向、完整回归、依赖检查、离线请求审计和[安全门](../data/model-validation/p53c2c-t8d8w94y/verification/pre-live-gate.json)全部通过后，只执行一次：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B 'D:\Incident Agent\scripts\preflight_real_model.py' --run-live --approve-paid-model-run
```

固定 deepseek-flash，DeepSeek Responses `https://api.deepseek.com/responses`；max_output_tokens=2048、reasoning.effort=none、tool_choice=none、tools=[]；SDK 构造及 with_options 的 max_retries=0；请求超时 30 秒、自有监督子进程总时限 90 秒。Runtime max_attempts/model requests=1、resume/human requests=0。没有提高任何预算，也没有第二次请求。

外层登记单次执行标记时，操作命令 `New-Item -LiteralPath` 因不支持该参数失败；后续付费命令仍实际执行一次。收到响应后用排他文件创建补记已消费标记，并对 SDK、Runtime、事件和产物核对为一次，未重新运行付费命令。该操作错误不是 API 失败，也不能说原标记在调用前已成功建立，见[登记记录](../data/model-validation/p53c2c-t8d8w94y/verification/operator-bookkeeping.json)。单次限制主要仍由 Provider/Runtime 预算及本阶段不再启动命令保证。

| 实际返回/用量 | 结果 |
| --- | --- |
| 请求/返回模型 | deepseek-flash / deepseek-flash |
| response.status / incomplete_details / error | completed / null / null |
| 输出 item | 一个 completed message；模型 function_call=0，无 reasoning item |
| input / cached / uncached tokens | 14924 / 6784 / 8140 |
| output / reasoning / total tokens | 1269 / 0 / 16193 |
| 正文 / 简短结论 | 3113 字符；conclusion 共 271 个基本 CJK 汉字、13 条，next_checks=3 |
| response.created_at | 2026-10-17T06:21:52+00:00 |
| summary.created_at | 2026-10-17T06:21:56.921285+00:00 |
| CLI / Session | 退出 2（结构拒绝）/ COMPLETED（保存流程完成） |

实际付费 SDK create 入口、Provider 请求入口和 Runtime 模型预留均为 1。此计数表示本地 SDK 入口，不是独立服务端账单审计；已收到真实响应。工具执行为 1 次本地精确替代的 bootstrap 版本 no_data，不是在线业务探测。模型工具调用为 0，没有真实修复。

独立临时目录、SQLite、Incident JSON 和日志均隔离。未启动生产 main、Monitor、Scheduler、RecoveryWorker，未访问真实 Prometheus、Loki 或业务服务，manager.remediation_planner=None。SQLite 用 mode=ro 在新进程重读：attempt_count/current_attempt_rounds/current_attempt_tool_calls/total_model_requests/total_tool_executions 均为 1；resume_count/human_request_count=0。model_reserved/model_response_saved/investigation_completed/tool_reserved 各 1，checkpoint_saved=9，无重试或 remediation 事件。tool_failed=1 来自本地 no_data 分类，不能视为真实服务故障。没有注入故障、待恢复服务或新增临时后台资源。

## 分开验收：结构化事实与调查推理

主 Agent 与独立只读审查 Agent 分别逐字段及逐条核查了原始 request、raw response、index 和 records；审查不代表用户本人已阅。原 summary 的 manual_reasoning_review=pending 和原响应均保留，实际判定另存审查 JSON，没有回写模型答案。

| 结构化事实项 | 判定 |
| --- | --- |
| 实际来源数及 ID | 通过；5 条真实来源各一次，无伪造或漏项 |
| 全部 collected_at、顶层状态 | 通过；版本保留本轮采集时刻，未继承历史日期 |
| A 两条查询 evaluation_time | 通过 |
| B 两条查询 evaluation_time | **失败**；原字段不存在，模型却各写 has_evaluation_time=true，并把样本 evaluated_at 填入 evaluation_time，共 4 个确定字段错误 |
| 8 个子指标状态、有效值、自身 evaluated_at | 通过；有效零保留，缺失 5xx 的 value/evaluated_at 均为 null |
| 版本无评估时间 | 通过；has_evaluation_time=false、evaluation_time=null、samples=[] |
| JSON/结构/完成状态 | 合法完整，但不能覆盖上述事实错误；机械结论 failed |

需要精确区分：B 组时间字面量本身确实来自原样本，不是随意生成一个日期；错误是**把样本字段升级成原始不存在的查询字段**。不能通过放宽字段验收来接受它。

| 调查推理项 | 判定与依据 |
| --- | --- |
| 跨来源采集日期 | 本轮未再出现“所有记录都采集于历史日期”的错误 |
| B 时间关系 | 样本相差 0.3 秒、不能拼成同刻快照正确；但原文“case-b 两次查询评估时刻相差 0.3 秒”仍把样本时刻说成未提供的查询时刻，**明确归属错误** |
| 告警时间未知 | 未断言与历史相同或不同，事实正确；但 null 来自 alert_event，仅引用 case-a-t1 不能证明这些告警字段 |
| 有效零/no_data/版本 | 正确区分，未从 no_data 推断服务不存在、停止、恢复 |
| 当前健康与根因 | 未用历史确认当前健康或恢复，根因保持未确认 |
| 引用充分性 | 所有引用 ID 均存在；但全局日志/依赖/数据库证据缺口仅引用版本 no_data，依据不对应；“全部指标过期”仅引用其中两条也不覆盖完整范围 |
| 定性表述 | 未再称“高请求率”；“无流量下低样本”缺少样本量或基线，可能是一般性提示，不能作为本案例低样本的确认事实；不夸大判为已编造具体计数 |
| 抓取表述 | “历史快照…抓取成功”的直接支持是 scrape_up=1；没有实际 scrape timestamp，不能据此确认抓取发生在该评估时刻 |
| 后续取证 | 三项方向，无请求/修复执行 |

自然语言严格判定为 failed，不能因大部分限制正确而忽略字段归属和引用不足。Runtime 安全条件在本次隔离运行中通过；这三项分别报告，整体严格验收仍失败。

## 费用、与 P5.3c.2b 比较及限制

本轮重新核对[DeepSeek 官方人民币价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)，周末全天为空闲时段。调用发生在北京时间 2026-10-17 周六 14:21，按每百万缓存输入 0.02 元、未缓存输入 1 元、输出 4 元估算：

`(6784 × 0.02 + 8140 × 1 + 1269 × 4) / 1000000 = 0.01335168 元`，约 **0.01335 元**。同缓存构成高峰口径 0.02670336 元；假设输入全未缓存的空闲口径 0.02 元。未查询账户余额、实际扣款或账单，以上不是已核实扣款。价格取值与用量计算分别归档于[价格来源](../data/model-validation/p53c2c-t8d8w94y/verification/pricing-source.json)及[实际用量检查](../data/model-validation/p53c2c-t8d8w94y/verification/live-inspection.json)。

对照 [P5.3c.2b 报告](P5_3C_2B_REAL_MODEL_VALIDATION_2026-10-17_13-00-50.md)：

| 项目 | P5.3c.2b | 本轮 P5.3c.2c |
| --- | --- | --- |
| 交付 | 原始证据＋分析＋十条规则 | 原输入保留，追加完整原记录＋确定性索引；两部分 JSON |
| input / cached / output tokens | 12286 / 7040 / 706 | 14924 / 6784 / 1269 |
| 完整性 / 预算 | completed / 2048 | completed / 2048，未提高 |
| 跨来源采集日期 | 误将历史日期覆盖版本 | 本次五条 collected_at 均正确 |
| 缺失子样本时间 | “三指标同刻”未充分区分 | 结构中缺失 value/evaluated_at 正确保留 null |
| 版本时间/引用 | 日期归属错，ID 已改善 | 日期及无评估字段正确，实际 ID 引用保留 |
| 查询/样本时间 | 自然语言归属仍不够严格 | B 两条显式补出不存在的 query 字段，确定性校验拒绝；结论也有同类措辞 |
| 严格验收 | 失败 | **失败** |

可以确认本次输出的部分归属改善；不能据单次结果声称索引稳定有效，也不能声称它单独造成改善。输出格式、TASK、索引及输入重复结构同时变化，缓存构成也不同，没有对照试验；这不是无提示的根因发现测试。模型仍可能复制索引并在解释中犯错，索引不是可信结论替代品。

未验证：其他故障案例、多次采样稳定性、真实工具和业务系统、当前健康/恢复、自主工具选择、多轮 Responses、干净环境安装、真实账单及推送后远端 CI。下一步等待代码审查，重点检查 B 组字段升级和引用归属；若另开实验，应先确定如何验证这类错误，不能在本轮追加调用或事后改写答案。本阶段不接入生产 Agent。

## 归档、哈希与交付状态

真实原目录 `C:\Users\zp1222\AppData\Local\Temp\incident-p53c2c-t8d8w94y`，最终离线目录 `C:\Users\zp1222\AppData\Local\Temp\incident-p53c2c-te7huyuy`。57 个文件逐文件复制到独立的 `data/model-validation/p53c2c-t8d8w94y`，SHA-256 均一致，见[归档清单](../data/model-validation/p53c2c-t8d8w94y/artifact-manifest.json)。此前三次目录没有覆盖。

| 文件 | SHA-256 |
| --- | --- |
| [实际请求](../data/model-validation/p53c2c-t8d8w94y/request.json) | `36050920e3c14e23ddfdb5585381d9bceb61fdc3fbc85419fef0f455bcaf37db` |
| [原始响应](../data/model-validation/p53c2c-t8d8w94y/response.raw.json) | `0a86043217f220098ce50ca3f1222fdb4b89abdc5621693419e56a58c55b669c` |
| [SDK 响应](../data/model-validation/p53c2c-t8d8w94y/response.sdk.json) | `e6f4b7abbdb17605048b048cc8f89374fe778469d10d045b182d79da32aaa732` |
| [原始来源记录](../data/model-validation/p53c2c-t8d8w94y/request-source-records.json) | `4ab726e9ab916f040c18c5dd6ee113edcf69fdf7cb9af69d4909501e7ec3ef3f` |
| [证据索引](../data/model-validation/p53c2c-t8d8w94y/evidence-index.json) | `8706ab382ef2799bb996375963061292f20db4b3fe17f28fa2e1b416cb11f3e6` |
| [机械事实检查](../data/model-validation/p53c2c-t8d8w94y/answer-validation.json) | `2d5531dba58aea1a2a310eae0be388e367f932805d5486a4ecc53ae0b4c9a710` |
| [Checkpoint](../data/model-validation/p53c2c-t8d8w94y/checkpoint.json) | `cd6c0d6a559e187dc3cdc94b016fc154b64c09c249f49949cf11089e705a9cea` |
| [主逐项复核](../data/model-validation/p53c2c-t8d8w94y/verification/manual-review.json) | `76cf33ac9eb1268960b1cdb9db412dddf74b1926bf579abeb4aa468a1a68fa9e` |
| [独立逐项复核](../data/model-validation/p53c2c-t8d8w94y/verification/independent-evidence-review.json) | `79dce4e027f44005c685aa5986c239d78d9471cc116c795b2084d86aa273592f` |
| [完整回归日志](../data/model-validation/p53c2c-t8d8w94y/verification/full-regression.txt) | `121d7f01dab851b51575e461fdce279b8b755a02963fb02e385199b2208e5657` |

响应 SDK 文本、raw 文本提取与 Checkpoint.answer 完全一致；[answer-original.txt](../data/model-validation/p53c2c-t8d8w94y/answer-original.txt)仅为审查方便提取，没有修订。请求、索引、运行原数据与审计材料受既有 /data/ 忽略规则保护，不加入 Git；这些本地链接在其他检出环境不会自动存在。

623 个既有文件保护快照在付费前、付费后均复核，仅本轮 scripts/tests 两个既有文件发生授权变更；新增测试和本报告另计。原有未提交修改、运行数据和三次旧验证产物未变，暂存前 index 与开始一致，源码冻结哈希在真实调用后保持一致，见[完整性检查](../data/model-validation/p53c2c-t8d8w94y/verification/final-integrity.json)。新产物和交付文本按实际密钥值扫描，不命中，不保存 Authorization 请求头或 .env 内容。

本报告正文写入后按实际北京时间秒级完成时间命名。提交仅包含上述两份既有文件、新增测试和本报告；提交、推送及本地/远端 SHA 一致性在实际完成后由最终交付消息和本地 delivery.json 记录，不提前将待提交状态写成已推送。完成提交推送后停止，等待代码审查，不自动进入下一轮模型测试。
