# 历史数据导入与一致性校验

## 模块与接口

| 模块 | 职责 |
| --- | --- |
| [legacy_sources.py](../db/legacy_sources.py) | 只读解析 Runtime SQLite、Incident v1/v2 和归档、Remediation v1/v2，生成脱敏诊断 |
| [import_legacy.py](../db/import_legacy.py) | 计划、冲突检查、按业务单元隔离的事务写入和提交前摘要核对 |
| [verify_consistency.py](../db/verify_consistency.py) | schema、身份、序号、外键、活动映射、归档、预算、审批与审计校验 |
| [import_sqlite.py](../db/import_sqlite.py) | 保留原有 Runtime 转换辅助和兼容入口，委托统一导入流程 |

复用既有表模型、领域校验和转换能力，没有另建权威存储，也没有新增依赖。`import_snapshot(source_path, database_url)` 保留显式程序化 apply 和计数字典返回形式；CLI 默认只预览。PyMySQL 因真实 8.4 认证问题升级到 1.2.4，原因见 Remediation 迁移说明。

## 使用方式

只对已经准备好的一致副本和独立演练库操作。目标必须先执行 `alembic upgrade head`；导入器不会创建或升级 schema。`--target` 可指定 MySQL URL，省略时读取 `INCIDENT_AGENT_DATABASE_URL`；避免在终端、日志或进程参数中公开带密码 URL，优先用环境变量。

```powershell
python -B -m db.import_legacy import-incidents-json --source '<Incident 副本>' --history-dir '<归档副本目录>' --dry-run
python -B -m db.import_legacy import-runtime-sqlite --source '<一致 SQLite 副本>' --dry-run
python -B -m db.import_legacy import-remediation-json --source '<Remediation 副本>' --dry-run
```

检查报告后，以 `--apply` 替换 `--dry-run` 才写目标；`--dry-run`、`--apply`、`--verify-only` 互斥。建议 Incident/归档先于 Runtime 和 Remediation，最后跨存储核对。

```powershell
python -B -m db.import_legacy import-incidents-json --source '<Incident 副本>' --apply --report '<独立报告路径>'
python -B -m db.import_legacy import-runtime-sqlite --source '<一致 SQLite 副本>' --verify-only
python -B -m db.verify_consistency --source '<Incident 副本>' --source-kind incidents --history-dir '<归档副本目录>'
python -B -m db.verify_consistency
```

兼容入口 `python -B -m db.import_sqlite --source '<副本>'` 支持原位置参数，同样默认预览。完整 dry-run 会读取 MySQL 并检查 head；单独解析源不是完整数据库预演。

## 事务与源数据保护

- 源不写入、不移动、不删除。SQLite 以只读 immutable 方式读取；非空 WAL/journal 或 WAL 模式头均拒绝导入，避免接受丢失已提交 WAL 的主文件。应通过 SQLite backup 获得一致备份，并仅在备份上转为 DELETE journal 模式。即使 WAL 模式当前没有侧车文件，也采取保守拒绝。
- JSON 拒绝重复键与 NaN/Infinity。归档用原 UTF-8 字节计算 SHA-256，检查 Incident 身份、Session 清单和证据归属，保留顺序、已有字段与 ID。
- 摘要相同视为 unchanged；内容、主键、序号或活动映射冲突不覆盖权威记录。Remediation 唯一允许替换的是 revision=0 且内容严格为空的迁移种子。
- 默认按 Incident、Session 或整个 Remediation 文档隔离错误，以 savepoint 提交其他有效单元；存在错误仍返回非零。`imported > 0` 与非零退出码可以同时出现，表示部分成功。`--fail-fast` 则回滚整个导入事务。
- COMMIT 前逐项核对成功写入摘要；回执丢失报告 `write_outcome=unknown`，不宣称已回滚、不自动重放。再次运行时先用摘要确认实际结果。
- 在数据库操作之前拒绝报告覆盖源文件、软/硬链接别名、SQLite 的 `-wal/-shm/-journal` 路径以及 Incident 归档源目录。本机真实硬链接用例已执行通过，未沿用上一会话的能力跳过。
- 导入只适用于受控停写目标；开始正式迁移前应停止目标业务写入并备份。行锁和冲突检查不能替代跨系统切换方案。

报告仅含问题码、计数、记录哈希、文档摘要和源清单摘要，不回显原始 ID、路径、URL、审计详情或校验输入。统计 expected/actual 仅公开整数，其他异常值只公开类型。

## 严格校验语义

导入没有错误时可以 `ok=true`，存在历史警告仍为 `consistent=false`。`--verify-only` 和独立 verify 保持严格：任何未解决警告或差异均退出非零。

旧 `checkpoint_saved` 事件只有 phase，没有 checkpoint_id。按同 Session、同 phase 和保留时间窗口建立有限历史线索，一条事件最多覆盖一个检查点，同时报告 `legacy_checkpoint_event_identity_unavailable`，不会补造身份。显式空 ID、跨 Session、phase 错误、重复保存或窗口内缺失事件按错误隔离。裁剪窗口之前的旧事件可以保留，不能据此证明已删除检查点的正文。

旧展示计数不等于权威预算。`checkpoint_result_display_count_mismatch` 保留展示差异；Session、预算预留事件和检查点另行核对。不会为通过校验而重写历史统计。

## 本会话真实 MySQL 演练

使用 `D:/conda/envs/agentlearn/python.exe -B`，在 8.0.46:3307 与 8.4.13:3308 的独立临时 schema 上分别执行 Alembic head、真实 CLI dry-run、apply、再次 apply、verify-only、逐项源目标对比和跨存储 verify。两边结果一致：

| 来源 | 实际内容 | 首次写入 | 重复导入 |
| --- | --- | --- | --- |
| Incident 与归档 | 24 Incident、1 活动映射、1 归档；26 条热历史、1 条归档历史 | 26 行 | 0 行；26 unchanged |
| Runtime | 14 Session、56 Checkpoint、7 Command、1317 Event、22 Metadata | 1416 行 | 0 行；1416 unchanged |
| Remediation | 真实 `data/remediation.json` 不存在，分别使用合成 v1/v2 | 每份 1 行单例文档 | 0 行；1 unchanged |

真实文件先稳定捕获到忽略目录，再仅对捕获副本使用 SQLite backup 并规范 journal 模式。捕获前后原 JSON、SQLite、存在的侧车及归档哈希核对不变；导入前后原文件和最终副本分别核对不变。SQLite 一致备份的二进制布局允许与原主文件不同，不把两者哈希误称相同。dry-run 前后目标摘要不变，两版本逐项源目标摘要差异均为 0。

Incident 严格校验退出 0。Runtime verify-only 与跨存储 verify 均退出 **1**，独立读取源副本再次分析得到：

| 历史问题 | 数量 |
| --- | ---: |
| `legacy_checkpoint_event_identity_unavailable` | 16 |
| `checkpoint_result_display_count_mismatch` | 22 |
| 合计 | 38 |

没有发现其他错误、权威预算差异或源目标摘要差异；合成 v1/v2 跨存储严格校验均退出 0。**这证明副本导入和幂等成立，但历史审计不足仍未解决。** 没有修改历史、补造事件或放宽严格退出码。真实 Remediation 历史不存在，因此不能宣称其真实数据迁移已演练。

## 专项与故障路径

```text
python -B -m pytest tests/test_legacy_import_mysql.py tests/offline/ -q --disable-warnings --tb=short -rs
100 passed in 90.46s
```

以上连接 8.0.46 的真实隔离 MySQL；其中 offline 子目录明确使用 SQLite 事务替身或解析/异常注入，只能证明相应契约。MySQL 专项覆盖 schema 不在 head、冲突不覆盖、部分失败隔离、fail-fast 回滚、实际 COMMIT 后丢回执不自动重放、迁移种子及回滚保护。新增侧车报告覆盖用例先真实复现 3 失败，修复后纳入 100 passed。

初轮 WAL 主文件缺失与旧审计可选字段问题先复现 2 失败，随后修复；离线重跑曾为 81 passed，后增加的侧车报告保护包含在最终 100 passed 中。双版本完整回归及其准确通过/跳过数量另由最终验收记录汇总，不能用这些专项数量代替。

## 数据与资源范围

只将真实副本导入新建的 `ia_test_` 演练 schema。演练脚本记录自己创建的 schema，完成后逐一清理；没有删除基础测试库、生产库或其他前缀资源。日志、原始报告、数据副本、凭据与安装包均不提交。真实 `data/` 保留原位。此次不涉及真实模型调用或真实人类批准，不以 Mock 或自动化身份代替这些验收。
