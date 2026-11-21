# 第一阶段回归修复、Web 真实性补齐与部署验收报告

- 本次正文更新时间：2026-11-21 11:29:58 +08:00（Asia/Shanghai）
- 原部署验收完成时间：2026-11-20 17:13:33 +08:00（Asia/Shanghai）
- 原修复基线：`main` / `bd07321161d9f02160f1a0b73bc756279a428d7a`；P6.5 与 Web 来源修复现已包含在远端 `89329b05037773a68025077b00a51fdcf6e11545`。P95 收官以该 SHA 为基线，第八节记录补齐的验证。
- 验收范围：对照 [开发路线图](INCIDENT_AGENT_DEVELOPMENT_PLAN_2026-11-11_10-49-02.md) 第一阶段（部署、迁移、观测页面、`demo_replay` 回放演示及已发现缺陷修复），未进入第二阶段知识库、第三阶段 SSE/Web 人工回复或 MCP
- 模型调用边界：**新增真实模型请求 0 次**；全部真实模型发送门禁保持关闭，未加载 `.env` 真实密钥，未继续旧 Session，未移除任何旧 `STOP_MODEL`

---

## 一、核心结论（分别判定）

1. **第一阶段代码交付状态**：**已完成本轮目标修复与补齐。** 已复现并修复 [tests/test_p65_readonly_investigation.py](../tests/test_p65_readonly_investigation.py) 中唯一的回归失败，并补齐 `web_api/`、`frontend/`、[alert_parser.py](../alert_parser.py)、[models/incident.py](../models/incident.py)、[agent/agent.py](../agent/agent.py)、[main.py](../main.py) 与 [investigation/manager.py](../investigation/manager.py) 中的结果可信状态分离展示、Incident/Session 页面一致性、来源保留与未记录预算展示。
2. **本地验收结论**：以下为 **2026-11-20 原候选的历史验收**，不能作为本轮 P95 候选完成部署验证的证据；P95 首次验证见第七节，收官复验见第八节。原候选在隔离 MySQL 8.4.13 下完成与 CI 对齐的完整后端回归（**2953 passed, 190 subtests passed**，退出码 `0`）、Windows 专项回归（**356 passed, 81 subtests passed**，退出码 `0`）、前端回归与构建（**91 passed**、`tsc --noEmit` 与 Vite 构建退出码 `0`、OpenAPI 契约测试 **1 passed**）、Edge 浏览器多状态/窄屏/键盘实测，以及基于隔离源码的 Compose `demo_replay` 完整故障/恢复/跨机重启回读验收。上述结果不构成“真实多轮模型调查达到可用标准”。
3. **基线远端 CI 状态**：远端 `main` 为 `89329b05037773a68025077b00a51fdcf6e11545`，[第 72 轮](https://github.com/yuj1ng1/incident-agent/actions/runs/44853739807) 已完成，总结论 `failure`。[tests](https://github.com/yuj1ng1/incident-agent/actions/runs/44853739807/job/136690899233) 与 [windows-remediation](https://github.com/yuj1ng1/incident-agent/actions/runs/44853739807/job/136690899270) 均为 `success`；[frontend](https://github.com/yuj1ng1/incident-agent/actions/runs/44853739807/job/136690899046) 为 `failure`（90 passed / 1 failed，执行确认用例未找到“补偿后独立验证未通过”）。本地本轮完整前端运行 93 passed，未据此推断远端已恢复；当时的查询覆盖基线 SHA。

---

## 二、已知回归失败复现、根因与最小修改

### 1. 复现与根因
- **失败用例**：`tests/test_p65_readonly_investigation.py::test_offline_resume_uses_saved_records_without_fabricating_outputs`
- **复现结果**：在隔离测试 MySQL 8.4.13（`127.0.0.1:23306`）与修复虚拟环境复现，日志 `D:\codex use\2026-11-18\stage1-acceptance-repair\logs\p65-reproduce.log` 记录退出码 `1`：`AssertionError: assert 'FAILED' == 'COMPLETED'`，Worker 日志记录 `status=FAILED reason=RequestConstraintError`。
- **直接原因**：引入发送前输入 token 计数边界校验后，[scripts/p65_readonly_investigation.py](../scripts/p65_readonly_investigation.py) 的离线路径已注入离线计数回调并对齐 `max_output_tokens`，但该历史单元测试直接构造 `Agent(adapter, TASK)`，未传入 `input_token_counter`（且默认 `max_output_tokens=8192` 与请求体 `2048` 不一致），触发 `ModelClient.call_once` 拒绝缺失计数证明的 `RequestConstraintError("verified_input_token_bound_required")`。

### 2. 最小修改范围
- **仅修改** [tests/test_p65_readonly_investigation.py](../tests/test_p65_readonly_investigation.py)（未改动任何生产默认计数器，未增加真实模式零计数回退，未放宽输入校验）：
  - 在 `test_offline_resume_uses_saved_records_without_fabricating_outputs` 作用域内定义 `offline_mock_token_counter` 回调，强制检查 `offline_scope_active is True`、`adapter.sdk is None`、`not adapter.in_sdk`、`adapter.offline_first_response is None`、`not adapter.credential`，以及 `model == MODEL` 和 `max_output_tokens == body["max_output_tokens"]`；
  - 将其注入恢复阶段的 `BoundedResponses` 与 `Agent(adapter, TASK, input_token_counter=offline_mock_token_counter)` 并对齐 `agent.model_client.max_output_tokens`；
  - 使用 `allow_isolated_mysql_sockets` 与 `openai.OpenAI` / `openai.AsyncOpenAI` / `requests.sessions.Session.request` 阻断守卫包住 `Worker(manager, agent).run_once()`，并在 `finally` 中关闭 `offline_scope_active`；
  - 完整保留恢复状态（`COMPLETED`）、`total_model_requests == 2`、`total_tool_executions == 3`、`fencing_token` 递增、已保存 `query_metrics` 记录、禁止伪造 `function_call_output` 及 `observation_id` 引用等全部原断言。
- **专项回归**：修复后运行 `pytest tests/test_p65_readonly_investigation.py tests/test_p65_protocol_compatibility.py -v --tb=short`，结果 **103 passed in 89.76s**（退出码 `0`，日志 `p65-after-fix.log`）。

---

## 三、Web 展示真实性与合成来源传递修改

### 1. 来源丢失与领域 `source` 问题定位
- 此前 [alert_parser.py](../alert_parser.py) 的 `normalize_prometheus_alert` 会将 `raw["labels"]` 传入 `AlertEvent`（故 `evidence_origin=synthetic_single_call_acceptance` 标签得以保留），但 `annotations` 在校验完规则字段后其余说明被丢弃且 [models/incident.py](../models/incident.py) 的 `AlertEvent` 缺少 `annotations` 字段，导致中文来源说明未进入模型上下文与持久化快照。
- `AlertEvent.source` 固定为 `"prometheus"`，承担指纹生成（`fingerprint.generate_alert_fingerprint`）与告警清除匹配（`alert_recovery.can_clear`）的格式标识职责，不能将其等同于“已验证的真实监控观测”。

### 2. 实际修改文件与设计
| 模块 | 实际改动与边界 |
| --- | --- |
| [models/incident.py](../models/incident.py) | `AlertEvent` 新增 `annotations: dict[str, str] = Field(default_factory=dict)`，兼容无该字段的历史持久化记录，保留 `source="prometheus"` 与指纹不变 |
| [alert_parser.py](../alert_parser.py) | 保留字符串 `annotations` 到 `AlertEvent`；`normalize_prometheus_alert` / `normalize_snapshot` 支持可信运行入口传入 `default_origin` / `default_origin_note`；新增 `qualify_alert_origin`、`extract_checkpoint_alert`、`qualify_investigation_origin` 保守判定 `demo_replay` / `synthetic` / `unconfirmed`，无可靠证明时一律返回 `unconfirmed` |
| [agent/agent.py](../agent/agent.py) | `build_incident_prompt` 在 `context["alert_event"]` 附加 `origin_qualification` 并明确提示 `source=prometheus` 仅代表规范化格式（未修改用户原有的 [prompts/agent_prompt.py](../prompts/agent_prompt.py)） |
| [main.py](../main.py) | `mode == "demo_replay"` 时为 `IncidentMonitor` 注入带 `default_origin="demo_replay"` 与中文演示回放说明的 `normalizer` |
| [investigation/manager.py](../investigation/manager.py) | `workflow(session_id)` 从 Checkpoint 只读计算 `origin_qualification` 并复用原有 `limits_source`（有 `execution_limits` 为 `session_checkpoint`，否则 `unrecorded`），不改写旧库 |
| [web_api/schemas.py](../web_api/schemas.py) · [web_api/queries.py](../web_api/queries.py) | 在 `AlertSummary`、`InvestigationAnswer`（含 `InvestigationHistory`）及 `SessionDetail` 暴露 `origin_status`、`evidence_origin`、`origin_note`；`IncidentDetail.recent_investigations` 与 `SessionDetail` 使用同一套 `result_trust` 与来源投影逻辑 |
| [types.ts](../frontend/src/api/types.ts) · [ui.tsx](../frontend/src/components/ui.tsx) · [SessionDetailPage.tsx](../frontend/src/pages/SessionDetailPage.tsx) · [IncidentDetailPage.tsx](../frontend/src/pages/IncidentDetailPage.tsx) · [styles.css](../frontend/src/styles.css) | 新增 `ResultTrustBadges`、`ResultTrustCallout`、`OriginBadge`、`OriginValue`；将执行状态（已完成）、回答状态（回答完成）与结论可信状态（待审查 / 已拒绝、不可信 / 未确认、机械校验范围、人工审批未记录）分离展示；Incident 最近调查与 Session 详情统一呈现；历史记录缺失来源证明时显示“来源未确认”，历史预算缺失上限（`limits_source != 'session_checkpoint'`）时显示“未记录”，指标或日志不可用继续显示失败/无数据 |
| [api-contract.json](../frontend/tests/api-contract.json) · [fixtures.ts](../frontend/tests/fixtures.ts) · [pages.test.tsx](../frontend/tests/pages.test.tsx) · [test_multi_signal.py](../tests/test_multi_signal.py) · [test_web_api.py](../tests/test_web_api.py) | 同步 OpenAPI 契约快照，补充来源保留、可信状态分离、历史兼容与页面一致性测试 |

---

## 四、实际运行检查、环境版本与结果（按场景严格区分）

### 运行环境与锁定依赖
- **Python 虚拟环境**：`D:\codex use\2026-11-18\stage1-token-counter-repair\venv\Scripts\python.exe`（Python `3.12.14`、`pytest 9.1.1`、`openai 3.14.0`、`httpx2 2.13.0`、`tokenizers 0.23.3`、`sqlalchemy 2.0.54`、`pymysql 2.2.8`、`alembic 1.20.1`、`fastapi 0.140.13`、`pydantic 2.13.5`；`pip check` 退出码 `0`）
- **Node / 前端工具链**：Node `v24.15.0`、npm `11.12.1`、Vitest `v5.0.3`、Vite `v8.3.4`
- **容器与数据库引擎**：Docker Desktop `29.8.1 linux`、`mysql:8.4.13`（临时 pytest 容器启用 5 GiB tmpfs、`--innodb-buffer-pool-size=128M --performance-schema=OFF --disable-log-bin`；历史单次真实调用副本复测使用 `mysqld 8.0.46`）

### 1. 离线测试与前后端回归（基于最终冻结候选）
| 检查项 | 执行范围与环境 | 实际结果 | 退出状态 |
| --- | --- | --- | --- |
| P6.5 复现与专项修复 | 离线 Mock + 隔离测试 MySQL `127.0.0.1:23306`（`tests/test_p65_readonly_investigation.py` + `tests/test_p65_protocol_compatibility.py`） | 复现 1 failed；修复后 **103 passed**（89.76s） | `0` |
| Web / 来源 / 契约专项 | 隔离测试 MySQL `127.0.0.1:23306`（`test_frontend_contract.py`、`test_result_trust.py`、`test_entry_paths.py`、`test_demo_replay.py`、`test_multi_signal.py`、`test_web_api.py` 等） | **129 passed, 1 skipped**（162.16s，未设 `ALERT_RULES_PATH` 条件跳过）；补充定向 5 项 **5 passed** | `0` |
| Windows Remediation 专项 | Windows 原生 + 隔离测试 MySQL `127.0.0.1:23306`（`tests/test_remediation*.py`、`test_w2_demo.py`、`test_w3_*.py`、`test_process_fixture_startup.py` 等） | **356 passed, 81 subtests passed**（369.53s） | `0` |
| 前端单测 / 类型 / 构建 / 契约 | `frontend/`（`vitest`、`tsc --noEmit`、`vite build`、`export_api_contract.py` + `test_frontend_contract.py`） | **6 文件 91 passed**（13.27s）；`tsc` + `vite build`（932ms）；OpenAPI 契约 **1 passed**（1.71s） | `0` |
| 系统临时目录快速预检（2026-11-20/19） | `$env:TEMP` + `ALERT_RULES_PATH=deploy/order-demo/alerts.yml` + 隔离 MySQL `8.4.13` | **6 passed**（38.35s） | `0` |
| **最终完整后端全量回归（2026-11-20）** | **`INCIDENT_AGENT_CLEAN_ENV=1` + `ALERT_RULES_PATH=D:\Incident Agent\deploy\order-demo\alerts.yml` + `--basetemp=$env:TEMP\ia-stage1-full-20261120` + 隔离 MySQL `8.4.13`（`full-pytest-20261120.log` / `full-pytest-20261120-junit.xml`）** | **2953 passed, 190 subtests passed, 0 failed, 0 errors, 0 skipped**（3296.80s / 54 分 56 秒） | **`0`** |

> **前置中断/环境误配全量运行如实记录**（均已保留原始日志供查，未覆盖或删除）：
> - `2026-11-18` 首轮（`full-pytest-tmpfs-2g-failed.log`）：因测试容器仅配 2 GiB tmpfs 导致磁盘空间耗尽，出现 297 failed / 129 errors；
> - `2026-11-18` 晚间（`full-pytest.log`）：推进至 81% 时因用户关机需求主动中止进程；
> - `2026-11-19` 首轮（`full-pytest-20261119.log`）：因指定了系统临时目录之外的 `--basetemp`，触发 `preflight_requires_os_temporary_directory` / `demo_requires_system_temporary_directory` / `demo_isolation_required` 隔离校验，导致 5 个文件共 32 failed / 147 errors（同时 P6.5 45 项与本次 Web 改动相关用例已全部通过）；
> - `2026-11-19` v2（`full-pytest-20261119-v2.log`）：改为 `$env:TEMP` 后推进至 87%（零非点输出），因用户要求先行暂停关机而手动取消任务。

### 2. 真实本地服务与模型回放（`demo_replay` Compose 完整部署验收）
- **隔离构建目录**：`D:\codex use\2026-11-18\stage1-acceptance-repair\candidate-demo`（145 个文件，排除真实 `.env`、`data/` 与密钥；关键修改文件与工作区 SHA-256 逐项一致；`.env` 明确配置 `INCIDENT_DEMO_MODEL_MODE=demo_replay` 且 `INCIDENT_DEMO_DEEPSEEK_API_KEY` 为空）。
- **`scripts/stage1_demo_acceptance.py` 验收结果**（日志 `stage1-demo-acceptance.log`，退出码 `0`）：
  - **迁移与就绪**：Alembic 升级至 `0005_remediation_store`，`/readyz` 返回 `{"status":"ok","read_only":true}`；
  - **真实观测链路**：`order-db`（PostgreSQL 17）+ `postgres-exporter`（`pg_up{service="order-db"} == 1`）+ Prometheus + Loki + Alloy 全部健康；
  - **受控故障与真实告警**：仅对本轮专属 `order-service` 容器启用 `SIMULATE_CODE_REGRESSION=true` 并请求真实 `/order`，触发 `HighHttp5xxRate`，生成 Incident `inc-eb53187c` 与 Session `ses-a168e860d08c4f3593b973de9465e849`；
  - **Runtime 与信号观测**：回放模型执行 `COMPLETED`（`model_rounds=2`、`total_tool_executions=3`、`limits_source="session_checkpoint"`），故障窗口内四项指标均 `status="ok"`（样本数：`http_5xx_rate: 2`，最高 `1.0`；`http_latency_p95_seconds: 2`；`process_cpu_cores: 6`；`process_resident_memory_bytes: 6`），Loki 捕获 `50` 条 `application_error` 日志；API 暴露 `origin_status="demo_replay"`、`evidence_origin="demo_replay"`、中文 `origin_note` 及 `result_trust`（`semantic_status="pending_review"`、`trusted=false`、`mechanical_status="passed"`）；
  - **独立业务恢复与重启回读**：`finally` 恢复 `simulate_code_regression=false` 后，独立业务探针验证 `/order` 返回 HTTP `200`（`dependency="order-db"`、非空 `order` 对象）且 `recovery_verification.status == "verified"`；Compose 内部 MySQL 重启以及 **2026-11-20 整机重启恢复后**（`post-reboot-20261120-compose-check.json`）回读 `inc-eb53187c` 与 `ses-a168e860d08c4f3593b973de9465e849` 完全一致且故障开关保持 `false`。

### 3. 浏览器实测与历史真实模型记录副本对照（仅使用只读副本）
| 检验对象 | 实测结果（来源：`browser-verify-result.json`、`step2b-summary.json`） |
| --- | --- |
| `demo_replay` 工作台（Edge，1440×900 & 390×844） | 服务/状态筛选、空状态、键盘 Tab 焦点顺序均正常；Incident 最近调查与 Session 详情均一致展示“执行状态 已完成”“待审查”“不可信 / 未确认”“机械校验：通过（仅结果结构）”“人工审批：未记录”及“演示回放 `demo_replay`”中文说明；Checkpoint 实际预算展示 `1/3`、`2/21`、`3/39`、`0/8`、`0/3`、`3600 秒`；恢复停流后的观测面板中 3 项显示真实曲线（5xx 9 样本、CPU 18 样本、RSS 18 样本），1 项明确显示 **“指标查询失败”**（未补零或伪造曲线），Loki 显示 50 条日志；控制台/页面 JS 错误均为 `0` |
| 历史单次真实模型副本（2026-11-18 `ses-b7cc99746d7d42acbcf48e0160458481`） | 在独立只读 MySQL `8.0.46` 副本与 SELECT-only 账号下，Incident 与 Session 页面一致展示“合成告警输入 `synthetic_single_call_acceptance`”与 `pending_review / trusted=false`，预算为 `1/1`、`1/1`、`1/13`、`0/0`、`0/0`、`180 秒`；监控未启动时 4 项指标均显示“指标查询失败”、日志显示“日志查询失败”；库表快照前后 SHA-256 零变化（`4ae99218f1493a481ce91f54fc2eeeae18ff899de23f70d1c4397983b13c928d`） |
| 历史旧记录（`ses-70894814ffe84c6996fec8f6fa4e78ca`） | 未补写或改写原库；前端正确显示“**来源未确认**（缺乏可信来源证明，仅凭 source=prometheus 或普通标签不能确认为真实观测）”与预算上限“**未记录**” |

---

## 五、原验收保留的 `demo_replay` 工作台与停止方式（历史配置）

- **Web 工作台地址**：`http://127.0.0.1:8088`（就绪探针 `http://127.0.0.1:8088/readyz`，演示案例 `inc-eb53187c` / `ses-a168e860d08c4f3593b973de9465e849`）
- **订单服务地址**：`http://127.0.0.1:18001`（`/version` 确认 `simulate_code_regression: false`，`/order` 返回 HTTP 200 正常订单）
- **运行模式**：`demo_replay`（无真实模型密钥，禁止真实模型外发）
- **Compose 项目与保留资源**：
  - 项目名：`incident-stage1-repair-20261118`
  - 10 个运行中容器：`frontend-1`、`api-1`、`monitor-1`、`mysql-1`、`order-service-1`、`order-db-1`、`postgres-exporter-1`、`prometheus-1`、`loki-1`、`alloy-1`（另有已退出一次性迁移容器 `migrate-1`）
  - 6 个保留数据卷：`incident-stage1-repair-20261118_mysql-data`、`orders-data`、`order-logs`、`prometheus-data`、`loki-data`、`alloy-data`
  - 专属网络：`incident-stage1-repair-20261118_default`（`172.30.161.0/24`）
- **临时测试清理确认**：临时测试容器 `ia-pytest-mysql-20261120` 与临时测试目录已全部清理；原有 `networklab-exp4-mysql`、`postgres-exporter`、`order-db` 与宿主机 `MySQL80` 保持运行未受影响。
- **按需停止工作台命令**（当前保持运行供查看）：
  - 仅停止容器并保留数据卷：
    `docker compose -f "D:\codex use\2026-11-18\stage1-acceptance-repair\candidate-demo\compose.yaml" --project-name incident-stage1-repair-20261118 stop`
  - 停止并删除容器与专属网络（保留数据卷）：
    `docker compose -f "D:\codex use\2026-11-18\stage1-acceptance-repair\candidate-demo\compose.yaml" --project-name incident-stage1-repair-20261118 down`

---

## 六、Diff、受保护证据与后续授权/待验证范围

- **受保护对象核验**：运行 `verify_protected.py` 确认 **633 个受保护文件** `changed_or_missing_count == 0`，含用户原有修改 [prompts/agent_prompt.py](../prompts/agent_prompt.py)（SHA-256 `85484d80c42ce0c3efb3fbf74b4766ed165336775a8f1908893d42cbfc9b62a2`）、[temp_test.py](../temp_test.py)（SHA-256 `71a2e3f7fc77b6988790b6bf158e75e071e6b7639d65644eaab1c4e644b1fef3`）、全部既有历史报告与资产、以及 2026-11-15 / 16 / 18 各取证目录。
- **真实模型门禁与历史审计警告保留**：全部旧 `STOP_MODEL` / `WATCHDOG_DISARM` / `STOP_WEB_REVIEW` 文件完整保留，两份历史账本保持 `CLOSED`；独立副本核验确认 **38 项历史审计警告原样保留**（16 项 `legacy_checkpoint_event_identity_unavailable` + 22 项 `checkpoint_result_display_count_mismatch`，`errors_count == 0`）。
- **静态检查与 Git 状态**：针对本次修改文件运行 `git diff --check` 退出码 `0`（未触碰原有 `temp_test.py`）；未执行 `git add`、`git commit` 或 `git push`。
- **仍需后续授权与验证的事项**：
  1. 人工审阅本地工作区完整变更并授权提交、推送至远端仓库，再观察新提交在 GitHub Actions 中的实际运行结果；
  2. 另行授予多轮真实模型调用预算与发送许可后，验证完整 Compose 真实故障下的多轮 `function_call` / `call_id` 证据采集、结论来源引用准确性与语义可信审核闭环；
  3. 由真实人类操作者完成正式 Remediation 审批记录（已有记录仅为自动化身份验证与回放演示）；
  4. 后续按路线图另行启动第二阶段知识库、第三阶段 SSE/Web 人工回复与 MCP 开发。

---

## 七、P95 区间查询候选首次验证（2026-11-21）

### 修复与公开契约

参考 Prometheus 官方 [HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/) 与 [histogram_quantile 说明](https://prometheus.io/docs/prometheus/latest/querying/functions/#histogram_quantile)，区分提示、查询失败与数值是否有效。

- 原因：`tools/metrics.py::_parse_metric_range_response` 将任意非空 `infos` 与失败响应等同，丢弃了 Prometheus 已返回的有效 `matrix` 样本。
- 区间查询现在接受最多 8 条、每条 1～512 字符的字符串提示，保留在 `infos`；错误或超限响应不保留样本。`warnings`、非法结构、NaN/Inf、非法类型、越界时间点、非法时间网格、重复时间点及越界数值的拒绝逻辑保持不变。
- `/api/incidents/{incident_id}/signals` 的 `MetricSignal` 新增必返 `infos: string[]`，使用 Pydantic 同时限制条数与字符串长度；`web_api/observations.py` 复用 `web_api/queries.py::public_text` 脱敏。OpenAPI 快照与 TypeScript 类型已同步。
- `IncidentSignalsPanel` 在曲线或无样本状态下展示纯文本提示；不会执行提示中的 HTML、补零或补造样本。P95 单调性修正提示保留给操作者判断，不能当成数据完全健康的证明。
- 复用现有解析、脱敏与展示链路，没有新增依赖、持久化格式、工具权限或模型预算；未进入第二阶段。

### 初次实际验证

| 检查 | 实际结果 | 边界 |
| --- | --- | --- |
| 修复前指标定向回归 | 5 failed / 32 passed，退出码 1 | 复现合法提示被拒绝、缺失 `infos` 和空区间错误原因；均为 Mock HTTP |
| 修复前前端 P95 定向回归 | 1 failed，退出码 1 | 曲线存在但缺少提示文字 |
| 修复后指标/契约专项 | 56 passed，退出码 0 | 含 5/9 样本、8×512 上界、越界拒绝、无样本及缺口 |
| 指标与契约离线回归 | 523 passed / 25 deselected，退出码 0 | `test_metric_ranges.py`、`test_metrics_aligned.py`、`test_metric_timestamps.py`、`test_frontend_contract.py`；25 项需 MySQL 的用例未运行，非通过或跳过 |
| 前端完整单测 | 6 文件 / 93 passed，退出码 0 | 含 P95 提示、HTML 按文本展示及空区间不绘图；远端既有失败用例本地通过 |
| TypeScript 与生产构建 | `tsc --noEmit`、`npm run build` 均退出码 0 | 构建含再次类型检查 |
| Web API HTTP / MySQL 回归 | 当前环境阻断，未完成 | TestClient 初始化停在 Windows asyncio `_fallback_socketpair` 的 `accept`；已中止本轮拥有的阻塞任务，未修改测试或业务代码来绕过限制 |
| Compose 镜像与 `inc-eb53187c/signals` | 当前环境阻断，未完成 | Docker 引擎已启动，但执行进程访问 Docker 命名管道被拒；8088 本机套接字同样被权限拒绝，尚未重构建或重启容器 |
| 新提交远端 CI | 待候选审阅与提交推送后验证 | 旧 SHA 第 72 轮实际结果见上文，未重跑旧 SHA |

本轮开始时已有 `test_metric_ranges.py`、`test_web_api.py` 与 `frontend/tests/pages.test.tsx` 的 P95 回归测试修改。保留并补充这些测试，未覆盖个人文件。另一次同时运行两个前端定向用例出现查询等待超时；之后完整前端 92 项及补充空区间用例后的最终完整 93 项运行均通过，未放宽生产行为或测试断言。

本机直接启动说明已补齐 Python 3.12 锁定依赖、显式 `INCIDENT_AGENT_DATABASE_URL`、`PYTHON_DOTENV_DISABLED=1` 与 `demo_replay` 示例，仅使用账号/密码/端口/schema 占位符。已核对锁定的 python-dotenv 1.2.3 实现确实支持该禁用环境变量；`INCIDENT_AGENT_CLEAN_ENV` 不是 `main.py` 禁用 dotenv 的开关。

### 保护与交付状态

- 开始时旧基线 633 个文件全部匹配；最终 629 个保持原 SHA-256，4 个差异仅为本次授权的部署指南改写/重命名及 3 处对应文档链接，未授权变更或缺失为 0。README 同步更新指南链接。6 个旧停止文件保留，2 份历史 SQLite 账本以 immutable/read-only 方式核对仍为 `CLOSED`，请求/工具计数均保持 1/1，前后 SHA-256 与原基线一致。本轮真实模型请求为 0，未加载真实 `.env`、未恢复旧 Session。
- 历史审计证据仍记录 38 项警告：16 项 `legacy_checkpoint_event_identity_unavailable`、22 项 `checkpoint_result_display_count_mismatch`，原记录 `errors_count == 0`。本轮没有修改运行库或清除警告；当前环境尚不能重新连接数据库执行一致性核验。
- P6.5 与 Web 真实性/来源修复已有提交，当前候选仅增加 P95 修复、相关契约与测试及本机启动说明和验收记录。不重复提交既有修复，不改写历史。
- `prompts/agent_prompt.py`、`temp_test.py`、`command.md`、`.pytest_tmp_*`、其他历史未跟踪报告与 `D:\codex use\` 全部不纳入当前候选；本轮日志与临时验证文件位于已忽略的 `build/stage1-final-20261121/`。
- `git add` 定向暂存也被当前只读 `.git` 权限拒绝（无法创建 `.git/index.lock`），真实暂存区保持为空，HEAD 与通过 GitHub API 核对的 `origin/main` 仍为 `89329b05037773a68025077b00a51fdcf6e11545`。尚未执行 `git commit` 或 `git push`。可以在忽略的临时目录生成独立索引和对象库用于候选预览；该预览不等于真实暂存或提交。
- 当前尚未完成全部收官条件。需要在允许本机套接字、Docker 命名管道和 `.git` 写入的执行环境继续受阻的 Web/MySQL 与隔离工作台验证，再按用户要求确认实际暂存清单、提交说明与统计，获确认后提交推送，并据新 SHA 的三个 job 实际结果更新本记录。

---

## 八、P95 收官复验

- Git 定向暂存预检可创建索引锁，`git ls-remote` 核对的提交前远端 `main` 为 `89329b05037773a68025077b00a51fdcf6e11545`。再次查询第 72 轮三个 job 并读取 frontend 失败日志，结果与基线记录一致。
- 从基线提交导出源码并覆盖本次允许的 P95 文件。在 Python 3.12.14、Windows 和全新回环 MySQL 8.4.13 容器上，指标、OpenAPI 契约、Web API、Web 修复接口和回放回归共 **633 passed**，包括 58 项 Web API 测试。P95 非法样本与脱敏用例使用 Mock Prometheus 响应，存储回归使用隔离 MySQL。测试容器已移除。
- Windows 使用 Node 24.15.0 重新执行 `npm ci`、`npm test` 和 `npm run build`，**6 个文件共 93 项通过**，TypeScript 检查和生产构建通过。前端测试覆盖提示的纯文本展示和空区间。
- 从同一份候选源码重新构建 Compose 的 API 和前端镜像。核对原演示项目、专属网络、卷及容器配置后，临时启动读取历史观测所需的依赖，挂接本轮临时 API 和前端。入口 `/readyz` 返回 `ok / read_only=true`，生产前端资源包含 P95 提示视图。原 Monitor 与迁移容器保持停止，候选 API 采用 `demo_replay` 且密钥为空。
- 回读既有 `inc-eb53187c` 与 `ses-a168e860d08c4f3593b973de9465e849`，记录为 `verified`、`demo_replay`、`trusted=false`。真实 Prometheus 3.15.0 在告警窗口的 41 个采样位置返回 **9 个 P95 样本和 1 条单调性修正提示**，候选 `signals` 的样本时间、数值及提示与原始响应一致。该接口同时返回 9 个 5xx、18 个 CPU、18 个 RSS 样本及 50 条 Loki 日志。
- 实际 `GET /order` 返回 HTTP 200 和订单记录，`/version` 确认故障开关关闭。本轮使用健康的历史案例，故障注入次数和真实模型请求均为 0。已移除临时候选容器，将原演示栈恢复到开始时的停止状态，并保留其数据卷。
- 按旧基线复核历史证据和停止文件。6 个停止文件均在，两份账本保持 `CLOSED` 且哈希一致，历史审计记录保持原样。本轮日志位于 Git 忽略目录 `build/stage1-p95-closeout-20261121/`。推送后用交付提交 SHA 对应的 GitHub Actions 运行记录核对远端 CI。
