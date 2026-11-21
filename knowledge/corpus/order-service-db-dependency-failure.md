# order-service 下游 PostgreSQL 依赖故障排查手册

- **doc_id**: `order-service-db-dependency-failure`
- **service**: `order-service`
- **status**: `active`
- **version**: `v2.0`
- **updated_at**: `2026-11-10`
- **alert_names**: `HighHttp5xxRate`, `DatabaseUnavailable`
- **error_codes**: `PG_CONN_REFUSED`, `database_connection_error`, `database_query_error`

## 故障现象与错误码映射

`order-service` 在服务注册表中声明了唯一注册依赖 `order-db`（PostgreSQL，`orders` 库，默认端口 `5432`）。当 `order-db` 不可用或响应超时时，`order-service` 的 `/order` 接口会捕获 `psycopg.Error` 并返回 HTTP `500`，同时把 `demo_dependency_up{service="order-service",dependency="order-db"}` 置为 `0`。

典型日志与错误码映射如下：
- `error_type=database_connection_error`：发生在 `psycopg.connect(..., connect_timeout=3)` 阶段，常见底层错误为 `connection refused`（对应错误标识 `PG_CONN_REFUSED`）、DNS 解析失败或认证失败。
- `error_type=database_query_error`：连接建立成功，但在执行 `SELECT id, item, quantity FROM orders ORDER BY id LIMIT 1` 时失败，常见原因为 `statement_timeout=3000` 超时、锁阻塞或目标表/权限异常。

## 排查顺序：先依赖后应用

当 `order-service` 触发 `HighHttp5xxRate` 时，必须遵守“先确认依赖健康，再排查应用代码”的顺序，防止在数据库宕机期间误触发应用修复动作 `disable_demo_regression`。

### 区分 database_connection_error 与 database_query_error

1. 检查 Loki 日志 `{service="order-service"}` 中 `error_type` 和 `exception_type`（如 `OperationalError`、`QueryCanceled`）。
2. 若为 `database_connection_error` 且包含 `PG_CONN_REFUSED` / `Connection refused`：
   - 说明 PostgreSQL 实例未在目标地址 `ORDER_DB_HOST:5432` 监听或网络不可达。
   - 请立即结合 `order-db` 的 `DatabaseUnavailable` 告警及四阶段连通性探针（DNS -> TCP -> 认证 -> `SELECT 1`）继续定位。
3. 若为 `database_query_error`：
   - 检查是否出现 `canceling statement due to statement timeout`，若存在，说明数据库连接通畅但 SQL 被长事务或锁等待阻塞超过 3 秒。

### 核实 demo_dependency_up 与数据库连接恢复

1. 在 `order-service` 侧，`/order` 采用短连接模式（每次请求建立连接并在 `with` 块退出后关闭），不会持有失效的长连接池句柄。
2. 这意味着只要 `order-db` 恢复正常监听与查询能力，**无需重启 `order-service` 进程**，后续 `/order` 请求即可自动恢复成功并将 `demo_dependency_up` 刷新为 `1`。
3. 验证恢复时需直接调用 `/order` 观测真实数据库往返，而非只查看历史 `demo_dependency_up` 缓存值（注意：在首次 `/order` 请求到达前，该指标不会产生样本）。

## 常见误判规避

- 即使当前也设置了 `SIMULATE_CODE_REGRESSION=true`，只要数据库阶段先失败，`/order` 就会直接返回 `database_connection_error` 并退出，此时日志中不会出现 `application_error`。因此恢复数据库后，还需持续观测至少 1 分钟的 `/order` 请求，确认不存在叠加的应用回归开关。
- 凭据安全提示：连接密码通过环境变量 `ORDER_DB_PASSWORD` 注入，日志层已做脱敏替换为 `[REDACTED]`，排查记录中严禁粘贴明文密码。
