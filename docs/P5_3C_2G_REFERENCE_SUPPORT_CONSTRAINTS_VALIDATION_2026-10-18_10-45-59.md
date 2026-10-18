# P5.3c.2g 引用失败归因与离线约束验证报告

## 结论与边界

本轮独立核对了 c.2f 的原答案、来源目录、请求前快照、权威事实和审查声明。确认 claim_index=3 是**具体身份实例错绑**，claim_index=5 是**局部来源不足以支持全快照缺口**。原件全部保留，没有修补原答案，也没有第二次真实模型调用。

最小实现采用独立支持声明（sidecar），不改变 v1/v2 claims：验证声明选中的精确来源、字段路径/值和集合范围；机械通过仍输出 `semantic_review=required`。新增查询前置条件分类和独立外部来源验收投影。没有接入生产调查、Incident 状态或模型请求。

本轮实际结果：现有定向 **182 passed**，新增定向 **99 passed**，完整回归 **1927 passed、2 skipped、190 subtests passed**，pip check **No broken requirements found.**。这些结果验证离线结构约束与既有回归，不能证明 DeepSeek 的引用或自然语言推理已经改善。

## 基线与原件核验

开始前读取本地 [AGENTS.md](../AGENTS.md)，检查分支、HEAD、暂存区、工作区、远程 URL，并实际执行 `git ls-remote origin refs/heads/main`。本地 `main`、HEAD 和真实远端 main 均为 `48b937f11e885b2c57ee72764c3a65df022750b1`，远端为 `yuj1ng1/incident-agent`，暂存区为空。

原有差异包括 README.md、prompts/agent_prompt.py、temp_test.py 的修改，6 项文档删除和12项未跟踪说明/归档。任务开始冻结899个原有路径的存在状态与字节 SHA-256，覆盖源码、既有说明、用户修改、密钥文件与全部 data 归档/运行数据。最终核对结果：**899个路径的存在状态与字节SHA-256均未变化，代码冻结一致**。没有 reset、clean、stash、格式化、恢复、生产入口启动或用户修改入提交。

[c.2f 中文报告](P5_3C_2F_TYPED_REFERENCES_REAL_MODEL_VALIDATION_2026-10-18_10-03-38.md)和归档均存在。只读重算 manifest 全74项文件的大小、SHA-256，全部一致；manifest 自身 SHA-256 为 `9c958c8a388837a161a828c0ba0acc3f1b2e6c4465ab9291db55bb48e8007d86`，与旁置校验一致。c.2f 报告字节 SHA-256 为 `bf255a257d35957db3a060d923c1287e75bb1a418eb21b5bec9f31b03c64776b`。

| 重点原件 | 本轮核验的字节 SHA-256 |
| --- | --- |
| [answer-original.txt](../data/model-validation/p53c2f-3uskvil0/live/answer-original.txt) | `9a900f99336de4b8102a1712a52bdba056a3a89bba90942f856553e045ff15df` |
| [evidence-index.json](../data/model-validation/p53c2f-3uskvil0/live/evidence-index.json) | `dbc70bfca9e4d34b50613c25818e5dc7c19f302e6f9eda99f43f0f52409a0a47` |
| [request-source-snapshot.json](../data/model-validation/p53c2f-3uskvil0/live/request-source-snapshot.json) | `bd27a2a6ec2b7a1867e0de3a6943d4279bb2e9c0d6f2bef3cb969b175dfc3b59` |
| [authoritative-facts.json](../data/model-validation/p53c2f-3uskvil0/live/authoritative-facts.json) | `4abc14b58f19299c5358e9c2c445cd056852a3a8541e24017c309d1a63c46800` |
| [independent-claim-review.json](../data/model-validation/p53c2f-3uskvil0/verification/independent-claim-review.json) | `c0a206400827ae4cc4094e241874f02ac300444e41b698d18056b61e14593379` |
| [reasoning-review-attestation.json](../data/model-validation/p53c2f-3uskvil0/verification/reasoning-review-attestation.json) | `f1ee9ed935398212ca495a7dafbdf7262bcfd6ce4907b90f62912b4df62d35e6` |

本轮通过归档 JSON 离线重跑 `validate_model_answer()`：结构 passed、来源 passed_external_rebuild、语义 pending、strict pending；重建的 Python facts 与原 authoritative-facts JSON 相同。再应用原绑定审查声明，整体仍为 failed。未重跑真实模型，也未打开原业务数据库来重做历史 SQLite 联调。本地哈希相同仅证明与冻结文件一致，不能证明任意输入真实可信。

## 两项失败的独立归因

**A / claim 3。** 原引用 `source_snapshot/records/4/identity` 的值只有 `unverified_instance`。records[0..3].identity 含历史 commit_sha，但未被该 claim 引用。版本 observation 可支持返回 no_data、无法确认当前版本；它不能替代另一条历史 identity 的字段支持。失败是来源实例错绑，不是 SHA 在全部背景中不存在，不是身份类型、ID、hash 错误。

**B / claim 5。** 原 claim 只引用 case-a-t1 指标和 capability_context。它们可以给出调查动机和配置的依赖关系，却不能证明全快照未保存日志、依赖探测、健康检查或数据库指标。原 scope 实际存在且标为 all_snapshot_records、Runtime observation coverage=complete，但该 claim 没有引用。即使引用 scope，也只能对限定集合与明确筛选条件作保存结果缺口陈述；不能证明现实世界无日志、无故障或本轮从未执行某工具。

这两项事实与原独立审查7 passed / 2 failed一致。这里重新核对了实际引用和值，未把前次报告本身当作事实依据。

## 现有校验保证与语义边界

[preflight_evidence_refs.py](../scripts/preflight_evidence_refs.py) 已按记录位置分别建立 identity 来源，位置参与 reference_id；相同字段值位于不同位置仍有不同 ID。目录绑定 Session、Checkpoint、records hash 和 Runtime 输入 hash，校验类型、来源位置、权限常量、去重、scope成员和摘要可见/省略关系；提供外部三项输入时，重新构造目录对比。

[preflight_real_model.py](../scripts/preflight_real_model.py) 的 `validate_model_answer()` 校验JSON结构、kind、条数/长度、引用存在、类型匹配与去重。它没有自然语言到字段的事实提取器。它允许两项原引用通过结构检查，符合当前契约；结构通过从未承诺整条中文获得支持。

必须区分：

1. 来源在结构上确实含该字段。
2. 声明者选择该字段支持这条 claim。
3. 字段在语义上充分支持整条 claim。

新辅助仅核对第一项和第二项的显式绑定。第三项仍需独立审查；复合句的未声明事实、否定范围、因果、时效、历史/当前和遗漏均不能由存在性检查认证。`claim.kind=limitation` 或 `hypothesis` 也可能内嵌确定事实；不能因 kind 放宽约束。正则、关键词、服务名、SHA 或本次原文特例均未用作语义规则。

## 最小通用设计与实际实现

只新增 [preflight_support_constraints.py](../scripts/preflight_support_constraints.py)，标准库加现有 refs helper，无I/O、无第三方依赖、无新Manager/Store/Agent。

### 来源实例与字段声明

`check_field_support()` 接收原 claim、已有 context catalog、外部 records/Runtime输入/snapshot 和独立声明。声明字段为 `claim_sha256`、`reference_id`、`field_path`（JSON键/数组索引列表）、`expected_value`、`use`。

检查外部重建、原claim摘要、该来源是否被原claim实际引用，再从该来源实例的value沿路径取值并比较JSON规范化值。禁止在其他来源中搜索替补；false、0、字符串和null保持类型/存在语义。缺失identity、null identity或空identity不会生成虚假来源；字段本身为null与字段缺失不同。

用途只接受保存字段或历史身份关联；声明 `current_deployment` 被拒绝。成功结果仍为 `current_deployment_verified=false`，并绑定 claim/catalog/declaration 摘要。未验证实例标记可以作为原样元数据描述，但不能认证当前部署。未来真实当前部署认证需要独立可信运行证据、版本/实例与时效设计，本轮未实现。

### 集合缺口声明

`check_collection_absence()` 要求原claim实际引用同一catalog下的 evidence_collection_scope，并显式匹配Session/Checkpoint。selector是精确工具名加参数子集，逐参数保留JSON类型；不从能力配置、工具别名或指标家族猜测执行。

| 声明范围 | 能检查的内容 | 拒绝的扩展 |
| --- | --- | --- |
| snapshot_saved_results | 完整请求前checkpoint records内无匹配已保存结果 | 现实不存在、本轮未执行 |
| runtime_visible_results | Runtime observation可见子集内无匹配记录 | 完整快照不存在 |
| runtime_complete_results | 仅在coverage=complete时检查整个Runtime observation集合 | partial摘要冒充完整 |
| preflight_executions / real_world_absence | 机械拒绝 | scope本身无此权威 |

saved no_data/error也算保存结果，不能被当作未保存。能力注册不构造执行记录；本轮是否执行须另有本轮执行账本。服务/metric等筛选应明确写入参数；存在 query_metrics 不能证明已采集数据库指标。selector拼写错误或过窄仍可能得到“无匹配记录”，所以工具选择是否合理、是否覆盖“日志”或“数据库指标”这类中文概念仍须语义审查。

Runtime complete仅表示observation成员覆盖，不表示原始结果所有字段都完整呈现。原始record集合相对导出的完整性、摘要部分省略、Runtime消息其他内容截断是不同问题；不对任意消息内容完整性作保证。“完整快照”以调用方从一致事务获得完整导出为前提；重建不能自行证明导出未删record或源系统没有其他Checkpoint，调用方仍须验证来源与导出完整性。

### 告警时间与执行前置条件

`assess_query_preconditions()` 仅作离线分类。c.2f告警 starts_at、observed_at、window均null，不能构造告警窗口查询。只有历史evaluation_time、只有starts_at，或调用方自造时间范围，均不补足窗口。

本次原型只识别输入告警中原样存在的明确start/end范围，要求带时区且start<end。相对窗口、默认偏移、从历史样本时间推导窗口均不实现；这不改变生产告警结构。来源可信度须先由调用方验证，字典本身不获得真实性认证。

缺范围或参数时返回direction_only；有范围且调用方声明参数完整时仅标candidate_operation。结果显式保留 `motivation=not_assessed`，不能称已核验建议动机。参数完整性与execution_authorized均是调用方声明，不替代真实工具schema或审批账本检查。缺授权列入missing_preconditions；即使三者齐全，`execution_allowed`仍始终false。未启动真实工具补窗口，未扩大任何权限。

### 验收接口边界

先以测试复现旧行为：`compose_preflight_report()` 的overall使用Runtime、facts、structure和reasoning四层；`reference_source_check`只展示，不作独立强制条件。因此v1/v2在内部索引完整性检查加完整审查声明时，旧overall可passed；来源标记缺失也可能如此。

这保留了独立预检的兼容行为，但下游只读overall可能误当外部来源重建成功。静态检索未发现生产Agent/Runtime读取这些验收字段，本轮没有证据证明已存在生产状态绕过。

新增 `external_evidence_acceptance(report)` 作为独立报告投影：精确scope为independent_preflight_only，所有原验收层均passed且来源标记精确等于passed_external_rebuild，才passed；来源仅内部完整性、缺失或相似/未知标记为pending；任一已有失败或scope不符为failed。原report不变，`production_state_authority=false`。

这一字段仍依赖既有流程产出的报告，不能认证调用方伪造的passed字典、任意输入来源真实性或审查者正确性。新字段也不代表本轮所有support声明已被自动覆盖或验收；没有默认挂接到旧入口。

## 离线反例与四类结果

新增 [test_preflight_support_constraints.py](../tests/test_preflight_support_constraints.py) 60例，以及 [test_preflight_acceptance_boundary.py](../tests/test_preflight_acceptance_boundary.py) 39例，均用内存合成来源并启用禁网fixture，无run_case、模型客户端请求、Runtime启动或数据库打开。

| 必要场景 | 本轮观察 |
| --- | --- |
| 正确历史commit字段及历史关联 | 机械通过，语义仍required |
| 合法unverified实例；commit只在其他来源；全部来源无commit | 字段缺失失败，不借用背景 |
| identity缺失/null/空 | 不产生虚假identity ref，旧引用失败 |
| 相同字段值位于不同来源位置 | 原样引用正确位置可通过，借另一未引用位置失败 |
| 历史commit认证当前部署 | 用途失败 |
| 完整快照缺少匹配保存结果 | 限定scope机械通过 |
| 单指标或capability证明整个集合缺口 | 来源缺失/类型不符失败 |
| 能力有注册但工具无记录 | 仅证明配置与限定保存结果缺口，不能证明执行 |
| Runtime部分省略且保存日志被省略 | visible范围可通过；snapshot缺口失败；complete范围失败 |
| 完整scope证明现实没有日志 | 显式world用途失败；窄声明配错误中文仍需语义审查 |
| 不同Session/Checkpoint混用 | 绑定/外部重建失败 |
| 窗口完全缺失、只有历史evaluation_time、只有starts_at | 缺窗口，不造时间 |
| 有窗口无授权；有方向但参数不足 | 分别保留授权缺口、参数缺口，均不执行 |
| 范围/参数/授权声明齐全 | 仅候选标签，执行仍false |
| 全部四种合法claim.kind、字段有值但中文越界 | 机械可以通过，不能替代语义审查 |
| present null、false/0、嵌套路径、声明摘要、selector变化 | 保留类型与声明绑定 |

四类结果明确分开：

| 类别 | 例子与判定 |
| --- | --- |
| 机械检查失败 | 未引用的来源、缺commit字段、跨快照、scope越权、Runtime partial冒充complete |
| 机械通过、语义待审 | 正确历史字段，或窄集合声明搭配“现实没有任何日志”中文；输出required |
| 独立审查明确失败 | 版本no_data却称“已证明当前部署成功”，手写审查理由指出无身份/部署成功依据，report与新验收均failed |
| 合法且有充分支持 | 唯一历史版本结果no_data，仅称“该历史版本工具未返回部署版本”；限定语义手写审查passed，外部重建及各层通过 |

正反语义oracle由测试作者显式给出，验证绑定、待审和审查结果分流；不是自动自然语言评分器，不是新模型或人类盲评。对c.2f的实际错误另有本轮独立原件复核，不能以合成oracle冒充DeepSeek改善。

原答案离线重放中新声明分别得到：claim3 `source_field_missing`，claim5 `declared_source_not_cited`；原claim6缺告警窗口、完整参数和执行授权。本轮没有把声明写回原答案，也没有把这组人工声明冒称模型自动生成。

## 兼容性、测试实录与保护

v1/v2原claims、允许字段、目录schema及生产数据格式均未改变。支持声明是可选独立实验接口，缺声明意味着未覆盖，不能自动视为passed。v1本身没有typed context，不能凭空获得身份/集合支持声明；保留原观察引用及外部重建能力。若未来要求模型必填sidecar，应另行提出可选版本/新契约设计、评估输入输出预算与迁移，再获授权实施；本轮没有破坏性修改。

实际使用 `D:\conda\envs\agentlearn\python.exe -B`，命令如下：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest -q -rs -p no:cacheprovider --tb=short tests/test_real_model_preflight.py tests/test_preflight_evidence_index.py tests/test_preflight_typed_references.py
& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest -q -rs -p no:cacheprovider --tb=short tests/test_preflight_support_constraints.py tests/test_preflight_acceptance_boundary.py
& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest -q -rs -p no:cacheprovider --tb=short
& 'D:\conda\envs\agentlearn\python.exe' -B -m pip check
```

| 本轮实际执行 | 结果 |
| --- | --- |
| 原有三份定向 | 182 passed in 142.03s，exit 0 |
| 新增两份定向 | 99 passed in 3.67s，exit 0 |
| 完整Python回归 | 1927 passed, 2 skipped, 190 subtests passed in 491.61s (0:08:11)，exit 0 |
| pip check | No broken requirements found. |
| 原件哈希与原答案函数重放 | 74项匹配；原facts一致；原严格验收仍failed |
| 原文件保护 | 899个路径的存在状态与字节SHA-256均未变化，代码冻结一致 |
| 本轮新辅助代码范围 | 1辅助模块、2测试文件、本文；未增加依赖 |

两项跳过分别为 tests/test_dependencies.py:28 的CI/隔离虚拟环境clean-install检查，以及 tests/test_multi_signal.py:111 未设置ALERT_RULES_PATH的可选跨仓库告警规则校验；本轮未验证这两项。

开发中将“动机已验证”的标签改为中性标签时，曾有一轮11项旧测试期望未同步而失败；同步后新增60项约束测试及上述99项定向均通过。未为此修改生产逻辑或弱化事实/安全断言。

完整回归包含临时SQLite及跨进程等既有测试；这不是业务数据库、真实模型、Prometheus/Loki/业务服务联调。没有前端修改，未运行前端验证。测试针对当前工作区执行，包含保留的既有Prompt等差异；未在纯Git检出环境另外重跑，不能把本次提交树称为逐字节相同的工作区。新辅助不读取生产Prompt。历史c.2f中的1828项回归与一次真实SDK请求仅为背景，不算本轮执行结果。

本轮审计目录为 `C:\Users\zp1222\AppData\Local\Temp\incident-p53c2g-_7dip6ne`；对应忽略归档为 [本轮审计归档](../data/offline-validation/p53c2g-_7dip6ne)。原始测试输出、c.2f哈希核对、离线重放、代码冻结和保护记录在此保留，未提交运行数据。归档内存在用户文件哈希，仅供本地核查；跨检出环境不保证这些链接可用。

## 风险、未验证事项与下一阶段

高风险边界仍然是语义覆盖不足：模型/声明者可选正确字段而遗漏整句中的另一断言，可用窄selector证明无关缺口；哈希及外部重建不能证明输入真实性，也不能认证审查意见。集合absence与工具执行、现实证据存在性不能互换。前置条件函数只分类输入声明，不完成真实参数或权限校验。

本轮未验证第二次DeepSeek、跨案例模型稳定性、真实服务/监控/日志/数据库、告警时间线补齐、生产调度、实际工具执行、修复或业务恢复，也未解决现有全部中文语义自动判定。没有更改模型调用预算、超时、重试或生产Prompt。

下一阶段最小建议：先独立审查本轮helper、合成反例和真实失败重放，重点确认可选sidecar是否应作为独立预检评分输入。若继续，先增加多种离线快照与人工语义样本，另行设计sidecar必需性和报告消费规则；不要直接接入生产状态或自动工具执行。真实模型复验需另获授权，本轮不自动继续。

仅选择本阶段4个新文件本地提交并普通推送main，保护既有未提交修改；实际提交首行、完整SHA、远端SHA复核由最终交付消息及本地delivery记录给出。本文不在推送前宣称已推送。交付后停止，等待独立代码审查。
