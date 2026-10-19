# P6.5 第二次真实 DeepSeek 调查执行记录：工具预算耗尽，第二轮未发生

本次唯一实验已执行并停止。**真实第一轮自主工具调用与实际结果持久化得到验证；第二次真实请求没有发出，核心多轮验收未达成。** 不重试、不重跑、不扩额、不修复冻结代码、不进入 P6.6，等待独立审查。本报告是过程及材料核验记录，不授予语义 accepted 或修复权限。

## 1. 授权、代码与实际执行

- 实验标识：`p65-second-freeze-952809b-20261019-02`；目标代码：`952809b157bcbd83f94a4d43ff6b5f666a2b2a0d`。
- 本轮起始父仓库 HEAD 与真实远端 main 均为 `9087f4259d6bd57801d913ae2ab06c753ee72eac`。执行代码来自 [冻结 tree](../data/p65-second-freeze-952809b-20261019-02/tree)，没有使用父仓库最新 HEAD 或用户未提交 Prompt。
- 已阅读实际根目录 [AGENTS.md](../AGENTS.md) 与 [冻结准备报告](P6_5_SECOND_EXPERIMENT_REFREEZE_2026-10-19_18-54-23.md)。业务源码、公开接口、配置、状态机、历史材料及依赖均未修改。本次仅新增本地执行/核验记录与本报告；复用既有 CLI、BoundedResponses、Worker、RuntimeStore、历史回放和审批校验。
- 授权原文为“可以的我授权”。来源是当前 Codex 对话中用户直接发送的正式执行指令，该指令明确引用其在 ChatGPT 对完整审查方案的确认，并再次要求按绑定范围执行。原始 ChatGPT 消息时间未提供，记录为 null；没有伪造原始消息时间或审查者身份。
- 新授权实际记录于 `2026-10-19T11:19:50.572709+00:00`（北京时间 `2026-10-19T19:19:50.572709+08:00`），时间来自本机 `datetime.now(timezone.utc)`。既有 validator 所需 authorized_at 使用这一实际记录时间，并在文件中明确其语义。新授权完整绑定实验、代码、scope、plan、历史及动态后轮外发范围、1/3/4/0 上限和已披露未知费用风险；仅限本次，不可长期复用。实验执行授权与调查结论人工认可分别记录。
- 独立进程 PID `6364`，仅一次启动、一次 Worker.run_once、一个隔离 Incident、一次 attempt。指定 Python 为 `D:\conda\envs\agentlearn\python.exe`，cwd 为冻结 tree。CLI 进程已退出，退出码 **2**。启动与退出凭据见 [process-launch](../data/p65-second-execution-952809b-20261019-02/process-launch.json)、[process-exit](../data/p65-second-execution-952809b-20261019-02/process-exit.json)。

实际命令（`-B` 仅禁止写入 bytecode）：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B -m scripts.p65_readonly_investigation --execute --prepared-dir 'D:\Incident Agent\data\p65-second-freeze-952809b-20261019-02\prepared' --authorization 'D:\Incident Agent\data\p65-second-execution-952809b-20261019-02\authorization.json' --output-dir 'D:\Incident Agent\data\p65-second-live-952809b-20261019-02'
```

凭据仅在授权后读取，经过真实值外发检查，再通过子进程环境交给既有 credentials()。没有复制或修改 .env，未打印凭据。PYTHONPATH 固定冻结 tree；子进程 verbose import 原件确认 CLI、boundary、Agent、ModelClient、Worker、registry 实际从该 tree 导入。显式 TASK 的 CLI 不导入 prompts/agent_prompt.py；用户 Prompt 未进入本次请求。

## 2. 执行前准入检查

全部检查在真实 SDK create 前通过，检查时 SDK create 和 Worker.run_once 均为 0。没有为促成检查通过重建或改写冻结材料。

| 检查项 | 本轮实际结果 |
| --- | --- |
| 目标源码 | 250 个文件集合一致；逐项核对 Git blob 对象身份、长度和 SHA-256，与目标提交一致 |
| 历史材料 | 原始目录与独立副本各 76 个文件逐项一致；既有 _verified_files/read_prepared 核验多层 manifest 和观测 |
| SDK | Python 3.12.14；OpenAI 3.14.0、httpx2 2.13.0、Pydantic 2.13.5；sdk-manifest 的全部版本和 8 项 SDK 源码哈希一致 |
| 执行关键文件 | plan 内 18 项源码哈希一致 |
| prepared | 严格四文件集合，现有 read_prepared 通过；完整首轮请求及 package 字节未改 |
| 授权隔离 | 旧授权对新 plan 被实际 validator 拒绝；旧原始响应、失败 Session、计划和消费记录哈希保持不变 |
| 单次消费 | 新 plan 在冻结 tree 中的消费记录执行前不存在，由既有 xb 逻辑独占预留；消费后未删除或释放 |
| 输出隔离 | 新输出目录执行前不存在，与历史输入、旧 live 和 prepared 隔离 |
| 边界 | 固定 DeepSeek / deepseek-flash，最多 3 次 SDK、4 次工具含探针、1 次 Worker；零重试/resume/fallback；仅历史 registry 三工具 |
| 外部执行 | 既有 endpoint gate、网络/requests/数据库/线程/子进程/修复 guard 生效；无生产入口、调度器或 RecoveryWorker 启动 |

| 绑定对象 | SHA-256 |
| --- | --- |
| scope | `77a6b5cc2973321becb2568636a328ebf8c234f77be0364b7a63c1545a5a1638` |
| plan | `685dc803754abf34b2ee8b93fea09824b37f8bb967e54f5b6fc33cd278f4e27c` |
| 首轮 body | `825cfc43c6c4e60e1fcced85eea16d3680d94158170e11cbbb6c88f3efc6a79d` |
| 历史 package | `0c99f1bbdab631c2e04e7b41aa72c12d1a993c9067a139c664519a8e24b0c99a` |
| source manifest | `7e35d8c94c87d2dacc4a8573964ae83087bf2f1f8782a5f059abb59a8f7519c4` |
| SDK manifest | `97a5d9e57b5189797491c4424ca57369e19384c740ca317ed74a20a3135bcc1e` |
| 历史 capture manifest | `350578d6914f7f09a530d85178f491da2366521b25b5f5fe4b5872bd58e5ae08` |

静态 scope 的 pending 标志保留原件；真实许可由本次新授权及消费记录表达，未回写已冻结的待审批材料。

## 3. 真实第一轮与工具实际执行

首轮 SDK 开始 `2026-10-19T19:20:38.270536+08:00`，完成 `2026-10-19T19:20:42.683707+08:00`。Provider 为固定 `https://api.deepseek.com`，模型 deepseek-flash，Response ID `c49000ba-94b4-4316-9159-126ae48660f4`，Provider status=completed，协议 error=null。原始 Provider 返回是英文开场 message 加 **4 个自主 function_call**；没有人为补入调用，没有最终调查结论。

已知 cache_write_tokens 缺失按既有局部兼容处理：raw 中缺失、SDK 解析中为 null，未知值未补零；其他协议规则未放宽。首轮原始返回与 SDK 返回都通过既有校验。因此本轮的停止原因是工具预算耗尽，不能写成 Provider 或 SDK 协议失败。

Worker 先执行自动版本探针，再按 Provider 输出顺序处理三个指标。以下 captured status 是历史原采集状态；四个实际工具返回外层均为 no_data、historical_only=true、use_for_current_state=false，不证明当前状态。

| 顺序 | 工具 | 参数补充 | 原始 call_id | 执行情况 | 历史 captured status |
| --- | --- | --- | --- | --- | --- |
| 1 | get_running_version | order-service | `runtime-probe-137de85b05314f6c88e5f2197bae86fa` | 已执行并保存 | no_data |
| 2 | query_metrics | http_5xx_rate | `call_00_rK698ozA1QvmwhNxllAz3779` | 已执行并保存 | no_data |
| 3 | query_metrics | http_request_rate | `call_01_wEhnoD0lEiidruyUNt3I8252` | 已执行并保存 | ok |
| 4 | query_metrics | http_latency_p95_seconds | `call_02_JeBQoinLdvwLq9ajZeHH4748` | 已执行并保存 | no_data |
| 5 | query_logs | order-service | `call_03_fSbzPQLAq4t3kddpwgnR6440` | 预算拒绝，未执行 | 无返回 |

全部实际工具读取已冻结文件，没有实时 HTTP、Prometheus 或 Loki 请求。http_request_rate 的历史 captured status=ok 仅属于原采集结果；其余列出的 no_data 不是零，也不是根因或恢复证据。tool_failed 事件是现有 Runtime 对外层 no_data 的记录，不能由此推导实际网络出错或工具未执行。

第 5 行的日志调用本身属于白名单、参数合法，但在执行前触发 session_tool_budget_exhausted。没有日志返回、observation 或 function_call_output；该调用完整保留在 pending。拒绝依据来自终态错误、budget_exhausted 事件、pending、工具 I/O 边界数及缺失日志记录的交叉核验；本次没有把它伪装成另一个 tool_budget_rejected 事件。

## 4. 持久化链与核心验收

| 验收项 | 本次结果 |
| --- | --- |
| 真实第一轮自主请求工具 | 已证实：原始 response 内 4 个合法 function_call |
| Worker 实际执行并保存结果 | 已证实：探针与三个指标共 4 个实际执行；3 个模型调用有匹配输出 |
| 第二次真实请求携带已提交 output | **未发生**：不存在 request-02，实际 SDK create 仅 1 次 |
| 第二轮真实返回并继续调查/结论 | **未发生，无法验证** |

3 个 function_call_output 均与真实调用的 call_id、name、arguments 及 evidence result 一致，原调用顺序保留。最终 SQLite checkpoint 与 summary.checkpoint 逐字段一致，3 个输出确已持久化；4 个工具调用各有 before_io/after_io/committed 边界。完整 Worker 边界共 15 项、Runtime 事件共 32 项。没有借剩余 2 次模型额度绕开未完成调用。

核验时发现两项本地衍生脚本的过严断言：tool_checkpoint_committed hook 先保存 evidence record，调用方随后才追加并保存本次 function_call_output；显式 TASK 路径也无需导入 prompt 模块。已依据实际 Worker 改为检查最终 SQLite 和实际 import 记录，再完成封存核验。该修正只涉及本地核验脚本；唯一实验、原始输出、冻结代码、授权和消费记录均未重跑或改写。没有把初始核验断言失败冒称为实验成功。

## 5. 终态、预算及费用

- Session：`ses-9771f56bc4b7486db1af42457a4c0a94`，终态 **EXHAUSTED**，last_error_category=`session_tool_budget_exhausted`，attempt=1、模型累计=1、工具累计=4、cache_hits=0、resume=0、human_request=0。
- Incident：`inc-0e392ae3`，保存状态仍为 **investigating**。这是停止时既有同步结果；没有为美化报告手动改成 completed、resolved 或 recovered。Session 终止不自动等于 Incident 业务恢复。
- 原始 Runtime result：success=false、status=model_incomplete、answer 为“调查停止：session_tool_budget_exhausted”。这是 **Runtime 生成的停止文本**，不是 Provider 最终调查答案。answer-original.txt 不存在，没有最终模型答案可供语义验收。
- ResultTrust：mechanical_status=passed，仅限 result_schema_only；semantic_status=pending_review、trusted=false、human_approval=not_recorded、review_origin=none。机械 schema 通过不代表调查成功，也不代表模型内容被接受。独立审查仍待完成。

| 项目 | 实际 / 授权上限 |
| --- | --- |
| 真实 SDK create | 1 / 3 |
| 工具实际执行含探针 | 4 / 4 |
| 模型自主工具请求 | 4；实际执行 3、预算拒绝 1 |
| Worker / Incident / attempt | 各 1 / 1 |
| SDK 与应用重试 / resume / fallback | 全部 0 |
| 新业务请求 / 实时 Prometheus、Loki / 故障注入 / 修复 | 全部 0 |
| 本次脚本 Mock SDK create | 0 |

Provider usage：input_tokens=2375，cached_tokens=2176，output_tokens=210，reasoning_tokens=0，total_tokens=2585；cache_write_tokens **未知**。价格、余额、实际货币费用及扣费状态未查询、未核实。该 usage 是 Provider 自报，SDK create 计数表示本地进入调用，不是账单证明。授权中的 30 秒 SDK timeout、3600 秒 Runtime 安全点检查不是实验硬墙钟；未新增或声称金额硬上限。

## 6. 原始材料、封存与保护结果

原始运行目录为 [data/p65-second-live-952809b-20261019-02](../data/p65-second-live-952809b-20261019-02)；授权、进程日志、执行前后核验及衍生索引在 [data/p65-second-execution-952809b-20261019-02](../data/p65-second-execution-952809b-20261019-02)。运行目录原始 seal 的 12 项文件全部复核通过，原始 Provider text 未作凭据替换或内容重写；哈希约束本地字节，不是 Provider 签名。

| 材料 | SHA-256 |
| --- | --- |
| [原始 Provider 返回](../data/p65-second-live-952809b-20261019-02/response-01.raw.txt) | `7ff76c709d7eb8471e740f54a0c81838787ba06b32a5dc6b0b0d7bb253e3620e` |
| [SDK 解析返回](../data/p65-second-live-952809b-20261019-02/response-01.sdk.json) | `ec579a56826275a2d775fbe950e802bce8f72c06686c34ae089e113293fcca8a` |
| [首轮实际 SDK 请求封装](../data/p65-second-live-952809b-20261019-02/request-01.json) | `56306c6157386bf4db06171a1096f84543285bdb43cb4a097e5361d240af3436` |
| [完成及 usage 元数据](../data/p65-second-live-952809b-20261019-02/completion-01.json) | `603cb541e8854f3a5ca32b6635a6524059f2a6b23119ffdedf76c90614d63b91` |
| [运行输出原始封印](../data/p65-second-live-952809b-20261019-02/sha256.json) | `f3b2e458ee0cc3171a52e2cfbbef344df8012aaf9b64427b02dc6f7abbb777e2` |
| [SQLite Runtime](../data/p65-second-live-952809b-20261019-02/runtime.sqlite3) | `e8c95864b655945704ece1b6db27f14605d6faf77aaa8479e0b380729facd973` |
| [完整 Worker 边界](../data/p65-second-live-952809b-20261019-02/worker-boundaries.json) | `0d78c1430d34c1b09bf1ba4e11ded8dc2705b865d0f870ad648e5217f68f0ec1` |
| [本次新授权](../data/p65-second-execution-952809b-20261019-02/authorization.json) | `4bee67c0b7f9f1e19a4b670cfe4596c95459cfdf4146bf8b4d12d843f9cce89c` |
| [新 plan 消费记录](../data/p65-second-freeze-952809b-20261019-02/tree/data/p65-paid-consumption/685dc803754abf34b2ee8b93fea09824b37f8bb967e54f5b6fc33cd278f4e27c.consumed.json) | `b39c4ab4a963846059659cffe2b487449f611b3280862f338c0443421edf27e2` |
| [执行前检查](../data/p65-second-execution-952809b-20261019-02/preflight.json) | `186113bbe79a1760163569fd5d9c41d4cab7ae3cecd7a2bccf0f55ea832bba7d` |
| [执行后核验](../data/p65-second-execution-952809b-20261019-02/postflight.json) | `a7f2897c986ce0d8299319f27ca788649717e25208b079587d04e6a091c926d6` |
| [调用、执行和拒绝索引](../data/p65-second-execution-952809b-20261019-02/calls-and-tools.json) | `0a7722cdd056d98d408c35be1be904da422a2366d4c4bd33f6e1c46a2f7c453c` |
| [Checkpoint 和事件索引](../data/p65-second-execution-952809b-20261019-02/checkpoint-and-events.json) | `39a95c20e872bd535471b5c2e2bea5c9939bf828190471f3f4474ac6697f879e` |
| [本地封存清单](../data/p65-second-execution-952809b-20261019-02/artifact-inventory.json) | `be5f0684c1974a68c5e4a65276170f2da06ee2cdd79c674f4b6acb7ea514af5c` |

真实 SDK 请求保存的是 create body 及 Runtime/checkpoint 绑定，不声称是底层 HTTP 原始请求字节。Provider 原始 JSON text、SDK parse、compatibility、usage、原始 function_call/arguments/call_id、实际工具结果/拒绝索引、Checkpoint、事件和终态全部保留；本次无第二轮文件，不能制造占位响应。

封存核验结果：SQLite integrity_check=ok；执行前登记的 **6167 个现存受保护文件变化 0**；250 个冻结源码、prepared/scope/SDK manifest、旧授权、旧 ledger、旧响应、旧失败 Session 原件均未改写。新增消费记录保留，不删除、不转移额度。原有 README、Prompt、temp_test、文档删除/归档、未跟踪文件及旧测试数据均保留，不顺带提交。

## 7. 验证边界与交付

本轮实际完成的是执行前完整性/边界核验、**一次真实 Provider 尝试**、停止后的只读封存核验；没有新增单元/Mock/集成测试、全回归、离线模型回放或诊断循环。没有新依赖、公开接口或业务数据结构变更。未启动生产 main.py、Scheduler、RecoveryWorker，没有故障开关或受控服务需要本轮恢复。

已确认的容量事实：自动探针占 1 次，第一轮一次返回 4 个待处理调用，合计需要 5 次实际执行；授权只有 4 次，所以在日志前停止。剩余模型额度不能替代工具额度。未验证事项包括真实第二轮 function_call_output 协议兼容、后续推理、最终语义、根因及当前业务恢复；不得由第一轮成功推导它们成立。本轮仅报告，不自动修改预算、Prompt、工具 schema 或 Worker 行为。

本次提交推送范围仅为本报告，原始运行数据、数据库、凭据及本地执行脚本不进入 Git。准确提交 SHA 与推送后远端一致性由最终交付说明给出，避免在报告内写入自引用 SHA。**本次实验至此结束，等待独立审查；任何新实验均需重新审查和明确授权。**
