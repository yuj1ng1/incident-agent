# 运维知识库检索评测集说明

本目录包含 Stage 2 运维知识库检索评测集 `queries.json` 及配套文档说明，对应语料位于 `knowledge/corpus/`（共 16 份脱敏 Markdown 手册）。

## 语料分布（`knowledge/corpus/`）

- `order-service`（6 份：5 份现行 `active` + 1 份已废弃 `deprecated`）
  - `order-service-high-http-5xx`
  - `order-service-high-latency`
  - `order-service-service-down`
  - `order-service-db-dependency-failure`
  - `order-service-deployment-rollback`
  - `order-service-legacy-sop-v1`（`deprecated`）
- `order-db`（5 份：4 份现行 `active` + 1 份已废弃 `deprecated`）
  - `order-db-unavailable`
  - `order-db-exporter-down`
  - `order-db-connection-pool-saturation`
  - `order-db-lock-waits-and-slow-queries`
  - `order-db-legacy-backup-restart-sop`（`deprecated`）
- `inventory-service`（5 份：4 份现行 `active` + 1 份已废弃 `deprecated`）
  - `inventory-service-high-5xx`
  - `inventory-service-high-latency`
  - `inventory-service-service-down`
  - `inventory-service-backpressure-circuit-breaker`
  - `inventory-service-legacy-cache-sop`（`deprecated`）

## `queries.json` 数据结构

顶层对象包含 `schema_version`、`description` 与 `queries` 数组，每条评测样本字段如下：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `query_id` | `string` | 唯一样本编号（`q01`–`q30`） |
| `split` | `string` | 数据集划分：`dev` 或 `holdout` |
| `service` | `string` | 检索限定的目标服务名（`order-service` / `order-db` / `inventory-service`） |
| `category` | `string` | 评测类别（见下表） |
| `query` | `string` | 输入检索文本 |
| `expected_doc_ids` | `list[string]` | 期望命中的权威 `doc_id` 列表；`no_match` 类别为空数组 `[]` |
| `expected_heading_hints` | `list[string]` | 期望命中的 Markdown 章节标题提示；`no_match` 类别为空数组 `[]` |
| `forbidden_doc_ids` | `list[string]` | 不应作为权威结果返回的文档 ID（例如已废弃 SOP 或跨服务误匹配文档） |

## Split 与 Category 分布

总计 **30 条**手工标注样本，其中 `dev` **15 条**、`holdout` **15 条**，按类别统计如下：

| `category` | 含义 | `dev` | `holdout` | 合计 |
| --- | --- | --- | --- | --- |
| `app_5xx` | 应用层 HTTP 5xx、回归开关、背压与恢复验证 | 3 | 2 | 5 |
| `dependency_unavailable` | 数据库不可达、Exporter 异常、连接池耗尽与锁等待 | 2 | 3 | 5 |
| `stale_doc` | 过期文档陷阱（含旧版关键词，期望命中新版并避开 `forbidden_doc_ids`） | 3 | 2 | 5 |
| `chinese_paraphrase` | 无精确英文报错码的自然中文口语改写问句 | 2 | 3 | 5 |
| `exact_code` | 精确故障码与告警/标志标识检索（如 `PG_CONN_REFUSED`、`INV_503_BACKPRESSURE`） | 3 | 2 | 5 |
| `no_match` | 超出系统范围或跨服务无匹配负样本（期望返回 `no_match` 且无命中） | 2 | 3 | 5 |
| **合计** | | **15** | **15** | **30** |

## 评测约定

- **权威命中**：有期望文档的样本（非 `no_match`）计算 `Recall@5` 与 `MRR`，命中任一 `expected_doc_ids` 计入召回，命中 `expected_heading_hints` 可用于切片级核对。
- **陷阱与隔离检查**：若 Top-K 中出现 `forbidden_doc_ids` 中的已废弃或跨服务文档，应记录为误召回样例（mis-retrieval），用于校验版本发布过滤与服务隔离策略。
- **负样本处理**：`no_match` 样本用于验证阈值与检索状态 `no_match` 语义，避免强行返回低相关结果误导调查。
