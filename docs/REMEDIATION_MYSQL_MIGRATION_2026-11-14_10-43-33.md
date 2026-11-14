# Remediation MySQL 迁移实现与专项验收

## 当前实现

新增 Alembic `0005_remediation_store`。`remediation_state` 以固定 id=1 的 MEDIUMTEXT 文档保存提案、审批、动作结果、验证结果和审计；数据库约束拒绝其他 id 与负 revision。沿用完整文档结构，可保留旧记录顺序及字段，避免在本次迁移中同时重构审批领域模型。代价是同一库的 Remediation 写入串行化，文档受 MEDIUMTEXT 大小限制；出现实际容量或吞吐瓶颈时再评估拆表。

`RemediationStore(database_url=None, *, read_only=False)` 默认使用 `INCIDENT_AGENT_DATABASE_URL`，仅接受 `mysql+pymysql` URL。`transaction()` 对单例行执行 `SELECT ... FOR UPDATE`，提案、决策、结果与审计一起提交，成功写入递增 revision。连接保存在 ContextVar 中，避免 Web 线程共享同一写事务。v1 读取不回写，下次成功写入才升级为 v2；损坏文档、缺失种子和无效配置均失败关闭。数据库错误或 COMMIT 结果不确定时，不自动重放事务或物理动作。

执行、检查、人工收口及人工重试统一使用 MySQL `GET_LOCK/RELEASE_LOCK`。锁名是稳定摘要，不超过 64 字符，可协调同一服务器上的多个 schema、连接和进程。只有明确的 0 表示忙；NULL、获取或释放异常均报错，结果不确定时丢弃连接，避免带锁连接回到连接池。参见 [MySQL 8.4 锁函数说明](https://dev.mysql.com/doc/refman/8.4/en/locking-functions.html)。

**不同 MySQL 服务器不共享命名锁，实际执行者必须使用同一协调服务器。** 锁会话断开也不能撤销已经发生的外部操作。执行前持久化 claim、执行中崩溃后拒绝盲目重放的既有规则继续生效，不能将数据库锁视为外部系统的事务。

## 调用方与兼容边界

- [executor.py](../remediation/executor.py)：替换全部生产 FileLock 调用，包括本次真实测试发现的 `retry_proposal()` 遗漏。
- [remediation_cli.py](../remediation_cli.py) 的 `--store`、[runtime_cli.py](../runtime_cli.py) 的 `--remediations` 改为 MySQL URL，默认读取环境配置。文件路径不再是生产存储接口。
- [app.py](../web_api/app.py) 增加 `remediation_url`；旧 `remediation_path` 关键字仅保留参数别名，不恢复文件存储。HTTP 路径、审批身份和权限规则保持既有定义。数据库不可用返回 503，执行锁忙返回 409；快照读取不再重试文件锁。
- Web 查询、演示脚本和进程夹具改用 URL。旧测试临时路径到独立 schema 的转换只位于测试层；生产代码不包含路径映射。
- [compensation.py](../remediation/compensation.py) 与 [remediation_views.py](../web_api/remediation_views.py) 兼容 v1 审计中省略的可选 `details`，读取不补造旧字段；审批命令幂等及补偿评估继续可用。
- Runtime 保留原有有界提案重试，数据库失败不会把调查结论隐藏或重放修复动作。

无新增第三方依赖。PyMySQL 最低版本及锁文件更新为 **1.2.4**：本机官方 1.2.3 在 caching_sha2_password 的完整 RSA 认证末尾未返回最终数据包，实际登录触发异常；官方 1.2.4 修复返回值后，使用 cryptography 50.0.2 的完整 RSA 登录实测成功。没有修改安装包源码，也没有改用 mysql_native_password。

## 数据迁移与回滚

生产运行不再读取 JSON。既有 JSON 通过 `python -B -m db.import_legacy import-remediation-json` 导入，由统一历史导入器处理；默认预览，只有显式 `--apply` 才写入。目标必须先迁移到 Alembic head。

0005 为干净库创建 revision=0 的空种子；从 0004 升级不改写原有 Incident。仅未使用的初始空文档允许直接 downgrade；已有审批、审计或其他写入时拒绝删除。实际回滚必须先停写、导出并制定恢复方案，不能靠 downgrade 自动丢弃数据。MySQL DDL 与外部动作不具备跨系统原子性。

## 本会话实际专项结果

解释器 `D:/conda/envs/agentlearn/python.exe`，均使用 `-B`；以下连接到 **MySQL 8.0.46，127.0.0.1:3307 的隔离 schema**，不是 SQLite 替身结果。

```text
python -B -m pytest tests/test_remediation_recovery.py::RecoveryTests tests/test_remediation_mysql.py tests/test_legacy_import_mysql.py tests/offline/ -q --disable-warnings --tb=short -rs
113 passed in 124.74s
```

覆盖干净库迁移、0004→0005、种子和 Incident 保留、初始空库 downgrade 后再升级、非空存储拒绝 downgrade，以及跨 schema / 跨进程命名锁、真实锁会话断开、获取/释放回执丢失、事务回滚和恢复规则。锁回执用例让语句实际到达 MySQL，再注入客户端回执丢失；不宣称真的制造网络设备故障。

最初交接要求的专项运行结果为 **4 failed、332 passed、81 subtests passed，851.14s**。4 个失败均由 `retry_proposal()` 遗留的 FileLock 名称引起，已修复；上述 RecoveryTests 重跑覆盖对应失败。本说明不把首轮专项写成全通过。

离线契约另覆盖 URL 校验、v1 读取不写回、审批与审计原子性和异常注入；这些结果不能代替 MySQL 行锁或真实模型验收。全量双版本结果、安装与历史演练汇总由最终验收记录单独记录。

## 操作范围

本次未改写 `data/`，没有把真实历史导入生产库。3306 系统实例未停止或修改；3307 原先未监听，确认独立测试 datadir 后启动原 8.0.46 实例，没有初始化或升级其 datadir。新增 8.4 使用独立目录和端口。其他开发者的 main.py、前端、配置、模型提示、Compose 与部署修改不属于本里程碑。

该专项不能证明真实人类审批已经完成，也不能证明生产修复后端可用。审批与动作的真实环境验收仍需各自独立授权和证据。
