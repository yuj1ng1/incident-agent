# Incident Agent 回归测试报告

最新验证日期：2026-10-06。**5 项问题已修复，原有 35 项测试全部通过，无新增失败。** 未修改测试文件或降低断言，项目整体架构保持不变。

## 本次修改

| 文件 | 修复方式 |
| --- | --- |
| `reinvestigation.py` | 使用 `math.isclose`，绝对容差 `1e-12`，处理浮点边界；配置阈值仍为 0.2 |
| `models/investigation.py` | 新增 `MODEL_INCOMPLETE="model_incomplete"`，用于结构化失败结果和调查记录 |
| `agent/agent.py` | 每次模型响应后、处理 function_call 前检查 status；只有 completed 继续正常流程，其余状态返回失败，不更新上次成功时间/数值 |
| `monitor/incident_monitor.py` | 校验响应类型、success 状态、data 类型、alerts 字段及其 list 类型；非法快照记录 warning 并跳过恢复判断。调查抛异常时先保存当前 Incident，保留已收集证据 |
| `tools/metrics.py` | 使用 `math.isfinite`；NaN、正负 Infinity 返回 no_data，有限数值 0 仍是有效指标 |

`requirements.txt` 在本次修复开始前已包含 requests，已核实，无需重复添加。本报告更新了修复状态；下方保留首次回归记录。

## 修复后验证

使用 `D:\conda\envs\agentlearn\python.exe` 执行：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B -m unittest discover -s tests -v
```

结果：`Ran 35 tests`、`OK`、退出码 0。原先 5 个失败用例全部通过，原先通过的 30 个用例也全部通过。

另外完成 22 项临时检查：6 种未完成/缺失模型状态（带工具调用仍不执行工具，原成功时间和数值保持不变）；12 类非法告警快照（下一轮继续，仅合法快照触发恢复判断）；NaN、正负 Infinity；有限值 0。全部通过，未增加或修改现有测试。

真实 Prometheus 的 http_5xx_rate 查询已返回 `status="no_data"` 和非有限值说明，不再返回带 NaN 的 ok 结果。本次没有重复调用真实模型，修复前的真实模型调用记录见下方。

SHA-256 比对确认：既有 incidents.json、agent.log、原测试文件、config.py、fingerprint.py、incident_store.py、main.py、ToolManager 和 Prompt 均未变化。临时数据与测试日志已清理。

已知设计问题仍然存在，按要求未修改：Store 仍按 fingerprint 保存单个 Incident，再次 firing 会覆盖旧 Incident；后续需要决定是否保留历史 Incident。

## 首次回归记录（修复前）

执行日期：2026-10-06。结论：**35 项自动用例，30 项通过、5 项失败；真实启动、真实模型调查通过。** 业务代码未修改。

## 测试方式与环境

- 使用当前工作区代码，解释器为 `D:\conda\envs\agentlearn\python.exe`：Python 3.12.14、Pydantic 2.13.5、OpenAI SDK 3.14.0、requests 2.34.2。
- 自动用例运行真实 Monitor、Agent、ModelClient、ToolManager、工具解析和 IncidentStore，只替换外部响应、测试时钟和轮询等待。模型响应由当前 SDK 的 `Response.model_validate` 构造。
- 所有测试 Incident 写入临时目录的 `data/incidents.json`；日志也重定向到临时目录。真实启动测试在隔离目录执行原 `main.py`，实际查询 Prometheus 两轮后受控停止。
- 实际模型调查使用合成告警（`order-service/http_5xx_rate=0.27`）和真实模型、监控、健康检查工具；没有制造真实服务故障。SDK 超时设为 20 秒，整个探测限时 120 秒，调查轮数、工具次数和重试规则保持当前配置。
- 既有 `temp_test.py` 实际运行成功；由于当前无 firing 告警，其输出为空，不能仅据此证明 firing 解析正确。

复现自动用例（当前预期返回失败，并显示下述 5 项问题）：

```powershell
Set-Location 'D:\Incident Agent'
& 'D:\conda\envs\agentlearn\python.exe' -B -m unittest discover -s tests -v
```

## 通过情况

| 场景 | 实际结果 |
| --- | --- |
| 启动、组装、轮询 | main.py 创建真实 Agent、PrometheusClient、IncidentStore、IncidentMonitor；mock 和真实查询都能完成两轮轮询 |
| 新告警 | firing 正确解析为 Alert，fingerprint 稳定，新 Incident 先持久化再调用 Agent，工具证据及调查记录可回读 |
| 去重 | 同 fingerprint 的 investigating Incident 复用原 ID，不重复创建；不同 service/metric 的 fingerprint 不同 |
| 重新调查 | 冷却前稳定告警跳过；恰好达到 5 分钟冷却时调查原 Incident；明显恶化可提前调查，ID 保持不变 |
| 恢复与再次 firing | 不再 firing 时标记 resolved；再次 firing 创建新 ID；pending 不启动调查 |
| InvestigationResult | completed 成功；达到最大轮数返回失败并保存 max_round_reached；失败及抛异常均不更新上次成功时间/数值 |
| Evidence 常规路径 | ok 保存；no_data、执行失败不保存；相同内容跨调查去重，JSON 键顺序不影响去重；变化内容新增 |
| 异常隔离 | Prometheus 单轮请求异常被记录，下一轮继续；Agent 异常被记录，其他告警及下一轮可继续处理 |
| 持久化兼容性 | 现有 incidents.json 能按当前模型加载；旧数据中 null evidence/investigations 转为空列表 |

真实模型调查完成 3 轮，返回 `completed`，持久化 1 条调查记录、4 条 evidence，成功时间已更新，未记录 ERROR。这证明调用与保存链路可运行；未评价模型结论的诊断准确度。

## 5 项失败及原因

| 问题 | 复现与实际行为 | 原因与代码位置 |
| --- | --- | --- |
| 恶化阈值边界误判 | 上次 0.50、当前 0.70、未到冷却时间：没有重新调查 | `0.70 - 0.50 = 0.19999999999999996`，直接与 0.2 比较失败；`reinvestigation.py:13`。按当前规则阈值为绝对增加 0.2，即 20 个百分点 |
| 未完成响应误记成功 | SDK Response 的 status=incomplete、无工具调用、答案为空：被保存为 completed，并更新 last_investigated_at | `agent/agent.py:54` 只根据是否有工具调用结束调查，`agent/agent.py:285` 未检查模型响应状态即返回 success=True |
| 非法告警快照误判恢复 | mock 返回 `{"status":"success","data":{}}`：已有 active Incident 被标记 resolved | `monitor/incident_monitor.py:28` 将缺少 alerts 当成空数组，随后执行恢复逻辑；这是异常响应防护用例，真实 Prometheus 本次未返回此类响应 |
| 调查中途异常丢失证据 | 工具先成功收集 1 条证据，下一轮模型抛异常：持久化文件中仍为 0 条 | `monitor/incident_monitor.py:137` 捕获调查异常后直接 return，没有保存 Agent 已追加到内存的 evidence；异常仅写日志，不生成调查记录 |
| NaN 被保存为有效证据 | 真实 http_5xx_rate 查询返回 NaN，工具标记 ok；相同响应的回归用例实际新增了 Evidence | `tools/metrics.py:75` 未检查数值有限性；`agent/agent.py:253` 接受 status=ok 后保存。真实分子和分母均为 0，因此比例无定义；这不应被当成有效指标证据 |

对应失败用例：

- `test_exact_decimal_worsening_threshold_reinvestigates`
- `test_incomplete_model_response_does_not_mark_investigation_successful`
- `test_invalid_alert_snapshot_does_not_falsely_resolve_active_incident`
- `test_successful_evidence_survives_later_model_exception`
- `test_undefined_metric_does_not_become_valid_evidence`

## 环境问题与验证边界

- Prometheus `localhost:9090`、Loki `localhost:3100`、order-service `localhost:8001` 均返回 HTTP 200，服务可达。告警列表为空；firing 生命周期由可控告警覆盖，未修改服务故障模式。
- 默认 `python` 指向 `D:\conda\python.exe`，Pydantic 为 1.10.8，实际启动报无法导入 `field_validator`。项目明确要求 Pydantic 2；这是选错运行环境，agentlearn 下启动正常。
- Loki 工具返回 no_data，不能据此认为服务没有故障；本次没有取得该 service 标签的日志。
- 无请求时 5xx 比例为 0/0 是当前数据状态；把 NaN 标记为 ok 并保存才是代码问题。
- `requirements.txt` 没有列出代码直接使用的 requests。当前环境预装了它，因此未导致本次启动失败；仅确认依赖清单缺项，未进行干净环境安装实验。
- Git HEAD 的 main.py 是较早的交互式入口，并非紧邻此次重构前的告警轮询版本。因此不能据此证明上述问题由本次重构新引入。

## 职责与修改建议

当前职责基本符合目标：main.py 只创建对象、组装依赖、启动；IncidentMonitor 编排告警生命周期和调查调度；Agent 调查、调用工具并收集证据；IncidentStore 读写 JSON。恢复批量更新由 Store 的 `resolve_inactive_incidents` 执行，由 Monitor 触发。

建议小范围修复上述 5 项，优先处理错误成功标记、异常后证据保存、非法快照误恢复和 NaN，再处理浮点边界；补齐 requests 依赖。不需要整体重构，也不应修改冷却时长、恶化阈值或 Incident 去重规则。

另一个需要明确的现有行为：Store 按 fingerprint 保存单条记录。resolved 后再次 firing 虽然创建新 ID，但会覆盖该 fingerprint 的旧 Incident 及其调查历史；是否保留历史属于业务决策，本次没有更改，也没有计入失败数。

## 文件与数据保护

仅新增 `tests/test_incident_regression.py` 和本报告；既有未提交业务改动保持原状。临时 Incident、测试日志和真实模型探测目录均已清理。

执行前后 SHA-256 一致：

- `data/incidents.json`：`E629ADD2BBD7B4776F97842F12524FD98ED4DCE4128CE6A9C9C85C7D477A98A6`
- `agent_logger/agent.log`：`9D4AB3CB447A04A34FCD7C7185DC47D9FE99AFF7BADACC3F9FBB1DA63AA18336`
