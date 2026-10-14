# P5.2b 受控单指标历史时间序列查询验证

## 实现范围与文件

本阶段新增一个只读模型工具 query_metric_range，并复用现有 ServiceRegistry、MetricProfile、工具注册、预算、租约、Checkpoint 和缓存机制。没有新增数据库表、依赖、前端或批量指标分析。

| 文件 | 实际改动 |
| --- | --- |
| [clients/prometheus_client.py](../clients/prometheus_client.py) | 新增时间窗口校验和受限 query_range 传输；原 query/get_alerts/get_rules/get_targets 的函数体不变。 |
| [tools/metrics.py](../tools/metrics.py) | 新增单指标工具、矩阵/序列/采样/缺口解析，保留 query_metrics 的接口和原有实现。 |
| [tools/__init__.py](../tools/__init__.py) | 导出并沿用装饰器注册新工具。 |
| [investigation/evidence_policy.py](../investigation/evidence_policy.py) | 增加历史窗口的 30 秒采集缓存 TTL；始终排除其当前证据资格。 |
| [investigation/worker.py](../investigation/worker.py) | 提供最多 12 个真实采样点的历史摘要，保留时间和缺失元数据。 |
| [tests/test_metric_ranges.py](../tests/test_metric_ranges.py) | 新增 Mock HTTP、时间/数值/格式/大小边界、临时 SQLite、缓存/上下文、权限/预算/租约回归。 |
| [tests/test_service_registry.py](../tests/test_service_registry.py) | 将新工具加入允许的注册工具集合，其余边界断言保留。 |
| [tests/test_runtime_quality.py](../tests/test_runtime_quality.py) | 仅使一个冻结 Runtime 时间的预算测试共用 Mock 时钟，保留全部 13 次预算断言。 |
| 本报告 | 记录参数、结果、验证证据和未验证范围。 |

PromQL 公式、Registry 定义、Checkpoint schema、Incident 状态机、RecoveryWorker、审批及修复执行权限均未修改。已通过 AST 比较确认客户端原有四个请求方法、两个时间函数，以及指标工具原有三个函数的函数体保持一致。

## 工具参数

```python
query_metric_range(
    service: str,
    metric: str,
    start: str,
    end: str,
    step_seconds: int,
) -> dict
```

五个参数全部必填。service 必须已登记 metrics 能力，并继续受当前 Session 服务/依赖权限校验。metric 只能取现有 HTTP 或 PostgreSQL profile 支持的名称，使用原 build_promql 生成表达式；不接受 PromQL 参数。可通过现有 get_service_capabilities 查询支持的指标。

start/end 使用带明确时区的 RFC3339 时间，允许 Z 和明确的正负偏移，最多九位小数；拒绝无时区、未知偏移 -00:00、非法日期和早于 Unix epoch 的时间。内部按整数纳秒检查原始区间，避免未来 1 纳秒或超过 6 小时 1 纳秒因浮点误差被接受。

请求区间要求 start < end，end 不晚于本地 UTC 当前时间，原始跨度最多 6 小时。为匹配 [Prometheus 时间戳的毫秒表示](https://github.com/prometheus/prometheus/blob/main/model/timestamp/timestamp.go)，实际 start 向上、end 向下对齐到毫秒，实际区间始终包含于原始区间；对齐后无法形成合法区间则拒绝。requested_range 保留调用者输入，query_range 表示实际发送的 UTC 区间。

step_seconds 必须为真正的整数，范围为 15～300，布尔、字符串和小数不能替代整数。端点包含在查询区间内，预计点数为 floor((实际 end - 实际 start) / step) + 1，请求前超过 120 点即拒绝，不请求后再裁剪。因此，6 小时配 15 秒会被拒绝，6 小时配 300 秒最多 73 点。[Prometheus 官方 API](https://prometheus.io/docs/prometheus/latest/querying/api/#range-queries)

## 完整结果结构

以下为实际执行 Mock 客户端生成的成功结构示例，数值和请求时间为测试数据，不是服务真实读数。真实请求起止时间由客户端本地记录，服务器响应中的同名字段不能覆盖它们。

```json
{
  "status": "ok",
  "service": "order-service",
  "metric": "http_request_rate",
  "evidence_scope": "time_window",
  "use_for_current_state": false,
  "samples": [
    {
      "evaluated_at": "2026-10-12T01:50:00+00:00",
      "value": 0.1
    },
    {
      "evaluated_at": "2026-10-12T01:50:15+00:00",
      "value": 0.1
    },
    {
      "evaluated_at": "2026-10-12T01:50:30+00:00",
      "value": 0.1
    },
    {
      "evaluated_at": "2026-10-12T01:50:45+00:00",
      "value": 0.1
    },
    {
      "evaluated_at": "2026-10-12T01:51:00+00:00",
      "value": 0.1
    }
  ],
  "sample_count": 5,
  "missing_point_count": 0,
  "missing_evaluated_at": [],
  "truncated": false,
  "point_limit": 120,
  "query_started_at": "2026-10-12T02:00:00+00:00",
  "query_finished_at": "2026-10-12T02:00:00.400000+00:00",
  "requested_range": {
    "start": "2026-10-12T01:50:00+00:00",
    "end": "2026-10-12T01:51:00+00:00"
  },
  "query_range": {
    "start": "2026-10-12T01:50:00+00:00",
    "end": "2026-10-12T01:51:00+00:00",
    "bounds": "[start, end]"
  },
  "step_seconds": 15,
  "expected_point_count": 5,
  "series_labels": {},
  "promql": "sum(rate(demo_http_requests_total{service=\"order-service\"}[1m]))"
}
```

成功结果的所有字段均在示例中展示。status 为 ok、no_data 或 invalid_arguments；失败结果另有受控 reason。没有发送请求时，query_started_at/query_finished_at 为 null；时间参数不合法时，不生成 query_range 等已验证窗口字段。失败信息不返回后端异常正文或凭据。

samples 仅包含验证通过的观测，单点为 evaluated_at UTC 时间和有限 value。顶层不提供即时指标的 value/evaluated_at，避免把序列当作当前单值。series_labels 标识唯一返回的序列；promql 仅在成功时展示受信任的表达式。

missing_point_count 和 missing_evaluated_at 表示缺失的预期求值点，不自动补零，也不插值。空矩阵或空 values 返回 no_data，并列出全部预期缺口； malformed、无法确定的多序列或请求失败的缺失情况保持 null，不伪造完整缺失统计。

有效结果的 truncated=false。超出响应字节、采样数量或结果大小上限时，拒绝整次结果，返回 no_data、空 samples 和 truncated=true；无法确定完整性的其他失败保持 truncated=null。不会把截断的部分数据作为成功证据。

## 传输和采样校验

新增客户端只请求固定 /api/v1/query_range，使用官方 start/end/step 参数。连接和读取 timeout 为 5 秒，另用单调时钟检查响应读取期限；禁用重定向，请求并校验 identity 编码，避免压缩体在库内无限膨胀。Content-Length 和实际读取字节均受 65,536 字节上限约束；不重试、不截取 JSON 前缀当作成功。

只接受 success + matrix + 一个确定序列；空结果有独立语义，多个序列一律拒绝。拒绝非法 JSON、重复 JSON 键、原生非有限常量、非空或类型错误的 warnings/infos，以及不支持的 histogram/混合序列结构。

逐点验证数值、时间戳、时间区间、严格递增、无重复、step 网格及数量上限。拒绝有限性错误、负的计数/延迟/速率、非 0/1 的 up 指标和超出 0～1 的比例。UTC 时间转换发生可检测精度损失也拒绝，防止区间外约 0.2 微秒的点被舍入到合法边界。标签数量、键和值长度也有界，最终工具 JSON 继续受 Runtime 的 65,536 字节限制。

网络 timeout 并不构成对所有 DNS、代理和操作系统阻塞的绝对总耗时保证；此次只验证了 Mock 超时及受控流读取期限。

## Checkpoint、缓存与当前证据隔离

完整且有界的结果沿用 records[].result 持久化，Checkpoint schema_version 保持 1，不新增存储或重置旧数据。临时 SQLite 测试重新打开 RuntimeStore 后核对原始采样和时间元数据仍在，120 点完整序列没有被模型摘要覆盖。

新工具的 fresh 只表示采集结果可在 30 秒内复用，不能表示历史采样点当前有效；历史点不套用即时指标 15 秒新鲜度规则。原 query_metrics 的 15 秒与允许时钟偏差规则保留。

缓存键沿用现有计算，含 Session 服务、工具名、全部参数、相关 Registry 配置及运行版本/进程身份。指标、服务、窗口、step、配置或身份不同，不能错误命中。缓存命中保留原请求时间，不续写观测时间；no_data 等失败结果不作为成功缓存。

effective_records 始终按新工具名称和 evidence_scope=time_window 排除历史指标，并防止带 time_window 标记的结果冒充 query_metrics。即使新工具记录缺少 scope 字段，也不能成为当前证据。模型上下文将其放入 historical_observations，标记 use_for_current_state=false；加入历史成功序列后，即使 provider_verified=true，也使用证据重建，避免完整历史消息链重新绕过隔离。

模型摘要从全序列选择最多 12 个真实点，保留首尾、全序列 min/max、原采样数量、预期数量、缺失数量、至多三个缺失时间示例和摘要裁剪标记；不插值，不填零。单条摘要受 5,000 字节限制，累计历史/调用摘要继续遵守原有上下文上限，降级时仍保留窗口、step、scope 和请求起止。summary_truncated 与原始结果的 truncated 分别表示模型摘要和原始响应的完整性。

工具执行继续通过原 Worker 的参数/目标校验、工具次数与累计预算、租约和 fencing 预留流程，不修改权限或生命周期。

## 实际验证

环境：Windows，D:\conda\envs\agentlearn\python.exe，Python 3.12.14、requests 2.34.2、pytest 9.1.1。完整测试运行于仅含待提交代码的 Git 索引导出快照，避免依赖未提交的提示词或其他工作区改动。

| 验证 | 真实结果 |
| --- | --- |
| P5.2a 指标时间与 Registry 初始基线 | 167 passed，19 subtests passed；9.07 秒。 |
| 最终相关测试：历史指标、即时指标、Registry、日志窗口、Runtime quality | 481 passed，44 subtests passed；88.16 秒。 |
| 最终完整 Python 测试 | 1193 passed，2 skipped，190 subtests passed；349.17 秒（5 分 49 秒）。 |
| 最终完整测试完成时间（UTC） | 2026-10-14T00:46:30.6107528Z |
| 完整快照代码树 | 14098673854267f961e2a204d92c679664af744c（不含本报告）。 |
| 源码/提交范围 | 8 个源码及测试文件；原有即时接口函数体与公式检查通过，diff 检查通过。 |
| 无关工作区保护 | 任务开始已有的 20 个路径，其文件 SHA-256 或删除状态保持一致，不纳入提交。 |

完整测试命令：

```text
D:\conda\envs\agentlearn\python.exe -B -m pytest tests -q --tb=short -ra
```

两项跳过分别是：仅在 CI/干净环境运行的依赖安装校验；需要配置 ALERT_RULES_PATH 的跨仓库告警规则验证。没有把这些项记为通过。

覆盖合法与非法时间、各时区换算、纳秒输入与毫秒对齐、空/多序列、非法和非有限值、数值域、缺失/重复/乱序/区间外/网格外采样、6 小时/15～300 秒/120 点边界、HTTP/JSON/超时/大小/重定向/编码、未注册服务和能力缺失、两种 profile、Checkpoint、缓存命中与过期、历史与当前证据隔离、模型摘要、权限、预算及丢失租约。

### 测试中发现和处理的问题

1. 新增的点数拒绝测试最初把 end 放在了未来，实际正确触发了未来区间拒绝。已修正测试输入，使其只测试点数边界。
2. 时间精度回归先复现两个区间外浮点时间戳被舍入为合法边界并返回 ok；新增的精度损失校验修复此问题，两例及相关回归通过。
3. 一个既有预算测试在完整测试中失败，单独复测也出现 0 != 13；它冻结 Runtime 时间却使用真实 Mock 指标时钟。显式让 Mock 快 6 秒可复现同一失败。仅将 FixtureAgent 时钟改为 Runtime 时钟，保留所有预算和输出断言，修复后相关模块通过。
4. 早期一次完整测试在 test_two_incidents_race_with_real_processes_and_fencing 等待子进程 ready 时出现 30 秒屏障超时；单独复测通过（9.96 秒）。该次根因尚未确定，没有延长超时、跳过测试或修改 fencing/租约生产逻辑。最终完整测试结果按上表原样记录。

## 未验证的环境与边界

本阶段使用 Mock HTTP/模型和临时 RuntimeStore，没有调用付费模型或真实 Prometheus/PostgreSQL exporter，没有制造 Demo 故障，没有操作真实运行数据。未验证真实部署的指标基数、代理/压缩策略、跨主机时钟偏差、保留期和实际后端可用性，也未运行前端构建。

evaluated_at 是 PromQL 表达式求值时间，不是原始 scrape 时间。连续的求值点不能证明每次抓取都成功；现有 rate/histogram/lookback 公式仍可能使用窗口前的原始样本。历史结果、采集时刻新鲜或告警解除均不能单独证明当前业务恢复。

本报告记录本地验证；Git 提交、远端 SHA 及远端 CI 状态以交付回复的实际核查结果为准。
