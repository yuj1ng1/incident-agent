# P6.5 动态预算协调修复与离线验证

本阶段完成实验边界修复及离线验收。真实 DeepSeek 请求为 0；没有启动第三次真实实验，没有恢复第二次 EXHAUSTED Session，没有进入 P6.6。离线 COMPLETED 只表示 Mock 流程完成，调查结论仍不可信、待独立审查。

## 1. 基线、范围和保护

- 开始时本地 HEAD、origin/main 和 `git ls-remote origin refs/heads/main` 均为 `93f37322153dcb74e75292cc4da8f32372081187`；提交前再次核对真实远端仍为该值。
- 已重新阅读 [AGENTS.md](../AGENTS.md)、[第二次真实实验报告](P6_5_SECOND_REAL_INVESTIGATION_2026-10-19_19-26-24.md)、实际 adapter/prepare、Agent、ModelClient、Worker、ToolManager、RuntimeStore/Checkpoint，以及第二次原授权与冻结 tree 内的消费记录；没有把前次审查结论直接当作事实。
- 代码修改仅限 [p65_model_boundary.py](../scripts/p65_model_boundary.py)、[p65_readonly_investigation.py](../scripts/p65_readonly_investigation.py)；新增 [动态预算测试](../tests/test_p65_dynamic_budget.py)，调整 [已有 P6.5 测试](../tests/test_p65_readonly_investigation.py)。复用现有 Worker、RuntimeStore、ModelClient、ToolManager、原始响应解析与历史回放实现，没有第二套 Runtime。
- 生产 Agent/Worker、Runtime 默认预算、工具执行权限、Remediation 和正式人工审批均未修改，没有新增或升级依赖。
- 保留用户已有 README、Prompt、temp_test、删除/归档文档及未跟踪文件。554 个既存工作区文件、1186 个既有 P6.5 材料逐项核对 SHA-256；初始 Git 状态条目仍在，保护目录未新增文件。证据见 [基线](../data/p65-dynamic-budget-offline-20261019-01/baseline.json) 与 [保护核验](../data/p65-dynamic-budget-offline-20261019-01/protection-result.json)。未使用 reset、clean、stash。

## 2. 缺陷定位与修复前复现

基线 `93f3732` 的 `scripts/p65_model_boundary.py::BoundedResponses.create` 第 295–316 行，仅用 `instructions.startswith(TASK)` 接受输入，随后 `deepcopy(first_request)`，只更新 input；仅第三轮强制 tools=[]/tool_choice=none。因此 Agent 的动态 instructions 与 tools 没有进入实际 SDK body。

实际调用链是：Worker._reserve 先持久化模型计数与 Checkpoint → Agent.request_durable_response → ModelClient.call_once → BoundedResponses.create → SDK。位置分别见 [Worker](../investigation/worker.py)、[Agent](../agent/agent.py)、[ModelClient](../model_client/model_client.py) 与 [ToolManager](../tools/tool_manager.py)。

- Worker 第 794 行附近先执行自动版本探针；工具计数先预留，再执行并保存 record。第 806 行附近预留模型额度，随后把 Incident 聚合剩余预算传入 Agent。
- Agent 第 22–42 行用 attempt 剩余额度和 Runtime 剩余额度的较小值构造提示；本项目当前 attempt 上限为 7 轮模型、13 次工具，P6.5 原上限为 3/4。因此首轮不是剩余 4 次工具，而是 **3 次工具、3 轮模型（含本轮）**。
- ModelClient 原样传 model/input/instructions/tools，不传 tool_choice；P6.5 adapter 负责按受控工具集合确定 tool_choice。
- 通用 registry 的原始 schema 比 P6.5 schema 宽松。但本次真实 P6.5 路径已先把 registry 缩减为冻结的严格三工具，所以该次 Agent 实际取得的也是严格 schema。不能把“通用 schema 更宽松”误写为“该次真实请求用了宽松 schema”。

修改前已通过真实 Worker 路径做最小离线复现，完整 kwargs 与最终 body 见 [before-differences.json](../data/p65-dynamic-budget-offline-20261019-01/before-differences.json)。

| 项目 | 修复前实际行为 | v2 行为 |
| --- | --- | --- |
| 首轮模型累计=1、工具累计=1 | Agent 提示剩余 3/3，SDK 只含静态 TASK | SDK 包含严格验证后的 3/3 动态提示 |
| 第二轮工具剩余=0 | Agent tools=[]，SDK 仍广告三工具，tool_choice=auto | tools=[]，tool_choice=none |
| 第三轮 | 旧 adapter 已关闭工具 | 保留关闭，并核对 Agent 与 Runtime 一致 |
| 不正确/额外提示 | TASK 前缀之后内容没有完整校验 | 完整字符串严格相等，否则 SDK 前拒绝 |

第二轮剩余 0 的修改前复现使用第一次已封存真实响应（其中三个调用可执行）；第二次原始响应有四个调用，在总预算 4 下根本没有第二轮，两者没有混为一谈。

## 3. 动态预算、instructions 与严格 schema

请求契约明确升级为 `p65_dynamic_runtime_v2`。静态 TASK 的具体“四次”表述改为冻结请求及 Python Runtime 的权威上限，避免与仅离线的合成预算矛盾；其余历史证据及禁止修复规则保留。默认实验工具预算仍是 4。新版本必须重新冻结、重新批准。

`runtime_budget` 从已提交 Session/Checkpoint 读取计数，要求 RUNNING、order-service、一个 attempt、无人工请求，attempt 计数等于累计计数，Checkpoint phase=model_started，budget 与 Session 完全一致，已提交 record 数及唯一 observation 数等于工具执行数。snapshot 另核对 Incident 聚合 usage 与本 Session 一致，拒绝借其他 Session/attempt 绕预算。

remaining_tools 取 attempt 与 Runtime 剩余额度较小值；remaining_rounds 同样取较小值，模型计数已经包含本轮，所以使用 `max_model_requests - total_model_requests + 1`。预算快照明确区分“模型已预留请求数”和“已提交工具执行数”；缓存命中不冒充新执行。

`budget_instructions` 固定整个 Agent 提示格式。kwargs 只允许 ModelClient 的四个字段，model 必须固定；instructions 必须与真实计数算出的完整文本逐字符相同。错误数字、额外指令、改动 TASK 或未来 Agent 模板漂移都拒绝。模型回答、工具结果和人类线索不能覆盖该文本。提示不能授予额度；最终执行仍由 Worker 的 Python 预算预留决定。Agent 固定模板中的 human 提问说明不授予权限：P6.5 不广告 HUMAN_TOOL，max_human_requests=0，Provider 请求它仍拒绝。

动态 tools 只用于选择已冻结严格 schema 的工具名，参数字段集合必须与对应冻结 schema 一致；非 function、新工具、重复名字、新增/缺失字段均拒绝。Agent 描述、service 范围或 additionalProperties 等宽松定义不会覆盖冻结 schema。最终 schema 仍仅三项历史只读工具、service enum=[order-service]、additionalProperties=false，保留历史回放说明。

工具剩余 0 或最后模型轮次，Agent 必须传空集合；SDK 同时使用 tools=[]/tool_choice=none。否则从严格池选择对应工具，tool_choice=auto。Provider 返回未广告工具仍拒绝；原始 response 先保存，再拒绝其执行，不伪造工具结果。

## 4. 新首轮冻结与每轮绑定

prepare 在离线阶段预建“自动探针已提交一次、当前模型已预留一次”的首轮 body，plan 记录版本、预期初始计数、代码哈希、证据哈希与首轮 SHA。SDK 入口重新用真实 Session/Checkpoint 构建 body，规范化字节必须与冻结首轮完全一致，且 SHA 等于 plan 传入的值；不一致时不会进入 SDK create。

冻结模板与严格 schema 池在各轮检查中均有哈希保护。后轮 input 继续使用原始 Provider output 顺序，以及已提交 Checkpoint 内可绑定的 function_call_output；核验 call_id/name/arguments、record result、缓存来源与完整链。缺失、重复、篡改、未提交或被拒绝调用的结果均停止，不补造成功输出。

每轮均重新执行敏感值/模式检查与 **131072 字节**规范化 body 上限检查，保存完整 body、Session、实际预算快照、Checkpoint SHA 和 body SHA。首轮新 body 为 9602 字节；本次离线预算 5/6 的第二轮分别为 21390/24624 字节，均通过上限。敏感检查是白名单加已知值/模式防线，不声称能识别任意自然语言秘密。

这里的 body SHA 针对 `encoded(body)` 的规范化 JSON 字节，不冒称 HTTP 传输层原始字节 SHA。安装 SDK 的 MockTransport 测试另验证实际序列化后 JSON 对象与新冻结 body 相同。

| 首轮请求 | SHA-256 | 授权语义 |
| --- | --- | --- |
| 第二次真实实验旧 body | `825cfc43c6c4e60e1fcced85eea16d3680d94158170e11cbbb6c88f3efc6a79d` | 历史原件，不可复用 |
| v2 默认总预算 4 | `c8b7cd2a3e0f2ec4a02d5869e701229529705b9f1d5cdee7e98f7e251caf633c` | 仅离线准备，未获新真实授权 |
| v2 合成预算 5 | `5ef5790a4ef6a5688dedefd58d347e2aeb2875cb26f0263abf6f87872d103150` | offline_only=true，禁止真实执行 |
| v2 合成预算 6 | `663b18013dd29c90c1f04c5d189d26551fd4e3f8f4acb1cc073e034a83534175` | offline_only=true，禁止真实执行 |

默认 4 次版本相对旧 body 只有 instructions 字段变化，input/schema 未变。合成 5/6 次版本另改变 input 中的预算数字。旧授权不仅 first SHA 不同，plan SHA 和版本也不符，实际 validator 已拒绝；没有创建新的真实批准。

材料见 [v2 预算 4 prepare](../data/p65-dynamic-budget-offline-20261019-01/v2-prepared-4)、[逐轮参数协调](../data/p65-dynamic-budget-offline-20261019-01/request-coordination.json)。prepare 的 `offline_tool_budget` / `--offline-tool-budget` 只创建测试计划，可取 1 至当前 attempt 上限 13；显式指定后即使数值为 4，也标记 offline_only，live 入口在创建输出、读凭据、预留消费和构造 SDK 前拒绝。真实执行入口继续限定原 4 次预算。

## 5. 第二次真实响应的离线回放

固定输入来自 [第二次原始运行目录](../data/p65-second-live-952809b-20261019-02)，经原 seal、raw integrity 和 SDK 解析一致性验证读取；没有重新生成四个调用冒充真实返回。原 raw SHA 为 `7ff76c709d7eb8471e740f54a0c81838787ba06b32a5dc6b0b0d7bb253e3620e`。旧授权与冻结 tree 内消费记录按实际内容绑定核对，仅读取，不作为本阶段许可。

| 离线场景 | 自动探针 | 模型首轮原始调用 | 实际工具执行总数 | 模型轮数 | 结果 |
| --- | --- | --- | --- | --- | --- |
| A：保持总预算 4 | 1 | 原始 4 个 | 4 | 1 | EXHAUSTED；日志 pending，无日志 record/output，无 request-02 |
| B：合成总预算 5 | 1 | 同一原始 4 个 | 5 | 2 | 四个输出完整绑定；第二轮剩余工具 0，tools=[]；后续为 Mock |
| B：合成总预算 6 | 1 | 同一原始 4 个 | 5 | 2 | 四个输出完整绑定；第二轮剩余工具 1，仍广告严格池；后续为 Mock |

四次预算场景保留原始 message 和四个 function_call，只保存三个已执行指标的 output。第五次工具调用没有执行，没有伪造日志成功，也没有虚构完整 output 链或擅自进入第二轮。

合成 5/6 场景的第二轮 input 保留原始 message、全部原始 function_call 的顺序/id/arguments，以及实际离线回放执行后提交的四个 output；每项与 Checkpoint record 相同。所有后续响应明确为 scripted_mock_followup，不是第二轮 DeepSeek 返回。每个场景都使用新建的隔离测试 Session，没有修改或恢复历史 EXHAUSTED Session。

证据见 [离线核验索引](../data/p65-dynamic-budget-offline-20261019-01/offline-audit.json)、[预算 4 回放](../data/p65-dynamic-budget-offline-20261019-01/v2-second-replay-4)、[预算 5 回放](../data/p65-dynamic-budget-offline-20261019-01/v2-second-replay-5)、[预算 6 回放](../data/p65-dynamic-budget-offline-20261019-01/v2-second-replay-6)。这些目录同时保存 Agent kwargs、Checkpoint 和实际构建的 SDK body。

## 6. 下一次实验的两种候选预算

| 变量 | 方案 A：仍为 4 | 方案 B：未来另行批准 T 次 |
| --- | --- | --- |
| 探针后首轮可用 | 3 | T-1 |
| 进入真实第二轮 | 首轮不超过 3 个实际执行可继续；再次返回该批 4 个则不能 | T>=5 可容纳本次固定批次，但不保证新响应相同 |
| 后续工具空间 | 首轮用满后为 0 | 本批执行后为 T-5；5 次时为 0，6 次时为 1 |
| 最坏工具执行数 | 4（含探针） | 明确获批的 T，仍受现有 attempt 上限约束 |
| 历史证据外发量 | 不增加工具执行上限 | 可能增加输出项或更早外发原先未执行的日志；每轮仍检查大小/敏感内容 |
| 请求 SHA | v2 修复已使其改变 | instructions 和 input 预算还会再次改变，必须重新冻结与批准 |

若目标专门验证本次四调用的 function_call_output 真实往返，方案 B 留有更合适的执行空间；这里仅作评估，没有选择真实 T，也没有实施付费扩额。T=5 只是容纳“探针 1 + 固定批次 4”的必要算术条件，不保证真实第二轮完成；Provider 可忽略预算数字、选择更多调用、返回协议错误或未完成结果，体积/敏感检查也可能停止发送。缓存、模型选择、响应大小、时延和第三轮禁止工具同样影响完成。现有样本不足以量化预算耗尽概率。

尤其是：固定旧响应是在旧请求下生成的，不能用离线回放推断新动态提示下模型仍会选择相同调用。工具广告和 Prompt 都不替代 Python 执行门禁。

## 7. 测试与执行边界

Python 使用 `D:\conda\envs\agentlearn\python.exe`。本机实际依赖为 OpenAI 3.14.0、httpx2 2.13.0、Pydantic 2.13.5，与锁文件一致；已检查安装 SDK 的 InputTokensDetails 定义，cache_write_tokens 原为必填 int。兼容逻辑未扩展：仅缺失/null 的该元数据可保持未知，其他字段类型/协议错误仍拒绝。

新增测试文件 **38 项**，覆盖真实预算/探针、动态轮数、SDK body、假预算/额外提示、工具白名单/字段/严格池、零额度与最终轮、首轮 SHA、旧批准、真实第二次响应完整保留、合成预算及完整输出、逐轮敏感/字节检查。已有测试继续覆盖 Checkpoint 缺失/篡改、call_id、历史参数、缓存和局部 SDK 兼容，并增加安装 SDK 实际 JSON body 与新冻结 body 相等断言。合成恢复测试仍只使用临时 Session，且将 Agent 工具集合缩减为同一严格白名单，不放宽生产或真实恢复限制。

实际运行记录：

1. 首次两组 P6.5 测试误把 basetemp 放入项目 data，触发既有 `tests_must_use_temporary_store_paths` 保护：41 failed、62 passed。保留 [初始日志](../data/p65-dynamic-budget-offline-20261019-01/pytest-initial.log)，没有关闭保护；之后使用系统临时目录。
2. 两组已有 P6.5 测试：103 passed；新增测试初次：35 passed。后续新增边界测试并完成自查后，统一运行下述最终相关回归。
3. 最终相关回归：**422 passed，58 subtests passed，0 failed，0 skipped，退出码 0，412.88 秒**。共 13 个文件，涵盖 P6.5 三组、Runtime 四组、Worker 指标三组、P6.4 两组、P6.3 模型验证一组。完整文件列表与禁用 I/O 保护见 [runner](../data/p65-dynamic-budget-offline-20261019-01/run-related-tests.py)、[边界结果](../data/p65-dynamic-budget-offline-20261019-01/regression-boundary.json)、[日志](../data/p65-dynamic-budget-offline-20261019-01/pytest-related.log) 和 [JUnit](../data/p65-dynamic-budget-offline-20261019-01/pytest-related.xml)。

最终回归在父进程阻断 socket、requests 真实网络、ActionExecutor.execute 和 RecoveryWorker.start；未记录禁用动作尝试。安装 SDK 的既有 MockTransport 测试只在内存序列化/解析，不连接 Provider；其测试报告中的 SDK entry 数不是付费调用。合成授权/消费夹具只在系统临时目录用于既有幂等测试，不是正式批准，不触及真实 ledger。

本次未修改生产模块，因此未运行完整后端/前端回归，也未运行真实 loopback HTTP、真实模型或真实监控联调；没有把历史回归数字沿用为本次结果。三份新 prepare 均再次通过 read_prepared；本次代码完整 diff 已自查，无新依赖或生产接口/持久化迁移。

| 真实操作 | 本阶段实际数量 |
| --- | --- |
| DeepSeek 网络请求 / 付费请求 | 0 |
| 新业务 HTTP / 真实 Prometheus / Loki | 全部 0 |
| 故障注入 / Remediation / 后台恢复启动 | 全部 0 |
| 新正式付费授权 / 正式消费记录 | 全部 0 |
| 历史 Session resume / 自动第三次实验 / P6.6 | 全部 0 |

## 8. 未验证事项与下一次重新批准范围

尚未验证真实 Provider 接受 v2 动态 instructions、空 tools/tool_choice=none、完整 function_call_output 链，以及真实第二/三轮协议、工具选择、最终推理与费用。离线结果不证明业务恢复，也不授予语义 accepted。

旧 `p65_protocol_replay.run_case` 保留旧请求相等约束，不用于 v2 验收；本轮通过其只读 read_frozen 加新 prepare/run_prepared 进行回放。旧 prepare 与旧授权继续拒绝，未为兼容它们放宽安全检查。本地复现脚本和原始运行数据保留在 data，按仓库约定不提交。

下一次真实实验必须重新独立审查并明确批准：目标提交及源码/SDK 冻结身份、v2 TASK 与完整新首轮 body/SHA、历史 package/manifest、模型与固定 endpoint、总工具预算（含探针及后续调用空间）、最多三轮/一次 attempt/零重试与 resume、动态后轮外发规则、字节/敏感门禁及费用未知风险。未来若选择扩额，还须另行修改并冻结当前仍限定 4 次的真实入口，不能把本次 offline_only 合成计划改成已批准计划。

本阶段不生成该批准，不使用旧实验剩余模型额度，不修改旧原件或消费记录。仅选择性提交本次五个源码/测试/报告文件，推送结果以最终交付核对的本地与远端 SHA 为准；完成后停止等待独立审查。
