# Incident Agent

基于 Prometheus 告警、Loki 日志和服务健康检查的 SRE 故障排查 Agent。

当前版本：完成告警生命周期监控、Incident 持久化和回归问题修复。

## 当前内容

- `main.py`：创建对象、组装依赖并启动 Monitor。
- `monitor/incident_monitor.py`：轮询告警，调度调查、重新调查和恢复判断，并隔离单轮异常。
- `agent/agent.py`：执行调查，管理工具调用上限与缓存，收集和去重证据，返回结构化调查结果。
- `incident_store.py`：将 Incident、证据和调查记录保存到 `data/incidents.json`。
- `clients/`、`tools/`：查询真实 Prometheus 指标、Loki 日志和服务健康状态。
- `models/`：Alert、Evidence、Incident、InvestigationResult 和 InvestigationRecord。
- `model_client/`：模型调用及连接、限流和服务错误的重试。
- `tests/`：35 项隔离回归测试，覆盖生命周期、证据、调查结果和异常处理。

## 环境配置

依赖列在 `requirements.txt` 中，包括 OpenAI SDK、python-dotenv、Pydantic 2 和 requests。请使用 Python 3.10 或以上版本及兼容依赖；本次验证环境为 Python 3.12、Pydantic 2。

在项目根目录安装依赖，将 `.env.example` 复制为 `.env` 并填写 `DEEPSEEK_API_KEY`。服务地址和模型名称在 `config.py` 中配置。

默认连接 Prometheus `http://localhost:9090`、Loki `http://localhost:3100` 和 order-service `http://localhost:8001`。告警解析要求 firing alert 的 labels 包含 service、metric、severity，annotations 包含 threshold，value 能转换为数值。

```powershell
python -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
# 编辑 .env 填写密钥，启动所需服务后运行：
python main.py
```

Incident 数据文件在首次启动时自动创建。真实 `.env`、运行数据、日志、编辑器配置、Python 缓存和虚拟环境均排除在版本控制之外。

## 回归验证

```powershell
python -B -m unittest discover -s tests -v
```

现有 35 项测试全部通过，不调用外部模型或监控服务，数据与日志写入临时目录。main.py 的对象组装测试仍需非空 DEEPSEEK_API_KEY，但不需要真实密钥；可通过本地 `.env` 提供测试占位值。

2026-10-06 还验证了真实启动、两轮告警查询及一次使用合成告警的真实模型调查。详细验证记录及修复说明见 [REGRESSION_REPORT.md](REGRESSION_REPORT.md)。真实服务当时没有 firing 告警，生命周期变化由可控响应覆盖。

本次修复了浮点阈值边界、未完成模型响应误记成功、非法告警快照误触发恢复、调查异常丢失已收集证据，以及 NaN/Infinity 被保存为有效指标的问题。

## 已知限制

Store 仍按 fingerprint 保存单个 Incident：恢复后再次 firing 会创建新 ID，但覆盖同 fingerprint 的旧记录。后续需要决定是否保留历史 Incident；本次未修改该业务设计。

默认每 30 秒轮询，重新调查冷却为 5 分钟，http_5xx_rate 绝对增加至少 0.2 时可提前重新调查。Agent 最多调查 5 轮、执行工具 8 次。

仓库中的依赖清单用于记录直接依赖，版本尚未全部锁定。
