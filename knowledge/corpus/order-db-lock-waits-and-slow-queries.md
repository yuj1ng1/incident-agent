# order-db 锁等待与慢查询阻塞排查手册

- **doc_id**: `order-db-lock-waits-and-slow-queries`
- **service**: `order-db`
- **status**: `active`
- **version**: `v2.0`
- **updated_at**: `2026-11-10`
- **alert_names**: `HighHttpLatency`, `HighHttp5xxRate`
- **error_codes**: `PG_LOCK_WAIT_TIMEOUT`, `database_query_error`

## 锁等待指标与上游症状关联

当 `order-db` 出现表级锁或行级锁竞争时，上游 `order-service` 的 `/order` 请求会从原本数毫秒升高至数秒，先触发 `HighHttpLatency`（P95 > 0.5s），随后因触发 `statement_timeout=3000`（3 秒）返回 `500` 与 `database_query_error`（错误标识 `PG_LOCK_WAIT_TIMEOUT`）。

在监控侧可通过以下指标交叉印证：
- `postgres_lock_waits`：统计 `pg_stat_activity_count{datname="orders",wait_event_type="Lock"}`。当该值持续 `> 0` 且 `postgres_up == 1` 时，说明存在真实锁排队。
- `postgres_active_sessions`：观察活跃查询数是否随请求堆积同步攀升。

## 阻塞链定位步骤

### 查询 pg_stat_activity 阻塞会话

1. 使用只读权限查询等待锁的会话及其阻塞源（`pg_blocking_pids(pid)`）：
   - 重点查看 `datname = 'orders'` 下 `wait_event_type = 'Lock'` 的后端 `pid`、等待锁模式（如 `AccessShareLock` 被 `AccessExclusiveLock` 阻塞）以及正在执行的 SQL 摘要。
   - 顺藤摸瓜定位最顶层持锁者（blocker PID）的 `state`、`query_start`、`xact_start` 与 `application_name`。
2. 常见阻塞场景包括：未加并发控制的大表 DDL（如 `ALTER TABLE`、`TRUNCATE`）、长事务内未提交的 `UPDATE`/`LOCK TABLE` 操作、或维护脚本在业务高峰期对 `orders` 表加排他锁。

### 核对 statement_timeout 与锁超时错误

- `order-service` 在连接 `orders` 库时显式传入 `options="-c statement_timeout=3000"`，监控账号 `postgres_monitor` 也设置了 `statement_timeout = '3000'`。
- 若应用日志出现 `QueryCanceled: canceling statement due to statement timeout`，说明 SQL 并非网络断连，而是在数据库侧等待锁或全表扫描超过 3 秒被主动取消。
- 若发现非预期的慢查询（`wait_event_type` 为 `IO` 或无锁等待但执行耗时高），检查 `orders` 表索引与数据量是否发生异常膨胀。

## 解除阻塞与事后复盘

1. 确认持锁阻塞会话属于可中止的异常 DDL 或滞留手工事务后，经审批/人工确认终止顶层阻塞 `pid`。
2. 阻塞解除后，确认 `postgres_lock_waits` 迅速回落至 `0`，`order-service` 的 `http_latency_p95_seconds` 恢复正常且 `/order` 不再报 `PG_LOCK_WAIT_TIMEOUT`。
3. 事后复盘应核查是否违反了“不在业务库高峰期执行无锁超时限制的 DDL”规范。
