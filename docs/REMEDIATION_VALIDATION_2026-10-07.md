# Remediation 实现与真实验证

日期：2026-10-07（Asia/Shanghai）；以下运行时间均为 UTC。架构、字段、状态机、
CLI 与限制详见 [REMEDIATION_GUIDE.md](REMEDIATION_GUIDE.md)。

## 仓库与提交范围

开始时实际执行两个仓库的 status、当前分支、最新 commit、diff 和 cached diff。
Incident Agent：main，起始 HEAD c746d14；Demo：codex/postgres-monitoring，
HEAD 585035a07e0d31c735ccb2d6d7674fa656ac69cf。开始时两个 index 均为空。

Agent 原有未提交内容：文档归档（两份根报告删除、README 和 docs）、main.py 的
dotenv 调用位置、monitor 创建日志字段拼写、temp_test.py、两份 remediation 草稿。
仅沿任务相关草稿补齐功能；修改前草稿和共享文件备份位于
`%TEMP%\incident-remediation-20261007`。其他原有改动保留在工作区且不暂存。
main 和 monitor 的提交内容专门排除原有 dotenv／日志拼写修改，工作区仍保留它们。

Demo 的现有 start_demo.ps1 已满足正常模式 false、真实 HEAD 和未提交源码拒绝条件，
因此无需改动或制造 Demo commit。Demo README 的原有修改保持原样未暂存。
没有 reset、checkout、丢弃本地改动或 force push。

新增／修改任务文件：

- models/remediation.py：完整 Pydantic records。
- remediation/__init__.py、state.py、approval.py、store.py：状态机、审批和持久化。
- remediation/planner.py：结构化证据闸门。
- remediation/demo_action.py、windows_process.py：唯一动作与进程归属验证。
- remediation/verification.py、executor.py：独立恢复验证与执行入口。
- remediation_cli.py：独立人工命令。
- monitor/incident_monitor.py、main.py：调查成功后可选 Planner 注入。
- tests/test_remediation.py：114 项新回归测试。
- docs/REMEDIATION_GUIDE.md、本报告：设计与经核实验证。

没有改 config.py、Agent 调查 loop、预算、Prompt、既有 tools 或 IncidentStore。

## 自动测试

解释器：D:\conda\envs\agentlearn\python.exe；当前实测 Pydantic 2.13.5。

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B -m unittest discover -s tests
```

- 改前 Agent 96 项通过；改后 210 项通过（原有 96 + 新增 114），没有删除旧测试。
- 包含模型字段／UTC、完整合法和非法转换、审批身份／重复审批／拒绝、白名单和参数、
  各 precondition、窗口／新进程日志边界、源码 AST、幂等、动作与恢复分离、所有主要
  HTTP 验证失败、Store 损坏／原子 replace／写失败、OS 锁竞争／执行 claim、CLI 和
  Monitor 隔离，以及模型 function call 无法找到 action。
- Native handle 的身份变化拒绝与仅终止匹配句柄有独立测试；真实本机过程见下方。
- Demo 既有 7 项测试在 .venv 中全部通过。
- 提交前另导出 Git index 冻结快照并运行同一 210 项，检查 cached diff 和实际本地
  secret 值；原始 .env、日志、运行 JSON、备份和临时文件均不在 index。

## 真实调查与 pending

明确验证旧 Demo 8001 listener 的 PID=35732、真实 CWD=D:\prometheus-demo、
exe=agentlearn/python.exe、argv=python.exe app.py，并检查 HEAD／运行代码状态。
按用户要求建立测试故障：只停止该 Demo，用现有 start_demo.ps1 -SimulateCodeRegression
启动新实例。没有停止原有 Monitor 或任何 Docker／数据库／观测组件。

故障进程 PID=11000；自报 process_started_at=2026-10-07T11:02:53.189466Z。
数据库实际 DNS/TCP/连接/SELECT 1 探测成功，reachable=true；真实 HighHttp5xxRate firing。

真实 Agent 使用现有 deepseek-flash、ModelClient、ToolManager、IncidentMonitor 和真实
外部服务，4 轮、10/13 次工具调用；没有 mock 模型或伪造 tool response：

1. query_metrics、check_service_health、get_service_dependencies、query_logs。
2. check_dependency_health、postgres_up、get_running_version、get_recent_code_changes。
3. get_commit_diff、get_source_file(app.py @ 当前完整 SHA)。
4. 最终 completed 结论。

Incident：inc-d92e1019。真实调查同时确认回归开关、实际指定应用异常、引入路径 diff、
当前源码仍包含该路径，以及数据库正常。没有将旧进程日志当成当前实例新证据。

生成 proposal：rem-3b7fe19f433341a8a75266e0eb4b0798，action=disable_demo_regression，
risk=low、status=pending。pending 前后完整 version 和进程身份相同；actions=0；
/order 仍 500。随后真正尝试 CLI execute pending，返回 proposal_not_approved:pending
和退出码 2，仍无运行变化。

## 拒绝、审批与执行

额外建立明确标识为拒绝测试的 pending proposal：
rem-reject-43273ccd87914a8aa741b840a164cc64，对应 inc-reject-integration。
真实 CLI reject --by zp1222 --reason ... 后执行，返回 proposal_not_approved:rejected，
退出码 2。Demo version、启动时间、PID=11000、flag=true、/order=500 均未变。

根据用户明确要求的集成测试，通过独立 CLI 对主 proposal 执行
approve --by zp1222，再 execute。zp1222 是本机实际 OS 用户名，并非由模型输出审批。
批准已保存时单独验证 actions=0、flag=true、/order=500；approve 没有隐式触发动作。

Action 开始：2026-10-07T11:05:15.223578Z；完成：11:05:25.026235Z。
旧 Demo PID=11000；新 Demo PID=36176；执行后 commit 与此前完整 SHA 相同。
ActionResult.success=true、status=action_completed。
Verification 时间：2026-10-07T11:05:25.086151Z；success=true。
最终 proposal=succeeded；rejected 测试 proposal 仍为 rejected。

| 项目 | 故障时 / pending 时 | 执行并验证后 |
| --- | --- | --- |
| /version HTTP | 200 | 200 |
| simulate_code_regression | true | false |
| commit_sha | 585035a07e0d31c735ccb2d6d7674fa656ac69cf | 相同 |
| process_started_at UTC | 11:02:53.189466Z | 11:05:22.592375Z |
| /order | 500（指定 application_error） | 200 |
| /health | 200 | 200 |
| 数据库可访问 | true | true |

真实再次 CLI execute succeeded，返回 already_completed；PID 仍为 36176，物理 action
仍只有 1 条，verification 仍只有 1 条。没有再次重启。

恢复后曾真实采到 http_5xx_rate=1.0，随后 idle 窗口为 no_data；没有把任何一个值当成
立即验证失败或虚构为 0。新进程尚无 status=500 序列时，现有比例 PromQL 的分子可为空。
没有用仍存在的旧 Loki RuntimeError 判定失败，也没有删除日志。

## Audit 与原始证据

主 proposal 审计包含 proposal_created、pending execution_denied、approved、
execution_started、action_succeeded、verification_succeeded、remediation_succeeded、
execution_skipped。拒绝 proposal 包含 proposal_created、rejected、execution_denied。
共 11 条，含明确 UTC、actor 和 action_type。

隔离真实数据保存在本地 Git 忽略目录：data/remediation-integration/ 下的
remediation.json、incidents.json、pending-summary.json、final-summary.json、调查日志和
一次性 investigate.py。未提交原始 JSON 或运行日志。默认生产 RemediationStore 没有
被本次集成数据覆盖；既有 Monitor 继续运行，可能独立写入其原来的 Incident 数据。

## 当前限制

- 只有本地 order-service 一个固定动作，Windows x64 原生进程识别；其他平台失败关闭。
- --by 是本机操作员声明，没有账号认证或签名；本地控制 Python／JSON 的用户不在
  模型权限边界内。摘要检测计划变化，不宣称能抵御本机恶意篡改。
- 用 Demo 默认数据库连接配置；自定义 ORDER_DB_* 环境不会继承。
- 进程停止和重新启动不是原子事务，短时业务中断；启动失败没有自动 fallback。
- 执行器／磁盘故障可保守留下 executing，需要人工核查，不自动重放动作。
- Planner 对当前调查证据严格；缺少完整当前源码、近期异常或数据库探测时不会提议。
- Verification 只验证被探测实例的即时恢复，未增加持续健康 SLO、多实例或制品认证。
- succeeded 不直接修改 Incident.status；原有 Monitor 根据告警快照继续判断恢复。
- 已运行的旧 Monitor 进程不会热加载新 Python 文件；正常重新启动 main.py 后使用新
  Planner。真实集成采用新代码独立 Monitor 并已验证完整调查链。
