# P5.3a 确定性指标证据分析与验证

## 范围与基线

Git 基线为 `98813713dced9a93e5df1182a4377f52d1029ecb`。只新增
[分析模块](../investigation/metric_analysis.py)、
[回归测试](../tests/test_metric_analysis.py)和本报告。
没有接入 Worker，也没有执行 P5.3b。

不修改指标工具、Tool Schema、PromQL、Checkpoint schema、缓存 TTL、
Incident 状态机、模型或工具预算、修复授权及恢复验证。没有新增依赖、
数据库、后台任务或独立服务。分析器没有网络请求、模型调用、时钟读取和输入写入。

任务开始时的 20 个既有变更路径包含文档整理、README、提示词及临时测试文件。
它们不属于本次提交；提交前按初始文件哈希及删除状态核对。
原有临时测试快照不清理。

## 已核实的现有数据结构

- `query_metrics` 成功结果带服务、指标、有限数值和实际 `evaluated_at`。
  `promql` 和 HTTP 请求时间可能在失败结果中缺失；不能假定所有字段存在。
- `query_metric_range` 使用真实 `samples`、实际时间窗、步长、预计/实际点数和
  `missing_evaluated_at`，历史结果为 `evidence_scope=time_window`、
  `use_for_current_state=false`。工具的 `status=ok` 不保证时间序列没有缺口。
- `query_metrics_aligned` 的逐项状态、`request_executed`、数值、实际评估时间
  和完整性必须分别校验；顶层 `status` 和共同 `evaluation_time` 不能替代样本时间。
  P5.2c.1 的 `request_executed` 记录是否进入 HTTP 客户端，不证明服务器已收到请求。
- Checkpoint 原始观测包括 `observation_id`、`name`、`arguments`、`call_id`、
  `result`、`success`、`collected_at`、`identity` 和 `cache_key`。
  `success` 表示 Python 工具执行情况，不等于取得有效监控证据。
- Worker 的 `collected_at` 是保存观测的时间。现有 `effective_records` 负责
  当前证据资格，明确排除两个历史指标工具；本阶段不改变该过滤器。

## 分析入口与返回结构

```python
from investigation.metric_analysis import (
    MetricToolResult,
    analyze_checkpoint_metrics,
    analyze_metric_evidence,
)

report = analyze_metric_evidence(
    [MetricToolResult(observation_id, "query_metrics", validated_tool_result)],
    service="order-service",
)
payload = report.to_dict()

report = analyze_checkpoint_metrics(
    loaded_checkpoint,
    service="order-service",
)
```

第一入口接受有界的 list/tuple，元素为带明确来源 ID 的 `MetricToolResult`、
现有 `ToolObservation` 或完整观测记录字典。第二入口接受现有 `Checkpoint`
对象或其字典，校验既有 schema 后只提取指标原始记录。Worker 摘要不能作为
完整原始证据：出现 `summary_*` 标记即拒绝。

返回冻结的 `MetricAnalysis`，`to_dict()` 返回独立的可 JSON 序列化副本。

| 字段 | 内容及边界 |
| --- | --- |
| `status` | `complete`、`partial`、`insufficient_evidence`；表示分析完整性，不表示系统健康或根因已确认 |
| `confirmed_observations` | 经校验的指标名、真实采样点、来源、可用的 PromQL、原历史 scope 和运行身份键 |
| `temporal_findings` | `ComparisonFinding` 或 `RangeFinding`，记录同刻比较、错位、缺口、首尾、极值及变化区间 |
| `limitations` | 受控代码和固定说明；包括无效输入、缺失数据、时间错位及通用证据限制 |
| `hypotheses_to_verify` | 固定候选解释，明确尚未验证；与确认观测独立 |
| `suggested_checks` | 后续取证建议，分析器不会执行这些建议 |
| `source_observation_ids` | 实际检查过的原始来源 ID；确认事实和各条发现也带来源引用 |
| `use_for_current_state` | 始终为 false；分析结果不能代替现有新鲜度与当前状态策略 |

缺失的可选 PromQL 不伪造，缺失的评估时间不替代。失败记录可以出现在限制的
来源引用中，但不会出现在确认数值中。输入中的 scope、用途标记、时间、数值和
Checkpoint 对象保持不变；即时输入没有 scope 时，输出保留 None。

## 确定性规则

### A：严格时间对齐

只使用经校验的实际 `evaluated_at`。显式时区转换成 UTC 后精确比较到微秒，
不使用采集时间、HTTP 起止时间或调用顺序替代。实际工具结果只具有至多微秒
精度，分析器拒绝更高精度字符串，避免静默舍入造成伪对齐。

关联工具还校验请求指标集合、每项状态及请求执行标记、共同时间与实际样本
时间，并重新计算 `comparison_complete` 和顶层状态。声明与复核结果冲突时
拒绝整组，不选择看起来正常的成员来认证快照。正确的不完整比较可保留通过
校验的成员，但只对实际存在的指标子集报告严格同刻，不声称完整比较成功。

不同评估时刻不拼接成快照。不同 Checkpoint 运行身份及不同 scope 分开比较。
运行身份键是原 `identity` 的确定性摘要，不是新的身份认证或持久化状态。
原始身份缺失时，同刻只能说明评估时间一致，不能证明是同一个运行实例。

### B：零、缺失、失败分别处理

有限数值零和一均有效。布尔值、数值字符串、NaN、Infinity、负值及违反已有
指标语义范围的值拒绝；过大整数不能经 float 转换丢失精度后成为确认事实。
`no_data` 不转换成零；`incomplete` 不冒充成功比较；请求未执行或失败不产生
有效数值。缺失记录附带数值或评估时间时拒绝该不一致结果。

### C：历史趋势与缺口

按真实、严格有序、位于实际区间及步长网格上的采样点计算首尾时间和值、
已观测最小值和最大值。独立重算预计点数和缺口，与原点数、缺口数量及时间
列表逐项核对；截断结果不能认证完整历史序列。

相邻有效采样点的值不同就记录 `ObservedChange(before, after,
missing_points_between)`。这是两个观测之间的变化区间，不能定位精确事件
发生时刻。跨缺口变化明确保留缺失点数。相邻值相同只表示所见点相同，
不证明区间内无异常。没有插值、补零或新增通用百分比阈值。

### D：一致性疑点与候选解释

仅在已校验、严格同刻、同一比较身份和 scope 的请求速率为零且 5xx 比例为正时，
记录 `numeric_consistency_question`。候选解释仅涉及待核实的查询窗口、原始
样本或后端读取一致性；不确认数据库故障、代码回归、重启或计数器重置。
建议检查原始计数器、抓取记录、PromQL 窗口和查询时间。

同刻重复指标存在不同值或不同表达式时保留各自事实，记录冲突，不选择一个
覆盖另一个。重复观测 ID 或重复 Checkpoint 调用 ID 则受控地拒绝整批，防止
来源歧义。相同评估时刻不保证原始样本抓取时刻相同，也不提供 HTTP 事务原子性。

### E：历史与当前隔离

历史结果必须保持 `time_window` 及 false 的用途标记，否则拒绝。过期的即时
观测仍可说明原评估时刻的值，分析器不读取当前时钟，也不续期缓存或提升为
当前健康证据。所有派生事实和报告自身都禁止直接用于当前状态。

## 输入、输出与来源边界

| 边界 | 限制 |
| --- | --- |
| 观测输入数量 | 最多 64 条；只接受有界 list/tuple，不消费任意迭代器 |
| 整批递归预检 | 256 KiB 字节计数、16384 节点、12 层；检测循环结构及非法 UTF-8 字符 |
| 单条递归预检 | 64 KiB；单字符串最多 8192 字符 |
| 每条历史序列 | 沿用现有最多 120 点、6 小时、15～300 秒步长 |
| 所有确认采样点 | 最多 480 点 |
| 每条变化区间明细 | 最多 16 条；保留准确变化总数与截断标记，结果为 partial |
| 序列化报告 | UTF-8 JSON 最多 32 KiB；超限返回小型证据不足报告，不静默丢弃事实 |

原有模型只约束外层 Checkpoint。分析器额外核对布尔执行标记、来源 ID、服务、
工具参数与结果对应关系、时间、点数和完整性，防止模型校验的类型转换将
`success="true"` 或错误 schema 类型升级为真实执行证据。

本模块不能认证伪造但结构完全相同的 Python 数据，也不重算基于当前 Registry
配置的缓存键、不检查数据库中的 Session 归属。调用方仍需通过可信工具执行
路径或 `RuntimeStore.load_checkpoint` 取得数据；后者已有行/Session/schema
检查。本阶段没有新增数据库签名或密码学真实性保证。

## 固定历史测试数据

[P5.2c 验证记录](P5_2C_METRICS_ALIGNED_VALIDATION_2026-10-14_15-48-41.md)
记录两组 2026-10-11 UTC 历史评估时刻：

| 时刻 | 请求速率 | 5xx 比例 | scrape up |
| --- | ---: | --- | ---: |
| 10:48:55.000 | 0.41821223361699034 | 1.0 | 1.0 |
| 10:48:55.300 | 0.0 | no_data | 1.0 |

本次仅将这些值作为固定夹具。请求速率 10:48:45 的 0.7273 是用户给定历史
数值的近似表达，10:49:00 的值为 0.0；不得把近似值写成更精确的新测量。
29 个预计点、3 个真实点、26 个缺口的 5xx 序列也是历史回归夹具。
这些测试没有新采集 Prometheus 观测，没有访问真实模型或注入故障。

## 实际验证

- 修改前现有三个指标测试文件：495 passed。
- 分析器最终定向回归：144 passed。
- `pip check`：No broken requirements found；没有修改依赖清单。
- 最终完整 Python 测试：1523 passed，2 skipped，190 subtests passed；
  用时 315.01 秒（5 分 15 秒）。命令为
  `D:\conda\envs\agentlearn\python.exe -B -m pytest tests -q --tb=short -ra`。
- 两个跳过项：干净安装环境的依赖 pin/legacy httpx 检查，以及需要设置
  `ALERT_RULES_PATH` 的跨仓库告警规则验证。二者未记为通过。
- 完整测试使用隔离 Git 源码快照，含基线和本阶段源码、测试；不混入既有
  工作区修改。最终源码快照 tree：`3bea01890ff62ad6241eedde305d26f2ab8d7bdf`。
  全量测试包括已有 Mock HTTP/模型、临时 SQLite 和子进程验证；不是生产服务联调。
- 新源码、测试均通过 AST 语法检查和 88 字符行宽检查；暂存 diff 无空白错误。
- 针对性测试覆盖历史错位与缺口、合法零、非法数值和时间、完整性伪造、
  逐项未执行请求、跨服务与运行身份、重复 ID、无效 Checkpoint、摘要拒绝、
  输入不可变、规模限制，以及不改变工具接口的 Mock 联接。
- 初始 20 个既有变更路径的文件哈希与删除状态复核：0 个差异。

## 未验证范围与 P5.3b 接入建议

未执行真实 Prometheus、付费模型、故障注入、修复或恢复验证；没有验证 Worker
实际使用派生分析后的决策质量，也未将本地测试等同于远端 CI 通过。结构合法
数据的真实性依赖调用方信任边界；抓取时间与 HTTP 原子性限制仍然存在。

P5.3b 应在现有 Worker 上下文构建的安全点，从已持久化原始记录选取有界的
指标子集调用本入口，并在现有上下文字节预算内呈现派生报告。保持
`effective_records` 为当前状态权威入口，历史分析另行说明评估时间；不能
把分析器的确认观测并入当前健康或恢复证据。分析步骤不需要新增模型请求，
也不需要工具执行额度；建议检查如被采纳，仍经过现有权限校验、预算预留、
租约及 fencing 路径。不得自动追加请求、扩大预算或修改 Checkpoint schema。
本阶段交付后等待代码审查，再决定 P5.3b 接入。
