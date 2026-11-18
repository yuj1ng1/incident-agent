# inventory-service HTTP 5xx 与背压错误排查手册

- **doc_id**: `inventory-service-high-5xx`
- **service**: `inventory-service`
- **status**: `active`
- **version**: `v2.0`
- **updated_at**: `2026-11-10`
- **alert_names**: `HighHttp5xxRate`
- **error_codes**: `INV_503_BACKPRESSURE`, `ERR_INV_INTERNAL`

## 服务特征与无数据库依赖边界

`inventory-service`（默认监听 `http://localhost:8002`）在服务注册表中是一个**无外部数据库依赖**的独立 HTTP 服务（`dependencies` 为空列表，`source.investigation_paths` 为 `inventory_app.py` 与 `http_observability.py`）。

排查 `inventory-service` 告警时的核心约束：
- **严禁将 `inventory-service` 的 5xx 或慢请求归因于 `order-db`（PostgreSQL）**。即使同一时间 `order-db` 恰好在波动，也不能跳过注册依赖边界做跨服务猜测。
- `inventory-service` 当前未配置 `disable_demo_regression` 等自动修复白名单，处置需依赖运行时开关核实或受控版本回退。

## 常见 5xx 错误码与故障模式

当 `http_5xx_rate > 0.2`（1 分钟内请求样本不少于 `20` 次且 `up == 1`）时，Prometheus 触发 `HighHttp5xxRate`。通过 Loki 查询 `{service="inventory-service"}` 区分两类典型错误：

### INV_503_BACKPRESSURE 限流背压触发

- 若日志出现 `status=503` 与错误标识 `INV_503_BACKPRESSURE`，表明服务工作队列或并发信号量已达到保护阈值，主动拒绝新请求以避免雪崩。
- 需同步检查 `http_request_rate` 是否在短时间内陡增，以及是否存在上游重试风暴，详见《inventory-service 背压保护与重试风暴排查手册》（`inventory-service-backpressure-circuit-breaker`）。

### simulate_inventory_error 故障注入识别

- 若日志出现 `status=500` 与 `ERR_INV_INTERNAL`，通过运行时信息检查 `allowed_flags` 中的 `simulate_inventory_error` 是否被开启。
- 检查 `inventory_app.py` 的变更记录与进程启动时间，确认是否因演练开关残留或近期代码变更在核心库存查询逻辑中抛出异常。

## 处置路径与恢复核查

1. 若为 `simulate_inventory_error` 误开启，由人工确认后按标准配置变更流程关闭并重启实例，禁止未经授权直接修改运行环境。
2. 恢复后验证 `GET /health` 返回 `200`，业务接口持续 1 分钟无 `5xx` 样本（`http_5xx_rate == 0`），且 Loki 中不再输出 `INV_503_BACKPRESSURE` 或 `ERR_INV_INTERNAL`。
