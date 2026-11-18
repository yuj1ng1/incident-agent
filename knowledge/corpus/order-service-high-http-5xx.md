# order-service HTTP 5xx 告警排查手册

- **doc_id**: `order-service-high-http-5xx`
- **service**: `order-service`
- **status**: `active`
- **version**: `v2.0`
- **updated_at**: `2026-11-10`
- **alert_names**: `HighHttp5xxRate`
- **error_codes**: `ERR_CODE_REGRESSION`, `application_error`

## 适用范围与告警标识

本手册适用于 `order-service`（默认监听端口 `8001`）触发 `HighHttp5xxRate` 告警时的标准分流与根因排查。

在 Prometheus 告警规则中，`HighHttp5xxRate` 会在满足以下全部条件持续 10 秒后触发：
- 过去 1 分钟（`1m`）内业务接口 `demo_http_requests_total{service="order-service",status=~"5[0-9][0-9]"}` 占总请求比率 `http_5xx_rate > 0.2`。
- 过去 1 分钟业务请求总量不少于 `20` 次（`minimum_requests=20`），避免低流量样本抖动误报。
- 实例采集状态 `up{service="order-service"} == 1`；若 `up == 0`，应先按 `ServiceDown` 排查抓取连通性。

## 快速分流：应用异常与数据库异常

`order-service` 的 `/order` 接口在每次请求中会先连接 `order-db` 执行只读 SQL 查询，连接关闭后再执行应用层业务逻辑。因此收到 `HighHttp5xxRate` 时，必须先区分故障来自下游 `order-db` 还是应用自身代码异常，禁止未取证直接重启。

### 判断日志中的 error_type

通过 Loki 查询 `{service="order-service"}` 最近 5 分钟错误日志，重点关注 `/order` 与 `/error` 的返回字段：
1. 若日志出现 `path=/order status=500 error_type=database_connection_error` 或 `database_query_error`，且伴随 `demo_dependency_up{service="order-service",dependency="order-db"} == 0`，说明问题位于数据库依赖链路，应转入《order-service 下游 PostgreSQL 依赖故障排查手册》（`order-service-db-dependency-failure`）与 `order-db` 手册。
2. 若日志出现 `path=/order status=500 error_type=application_error exception_type=RuntimeError`（错误代码归类为 `ERR_CODE_REGRESSION`），而 `demo_dependency_up == 1`，说明数据库查询已成功，故障发生在应用业务处理阶段。
3. 若大量 500 集中于 `/error` 探针或压测路径，需核实是否存在外部测试流量误打 `/error` 接口，不能将测试流量直接等同于核心 `/order` 回归。

### 核对运行版本与回归开关

确认属于应用层 `application_error` 后，使用运行时版本与配置检查工具核对以下信息：
- 读取 `APP_COMMIT_SHA`（40 位完整 Git SHA；若缺失会显示 `unknown`）及 `PROCESS_STARTED_AT` 进程启动时间。
- 检查运行时标志位 `simulate_code_regression`（对应环境变量 `SIMULATE_CODE_REGRESSION`）。若该标志为开启状态，`/order` 会在数据库查询完成后稳定抛出 `RuntimeError("simulated application code regression")`。
- 结合 `app.py` 源码比对最近提交，确认异常堆栈是否与当前运行 SHA 对应。

## 处置与恢复验证

### 审批执行 disable_demo_regression

`order-service` 已注册受控修复动作 `disable_demo_regression`：
- 仅允许通过 Remediation 审批工作流发起提案、由具备 `approve` 权限的操作员审批后，再由具备 `execute` 权限的执行器在确认进程身份与仓库状态后受控重启并关闭回归开关。
- 严禁通过手工 Shell 强杀进程或直接跳过审批链路修改线上环境变量。

### 业务读链路恢复验证

处置执行完成后，必须同时核实以下观测信号，不可仅凭单次 `/health` 返回 200 宣布恢复：
1. 调用只读业务恢复探针 `GET /order`，确认返回 HTTP `200` 且 JSON 包含 `service="order-service"`、`dependency="order-db"` 以及非空 `order` 对象。
2. 观察连续 1 分钟以上 `http_5xx_rate` 降回阈值以下（通常为 `0`），且样本总量满足 `>= 20`。
3. 确认 Loki 中不再新增 `error_type=application_error` 错误日志。
