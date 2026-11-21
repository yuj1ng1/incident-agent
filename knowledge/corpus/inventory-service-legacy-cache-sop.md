# [已废弃 v1.2] inventory-service 缓存击穿与 Redis 清理手册

- **doc_id**: `inventory-service-legacy-cache-sop`
- **service**: `inventory-service`
- **status**: `deprecated`
- **version**: `v1.2`
- **updated_at**: `2025-05-01`
- **superseded_by**: `inventory-service-high-5xx`, `inventory-service-backpressure-circuit-breaker`
- **alert_names**: `HighHttp5xxRate`

## 文档废弃声明

> **注意：本文档（v1.2）已于 2026 年随架构精简正式废弃。现行 `inventory-service` 已移除 Redis 外部缓存依赖，请勿按本手册操作！**

出现 HTTP 5xx 或 `INV_503_BACKPRESSURE` 时，请查阅现行文档：
- `inventory-service-high-5xx`
- `inventory-service-backpressure-circuit-breaker`

## 已下线的 Redis 缓存排查流程

以下内容仅对应已退役的 v1.2 版本：

### 旧版 /api/v1/stock/flush 清理步骤

- v1.2 架构曾连接 `redis-inventory:6379` 缓存库存计数。当库存服务返回 500 或 503 时，旧SOP要求运维连接 `redis-cli -h redis-inventory -p 6379` 执行 `FLUSHDB`，或调用管理端点 `POST /api/v1/stock/flush` 强制重建缓存。
- 现行 `inventory-service`（v2.0）为无外置依赖的内存服务，既无 `redis-inventory:6379` 实例，也已下线 `/api/v1/stock/flush` 接口。
