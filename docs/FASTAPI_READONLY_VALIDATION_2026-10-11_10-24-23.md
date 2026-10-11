# Runtime 收尾与本地只读 FastAPI 验收

起点：`82a67504338a0b55f8e7c4b5c9cfbf1ee05c29ad`，实际检查分支 main、HEAD、暂存区、未提交改动、AGENTS.md、独立审查、相关测试与依赖。原有 Prompt、README、temp_test.py、AGENTS.md 和文档整理改动保留，未混入提交。

Runtime 已单独提交为 `7d5262bf531f64f48bdc0abf76c8a86b5be46841`。API 以第二个独立提交交付，不改变原 `main.py`、`runtime_cli.py`、`remediation_cli.py` 的启动/操作行为。

## Runtime 验证结论

- 旧审查报告不能直接作为事实。正常成功补证期间的 cancel/message/cleared/主动让出和保存回答后的租约恢复已有正确实现；新增 pause 验证通过。终态普通 message 原本就被拒绝，未修改此规则。
- 确认并修复无 claim 投影的旧快照写入窗口：所有 JSON 发布先在短 SQLite 写事务内核对版本；有 claim 的路径保留 fencing/lease 校验。旧快照不进入 IncidentStore，较新 Checkpoint 仍待投影。确认事务仍独立，通过 CAS 避免误确认后续工作；已有 JSON 提交后进程退出的幂等恢复测试保留并通过。
- 新发现并复现补证失败同时收到控制命令的缺陷：原代码在 `tool_error` 出口跳过安全检查并完成调查。最终安全检查移入 `_finish`，覆盖所有补证出口，保证 pause/cancel/message/cleared/让出被处理。成功/失败两组同时验证状态、审计、attempt、模型/工具计数与恢复。
- 新增测试保护发现两项旧 CLI 测试遗漏临时 RemediationStore 路径，已补齐。保护会在访问项目真实 data 目录前失败；所有测试都使用隔离 Store，保留用户历史文件。

第一阶段的复现过程与初轮失败如实记录于 [Runtime 收尾记录](RUNTIME_FINALIZATION_2026-10-11_10-08-30.md)。最终源码快照的完整回归已通过，见下表。

## API 结构与公共接口

| 文件 | 职责 |
| --- | --- |
| [web_api/app.py](../web_api/app.py) | `create_app`/`app`、五个同步 GET 路由、参数边界、404 与安全错误响应、可信 Host 检查 |
| [web_api/schemas.py](../web_api/schemas.py) | 分页、Incident/Session、预算、调查结果、投影、HumanRequest 和提案生成响应模型 |
| [web_api/queries.py](../web_api/queries.py) | 复用 Store/Manager 公共查询，字段筛选、脱敏与有界摘要；无 SQL 或调查循环 |
| [web_api/__main__.py](../web_api/__main__.py) / [__init__.py](../web_api/__init__.py) | 固定本地启动器与无副作用包入口 |
| [incident_store.py](../incident_store.py) | `read_only=True` 打开现有 JSON，禁止写入；增加 `list_incidents(status, limit, offset)`，不读归档 |
| [investigation/store.py](../investigation/store.py) | `read_only=True` 使用 SQLite `mode=ro`，校验而不初始化/迁移；扩展 `list_sessions` 可选分页和排序，旧默认行为保持 |
| [investigation/manager.py](../investigation/manager.py) | `workflow` 增加已有 Checkpoint 的 `projected` 查询字段 |
| [tests/test_web_api.py](../tests/test_web_api.py) | 隔离 API、旧数据、隐私、损坏、无副作用和真实 SQLite 并发读取测试 |
| [requirements.txt](../requirements.txt) / [requirements.lock.txt](../requirements.lock.txt) | 新 Web 依赖与固定依赖闭包 |

Runtime 另修改 manager/worker，以及 tests/conftest.py、test_runtime.py、test_runtime_finalization.py、test_review_quality.py、test_persistence_boundaries.py、test_runtime_quality.py；未增加业务依赖。

| 接口 | 行为 |
| --- | --- |
| `GET /healthz` | Web 自身健康，不调用外部监控、模型或修复 |
| `GET /api/incidents` | limit/offset/status；默认 20、最多 100，最新创建记录优先 |
| `GET /api/incidents/{incident_id}` | 默认不加载历史归档，仅最近三条告警/调查、当前 Evidence 数量与归档数量；不存在为 404 |
| `GET /api/sessions` | 有限分页，支持状态/Incident 过滤，最新创建 Session 优先 |
| `GET /api/sessions/{session_id}` | 现有状态、累计/当前尝试计数、预算组用量、最终回答、投影、HumanRequest、提案生成状态；不存在为 404 |

offset 最大 10000，无效参数为 422。文件缺失返回 503；损坏、schema 不支持或记录不合法返回安全的 500，诊断日志仅记录异常类别/errno，不输出输入记录、凭据或堆栈。

没有第二份 Session 状态，也没有在 HTTP 中运行 Agent、Worker、Scheduler 或模型客户端。启动只注册应用与路径，查询时只读打开 Store。工具明细、原始模型请求、HumanMessage 正文、所有权 token 和修复参数不进入响应；旧文本也经过脱敏，回答超过 8000 字符时显式标记截断。

`proposal_generation.not_pending` 不承诺已经产生 Proposal 或修复成功。本版不读取 Proposal 内容，审批权仍属于原有 RemediationStore。

## 实际验证

| 验证 | 实际结果 |
| --- | --- |
| 聚焦 Runtime 干预/投影 | 17 passed；旧快照和补证失败并发缺陷均先复现再修复 |
| 修复旧 CLI 临时路径 | 2 passed |
| 新 API 测试 | 34 passed |
| 干净环境、暂存源码快照完整回归 | 606 passed、1 skipped、190 subtests passed，305.48 秒 |
| Runtime 提交 GitHub Actions | [实际 CI](https://github.com/yuj1ng1/incident-agent/actions/runs/38104372925) success；日志为 572 passed、1 skipped、190 subtests passed，281.09 秒 |
| 固定依赖全新安装与 `pip check` | 成功，无依赖冲突；OpenAI 3.14.0、Pydantic 2.13.5、httpx2 2.13.0 未更换；干净环境未安装旧 httpx |
| 本地真实 HTTP 127.0.0.1:8002 | healthz/docs/openapi/Incident 列表及详情/Session 列表及详情均 200；列表读取当前 19 个 Incident 与 4 个 Session |
| 真实 Store/原有改动保护 | 真实 SQLite/JSON/凭据文件存在状态与哈希、原有未提交文件哈希一致；未删除用户数据或历史快照 |
| 导入/启动、SQL 只读与并发 | 独立进程导入不建 Store/启动线程/加载模型 SDK；启动不创建业务对象；只读写入被拒绝；四个 HTTP 读请求与同一 SQLite 未提交写事务同时运行，读取一致的已提交预算快照 |

完整回归使用 Python 3.12.14、全新虚拟环境、`requirements-test.lock.txt` 和 `INCIDENT_AGENT_CLEAN_ENV=1`，并从暂存树 `5ffb666fc30fda57b360888b6903658f90c7a397` 导出源码，排除用户原有未提交内容。本报告晚于测试添加，业务源码与依赖未变化。唯一 skip 是可选的跨仓库告警规则检查，需要设置 ALERT_RULES_PATH；不是新增 API 测试跳过。

模型与工具使用 Fixture/Mock；SQLite、JSON、跨进程与 HTTP 为实际执行。未进行新的真实模型调用、故障注入、生产修复、断电或长期压力验证。本地验收启动的 API 进程已停止，没有启动新的业务 Worker/Scheduler。第二次提交推送后的 CI 结果在最终交付中提供，本报告不将尚未运行的 CI 写成已通过。

## 本地使用与限制

```powershell
cd 'D:\Incident Agent'
& 'D:\conda\envs\agentlearn\python.exe' -m uvicorn web_api.app:app --host 127.0.0.1 --port 8002 --no-proxy-headers
```

激活 agentlearn 后，也可以 `python -m web_api`，启动器固定为 127.0.0.1:8002。访问 [Swagger 文档](http://127.0.0.1:8002/docs)。从新环境安装时用固定依赖清单 `python -m pip install -r requirements.lock.txt`。

新增生产依赖为 FastAPI 0.140.13、Uvicorn 0.54.0 及必要的 Starlette 1.7.0、annotated-doc 0.0.5、click 8.5.0。未加入缓存、任务队列或新 Agent 框架。锁定版本的 SDK 与 TestClient 原生使用 httpx2，并由干净安装和真实传输类型测试确认。

当前没有登录、RBAC、HTTP 消息/回答/重开、审批/执行修复、实时推送或 React/Vue 前端。只能作为本地开发接口使用，认证授权完成前不能直接暴露公网。JSON 查询仍整文件读取，分页限制输出；不同 Store 之间没有联合原子快照，未针对大规模历史或高并发优化。

建议先认证与 RBAC，再把人工命令接入现有持久化 Manager 和独立审批边界，最后增加前端与按需求实现的轮询/SSE。学习入口见 [项目 FastAPI 学习说明](FASTAPI_LEARNING_2026-10-11_10-14-59.md)。
