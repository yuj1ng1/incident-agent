# P6.5-S2 离线调查质量评估与安全验收门禁验证报告

本阶段达到限定范围内的 S2 离线工程验收条件，**独立审查尚未完成**。新模块能报告引用关系、显式字段矛盾和预定义证据要求，并始终禁止自动采纳。它不理解任意自然语言，不证明真实模型的调查准确率，也没有生成正式人工批准。

## 1. 开始前只读核查与范围

- 项目：`D:\Incident Agent`；已阅读根目录 [AGENTS.md](../AGENTS.md)。起始 HEAD、本地 main、`git ls-remote --heads origin` 的真实远端 main 均为 `b4aa148aaf667e109ece9966f2ad9ddffd665837`；起始暂存区为空。
- 工作区已有 README、生产 prompt、temp_test 和文档整理等修改、删除及未跟踪文件。它们未被本阶段修改或暂存。保护清单覆盖 7,175 个既有文件，逐项核对 SHA-256、大小与 mtime_ns；核验结果为零变化。
- 阅读 [S1 报告](P6_5_S1_OFFLINE_REPAIR_2026-10-22_12-00-22.md)、[第三次真实实验报告](P6_5_THIRD_REAL_INVESTIGATION_2026-10-22_10-44-31.md)、本地既有独立语义审查、S1 模块及测试/fixtures；核查 P5 typed references/support constraints、P6.3 封存证据、P6.4 只读可信度投影与 [当前证据策略](../investigation/evidence_policy.py)。
- 使用 S1 `load_real_case()` 重新校验第三次运行 manifest、两轮封存 request/raw/SDK/integrity/completion、call/output/record/Checkpoint 绑定、原始回答及 immutable 只读 SQLite。封印 SHA：`ce0ce8dfe6af08d23410f392250081d011a45a552a61daa324d7ae6afa368f88`；原回答 SHA：`ad84a451320c641e02c083169ac9f54fb8b85cd449bc5c3e914c8eed6f6bd4f8`。未启动旧 Session。
- 事实边界：原回答确有三处 p63 ID 被标作 call，以及将封存材料误解为服务冻结的待审问题；它没有声称已读取被拒绝的日志、13 次独立故障或数据库根因已确认。这些新增反例没有倒记为原回答的既有错误。
- Python 实测为 `D:\conda\envs\agentlearn\python.exe` / 3.12.14；Pydantic 2.13.5、OpenAI 3.14.0、httpx2 2.13.0、pytest 9.1.1。没有新增依赖。

## 2. 新增文件与接口

| 文件 | 职责 |
| --- | --- |
| [scripts/p65_claim_quality.py](../scripts/p65_claim_quality.py) | S2 纯离线评价、独立保守门禁、JSON 字节容量分析，以及复用固定封存读取器的 CLI |
| [tests/fixtures/p65_s2/corpus.json](../tests/fixtures/p65_s2/corpus.json) | 23 个固定样本、合成 records/input_context、预设问题标签与规则能力范围 |
| [tests/test_p65_claim_quality.py](../tests/test_p65_claim_quality.py) | 固定集、门禁、绑定、防误报、字段类型、未知值与真实封存样本的 63 项测试 |
| 本报告 | 工程结果、能力限制、容量与离线验证记录 |

公开的实验接口为 `evaluate_claim_quality(answer, records, input_context, s1_result)`、`decide_acceptance(report)` 和 `measure_answer_capacity(answer)`，报告版本为 `p65_s2_quality_v1`。没有新增生产接口、生产领域对象或数据库 schema。

复用 S1 的 Answer/Claim 模型、严格 JSON 解析、引用归因、provenance、字段路径选择与证据要求；复用严格 JSON 哈希比较和当前预算常量。没有第二套 Runtime、审批系统或 ResultTrust 状态机，没有修改 S1 代码与既有样本。

## 3. S1、字段与语义的分层

1. **S1 声明检查**：工具 call_id/name/arguments、observation_id、历史 ID、实际来源、状态及时间等声明与 supplied records 对齐。S2 重新计算 S1，再与传入结果作严格 JSON 比较，防止把其他回答或被修改的结果当作当前检查结果。
2. **S2 字段与证据要求**：对显式 field_path/value 作精确比较；即使用途标为 observation，只要给出了显式字段声明也会检查。字段的 null 与缺失、false 与 0、数字与字符串分别处理。证据要求复用 S1 对历史范围、有效零值、日志正文、根因及当前健康等限制。
3. **独立语义审查**：不从 text 提取事实、不按关键词理解因果、不相信模型自述的 kind/use/confidence。字段匹配不能证明整句话真实，也不能证明未夹带额外断言。所有 claim 均保留 `text_semantically_verified=false` 和人工复核要求。

每条合法结构的 claim 报告包含原始 text、claim_id、kind、use、citation IDs、匹配的实际 call_ids、S1 解析的实际来源、字段检查的声明值/实际值、证据支持范围、问题代码与人工复核要求。无效引用下的来源仅作为候选关联展示，`relationship_verified=false`，不会获得 field_verified。input_context 明确标为输入声明，不能冒充本轮工具读取或当前部署事实。

| 离线评价状态 | 精确含义 |
| --- | --- |
| field_verified | 仅该来源上的具体 JSON 字段关系严格匹配 |
| citation_invalid | 引用身份、参数、provenance 或记录唯一性检查存在问题 |
| insufficient_evidence | 声明的字段不存在，或既有证据未达到所声明用途的要求 |
| contradicted_by_field | 可解析的实际字段与显式声明值不同 |
| semantic_review_required | 自然语言含义未由确定性代码核实，始终保留 |

这些状态可以同时出现。例如字段正确的累计值仍不能支持故障次数；field_verified 与 semantic_review_required 并不冲突。结构整体无效时报告 S1 失败，claims 不作伪解析，门禁拒绝自动采纳。

重算结果相同不是来源签名；S1 未使用的背景变化也可能不改变 S1 检查结果。S2 另记录完整 records/input_context 的哈希以标识当前输入，但纯函数不能认证任意调用方提供的材料。CLI 先使用固定封存读取器；本地封印也不等于对外部系统观测的权威认证。

## 4. 安全验收门禁

`decide_acceptance()` 始终返回：

```json
{
  "automatic_acceptance_allowed": false,
  "manual_semantic_review_required": true,
  "decision_origin": "offline_advisory_only",
  "formal_approval": "not_performed"
}
```

同时列出 S1 失败、显式字段矛盾、证据不足、检查结果未绑定等 blocking_reasons，以及始终存在的独立语义审查未完成原因。完全合法结构、正确字段、真实 Provider 来源或自授 accepted 标签均没有自动采纳路径。空报告也不能批准。

该函数不修改 `trusted`、`semantic_status` 或 `human_approval`，不持久化任何生产状态。既有 [ResultTrust](../investigation/result_trust.py) 的 trusted=false、pending_review/rejected 只读投影保持不变。新的门禁供离线消费方调用，没有接入生产；直接忽略门禁和既有 ResultTrust、读取原始回答的外部消费者不在本阶段强制范围内。

## 5. 固定对抗集结果

所有 S2 records 为显式合成离线材料，p63 字符串只复用了历史身份示例。每个样本及其 text 均标明开发 Agent 手工构造、非 Provider 回答、非正式人工批准。预设标签由开发 Agent 编写，**不是独立真人标注或正式批准**；标签不作为评价器输入，也不作为语义自动检测结果。

| 类别/样本 | 预设问题 | 实际确定性结果 |
| --- | --- | --- |
| A_p63_as_call | 真实存在的 p63 身份冒充 call_id | historical_id_used_as_call_id；并报告未知引用及字段来源不足 |
| A_forged_observation | 伪造 observation_id | observation_id_mismatch |
| A_wrong_arguments | 工具参数不符 | tool_arguments_mismatch |
| A_other_metric_source | 另一指标的历史来源挂到当前指标 | provenance_mismatch |
| B_outer_status | 外层 no_data 声明为 ok | source_field_value_mismatch |
| B_current_health | 内层历史 ok 提升为当前健康 | unverified_fact_in_historical_scope |
| B_no_data_zero | no_data 声明有效零值 | no_valid_zero |
| B_fault_count | 累计13.0声明13次独立故障 | independent_fault_count_not_established |
| B_commit_head | null 替换为仓库 HEAD | source_field_value_mismatch |
| C_seal_means_freeze | 封存证据变成服务冻结 | 无自动语义检测；保留预设语义风险标签，必须人工复核 |
| C_confirmed_cause_disguised | use=observation 却写数据库根因确认 | 同上 |
| C_history_as_current | use=observation 却写当前健康 | 同上 |
| C_denied_logs_as_read | use=observation 却写已读日志 | 同上 |
| C_late_logs_as_now | 所有历史日志事件写成采集瞬间 | 同上 |
| C_temporal_causality | 时间接近解释为因果 | 同上 |
| C_explicit_root_cause | 显式 root_cause/fact，无支持证据 | unverified_fact_in_historical_scope |
| C_explicit_logs | 显式 log_content/fact，范围拒绝 | logs_not_read |
| C_explicit_event_time | 显式事件字段写成采集时间，use仍为observation | S1通过，S2识别 source_field_value_mismatch |
| D_historical_value | 合法历史13.0，未扩大为故障次数 | 字段匹配，无问题代码，仍不自动采纳 |
| D_hypothesis | 清楚标为未确认假设 | 无问题代码，仍人工复核 |
| D_insufficient | 明确证据不足 | 无问题代码，仍人工复核 |
| D_unknown_commit | input_context中null保持未知 | 仅输入字段匹配，仍不验证部署 |
| D_complete_legal | 合法引用/字段、假设及证据不足共存 | S1通过、字段匹配，仍不自动trusted |

当前固定集：A 4项、B 5项、C 9项、D 5项，共23项。12个确定性反例均命中预期代码，规则范围内预设问题无漏检；5个合法样本无问题代码；在当前预设代码标签范围内无误报。6个开放语义反例没有被自动识别，均被强制人工复核门禁阻止自动采纳。**不能把12/12扩展为真实模型语义准确率，也不能把门禁拒绝采纳算作识别了这6个语义错误。** 未计算通用 precision/recall。

详细逐样本来源、预期/实际问题、误报代码、漏检代码、未检测语义标签及完整报告保存在 [corpus-results.json](../data/p65-s2-offline-20261022-01/corpus-results.json)。标签不影响代码执行；更换服务、指标、call_id、数值和中英文文字的测试也验证了没有对13.0或旧回答增加关键词特判。

S1 合法人工样本在真实已核验 records 上仍为零问题代码，5条字段声明匹配，仍禁止自动采纳。固定原始 Provider 回答保持原字节，S2报告 S1协议缺失及历史ID冒充call问题，不重写原文。另有有效历史0.0和确实含正文的历史日志测试，均未被错误拒绝。

## 6. v4 输出容量

测量为 UTF-8 字节数。S1原始大小取文件原字节；S2取固定answer对象以ensure_ascii=False、indent=2序列化的完整文本，并非整个corpus文件。压紧表示仅删除 JSON 非必要空白，保留同一内容；identity-only 示意只保留 citation身份与工具参数，claims不变，**不符合当前v4要求**，已有测试证实缺少provenance字段会被拒绝。

| 样本 | 原始完整JSON字节 | 压紧完整JSON字节 | citation/claim数量 | 完整引用数组字节 | identity-only引用数组字节 | identity-only完整JSON字节 |
| --- | ---: | ---: | --- | ---: | ---: | ---: |
| S1合法人工样本 | 5856 | 4241 | 6 / 8 | 2239 | 899 | 2901 |
| S2完整合法小样本 | 1593 | 1170 | 1 / 3 | 465 | 153 | 858 |

S1样本引用数组可示意减少1340字节，但需要另行设计版本、重新冻结及授权；本阶段仅测量，没有改协议。S2小样本仅验证一个历史来源下的合法结构，不代表完整调查覆盖。

项目存在 `tests/test_worker_metric_analysis_growth.py` 的 o200k_base 参考编码测试，该文件已声明它不是 DeepSeek 用量或计费token。本次搜索未发现适用于当前deepseek-flash的已验证同模型tokenizer，也没有当前两个JSON样本的Provider usage。因此 `exact_deepseek_tokens=null`，没有下载tokenizer、没有拿字符/字节或其他模型编码冒充精确token数。

原第三次封存Provider的usage为：首轮output_tokens=266、第二轮=1012。它们对应旧v3的工具选择/Markdown回答，不对应v4 JSON，不能按比例推算新样本的精确token数。

**2048存在实质容量风险，尤其当引用、参数、来源字段及claims继续增多时；目前不能精确证明上述样本是否必然超限或尚余多少token。** 候选优化是复用citation、减少重复文字，或另行设计让离线审查器按call_id从核验records展开provenance的版本。后者会改变现有协议，当前未实施。正式预算、已保存请求和旧授权均未修改，未重新冻结实验。

测量详情与原usage位于 [capacity.json](../data/p65-s2-offline-20261022-01/capacity.json)。

## 7. 实际测试、原始日志与退出码

测试由新建独立进程中的 [offline-runner.py](../data/p65-s2-offline-20261022-01/offline-runner.py) 执行。Python audit hook拒绝socket.connect/getaddrinfo/sendto/sendmsg及子进程/系统命令；另阻断connect_ex、真实ActionExecutor.execute与RecoveryWorker.start。只按标准库实际函数code身份放行Windows内部socketpair自连接，不放行业务loopback HTTP。回归实测内部socketpair连接1次，业务或外部网络连接0次，禁止边界尝试列表为空。测试自身的Mock边界不会变成真实授权。

| 批次 | 结果 | 退出码 | 原始记录 |
| --- | --- | ---: | --- |
| S2首轮 | 54 passed，0 failed，0 skipped | 0 | [日志](../data/p65-s2-offline-20261022-01/s2-initial-tests.log)、[退出记录](../data/p65-s2-offline-20261022-01/s2-initial-exit.json) |
| S2补充字段、绑定与门禁测试 | 62 passed，0 failed，0 skipped | 0 | [日志](../data/p65-s2-offline-20261022-01/s2-expanded-tests.log)、[退出记录](../data/p65-s2-offline-20261022-01/s2-expanded-exit.json) |
| S2最终版本 | 63 passed，0 failed，0 skipped，4.78s | 0 | [日志](../data/p65-s2-offline-20261022-01/s2-tests.log)、[JUnit](../data/p65-s2-offline-20261022-01/s2-tests.xml)、[退出记录](../data/p65-s2-offline-20261022-01/s2-exit.json) |
| 既有必要回归 | 540 passed，0 failed，0 skipped，终端315.44s | 0 | [日志](../data/p65-s2-offline-20261022-01/regression-tests.log)、[JUnit](../data/p65-s2-offline-20261022-01/regression-tests.xml)、[退出记录](../data/p65-s2-offline-20261022-01/regression-exit.json) |
| 固定集、封存原件、容量分析 | 全部报告生成，禁止边界尝试为空 | 0 | [输出](../data/p65-s2-offline-20261022-01/analysis-final.log)、[退出记录](../data/p65-s2-offline-20261022-01/analysis-exit.json) |

前三批大量重叠，不相加。最终S2为63个新增用例；540项为既有回归节点，不包含S2文件。多项测试共享fixture和调查链，不能将测试节点数量当作独立业务场景数量。本轮没有原始测试失败，也没有失败后定向复验；未隐藏失败。未运行全仓回归、子进程恢复测试、真实HTTP/监控联调或真实模型验证。未选项目不算skip或通过。

540项明细：S1 62；P6.5 readonly 45、dynamic budget 38、protocol compatibility 58、third preparation 28；P6.4 historical acceptance 8、offline investigation 9；ResultTrust 5；P6.3 model validation 38；support constraints 60、typed references 48、acceptance boundary 39、evidence index 89；Checkpoint/ContextPolicy 10及指定Runtime字段/保留/无数据检查3。各文件和node清单在退出记录，分类统计在 [test-summary.json](../data/p65-s2-offline-20261022-01/test-summary.json)。

真实封存文件本机齐全，因此实际无缺件skip。跨机器缺操作员data时，相关测试会明确skip，不能以合成样本冒充真实Provider；CLI固定读取也会失败。logs、JSON/XML及保护清单均保留在本地忽略的data目录，不随Git发布；可复跑的模块、静态样本、测试与本报告提交。

## 8. 原件保护、生产影响与验收结论

- [保护基线](../data/p65-s2-offline-20261022-01/protection-baseline.json)和[最终核验](../data/p65-s2-offline-20261022-01/protection-final.json)覆盖既有tracked源码/配置/测试、data、SQLite、原实验授权与消费ledger、logs及用户既有文件；7,175个文件哈希、大小、mtime_ns保持一致。没有改动或复用旧实验剩余额度。
- 生产Worker、Agent、ToolManager、RuntimeStore、租约、fencing、人工审批、Remediation与ResultTrust文件没有本轮diff。没有第二套状态机、数据迁移、新持久化对象、新依赖或生产预算变化。回归的临时Mock Store/授权数据只在测试隔离目录，不是正式消费ledger。
- 真实DeepSeek请求=0；真实业务HTTP=0；真实Prometheus/Loki查询=0；新付费授权=0；新正式消费ledger=0；旧Session恢复=0；实际Remediation=0；后台恢复=0；未进入P6.6。没有启动故障开关或待恢复业务资源。Git远端核对与推送是用户授权的版本交付操作，不属于业务/模型访问。
- 静态检查：新Python AST、88列、尾空白、严格JSON、完整本次diff、暂存白名单及明显凭据前缀检查通过；文档命名和相对链接检查通过。保留用户既有修改，不做自动格式化、reset、clean、stash或历史改写。

**验收判断：本阶段离线工程条件满足，安全门禁维持默认拒绝自动采纳；开放自然语言的语义正确性、来源的外部权威认证、完整真实v4输出的token容量与真实Provider遵循率仍未完成验证。** 6个预设语义反例仍需独立复核；合法样本同样没有正式语义批准。S2未接入生产，不能声称已强制治理所有外部消费者。

按本轮明确授权，仅提交并推送本模块、测试、固定样本及报告；实际完整提交SHA与真实远端核对结果在最终交付回复中给出。交付后停止，等待独立审查，不继续真实实验或扩大范围。
