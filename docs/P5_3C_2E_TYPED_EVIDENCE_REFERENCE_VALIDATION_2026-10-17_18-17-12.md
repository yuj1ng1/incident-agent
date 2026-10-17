# P5.3c.2e 证据来源建模与引用范围补全：离线验证报告

## 结论与修改边界

本阶段完成独立联调入口的 typed reference 和证据集合范围建模，保留 P5.3c.2d 的 Python 事实、模型 claims、独立语义审查三层分工。**没有调用真实 DeepSeek API，没有进行付费模型验证。** 本报告的通过结果属于离线结构、Mock 和本地 Runtime 验证，不代表模型已能正确解释新增引用或真实服务已经恢复。

基线和任务开始时的本地 HEAD、远端 main 均为 `baf502ff73fd9456a778f5f410cf0f1b5fab889e`。任务开始时暂存区为空。原有 Prompt、README、temp_test.py、历史文档整理及未跟踪文件均保留，没有将它们加入本次提交。

本次源代码与测试仅涉及：

- [独立联调入口](../scripts/preflight_real_model.py)：请求前快照、v2 索引、机械校验、审查引用绑定和产物保存。
- [纯辅助模块](../scripts/preflight_evidence_refs.py)：固定位置提取上下文、typed catalogue、集合描述及来源重建；仅使用标准库，无 I/O、网络或生产模块导入。
- [原有索引测试](../tests/test_preflight_evidence_index.py)与[原有入口测试](../tests/test_real_model_preflight.py)：适配新增索引版本，并验证快照对应实际 Checkpoint 事件。
- [新增 typed reference 测试](../tests/test_preflight_typed_references.py)：48 项离线用例。

复用现有 ToolObservation、Checkpoint.records、Runtime 输入和隔离入口，不新增生产 Manager/Store，不修改 Worker、RuntimeStore、数据库 schema、MetricAnalysis、生产 Prompt、审批或生产 API，不扩大工具、预算、超时或重试权限。未新增依赖。

## 实际来源及证据权限

核实实际结构后，工具观测来自 `ToolObservation` 和 `Checkpoint.records`；告警及能力来自 `Agent.build_incident_prompt()` 已序列化的首段 JSON；历史身份来自记录的 `identity`。不是假设新字段已经存在。

| 类型 | 可引用的实际来源 | 能够支持 | 不能据此证明 |
| --- | --- | --- | --- |
| observation | 真实持久化工具记录及 Runtime 对应 observation | 该工具记录的原始观测及其时间、缺失、历史使用约束 | 单凭 ID 证明自然语言陈述充分成立；历史观测证明当前健康 |
| alert_event | 实际 Runtime 输入首段 JSON 的 alert_event | 本次输入携带的告警元数据 | 根因、当前健康或工具已执行 |
| capability_context | 同一 JSON 的 capabilities，包含实际声明的 dependencies 等字段 | 配置声明的能力、依赖及支持范围 | 后端可达、依赖健康、日志或其他工具已执行 |
| identity_context | 每条记录已有的 identity；快照中实际存在的 identity | 历史身份关联或未验证身份标记的原始内容 | 当前部署版本认证或当前健康 |
| evidence_collection_scope | 请求前一致事务读取的完整 Checkpoint.records 与实际 Runtime 输入 | 所列快照的记录成员、该快照内某类结果缺失及摘要可见范围 | 现实全部证据、日志不存在、所有时间或所有服务的全称结论 |

AlertEvent 已序列化的默认值或 null 只描述 Runtime 输入，不能声称原始监控后端提供过该字段。字段省略与 null 保持原样；顶层 alert/capability/identity 缺失或 null 时不生成对应引用。历史 SHA 与 `unverified_instance` 不互相替代，后者不被解释为已验证版本。

元数据解析只接受当前 Agent/Worker 的固定位置和格式；普通用户 JSON、正文中的相似标记不作为元数据来源；重复 Runtime 封套会拒绝。本设计针对独立入口的 evidence_rebuild 路径，不宣称是通用 complete_chain 解析器。

## 引用结构与兼容方式

工具仍使用原始 observation_id，禁止工具 ID 占用 `context:` 前缀。索引 v2 保留原 `sources`，为工具来源添加 `source_type=observation` 及证据权限；另增加 `reference_context`，其每个上下文来源包含：

`reference_id / source_type / source_location / value / evidence_permissions`

上下文 ID 使用 `context:<类型>:<SHA-256>`，由来源内容、位置、权限和请求绑定共同派生；不同位置的同值身份元数据仍有不同来源身份。每个引用都可以回溯到实际输入或快照位置，不生成虚假 ToolObservation，不制造 function_call 或 function_call_output。

claims 仍必须包含 `kind / text / observation_ids`，可增加 `context_refs`：

```json
{
  "kind": "limitation",
  "text": "本项元数据的适用范围仍需结合原始来源审查。",
  "observation_ids": [],
  "context_refs": [{"type": "alert_event", "id": "context:alert_event:<由实际来源生成的哈希>"}]
}
```

上面只说明契约，示例 ID 不能通过校验。每条 claim 至少有一个工具或上下文引用；工具和上下文各自去重，类型与 ID 必须匹配目录。v1 旧索引及仅含非空 observation_ids 的旧 claims 继续支持，无需改写历史回答。需要元数据引用的新调用方读取 v2 catalogue 并增加 context_refs；Python fact view 同步使用 v2。旧的事实/审查哈希只适用于原事实集合，不迁移成新事实的通过凭据。

审查记录原字段保留；若 claim 有 context_refs，对应逐项审查行也必须提供完全相同的 context_refs。没有修改生产接口或持久化格式。

只调整独立入口的引用契约及通用证据权限说明，没有向模型注入先前九条 claims 的逐句答案、人工 rubric 或固定案例正确结论，也不要求模型重新生成事实表。

## 证据集合与哈希绑定

实际 Provider 在模型请求前，用同一 Store 事务读取 Session、checkpoint_id、records 和实际存在的 identity，另存 `request-source-snapshot.json`。绑定包含：

`session_id / checkpoint_id / records_sha256 / runtime_input_sha256`

集合来源列出记录的 observation_id、工具名、参数、status 和逐记录哈希，以及 Runtime 可见/遗漏 ID、已保存工具名称。范围固定为 `pre_request_checkpoint_records_only`。

当前案例有四条导入的历史指标记录，以及一条本地隔离版本探测记录；**五条持久化记录不等于本轮实际执行五次工具**。当前快照未保存 query_logs 或依赖健康结果，可以支持“该快照未收集相应结果”，不能支持“日志不存在”“依赖健康”或“工具能力缺失”。

Worker 摘要存在 cache_key 去重和长度上限，不能把摘要天然当完整集合。辅助结构能够明确表达 partial 覆盖并列出遗漏 ID；独立入口仍要求 records 与 Runtime observation ID 全等，遇到去重或截断造成不等时会拒绝继续。这是当前明确保留的 fail-closed 边界，未宣称支持不完整摘要继续付费调用。

机械校验检查引用存在性、合法类型、ID/类型冲突、伪造上下文、metadata/工具冒用、权限和目录哈希、集合与快照绑定。实际 Provider 在请求前和答案校验时均提供外部原始来源，重新派生 catalogue 和整个 observation index 并比较。

目录哈希是完整性校验，不是来源签名。仅传索引的兼容调用会明确标记 `trusted_index_internal_integrity_only`；提供实际来源并重建通过才标记 `passed_external_rebuild`。所有上下文和集合进入 Python 事实哈希；审查仍同时绑定 facts、claims 和逐项引用，防止跨告警、能力、身份或快照复用。

独立只读复查发现旧 v1 路径曾忽略显式外部来源、可能误报重建通过；现已修复为同源重建全等比较，缺少任一外部输入会拒绝，并新增篡改与降级回归。[复查记录](../data/model-validation/p53c2e-1xqztcjd/verification/code-review.json)保留该发现及复现结果。

**引用真实存在不等于语义支持充分。** 缺失元数据引用不能通过附加无关指标修复；如果模型删去元数据引用却仍在文字中断言元数据事实，机械校验无法仅凭自然语言认定充分支持，仍保持 pending。日志不存在、能力即执行、历史健康和无限范围全称结论的 Mock 错误回答，合法引用后也不会自动 passed；显式 Mock 失败审查会得到 failed。测试中的通过审查记录只是契约测试，不代表真实人工审查已完成。

## 实际执行的离线验证

使用现有 `D:\conda\envs\agentlearn\python.exe -B`，Python 3.12.14；SDK/依赖版本从实际离线入口读取：openai 3.14.0、httpx2 2.13.0、pydantic 2.13.5、python-dotenv 1.2.3、pytest 9.1.1。未安装依赖。

| 验证 | 实际结果 |
| --- | --- |
| 原有入口与事实/claims 定向测试 | 134 passed in 86.97s |
| 新增来源、语义边界和来源绑定测试首批 | 35 passed in 27.94s |
| 审查后补充回归 | 13 passed、35 deselected in 15.44s；合计新增 48 项均通过 |
| 完整 Python 回归 | **1828 passed、2 skipped、190 subtests passed in 416.61s（6 分 56 秒）** |
| pip check | No broken requirements found |
| 四类上下文与工具混合引用的 SDK Mock 链路 | 一次注入内存 SDK 返回 typed claims；事实/结构通过、语义与整体 pending，原回答与 Checkpoint 一致；socket 拒绝网络，真实请求 0 |
| 最终默认离线 CLI | exit 0，Runtime COMPLETED，真实模型请求 0，模型预算预留 1、本地工具执行 1 |
| 请求及产物审计 | 5 条 observation、9 条 context；离线请求体按入口序列化为 68,737 字节（未发送），小于原 98,304 上限；原始夹具与回答保留，无 rubric/凭据泄露，无伪造工具调用项；索引及事实可重建 |
| 源码冻结及既有文件保护 | 5 个代码/测试文件冻结后无变化；575 个原有受保护文件检查无变化 |
| 本次范围 diff 检查 | 通过；全工作区 diff --check 会报告用户原有 temp_test.py 第 7 行空白问题，未修改该无关文件 |

完整回归命令：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest -q -rs -p no:cacheprovider --tb=short
& 'D:\conda\envs\agentlearn\python.exe' -B -m pip check
& 'D:\conda\envs\agentlearn\python.exe' -B scripts/preflight_real_model.py
```

2 项跳过分别为 tests/test_dependencies.py 的 CI/隔离虚拟环境 clean-install 检查，以及 tests/test_multi_signal.py 未设置 ALERT_RULES_PATH 的可选跨仓库规则校验；两者均不作为已验证事项。没有前端改动，未运行前端测试。

初期辅助检查曾误用 conda base，因 Pydantic 缺少 field_validator 在收集时失败，未执行用例；随后使用上述项目环境验证。该失败不冒充测试通过。

另做了四类 context_refs 加工具 observation_id 的混合引用 Mock 贯通检查，使用显式注入的内存 SDK 并拒绝 socket 连接；[Mock 证明记录](../data/model-validation/p53c2e-1xqztcjd/verification/mock-typed-integration.json)保存结果。该路径因沿用 live=True 分支，本地 Provider 的 real_model_api_requests 字段为 1，只统计注入 SDK 方法入口，不代表真实 HTTP 或付费；真实网络请求为 0。

全部新增/相关定向测试禁网；注入 Mock SDK 或 MockTransport 的 live=True 分支属于离线 Mock，不表示真实 DeepSeek 请求。完整回归含本地 SQLite 和跨进程检查，不代表真实监控、业务服务或真实模型可用。没有执行带付费开关的 CLI，没有启用 Demo 故障开关、启动生产后台服务或执行修复。

最终离线占位回答保留原文，因其不是 claims JSON，结构和整体严格验收为 failed；事实完整性通过，推理为 pending。CLI exit 0 和 Runtime COMPLETED 仅证明该离线流程完成，不是模型推理通过。

## 产物、后续准备与交付

离线原始目录为 `C:\Users\zp1222\AppData\Local\Temp\incident-p53c2e-1xqztcjd`；本地归档为 `data/model-validation/p53c2e-1xqztcjd`，包含 [offline/](../data/model-validation/p53c2e-1xqztcjd/offline/summary.json) 和 [verification/](../data/model-validation/p53c2e-1xqztcjd/verification/artifact-audit.json)。请求快照、Runtime 输入、索引、权威事实、原始占位回答、SQLite、测试日志、冻结清单与保护检查保留；原件和副本逐文件 SHA-256 一致。归档受既有 data 忽略规则保护，不提交运行数据；这些本地链接在其他检出环境不会自动存在。

来源构建和校验已可用相同纯接口重放不同 Runtime 输入及快照；测试包含上下文存在/缺失/null、真假引用、身份变化、记录与摘要覆盖变化、审查跨事实复用等场景，为后续多案例测试提供了基础。尚未建立真实多案例模型效果基准、盲审数据集或跨模型比较结论，未验证模型能否正确使用新引用。

后续最多一次真实模型验证属于下一阶段；此前持续授权不被本次阶段性离线限制撤销，本阶段不会提前执行。自然语言支持充分性、因果解释、引用覆盖和建议可执行性仍需独立语义审查。

本报告正文完成后读取实际北京时间命名。按本次明确授权仅提交上述五个代码/测试文件和本报告，推送后另核对本地与远端 SHA。实际提交信息和远端结果以交付消息及本地 delivery.json 为准，不提前在报告中写成已完成。完成后停止，等待代码审查。
