# Incident Agent P5.2c 多指标统一评估时间与关联证据验证

## 实施范围

新增只读工具 query_metrics_aligned(service, metrics, at)，比较同一已授权服务或依赖的 2～4 个 MetricProfile 指标。多次 Prometheus instant query 共用一个先验证、再标准化的 UTC 评估时刻。工具保留每项指标自己的 PromQL、HTTP 请求起止时间、实际样本时间、数值或受控失败原因。完整结果沿用现有 Worker 和 Checkpoint 记录路径。

没有改变 query_metrics、query_metric_range 的公开参数和结果语义；没有修改 PromQL 公式、Registry、Checkpoint schema、状态机、预算或审批。复用 ServiceRegistry、MetricProfile、工具注册、Session 服务/依赖授权、缓存、SQLite Runtime 和现有数值域检查。没有新增依赖。

## 修改文件

| 文件 | 修改内容 |
| --- | --- |
| [clients/prometheus_client.py](../clients/prometheus_client.py) | 复用毫秒时间解析，增加有界指定时刻查询和共享总时限；保留现有 range 与即时请求接口。 |
| [tools/metrics.py](../tools/metrics.py) | 增加受控多指标工具、时间和参数验证、逐项响应解析与一致性提示。 |
| [tools/__init__.py](../tools/__init__.py) | 导出并注册新工具。 |
| [investigation/evidence_policy.py](../investigation/evidence_policy.py) | 增加新工具缓存 TTL，并将其列入历史证据排除条件。 |
| [investigation/worker.py](../investigation/worker.py) | 为模型上下文生成保留全部指标和时间元数据的有界摘要。 |
| [tests/test_metrics_aligned.py](../tests/test_metrics_aligned.py) | 增加新工具的参数、时间、HTTP、Checkpoint、权限、预算和历史证据回归。 |
| [tests/test_service_registry.py](../tests/test_service_registry.py) | 在注册表边界测试中登记新只读工具。 |
| 本报告 | 记录实际实现、完整测试结果和本地 Prometheus 联调。 |

## 参数和时间规则

三个必填参数均为字符串。metrics 是逗号分隔的指标名，不允许空项、重复项、PromQL、未知项或超过四项的列表；单个名称最多 64 字符，总长度最多 259 字符。每个指标都须属于同一服务的 MetricProfile。Worker 在执行前仍按 Session 检查服务目标；对未授权目标的直接模型调用会被拒绝并计入零次工具执行。

at 必须是有效 RFC3339 日期，包含 Z 或明确 UTC 偏移，最多九位小数；拒绝无时区、未知偏移 -00:00、Unix epoch 之前、未来以及超出本地 Prometheus 保留期的时间。检查原始输入后，按 Prometheus 毫秒时间戳向下取整。结果同时返回原始 requested_evaluation_time、实际 evaluation_time、毫秒精度和 evaluation_time_adjustment_ns；向下取整最多改变不到 1 毫秒的时间，调整值为零或负值。

在执行实现时只读核对了本地 Prometheus 3.15.0 的状态：保留期为 15d。代码据此将最大回溯设为 15 天；这样不会接受当前后端保留范围以外的请求。该上限跟随当前环境配置；部署修改 Prometheus 保留期后，应同步核对该值。P5.2b 的单指标历史工具继续使用自身最长 6 小时区间，不受本次修改影响。

## 返回结构与状态语义

顶层返回 status、service、evidence_scope、use_for_current_state、请求和实际评估时间、时间精度与取整偏移、requested_metrics、comparison_complete、results、consistency_warnings、limitations、limits，以及整个工具调用起止时间。

results 按请求顺序为每项指标保存独立状态、metric 名称、受信任 PromQL、request_executed、HTTP 起止时间。成功项还包括 value、evaluated_at 和 series_labels；失败项包括受控 reason，样本时间不匹配时另记录 observed_evaluated_at。以下结构展示全部字段；方括号中的说明值是占位标记：

    {
      "status": "ok | incomplete | no_data | invalid_arguments",
      "service": "order-service",
      "evidence_scope": "time_window",
      "use_for_current_state": false,
      "requested_metrics": ["http_request_rate", "http_5xx_rate"],
      "comparison_complete": true,
      "results": {
        "http_request_rate": {
          "status": "ok",
          "metric": "http_request_rate",
          "promql": "由现有 MetricProfile 生成的表达式",
          "query_started_at": "UTC HTTP 起始时间",
          "request_executed": true,
          "query_finished_at": "UTC HTTP 结束时间",
          "value": 0.41821223361699034,
          "evaluated_at": "2026-10-11T10:48:55+00:00",
          "series_labels": {}
        },
        "http_5xx_rate": {
          "status": "no_data",
          "metric": "http_5xx_rate",
          "promql": "由现有 MetricProfile 生成的表达式",
          "query_started_at": "UTC HTTP 起始时间",
          "request_executed": true,
          "query_finished_at": "UTC HTTP 结束时间",
          "reason": "no_samples_at_evaluation_time"
        }
      },
      "consistency_warnings": [],
      "limitations": [
        "相同评估时间不保证底层指标抓取时间相同。",
        "多个 HTTP 查询不具有事务级原子性。",
        "历史观测不能证明当前健康或根因。"
      ],
      "limits": {
        "max_metrics": 4,
        "max_lookback_days": 15,
        "response_bytes_per_metric": 16384,
        "total_timeout_seconds": 10
      },
      "query_started_at": "UTC 工具调用起始时间",
      "query_finished_at": "UTC 工具调用结束时间",
      "requested_evaluation_time": "2026-10-11T10:48:55.000Z",
      "evaluation_time": "2026-10-11T10:48:55+00:00",
      "timestamp_resolution": "1ms",
      "evaluation_time_adjustment_ns": 0
    }

所有请求都返回唯一序列、合法有限数值和与共同评估时刻严格一致的 Prometheus 时间戳，才会令 comparison_complete=true、顶层 status=ok。缺结果、多序列、警告、非法/非有限值、时间戳偏差或单项 HTTP 失败都会将该指标标成 no_data。剩余有效观测照实保留；部分成功为 incomplete，全部失败为 no_data，缺失值不填零。

仅当 http_request_rate 为零且 http_5xx_rate 为正、并且比较完整时，才生成候选一致性提示。提示要求进一步核查数据，不宣称根因或因果关系。

每项 HTTP 响应最多读取 16 KiB，四项总量最多 64 KiB；校验 JSON 重复键、warnings/infos、标签与数值。多指标共用最多 10 秒的单调时钟请求预算，每次请求最多 5 秒，不重试。同步网络库和操作系统仍可能使实际返回略超出预算；该上限是客户端控制，不构成操作系统级的硬截止保证。

Prometheus 官方 instant query 接口用 time 参数指定求值时刻：[HTTP API 文档](https://prometheus.io/docs/prometheus/latest/querying/api/#instant-queries)。

## Checkpoint、摘要与证据边界

完整、经过校验的逐项结果写入既有 records[].result；Checkpoint schema 仍为 1。结果的缓存键沿用当前策略，包含 Session 服务、工具名和全部参数、关联 Registry 配置及运行身份；指定时间、指标组合、目标、配置或身份不同就会产生不同缓存键。成功观测可在 30 秒内复用；no_data 不作为成功缓存。缓存复用不更新历史评估时间。

evidence_policy 给新工具设置 30 秒缓存 TTL，并无条件将 query_metrics_aligned 从 effective_records 排除。每条结果还带 evidence_scope=time_window 和 use_for_current_state=false。Worker 重建上下文时保留全部请求指标名、共同评估时间、各项数值或原因、完整性状态、请求时间和局限性；摘要至多四项，省略完整 PromQL 和标签。历史观测不能通过旧模型响应链绕过当前证据过滤。源 Checkpoint 保留全部结果，不被摘要覆盖。

求值时刻只说明 Prometheus 在该时刻计算表达式。指标可能使用不同的原始采集样本或函数窗口；多个独立 HTTP 请求之间也没有事务原子性。不得把共同求值时刻误说成共同抓取时刻，也不得把历史结果纳入当前健康或恢复结论。

## 验证结果

验证环境使用 D:\conda\envs\agentlearn\python.exe、Python 3.12.14、requests 2.34.2、pytest 9.1.1；测试在只包含本阶段暂存源码和测试的归档快照中运行。快照 Git tree：4d3a22626576859dee266cc98ca236dd5058696d。

- 相关回归：661 passed，44 subtests passed。
- 最终完整 Python 测试：1373 passed，2 skipped，190 subtests passed，用时 535.77 秒。
- 跳过项是仅在干净 CI/隔离环境运行的依赖检查，以及需要 ALERT_RULES_PATH 的跨仓库告警规则验证；未将二者记为通过。
- 实际覆盖参数白名单、服务和 Session 权限、UTC 标准化、毫秒取整、15 天边界、各类无效时间、两组固定历史时刻、共同请求 time、整数/小数样本时间、样本错位、多序列、空结果、合法零、比例 1、no_data、部分 HTTP/网络失败、大小和总时限、Checkpoint、缓存隔离、摘要、当前证据排除、预算和 fencing。没有付费模型调用。
- 代码与 staged 范围检查通过；比较 AST 确认 query_metrics、query_metric_range、原 Prometheus query 及其读告警接口等已有函数体未被改写；原 PromQL profile、服务 Registry、Checkpoint、配置和预算源码均未改变。
- 任务开始前已有 20 个未提交或未跟踪路径已逐一核对文件哈希/删除状态，无一变化；均未包含在本阶段暂存内容中。

### 本地 Prometheus 只读联调

本地 /-/ready 返回 HTTP 200；只读版本与 flags 接口报告 Prometheus 3.15.0、retention=15d。随后分别运行两次新工具调用，每次查询同一服务的 http_request_rate、http_5xx_rate、service_scrape_up，没有启动故障 Demo 或模型调查。

| UTC 评估时间 | http_request_rate | http_5xx_rate | service_scrape_up | 整体比较 |
| --- | ---: | --- | ---: | --- |
| 2026-10-11T10:48:55.000Z | 0.41821223361699034 | 1.0 | 1.0 | 完整 |
| 2026-10-11T10:48:55.300Z | 0.0 | no_data | 1.0 | 不完整 |

第二次调用按预期返回 status=incomplete、comparison_complete=false，没有把 5xx 的 no_data 变为零。两个时间点是分别独立的调用；没有混合各指标观测。本次只读联调与所给历史数据相符，但不能证明历史原始查询保存了同一评估时间，也不能据此绝对确认过去的根因。

未验证真实 Prometheus 不同保留配置、外部代理/阻塞故障、非本机时钟误差、模型 API，以及多请求间底层抓取或数据写入一致性。完整测试使用 Mock HTTP、Mock 模型和临时 SQLite；真实联调只覆盖当前本机 Prometheus。
