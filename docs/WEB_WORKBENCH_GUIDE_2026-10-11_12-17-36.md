# 第一版只读 Web 工作台

工作台读取现有 FastAPI 返回的真实持久化记录。浏览器只发送 GET，不启动模型、Monitor、Scheduler 或 Worker。FastAPI 与前端分别运行；需要产生新调查数据时，仍由现有 `main.py` 管理调查进程。

## 目录与阅读顺序

```text
web_api/
  __main__.py                 本地 API 启动器，默认 127.0.0.1:8010
  app.py                      现有 GET 路由、错误处理与本地 CORS
  queries.py                  复用现有只读 Store 与 workflow
  schemas.py                  API 允许公开的响应字段
frontend/
  package.json / package-lock.json
  vite.config.ts              本地开发、预览与测试配置
  src/
    main.tsx                  React 入口
    App.tsx                   布局、导航与浏览器路由
    api/types.ts              与 Python 响应模型对应的字段
    api/client.ts             只读请求、地址限制、超时和错误提示
    hooks/useApi.ts           按需加载、手动刷新与请求取消
    components/               共享表格、状态、分页和页面组件
    pages/                    总览、列表、Incident / Session 详情
    lib/format.ts             北京时间、ID 编码和分页参数
    styles.css                布局与响应式样式
  tests/                      请求、页面和切换请求的隔离测试
tests/test_web_api.py          Python API、只读边界与 CORS 测试
```

建议先读 `web_api/schemas.py` 与 `frontend/src/api/types.ts`，了解数据；再从 `App.tsx` 跟到一个列表页面，最后看 `useApi.ts → client.ts → FastAPI 路由 → RuntimeQueries`。

React、TypeScript、Vite、React Router 和图标库用于页面与导航；请求使用浏览器原生 fetch，没有新增 Python 依赖，也没有引入前端状态管理框架。版本以 [package-lock.json](../frontend/package-lock.json) 为准。

## 启动 FastAPI

在第一个 PowerShell 终端执行：

```powershell
Set-Location 'D:\Incident Agent'
& 'D:\conda\envs\agentlearn\python.exe' -B -m web_api
```

默认地址为 [本地 API 文档](http://127.0.0.1:8010/docs)。新环境先安装 `requirements.lock.txt`。API 固定监听 127.0.0.1，关闭代理头信任，不读取 `.env`、不创建缺失的 Runtime 数据、不初始化 Worker。

如果已有旧 API 进程，它不会自动加载新增的 CORS 配置。请在启动该进程的终端按 Ctrl+C，再按上面的命令启动新版本。不要批量结束 Python 进程。

## 启动前端

本次验证使用 Node.js 24.15.0。安装 Node.js 24 后，在第二个 PowerShell 终端执行：

```powershell
Set-Location 'D:\Incident Agent\frontend'
npm ci
npm run dev
```

访问 [本地工作台](http://127.0.0.1:5173)。开发服务与构建预览均绑定 127.0.0.1:5173；端口占用时直接报错，不会悄悄换端口。未设置 API 开发代理。开发热更新与 WebSocket 已关闭，修改源文件后手动刷新浏览器。

如果 8010 被占用，可以在 API 终端使用：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B -m web_api --port 8011
```

对应的前端终端使用：

```powershell
$env:VITE_API_BASE_URL = 'http://127.0.0.1:8011'
npm run dev
```

这个环境变量属于当前终端，供 Vite 启动或构建时读取；无需建立前端 `.env`。仅接受 `http://127.0.0.1:端口` 或 `http://localhost:端口`，拒绝远程地址、凭据、路径及查询串。删除当前终端的覆盖可使用 `Remove-Item Env:VITE_API_BASE_URL -ErrorAction SilentlyContinue`。

构建与本地预览：

```powershell
npm run build
npm run preview
```

预览同样使用 5173，先在开发终端按 Ctrl+C 停止开发服务。API 地址会固化在构建文件中，改变地址后应重新构建。当前没有部署、登录或远程访问流程。

## 页面与 API 对应

| 页面 | 请求 | 展示内容 |
| --- | --- | --- |
| 总览 `/` | `GET /api/incidents?limit=100&offset=0` 与 `GET /api/sessions?limit=100&offset=0` | Incident 数量，运行中、等待人类及已完成的调查数量，完整 Session 状态分布，近期调查动态，最近 6 个 Incident |
| Incident 列表 `/incidents` | `GET /api/incidents?limit=20&offset=...&status=...` | 服务、严重程度、事件状态、最近调查时间，状态筛选与分页 |
| Incident 详情 `/incidents/:id` | `GET /api/incidents/{incident_id}` | 告警状态、解除与恢复验证时间、证据数量、最多 3 条最近告警及调查记录，跳转对应 Session |
| Session 列表 `/sessions` | `GET /api/sessions?limit=20&offset=...&status=...` | 运行状态、累计模型请求与工具执行次数、更新时间，状态筛选与分页 |
| Session 详情 `/sessions/:id` | `GET /api/sessions/{session_id}` | 最终回答、本 Session 计数、Incident 授权组累计预算、结果投影与提案生成状态，跳转关联 Incident |

列表搜索只筛选当前已加载页面，输入框明确标注“搜索当前页”；翻页和改变状态会清空搜索。Incident 状态只接受 `investigating`、`resolved`，不使用 `status=open`。Session 的等待、暂停、失败、预算耗尽和取消保持各自含义。

加载中显示进度提示，空列表显示无记录；404、422、500、503、连接失败、非 JSON 和超时显示可重试错误。请求超时为 10 秒，无无限重试和后台轮询。切换页面或筛选后取消旧请求，避免较晚返回的旧结果覆盖当前页面。所有日期显式按北京时间显示；旧的无时区记录按现有 Runtime 的北京时间约定解释。

## 数据含义与限制

- 现有 API 没有全库总数。总览每类最多读取最近 100 条；`has_more=true` 时数量显示 `+`，并提示加载范围。状态分布也是已加载 Session 的分布，不能当成超出范围的全库统计。
- “近期事件”来自已加载 Session 的最新状态，按 `updated_at` 排列，每个 Session 一条。它不是实时告警流，也不是完整状态变更审计记录。
- API 的 Incident 列表按创建顺序返回，Session 列表按现有创建时间排序。总览不把这个顺序当作全部历史的最后更新时间顺序。
- Incident 详情仅显示最近 3 条保留记录，不加载完整归档。调查回答上限为 8000 字符，截断时明确提示；缺失观测显示“无数据”，数值 0 保持为 0。
- 最终回答完成不表示业务恢复；告警解除时间、恢复验证时间独立展示。`proposal_generation.status=not_pending` 仅表示没有待处理的生成任务，不证明提案已生成、批准或执行。
- Incident JSON 与 Runtime SQLite 没有共同事务，不保证跨接口和分页的原子快照；需要最新视图时手动刷新。界面读取的是保存记录，历史 firing 也不代表当前 Prometheus 仍然告警。

## 当前安全边界

API 与前端只监听本机。FastAPI 保留 TrustedHost 检查，只允许 `http://127.0.0.1:5173` 和 `http://localhost:5173` 发起浏览器跨域 GET，不使用通配来源，也不开放带凭据的跨域请求。CORS 是浏览器读取策略，不是登录或权限系统，参见 [FastAPI 官方说明](https://fastapi.tiangolo.com/tutorial/cors/)。Vite 关闭 CORS，限制开发文件读取到前端目录，无 API 转发代理，参见 [Vite 服务选项](https://vite.dev/config/server-options)。

回答按 React 转义后的纯文本展示，不执行其中的 HTML 或脚本。原有 API 的字段筛选、脱敏、只读 Store 和错误分类继续生效；无审批、修复执行、人类命令写入、身份认证、Redis、队列或实时连接。Runtime 状态机、预算、安全白名单和存储格式均沿用现有实现。

不要添加 `--host 0.0.0.0`、反向代理或公网转发。任何能访问本机接口的本地进程仍可能查询公开字段；后续远程访问必须先设计身份认证与权限控制。这里没有生产管理平台的授权保障。

## 验证命令

```powershell
Set-Location 'D:\Incident Agent'
& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest tests -q --disable-warnings
Set-Location 'D:\Incident Agent\frontend'
npm test
npm run build
```

前端测试只在 `frontend/tests/` 使用合成 API 响应，产品页面没有模拟数据或调查结论回退。Python 回归使用临时 Store，涵盖原有 Runtime、审批、恢复与新 CORS 边界。GitHub Actions 同时运行 Windows/Python 隔离全量回归、Node 24 前端测试及构建；每次推送的实际结果应查看对应 Commit 的 Actions Run。

真实页面验收只读取本地 API 保存记录，不等于本次运行了新模型调查、故障注入或修复执行。
