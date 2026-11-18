# [已废弃 2024版] order-db 快速强杀与重建卷应急手册

- **doc_id**: `order-db-legacy-backup-restart-sop`
- **service**: `order-db`
- **status**: `deprecated`
- **version**: `v1.0`
- **updated_at**: `2024-11-20`
- **superseded_by**: `order-db-unavailable`, `order-db-connection-pool-saturation`
- **alert_names**: `DatabaseUnavailable`

## 废弃说明与风险提示

> **注意：本文档（2024 版）已被永久废弃，其中包含高危破坏性操作，严禁在现网或测试恢复流程中使用！**

当 `order-db` 触发 `DatabaseUnavailable` 或连接异常时，请改用现行规范文档：
- 连通性与实例异常排查：`order-db-unavailable`
- 连接数耗尽与空闲事务处理：`order-db-connection-pool-saturation`

## 已停用的强停与清理操作

以下操作属于历史错误实践，已在现行安全基线中明确禁止：

### 禁止直接删除数据卷

- 2024 旧版手册曾建议：当 `orders` 库连接卡死或报 `PG_CONN_REFUSED` 时，直接运行 `docker compose down -v` 删除 `PGDATA` 持久化卷并重新执行初始化脚本。
- **危险后果**：删除数据卷会直接销毁全部订单业务数据及监控角色授权状态，且无法区分网络故障、Exporter 误报与真实磁盘损坏。
- 旧版手册中使用的 `pg_ctl restart -m immediate` 强行立即中止模式亦已停用，避免造成不必要的崩溃恢复回放延迟。
