# Incident Agent W3：真实人工审批与安全恢复验收报告

**结论：代码和自动化隔离验收通过；真实人工浏览器验收待完成。不得据此宣称 W3 全部验收通过或直接放行 W4。**

本轮所有恢复批准来自自动化测试身份。Codex 浏览器仅查看页面、展开配置和检查布局，没有连接操作凭据或点击批准、拒绝、执行按钮。启动器检查也没有创建恢复申请。当前没有用户真实浏览器批准的事实或不同自然人参与的证据。

## 1. 实际 Git 基线

| 仓库 | HEAD / 分支 | 开始时真实远端 | 既有修改 |
|---|---|---|---|
| D:\Incident Agent | b5b30dd10be3521f697936078d1b04438cad8b3f / main | origin/main 同 SHA | 9 个已有跟踪变更，含文档删除/迁移、README、prompts/agent_prompt.py、temp_test.py，另有未跟踪文件及运行记录 |
| D:\prometheus-demo | 0e2f6f3220b9fb9f0baf8730de5ffaaac00d8c38 / codex/postgres-monitoring | origin/codex/postgres-monitoring 同 SHA | README.md 已修改 |

已完整读取主仓库 AGENTS.md；Demo 没有 AGENTS.md，也未发现相关子目录有额外 AGENTS.md。主仓库初始 Schannel 查询失败后，使用 OpenSSL 且保持证书验证成功读取远端。结束时再次查询遇到 TLS EOF 失败，因此只确认开始时远端匹配，未声称结束时远端仍未变化。

本轮没有 reset、clean、stash、restore、分支切换、提交、推送或历史改写。源码固定测试中的 Git 提交仅用于新建的 TEMP Git 夹具，不是主仓库或 Demo 的提交。

## 2. 改动文件和原因

共 14 个既有代码/测试/契约快照文件发生授权修改，新增 4 个源文件，另新增本报告和独立操作指南。没有新增依赖、用户数据库、OAuth、第二套 RBAC、Store、执行器或 Web 应用。

| 文件 | 目的 |
|---|---|
| [remediation/compensation.py](../remediation/compensation.py) | 从持久化申请评估确定 Requester，统一检查补偿职责分离 |
| [remediation/store.py](../remediation/store.py) | 批准/拒绝时执行身份规则；保存精确摘要与授权结果 |
| [remediation/executor.py](../remediation/executor.py) | 阻止申请/审批身份执行；实例探测后再次校验证据；提供只关闭自动申请的构造选项 |
| [web_api/remediation_views.py](../web_api/remediation_views.py)、[schemas.py](../web_api/schemas.py) | 公开字段最小化、原业务证据与到期时间、申请身份和审计绑定字段 |
| [scripts/w2_demo_build.py](../scripts/w2_demo_build.py) | 显式预期源码 SHA、HEAD 和源码工作区校验 |
| [scripts/w2_demo.py](../scripts/w2_demo.py) | 在现有隔离入口增加 --manual-approval；只自动布置 B，等待显式 Requester |
| [scripts/w3_demo_acceptance.py](../scripts/w3_demo_acceptance.py)（新增） | 真实本地 HTTP 三种验收阶段，明确标注自动化来源 |
| [scripts/w3_manual.py](../scripts/w3_manual.py)（新增） | 一个命令启动现有 API 和前端；新 TEMP、只清理核验身份的自有进程 |
| frontend/src/api/client.ts、types.ts、pages/RemediationPages.tsx | 展示审批资料/原证据/到期提示；身份切换清空确认、命令与理由，忽略旧身份迟到响应 |
| frontend/tests/remediation.test.tsx、api-contract.json | 新页面与身份切换回归；由当前后端导出的公开契约 |
| [tests/test_w3_authorization.py](../tests/test_w3_authorization.py)、[test_w3_source_pin.py](../tests/test_w3_source_pin.py)（新增） | 授权、敏感字段、证据期限、轮换、Git 固定的定向测试 |
| tests/test_compensation.py、test_w2_demo.py | 并发测试使用独立执行身份；W2 断言改为公开哈希及 Store 完整参数双重验证 |

WebAuthorization、FastAPI 既有权限依赖及 Remediation/CompensationContract 都被复用。未添加新的可绕过接口，生产 COMPENSATION_REGISTRY 仍默认空。

## 3. Requester / Approver / Executor 身份隔离

既有后端 Bearer 权限 request / approve / execute 继续决定谁能调用接口。客户端提交 approved_by、role、parameters 等未声明字段会被拒绝；服务端不从请求参数信任审批人。

补偿申请的实际身份来自 Store 审计中与 CompensationLink.command_id、原提案和补偿提案匹配的唯一 `approval_required` 申请评估；缺失或不唯一则 fail-closed。批准/拒绝在 Store 事务内检查：Requester 不能审批自己的补偿。执行在执行锁和 Store 事务内检查：Requester 不能执行自己的补偿，Approver 不能执行自己批准的补偿。即使配置给同一个身份全部三个 Web 权限，也会在后端拒绝。

三种操作身份分离不证明三个自然人参与。直接控制本机文件、Python 或 CLI 的受信任操作者仍属于本机信任边界，现有 CLI 不构成独立身份认证系统。

## 4. 审批摘要与审计绑定

沿用 approval_digest 的权威算法，覆盖完整原始参数、提案身份、原修复关联、目标制品及配置、源实例、契约摘要和证据关联。隐藏路径只是公开投影，不改变 Store 原始参数或已有摘要算法。

批准/拒绝事件新增实际 actor、occurred_at、proposal_digest、authorization_result，以及 Web command_id；提案保留 approved_by / approved_at 或 rejected_by / rejected_at / reason。Web 审计公开这些允许字段，不递归公开原始记录。执行仍校验原审批摘要、固定契约、原动作和验证、当前实例、制品精确字节及证据有效期，并先持久化 executing 再产生副作用。

相同命令幂等；第二次冲突审批没有新增另一个 approved 授权事件。相同或新的执行命令再次调用已成功提案，不增加物理替换、动作结果或验证。身份切换会清空本页确认与命令缓存；旧身份的迟到响应被忽略，并提示核查结果。

无 Token / 错 Token / 无权限的认证边界拒绝不修改业务 Store；本轮 HTTP 验收文件保留相应 401/403 响应。业务状态拒绝在可用 Store 中记录 web_operation_denied。这里没有声称新增了持久化鉴权安全事件中心。

## 5. 源码固定与制品保护

构建器默认固定完整预期 commit `0e2f6f3220b9fb9f0baf8730de5ffaaac00d8c38`，也支持显式传入 --expected-source-commit。它要求仓库根目录正确、HEAD 与预期一致，并拒绝 inventory_app.py / http_observability.py 的暂存或工作区变化；从精确 Git blob 读取源码，校验失败先于输出目录创建和依赖收集。README 的既有非源码修改得到保留，未当作构建来源。

隔离 Demo 启动还会拒绝 build.json 中不符合 W3 固定 commit 的来源。没有切换 Demo 分支或修改其源码。

本轮实际制品与原 W2 哈希一致：

- A：`3654279c9ba7276d1e73b7e976301493738a35865b1715a9ef7c7c7b8fdd7635`
- B：`461f9075ec3ae4b7bd8bf74e1fc42e0323e2a6debc701d360c587f10168057b7`

同源码、不同配置；不是两个历史源码版本。保留确定性 ZIP、依赖/解释器/源码文件清单和 SHA-256 校验。目标字节先核验再停止 B，实际启动使用已验证的精确字节。审批后真实篡改 A 文件时，API 返回保存的 not_executed 动作结果，B 保持运行；HTTP 200 响应本身不表示恢复成功。

## 6. Web 敏感运行信息最小化

DemoRecoveryView 删除 artifact_source 与 startup_configuration，替换为 configuration_digest 与固定允许的配置摘要。未认证公开 API 保留制品哈希、源码版本、release、实例、配置摘要和必要审计，不能获取制品或解释器绝对路径、完整环境或原始启动信息。自由文本投影还遮盖本机绝对路径和 URL。

没有新增“查看完整环境”权限或接口，因为本轮审批核查不需要该内容。完整可信执行参数继续留在本机 Store，审批摘要与执行契约仍覆盖它们。测试同时验证“公开响应不含敏感标记”和“Store 保留原始参数、摘要正确、只读 API 不改写文件”。当前公开 OpenAPI 与 TypeScript 契约已同步。

## 7. 300 秒证据处理

未增加有效期。页面新增原修复内容、动作、摘要、独立验证结局、verified_at、expires_at、checked_at、max_age_seconds 和有效状态；浏览器每秒更新到期显示。页面确认时显示当前精确摘要、目标哈希与配置，不笼统授权任意回滚。

后端批准和执行继续用真实时钟检查原业务证据，执行器在可能耗时的实例探测后再次检查，避免慢探测携带过期证据进入 handler。实际自然等待实验最后观测的证据年龄为 **348.642206 秒**，执行返回 409 verification_stale，原已批准状态保留、没有补偿动作，B 的库存仍为 500。

人工核查只新增当前观测，不替换原绑定证据。过期应停止并新建隔离实验，重新申请、批准、执行；没有增加刷新旧审批、改时间或绕过证据的接口。页面时钟仅供展示，实际授权由后端时钟决定。

## 8. 自动化实际结果与失败记录

所有下列验收均为测试身份/自动化，不能作为真实人工验收。

| 验证 | 实际结果 | 退出码 / 证据 |
|---|---|---|
| 审批、授权、版本固定与 Web 最小化定向最终轮 | 44 passed | 0；[日志](../data/w3-acceptance/2026-10-24_16-39-01/run/directed-final.log) |
| 受影响 Remediation / Recovery / Web API / W1 / W3 回归最终轮 | 393 passed，81 subtests passed | 0；[日志](../data/w3-acceptance/2026-10-24_16-39-01/run/regression-final.log) |
| 首轮含 W2 的回归 | 389 passed，1 failed，81 subtests passed | 1；[原始失败日志](../data/w3-acceptance/2026-10-24_16-39-01/run/regression-first.log) |
| W2 真实应用、制品、实例和四个崩溃窗口 | 17 项在首轮含 W2 回归中通过；唯一失败在 test_compensation 的旧并发身份夹具 | 与上行同一轮，未伪造独立运行日志 |
| 前端完整 Vitest 最终轮 | 86 passed / 6 files | 0；[日志](../data/w3-acceptance/2026-10-24_16-39-01/run/frontend-final.log) |
| TypeScript 最终检查 | 通过 | 0；[日志](../data/w3-acceptance/2026-10-24_16-39-01/run/typescript-final.log) |
| Vite 构建 | 通过，输出至新 TEMP，未覆盖原 dist | 0；[日志](../data/w3-acceptance/2026-10-24_16-39-01/run/build.log) |
| 真实 HTTP 全流程 | 18 次 API 请求；B→A，独立 inventory=200 且精确 demo-item:10，重复执行无新增副作用 | 0；[记录](../data/w3-acceptance/2026-10-24_16-39-01/run/http-full/w3-http-acceptance.json) |
| 真实 HTTP 审批后制品篡改 | 15 次 API 请求；阻断恢复，not_executed，B 持续运行 | 0；[记录](../data/w3-acceptance/2026-10-24_16-39-01/run/http-tamper/w3-http-acceptance.json) |
| 真实 HTTP 审批后自然过期 | 18 次 API 请求；执行 409，B 持续运行 | 0；[记录](../data/w3-acceptance/2026-10-24_16-39-01/run/http-expiry/w3-http-acceptance.json) |
| 人工入口启动检查 | 自动布置 B，三种凭据存在，无恢复提案；随后停止 | 通过；[记录](../data/w3-acceptance/2026-10-24_16-39-01/run/manual-launcher-smoke.json) |

测试组包含重叠，不能把行数直接相加。44 项定向测试中的 20 项 W3 测试也在 393 项回归中。前端首次 85 项通过后增加了旧身份迟到响应测试，最终 86 项通过；不是删除失败测试。

首次唯一回归失败原因已确认：旧并发测试同时以 operator 申请和执行，触发新增的后端职责分离；改用 independent-executor 后保留“副作用精确一次”的原断言，重新运行相关套件通过。没有删测试、放宽断言或掩盖间歇性失败。

本轮使用既有 `D:\conda\envs\agentlearn\python.exe`（pytest 9.1.1 / FastAPI 0.140.13 / Pydantic 2.13.5），构建使用 Demo 既有 .venv；前端使用既有 Node v24.15.0。初始通用 conda Python 没有 FastAPI，随后切换至已存在的项目环境，没有安装新依赖。Vite 仅有输出目录在项目外、不清空该目录的预期提示。真实模型调用 0，没有生产业务、数据库、Docker 或监控变更。

### 异常与权限边界覆盖

| 场景 | 验证来源与结果 |
|---|---|
| 无 Token、错 Token | 真实 HTTP 401，业务 Store 不变；ASGI 定向回归 |
| 无审批 / 无执行权限 | 真实 HTTP 403；即使同身份配置全部权限，核心仍以 409 阻断职责冲突 |
| Requester 伪造审批身份 | 真实 HTTP 422，未创建恢复提案；响应不回显敏感输入 |
| 重复申请 / 审批 / 执行 | 幂等重试、冲突决策拒绝；动作和验证没有重复 |
| 审批摘要、参数、目标变化 | 真实 HTTP 错摘要 409；W2 真实应用 + ASGI 的存储参数/目标/源身份变更测试阻断 |
| 原实例变化 | W2 实际重新启动隔离 B 后，旧源实例绑定被拒绝，不停止新实例 |
| 制品哈希变化 | 真实 HTTP 专门篡改 TEMP A 制品；B 未停止 |
| 批准时有效、执行前过期 | 自然等待超过 300 秒的真实 HTTP；另有时钟注入和慢探测定向测试 |
| 执行器进程崩溃 | W2 四个真实 os._exit 窗口：executing 提交后、停止 B 后启动 A 前、handler 返回后、ActionResult 提交后 |
| 结果未知 | 保留 executing / unknown，拒绝重放；实际 A 存在也不补写历史成功 |
| 部署成功但验证不通过 | 真实 B 的库存 500；W2 恢复后停止 A 的真实 no_data；补偿后明确业务 failed 另用离线 FixtureVerifier 验证，不冒充真实 A 的业务失败 |
| 凭据撤销/轮换 | ASGI 验证已加载授权对象不随环境变化，新授权对象拒旧 Token、接受新 Token 并可审批；不宣称即时撤销 |
| 身份切换旧确认 | Vitest 实际切换到另一 actor、摘要刷新、迟到审批响应；旧确认和命令不保留 |

## 9. 真实人工浏览器验收状态

**待完成。** 没有你的真实批准记录，不得标为通过。浏览器自动检查只查看本轮真实恢复记录、键盘展开摘要和过期提示。桌面 1440px 的 document client/scroll 均为 1425px；390px 窄屏均为 375px，无横向溢出；本轮浏览器 error/warn 日志为空。未操作登录或审批按钮。

[完整人工操作指南](W3_MANUAL_BROWSER_ACCEPTANCE_GUIDE_2026-10-24_16-42-49.md)包含一个命令启动、地址、三种凭据用法、Requester/Approver/Executor 每一步、预期状态、独立库存验证、审计、拒绝分支、过期重建、停止和凭据轮换限制。

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B 'D:\Incident Agent\scripts\w3_manual.py'
```

工作台地址为 http://127.0.0.1:5173/remediations/rem-w2-deploy-b。自动化验收进程均已结束；准备手动操作时运行上述命令获得新证据窗口与新 Token。启动器只布置原修复 B，不会替你请求或批准恢复。

浏览器证据：[桌面](../data/w3-acceptance/2026-10-24_16-39-01/run/browser/desktop.png)、[390px](../data/w3-acceptance/2026-10-24_16-39-01/run/browser/narrow.png)、[过期提示](../data/w3-acceptance/2026-10-24_16-39-01/run/browser/expired.png)、[检查记录](../data/w3-acceptance/2026-10-24_16-39-01/run/browser/browser-checks.json)。截图属于自动化记录页面，不代表实际人员已批准。

## 10. 剩余风险与未验证事项

- 凭据在 WebAuthorization 创建时加载，无即时撤销。修改环境变量或 operators.json 不改变运行中对象；需受控重启，新实验生成新凭据。没有新增热更新或持久用户管理。
- 技术角色和 Bearer 凭据不证明自然人身份，不证明多人审批。真实浏览器过程仍需实际人员操作并结合审计和明确确认验收。
- 只验证 Windows x64、本机单实例、无持久业务数据的隔离库存应用；不证明生产回滚、数据库迁移、容器、集群、多副本、完整 OS 隔离、制品签名或 WORM。
- 本机源码仓库、解释器、依赖环境、Store 文件、标准库和系统组件仍在信任边界内。公开配置摘要由固定 Demo 契约表达，不是可任意指定启动命令的入口。
- 停 B 与启 A 有服务中断窗口。崩溃可能留下已获批副作用而结果未知；禁止自动重放不能抹去已发生的操作。四个崩溃窗口已验证，未穷举磁盘/OS 故障。
- 401/403 是认证边界拒绝，不写业务 Store；需要独立持久安全事件审计时应另行设计，不能把本轮测试请求归档当作生产鉴权日志系统。
- 开始时已确认远端；结束时 TLS 查询失败，未确认任务结束时远端是否被其他人员更新。

## 11. 工作区、进程和证据保护

[内容基线](../data/w3-acceptance/2026-10-24_16-39-01/run/baseline.json)与[最终核对](../data/w3-acceptance/2026-10-24_16-39-01/run/final-audit.json)：主仓库 7,664 个基线文件，仅上述 14 个授权既有文件改变，其余一致；Demo 79 个基线文件全一致。两个暂存区均未变化，HEAD 均未变化。保留全部既有用户修改、删除状态、未跟踪文件、历史报告和数据。Git 元数据、依赖目录、可再生 Python/pytest 缓存及 dist 不在内容清单内。

核对 49 个唯一应用/API/前端进程身份，没有仍活动的同一身份；无法打开的 PID 按 closed_or_unavailable 保留，不把“无法打开”夸大为每个 PID 都有独立退出证明。已持有的进程在清理时校验 OS 创建身份、cwd、exe 和 argv；没有按裸 PID 结束未知进程。

[端口核对](../data/w3-acceptance/2026-10-24_16-39-01/run/listener-audit.json)确认 5173、18330–18332、18430–18432 无监听。三个 HTTP 实验及手动入口启动检查各有 cleanup 记录，错误列表为空。没有启动、停止或修改既有业务服务、数据库、Docker、监控或未知进程。

新证据保存在 `D:\Incident Agent\data\w3-acceptance\2026-10-24_16-39-01`，包含测试成功和首次失败日志、真实 HTTP、独立观测、Store、制品、崩溃记录、清理、浏览器检查、源文件快照与本轮专用 diff。归档排除 22 份 operators.json；对所有实际临时 Token 值扫描归档字节，命中 0。部署 vendor 副本省略，保留相应不可变 ZIP、manifest、运行 JSON 和进程日志。文件 SHA-256 清单见 [sha256.json](../data/w3-acceptance/2026-10-24_16-39-01/sha256.json)。未改写历史原始审计。

## 12. W4 准入判断

具备进行 W3 人工操作的代码与自动化基础；**尚不满足以“W3 全部验收通过”为前提进入 W4 的条件**。先由你实际在浏览器核查并批准/拒绝，使用独立执行身份完成恢复和独立库存校验，再将真实操作者、提案摘要、时间、来源与审计一起核对。本轮不替代人工确认，也不预先批准任何 W4 生产或业务恢复动作。
