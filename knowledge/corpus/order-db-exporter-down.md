# order-db Exporter 抓取异常排查手册

- **doc_id**: `order-db-exporter-down`
- **service**: `order-db`
- **status**: `active`
- **version**: `v2.0`
- **updated_at**: `2026-11-10`
- **alert_names**: `PostgresExporterDown`
- **error_codes**: `ERR_EXPORTER_SCRAPE_FAILED`, `ERR_MONITOR_ROLE_AUTH`

## 监控缺失与数据库宕机的区分

当 Prometheus 无法抓取 `job="postgres"` 对应的 PostgreSQL Exporter 时，会触发 `PostgresExporterDown`（级别 `warning`，`exporter_scrape_up == 0`）。

核心判断原则：
- **`exporter_scrape_up == 0` 或 `pg_up` 指标缺失属于“证据不可用”，绝不等于数据库宕机。**
- 此时应先通过 `order-db` 的直连健康探针（TCP 5432 与 `SELECT 1`）以及上游 `order-service` 的 `/order` 接口确认业务数据库实际是否可用，防止误报导致不必要的数据库重启。

## Exporter 目标与权限检查

### 检查 postgres_monitor 只读监控角色

项目为 `order-db` 配置了最小权限监控角色 `postgres_monitor`（参见 `postgres-monitoring.sql`）：
- 拥有 `pg_monitor` 角色及 `orders` 数据库 `CONNECT` 权限。
- 限制了 `CONNECTION LIMIT 10`、`default_transaction_read_only = on` 和 `statement_timeout = 3000`。
- 若 Exporter 容器本身在线（`exporter_scrape_up == 1`）但 `pg_up == 0`，需检查是否因 `postgres_monitor` 密码未同步、连接上限 `10` 被占满（例如多个 Exporter 实例重复连接）或监控角色被意外锁定所致。

### 排查 Exporter 容器与 Prometheus 抓取

若确认 `exporter_scrape_up == 0`：
1. 检查 Prometheus `check_scrape_target` 返回的 `labels`、`health` 与 `lastError`。
2. 核对 PostgreSQL Exporter 容器运行状态与端口监听是否正常，排查配置中的目标地址格式（如 DSN 未正确引用环境变量或容器服务名写错）。
3. 恢复 Exporter 后，确认 `exporter_scrape_up == 1` 且能稳定导出 `pg_up`、`pg_stat_database_numbackends` 与 `pg_stat_activity_count` 指标。
