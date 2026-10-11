# Incident Agent 工作台视觉重设计与验证

## 设计结果

将已有五个只读页面统一为浅色内容区、石墨色导航与蓝色强调的开发者工作台。保留原有导航和业务入口，调整字号、留白、卡片、表格、状态表达和长文本阅读层次，没有增加无功能按钮。

- 导航：桌面使用 224px 侧边栏；笔记本收为 72px 图标栏，保留名称提示和无障碍标签；平板和手机使用顶部三项导航。
- 总览：四张数字卡、紧凑状态比例卡、近期 Incident 表格与 Session 活动列表。单一状态不再铺满一条大进度条。统计仍来自已有分页 API，超过 100 条时保留数量下界和加载范围说明。
- 列表：服务名称与等宽 ID 分层，状态保留文字，时间按北京时间分行显示；搜索范围与分页数量明确；窄屏只在表格自身容器内滚动。
- Incident 详情：事件摘要、告警和调查记录在主栏，事件信息独立显示。
- Session 详情：先展示状态、本 Session 调用量与 Incident 授权组累计预算；结论、人工请求、调查信息、结果投影和 Remediation 提案生成分别分组。提案生成状态不代表正式审批结果。
- 长结论：使用受限 Markdown 呈现标题、段落、列表和代码。HTML 保留为转义文本；不生成图片或可导航链接。模型标题不会替代页面主标题。

## 统一 Token

Token 集中在 [styles.css](../frontend/src/styles.css)，没有引入新的 UI 框架。

| 用途 | Token | 值 |
| --- | --- | --- |
| 页面 / 卡片 | `--color-bg` / `--color-surface` | `#F8FAFC` / `#FFFFFF` |
| 导航 / 主要文字 | `--color-sidebar` / `--color-text` | `#0F172A` |
| 次要文字 / 分隔线 | `--color-muted` / `--color-border` | `#64748B` / `#E2E8F0` |
| 强调 / 浅强调背景 | `--color-accent` / `--color-accent-soft` | `#2563EB` / `#EFF6FF` |
| 成功 / 等待 / 错误 | `--color-success` / `--color-warning` / `--color-error` | `#16A34A` / `#D97706` / `#DC2626` |
| 间距 | `--space-1` 至 `--space-5` | 8 / 16 / 24 / 32 / 40px |
| 卡片 / 控件圆角 | `--radius-card` / `--radius-control` | 12 / 8px |
| 控件高度 | `--control-height` | 36px；手机 40px |
| 正文 / 标题 / 数字 | `--text-base` / `--text-title` / `--text-stat` | 14 / 28 / 36px |

无衬线字体栈使用系统字体、Segoe UI、苹方、冬青黑体及微软雅黑；技术字段使用 Cascadia Code、SFMono、Consolas 等宽字体。数字采用等宽数字特性。边框、浅色状态背景与清晰的键盘焦点承担主要视觉反馈，不使用重阴影、装饰渐变或玻璃效果。

## 展示问题修复

| 编号 | 实际改动 | 回归覆盖 |
| --- | --- | --- |
| F-1 | 统计卡区分正在加载、尚无数据、读取失败和成功读取为 0；失败提示使用已有刷新功能；一个接口失败不掩盖另一接口的成功统计 | 延迟响应、成功空列表、HTTP 500/503、刷新恢复、单接口失败 |
| F-2 | 页脚分别显示过滤后的可见条数与本页已加载条数；加载时不显示假零值 | 两类列表 1/3、0/3、3/3，翻页与筛选沿用原流程 |
| F-3 | 搜索框显示“搜索当前页”，辅助说明“仅筛选本页已加载记录” | 两类列表的可见标签、说明关联与搜索不触发额外请求 |
| F-4 | 轻量 OpenAPI 快照、实时 Python Schema 对照及 TypeScript 严格类型断言 | 嵌套字段、枚举、缺失字段、可选性、null 和响应序列化选项；负向探针验证常见漂移会被拒绝 |

## 涉及文件与维护方式

改动主要集中于 [App](../frontend/src/App.tsx)、[公共展示组件](../frontend/src/components/ui.tsx)、[表格](../frontend/src/components/RecordTables.tsx)、[结论排版](../frontend/src/components/InvestigationText.tsx)、[总览](../frontend/src/pages/OverviewPage.tsx)、[列表](../frontend/src/pages/ListPages.tsx)、[Incident 详情](../frontend/src/pages/IncidentDetailPage.tsx)、[Session 详情](../frontend/src/pages/SessionDetailPage.tsx) 和 [API 类型](../frontend/src/api/types.ts)。沿用 React、TypeScript、Vite、Lucide、已有 API Hook 与 reload，不改变后端接口。

新增的运行时依赖只有 `react-markdown@10.1.0`，用于真实长结论的安全排版；使用方式参考其[官方说明](https://github.com/remarkjs/react-markdown)。此依赖及解析器增加了前端包大小，构建 JS 约 416KB、gzip 约 131KB。本次没有引入组件库或代码生成框架。

契约维护文件为 [导出脚本](../frontend/tests/export_api_contract.py)、[OpenAPI 快照](../frontend/tests/api-contract.json)、[TypeScript 契约测试](../frontend/tests/contract.test.ts) 和 [实时 Schema 测试](../tests/test_frontend_contract.py)。后端响应模型变化后，先审查前端类型，再从仓库根目录使用项目 Python 环境运行 `python -B frontend/tests/export_api_contract.py` 更新快照，随后运行两端测试。导出仅调用应用 OpenAPI 构建，不读取业务 Store 或启动 Worker。`npm test` 自身无需 Python。

未改动 Investigation Runtime、SQLite 状态机、Worker/Scheduler、修复权限或 FastAPI 只读权限。请求取消、超时、API 地址白名单和原有 Hook 未改动。真实 Incident 数据与用户已有的未提交文件保持原状。

## 实际验证

- 本地前端：`npm test`，38 项通过；保留原有 28 项覆盖，新增展示和契约回归。
- 本地构建：`npm run build`，TypeScript 检查与 Vite 生产构建通过。
- 本地 Python：项目 `agentlearn` 环境运行 `python -B -m pytest tests/test_frontend_contract.py tests/test_web_api.py -q --disable-warnings`，52 项通过。这里包括测试客户端和测试存储，不能视为真实模型验证。
- 真实浏览器：通过现有本机只读 API 浏览五个页面；在 1440、1024、768、390px 下完成 20 组 DOM 布局检查，根页面无横向溢出，窄屏表格在容器内滚动。检查了真实数据、加载、搜索无匹配结果和不存在记录的 404 错误页面，并保存正常页面截图进行视觉检查。截图仅保存在本机验收目录，没有将真实记录截图提交到仓库。
- 无障碍与内容：保留文字状态，搜索说明关联输入框，滚动表格可获得键盘焦点，预算进度具有数值语义；测试确认模型 HTML、图片、脚本及导航链接不会成为可执行内容。
- 差异检查：本次文件通过 `git diff --check`。业务数据和用户既有修改以文件哈希复核。

GitHub 推送后的验证使用现有 `Runtime regression` 工作流：Windows 后端完整回归，以及前端 `npm ci`、测试和构建。最终提交 SHA 与该次 CI 结论在交付回复中记录；本文件不预先声明尚未发生的 CI 成功。

## 验证边界

本次没有执行真实模型调查、故障注入或 Remediation 执行，也没有新增全库搜索或统计接口。浏览器展示的是已有持久化记录，不能用来证明当前服务健康。HTTP 500/503 与部分接口失败通过前端请求模拟验证，真实浏览器错误态检查使用只读 404；没有为了截图修改生产 API 或运行数据。
