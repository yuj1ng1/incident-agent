# P6.2a 授权后正常业务证据验证

## 1. 实际结果与授权沿革

本轮已建立 **8 条有完整原件的正常业务响应，与真实 Prometheus、Loki 和隔离 Runtime 回放之间的证据关联**。执行器在 60 秒截止终止自身 helper，未完成计划上限 30 次，未重跑或追加流量。其原始 supervisor 计数仍为 **null/unknown**，不能将“8 条已确认响应”替换为“完整执行总数 8”或“30 次成功”。

5xx 比率和 P95 仍为 no_data；运行 SHA 未知；因此本次只是有边界的部分业务基线，**没有通过 P6.2b 故障实验或完整正常错误率/延迟基线的准入**。没有 DeepSeek/其他真实模型调用、故障注入、服务重启或 Remediation。

先前 [授权前交付快照](P6_2A_BUSINESS_BASELINE_VALIDATION_2026-10-18_16-34-56.md) 如实记录当时 0 次业务请求，已以 `5fe9ff5678e0ff6ae7f6dbbc2f599aeec50c417f` 提交推送。随后用户在本聊天对独立授权问题回复“明确授权上述副作用及流量上限”。本报告记录授权后的新增事实，保留前述快照供审查授权先后，不能将新授权追溯到之前阶段。

授权原件：[business-authorization.json](../data/p62a-20261018T075858Z-719154a6/business-authorization.json)。范围为唯一一批固定本地 GET、最多 30 次、60 秒截止，包含 PostgreSQL SELECT、请求/依赖指标更新及日志追加；异常即停，不包含故障、付费模型、重启或修复。

## 2. 实际环境、路由副作用与前检

Incident Agent 初始 SHA 为 `f077ac3a58c5c462d3499f2744554f19cf61c718`，真实 origin `https://github.com/yuj1ng1/incident-agent.git`，main；本次执行采用上述已推送准备提交，生产源码没有进一步修改。Demo 为 `D:\prometheus-demo`、分支 `codex/postgres-monitoring`、HEAD `0e2f6f3220b9fb9f0baf8730de5ffaaac00d8c38`、origin `https://github.com/yuj1ng1/prometheus-demo.git`。解释器为 `D:\conda\envs\agentlearn\python.exe`。

授权后再次核实 8001 归属 PID 29476，命令行 `python.exe -u d:\prometheus-demo\app.py`；HTTP 启动身份 `2026-10-18T03:31:32.744052Z` 保持一致。两个 simulate 开关均 false，commit_sha=null。order-service target up，实际 scrape interval/timeout 均 5 秒；postgres_up、exporter_scrape_up 均为 1。告警只有既有 inventory-service ServiceDown；8004 的 uvicorn/交互 Python 仍存在、用途未完全确认，未观察到 active pytest 或其他 order 流量，不能据此声称无任何其他活动。

前检原件：[authorized-pre](../data/p62a-20261018T075858Z-719154a6/authorized-pre/)，31 项清单校验一致。前检业务 Counter/Histogram/dependency 样本仍缺失；原 app.log 长度仍为 900127、最后写入 10 月 12 日。未把 Health=ok 或 target up 当作业务验证。原非回环端口绑定未改变，不能宣称网络全局隔离。

实际源码已审查：`/order` 每次连接 PostgreSQL，执行 SELECT，更新 dependency Gauge、Counter/Histogram，并追加日志；可能因真实数据库/应用错误返回 500。它不是零副作用接口。本次响应的非敏感结构确认为顶层 service/dependency/order，order 字段名 id/item/quantity；行值只在本地原件中保存，不写入本报告或提交。

## 3. 流量边界与截止执行

入口 [p62a_business_baseline.py](../scripts/p62a_business_baseline.py) 默认离线，授权后仅执行固定 `GET http://localhost:8001/order`。串行、循环间隔至少 1 秒；单次 connect/read 超时 2/3 秒；禁重试、跳转、环境代理/认证/cookie、压缩，响应上限 256 KiB。每次业务前后核对 `/version` 身份与两个开关。P6.1 白名单保持不变。

supervisor 从 `2026-10-18T08:45:53.095222Z` 开始，`08:46:53.121644Z` 完成回收，总耗时记录约 **60.016 秒**；子进程 PID 43708 已被其父监督进程终止并等待退出。当前没有残留本入口 helper，Demo PID 保持 29476。

原始状态为 deadline_exceeded、child_killed_by_supervisor=true、originals_may_be_incomplete=true、business_route_requests=null、count_status=unknown。没有子进程最终 traffic_report.json，不能伪造补写它。普通操作系统并非硬实时环境，截止触发终止之后回收/报告写入可稍晚；终止客户端不能撤回服务端已接收的请求。

本轮没有再次使用剩余额度，也没有为获得 20 个样本延长截止。完整落盘 HTTP 共 24 条，其中业务 8 条、版本 16 条，均 200；截止附近可能存在未完成落盘的请求，原始总数继续保持 unknown。下一批业务流量需要新的单独授权。

## 4. 已确认业务响应原件

下表均为 2026-10-18 UTC，来源固定为上述 /order；耗时取 monotonic 记录，原 `.json` 保存完整 UTC、状态、来源、正文完整性及 SHA-256。

| 请求号 | HTTP 原件 ID | 开始 UTC | 完成 UTC | 状态 | 耗时秒 |
| --- | --- | --- | --- | --- | --- |
| 1 | http-002 | 08:45:56.022774 | 08:45:58.180906 | 200 | 2.156 |
| 2 | http-005 | 08:46:03.218606 | 08:46:05.251498 | 200 | 2.031 |
| 3 | http-008 | 08:46:10.296854 | 08:46:12.337596 | 200 | 2.032 |
| 4 | http-011 | 08:46:17.390615 | 08:46:19.485552 | 200 | 2.094 |
| 5 | http-014 | 08:46:24.528827 | 08:46:26.565038 | 200 | 2.047 |
| 6 | http-017 | 08:46:31.958305 | 08:46:33.998564 | 200 | 2.031 |
| 7 | http-020 | 08:46:39.048219 | 08:46:41.082968 | 200 | 2.032 |
| 8 | http-023 | 08:46:46.120216 | 08:46:48.157307 | 200 | 2.047 |

原件位于 [authorized-traffic/traffic](../data/p62a-20261018T075858Z-719154a6/authorized-traffic/traffic/)，每次响应摘要通过 source_request_id 对应 `.json/.body`。只根据已持久化原件确认这 8 次响应，不擅自修订监督进程未知计数。所有版本原件的身份/开关一致。

客户端业务耗时约 2.031–2.156 秒，版本探测也约 2 秒；后者不访问数据库。**耗时是已确认事实，其来源待验证**，不能直接归因 PostgreSQL、代码回归或业务慢请求，也不能为此自动注入故障或调整配置。

## 5. Prometheus 实际指标、窗口及零值

现有指标标签为 service/status，无 path/request_id；5xx/P95 采用 1m 窗口、increase>=20 和 scrape up 门槛。前检缺失样本不是数值 0，不能把后续 8 写成“由 0 精确增长到 8”。本次没有为绕过门槛放宽 profile 或伪造值。

| 指标 | 业务附近历史评估 08:46:56.157000Z | 较晚评估 08:48:43.834000Z |
| --- | --- | --- |
| http_requests_total | ok，8.0 | ok，8.0 |
| http_request_rate | ok，0.12728429857259752 请求/秒 | ok，**0.0** |
| http_5xx_rate | no_data，no_samples_at_evaluation_time，无 value | 同左 |
| service_scrape_up | ok，1.0 | ok，1.0 |
| http_latency_p95_seconds | no_data，no_samples_at_evaluation_time，无 value | 同左 |

业务附近评估时间取最后已确认响应完成时间 +8 秒，再由现有 aligned 工具向下对齐毫秒；实际查询执行于 `08:54:49Z`，原件分别保存 evaluation_time、evaluated_at、query_started_at/finished_at。较晚快照发生在流量结束约 115 秒后，rate=0.0 是停止流量后的真实窗口零值，不能代表流量期间速率、正常错误率或健康。

五项指标按原工具限制分 3+2 两组；相同评估时间不保证相同 scrape timestamp 或事务原子性。两次都是 time_window/use_for_current_state=false 的历史关联证据。对 5xx/P95 没有样本时不存在合法 evaluated_at，未补造时间或 0。

原件分别见 [authorized-traffic-time](../data/p62a-20261018T075858Z-719154a6/authorized-traffic-time/) 和 [authorized-window](../data/p62a-20261018T075858Z-719154a6/authorized-window/)。后检 [authorized-after](../data/p62a-20261018T075858Z-719154a6/authorized-after/) 同时保留 /metrics、target、版本、健康和现有工具输出。

## 6. Loki 与请求时间关联

明确 LogQL `{service="order-service"}`，范围 `[2026-10-18T08:45:54.022774Z,2026-10-18T08:48:43.834122Z)`。HTTP 200、API success，工具 status=ok，log_count=8、truncated=false、out_of_range_count=0；均为本次日期的 `/order status=200` 日志，不是之前查到的 10 月 12 日旧日志。

将日志正文的本机北京时间换算 UTC 后，其秒级完成时间与上表 8 条请求依次相符；Loki 时间反映稍后的读取/摄取，例如第一条为 `08:45:58.405557131Z`、最后一条为 `08:46:48.178236911Z`。数量、内容与时间范围支持本轮请求关联，也证明了本次追加日志经 Alloy 到 Loki 可查询。

配置仍为 Demo 日志目录只读挂载至 Alloy `/var/log/demo`、source.file 直接转发 Loki，没有新增时间解析 stage；必须同时核对文本时间和 Loki 时间。日志缺少共享 request_id，指标也无 path，所以不宣称具有逐请求强身份或一对一因果证明。

原始日志响应为 authorized-window/http-006.body；日志及指标 11 项原件重解析检查、8 条业务结构/哈希/时间关联见 [authorized-verification.json](../data/p62a-20261018T075858Z-719154a6/authorized-verification.json)。此核验离线封禁真实网络，未重新请求业务接口。

## 7. Agent 工具、隔离持久化与 Mock 解释

真实 HTTP 请求与 Agent 调查分开保存。P6.1 真实采集器调用现有 query_metrics/query_logs/health/version Provider；新增离线 verifier 对四主工具重解析一致、补充观察校验来源/参数/哈希。Runtime 不重新请求业务，只回放这些捕获结果。

授权后 [隔离回放](../data/p62a-20261018T075858Z-719154a6/authorized-replay/report.json)：Session `ses-aa92851c21bb4f86a6a18f320a442efb`，Incident `inc-4441c22f`，`08:55:37.303902Z–08:55:43.221180Z`；2 轮脚本化 Mock SDK、4 条受控工具记录，COMPLETED。真实模型请求 0 次；没有生产后台组合或 Remediation。

四条历史记录均保留 captured_result/captured_at，并以外层 no_data、time_window、use_for_current_state=false 阻止回放续期。真实日志原件中的 8 条记录仍保存，不能因外层隔离状态丢掉或改写。Checkpoint/函数调用链/预算 reservation/重开 SQLite 校验通过；隔离夹具 Incident 当前证据 0 条、alert_state=firing、recovery_verified_at=None；其合成 HighHttp5xxRate 告警仅用于流程验证，不是本轮真实 order-service 告警。模拟回答明确标注待人工审查，未升级为已确认根因或恢复。

复用原 Agent、ToolManager、Manager、Worker、RuntimeStore 及注册系统；没有第二套系统。生产状态机、修复权限和默认模型预算均未修改，无新依赖。

## 8. 测试、保留与实际改动

同一执行源码已完成：最终定向 159 passed；受限完整回归 **2046 passed、2 skipped、8 deselected、190 subtests passed，752.33s**；pip check 通过。测试具体命令、失败尝试与 8 项真实 HTTP 测试排除范围见授权前报告及原 XML。授权后未改源码，仅增加真实采集/核验产物和本报告，因此没有重复宣称新跑全量测试。

本次实网证明了真实 helper 在 60 秒截止被监督终止、已落盘的业务与 metadata 原件、来源解析、日志摄取及历史回放；没有证明完整 30 次执行、有效 5xx/P95 样本或真实模型推理能力。

保留审计：[authorized-preservation.json](../data/p62a-20261018T075858Z-719154a6/authorized-preservation.json)。原 1390 个文件中 1389 个哈希不变；唯一预期改变为 app.log 追加 **872 字节/8 行**（900127→900999）。原 900127 字节前缀 SHA-256 与开始时完全相同，旧日志未覆盖。两个仓库用户原有未暂存 diff 逐字节相同，生产 Incident/Runtime/Agent 日志等保持原哈希。新增 Prometheus/Loki 遥测属于本次获授权副作用。

本次后续提交仅本报告；四个入口/测试文件已在准备提交保存。原始响应、数据库行、日志、授权正文及 SQLite 全留本地 ignored data，未暂存。没有 reset/clean/stash、修改 Demo 配置、删除旧产物或重启服务。

## 9. 证据路径及 SHA-256

所有路径均相对于 [本地产物根目录](../data/p62a-20261018T075858Z-719154a6/)。清单逐文件绑定来源元数据和字节原件；哈希仅证明本地一致性，不是服务端签名。request_executed 只表示进入本地 HTTP 客户端。

| 清单/核验 | SHA-256 |
| --- | --- |
| authorized-pre/sha256.json | `20d75fd5f01f43c760580fc89f192358e8049c1c603b487cc835b4c281f743f8` |
| authorized-traffic/sha256.json（含59项） | `79a284537cd844fc95eb1a170e641bd5f119897700c5ede93c80f48fc2038145` |
| authorized-after/sha256.json | `e4fa52fe77c1c4dccf316ab36f78b267d5b40cf81c6366f84feefe3c5c1ce69e` |
| authorized-window/sha256.json | `614cf11feb149227e6e5e18484fa0f76ef22f045d64c19957f1099da2127eccf` |
| authorized-traffic-time/sha256.json | `8030dc2744fa41ad8bce1057cc65a62e84f374c4192dbaa4b0d49bf72a8063ce` |
| authorized-replay/sha256.json | `a587f78061da0dfaded358f3ec8252a645e90b9db730b4d58be7050bb348c3de` |
| authorized-verification.json | `54efe6172a7505d79584bc43a4d842a8fbae06963a05b4b522c8561bb6bfaf7f` |

## 10. 已确认问题、缺口与 P6.2b 准入

1. **完整批次证据不完整**：60 秒截止正常触发保护，但最终 child 汇总缺失。保留 8 条完整 200 原件及 supervisor unknown，不能称 30 次成功，也不能将截止中断写成业务根因。
2. **错误率与 P95 无有效数值**：少量请求、窗口和抓取共同约束；当前不具备正常 5xx/P95 数值基线。不得为获得值放宽最小量、加压或自动追加请求。
3. **客户端/metadata 开销待定位**：版本探测也约 2 秒，使 60 秒只留下 8 条完成业务证据。需独立审阅调用/连接路径，不能猜测成数据库慢；优化方案不得绕过身份校验、白名单或超时。任何重跑需重新单独授权。
4. **时间与身份局限**：运行 commit_unknown，日志时间未由 Alloy 解析，聚合指标和日志缺共享请求 ID。历史评估与摄取时间均不等于当前业务健康或源码因果。
5. **P6.2b 仍待独立审查**：先审查原始未知计数、8 条证据关联、受限测试范围及上述缺口，明确足够的正常基线验收条件与后续授权；当前不进入故障注入、真实模型或修复。

本轮真实业务流量到此结束；后续只提交推送本报告并停止，等待独立审查。
