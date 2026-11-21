# order-db 连接池耗尽与空闲事务堆积排查手册

- **doc_id**: `order-db-connection-pool-saturation`
- **service**: `order-db`
- **status**: `active`
- **version**: `v2.0`
- **updated_at**: `2026-11-10`
- **alert_names**: `DatabaseUnavailable`, `HighHttp5xxRate`
- **error_codes**: `PG_TOO_MANY_CONNECTIONS`, `ERR_IDLE_IN_TRANSACTION`

## 关键连接指标与阈值参考

`order-db` 通过 Prometheus `postgresql` Profile 暴露以下连接维度指标（限定 `datname="orders"` 与实例维度）：
- `postgres_connections`：`pg_stat_database_numbackends{datname="orders"}`，当前业务库总后端连接数。
- `postgres_max_connections`：`pg_settings_max_connections`，数据库实例允许的最大连接数。
- `postgres_connection_usage`：总连接数占 `max_connections` 的比例。当使用率持续高于 `0.85` 时进入高风险区间；达到 `1.0` 时新连接将报 `FATAL: sorry, too many clients already`（错误标识 `PG_TOO_MANY_CONNECTIONS`）。
- 会话状态细分：`postgres_active_sessions`（`state="active"`）、`postgres_idle_sessions`（`state="idle"`）与 `postgres_idle_in_transaction`（`state="idle in transaction"`）。

## 定位连接消耗来源

### 排查 idle in transaction 长事务

若 `postgres_connection_usage` 飙升且 `postgres_idle_in_transaction` 数值异常偏高（对应 `ERR_IDLE_IN_TRANSACTION`）：
1. 空闲事务不仅长期占用 `max_connections` 配额，还会持有行锁或表锁，阻塞其他请求并阻止 `VACUUM` 推进。
2. 通过只读方式查询 `pg_stat_activity`，按 `state`、`application_name`、`client_addr` 及 `xact_start` 持续时间统计连接分布，找出开启事务后未及时 `COMMIT`/`ROLLBACK` 的客户端。
3. 核查是否有人为手工连接（如未关闭的 psql 会话）或批处理脚本异常退出前未释放事务。

### 核对业务侧短连接释放与连接上限

- 在现行 `order-service` 的 `/order` 实现中，每次请求使用 `with psycopg.connect(..., autocommit=True)` 建立短连接并在块结束时自动关闭，正常情况下不会产生大量 `idle in transaction` 堆积。
- 若发现连接来自未知客户端或超出预期的外部压测并发（且 `postgres_active_sessions` 与 `postgres_idle_sessions` 同步暴涨），需先确认业务侧请求速率 `http_request_rate` 是否突增，同时核对 `postgres_monitor` 自身的 `CONNECTION LIMIT 10` 未被滥用。

## 应急处置与恢复观察

1. 对于确认卡死且持有锁的孤立 `idle in transaction` 会话，经值班负责人复核会话来源与影响后，可通过 `pg_terminate_backend(pid)` 精准终止，禁止直接强杀整个 PostgreSQL 主进程。
2. 观察 `postgres_connection_usage` 降至安全水位（`< 0.7`）且 `postgres_idle_in_transaction` 归零后，调用 `order-service` `/order` 验证 `PG_TOO_MANY_CONNECTIONS` 消除。
