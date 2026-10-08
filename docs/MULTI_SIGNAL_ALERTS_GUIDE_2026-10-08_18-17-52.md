# 多信号告警系统指南

## 架构和模块

现有 ServiceRegistry → ServiceProfile → MetricProfile/Provider 保留。PrometheusClient 拉取响应，由 [alert_parser.py](../alert_parser.py) 标准化成 AlertSnapshot 和 AlertEvent；[alert_policy.py](../alert_policy.py) 提供可信语义配置。[IncidentMonitor](../monitor/incident_monitor.py) 只处理统一事件、独立身份、调查节流和生命周期。Normalizer 可以通过构造参数注入；未来来源增加 Adapter，不重写 Agent 调查循环。

阈值唯一生产来源是 Demo alerts.yml 的 annotations.threshold，同时必须与 expr 比较值一致。Policy 不维护第二份触发阈值。跨仓库 test_rules_contract 校验五种规则、metric、signal_type、severity、direction、unit、窗口、最少请求量、for 与比较值；promtool 检查实际触发行为。运行跨仓库测试可设置 ALERT_RULES_PATH 指向规则文件。

## AlertEvent 与兼容性

[models/incident.py](../models/incident.py) 的 AlertEvent 包含：

| 字段 | 含义 |
|---|---|
| alertname / service / severity | 注册规则名称、Registry 服务名、严重程度 |
| signal_type | numeric、state 或 event |
| source / state | 来源；inactive、pending、firing、cleared |
| metric / value / threshold | 允许 None；数值必须有限；数值/状态型现代事件必须完整 |
| unit / direction | ratio、seconds、boolean 等；higher_is_worse、lower_is_worse、event |
| labels / identity_labels | 原始字符串标签；Policy 指定的稳定身份标签 |
| starts_at / observed_at | 告警开始和标准化观测时间；必须有时区，统一 UTC |
| window / minimum_requests | 采样窗口和低流量保护 |

Alert 是 AlertEvent 的兼容别名。旧五字段 Python 构造与历史 Store 数据显式处于 source=legacy；生产 Parser 不从缺失 alertname 的损坏输入推造有效告警。旧记录缺少新字段时使用兼容默认值，不删记录。新来源可传入不带数值的 event 型事件；Prometheus 当前 Adapter 只接受本次登记的五类数值/状态信号。

单条解析结果分为 valid、ignored、invalid。pending/inactive 被正常忽略，不启动新调查；格式、标签、规则、服务、profile、单位、比较方向、阈值、NaN/Infinity、时间戳错误属于 invalid。有效条目仍可处理，但任一损坏条目、API envelope 错误或 warnings 都使快照不完整，禁止本轮解除旧 Incident。原始异常输入不会写入 Store。

## 五种规则

所有规则 for=10s；本地 scrape/evaluation interval=5s。HTTP 的窗口为 1m，increase 窗口请求估计量至少 20；该量是 Prometheus Counter 外推估计，不等于逐请求整数审计记录。错误率和 P95 查询在低流量、无样本、分母零或抓取失败时返回 no_data。

| 告警 | 条件 | 监控含义 |
|---|---|---|
| HighHttp5xxRate | 完整 500—599 rate 占比 > 0.2，满足流量保护且业务目标 up=1 | 观测窗口内 HTTP 业务错误比例高；不自动证明代码或数据库根因 |
| ServiceDown | HTTP 业务 scrape target up < 1 | 抓取失败；不证明进程退出；健康端点 200 也不证明业务成功 |
| DatabaseUnavailable | pg_up < 1 且同 job/instance 的 exporter up=1 | exporter 当前无法正常访问 PostgreSQL；认证、网络、实例状态都需调查 |
| PostgresExporterDown | PostgreSQL exporter target up < 1 | 监控采集链路失败；不是数据库宕机结论 |
| HighHttpLatency | Histogram P95 > 0.5 秒，满足流量保护且业务目标 up=1 | 窗口内请求尾延迟高；桶内插值估计，不是平均或最慢单请求 |

核心 PromQL（完整标签和 for 见 Demo alerts.yml）：

```promql
# 分子缺失仅在有效分母存在时用 0 * total_rate 补出观测零值。
(sum by(service)(rate(demo_http_requests_total{status=~"5[0-9][0-9]"}[1m]))
 or on(service) 0 * sum by(service)(rate(demo_http_requests_total[1m])))
/ sum by(service)(rate(demo_http_requests_total[1m])) > 0.2
and on(service) sum by(service)(increase(demo_http_requests_total[1m])) >= 20
and on(service) min by(service)(up{job=~"demo-python|inventory-service"}) == 1

up{job=~"demo-python|inventory-service"} < 1
pg_up{job="postgres"} < 1 and on(job,instance) up{job="postgres"} == 1
up{job="postgres"} < 1

histogram_quantile(0.95,
  sum by(service,le)(rate(demo_http_request_duration_seconds_bucket[1m]))) > 0.5
and on(service) sum by(service)(increase(demo_http_request_duration_seconds_count[1m])) >= 20
and on(service) min by(service)(up{job=~"demo-python|inventory-service"}) == 1
```

MetricProfile 使用固定可信模板，不执行模型生成的 PromQL。新增 service_scrape_up、exporter_scrape_up、http_latency_p95_seconds；postgres_up 查询要求 exporter up=1，以免停止采集后的历史 pg_up 被误用。多实例结果不随意取第一项，query_metrics 返回 no_data 并说明歧义。check_scrape_target 从实际 /api/v1/targets 获取目标标签、health、lastError、lastScrape，不赋予 shell 权限。

调查 Prompt 使用 AlertEvent、Policy、Registry.describe 返回的能力和候选关系。SourceSettings.investigation_paths 配置仓库相对路径，describe 以 source_paths 提供给模型：order-service 为 app.py/http_observability.py，inventory-service 为 inventory_app.py/http_observability.py。模型读取运行完整 SHA 的源码，不根据服务名猜目录；数据库服务缺少 logs/source/runtime 时明确保留证据缺口。

## Histogram 与故障注入

Demo http_observability.py 在 before_request 开始计时，在 after_request 按最终响应状态增加 Counter、观察 Histogram。order-service 仅统计 /、/error、/order；inventory-service 仅统计 /inventory；/health、/metrics、/version 不计入。成功、业务异常、503/599 等失败响应都保留最终 status 标签。Histogram 单位秒，桶为 0.005、0.01、0.025、0.05、0.1、0.25、0.5、1、2、5、+Inf。

SIMULATE_CODE_REGRESSION、SIMULATE_INVENTORY_ERROR 和 SIMULATE_SLOW_REQUEST 默认关闭，进程启动时读取。慢请求固定延迟 0.8 秒，不无界 sleep；需要持续业务流量与至少 20 个窗口请求估计量，单个慢请求不会必然触发告警。/version 和 Runtime profile 仅公开各服务的非敏感 allowlist；源码脏时启动验证程序报告 commit unknown，不能拿 HEAD 冒充实际代码。

正常启动与应用故障可用 Demo 的 start_demo.ps1 / start_inventory.ps1，分别传入 -SimulateCodeRegression、-SimulateInventoryError、-SimulateSlowRequest。启动脚本会拒绝未提交运行代码。up 始终由 Prometheus 自己采集，应用不伪造 up。

完整真实注入脚本为 [scripts/validate_multi_signal.py](../scripts/validate_multi_signal.py)：

```powershell
# 从 D:\Incident Agent 执行；显式授权本地故障，真实模型调用可能产生 API 费用。
& 'D:\conda\envs\agentlearn\python.exe' -B scripts/validate_multi_signal.py --inject-faults --run-model
```

也可指定 --scenario order-500 / inventory-500 / service-down / database-down / exporter-down / latency。省略 --run-model 时只验证生命周期，结果明确标记 model_run=false，不能算真实模型验证。程序在终止进程前持有 Windows native handle，核对 Python 完整路径、cwd、命令行、创建身份；容器核对 ID、镜像及 Compose 工作目录；Prometheus 核对 Demo bind mount。注入前要求有效、样本充足且无告警的正常基线，避免仅依靠刚启动时的空告警列表。finally 恢复原有两个服务及同一数据库/exporter 容器，再检查业务返回 200、数据库查询成功和所有安全开关关闭；身份改变或恢复失败显式报错。不删数据库 volume、不停止未知进程。不使用修复 Executor。

## 身份、关联和生命周期

现代 fingerprint 是规范 JSON 的 SHA-256：版本、source、alertname、service、Policy 身份标签。HTTP 错误率和 P95 按服务聚合；三种状态告警使用 job+instance，分别独立。数值、时间、severity、summary 不参与。旧 source=legacy 保留 service|metric 算法。只有无歧义的旧 HTTP 错误率活跃记录在首次收到现代同类告警时安全换 active index，保留 Incident ID、原始 alerts、Evidence 和调查历史；其他不明确旧身份不自动匹配，不丢历史。v1 fingerprint→record 映射可读取成 v2 视图；写入使用同目录临时文件和原子替换，保留全部历史记录。

候选关联仅限相同注册服务或 Registry 明确依赖，且开始时间相差不超过 300 秒；缺少时间不猜关系。related_alerts 标记 relationship=candidate、shared_root_cause_confirmed=false。不会合并 Incident。只读工具缓存限同一轮，共享结果不计成独立观测；后续 Incident 开始时若缓存超过 30 秒即丢弃，下一轮必清空。

生命周期：inactive/pending 尚未触发正式调查 → firing 创建或复用独立 Incident → trusted snapshot 且有效指标恢复正常后 cleared。旧 status=resolved 用于兼容“告警解除”，不能代表业务恢复。另有 alert_state、cleared_at、recovery_verified_at、recovery_evidence。pending 信号仍超过阈值，不解除已活跃 Incident。

解除时还验证 /api/v1/rules 对应规则健康、最近 30 秒内完成评价，规则阈值/单位/方向与事件一致，目标抓取 up=1 且样本新鲜，以及对应 metric 存在、有限并处于正常侧。未知、停抓取、低流量、规则移除、pg_up 缺失或工具故障保持活跃。业务恢复需独立及时探测，调用 Store.record_recovery_verification 记录证据；Monitor 不自动认证业务恢复。

## 重调查和权限

Policy.reinvestigation 指定策略：HTTP 5xx 绝对增加 0.2 或相对增加 50%；P95 绝对增加 0.5 秒或相对增加 50%；状态信号只看状态变化和有限 cooldown，不套用 +0.2。默认 cooldown 300 秒、失败重试 60 秒；真实成功调查结束后最少间隔 30 秒。旧无时区的本地调查记录按本项目 Asia/Shanghai 解释用于兼容节流；新时间统一 UTC，模型不能据此猜旧日志精确相关性。当前没有自动注入“新外部证据版本”事件接口，变化证据由有限 cooldown 重查获取。

仍为每次调查最多 7 轮模型响应、13 次实际工具调用。调查失败保存既有有效 Evidence、记录尝试时间并节流；连续 3 次失败后停止自动重试。模型 API 401/402/403 立即暂停该 Monitor 会话的后续模型请求，并持久化 incident.investigation_suspended_reason，避免余额不足或权限异常时对每条告警反复收费/重试。修复外部条件后，操作员通过 Store.resume_investigation(incident_id) 解除对应记录的暂停并重启 Monitor；该入口不是模型工具，不改变告警或业务恢复状态。工具返回 no_data 留在调查 trace，不伪造成功 Evidence。历史调查记录增加 model_rounds、tool_calls、tool_trace；旧记录默认零值。

仅有 disable_demo_regression 固定动作。HighHttp5xxRate 本身不能批准修复；Planner 还需本实例版本与开关、启动后新鲜日志、精确 SHA 的完整源码、真实数据库访问成功证据。其余四类信号禁止建立该动作；inventory-service 无该权限。跨关联 Incident 的相同 service/action/parameters/SHA/process_started_at Proposal 去重；人工批准、拒绝、独立执行、立即验证和 Audit 保留。Monitor 和模型不执行动作。

## 新增第六种告警

同类 HTTP/状态指标：在 Demo 增加规则（expr、for、语义 annotations），在 AlertPolicy 增加声明，复用已有 Profile；若尚未暴露该逻辑指标，在对应 MetricProfile 增加可信模板并登记 supported_metrics。同步 contract 与 promtool 行为测试。普通服务只需新增 Registry Profile、稳定 scrape service 标签和相应通用业务计数/Histogram；无需改 query_metrics 中的服务名判断或 Agent 主循环。

CPU、内存、磁盘等已有 exporter 指标可通过新 MetricProfile 模板接入。确实没有采集代码的信号要新增 exporter/应用 instrumentation；Redis、消息队列等新协议健康探测需要新 Provider。未来告警来源需新增 Normalizer Adapter（事件校验、完整性元数据、来源身份）和相应解除证据检查；复用 AlertEvent、Policy、身份、关联、Store 与 Agent，不能把来源私有字段判断塞进 Monitor。

限制：单机串行轮询、JSON Store 单写者；没有复杂共享根因认证、SLO 平台或自动监控缺失告警。多实例 scalar 查询歧义保守 no_data；HTTP 数值身份为服务聚合。for=10s、1m 窗口适合此学习 Demo，生产阈值/流量门槛需按实际负载重新验证。规则变化不自动认证旧告警恢复，需复核旧事件。恢复接口的证据由调用方验证，不让模型自行认证。

参考：[Prometheus Histogram 函数](https://prometheus.io/docs/prometheus/latest/querying/functions/)、[规则行为测试](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/)、[HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)。
