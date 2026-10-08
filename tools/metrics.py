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
def check_scrape_target(service: str) -> dict:
    """读取 Prometheus 实际采集目标状态和 lastError，不能据此证明进程已退出。

    Args:
        service: 已登记监控能力的服务或数据库名称
    """
    try:
        settings = registry.require_capability(service, "metrics")
        response = prometheus_client.get_targets()
        if (
            not isinstance(response, dict)
            or response.get("status") != "success"
            or response.get("warnings")
        ):
            raise ValueError("invalid_target_response")
        expected = {"job": settings.job} if settings.profile == "postgresql" else (
            settings.labels or {"service": service}
        )
        targets = [
            target for target in response["data"]["activeTargets"]
            if all(target.get("labels", {}).get(k) == v for k, v in expected.items())
        ]
        for target in targets:
            if (
                not isinstance(target.get("labels"), dict)
                or any(not isinstance(value, str) for value in target["labels"].values())
                or target.get("health") not in {"up", "down", "unknown"}
                or not isinstance(target.get("lastError", ""), str)
            ):
                raise ValueError("invalid_target_fields")
        if not targets:
            return {
                "status": "no_data",
                "service": service,
                "reason": "target_not_found",
            }
        return {
            "status": "ok",
            "service": service,
            "targets": [
                {
                    "labels": target["labels"],
                    "health": target["health"],
                    "last_error": target.get("lastError", ""),
                    "last_scrape": target.get("lastScrape"),
                    "scrape_duration_seconds": target.get("lastScrapeDuration"),
                }
                for target in targets
            ],
        }
    except (
        CapabilityUnavailable,
        requests.RequestException,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        return {
            "status": "no_data",
            "service": service,
            "reason": "targets_unavailable_or_invalid",
        }


@tool
def query_metrics(service: str, metric: str) -> dict:
    """
    查询指定服务的 HTTP 或 PostgreSQL 监控指标，返回单个数值。

    Args:
        service: 需要查询指标的服务或依赖名称，指标能力从 ServiceRegistry 读取
        metric: HTTP 支持 http_requests_total、http_request_rate、http_5xx_rate、http_latency_p95_seconds、service_scrape_up；PostgreSQL 支持 postgres_up、exporter_scrape_up、postgres_connections、postgres_max_connections、postgres_connection_usage、postgres_active_sessions、postgres_idle_sessions、postgres_idle_in_transaction、postgres_lock_waits。P95 单位秒，5xx 单位比例，窗口 1m，至少 20 个请求；不足或缺失返回 no_data。
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
        or response.get("status") != "success"
        or bool(response.get("warnings"))
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
        raw_value = results[0]["value"][1]
        if isinstance(raw_value, bool):
            raise ValueError("invalid_boolean_measurement")
        value = float(raw_value)
    except (ValueError, TypeError, IndexError, KeyError, OverflowError):
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

    if (
        metric in {"service_scrape_up", "exporter_scrape_up", "postgres_up"}
        and value not in {0, 1}
    ) or (
        metric == "http_5xx_rate"
        and not 0 <= value <= 1
    ) or (
        metric == "http_latency_p95_seconds"
        and value < 0
    ):
        return {
            "status": "no_data",
            "service": service,
            "metric": metric,
            "reason": "metric_value_outside_semantic_domain",
        }

    return {
        "status": "ok",
        "service": service,
        "metric": metric,
        "value": value,
        "promql": promql,
    }
