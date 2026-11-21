# [已废弃 v1.0] order-service 早期故障应急手册

- **doc_id**: `order-service-legacy-sop-v1`
- **service**: `order-service`
- **status**: `deprecated`
- **version**: `v1.0`
- **updated_at**: `2025-03-15`
- **superseded_by**: `order-service-high-http-5xx`, `order-service-db-dependency-failure`
- **alert_names**: `HighHttp5xxRate`

## 废弃声明与替代文档

> **注意：本文档（v1.0）已于 2026 年全面废弃，仅作历史档案留存，禁止作为当前故障处置依据。**

旧版 `order-service` 曾使用早期单体接口和 MySQL 3306 后端，当前系统已迁移至 PostgreSQL（`order-db`，端口 `5432`）并引入了受控 Remediation 审批机制。发生相关故障时，请改用以下现行文档：
- 应用 5xx 排查请查阅：`order-service-high-http-5xx`
- 下游数据库故障排查请查阅：`order-service-db-dependency-failure`

## 旧版架构排查步骤（已失效）

以下内容描述的是已下线的 v1.0 架构，在当前环境中不可执行且与现网不符：

### 旧版 MySQL 3306 连接检查

- 旧版架构曾连接 `mysql-orders:3306` 执行订单写入。若发生连接超时，旧手册要求值班人员登录跳板机执行 `mysql -h 10.0.12.8 -P 3306 -e "SHOW PROCESSLIST;"` 并手动 `KILL` 线程。
- 现行 `order-service` 业务依赖已变更为 PostgreSQL `order-db`（端口 `5432`，数据库 `orders`），不再使用 MySQL 3306 业务库。

### 旧版直接重启脚本

- 旧版应急流程允许在观察到 `/v1/orders/health` 报错后，不经审批直接执行 `systemctl restart order-api` 或运行 `kill -9` 强制重启进程。
- 当前版本已取消 `/v1/orders/health` 接口（现为 `/health` 与只读验证 `/order`），且禁止任何未经过 Remediation 人工审批的进程强杀操作。
