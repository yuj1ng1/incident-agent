# 多服务升级验证记录（2026-10-08，Asia/Shanghai）

## 架构和配置

Incident → Agent → 原名称 Tool → ServiceRegistry → ServiceProfile → provider/profile → Client。
服务唯一配置入口为 services/configuration.py 的 SERVICES。注册同类型服务无需改 Tool。
Pydantic Profile 字段：name、type、health、logs、metrics、runtime、source、dependencies、remediation。
Registry 校验重复服务、必要字段、provider、URL、GitHub repo、metric profile 以及依赖引用；
初始化失败早于 Incident 调查。Profile 中数据库凭据仅为 password_env，禁止密码字面值。

旧 SERVICE_URLS、SERVICE_DEPENDENCIES、DEPENDENCIES、MONITORING_TARGETS、CODE_REPOSITORIES
保留为只读派生视图，不再各自维护配置。DEPENDENCIES 的 password 改为 password_env；
所有调查 Tools 直接读取 Registry。为保持本机 DB 探测可用，在被忽略的 .env 追加了
ORDER_DB_PASSWORD（取自现有 Demo DB 配置，未改写已有键，也未纳入提交）。

get_service_capabilities 显式构造 capabilities、providers、missing_capabilities、supported_metrics、
dependencies 和 remediation。只输出安全字段，不序列化 Profile、不输出地址、凭据或环境。
dependencies 查询能力总是存在；空列表仅表示配置中没有登记依赖。

| 能力 | 实现 |
| --- | --- |
| health | provider=http 调配置 endpoint；postgresql 保留 DNS/TCP/DB connect/SELECT 1 |
| logs | labels 生成转义后的 LogQL；模型只传 service，不接受 LogQL |
| metrics | http_service/postgresql profile 的可信模板；job/database 或 HTTP labels 来自配置 |
| source | provider=github；repo/branch 来自 Profile；保留路径/ref/SHA 和 diff/source 上限 |
| runtime | 配置 http_version 地址和 endpoint；校验返回服务名、完整 SHA、UTC 时间以及各自布尔开关 |
| dependencies | Profile 中名称列表；引用须已注册；服务健康和依赖健康 Tool 仍独立 |
| remediation | 只声明，不注册 Tool；执行策略继续固定 order-service/disable_demo_regression |

inventory-service 的 capabilities 为 health/logs/metrics/dependencies/runtime/source，
dependencies=[]、remediation=[]。runtime_flags 只含 simulate_inventory_error。
order-db 的 capabilities 为 health/metrics/dependencies，没有伪造日志、源码或运行版本能力。

## 测试结果

- 修改前实际运行 Agent 全部 210 项测试通过。
- 新增 Agent 测试 67 项，合计 277 项通过；原有测试没有删除。
- 旧 metric 测试仅迁移配置注入，并将不完整配置测试改为初始化失败；PromQL、数值、
  标签转义、多实例拒绝、NaN/Infinity、有限零等原断言保留。
- 冻结 Git index 候选单独运行 277 项，也全部通过，未依赖未提交的 Prompt 预算规则。
- Demo 新增 7 项，连原有 7 项合计 14 项通过；两个仓库总计 291 项，新增加 74 项。
- test-service 仅在测试配置定义，health/logs/metrics/dependencies/capabilities 五个原 Tool
  全部成功；无 source/runtime 时返回证据缺口。这是配置驱动的核心验收。
- Agent Tool Registry 恰为 10 个只读工具（原有 9 个加能力发现）；provider/helper/action 均未注册。
- 调查 Tools 和 Registry 的 AST 比较及文本检查没有具体 order-service/order-db/inventory-service 分支。
- Prometheus 配置/告警由 promtool 校验成功，Alloy validate 成功；候选 diff 检查及凭据扫描通过。

## 真实集成

完整故障调查使用 Demo commit：e1850c061c83cd89668bf9176363c0323959da22。
可读性整理后的最终 Demo commit：75f4137ab0c9052f5855a01cea4acab151b8e58b，已推送 codex/postgres-monitoring。
两个启动脚本以实际 HEAD 提供运行 SHA；运行源码干净（原 README 本地修改保留）。
现有 Prometheus、Loki、Alloy 容器实际启动并重启加载新配置，数据库和 exporter 保持运行。
联调时还发现一个编辑器启动的旧 order Demo，没有运行 SHA；核对其 cwd/exe/argv 后停止，
使已提交版本成为实际被探测服务。没有停止其他程序。

| 对象 | 实测结果 |
| --- | --- |
| order-service 正常 | /health、/order、/metrics、/version 200；正确运行 SHA；Prometheus 和 Loki 可读 |
| order-db | DNS/TCP/连接/SELECT 1 全通过；pg_up=1；八项 PostgreSQL 指标全为有效有限数值 |
| inventory 正常 | /health、/inventory、/metrics、/version 200；http_request_rate 可读，http_5xx_rate=0；Loki stream label 正确 |
| order 故障 | /order 500、application_error/RuntimeError；/health 200；真实 Agent 完成代码因果调查 |
| inventory 故障 | /inventory 500、application_error/RuntimeError；/health 200；两服务高 500 占比告警均真实 firing |
| inventory Agent | Incident inc-bc7c1364；先 capabilities，10 次实际 Tool 执行，源码/diff/runtime/logs/metrics 因果链完整 |
| 数据库调查隔离 | inventory 调用中 check_dependency_health、order-db、postgres_* 均为 0 次；没有修复提案 |
| order Agent | Incident inc-600014d7；首轮 13 次调用；当前源码读取一次 GitHub 超时，明确证据缺口且 Planner 未提案 |
| order 补证 | 再次真实 GitHub 读取当前 app.py 成功；真实模型复核 completed；完整现场证据生成 pending 提案 |
| 审批执行 | 用户要求的 smoke 使用独立 CLI 审批，approved_by=本机用户 zp1222；固定执行器真实重启 order Demo |
| 恢复验证 | 提案 rem-e65d1a36706c499b9ff1439de1260dc0 为 succeeded；同 SHA、开关 false、/order 200、/health 200 |
| Audit | proposal_created → approved → execution_started → action_succeeded → verification_succeeded → remediation_succeeded |

Loki 原通配路径会把所有日志标为 order-service。已改成 app.log 和 inventory.log 两条
明确路径；重启 Alloy 后的新日志标签正确。早先产生的错误标签历史日志没有被删除，
调查取最近故障日志时已验证各服务标签与内容一致。

Smoke 后 inventory 以正常模式重启，order 由白名单动作恢复；两个开关均为 false，
业务接口均返回 200。独立恢复检查和最终告警状态保存在本机被忽略的 data/upgrade-verification。
真实 Prometheus 告警、Incident、Tool trace、模型调查结论、Remediation/Audit 均存该目录，未提交。

## 保留的边界和局限

原 Agent 循环/缓存/Evidence、MAX_AGENT_LOOPS=7、MAX_TOOL_CALLS=13、每轮预算提示没有改变。
RemediationPlanner、DemoRuntime 的服务名、固定路径/端口和 Action Policy 继续保留，
它们属于特定执行策略。Demo app 和默认配置也必然包含服务名；调查通用逻辑没有具体服务分支。

http_5xx_rate 继续原语义，只统计 status=500，并非全部 5xx。order 正常模式启动后若从未产生
500 样本，该比率会返回 no_data；没有把缺失数据伪装为零。inventory 初始化 200/500 计数零样本。
指标和告警是一分钟窗口，HTTP 恢复后需要等待窗口/告警评估恢复；health=200 不等于业务正常。
配置能力不等于后端可达，空依赖不排除未登记依赖。运行 commit 仍是实例自报信息，不能推广为
任意第三方服务的 CI/CD 部署认证或多副本一致性证明。当前各 provider 使用项目统一 Loki/Prometheus
地址；新增不同监控集群或新协议需要相应配置/adapter 扩展，本次未引入大型插件系统。

接入第三方 HTTP 服务：追加 SERVICES → 接通 Loki 标签和 Prometheus scrape → 选择匹配的指标
profile → 按实际接口填写 health/runtime（没有就省略）→ 配置 GitHub source/依赖 → 运行
capability 和只读工具 → 用真实告警验证。指标命名不同则新增可信 metric profile；新协议则添加
Profile 校验和 provider adapter；新增同类服务本身不改 Tools。

## 提交范围

原有本地 README、main.py、monitor 日志字段、temp_test、Prompt 预算规则、根报告删除和 docs
归档都保留。Prompt 文件仅把本次多服务规则放入 Git index，原有第 52 条仍在工作区。
未使用 reset/checkout/force push；未提交 .env、凭据、日志、Incident/Remediation JSON 或测试运行数据。

## 用户追加的可读性整理

仅整理本次新增文件与已修改方法，没有格式化整个仓库。多字段字典一行一个字段、
三个及以上参数的调用展开，长条件按逻辑分支换行；原来已展开的监控响应读取保留换行。
19 个相关 Python 文件通过语法树语义对比；唯一的结构性整理是把 HTTP 错误标签字典
从 f-string 内部提为局部 error_labels，代回后语法树与原表达式相同，PromQL 断言保持不变。
作用域检查确认不存在多个 dict 字段共占一行，也没有三个及以上参数的单行调用。
整理后 Agent 277 项、Demo 14 项测试全部重新通过；测试断言语义未变。

用户追加要求到达时，Agent 仍未提交，但 Demo 的 e1850c0 已为真实源码取证推送。
因此 Agent 的升级和可读性整理合为一个最终提交；Demo 追加 75f4137 普通提交，
没有通过 force push 改写已推送历史。格式整理后的版本重新启动两个正常模式服务，
并核对 /health、业务接口、/version、监控采集与当前 HEAD。完整模型故障调查无需重复，
其适用性由语义对比、全部回归测试及最终运行冒烟共同验证。
