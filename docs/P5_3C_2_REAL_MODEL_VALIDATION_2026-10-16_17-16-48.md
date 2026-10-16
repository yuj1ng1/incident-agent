# P5.3c.2 获批后的首次真实模型只读联调

## 实际结论

用户在预检交付后明确回复“批准”。随后按原定预算执行 **1 次**真实 `deepseek-flash` Responses 请求，没有重试或追加请求。本次接口返回了真实模型内容，但响应因输出上限截断，**未完成验收，不能判为真实模型联调通过**。

另有一处明确的证据推理错误：告警评估时刻未知，模型却断言它与历史快照处于不同评估时点。部分文字正确解释了历史指标限制，不能抵消该问题，也不能把截断文本当作完成结论。

原始开发基线为 `8f82c10a4d62ccaa20b71092658c26d1d35f6433`；本次实际运行预检入口所在提交为 `f22535050706c24243632fb7c354356c085d1a8c`。沿用当前工作区提示词，SHA-256 为 `94faddc0b0029a11be7f815d571f3fad47e0d2cad6305be0aa899752f8c7350a`，与预检一致。该提示词包含用户已有未提交内容，本轮未改动或提交它。

## 调用与使用量

实际运行命令：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B 'D:\Incident Agent\scripts\preflight_real_model.py' --run-live --approve-paid-model-run
```

| 项目 | 实际结果 |
| --- | --- |
| 模型 / 端点 | `deepseek-flash` / `https://api.deepseek.com/responses` |
| Response ID | `d1fb040c-9877-4fce-abcc-bdf6032ca24e` |
| 服务端响应创建时间 | 2026-10-16 17:08:20 +08:00 |
| 本地结果记录时间 | 2026-10-16 17:08:32.185369 +08:00 |
| Session | `ses-5cfd30e65cbc4850947b650e8a7eaf9a` |
| 实际 SDK create 入口次数 | 1；收到真实模型响应；没有第二次调用 |
| 请求配置 | `max_output_tokens=2048`、`reasoning.effort=none`、`tool_choice=none`、`tools=[]` |
| SDK 重试 / 超时 / 总时限 | 0 / 30 秒 / 90 秒；未触发总时限 |
| 响应状态 | `incomplete`，`incomplete_details.reason=max_output_tokens` |
| 原始 usage 输入 / 缓存命中 | 11,782 / 0 token |
| 原始 usage 输出 / reasoning | 2,049 / 0 token |
| 原始 usage total | 13,831 token |
| Worker 结果 | `EXHAUSTED`，错误类别 `model_incomplete`，命令非零退出 |

**上限差异是实测结果：**请求确实设置 2,048，但服务端 usage 返回 2,049。该差异需要保留，不能报告为“严格生成不超过 2,048 token”，也不能自行解释成 SDK Bug、应用提高预算或重试；原因尚未验证。

按本轮重新读取的 [DeepSeek 人民币价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)，以高峰、无缓存输入 2 元/百万 token、输出 8 元/百万 token 估算：

`11782 × 2 / 1000000 + 2049 × 8 / 1000000 = 0.039956 元`，约 **0.04 元**。

这是基于响应 usage 与官方标价计算的费用估算，未查询账户账单或余额，不等同于已核实实际扣款。部分响应也可能计费；本轮没有因未完成而重新请求。

## 固定历史案例的人工复核

首案沿用预检的 P5.3c.1 A+B 复合固定历史输入，没有真实 Prometheus 查询。模型获得了原始观测、Runtime 派生分析和系统规则；期望答案 `case.json.expected` 未进入请求。以下是对已返回部分文字的逐项检查，由主 Agent 与独立审查 Agent 分别复核。

| 标准 | 已返回文字的表现 | 判定 |
| --- | --- | --- |
| A 组数值与时刻 | 正确引用 `case-a-t1` 的 0.41821223361699034 / 1.0 / 1.0，以及 `case-a-t2` 晚 300ms 的 0.0 / no_data / 1.0 | 部分输出已体现 |
| A 组边界 | 没有把两个快照拼为同一瞬时状态，识别 T2 的 5xx 缺失 | 部分输出已体现 |
| B 组错位 | 引用 `case-b-errors-t1` 与 `case-b-rate-t2`，拒绝把不同评估时刻拼成“100% 错误率同时零流量” | 部分输出已体现 |
| 有效零与缺失 | 说明 `0.0` 有效，`no_data` 不是零，也不是健康 | 部分输出已体现 |
| 评估时间与抓取 | 说明相同评估时刻不保证抓取同刻，多个 HTTP 查询不具有原子性 | 部分输出已体现；这些提示也已由 Runtime 提供 |
| 历史与当前 | 没有宣称当前健康或恢复，明确历史数据不能证明业务恢复 | 部分输出已体现 |
| 根因边界 | 没有把应用回归、数据库不可达等升级为确定根因；未单独讨论计数器重置 | 部分输出已体现，覆盖并不完整 |
| 引用可追溯 | 四个 `case-*` 和本轮 bootstrap 的 `obs-fb83955e6e8e49ad9bbcd624d721876c` 均存在于原始输入/Checkpoint | 引用检查通过 |
| 未知时间的处理 | 告警时间字段为空，却断言与快照处于不同评估时点 | 不通过 |
| 完整回答 | 结尾停在“以下仅为方向性说明，不”，响应与消息均为 incomplete | 未完成 |

明确错误位于原始回答第五节：

> `value=0.3`、`threshold=0.2` 与历史快照中的 `5xx=1.0` 处于不同评估时点

请求中的 synthetic legacy Alert 的 `starts_at`、`observed_at`、`window` 均为 `null`。数值不同不能证明评估时间不同。正确结论只能是：**告警评估时刻未知，无法确认与快照是否同刻，因此不能直接相互印证或否定。**

另外两处表述应收紧：模型将 A/B 重复数字称为同一证据在不同查询构造下的复现，强于本固定案例能够独立认证的真实采样来源关系；应仅说本案例不计作独立佐证。模型把 `up=1` 概括为评估时刻目标在线，更严谨的表述应限于返回了抓取成功样本，不能升级为精确瞬时在线或业务健康认证。

本次只能观察模型在给定原始证据及 Runtime 已派生分类下的部分解释表现，不能声称模型独立发现了时间分类、完整理解指标分析，或已验证一般根因推理能力。

## 运行时边界与原始产物

真实 Worker/SQLite 的只读重读确认：模型预留 1、工具执行 1、attempt 1、resume 0、human request 0。唯一工具仍为本地版本夹具，返回 `no_data`；没有访问真实服务。事件中 `model_reserved=1`、`tool_reserved=1`、`waiting_retry=0`、`model_response_saved=0`、`remediation_proposed=0`。

Checkpoint 结果为 `success=false`、`status=model_incomplete`、`answer=调查停止：model_incomplete`，累计统计为 1/1。截断回答没有被提升为成功结论；未返回完整响应时，原始文字由专用 Provider 产物保留，而非伪造为 Worker 已完成结果。

本轮原始临时目录是 `C:\Users\zp1222\AppData\Local\Temp\incident-p53c2-n41oz3xk`。为避免系统临时清理，已完整复制 12 个原始文件到独立新目录 [本地原始产物](../data/model-validation/p53c2-n41oz3xk/summary.json)，路径为 `D:\Incident Agent\data\model-validation\p53c2-n41oz3xk`；复制前后逐文件 SHA-256 一致，另存 `artifact-manifest.json`。

- [原始响应正文](../data/model-validation/p53c2-n41oz3xk/response.raw.json)：SHA-256 `7ff9426e4f35a4a8c11000ecc978919a66c96cfcfec9ce71fab1535c82df95a8`。
- [SDK Response](../data/model-validation/p53c2-n41oz3xk/response.sdk.json)：SHA-256 `edaa0e7520f744b27bd1e9ce5dd734c8a89a13d44dd0fcb649ded5c53ac5b3d2`。
- [实际请求正文](../data/model-validation/p53c2-n41oz3xk/request.json)：SHA-256 `b6e9fedbaed37a662d49b6c7365f2085f53e756b6cfa9f45b0b5414747c99322`。

SDK 增加了一些默认字段，包括 usage 中的 `cache_write_tokens=null`；因此原始 JSON 与 SDK dump 不逐对象相等。原始文本、状态、截断原因及实际 token 计数均保留，原始响应仍以 `response.raw.json` 为准。原始产物受现有 `/data/` Git 忽略规则保护，未加入提交或推送；报告中的本地产物链接在其他检出环境不会自动拥有对应文件。

密钥只从原有配置读取；本轮产物按实际配置密钥值扫描无命中，没有保存请求 Authorization。生产 main、Monitor、Scheduler、RecoveryWorker 和真实修复执行器均未启动，没有故障注入或生产服务变更。对任务开始时 322 个受保护路径逐文件比较，用户原有修改与已有运行数据内容均未变化；产物归档为本轮新建目录。

## 验证范围与下一步

本轮实际执行了 1 次真实模型请求、原始响应/请求/usage 人工复核、独立内容审查、临时 SQLite 的新进程只读重读、事件计数检查、原始文件归档哈希校验及密钥/原有文件保护检查。没有修改代码、配置或测试，因此没有重新运行前轮已通过的 38 项预检与 253 项相关单元/Mock 回归；不得把它们写成本轮重新执行的结果。

尚未验证：完整真实模型结论、第二次请求、模型自主取证、多轮 Responses 工具链、真实 Prometheus 历史数据、生产服务健康/恢复及账户实际扣款。

建议后续先将独立首案输出约束为短表及简短结论，明确“时间未知不等于时间不同”，保留原来的请求次数和安全边界，再离线验证入口。现有冗长提示词可能影响回答长度，这是待验证的解释；本次只能确认截断原因是返回的 `max_output_tokens`。本轮未进行这些修改，也未增加 token 预算。**第二次付费请求须另行明确批准。**
