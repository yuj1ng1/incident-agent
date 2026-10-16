# P5.3c.2 真实模型只读联调预检

## 结论与范围

基线为 `8f82c10a4d62ccaa20b71092658c26d1d35f6433`。本轮只准备入口和离线验证，真实模型 API 请求为 **0 次**，模型费用为 **0 元**。付费模式必须等待用户后续明确批准，本轮没有运行带付费开关的命令。

新增 `scripts/preflight_real_model.py` 和对应测试；复用真实 Agent、ModelClient、Worker、InvestigationManager、RuntimeStore、IncidentStore 及确定性指标分析器，不修改生产接口、依赖、配置、预算、状态机或审批边界。入口依赖项目测试环境及已跟踪的 `tests/runtime_support.py`、`tests/test_metric_analysis.py`；应使用测试锁文件对应环境。

当前工作区已有 README、提示词、临时测试及文档整理改动，均保留且不纳入本轮提交。入口读取当前提示词，产物记录 `prompt_sha256`；本轮离线预检值为 `94faddc0b0029a11be7f815d571f3fad47e0d2cad6305be0aa899752f8c7350a`。该提示词包含用户尚未提交的修改，因此其他检出环境的请求内容可能不同；应对照每次产物中的哈希和 `request.json`，不要将本报告当作所有环境的同一请求快照。

## 已核实的调用条件

- 解释器：`D:\conda\envs\agentlearn\python.exe`，Python 3.12.14。默认 `D:\conda\python.exe` 缺少 `httpx2`，不用于本次入口。
- 实际安装且符合锁文件：OpenAI SDK 3.14.0、httpx2 2.13.0、Pydantic 2.13.5、python-dotenv 1.2.3；`pip check` 通过。
- 拟用模型：`deepseek-flash`；固定地址 `https://api.deepseek.com`，请求路径 `/responses`。不读取模型指定的地址，不从外部参数覆盖模型。
- `.env` 中 `DEEPSEEK_API_KEY` 已配置；检查只使用存在性和来源，不输出内容。尚未验证密钥有效性、账户余额、权限或实际服务端响应。
- 生产调用链为 `Worker.run_once()` → `Agent.request_durable_response()` → `ModelClient.call_once()` → SDK `responses.create()`。ModelClient 已关闭 SDK 重试，但生产接口未设输出 token 上限；本入口在专用 Provider 边界增加上限，不改生产 ModelClient。
- DeepSeek Responses API 是无状态接口，不能用 `previous_response_id` 推断服务端保留历史；`max_tool_calls`、`parallel_tool_calls` 不提供本地预算保证。本次仅一次判断，不声称已验证多轮工具链兼容性。参见 [DeepSeek Responses 兼容性文档](https://api-docs.deepseek.com/zh-cn/guides/responses_api/) 与 [OpenAI Python SDK 文档](https://developers.openai.com/api/reference/python/)。
- 请求显式设置 `reasoning.effort="none"`、`tool_choice="none"`、`tools=[]`、`max_output_tokens=2048`。这些参数有 [DeepSeek 官方接口说明](https://api-docs.deepseek.com/api/create-response/) 支持，并通过本机 SDK 的 MockTransport 序列化验证；尚未通过真实模型请求验证服务端执行效果。

## 首个判断任务与比较标准

首案是 P5.3c.1 A 不完整快照与 B 独立错位指标的复合固定案例，原始结果复用 [历史夹具](../tests/test_metric_analysis.py) 和 [Mock E2E 案例](../tests/test_incident_mock_e2e.py)。A 与 B 中重复数值不是新增独立佐证。四条原始记录以本地历史输入导入临时 Checkpoint，另存来源事件；不伪造模型工具调用，也不把导入计成本轮工具执行。

| 分组与引用 | 保存的历史输入 | 人工复核标准 |
| --- | --- | --- |
| A / `case-a-t1` | 2026-10-11T10:48:55Z：请求速率 0.41821223361699034、5xx 1.0、scrape 1.0 | 引用该快照自己的评估时间与观测，不宣称同一抓取时刻或事务原子性 |
| A / `case-a-t2` | 晚 300ms：请求速率 0.0、5xx no_data、scrape 1.0 | 0.0 是有效零；5xx 缺失不能补零，不能据此证明没有 5xx |
| B / `case-b-errors-t1` | 独立历史 5xx=1.0，时间为 T1 | 与 B 的 T2 请求速率不是同刻观测 |
| B / `case-b-rate-t2` | 独立历史请求速率=0.0，时间为 T2 | 不得把错位数据当作同刻数值矛盾或确定根因 |

A 是两个组内对齐的快照，不能仅因跨快照时间不同就把 A 误报为 `time_not_aligned`；B 才是独立查询的时间错位案例。`query_metrics` 的历史输出来自固定注入，不表示真实工具具有历史 `at` 参数。

模型应引用 observation ID，区分已确认历史观测、时间限制、缺失与未经验证的解释；不得确认计数器重置、数据库故障或代码回归，不得用历史观测证明当前健康、恢复或业务成功。工具结果中的历史 `request_executed` 也是保存的夹具字段，不代表本轮访问过 Prometheus。

期望结论单独保存在 `case.json.expected`，不放入模型请求。模型会看到真实 Runtime 派生分析及原始历史观测，因此本次衡量的是“给定证据及派生分析的一次解释能力”，不是独立计算或自主取证能力。不能把 Session `COMPLETED`、关键词命中或离线占位回答当作模型理解通过；需要人工对照原始输出逐项评审。

## 隔离与预算

| 控制项 | 本入口设置 |
| --- | --- |
| 存储 | 每次新建 OS 临时目录 `incident-p53c2-*`，SQLite、Incident JSON、日志和产物均为测试文件；拒绝生产项目目录及非空目录，不允许续用历史测试 Session |
| 启动 | 父进程只启动自有子进程；不导入或启动生产 main、Monitor、Scheduler、RecoveryWorker |
| 修复 | Manager 的 `remediation_planner=None`；不实例化真实修复执行器、不生成审批或执行动作 |
| 工具 | 最多 1 次：Worker 自带的 `get_running_version`；只接受精确服务参数，由本地只读夹具返回 no_data，不查询服务、不伪造当前版本；未知工具、目标及异参均拒绝，无生产 dispatcher fallback |
| 模型 | 最多 1 次 SDK 请求入口、1 个 attempt、0 次 resume、0 个人工请求；单次 Worker，不循环调度或自动重试 |
| 请求内容 | 只允许固定模型、input、instructions 和空 tools；本地增加输出/思考/工具选择参数；请求正文 UTF-8 序列化最多 98,304 字节 |
| 输出 | 最多 2,048 token，包含接口计入输出的 reasoning token；当前请求关闭思考模式 |
| 时间 | SDK 超时 30 秒；Runtime 时长 90 秒；CLI 父进程 90 秒墙钟上限，必要时只终止自有测试子进程 |
| 网络 | 默认不构造 SDK，Worker 期间禁用 socket 连接；获批付费分支使用固定 HTTPS 地址，禁用环境代理继承及重定向 |
| 日志 | 新子进程在业务 import 前设置临时日志路径；直接调用函数时，若已有 logger 指向 OS 临时目录之外则拒绝，避免继续写真实日志 |

SDK timeout 是分阶段网络等待限制，不是绝对墙钟限制；父进程用于提供总上限。本地超时或 Ctrl+C 不能撤销服务端已经接收的请求，可能仍计费。每次命令只允许一次请求，但用户手动重复启动会新建一次运行并再次获得一次请求额度。

停止条件：HTTP 401/402/403 后临时 Session 暂停；429、5xx、超时、不完整响应、空回答、非法模型工具输出或预算耗尽后本次运行停止。不会自动修复、切换模型、重试、继续或要求用户回答来解锁额度。输入超限在 SDK create 前拒绝。非完成状态或监督进程终止返回非零退出码。

本案不使用真实 Prometheus，因此本轮没有请求连通性、保留时长、历史查询或故障注入。若后续改用真实 Prometheus，应先另做只读连通性和目标历史区间检查，不能将本入口的夹具结果当作实际保留数据证据。

## 输出保留与费用

临时目录保留 `case.json`、`request.json`、`request-attempt.json`、`response.sdk.json`、`checkpoint.json`、`events.json`、`summary.json`、SQLite 和 Incident JSON。付费响应额外保留 `response.raw.json`，先保存原始响应正文，再解析 SDK Response，保存完整 usage 字段与证据引用；不使用会把 token 字段统一脱敏的 Checkpoint 投影替代原始响应。

不保存 Authorization 等请求头。异常只保存类别与 HTTP 状态，不记录原始错误详情；启动失败另存 `startup-error.json`，只允许已知本地错误码，其他异常仅记通用原因。若服务端意外回显配置密钥，专用产物和进入 Runtime 的响应都会移除该密钥值。计数 `real_model_api_requests` 表示本地进入 SDK create 的次数，不证明服务端收到或扣费。超时仍保留已写的请求意图及监督停止原因。临时产物不加入 Git，也未上传；OS 临时目录可能被系统清理，后续真实联调结束后应及时人工保留需要的原始产物。

按本轮读取的 [DeepSeek 人民币价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)，Flash 高峰未命中缓存输入 2 元/百万 token、输出 8 元/百万 token；以高峰且无缓存估算：`费用 = 输入 token × 2/1,000,000 + 输出 token × 8/1,000,000` 元。

若按 1 万至 10 万输入 token、输出用满 2,048 token 的预算假设，单次约 **0.04～0.22 元**。本轮草稿实际正文约 49.5 KB；字节上限不是 DeepSeek tokenizer 的精确计数，也不是账户人民币硬限额。真实账单仍以批准执行时资费及返回 usage 为准；执行前应重新核对模型别名和价格。本轮实际模型费用为 0 元。

## 启动与验证

本轮已执行的无付费入口：

```powershell
Set-Location -LiteralPath 'D:\Incident Agent'
& 'D:\conda\envs\agentlearn\python.exe' -B scripts/preflight_real_model.py
```

待用户另行明确批准后才可执行以下命令；双开关只防止误启动，不能替代人的授权：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B scripts/preflight_real_model.py --run-live --approve-paid-model-run
```

已完成验证：

- 默认 CLI 使用独立子进程，记录 `real_model_api_requests=0`、Runtime 模型预留 1、工具执行 1、离线 Session COMPLETED；回答明确标为离线传输占位，不是推理结论。
- P5.3c.1、指标分析与 Worker 相关回归：253 passed。
- 新入口离线回归：38 passed in 16.83s；包含默认禁网、SDK 序列化、原始证据引用、零与缺失、全部文件密钥回显清理、401/402/403/429/500、超时/空回答/不完整响应/非法工具、输入与重复请求拒绝、目录/日志隔离、启动错误及自有子进程超时停止。
- 已安装 OpenAI SDK 与 httpx2 MockTransport 的序列化验证通过；不向真实网络发送请求。
- `pip check` 通过。
- 相关测试合计 291 项通过；未运行全量测试套件，不以本次结果代替真实模型或真实外部服务验证。
- 结束前对比任务开始时保存的 322 个受保护路径哈希，原有修改及生产运行数据内容均未变化；新增文件未检出配置密钥的实际值。

尚未验证：付费模型解释质量、真实鉴权/余额/限流、实际 Provider 超时及输出上限执行、真实 Prometheus 数据、模型自主工具选择、多轮 Provider Responses 链，以及生产服务恢复。本轮没有开启故障开关、启动服务或修改生产运行数据。
