# Incident Agent

基于 Prometheus 告警、Loki 日志和服务健康检查的 SRE 故障排查 Agent。

当前版本：完成告警生命周期监控、Incident 持久化和回归问题修复。

调查统一由 `IncidentMonitor → InvestigationManager → Scheduler/Worker → Agent → ToolManager` 执行。
Agent 保留 Prompt、FunctionCall 解析与回答解释；旧同步 `Agent.investigate()` 已移除。

## 运行环境与结果查询

项目要求 Pydantic 2；默认 Conda base 的 Pydantic 1 无法导入 `field_validator`。
已验证环境为 `agentlearn`：Python 3.12.14、Pydantic 2.13.5、OpenAI SDK 3.14.0。
在 PowerShell 中先进入项目目录，再显式选择解释器：

```powershell
Set-Location 'D:\Incident Agent'
& 'D:\conda\envs\agentlearn\python.exe' -B main.py
& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest tests -q --disable-warnings
```

也可先 `conda activate agentlearn`。在新的 Python 3.12 环境中使用
`python -m pip install -r requirements.lock.txt` 安装本项目及 pytest 的固定依赖；
该文件只包含项目依赖闭包，没有导出 Conda 中的无关包。一般依赖声明仍在 `requirements.txt`。
`.github/workflows/runtime-tests.yml` 在 Windows/Python 3.12 运行隔离回归，测试不需要真实密钥。

Runtime 保存结果后，终端显示 `[Investigation Completed]` 和完整回答；
等待人类时显示 `[Human Input Required]`、Request ID 和问题。
通知使用 SQLite 中的简单标记避免每次轮询或重启重复展示。
终端故障不影响结果保存，CLI 可随时主动查询历史结果：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py list-sessions
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py show-session ses-替换为真实ID
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py list-human-requests
& 'D:\conda\envs\agentlearn\python.exe' -B runtime_cli.py answer-human-request ses-替换为真实ID --request-id req-替换为真实ID --actor 本地操作员 --text '回答内容'
```

`show-session` 包含 `investigation_result.answer`、Session 计数、限制、HumanRequest、
错误类别及投影重试信息。自定义 `--store`、`--incidents`、`--remediations` 参数放在子命令之前。
模型认证/余额错误会全局阻塞请求；取消 Session 不代表问题已修复。
修正认证/余额后，可显式运行 `runtime_cli.py unblock-model --actor 本地操作员`；
暂停的 Session 仍需 `resume`，已取消的 Session 不会复活。修复批准与执行继续使用独立 `remediation_cli.py`。

本次问题核实、预算规则、测试结果和未验证范围见 [Runtime 质量修复验证记录](docs/RUNTIME_QUALITY_FIX_VALIDATION_2026-10-09_19-01-18.md)。

## 当前内容

- `main.py`：创建对象、组装依赖并启动 Monitor。
- `monitor/incident_monitor.py`：轮询告警，调度调查、重新调查和恢复判断，并隔离单轮异常。
- `investigation/`：管理调查生命周期、权威预算、Checkpoint、缓存、HITL 和恢复对账。
- `agent/agent.py`：构建 Prompt、发起单轮模型请求、解析 FunctionCall 和最终回答。
- `incident_store.py`：将 Incident、证据和调查记录保存到 `data/incidents.json`。
- `clients/`、`tools/`：查询真实 Prometheus 指标、Loki 日志和服务健康状态。
- `models/`：Alert、Evidence、Incident、InvestigationResult 和 InvestigationRecord。
- `model_client/`：单次模型请求；重试及累计预算由 Runtime 控制。
- `tests/`：隔离回归测试，覆盖生命周期、证据、预算、HITL、审批和跨进程恢复。

## 环境配置

依赖列在 `requirements.txt` 中，包括 OpenAI SDK、python-dotenv、Pydantic 2 和 requests。请使用 Python 3.10 或以上版本及兼容依赖；本次验证环境为 Python 3.12、Pydantic 2。

在项目根目录安装依赖，将 `.env.example` 复制为 `.env` 并填写 `DEEPSEEK_API_KEY`。服务地址和模型名称在 `config.py` 中配置。

默认连接 Prometheus `http://localhost:9090`、Loki `http://localhost:3100` 和 order-service `http://localhost:8001`。告警解析要求 firing alert 的 labels 包含 service、metric、severity，annotations 包含 threshold，value 能转换为数值。

```powershell
python -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
# 编辑 .env 填写密钥，启动所需服务后运行：
python main.py
```

Incident 数据文件在首次启动时自动创建。真实 `.env`、运行数据、日志、编辑器配置、Python 缓存和虚拟环境均排除在版本控制之外。

## 回归验证

```powershell
python -B -m pytest tests -q --disable-warnings
```

2026-10-06 的原始回归记录为 35 项测试全部通过。当前回归使用 Fixture/Mock，数据与日志写入临时目录；生产装配测试提供占位密钥，不需要真实密钥。最新结果见本次 Runtime 质量验证记录。

2026-10-06 还验证了真实启动、两轮告警查询及一次使用合成告警的真实模型调查。详细验证记录及修复说明见 [REGRESSION_REPORT.md](REGRESSION_REPORT.md)。真实服务当时没有 firing 告警，生命周期变化由可控响应覆盖。

本次修复了浮点阈值边界、未完成模型响应误记成功、非法告警快照误触发恢复、调查异常丢失已收集证据，以及 NaN/Infinity 被保存为有效指标的问题。

## 已知限制

IncidentStore 按 Incident ID 保留历史，fingerprint 指向当前活跃 Incident。SQLite Runtime、Incident JSON 和 Remediation JSON 分别承担调查状态、告警历史和修复审批职责；跨存储通过幂等投影对账，不具有共同事务。

默认每 30 秒轮询，重新调查冷却为 5 分钟，http_5xx_rate 绝对增加至少 0.2 时可提前重新调查。Agent 最多调查 7 轮、执行工具 13 次，每轮提示剩余预算。

仓库中的依赖清单用于记录直接依赖，版本尚未全部锁定。

## 代码级因果证据工具

调查仍沿 Agent → Tool → Client → 只读 HTTP。新增：

- `get_running_version(service)`：从 `SERVICE_URLS[service] + /version` 获取实例自报
  SHA、UTC 进程启动时间及允许公开的 `simulate_code_regression`。未配置、404、
  unknown SHA 或异常结构明确返回 `no_data` 和 reason，不猜测版本。
- `get_commit_diff(service, commit_sha)`：只通过 `CODE_REPOSITORIES` 选择 GitHub 仓库。
  最多 15 个文件，每个 patch 6000 字符，所有 patch 合计 24000 字符；分页、缺失
  patch、大小截断均显式标记。缺失 patch 不表示没有代码变化。
- `get_source_file(service, path, ref)`：同一仓库白名单内读取指定 ref 的 UTF-8 文本，
  最多 20000 字符；拒绝绝对路径、目录穿越、目录、symlink 和二进制/不支持编码。
  >1MB 文件通过受限 raw 前缀读取，最多 80004 字节，再限制字符数并标记 truncated。
  查当前逻辑时 ref 必须使用运行完整 SHA。超大文件前缀之外内容未验证。

GitHub 使用 requests 和环境变量 `GITHUB_TOKEN`，无需新 SDK；fine-grained Token
只需目标仓库 Contents 读取权限，无需 All repositories。所有 Agent 工具只读，
不提交修复、不推送、不改变环境或重启服务。
API 依据：[commit](https://docs.github.com/en/rest/commits/commits#get-a-commit)、
[contents](https://docs.github.com/en/rest/repos/contents#get-repository-content)。

`query_logs` 保留原始 timestamp、line、labels，并从 Loki 纳秒时间戳精确生成
`timestamp_utc`，包含 UTC Z 和需要时的 9 位小数；不要使用 line 内的无时区时间
计算与 commit 的精确间隔。近期提交日期使用 committer.date，区分 author 日期。

调查顺序：日志和依赖证据支持应用层异常时，先查近期提交，再查运行版本，然后
查疑似引入逻辑的 diff；仅在缺 patch、截断、需上下文或需确认当前运行逻辑时读源码。
Prompt 区分提交存在、实例版本自报、进程启动、代码修改、源码存在和路径实际执行。
代码引入条件与运行时触发条件必须分开；13 次工具预算、7 轮和每轮剩余次数提示保留。

回归测试：`& 'D:\conda\envs\agentlearn\python.exe' -B -m unittest discover -s tests -v`。

## 多信号告警升级

- [架构、规则与扩展指南](docs/MULTI_SIGNAL_ALERTS_GUIDE_2026-10-08_18-17-52.md)
- [单元、规则及真实集成验证](docs/MULTI_SIGNAL_ALERTS_VALIDATION_2026-10-08_18-24-41.md)

## Durable Runtime 与人类介入

- [持久化调查、恢复、预算及 CLI 使用指南](docs/DURABLE_AGENT_RUNTIME_GUIDE.md)
- [单元、跨进程及真实模型验收记录](docs/DURABLE_AGENT_RUNTIME_VALIDATION.md)
