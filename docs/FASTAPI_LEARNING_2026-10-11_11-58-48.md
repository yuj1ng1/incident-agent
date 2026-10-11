# 用本项目学习 FastAPI

这是一层本地只读查询 API。Agent 仍由原来的 `main.py`、Scheduler 和 Worker 运行；Web 启动不会创建业务任务或模型客户端。

1. **入口在哪里？** [web_api/app.py](../web_api/app.py) 的模块级 `app = create_app()` 是 Uvicorn 需要加载的对象。`web_api.app:app` 中，冒号左边是 Python 模块，右边是模块中的变量。`create_app` 接受可信的 Store 路径，方便测试传入临时文件；HTTP 客户端不能传入文件路径。
2. **`app = FastAPI()` 做什么？** 在 `create_app` 中，它创建 ASGI 应用，保存路由、响应模型、文档和错误处理规则。它不启动监听端口。监听由 Uvicorn 完成。
3. **`@app.get()` 做什么？** 例如 `@app.get("/healthz", response_model=Health)` 将 GET 请求交给下面的 `healthz()` 函数。当前只有五个业务 GET 路由，直接放在一个入口文件中容易追踪；需要更多独立功能时再考虑 APIRouter。
4. **路径参数怎样进入函数？** `/api/incidents/{incident_id}` 中的占位符对应 `get_incident(incident_id: str)` 的同名参数。请求 `/api/incidents/inc-example` 时，函数收到字符串 `inc-example`。不存在则抛 `HTTPException(status_code=404)`。`limit`、`offset`、`status` 是查询参数；`Query` 和类型声明负责边界检查，无效输入返回 422。
5. **Pydantic 响应模型有什么用？** [schemas.py](../web_api/schemas.py) 中的 `IncidentPage`、`SessionDetail` 等明确声明允许返回的字段和类型，生成 JSON Schema 并校验输出。API 只返回这些模型。工具结果、Checkpoint history、消息正文、owner token 和修复参数不进入响应；文本另做脱敏，调查回答最多 8000 字符，并用 `answer_truncated` 标明截断。
6. **如何复用 Runtime？** [queries.py](../web_api/queries.py) 使用 `IncidentStore.list_incidents/get_incident`、`RuntimeStore.list_sessions` 和 `InvestigationManager.workflow(include_remediations=False)`。路由不写 SQL，也不复制调查逻辑。两个 Store 用 `read_only=True` 打开已有文件；SQLite 连接使用 `mode=ro`，不建库、迁移或回填 outbox。`workflow` 返回内部查询结果，适配层再选字段构造响应模型。
7. **为什么使用 `def`？** 当前 JSON 和 SQLite 访问是同步 I/O。同步路由由 Starlette 在线程池运行，因此没有把阻塞文件操作直接放进 `async def` 的事件循环。仅当实际引入异步 I/O 时再使用 `async def/await`。参见 [Starlette 线程池说明](https://www.starlette.io/threadpool/)。
8. **为什么导入 Web 不能启动 Worker？** Uvicorn 导入模块，开发重载和测试也会重复导入。导入时启动 Worker 会产生重复 Scheduler、模型调用和状态写入。`create_app` 只保存路径和注册路由；Store 到查询请求时才打开。`/healthz` 只证明 Web API 可以响应，不代表模型、监控或存储健康。文件缺失的查询返回 503，损坏数据返回安全的 500，同时记录诊断类别。
9. **怎样用 `/docs`？** 在项目根目录，激活 `agentlearn` 后执行 `python -m web_api`，访问 [本地 Swagger 文档](http://127.0.0.1:8010/docs)，选择 GET 接口、点击 Try it out、填入真实 ID 或分页参数，再点击 Execute。先查询列表获取 ID。Swagger 页面使用 FastAPI 默认的外部文档资源；资源不可达时仍可用终端调用 API。分页默认 20，最多 100，offset 最大 10000；Incident 状态只支持 `investigating` 和 `resolved`，传入 `status=open` 会返回 422；Session 使用现有枚举。
10. **下一步如何扩展？** 先加入身份认证和 RBAC，使查询、操作、正式审批和执行权限分别可控；再让人工消息接口调用现有 `submit`，持久化命令、目标 Session 与可信身份。HTTP 回复仍不等于正式 Remediation 审批。Web 状态先用有限轮询，确有需求后再增加只读 SSE；调查继续由同一 Worker 运行。前端通过这些响应模型访问 API。

查询调用链：

```mermaid
flowchart LR
    HTTP[GET 请求] --> Route[同步路由]
    Route --> Query[RuntimeQueries]
    Query --> Store[现有只读 Store / workflow]
    Store --> Model[经过筛选与校验的响应模型]
    Model --> JSON[JSON 响应]
```

也可以显式启动：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -m uvicorn web_api.app:app --host 127.0.0.1 --port 8010 --no-proxy-headers
```

`python -m web_api` 的 [启动器](../web_api/__main__.py) 固定绑定 127.0.0.1，默认端口 8010，也可通过 `python -m web_api --port 8011` 指定其他本地端口；关闭代理头信任。未实现认证授权前，这个本地开发接口不能直接暴露到公网，也不应通过反向代理绕开本地边界。

只读查询不会证明修复成功。`proposal_generation.status = not_pending` 只表示没有待处理的提案生成任务；当前不读取 Proposal 内容或提供审批、执行接口。Incident 默认不读取归档，详情只展示最近三条告警和调查记录。JSON 列表仍需要读取现有整文件；分页约束的是返回数量，尚未针对大规模历史或高并发优化。不同 Store 之间没有原子的联合快照。

继续阅读：[FastAPI 首个应用](https://fastapi.tiangolo.com/tutorial/first-steps/)、[响应模型](https://fastapi.tiangolo.com/tutorial/response-model/)、[测试](https://fastapi.tiangolo.com/tutorial/testing/)。本项目锁定的 Starlette TestClient 实际使用 httpx2，原 OpenAI SDK 依赖保持不变。
