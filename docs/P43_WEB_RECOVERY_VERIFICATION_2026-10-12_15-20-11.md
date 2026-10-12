# P4.3 Web 工作台：独立业务恢复验证

本次扩展现有 Incident 详情的只读数据映射和页面展示。`resolved`、告警解除与业务恢复验证分别呈现；成功文案明确指向已记录的验证时间，不表示服务现在仍然健康。

## 修改范围与设计

复用 IncidentStore 的只读读取、既有恢复领域模型、React Panel / Fields / StatusBadge / DataState、Lucide 图标和 CSS 变量。只新增公开视图结构与专用展示组件，无新增依赖、路由或持久化格式。

| 文件 | 实际改动 |
| --- | --- |
| [web_api/schemas.py](../web_api/schemas.py) | 增加 RecoveryVerificationView、RecoveryAttemptView、RecoveryCheckView；扩展 IncidentDetail |
| [web_api/queries.py](../web_api/queries.py) | 从已存储任务和受限收据生成白名单视图，核对收据归属及业务检查完整性 |
| [frontend/src/api/types.ts](../frontend/src/api/types.ts) | 同步恢复视图类型、状态和受控原因 |
| [frontend/src/components/RecoveryVerificationCard.tsx](../frontend/src/components/RecoveryVerificationCard.tsx) | 独立验证卡片、业务条件摘要、可键盘展开的尝试记录 |
| [frontend/src/components/ui.tsx](../frontend/src/components/ui.tsx) | StatusBadge 支持可选的语境标签和颜色，保留既有调用兼容性 |
| [frontend/src/pages/IncidentDetailPage.tsx](../frontend/src/pages/IncidentDetailPage.tsx) | 在详情主列增加验证卡片，将历史时间的证据限制放入卡片 |
| [frontend/src/styles.css](../frontend/src/styles.css) | 局部卡片样式、窄屏换行、180ms 展开箭头过渡和减少动态效果支持 |
| [frontend/tests/fixtures.ts](../frontend/tests/fixtures.ts) | 更新仅用于测试的合成数据 |
| [frontend/tests/recovery.test.tsx](../frontend/tests/recovery.test.tsx) | 增加主要状态、历史限制、缺失数据、错误、只读刷新和文本安全测试 |
| [frontend/tests/api-contract.json](../frontend/tests/api-contract.json) | 从实际 OpenAPI 重新导出受检验的公开响应契约 |
| [tests/test_web_recovery.py](../tests/test_web_recovery.py) | 增加隔离 JSON 上的 API 状态、收据、白名单、限量、脱敏和只读约束测试 |

P4.2 执行逻辑、Incident 状态机、调查预算、人工审批和修复权限均未修改。原工作区中的文档整理、README、提示词和 temp_test.py 修改保留，未纳入本次提交范围。

## 公开 API

现有 `GET /api/incidents/{incident_id}` 增加必需的 `recovery_verification` 对象。Incident 列表响应保持原有字段。以下是新增对象的全部字段：

| 字段 | 含义与限制 |
| --- | --- |
| status | 受控的展示状态，见下表 |
| target_id | 已存储的注册目标标识，最多 120 字符；缺失为 null |
| attempt_count | 已记录的尝试次数，0–5 |
| max_attempts | 已存储的次数上限，1–5；未记录为 null |
| last_attempt_at | 最近尝试的开始时间，缺失为 null |
| next_retry_at | 已安排的重试时间，缺失为 null；读取不安排重试 |
| reason | 复用恢复模型中的原因白名单，不接受任意错误原文 |
| last_http_status | 最近尝试最后采样中的 HTTP 状态，100–599；没有证据为 null |
| verified_at | 仅在有效结构化成功收据与本次任务一致时提供完成时间 |
| attempts | 最多 5 条，按原记录顺序，页面按最近优先展示 |

每条 attempts 仅包含 `number`、`started_at`、`completed_at`、`outcome`、`reason`、`http_status`、`checks`。每条 checks 仅包含 `kind`、`field`、`matched`，最多 21 条，字段标识最多 80 字符。检查摘要来自该尝试的最后一条采样；缺失的业务条件不会被补成已通过。标识复用既有凭据脱敏逻辑。

成功收据从 recovery_evidence 读取，最多检查最近 5 条候选，每条限制为 32768 字节并经过 RecoveryReceipt 校验。公开对象不包含领取令牌、租约、收据原文、响应正文、响应头、凭据、完整目标 URL、配置摘要或内部调试对象。

收据归属或检查完整性无法确认时，展示视图保守地隐藏 HTTP / 检查结果并标为证据不足；不回写持久化状态。已有 superseded 状态优先保留为任务失效。历史有效成功收据不会仅因距今较久而被显示成验证失败，它只能证明记录时的结果。

## 页面状态语义

| 视图状态 | 页面表现 |
| --- | --- |
| alert_firing | 告警仍在触发；覆盖 resolved 或旧成功时间带来的歧义 |
| pending | 告警已解除，等待验证；需要存在结构化任务 |
| running | 正在验证；结果尚未记录 |
| verified | 验证成功，显示“业务已于某时间通过恢复验证” |
| failed | 有 next_retry_at 时显示等待重试；耗尽次数时明确未安排自动重试 |
| insufficient | 证据不足，缺失检查不视为通过 |
| superseded | 验证任务已失效，不能确认本次业务恢复 |
| not_recorded | 未记录独立业务恢复验证；不会将历史解除事件解释为失败 |
| legacy_only | 仅有历史恢复验证时间；明确缺少结构化业务检查收据 |

后端版本尚未提供新增视图时，页面显示“恢复验证数据暂不可用”，不会依据 resolved 猜测验证结果。请求加载与失败继续使用原有 DataState；长标识与条件文本按纯文本显示并换行。

## 实际验证

使用项目指定环境执行了完整命令：

```powershell
& "D:\conda\envs\agentlearn\python.exe" -B -m pytest tests -q
```

最终结果：`884 passed, 2 skipped, 190 subtests passed in 337.06s`。两个跳过项是既有的干净安装检查和未配置 ALERT_RULES_PATH 的跨仓库告警规则检查；本机未启用相应条件。

相关 API / 契约测试最终结果：`89 passed in 25.57s`。覆盖所有公开状态、v1/v2 历史记录、旧版时间、缺失/错误/过大/异属收据、业务检查缺失、5 次尝试与 21 项检查边界、敏感字段隔离、请求前后文件内容及目录不变，以及禁止 GET 领取任务、HTTP 探测、创建 Worker / 模型客户端或执行写操作。已失效任务附带无效旧收据的边界用例先复现失败，再完成展示优先级修正。

一次并行相关测试曾输出 Windows WMI 平台查询的 `0x8007000e` 异常诊断，测试仍通过并以 0 退出；随后单独执行的最终全量测试未再出现该诊断。未据此断言其系统原因。

前端最终执行：

```text
npm test
npm run build
```

结果：`5 test files passed, 54 tests passed`；TypeScript 检查与 Vite 生产构建成功。前端测试覆盖主要恢复状态、旧记录证据限制、resolved 与 verified 的区别、HTTP 200 但业务条件失败、无 HTTP / 检查证据、次数耗尽、加载/错误、旧 API、只读刷新和 HTML 文本隔离。初次前端运行中摘要与折叠记录的 HTTP 200 查询歧义已通过限定摘要字段修正，后续两次完整前端测试通过。

## 浏览器与真实记录

通过本地只读 API 读取真实存量记录，并在浏览器检查了桌面 1280px、窄屏 390px 的卡片及键盘 Enter 展开尝试记录。测试期间的视口覆盖已恢复。

- inc-628e52b1：显示“业务已于 2026/10/12 14:36:40 通过恢复验证”；目标 order_read_query，1 / 3 次，HTTP 200；HTTP 状态、dependency、service、order 非空对象共 4 项通过。
- inc-011c9a4a：显示验证失败、3 / 3 次耗尽、HTTP 500、未安排重试，并可展开三条失败记录。
- inc-bdd6ec87：虽然 Incident 已解决且告警已解除，仍显示“未记录独立业务恢复验证”。

窄屏验证卡片无页面横向溢出，业务检查分为单列，长文字正常换行，展开控件有可见键盘焦点。额外等待、进行中、证据不足、失效及仅旧版时间状态由合成数据测试覆盖；没有在真实数据中人为制造这些状态。

为载入当前代码，只重新启动了已核实身份的本项目只读 API；保留原有前端服务。API 保持 127.0.0.1:8010，仅初始化读取接口。未为页面预览启动监控、调查、恢复 Worker 或模型调用；未对真实服务重新执行业务探测、故障注入或修复操作。

## 验证边界

本次验收针对只读视图及存量记录展示。未重新执行 P4.2 的真实业务恢复探测或真实模型调查；当前服务健康状态不由页面历史收据证明。未另建干净安装环境，也未启用可选跨仓库规则检查。暂存范围、diff 空白检查、凭据模式扫描与运行数据排除检查通过；没有新增依赖或已确认的待修复业务问题。
