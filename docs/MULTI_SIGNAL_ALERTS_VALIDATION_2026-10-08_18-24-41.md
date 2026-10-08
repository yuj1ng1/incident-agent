# 多信号告警升级验证记录

## 范围、初始状态与证据分级

本次实现五种告警、统一 AlertEvent、声明式 Policy、独立身份、候选关联、生命周期和重调查策略。保留 ServiceRegistry、原 Agent 循环、7 轮/13 次工具预算和唯一固定修复动作。未引入新自动执行权限。

开始时实际读取的 Agent HEAD 为 `f2d7873e387f47ce7c6f2d1a663f86669e79aca8`，分支 main；Demo HEAD 为 `75f4137ab0c9052f5855a01cea4acab151b8e58b`，分支 codex/postgres-monitoring。暂存区均为空。Agent 原有文档归档、README、main.py、temp_test.py、Prompt 第 52 条和 Monitor 日志拼写修改，以及 Demo README 的已有示例请求路径修改，均单独保留，不纳入本任务提交。

证据类型明确区分：

- Python 单元/Mock 测试：校验数据、节流、Store、工具响应及权限；不能证明真实外部服务或模型成功。
- promtool 规则测试：使用合成时间序列实际执行 PromQL 与告警状态断言；不是线上观测。
- 本地真实集成：真实 Flask 请求、Docker PostgreSQL/exporter、Prometheus/Loki/GitHub API 和 IncidentMonitor。
- 真实模型调查：通过项目现有 `deepseek-flash` Responses 工具循环获取结论；计数来自持久化 InvestigationRecord。未将 Mock Observer 算作模型调查。

全部原始运行证据仅保存在本机被忽略的 `data/multi_signal_validation/`、`data/multi_signal_model_ledger.json` 和 `data/multi_signal_final_postflight.json`。本报告只提交可复核的摘要，不提交 Incident JSON、日志、密钥或数据库密码。

## Python 和规则测试

| 项目 | 原有测试 | 新增测试方法 | 最终总数 | 结果 |
|---|---:|---:|---:|---|
| Incident Agent | 277 | 65 | 342 | 全通过 |
| prometheus-demo | 14 | 7 | 21 | 全通过 |
| 合计 | 291 | 72 | 363 | 全通过 |

保留原有全部测试；调整真实 API fixture、身份断言、pending/cleared 区别、失败重试和完整 5xx PromQL 预期。新增测试见 [test_multi_signal.py](../tests/test_multi_signal.py)。覆盖五种 Policy、有限值/时区/缺字段、真实 alerts envelope、单条损坏隔离、警告/不完整快照、身份和 v1/v2 历史兼容、多告警、关联、跨 Incident 缓存/证据保留与 30 秒过期、状态与数值策略、API 402 暂停、失败上限和操作员恢复、无数据安全解除、工具语义域以及 Remediation 权限/去重。

Demo 的 7 个新 Histogram 测试验证业务范围、成功/失败计数和耗时、500/503/599、非业务端点排除、桶与秒单位、默认关闭和两服务隔离。Flask 测试客户端属于进程内测试，不冒充真实 HTTP 集成。

promtool 3.15.0：`check rules` 成功识别 5 条规则；`test rules` 全通过。规则测试共 20 组、100 个告警断言、4 个 PromQL 数值断言（104 项断言，不与 Python 测试方法数混算）。覆盖完整 5xx、零分母、低流量、无数据、缺错误序列的有效零值、阈值边界、pending/for、Counter reset、双服务、数据库与 exporter 区分、pg_up 缺失、真实 Histogram 分位数、少数慢样本和 P95 精确 0.5 秒边界。

```powershell
# Agent
Set-Location 'D:\Incident Agent'
& 'D:\conda\envs\agentlearn\python.exe' -B -m unittest discover -s tests -q

# Demo Python 与规则测试；规则已挂载在现有本地 Prometheus 容器。
Set-Location 'D:\prometheus-demo'
& 'D:\conda\envs\agentlearn\python.exe' -B -m unittest discover -s tests -q
docker exec prometheus promtool check rules /etc/prometheus/alerts.yml
docker cp tests\alerts.test.yml prometheus:/tmp/multi-signal-alerts.test.yml
docker exec -w /etc/prometheus prometheus promtool test rules /tmp/multi-signal-alerts.test.yml
```

## 真实故障注入与恢复

真实 Demo Python 运行代码已提交并推送为 `68343571adb8da171d1a9e05b733b1efc9af57b4` 后，再取该完整 SHA 的 GitHub diff/源码和 /version 证据。最终文档/规则边界测试提交不改变这次运行的 Python 代码。

下表列出完整六场景运行 `20261008T083550Z` 的故障时刻真实值。HTTP 窗口为 1m、请求估计量至少 20、for=10s。表中 5xx 为比例，P95 为秒，up 为 0/1。

| 故障 | firing 告警及服务 | 关键实测 | 独立 Incident |
|---|---|---|---|
| order 受控 500 | HighHttp5xxRate / order-service | 5xx=1.0、up=1、P95≈0.1375 | inc-78ba2dfa |
| inventory 受控 500 | HighHttp5xxRate / inventory-service | 5xx≈0.58986、up=1、P95=0.00475 | inc-6801c7d6 |
| inventory 不可访问 | ServiceDown / inventory-service | up=0；5xx/P95 查询为空 | inc-4dac0881 |
| PostgreSQL 停止、exporter 正常 | DatabaseUnavailable / order-db | exporter up=1、pg_up=0 | inc-50dc3b51 |
| exporter 停止 | PostgresExporterDown / order-db | exporter up=0；pg_up 为 no_data | inc-cb1a9888 |
| inventory 慢模式 | HighHttpLatency / inventory-service | P95≈0.94028、5xx=0、up=1 | inc-989202ce |

六项均实际注入、观察指标、等待 firing、通过 Monitor 创建独立 Incident、重复轮询复用身份、恢复、验证告警解除与两业务路由 200。完整运行在注入检查点未发现不相关 firing 告警；数据库场景允许已登记依赖的 order-service 5xx。告警首次消失时 Store 可能因恢复数据尚不充分而暂时保持 firing；最终 Store 均已 cleared 并有独立恢复证据，不能拿旧 results 快照状态代替最终 Store。

该完整运行在 order 场景后遭遇模型 API 402，后五项的物理信号/生命周期成功，但当时没有成功的模型调查。补充余额并只读确认 API 可用后，重新完成数据库、exporter、高延迟真实模型场景。inventory 500 和 ServiceDown 已在此前独立运行取得成功模型结论，详见下表。

补充真实工具验证（未运行模型，Observer 仅驱动生命周期）：`20261008T084824Z` 数据库 TCP ConnectionRefused；`20261008T085042Z` exporter down 而数据库 DNS/TCP/连接/SELECT 1 全成功；`20261008T085201Z` inventory P95=0.975、5xx=0。这些结果不计入真实模型数量。

## 成功的真实模型调查

| Incident / 运行目录 | 类型与服务 | 轮数 / 实际工具调用 | 核心结论与证据缺口 |
|---|---|---:|---|
| inc-78ba2dfa / 20261008T083550Z | 5xx / order-service | 4 / 9 | 错误率 1、真实应用 RuntimeError 日志、开关开启、完整运行 SHA 的 app.py、DB 查询成功；支持受控应用回归路径。运行版本仍为实例自报，非独立部署认证。 |
| inc-816f5266 / 20261008T082626Z | 5xx / inventory-service | 5 / 8 | 错误率约 0.6034、真实 application_error 日志、开关开启、无 DB 依赖；支持应用异常。该早期调查猜错源码路径，源码工具 no_data，代码因果证据不完整。随后加入 Registry source_paths，慢请求调查已验证正确路径取证。 |
| inc-85baef46 / 20261008T082626Z | ServiceDown / inventory-service | 4 / 7 | up=0、采集目标 connection refused、health/version 不可达；确认访问/采集失败，无法区分进程停止、崩溃或监听问题。没有机械查询 PostgreSQL。 |
| inc-12d6b75e / 20261008T095428Z | DatabaseUnavailable / order-db | 3 / 5 | exporter up=1、pg_up=0、DNS 成功而 TCP 被拒绝；确认数据库访问路径异常，容器/进程底层原因未进入模型证据。 |
| inc-f5457e35 / 20261008T095706Z | PostgresExporterDown / order-db | 2 / 4 | exporter up=0、target connection refused、pg_up no_data，但独立数据库查询成功；确认监控链路异常，未宣布数据库宕机。 |
| inc-68c3c777 / 20261008T100058Z | HighHttpLatency / inventory-service | 4 / 10 | 复查 P95≈0.94167、5xx=0、真实日志 200、slow flag=true、完整运行 SHA 的 inventory_app.py/http_observability.py 与固定 0.8 秒路径；未查询无依赖的 PostgreSQL。逐请求耗时、部署认证和开关设置者仍缺证据。 |

模型结论保留原文，不能把开发者已知的故障注入操作偷偷当成模型工具证据。P95 的桶内样本分布和框架额外开销中，模型存在推断；它没有直接查询逐请求耗时或原始桶明细。验证者能确认固定慢路径被显式启动，模型则应保留这些测量缺口。

额外真实模型记录：inc-0ae7933d 3/8；inc-efd20bf9 两次 4/8 与 2/4；inc-4fb07ee8 5/11。inventory 错误率实际从约 0.379 增至 1，曾触发同一 Incident 合法重调查；测试脚本最初错误地把任何第二次调查都当作重复。修正验证假设并加入成功完成后 30 秒最小间隔，保留显著恶化与 cooldown 的合法重查。

exporter 补测期间，order-service 额外出现真实短暂 P95 越限：inc-88f4dbdb，4 轮/13 次工具。故障检查点 P95≈0.75248，模型事件值≈0.65019、复查≈0.5268；5xx=0、DB 主动查询成功、slow flag=false。模型保持独立 Incident 与候选关联，根因未确认，没有把 exporter 停止当作数据库慢的原因。该场景不能声称全程只有目标告警；数据支持它是实际越限，冷启动/连接开销是推测。随后加强注入前有效样本与正常 P95 的基线等待。此额外事件也已解除并验证恢复。

累计 **11 次成功真实调查、40 轮模型响应、87 次实际工具执行**，每次均在 7 轮/13 次预算内。缓存命中不计实际执行；失败 API 请求、Mock Observer、脚本主动探测和开发验证命令均不混入该数字。

| 模型实际调用工具 | 次数 |
|---|---:|
| query_metrics | 26 |
| query_logs | 9 |
| check_service_health | 10 |
| get_running_version | 8 |
| get_recent_code_changes | 8 |
| get_source_file | 8 |
| check_dependency_health | 5 |
| get_commit_diff | 6 |
| get_service_dependencies | 3 |
| check_scrape_target | 4 |

工具 no_data 也计一次实际执行，但不写成成功 Evidence。例如 exporter 的 pg_up 缺失和额外 order 延迟调查的 GitHub 访问失败都留有 trace；没有伪造成功来源。

## 恢复、边界和限制

本地进程终止前核对 agentlearn Python 完整路径、Demo cwd、脚本参数和持有 native handle 的进程身份；仅操作已确认的 order-service/inventory-service。容器仅操作相同 ID、镜像及 Compose 工作目录确认的 order-db/postgres-exporter；不删除 volume。Prometheus 容器核对本 Demo 配置 bind mount，重启仅用于重新加载规则。

早期恢复 helper 曾因空监听查询的非零退出码无法启动 inventory。立即修正为查询全部监听再筛选，并实际恢复 inventory，随后对所有验证 Store 做最终恢复复核；没有隐藏这一验证工具失败。新的 finally 同时验证数据库查询、业务路由和关闭的安全开关，身份变化或恢复失败报错。

最终独立恢复复核时间为北京时间 **2026-10-08 18:12:43**（UTC 10:12:43）：/order 与 /inventory 均为 200，数据库 query_ok=true，业务目标 up、exporter up、pg_up 均为 1；两服务 5xx=0，order P95≈0.06776 秒、inventory P95≈0.00494 秒；/version 所有安全开关为 false，Prometheus alerts=[]。9 个私有验证目录共 18 条 Incident 均为 cleared 且 recovery_verified_at 非空。正常流量停止后，低流量指标可能变成 no_data，这不能被报告成新的恢复证据。

监控数据损坏、API warnings、未知规则/服务、缺 pg_up、低流量、停抓取、规则评价异常时，不清除旧 Incident。解除还要求对应规则健康、新鲜 up 和有效指标回到正常侧。告警解除与业务恢复分别记录。调查异常不清除既有证据；失败有 retry/cooldown、连续失败上限及认证/余额错误暂停。

Remediation 原审批/拒绝/独立执行/验证/Audit 单元与 Mock 测试全部通过。新信号本身不能授权；ServiceDown 不重启服务、DatabaseUnavailable 不操作数据库、inventory 无订单动作权限。关联不合并 Incident，相同进程/版本的固定动作 Proposal 去重。本次真实注入的恢复由明确授权的验证脚本完成，**未在真实环境重新执行 Remediation 审批与动作**，因此不把它报告成新增的真实修复验证。

限制仍包括单写者 JSON Store、串行调查、HTTP 数值身份按服务聚合、多实例 scalar 歧义 no_data、无进程/容器 Agent 工具、无独立部署认证、无逐请求 duration 日志，以及没有“新外部证据版本”的自动触发接口。for=10s 与 1m/20 请求用于学习 Demo，不能直接作为所有生产服务的阈值。额外瞬态延迟与偶发 GitHub no_data 已如实记录。

新增同类告警主要修改 AlertPolicy、Prometheus 规则与已有 MetricProfile 模板/声明，补 contract 与行为测试；新增普通 HTTP 服务复用 instrumentation 和 Profile，增加 Registry 配置与稳定标签。尚未采集的指标新增 instrumentation/exporter；新协议增加 Provider；新告警来源增加 Normalizer Adapter 与来源恢复验证。Agent 主循环无需按每种规则重新实现。

提交采用显式文件清单及共享文件的任务片段，排除原有无关修改。暂存检查包括完整 diff、空白检查、禁入路径和本地实际秘密值扫描；最终分支/commit/远端一致性由交付报告列出。未 force push、reset 或覆盖他人修改。
