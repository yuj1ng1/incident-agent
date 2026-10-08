# Durable Agent Runtime 验收记录

本次按用户指定保留固定文件名。日期为北京时间 2026-10-08；下面明确区分实际测试与当前限制。

## 1. 起始状态与范围

主项目为 `yuj1ng1/incident-agent`，main，起始 HEAD `935e3434be7b402e7385f8a88f1220a05b411e7a`；远端 refs/heads/main 当时相同。Demo 为 `yuj1ng1/prometheus-demo`，codex/postgres-monitoring，HEAD `0e2f6f3220b9fb9f0baf8730de5ffaaac00d8c38`，远端当时相同。

开始时主项目已有 README/历史文档整理、main.py 的 dotenv 顺序、monitor 的日志文字、prompts/agent_prompt.py 和 temp_test.py 修改及未跟踪 AGENTS.md；暂存区为空。Demo 已有 README 修改。它们不是本次升级内容，提交时单独排除并保留工作区内容。没有 reset、checkout、force push 或删除业务数据。

实际使用 `D:\conda\envs\agentlearn\python.exe`，OpenAI SDK 3.14.0，SQLite 3.53.4。开始时原有 342 项测试通过。实际依赖关系为本地 Flask Demo、Prometheus、Loki、GitHub 只读取证，以及原有 PostgreSQL/Exporter Docker 服务；数据库与 exporter 在测试开始时已运行，未停止或删除。

## 2. 实现结果与三个里程碑

Durable Runtime 已建立严格 Session、SQLite 事务、状态机、累计预算、Checkpoint、过期 claim 恢复、带 fencing token 的执行权和公平调度。Human Interaction 已实现持久化 message/pause/resume/cancel/answer、CLI、HumanRequest 与 WAITING_HUMAN。集成阶段接入 Monitor、Agent 单次请求接口、原有 Planner 和审批存储，并验证实际模型与外部工具。

模块、SQLite 表、合法状态转换、六种 Checkpoint 恢复语义、CLI、时间策略及 Web 可复用应用接口详见 [使用指南](DURABLE_AGENT_RUNTIME_GUIDE.md)。新增生产依赖仅使用标准库；保留原来的 7/13 单次尝试预算、ServiceRegistry、AlertPolicy、多信号监控及固定修复动作。

## 3. 自动化测试

运行命令：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B -m unittest discover -s tests -q
```

最终整套结果在发布前再次以暂存快照验证，下列数量以实际输出为准：原有 342 项；本次新增 76 项；总计 418 项通过。`tests/test_runtime.py` 包括 6 个崩溃位置的 subTest；subTest 不冒充 6 个独立 test 数量。

| 类型 | 实际覆盖 |
|---|---|
| 模型/状态单元测试 | UTC、严格模型、非法转换、终止状态、预算、提问上限 |
| Mock 集成 | 多轮模型、失败模型/工具、no_data、消息顺序、人工消息中断未开始工具 |
| 真实 SQLite 事务 | 回滚、未知 schema、损坏不清空、CAS、claim 排他、fencing、Checkpoint 轮替 |
| SQLite 跨进程 | 独立 Worker 子进程运行时，另一个 CLI 进程持久化消息；第二执行权拒绝 |
| 恢复 | 6 次指定边界子进程 os._exit(23)、跨存储 JSON 成功但 SQLite 确认失败、孤立 Incident 对账 |
| 证据/缓存 | TTL、服务隔离、实例身份隔离、缓存/实际执行分计数、稳定 SHA 缓存重用 |
| 人类交互 | pause/resume/cancel、重复 ID、版本冲突、cancel 优先、回答重复/过期/重启、回答后仍暂停 |
| Remediation | 真实结构化门槛的 Mock 观测、独立 pending 提案、过期证据拒绝、存储失败不阻塞其他调查 |
| 旧能力回归 | 原有 IncidentStore、多服务、多信号、代码取证、人工审批/受控执行/验证/审计测试全部保留 |

测试没有终止未知 Python 进程。崩溃测试只让测试自行创建的 child 在指定边界退出。实际 SQLite 文件位于临时目录，不使用真实 Incident 数据做破坏性测试。

## 4. 真实外部工具与模型

验证命令为 `scripts/validate_durable_runtime.py --inject-faults --run-model`，后来使用 `--cases F` 和 `--cases E` 对发现的问题补验。程序实际启动受控 Demo，向业务接口发流量，等待真实 Prometheus 的 firing HighHttp5xxRate，再通过真实 Monitor 建立隔离 Session。没有把手工构造的数字称为实际 Prometheus 告警。

所有真实运行记录保存在 Git 忽略的 `data/runtime-validation/`，不提交数据库、Incident、Proposal、原始 trace 或日志。当前证据目录为：

- `20261008T115322Z/summary.json`：A、B、C、G 的完整真实运行。
- `20261008T120250Z/summary.json`：最初的 F，未生成 Proposal，作为失败记录保留。
- `20261008T120734Z/summary.json`：修正源码脱敏后 F 成功。
- `20261008T121012Z/summary.json`：E，受控真实 socket timeout 和消息介入。
- `response-compatibility.json`：独立 Responses 消息配对兼容性探测。

| 场景 | 实际结果 | 累计模型/工具执行 |
|---|---|---:|
| A 正常调查 | 真实 order-service HTTP 回归，Session COMPLETED，Checkpoint 与 Incident 投影已保存 | 5 / 13 |
| B 运行中发送方向消息 | “先检查数据库连接情况。”命令 applied，human_message_applied 有审计，原 Session COMPLETED | 6 / 13 |
| C 主动提问 | order-service 进入 WAITING_HUMAN；保存问题，未继续耗费模型；回答后原 Session COMPLETED，resume_count=1 | 等待时 1 / 1；完成时 5 / 13 |
| G 多任务 | order-service 仍 WAITING_HUMAN 时，inventory-service 在单 Worker 上 COMPLETED | inventory 4 / 12 |
| D 跨进程中断 | 六个边界分别退出并重启，保存的结果保留，未知返回未伪造，预算不下降 | SQLite + 确定性模型/工具 fixture，不冒充真实模型崩溃测试 |
| E 工具超时 + 消息 | 真实 GithubClient 指向本进程受控 loopback 超时端点，出现两次 github_unreachable；同一时段消息交付成功；inventory Session COMPLETED | 5 / 13 |
| F 审批边界 | 修正后真实 order-service 调查创建 1 个独立 pending Proposal；批准 0、动作 0 | 5 / 11 |

C/B/E 的本地命令由验收脚本模拟操作员提交，模型和外部工具是真实调用；不声称是用户本人现场输入或真实身份认证。E 的传输故障来自隔离的本地超时端点，不声称 GitHub 官方在当时发生了故障。inventory 未配置数据库依赖，消息不能替它扩大允许调查目标。

F 成功提案 ID 为 `rem-9c928c05c901424094218fb1ac65b176`，仅存在该隔离测试的 RemediationStore。没有 approve、execute 或真实修复动作。用户授权的是调查/验收，普通消息与答案均不能变成批准。

Prometheus、Loki、PostgreSQL 健康、运行版本和当前 SHA 源码至少在成功真实运行中各取得 ok 观测。E 开始时的独立源码探测返回 no_data，完整记录保留；没有把该次读源失败写成成功。CG 还观测到一次实际 github_unreachable，调查保留了证据缺口。

## 5. Responses 兼容性与实际失败

最初 32 token 的低额度探测只返回 reasoning、status=incomplete，没有当成成功。随后单独指定 function 的探测返回 HTTP 400，该失败发生在首个请求，因此不能据此认定完整链恢复不兼容。

改用普通工具选择后，真实 API 返回 `reasoning` 和 `function_call`；以实际 call_id 配对只读探测 output，并回传完整链后，第二次响应 completed 且有文本。A 的基本完整链被真实 API 接受；相关非法/缺失链由自动化测试证明回退 B。

生产默认仍采用 B。原因是崩溃窗口、取消未执行调用、过期观测和安全脱敏都可能使某个 Checkpoint 不适合完整复放。基本链兼容性不证明所有复杂 reasoning 历史都可精确恢复。文档依据：[OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling)、[DeepSeek 兼容性明细](https://api-docs.deepseek.com/zh-cn/guides/responses_api/)。

## 6. 发现并修正的问题

1. 新测试中 SQLite connection context 只提交事务、不关闭连接，Windows 清理目录失败。改为显式 closing，复验通过；不是数据库损坏。
2. 原有 IncidentStore 只有原子替换，不能防止 Monitor/Worker 的读改写竞争。新增共用 OS 锁和线程内重入控制，Monitor 只合并自己拥有的观测字段，避免旧对象丢弃新 Evidence。
3. 首次完整回归发现源码中的密码变量引用被泛化脱敏替换，AST 不再合法，真实 F 未生成 Proposal。改为保护源码语法并隐藏实际值，增加 AST/凭据回归，随后真实 F 形成 pending Proposal。没有放松原有 Planner 的证据条件。
4. 长调查的动态证据可能在提案前过期。新增有预算约束的候选补证：刷新运行身份和过期关键动态证据，预算不足则不产生不安全提案。
5. Remediation 存储临时失败不能使其他调查停止。新增带事件的投影待处理状态；Incident 结果已保存，过期提案证据不补造。
6. 验收脚本加入分场景执行时出现缩进错误；该次在解析阶段退出，没有启动服务。修正后 F/E 实际执行成功。

原有 temp_test.py 的未提交空白格式问题会使全工作区 diff --check 报告；它不属于本次内容，不修改也不提交。发布只检查本次暂存 diff。

## 7. 恢复、预算与安全结果

同一 Session 的双 claim 拒绝；过期 owner 的 fencing guard 拒绝写入。CLI 与 Worker 在不同进程访问同一个实际 SQLite。命令先提交，再按 cancel 优先/sequence 顺序处理；人类线索没有被升级为 Evidence。等待人类没有模型轮询。

模型 401/402/403 的 PAUSED/全局阻塞、429/503/连接失败的有限退避、非法工具拒绝、失败执行计费和多次 resume 不重置累计预算由故障测试覆盖。真实模型本次可用，没有实际耗尽余额后反复重试。累计工具预算包括 Runtime 的身份探测和提案补证。

跨 JSON/SQLite 不宣称原子事务。可靠 observation 与结果先落 SQLite，再按稳定 ID 合并旧 JSON，最后确认；部分成功可以对账。已解除告警的待执行任务取消，缺失 Session 的合法活动 Incident 会补回。原有 IncidentStore 和 RemediationStore 未强制迁移。

所有真实验收最后都启动关闭故障开关的健康实例，验证 /order=200、/inventory=200、开关 false，再停止此次拥有的 Demo child，恢复测试前端口未占用的状态。未关闭已有数据库/Exporter/Prometheus/Loki，未清除 Volume，未遗留故障开关。

## 8. 当前限制与未验证项

- 第一版仅本地单 Worker，未验证跨机器、网络文件系统或高吞吐部署；不提供公网 Web。
- D 的六种真实进程退出使用可重复 fixture，未在每个退出边界重复花费真实模型调用；它证明持久化和恢复语义，不能称为真实模型请求的 exactly-once。
- 外部 HTTP 采用有限 at-least-once。调用前预算预留可能保守多计；不承诺取消已发出请求。
- 没有实际执行 `disable_demo_regression`。原有执行/审批/过期/验证/去重的安全回归测试通过；真实 F 只验证提案与待审批边界。
- 没有做大数据库压力测试、系统断电/磁盘硬件故障试验，也没有自动按天删除审计。Checkpoint 轮替及 payload 上限已实现。
- 审计脱敏不是任意自由文本秘密识别的形式化证明；actor 只是本地记录。未来 Web 需要独立认证授权。
- 单次 SDK timeout 和 lease 是运行约束，不承诺第三方接口的硬实时终止；没有通过杀未知进程实现中断。

本次不添加新权限、不修改服务配置/阈值、不改变已有修复白名单。未来 Web 直接复用 Manager 应用服务和 RuntimeStore 查询，不通过 CLI 子进程操作业务。

## 9. 发布记录

发布流程为：只暂存本次文件及重叠文件的本次内容 → 敏感/运行数据检查 → 暂存快照完整测试 → 普通提交 → 原有 main 分支普通推送 → 核对本地/远端 SHA。

具体 commit SHA 在最终交付回复中给出，避免让文档嵌入自身提交 SHA。主项目之外的 Demo 代码未修改、未提交、未推送；原有用户工作区修改保留。
