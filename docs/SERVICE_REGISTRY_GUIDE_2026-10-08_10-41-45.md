# 多服务 Incident Agent

服务定义的唯一入口是 `services/configuration.py` 的 `SERVICES`。启动加载
`services/registry.py`，校验为 Pydantic `ServiceProfile` 后，Tools 读取 Registry 并按
provider/profile 调用现有 Clients。Agent 只根据能力和证据选择调查路径。

## 注册一个 HTTP 服务

在 SERVICES 列表追加配置即可，不需要修改 Agent、Tools 或 Clients：

```python
{
    "name": "billing-service",
    "type": "http_service",
    "health": {
        "provider": "http",
        "base_url": "http://localhost:8003",
        "endpoint": "/ready",
    },
    "logs": {
        "provider": "loki",
        "labels": {
            "service": "billing-service",
            "environment": "demo",
        },
    },
    "metrics": {
        "provider": "prometheus",
        "profile": "http_service",
    },
    "runtime": {
        "provider": "http_version",
        "base_url": "http://localhost:8003",
        "endpoint": "/version",
        "allowed_flags": [],
    },
    "source": {
        "provider": "github",
        "repo": "owner/repository",
        "branch": "main",
    },
    "dependencies": [],
}
```

可选字段不配置就省略。`dependencies` 默认空列表，表示配置中没有依赖；不证明现实中
没有依赖。每个引用必须是已注册服务，重复服务名、重复依赖、自依赖和未知引用初始化失败。
依赖图允许多个服务相互依赖，Agent 应使用已有证据避免重复探测。

| 字段 | 含义及要求 |
| --- | --- |
| name/type | 唯一服务名、服务分类；分类不直接决定能力 |
| health | http 的 base_url/endpoint（默认 /health），或 postgresql 的 host/port/database/user/password_env |
| logs | loki 和非空 labels；label 名校验，值自动转义，模型不能传 LogQL |
| metrics | prometheus 的 profile；HTTP 可配置 labels（默认 service=name）；PostgreSQL 必需 job/database |
| runtime | http_version 的地址、endpoint（默认 /version）、allowed_flags |
| source | github 的 repo 和 branch；Token 仍取环境变量，模型不能指定仓库 |
| dependencies | 已注册服务名称列表，默认空 |
| remediation | 只声明固定 action，默认空；不会注册或执行 Agent Tool |

HTTP URL 必须是 http/https，禁止内嵌认证信息。配置不能提供 Python callback、任意 import
或密码字面值。`password_env` 只保存环境变量名称，实际数据库密码需在运行环境设置
`ORDER_DB_PASSWORD`。缺凭据返回 `database_credentials_not_configured`，不猜密码。
GitHub 使用 `GITHUB_TOKEN`，模型使用 `DEEPSEEK_API_KEY`；不要提交 `.env`。

## 能力发现和降级

```python
from tools.capabilities import get_service_capabilities

print(get_service_capabilities("inventory-service"))
```

返回 capabilities、providers、supported_metrics、missing_capabilities、dependencies 和
remediation。通过显式白名单构建响应，不序列化 Profile，不返回地址、密码、Token 或环境。
`dependencies` 是每个注册服务都有的查询能力，即使列表为空。

未知服务返回 `status=no_data, reason=service_not_registered`。
缺某个能力返回 `<capability>_capability_not_configured`，继续使用剩余来源。
能力已配置也可能后端不可达或没有样本，不能把能力存在或缺失作为健康结论。
Prompt 要求早期发现能力、只调用支持的指标、只沿已配置依赖调查，并说明证据缺口。

## Provider 与指标

- HTTP health 调用配置 endpoint，归一化 health/http_status/message。PostgreSQL 保留
  DNS、TCP、连接和 SELECT 1 的分阶段证据，不将驱动错误消息输出为证据以免暴露凭据。
- HTTP profile 复用 `demo_http_requests_total`，支持总数、1 分钟请求速率和 500 占比。
  **当前 http_5xx_rate 延续旧语义，只统计 status="500"，不包括其他 5xx。**
  分母为零或样本缺失返回 no_data，有限的零仍然是有效值。
- PostgreSQL profile 保留现有八个指标及实例级多结果拒绝逻辑。connection_usage
  用实例全部数据库连接数；database 只约束相应数据库指标；锁等待补零仍受 pg_up=1 保护。
- Runtime 校验响应 service、完整 SHA、UTC 启动时间，只返回配置的布尔开关。
  当前开关白名单限 `simulate_*`。没有 SHA 明确 commit_unknown，不猜部署版本。
- GitHub 延续路径/ref/SHA 校验、diff/source 大小上限和只读请求。

新增同类型服务只改 SERVICES，并接通采集基础设施。第三方服务的指标名称或标签若不同，
在 `services/metric_profiles.py` 添加可信模板和 METRIC_PROFILES，再配置新 profile。
增加新协议/provider 时才需要扩展 Profile 校验及 provider adapter；不用修改 Agent。

## 兼容与执行边界

config.py 的 SERVICE_URLS、SERVICE_DEPENDENCIES、MONITORING_TARGETS、DEPENDENCIES、
CODE_REPOSITORIES 保留为只读派生视图，配置不再维护两份。DEPENDENCIES 中密码字面值
已移除，改为 password_env。调查工具全部读取 Registry，不读取旧视图。

调查可以通用，因为 Tools 只读取证据；执行涉及具体服务的启动方式、身份和风险。
RemediationPlanner、ActionExecutor、Approval Gate、Audit 保持原策略：
只允许 order-service 的 disable_demo_regression，inventory 没有自动修复。
MAX_AGENT_LOOPS=7、MAX_TOOL_CALLS=13、缓存、预算提示及 Evidence 持久化保持不变。

## 两服务 Demo

在 D:\prometheus-demo 使用 `start_demo.ps1` 和 `start_inventory.ps1` 启动 8001/8002。
默认故障关闭，后者通过 `-SimulateInventoryError` 开启受控故障。
启动脚本拒绝未提交的运行源码，避免用 HEAD 冒充实际运行版本。
inventory 仅有自身 simulate_inventory_error 开关，完全没有数据库连接。

Prometheus 新增 inventory-service target，并用同一条按 service 分组的告警规则。
Alloy 明确将 app.log 标记为 order-service，将 inventory.log 标记为 inventory-service，
避免原先通配符把所有日志标记成 order-service。

验证：`python -B -m unittest discover -s tests -v`。新增 test-service 只在测试配置出现，
同一套 health/logs/metrics/dependencies/capabilities Tools 均可处理。
