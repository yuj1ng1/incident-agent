# inventory-service 存活检查与抓取中断排查手册

- **doc_id**: `inventory-service-service-down`
- **service**: `inventory-service`
- **status**: `active`
- **version**: `v2.0`
- **updated_at**: `2026-11-10`
- **alert_names**: `ServiceDown`
- **error_codes**: `ERR_INV_UNREACHABLE`, `ERR_SCRAPE_TARGET_DOWN`

## 告警含义与核实要点

`inventory-service` 触发 `ServiceDown` 表示 Prometheus 对 `job="inventory-service"` 的 `up` 指标在 10 秒以上处于 `< 1` 状态（`service_scrape_up == 0`）。

与所有 HTTP 服务一致，排查需区分“进程真的挂了”还是“Prometheus 抓不到”：
- `service_scrape_up == 0` 只证明抓取失败，不等于进程已终止。
- 不要拿历史窗口内的 `INV_503_BACKPRESSURE` 或慢请求记录充当当前 `ServiceDown` 的根因。

## 端口 8002 与指标抓取排查

### 核对服务健康接口与容器状态

1. 访问 `inventory-service` 默认地址 `http://localhost:8002/health`（容器网络内按服务名 `inventory-service:8002` 访问）。
2. 若返回 `Connection refused`（`ERR_INV_UNREACHABLE`），说明 `8002` 端口未监听，检查容器是否退出、端口是否被其他本地进程占用或启动命令失败。
3. 若 `/health` 返回 `200` 但 Prometheus 仍报 `ServiceDown`，检查 `/metrics` 端点是否阻塞超时以及 `prometheus.yml` 中 `inventory-service` 的目标配置。

### 检查 Loki 日志与进程异常退出

1. 查询 Loki `{service="inventory-service"}` 告警前后的输出，关注启动期异常（如环境变量缺失、模块导入失败、端口绑定报错 `Address already in use`）。
2. 恢复服务并确认 `service_scrape_up == 1` 后，继续观测 1 分钟以上的业务请求与健康响应，确保没有连带触发 `HighHttp5xxRate`。
