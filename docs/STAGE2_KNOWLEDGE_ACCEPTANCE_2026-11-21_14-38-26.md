# 第二阶段运维知识库与 RAG 开发及验收报告

本报告记录 [开发路线图](INCIDENT_AGENT_DEVELOPMENT_PLAN_2026-11-11_10-49-02.md) 阶段 2（运维知识库与 RAG）的实现范围、分支并入状态、实测结果、检索对照数据与尚未验证边界。交付时间：2026-11-21 14:38:26（Asia/Shanghai）。

## 一、基线与分支合并状态

- **阶段 1 主分支基线**：`e41fe20`（`11.21 修复 P95 区间查询提示误判并补齐部署验收`）。
- **阶段 2 并入记录**：
  - `d172cf9`：并入 `feat/stage2-knowledge-base` 第一批独立内核提交（`479e8c4` 语料与评测集、`8a23ada` MySQL 表与 `0006_knowledge_base` 迁移、`361e662` / `6d46dd0` `knowledge/` 模块与 `web_api/auth.py` 的 `knowledge_write` 权限）。
  - `4722ede`：并入 `feat/stage2-knowledge-base` 第二批集成提交（`cdf377e` FastAPI 知识库接口与 OpenAPI 契约、`d6abdbf` `search_runbooks` 工具与 Runtime/Agent 集成、`c6911d8` React 知识库页面与 Session 手册引用跳转）。
- **合并与工作区保护**：两次合并均为零冲突；未修改 `requirements.txt` / `requirements.lock.txt`，未推送远端，工作区原有未提交的 `prompts/agent_prompt.py` 与 `temp_test.py` 改动保持原状。

## 二、交付模块清单

| 模块 | 主要文件 | 职责与边界 |
| --- | --- | --- |
| 语料与评测集 | `knowledge/corpus/*.md`、`knowledge/eval/queries.json`、`knowledge/eval/README.md` | 16 份脱敏 Runbook（`order-service` 6、`order-db` 5、`inventory-service` 5；含 13 份启用、3 份废弃旧 SOP）与 30 条标注查询（`dev` 15 / `holdout` 15，覆盖 6 类场景） |
| 数据表与迁移 | `db/tables.py`、`migrations/versions/0006_knowledge_base.py`、`tests/test_knowledge_migration.py` | 新增 `knowledge_documents`、`knowledge_versions`、`knowledge_chunks`；ID/状态列保留 `utf8mb4_0900_bin`，`heading_path` 与 `chunk_text` 使用 `utf8mb4_0900_ai_ci` 并建立 `FULLTEXT ... WITH PARSER ngram` 索引 |
| 知识库核心 | `knowledge/{models,chunker,embeddings,store,retriever,evaluator}.py`、`knowledge/__init__.py` | Markdown 标题/段落切分与行号/SHA-256；可插拔 Embedding（OpenAI 兼容 HTTP 客户端 + 离线 `offline-ngram-v1`）与 `float32` 维度/有限值/非零校验；短事务原子发布、`(document_id, content_sha256)` 幂等、失败保留已发布版本；MySQL `ngram` + 精确代码匹配 + Python 精确余弦 + RRF 融合；离线 BM25 与指标评测 |
| 后端接口与鉴权 | `web_api/auth.py`、`web_api/knowledge_views.py`、`web_api/schemas.py`、`web_api/app.py`、`.env.example`、`frontend/tests/api-contract.json`、`tests/test_web_knowledge.py` | 扩展 `Permission` 新增 `"knowledge_write"`（与 Remediation 审批权限隔离）；新增 `GET/POST /api/knowledge/documents`、`POST/GET /api/knowledge/documents/{id}/versions...`、`GET/POST /api/knowledge/search`；保留只读 CORS（`not auth.enabled` 时仅允许 `GET`）；在 `SessionDetail.runbook_citations` 提取 `search_runbooks` 引用坐标 |
| Agent 只读工具 | `tools/runbooks.py`、`tools/__init__.py`、`investigation/evidence_policy.py`、`investigation/worker.py`、`agent/agent.py`、`tests/test_search_runbooks_tool.py`、`tests/test_service_registry.py` | 注册只读 `search_runbooks(service: str, query: str, limit: int = 5)`；明确区分 `ok`（`evidence_type="reference_runbook"`、`authoritative_for_current_state=False`）、`no_data` 与 `tool_error`；`TTL["search_runbooks"] = 300` 且排除出 `effective_records` 实时健康结论；Worker 摘要与 Checkpoint 保留完整引用定位 |
| 前端工作台 | `frontend/src/App.tsx`、`frontend/src/api/{client,types}.ts`、`frontend/src/lib/knowledge.ts`、`frontend/src/pages/{KnowledgePage,KnowledgeVersionPage,SessionDetailPage}.tsx`、`frontend/src/components/ui.tsx`、`frontend/src/styles.css`、`frontend/tests/{contract.test.ts,fixtures.ts,knowledge.test.tsx}` | 新增 `/knowledge`（服务/状态筛选、本地 `.md` 选择与安全预览、`knowledge_write` 写入鉴权、新版本上传与版本切换、只读检索状态展示）、`/knowledge/documents/:documentId/versions/:versionId`（切片大纲与 `?start=&end=&chunk=` 原文行号高亮定位），以及 Session 详情“手册引用（辅助线索，非实时状态）”直达链接 |

## 三、测试与构建实测结果

所有 MySQL 集成测试均在独立 MySQL 8.4.13 实例（`127.0.0.1:3308`，`ia_test_*` 隔离 schema，`INCIDENT_AGENT_CLEAN_ENV=1`）执行，未触碰宿主 3306 与真实 `data/`。

### 1. 合并后直接在 `main`（`4722ede`）执行通过的验证

| 验证项 | 命令 | 实际结果 |
| --- | --- | --- |
| 前端测试 | `npm test`（`frontend/`） | **7 passed (7), 102 passed (102)**，13.91s，退出 0（含 `contract.test.ts` 1、`knowledge.test.tsx` 9、`client.test.ts` 26、`pages.test.tsx` 24、`remediation.test.tsx` 25、`recovery.test.tsx` 16、`useApi.test.ts` 1） |
| 前端类型检查与构建 | `npm run build`（`tsc --noEmit && vite build`） | **0 errors**，622ms，退出 0（`dist/assets/index-CrLWiYif.js` 487.52 kB、`index-0RtajHgo.css` 41.90 kB） |
| 阶段 2 后端专项（8 组） | `D:\conda\envs\agentlearn\python.exe -B -m pytest tests/test_frontend_contract.py tests/test_knowledge_migration.py tests/test_knowledge_unit.py tests/test_knowledge_auth.py tests/test_knowledge_store.py tests/test_web_knowledge.py tests/test_search_runbooks_tool.py tests/test_service_registry.py -q --disable-warnings` | **113 passed, 19 subtests passed**，119.35s，退出 0 |

### 2. 合并前在等价临时工作树（`verifier-2` 验证的同套 25 文件集）的扩展验证

- **14 组后端回归**（在上述 8 组基础上附加 `test_web_api.py`、`test_w3_authorization.py`、`test_runtime_quality.py`、`test_demo_replay.py`、`test_token_counter.py`、`test_stage1_model_delivery.py`）：**251 passed, 44 subtests passed**（895.68s，退出 0）。
- **跨模块端到端闭环**：真实 `Worker` 在隔离 MySQL 8.4 执行 `search_runbooks` 后，`GET /api/sessions/{session_id}` 返回 `status=COMPLETED` 与 3 条 `runbook_citations`（首条 `order-service-high-http-5xx@ver-49ac7a996550d3f421e8bfc1#L11-L18`），请求 `GET /api/knowledge/documents/{doc}/versions/{ver}` 返回 `status=published` 且原文 `L11-L18` 精确匹配。
- **阶段 2 第一批历史导入兼容检查**：`tests/test_legacy_import_mysql.py`（在 `0006_knowledge_base` 为 `head` 时执行）**18 passed**（188.74s）。

## 四、检索方式对照评测与默认策略结论

评测基于隔离 MySQL 8.4.13，导入 `knowledge/corpus/` 共 **16 份文档**（13 份已发布、3 份废弃草稿）、**80 个总切片**（其中 **67 个已发布切片**参与检索），Embedding 使用离线确定性 `offline-ngram-v1`（256 维），`top_k = 5`，在 `knowledge/eval/queries.json` 的 30 条标注查询（25 正例 / 5 `no_match`）上测得：

| 数据集（总条数：正例 / 无匹配） | 检索方式 | Recall@5 | MRR | 无匹配准确率 | 禁用/过期命中率 | P95 (ms) | 平均候选切片数 | 误召回数 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `dev`（15：13 / 2） | `mysql_ngram` | 1.000 | 0.885 | 1.000 | 0.000 | 80.27 | 14.80 | 0 |
| `dev`（15：13 / 2） | `vector_cosine` | 0.846 | 0.846 | 1.000 | 0.000 | 10.73 | 14.20 | 2 |
| `dev`（15：13 / 2） | `hybrid_rrf` | 1.000 | 0.885 | 1.000 | 0.000 | 18.72 | 29.00 | 0 |
| `dev`（15：13 / 2） | `offline_bm25` | 1.000 | 0.885 | 1.000 | 0.000 | 0.57 | 30.47 | 0 |
| `holdout`（15：12 / 3） | `mysql_ngram` | 1.000 | 1.000 | 1.000 | 0.000 | 9.83 | 9.33 | 0 |
| `holdout`（15：12 / 3） | `vector_cosine` | 0.583 | 0.438 | 1.000 | 0.000 | 12.69 | 6.60 | 5 |
| `holdout`（15：12 / 3） | `hybrid_rrf` | 1.000 | 1.000 | 1.000 | 0.000 | 19.08 | 15.93 | 0 |
| `holdout`（15：12 / 3） | `offline_bm25` | 1.000 | 1.000 | 1.000 | 0.000 | 0.66 | 27.80 | 0 |
| `all`（30：25 / 5） | `mysql_ngram` | 1.000 | 0.940 | 1.000 | 0.000 | 8.63 | 12.07 | 0 |
| `all`（30：25 / 5） | `vector_cosine` | 0.720 | 0.650 | 1.000 | 0.000 | 16.64 | 10.40 | 7 |
| `all`（30：25 / 5） | `hybrid_rrf` | 1.000 | 0.940 | 1.000 | 0.000 | 20.87 | 22.47 | 0 |
| `all`（30：25 / 5） | `offline_bm25` | 1.000 | 0.940 | 1.000 | 0.000 | 0.98 | 29.13 | 0 |

### 错误召回样例分析

全部 **7 条未命中**均出现在单独使用离线字符哈希 `vector_cosine` 时（因低于 `min_vector_score = 0.28` 返回 `no_match`；无跨服务或废弃文档误召回）：

1. `q14`（`stale_doc` / `order-service`）：“请求 `/v1/orders/health` 失败后可以直接 `kill -9` 重启订单服务进程吗？...” — 期望命中 `order-service-high-http-5xx` 或 `order-service-service-down`，离线向量返回空。
2. `q15`（`stale_doc` / `inventory-service`）：“库存服务发生缓存击穿或限流拒绝时，现在的版本还需要清理 Redis 缓存吗？...” — 期望命中 `inventory-service-backpressure-circuit-breaker`，离线向量返回空。
3. `q16`–`q20`（`chinese_paraphrase`，横跨 `order-service`、`order-db`、`inventory-service`）：例如 `q16`（“订单服务接口失败率突然飙升...”）、`q18`（“库存服务的响应耗时变高，P95 超过了 500 毫秒...”）、`q20`（“数据库里堆了很多开启了事务但一直挂着不提交的连接...”），离线字符 n-gram 哈希难以捕获同义中文改写，均返回空；而 `mysql_ngram` 与 `hybrid_rrf` 全部准确命中。

### 默认检索策略结论

- 默认策略采用 **`hybrid_rrf`**：在离线无密钥模式下与 `mysql_ngram` 同样达到 `Recall@5 = 1.000`、`MRR = 0.940`、`no_match = 1.000`、`forbidden = 0.000`，全量 P95 为 `20.87 ms`（67 个已发布切片，精确余弦远未成为瓶颈，当前无需引入 pgvector/HNSW），且在配置真实外部语义 Embedding Provider 后可直接利用向量分支补强语义改写召回。

## 五、已知限制与未验证项

1. **未执行真实第三方 Embedding API 与真实 DeepSeek 多轮 RAG 联调**：本轮测试使用离线确定性 Embedding 与受控/Mock 模型响应，真实外部 Embedding 模型的语义召回增益及真实付费模型调用 `search_runbooks` 的表现待后续按预算单独联调验证。
2. **未执行约 2900 项全量后端回归与远端 CI**：本次仅在本地合并后验证了上述 113 项核心后端用例、251 项合并前扩展用例及 102 项前端用例，未推送到远端 GitHub Actions。
3. **代码审查记录的微小非阻塞改进点**：`tools/runbooks.py` 在配置外部 `OpenAICompatibleEmbeddingProvider` 时，单次工具调用结束后尚未在 `finally` 块中显式关闭其 `httpx2.Client`（默认离线 Provider 无影响；建议后续补齐资源关闭）。
4. **环境收尾**：本轮临时测试用 `mysqld.exe`（端口 `3308`）已退出，临时工作树与测试 schema 均已清理，未触碰宿主 MySQL 3306 与原有未提交改动。
