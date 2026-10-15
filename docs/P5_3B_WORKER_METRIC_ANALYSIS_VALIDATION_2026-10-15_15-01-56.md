# P5.3b Worker 指标分析接入验证

## 结果与范围

基线为 `4d3c7d0e91f6875e57740156b02b5a3f494b0769`，开始时本地 HEAD 与
`origin/main` 均与基线一致。本次将 P5.3a 的确定性分析接入 Worker 上下文，
没有重构调查主循环，没有新增依赖、公开工具、数据库表或 Checkpoint schema 字段。

最终聚焦测试 **185 passed**；完整 Python 测试 **1562 passed, 2 skipped,
190 subtests passed**。没有调用付费模型、访问运行中的 Prometheus 或注入真实故障。

本次提交范围：

| 文件 | 实际改动 |
| --- | --- |
| [Worker](../investigation/worker.py) | 从已加载的原始指标记录选取有界子集，分目标分析，交付上下文并处理降级 |
| [分析器](../investigation/metric_analysis.py) | 局部修复完整历史快照与同刻独立指标混合时的跨时间误报 |
| [分析器测试](../tests/test_metric_analysis.py) | 增加混合快照复现，以及时间未覆盖、不完整快照、不同身份的反向边界 |
| [Worker 测试](../tests/test_worker_metric_analysis.py) | 增加上下文、隔离、大小限制、消息链、异常、预算、缓存、人工等待及恢复验证 |
| 本报告 | 记录实际行为、验证结果和限制 |

## 分析进入上下文的时机

原有 `_run()` 在每轮模型请求前调用 `_context()`。本次在 `_context()` 内，
直接从当前 Session 已加载的 `checkpoint["records"]` 选取
`query_metrics`、`query_metric_range`、`query_metrics_aligned` 原始记录，
然后调用 `analyze_metric_evidence()`。不调用分析整个 Checkpoint 的入口，
也不以 `_observation_summary()` 的裁剪结果作为分析输入。

选择从记录尾部开始，优先最近的完整记录；不切割单条历史序列，不补样本，
不改写原始 `observation_id`、调用身份、`identity`、采集时间、参数和历史 scope。
已有新鲜度判断仍由原 `effective_records()` 负责，两条路径相互独立。

允许目标与现有 Worker 的目标授权一致：Session 主服务及当前 Registry 中的
直接依赖。显式的结果服务必须与参数服务一致。未授权目标及不一致目标排除，
在 `excluded_record_counts` 中计数。记录按实际目标分别调用分析器，报告带
`service` 和 `target_role=primary/dependency`，不把依赖观测改标为主服务观测。
不同运行身份仍由分析器分开比较，不把旧身份替换成当前 Session 身份。

模型上下文的 `metric_analysis` 包含：

- `confirmed_observations`：经过校验的历史原始观测事实。
- `temporal_findings`：评估时间比较、历史变化区间及缺口。
- `hypotheses_to_verify`：尚待验证的候选解释。
- `source_observation_ids`：报告及各项发现的原始来源引用。
- `limitations`、`suggested_checks`：限制及取证建议，不会自动执行建议。

外层始终标记 `evidence_scope=historical_analysis`、`use_for_current_state=false`；
内层分析报告及确认观测也保留 false 的当前状态用途标记。报告不加入
`incident.evidence`，不写入原始 `records`，不改变 `effective_records()`。
没有指标记录时该字段为 null，不产生额外分析调用。

## 输入、输出与降级

| 边界 | 本次实施 |
| --- | --- |
| 原始记录数 | 全局最多 64 条，主服务与依赖共享上限 |
| 样本槽 | 全局最多 480；历史序列按实际样本数、关联查询按成员数保守计数，缺失成员也占槽 |
| 输入大小 | 全局最多 262144 字节；按完整记录 UTF-8 JSON 加保守分隔开销计数 |
| 分析器内部限制 | 沿用单条 64 KiB、节点数、深度、单字符串长度及有效样本总数等既有校验 |
| 上下文分析字段 | 整个 `metric_analysis` 值的 UTF-8 JSON 最多 32768 字节，包含选择统计和各目标报告 |

不满足选择上限时跳过整条记录，明确记录排除原因；不是把缺失当作零。
存在排除且仍有有效分析时，外层状态为 partial。无可分析证据为
insufficient_evidence。分析器的输入校验或输出上限失败保留其受控限制报告。
汇总后的上下文分析字段超限时，移除本轮派生报告，以
`metric_analysis_context_size_limit` 明确降级，不静默截掉部分事实后声称完整。

局部分析异常降级为 `metric_analysis_failed`，日志仅记录异常类型，不输出原始
异常文本。异常边界仅包围可选的本地选择、分析和序列化；数据库、租约、fencing、
模型调用及原调查状态处理仍在原路径中。普通分析失败不会触发自动重试或调查失败。

## Responses 链与崩溃恢复

`restore_context()`、`valid_chain()` 的接口和实现没有修改：

1. 未验证 Provider、证据过期或链不完整时，仍用现有 evidence_rebuild 路径；
   新分析随该轮真实的结构化上下文输入交付。
2. `provider_verified=true`、`chain_fresh=true` 且 `valid_chain=true` 时，保留
   原 complete_chain 及所有真实调用/输出对，在其后追加本轮 Runtime 上下文输入。
   该输入带 `context_origin=runtime_derived_metric_analysis`，明确不是人类消息或审批。
   不伪造 assistant/reasoning 消息、调用 ID 或 function_call_output。
3. `_context()` 不原地修改已存 history、pending 或 records。模型实际收到的
   上下文继续由原 `_run()` 写入已有 history；没有新增持久化字段。
4. 已落盘指标但工具输出尚未落盘时中断，重启后仍按原流程放弃未完成的只读调用，
   保留已存原始记录并重算分析；不补造缺失输出，不重放该指标请求。

开发时曾尝试对含分析的上下文强制使用重建分支。扩大回归发现两项原有消息链
测试失败，因为后续输入缺少真实工具输出链；随后改为上述 complete_chain
补充输入方案，保留原关键测试，最终完整回归通过。

## 混合历史快照边界

先新增复现测试，再修改分析器。修复前，两个完整历史快照位于不同时刻，
并加入其中一刻的独立历史指标，会错误返回 partial、`time_not_aligned` 和
重新获取同刻指标的建议。原始工具结果和完整 Checkpoint 记录两类输入，
各覆盖重复指标及新增指标，四个复现用例均实际失败。

最小修复只调整时间错位判定：同一运行身份和 scope 内，独立指标的评估时刻
若已被一个校验完整的关联快照覆盖，不再因另一个历史快照存在就宣称跨时间拼接。
同刻值/表达式冲突的检查仍保留。未覆盖的独立时刻、不完整快照或不同运行身份
不能用来抑制实际错位。没有增加新阈值、预测或根因推断规则。

## 实际测试与证据

使用 `D:\conda\envs\agentlearn\python.exe`：Python 3.12.14、pytest 9.1.1、
Pydantic 2.13.5、OpenAI SDK 3.14.0。没有安装或更新依赖。

| 验证 | 实际结果 |
| --- | --- |
| 修复前混合快照复现 | 4 failed，复现已确认的误报 |
| 最终聚焦回归 | 185 passed，19.53 秒；分析器、Worker 新测试及两项原有消息链测试 |
| 完整 Python 回归 | 1562 passed, 2 skipped, 190 subtests passed，325.72 秒，退出码 0 |
| 本次代码暂存 diff 检查 | 通过；四个代码文件与完整测试快照的 SHA-256 一致 |
| 用户既有文件及运行数据 | 20 个既有变更路径的内容/删除状态一致；300 个运行文件清单和 SHA-256 一致 |

最终聚焦回归使用：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest tests/test_runtime_quality.py::RuntimeQualityTests::test_complete_chain_pairs_every_denied_call tests/test_runtime_quality.py::RuntimeQualityTests::test_real_agent_prompt_and_thirteen_distinct_calls_share_same_budget tests/test_worker_metric_analysis.py tests/test_metric_analysis.py -q --disable-warnings
```

完整回归在临时源码快照中执行：导出指定基线，再覆盖本次四个代码文件，
避免把用户未提交的提示词等修改算入交付版本的验证。源码快照、测试日志和
哈希清单保留在本机临时目录，没有清理用户既有临时快照。

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest tests -q --disable-warnings -ra
```

新测试直接验证：大型混合 Checkpoint 只向分析器传递完整指标子集；120 点
原始序列不被旧工具摘要裁剪；缺失、不完整和 26 个历史缺口不补零；全局记录、
样本及字节上限；分析器和汇总输出超限；主服务/依赖/其他服务隔离；身份变化；
合法新鲜链仍交付分析并保留输出；重复上下文与重新打开 SQLite 不修改原始记录。

预算对照使用真实 Worker 循环和临时 SQLite，模型及工具为 Fixture：关闭分析、
开启分析、分析抛出异常，三种情况均为 **3 次模型请求、2 次工具执行、1 次缓存命中**，
只执行版本探测及一次关联指标工具。即使生成 suggested_checks，也没有额外执行。
另验证 WAITING_HUMAN 后重复轮询不增加调用。崩溃用例在工具保存边界通过
BaseException 模拟中断，重建 RuntimeStore 后指标记录不变，缺失输出没有被伪造。
完整套件还运行了既有真实 SQLite、子进程恢复、预算、缓存、租约、fencing、
人工审批及修复权限回归；没有修改这些权限或执行路径。

## 限制与未验证范围

- 两项跳过分别为仅供全新隔离安装环境的依赖锁校验，以及未设置
  `ALERT_RULES_PATH` 的可选跨仓库告警规则检查；不能声称这两项已完成。
- 完整套件含隔离本地 HTTP、SQLite 和子进程测试。未执行真实模型、真实
  Prometheus/Monitor/Demo 端到端联调、真实故障注入或修复动作；未核实远端 CI。
- 分析增加本地计算及模型输入字节。请求次数和预算不变不代表输入 token 或费用
  不变；本次没有测量真实模型费用、回答质量和第三方 Provider 对补充输入的行为。
- complete_chain 中真实交付的分析输入会随既有 history 保留，仍受原 4 MiB
  Checkpoint 上限约束；未新增全预算、大载荷组合压力测试。
- 依赖记录的 identity 沿用原 Worker 的采集上下文，不等于独立认证了依赖实例
  身份。按目标分组避免跨服务联合状态，但不新增身份采集或来源认证机制。
- 本次只检查本次文件的格式与链接；用户既有文档整理、README、提示词和临时
  文件修改均保留，不纳入提交。实际提交 SHA 与远端分支以最终交付核对为准。
