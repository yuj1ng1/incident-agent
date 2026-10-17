# P5.3c.2b 多来源证据时间与引用准确性验证

本轮已完成独立 TASK 优化、离线验证及一次已授权真实模型复测，**P5.3c.2 严格验收仍不通过**。回答完整，版本探测的 observation_id 引用已补齐，但仍将四条历史指标的采集日期推广到版本记录；缺失样本评估时间的表述也不满足严格要求。未追加请求、重试、提高预算或在返回后继续调整 TASK，没有用 Python 改写回答。

## 基线、修改范围与设计

项目 `D:\Incident Agent`，仓库 `yuj1ng1/incident-agent`。开始时本地 HEAD 与远端 `origin/main` 均为 `00ecbf4a1ec08268df59a142f1047ed932a3909a`，分支 `main`，暂存区为空。README、生产 Prompt、temp_test.py、历史文档删除/归档及多份未跟踪文档是任务前已有改动，均保留并排除在本次提交之外。

实际修改仅为：

- [scripts/preflight_real_model.py](../scripts/preflight_real_model.py)：只增加 TASK 中十条通用证据解释规则，强调每条记录及子指标的 ID、采集/评估时间、状态独立；缺失不继承；全称必须核实范围；关键数值/no_data/工具失败/版本事实就近引用；历史与当前状态分开；根因保持假设。对无来源 ID 的输入要求说明字段来源，避免为满足引用而编造 ID。
- [tests/test_real_model_preflight.py](../tests/test_real_model_preflight.py)：补充逐来源请求核对、异日采集、版本无评估时间、缺样本与有效零，以及五种合成回答的引用覆盖/待人工复核/原文保留测试。
- 本中文验证报告。没有新增生产接口、业务数据结构或依赖。

保留既有五段中文结构、约 500～800 汉字目标、最多三项后续验证方向。不向 TASK 写入本案正确日期、具体 observation_id 或标准答案。AST 比较确认脚本除 TASK 外与基线一致；没有修改生产 Agent Prompt、Worker、RuntimeStore、确定性指标分析器、状态机、预算、数据库或审批权限。

复用现有 Mock、固定历史夹具、真实隔离 Runtime/Agent 请求链及 metric_analysis，不另建生产逻辑。测试内 `review_citations` 只检查完整已知 ID 和显式 `[observation_id=...]` 的未知 ID；其他格式的伪造、就近归属、全称范围和语义均需逐字段复核。ID 齐全时仍返回 `semantic_review=required`、`strict_acceptance=pending`，不会判为推理通过。漏版本、错误全称、正确限定、伪造 ID、截断 ID 的合成回答均原样保留；错误全称和正确限定回答的测试证明的是“不自动放行、待审且不改写”，不是 Python 已理解了自然语言。

## 离线验证和实际请求审计

解释器 `D:\conda\envs\agentlearn\python.exe`，Python 3.12.14；OpenAI SDK 3.14.0、httpx2 2.13.0、Pydantic 2.13.5、python-dotenv 1.2.3、pytest 9.1.1。实际 `pip check` 返回 `No broken requirements found.`。

| 检查 | 实际结果 |
| --- | --- |
| `python -B -m pytest tests/test_real_model_preflight.py -q -p no:cacheprovider` | 45 passed in 24.44s，退出 0 |
| `python -B -m pytest -q -rs -p no:cacheprovider` | 1691 passed、2 skipped、190 subtests passed in 453.41s；进程总耗时 458.399s，退出 0 |
| `frontend` 中 `npm test` | 5 个测试文件、54 项测试通过，退出 0 |
| `frontend` 中 `npm run build` | TypeScript 检查与 Vite 构建通过，退出 0 |
| 默认离线 CLI | COMPLETED；真实模型请求 0；回答是明确标识的传输占位，不证明模型推理 |
| diff/AST 检查 | 本次 diff 无空白错误；脚本除 TASK 外 AST 一致 |

两项 skipped 原因分别为 `tests/test_dependencies.py:28` 的 clean-install 检查仅在 CI/隔离 venv 运行，以及 `tests/test_multi_signal.py:111` 未设置可选的跨仓库 `ALERT_RULES_PATH`。跳过不计通过；定向结果已经被完整回归覆盖，不与完整回归相加成独立测试数量。前端检查属于额外本地回归，未修改前端源码；没有在干净环境重装依赖。

默认离线产物目录 `C:\Users\zp1222\AppData\Local\Temp\incident-p53c2-qr1sndsx`。实际请求紧凑 UTF-8 为 51843 字节，低于原 98304 字节上限；instructions 为 18549 字符，其中 TASK 为 1170 字符。实际序列化 body 保留四条固定指标、一条 Runtime 版本记录的完整结果、来源 ID、各自 collected_at 和可用评估时间。有效零仍有 value/evaluated_at；no_data 子项没有被补值或补样本时间。

metric_analysis 的 selected_record_count=4、各 excluded_record_counts=0、status=partial、evidence_scope=historical_analysis、use_for_current_state=false；与离线请求中的完整 metric_analysis 逐字段一致。真实与离线的四条固定夹具和固定请求参数一致；动态 Session、Incident、版本来源 ID 与采集时刻按各次运行生成。`case.json.expected`、grading、人工 rubric 与合成回答均未进入实际请求。

生产 Prompt 的实际 SHA-256 为 `94faddc0b0029a11be7f815d571f3fad47e0d2cad6305be0aa899752f8c7350a`，沿用任务前用户已有的未提交修改；不能将此次真实请求说成在干净基线 Prompt 上运行。完整实际 instructions 已保存在原始 request，供复现审查。TASK 原有 A/B 案例背景与确定性分析也已提示时间分类、缺失分类和当前状态边界，因此本次不是无提示的独立根因发现实验。

## 安全条件与唯一真实请求

定向、完整回归、默认离线 CLI、请求审计、依赖检查和安全门全部通过后，按本阶段已有授权执行以下命令一次：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B 'D:\Incident Agent\scripts\preflight_real_model.py' --run-live --approve-paid-model-run
```

模型 `deepseek-flash`，端点 `https://api.deepseek.com/responses`；`max_output_tokens=2048`、`reasoning.effort=none`、`tools=[]`、`tool_choice=none`。SDK 构造和 with_options 均 max_retries=0；SDK 超时 30 秒，自有子进程监督总时限 90 秒，Runtime max_attempts=1、model requests=1、resume=0、human requests=0。工作临时目录另留单次入口标记，防止本阶段重复运行。

本轮实际付费 SDK create 入口 **1 次**，Provider 请求入口和 Runtime 模型预留均为 1；收到一个真实 Response。该计数表示本地进入 SDK，不是独立的服务端账单审计。没有自动重试、人工追加请求或批量对照。CLI 退出 0、Session COMPLETED 仅表示流程和响应完成，原 summary.manual_reasoning_review=pending 未改写，严格失败另存审查文件。

每次使用新建空 OS 临时目录、隔离日志及临时 SQLite/Incident JSON。未启动生产 main、Monitor、Scheduler、RecoveryWorker，未访问真实 Prometheus、Loki 或业务服务，未实例化或执行真实修复器。模型工具调用为 0；Runtime bootstrap 本地版本夹具执行 1 次，返回 no_data，不是在线版本探测。Manager.remediation_planner=None；导入的工具客户端构造仅保留配置，实际执行已由本地精确夹具替换。

新进程以 SQLite `mode=ro` 重读本轮临时 Session：attempt_count=1、current_attempt_rounds=1、current_attempt_tool_calls=1、total_model_requests=1、total_tool_executions=1、resume_count=0、human_request_count=0。事件 model_reserved/model_response_saved/investigation_completed/tool_reserved 各 1；无 waiting_retry 或 remediation 事件。tool_failed=1 来自本地版本 no_data 分类，不能解释为真实服务故障。

## 返回、实际来源与逐项严格验收

| 返回项 | 实际值 |
| --- | --- |
| 请求/返回模型 | deepseek-flash / deepseek-flash |
| response.status / incomplete_details / error | completed / null / null |
| output | 一个 completed message；无模型工具或 reasoning item |
| input / cached input / uncached input tokens | 12286 / 7040 / 5246 |
| output / reasoning / total tokens | 706 / 0 / 12992 |
| 正文 | 1283 个总字符，U+4E00～U+9FFF 汉字 540 个；五段完整，三项后续方向 |
| response.created_at | 1792212988，即 2026-10-17T04:56:28+00:00 |
| summary.created_at | 2026-10-17T04:56:32.509628+00:00 |

实际 source 字段如下；以下时间来自本轮 request/checkpoint，不是模型推测：

| observation_id | collected_at | 可用评估时间与状态 |
| --- | --- | --- |
| case-a-t1 | 2026-10-14T02:00:00+00:00 | 查询 evaluation_time 为 2026-10-11T10:48:55+00:00；三条有效样本 evaluated_at 相同，status=ok |
| case-a-t2 | 2026-10-14T02:00:00+00:00 | 查询 evaluation_time 为 2026-10-11T10:48:55.300000+00:00；请求率/抓取样本有此 evaluated_at，5xx=no_data 无 value/evaluated_at；整体 incomplete |
| case-b-errors-t1 | 2026-10-14T02:00:00+00:00 | evaluated_at=2026-10-11T10:48:55+00:00，5xx=1.0，status=ok |
| case-b-rate-t2 | 2026-10-14T02:00:00+00:00 | evaluated_at=2026-10-11T10:48:55.300000+00:00，请求率=0.0，status=ok |
| obs-598cd192842742d18788fcf6a5fc2a84 | 2026-10-17T04:56:25.434698+00:00 | 本地 get_running_version，status=no_data；无 evaluated_at 或 evaluation_time，评估时间未知 |

主开发 Agent 与两名独立只读审查 Agent 均对照实际 request、raw response、checkpoint 逐项复核，结论一致为失败；这不表示用户本人已审阅。并非只按 completed、关键词或 ID 集合打分。

| 用户严格验收项 | 实际判定与依据 |
| --- | --- |
| 1. completed、正文完整 | 通过；706 输出 token，五段和结尾完整 |
| 2. 历史指标采集/评估区分 | 限四条历史指标通过；日期正确分开，T1/T2 不混为同一快照；不能外推到版本 |
| 3. 版本采集时间不被替换 | **失败**；“各记录 collected_at 均为 2026-10-14T02:00:00Z”未限定指标范围，与本轮版本采集时间冲突 |
| 4. 缺失评估时间保持未知 | **未满足严格要求**；未交代版本评估时间未知，又写“case-a-t2 虽三指标同刻”，没有区分查询目标时点与缺失样本的实际时间 |
| 5. 不推广部分来源属性 | **失败**；四条指标的采集日期被全称赋给五条来源 |
| 6. 关键事实引用真实 ID | 主要事实通过；首段四个指标 ID 均真实，版本 no_data 也就近引用真实 obs ID；后文重复事实未总是重复引用，不能据 ID 齐全认定时间正确 |
| 7. no_data 不转换为有效零 | 通过；有效请求率 0.0 与缺失 5xx、缺失版本信息分别解释，没有从 no_data 断言服务不存在/停止/恢复 |
| 8. 历史不确认当前健康/恢复 | 通过；当前健康和恢复均未确认，告警时间缺失也未断言其与历史的具体关系 |
| 9. 假设不升级根因 | 通过；应用/依赖/流量解释均为可能或未经验证，根因未确认 |
| 10. 不捏造时间、数值或记录 | **失败（字段归属）**；没有发现新造的来源 ID 或数字字面量，但把已有日期错误赋予另一来源，仍是时间事实错误 |

需要区分确认错误和表述缺口。版本采集日期被“各记录均为”覆盖是确认错误；版本评估时间是未交代，不是发现模型明确编造了一个具体版本评估时间。“三指标同刻”可能意指共同查询目标，但没有限定这一语义，且分析已将完整三指标比较列为 strict_time_alignment=false，只有两条有效样本可严格对齐；严格验收不能自动作有利解释。输入共享查询评估时点不能为 no_data 子项创造实际样本时间。

回答另一句“所有记录均标记 expired、use_for_current_state=false”对本次五条来源是正确的，不应一概把所有全称句都判错。本次版本 no_data 已引用真实 ID，确实修复了上一阶段的引用遗漏；不能因此忽略仍存在的时间范围错误。

额外精度问题：回答称“高请求率”，而输入没有请求率高低阈值或基线，属于无依据的定性增强；未进一步升级为已确认根因。0.4182 是原值 0.41821223361699034 的近似，未发现新增数值。告警 0.3 来自 alert_event.value，其自身没有 observation_id，不能要求模型为它编造 ID。

## 费用估算与阶段结论

本次重新读取 [DeepSeek 官方人民币价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)，官方页面规定周末全天为空闲时段。本轮北京时间 2026-10-17 周六 12:56，按缓存命中输入 0.02 元/百万、未命中输入 1 元/百万、输出 4 元/百万计算：

`(7040×0.02 + 5246×1 + 706×4) / 1000000 = 0.0082108 元`，约 **0.00821 元**。相同缓存构成的高峰口径为 0.0164216 元；假设全部输入未命中的空闲口径为 0.01511 元。未查询实际账单、扣款或余额，以上不是已核实扣款。

相比 P5.3c.2a：输入 11886→12286，输出 644→706，汉字 514→540；均 completed、未提高 2048 上限。本轮缓存命中 7040 token，前轮为 0，费用变化不能全部归因于 TASK。版本引用改善，但跨来源日期全称错误仍存在，因此 **不能宣布 P5.3c.2 本轮严格通过**，更不能据一次固定案例推断稳定通过率、其他故障推理能力或真实服务健康。

本次没有证据表明必须修改生产逻辑才能解决；实际请求已经提供独立字段，错误发生在解释阶段。单次结果只证明这些新增通用规则在这一输出中仍未充分约束时间范围，不足以证明所有 Prompt 改进都无效。下一步先代码审查，重点核对时间范围和缺失样本语义；若另开阶段，再考虑仍在独立 TASK 范围内让各来源时间核对显式化。当前不增加预算、次数、输出上限或生产逻辑，不自动发起新阶段。

## 原始产物、哈希与保护检查

本轮原始临时目录 `C:\Users\zp1222\AppData\Local\Temp\incident-p53c2-wb9_o2bl`。12 个原始文件逐文件完整复制到新目录 `D:\Incident Agent\data\model-validation\p53c2b-wb9_o2bl`，另附离线原始产物和本轮验证/审查材料，共复制 54 个文件，逐文件 SHA-256 一致，见 [artifact-manifest.json](../data/model-validation/p53c2b-wb9_o2bl/artifact-manifest.json)。没有覆盖此前两次目录或报告。

| 归档文件 | SHA-256 |
| --- | --- |
| [实际请求](../data/model-validation/p53c2b-wb9_o2bl/request.json) | `4d8ae19a2b13478432e70bb334f76a207937a1b6bbbc1ab887fb998086304828` |
| [原始响应](../data/model-validation/p53c2b-wb9_o2bl/response.raw.json) | `2a119e96854b28fa49a043f61d2931aff17717d81d5419d060ca1254e57c7230` |
| [SDK 响应](../data/model-validation/p53c2b-wb9_o2bl/response.sdk.json) | `270bea9f5651e95df2cb9c2af0ad021d19b156da02e24f6b8d485cb1273c8196` |
| [综合逐项审查](../data/model-validation/p53c2b-wb9_o2bl/verification/manual-review.json) | `f1df7ae909a57e78b03a0dfa95c488d45d26eb049248c2c92560c6046121f6c9` |
| [独立证据审查](../data/model-validation/p53c2b-wb9_o2bl/verification/independent-evidence-review.json) | `ff2ba228243236c0e243caa3a60a6f368d29cb2bdfcebfc874bfc31c48ec8782` |
| [第二独立审查](../data/model-validation/p53c2b-wb9_o2bl/verification/manual_response_review_secondary.json) | `7eceb36b9568b157e463665d8f0101cbe2bf3c17258e114485ae5b44c028bb58` |
| [完整回归日志](../data/model-validation/p53c2b-wb9_o2bl/verification/full-regression.txt) | `274fdf7a9b6074d523eefa176b85a336632951b236f6ba3a05da7eef4925a54f` |

[离线请求审计](../data/model-validation/p53c2b-wb9_o2bl/verification/offline-request-audit.json)、[真实请求审计](../data/model-validation/p53c2b-wb9_o2bl/verification/live-request-audit.json)、[运行/费用检查](../data/model-validation/p53c2b-wb9_o2bl/verification/live-inspection.json)、[付费前安全门](../data/model-validation/p53c2b-wb9_o2bl/verification/pre-live-gate.json)、[完整性核查](../data/model-validation/p53c2b-wb9_o2bl/verification/final-integrity.json)均独立保留。原始响应、SDK 文本和 Checkpoint 正文完全相同；提取文本仅为方便审查，没有修订回答。

任务前保存的 558 个既有文件内容哈希，经离线、付费前及付费后核对，仅本次 scripts/tests 两文件发生预期变化；此前两次产物、已有用户修改和纳入快照的运行数据均未变。暂存前 index 哈希也与开始一致。密钥按实际配置值扫描新产物和交付文本，不命中；没有归档 Authorization 头或 .env 内容。没有故障注入、故障开关或待恢复服务。

原始产物继续受已有 `/data/` 忽略规则保护，不加入 Git；这些本地链接在其他检出环境不会自动存在。本次提交仅包含脚本、测试和新报告；完整 SHA、目标分支及远端一致性核对在提交后交付消息与本地交付记录中记录，避免把待提交状态写成已推送事实。

未验证范围：实际扣款、其他案例及多次采样稳定性、真实 Prometheus/Loki/业务服务、当前健康与恢复、自主工具选择、多轮 Responses、干净环境安装及推送后的远端 CI。完成本轮提交推送后停止，等待代码审查。

历史参照：[P5.3c.2a 验证报告](P5_3C_2A_REAL_MODEL_VALIDATION_2026-10-17_10-30-56.md)、[首次真实验证报告](P5_3C_2_REAL_MODEL_VALIDATION_2026-10-16_17-16-48.md)。
