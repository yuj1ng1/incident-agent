# W2 隔离真实 Demo 恢复：本轮实现与验收

本轮复核已有未提交 W2 实现，补强后重新真实构建、运行并验证 A→B→A。建立的是 Windows x64 单机、单实例、无数据库的隔离恢复能力，不能据此宣称历史生产版本可回滚。分离权限的临时 API 测试身份完成审批链验证，没有冒充真实人工签字；未调用真实模型接口。

## 1. 仓库基线与目标选择

- 主仓库 main，开始时 HEAD 为 3498b1e82c7e028364181ed3ca36d3020df9cbff，与实际远端 main 一致；已读取根 AGENTS.md，无更深层指令文件。
- Demo 分支 codex/postgres-monitoring，HEAD 和实际远端均为 0e2f6f3220b9fb9f0baf8730de5ffaaac00d8c38；无 AGENTS.md。
- 工作区已有未提交 W2 源码、历史报告和截图；另有文档整理、README、prompts/agent_prompt.py、temp_test.py 等修改。没有 reset、clean、stash 或覆盖无关工作。
- 选择 inventory-service：inventory_app.py、http_observability.py 为静态库存 HTTP 服务，不依赖数据库或持久业务数据。原入口固定 8002，原启动脚本不能指定隔离端口；原 /version 环境 SHA 只能声明来源，不能认证部署。
- 订单服务依赖 PostgreSQL，未选择它。历史 Git commit 只作源码来源，A 经本轮真实运行及业务断言才成为恢复目标。未运行原服务脚本、连接现有数据库、操作 Docker、改动监控系统或使用监控快照作回滚包。

## 2. A/B 来源与真实制品

A/B 共用完整 Git SHA：0e2f6f3220b9fb9f0baf8730de5ffaaac00d8c38。B 是相同源码的 simulate_inventory_error=true 受控配置制品，A 为 false，不是两个不同 Git 提交。

| 版本 | 实际用途 | 完整 SHA-256 |
| --- | --- | --- |
| A | 本轮实测正常的恢复目标 | 3654279c9ba7276d1e73b7e976301493738a35865b1715a9ef7c7c7b8fdd7635 |
| B | 受控库存业务错误配置 | 461f9075ec3ae4b7bd8bf74e1fc42e0323e2a6debc701d360c587f10168057b7 |

制品真实包含精确 Git blob、固定 launch.py、release manifest、第三方依赖文件和哈希，绑定解释器绝对路径及 SHA-256、依赖版本、配置及所有文件哈希。构建只读 Git 和现有 Python 环境，没有新依赖安装或联网。专项测试在两处独立输出目录重建后核对 A/B 字节一致；本轮两次真实 API 实验的制品哈希也一致。

原始 A 绝对来源：C:\Users\zp1222\AppData\Local\Temp\incident-w2-current-42ff9ad64cc147efa4baf6b56942c9eb\final-acceptance\artifacts\inventory-A-3654279c9ba7276d1e73b7e976301493738a35865b1715a9ef7c7c7b8fdd7635.zip。见[归档制品清单](../data/w2-acceptance/2026-10-24_14-35-14/final-acceptance/artifacts/build.json)。运行使用固定解释器和 -I -S -B，依赖从 vendor 加载，应用仅监听 127.0.0.1:18241，日志只写本次 TEMP。

内容哈希提供精确身份，本地文件不是 WORM；篡改会在执行前被拒绝。未声称制品签名、防管理员篡改或完整供应链认证。

## 3. 真实恢复与业务证据

最终实例 w2-4c8cf7682477f0937fed8bcd65fe906e；目录 C:\Users\zp1222\AppData\Local\Temp\incident-w2-current-42ff9ad64cc147efa4baf6b56942c9eb\final-acceptance；API 127.0.0.1:18240。

| 阶段 | PID | 版本 / 健康 / 库存 HTTP | 独立结果 |
| --- | --- | --- | --- |
| A 基线 | 6380 | 200 / 200 / 200 | 库存精确为 demo-item:10，06:25:45.813627Z 验证通过 |
| 部署 B | 20832 | 200 / 200 / 500 | 动作已执行，业务明确失败，verification_failed |
| 恢复 A | 22368 | 200 / 200 / 200 | 真实重新解包并启动新进程，06:26:40Z 验证通过，succeeded |

时间均为 2026-10-24 UTC。B 的 /health=200 未被当成业务恢复。恢复后又在执行器之外独立读取三个接口，确认 A 的哈希、实例、PID 及精确业务对象，没有改写原始动作或验证记录。

证据：[A 基线](../data/w2-acceptance/2026-10-24_14-35-14/final-acceptance/baseline-A.json)、[完整 Store](../data/w2-acceptance/2026-10-24_14-35-14/final-acceptance/remediation.json)、[真实 HTTP 审批/执行](../data/w2-acceptance/2026-10-24_14-35-14/final-acceptance/api-acceptance.json)、[恢复后额外 HTTP 观测](../data/w2-acceptance/2026-10-24_14-35-14/final-http-observation.json)。

## 4. 固定契约及修改文件

复用 CompensationContract、CompensationTarget、RemediationStore、ActionExecutor、审批摘要、幂等命令、执行锁、独立验证器接口、Web API 与 W1 React 详情页，没有新增项目依赖或第二套审批、Store、Web 系统。

- [isolated_demo.py](../remediation/isolated_demo.py)：动作 w2_inventory_deploy_b、补偿 w2_inventory_restore_a；契约 isolated-inventory-recovery-v1，目标 isolated-inventory-A。绑定原动作、源身份、源/目标 A/B 与 SHA、绝对制品来源、完整性、解释器、固定参数、配置摘要、隔离实例、前置条件、副作用、无持久数据的兼容条件及业务断言。
- [构建器](../scripts/w2_demo_build.py)、[制品入口](../scripts/w2_inventory_entry.py)、[显式实验入口](../scripts/w2_demo.py)、[API 验收驱动](../scripts/w2_demo_acceptance.py)：仅显式启动的新 TEMP 实验注册 handler。普通导入、日常 Web 不注册 W2，不自动重放旧实验。--review 查看已有临时记录时关闭写权限。
- [executor.py](../remediation/executor.py)：保留原执行链，补充前置条件阻断原因码，避免公开任意异常原文。
- [公开模型](../web_api/schemas.py)、[白名单投影](../web_api/remediation_views.py)：可选 DemoRecoveryView、源/目标制品、SHA、实际进程和验证原因。浏览器不能提供任意执行路径、PID 或 Shell 命令。
- [前端类型](../frontend/src/api/types.ts)、[原因文案](../frontend/src/lib/remediation.ts)、[现有详情页](../frontend/src/pages/RemediationPages.tsx)：最小展示增量，无重设计。
- [W2 测试](../tests/test_w2_demo.py)、[真实崩溃 worker](../tests/w2_crash_worker.py)、[补偿回归](../tests/test_compensation.py)、[前端测试](../frontend/tests/remediation.test.tsx)。

本轮补强：缺少完整进程身份快照时拒绝终止；超时保留 timeout，身份/完整性不匹配保留 mismatch，无数据保持 no_data；公开安全原因码；明确源/目标 A/B。原 W2 文件在 TEMP 备份。

## 5. 必须场景的结果

| 场景 | 本轮验证 |
| --- | --- |
| 制品正确 | 真实 A→B→A，新进程通过独立 HTTP 库存断言 |
| 制品缺失 | demo_artifact_missing，not_executed，B 未停止；浏览器实际展示阻断 |
| 哈希不符 | 停止 B 前阻断，保留原进程 |
| 服务身份变化 | 实际重启隔离 B 后，原审批身份失效；另覆盖部署文件变化 |
| 未审批 | 真实 API 409；requester 越权审批 403 |
| 审批后参数/目标改变 | 制品路径、目标哈希、源配置身份篡改被拒；错误摘要真实 API 409 |
| 重复命令 | 同一 command_id 不新增动作、验证或物理替换 |
| 执行崩溃 | 四个真实 os._exit 窗口保持 executing，恢复时拒绝盲目重放 |
| 动作成功但验证未通过 | B 的真实业务 500 不标恢复；另在 A 替换后、验证前停止 A，no_data / verification_failed |
| Web 完整链路 | 实际查看原动作、补偿、审批、执行、独立验证、审计及制品缺失阻断 |

崩溃窗口：executing 提交后、B 停止后 A 启动前、handler 返回后动作结果提交前、动作结果提交后。只有已持久化 ActionResult 才说明执行发生，其余保持 unknown；即使 A 实际已启动，也不补写历史成功。不自动重放、不重新开启错误配置、不按裸 PID 清理。

## 6. 测试分类及结果

- 最终 W2：17 passed，[日志](../data/w2-acceptance/2026-10-24_14-35-14/w2-tests-final.log)。含真实本地进程、HTTP、执行器崩溃。超时与身份分类另用异常注入，没有冒充实际网络超时。
- 相关回归：202 passed、81 subtests passed，[日志](../data/w2-acceptance/2026-10-24_14-35-14/regression-final.log)。只运行 test_w1_web_demo、test_compensation、test_web_remediation、test_remediation、test_remediation_recovery。
- 前端：22 passed，[单独重跑日志](../data/w2-acceptance/2026-10-24_14-35-14/frontend-tests-rerun.log)；TypeScript 和 Vite 构建通过，[日志](../data/w2-acceptance/2026-10-24_14-35-14/frontend-build-final.log)。产物使用 TEMP，没有覆盖原 dist。
- 一次并行运行中前端等待断言失败，相同代码单独重跑通过，未放宽断言或超时。原因未独立确认，[失败日志原样保留](../data/w2-acceptance/2026-10-24_14-35-14/frontend-tests-final.log)。
- W1 A–E 仍是合成夹具；Mock、异常注入、临时审批身份只证明各自代码边界，不等于生产联调、真实人工批准或模型调用。未运行无关数千项测试。

## 7. 本轮 Web 实际验收

内置浏览器初次连接失败，重建会话后恢复，随后实际完成验收。Windows 窗口控制曾因 URL 核验策略被停止，没有继续该 Windows 控制路径，也没有用历史截图代替本轮验收。

实际查看：恢复成功 B→A 页、原动作 A→B 的库存 HTTP 500 页、制品缺失阻断页。阻断页以只读方式加载专项测试真实保存记录，不再次执行动作。页面显示源/目标、完整 SHA、绝对来源、实例/PID、独立业务、审批、原动作关联和审计。

桌面 1440px 的 document 宽 1425px，窄屏 390px 的 document 宽 375px，无横向溢出；Enter 可展开、收起固定配置，结束后重置 viewport。观察到一条 Vite 开发 WebSocket 连接错误；上述数据展示仍完成，未声称该错误已修复。见[浏览器记录](../data/w2-acceptance/2026-10-24_14-35-14/web/browser-acceptance.json)及同目录 DOM。

截图：[恢复桌面](assets/w2-web-20261024/recovery-desktop.png)、[390px 窄屏](assets/w2-web-20261024/recovery-narrow.png)、[原业务失败](assets/w2-web-20261024/original-business-failure.png)、[制品缺失阻断](assets/w2-web-20261024/artifact-missing.png)。

证据只说明记录时的观测，过期不能冒充当前健康。本轮应用已按要求停止。

## 8. 清理、工作区保护与交付

- 最终 A/B/A 的 6380、20832、22368 均停止，[cleanup](../data/w2-acceptance/2026-10-24_14-35-14/final-acceptance/cleanup.json)无错误；两次真实验收、专项与崩溃测试按所属实例清理。
- 两个实验 API、只读 review、Vite 均停止。Vite 在核对 OS 创建身份、cwd、exe、argv 和本轮创建时间后终止；未批量结束 Python/Node 或终止未知 PID。
- 核对 74 个唯一应用进程身份，无活动本轮实例；5173、18240、18241 无监听。OpenProcess 失败保留为 closed_or_unavailable，不伪称所有不可打开 PID 均经独立退出确认。
- 基线主仓库 7,411 个文件、Demo 79 个文件。除 8 个明确授权的 W2 补强文件，其余基线内容一致，非 W2 变化为 0；Demo 79 个文件全部一致。原删除状态、未跟踪文件、历史报告保留。Git 元数据、依赖目录、可再生 Python/pytest 缓存不在清单内。
- [保护和进程审计](../data/w2-acceptance/2026-10-24_14-35-14/final-audit.json)、[内容基线](../data/w2-acceptance/2026-10-24_14-35-14/baseline.json)。文档整理、README、提示词和 temp_test.py 不并入 W2 提交。
- 新归档 247 个文件及[SHA-256 清单](../data/w2-acceptance/2026-10-24_14-35-14/sha256.json)，包含成功、失败、崩溃、HTTP、审批、清理和浏览器原始证据。未归档 operators.json 凭据；运行数据只留 TEMP 和 Git 忽略的新 data/w2-acceptance 目录。
- 历史 W2 报告/截图原样保留；本文是新验收快照。提交推送按用户最后的明确授权执行，最终 SHA 和真实远端核对见交付回复。

## 9. 剩余风险和下一步

仅覆盖受信任本地构建环境中的 Windows x64 固定 Demo，不支持数据库迁移、历史生产版本认证、生产流量、容器/集群、多副本或完整 OS 隔离。解释器文件及第三方包有哈希，标准库、系统组件和构建机仍属信任边界；没有制品签名或 WORM。

停止 B 与启动 A 之间有停机窗口；启动失败保持 unknown，需人工核查。执行器崩溃可能留下子进程，不能自动重放或按裸 PID 清理；本轮仅证明四个列明窗口，没有穷举 OS/磁盘故障。

下一步先独立审查固定契约、证据与权限，补齐真实人工审批，再讨论签名、持久业务数据兼容策略和部署控制器。不得默认注册至日常生产环境。
