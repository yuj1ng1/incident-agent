import math
import requests
from tools.tool_registry import tool
from clients.prometheus_client import PrometheusClient
from config import PROMETHEUS_URL
from services.registry import registry, CapabilityUnavailable
from services.metric_profiles import build_promql


prometheus_client=PrometheusClient(
    base_url=PROMETHEUS_URL
)


@tool
def query_metrics(service: str, metric: str) -> dict:
    """
    查询指定服务的 HTTP 或 PostgreSQL 监控指标，返回单个数值。

    Args:
        service: 需要查询指标的服务或依赖名称，指标能力从 ServiceRegistry 读取
        metric: HTTP 支持 http_requests_total、http_request_rate、http_5xx_rate；PostgreSQL 支持 postgres_up、postgres_connections、postgres_max_connections、postgres_connection_usage、postgres_active_sessions、postgres_idle_sessions、postgres_idle_in_transaction、postgres_lock_waits
    """

    try:
        settings = registry.require_capability(service, "metrics")
    except CapabilityUnavailable as exc:
        return {
            "status": "no_data",
            "service": service,
            "metric": metric,
            "reason": exc.reason,
            "message": "没有找到该服务的指标能力配置",
        }
    promql = build_promql(
        service,
        settings,
        metric,
    )
    if promql is None:
        return {
            "status": "no_data",
            "service": service,
            "metric": metric,
            "reason": "metric_not_supported_by_profile",
            "message": "当前监控 profile 不支持该指标",
        }
    try:
        response = prometheus_client.query(promql)
    except (requests.RequestException, ValueError):
        return {
            "status": "no_data",
            "service": service,
            "metric": metric,
            "reason": "metrics_unreachable_or_invalid",
        }
    if (
        not isinstance(response, dict)
        or not isinstance(response.get("data"), dict)
    ):
        return {
            "status": "no_data",
            "service": service,
            "metric": metric,
            "reason": "invalid_metrics_response",
        }

    results = response.get(
        "data",
        {},
    ).get(
        "result",
        [],
    )

    if not isinstance(results, list):
        return {
            "status": "no_data",
            "service": service,
            "metric": metric,
            "reason": "invalid_metrics_response",
        }

    if not results:
        return {
            "status": "no_data",
            "service": service,
            "metric": metric,
        }

    # 实例级指标不能把多个实例的值随意合并，也不能静默取第一条。
    if len(results) != 1:
        return {
            "status": "no_data",
            "service": service,
            "metric": metric,
            "promql": promql,
            "message": "Prometheus 返回多个结果，无法确定单个指标值，请检查监控目标和 PromQL",
        }

    try:
        value = float(results[0]["value"][1])
    except (ValueError, TypeError, IndexError, KeyError):
        return {
            "status": "no_data",
            "service": service,
            "metric": metric,
            "reason": "invalid_metrics_response",
        }

    if not math.isfinite(value):
        return {
            "status": "no_data",
            "service": service,
            "metric": metric,
            "message": "Prometheus 返回的指标值不是有限数值",
        }

    return {
        "status": "ok",
        "service": service,
        "metric": metric,
        "value": value,
        "promql": promql,
    }
