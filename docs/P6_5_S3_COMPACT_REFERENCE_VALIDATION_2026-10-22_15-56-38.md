# P6.5-S3 精简引用协议与兼容性离线验证报告

本轮通过限定范围内的离线验收：精简回答能减少输出字节，合法 v1/v2 样本的规范化来源、声明和字段检查结果一致，既有来源绑定与人工语义审查要求保留。真实 Provider 对新协议的遵循情况、精确 token 容量和调查语义正确性尚未验证；没有正式人工批准。

## 1. 基线、范围与既有修改

已阅读根目录 [AGENTS.md](../AGENTS.md)、S1/S2/S2.1 报告、P6.5 请求准备及审查链路、相关测试和现有 fixtures。起始分支为 main，HEAD、本地 origin/main 和 `git ls-remote --heads origin main` 均为 `523147ed20f9e574a6295fb68913bb5a6aaa5b17`，暂存区为空；提交前再次查询的远端也保持该 SHA。

工作区并非干净：已有 README 文档索引、生产 prompt、temp_test 修改、旧文档删除与归档文件，以及历史测试临时目录。其种类与 S2/S2.1 的既有记录一致，本轮逐项保留，不暂存，不执行 reset、clean、stash、历史改写或删除。初始保护清单覆盖 7,376 个文件/链接条目，其中 169 个是历史测试符号链接；不跟随这些链接读取外部目标。

只改动实验脚本、测试和本报告。没有修改生产 Agent、Worker、RuntimeStore、ResultTrust、ToolManager、预算、租约、fencing、审批状态机、配置或依赖。

## 2. 协议与兼容读取

| 项目 | v1 / 旧请求 | v2 / 新请求 |
| --- | --- | --- |
| 回答版本 | `p65_s1_answer_v1` | `p65_s1_answer_v2_compact_refs` |
| 请求版本 | `p65_s1_semantics_v4_tools5` | `p65_s1_semantics_v5_compact_refs_tools5` |
| tool_call 字段 | 身份、参数及模型重复提供的来源元数据 | 仅 `id/type/call_id/tool/arguments` |
| input_context 字段 | 原有路径引用规则 | 仅 `id/type/input_path` |
| claims | 保留既有结构和验证 | 同一 Claim 模型；`id/text/kind/refs/use` 保留 |
| field_value | 严格比对引用对象中的路径和值 | 保留；缺路径、缺值或值不符均不能验证通过 |
| 多余字段 | 保留原有契约与问题报告 | 按 type 区分的严格模型直接拒绝，含值为 null 的多余字段 |

`parse_answer()` 根据明确版本选择独立契约，原 `Answer`/`Citation` 继续表示 v1。v2 不套用 v1 的元数据必填规则，v1 也不从 v2 自动补齐字段。v1 原有八项来源字段必须显式提供并逐项匹配；隐藏 Runtime observation_id 允许省略/null 的旧规则保持，提供错误 ID 仍拒绝。

两版共用 `call_id → 实际记录` 绑定、严格 JSON 参数比较、声明检查和来源规范化。拒绝未知 call、p63 历史 ID 冒充 call、工具或参数错绑、重复 citation/call/claim/ref，以及重复 Runtime call/observation 身份。重复记录不会再覆盖索引中的前一条；涉及歧义的引用不能被验证。即使第一条 citation 已无效，后续同名 ID 也仍被识别为重复。

规范化对象中的 observation_id、状态、历史证据 ID、source、originals、采集/评价/查询时间和日志请求范围均从记录生成。模型夹带这些字段会使整个 v2 回答契约失败；即使字段与记录完全相同也不接受。错误绑定时可保留实际记录作为诊断展示，但 S2 明确标记 citation_invalid，不将其字段关系写成已验证。

**缺失元数据需要与损坏记录区分。** 真实的日志范围拒绝结果没有 captured_result、历史来源和采集时间，因此这些规范化字段保持 null/[]，并保留实际请求的 start/end；不能补成成功或已读日志。若声明试图读取原记录中不存在的字段，仍返回 source_field_missing。缺少 Runtime 身份/结果/回放时间，或来源字段形状不合法，则报告 ambiguous_runtime_records / invalid_runtime_record；不会猜测来源。

`input_context` 只解析初始输入中的实际路径和值。S2 的来源等级仍为 supplied_input_declaration_only，不获得工具身份、调用成功或当前状态权限。有效零值与日志正文仍分别要求实际历史工具内容，no_data 不等于零，范围拒绝不等于取得日志。

## 3. 来源认证、权限及版本隔离

纯函数 `review_answer()`、`evaluate_claim_quality()` 不认证任意调用方提供的 records。局部结构校验和规范化不能把自行构造的记录变成可信证据；该可信边界说明保持在代码与输出中。

CLI 仍先执行未修改的 `load_real_case()`：独立核验固定 manifest、原始 raw/SDK/请求哈希、call/output/record/Checkpoint 关系及 immutable 只读 SQLite。它只绑定既有固定真实案例，并不是适用于未来任意运行的通用认证器。本轮没有放宽封存加载器。

S2/S2.1 门禁逻辑和报告版本未改，始终返回 automatic_acceptance_allowed=false、manual_semantic_review_required=true、formal_approval=not_performed。结构合法、字段匹配及来源真实均不能替代自然语言语义审查，不能产生 trusted/accepted 或修复批准。

实验请求仍限定 3 次模型请求、5 次工具执行（包含自动版本探针）、max_output_tokens=2048，重试仍为 0。`p65_readonly_investigation.py` 本轮不变，通过现有导入、请求构造、冻结 scope/code SHA 和授权版本校验使用 v5。旧 v4 授权以及改封印但仍为 v4 的计划，均在凭据、SDK 和正式消费边界前拒绝。没有创建正式的新冻结实验、付费授权或消费记录；测试使用的合成计划/授权/Mock Store 仅位于系统临时目录。

## 4. 实际修改

| 文件 | 本轮变化 |
| --- | --- |
| [scripts/p65_answer_review.py](../scripts/p65_answer_review.py) | 增加严格 v2 类型与版本选择；共用记录规范化；阻断歧义/重复；隔离返回值与传入对象 |
| [scripts/p65_claim_quality.py](../scripts/p65_claim_quality.py) | S2 与容量分析读取两种契约；补充版本、引用字段数及投影适用性信息；门禁不变 |
| [scripts/p65_model_boundary.py](../scripts/p65_model_boundary.py) | 请求升级到独立 v5；明确五字段工具引用与三字段输入引用；预算不变 |
| [tests/test_p65_compact_refs.py](../tests/test_p65_compact_refs.py) | 新增 138 个离线测试节点，复用既有合成 corpus 与封存读取器 |
| [tests/test_p65_answer_review.py](../tests/test_p65_answer_review.py) | 仅更新当前请求/回答版本断言；旧 v1 审查测试保留 |
| [tests/test_p65_third_preparation.py](../tests/test_p65_third_preparation.py) | 更新当前版本断言，增加旧 v4 授权拒绝检查 |
| 本报告 | 记录离线兼容性、容量、测试和限制 |

没有新增第三方依赖、生产接口、持久化格式或第二套调查/审批系统。新增接口仅为实验审查层的 CompactToolCitation、InputCitation、CompactAnswer、parse_answer 和 resolve_tool_record。

## 5. 测试矩阵与原始回答保护

新增测试覆盖：合法 v1/v2 等价；身份字段缺失、未知 ID、p63/observation ID 错位；工具和参数错绑；布尔/整数/浮点参数严格区分；重复 citation/call/claim/refs/record/observation；输入路径错误及身份夹带；所有重复元数据与未知字段；必需声明字段；缺失/损坏记录、缺失来源字段；field_value 错值/缺路径；历史 no_data 零值及未读日志误报；真实历史零值/日志正文的正向场景；偏题与未经证实的自然语言；输入不变、结果一致、返回值隔离和严格 JSON；旧版本授权与计划隔离；经封存加载器认证后的 CLI v2 审查。

对既有 corpus 中 21 个适用样本，v1/v2 的规范化来源、问题、逐声明结果与门禁相同。两项模型自行伪造元数据的样本不通过“删字段转换”来证明等价：v1 逐项比对后拒绝，v2 从结构上拒绝这类字段。另对六种错误来源/重复情形保存实际[对比结果](../data/p65-s3-offline-20261022-01/binding-negative-comparison.json)：p63 call、错误工具、错误参数、重复 citation、重复 call 的问题代码一致；伪造 source 在 v1 为 provenance_mismatch，在 v2 为 answer_contract_missing_or_invalid，两者均阻断。

原始真实回答未转换、未改写，SHA-256 仍为 `ad84a451320c641e02c083169ac9f54fb8b85cd449bc5c3e914c8eed6f6bd4f8`；固定 manifest SHA-256 仍为 `ce0ce8dfe6af08d23410f392250081d011a45a552a61daa324d7ae6afa368f88`。原回答 S1/S2 审查对象与修改前逐项相同，仍报告协议缺失及三处历史 ID 被标为 call；没有倒记原回答从未宣称的日志读取或故障次数问题。既有 23 份 S2 corpus 审查对象也全部保持一致。

修改前审查结果已原样保存。新生成的前后审查文件分别使用 CRLF 与 LF，因此一次辅助字节相等断言退出 1；确认只有换行差异后，严格 JSON 对象与 canonical SHA 比较通过，没有重写这些文件。S1 canonical SHA 为 `befc48caeef7cdb6491c972aac9d9f380202030e4e7d1402fb7ef979573ba4e4`，S2 为 `ec99fb311ef4678eee51c00d2505da5f3fcf6b77b687fb06ed61a8b6a70ae2fd`。原始回答的字节 SHA 与报告序列化 SHA 是不同对象，不能混称。见[前后报告哈希记录](../data/p65-s3-offline-20261022-01/report-integrity-comparison.json)及[兼容性摘要](../data/p65-s3-offline-20261022-01/compatibility-summary.json)。

偏题、根因/修复/当前健康/业务恢复文字若伪装为 observation，现有规则仍不自动理解；测试只确认它们必须人工复核，未把门禁拒绝自动采纳计为识别了语义错误。显式 use 的无依据事实继续返回既有问题代码。

## 6. 容量比较

两个样本均为开发 Agent 手工测试样本，非 Provider 原答或正式人工批准。保留完全相同的 claims；完整 JSON 统一使用 ensure_ascii=False、indent=2 和结尾换行，同时单独比较无非必要空白的 JSON。小样本完整字节数比 S2 旧报告多 1，是此处统一保留结尾换行导致。

| 合法样本 | 引用/声明数 | 完整 UTF-8：v1 → v2 | 节省 | 无空白 JSON：v1 → v2 | 节省 | 引用数组字节：v1 → v2 |
| --- | --- | --- | --- | --- | --- | --- |
| S1 人工样本，使用已独立核验记录 | 6 / 8 | 5856 → 4139 | 1717，29.32% | 4241 → 2914 | 1327，31.29% | 2239 → 899 |
| S2 合成完整合法小样本 | 1 / 3 | 1594 → 1207 | 387，24.28% | 1170 → 871 | 299，25.56% | 465 → 153 |

S1 四条工具引用各由 14 个字段降为 5 个，两条 input_context 各保留 3 个，总引用字段数 62 → 26；S2 为 14 → 5。新版版本名增加的字节已包含在完整回答比较中。

两组规范化引用来源完全相同，其编码 SHA-256 分别为 `dc9d6e351dc6a40b2b5b8b150c88b221bf566aac09b99ff3bd42bf58145aeb89` 和 `a5c050b64acc368857efa352c170cb21413341a680ac922403a63d76b8e7fa3f`。S1 仍有 5 项字段声明验证匹配；两组 claims、问题代码、字段检查与门禁结果分别相同。详情与完整样本副本保存在[容量对比](../data/p65-s3-offline-20261022-01/capacity-comparison.json)和同目录的明确标为 handcrafted 的文件中，没有冒充 Provider 输出。

**这里只测量字节，没有同模型 tokenizer 或新 Provider usage。** exact_deepseek_tokens=null，未提高 2048 上限，也不能根据约 25–31% 的字节减少声称精确 token 节省、一定不超限或真实模型一定能遵循 v2。

## 7. 实际命令、测试批次与重叠

环境：`D:\conda\envs\agentlearn\python.exe`，Python 3.12.14；pytest 9.1.1、Pydantic 2.13.5、OpenAI 3.14.0、httpx2 2.13.0。测试通过独立[离线进程脚本](../data/p65-s3-offline-20261022-01/offline-runner.py)执行，实际 Python 命令为：

```powershell
$p65Python = 'D:\conda\envs\agentlearn\python.exe'
& $p65Python -B 'data\p65-s3-offline-20261022-01\offline-runner.py' new
& $p65Python -B 'data\p65-s3-offline-20261022-01\offline-runner.py' related
& $p65Python -B 'data\p65-s3-offline-20261022-01\offline-runner.py' analysis
& $p65Python -B 'data\p65-s3-offline-20261022-01\offline-runner.py' p65-regression
& $p65Python -B 'data\p65-s3-offline-20261022-01\offline-runner.py' new-final
```

每个测试批次的精确 pytest 参数、node ID、Python 命令及退出码保存于相应 exit.json，JUnit 独立记录数量。测试参数统一为目标文件加 `-q -p no:cacheprovider --tb=short --junitxml=<对应路径>`。

| 批次 | passed | failed / errors / skipped | 退出码 | 实际耗时 | 日志 / JUnit / 退出记录 |
| --- | ---: | --- | ---: | --- | --- |
| S3 首轮 | 135 | 0 / 0 / 0 | 0 | 57.35s | [日志](../data/p65-s3-offline-20261022-01/new-tests.log) / [XML](../data/p65-s3-offline-20261022-01/new-tests.xml) / [退出](../data/p65-s3-offline-20261022-01/new-exit.json) |
| S1、S2/S2.1、ResultTrust | 197 | 0 / 0 / 0 | 0 | 13.48s | [日志](../data/p65-s3-offline-20261022-01/related-tests.log) / [XML](../data/p65-s3-offline-20261022-01/related-tests.xml) / [退出](../data/p65-s3-offline-20261022-01/related-exit.json) |
| 其余 P6.5 回归 | 169 | 0 / 0 / 0 | 0 | 220.11s | [日志](../data/p65-s3-offline-20261022-01/p65-regression-tests.log) / [XML](../data/p65-s3-offline-20261022-01/p65-regression-tests.xml) / [退出](../data/p65-s3-offline-20261022-01/p65-regression-exit.json) |
| S3 最终版本，补充正向/严格字段场景 | 138 | 0 / 0 / 0 | 0 | 14.50s | [日志](../data/p65-s3-offline-20261022-01/new-final-tests.log) / [XML](../data/p65-s3-offline-20261022-01/new-final-tests.xml) / [退出](../data/p65-s3-offline-20261022-01/new-final-exit.json) |

首轮 135 项全部包含于最终 138 项，不能相加。最终 138、197、169 三批 node 集合两两无交集，合计 **504 项**，失败/错误/跳过均为 0，无 deselected。197 项为 S1 62、S2/S2.1 130、ResultTrust 5；169 项为 readonly 45、dynamic budget 38、protocol compatibility 58、third preparation 28。共享 fixture 和链路的节点不代表独立业务场景数。见[机器可核对统计](../data/p65-s3-offline-20261022-01/test-summary.json)。

analysis 批次只运行比较脚本，不调用 pytest，不计入测试数量；退出 0，原始结果见[日志](../data/p65-s3-offline-20261022-01/analysis.log)。其 exit.json 中的 arguments 是 runner 初始化的 pytest 参数，未用于分析执行，应以 command、analysis.log 和实际代码分支为准。

## 8. 静态检查、保护核验与安全边界

六个 Python 文件均通过 AST 与不执行代码的 compile 检查，无尾空白；新增或实质修改的行均在 88 列内，boundary 文件既有七处长行保留。目标 diff 的 `git diff --check` 通过；JSON 输入解析拒绝重复键及 NaN/Infinity/溢出数值，测试与分析输出均可严格 JSON 序列化。实际静态证据和源码 SHA 在 [static-checks.json](../data/p65-s3-offline-20261022-01/static-checks.json)，没有声称运行外部类型检查器或完整仓库回归。

原始[保护基线](../data/p65-s3-offline-20261022-01/protection-baseline.json)与[报告前核验](../data/p65-s3-offline-20261022-01/pre-report-protection.json)显示：7,376 个既有条目中，仅五个授权已有文件变化；其余 7,371 个条目的 SHA-256、大小、mtime_ns 与链接目标完全一致，无非预期变化或删除。新增内容仅为测试、本报告及独立审计输出目录；后续最终核验见 [final-protection.json](../data/p65-s3-offline-20261022-01/final-protection.json)。初次清单遇到既有测试符号链接而中止后，采用 lstat 记录链接本身，没有跟随、删除或改写链接。

保护范围包含 .env、既有源码与用户修改、data、SQLite、原始请求/回答/授权/消费记录、历史报告及 logs；排除 .git、虚拟环境、node_modules 和 Python/pytest 等缓存，并单独排除本轮新建的审计目录。未把 SHA 清单误称为对未检查路径的全面认证。

离线测试/分析进程使用 audit hook 拒绝业务网络连接、DNS、数据报、子进程/系统命令和项目既有文件写入；另阻断 connect_ex、真实 ActionExecutor.execute 及 RecoveryWorker.start。各批次 forbidden_attempts 均为空。related 批次按标准库函数 code 身份放行 1 次 Windows 内部 socketpair 自连接，用于进程内 TestClient；它不是业务 HTTP 或 Provider 请求。

真实付费模型请求=0，业务 HTTP=0，实时 Prometheus/Loki 查询=0，故障注入=0，真实修复/Remediation=0，历史 Session 恢复=0，新正式付费授权/消费记录=0，后台恢复启动=0。没有启动故障开关、服务或待恢复的业务资源。Git 远端 SHA 核对属于版本管理读取。

提交范围仅为第 4 节七个文件，data 的审计、样本副本、原始请求/响应、授权/消费及本地运行材料不加入 Git。当前任务明确授权提交，未明确要求推送，因此本轮不推送；实际提交 SHA 在最终交付回复中给出。

## 9. 验收结论与停止点

新协议通过本轮离线验收，两个合法样本均有清晰字节精简收益；引用身份绑定、规范化来源和字段验证保持，伪造来源从逐字段校验改为结构拒绝。v1 历史审查能力及旧报告结果保留，v5 请求与旧授权隔离，没有未解释的原件或工作区完整性差异。

剩余风险是开放自然语言、外部来源权威性、当前业务状态、真实 Provider 遵循率和精确 token 容量；单次固定封存案例及手工 corpus 不能证明通用调查准确率。新协议仍是实验审查层能力，没有把人工审查升级为生产自动采纳。

完成选择性提交及核验后停止，不进行新真实模型实验，不进入 P6.6。
