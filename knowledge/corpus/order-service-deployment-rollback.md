# order-service 版本核查与变更回退操作指南

- **doc_id**: `order-service-deployment-rollback`
- **service**: `order-service`
- **status**: `active`
- **version**: `v2.0`
- **updated_at**: `2026-11-10`
- **alert_names**: `HighHttp5xxRate`, `HighHttpLatency`
- **error_codes**: `ERR_CODE_REGRESSION`, `ERR_VERSION_MISMATCH`

## 运行版本与进程启动时间核对

在怀疑代码发布导致 `order-service` 异常时，必须严格区分**进程启动时间**与**代码部署时间**，避免仅凭时间接近就建立因果关系。

1. 通过运行时版本接口查询 `order-service` 的版本信息：
   - `APP_COMMIT_SHA`：需为 40 位十六进制 Git 提交哈希。若运行时未注入或格式不合法，接口会明确返回 `unknown`（或 `ERR_VERSION_MISMATCH`），此时不得猜测当前运行代码版本。
   - `PROCESS_STARTED_AT`：记录的是当前 Flask 进程初始化的 UTC 时间，仅代表进程最近一次重启时间，不代表 CI/CD 发布时间。
2. 核对告警首次触发时间（`startsAt`）与 `PROCESS_STARTED_AT` 的先后顺序。若进程重启远早于告警触发，且期间无版本变更，则代码发布直接致障的可能性较低，应重点排查运行时配置、依赖状态或流量变化。

## 代码变更比对范围

### 核心业务文件 app.py 与观测埋点

`order-service` 在服务注册表中受限允许检查的源码路径仅包含：
- `app.py`：包含 `/`、`/error`、`/order`、`/health`、`/metrics` 路由以及 `SIMULATE_CODE_REGRESSION` 逻辑与 PostgreSQL 查询逻辑。
- `http_observability.py`：包含 `instrument_business_requests` 指标埋点与 `SIMULATE_SLOW_REQUEST`（固定延迟 `0.8s`）中间件逻辑。

排查流程：
1. 若 `APP_COMMIT_SHA` 已知，拉取该提交及其前序提交对 `app.py` 与 `http_observability.py` 的变更 diff。
2. 重点核查是否存在异常捕获被吞掉、SQL 超时参数被改动、`SIMULATE_*` 默认值被修改或指标标签基数变化。
3. 若只看到历史提交改动，但无法确认当前运行进程是否加载了该提交（例如 `APP_COMMIT_SHA` 为 `unknown`），必须将该结论标记为“待核实假设”，不得当作已确认根因。

## 回退与审批边界

- 若确认故障由可控的回归演示开关导致，优先使用已注册的 `disable_demo_regression` 修复提案走标准审批流，而不是手动修改线上源码。
- 若确认属于真实代码版本回归需要版本回退，需在人工审批确认后，按标准发布流水线切换镜像或 Git 提交并重新验证 `APP_COMMIT_SHA` 与 `/order` 业务读链路。
- 严禁在未确认工作区干净状态和虚拟环境一致性的前提下直接覆盖生产或隔离演示目录中的 `.py` 文件。
