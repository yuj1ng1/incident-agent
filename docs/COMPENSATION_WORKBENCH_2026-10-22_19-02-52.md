# 安全补偿闭环与 Web 工作台

本轮只进行了静态核验、临时 Store / Mock / ASGI 测试、受控测试子进程和前端构建。没有调用真实业务服务、Prometheus、Loki、模型，没有注入故障、执行真实修复或补偿，没有启动、停止或重启服务，没有提交或推送。

## 交付范围与真实回滚目标

实现了原修复验证明确失败后的确定性资格评估，以及 Web 人工申请、独立审批、受控执行、独立业务验证、关联查询、阻断审计和人工核查/收口入口。没有创建第二套 Store、Manager、执行器、Runtime 或前端应用，也没有新增依赖。

**当前真实可执行的补偿动作是 0 个。** 原有 `disable_demo_regression` 仍是唯一普通修复动作；重新打开故障开关不属于安全补偿。生产 `COMPENSATION_REGISTRY` 为空，测试契约只在临时测试环境注入。不能把测试文件上的副作用验证称为真实应用回滚。

演示仓库核验：

- `D:\prometheus-demo` 没有 `AGENTS.md`；仅作只读检查。
- 分支 `codex/postgres-monitoring`，HEAD 与只读 `git ls-remote` 返回的对应远端分支一致：`0e2f6f3220b9fb9f0baf8730de5ffaaac00d8c38`。
- 用户提供的 `0e2f6f3220b9fb9f0baf8730de5ffaac00d8c38` 与实际 SHA 不同，不能作为存在的精确引用使用。
- `585035a07e0d31c735ccb2d6d7674fa656ac69cf` 存在，是当前 HEAD 的祖先。到当前版本之间包含 inventory 服务、业务延迟指标、五类告警和启动参数等变化。提交说明、祖先关系和历史验证文档都不等于已证明可回滚。
- 当前 Compose 比 `backups/before-postgres-monitoring/docker-compose.yml` 多出 PostgreSQL exporter 等监控配置。两者均使用可变 `postgres:17-alpine` 标签，没有因此得到精确数据库镜像或应用制品身份。
- 已跟踪材料中未找到可直接注册的应用部署制品、完整回滚 manifest 或数据兼容恢复契约。没有连接 Docker daemon 验证镜像，也没有启动任何数据库；因此不宣称已验证某个镜像或旧版本可恢复业务。

## Git 基线与保护

主仓库分支为 `main`，HEAD 和真实远端 `origin/main` 均为 `faee01def60f90e556e0d4cd4894a8c0f832148b`。开始时暂存区为空，未切换分支。

原工作区已有：根目录和 docs 中的旧说明删除/新归档说明、README 文档索引调整、`prompts/agent_prompt.py`、`temp_test.py`、历史独立审查文件、command.md 和多个 `.pytest_tmp_*` 目录。本轮不恢复删除文件，不重命名/归档历史材料，不清理临时历史目录，不修改 Prompt 和临时脚本。

保护清单保存在当前操作者 Temp 中的 `incident-compensation-7f153746493242669f9dd7300d0a4a8a` 目录，包含 7,856 个清单条目、7,318 个唯一文件路径，覆盖既有修改、未跟踪材料、data、演示日志/备份和 .env 等。最终保护结果见文末验证结果。默认 `agent_logger/agent.log` 未纳入初始哈希清单；它的最后写入时间为 2026-10-12 09:32:29 UTC，早于本轮基线创建时间 2026-10-22 10:08:25 UTC。不能把这个元数据检查冒充初始哈希对比。

## 数据与控制流程

没有新增 RemediationStatus，也不将失败记录改回 pending。新增结构：

- `ServiceIdentity`：服务、精确 commit、进程启动时间、配置摘要。
- `CompensationTarget`：精确目标版本、制品/配置 SHA-256、前置条件、副作用、验证标准、数据兼容条件。
- `CompensationLink`：原提案/动作/验证标识、原审批摘要、源身份、契约摘要、目标、触发方式和命令 ID。
- `CompensationAssessment`：申请人/理由、评估状态、阻断原因、关联提案和时间，持久化在原 Store 的审计事件中。
- `VerificationOutcome`：passed / failed / no_data / timeout / stale / mismatch / unknown。旧记录默认 unknown，不从旧 success=False 反推明确业务失败。
- Proposal 可选增加 incident_fingerprint、compensation；ActionResult 可选增加 resulting_identity；VerificationResult 增加动作、摘要、观测身份和时间绑定。

路径 A：原动作结果先持久化 → 独立验证 → 只有明确 `failed` 才在**同一 JSON 事务**中写入最终验证、原提案最终状态、资格评估、必要的独立 pending 补偿提案及关联审计。no_data、超时、过期、不匹配、异常和旧 unknown 不自动触发。原动作未执行、未知、失败或范围不符时阻断。

路径 B：Web GET 只展示持久化事实及无副作用预览 → 已认证申请带 command_id、当前提案摘要、理由和可选目标 ID → 后端重新读取、检查 Incident 身份和状态 → 原 Store 事务重新评估并去重 → 没有安全目标时保存 blocked 评估，有目标时生成独立 pending 提案 → 独立批准或拒绝 → 原 ActionExecutor 执行 → 独立业务验证。申请接口本身不执行动作。

安全边界：

- 新提案的批准绑定全部原始参数、目标、契约、源身份和关联摘要。原修复批准不能批准补偿。
- 批准和执行均重新检查原事实、审批摘要、目标、证据时效、冲突操作与参数。当前观测最多允许 300 秒，过期后不会自动刷新或重放副作用。
- 保留现有跨进程文件锁、服务执行锁及 executing 持久化 claim。重复命令返回已有事实；锁竞争返回 409，不能重复产生副作用。
- 副作用前先持久化 executing。崩溃后的 executing 不自动回退或重放；读取已保存 ActionResult 区分 executed 与 unknown。人工核查不能证明历史动作执行成功，人工收口保留原执行结局。
- 每个原提案最多有一个补偿提案；拒绝、失败或验证未通过后也不递归生成下一层补偿。当前没有自动补偿授权策略，始终要求独立人工审批。
- 补偿 handler 返回 unknown 时保持 executing，等待核查或人工收口；即使原注册契约已撤销，也能以 no_data 观测收口，而不会发起不可确认的探测或重放动作。
- 注册契约属于后端受信代码，不能从请求或模型输出加载。handler/探测/验证行为变化必须更新 contract_id；可序列化契约摘要不能替代代码发布审查。
- 原提案、动作、验证和审计均保留；补偿是独立记录，没有覆盖原字段。
- Demo 业务验证要求同版本/关闭开关的身份观测、真实业务响应的服务和依赖身份及非空订单对象，并检查进程可达。仅 HTTP 200 或 /health 200 不再证明业务恢复。

## Web 权限配置与接口

这是本机单用户部署范围的最小授权方案，不能直接当作公网认证体系。API 仍固定绑定 127.0.0.1，禁用 proxy headers；公网使用还需要独立的 TLS、成熟身份认证、凭据轮换/撤销、访问控制与部署评审。

后端 `INCIDENT_AGENT_WEB_OPERATORS` 接受 JSON 数组，权限为 request / approve / execute。配置缺失、空数组、格式错误、重复 token 摘要或无效权限会使写操作关闭。`.env.example` 保持空配置；没有修改本机 `.env` 或替用户生成真实授权。

```json
[{"actor":"local-operator","token_sha256":"<64 位小写十六进制 SHA-256>","permissions":["request","approve","execute"]}]
```

操作者应使用密码管理器生成至少 32 字符的随机 Token，只在可信后端环境保存其 SHA-256。下列 PowerShell 示例只供**后续人工部署**，本轮没有执行：

```powershell
$webOperatorToken = Read-Host '输入密码管理器生成的随机 Token' -MaskInput
$webOperatorHash = [Convert]::ToHexString(
    [Security.Cryptography.SHA256]::HashData(
        [Text.Encoding]::UTF8.GetBytes($webOperatorToken)
    )
).ToLowerInvariant()
$env:INCIDENT_AGENT_WEB_OPERATORS = ConvertTo-Json -Compress -InputObject @(
    @{
        actor = 'local-operator'
        token_sha256 = $webOperatorHash
        permissions = @('request', 'approve', 'execute')
    }
)
Remove-Variable webOperatorToken
```

也可在受保护的 .env 中提供同样 JSON；`python -m web_api` 会使用现有 dotenv 机制读取。直接使用 uvicorn 导入 app 时需事先设置进程环境。配置在 app 创建时读取，变更配置需后续由操作者重新加载 API，本轮未进行部署/重启。

浏览器通过密码字段输入 Token，经 GET /api/operator 确认后端身份和权限，仅保存在 React 内存中；刷新后清除，不使用 localStorage、Cookie、URL 或 VITE_*。Bearer Token 只在 Authorization 请求头传输。CORS 仅允许两个固定本地来源，在授权启用时允许 POST 和 Authorization/Content-Type，但 CORS 本身不构成授权。非本地来源、非 loopback 客户端、无效 Token 或无权限均由后端拒绝。客户端不能通过 approved、role、approved_by 等正文属性取得权限。

| 接口 | 权限/用途 |
| --- | --- |
| GET /api/remediations | 只读列表，分页/状态/incident_id 筛选 |
| GET /api/remediations/{id} | 只读详情、绑定/过期证据状态、关联和审计 |
| GET /api/operator | Bearer 认证，返回后端身份和权限 |
| POST .../{id}/compensation-requests | request；申请和评估，不执行 |
| POST .../{id}/approve、reject | approve；独立批准/拒绝，摘要绑定 |
| POST .../{id}/execute | execute；唯一 ActionExecutor 入口 |
| POST .../{id}/inspect-execution | execute；独立当前核查，不重放 |
| POST .../{id}/close-execution | execute；理由和命令 ID，人工收口 |

写请求使用 `command_id` 和 `proposal_digest`；拒绝、申请和收口还要求理由。未配置写授权返回 503；无凭据/无效凭据为 401；权限或来源不符为 403；不存在为 404；状态、摘要和命令冲突为 409；非法字段为安全的 422。新接口不回显非法请求正文、Token、请求头、原始响应或完整秘密 URL。详情只公开 service/target_id 参数摘要，其他参数只计数，审批仍绑定全部参数。

## 文件职责

| 文件 | 本次作用 |
| --- | --- |
| [models/remediation.py](../models/remediation.py) | 可选关联/身份/验证结局结构；旧数据保守默认 |
| [remediation/compensation.py](../remediation/compensation.py) | 空生产注册表、确定性资格、独立提案生成、执行契约校验 |
| [remediation/store.py](../remediation/store.py) | 原子评估/关联/审计、命令幂等、独立审批、只读打开 |
| [storage_lock.py](../storage_lock.py) | 复用同一 OS 锁实现，允许只读打开已有锁，避免 Web 读句柄妨碍 Windows 原子替换 |
| [remediation/approval.py](../remediation/approval.py) | 非空新字段纳入摘要，保留旧摘要 |
| [remediation/planner.py](../remediation/planner.py) | 可信 Incident 指纹进入新原提案 |
| [remediation/executor.py](../remediation/executor.py) | 同一执行入口承接补偿、重新校验、持久化与人工处置 |
| [remediation/verification.py](../remediation/verification.py) | 明确验证结局、限定 JSON 读取、业务对象与身份验证 |
| [remediation/demo_action.py](../remediation/demo_action.py) | 保存实际启动返回的结果身份，不新增动作 |
| [web_api/auth.py](../web_api/auth.py) | 后端身份配置、Token 摘要比较、权限与来源校验 |
| [web_api/app.py](../web_api/app.py) | 复用 Store/Executor 的受控路由、状态错误、默认关闭写操作 |
| [web_api/queries.py](../web_api/queries.py)、[remediation_views.py](../web_api/remediation_views.py)、[schemas.py](../web_api/schemas.py) | 只读事实投影、固定公开字段、证据状态与安全脱敏 |
| [web_api/__main__.py](../web_api/__main__.py)、[.env.example](../.env.example) | 延用 dotenv，提供关闭状态的配置说明 |
| [frontend/src/pages/RemediationPages.tsx](../frontend/src/pages/RemediationPages.tsx) | 列表、详情、申请、审批、执行、核查、收口、验证及审计 |
| [frontend/src/components/OperatorAccess.tsx](../frontend/src/components/OperatorAccess.tsx) | 内存凭据与后端身份确认 |
| [frontend/src/api/client.ts](../frontend/src/api/client.ts)、[types.ts](../frontend/src/api/types.ts)、[lib/remediation.ts](../frontend/src/lib/remediation.ts) | 受控请求、类型、错误与状态显示 |
| [frontend/src/App.tsx](../frontend/src/App.tsx)、[IncidentDetailPage.tsx](../frontend/src/pages/IncidentDetailPage.tsx)、[styles.css](../frontend/src/styles.css) | 原应用路由和入口、响应式布局、焦点和减弱动画 |
| [tests/test_compensation.py](../tests/test_compensation.py)、[compensation_support.py](../tests/compensation_support.py)、[compensation_process_fixture.py](../tests/compensation_process_fixture.py) | 离线资格、绑定、并发、审批、文件副作用及实际测试子进程崩溃 |
| [tests/test_web_remediation.py](../tests/test_web_remediation.py) | ASGI 权限、幂等、冲突、旧数据/只读、敏感内容投影 |
| [frontend/tests/remediation.test.tsx](../frontend/tests/remediation.test.tsx) | 页面状态、权限、重复点击和失败上下文 |
| [frontend/tests/export_api_contract.py](../frontend/tests/export_api_contract.py)、[contract.test.ts](../frontend/tests/contract.test.ts)、[api-contract.json](../frontend/tests/api-contract.json) | 从实际 OpenAPI 检查新增 GET/嵌套类型，保留旧契约验证 |
| tests/test_remediation.py、test_remediation_cli_semantics.py、test_remediation_recovery.py、remediation_process_fixture.py | 将成功 Mock 补齐为真实接口约定的业务对象/启动版本；保留关键断言 |
| [README.md](../README.md) | 只更新 Web 能力说明和本指南链接；保留先前用户的文档整理修改 |

## 验证结果与限制

环境实测为 `D:\conda\envs\agentlearn\python.exe`，Python 3.12.14，pytest 9.1.1、Pydantic 2.13.5、FastAPI 0.140.13、Starlette 1.7.0、httpx2 2.13.0；Node.js v24.15.0、npm 11.12.1。没有切换 conda base、安装/升级依赖或修改依赖清单。

| 实际验证 | 结果 / 退出码 |
| --- | --- |
| 修改前 Remediation / Web / 契约基线，`-k 'not real_loopback'` | 234 passed，1 deselected，81 subtests；0 |
| 新增补偿与 ASGI 测试首轮 | 49 passed；0 |
| 相关回归（包含后续资格/绑定测试） | 283 passed，1 deselected，81 subtests；0 |
| 完整离线回归 `pytest tests`，仅排除下述 8 个会启动服务的用例 | **2748 passed，2 skipped，8 deselected，190 subtests；0**；751.22 秒 |
| 完整回归后：结果未知、撤销契约收口、只读锁配合、拒绝布尔式验证成功/缺失动作身份等局部修正的最终相关回归 | **385 passed，8 deselected，81 subtests；0**；52.60 秒 |
| frontend 的 `npm test` | **6 个文件，69 passed；0** |
| frontend 的 `npm run build`（包含 `tsc --noEmit`） | **通过；0** |
| `python -B -m pip check` | No broken requirements found；0 |
| 本次文件 `git diff --check` | 通过；0；只有 Git 的 LF/CRLF 提示 |
| 19 个相关 Python 文件 AST 语法检查 | 通过；0；未导入或执行服务 |

全量回归完成后进行了上述最后局部修正，最终版本用 385 项相关测试重新验证；**没有再重复运行第二次全量回归**。不能将较早全量结果描述为最终每一行代码都重新执行过全套测试。

实际全量命令在主仓库运行：

```powershell
$offlineSelection = 'not real_loopback_http_ignores_environment_proxy and not first_worker_skips_yesterdays_legacy_clear_without_real_http and not background_startup_only_probes_durable_jobs and not real_http_transport_against_isolated_loopback_server and not real_redirect_is_refused_before_library_drains_body'
& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest tests -q --disable-warnings -k $offlineSelection
```

最终相关回归使用相同排除式：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest tests/test_compensation.py tests/test_web_remediation.py tests/test_remediation.py tests/test_remediation_cli_semantics.py tests/test_remediation_recovery.py tests/test_recovery_verification.py tests/test_web_api.py tests/test_web_recovery.py tests/test_frontend_contract.py tests/test_entry_paths.py -q -ra --disable-warnings -k $offlineSelection
```

前端命令均在 `D:\Incident Agent\frontend` 执行 `npm test`、`npm run build`。从主仓库执行 `python -B frontend/tests/export_api_contract.py` 更新公开契约快照；该脚本不读取真实运行数据。`pip check` 使用上述同一解释器。

排除的 8 项经 `--collect-only` 精确核对，原因都是用户明确禁止启动服务，未扩大排除范围：

- test_first_worker_skips_yesterdays_legacy_clear_without_real_http 的 missing-1、missing-2、null-1、null-2 四项（fixture 仍会启动临时 HTTP server）。
- test_background_startup_only_probes_durable_jobs。
- test_real_http_transport_against_isolated_loopback_server。
- test_real_redirect_is_refused_before_library_drains_body。
- EnvironmentTests.test_real_loopback_http_ignores_environment_proxy。

2 个既有 skipped 项已额外用 `-rs` 定向确认：DependencyTests.test_clean_install_has_all_pins_and_no_legacy_httpx 要求 CI/隔离安装环境的 INCIDENT_AGENT_CLEAN_ENV=1；PolicyAndEventTests.test_external_rules_contract_when_explicitly_configured 要求显式 ALERT_RULES_PATH，本轮均未配置。没有把它们计入 passed，也没有为了全绿改动测试。排查 skip 时另外运行了 symlink 捕获原件测试（1 passed）、三个冻结制品门禁测试节点（参数化合计 5 passed），未访问真实服务。

失败与修正记录：

- 新验证要求业务对象和启动后身份后，中间相关回归曾为 10 failed、224 passed、1 deselected、80 subtests。原因是旧 Mock 的 200 响应无订单对象、启动 Mock 无返回版本；补齐与真实接口一致的成功输入，保留原关键断言，并新增“200 无业务对象为 no_data”和超时/身份不匹配回归，最终相关测试通过。
- 前端中间曾为 1 failed、68 passed：结果未知文本在徽标和字段中各出现一次，查询改为指定状态徽标后通过。请求失败后现在保留记录和理由，禁用进一步写入直到刷新，避免误触。
- 显式导出测试 HTML 的辅助分支曾因 jsdom 的 import.meta URL 不是 file 而失败，改用本地绝对 CSS 路径后，该分支 15 项 UI 测试通过。浏览器本地 file 预览依然被 URL 策略拒绝，没有尝试绕过，也没有启动服务器作为替代。
- 一次误在主仓库执行 npm run build 得到 ENOENT（根目录无 package.json）；随后在 frontend 正确执行并通过，没有改依赖来掩盖路径问题。
- 全仓 `git diff --check` 还会报告用户原有 temp_test.py 的行尾空白/EOF 空行；该文件未修改。本次文件的定向 diff 检查通过。

用例覆盖用户要求的资格 1–6、审批/执行 7–14、Web 15–22、前端 23–28：包含原动作/验证的绑定篡改、无数据/超时/过期、未执行/未知、独立审批、审批后修改参数、重复/并发、唯一副作用、补偿验证失败不递归、三个实际测试子进程崩溃边界、撤销契约后的人工收口、权限伪造拒绝、旧数据读取和敏感内容投影。正向补偿使用明确标识的合成契约和临时文件，绝非真实恢复证据。

最终保护检查：既有 7,856 个清单条目 / 7,318 个唯一文件路径的原始哈希在 README 必要 Web 段落更新前全部一致；该 README 的既有用户修改继续保留，其余保护文件在交付前再次核验。主仓库 data、演示 logs 和 backups 未新增文件；默认 Agent 日志元数据仍早于基线。未修改本机 .env、历史模型原件、授权记录、SQLite/JSON 运行数据、Prompt 或 temp_test.py；没有恢复既有删除，没有暂存、提交或推送。

测试原始完整输出保存在上述操作者 Temp 目录的 full-pytest.log、final-focused-pytest.log、final-lock-pytest.log、final-binding-pytest.log；分别保留各次结果，未覆盖为另一轮的结果。最后一份为 385 项最终相关回归。final-protection-check.json 记录 7,318 个唯一保护文件的零差异核查。静态合成页面 preview.html 也仅在该 Temp 目录，不属于产品数据。

兼容性：沿用 v2 JSON 容器，可读取 v1/v2 旧记录；读操作不迁移或重写。新增字段是可选的，旧 approved 提案摘要保持不变。**旧程序采用 extra=forbid，不能据此保证新记录可以直接降级交给旧程序读取**，降级需要另行评审和备份策略。

工程限制：这是本机文件锁/原子 JSON 范围的保护，不是分布式执行或外部系统 exactly-once 保证；IncidentStore 与 RemediationStore 之间不具有跨存储事务。未来真实动作仍必须提供经过验证的制品完整性、数据兼容、原子前置条件和独立验证契约。当前没有真实回滚制品、没有真实模型或外部服务联调，没有验证电源故障、远程部署和公网认证。浏览器 file URL 被策略拒绝，加上本轮禁止启动服务，因此没有浏览器像素/窄屏截图验收；React DOM 测试与 TypeScript/构建不能代替该验收。

后续需人工决定：审查本次 diff；选定真正可验证的补偿制品和目标契约；决定本机操作人员及最小权限；另行授权真实联调或部署。没有这些决定，生产补偿保持阻断。本轮不提交或推送。
