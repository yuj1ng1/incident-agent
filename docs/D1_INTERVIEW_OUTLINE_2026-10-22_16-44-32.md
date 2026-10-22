# Incident Agent D1：面试讲解提纲

本提纲依据当前仓库和 D1 实测整理。个人贡献按实际参与范围表述；代码中存在某功能，不等于面试者已经验证所有真实场景。

## 30 秒开场

“这个项目把监控告警变成可追踪的故障调查任务。模型提出工具调用，Python 负责校验权限、控制预算并持久化执行过程。结果不会因为模型回答完成就自动可信，修复也必须经过独立的人工审批。今天展示离线 Mock 流程；另有已封存的真实两轮模型实验，二者分开说明。”

## 5 分钟演示顺序

| 时间建议 | 做什么 | 说明什么 |
| --- | --- | --- |
| 0:00–0:40 | 运行 `scripts/d1_demo.py`，展示合成告警 | 输入是 0.27 的错误率夹具，不是当前业务故障；阈值不是根因 |
| 0:40–1:30 | 指出 Incident ID、Session ID 和 3 轮 Mock 选择 | Incident 表示事件，Session 表示一次调查；模型响应是夹具，Python 执行链是真实运行 |
| 1:30–2:30 | 展示参数、call_id、observation_id 和工具结果 | 自动版本探针也计预算；响应来自哪里、什么时候采集、是否缺失，比一段自然语言更可核查 |
| 2:30–3:20 | 展示 SQLite 重开核验、Checkpoint 和事件数 | 状态和已执行操作可持久化追踪；不是只在内存中拼一段回答 |
| 3:20–4:20 | 阅读原答与 ResultTrust | `COMPLETED` 是流程状态；结构合法不代表语义正确，`pending_review` 没有被冒充为人工批准 |
| 4:20–5:00 | 指出独立审批模块及历史真实实验 | D1 不执行修复；历史 v3 协议完成两轮请求，当前 v5 只通过离线验证 |

时间是讲解建议，不是性能测试或响应时间承诺。

## 应当能沿源码讲清的技术点

| 技术点 | 源码入口 | 要能解释的问题 |
| --- | --- | --- |
| 告警输入与去重 | [IncidentMonitor](../monitor/incident_monitor.py)、[alert_parser](../alert_parser.py)、[fingerprint](../fingerprint.py) | 如何区分重复观察和新事件？为什么不把当前数值或时间戳当作稳定身份？ |
| 生命周期与运行权 | [Manager](../investigation/manager.py)、[Worker](../investigation/worker.py)、[RuntimeStore](../investigation/store.py) | 谁能认领任务？状态转换、租约和 fencing 如何防止旧 Worker 继续提交？ |
| 模型与代码分工 | [Agent](../agent/agent.py)、[ModelClient](../model_client/model_client.py)、[ToolManager](../tools/tool_manager.py) | Agent 负责一次请求和响应解析，谁控制重试和累计预算？为什么不能依靠 Prompt 限权？ |
| 持久化与恢复 | [Checkpoint](../investigation/checkpoint.py)、[RuntimeStore](../investigation/store.py)、[IncidentStore](../incident_store.py) | 工具执行与保存的边界在哪里？已保存结果怎样影响恢复？跨 SQLite / JSON 为什么需要投影对账？ |
| 证据有效性 | [evidence_policy](../investigation/evidence_policy.py)、[Worker](../investigation/worker.py) | `success`、`status=ok`、`no_data`、历史/当前状态分别意味着什么？ |
| 结论可信性 | [result_trust](../investigation/result_trust.py) | 结果结构检查、语义审查、人工批准为何分离？当前接受写入口缺在哪里？ |
| 修复边界 | [Planner](../remediation/planner.py)、[approval](../remediation/approval.py)、[Executor](../remediation/executor.py) | 什么结构化证据才能产生提案？为什么低风险或模型同意不能替代审批？ |

## 关键工程难点与可核查的回答

1. **可恢复不是简单重跑。** 请求、工具执行与落库可能在不同边界失败；Checkpoint 只能记录已可靠完成的动作。Worker 使用预算预留、已保存记录和租约/fencing 管理恢复。D1 主要展示正常路径，恢复行为由本轮运行的既有 P6.4 隔离回归补充验证，不能把它扩展成任意外部副作用的恰好一次保证。
2. **运行完成与结论正确分开。** `result_schema_only` 检查对象结构；observation_id 和哈希帮助追溯内容，却不证明陈述由证据支持。当前 ResultTrust 保守保持不可信，正式人工接受入口仍未实现。
3. **数据缺失需要保持缺失。** D1 的 `--scenario no_data` 可以直接展示无 `value` 的结果。不能补零，不能因健康路由正常就认定业务恢复，也不能把历史 replay 当作当前状态。
4. **人工交互与执行授权分开。** 普通消息、HumanRequest 回答、Remediation 批准和执行是不同业务动作。D1 连续流程没有 HumanRequest 或提案；本轮只对已有审批模块做独立临时 Store / Mock 测试。
5. **多个存储没有共同事务。** SQLite 是调查状态权威来源；Incident JSON 和 Remediation JSON 有各自职责，通过幂等投影和对账处理部分失败。备份与恢复不能只考虑一个文件。

## 遇到追问时保持的证据边界

| 追问 | 有依据的回答 |
| --- | --- |
| “模型这次真的自主选工具了吗？” | 本次选择来自固定 Mock SDK，生产解析和执行逻辑实际运行。历史真实实验有 Provider 自主工具调用记录，需要另看封存材料 |
| “是不是找到了数据库或代码根因？” | 本次没有确认根因；只有合成指标和日志，没有完整因果证据链 |
| “真实两轮证明了什么？” | 历史请求协议 v3 下，第二次真实 SDK 请求携带 4 组已保存工具输出并返回最终回答；不是根因或语义正确性验收 |
| “v3 是 DeepSeek V3 模型吗？” | 此处 v3/v5 是实验请求协议版本；历史请求模型字段是 `deepseek-flash`，不能由协议版本推定模型代际 |
| “v5 已经真实调用成功了吗？” | 没有。已完成的是离线精简协议与兼容验证，没有新的真实 Provider 试验 |
| “人工审核已经做完了吗？” | 没有正式语义批准记录；当前无人工接受写入口。修复提案审批是另一套已有流程 |
| “能自动修复任意故障吗？” | 现有执行器只支持固定白名单及执行前检查，D1 没有调用修复；不能从模型建议生成任意命令 |
| “能证明生产性能和通用准确率吗？” | 不能。本轮是单机离线演示和必要回归，没有负载测试、业务恢复验证或通用准确率评估 |

## 已知限制与后续讨论

当前仍缺少正式的调查结论人工接受写入口；真实模型对最新 v5 的遵循情况未验证；D1 没有验证实时告警链路、真实监控服务、业务恢复、Web 页面或真实修复。SQLite/JSON 的跨存储一致性需要持续依靠对账；日志窗口与告警窗口、版本身份与实际故障路径也必须分别核对。

这些是后续工作的讨论点，不是本轮已实现承诺。本轮不进入 P6.6，也不继续扩展引用协议。
