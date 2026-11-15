# Remediation 与历史导入：双版本 MySQL 真实验收

## 验收范围与结论

本次在共享工作区当前文件上验收，保留其他开发者的改动。所有 MySQL 业务写入均在隔离 schema；真实 `data/` 未改写，真实历史未导入生产库。Remediation 存储、统一历史导入和严格校验已分成两个本地里程碑；CI 与本记录形成第三个里程碑，未授权也未执行推送或历史改写。

修复后在 **MySQL 8.0.46:3307 与 8.4.13:3308 均完成原命令的整套回归并退出 0**，各 2917 passed、2 skipped、190 subtests passed。两版均完成日志清理和临时库清理，运行前后 Python 源码哈希无变化。以下严格历史校验仍有 38 项警告，不能把测试套件通过等同于历史审计完全通过。

历史副本在两个版本均首次导入 Incident/映射/归档 26 行、Runtime 1416 行，重复导入 0 行，逐项摘要无差异。严格 Runtime 和跨存储校验仍退出 1，保留 16 项事件身份缺口与 22 项旧展示计数警告；这不是严格校验全通过。真实 Remediation JSON 不存在，使用合成 v1/v2 验证，不能替代不存在的真实历史。

本次交付涉及 `remediation/`、`db/`、CLI、Web 查询及调查调用方：新增 `0005_remediation_store` 和 `remediation_state` 单例文档（MEDIUMTEXT、revision），以行锁事务共同保存提案、审批、结果和审计，以命名锁协调同一 MySQL 服务器上的受控执行。`RemediationStore(database_url=None, *, read_only=False)` 默认读取环境 URL；CLI 的 `--store`/`--remediations` 接受 MySQL URL。复用现有审批模型、执行 claim 和 SQLite 转换辅助，保留崩溃后不重放动作的边界，没有另建审批系统或新增依赖，仅将已有 PyMySQL 升到 1.2.4。

统一导入/校验入口为 `db.import_legacy` 和 `db.verify_consistency`，支持预览、显式 apply、重复导入、冲突隔离、fail-fast、提交前摘要及提交不确定性报告；源文件和侧车、报告别名、损坏归档与 v1/v2 兼容均有相应验证。

实现与使用细节见 [Remediation MySQL 迁移](REMEDIATION_MYSQL_MIGRATION_2026-11-14_10-43-33.md) 和 [历史导入与一致性校验](LEGACY_DATA_IMPORT_2026-11-14_10-42-54.md)。

## 现场重新核实

- 开始时分支 main，HEAD `f071ed5b336d7fcbe25af5197f88be18fe87c3a6`，暂存区为空。只读 `git ls-remote origin refs/heads/main` 核实当时远端也是该 SHA，没有把本地跟踪引用当成实时结果。
- 本会话 `.git` 实际可写。3307 首次连接为 ConnectionRefused/WinError 10061，确认独立测试目录后启动原 8.0.46 实例即连接成功；没有沿用前会话的 WinError 10013 和 ACL 结论，没有删除 index.lock、改防火墙或批量修改 ACL。
- 本任务未停止或修改 3306 系统 MySQL80 服务；11 月 15 日主机重启后服务正常，PID 随系统重启变化。3307 使用原 `D:/incident-agent-mysql-test/data`，未初始化、未升级。新增 3308 使用完全独立目录。
- 其他开发者的 main.py model mode、config.py、prompts、services、前端、Compose、deploy、temp_test.py 和无关文档移动均留在工作区，未混入提交。开始时保存改动清单与哈希，结束时按本任务文件排除后再比较。

## 独立 MySQL 8.4 安装与包真实性

| 项目 | 实测值 |
| --- | --- |
| 版本 | MySQL Community Server 8.4.13 LTS，Windows x86_64 ZIP |
| basedir / 配置 | `D:/mysql-8.4` / `D:/mysql-8.4/my.ini` |
| datadir | SQL 返回 `D:\mysql-8.4\data\` |
| 监听 | `127.0.0.1:3308`，mysqlx 活跃插件数 0 |
| 运行形式 | 独立隐藏后台进程；未注册 Windows 服务，不自动开机启动 |
| 测试身份 | `incident_agent_test84@127.0.0.1`，基础库 `incident_agent_test84` |
| 认证 | caching_sha2_password；PyMySQL 1.2.4 + cryptography 50.0.2 的完整非 TLS RSA 认证实际执行成功 |

包来自 [MySQL 官方 8.4 下载页](https://dev.mysql.com/downloads/mysql/8.4.html?os=3) 的 [官方 ZIP](https://cdn.mysql.com/Downloads/MySQL-8.4/mysql-8.4.13-winx64.zip)，281154912 字节。独立下载同目录 `.asc` 签名及官方 MD5，并从 [官方 GPG 验证说明](https://dev.mysql.com/doc/refman/8.4/en/checking-gpg-signature.html) 获取发布密钥：

```text
官方公布 MD5：7b94489d5524dea9b27097068bea25a8（匹配）
官方密钥指纹：BCA43417C3B485DD128EC6D4B7B3B788A8D3785C
GPG：GOODSIG + VALIDSIG，RSA/SHA256，签名日期 2026-10-01
经该签名验证的本地 ZIP SHA-256：
e9cd98b1d23f122987b1afca26ad942efec5891481103d48ae3cde237fa3ea54
```

**未找到独立的官方 SHA-256 校验值**：官方下载页及 [包完整性说明](https://dev.mysql.com/doc/refman/8.4/en/verifying-package-integrity.html) 提供 MD5/GPG 校验途径；实际检查的 `.sha256` 和 `.sha256sum` 地址返回 404，不能把上面的本地计算值称为官方公布值。实际以官方密钥的 RSA/SHA256 签名确认包真实性，再记录 ZIP 的本地 SHA-256，并用于 CI 固定下载校验。GPG 的未建立 Web of Trust 警告不等于坏签名；指纹与官方 HTTPS 文档一致，但没有宣称该密钥已被本机信任链认证。

安装包、签名、密钥和校验结果仅留在 Git 外 `D:/mysql84-downloads`。root 与测试密码随机生成，仅保存在 `D:/mysql-8.4/private`；该目录关闭权限继承，仅当前用户、SYSTEM 与 Administrators 有访问权限。引导 SQL 在启动和认证成功后删除；用不含 init-file 的正式配置干净重启并复核。

测试账号仅有基础库精确名称和转义后的 `ia\_test\_%` schema 权限，没有全局管理权限。3307 使用既有测试账号并保留其原授权（包括 `ia_stage2_%`）；本节最小权限及越界拒绝验证针对新增的 3308 账号。实际创建并删除自己新建的前缀 schema 成功；尝试非前缀库、基础库通配别名和 mysql.user 均被拒绝。对基础库名称中的下划线也转义，避免误授权类似名称。

初装联调暴露 PyMySQL 1.2.3 完整 RSA 认证缺少返回数据包的问题；核对官方 wheel 后安装 1.2.4，并同步最低版本与锁文件，没有修改第三方包源码或启用 mysql_native_password。8.4 安装验证包含一次实际 RSA 加密调用的观测，不以认证缓存命中代替完整认证。

如需在重启系统后再次启动本实例，可用：

```powershell
Start-Process -FilePath 'D:\mysql-8.4\bin\mysqld.exe' -ArgumentList '--defaults-file=D:/mysql-8.4/my.ini' -WindowStyle Hidden
```

## 测试执行与修复轨迹

解释器 `D:/conda/envs/agentlearn/python.exe`，Python 3.12.14、SQLAlchemy 2.0.54、Alembic 1.20.1、PyMySQL 1.2.4、cryptography 50.0.2、pytest 9.1.1。3307 与 3308 全量顺序执行，避免 Demo 端口和物理进程相互干扰。当前 Conda 环境另执行 pip check，返回 No broken requirements found；这不等于重新创建干净环境安装。

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B -m pytest tests -q --disable-warnings
```

| MySQL / 端口 | passed | failed | skipped | subtests passed | pytest 耗时（秒） | 退出码 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 8.0.46 / 3307 | 2917 | 0 | 2 | 190 | 5435.24 | 0 |
| 8.4.13 / 3308 | 2917 | 0 | 2 | 190 | 4954.19 | 0 |

两版各 2 skipped，均为既有条件：`DependencyTests.test_clean_install_has_all_pins_and_no_legacy_httpx` 需要 `INCIDENT_AGENT_CLEAN_ENV=1` 的干净安装环境；`PolicyAndEventTests.test_external_rules_contract_when_explicitly_configured` 需要外部 `ALERT_RULES_PATH`。实际另用 `-rs` 核实这两个原因。当前主机硬链接用例正常执行，未沿用上一会话的硬链接能力跳过。190 个 subtests 单列，未加到 2917 个主用例中重复计数。

测试使用真实 MySQL 持久化、事务和进程，模型与外部监控等按既有 Fixture/Mock 隔离；offline 子目录明确使用 SQLite 事务替身或纯解析/异常注入。通过结果不证明真实 Provider、生产修复后端或真实人类批准已经验收。完整套件包含交接要求的 Remediation、恢复、CLI、补偿、Web、W3、W2、fixture 启动、导入/offline 及 Incident/Runtime 回归。

本轮新增真实 MySQL 验证包括：干净库与 0004→0005、种子和 Incident 保留、空种子 downgrade/re-upgrade、真正批准后拒绝 downgrade 且摘要不变；跨 schema / 进程锁；杀断自己明确持有锁的会话；实际 GET_LOCK/RELEASE_LOCK 后注入回执丢失；实际 COMMIT 后注入回执丢失；savepoint 内写入后异常和 fail-fast 整体回滚。回执丢失使用客户端故障注入，不宣称实际切断网络。

另在两版新建隔离 schema 中直接尝试 id=2 和 revision=-1，均返回 MySQL 3819，单例行前后完全相同，随后清理自己创建的 schema。首次辅助脚本只捕获 IntegrityError 而中止；真实驱动将 CHECK 违规归类为 OperationalError，改为检查实际错误码后两版重新验证成功。生产层捕获 SQLAlchemyError，已经覆盖该真实异常类型，没有为此修改业务源码。

初次专项 4 failed、332 passed、81 subtests passed（851.14s），均为 `retry_proposal()` 遗漏 FileLock 替换；修复后相关恢复/锁/迁移/offline 专项 113 passed（124.74s）。WAL 主文件不完整与可选审计 details 首先复现 2 失败；修复后离线 81 passed。报告覆盖 SQLite 侧车先复现 3 失败，修复后导入/offline 100 passed（90.46s）。这些失败和重跑均保留真实日志，没有将失败轮次计成通过。

第一次 8.0 全量尝试因本会话临时增加的进度记录器在 Windows 上替换文件返回 WinError 5 而内部中断，退出 3：1978 passed、2 skipped、32 subtests passed（1859.65s）。这不是业务用例失败，也不是全量通过；等待其正常清理并退出后撤下该记录器，保留中断日志，按原命令从头重跑。最终表格仅列没有附加观察插件的完整运行，不拼接中断前后的测试数量。

不附加观察插件的第一轮完整 8.0.46 回归：10 failed、2905 passed、2 skipped、190 subtests passed，5407.34s，退出 1。十项失败均是日志记录断言：Alembic 的 `fileConfig()` 默认禁用已存在应用 logger，导致同进程迁移后 Remediation 异常、工具 no_data 和 Web 存储错误的安全日志不可见。先新增真实迁移后的日志回归，修复前复现 1 failed（11.34s）；再在 [migrations/env.py](../migrations/env.py) 显式保留已有 logger。同进程运行完整导入迁移测试和原十项失败用例，27 passed（160.38s），没有放宽日志、脱敏或业务断言。

随后重跑前的资源检查发现一个本轮遗留空 schema：并发 reopen CLI 子进程创建 Remediation schema，而父进程未登记它。核实首轮前不存在、创建时间落在本轮、revision=0 且领域表为空、无活动连接后，只删除该确定的测试 schema；未按前缀批量清库。修正 [test_runtime_stability.py](../tests/test_runtime_stability.py) 由父进程先登记资源，原跨进程预算幂等用例重跑 1 passed（37.55s）。最终双版本运行在每版开始前要求无遗留临时库，并在结束后检查临时库清零与源码哈希，最终表格只记录修复后的完整套件。

第二轮完整 8.0.46 已执行全部用例，2916 passed、2 skipped、190 subtests passed（5445.14s），但进程退出 1：在 pytest_unconfigure 删除临时 agent.log 时遇到 WinError 32。此轮没有用例失败，临时 schema 为零，Python 源码哈希未变；退出清理失败仍不能报告整套通过。追查实际 Python logging.config 源码确认：即使 disable_existing_loggers=False，fileConfig 仍关闭已有 handler 并清空其注册列表，后续重新打开的 FileHandler 不再被 logging.shutdown 关闭。新增真实文件句柄回归先复现 1 failed（10.06s），随后让程序内迁移保留调用方日志配置，仅 Alembic CLI 通过 cmd_opts 配置控制台日志；相关迁移、原日志失败用例、文件句柄与并发 CLI 同进程 29 passed（184.62s），退出 0。CLI 另以不可连接的虚拟目标生成完整离线 SQL，确认 0005 SQL 和控制台升级日志正常，没有连接数据库。之后再从头运行两版完整套件，不把第二轮的用例通过视作退出成功。

清理第二轮遗留临时日志的命令被自动审批拦截，仅返回 blocked by policy。该日志保留在 `C:/Users/zp1222/AppData/Local/Temp/incident-agent-pytest-logs-ixrxst7o/agent.log`，没有绕过限制删除；完整 pytest 输出与检查 JSON 已在项目忽略目录归档。此项是已退出进程的故障日志留存，不是残留数据库、故障开关或执行进程。

2026-11-15 恢复工作时，确认主机当日 09:00:56 已重启，两个人工启动的隔离 MySQL 实例均未运行。上次执行日志最后停在 75%，没有完成汇总或退出码文件，不能计作完整回归。保留该未完成轮次后，用原 8.0.46 测试 datadir 和原 8.4.13 配置重新启动，没有初始化或升级 3307。再次 SQL 核对两版版本、端口、datadir、回环监听和测试身份。

中断执行前临时 schema 清单为空；恢复后 3307 有 770 个、3308 为零。核对名称格式、NTFS 目录创建时间全部处于该轮执行窗口、表名仅包含项目表及 Alembic 版本表，且无测试进程或连接后，只按已记录的精确清单执行 DROP DATABASE。770 个确认属于该轮的库已全部清理，基础测试库与其表数量不变；真实历史原件和副本哈希复核均未变。随后由独立隐藏后台脚本重新顺序执行两版完整套件，持续写入各版日志、退出码和结束检查，不拼接未完成轮次的通过数量。

两版正式全量启动前的 **216 个 Python 文件哈希完全一致**；各版退出后的比较均无变更，结束后再次核对也相同。全量过程中只修改忽略目录中的验收草稿，没有编辑业务 Python 或测试代码。该检查针对 Python 文件；共享工作区及干净源码归档的验证范围在下文分别说明。

## 历史副本与严格审计

重新捕获当前 JSON、SQLite 与归档；先检查包含现有侧车的源集合稳定，再只对捕获副本执行 SQLite backup 并转换 DELETE journal，quick_check 为 ok。原数据未用可写 SQLite 连接打开。导入前后分别核对原文件和最终副本哈希，不把 backup 后的二进制布局误称与原主文件逐字节一致。

两个服务器都执行真实 CLI dry-run、apply、重复 apply、verify-only、逐项摘要与跨存储 verify。dry-run 目标摘要不变；首次写入 Incident 26 行、Runtime 1416 行，重复均 0 行，源目标逐项摘要差异为 0。Incident 严格 verify 退出 0；Runtime 与跨存储 verify 退出 1，独立读取源副本确认相同 38 项警告：

逐项摘要使用导入器规范化后的业务字段；忽略导入更新时间、部分导入创建时间及 Remediation revision，保留正文、身份、序号与权威预算等检查。它不等于 SQLite 和 MySQL 文件逐字节相同；原文件是否改写由另行记录的文件哈希判断。

| 分类 | 数量 | 解释 |
| --- | ---: | --- |
| legacy_checkpoint_event_identity_unavailable | 16 | 旧事件仅有 phase，无法证明 checkpoint_id |
| checkpoint_result_display_count_mismatch | 22 | 旧结果展示计数与权威 Session 预算不同 |

未发现额外错误、权威预算计数差异或源目标摘要差异。旧检查点裁剪与旧事件保留按既有规则校验，没有补造身份、改写预算或删除警告。合成 Remediation v1/v2 均重复导入幂等、严格跨存储 verify 退出 0。真实 Remediation 文件缺失是验证范围限制，不以合成结果冒充真实历史。

## CI 与未验证范围

[runtime-tests.yml](../.github/workflows/runtime-tests.yml) 的 Windows 任务新增独立 MySQL 8.4.13：官方 ZIP 加固定摘要、随机并屏蔽的密码、受保护引导文件、回环监听、caching_sha2_password、精确基础库和转义前缀权限、SQL 就绪检查、测试 URL，以及只关闭本任务实例的清理。Windows 专项补齐导入、锁和 offline，Linux 完整回归与 Windows 任务超时均延长到 120 分钟。

YAML、Windows PowerShell 语法树及内嵌 Python 语法检查通过。另以匿名只读请求查询 Docker 官方 Registry，`library/mysql:8.4.13` manifest 返回 HTTP 200，包含 Linux amd64/arm64，索引摘要为 `sha256:a81d043930e51745522bd5c430961cf8286f0a23f0a9a2a6e0108f7ea71b0902`。这只确认查询时标签存在，没有执行镜像拉取或 CI。**本任务未推送，未执行或验收这份 GitHub Actions 配置，不能报告 CI 通过。** W2 用例仍有既有外部 Demo 解释器条件；托管 Windows 环境是否提供该条件尚未验证，没有删除测试或新增无依据的跳过。Linux 及干净环境安装同样只保留 CI 定义，不能以当前 Conda 环境通过代替。

为检查对未提交并行源码的隐式依赖，另从 `94324df758a2f811dd1756ad1f98426a133461e5` 导出 Git 已跟踪源码，只叠加本次待提交的 `migrations/env.py`、`tests/test_legacy_import_mysql.py` 和 `tests/test_runtime_stability.py`。该候选源码归档不带 Git 元数据、其他未提交源码和测试数据库 URL，在同一 Conda 解释器下只收集 2915 项（10.67s），运行 `tests/offline/` 得到 84 passed（9.14s），未使用 MySQL。这不是独立依赖安装，也不是该归档的完整 MySQL 回归。

共享工作区含未提交的并行业务修改，完整回归针对该工作区文件；没有额外声称从最终提交重新检出的干净环境完成了同一全量回归。未测试生产数据库、真实模型调用、真实人类审批或不同 MySQL 服务器共同协调同一物理目标。后者本就不受 GET_LOCK 的跨服务器保护，实际执行者必须统一协调服务器。

## 清理与交付

最终只读复核：3307 为 8.0.46，3308 为 8.4.13，均绑定 127.0.0.1，基础测试库仍存在，临时 `ia_test_` schema 均为零。3306 的 MySQL80 服务正常，本任务未停止或修改它。两个测试实例保留运行；它们没有注册为自动启动服务。

正式测试与控制脚本已退出，没有匹配到本次 pytest、进程 fixture 或 Demo 脚本；用户已有 Uvicorn 进程保持运行。两版最终全量均成功完成临时日志释放；前述被拦截的旧故障日志保留。真实历史 3 个源文件和 3 个最终副本哈希保持原值，没有新增 SQLite WAL/SHM/journal；真实 Remediation JSON 仍不存在。

排除本任务文件后，587 个原有文件哈希和原有 Git 状态均与开始时相同。只读核对远端 main 仍为 `f071ed5b336d7fcbe25af5197f88be18fe87c3a6`。本任务未推送。

日志、JSON 原始报告、数据副本与源哈希清单留在忽略目录 `build/validation/mysql-acceptance`，没有加入 Git。安装包和私密文件留在工作区之外。除本任务文件外，原有文件哈希和 Git 状态再核对；不做无关目录整理。

| 本地里程碑 | 完整 SHA |
| --- | --- |
| 11.14 将修复审批存储迁移到 MySQL 并验证执行锁 | 001bed01543b1ed0e34d9f35a30546a017e34ab8 |
| 11.14 增加历史数据导入与严格一致性校验 | 94324df758a2f811dd1756ad1f98426a133461e5 |

本记录、迁移日志与测试资源清理修复及 CI 随第三里程碑本地提交，实际提交说明与完整 SHA 在交付回复给出，避免在提交自身内容中伪造自引用 SHA。当前分支 main；未推送、未改写历史。
