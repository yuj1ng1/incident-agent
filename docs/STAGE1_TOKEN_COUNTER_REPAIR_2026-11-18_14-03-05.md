# 第一阶段模型输入 Token 计数补齐与离线验收报告

本次在 `D:\Incident Agent` 补齐了面向 DeepSeek Responses API（`deepseek-flash` → `DeepSeek-V4.1-Flash-1017`）的发送前输入 token 计数能力，接通 `main.py`、SDK 包装器及新 Git 外隔离入口，并完成了匹配当前风险的离线验证与交叉核对。**本次没有发起任何真实模型 API 请求，没有读取 `.env` 真实凭据进行探测，没有恢复旧实验 Session，也没有进入第二阶段，未执行 `git commit` 或 `git push`。**

## 一、基线核对与历史证据保护

- 仓库目录为 `D:\Incident Agent`，当前分支 `main`，起始及结束 HEAD 均为 `8e2bc425d304181e9cb95609db58b53e50820a8b`，暂存区为空。
- 本轮起始差异与基线记录保存在 `D:\codex use\2026-11-18\stage1-token-counter-repair\baseline\`（`head.txt`、`git-status.txt`、`starting-diff.patch`）。
- 原有修改与只读历史证据保持未改动：
  - `prompts/agent_prompt.py` SHA-256：`85484D80C42CE0C3EFB3FBF74B4766ED165336775A8F1908893D42CBFC9B62A2`
  - `temp_test.py` SHA-256：`71A2E3F7FC77B6988790B6BF158E75E071E6B7639D65644EAAB1C4E644B1FEF3`
  - `D:\codex use\2026-11-15\stage1-live-20261115-154614\private\live-ledger.sqlite3` SHA-256：`BF5180049F6411427E59A3850F83D40330DC75F13A2B5B4861AEED79A637A2F4`（账本保持 `CLOSED`，`STOP_MODEL` 与 `WATCHDOG_DISARM` 均存在，38 项历史审计警告原样保留）
  - `D:\codex use\2026-11-16\stage1-offline-repair\harness\live_worker_repaired.py` SHA-256：`0C326A4473BAA291BFF0C995EF10AC3C21A59BB1C20F72122DB835822FB82974`

## 二、官方计数方案核实

- 官方文档与源码核查（下载保存于 `D:\codex use\2026-11-18\stage1-token-counter-repair\official\`）：
  - `deepseek-recipe` 仓库（HEAD `935817c66f5d6e64838f419692545f06787e485f`，MIT 许可）：实现 OpenAI Responses API 请求到内部 `Conversation` 与 `DeepseekV41Encoding` 模板渲染（`BOS`、`EOS`、`System`/`User`/`Assistant`/`<think>`/`</think>`、`｜DSML｜` 工具定义与调用参数、`<tool_result>`、reasoning effort 提示、消息合并规则及普通文本片段隔离编码）。
  - 模型与分词器文件：`deepseek-flash` 对应 `deepseek-ai/DeepSeek-V4.1-Flash-1017`（revision `08c8657f02291e11b6f6b63e0e16014f8e748518`）。其 `tokenizer.json`（SHA-256 `c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b`）与 `deepseek-recipe/static/tokenizers/v41/tokenizer.json` 仅在多模态图像占位 token（ID `129264`：`&lt;｜deepseek_image｜&gt;` vs `&lt;｜image｜&gt;`）存在命名差异；本项目纯文本场景拒绝图像/文件块，二者普通文本与协议特殊 token 词表一致。
  - **兼容性核实**：PyPI `deepseek-recipe==0.2.0` 仅提供 macOS 与 `manylinux_2_28` 预编译 wheel，未发布 Windows wheel（Windows 源码编译需 Rust + VS Build Tools + LLVM/Clang + CMake + OpenCV 4.12）。相比之下，`tokenizers==0.23.3` 与 `deepseek-recipe` 上游底层版本匹配且提供 Windows/Linux 预编译 wheel（约 2.9–3.5 MB）。因此采用 `tokenizers==0.23.3` 搭配本地校验哈希的 V4.1 `tokenizer.json` 在 Python 中逐片段精确实现 `deepseek-recipe` 的编码流程。
  - 对已有真实保存样本 `D:\codex use\2026-11-15\stage1-live-20261115-154614\private\models\request-01.json` 的离线测试返回 **10,117 tokens**，与保存响应 `response-01.body` 的 `usage.input_tokens == 10117` 一致（生产代码中未硬编码该数字）。

## 三、实施改动清单

### 1. 仓库内改动

- [token_counter.py](../model_client/token_counter.py)（新增）与 [tokenizer.json](../model_client/tokenizers/v41/tokenizer.json)、[LICENSE](../model_client/tokenizers/v41/LICENSE)：
  - 实现 `DeepseekV41TokenCounter`、`build_deepseek_input_token_counter`、`load_v41_tokenizer` 与 `TokenCountError`。
  - 启动/调用前离线加载本地 `tokenizer.json`，拒绝符号链接、缺失文件、损坏 JSON、特殊 token 缺失及 SHA-256 哈希不匹配。
  - 完整编码 `instructions`、`input`（`message`、`reasoning`、`function_call`、`function_call_output`）、`tools` JSON schema、`max_output_tokens` 正整数校验、`reasoning`、`tool_choice` 及协议特殊标记；剥离纯文本词表 `added_tokens` 以防止输入文本中的字面特殊标记被误编码为协议控制 token。
  - 明确拒绝图像（`input_image`）、文件（`input_file`）、原生付费工具（非 `function` 工具）、`strict` 工具 schema、未配对 `function_call`、未知顶级字段以及非 `deepseek-flash` 模型名（包括 HF 仓库 ID）。
  - 暴露 `encoder_metadata()` 记录 `encoder_version`、`upstream_encoding_source`、`upstream_model_revision`、`tokenizer_sha256` 与 `model_versions`。
- [model_client.py](../model_client/model_client.py)：
  - 抽取共用辅助函数 `enforce_input_token_limit(...)` 并由 `ModelClient.call_once` 复用，保留输入上限 `65536` 与输出上限 `8192`。
- [main.py](../main.py)：
  - `mode == "deepseek"` 装配 `build_deepseek_input_token_counter()`；`mode == "demo_replay"` 保持专用 `(lambda body: 0)` 回调，仅用于不产生网络与模型请求的回放模式。
- SDK 包装器与调用方最终请求边界校验：
  - [preflight_real_model.py](../scripts/preflight_real_model.py)：`live=True` 时装配 `build_deepseek_input_token_counter()`；`BoundedProvider.create` 在追加 `load_snapshot` 上下文及更新 `reasoning`/`tool_choice`/`max_output_tokens` 后的最终 `body` 上执行 `enforce_input_token_limit(...)`。
  - [p63_model_validation.py](../scripts/p63_model_validation.py)：`live=True` 时为 `_SingleRequest` 与 `ModelClient` 装配真实计数器；`_SingleRequest.create` 对实际发送的最终 `self.body`（含 `reasoning` 与 `tool_choice`）执行 `enforce_input_token_limit(...)`。
  - [p65_model_boundary.py](../scripts/p65_model_boundary.py) 与 [p65_readonly_investigation.py](../scripts/p65_readonly_investigation.py)：`BoundedResponses.create` 对重建 `input`、`instructions`、`tools` 与 `tool_choice` 后的最终 `body` 执行 `enforce_input_token_limit(...)`；`live=True` 装配真实计数器；`CODE_FILES` 纳入 `model_client/token_counter.py`。
- 依赖与打包配置：[.gitattributes](../.gitattributes)（保证 `model_client/tokenizers/**/*.json` 以 LF 保留字节与 SHA-256）、[.dockerignore](../.dockerignore)（放行 `!model_client/tokenizers/**`）、[requirements.txt](../requirements.txt) 与 [requirements.lock.txt](../requirements.lock.txt)（固定 `tokenizers==0.23.3` 及其子依赖，无本地安装全套推理/GPU 环境）。
- 新增离线测试：[test_token_counter.py](../tests/test_token_counter.py)。

### 2. Git 外隔离入口与测试

位于 `D:\codex use\2026-11-18\stage1-token-counter-repair\harness\`：

- `audited_transport.py`：保留原传输修复（响应 `read()` 一次解码并记录后返回同一个 `Response` 对象及流生命周期，异常时关闭并标记失败，不再用已解码正文重建带原压缩头的响应）。
- `live_gate.py`：扩展至在请求边界记录与校验完整 `input_upper_bound`，将 `encoder` 元数据冻结入 `parameters_sha256` 校验范围。
- `live_worker_token_verified.py`：将原入口启动即 `SystemExit` 的临时阻断改为显式可测试的门禁检查 `check_send_gate(...)`。默认关闭真实发送；缺少 `STOP_MODEL` 通过检查、显式 `INCIDENT_AGENT_LIVE_SEND_ENABLED="1"`（或 `live_send_enabled=True`）、项目与授权人信息及显式费用策略（`fee_cap_enabled` 为 `False` 时须有 `fee_cap_disabled_authorized=True`）时，在读取凭据和构造 `OpenAI` 客户端前停止。
- `test_token_verified_harness.py`：验证默认关闭、授权缺失、`STOP_MODEL` 阻断、测试开启流程（占位密钥 + `MockTransport` + `ia_test_*` MySQL）及 8 类失败路径。

## 四、离线验证与实际结果

测试环境使用基于 Python 3.12.14 建设的隔离虚拟环境 `D:\codex use\2026-11-18\stage1-token-counter-repair\venv`、`mysql:8.4.13` 临时容器（`127.0.0.1:23306`，仅创建与销毁 `ia_test_*` schema）、内存 `httpx2.MockTransport` 及 `allow_isolated_mysql_sockets` 网络守卫；未加载 `.env` 真实凭据，未访问模型服务。

| 验证项 | 命令与覆盖内容 | 实际结果 |
| --- | --- | --- |
| 官方 wheel 逐 token ID 对照 | 在 `python:3.12.14-slim-bookworm` 中用 `deepseek-recipe==0.2.0` 与本地 `DeepseekV41TokenCounter` 对比 8 组多语言、代码、特殊 token、工具定义、thinking/none、多轮工具与后续 user 轮次 | **8/8 逐 token ID 序列完全一致**（退出 0） |
| 入口路径与离线回放 | `pytest tests/test_entry_paths.py tests/test_demo_replay.py -v` | **5 passed**（退出 0） |
| 阶段一模型交付与预检回归 | `pytest tests/test_stage1_model_delivery.py tests/test_real_model_preflight.py -v` | **64 passed**（222.37s，退出 0） |
| 本轮新增 Token Counter 与 Git 外隔离入口 | `pytest tests/test_token_counter.py "D:\codex use\2026-11-18\stage1-token-counter-repair\harness\test_token_verified_harness.py" -v` | **19 passed**（143.62s，退出 0；含中英/Unicode/长日志/文本内特殊标记、工具与历史增长、65536 超限 0 HTTP 调用、损坏/缺失/哈希不一致/图像/文件/原生付费工具阻断、`main.py` 真实/回放分支隔离、三大包装器最终请求体检查、三轮 `Agent→ToolManager→Worker→MySQL→Checkpoint` 离线交付及 gzip/非 gzip 响应、真实 `request-01.json` 与 Round-2 对照、默认关闭门禁及 HTTP 401/429/500/超时/连接/损坏 JSON/损坏 gzip/不完整响应单次尝试后闭锁） |
| P6.3/P6.4/P6.5 动态预算与准入 | `pytest tests/test_p63_model_validation.py tests/test_p64_offline_investigation.py tests/test_p65_dynamic_budget.py tests/test_p65_third_preparation.py -v` | **113 passed**（218.90s，退出 0） |
| P6.5 只读调查与协议兼容 | `pytest tests/test_p65_readonly_investigation.py tests/test_p65_protocol_compatibility.py -v` | **102 passed, 1 failed**（121.95s；失败项为既有 Mock 用例 `test_offline_resume_uses_saved_records_without_fabricating_outputs`，其 [第 383 行](../tests/test_p65_readonly_investigation.py#L383) `Agent(adapter, TASK)` 未传入离线零回调，触发 `RequestConstraintError`，本轮未改写该历史测试） |
| 事件回归与 Runtime 核心 | `pytest tests/test_incident_regression.py tests/test_runtime.py -v` | **136 passed, 53 subtests passed**（991.00s，退出 0） |
| AST 与静态检查 | 对 12 个新增/实质修改的 `.py` 文件执行 `ast.parse`，以及 `git diff --check` | 12 个文件 AST 通过；本轮修改无空白错误（`git diff --check` 仅报原有未触碰的 `temp_test.py:7`） |

## 五、已确认范围、未确认服务端差异与后续前提

1. **已证明部分（官方材料与离线本地一致）**：
   - 本地 `DeepseekV41TokenCounter` 的消息转换、特殊 token 隔离、工具与推理前缀编码在 8 组覆盖场景中与 `deepseek-recipe==0.2.0` 官方 Python wheel 的 token ID 序列完全一致，且在 `request-01.json` 样本上与保存的服务端 `usage.input_tokens == 10117` 一致。
   - 生产 `main.py` 真实模式、三个已有包装器及 Git 外隔离入口均已接入可验证的输入计数与 65,536/8,192 上限校验，在计数后未发生未经复查的输入扩张。
2. **尚未确认的服务端差异与证据缺口**：
   - **单一真实样本不构成多轮与全参数服务端等价证明**：当前仅有 `request-01.json` 1 条真实服务端返回 `usage` 对照；多轮 `function_call` / `function_call_output` / 推理内容连续交互、`reasoning.effort` 分档（`low`/`medium`/`max`）以及工具列表动态变化时，真实 DeepSeek API 服务端计数与开源 `deepseek-recipe@0.2.0` 是否在任何细节或后续服务端升级后出现偏差，仍属**未验证**。
   - 隔离门禁与生产测试本轮仅覆盖离线 `MockTransport` 与合成故障/工具响应，未做新一轮真实模型联调、真实网络超时恢复、真实账单对账及第二阶段 Remediation 验证。
   - `frontend`、完整全量后端测试套件及容器内实际 `live_worker_token_verified.py` 启动尚未在本次执行。
3. **收尾状态**：
   - 临时容器 `ia-token-repair-mysql-20261118` 已移除；`docker ps` 确认仅原有 `networklab-exp4-mysql`、`postgres-exporter`、`order-db` 保持运行。
   - 后续只有在获得新的明确真实发送授权后，方可先做受控单轮/多轮真实样本与服务端 `usage.input_tokens` 核对（如出现偏差应先停发定位），再决定是否开展更长链路的真实联调。
