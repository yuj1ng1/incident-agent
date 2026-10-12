# P5.2a Prometheus 指标时间元数据与证据可靠性验证

## 范围与结论

本次完成即时指标查询的时间可追溯性升级。未重放或重新调查历史 Session `ses-89880a37cf63490e9685749f7879bc5e`；用户提供的历史请求速率差异仍没有经过本次任务确认的根因。本次实现不能据此证明历史 `http_request_rate=0` 是缓存、抓取或某种 PromQL 行为导致的。

复用原 PrometheusClient、ServiceRegistry、受控 MetricProfile 模板、Worker 工具结果持久化和 EvidencePolicy。没有新增第三方依赖、工具、数据库表或 Checkpoint schema。HTTP 与 PostgreSQL PromQL、告警规则、Demo 埋点、审批权限、恢复验证和调查预算保持原有实现。

## 修改文件

| 文件 | 实际修改 |
| --- | --- |
| [clients/prometheus_client.py](../clients/prometheus_client.py) | 可选注入时钟；记录请求开始与响应接收时间；保留原异常类型及对象，通过异常属性携带失败尝试时间；增加时间戳转换与新鲜度校验函数。 |
| [tools/metrics.py](../tools/metrics.py) | 保留原调用参数与指标值校验；解析样本评估时间；成功结果增加三项时间字段；拒绝不可靠时间；实际请求的无数据结果携带可获得的请求时间。 |
| [investigation/evidence_policy.py](../investigation/evidence_policy.py) | 维持 15 秒保存时间 TTL，同时检查指标评估时间；没有有效评估时间的历史指标不能成为当前证据。 |
| [investigation/worker.py](../investigation/worker.py) | 仅调整超过 5 KB 的单结果摘要和超过 16 KB 的调用摘要简化分支，保留指标时间字段。 |
| [tests/test_metric_timestamps.py](../tests/test_metric_timestamps.py) | 新增时间、异常、权限、接口兼容、缓存、临时 Checkpoint 与实际工具上下文回归。 |
| [tests/runtime_support.py](../tests/runtime_support.py) | Mock 指标补充当前评估时间，可注入时钟；显式历史时间字段优先。 |
| [tests/test_incident_regression.py](../tests/test_incident_regression.py) | 将原 Unix 0 的当前样本夹具改为测试时钟；更新成功结果的完整字段断言，保留原业务断言。 |
| [tests/test_service_registry.py](../tests/test_service_registry.py) | 当前样本使用当前 UTC Unix 时间，保留能力与配置驱动查询断言。 |
| [tests/test_runtime_quality.py](../tests/test_runtime_quality.py) | 配置变更失效测试的当前指标夹具补充有效评估时间，保留原缓存隔离断言。 |

以下相关模块已经实际阅读，并保持现有职责与实现：`services/metric_profiles.py`、`services/registry.py`、`investigation/checkpoint.py`、`tools/tool_registry.py`、RuntimeStore 与 Checkpoint 数据模型。提示词既有本地修改没有纳入本次提交。

## 时间字段与校验规则

[Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/#instant-queries) 说明即时查询在一个时刻评估表达式，未指定 `time` 时使用服务器当前时间，响应时间戳为 Unix 秒。本次请求仍只发送原有 `query`，不增加查询时间参数。

| 字段 | 含义 |
| --- | --- |
| `evaluated_at` | 单个向量浮点样本 `value[0]` 转换的 UTC ISO 8601 评估时刻。它不是底层原始指标的抓取时间。 |
| `query_started_at` | 客户端发起 HTTP 请求前的本地 UTC 时间。 |
| `query_finished_at` | HTTP 响应接收后的本地 UTC 时间，早于状态检查和 JSON 解析；没有响应的传输失败时，表示尝试结束时间。 |
| `collected_at` | Worker 保存观测的时间，继续保留原含义，不替代上述字段。 |

校验样本结构、有限数值和原有指标语义之后，校验样本时间：

1. 时间戳必须是 JSON 数字对应的 Python `int` 或 `float`。布尔值、字符串（包括数字字符串）、缺失值和其他结构拒绝。
2. 拒绝 NaN、正负 Infinity，以及转换数值时的溢出。
3. Unix 秒范围为 `0 <= timestamp < 253402300800`，上界是 UTC 年 10000 的排他边界；转换产生的 ValueError、OverflowError、OSError 均转为受控无数据。
4. 使用 `datetime.fromtimestamp(timestamp, timezone.utc)` 转换整数或小数秒；输出 `+00:00` UTC ISO 8601，精度遵循 Python datetime 的微秒精度。
5. 接收时的评估年龄必须满足 `-5 <= query_finished_at - evaluated_at <= 15` 秒。允许最多 5 秒服务器时钟领先，15 秒以内的网络延迟与时钟滞后正常计入年龄。边界包含在内。
6. 请求开始与完成时间必须有时区且顺序合法；无法证明请求时间可靠时返回 `invalid_query_timing`。
7. 数值 `0.0` 在有效时间下仍是 `status=ok`，不能转换成无数据。

15 秒评估年龄和 5 秒允许偏差是本项目本次选择的保守策略，不是 Prometheus 协议固定阈值。明显时钟不同步时可能返回无数据，应核对机器时钟与传输延迟。

## 最终返回结构与兼容性

成功结果为原有五个字段加三项时间字段。例如下面是隔离 Mock 的观测，非真实 Prometheus 测量：

```json
{
  "status": "ok",
  "service": "order-service",
  "metric": "http_request_rate",
  "value": 0.41821223361699034,
  "promql": "sum(rate(demo_http_requests_total{service=\"order-service\"}[1m]))",
  "evaluated_at": "2026-10-12T02:00:00+00:00",
  "query_started_at": "2026-10-12T02:00:00+00:00",
  "query_finished_at": "2026-10-12T02:00:00.400000+00:00"
}
```

| 场景 | 返回 |
| --- | --- |
| 有效单个样本 | `status/service/metric/value/promql` 保留，增加三项时间元数据。 |
| 空结果、多结果、非法值、非法样本时间、过期/未来样本、无效响应 | `status=no_data`，保留原服务、指标、已有 reason/message/promql 语义，附加可靠的请求起止时间；不返回 `value` 或 `evaluated_at`。空结果仍保持原有无 reason 的结构。 |
| 请求异常或超时、HTTP 或 JSON 解码错误 | 原有 `metrics_unreachable_or_invalid`，携带尝试开始与结束时间，不泄露异常详情，不伪造评估时间。 |
| 服务/能力未登记，或 profile 不支持指标 | 查询没有发生；沿用原结构，没有查询时间或评估时间。 |
| 请求时间元数据损坏 | `status=no_data/reason=invalid_query_timing`，不输出无法信任的时间。 |

真实客户端的字典响应采用 HTTP 时间。直接替换 `query()` 的普通 Mock，以及非字典非法 JSON 响应无法携带客户端时间时，工具记录实际 `query()` 调用的起止边界；该边界包含调用包装和解析开销，不伪装成 Prometheus 评估时间。

`query_metrics(service, metric)` 的必填参数、Tool Schema 的 properties/required、工具注册名与说明保持兼容。没有加入任意 PromQL 或历史查询参数。`PrometheusClient.query(promql)` 的原参数和 JSON envelope 保持，字典响应只增加两个字段；原异常类型及异常对象继续抛出。`get_alerts()`、`get_rules()`、`get_targets()` 的参数、请求和响应未改变。

## Checkpoint、缓存与模型上下文

工具结果通过现有 `ToolManager → Worker._tool → protect → RuntimeStore.checkpoint` 路径进入 `records[].result`。新增时间字段是 JSON 字符串，原 `schema_version=1` 和数据库表均可直接承载。

缓存仍要求工具执行成功、结果 `status=ok`、原缓存身份与参数匹配，以及 `collected_at` 的年龄不超过 15 秒；指标额外要求 `evaluated_at` 在当前时钟的同一新鲜度窗口内。保存延迟不能重新延长旧评估结果的有效期。

缓存命中返回原始结果并引用原 observation，没有增加新采集记录，也没有改写三个时间字段。过期后执行新的只读查询；旧结果继续留作历史观测。缺少评估时间的旧 Checkpoint 可读取、可保留，但不会被缓存或作为当前指标证据使用。这可能增加一次预算内的只读查询，不重置或扩大预算。

实测的隔离 Worker 流程证明时间字段出现在有效证据、历史摘要以及 recent_function_call_results 中。超过现有摘要大小限制时仍保留时间。未修改模型提示词，没有进行多指标时间序列分析。

## 实际验证

环境：Windows，Python 3.12.14，requests 2.34.2，Pydantic 2.13.5，pytest 9.1.1，解释器 `D:\conda\envs\agentlearn\python.exe`。`pip check` 返回 `No broken requirements found.`。

所有新增测试使用 HTTP/模型 Mock 或临时 IncidentStore/RuntimeStore。完整测试使用独立导出的准备提交代码与测试快照，没有复制工作区无关提示词修改或真实运行数据。

| 验证 | 实际结果 |
| --- | --- |
| 修改前完整 Python 基线 | 884 passed, 2 skipped, 190 subtests passed；396.41 秒。 |
| 三个修改前缺口复现 | 3 failed：过旧评估仍可缓存、缺少评估时间仍是当前证据、过大摘要丢失时间。 |
| 工具/Registry/指标/部分 Runtime 相关回归 | 276 passed, 1 skipped, 51 subtests passed；43.93 秒。 |
| 最终新增时间回归与日志摘要 | 112 passed；9.66 秒。 |
| 完整准备提交代码与测试快照 | 984 passed, 2 skipped, 190 subtests passed；345.27 秒。 |
| 暂存 diff 与范围 | 仅本次源码、测试和本报告；diff 检查通过；凭据模式与真实运行数据路径检查通过。 |
| 无关工作区修改保护 | 原有 20 个路径的文件哈希或删除状态保持原样；不纳入本次暂存。 |

完整测试命令：`D:\conda\envs\agentlearn\python.exe -B -m pytest tests -q --tb=short -ra`。

完整测试中的跳过项：干净依赖安装检查只在 CI/隔离环境运行；跨仓库告警规则验证未设置 `ALERT_RULES_PATH`。。

新增回归覆盖整数/小数时间戳、合法零、缺失/布尔/非数字/非有限/巨大时间戳、未来/过期/偏差边界、空/多结果、非法数值、请求超时/异常、HTTP/JSON 错误、非法响应、客户端时间不可被服务器覆盖、普通 Mock 兼容、HTTP 和 PostgreSQL 全部现有 profile 指标、Schema 和权限、临时 Checkpoint、原时间缓存命中与过期重查、旧记录保留及两种摘要截断路径。

## 尚未验证与已知边界

没有调用真实 Prometheus、付费模型或制造业务故障；未对给出的历史 Session 做根因验证，未验证真实部署的机器时钟偏差、代理链路或极端网络延迟。没有运行前端构建，也未将本地 pytest 结果等同于远端 GitHub Actions 结果。

即时查询的评估时间只能证明表达式在哪个时刻计算，不能保证底层抓取样本同样新鲜；PromQL 的 lookback、窗口与指标抓取间隔仍需单独取证。各次即时查询也仍有各自评估时刻，本次不强制它们对齐。

## P5.2b 最小扩展建议（未实现）

继续复用 ServiceRegistry 与受控 MetricProfile，只增加客户端 `query_range` 和单指标的受控时间序列工具。由 Python 限制窗口、step、点数和响应大小，解析矩阵并复用单点数值/Unix 时间转换校验；历史点按请求区间验证，不能套用当前即时样本 15 秒新鲜度判断。

结果标记 `evidence_scope=time_window`，保留请求开始/结束和每点评估时间；通过现有 `records[].result` 持久化。缓存键包括服务、指标、区间、step 和已有版本/实例身份；历史序列不能成为当前健康状态证据。保持现有即时工具、审批、恢复验证和预算边界，先覆盖空/多序列、非法点、区间边界与大小限制再进行真实只读联调。
