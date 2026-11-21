# order-service 抓取中断与服务不可达排查手册

- **doc_id**: `order-service-service-down`
- **service**: `order-service`
- **status**: `active`
- **version**: `v2.0`
- **updated_at**: `2026-11-10`
- **alert_names**: `ServiceDown`
- **error_codes**: `ERR_SCRAPE_TARGET_DOWN`, `ERR_CONNECTION_REFUSED`

## 告警语义与常见误区

当 Prometheus 对 `order-service` 的指标抓取失败并持续 10 秒时，会触发 `ServiceDown`（`service_scrape_up` 即 `up{service="order-service"} < 1`）。

排查时必须牢记以下约束，避免得出错误结论：
- `up=0` 仅证明 Prometheus 当前无法成功抓取目标的 `/metrics` 端点，**不能直接证明业务进程已经退出**。网络隔离、端口监听异常、防火墙拦截或 `/metrics` 响应超时均会导致 `up=0`。
- 禁止把告警发生之前历史窗口中的 `5xx` 或数据库错误日志当作当前抓取失败的原因。

## 分步排查流程

### 验证实例监听与健康端点

1. 使用服务健康检查工具核对 `order-service`（默认地址 `http://localhost:8001`）的 `/health` 连通性。
2. 若健康检查返回 `ERR_CONNECTION_REFUSED`（连接被拒绝），说明 `8001` 端口无进程监听，需检查容器状态或宿主机监听进程是否异常退出。
3. 若 `/health` 返回 `200` 正常，但 `up=0`，说明服务进程仍在运行，问题出在 Prometheus 抓取链路或 `/metrics` 接口本身。

### 检查 Prometheus 抓取目标与网络连通

1. 查询 Prometheus `targets` 状态，查看 `order-service` 对应 target 的 `health` 与 `lastError` 字段（例如 `context deadline exceeded`、`connection refused` 或 `no such host`）。
2. 在 Docker Compose 环境中，核对 `prometheus.yml` 中的抓取目标服务名与容器网络是否在同一桥接网络内，避免将容器内服务名与宿主机 `127.0.0.1` 混用。
3. 查看 Loki 日志 `{service="order-service"}` 在告警触发前 2 分钟是否有未捕获异常、端口绑定冲突或 OOM 终止迹象；若日志也完全中断，应优先检查容器重启次数与退出码。

## 恢复与业务观测

1. 恢复抓取后，先确认 `service_scrape_up == 1`。
2. 再通过 `GET /order` 验证业务读链路返回 `200` 及正常订单数据，确保服务不仅“可被抓取”，而且业务能力完整可用。
3. 由于 `HighHttp5xxRate` 和 `HighHttpLatency` 规则均要求 `up == 1` 才触发，服务恢复抓取后的前 1 分钟内应继续观察 5xx 率与 P95 延迟，防止掩盖潜在并发故障。
