# 代码级因果证据能力升级与实测报告

验证日期：2026-10-07（Asia/Shanghai）。以下运行证据时间统一使用 UTC。

## 仓库范围与本地修改

- Demo：`D:\prometheus-demo`，分支 `codex/postgres-monitoring`，起始 HEAD
  `4bb634897990256ab67640fe18b64e73f7d0e863`。
- Incident Agent：`D:\Incident Agent`，分支 `main`，起始 HEAD
  `596bc3121d32bb53a50d218245ba462845ab64f6`。
- 执行前实际检查了 status、branch、最近 commit、remote、diff 和代码结构。
  远端分支起始 SHA 均与对应本地 HEAD 一致，没有用远端覆盖本地。
- 保留并纳入本次相关改动：本地 GitHub recent commits 能力、CODE_REPOSITORIES、
  MAX_TOOL_CALLS=13、MAX_AGENT_LOOPS=7、每轮剩余工具调用次数提示、既有相关 Prompt。
- 无关改动仍留在工作区：Agent 的 main.py dotenv 调用位置、monitor 日志字段笔误、
  temp_test.py；Demo README 原有请求示例 `/order` → `/error` 修改保持未暂存。
  没有 reset、checkout 覆盖或 force push。

## 修改文件

Demo：

| 文件 | 行为 |
| --- | --- |
| app.py | 只读 /version，启动时捕获 SHA、UTC 进程时间、显式开关白名单 |
| start_demo.ps1 | 拒绝未提交运行代码；从真实 HEAD 设置 SHA；不一致时警告 |
| tests/test_version.py | 7 项隔离测试，不写业务日志、不连接真实数据库 |
| README.md | 正确启动、核对 SHA、版本证据边界与验证方法 |

Incident Agent：

| 文件 | 行为 |
| --- | --- |
| clients/service_client.py | 复用现有客户端增加 /version 请求 |
| clients/github_client.py | requests 只读 recent/commit/contents/raw，有界 raw 前缀 |
| clients/loki_client.py | 修正 direction 参数拼写 |
| tools/version.py | get_running_version，校验版本结构/UTC/布尔开关，仅输出白名单 |
| tools/code_changes.py | 保留 recent；新增 diff/source；仓库映射、大小与路径校验、错误语义 |
| tools/logs.py | Loki 整数纳秒转换成精确 timestamp_utc，保留原 timestamp/line/labels |
| tools/__init__.py | 注册新工具，仅 @tool 函数进入 registry |
| config.py | 保留本地 13 次、7 轮与服务仓库映射 |
| agent/agent.py | 保留每轮工具余额；补充剩余模型轮数，提醒预留最终结论 |
| prompts/agent_prompt.py | 调查顺序、时间基准、证据边界、受控回归推理、只读约束 |
| tests/test_code_evidence.py | 38 项版本/diff/source/Loki/registry/剩余预算测试 |
| tests/test_incident_regression.py | 原有入口烟测使用占位 Key，避免依赖本地 secret；保留全部旧断言 |
| README.md | 工具契约、预算和验证命令 |
| CODE_EVIDENCE_REPORT.md | 本报告，仅记录经核实的摘要，不提交原始运行文件 |

## 工具与限制

新增 Agent-facing tools：get_running_version、get_commit_diff、get_source_file。
既有本地 get_recent_code_changes 同时保留并发布。

get_running_version 沿 Agent → Tool → ServiceClient → 配置服务 /version；
未配置、缺失接口、unknown SHA、异常结构明确 no_data/reason。SHA 只接受完整
40 位十六进制；时间必须明确 UTC。unknown SHA 时仍保留有效的开关/启动时间。

GitHub 仅访问 CODE_REPOSITORIES 映射的仓库，不接受模型指定 repo。Token 从环境
延迟读取，避免导入早于 load_dotenv 后永久缓存空 Token；没有新 SDK 或写 GitHub API。
现有 fine-grained Token 实际成功读取目标仓库，不需要扩大仓库范围。

diff 最多 15 个文件、单 patch 6000 字符、所有 patch 总计 24000 字符。
API 下一页、文件数超限、字符截断、GitHub 未提供 patch 都明确标记；文件保留
filename/status/additions/deletions/changes。没有 patch 不能证明未修改。

source 按指定 ref 读取最多 20000 字符 UTF-8 文本；拒绝目录穿越、绝对路径、
反斜线、编码绕过和非文件类型。无效 UTF-8/二进制控制字符返回 unsupported/no_data。
>1MB 时读取最多 80004 字节的 raw 前缀，标记 truncated；未返回的后缀未验证。
HTTP 404、权限/限流、不可达、结构错误有明确原因；错误不返回 Token 或请求头。

Loki timestamp 用整数 divmod 拆出秒和纳秒，生成 UTC Z（必要时 9 位小数），
保留原始 timestamp 和 line。recent committed_at 使用 committer.date，避免混用
author 时间。无时区 Incident/日志日期不得用于精确时间间隔计算。

Prompt 区分：仓库存在提交、实例自报版本、进程启动时间、diff 修改、源码包含路径、
运行日志中实际异常。只有需要补全上下文/当前版本确认时读取源码；应用异常且数据库
访问正常时优先调查代码，不机械扫所有 PostgreSQL 指标。数据中的指令不被执行。

## 回归测试

- 修改前：Incident Agent 58 项全部通过。
- 修改后：Incident Agent 96 项全部通过，包含原有 58 项和新增 38 项，未删除旧测试。
  环境：D:\conda\envs\agentlearn\python.exe。
  命令：`python -B -m unittest discover -s tests`。
- Demo：.venv 下 7 项全部通过；覆盖 SHA unknown/非法值、UTC 稳定时间、开关
  allowlist、开启后的指定应用异常、关闭后的 200，以及 version 不影响业务 Counter/health。
- 新增工具 registry/schema 正确，路径/时间辅助函数没有注册为 Agent 工具。
- 冻结暂存快照也运行同一 96 项全部通过，不复制 .env；入口烟测显式使用占位 Key，
  其 Prometheus 轮询本就隔离为无告警快照，不发生真实模型请求。
- 最终暂存范围执行 diff --cached --check 和配置 secret 值扫描；原有 temp_test.py
  的空白缺陷未修改、未暂存。

## 真实 Demo、数据库与 GitHub

Demo 已提交并推送：`585035a07e0d31c735ccb2d6d7674fa656ac69cf`，
分支 `codex/postgres-monitoring`；随后使用 start_demo.ps1 启动实际 HEAD，
运行目录没有未提交 Python/PowerShell 源码。启动前 8001 没有监听进程。

实际 /version：

```json
{
  "service": "order-service",
  "commit_sha": "585035a07e0d31c735ccb2d6d7674fa656ac69cf",
  "short_sha": "585035a",
  "process_started_at": "2026-10-07T08:57:43.930178Z",
  "runtime_flags": {"simulate_code_regression": true}
}
```

- /health=200；/order=500，error_type=application_error，
  message=simulated application code regression。
- 后续受控流量共观测 19 次 /order=500，没有请求异常。
- order-db 容器 healthy；主动探测 DNS/TCP/数据库连接/SELECT 1 均成功，reachable=true。
- 真实 Prometheus postgres_up=1.0，http_5xx_rate=1.0；HighHttp5xxRate 实际 firing。
- 真实 Loki 取得本进程启动后的 application_error / RuntimeError 对应日志，
  例如 timestamp_utc=`2026-10-07T08:59:19.877436298Z`。
- GitHub diff 确认 `4bb634897990256ab67640fe18b64e73f7d0e863` 引入故障路径。
- GitHub app.py @ running SHA 完整返回，未截断；blob SHA
  `9a8867ad99a6d0a83e474ef0dc2ef10c6b48c375` 与本地实际 app.py Git blob 一致，
  全文也相同。运行源码仍包含 if SIMULATE_CODE_REGRESSION 和对应 raise。
- 启动、commit、日志比较使用 UTC，没有将 line 的 16:xx 与 GitHub 的 08:xx
  直接相减。进程启动时间没有被当成 CI/CD deployment 时间。

## 真实 Agent 调查

使用真实配置的 deepseek-flash/Responses 接口、真实 Agent、ToolManager、
IncidentMonitor 和 PrometheusClient。Monitor 处理真实 firing 告警；没有伪造模型
工具调用或外部服务响应。仅把 IncidentStore 和日志目的地隔离到临时目录。

本次独立调查 Incident 为 inc-63d10dad；模型 completed，5 轮，12/13 次工具调用：

1. 第一轮：query_logs、check_service_health、get_service_dependencies、
   http_5xx_rate、http_request_rate。
2. 第二轮：check_dependency_health、postgres_up、get_running_version、
   get_recent_code_changes。
3. 第三轮：get_commit_diff(4bb6348)、get_commit_diff(585035a)。
4. 第四轮：get_source_file(app.py, ref=585035a 的完整 SHA)。
5. 第五轮：输出最终调查结论。

没有机械查询 connections、connection_usage、active、idle、idle_in_transaction、
lock_waits。Agent 自主取得运行版本 + runtime flag + 引入 diff + 当前源码 + 运行
日志，全部成为有效 evidence。两个 commit 的 diff 用于分别确认引入逻辑与当前
版本修改；源码查询用于确认逻辑仍存在。

Agent 最终明确得出：当前运行实例的 SIMULATE_CODE_REGRESSION=true 激活受控
应用回归逻辑，数据库查询成功后抛出同一 RuntimeError 并持续 500。4bb6348 引入
这条路径，但同一代码在开关关闭时可以正常运行，不能简化为“commit 自动导致故障”。
最终回答包含【已确认事实】【代码级因果证据】【最终根因 / 高确定度根因】
【仍然存在的不确定事项】【建议修复方式】，没有执行建议修复或数据库操作。

## 不确定事项与当前运行状态

- 服务 SHA 仍是启动环境自报。此次验证额外核对启动 HEAD、运行目录和 GitHub
  blob，但未建立正式 CI/CD 制品认证。进程启动后不应修改运行目录。
- 仅验证本地被探测实例；多实例一致性、部署记录/镜像 digest 尚未实现。
- 健康探测与 postgres_up 不能排除所有数据库性能问题；本次具体故障由更直接的
  failure path 与运行异常证据解释。
- Prompt 控制模型调查策略；这次真实模型成功不代表所有未来 Incident 都必然
  在有限预算内完成。HTTP 错误、缺失 patch、权限及文件截断仍可能留下证据缺口。
- 原有监控进程在测试时仍运行，独立响应了同一真实告警，向原有数据/日志写入
  另一个 Incident（inc-1ffc4e48）；所以原有运行文件哈希发生变化。本次隔离调查
  没有把 inc-63d10dad 写入原有 Store，也没有覆盖或回滚这些数据。
- 测试请求已结束；新启动 Demo 仍在 8001，回归开关仍 true。没有自动关闭开关、
  重启服务或停止数据库。数据库与观测容器未改动。
- .env、Token、API Key、.secrets、日志、原始 JSON、临时目录和运行数据均不进入提交。
- 两个仓库各自推送当前分支，并在推送后核对本地/远端完整 HEAD。
  Incident Agent 本提交的 SHA 由最终交付消息记录，避免报告引用自身产生循环。
