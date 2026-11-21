# order-db 数据库不可用（DatabaseUnavailable）排查手册

- **doc_id**: `order-db-unavailable`
- **service**: `order-db`
- **status**: `active`
- **version**: `v2.0`
- **updated_at**: `2026-11-10`
- **alert_names**: `DatabaseUnavailable`
- **error_codes**: `PG_CONN_REFUSED`, `ERR_PG_UNREACHABLE`

## 告警前置条件：确认 Exporter 在线

`order-db`（PostgreSQL，`orders` 库，默认端口 `5432`）触发 `DatabaseUnavailable` 告警的表达式为：
`pg_up{job="postgres"} < 1 and on (job, instance) (up{job="postgres"} == 1)`

这意味着**在解释 `postgres_up == 0` 之前，必须确认 `exporter_scrape_up == 1`**。
- 若 `exporter_scrape_up == 0`，说明 Prometheus 连 Exporter 自身都抓不到，问题属于 `PostgresExporterDown`，不能凭缺失的 `pg_up` 直接判定数据库宕机。
- 即使 `exporter_scrape_up == 1` 且 `pg_up == 0`，也仅说明 Exporter 访问 PostgreSQL 失败，仍需继续定位是网络阻塞、进程停止还是监控账号认证问题。

## 四阶段连通性探针诊断

对 `order-db` 的健康诊断按固定四阶段依次执行，切勿跳步下结论：

### DNS 与 TCP 5432 端口检查

1. **DNS 解析**：验证目标主机名（Compose 环境为 `order-db`，本地隔离环境为 `127.0.0.1` 或 `INCIDENT_AGENT_ORDER_DB_HOST`）能否正确解析为预期 IP。
2. **TCP 握手**：向 `5432` 端口发起 TCP 探针。
   - 若返回 `PG_CONN_REFUSED`（`Connection refused`），通常表示 PostgreSQL 服务进程未启动、崩溃或未监听在预期网卡/端口（例如仅监听 `localhost` 而未开放容器网络）。
   - 若返回 `Timeout`，重点排查防火墙策略、Docker 网络隔离或宿主机负载极高导致 SYN 队列溢出。

### 数据库握手、认证与 SELECT 1 验证

3. **PostgreSQL 连接与认证**：使用只读探测凭证（监控账号 `postgres_monitor` 或受控探测账号）尝试接入 `orders` 库。
   - 若提示 `password authentication failed`，核实环境变量 `ORDER_DB_PASSWORD` 或 `postgres_monitor` 凭据配置是否与实例初始化状态一致（严禁在日志或报告中输出密码明文）。
   - 若提示 `too many clients already`，说明连接数已打满，应转入《order-db 连接池耗尽与空闲事务堆积排查手册》（`order-db-connection-pool-saturation`）。
4. **只读探针 `SELECT 1`**：确认连接建立后能够正常返回 `1`，排除数据库处于只读恢复卡死或内部挂起状态。

## 恢复与下游联动确认

1. 恢复 PostgreSQL 实例后，依次确认 `postgres_up == 1` 且健康探针四阶段全部通过。
2. 联动核查上游 `order-service` 的 `GET /order` 业务读接口，确认 `demo_dependency_up{service="order-service",dependency="order-db"}` 恢复为 `1`，`http_5xx_rate` 归零。
