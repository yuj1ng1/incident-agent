# P6.5 第三次真实 DeepSeek 多轮实验执行报告

**回答：是。本次在已封存的前三次 P6.5 实验中，第一次完成真实 DeepSeek 两轮请求；第二轮 SDK 请求携带了与 Worker 已持久化记录、发出前 Checkpoint 和 SQLite 一致的全部 4 组 function_call_output，Provider 接受请求并返回最终回答。**

实测为 **2 次真实 SDK create、1 次 Worker.run_once、1 个 Incident、1 次 attempt、5 次历史只读工具执行（自动版本探针 1 次、模型工具 4 次）**。未使用第三轮，没有重试、resume、fallback、重新实验或 P6.6。Session=COMPLETED，Incident=investigating，ResultTrust=trusted=false / pending_review；不构成根因、业务恢复或最终回答语义的认可。

这里的 Provider 接受和完整输出绑定，以本地真实 SDK 调用边界、真实 Provider 原始响应和持久化一致性共同证明；没有 Provider 服务端请求日志或本次网络抓包，不能声称完成服务端逐字节独立审计。下述请求 SHA 均为 encoded(body) 的规范化 JSON 哈希，不是 HTTP wire 字节哈希。

## 1. 授权、范围与实际入口

- 实验：`p65-third-tools5-241dca6-20261021-01`；协议：`p65_dynamic_runtime_v3_tools5`。
- 执行提交：`241dca6353e6bd8aff8db50dfd74a7a9bed1949a`。只从 [冻结 tree](../data/p65-third-tools5-241dca6-20261021-01/tree) 执行，未采用当前工作区源码。
- Python：`D:\conda\envs\agentlearn\python.exe`，3.12.14；OpenAI 3.14.0、httpx2 2.13.0、Pydantic 2.13.5。DeepSeek `deepseek-flash`，固定 `https://api.deepseek.com/responses`。
- 用户本任务明确确认已阅读范围和费用风险并授权；原确认文本按原样保存为 **“我授权”**。[新授权记录](../data/p65-third-execution-241dca6-20261022-01/authorization.json) SHA 为 `f723cd633c707742a46e54447ea7ec51638041a505a1ad2cf865f42871a8b383`。
- 原 ChatGPT 消息时间未知，记录为 null / unknown。记录实际创建时间为 `2026-10-22T02:29:58.965981+00:00`（北京时间 10:29:58）；既有 validator 所需 authorized_at 也采用这个记录时间，并显式注明它不是原消息时间。没有伪造原授权消息时间。
- 授权绑定完整新计划、prepared plan、首轮请求、历史 package、源码/SDK/历史/capture manifest、1/1/1/3/5 上限、零重试/恢复/修复、已批准的历史字段及同范围真实动态后轮输入，以及披露的费用不确定性。授权只用于这次冻结实验，不是语义接受、Remediation 审批或长期 API 授权。

独占新消费 ledger 已由原 CLI 创建并保留，失败也不释放：[消费记录](../data/p65-third-tools5-241dca6-20261021-01/tree/data/p65-paid-consumption/eb3b063ecb5e783f977c6890b40ba4ee09b70c561f80a6c3dc37a1be80b5ac76.consumed.json)。旧授权、旧消费记录和旧 EXHAUSTED Session 未复用。

实际唯一子进程命令如下；[启动记录](../data/p65-third-execution-241dca6-20261022-01/execution-started.json)和[退出记录](../data/p65-third-execution-241dca6-20261022-01/execution-finished.json)保留具体参数，exit_code=0、execution_processes_started=1。

```powershell
# cwd: D:\Incident Agent\data\p65-third-tools5-241dca6-20261021-01\tree
& 'D:\conda\envs\agentlearn\python.exe' -B -m scripts.p65_readonly_investigation --execute --prepared-dir 'D:\Incident Agent\data\p65-third-tools5-241dca6-20261021-01\prepared' --authorization 'D:\Incident Agent\data\p65-third-execution-241dca6-20261022-01\authorization.json' --output-dir 'D:\Incident Agent\data\p65-third-live-tools5-241dca6-20261022-01'
```

凭据由主工作区既有配置只读加载至该子进程环境，没有复制进冻结 tree，没有进入模型请求或报告。只允许 query_metrics、query_logs、get_running_version；目标仅 order-service。未启用 --mock 或 --offline-tool-budget。

## 2. 执行前独立准入

实际阅读根目录 AGENTS.md，核对现有调用链及请求边界代码。预检时本地 main、HEAD 和 git ls-remote 得到的远端 main 均为 `613a7dcf2a372693ae93624c6a5066ecb1a64964`；暂存区为空，工作区存在用户原有修改，均未用于执行。

独立预检和执行前紧邻复查均通过：263 个源码文件与目标 Git tree 的完整文件集合、Git blob、长度和 SHA-256 一致；18 个执行关键文件匹配；76 个历史原件与独立副本匹配；各层 seal、第二次 raw/SDK/授权/消费记录及 EXHAUSTED Session 保持不变；prepared 恰为四文件且 seal 完整；首轮完整 body 与批准值相同；解释器、安装版本及 8 项 SDK 源码哈希一致。实际 Agent、Worker、ModelClient、registry、adapter 及项目导入均来自冻结 tree。

预检还确认 3 次模型、5 次工具（探针计 1 次）的上限及三个历史工具白名单，重试/resume/fallback 为 0；新输出与消费路径不存在；敏感字段/已知凭据值外发检查通过。独立预检阻断网络、SDK 构造与后台线程，真实 SDK create=0。正式运行沿用原脚本对业务 HTTP、Prometheus/Loki、子进程、后台线程、Remediation 和 Recovery 的确定性阻断。原计划完整外发范围和风险说明未改变。

| 已批准对象 | 实测匹配 SHA-256 |
| --- | --- |
| `new-experiment-plan.json` | `413188aa386674e5ffd6d3663caeae25eb70822dd2771cb295fe8977bfb5584c` |
| `prepared/plan.json` | `eb3b063ecb5e783f977c6890b40ba4ee09b70c561f80a6c3dc37a1be80b5ac76` |
| `prepared/first-request.json` | `d9cfac4386a1adfc2d565820e70bc03fc910cdf987020ab168df5481f2ff76a1` |
| `prepared/package.json` | `0c99f1bbdab631c2e04e7b41aa72c12d1a993c9067a139c664519a8e24b0c99a` |
| `source-manifest.json` | `a241177ea5a244d6b46ec5270f2a836fad1aa01cc51e63ec50f08923d1f35103` |
| `sdk-manifest.json` | `d3c3c87fe5dd31af060e8116f30db410c48f1d767fd5d91f7ef12ab8703803d9` |
| `historical-manifest.json` | `44e2427c3cabe2d85b421b1afb18d49e374bd5264ba2bb2684ffb6bccc5192ce` |
| `tree/data/p62b-first-error-20261018-01/sha256.json` | `350578d6914f7f09a530d85178f491da2366521b25b5f5fe4b5872bd58e5ae08` |

完整实测：[准入结果](../data/p65-third-execution-241dca6-20261022-01/preflight-result.json)、[预检脚本](../data/p65-third-execution-241dca6-20261022-01/preflight.py)；prepared seal 也匹配完整计划中的 `0402b1756a4b54b64d3b5075c86d3447c3328c51077c024e6d5b65c59885b5fa`。

## 3. 两轮真实交互与工具输出绑定

正式执行实际时间为 **2026-10-22 北京时间 10:30:11.995824–10:30:24.933394**。两轮 completion 的 response_origin 均为 live_provider，原始 Provider status 均为 completed，error 均为 null。没有用旧实验响应或 Mock 生成本次 function_call。

| 轮次 | 完整 body 字节数 | body SHA-256 | 剩余工具 | 工具 schema 数 / tool_choice |
| --- | --- | --- | --- | --- |
| 1 | 9657 | `d9cfac4386a1adfc2d565820e70bc03fc910cdf987020ab168df5481f2ff76a1` | 4 | 3 / auto |
| 2 | 15542 | `8a883eade57e151a4300c7670f0fc58b3ffc31943fc873b895d0c6b5c89a6088` | 0 | 0 / none |

第一轮 response_id=`0189f1ba-c066-42b6-ae5b-32d61a0104e5`，Provider 自主返回一条说明消息及以下四个 schema 合法的函数调用。自动版本探针先提交，首轮 remaining_tools=4。原始顺序为日志、5xx 率、请求总数、p95 延迟。

| 顺序 | 工具及参数概要 | 原始 call_id | 已提交 observation_id | captured_result.status |
| --- | --- | --- | --- | --- |
| 1 | `query_logs` / 指定历史日志时间 | `call_00_KKArzu43KJ8RWfUA095U7412` | `obs-b85b845904624a9aacaed4db6e740419` | 未命中冻结结果 |
| 2 | `query_metrics` / http_5xx_rate | `call_01_QsrDcUCZWYh4ZFxGOvO45102` | `obs-701dd96e25504d37aef28c9fdec6b0e8` | no_data |
| 3 | `query_metrics` / http_requests_total | `call_02_Xsad8ng4Cgb3HYiQVVlB5122` | `obs-7f93850560514d7cab4f71d54bb4860d` | ok |
| 4 | `query_metrics` / http_latency_p95_seconds | `call_03_jgfbKdtHN7UQgQhEecvG3549` | `obs-86a69a03d7b64323b91de67d2305d150` | no_data |

4 次工具均真实进入本地历史 replay 并提交；**日志调用参数未命中批准历史范围，返回受控 no_data；并非四次都命中了历史采集值**。其请求 start=`2026-10-18T11:52:00Z`、end=`2026-10-18T11:54:00Z`，冻结参数实际为 start=`2026-10-18T11:52:54.430057+00:00`、end=`2026-10-18T11:53:34.815895+00:00`。依冻结计划允许的确定性 no_data/rejection 分支处理，不查询实时 Loki，不改参数、不捏造历史日志输出。

探针 + 四次模型工具的外层 status 均为 no_data，Runtime 相应记录 5 次 tool_reserved、5 次 tool_failed；后者在本次表示 no_data 的非成功证据语义，不是五次网络错误。http_requests_total 的内层 captured_result.status=ok，历史值 13.0；不能因此将外层提升为当前有效观测，其余指标原采集 no_data 与日志范围拒绝也不等同于有效零值。

第二轮请求严格为：原首轮 input + 完整原始 Provider output（包括 message 和 4 个 function_call，字段、arguments 原字符串及顺序不变）+ 4 个逐一绑定的 function_call_output。每个 output 的 JSON 值与原 call_id/name/arguments 对应的 Worker record.result 相等；字符串本身与请求发出前 Checkpoint.history 保存的 output 相同；pending=[]。

第二轮预请求 Checkpoint ID 为 `cp-03653cbd170841ccbbf0009c82e35d42`，SQLite 仍保留该记录，其规范化 SHA 为 `8c21d956950d466e9a87ce839d85378d47b97c76463c670b1820badee8c06f0f`，与 request-02.checkpoint_sha256 一致。最终 SQLite 的 5 个 record 也逐项一致。请求二的 Python 权威预算为模型预留 2、已提交工具 5、remaining_tools=0、remaining_rounds=2、tools=[]、tool_choice=none。

第二轮 response_id=`4d278fcd-3fb1-4688-87fa-73bf4590646f`。Provider 接受后返回完整中文最终回答，没有返回 function_call，正常完成调查循环；未发第三次请求。两轮 raw 原文与 integrity SHA 匹配，credential_redacted=false；未修改原始 Provider 返回。cache_write_tokens 仍是 raw 未报告、SDK null，沿用冻结代码已有的局部兼容，不补零。

证据：[首轮请求](../data/p65-third-live-tools5-241dca6-20261022-01/request-01.json)、[首轮 raw](../data/p65-third-live-tools5-241dca6-20261022-01/response-01.raw.txt)、[第二轮请求](../data/p65-third-live-tools5-241dca6-20261022-01/request-02.json)、[第二轮 raw](../data/p65-third-live-tools5-241dca6-20261022-01/response-02.raw.txt)、[Worker 边界](../data/p65-third-live-tools5-241dca6-20261022-01/worker-boundaries.json)、[只读机械核验](../data/p65-third-execution-241dca6-20261022-01/mechanical-verification.json)。

## 4. Runtime、SQLite 与信任终态

| 项目 | 实际结果 |
| --- | --- |
| Session | ses-2e66961afb0a40e9b5462c40c6c5341b / COMPLETED |
| Incident | inc-0cae7118 / investigating |
| Worker / attempt / Incident | 1 / 1 / 1 |
| 实际 SDK / 模型轮数 | 2 / 2 |
| 工具 / 缓存命中 | 5 / 0 |
| 重试 / resume / fallback / 人类请求 | 0 / 0 / 0 / 0 |
| 第二轮完整输出 / pending | 4 / [] |
| 最终 Checkpoint | cp-9e43f4ed019a45b18e0eb1c7d880c30c |
| SQLite | mode=ro&immutable=1，integrity_check=ok；终态 Session 和 Checkpoint 与 summary 相同 |
| ResultTrust | mechanical_status=passed，mechanical_scope=result_schema_only；semantic_status=pending_review；trusted=false；human_approval=not_recorded |
| provider_verified 生产开关 | false，未因本次局部协议通过而提升 |
| 当前业务健康 / 恢复 | 未验证，business_recovery_verified=false |

SQLite 核对出 1 个 Session、1 次 worker_claimed、1 次 attempt_started、2 次 model_reserved、2 次 model_response_saved、5 次 tool_reserved、5 次 tool_failed、1 次 investigation_completed 和 23 次 checkpoint_saved。Runtime 的完成与 Incident 的业务状态分开保留，未人为改写成 resolved 或 recovered。

本次实验执行授权已经保存；ResultTrust 中 human_approval=not_recorded 指最终调查语义/正式审批没有记录，不是否认本次运行授权。

## 5. 最终回答及待独立审查问题

[最终模型回答原文](../data/p65-third-live-tools5-241dca6-20261022-01/answer-original.txt)已逐字核对第二轮 raw 输出与 Checkpoint.result.answer，SHA-256=`ad84a451320c641e02c083169ac9f54fb8b85cd449bc5c3e914c8eed6f6bd4f8`。原文未改写、润色、纠错或替换。它保留了“历史回放不能判断当前健康”“运行 commit 未知”“不能从总请求数推断独立故障计数”等限制，没有给出已验证根因或业务恢复结论。

但本次不能把机械验收通过写成语义验收通过，至少以下内容需独立审查：

1. 回答把“冻结历史证据”进一步解释成服务冻结、进程停滞或阻塞，并围绕是否发生“冻结”作结论。任务要求调查冻结材料，未给出服务冻结事实；这一表述疑似语义偏题，不能作为本实验确认的事件。
2. 部分指标来源使用了 sanitized_projection 中的 p63:metric-* 标识，未按 TASK 逐项引用本轮真实 function_call 的 call_id。引用字符串存在不代表满足实际调用归因要求，更不代表陈述已被语义核验。
3. 日志参数被模型主动扩大到未批准历史范围，实际得到的是范围拒绝 no_data。不能把它描述成批准范围的原始历史日志已经被成功读取，也不能据此断言无故障日志。

这些是执行报告中列出的复核点，没有据此写入 accepted/rejected，没有把本 Agent 的观察当成人工认可。语义正确性、因果链和根因保持 pending_review；等待独立审查。

## 6. Provider usage 与费用未知项

| 轮次 | input_tokens | output_tokens | total_tokens | cached_tokens | reasoning_tokens |
| --- | --- | --- | --- | --- | --- |
| 1 | 2501 | 266 | 2767 | 0 | 0 |
| 2 | 4517 | 1012 | 5529 | 0 | 0 |
| 合计 | 7018 | 1278 | 8296 | 0 | 0 |

来源为两轮真实 Provider 返回及 SDK 解析；cache_write_tokens 未报告/未知，不以 null 当零。未查询余额、价格或账单，实际货币费用、计费状态和 Provider 账单均未知。SDK create 计数表示本地进入 SDK；本次两轮确有真实返回，但不等于费用核对。30 秒 SDK timeout 和 Runtime 协作时限均不宣称为实验硬墙钟；本次没有超时。

## 7. 验收脚本失败、补充核验及原件保护

第一次只读 postflight 在 output_not_in_committed_history 处失败：[原脚本](../data/p65-third-execution-241dca6-20261022-01/postflight.py)、[失败记录](../data/p65-third-execution-241dca6-20261022-01/postflight-failure.json)均保留。原因是验收脚本错误要求最终 history 继续保留先前所有 function_call_output；现有 Worker 在 provider_verified=false 的 B 策略下构建后轮 context，并在保存回答时重建 history。最终 history 只有重建 user context 和最终 message，但 5 条 records 不丢失。

随后只读 [补充核验脚本](../data/p65-third-execution-241dca6-20261022-01/postflight_supplement.py)改在**第二轮发出前、已提交且仍保留于 SQLite 的 Checkpoint**核对完整 history/输出及请求 hash，同时独立核对最终 records/result 和终态 SQLite。此核验全部通过，前述初次失败不被隐去。没有重启 Worker，没有发新 SDK 请求，没有修补冻结材料、原始响应或 SQLite。

执行前后 7097 个既存工作区/数据/日志/凭据文件的 SHA、长度及 mtime_ns 对比无变化，用户原有 Git 状态相同。冻结目录唯一新增文件为本次专用消费 ledger；原 263 文件源码、76 文件历史副本、prepared、旧授权/消费/Session 和所有已有输出不变。新输出封印在核验前后都匹配，已知 API 凭据值对新原始文件、日志、授权、验收结果及 ledger 扫描通过。

证据：[原始封存核验](../data/p65-third-execution-241dca6-20261022-01/raw-freeze-verification.json)、[补充封存核验](../data/p65-third-execution-241dca6-20261022-01/raw-freeze-verification-supplement.json)、[保护结果](P6_5_THIRD_REAL_EXECUTION_241dca6_01/protection-result.json)、[保护前哈希](../data/p65-third-execution-241dca6-20261022-01/protection-before.json)、[保护后哈希](../data/p65-third-execution-241dca6-20261022-01/protection-after.json)。完整本地路径/字节数/SHA 见[文件索引](P6_5_THIRD_REAL_EXECUTION_241dca6_01/LOCAL_ARTIFACT_INVENTORY.json)。

业务 HTTP、实时 Prometheus/Loki、故障注入、Remediation、后台恢复的实际执行均为 0，forbidden_io_attempts=[]；没有开启测试故障开关或新业务测试资源，不存在本次需要恢复的故障注入。工具仅回放历史采集值，不能作为当前业务观测。

## 8. 验证范围、改动与交付边界

本次运行了独立准入、唯一一次真实 DeepSeek/Worker 集成实验、原始响应及请求/工具/Checkpoint 绑定核验、SQLite 只读完整性与终态核验、敏感值扫描和执行前后保护比对；首个验收脚本失败及通过的补充核验如实保留。**本次没有运行新的单元/Mock/全仓回归，不把准备阶段的离线测试当作本次真实多轮结果。**

重新核对既有前两次实验的当前原始 summary：第一次 SDK=1、Session=FAILED、无 request-02；第二次 SDK=1、Session=EXHAUSTED、无 request-02。因此“第一次成功完成真实两轮”仅指本项目这三次封存实验的可核对记录，不声称其他环境或未留存实验的历史。

生产源码、公开接口、数据结构、依赖、预算、工具权限及审批系统均未修改。复用冻结提交已有 CLI、Agent、Worker、ModelClient、registry、历史 replay、RuntimeStore、Checkpoint 和 ResultTrust；本轮新增本地授权/验收脚本与运行证据、中文报告及静态核验元数据，没有新依赖。

按 AGENTS.md 的真实运行数据保护规则，原始请求/响应、最终回答、SQLite、日志、授权、消费记录和保护基线留在忽略的本地 data，**不加入 Git**。本次仅提交本报告与静态核验元数据，保留用户既有未提交修改；实际提交首行、SHA 和远端一致性在最终交付回复中报告。[静态验收摘要](P6_5_THIRD_REAL_EXECUTION_241dca6_01/verification-summary.json)只记录本次核验结论及边界，不改变任何 Runtime 状态。

执行已停止。剩余模型额度不继续使用，不恢复本 Session，不启动新实验或 P6.6；报告完成、提交推送后等待独立审查。
