# W3 真实人工浏览器审批操作指南

状态：**自动化通过，真实人工验收待完成。** 本指南未记录任何自然人已批准恢复。演示操作身份由后端 Bearer 配置确定；一个人使用三个角色，只能证明该人参与，不能证明三个自然人参与。

## 1. 启动一个全新的隔离实验

在 PowerShell 运行：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B 'D:\Incident Agent\scripts\w3_manual.py'
```

使用已有 Python、Node 和 frontend/node_modules，不安装依赖，不调用模型。启动器复用现有 W2 Runtime、Store、ActionExecutor 和 FastAPI，启动本机 Vite。默认端口 5173、18330、18430；端口占用时应停止本次启动，不要结束占用端口的未知进程。

启动输出会给出本次 `experiment`、`credentials_file` 和 `stop_file` 的精确路径。每次都建立新的 TEMP 目录、实例、制品绑定和临时凭据，不复用已审批的历史提案。

工作台：[隔离原修复](http://127.0.0.1:5173/remediations/rem-w2-deploy-b)。API：[提案列表](http://127.0.0.1:18330/api/remediations)。独立应用：[库存](http://127.0.0.1:18430/inventory)。

启动器只自动布置 A→B：A 基线独立验证通过，B 库存返回 500、health 返回 200。该布置审批身份明确叫 `w3-automated-setup-approver`，执行身份叫 `w3-automated-setup-executor`，属于自动化测试准备。**不会自动创建、批准或执行恢复提案。**

准备好阅读本指南后再启动。B 的动作后业务验证开始计算最长 300 秒证据窗口；申请、批准、执行需要在窗口内完成。超时后请结束并重新创建实验，不能修改时间戳或延长有效期。

如只需后端，可单独使用既有入口：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B 'D:\Incident Agent\scripts\w2_demo.py' --manual-approval --port 18330 --app-port 18430
```

该命令不启动前端。完整人工操作推荐使用第一个命令。

## 2. 获取本轮三个演示操作身份

在本机打开启动输出中 `credentials_file` 指向的 `operators.json`。其中键 `requester`、`approver`、`executor` 对应三个随机临时凭据。把对应值粘贴到页面“本地操作凭据”，点击“连接操作身份”。页面显示的身份应分别为：

| 凭据键 | 后端操作身份 | 权限 |
|---|---|---|
| requester | w3-requester | request |
| approver | w3-approver | approve |
| executor | w3-executor | execute |

Token 仅在当前页面内存中使用，通过 Authorization 请求头发送。不要把 Token 放到网址、申请理由、截图、报告或聊天中。刷新后需要重新输入。切换身份先点“断开操作身份”，再连接下一身份；旧确认框、理由、操作标识会清空，旧身份的迟到响应不会作为新身份的操作成功展示。

这些是技术身份。请在验收记录中另行如实注明实际操作者、采用的角色以及浏览器操作时间，不要把角色名当作自然人身份认证。

## 3. Requester 发起申请

1. 在原修复页确认顶部“独立验证未通过”，原动作是 `w2_inventory_deploy_b`，库存业务检查 HTTP 500，健康检查 HTTP 200。
2. 连接 requester 凭据，确认显示 `w3-requester` / request。
3. 查看 A 候选目标、制品 SHA-256、配置 SHA-256、原验证时间和到期时间。
4. 在理由中说明申请原因，例如“隔离 B 的库存业务明确失败，申请恢复至本轮实测正常的 A”。点击“申请回滚”一次。
5. 预期：申请记录出现，关联补偿提案为“待审批”；点击关联补偿提案进入详情。尚无补偿动作或补偿后业务验证。

Requester 无审批、执行权限。即使服务端配置给同一个身份全部权限，后端仍禁止其审批或执行自己的补偿。

## 4. Approver 由实际人员核查并明确决定

在补偿页断开 Requester，再连接 approver 凭据。确认显示 `w3-approver` / approve，原确认框没有保留。

批准前核查：

- 原修复关联、内容、动作与原提案摘要；必要时打开原修复链接查看完整动作和验证记录。
- 申请理由与申请身份；原库存业务确实失败，health=200 不代表业务正常。
- 源制品 B、目标制品 A；两者共用源码 `0e2f6f3220b9fb9f0baf8730de5ffaaac00d8c38`，差别为明确的库存错误配置。
- 目标制品完整 SHA-256、配置 SHA-256、当前精确提案摘要；公开页面隐藏路径和完整启动信息，后端摘要仍绑定全部可信原始参数。
- 中风险、固定执行动作、停止当前隔离 B 并启动 A 的副作用；展开“固定配置摘要（审批绑定）”，核查短暂服务中断风险。
- 数据兼容条件为无数据库、无持久业务数据；本实验不能证明生产数据库迁移兼容。
- 证据记录时间、到期时间和当前有效性；执行前后端还会重查实例、制品和证据。

只有本人决定批准时，才勾选“确认已核对本次审批的精确目标和参数”，点击“批准此提案”。**此操作必须由实际人员在浏览器完成，Codex 不能代点。**

预期：状态“已批准”；批准人为 `w3-approver`，批准时间、摘要一致状态及 approved 审计事件可见。approved 审计含 proposal_digest、command_id、authorization_result。

如决定拒绝，填写理由，勾选确认后点击“拒绝此提案”。预期“已拒绝”，保留拒绝身份、时间、理由和摘要；执行被阻断。拒绝分支不能同时作为恢复成功分支验收，需要另建全新实验测试通过路径。

## 5. Executor 使用独立执行权限

1. 断开 Approver，连接 executor，确认 `w3-executor` / execute；旧确认必须清空。
2. 核对提案为“已批准”，审批摘要一致，目标仍为刚才批准的 A，证据仍有效。
3. 勾选“确认执行已批准的精确目标和参数”，点击“执行已批准动作”一次。
4. 等待结果并核查动作与独立业务验证。正常分支为“流程已完成”、动作“确认已执行”、独立验证 passed，实际启动制品 A。

服务端会阻止 Approver 使用同一操作身份执行自己批准的补偿，也会阻止 Requester 执行自己的补偿。浏览器显示或隐藏按钮不能代替这些检查。

相同 command_id 重复提交不会重放；已成功提案的再次执行也不会新增动作或验证。不要用反复点击判断执行结果；超时、进程崩溃或结果未知时，请刷新核查，禁止盲目重试。

## 6. 独立业务验证与审计

在另一个 PowerShell 窗口运行：

```powershell
Invoke-RestMethod 'http://127.0.0.1:18430/inventory' | ConvertTo-Json -Depth 5
Invoke-RestMethod 'http://127.0.0.1:18430/version' | ConvertTo-Json -Depth 5
```

库存必须为 `service=inventory-service`，`items=[{item:demo-item, quantity:10}]`；version 必须为 A，制品、配置、实例和 PID 与本轮记录相符。HTTP 200 / health OK 单独不能证明恢复。

Web 查看原提案的申请评估、关联补偿、审批记录、动作、独立验证与审计时间线。补偿页顶部保留实际提案 ID，可以在 `http://127.0.0.1:18330/api/remediations/` 后追加该 ID 查看公开 JSON。完整可信参数和原始审计位于本次 experiment/remediation.json，不应手工编辑。

验收后记录：实际人员与角色、是否由本人在浏览器批准或拒绝、提案 ID、批准摘要与时间、独立库存结果、实验目录。`manual-acceptance.json` 默认 pending，不会根据一次 API 成功自动改写为“真实人工通过”。后续结合你的明确确认和审计证据生成新验收记录。

## 7. 证据过期、凭据轮换与结束

证据超过 300 秒，页面会显示过期原因和到期时间，禁止批准/执行；后端仍检查实际时间。`inspect-execution` 是当前观测，不替换原证据、不刷新旧审批。不要编辑 Store、时间、参数、哈希、审批状态或运行目录来继续旧实验。

需要重做时，在启动终端 Ctrl+C；或在另一个终端创建启动输出精确指定的 STOP 文件。启动器会核验并关闭本次拥有的应用、API 和前端进程，保留证据目录。不要批量结束 Python/Node 或结束未知 PID。确认本次进程退出后，再运行启动命令，使用新目录和新 Token。

WebAuthorization 在创建应用时加载配置，不支持即时凭据撤销。修改环境配置或 operators.json 不会改变正在运行的授权对象。受控停止并启动新实验后，新凭据生效，旧 Token 被拒绝。旧 Token 文件留在本机 TEMP，但没有活动的本轮 API 继续接受它。

如动作已成功而业务验证失败，应记为“独立验证未通过”；如执行结果未知，不得把收口或健康检查当作历史动作成功。保留原证据并人工调查。
