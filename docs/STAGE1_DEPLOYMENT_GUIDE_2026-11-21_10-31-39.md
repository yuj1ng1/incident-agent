# 第一阶段部署与复跑

## 从源码启动

使用支持 Compose V2 的 Docker，并将引擎切换到 Linux 容器。在包含 compose.yaml 的项目根目录执行。

```powershell
docker compose up --build -d --wait --wait-timeout 180
```

打开 [调查工作台](http://127.0.0.1:8088)，订单接口位于 [订单读取](http://127.0.0.1:18001/order)。首次启动会创建独立卷，启动 MySQL 8.4.13，并通过现有 Alembic 链升级到 0005_remediation_store。Runtime、Incident、归档、Remediation 共用该 MySQL schema。

默认使用 demo_replay。模型响应由脚本回放提供，Prometheus、Loki、Alloy、订单服务和 PostgreSQL 实际运行。回放会读取真实的工具返回内容，并在结论中标明“演示回放”和待核实根因。

Compose 仅向主机回环地址发布工作台和订单端口，数据库、Prometheus 和 Loki 通过内部网络访问。文件中的密码用于本地演示。已有宿主机 MySQL、订单库和其他 Compose 项目各自使用原有资源。

## 直接从宿主机启动 Python 入口

直接运行 `main.py`、`python -m alembic` 或 Web API 时，必须显式设置
`INCIDENT_AGENT_DATABASE_URL`，指向已创建的 MySQL schema。程序没有文件存储或
默认数据库回退；Compose 内部的 `mysql` 服务名也不能直接用作宿主机地址。

使用 **Python 3.12** 创建独立环境，并安装锁定依赖。以下连接串仅为占位符，
执行前替换为自己的本机测试账号、端口及空 schema；真实凭据不要写入仓库。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.lock.txt
.\.venv\Scripts\python -m pip check

$env:INCIDENT_AGENT_DATABASE_URL = 'mysql+pymysql://<user>:<password>@127.0.0.1:<port>/<database>?charset=utf8mb4'
$env:PYTHON_DOTENV_DISABLED = '1'
$env:INCIDENT_AGENT_MODEL_MODE = 'demo_replay'
.\.venv\Scripts\python -m alembic upgrade head
.\.venv\Scripts\python main.py
```

`PYTHON_DOTENV_DISABLED=1` 禁止入口加载 `.env`，`demo_replay` 使用回放响应。
数据库 URL、监控端点与订单服务地址需要匹配自己的隔离环境。运行测试时改用
`requirements-test.lock.txt` 安装测试依赖，并单独配置
`INCIDENT_AGENT_TEST_DATABASE_URL`，不得指向真实运行库。

## 一次完整故障演示

在 Python 3.12 环境执行项目自带验收入口。

```powershell
python scripts/stage1_demo_acceptance.py
```

脚本先确认项目标签、完整订单源码 SHA、端口和健康基线，再给本次订单容器启用 SIMULATE_CODE_REGRESSION，持续访问真实 /order。Prometheus 按原有规则触发 HighHttp5xxRate，Monitor 建立 Incident，Worker 查询指标和日志。

在工作台筛选 order-service，进入相应 Incident。观测区域显示同一告警时间窗的 HTTP 5xx、P95、进程 CPU、进程常驻内存和日志。曲线保留采样缺口，各项分别显示样本、无样本或查询失败。CPU 单位为消耗的核数，内存为应用进程 RSS。

Prometheus 返回的合法 `infos` 会在对应曲线下作为“Prometheus 查询提示”显示，
最多 8 条、每条 512 字符，经过脱敏且按纯文本展示。P95 单调性修正提示说明
直方图输入需要检查；保留提示与有效数值不等于确认数据完全健康。
`warnings`、非法响应结构、非有限数值、越界时间点及越界数值仍拒绝整段结果；
没有样本时不补零。

脚本在 finally 中关闭故障开关，等待原有业务恢复验证返回 HTTP 200 和 verified。随后停止 Monitor/API、重启 MySQL，再启动 Monitor/API，比较已保存 Incident 和 Session 的回读结果。成功输出包含 restart_readback=true、fault_flag_restored=true。

更改过项目名或端口时，将同一配置传给 Compose 和验收入口。

```powershell
$env:COMPOSE_PROJECT_NAME = 'incident-agent-my-demo'
$env:INCIDENT_DEMO_WEB_PORT = '8089'
$env:INCIDENT_DEMO_ORDER_PORT = '18009'
$env:INCIDENT_DEMO_SUBNET = '172.30.163.0/24'
$env:INCIDENT_DEMO_ORDER_IP = '172.30.163.10'
docker compose up --build -d --wait --wait-timeout 180
python scripts/stage1_demo_acceptance.py --project incident-agent-my-demo --web-port 8089 --order-port 18009
```

订单实例 IP 应位于所选子网内，并避开网络、网关和广播地址。普通容器连接使用服务名。恢复探测沿用 HTTPRecovery 的固定 IP 校验，因此只为订单实例保留静态内部地址。

## 模型配置

真实模型通过 INCIDENT_DEMO_MODEL_MODE=deepseek 和 INCIDENT_DEMO_DEEPSEEK_API_KEY 显式启用，再重新创建 monitor。该模式调用项目现有 DeepSeek 配置及模型预算。第一阶段演示的验收范围使用回放模型。

本机运行 main.py 时，INCIDENT_AGENT_MODEL_MODE 默认是 deepseek，需要 DEEPSEEK_API_KEY。Compose 使用独立的 INCIDENT_DEMO_* 配置名。

## Windows 审批与制品恢复

沿用 scripts/w3_manual.py 和 W2/W3 的隔离库存制品流程。先在独立 MySQL 中创建空 schema，例如 ia_test_stage1_w3，再用自己的演示账号设置环境变量并迁移。

```powershell
$env:INCIDENT_AGENT_DEMO_DATABASE_URL = 'mysql+pymysql://演示账号:替换为密码@127.0.0.1:3308/ia_test_stage1_w3?charset=utf8mb4'
$env:INCIDENT_AGENT_DATABASE_URL = $env:INCIDENT_AGENT_DEMO_DATABASE_URL
python -m alembic upgrade head
python scripts/w3_manual.py
```

该入口要求 Windows、Node、frontend/node_modules，以及已有的 D:/prometheus-demo/.venv/Scripts/python.exe 和固定版本 Demo 源码。启动输出给出本轮临时凭据文件、工作台和停止文件位置。

通过本轮 requester、approver、executor 三种测试身份分别申请、审批和执行，核对制品、提案摘要与独立业务检查。证据有效期为现有 300 秒，超时后创建新实验。自动化测试身份的操作记录属于自动化验收，真人参与情况由实际操作者记录。

## 存储迁移与历史记录

使用现有 db.import_legacy 和 db.verify_consistency，完整用法见 [历史数据导入与一致性校验](LEGACY_DATA_IMPORT_2026-11-14_10-42-54.md)。旧数据先捕获为一致副本，再导入已迁移且停写的隔离 schema。

当前真实历史保留 16 项旧检查点事件身份缺口和 22 项展示计数差异。它们会使严格一致性校验返回非零。首次导入、重复导入和源目标摘要的验收结果见本阶段验收记录。

## 故障排查与停止

```powershell
docker compose ps -a
docker compose logs --tail=80 migrate monitor api alloy
docker compose stop
```

/healthz 显示 API 进程状态，/readyz 读取配置指向的三个存储。连接不可用或迁移未就绪时，服务返回受控错误。依赖启动状态可在 Compose 的健康检查中查看。

恢复演示后可使用 docker compose stop 保留数据卷，下一次启动继续读取同一批记录。确认本项目演示数据可以删除后，使用 docker compose down --volumes 清理本项目容器、网络和卷。

## 版本来源

deploy/order-demo 的订单代码、初始化 SQL、告警规则和 Loki/Alloy 原始配置来自 yuj1ng1/prometheus-demo 的完整提交 0e2f6f3220b9fb9f0baf8730de5ffaaac00d8c38。Dockerfile 只对容器通信地址、PostgreSQL 监控账号初始化和 Loki 数据目录权限做装配。订单 Python 依赖锁在 deploy/order-requirements.lock.txt。

平台继续使用 requirements.lock.txt、requirements-test.lock.txt 和前端 package-lock.json。部署复用现有 ServiceRegistry、工具、Manager、Worker、恢复验证与审批边界。
