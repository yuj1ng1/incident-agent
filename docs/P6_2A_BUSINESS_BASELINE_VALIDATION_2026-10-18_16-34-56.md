# P6.2a 正常业务证据基线验证（授权前交付）

## 1. 结论与授权边界

**本轮完成离线准备、真实只读快照及隔离回放，尚未建立正常业务请求基线。** 用户要求业务流量单独授权，截至本报告完成时尚未获得，因此本轮实际 `/order` GET 为 **0 次**；真实 DeepSeek/其他模型请求 **0 次**，故障注入、修复、业务数据写入、服务重启均为 **0 次**。没有运行生产 `main.py` 或启动生产 Monitor/Scheduler/RecoveryWorker 后台组合。

HTTP Health=ok、Prometheus target up、调查 Session COMPLETED 均不等于业务健康或恢复。实际业务请求与指标、日志之间的本轮时间关联仍待授权后验证。此前 P5/P6.1 授权未被沿用。

## 2. 基线、配置与实际进程

| 项目 | 本轮核实结果 |
| --- | --- |
| Incident Agent | `D:\Incident Agent`，分支 `main`，初始 HEAD `f077ac3a58c5c462d3499f2744554f19cf61c718` |
| Agent 真实 origin | `https://github.com/yuj1ng1/incident-agent.git`；开始时 `git ls-remote` 的 main 与本地 HEAD 相同 |
| Demo | `D:\prometheus-demo`，分支 `codex/postgres-monitoring`，HEAD `0e2f6f3220b9fb9f0baf8730de5ffaaac00d8c38` |
| Demo 真实 origin | `https://github.com/yuj1ng1/prometheus-demo.git`；开始时远端分支与本地 HEAD 相同 |
| Python | `D:\conda\envs\agentlearn\python.exe`；requests 2.34.2、openai 3.14.0、psycopg 3.3.6、pytest 9.1.1 |
| Demo 8001 | PID 29476，命令行为上述解释器 `-u d:\prometheus-demo\app.py`；进程创建 `2026-10-18T03:31:29Z` |
| HTTP 进程身份 | `/version` 的 `process_started_at=2026-10-18T03:31:32.744052Z`；service=order-service；两模拟开关均 false |
| 运行版本 | `commit_sha=null`，现有版本工具返回 `no_data/commit_unknown`；磁盘 HEAD 不能代替运行 SHA |
| Prometheus | 容器 `a72b1946a4ee`；order-service target 指向 `host.docker.internal:8001`，up；实际 scrape interval/timeout 均 5s |
| PostgreSQL/exporter | order-db `2a0c1a0756ff`，exporter `0a7f02c6bea2`；现有工具经 exporter 返回 postgres_up=1、exporter_scrape_up=1；没有直接连接数据库 |
| Loki/Alloy | Loki `b0d2e9b47c80`，Alloy `106e8a6e802f`；正式 ready 只读探测返回 200 |

先读取了实际根目录 `AGENTS.md`；未发现 Demo 的适用 AGENTS。初始 Agent 暂存区为空，存在用户已有 README、prompt、temp_test 修改、历史文档迁移及未跟踪测试产物；Demo 仅已有 README 修改且暂存区为空。完整状态和 diff 保存在本轮本地目录，全部保留，未 reset/clean/stash。

实际进程/监听信息见 [environment.json](../data/p62a-20261018T075858Z-719154a6/environment.json)。另有 PID 33252 的 `uvicorn api_app:app --reload --port 8004` 和交互 Python；用途未确认，不能声称没有其他测试。inventory-service:8002 已拒绝连接且存在 ServiceDown firing（从 `03:29:26Z` 起），未干预。order-service 的两个开关关闭仅说明这些开关状态，不排除实际数据库或其他故障。

8001、9090、3100、12345 的既有非回环绑定没有改变；本轮只访问本地地址，未据此断言网络全局隔离或公网不可达。

## 3. 业务路由副作用与监控语义

审查了 Demo `app.py`、`http_observability.py`、`prometheus.yml`、`alerts.yml`、`alloy-config.alloy`、`loki-config.yaml` 和相关 Docker 挂载；同时审查了本项目 [metric_profiles.py](../services/metric_profiles.py)、[metrics.py](../tools/metrics.py)、[logs.py](../tools/logs.py)、[health.py](../tools/health.py) 及两个 P6.1 入口和测试。

`/order` 每次建立 PostgreSQL 连接，执行 `SELECT id, item, quantity FROM orders ORDER BY id LIMIT 1` 并关闭连接；数据库连接/语句有超时。它不执行该路由代码中的业务 INSERT/UPDATE，但会更新 `demo_dependency_up`、Counter、Histogram，追加应用日志，并使用数据库资源。实际数据库故障或模拟应用回归都可能返回 500，因此不是零副作用接口。响应可能包含数据库业务行；本轮未获得任何新业务行，未来原始响应只留本地，报告/摘要不抄录行值。

业务 instrumentation 覆盖 `/`、`/error`、`/order`；`/health`、`/version`、`/metrics` 不访问业务数据库，也不计入这些业务指标。Counter/Histogram 的标签是 `service,status`，没有 path 或 request_id，所以聚合指标不能逐条唯一归因到某个请求。

`http_5xx_rate` 和 P95 使用 1m 滑动窗口，要求 `increase(...) >= 20` 且 scrape up=1；rate 也需要足够抓取样本。发出 20 次甚至 30 次请求并不保证得到有效比率：首次系列初始化、窗口、抓取和查询时机均有影响。0.0 必须是有效有限样本，空 vector、非法时间戳、非有限值、异常结果不得补零。

## 4. 可审查的受限流量方案（未授权，未执行）

新入口为 [p62a_business_baseline.py](../scripts/p62a_business_baseline.py)，默认只打印离线计划，导入不发请求。没有放宽 [P6.1 采集器](../scripts/p61_collect_observations.py) 的白名单。

- 只允许固定 `GET http://localhost:8001/order`，业务请求总上限 30 次，串行、相邻业务循环至少间隔 1 秒，不做压力测试。
- 实网必须走公开 CLI 的 supervisor；从创建自身 helper 前计时，最多 60 秒截止，到期仅终止自己创建的子进程。单次连接/读取超时为 2/3 秒，内部总期限检查 8 秒；禁重试、代理、netrc、cookie、压缩和重定向，响应上限 256 KiB。
- 每次业务前后读取固定 `/version`，核对 service、启动时间、显式预期 SHA（允许 unknown 原样比对）和两个开关严格 false。任何非 200、超时、异常响应、开关或身份变化立即停止后续流量。
- 执行必须提供单独授权说明、外部核实的 PID/归属说明、预期身份及全新目录。授权参数仅放新建本地文件，最大 64 KiB；不经命令行传递授权正文，不使用 Windows 同步 stdin 写入。
- 不调整故障开关、不重启服务、不直接写数据库业务数据；元数据探测与 30 次业务上限分别计数。全部业务 HTTP 明确标记为直接测试流量，不冒充 Agent 自动工具调用。

普通操作系统不提供数学意义的硬实时保证：这里是到截止触发终止本地 helper，进程回收和报告写入可稍后完成；杀死客户端不能撤回服务端已接收的请求。监督中断时原件可能不完整，请求数记为 null/unknown，不虚填 0。HTTP 身份检查也不能独立证明 PID 归属或消除前置检查竞态。

授权前还需复核当前进程、开关、target 和其他测试冲突。授权不会追溯成为过去的业务验证，也不覆盖 P6.2b 故障实验或任何付费调用。计划原件见 [traffic-plan.json](../data/p62a-20261018T075858Z-719154a6/traffic-plan.json)。

## 5. 本轮真实只读观测

正式 P6.1 复用采集发生于 `2026-10-18T08:00:23.360049Z` 至 `08:00:30.665544Z`，保存 14 条 HTTP 原件，均为 200 且正文完整。补充采集保存 7 条只读 HTTP 原件；Alloy 另存 ready/metrics 两条正式只读响应。它们均不访问 `/order`。探索性只读审查未保存完整逐请求原件的内容不作为正式证据计数。

五项补充指标使用同一请求评估时间 `2026-10-18T08:01:21.715826Z`，现有工具向下对齐为 `evaluation_time=2026-10-18T08:01:21.715000Z`，调整 -826000ns。因现有 aligned 工具每组最多四项，保持原限制分为 3+2 两组。

| 指标 | 实际工具结果 | 对应原件 |
| --- | --- | --- |
| http_requests_total | no_data，无 value，no_samples_at_evaluation_time | supplement/http-001 |
| http_request_rate | no_data，无 value，同上 | supplement/http-002 |
| http_5xx_rate | no_data，无 value，同上；P6.1 主观测也为空 vector | supplement/http-003 |
| service_scrape_up | ok，1.0；evaluated_at 与上述 evaluation_time 相同 | supplement/http-004 |
| http_latency_p95_seconds | no_data，无 value，no_samples_at_evaluation_time | supplement/http-005 |

缺失项没有伪造 evaluated_at，也没有转换成 0。相同 evaluation_time 不代表底层 scrape timestamp 相同或多 HTTP 查询具有事务原子性。`/metrics` 的业务指标只有 HELP/TYPE、没有带标签样本，符合当前缺失观测；不足以确认其原因。

日志使用 `{service="order-service"}`，明确 `[start,end)`：

| 范围（UTC） | HTTP/API 与工具状态 | 解释 |
| --- | --- | --- |
| 07:31:21.715826–08:01:21.715826 | HTTP 200，success/streams 空；工具 no_data/no_logs_in_query_range | 空结果与采集失败区分，不判定 Loki 故障 |
| 02:01:21.715826–08:01:21.715826 | HTTP 200，工具 ok；保留最新 50 条，truncated=true | 原始响应查询上限 51；不是完整日志清单 |

较长范围中的最新 `/order status=200` 文本时间为 `2026-10-12 17:05:40/41`，Loki 时间却约为 `2026-10-18T03:29:25Z`。Alloy 配置没有时间解析 stage，source.file 直接转发 write；因此不能把摄取时间当业务发生时间，也不能把这些旧日志算作本轮请求。

有效路径为 `D:\prometheus-demo\logs` 只读挂载至 `/var/log/demo`，读取 `app.log` 后发送 Loki。Alloy metrics 显示两个 active 文件、读取记录及 healthy 组件，写入 retries/drops=0；Docker 容器自身没有 healthcheck（不是 Docker health=healthy）。这支持配置及旧日志读取路径存在，不能替代新业务日志端到端验证。原 app.log 900127 字节、最后写入 `2026-10-12T09:05:41.326007Z`，校验期间未改变。

## 6. 原件保存、来源及 SHA-256

本轮本地产物根目录：[data/p62a-20261018T075858Z-719154a6](../data/p62a-20261018T075858Z-719154a6/)。该目录被既有 data 忽略规则保护，**原始数据、日志、SQLite 和配置没有提交到 Git**；这些链接供本机审查，远端克隆不包含原件。

| 清单/原件 | SHA-256 |
| --- | --- |
| [real-readonly/sha256.json](../data/p62a-20261018T075858Z-719154a6/real-readonly/sha256.json)，31 个被绑定文件 | `7821521c737e0d1de9cb306a86a439488d66da7e03e35195e9a979c6ff23b844` |
| [supplement/sha256.json](../data/p62a-20261018T075858Z-719154a6/supplement/sha256.json)，16 个被绑定文件 | `0fa2f795de26f016f67a705de984d2bc4e27672b84072c7cac4b67c007de7567` |
| [captured-replay/sha256.json](../data/p62a-20261018T075858Z-719154a6/captured-replay/sha256.json) | `da4f5e037054e48ddeb9c6520b5e89db8933070e7bab2c1da67c189ccb5c466c` |
| [Alloy 确认清单](../data/p62a-20261018T075858Z-719154a6/alloy-audit/manifest-confirmed.json) | `4ec3fd84a9938a6e959bb25210d5a78df9d902df5467cd11700abd09949b5c71` |
| supplement/http-004.body，scrape_up 原响应 | `056dfef780c85604fdab9eca5bb3763770d3623b7d54f57d9a333a468c3f04da` |
| supplement/http-006.body，近 30 分钟空日志 | `823ef65990eee96cf1bb15fa732ecf9717b2b19e97e39ac35251a81b4fb6a295` |
| supplement/http-007.body，6 小时旧日志 | `adfe4369390587734587c8af2937c020d4dbb8d9c931bc1b3461d57ea3d11aad` |

每条 HTTP `.json` 保存实际 URL/参数、UTC 起止、状态、进入本地请求标记、正文完整性、原件文件名及哈希；观察通过 request_ids 关联。哈希是本地一致性校验，不是外部来源签名；request_executed 只表示进入本地客户端，不独立证明服务端已收到。

## 7. 工具解析、Runtime 持久化与历史回放

新增 [p62a_verify_evidence.py](../scripts/p62a_verify_evidence.py) 的 `verify_collection(path)`，验证 P6.1 文件全集、SHA-256、安全路径/符号链接、逐请求来源、参数、响应 URL、时间包含关系，以及观察与原件绑定。四个主工具以原采集时间重解析，不续期；六项 P6.1 补充观察只校验哈希/来源/参数，此范围写在结果限制中。

本轮四主工具重解析一致，见 [verification-final.json](../data/p62a-20261018T075858Z-719154a6/verification-final.json)。本轮另外五项 aligned 指标及两个显式日志窗口在网络封禁条件下用现有解析器重新核对，7 项一致，见 [supplement-verification.json](../data/p62a-20261018T075858Z-719154a6/supplement-verification.json) 和同目录实际执行脚本。

复用原 [p61_readonly_integration.py](../scripts/p61_readonly_integration.py) 的 `run_case(observations=...)`，没有新建 Agent、Manager、Worker、RuntimeStore 或注册系统。正式 [回放记录](../data/p62a-20261018T075858Z-719154a6/captured-replay/report.json) 为：

```text
采集：08:00:23Z–08:00:30Z；回放：08:07:52.758349Z–08:07:58.737856Z
Session: ses-012c8961ef5f4ecb941ac37ee9f95b86 / COMPLETED
Incident: inc-5eb96bae / investigating / alert_state=firing
Mock SDK 决策 2 轮；受控工具记录 4 条；真实模型 0 次
所有记录：no_data + time_window + use_for_current_state=False
Incident 当前证据 0 条；recovery_verified_at=None
```

SQLite/Incident JSON 位于新隔离 stores，Checkpoint schema、function_call/output 链、重开存储、预算 reservation 与计数均由原入口实际校验。捕获原件与 captured_result 保持相同，历史 health/version 不因回放时间重新变为当前。缺失内容未以 Mock 补齐，回放没有重新请求业务接口或任何真实服务。

Mock 原答保留并明确标注模拟、待人工审查，不宣称已确认根因或恢复。合成告警只是流程输入，不是本轮真实 order-service 故障。生产状态机、修复白名单/审批权限、默认模型预算及依赖均未修改。

## 8. 实际测试、失败记录与范围

全部命令使用项目解释器和 `-B`，pytest 指定全新系统临时目录并关闭缓存。原有 `tests/conftest.py` 的生产 data 存储保护保持有效。额外本地测试 guard 阻断未 Mock 的 socket connect；仅允许 Windows 标准库 socketpair 建立 asyncio 内部唤醒管道，不用于 HTTP。真实服务只读采集另行运行，与 Mock 测试分开记录。

| 验证 | 实际结果 | 原件 |
| --- | --- | --- |
| 联合定向：P6.2a、P6.1 两入口、现有 Mock E2E | 155 passed，91.18s（文件交接修订前的版本） | [targeted.xml](../data/p62a-20261018T075858Z-719154a6/targeted.xml) |
| 最终两组新测试 + 实际 Demo 告警契约/多信号测试 | 159 passed，40.01s | [targeted-final2.xml](../data/p62a-20261018T075858Z-719154a6/targeted-final2.xml) |
| 最终冻结版本受限完整回归 | 2046 passed、2 skipped、8 deselected、190 subtests passed；752.33s | [full-final.xml](../data/p62a-20261018T075858Z-719154a6/full-final.xml) |
| pip check | No broken requirements found | [pip-check.log](../data/p62a-20261018T075858Z-719154a6/pip-check.log) |

新测试覆盖：未授权前目录/业务 GET/helper 拒绝；默认计划/导入离线；URL/方法、数量、时间边界；500/302/超时/连接失败/身份变化立即停止；超大/压缩/部分响应；原件 SHA 与来源请求号；0.0 与 no_data；Loki 空与不可用；篡改/越界路径/时间不符；回放无新请求；监督超时只终止自己的 Mock child、失败计数 unknown、授权文件大小与父身份、既有目录不覆盖。全部为 Mock HTTP/SDK/Popen，尚未用真实 helper 发送业务流量。

本轮必须排除会启动真实临时 HTTP 服务的五个测试函数（共 8 个 collected cases），没有把它们写成通过：

```text
test_real_loopback_http_ignores_environment_proxy
test_first_worker_skips_yesterdays_legacy_clear_without_real_http
test_background_startup_only_probes_durable_jobs
test_real_http_transport_against_isolated_loopback_server
test_real_redirect_is_refused_before_library_drains_body
```

完整回归使用上述五项 `not ...` 的 `-k` 表达式。默认 skip 项是干净安装和未指定跨仓库规则；后者已在最终定向中设置 `ALERT_RULES_PATH=D:\prometheus-demo\alerts.yml` 另外验证。全量测试基于当前受保护工作区（含用户既有未提交 prompt 修改），不声称已在一个无这些修改的独立干净克隆中复现。

失败尝试也保留：初版网络 guard 把 Windows asyncio 的标准库 socketpair 一起封禁，导致 smoke 3 failed/40 errors；只修正本地 guard 的标准库唤醒管道例外，既有 Web API 随后 52 passed。一次最终定向命令误写不存在的测试文件，退出 4、无测试执行，原输出为 targeted-final.log；更正实际 test_multi_signal.py 后得到上表 159 passed。子任务早期还有临时目录/超长参数 ID 的 setup 失败，失败目录保留。以上均不冒充业务故障，也未弱化生产安全代码。

可复现零业务请求命令：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B scripts/p62a_business_baseline.py
& 'D:\conda\envs\agentlearn\python.exe' -B scripts/p62a_verify_evidence.py --collection-dir '<本轮 real-readonly 目录>'
& 'D:\conda\envs\agentlearn\python.exe' -B scripts/p61_readonly_integration.py --observations '<本轮 observations.json>' --output-dir '<全新回放目录>'
```

## 9. 保留审计与实际修改范围

对开始时记录的 1390 个既有文件重新核对，全部 SHA-256 不变，0 缺失、0 未预期变化；两个仓库的用户既有未暂存 diff 与初始逐字节相同。三组采集/回放清单共 50 个绑定文件哈希匹配。详见 [final-audit.json](../data/p62a-20261018T075858Z-719154a6/final-audit.json)。

本轮提交范围仅四个新增 Python 源码/测试及本报告。没有修改 Demo、生产状态机、工具白名单、审批权限、预算或第三方依赖；复用既有采集器、解析器和隔离 Runtime。新监督外壳只控制本入口自己的 helper，不是新的调查 Worker。

`git diff --check` 在用户已有 temp_test.py 改动上报告空白问题，保持原样，没有顺手格式化；本轮暂存内容单独检查。初始用户差异及历史产物保留，运行数据不暂存。本次提交/远端 SHA 的最终核对由交付回复和本地产物记录给出，报告不嵌入自指提交 SHA。

## 10. 证据缺口、P6.2b 准入及停止点

1. 本轮业务请求为 0，没有新业务日志、业务响应与指标增量的真实关联；**P6.2a 正常业务基线未通过实网验收**。必须先获得对 SELECT/遥测副作用及 30 次/60 秒上限的单独明确授权，再重新执行安全前检。
2. 授权后按该上限采集，遇异常立即停止并保留事实；不能自动改为故障实验。至少等待合理抓取时机并记录 evaluation_time/evaluated_at、实际窗口、日志正文与摄取时间；指标缺失不得补零，旧日志不得算新请求。
3. 运行 commit 未知阻断运行源码因果判断；不为补 SHA 擅自重启服务。若 P6.2b 需要代码定位，需另行确认可信运行版本来源。
4. 需要人工核对其他运行进程/测试冲突及既有 inventory 告警；不能以 order-service 开关 false 宣称整个环境没有异常。
5. 要确认新日志的摄取链路，需授权业务后验证新日志；当前只确认有效挂载、读取状态及旧日志可查。指标只有 service/status、日志无本轮请求 ID，时间关联存在聚合与摄取延迟限制，不能推导一对一因果。
6. 真实模型推理能力、真实 helper 业务执行、干净安装、故障诊断及业务恢复尚未验证。任何 P6.2b 故障注入、重启、修复或付费模型调用均需其自身明确授权，当前不执行。

完成本轮离线准备提交推送后停止，等待独立审查和业务流量授权；不得把这份授权前报告描述为已建立真实正常业务基线。
