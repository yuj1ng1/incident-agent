# inventory-service 高延迟与 P95 超标排查手册

- **doc_id**: `inventory-service-high-latency`
- **service**: `inventory-service`
- **status**: `active`
- **version**: `v2.0`
- **updated_at**: `2026-11-10`
- **alert_names**: `HighHttpLatency`
- **error_codes**: `ERR_INV_TIMEOUT`

## 延迟指标与资源画像

当 `inventory-service` 的 `http_latency_p95_seconds > 0.5` 秒（1 分钟窗口内样本数 `>= 20` 且 `up == 1`）时，会触发 `HighHttpLatency` 告警，上游调用超限则记录 `ERR_INV_TIMEOUT`。

评估延迟原因时需联合采集以下四类 Prometheus 指标：
- `http_latency_p95_seconds`：确认延迟突增起始时间与持续时长。
- `http_request_rate`：判断延迟是否伴随突发流量峰值。
- `process_cpu_cores`：排查是否出现 CPU 饱和或死循环计算。
- `process_resident_memory_bytes`：排查内存持续增长或频繁 GC 停顿。

## 排查步骤

### 检查运行时慢请求标志位

1. 读取 `inventory-service` 的运行时版本与配置状态，核对 `allowed_flags` 中的 `simulate_slow_request`。
2. `inventory-service` 与 `order-service` 共享同一套 `http_observability.py` 观测中间件：当 `simulate_slow_request` 为真时，业务请求在进入路由前会额外阻塞 `0.8` 秒，导致 P95 稳定落在 `0.8s` 附近。
3. 请勿仅凭标志位开启就直接结案，仍需核对 `http_latency_p95_seconds` 实际分位值与 `inventory_app.py` 源码路径，确认不存在其他代码级阻塞。

### 排除不存在的下游数据库误判

- 根据服务注册表，`inventory-service` **没有注册任何 PostgreSQL 或 Redis 依赖**。
- 当看到 `HighHttpLatency` 时，禁止套用 `order-db` 的锁等待或慢 SQL 结论来解释 `inventory-service` 的延迟；若需要排查计算耗时，应聚焦 `inventory_app.py` 自身逻辑及宿主机 CPU/内存资源。

## 缓解建议

- 若确认为 `simulate_slow_request` 开启所致，按受控变更流程关闭标志位并重启服务。
- 若为突发流量导致 CPU 饱和与排队延迟，配合限流与背压策略降低并发压力，并确认恢复后 `http_latency_p95_seconds < 0.5s` 且无 `ERR_INV_TIMEOUT`。
