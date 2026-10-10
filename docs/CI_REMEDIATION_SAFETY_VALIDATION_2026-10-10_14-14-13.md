# CI 与 Remediation 安全恢复验证记录

## 基准与范围

审查基准为 `e97fbab07e2b47c2c8efe09fb7bcd33b882fa472`。开始时 `D:\Incident Agent` 的 main、HEAD 和远端 main 均指向该提交，暂存区为空；工作区有 19 项已有修改或未跟踪文件，包括 README、Prompt、Monitor、main、文档归档和 AGENTS.md。已读取当前 AGENTS.md。

在独立的 Git worktree 中实现与测试，只提交本任务文件。原工作区随后安全快进；18 个其他路径的内容哈希及 README 原有增删行逐项一致，暂存区仍为空。没有修改运行数据、历史报告、真实 Demo 的配置或进程。

实现提交为 `d9e5b76ad90a65eee08ecd83d1f73e7bdcea1835`；Windows 编码补修提交为 `db90e7feb7ffca97b90cc4a5463bf7fea56f8fb6`。本报告记录这两个源代码提交的验证，报告和 README 索引将另行提交。

## 逐项核实结果

| 问题 | 核实结论与处理 |
| --- | --- |
| CI 缺少 httpx | 成立。SDK 3.14.0 的已安装元数据要求 httpx2，生产锁定 httpx2 2.13.0；测试残留旧 httpx 导入。两处测试改用原生 httpx2，未为迁就测试增加旧生产依赖。 |
| P1-A 工具预算差 1 | 不成立。原有两个指定测试在修改前通过。10 次加版本探测为 11，剩余 2；计数 1 时剩余 12；第 13 次预留成功，第 14 次拒绝。业务预算和业务 Prompt 未修改。 |
| EXECUTING 永久挂起 | 成立。增加可信即时核查与人工收口入口；不自动重放、不补造 ActionResult、不把当前健康当作历史动作成功。 |
| rejected/failed 永久去重 | 同一运行实例确实无法通过原入口重新申请；另有过宽的 incident/action/SHA 条件阻止新进程的合理申请。保留同目标去重，允许受控、有限的新提案，取消阻止新进程的过宽条件。 |
| already_completed 重新验证成功 | 成立。旧成功状态下再次 execute 不执行、不验证；返回 success=false、verification_performed=false，明确仅承认历史状态。CLI 的退出 0 表示接受这次幂等查询，不表示新验证成功。 |
| Handler 异常没有安全日志/审计 | 成立。固定 handler 内部及外层异常保留异常类型、proposal ID、动作结果语义和审计；不记录原始异常文本或凭据。 |
| 启动环境完整性 | 成立。原先只检查脚本和解释器文件存在。增加启动前隔离导入检查，确认必要模块来自目标 .venv；失败发生在停止服务前。未修改原启动脚本。 |
| 本地 HTTP 受代理影响 | 成立。隔离临时 HTTP 服务证明无效代理可使旧请求失败。固定回环客户端使用 trust_env=false，并禁止重定向；真实临时请求成功。 |
| Windows Store 读写竞争 | 已在临时 Windows 文件上复现读句柄阻止原子替换。读写现在共用原有文件锁，事务内用无嵌套锁读取；跨进程读写和两写者审批测试通过。 |
| 普通 resume 清除全局模型阻塞 | 成立。移除隐式清除；只有显式 unblock_model(actor)/unblock-model --actor 解除，并持久化 actor、UTC 时间、原原因、结果和操作 ID。 |
| Windows 中文编码 | 真实 CI 后续发现的两项缺陷。JSON 测试显式 UTF-8 读取，两 CLI 显式 UTF-8 输出；增加 cp1252 子进程验证。 |

OpenAI SDK 兼容性还通过实际 SDK、httpx2.MockTransport、httpx2.Request/Response 与 APIStatusError 的 402 回调验证；回调调用次数为 1。这里只验证真实 SDK 类型与异常处理，网络和模型响应被隔离替身替代。参考 [OpenAI Python SDK 官方文档](https://developers.openai.com/api/reference/python)。

## 最终恢复语义

复用 [现有 ActionExecutor](../remediation/executor.py)、[RemediationStore](../remediation/store.py)、[审批摘要](../remediation/approval.py) 和 [状态机](../remediation/state.py)。没有第二套执行器、新模型修复工具或 Web 功能。

动作结果与业务恢复分开：

- `not_executed`：尚待批准/执行，或已可靠记录的执行前失败、无需执行。
- `executed`：已保存结果确认发生了动作副作用；部分执行失败也可能属于此值，它不等于业务恢复成功。
- `unknown`：没有足够可靠的历史动作记录，或停止动作已尝试但效果无法确认。旧记录没有该字段时也采用此值。

`executed_at` 表示已持久化的执行认领时间，不证明实际动作已发生。认领后、动作前崩溃和动作后、结果保存前崩溃，在恢复存储中都没有可区分的可靠动作结果，均保持 unknown。动作结果保存后、验证前崩溃，人工收口保留已经记录的 outcome；没有独立验证时不补判 succeeded。

核查仅重新调用固定可信 HTTP 验证器，保存本次 UTC 时间、verification_id、检查结果与 `purpose=manual_check`。结果必须匹配 Proposal 且产生于本次核查；异常、过期或错配形成真实的“本次观测失败”记录。核查不生成历史 ActionResult，也不修改历史动作结果。

人工收口必须提供操作者、理由和命令 ID，并在服务执行锁内完成。新的观测、CLOSED 状态和收口审计原子保存；记录原 action_ids 和本次 verification_id。当前检查失败也可以由操作者接管收口，但原 unknown 必须保留。相同命令 ID、操作者和理由重复提交只记幂等审计，不再次观测；冲突拒绝并审计。仍在执行的进程持有服务锁时，核查/收口不能并行进入。

CLOSED 是终态，不是成功状态。原 Proposal 此后不能 approve、execute 或 retry；unknown 的 CLOSED 仍在默认关注列表中。无法确认历史结果时应转人工调查，不能通过旧提案自动重试。

## 状态转换与去重

| 起点 | 终点或新记录 | 条件 |
| --- | --- | --- |
| PENDING | APPROVED / REJECTED | 原有人工审批或拒绝，保留 |
| APPROVED | EXECUTING | 原执行入口校验批准摘要、白名单与参数，先持久化认领，再进入原 handler |
| EXECUTING | SUCCEEDED / FAILED / VERIFICATION_FAILED | 原动作结果及独立验证规则，保留 |
| EXECUTING | CLOSED | 新人工收口，带 ManualClosure、本次观测和审计 |
| REJECTED / FAILED | 原状态保留，生成另一个 PENDING | 显式 retry-proposal；FAILED 必须可靠确认 not_executed；重新核查原目标身份和开关；必须另行批准 |
| CLOSED / unknown 失败 | 无重执行转换 | 拒绝动作重放 |

去重身份仍包含服务、固定动作、参数、完整 SHA 与进程启动时间，跨 Incident 也不重复生成相同动作。新进程是新身份。显式重申请用 retry_of 链接旧记录；每个父提案只有一个后继，相同请求返回已有后继；同一链最多 2 次后续申请，防止无限生成和重复审批。新提案为 PENDING，不继承批准，摘要包含 retry_of。原批准摘要与执行前版本、进程身份、工作树检查没有绕过。

## CLI 操作

以下均为明确的本地人工入口，actor/by 是审计身份，仍依赖原操作系统访问控制，不新增身份认证系统。自定义 --store 放在子命令之前。

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B remediation_cli.py list
& 'D:\conda\envs\agentlearn\python.exe' -B remediation_cli.py list --all
& 'D:\conda\envs\agentlearn\python.exe' -B remediation_cli.py list --status executing
& 'D:\conda\envs\agentlearn\python.exe' -B remediation_cli.py show rem-替换为真实ID
& 'D:\conda\envs\agentlearn\python.exe' -B remediation_cli.py inspect-execution rem-替换为真实ID --by 操作者
& 'D:\conda\envs\agentlearn\python.exe' -B remediation_cli.py close-execution rem-替换为真实ID --by 操作者 --reason '核查后人工接管，旧动作结果无法确认' --command-id 人工唯一命令ID
& 'D:\conda\envs\agentlearn\python.exe' -B remediation_cli.py retry-proposal rem-替换为真实ID --by 操作者 --reason '执行前问题已修正，申请重新批准'
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py unblock-model --actor 操作者
```

默认 list 包含 pending、approved、executing、failed、verification_failed 与 outcome 为 unknown 的 CLOSED。list --all/show 可查看全部历史。show 从单一锁定快照输出状态、历史动作、验证、审计和 execution_outcome。inspect/close 不执行修复动作。

审批、执行仍为不同命令；普通消息、HumanRequest 回答、resume、人工核查与收口都不是批准。

## 存储与兼容

Remediation JSON 读取 version 1/2，下一次原子写入升级到 version 2。只读不会迁移，不清空旧数据。新增可选 retry_of、closure，ActionResult 的缺省 outcome 为 unknown，VerificationResult 新增 ID 与 purpose。旧批准摘要无 retry_of 时计算不变，已有合法批准仍可使用原执行入口；迁移回归已经验证。

旧程序不能读取 version 2，按原校验拒绝，从而避免旧执行器忽视新终态；需要继续使用新版程序访问迁移后的数据。没有新增生产依赖、数据库或跨存储事务框架。

## 验证记录

本地使用 Python 3.12.14 创建无系统包继承的新 venv，实际安装 requirements-test.lock.txt；pip check 输出 `No broken requirements found.`。生产固定依赖与 pytest 依赖分别由 [requirements.lock.txt](../requirements.lock.txt) 和 [requirements-test.lock.txt](../requirements-test.lock.txt) 管理。CI 自建 .ci-venv、实际安装、检查并执行完整 tests，且检查所有固定版本与旧 httpx 缺失。

| 验证 | 实际输出 / 覆盖 |
| --- | --- |
| 修改前两项指定预算测试 | 2 passed、28 deselected、2 subtests passed，15.15s |
| SDK、预算、恢复与模型阻塞局部回归 | 146 passed、100 subtests passed |
| Windows 编码定位后局部回归 | 3 passed，12.28s；两个真实 CLI 子进程的 cp1252 输出/持久化断言 |
| 最新完整隔离 pytest | 475 passed、178 subtests passed，184.86s |
| 实际 Demo .venv 只读预检 | 必要模块导入且来源在目标 .venv 中，通过；没有停止或启动服务 |
| 临时原生集成 | 3 个崩溃边界、JSON 跨进程锁/审批竞争、临时回环 HTTP 代理验证，通过 |

关键测试包括：

- `test_two_remaining_three_calls_keep_outputs_and_complete_without_retry`、`test_real_thirteen_boundary_prompt_and_reservations`：版本探测计数、输出配对与最终 13 次执行。
- 真实 Agent Prompt 的 12/2/0 余量、13 次不同参数工具调用、13 成功/14 拒绝且不改计数。
- `test_real_crashes_at_three_boundaries_never_replay_actions`：只在自建子进程和临时目录中 os._exit(73)，用 callback-reached 和 test-effect 文件确认回调实际到达与副作用边界。
- 动作结果保存失败、收口保存失败、重复收口、未确认结果禁止重试、过期/错配观测、活跃执行锁、旧 v1 迁移。
- Handler 内外异常安全日志、already_completed 不验证、审批与动作仍分离、重申请必须新批准与有限去重。
- `test_resume_never_clears_global_block_or_changes_other_sessions`：401/402/403 下两个 Session，resume 不调用模型/工具、不改变其他 Session；显式解除与重复解除均有持久审计。
- 实际 SDK 回调、干净环境依赖检查、CLI 中文子进程、真实 Windows 读写与临时 HTTP。

首次本地干净全量曾出现 1 failed、448 passed，定位到 test_runtime_quality 中的内层旧 httpx 导入并修正；以上最新全量结果是在这些修复后重新实际运行所得。

这些是单元/Mock 测试和隔离原生进程、文件、HTTP 集成，不是实际模型调用或真实服务停止/启动联调。

### GitHub Actions 实际结果

原始失败运行 [37921657418](https://github.com/yuj1ng1/incident-agent/actions/runs/37921657418)：测试收集时 `ModuleNotFoundError: No module named 'httpx'`。

第一次修复运行 [37927417287](https://github.com/yuj1ng1/incident-agent/actions/runs/37927417287)：干净安装成功，pytest 为 `2 failed, 471 passed, 1 skipped, 178 subtests passed in 206.93s`。已读取日志：一处 UTF-8 JSON 通过 cp1252 读取失败；另一处 CLI 中文输出报 UnicodeEncodeError，虽然消息已持久化，命令退出 2。本地强制 cp1252 复现后修正，保留该失败事实。

编码补修提交的运行 [38029918017](https://github.com/yuj1ng1/incident-agent/actions/runs/38029918017)：结论 **success**，源代码 SHA 为 `db90e7feb7ffca97b90cc4a5463bf7fea56f8fb6`。已读取实际日志：隔离安装成功，pip check 无依赖冲突，完整 pytest 为 `474 passed, 1 skipped, 178 subtests passed in 121.45s`，所有工作流步骤通过。

远端已有跨仓库规则测试在缺少 `D:/prometheus-demo/alerts.yml` 且未设置 ALERT_RULES_PATH 时跳过；本地该文件存在所以通过。没有删除测试或为本次安全测试添加跳过。

## 限制与交付检查

默认 `D:\Incident Agent\data\remediation.json` 在检查时不存在，未发现该默认路径中的待收口 Proposal。本任务没有对真实 Proposal 执行动作或人工收口。未知结果场景在临时测试中实际产生并保留，临时目录随测试清理；其他自定义存储没有扫描，不能声明其未知结果已清零。

不能从普通 JSON、当前 HTTP 健康或认领时间重建无法持久化的历史动作，更不承诺外部副作用与本地 JSON 具备跨系统原子性。保持 unknown 是有意的保守语义。跨进程锁只保护遵循存储接口的调用，手工改写 JSON 或不受控进程不属于受支持恢复方式。

本次完整源代码 diff 已审查；预算、工具权限、批准摘要、固定目标与执行前身份检查保持原边界。凭据格式扫描未发现真实凭据；未提交 .env、缓存、日志、运行数据、用户已有 AGENTS.md 或无关修改。新报告使用实际正文完成时的北京时间命名，README 仅增加本次索引；既有报告正文不变。最终文档提交的远端 CI 结果应以对应提交的 Actions 记录为准。
