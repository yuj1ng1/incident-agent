# Incident Agent 接续开发文档

## 开发目标

把现有 Incident Agent 做成可部署、可演示、可复跑评测的智能故障诊断与处置平台。以订单服务为第一个场景，完成从 Prometheus 告警到 Loki 日志与指标取证、Runbook 引用诊断、实时调查和人工补充线索的流程。处置演示沿用现有 Remediation 审批、执行、验证和审计链路。

Codex 负责实现、调试、自动化验收和交付记录。先读项目 [AGENTS.md](../AGENTS.md)，按阶段验收推进。真实人工审批的记录由实际操作者产生，Codex 负责准备隔离环境、验证授权规则和核对恢复证据。 [W3 人工验收入口](W3_MANUAL_BROWSER_ACCEPTANCE_GUIDE_2026-10-24_16-42-49.md)

## 当前基线与 MySQL 衔接

- **调查入口已存在。** [main.py](../main.py) 组装 Monitor、Scheduler 和 Worker，[InvestigationManager](../investigation/manager.py) 管理 Session 与人工命令，[FastAPI 工作台](../web_api/app.py) 单独读取记录并提供处置接口。新功能从这些入口接入。
- **MySQL 迁移正在进行。** 当前 `main` 的 Runtime 使用 SQLite，Incident 和 Remediation 有各自的 JSON 存储。本地草稿已引入 SQLAlchemy、PyMySQL 和 Alembic，并包含 Runtime 表迁移。接手时以迁移候选提交核实实际生效的 Store、历史数据迁移范围、MySQL 发行版和版本。 [当前 Runtime Store](../investigation/store.py) · [本地数据库配置草稿](../db/config.py) · [本地迁移草稿](../migrations/versions/0001_runtime_store.py)
- **订单库是被观测的业务依赖。** `D:\prometheus-demo` 当前的 `order-db` 为 PostgreSQL，用于订单故障演示。平台的目标 MySQL 用于保存调查和知识库数据。两者通过各自的服务配置装配。 [订单服务配置](../services/configuration.py)
- **前端 CI 已有新基线。** `b5b30dd` 的 `RemediationDetail` 契约快照曾漏掉 `demo_recovery`。`5ab7fbd` 已包含契约更新，本次核对时该提交的前端 CI 已通过。进入实施时读取候选提交的完整 CI 结果并处理实际失败项。 [原失败记录](https://github.com/yuj1ng1/incident-agent/actions/runs/40071253570) · [新 CI 记录](https://github.com/yuj1ng1/incident-agent/actions/runs/42946822936)

| 阶段 | 交付物 | 推进条件 |
| --- | --- | --- |
| 1. 部署与迁移验收 | 可从干净检出启动的 Compose 演示，带告警、指标、日志和恢复观测 | 前后端基线通过，演示可复现，迁移候选验证了存储兼容性 |
| 2. 运维知识库 | Markdown 导入、版本化检索、Agent 引用和检索对照报告 | MySQL 配置、驱动和迁移版本已可用，语料及检索标注已准备 |
| 3. 调查工作台与 MCP | SSE 事件流、人工输入、两项只读 MCP 工具 | 事件和人工命令可追溯，兼容现有 Session 和权限 |
| 4. 评测与交付 | 诊断对照评测、并发结果、演示和部署说明 | 第三方能复现故障演示和评测报告 |

## 阶段 1｜部署与迁移验收

1. **核对实际基线。** 查看 Git 工作区、暂存区、候选提交和 CI。接入已验收的 MySQL 迁移版本，复用已有连接装配与 Alembic 链。核对 `INCIDENT_AGENT_DATABASE_URL` 的配置、目标服务器版本和依赖锁。在新建隔离库执行迁移，并用旧数据副本验证已有 Session、预算、Checkpoint、事件、人工请求及纳入迁移范围的其他记录。后端以配置选择的存储作为写入来源，连接与迁移错误应有明确状态。
2. **交付 Docker Compose。** 复用固定版本的订单服务及其依赖、Prometheus 规则和 Loki 采集配置。编排 React、FastAPI、一份 Monitor/Worker、Prometheus、Loki、Alloy 和对应数据库。容器用服务名通信，服务配置提供就绪检查和独立演示数据卷。用现有 D1 的模型夹具或等价回放装配提供无密钥演示，保留真实 Prometheus 和 Loki 查询。真实模型通过显式配置启用。 [Docker Compose 就绪检查](https://docs.docker.com/compose/how-tos/startup-order/) · [D1 演示入口](../scripts/d1_demo.py)
3. **补齐告警详情。** 复用 Incident 列表和详情，提供服务、状态筛选，以及同一时间窗的 5xx、P95 曲线和日志。优先复用已有 Prometheus 区间查询和 Loki 查询。页面分别呈现有样本、无样本和查询失败。CPU、内存展示以采集端实际可用指标为准。 [指标工具](../tools/metrics.py) · [日志工具](../tools/logs.py)
4. **保留可验收的处置演示。** Linux Compose 提供告警、调查和业务恢复观测。Windows 隔离环境沿用 W2/W3 制品恢复入口，验证申请、审批、拒绝、执行身份及证据绑定。浏览器通过测试身份的结果标记为自动化验收，真人审批状态按实际记录。 [W2 验收记录](W2_DEMO_RECOVERY_REVALIDATION_2026-10-24_14-39-27.md) · [W3 验收记录](W3_APPROVAL_RECOVERY_ACCEPTANCE_2026-10-24_16-46-39.md)

**验收。** 同一候选版本通过后端全量测试、前端测试和构建。干净检出可通过 `docker compose up --build` 启动演示。观察到启用隔离故障后真实告警触发、页面能定位相关指标和日志、解除故障后业务检查恢复。重启后旧 Incident 和 Session 可回读。MySQL 候选使用实际目标版本验证首次建库、旧数据回读、并发命令幂等、Worker 领取及异常重启恢复。

## 阶段 2｜运维知识库与 RAG

### 首版技术栈

**复用平台 MySQL、SQLAlchemy、PyMySQL 和 Alembic。** 在平台库内新增知识库专用表，使用现有迁移体系。MySQL 持久化原文、版本、切片和 Embedding，中文词法检索使用 InnoDB `FULLTEXT` 的 `ngram` 解析器。Python 对当前服务与已发布版本下的向量做精确余弦检索。两路独立取候选，再用 RRF 按名次融合。对错误码和告警标识保留精确匹配。 [MySQL ngram 手册](https://dev.mysql.com/doc/refman/8.4/en/fulltext-search-ngram.html)

迁移就绪后，用实际镜像验证全文索引的中文和错误码表现。向量类型、距离函数与索引的可用性按发行版核实。以 9.7 为例，官方将 `DISTANCE()` 列为 MySQL HeatWave on OCI 和 MySQL AI 的能力。记录实际切片数、并发和 P95 后，若精确检索成为瓶颈，再用同一语料对照 pgvector/HNSW。 [MySQL 向量函数](https://dev.mysql.com/doc/refman/9.7/en/vector-functions.html) · [pgvector 精确与近似检索](https://github.com/pgvector/pgvector)

### 数据与导入

| 数据 | 要保存的内容 |
| --- | --- |
| 文档 | 文档 ID、服务范围、标题、来源和当前发布版本 |
| 版本 | 文档 ID、版本 ID、Markdown 原文、内容哈希、创建时间和发布状态 |
| 切片 | 版本 ID、切片 ID、顺序、标题路径、原文行范围、文本、Embedding 模型、维度和向量 |

- 首批准备 12–20 份脱敏 Markdown 手册和约 30 条人工标注的检索样例，覆盖应用 5xx、依赖不可用、过期文档、中文改写、精确故障码和无匹配。数量用于启动实验。按故障场景划分开发和留出数据。
- 使用 Markdown 标题与段落切分。只接受受限大小的 UTF-8 文本，从选定文档读取内容。保存稳定的文档、版本和切片身份，引用能回查当时发布的原文位置。
- Embedding 以单独配置接入，记录模型和维度。向量可用 `float32` 二进制存入 MySQL，导入时校验维度、有限值和零向量。先计算完整切片与向量，再在短事务内保存并发布版本。重复导入按文档与内容哈希幂等处理，失败时继续读取当前可用版本。
- 接入时查阅现有数据库配置和 Alembic 迁移头，再增加知识库迁移。文档写入通过现有身份验证并检查知识库写权限，查询按授权服务范围读取。

### 查询、页面与 Agent

首版沿用 FastAPI 和 React 工作台。以下路径按现有 API 命名落地，已有等价入口时复用。

| 能力 | 建议接口 |
| --- | --- |
| 查看与新建文档 | `GET /api/knowledge/documents`、`POST /api/knowledge/documents` |
| 发布或查看具体版本 | `POST /api/knowledge/documents/{id}/versions`、`GET /api/knowledge/documents/{id}/versions/{version}` |
| 检索 | `POST /api/knowledge/search`，接受服务、查询文本和有上限的返回条数 |

- 列表展示服务、文档与发布状态，页面支持选择 Markdown 文件、预览内容、检索和跳转原文。
- 同一授权服务与已发布版本范围内，词法召回和语义召回各自取候选。向量分支按 Embedding 模型和维度选取可比较的切片，读取向量和切片 ID 完成排序，再取 Top-K 原文。用固定数据对比 MySQL 全文、向量和混合检索。可用离线 BM25 作额外对照，重排按实测收益启用。
- 注册只读 `search_runbooks`。以已有告警的服务、类型和摘要构造检索，通过 Worker 和 ToolManager 记录预算、工具状态及证据引用。模型将手册用于解释线索和建议，确认当前根因时引用实时观测。无匹配、超时和依赖不可用应有明确状态，调查可继续使用现有工具。 [工具执行入口](../tools/tool_manager.py) · [证据模型](../models/incident.py)

**验收。** 在一次告警调查中看到相关手册引用、实时指标和日志，引用跳到正确文档版本与切片。测试重复提交、索引失败后重试、版本切换、跨服务隔离、无匹配与检索不可用。报告给出各检索方式的 Recall@5、MRR 和 P95，附错误召回样例与实际候选切片数。按样本测量决定默认检索策略。

## 阶段 3｜实时调查工作台和只读 MCP

1. **SSE。** 在现有 Session 详情上加事件时间线。建议 `GET /api/sessions/{session_id}/events` 返回 `text/event-stream`，从 Runtime 已提交的事件表读取。提供事件 ID、可恢复游标、心跳和断开释放。展示已保存的状态、工具结果摘要、证据引用和待答请求。复用鉴权与脱敏规则，需 Bearer 时通过请求头发送。为 SSE 定义独立的响应契约，保留 JSON 契约检查。 [事件存储](../investigation/store.py) · [契约导出器](../frontend/tests/export_api_contract.py) · [SSE 标准](https://html.spec.whatwg.org/multipage/server-sent-events.html)
2. **并发和重连。** MySQL 下用并发提交用例检查游标读法，覆盖较小序号晚提交的情况。断线后能补齐可见事件并去重，刷新后可从详情快照继续。查询采用有上限的批次，慢订阅者断开后释放资源。展示取自已持久化证据的工具结果。
3. **人工交互。** 在已有 Incident 详情发起调查，在 Session 上回复已存在的 HumanRequest。复用 Manager 的入队与命令处理能力，服务端确认身份、目标、状态、现有预算和请求版本。重复命令返回一致结果，Worker 在现有安全执行点处理。正式修复仍走已有审批接口。 [Manager](../investigation/manager.py) · [CLI 命令入口](../runtime_cli.py)
4. **MCP。** 首版用 stdio，复用 `query_metric_range` 和 `check_service_health`。限制服务范围、参数、请求超时和输出大小。用真实 MCP 客户端完成发现和调用，记录成功、无数据、非法参数及超时的表现。远程接入出现需求时再评估 Streamable HTTP。 [MCP 传输规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)

**验收。** 浏览器断线并重连时，持久化事件可补齐且每条只显示一次。重复提交同一条人工回答只消费一次。未获权限、状态过期及请求错误有明确反馈。MCP 客户端对允许的 Demo 服务完成两项查询，对越界输入返回受控错误。

## 阶段 4｜评测、性能和交付

1. **扩展现有评测。** 从 D1/P6.5 的可回放案例起步，冻结告警、时间窗、证据、预期根因和证据不足的标注。先在同一模型、相同案例与预算下比较有无 RAG，再比较 Prompt 或模型。记录根因命中率、引用有效率、正确保留待核实结论的比例、工具调用次数和端到端 P95。真实模型调用在已配置的隔离环境按明确预算运行，报告实际 token 和成本，缺失项标为未知。 [D1 报告](D1_DELIVERY_REPORT_2026-10-22_16-48-43.md) · [现有证据评测](../scripts/p65_claim_quality.py)
2. **验证共享数据库的负载。** 在同一套数据上记录低、中、高并发下的向量候选数、Embedding 耗时、SQL 读取、相似度计算与事件延迟。用 Incident、Session 和工具调用 ID 关联指标。对照知识库检索时的 Worker 领取、人工命令和 SSE 重连。优先解决实测出现的慢查询、阻塞 I/O 或锁竞争。
3. **交付可复现作品。** 提供从干净检出启动的说明、隔离故障演示、架构图和带样本数的检索及诊断评测报告。工作台可查看历史评测摘要。报告标明 Mock、真实本地服务、真实模型与人工验收的实际范围。

**验收。** 第三方可根据文档启动隔离环境、复现一次告警和引用诊断，再复跑同一批评测。报告保留版本、用例、样本数、评价口径、结果和已知限制。完成后确认演示故障已解除、临时资源已收尾。

## Codex 从这里开始

1. 核对当前迁移文件、Git 改动和完整 CI。以实际 MySQL 候选确认连接、迁移和回归状态。阶段 1 的 Compose 与告警联调可并行推进，知识库写入依赖已验收的迁移。
2. 同步准备 Runbook 语料、标注和可独立运行的切分及检索测试。数据库接入后完成第 2 阶段的入库、检索和引用，再进入实时工作台。
3. 每完成一个阶段，记录实际修改、命令、通过项、未验证项及当前 Git 状态。能独立复现的失败先诊断和修复，依赖进行中的迁移时继续完成可独立验证的任务。

现有基线命令在干净隔离环境执行。Python 使用项目锁定依赖，前端使用现有锁文件。

```powershell
python -m pip install -r requirements-test.lock.txt
python -m pip check
$env:INCIDENT_AGENT_CLEAN_ENV = '1'
python -B -m pytest tests -q --disable-warnings
```

在前端目录执行。

```powershell
npm ci --no-fund --no-audit
npm test
npm run build
```

以本次接手时的候选版本与实际输出为验收依据。 [CI 配置](../.github/workflows/runtime-tests.yml)
