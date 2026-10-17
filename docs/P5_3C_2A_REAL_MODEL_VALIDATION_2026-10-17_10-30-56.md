# P5.3c.2a 独立模型输出优化与一次真实复测

本轮已按授权完成唯一一次真实请求，未重试。回答完整性和篇幅目标达到，但出现未限定记录范围的时间概括错误、版本 no_data 事实引用遗漏，因此仍未通过 P5.3c.2 严格验收。没有把 Session COMPLETED 当作推理通过，未提交或推送。

## 任务边界与代码变更

基线及本轮开始时 HEAD 均为 `b25519dd9566aa27d5ab113155e2fd8d52c7f3cd`，分支 main；本地 origin/main 与 HEAD 差异为 0/0，未执行 fetch、commit 或 push。本轮用户已明确持续授权真实模型测试，每轮最多 1 次；本次不再次询问，仍保留 CLI 双安全参数，禁止 SDK 及应用自动重试。

开始时工作区已有 README、生产 Prompt、temp_test.py 及文档整理修改。本轮只修改独立入口的 TASK 和既有离线测试断言，新增本报告和本轮隔离产物。未改变生产 Prompt、Worker、状态机、预算、Checkpoint schema、数据库表、Prometheus Client、修复权限或依赖。沿用现有 Agent/ModelClient/Worker/RuntimeStore 和分析器，无新增接口、数据结构或第三方包。

`scripts/preflight_real_model.py`：只替换 TASK，指定五段简短中文、约 500～800 汉字、关键事实就近引用真实 observation_id、最多三项后续验证方向；显式由本轮五段替代通用报告栏目，并加入未知时间规则。没有提高 2048 token 请求上限，也没有修改返回处理或工具能力。

`tests/test_real_model_preflight.py`：扩充两个既有测试，核对 TASK 确实送达普通请求及安装 SDK MockTransport 的序列化边界；核对全部 5 条记录的观测、参数、时间与历史用途标识；核对 4 条指标分析无排除，完整派生报告与分析器结果经 JSON 归一化后一致；expected 字段及人工标准原文未进入模型请求。

## 首次失败分析与本次设计依据

首次报告与 `data/model-validation/p53c2-n41oz3xk` 原始产物已只读复核，旧报告/请求/响应均保留。旧 request 设置 2048；原始 response 为 incomplete，incomplete_details.reason=max_output_tokens；usage 为输入 11782、输出 2049、reasoning 0。旧正文 4063 字符，其中基本 CJK 汉字 1149 个，含 6 节，结尾停在“以下仅为方向性说明，不”。

已确认的直接截断原因仅为返回的 max_output_tokens。原 TASK 无篇幅/条数/去重要求，生产 Prompt 同时有通用栏目要求；重复陈列原始事实、表格及限制可能促进展开，这是推测，不能声称已通过对照实验证明 Prompt 长度造成截断。输出 usage 超请求上限 1 token 的原因仍未知。

首次推理错误确实存在：Alert starts_at/observed_at/window 均 null，却声称告警值与快照值位于不同评估时点。缺失时间只支持关系未知。本次添加通用规则，没有把“本案例某告警与某快照的标准关系”写进新 Prompt。

## 离线验证与实际请求审计

实际解释器 `D:\conda\envs\agentlearn\python.exe`；OpenAI SDK 3.14.0、httpx2 2.13.0、Pydantic 2.13.5、python-dotenv 1.2.3、pytest 9.1.1。pip check 返回 No broken requirements found。

定向最终命令 `python -B -m pytest tests/test_real_model_preflight.py -q -p no:cacheprovider`：38 passed in 20.11s。首次执行为 1 failed、37 passed in 24.88s，失败来自本轮新增比较断言未归一化 tuple/list；已修正测试表示比较，未删减事实或安全断言。首次提前开始的完整回归在此期间仅终止了经 PID/父 PID/完整命令确认的自有 pytest，退出 -1、无最终统计，不计成功或失败；修正后再执行最终完整回归。

默认离线 CLI 返回 COMPLETED、real_model_api_requests=0；它的回答是明确标注的传输占位，不能作为真实推理表现。离线请求正文紧凑 UTF-8 50027 字节，小于 98304 字节上限；instructions 17773 字符、TASK 394 字符。与首次真实请求相比，instructions 逐字等于仅替换 TASK 后的结果，4 条原始指标夹具完全相同。

生产 Prompt 实际内容 SHA-256 始终为 `94faddc0b0029a11be7f815d571f3fad47e0d2cad6305be0aa899752f8c7350a`，其中含用户任务前已有的未提交修改，不能声称这等同于一个干净基线检出的 Prompt。

请求含全部原始历史/缺失结果及完整 metric_analysis，selected_record_count=4，各 excluded_record_counts=0；分析状态 partial，保留 T2 缺失而未补零。未注入 case.json.expected、Mock 标准回答或人工验收 rubric。基线 TASK 自带 A aligned/B 错位/非独立佐证背景，Runtime 也自带时间分类、缺失分类及当前状态限制：本次不是无提示的独立根因发现测试。篇幅限制是 Prompt 软约束，离线只能证明送达。

固定请求 deepseek-flash，https://api.deepseek.com/responses；max_output_tokens=2048、reasoning.effort=none、tools=[]、tool_choice=none；Runtime/Provider 最多一次模型请求，max_attempts=1、resume=0、human requests=0；SDK 构造和 with_options 均 max_retries=0。SDK 超时 30 秒，监督自有子进程总时限 90 秒。失败/超时/不完整/空回答/非法工具均保持既有停止行为，不重新请求。

每次 CLI 使用新建空 OS 临时目录及隔离日志、SQLite/Incident JSON，拒绝生产目录和非空目录。不启动生产 main、Monitor、Scheduler、RecoveryWorker；Manager.remediation_planner=None，不实例化真实修复执行器，无故障开关或生产变更。工具模块会导入仅保存 URL/clock 的 Prometheus/Loki Client，但构造函数不发请求；所有工具执行被本地精确夹具替代。模型工具调用为 0，Runtime bootstrap 本地固定 get_running_version 探针为 1，返回 no_data，不访问真实 Prometheus/Loki/业务服务，不能误记为在线探测或修复。

安全测试覆盖默认禁网、SDK MockTransport、重复调用拒绝、HTTP 401/402/403/429/500、超时、incomplete、空回答、非法工具、目录/日志隔离、输入上限、自有进程停止和密钥回显脱敏。传入 Mock SDK 的 live=True 测试全部受禁网夹具保护，属于离线测试，不是实际付费请求。

## 真实调用与实际返回

离线定向、默认 CLI、请求审计、pip check 及最终完整回归全部通过后，直接按已有授权执行了以下命令一次；没有再次询问，也没有再次运行本命令：

```powershell
& 'D:\conda\envs\agentlearn\python.exe' -B 'D:\Incident Agent\scripts\preflight_real_model.py' --run-live --approve-paid-model-run
```

本轮实际付费 SDK 请求入口为 **1 次**，provider_request_entries=1、Runtime 模型预留=1，收到一个真实服务端 Response。此计数是本地 SDK create 入口次数，不是独立的服务端账单审计；SDK 和应用均无自动重试。CLI 退出码 0、Session COMPLETED 仅表示返回完整并完成本地流程，不能代替下方人工推理验收。

| 项目 | 实际结果 |
| --- | --- |
| 请求/返回模型 | deepseek-flash / deepseek-flash |
| 请求输出上限 | max_output_tokens=2048，未提高 |
| 思考/工具 | reasoning.effort=none；tools=[]；tool_choice=none |
| response.status | completed |
| incomplete_details / error | null / null |
| 输出 item | 1 个 message，status=completed；无模型工具或 reasoning item |
| usage.input_tokens | 11886 |
| usage.input_tokens_details.cached_tokens | 0 |
| usage.output_tokens | 644 |
| usage.output_tokens_details.reasoning_tokens | 0 |
| usage.total_tokens | 12530 |
| 正文长度 | 1279 个总字符，其中 U+4E00～U+9FFF 汉字 514 个；五段及最多三项建议完整 |
| 请求/响应时间 | 版本夹具 collected_at=2026-10-17T02:24:55.518727+00:00；response.created_at=1792203898，即 2026-10-17T02:24:58+00:00；summary.created_at=2026-10-17T02:25:02.217693+00:00 |

真实 request 与最终离线 request 的 instructions、全部固定请求参数、case.json 和完整 metric_analysis 均一致；只读复核再次确认人工 expected 未进入真实请求。动态 Incident/Session/版本观测 ID 及本次采集时刻按各次运行生成，不要求离线和真实请求逐字相同。

新进程使用 SQLite mode=ro 重读归档数据库，Session 为 attempt_count=1、current_attempt_rounds=1、current_attempt_tool_calls=1、total_model_requests=1、total_tool_executions=1、resume_count=0、human_request_count=0。Checkpoint 结果 success=true/status=completed，model_rounds/tool_calls=1/1。事件 model_reserved=1、model_response_saved=1、investigation_completed=1、tool_reserved=1；无 waiting_retry 或 remediation 事件。事件 tool_failed=1 来自本地版本夹具的 no_data 分类，不能解释成真实服务探测失败。

## 人工逐字段验收

主 Agent 与两个独立只读审查 Agent 均对照实际 request、raw response 和 Checkpoint，结论一致：**本轮不通过 P5.3c.2 严格验收**。该结论基于实际字段和原始回答，未用关键词命中或 completed 直接判定。

| 用户验收项 | 实际表现 | 判定 |
| --- | --- | --- |
| 完整回答 | 五段齐全，最后三项建议完整结束；completed、无 incomplete_details；644 输出 token | 通过 |
| 告警时间未知 | 明确 starts_at/observed_at/window=null，无法判断与快照的时间关系 | 通过；首次错误已纠正 |
| T1/T2 历史观测 | 四个 case observation_id 均真实；T1=2026-10-11T10:48:55Z，T2 晚 300ms；0.418 为原始 0.41821223361699034 的小数简写 | 通过，原始精度仍在 request |
| 0.0/no_data/失败 | 明确 T2 请求率有效零、5xx 缺样本不代表零错误或健康；incomplete 是比较不完整而非工具失败 | 通过 |
| 不同时刻不拼接 | B 组不能合成同刻快照；A 的 t1/t2 比较明确跨快照 | 通过 |
| 历史不冒充当前健康 | 明确不能判断当前状态或业务恢复 | 通过 |
| 根因保持未经验证 | 未将 order-db、历史 5xx 或零请求升级为当前根因 | 通过 |
| 不捏造来源/数值/时间 | 四个已引用 ID 均存在，没有新造 ID；但对所有观测的采集/评估日期作了错误全称概括 | 时间字段范围不通过 |
| 简短结构化要求 | 五段、514 个汉字、三项建议；低于 2048 token | 通过 |

**已确认错误：没有限定指标范围的时间泛化。** 回答先把 get_running_version 纳入“历史观测”，随后说：

> 所有历史观测均标记 expired=true、use_for_current_state=false；采集时间为 2026-10-14，评估时间为 2026-10-11

expired/use_for_current_state 两个标记对本次五条记录均正确。但后半句只适用于四条固定历史指标，不能覆盖版本记录：

| 记录 | 实际 collected_at | 实际评估时间 |
| --- | --- | --- |
| 四条 case-* 指标 | 2026-10-14T02:00:00+00:00 | 2026-10-11 的 T1 或 T2 |
| obs-e335bee747c047c4a77b9660dda46ff7，本地 get_running_version no_data | 2026-10-17T02:24:55.518727+00:00 | 没有 evaluated_at/evaluation_time，必须保持未知 |

这是新的时间范围错误；不能把它写成“首次告警未知时间错误重现”，因为本次对告警与快照关系的判断确实已经正确。也不能因为其最后没有声称恢复，就忽略错误的字段概括。

**关键事实引用不足：** 回答写了 get_running_version=no_data，却未引用实际 `obs-e335bee747c047c4a77b9660dda46ff7`。该事实有真实输入支持，属于引用遗漏，不是来源捏造。四个 case 引用本身均可追溯。自动提取 ID 的集合检查只作为辅助，人工审查同时检查了未引用的事实。

以下疑点经核查不应夸大为已确认错误：

- “阈值方向未给出”与实际 alert_event.direction=null、policy=null 一致。0.3>0.2 的算术关系不等于证明当前值仍为 0.3，更不能补全缺失的时间和方向。
- “A 案 t1 与 t2 相差 300ms，严格时间对齐不成立”的对象是两个快照，不能强行读成否认单个快照中已有样本的评估时间对齐。
- `service_scrape_up=1` 被表述为“对应评估时刻抓取成功”，较首次“目标在线”保守，但应进一步收紧为“该评估返回抓取成功样本”。回答没有重述同评估时间不保证同抓取时间、多次 HTTP 查询不具原子性，这是语义/覆盖缺口；没有足够依据说它断言了业务健康。
- “无运行身份”应写成“未取得运行身份证据”，不能从 no_data 推断运行身份本身不存在。结合后文“没有运行版本证据”，本报告将其列为表述精度问题。

Runtime 已经确定性提供原始数值的整理、时间分类、缺失分类、当前状态限制与 suggested_checks；基线 TASK 也预告 A/B 的案例类型。模型正确复述这些内容，只能证明它在本次上下文中的解释表现，不能描述为模型独立发现了根因或已验证一般根因推理能力。

## 费用估算与首次对比

本轮于 2026-10-17 重新读取 [DeepSeek 官方人民币价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。当前页面规定周末为空闲时段；本次为北京时间周六上午，因此采用 Flash 未命中缓存输入 1 元/百万 token、输出 4 元/百万 token。实际 usage.cached_tokens=0，reasoning_tokens=0，计算为：

`11886 × 1 / 1000000 + 644 × 4 / 1000000 = 0.014462 元`，约 **0.0145 元人民币**。

若仍沿首次报告的“高峰且无缓存”保守口径，则为 `11886×2/1000000 + 644×8/1000000 = 0.028924 元`。本轮未查询账单、扣款或余额，以上都是按 usage 和本次读取标价的估算，不能称为已核实实际扣款。

| 对比项 | 首次 P5.3c.2 | 本轮 P5.3c.2a |
| --- | --- | --- |
| 真实请求数 | 1 | 1，无追加请求 |
| 请求输出上限 | 2048 | 2048 |
| 输入/输出/reasoning token | 11782 / 2049 / 0 | 11886 / 644 / 0 |
| 状态 | incomplete / max_output_tokens | completed / null |
| 总字符 / 基本 CJK 汉字 | 4063 / 1149 | 1279 / 514 |
| 告警时间未知的处理 | 错误断言不同评估时点 | 正确保持关系未知 |
| 新发现的限制 | 回答截断 | 日期范围泛化、版本事实引用遗漏 |
| 费用估算 | 高峰无缓存 0.039956 元，约 0.04 元 | 空闲无缓存 0.014462 元；相同高峰口径 0.028924 元 |
| 严格推理验收 | 不通过 | 仍不通过 |

输出 token 显著减少，未通过增大上限解决；输入反而增加 104 token。费用比较还受到周末费率变化影响，不能把全部费用变化归因于 Prompt 优化。这只是固定案例的一次样本，不能据此估计稳定通过率。

## 原始产物、哈希与保护检查

本轮原始临时目录为 `C:\Users\zp1222\AppData\Local\Temp\incident-p53c2-g9tpbkin`，12 个原始文件完整复制到新建归档 `D:\Incident Agent\data\model-validation\p53c2a-g9tpbkin`，逐文件 SHA-256 一致，另存 artifact-manifest.json。没有复用或覆盖首次目录。运行入口原 summary.manual_reasoning_review=pending 保持原样，最终人工判定另存 manual-review.json，避免改写原始结果。

- [原始响应正文](../data/model-validation/p53c2a-g9tpbkin/response.raw.json)：`e1d7e46a60f896a700d6de751dd64a2105bbe74e7529f1ff900f87676a475006`。
- [SDK 响应](../data/model-validation/p53c2a-g9tpbkin/response.sdk.json)：`c09508185901441998d81e281378cfd1c3bf3d9c99ad37908fe5cafd0a1eae18`。
- [实际请求](../data/model-validation/p53c2a-g9tpbkin/request.json)：`62d5abc2419de4fa69d2c248f442631b3032142a55ac31e39c8d9fc58f4bbaab`。
- [人工判定与费用明细](../data/model-validation/p53c2a-g9tpbkin/manual-review.json)、[原始文件哈希清单](../data/model-validation/p53c2a-g9tpbkin/artifact-manifest.json)。
- [最终全量日志](../data/model-validation/p53c2a-g9tpbkin/verification/full-regression-final.txt)：`563c1e0d5293fc5cc1d62d87c80500bb44de76bea74948ca0acbc7aae0960bac`。
- [最终定向日志](../data/model-validation/p53c2a-g9tpbkin/verification/targeted-preflight-final.txt)、[离线请求审计](../data/model-validation/p53c2a-g9tpbkin/verification/offline-audit.json)、[付费前安全门](../data/model-validation/p53c2a-g9tpbkin/verification/pre-live-gate.json)。初次定向失败及完整回归中止日志也保留在 verification；默认离线原始产物保留在 offline-preflight。

首次原始响应 SHA-256 仍为 `7ff9426e4f35a4a8c11000ecc978919a66c96cfcfec9ce71fab1535c82df95a8`，旧请求仍为 `b6e9fedbaed37a662d49b6c7365f2085f53e756b6cfa9f45b0b5414747c99322`；旧报告正文未更新。

任务前保存 501 个既有文件内容哈希。离线后、付费前及交付前核对，仅本次 scripts/tests 两个文件发生预期变化，其他原有用户修改、日志、运行数据和前次产物内容均未变；Git index 内容哈希保持不变。新请求、响应、归档及交付文本按配置密钥的实际值扫描无命中；未保存 Authorization 头或打印密钥。归档仍受现有 `/data/` 忽略规则保护，不加入 Git。报告中的本地产物在其他检出环境不会自动存在。

## 最终验证与停止点

最终完整命令 `python -B -m pytest -q -p no:cacheprovider` 实际退出码 0：**1684 passed、2 skipped、190 subtests passed in 411.45s**；进程总耗时 418.807s。无 warnings summary。38 项定向结果被完整回归覆盖，不能将二者简单相加为独立测试数量。两项 skipped 不计通过；本次 `-q` 日志未展开具体跳过原因。

git diff --check 对本次脚本、测试和新报告通过；Git 的 LF/CRLF 提示不是测试失败。本次 diff 只改 TASK 及离线断言，入口 AST 除 TASK 外与基线一致。未生成新业务接口/数据结构、未增加依赖、未修改生产控制能力。完整回归和真实模型复测不能证明真实 Prometheus/Loki/业务服务健康、模型自主取证或多轮模型链兼容。

尚未验证：实际扣款、其他案例/多次采样的稳定性、真实历史服务数据、当前业务健康与恢复、模型自主工具选择和多轮 Responses。严格验收失败的原因已明确，不再额外请求或在真实输出后继续调整代码。

建议后续先审查本次 TASK 与报告；如继续优化，仍只在独立 TASK 加通用“按 observation_id 分别限定采集/评估时间，禁止把一类记录的字段外推到全部记录；所有关键 no_data 事实也必须引用 ID”的规则，不写本案版本 ID 或标准日期。可先离线检查规则送达，再按用户持续授权在新一轮任务中最多调用一次，未经另外同意不扩大次数或预算。

本轮已完成一次已授权复测并保存实际失败结果，**无第二次请求，未 commit、未 push，停止等待代码审查**。

相关源码：[独立入口](../scripts/preflight_real_model.py)、[离线安全测试](../tests/test_real_model_preflight.py)、[固定历史夹具](../tests/test_metric_analysis.py)。历史参照：[首次真实验证报告](P5_3C_2_REAL_MODEL_VALIDATION_2026-10-16_17-16-48.md)、[首次预检报告](P5_3C_2_REAL_MODEL_PREFLIGHT_2026-10-16_17-00-22.md)。
