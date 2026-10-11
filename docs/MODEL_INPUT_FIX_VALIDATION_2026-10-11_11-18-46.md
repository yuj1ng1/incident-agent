# 不可信模型输入与调度器正确性修复验证

## 范围与基准

基准及开始时的本地 main、远端 main 均为 `38fdcddc356c14d5b574be2dcac78a59f6539739`。已阅读原项目 AGENTS.md。原工作区有 20 个未提交修改或未跟踪路径，暂存区为空，因此在从 origin/main 创建的隔离工作树中实现和测试。

代码与测试提交：[d53ec66f6eb073a49f5a6f62edf77a420a927a6a](https://github.com/yuj1ng1/incident-agent/commit/d53ec66f6eb073a49f5a6f62edf77a420a927a6a)，已正常推送 main，无强制推送。本文是该提交 CI 完成后的验证记录；文档提交不改变下述代码和测试快照。

## 已复现的触发机制

旧 [Worker](../investigation/worker.py) 的 request_human_input 分支直接 json.loads，再将 question 传入 [InvestigationManager.request_human](../investigation/manager.py)。空字符串、null、整数、对象及超长字符串使 HumanRequest 创建抛出 Pydantic ValidationError；Worker.run_once 将其上抛，Scheduler.run 将其判为存储一致性错误，设置停止标志并退出。已占用的 Session 留在 RUNNING，其他 Session 无法继续被该 Scheduler 执行。

非法 JSON 的相邻风险也已复现：json.loads 抛出的 JSONDecodeError 经过同一严重错误分支，使 Scheduler 仅执行第一轮。纯空格字符串则被旧 HumanRequest 接受并进入 WAITING_HUMAN。多余字段虽然会以 invalid_human_request 结束调查，但拒绝前已把原始模型参数写入 response checkpoint；固定测试敏感文本因此进入持久化记录。

先加入真实临时 SQLite 与 FixtureAgent 回归测试、保持生产代码未修改时，得到 `21 failed, 11 passed in 16.42s`。完整原始失败输出保留在本文附录，未删除或弱化失败断言。

另验证了脱敏扩展长度：原始 question 恰为 2000 字符，token=a 替换为 token=[redacted] 后超出持久化上限，仍会触发 ValidationError。该失败已单独复现并修复。

## 局部设计与实际改动

| 文件 | 行为与原因 |
| --- | --- |
| [investigation/models.py](../investigation/models.py) | 增加内部 HumanInputArguments，严格字符串、长度 1–2000、拒绝纯空格及多余字段。持久化 HumanRequest schema 保持原样，避免改变旧记录兼容性。 |
| [investigation/worker.py](../investigation/worker.py) | 在保存模型响应前，用输入模型局部解析和校验每个人类提问调用；非法 JSON、非对象、缺少字段和错误值统一转成固定 invalid_human_request，不持久化被拒绝的原始响应。工具声明补充长度边界。 |
| [investigation/manager.py](../investigation/manager.py) | guard 后分别校验原始 question 和经现有 protect 脱敏后的 question；仅输入校验异常转换为固定业务错误。存储读取、checkpoint 和 HumanRequest 构造的校验错误仍上抛。 |
| [main.py](../main.py) | IncidentStore 使用基于 __file__ 的绝对路径，与同一项目中的 CLI/Web 默认路径一致。 |
| [web_api/__main__.py](../web_api/__main__.py) | 默认 8010，支持 --port，校验端口 1–65535，继续固定绑定 127.0.0.1 并关闭代理头信任。 |
| [web_api/app.py](../web_api/app.py) | 实际工作流使用 investigating/resolved，未定义 open 的业务含义；移除 open 筛选，返回 422，且参数拒绝时不读取 Store。 |
| [clients/database_client.py](../clients/database_client.py) | 删除完全被后一个同名类覆盖的定义和重复 import；比较前后有效类的 AST，构造函数与 health 完全一致。 |

复用现有 Pydantic、protect、Worker 失败流程、状态机和 outbox 投影。非法输入一般以 FAILED/invalid_human_request 结束；若原预算已耗尽，仍遵守既有 EXHAUSTED 规则。Worker 保存失败结果、释放 owner/lease 后执行原有 Incident 投影，没有重置预算或自动新建授权。

未修改 Worker.run_once 或 [Scheduler.run](../investigation/scheduler.py) 的严重错误处理。真实 SQLite 损坏、checkpoint JSON 损坏、checkpoint 校验错误和 Session 持久化校验错误仍使 Scheduler 停止；未降级为工具拒绝。fencing、lease、预算、人工消息、Remediation 审批和未知执行结果机制均复用原有流程。没有新增依赖、存储版本变化或迁移；没有拆分 Manager、重写 Scheduler、合并两次 Remediation 规划或新增 HTTP 写接口。

## 实际有效的回归测试

共新增 46 个可执行用例，另调整一条既有入口装配测试的隔离方式。

| 测试文件 | 新增覆盖 |
| --- | --- |
| [test_human_input_boundary.py](../tests/test_human_input_boundary.py) | 33 项：15 种非法参数、正常问题及 1/2000 字符边界、脱敏扩展、Manager 直接入口拒绝与事务不变、失效 fencing、两种 Scheduler 后续 Incident 进展、4 种真实持久化损坏、Manager 不掩盖已损坏 Session 的 ValidationError。检查所有保留的 checkpoint/event，不只检查最新查询视图。 |
| [test_entry_paths.py](../tests/test_entry_paths.py) | 2 项：从其他工作目录调用 Monitor/CLI，默认 Incident 路径均与 Web 一致，拦截构造并确保没有打开真实数据。 |
| [test_runtime_finalization.py](../tests/test_runtime_finalization.py) | 3 项：EXHAUSTED 时 result 缺失或为 null，及真实过期 lease 恢复耗尽且没有模型结果。验证失败投影、计数一次、重放幂等、未伪造成功调查或业务恢复。生产投影代码已有正确行为，因此未修改。 |
| [test_web_api.py](../tests/test_web_api.py) | 净新增 8 项：默认/指定/边界端口，非法端口及公网绑定参数拒绝，open 筛选 422。保留原有只读方法、字段过滤、脱敏、错误响应、只读哈希、并发读和无导入副作用测试。 |

两种 Scheduler.run 回归均实际运行调度循环：非法 Session 结束为 FAILED，第二个 inventory-service Session 完成，模型调用总数为 2，无 human request，无存储一致性停止日志。测试的停止标志仅在第二轮执行完毕后设置。

首次完整测试得到 `1 failed, 650 passed, 2 skipped, 190 subtests passed`。唯一失败是旧入口装配测试依赖工作目录隔离，在 main 改用稳定绝对路径后被 tests_must_use_temporary_store_paths 阻止。为该测试显式注入真实临时 IncidentStore，保留真实对象、两轮轮询和 v2 数据内容断言，新增默认绝对路径断言；没有移除测试保护机制。调整后该用例单独通过。

## 完整验证与 CI

本地解释器为 `D:\conda\envs\agentlearn\python.exe`，Python 3.12.14，Pydantic 2.13.5，pytest 9.1.1，FastAPI 0.140.13。

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest tests -q --tb=short --disable-warnings -ra
```

最终完整结果：`651 passed, 2 skipped, 190 subtests passed in 249.00s`。定向模块最终为 `83 passed`；入口装配调整后单独为 `1 passed`。pip check 返回 No broken requirements found；git diff --check、选择性暂存和凭据模式扫描均通过。

本地两个跳过项：test_clean_install_has_all_pins_and_no_legacy_httpx 仅在干净环境启用；可选外部告警规则验证需要 ALERT_RULES_PATH。未将跳过项算为通过。

GitHub Actions：[Runtime regression / 38107581292](https://github.com/yuj1ng1/incident-agent/actions/runs/38107581292)，验证 SHA 为 `d53ec66f6eb073a49f5a6f62edf77a420a927a6a`，结果 **success**。Windows/Python 3.12 的干净隔离环境完成固定依赖安装、pip check 和完整 pytest。日志中的最终输出为：

```text
652 passed, 1 skipped, 190 subtests passed in 204.25s (0:03:24)
```

CI 中干净安装检查已实际运行；可选外部告警规则检查仍跳过。

## 真实 HTTP 启动与使用方式

在隔离工作树实际运行启动器：默认 8010 和 --port 8011 均验证 healthz/docs/openapi 为 200，open 为 422，POST 为 405，不可信 Host 为 400。该工作树没有 data 文件，Incident/Session 查询按设计返回 503 且不建库。已有临时数据的正确查询、只读哈希和并发读取由 API 集成测试验证。两个自有测试进程均已停止，端口不再监听，无遗留 Store。

在项目根目录使用：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -m web_api
# 默认 http://127.0.0.1:8010/docs
& 'D:\conda\envs\agentlearn\python.exe' -m web_api --port 8011
```

未向 HTTP 客户端开放 Store 路径、host、调查命令或修复权限。

## 保留事项、限制与技术债

截至本文生成前，原工作区的 20 个未提交路径和 data/.env 共 299 个真实文件逐项核对未变化。实现和运行验证使用隔离工作树；提交未包含真实 data、.env、日志、README、AGENTS.md、用户提示词或 temp_test.py。

没有进行真实模型调用、真实数据库/Prometheus/Loki 联调或故障注入；本次修复的外部验证是本机 HTTP，调查回归是 FixtureAgent 加真实临时 SQLite，不能据此声称外部模型和监控服务均可用。

非阻塞技术债：Incident.status 仍为宽泛 str，后续可单独统一领域状态；Manager 职责集中以及 Worker/Manager 两次 Remediation 规划按要求保留；旧 FastAPI 学习指南保留了 8002/open 说明，本次新报告给出当前命令与筛选语义，未改写历史验证报告或用户的 README 修改。

## 原始失败与最终结果输出

以下保留运行输出的完整内容，仅去除行末空白以满足 git diff --check。涉及敏感文本的字符串均为固定 Fixture，未使用真实凭据。首次完整测试失败和最终通过分别列出，避免把中间失败隐藏为一次通过。

### 生产代码未修改时的 P1 回归

```text
FFFFFFFFF.FFF.F...FFFFFF.FF.....                                         [100%]
================================== FAILURES ===================================
___________ test_invalid_human_input_is_a_controlled_failure[empty] ___________
tests\test_human_input_boundary.py:48: in test_invalid_human_input_is_a_controlled_failure
    result = Worker(manager, agent).run_once()
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
investigation\worker.py:66: in run_once
    self._run(session.session_id, claim, checkpoint)
investigation\worker.py:532: in _run
    self.manager.request_human(session_id, claim, arguments["question"], checkpoint)
investigation\manager.py:443: in request_human
    request = HumanRequest(
E   pydantic_core._pydantic_core.ValidationError: 1 validation error for HumanRequest
E   question
E     String should have at least 1 character [type=string_too_short]
E       For further information visit https://errors.pydantic.dev/2.13/v/string_too_short
________ test_invalid_human_input_is_a_controlled_failure[whitespace] _________
tests\test_human_input_boundary.py:51: in test_invalid_human_input_is_a_controlled_failure
    assert result.status == S.FAILED
E   AssertionError: assert <SessionStatu...AITING_HUMAN'> == <SessionStatu...LED: 'FAILED'>
E
E     - FAILED
E     + WAITING_HUMAN
_______ test_invalid_human_input_is_a_controlled_failure[null-question] _______
tests\test_human_input_boundary.py:48: in test_invalid_human_input_is_a_controlled_failure
    result = Worker(manager, agent).run_once()
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
investigation\worker.py:66: in run_once
    self._run(session.session_id, claim, checkpoint)
investigation\worker.py:532: in _run
    self.manager.request_human(session_id, claim, arguments["question"], checkpoint)
investigation\manager.py:443: in request_human
    request = HumanRequest(
E   pydantic_core._pydantic_core.ValidationError: 1 validation error for HumanRequest
E   question
E     Input should be a valid string [type=string_type]
E       For further information visit https://errors.pydantic.dev/2.13/v/string_type
_____ test_invalid_human_input_is_a_controlled_failure[integer-question] ______
tests\test_human_input_boundary.py:48: in test_invalid_human_input_is_a_controlled_failure
    result = Worker(manager, agent).run_once()
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
investigation\worker.py:66: in run_once
    self._run(session.session_id, claim, checkpoint)
investigation\worker.py:532: in _run
    self.manager.request_human(session_id, claim, arguments["question"], checkpoint)
investigation\manager.py:443: in request_human
    request = HumanRequest(
E   pydantic_core._pydantic_core.ValidationError: 1 validation error for HumanRequest
E   question
E     Input should be a valid string [type=string_type]
E       For further information visit https://errors.pydantic.dev/2.13/v/string_type
_____ test_invalid_human_input_is_a_controlled_failure[boolean-question] ______
tests\test_human_input_boundary.py:48: in test_invalid_human_input_is_a_controlled_failure
    result = Worker(manager, agent).run_once()
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
investigation\worker.py:66: in run_once
    self._run(session.session_id, claim, checkpoint)
investigation\worker.py:532: in _run
    self.manager.request_human(session_id, claim, arguments["question"], checkpoint)
investigation\manager.py:443: in request_human
    request = HumanRequest(
E   pydantic_core._pydantic_core.ValidationError: 1 validation error for HumanRequest
E   question
E     Input should be a valid string [type=string_type]
E       For further information visit https://errors.pydantic.dev/2.13/v/string_type
______ test_invalid_human_input_is_a_controlled_failure[object-question] ______
tests\test_human_input_boundary.py:48: in test_invalid_human_input_is_a_controlled_failure
    result = Worker(manager, agent).run_once()
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
investigation\worker.py:66: in run_once
    self._run(session.session_id, claim, checkpoint)
investigation\worker.py:532: in _run
    self.manager.request_human(session_id, claim, arguments["question"], checkpoint)
investigation\manager.py:443: in request_human
    request = HumanRequest(
E   pydantic_core._pydantic_core.ValidationError: 1 validation error for HumanRequest
E   question
E     Input should be a valid string [type=string_type]
E       For further information visit https://errors.pydantic.dev/2.13/v/string_type
______ test_invalid_human_input_is_a_controlled_failure[array-question] _______
tests\test_human_input_boundary.py:48: in test_invalid_human_input_is_a_controlled_failure
    result = Worker(manager, agent).run_once()
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
investigation\worker.py:66: in run_once
    self._run(session.session_id, claim, checkpoint)
investigation\worker.py:532: in _run
    self.manager.request_human(session_id, claim, arguments["question"], checkpoint)
investigation\manager.py:443: in request_human
    request = HumanRequest(
E   pydantic_core._pydantic_core.ValidationError: 1 validation error for HumanRequest
E   question
E     Input should be a valid string [type=string_type]
E       For further information visit https://errors.pydantic.dev/2.13/v/string_type
_________ test_invalid_human_input_is_a_controlled_failure[too-long] __________
tests\test_human_input_boundary.py:48: in test_invalid_human_input_is_a_controlled_failure
    result = Worker(manager, agent).run_once()
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
investigation\worker.py:66: in run_once
    self._run(session.session_id, claim, checkpoint)
investigation\worker.py:532: in _run
    self.manager.request_human(session_id, claim, arguments["question"], checkpoint)
investigation\manager.py:443: in request_human
    request = HumanRequest(
E   pydantic_core._pydantic_core.ValidationError: 1 validation error for HumanRequest
E   question
E     String should have at most 2000 characters [type=string_too_long]
E       For further information visit https://errors.pydantic.dev/2.13/v/string_too_long
_______ test_invalid_human_input_is_a_controlled_failure[invalid-json] ________
tests\test_human_input_boundary.py:48: in test_invalid_human_input_is_a_controlled_failure
    result = Worker(manager, agent).run_once()
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
investigation\worker.py:66: in run_once
    self._run(session.session_id, claim, checkpoint)
investigation\worker.py:526: in _run
    arguments = json.loads(call.arguments)
                ^^^^^^^^^^^^^^^^^^^^^^^^^^
D:\conda\envs\agentlearn\Lib\json\__init__.py:346: in loads
    return _default_decoder.decode(s)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^
D:\conda\envs\agentlearn\Lib\json\decoder.py:338: in decode
    obj, end = self.raw_decode(s, idx=_w(s, 0).end())
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
D:\conda\envs\agentlearn\Lib\json\decoder.py:354: in raw_decode
    obj, end = self.scan_once(s, idx)
               ^^^^^^^^^^^^^^^^^^^^^^
E   json.decoder.JSONDecodeError: Unterminated string starting at: line 1 column 13 (char 12)
________ test_invalid_human_input_is_a_controlled_failure[extra-field] ________
tests\test_human_input_boundary.py:69: in test_invalid_human_input_is_a_controlled_failure
    assert UNTRUSTED_TEXT not in persisted + caplog.text + manager.incident_store.file_path.read_text(encoding="utf-8")
E   assert 'fixture-unt...nsitive-text' not in '{"session_i...54e"\n  }\n}'
E
E     'fixture-untrusted-sensitive-text' is contained here:
E       xtra\": \"fixture-untrusted-sensitive-text\"}", "call_id": "human-call"}], "history": [{"role": "user", "content": "\n    统一调查上下文（Python 校验；Policy 提示和候选关联不是根因）：\n    {\"alert_event\": {\"service\": \"order-service\", \"severity\": \"critical\", \"alertname\": \"legacy\", \"signal_type\": \"numeric\", \"source\": \"legacy\", \"state\": \"firing\", \"metric\": \"http_5xx_rate\", \"value\": 0.3, \"threshold\": 0.2, \"unit\": null, \"direction\": null, \"labels\": {}, \"identity_labels\": [], \"starts_at\": null, \"obse...
E
E     ...Full output truncated (82 lines hidden), use '-vv' to show
------------------------------ Captured log call ------------------------------
ERROR    agent:worker.py:701 调查状态更新 session=ses-b0e36891d1204e93987dad41a2886187 status=FAILED reason=invalid_human_request
_________ test_invalid_human_input_is_a_controlled_failure[null-json] _________
tests\test_human_input_boundary.py:52: in test_invalid_human_input_is_a_controlled_failure
    assert result.last_error_category == "invalid_human_request"
E   AssertionError: assert 'TypeError' == 'invalid_human_request'
E
E     - invalid_human_request
E     + TypeError
------------------------------ Captured log call ------------------------------
ERROR    agent:worker.py:701 调查状态更新 session=ses-24bb7bcccfef4954b17e502a62d7e0e5 status=FAILED reason=TypeError
_______ test_invalid_human_input_is_a_controlled_failure[integer-json] ________
tests\test_human_input_boundary.py:52: in test_invalid_human_input_is_a_controlled_failure
    assert result.last_error_category == "invalid_human_request"
E   AssertionError: assert 'TypeError' == 'invalid_human_request'
E
E     - invalid_human_request
E     + TypeError
------------------------------ Captured log call ------------------------------
ERROR    agent:worker.py:701 调查状态更新 session=ses-686808b5dc46488294a48a50f70e5307 status=FAILED reason=TypeError
________ test_invalid_human_input_is_a_controlled_failure[array-json] _________
tests\test_human_input_boundary.py:52: in test_invalid_human_input_is_a_controlled_failure
    assert result.last_error_category == "invalid_human_request"
E   AssertionError: assert 'TypeError' == 'invalid_human_request'
E
E     - invalid_human_request
E     + TypeError
------------------------------ Captured log call ------------------------------
ERROR    agent:worker.py:701 调查状态更新 session=ses-8898441693414a7a85731ac148530e95 status=FAILED reason=TypeError
____ test_manager_rejects_invalid_question_without_durable_changes[empty] _____
tests\test_human_input_boundary.py:101: in test_manager_rejects_invalid_question_without_durable_changes
    manager.request_human(claimed.session_id, ("test-worker", claimed.fencing_token), question,
investigation\manager.py:443: in request_human
    request = HumanRequest(
E   pydantic_core._pydantic_core.ValidationError: 1 validation error for HumanRequest
E   question
E     String should have at least 1 character [type=string_too_short]
E       For further information visit https://errors.pydantic.dev/2.13/v/string_too_short
__ test_manager_rejects_invalid_question_without_durable_changes[whitespace] __
tests\test_human_input_boundary.py:100: in test_manager_rejects_invalid_question_without_durable_changes
    with pytest.raises(RuntimeErrorBase, match="^invalid_human_request$"):
         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E   Failed: DID NOT RAISE RuntimeErrorBase
_____ test_manager_rejects_invalid_question_without_durable_changes[null] _____
tests\test_human_input_boundary.py:101: in test_manager_rejects_invalid_question_without_durable_changes
    manager.request_human(claimed.session_id, ("test-worker", claimed.fencing_token), question,
investigation\manager.py:443: in request_human
    request = HumanRequest(
E   pydantic_core._pydantic_core.ValidationError: 1 validation error for HumanRequest
E   question
E     Input should be a valid string [type=string_type]
E       For further information visit https://errors.pydantic.dev/2.13/v/string_type
___ test_manager_rejects_invalid_question_without_durable_changes[integer] ____
tests\test_human_input_boundary.py:101: in test_manager_rejects_invalid_question_without_durable_changes
    manager.request_human(claimed.session_id, ("test-worker", claimed.fencing_token), question,
investigation\manager.py:443: in request_human
    request = HumanRequest(
E   pydantic_core._pydantic_core.ValidationError: 1 validation error for HumanRequest
E   question
E     Input should be a valid string [type=string_type]
E       For further information visit https://errors.pydantic.dev/2.13/v/string_type
____ test_manager_rejects_invalid_question_without_durable_changes[object] ____
tests\test_human_input_boundary.py:101: in test_manager_rejects_invalid_question_without_durable_changes
    manager.request_human(claimed.session_id, ("test-worker", claimed.fencing_token), question,
investigation\manager.py:443: in request_human
    request = HumanRequest(
E   pydantic_core._pydantic_core.ValidationError: 1 validation error for HumanRequest
E   question
E     Input should be a valid string [type=string_type]
E       For further information visit https://errors.pydantic.dev/2.13/v/string_type
___ test_manager_rejects_invalid_question_without_durable_changes[too-long] ___
tests\test_human_input_boundary.py:101: in test_manager_rejects_invalid_question_without_durable_changes
    manager.request_human(claimed.session_id, ("test-worker", claimed.fencing_token), question,
investigation\manager.py:443: in request_human
    request = HumanRequest(
E   pydantic_core._pydantic_core.ValidationError: 1 validation error for HumanRequest
E   question
E     String should have at most 2000 characters [type=string_too_long]
E       For further information visit https://errors.pydantic.dev/2.13/v/string_too_long
_ test_scheduler_continues_with_other_incident_after_invalid_call[validation] _
tests\test_human_input_boundary.py:141: in test_scheduler_continues_with_other_incident_after_invalid_call
    assert ticks == 2
E   assert 1 == 2
------------------------------ Captured log call ------------------------------
ERROR    investigation.scheduler:scheduler.py:111 Runtime stopped; store consistency failure category=ValidationError
_ test_scheduler_continues_with_other_incident_after_invalid_call[json-decode] _
tests\test_human_input_boundary.py:141: in test_scheduler_continues_with_other_incident_after_invalid_call
    assert ticks == 2
E   assert 1 == 2
------------------------------ Captured log call ------------------------------
ERROR    investigation.scheduler:scheduler.py:111 Runtime stopped; store consistency failure category=JSONDecodeError
=========================== short test summary info ===========================
FAILED tests/test_human_input_boundary.py::test_invalid_human_input_is_a_controlled_failure[empty]
FAILED tests/test_human_input_boundary.py::test_invalid_human_input_is_a_controlled_failure[whitespace]
FAILED tests/test_human_input_boundary.py::test_invalid_human_input_is_a_controlled_failure[null-question]
FAILED tests/test_human_input_boundary.py::test_invalid_human_input_is_a_controlled_failure[integer-question]
FAILED tests/test_human_input_boundary.py::test_invalid_human_input_is_a_controlled_failure[boolean-question]
FAILED tests/test_human_input_boundary.py::test_invalid_human_input_is_a_controlled_failure[object-question]
FAILED tests/test_human_input_boundary.py::test_invalid_human_input_is_a_controlled_failure[array-question]
FAILED tests/test_human_input_boundary.py::test_invalid_human_input_is_a_controlled_failure[too-long]
FAILED tests/test_human_input_boundary.py::test_invalid_human_input_is_a_controlled_failure[invalid-json]
FAILED tests/test_human_input_boundary.py::test_invalid_human_input_is_a_controlled_failure[extra-field]
FAILED tests/test_human_input_boundary.py::test_invalid_human_input_is_a_controlled_failure[null-json]
FAILED tests/test_human_input_boundary.py::test_invalid_human_input_is_a_controlled_failure[integer-json]
FAILED tests/test_human_input_boundary.py::test_invalid_human_input_is_a_controlled_failure[array-json]
FAILED tests/test_human_input_boundary.py::test_manager_rejects_invalid_question_without_durable_changes[empty]
FAILED tests/test_human_input_boundary.py::test_manager_rejects_invalid_question_without_durable_changes[whitespace]
FAILED tests/test_human_input_boundary.py::test_manager_rejects_invalid_question_without_durable_changes[null]
FAILED tests/test_human_input_boundary.py::test_manager_rejects_invalid_question_without_durable_changes[integer]
FAILED tests/test_human_input_boundary.py::test_manager_rejects_invalid_question_without_durable_changes[object]
FAILED tests/test_human_input_boundary.py::test_manager_rejects_invalid_question_without_durable_changes[too-long]
FAILED tests/test_human_input_boundary.py::test_scheduler_continues_with_other_incident_after_invalid_call[validation]
FAILED tests/test_human_input_boundary.py::test_scheduler_continues_with_other_incident_after_invalid_call[json-decode]
21 failed, 11 passed in 16.42s
```

### 脱敏扩展长度的失败复现

```text
F                                                                        [100%]
================================== FAILURES ===================================
______________ test_redaction_expansion_is_a_controlled_failure _______________
tests\test_human_input_boundary.py:102: in test_redaction_expansion_is_a_controlled_failure
    result = Worker(manager, agent).run_once()
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
investigation\worker.py:66: in run_once
    self._run(session.session_id, claim, checkpoint)
investigation\worker.py:538: in _run
    self.manager.request_human(session_id, claim, human_questions[call.call_id], checkpoint)
investigation\manager.py:451: in request_human
    request = HumanRequest(
E   pydantic_core._pydantic_core.ValidationError: 1 validation error for HumanRequest
E   question
E     String should have at most 2000 characters [type=string_too_long]
E       For further information visit https://errors.pydantic.dev/2.13/v/string_too_long
=========================== short test summary info ===========================
FAILED tests/test_human_input_boundary.py::test_redaction_expansion_is_a_controlled_failure
1 failed in 5.40s
```

### 首次完整 pytest（入口隔离失败）

```text
......................................s................................. [ 11%]
..................F..........................................s.................................................. [ 28%]
........................................................................... [ 39%]
............................................................... [ 49%]
..................................................................... [ 59%]
............................................................. [ 69%]
................................................................................................. [ 84%]
..................................................... [ 92%]
...................................................                      [100%]
================================== FAILURES ===================================
_ IncidentRegressionTests.test_main_constructs_real_objects_and_enters_two_poll_cycles _
tests\test_incident_regression.py:243: in test_main_constructs_real_objects_and_enters_two_poll_cycles
    namespace = runpy.run_path(str(ROOT / "main.py"), run_name="__main__")
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
<frozen runpy>:287: in run_path
    ???
<frozen runpy>:98: in _run_module_code
    ???
<frozen runpy>:88: in _run_code
    ???
main.py:58: in <module>
    main()
main.py:36: in main
    incident_store = IncidentStore(Path(__file__).resolve().parent / "data" / "incidents.json")
                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
tests\conftest.py:32: in isolated
    raise AssertionError("tests_must_use_temporary_store_paths")
E   AssertionError: tests_must_use_temporary_store_paths
=========================== short test summary info ===========================
SKIPPED [1] tests\test_dependencies.py:28: clean-install check runs in CI/isolated venv
SKIPPED [1] tests\test_multi_signal.py:111: Set ALERT_RULES_PATH for optional cross-repository validation
FAILED tests/test_incident_regression.py::IncidentRegressionTests::test_main_constructs_real_objects_and_enters_two_poll_cycles
1 failed, 650 passed, 2 skipped, 190 subtests passed in 256.95s (0:04:16)
```

### 最终完整 pytest

```text
......................................s................................. [ 11%]
.............................................................s.................................................. [ 28%]
........................................................................... [ 39%]
............................................................... [ 49%]
..................................................................... [ 59%]
............................................................. [ 69%]
................................................................................................. [ 84%]
..................................................... [ 92%]
...................................................                      [100%]
=========================== short test summary info ===========================
SKIPPED [1] tests\test_dependencies.py:28: clean-install check runs in CI/isolated venv
SKIPPED [1] tests\test_multi_signal.py:111: Set ALERT_RULES_PATH for optional cross-repository validation
651 passed, 2 skipped, 190 subtests passed in 249.00s (0:04:08)
```

### 真实 HTTP 输出

```text
{"port": 8010, "checks": {"healthz": 200, "/docs": 200, "/openapi.json": 200, "/api/incidents?status=open": 422, "/api/incidents": 503, "/api/sessions": 503, "post": 405, "untrusted_host": 400}}
{"port": 8011, "checks": {"healthz": 200, "/docs": 200, "/openapi.json": 200, "/api/incidents?status=open": 422, "/api/incidents": 503, "/api/sessions": 503, "post": 405, "untrusted_host": 400}}
owned servers stopped; no store files created
```
