# inventory-service 背压保护与重试风暴排查手册

- **doc_id**: `inventory-service-backpressure-circuit-breaker`
- **service**: `inventory-service`
- **status**: `active`
- **version**: `v2.0`
- **updated_at**: `2026-11-10`
- **alert_names**: `HighHttp5xxRate`, `HighHttpLatency`
- **error_codes**: `INV_503_BACKPRESSURE`, `ERR_INV_RATE_LIMIT`

## 背压触发机制与错误标识

为防止瞬时高并发打垮内存进程，`inventory-service` 在入口层设置了并发保护与队列上限。当等待队列积压超过安全阈值时，服务会直接返回 HTTP `503 Service Unavailable`，并在响应与日志中记录 `INV_503_BACKPRESSURE` 或限流标识 `ERR_INV_RATE_LIMIT`。

需要注意的是，`503` 背压保护是**主动自我保护行为**，与未捕获的 `500` 代码异常（如 `simulate_inventory_error`）具有完全不同的处理路径。

## 流量突增与级联重试定位

### 联动观测 http_request_rate 与 5xx 曲线

1. 在同一时间窗内同时拉取 `inventory-service` 的 `http_request_rate`、`http_5xx_rate` 与 `http_latency_p95_seconds`。
2. 典型重试风暴特征：
   - 先出现短暂的延迟升高（`http_latency_p95_seconds` 上升）。
   - 紧接着 `http_request_rate` 呈现阶梯式倍增（例如正常基线的 3–5 倍）。
   - 随后大量请求触发 `INV_503_BACKPRESSURE`，使 `http_5xx_rate` 迅速越过 `0.2` 阈值。
3. 核查 Loki 日志 `{service="inventory-service"}` 中来源请求路径与状态码分布，确认是否来自上游客户端或压测脚本的无退避高频重试。

### 抑制重试放大与限流恢复

1. 优先从调用方或网关侧收敛异常重试流量（增加指数退避或熔断开关），而非盲目重启 `inventory-service`（重启期间只会产生更多失败并进一步放大上游重试）。
2. 若确认由单一异常压测源造成，按审批规范在入口层限制突发流量。
3. 观察 `http_request_rate` 回落至基线、队列清空后，`INV_503_BACKPRESSURE` 会自动停止触发，无需额外清理外部缓存。
