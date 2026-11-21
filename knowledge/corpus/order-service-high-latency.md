# order-service 请求耗时飙升与超时排查手册

- **doc_id**: `order-service-high-latency`
- **service**: `order-service`
- **status**: `active`
- **version**: `v2.0`
- **updated_at**: `2026-11-10`
- **alert_names**: `HighHttpLatency`
- **error_codes**: `ERR_ORDER_TIMEOUT`

## 告警触发条件与延迟指标

当 `order-service` 业务接口的 P95 响应耗时显著升高时，Prometheus 会触发 `HighHttpLatency`（级别 `warning`）。其核心计算表达式基于直方图指标 `demo_http_request_duration_seconds_bucket{service="order-service"}`：
- 1 分钟窗口内 `http_latency_p95_seconds > 0.5` 秒（500ms）。
- 1 分钟内观测样本数 `demo_http_request_duration_seconds_count >= 20`。
- 注意：`/health` 和 `/metrics` 不计入业务直方图，因此不能用 `/health` 响应快反证业务接口无延迟。

当上游网关或调用方因耗时过长主动中断请求时，业务侧常伴随超时标识 `ERR_ORDER_TIMEOUT`。

## 根因定位流程

高延迟在证据未齐备前只是现象，常见来源分为两类：应用自身请求拦截注入的额外耗时，以及下游 `order-db` 查询/锁等待导致的阻塞。

### 检查慢请求运行开关与版本

1. 检查 `order-service` 运行时信息中的 `allowed_flags` 状态，重点核对 `simulate_slow_request`（环境变量 `SIMULATE_SLOW_REQUEST`）。
2. 在 `http_observability.py` 中，当 `slow_enabled()` 返回真值时，所有命中业务路径（`/`、`/error`、`/order`）的请求会在 `@app.before_request` 阶段固定休眠 `SLOW_REQUEST_SECONDS = 0.8` 秒，直接把 P95 推高至 `0.8s` 以上并触发 `HighHttpLatency`。
3. 注意：仅看到 `simulate_slow_request=true` 且 P95 升高时，仍属待核实假设，必须结合直方图分桶分布及版本启动时间 `PROCESS_STARTED_AT`、日志时间戳确认耗时确实出现在请求处理前段。

### 区分应用层延迟与下游 order-db 慢查询

若 `simulate_slow_request` 为关闭状态，或 P95 远超 `0.8s`（例如接近 `3.0s` 并引发 `ERR_ORDER_TIMEOUT`）：
1. 对比 `/` 与 `/order` 的响应耗时与日志出现频率：`/` 不访问数据库，若仅 `/order` 慢而 `/` 正常，重点怀疑 `order-db` 查询阻塞。
2. `order-service` 连接 `order-db` 时配置了 `connect_timeout=3` 秒与 `statement_timeout=3000` 毫秒。若数据库存在行锁或表锁等待，`/order` 会在等待满 3 秒后抛出 `database_query_error`（而非一直挂起）。
3. 联动查询 `order-db` 的 `postgres_lock_waits` 与 `postgres_active_sessions` 指标，确认是否存在 `Lock` 等待事件。

## 缓解与恢复确认

- 若根因为 `simulate_slow_request` 误开启且属于受控演练残留，按标准变更/审批流程关闭环境变量并重启服务，避免未经授权直接操作进程。
- 若根因为数据库锁等待，参考《order-db 锁等待与慢查询阻塞排查手册》（`order-db-lock-waits-and-slow-queries`）解除阻塞会话。
- 恢复后需观测 `http_latency_p95_seconds` 持续回落至 `0.5s` 以下（正常区间通常在 `0.05s` 以内），且 `ERR_ORDER_TIMEOUT` 报错归零。
