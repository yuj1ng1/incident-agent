# P6.5 第二次真实多轮调查：目标提交重新冻结与新授权申请

**本报告及附件仅为待审批材料，当前没有真实实验执行许可。** 本阶段只读核验旧材料，生成新的独立准备目录；真实 DeepSeek SDK create、Worker run_once、新业务请求、新批准授权及消费记录均为 0，未进入 P6.6。用户追加的“完成后提交并推送”只授权交付本次准备材料，不授权付费实验。

## 1. 已确认事实、基线与范围

- 目标提交：`952809b157bcbd83f94a4d43ff6b5f666a2b2a0d`。开始时 main HEAD、origin/main 和真实 `git ls-remote origin refs/heads/main` 三者一致，暂存区为空。已读取实际根目录 [AGENTS.md](../AGENTS.md)。本次交付提交会前移父仓库 main，但实验源码始终绑定该目标提交的独立快照，不能自动改用交付后的新 HEAD。
- 新实验标识：`p65-second-freeze-952809b-20261019-02`；本地完整目录：[data/p65-second-freeze-952809b-20261019-02](../data/p65-second-freeze-952809b-20261019-02)。独立源码在 `tree/`，新计划、完整首轮请求、历史 package 及其 manifest 在 `prepared/`。新目录没有复用旧 prepared、旧授权或旧消费记录。
- 从 Git 对象 `cat-file` 直接导出 250 个文件，逐项校验 blob 身份及 SHA-256；包含源码、可信配置、依赖锁定、协议和相关测试。未从脏工作区复制实验代码。实际源码 manifest 同时记录 Git 对象 SHA、目标字节哈希、快照哈希及原工作区哈希，关系统计：`{"byte_identical": 159, "line_endings_only": 82, "missing_in_worktree": 6, "user_content_diff_excluded_from_experiment": 3}`。换行差异与实质内容差异分别标记。
- 用户原有 README、Prompt、temp_test、文档删除/归档、未跟踪文件及旧测试目录均保留。初始 5814 个受保护文件逐项复核，变化 0；原已删除的跟踪文件仍保持删除。本地 .env 只计算保护哈希，没有加载或打印凭据。缓存及 Git 元数据的排除范围写在 [baseline.json](../data/p65-second-freeze-952809b-20261019-02/baseline.json)。
- 本次不修改业务源码、公开接口、数据结构、配置、测试或依赖。复用现有 prepare/read_prepared、历史验证、审批校验及工具参数校验；新增的两个本地脚本只生成与校验准备材料，不提供执行入口。

## 2. 最新准入证据与历史记录复核

已读取 [最新最终准入报告](P6_5_FINAL_ADMISSION_VALIDATION_2026-10-19_18-28-50.md)，并逐项验证其 validation-manifest 的 33 个文件哈希，读取真实日志、进程退出记录和结构化 summary，确认针对目标提交的那次唯一完整回归为 **2296 passed、0 failed、2 skipped、8 deselected、190 subtests passed，退出码 0**。原 pytest 用时 749.09 秒，本阶段没有重新运行全回归、定向 pytest 或诊断循环。

最新报告 SHA-256：`1a2f3c7a5d19eb3c4813e5bab4814235e8b3a2a823da8b3108ae60ccf518f099`；原回归日志 SHA-256：`36ce6a86be7e98bd26a7c2cba894789fbfdc7ea3417db889da56039851b47632`。该报告原为未跟踪用户材料，本次不修改、不顺带提交。单次通过不证明所有锁竞争可靠；那次没有观察到 ready 前 FileLockError 重试事件，也未验证失败清理路径。8 项排除和 2 项 skip 不计通过，离线结果不证明真实 Provider 第二轮兼容。

已读取旧范围、旧计划、旧授权与消费原件，验证旧 live manifest 和原始响应完整性，并以 `RuntimeStore(read_only=True)` 读取旧 Session：`ses-41e8e4e92f8b445ea62085ba48d2883e` 仍为 FAILED，模型计数 1、工具计数 1。旧计划 `d9efc7591dc5ba70f5c62a4f9d454fff759628e19ab03685cf998c26b2f6d41d` 的消费记录仍存在；旧的三次额度不能减去一次后转给新实验。历史旧审查豁免不适用于新范围。

旧 scope 仍绑定 `6cf6f07044e1002991a9c264d4c97c3ccf2fb74c`；旧原始请求、回答、失败 Session、授权、ledger、manifest 和历史失败回归全未改写。[historical-review.json](../data/p65-second-freeze-952809b-20261019-02/historical-review.json) 保存关键原件哈希。历史 evidence 中的原故障注入授权只是当时实验设计证据，不是本次付费批准。哈希只证明本地一致性，不是 Provider 签名或计费证明。

## 3. 新范围与哈希绑定

| 对象 | 文件字节 SHA-256 |
| --- | --- |
| [实验范围](../data/p65-second-freeze-952809b-20261019-02/new-experiment-scope.json) | `77a6b5cc2973321becb2568636a328ebf8c234f77be0364b7a63c1545a5a1638` |
| [新计划](../data/p65-second-freeze-952809b-20261019-02/prepared/plan.json) | `685dc803754abf34b2ee8b93fea09824b37f8bb967e54f5b6fc33cd278f4e27c` |
| [完整首轮 SDK create 请求体（9040 字节）](../data/p65-second-freeze-952809b-20261019-02/prepared/first-request.json) | `825cfc43c6c4e60e1fcced85eea16d3680d94158170e11cbbb6c88f3efc6a79d` |
| [历史 evidence package](../data/p65-second-freeze-952809b-20261019-02/prepared/package.json) | `0c99f1bbdab631c2e04e7b41aa72c12d1a993c9067a139c664519a8e24b0c99a` |
| [prepared 三文件 manifest](../data/p65-second-freeze-952809b-20261019-02/prepared/sha256.json) | `e353a85179092ec2f0ce4df077dd64a164afd9242beff4247221510caf505bce` |
| [完整源码与协议 manifest](../data/p65-second-freeze-952809b-20261019-02/source-manifest.json) | `7e35d8c94c87d2dacc4a8573964ae83087bf2f1f8782a5f059abb59a8f7519c4` |
| [SDK manifest](../data/p65-second-freeze-952809b-20261019-02/sdk-manifest.json) | `97a5d9e57b5189797491c4424ca57369e19384c740ca317ed74a20a3135bcc1e` |
| [历史原件逐项清单](../data/p65-second-freeze-952809b-20261019-02/historical-originals-manifest.json) | `b3b5c13b963bead84b6890cb3eddb49ce4d3b6e3de789c52984e70c8ce574972` |
| [授权要求](../data/p65-second-freeze-952809b-20261019-02/authorization-requirements.json) | `69fa9bc2dcfefff30d393722db49befe3f19ae330e24f1a77cfa8468c72f61b3` |
| [离线检查](../data/p65-second-freeze-952809b-20261019-02/offline-checks.json) | `d7df733babec652ddbda6518577016241366f6d804862be29d2a34fc34ccf228` |
| [保护及准入复核](../data/p65-second-freeze-952809b-20261019-02/verification-summary.json) | `48ce850003a0cc8a0b99d9de90654fb4e6fdd72794e284303aafec3f637e5ab8` |

首轮哈希采用现有 `encoded()`：UTF-8、ensure_ascii=False、indent=2、allow_nan=False、末尾换行。完整文件是实际传入 SDK create 的 body；它不是尚未发送的线上 HTTP 原始字节。首轮与旧实验相同，故哈希相同；新计划新增目标提交、实验标识、完整源码 manifest 及快照路径绑定，计划哈希不同，旧授权经实际 validator 对新计划被拒绝。

现有 `read_prepared` 仅允许 package.json、first-request.json、plan.json、sha256.json 四文件，所以范围和补充 manifest 放在 prepared 外部。新增计划字段受 plan 整体哈希绑定；现有审批函数不单独校验新增 target/scope 字段，执行前仍须按本报告核验目标快照、scope 与实际人类消息，不能把文件存在视为许可。

## 4. 历史范围、查询参数与可外发内容

历史原件为 P6.2b 已封存的 `data/p62b-first-error-20261018-01`，根 manifest `350578d6914f7f09a530d85178f491da2366521b25b5f5fe4b5872bd58e5ae08`；本次复制 76 个文件到独立快照并逐字节复核，全部多层 manifest 及实际采集解析通过。package 含 7 个冻结工具观测：5 个指标、1 个日志、1 个版本。所有工具外层均为 no_data、historical_only=true、use_for_current_state=false；captured_result 才是原采集状态，原 no_data 不等于有效 0.0。

- query_metrics 仅限 service=order-service，metric 为 `http_5xx_rate`, `http_requests_total`, `http_request_rate`, `http_latency_p95_seconds`, `service_scrape_up`，按实际字典严格相等匹配，不添加时间字段或新指标。
- query_logs 精确参数为 service=order-service、start=`2026-10-18T11:52:54.430057+00:00`、end=`2026-10-18T11:53:34.815895+00:00`；只传 service 的唯一别名同样读取这一冻结行。它不是实时“最近一小时”，也不是自动改用采集窗口。等价时刻的不同字符串、单边时间、其他窗口均不匹配，返回 no_data 或拒绝，绝不访问实时 Loki。
- get_running_version 仅接受 service=order-service，回放历史缺失 commit 的结果；未知运行 SHA 不以本仓库 HEAD 填充。
- 故障窗口为 `2026-10-18T11:52:54.430057+00:00` 至 `2026-10-18T11:53:24.815895+00:00`，半开区间；采集窗口为 `2026-10-18T11:54:48.842673+00:00` 至 `2026-10-18T11:54:56.306126+00:00`，二者不能混用。冻结日志查询上界比故障结束晚 10 秒是原有采集设计，不是新扩大范围。日志内容时间与 Loki 时间的关联缺口保留在 context/known_gaps。

首轮外发为 TASK、历史任务/服务/来源标志、manifest 哈希、窗口、known_gaps、自动版本探针的完整冻结 envelope、预算和三种 schema。后续可外发的工具结果包含 status/service/reason、historical flags、captured_at/source、originals 的相对路径/哈希、captured_result 原采集内容、sanitized_projection、fault/collection windows，确切全部字段和值以 package.json 为准。

动态请求只能由此前批准的输入、真实验证通过的 Provider output（保持顺序，可能含 message/reasoning/function_call）、实际已提交且与 call_id/原调用/证据记录匹配的 function_call_output 组成。没有额外历史输入或实时补采。第三轮固定 tools=[]、tool_choice=none。不会外发整个源码树、.env、旧审批/ledger 或整包历史原件；本地字段与值检查不宣称能识别任意自然语言秘密，真正执行前还须用实际凭据值复查。

## 5. 实际调用链与确定性边界

已核对实际 SDK：指定 Python 3.12.14、OpenAI 3.14.0、httpx2 2.13.0、Pydantic 2.13.5，相关 SDK 实现与 Response/usage schema 的 8 项源码哈希见 sdk-manifest。SDK 默认 max_retries=2，但本实验 OpenAI 构造和每次 with_options 都显式为 0；检查本机 SDK 的超时、连接及状态码重试分支仅在 remaining_retries>0 时重试。没有实例化客户端或调用 SDK create。

授权后预定链路为：快照 `p65_readonly_investigation.run_prepared` → read_prepared → validate_approval → 独占创建新 plan 消费记录 → 读取凭据/构造固定端点 SDK → 隔离 Manager/RuntimeStore/IncidentStore → Worker.run_once → 自动版本工具 → Agent.request_durable_response → ModelClient.call_once → BoundedResponses.create → `responses.with_raw_response.create(**body)`。SDK 参数由 adapter 构造，不能只看 Agent 上游参数推测最终请求。

Worker 在模型/工具 I/O 前用 SQLite 事务预留累计预算并保存 checkpoint；模型响应先存 response_saved，工具结果和真实 output 提交后才允许后轮。`BoundedResponses._input` 校验实际 call/output 链、pending 清空、记录绑定，不合成缺失输出。最终 `_finish` 与 sync_incident 仍使用既有持久化规则；COMPLETED 不等于语义 ACCEPTED 或业务恢复。Worker.run_once 内部已有 `manager.recover()` 只对这一个全新隔离存储做对账，不启动 RecoveryWorker 后台流程。

三个只读 schema 只广告 order-service、拒绝额外属性；Worker 实际检查字段集合、required、类型及 Session 目标范围，replay 又检查 service 和完整历史参数/source，不能把 schema enum 单独当全部安全保证。全局 registry 只在隔离进程内缩减；requests、数据库、线程、子进程和修复路径被 guard 拦截。付费 SDK 使用 trust_env=false、follow_redirects=false，只允许 `https://api.deepseek.com/responses` 的请求入口；这不是 DNS/Provider 内部行为的端到端证明。

## 6. 预算、停止策略及未确认风险

固定上限：DeepSeek/deepseek-flash；SDK create 最多 3 次；工具执行最多 4 次（自动版本探针占用其中一次）；1 个隔离 Incident、1 次尝试、1 次 Worker.run_once。SDK 重试、应用重试、resume、fallback、人类工具调用均为 0。不启动生产 main.py、Scheduler、RecoveryWorker 或 Remediation；不产生新业务流量、故障、修复或业务恢复验证。

每轮 max_output_tokens=2048，三轮设置合计最多 6144；每轮 encoded 请求最多 131072 字节。字节不是 token，上述均不是货币硬上限。**30 秒 SDK timeout 不等于整个实验硬墙钟上限；Runtime 默认 3600 秒也只是安全点检查，不会硬中断在途 SDK。** 本轮没有增设或假称进程硬截止。

计划、源码、SDK、manifest、授权绑定不匹配或新 ledger 已存在，在 SDK 前停止。第一个 Provider/传输/协议/敏感检查错误终止并保留实际失败状态，不自动重试、resume、fallback 或换 Session。超限、末轮请求工具、未完成/空输出同样停止；no_data 保留为缺口，不补采。若模型直接给结论而未调用工具，保存原回答，但不能记作完成真实多轮目标，不因剩余额度重跑。失败后的原件、usage、响应与终态先封存，再独立审查；任何新尝试都需要重新审查和独立授权。

费用与 Provider 风险仍未知：本轮未查价、未读余额、未调用收费接口，不能给出已确认单价、余额或实际费用。后轮会重发增长的历史输入，最多 3 次可能付费；超时、HTTP 错误或客户端未收到回答仍可能计费。local SDK entry 不是服务端接收或扣费证明。已封存旧响应的 cache_write_tokens 缺失/null 离线兼容检查通过，只说明该字段可保留未知，不证明真实第二轮或其他字段兼容，更不能填零估价。

## 7. 新授权申请全文（申请，不是人类确认）

请求人工独立审查并决定是否批准下列唯一实验；此段不记录已批准，不包含伪造的用户签名或授权时间：

> 申请执行 P6.5 第二次冻结历史多轮调查，实验标识 `p65-second-freeze-952809b-20261019-02`，代码目标 `952809b157bcbd83f94a4d43ff6b5f666a2b2a0d`，仅运行已核对的独立 Git 字节快照。申请绑定 scope SHA-256 `77a6b5cc2973321becb2568636a328ebf8c234f77be0364b7a63c1545a5a1638`、plan SHA-256 `685dc803754abf34b2ee8b93fea09824b37f8bb967e54f5b6fc33cd278f4e27c`、首轮完整请求 SHA-256 `825cfc43c6c4e60e1fcced85eea16d3680d94158170e11cbbb6c88f3efc6a79d`、历史 package SHA-256 `0c99f1bbdab631c2e04e7b41aa72c12d1a993c9067a139c664519a8e24b0c99a`、P6.2b capture manifest SHA-256 `350578d6914f7f09a530d85178f491da2366521b25b5f5fe4b5872bd58e5ae08`，并绑定 scope 所列源码/SDK manifest。
>
> 申请允许向 DeepSeek 的固定 Responses 端点发送上述首轮历史字段，以及同一冻结范围内真实 Provider output 与已持久化匹配 function_call_output 构成的动态后轮输入。Provider 为 DeepSeek，模型为 deepseek-flash。只允许 order-service 的 query_metrics/query_logs/get_running_version，查询参数及省略日志边界别名严格按第 4 节。未匹配返回 no_data 或拒绝，禁止实时补采。
>
> 申请上限为 3 次 SDK create、4 次工具执行（含自动版本探针）、1 次 Worker.run_once；SDK/应用重试、resume、fallback 均为 0。失败、超时、协议错误、额度耗尽或不确定结果均封存并停止，不用旧授权余量、不自动重跑、不转入 P6.6。禁止生产入口、后台调度恢复、修复、新业务流量、故障注入及业务恢复验证。
>
> 请求审查人明确评估：最多三次潜在付费、动态历史输入增长、超时仍可能计费、价格/余额/实际费用未知、30 秒不是实验硬墙钟，以及单次回归/Provider 兼容与历史证据的已披露局限。批准只可来自后续真实人类明确决定；当前申请不表示已接受这些风险，不沿用旧独立审查豁免，不授予结果语义 accepted 或修复权限。

新 scope 的 explicitly_authorized=false、authorized_at=null、human_confirmation=null、review_status=pending。没有创建当前授权文件或新 ledger。未来只能在真实批准后记录原文和带时区时间，并调用既有校验；不预填写用户授权正文或批准时间。

## 8. 真实执行前必须重新完成的检查

1. 独立审查本报告、scope、plan、完整首轮和 package；取得明确绑定上述哈希的新人类批准。旧授权及其豁免仅属历史。
2. 重查父仓库 HEAD、origin/main、真实远端及工作区。允许仅交付准备材料的后续提交，但执行源码仍必须是目标 `952809b157bcbd83f94a4d43ff6b5f666a2b2a0d`；逐项复核 250 个快照文件与 plan 的 18 项 code_sha256。若源码要改，先重新冻结和申请。
3. 复核 scope/plan/prepared/package/capture/完整源码/SDK 哈希及严格文件集合，确认实际模块从该快照导入。`read_prepared` 对全树的额外校验由本项补足，不拿同一 HEAD 替代它。
4. 复核实际解释器、SDK 版本与源码、模型、端点、max_retries=0、30 秒、输出/字节上限、1/3/4/0 边界，以及无 main/Scheduler/RecoveryWorker/Remediation/live 工具旁路。
5. 仅在批准后检查凭据存在性和实际值外发检测，核实账户能承担明确接受的费用风险。若需要金额硬上限或硬墙钟，应先单独设计和授权，不能把当前配置当作已经具备。
6. 确认全新 output 不存在，快照中消费路径 `D:\Incident Agent\data\p65-second-freeze-952809b-20261019-02\tree\data\p65-paid-consumption\685dc803754abf34b2ee8b93fea09824b37f8bb967e54f5b6fc33cd278f4e27c.consumed.json` 不存在。该路径不同于父工作区旧 ledger；不得预建、删除或复用。由现有 xb 逻辑在执行时独占预留，即使失败也不自动释放。
7. 只执行一次隔离 run_once，冻结每次实际请求、原始响应、SDK parse、usage、checkpoint 与终态；停止后审查真实第二轮是否成立、语义是否支持。不得用 Mock、旧响应回放、COMPLETED 或 HTTP 200 代替成功证明。

## 9. 本阶段实际验证与完整清单

实际运行：`freeze_offline.py` 一次完成源码导出、旧原件复核、prepare/read_prepared、实际 Response schema 和审批拒绝校验；7 项精确观测匹配、1 项省略日志边界别名、4 项越界 no_data、5 项非法参数/工具拒绝（offline-checks 中列 16 项，别名单独断言）。工具回放测试使用目标源码 AST 中原样提取的 nested replay，并直接调用现有 Worker._validate_tool；**未运行 Worker、Agent 调查、Mock SDK create 或真实 SDK create**。预算使用实际 RuntimeLimits 和确定性源码检查，没有冒称本轮跑过预算执行回归。

准备进程中 SDK 构造、网络、业务请求、子进程/线程均被阻断，forbidden_attempts 为空。后续材料终结仅进行文件哈希、JSON/链接及清单核对。新全回归 0、诊断循环 0、真实模型 0。最新准入结果是第 2 节核实的既有实测结果，不是本轮新测试。历史和用户保护清单差异为 0。

完整新增文件逐项列表见 [本地 file-manifest.json](../data/p65-second-freeze-952809b-20261019-02/file-manifest.json)，可提交的同字节索引见 [review FILE_INVENTORY.json](P6_5_SECOND_EXPERIMENT_FREEZE_952809b_02/FILE_INVENTORY.json)。两份清单列出自身但将自引用哈希置 null，避免自哈希循环；其余每项均有实际字节数和 SHA-256，清单根哈希由最终交付回复给出。清单和摘要都绑定目标提交。

提交范围仅为本报告与 `docs/P6_5_SECOND_EXPERIMENT_FREEZE_952809b_02/` 的静态范围、计划、源码/SDK manifest、授权要求、离线校验和完整清单。完整请求、package、历史副本、日志、原始模型回答、SQLite 及 ledger 保留于被忽略的本地 data，不把运行数据加入 Git。授权申请和完整本地材料均可独立审阅，但远端静态文件不能替代审查本地首轮/package 实值。

**当前仍未获许可：只获得离线准备及提交推送授权；新范围尚待独立审查与明确人类批准。到此停止，等待审查，不自动真实执行或进入 P6.6。**
