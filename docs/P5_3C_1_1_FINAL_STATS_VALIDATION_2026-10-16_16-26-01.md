# P5.3c.1.1 最终调查结果统计一致性验证

## 范围与基线

基线：`3914135f0691c02d404365fd95dfb53662d44829`，分支 `main`。
实施前实际 HEAD、上游跟踪分支及实时读取的远端 main 均为该 SHA，暂存区为空。

只修复最终 InvestigationResult 的 `model_rounds`、`tool_calls`。
生产代码仅修改 [RuntimeStore.checkpoint](../investigation/store.py) 的 11 行。
复用现有 Session 计数、SQLite 事务、Checkpoint 和 Incident 幂等投影；
没有新增公开接口、数据结构、数据库字段或依赖。

已有的 README、提示词、临时测试、文档整理和运行数据保持原内容。
P5.3c.1 的两份测试此前尚未跟踪，本次沿用并增强后纳入交付；
原 P5.3c.1 验证报告仍记录当时事实，未改写。
本阶段没有执行 P5.3c.2，没有请求真实模型、Prometheus 或业务服务。

## 修复前复现

1. 使用既有 P5.3c.1 测试运行 `tests/test_incident_mock_e2e.py`：
   **25 passed in 72.50s**。旧断言没有比较最终结果中的两个计数字段。
2. 在既有 `finish_and_reopen()` 中加入最终统计断言，再运行原场景
   `test_a_two_historical_snapshots_complete_the_incident[False]`：
   **1 failed in 9.08s**，失败点为最终 InvestigationResult 计数与 Session 的 3/3 不符。
3. 核对生产调用链：Agent.handle_final_answer 创建结果时未传入统计字段，
   InvestigationResult 使用默认 0/0；IncidentStore._runtime_record 另从 Session 取值，
   Web API answer_view 从结果取值。这是不同读取来源造成的不一致。

修复后同一场景 **1 passed in 6.87s**；最终定向运行再次覆盖该场景及 HTTP 返回。

## 权威来源与最终确定时刻

- `model_rounds = session.total_model_requests`。
- `tool_calls = session.total_tool_executions`。
- 使用当前 Session 的累计值，包括跨尝试累计；不使用当前尝试计数或跨 Session
  的 Incident 预算组汇总值。
- 工具累计值不由 records 或 trace 长度推算：缓存调用可以留下 trace，失租时已预留并发生的
  I/O 也可能尚未保存 observation。两者均须保留现有独立计数语义。
- 保持既有计数规则：Runtime 版本探测、正常工具调用和合法的 proposal evidence
  refresh 使用原有工具额度；缓存命中只增加 cache_hits，不增加工具执行次数。

顺序经当前代码核实：

1. Worker 预留模型额度，接收最终回答，将暂存结果和 Responses history 保存到 SQLite。
2. Worker._finish 调用原有 proposal evidence refresh，仍可能增加工具执行次数；
   随后处理原有安全点、人类消息及租约检查。
3. 在原有终态事务中，Session 转为 COMPLETED、FAILED 或 EXHAUSTED。
   RuntimeStore.checkpoint 先确认数据库中的旧 Session 尚未进入终态，再用该事务内
   的 Session 累计计数覆盖结果的两个字段。
4. 写入 Checkpoint、Session 及事件，由同一 SQLite 事务提交。
5. Manager 沿原有 outbox 路径投影 Incident，再确认投影；API 直接读取保存的结果。

因此，最终计数在受控刷新结束后、终态提交时确定。读取层不另算统计。
SQLite 内 Session 与 Checkpoint 同事务；Incident JSON 沿用原有可重试幂等投影，
不能据此声称 SQLite 和 JSON 是跨存储原子提交。

## 最终验证结果

- 最终定向：**108 passed in 75.11s**。
- 最终完整 Python 测试：**1646 passed, 2 skipped, 190 subtests passed in 435.54s**。
- 同一已验证环境的依赖检查：**No broken requirements found.**
- 本次六份源码/测试文件的暂存对象与完整测试快照逐份一致。

定向命令：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest tests/test_incident_mock_e2e.py tests/test_runtime_finalization.py tests/test_web_api.py tests/test_review_quality.py::test_finish_refresh_interruption_semantics -q --tb=short -ra
```

完整命令：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest tests -q --tb=short -ra
```

完整测试使用从基线建立的 193 文件隔离快照，覆盖本次六份源码/测试文件。
这些文件与工作区交付内容的哈希一致；没有把用户其他未提交改动加入验收快照。
所有测试存储和日志均位于临时目录。

| 场景 | 实际核验的计数（模型/工具） | 其他实际断言 |
| --- | --- | --- |
| 原 P5.3c.1 两个历史快照 | 四处均 3/3 | Checkpoint.budget 同为 3/3；HTTP Session 与 Incident 端点均返回 3/3 |
| 缓存与完整 Responses 链 | 四处均 4/2 | cache_hits=2，保留全部三对真实 function_call/output |
| 跨尝试成功 | 四处均 5/4 | 第二次尝试为 3/2，cache_hits=1，没有用当前尝试覆盖累计值 |
| 最终回答后刷新版本证据 | 四处均 1/2 | 保存回答时为 1/1；原刷新增加一次工具执行；history 不变 |
| 模型永久失败 / context limit | 四处均 2/2 | FAILED，保留已保存证据及原失败语义 |
| 重试后模型失败 | 四处均 4/4 | FAILED；当前尝试 2/2 |
| 模型 / 工具 Session 预算耗尽 | 四处均 1/2 | EXHAUSTED，没有执行超额模型或工具调用 |
| 原尝试模型预算耗尽 | 四处均 7/2 | EXHAUSTED，6 次缓存命中，没有第八次模型请求 |
| 暂存回答后崩溃恢复 | 四处均 1/1 | 模型或工具总预算已用尽，仍不新增请求或工具执行 |
| 终态事务中断后恢复 | 四处均 1/1 | Checkpoint 插入后、Session save 前注入崩溃；事务回滚，恢复后统一提交 |
| 刷新 I/O 后失租再恢复 | Session、Checkpoint.result 均 1/2 | 不重做模型或工具调用；保留原 history、records、trace；该用例未单独断言 API |
| 保存最终结果后恢复次数耗尽 | 四处均 1/1 | Manager 直接转 EXHAUSTED，复用已保存回答，无新 I/O |
| 历史终态结果重新投影与归档 | Checkpoint 结果保持 0/0，历史记录保持 3/4 | 原不一致被刻意保留；归档字节不变，重复 recover/sync 后 SQLite 与 JSON 字节不变 |

历史快照、错误、数值和最终回答均为固定 Mock 输入；不代表新的外部采集或真实推理结论。
HTTP 检查复用 P5.3c.1 的真实 Agent/ModelClient、Worker、RuntimeStore 和 Mock Provider，
通过现有 TestClient 及实际只读端点验证。Windows 事件循环所需的本地通信不连接业务服务。

第一次完整测试为 **4 failed, 1642 passed, 2 skipped, 190 subtests passed in 425.27s**。
四项失败均来自已有 test_finish_refresh_interruption_semantics 的旧断言：恢复结果必须与
崩溃前 0/0 的暂存结果完全相同。实际差异仅为 model_rounds/tool_calls 修正成 1/2；
原有模型/工具调用次数、失租和 fencing 断言通过。更新该统计预期时保留其他字段的
完整相等比较，并新增 history、records、trace 不变断言，没有删除或弱化原安全验证。

定向开发过程中曾出现两类测试夹具问题，均已修正：
HTTP TestClient 在 Windows 初始化事件循环时与 P5.3c.1 的 socket 禁网夹具冲突，
因此将 HTTP 检查放入现有 API 测试，保留原端到端测试的禁网保护；
HTTP 对照的初版两次查询使用相同参数，实际命中了缓存，后改用两个不同评估时刻，
使 3/3 对照与原 P5.3c.1 场景相同。没有为这些测试修改生产缓存或网络边界。

## 崩溃、失败及历史兼容边界

- 新增崩溃回归分别在最终回答保存后，以及终态 Checkpoint 已写入事务但 Session
  尚未保存时注入 BaseException。后者验证事务回滚，恢复时不重做模型请求和普通工具调用。
  同时覆盖原默认零计数结果与预先填好计数的结果；恢复不会将正确累计值退回零。
- FAILED / EXHAUSTED 首次写入结果时也会统一计数，包括 Worker._failure 以及
  Manager 恢复直接进入终态且已有 result 的路径。
- Manager 在没有 result 的预算/恢复耗尽路径仍保留原有空结果及 Incident fallback，
  没有额外构造最终答案。已有相关测试继续保留并通过。
- 终态后重投影、ack、修复提案重试及归档不会回填旧结果。旧版本已终态的 0/0、
  缺少统计字段或缺少 result 的数据没有自动迁移；相关旧 API 差异可能继续存在。
  历史兼容回归具体验证了零值结果在热记录和归档场景中的不变性，
  未枚举所有手工修改或损坏的历史数据。
- 结果在 RUNNING 状态保存时仍属于暂存回答；本次只在首次终态提交时冻结最终计数。
- 工具和模型 I/O 中断仍沿用原有先预留计数的预算规则；这些计数不是远端收到请求的证明。
  本次没有引入新的请求重放、修改证据刷新策略或重新定义预算。
- proposal refresh 的成功增量已验证；没有为统计修复新增跨进程刷新去重机制，
  也没有声称任意未完成 I/O 都能实现 exactly-once。
- result.tool_trace 的填充不属于本次两个计数字段的修复范围；Checkpoint trace、原
  function_call/output、分析上下文及其顺序沿原路径维护。

## 改动文件与保留范围

- [investigation/store.py](../investigation/store.py)：首次终态 Checkpoint 冻结统计。
- [tests/p53c1_support.py](../tests/p53c1_support.py)：统一校验最终结果、Checkpoint budget、
  Session、Incident 和 API 的累计值。
- [tests/test_incident_mock_e2e.py](../tests/test_incident_mock_e2e.py)：沿用原 25 项，
  增加刷新、Session 预算、跨尝试失败、已有结果及事务中断恢复回归。
- [tests/test_runtime_finalization.py](../tests/test_runtime_finalization.py)：补充恢复耗尽、
  原终态结果、重复投影与归档不变性。
- [tests/test_web_api.py](../tests/test_web_api.py)：P5.3c.1 Mock 调查实际 HTTP 返回 3/3。
- [tests/test_review_quality.py](../tests/test_review_quality.py)：更新已有中断恢复用例的统计预期，
  保留其他结果字段的完整比较，新增 history、records、trace 不变断言。
- 本报告。

没有修改 Agent、分析器、工具白名单、权限、人工审批、状态机、租约、fencing、
计数预留、缓存、运行预算或任何真实运行数据。没有新增生产接口和依赖。
本轮对初始清单中 500 个非任务路径进行了哈希保护核对。
本次修改范围的 diff 检查通过。

完整测试中两项既有跳过：干净环境依赖安装检查需在 CI/隔离虚拟环境运行；
跨仓库告警规则检查需显式设置 ALERT_RULES_PATH。

## 尚未验证与交付

没有执行真实 Provider/模型、Prometheus、业务服务或 P5.3c.2。
Mock 验证调查流程、证据约束、统计落盘与读取一致性，不证明模型真实根因推理能力。
本轮未单独核验远端 GitHub Actions。

本报告记录提交前验证。按本任务授权，完成后仅提交本次列出的测试、生产代码与报告，
实际提交信息、完整 SHA、目标分支及远端 SHA 核对结果由最终交付回复报告。
