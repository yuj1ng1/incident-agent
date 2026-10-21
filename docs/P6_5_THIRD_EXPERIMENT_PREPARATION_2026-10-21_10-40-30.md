# P6.5 第三次真实多轮实验离线准备：5 次工具预算，待独立审查

本轮只完成离线代码调整、快照冻结、历史响应回放与测试。**没有发起第三次付费实验；真实 DeepSeek、业务 HTTP、Prometheus/Loki、故障注入、Remediation、后台恢复均为 0。** 新实验仍为 pending，未创建新的真实授权或消费 ledger，没有恢复旧 EXHAUSTED Session，没有进入 P6.6。

## 1. 基线、调用链与实际修改

开始时重新阅读根目录 [AGENTS.md](../AGENTS.md)。本地 HEAD、origin/main、真实远端 main 均为 `0ff72e66f5a2d9b0dfe09f8c630e1ed759e26891`，暂存区为空；已核对第二次真实实验报告、动态预算修复报告及测试、旧授权/消费记录/失败 Session 和当前源码。没有直接接受历史结论。

已确认的链路为 Worker 事务预留模型/工具额度与保存 Checkpoint → Agent.request_durable_response → ModelClient.call_once → BoundedResponses.create → SDK。自动版本探针先于模型请求，属于工具额度。第二次实验原始响应请求了 4 个模型工具，原总预算 4 只能容纳探针和其中 3 个工具，日志调用留在 pending，未发生第二次真实请求。

本次源码和测试共 6 个文件：

- [p65_model_boundary.py](../scripts/p65_model_boundary.py)：实验 MAX_TOOLS 从 4 改为 5；请求版本升级为 `p65_dynamic_runtime_v3_tools5`，版本进入完整 TASK，避免旧合成 5 次 body 与新正式请求混同。
- [p65_readonly_investigation.py](../scripts/p65_readonly_investigation.py)：新增实验局部 `fixed_scope()`，统一 prepare/read_prepared 的固定策略与 SDK 指纹；对预算、模型/目标、重试/resume/fallback/Worker/Incident 上限、SDK 版本与 8 项源码哈希重新核验。它不是新的 Runtime 或生产配置来源。
- [test_p65_third_preparation.py](../tests/test_p65_third_preparation.py)：新增正式 5 次版本、篡改/旧计划拒绝、封存四调用回放、Checkpoint 输出绑定与安装 SDK 内存传输验证。
- [test_p65_dynamic_budget.py](../tests/test_p65_dynamic_budget.py)、[test_p65_readonly_investigation.py](../tests/test_p65_readonly_investigation.py)：适配新正式预算，保留合成预算隔离、超额停止、schema/敏感字段/字节限制和零额度工具拒绝。
- [test_p65_protocol_compatibility.py](../tests/test_p65_protocol_compatibility.py)：原 4 次历史场景显式使用 offline_tool_budget=4，继续验证旧失败边界；未改历史材料。

复用现有 ModelClient、Agent、Worker、ToolManager、RuntimeStore、Checkpoint、严格 schema、原始 Provider 校验和历史 replay。没有新依赖，没有修改生产 Agent/Worker、MAX_TOOL_CALLS=13、生产 Runtime 默认 39 次工具预算、数据库协议、租约/fencing、审批或 Remediation。

## 2. 新实验范围与冻结绑定

- 实验标识：`p65-third-tools5-241dca6-20261021-01`。
- 请求版本：`p65_dynamic_runtime_v3_tools5`。
- **执行源码提交：`241dca6353e6bd8aff8db50dfd74a7a9bed1949a`**。先单独提交代码，再从该提交逐个导出 Git blob；后续文档交付提交不改变此执行快照。
- 本地完整独立目录：[冻结包](../data/p65-third-tools5-241dca6-20261021-01)；[完整源码清单](P6_5_THIRD_EXPERIMENT_FREEZE_241dca6_01/source-manifest.json)绑定 263 个文件的 Git blob、长度与 SHA-256。[历史清单](P6_5_THIRD_EXPERIMENT_FREEZE_241dca6_01/historical-manifest.json)绑定原件及 76 文件独立副本、第二次 raw/SDK/seal、旧授权及消费记录哈希。
- Python 3.12.14，解释器 `D:\conda\envs\agentlearn\python.exe`；OpenAI 3.14.0、httpx2 2.13.0、Pydantic 2.13.5。[SDK 清单](P6_5_THIRD_EXPERIMENT_FREEZE_241dca6_01/sdk-manifest.json)记录版本和 8 项实际 SDK 源码哈希，prepared plan 同时绑定这些 SDK 指纹和 18 项执行关键源码哈希。

| 对象 | SHA-256 |
| --- | --- |
| [完整新实验计划](P6_5_THIRD_EXPERIMENT_FREEZE_241dca6_01/new-experiment-plan.json) | `413188aa386674e5ffd6d3663caeae25eb70822dd2771cb295fe8977bfb5584c` |
| [适配器 prepared plan](P6_5_THIRD_EXPERIMENT_FREEZE_241dca6_01/prepared-plan.json) | `eb3b063ecb5e783f977c6890b40ba4ee09b70c561f80a6c3dc37a1be80b5ac76` |
| 完整源码 manifest | `a241177ea5a244d6b46ec5270f2a836fad1aa01cc51e63ec50f08923d1f35103` |
| SDK manifest | `d3c3c87fe5dd31af060e8116f30db410c48f1d767fd5d91f7ef12ab8703803d9` |
| 历史材料 manifest | `44e2427c3cabe2d85b421b1afb18d49e374bd5264ba2bb2684ffb6bccc5192ce` |
| capture manifest | `350578d6914f7f09a530d85178f491da2366521b25b5f5fe4b5872bd58e5ae08` |
| 完整历史 package | `0c99f1bbdab631c2e04e7b41aa72c12d1a993c9067a139c664519a8e24b0c99a` |
| prepared 四文件封印 | `0402b1756a4b54b64d3b5075c86d3447c3328c51077c024e6d5b65c59885b5fa` |

完整新计划将代码提交、全树清单、SDK、证据、首轮请求、适配器计划及预算统一绑定。现有 read_prepared 会核验四文件集合、关键源码/SDK/固定策略及请求；**完整 Git 快照和外层计划的核对仍是未来执行前必须独立完成的准入检查**，不能拿父仓库 HEAD 或单次 read_prepared 替代。实际 replay 的 Agent、ModelClient、Worker、registry、adapter 均从此冻结 tree 导入，路径已记录在离线核验中。

## 3. 五次预算的 Python 边界

| 项目 | 新正式实验上限 |
| --- | --- |
| 工具实际执行 | 总计 5，含自动版本探针 1 次 |
| 首轮模型可用工具 | 探针提交后剩余 4 次 |
| 模型请求 | 最多 3；第三轮不要求发生 |
| Incident / Worker.run_once / attempt | 各 1 |
| SDK/应用重试、resume、fallback、人类请求 | 全部 0 |
| 工具权限 | 原 query_metrics、query_logs、get_running_version；仅 order-service 冻结历史材料 |

预算继续由 Worker._reserve 的 Runtime 事务与累计 usage 强制执行。提示中的数字只是经 runtime_budget/budget_instructions 严格验证的告知，不授予额度。超过剩余工具次数时停止，未执行调用保留 pending，不制造 observation 或 output。额度为 0 或最后模型轮次时必须 tools=[]、tool_choice=none；Provider 再返回 function_call 会在 Worker 执行前拒绝并停止。

offline_tool_budget 仍只生成 offline_only=true 计划，即使显式给 5 也不能付费执行；live 路径在读取凭据、创建新消费记录或 SDK 前拒绝。旧 v2 正式 4 次与合成 5 次计划也被新版本拒绝。旧授权不能通过新的 plan/version/request 绑定校验。没有解除旧 offline_only 限制或改写历史失败 Session。

## 4. 新旧首轮完整请求

[新首轮完整 body](../data/p65-third-tools5-241dca6-20261021-01/prepared/first-request.json)为 **9657 字节**，以项目 encoded(body) 规范化 JSON 字节计算 SHA。实际 SDK 请求前再次核验首轮对象及该 SHA；后轮逐次检查敏感字段、凭据真实值（仅未来获批执行时读取）和 131072 字节上限。SDK 内存传输测试确认真实安装 SDK 发出的 JSON 对象与保存 body 相等。该 SHA 不宣称是 HTTP wire 原始字节哈希；SDK 的字段排序/编码形式可以不同。

| 首轮 | SHA-256 |
| --- | --- |
| 第二次真实实验旧 4 次 | `825cfc43c6c4e60e1fcced85eea16d3680d94158170e11cbbb6c88f3efc6a79d` |
| 动态 v2 正式 4 次 | `c8b7cd2a3e0f2ec4a02d5869e701229529705b9f1d5cdee7e98f7e251caf633c` |
| 动态 v2 合成 5 次，仍仅离线 | `5ef5790a4ef6a5688dedefd58d347e2aeb2875cb26f0263abf6f87872d103150` |
| **新正式 v3，5 次** | **`d9cfac4386a1adfc2d565820e70bc03fc910cdf987020ab168df5481f2ff76a1`** |

相对旧 4 次请求，变化为 instructions 的版本和动态预算，以及 input 中的预算；相对旧合成 5 次请求，仅 instructions 新增协议版本。工具 schema、历史 package 与工具权限没有扩大。[逐字段比较](P6_5_THIRD_EXPERIMENT_FREEZE_241dca6_01/request-comparison.json)保留差异，原件不变。

## 5. 封存真实首轮的离线回放

使用第二次真实实验已封存的原 raw 响应，SHA 为 `7ff76c709d7eb8471e740f54a0c81838787ba06b32a5dc6b0b0d7bb253e3620e`。read_frozen 验证原目录完整 seal、原 raw integrity 与 SDK 解析一致性；没有重新生成该首轮调用。**该响应来自旧请求，不能证明新 v3 提示的真实自主行为。**

从冻结提交启动的本地 replay 只执行 1 次 Worker.run_once、1 个新隔离 Incident/attempt：探针 1 次 + 原始三个指标和一个日志调用，共 5 次。4 个模型调用的 call_id/name/arguments、已提交 evidence record/result、Checkpoint history 与下一轮 function_call_output 均一致；第二轮请求前 pending=[]，Checkpoint SHA 匹配，最终 SQLite 以只读方式重新打开核验。

| 检查 | 实测结果 |
| --- | --- |
| 原 raw 与 SDK 返回 | 字节/对象保持原样，原始调用顺序及参数保留 |
| 工具执行 / 输出 | 5 个执行记录；4 个模型调用均有已提交绑定输出 |
| 日志来源 | 只使用冻结 package 的 query_logs 结果；没有实时 Loki |
| 第二轮构建 body | 包含全部原始 message/call 和 4 个对应输出，21445 字节 |
| 第二轮预算 | remaining_tools=0，tools=[]，tool_choice=none |
| 后续回答 | 明确 scripted_mock_followup / 离线 Mock，不是真实 DeepSeek 第二轮 |
| ResultTrust | trusted=false，semantic_status=pending_review；mechanical 仅 result_schema_only |

首轮 cache_write_tokens 缺失仅沿用局部兼容：raw missing、SDK null，值未知，不补零；其余字段、类型与工具协议仍严格校验。历史 no_data 和有效历史 0.0 不混同，也不把历史观测当成当前业务健康。

证据：[离线核验](P6_5_THIRD_EXPERIMENT_FREEZE_241dca6_01/offline-checks.json)、[完整本地 replay](../data/p65-third-tools5-241dca6-20261021-01/offline-replay)、[文件索引](P6_5_THIRD_EXPERIMENT_FREEZE_241dca6_01/FILE_INVENTORY.json)。离线第二轮 body SHA 为 `88c5606c3ff61f727ba80f0c703fe638611117bad4b595cdb464109de83ce316`，不是已发送真实请求。

## 6. 实际测试范围、失败与定向复验

本轮只运行相关回归，不是全仓回归。使用上述解释器，选择 14 个测试文件，覆盖 P6.5 动态预算/协议/工具边界、Worker、Checkpoint、Runtime 预算/持久化/稳定性/完成流程及 P6.3/P6.4 信任边界；未选其他仓库测试。本轮选择范围内跳过 0、排除 0。

| 运行 | 实际结果 |
| --- | --- |
| P6.5 首批 4 文件 | 167 passed，1 failed，129.65 秒 |
| 10 个相关文件 + 新 SDK 内存传输用例 | 281 passed，1 failed，58 subtests passed，309.81 秒 |
| 修正预算断言与加强 pending 检查后定向复验 | 2 passed，9.36 秒 |
| 修正 SDK 表示层哈希断言后仅复验该项 | 1 passed，13.08 秒 |
| 按最后一次结果合并去重 | **450 个独立用例通过，58 个子测试通过；最终失败/错误/跳过均为 0** |

两次初始失败完整保留：一处测试仍按旧 4 次预算断言第二轮剩余 2，实测新预算应为 3；另一处错误地要求重新解析 SDK wire 后的字段顺序产生与 adapter encoded(body) 相同的 SHA，实际对象相等。分别修正为新预算的精确断言，以及冻结 body 哈希 + SDK 完整对象相等校验；没有放宽生产检查或修改原始 Provider 输出。没有反复重跑完整回归。

各次 runner 均阻断 socket/requests、Remediation 执行和后台恢复，forbidden_attempts=[]。SDK 测试使用虚构测试凭据/批准记录和系统临时目录中的 MockTransport/消费记录，只有内存传输，不能视为本实验的新真实授权、真实 ledger 或真实 DeepSeek 调用。[测试清单与 JUnit 汇总](P6_5_THIRD_EXPERIMENT_FREEZE_241dca6_01/test-verification.json)保留各次实际范围、失败和最新结果；原始 log/XML 在 [准备控制目录](../data/p65-third-preparation-20261021-101841)。

## 7. 外发范围、未知风险与新人类授权申请

未来获批时，仅允许发送冻结首轮完整 body，以及同一调查的动态后轮：先前已批准 input + 经严格校验的原始 Provider output + 与原调用及已提交 Checkpoint/result 绑定的实际 function_call_output。历史 envelope 可包含 status/service/reason、captured_at/captured_result、source、originals 路径/哈希、sanitized_projection、fault_window/collection_window、historical_only/use_for_current_state/evidence_scope；具体值以 package.json 为准。省略日志时间只映射固定历史窗口；其他不匹配查询返回 no_data 或拒绝，不做实时补采。

不外发 .env、凭据值、整份源码、无关 Incident 或历史授权/ledger。敏感防线是固定字段投影加已知值/模式检查，不保证识别任意自然语言秘密。

价格、余额、实际货币费用、超时后的扣费与 Provider 对 v3/第二轮协议的真实兼容性均未核实。最多 3 次潜在付费请求会重复发送增长的上下文；字节/输出 token 设置不是金额或输入 token 硬上限。30 秒 SDK timeout 和 3600 秒 Runtime 安全点检查都不是实验硬墙钟。5 次只容纳本封存样本的“探针 1 + 四调用”，新模型仍可能选择过多工具、直接结论、未完成或协议错误；不能保证达到最小验收目标。

**申请范围：** 请求后续独立审查人审核本报告、完整新计划、源码/SDK/证据 manifest、首轮实值与哈希、预算和上述风险，再由人类给出明确的新授权。新授权须绑定本实验标识、`241dca6353e6bd8aff8db50dfd74a7a9bed1949a`、新计划 SHA `413188aa386674e5ffd6d3663caeae25eb70822dd2771cb295fe8977bfb5584c`、适配器 plan/first-request/manifest 哈希与完整 1/3/5/0 范围，保存真实确认原文和带时区时间。批准实验不等于接受结果语义或批准修复。

[授权要求](P6_5_THIRD_EXPERIMENT_FREEZE_241dca6_01/authorization-requirements.json)当前 explicitly_authorized=false、authorized_at=null、review_status=pending。没有创建新的 authorization.json 或预建消费 ledger。未来消费仅可在明确批准及准入通过后由既有独占 xb 逻辑创建；失败或超时也不得自动释放、重试或重跑。旧授权、旧未用模型额度及旧消费记录均不能复用。

未来执行前必须重核全树/SDK/计划/证据/请求哈希、实际导入路径、凭据值外发检查、全新输出及消费路径未占用。任何绑定失败、协议/超时/敏感内容/大小错误、工具/模型额度耗尽或关闭工具后返回调用均停止；不以追求第三轮或验收成功为由补跑。

## 8. 原件保护与交付停止

核对了 807 个既存工作区文件和 5908 个受保护数据/日志/凭据文件记录；除本次明确修改的源码/测试外差异为 0，原数据目录未新增测试文件。额外核对既有 agent_logger/agent.log，其修改时间仍为 2026-10-12，SHA 不变。用户原有 README、Prompt、temp_test、删除/归档文档和未跟踪条目全部保留；暂存时仅选择本轮文件，没有 reset、clean、stash。[保护核验](P6_5_THIRD_EXPERIMENT_FREEZE_241dca6_01/protection-result.json)记录具体结果。

源码及测试已形成目标提交；交付提交仅增加本报告与 `P6_5_THIRD_EXPERIMENT_FREEZE_241dca6_01/` 的静态准备元数据。清单目录的局部 .gitattributes 仅固定这些 JSON 的原始字节，避免 Git 换行转换造成审查哈希漂移。完整首轮、package、原始响应、历史副本、日志、SQLite、冻结源码和离线运行数据保留在忽略的本地 data，没有加入 Git。提交推送的实际首行、最终 SHA、origin/main 一致性由最终交付回复列明。

**阶段结束：准备材料 pending，真实操作均为 0，真实 DeepSeek 第二轮尚未验证。等待独立审查和新的明确人类授权；不自动执行第三次实验或进入 P6.6。**
