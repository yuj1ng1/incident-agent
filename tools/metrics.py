from datetime import datetime, timedelta, timezone
import json
import math
import re
import time
import requests
from tools.tool_registry import tool
from clients.prometheus_client import (
    PrometheusClient,
    MAX_RANGE_POINTS,
    MAX_RANGE_RESPONSE_BYTES,
    MAX_ALIGNED_METRICS,
    MAX_ALIGNED_LOOKBACK_DAYS,
    MAX_ALIGNED_RESPONSE_BYTES,
    ALIGNED_TOTAL_TIMEOUT_SECONDS,
    evaluation_is_fresh,
    parse_evaluated_at,
    parse_metric_range,
    parse_metric_evaluation_time,
)
from config import PROMETHEUS_URL
from services.registry import registry, CapabilityUnavailable
from services.metric_profiles import build_promql


ALIGNED_LIMITATIONS = [
    "相同评估时间不保证底层指标抓取时间相同；rate 等表达式仍使用各自窗口样本。",
    "多个 HTTP 查询不具有事务级原子性，迟到数据或后端变化可能影响观测。",
    "指定时刻的历史观测不能证明当前健康，数值组合提示不代表根因或因果关系。",
]
MAX_ALIGNED_PROMQL_BYTES = 4096
MAX_ALIGNED_LABEL_BYTES = 2048


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
    started = datetime.now(timezone.utc)
    try:
        response = prometheus_client.query(promql)
    except (requests.RequestException, ValueError) as exc:
        return {
            "status": "no_data",
            "service": service,
            "metric": metric,
            "reason": "metrics_unreachable_or_invalid",
            "query_started_at": getattr(
                exc, "query_started_at", started.isoformat()
            ),
            "query_finished_at": getattr(
                exc, "query_finished_at", datetime.now(timezone.utc).isoformat()
            ),
        }
    finished = datetime.now(timezone.utc)
    # Plain-dict test clients need not implement a new interface. Real clients
    # supply the narrower HTTP interval; fallback times bound the query() call.
    timing = {
        "query_started_at": started.isoformat(),
        "query_finished_at": finished.isoformat(),
    }
    if isinstance(response, dict):
        timing.update({key: response[key] for key in timing if key in response})
    try:
        request_time = datetime.fromisoformat(
            timing["query_started_at"].replace("Z", "+00:00")
        )
        reference_time = datetime.fromisoformat(
            timing["query_finished_at"].replace("Z", "+00:00")
        )
        if (
            request_time.utcoffset() is None
            or reference_time.utcoffset() is None
            or reference_time < request_time
        ):
            raise ValueError("invalid_query_interval")
    except (ValueError, TypeError, AttributeError):
        return {
            "status": "no_data",
            "service": service,
            "metric": metric,
            "reason": "invalid_query_timing",
        }
    timing = {
        "query_started_at": request_time.astimezone(timezone.utc).isoformat(),
        "query_finished_at": reference_time.astimezone(timezone.utc).isoformat(),
    }
    return _parse_metrics_response(
        response,
        service,
        metric,
        promql,
        reference_time,
    ) | timing


def _parse_metrics_response(
    response,
    service,
    metric,
    promql,
    reference_time,
):
    if (
        not isinstance(response, dict)
        or response.get("status") != "success"
        or bool(response.get("warnings"))
        or not isinstance(response.get("data"), dict)
        or response["data"].get("resultType", "vector") != "vector"
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
        sample = results[0]["value"]
        if not isinstance(sample, (list, tuple)) or len(sample) != 2:
            raise ValueError("invalid_sample_pair")
        raw_value = sample[1]
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

    try:
        evaluated_at = parse_evaluated_at(sample[0])
    except ValueError as exc:
        return {
            "status": "no_data",
            "service": service,
            "metric": metric,
            "reason": str(exc),
        }
    if not evaluation_is_fresh(evaluated_at, reference_time):
        return {
            "status": "no_data",
            "service": service,
            "metric": metric,
            "reason": "metrics_timestamp_outside_freshness_window",
        }

    return {
        "status": "ok",
        "service": service,
        "metric": metric,
        "value": value,
        "promql": promql,
        "evaluated_at": evaluated_at.isoformat(),
    }


@tool
def query_metric_range(
    service: str,
    metric: str,
    start: str,
    end: str,
    step_seconds: int,
) -> dict:
    """查询受控单指标历史序列，仅证明指定时段的观测，不能判断当前健康状态。

    Args:
        service: 已在 ServiceRegistry 登记 metrics 能力的服务或依赖
        metric: 现有 HTTP/PostgreSQL MetricProfile 支持的单个指标名，不接受 PromQL
        start: 带明确时区的 RFC3339 时间，最多 9 位小数；区间向内对齐到毫秒精度，包含实际起点
        end: 带明确时区的 RFC3339 时间，晚于 start 且不得在未来，跨度最多 6 小时，包含终点
        step_seconds: 整数 15～300 秒，包含端点的预计采样数最多 120；缺口不填零
    """
    result = {
        "status": "no_data",
        "service": service,
        "metric": metric,
        "evidence_scope": "time_window",
        "use_for_current_state": False,
        "samples": [],
        "sample_count": 0,
        "missing_point_count": None,
        "missing_evaluated_at": [],
        "truncated": None,
        "point_limit": MAX_RANGE_POINTS,
        "query_started_at": None,
        "query_finished_at": None,
    }
    started = datetime.now(timezone.utc)
    try:
        if not isinstance(service, str) or not isinstance(metric, str):
            raise ValueError("invalid_metric_range_target")
        start_time, end_time, expected_points = parse_metric_range(
            start,
            end,
            step_seconds,
            started,
        )
    except ValueError as exc:
        return result | {"status": "invalid_arguments", "reason": str(exc)}
    result.update(
        requested_range={"start": start, "end": end},
        query_range={
            "start": start_time.isoformat(),
            "end": end_time.isoformat(),
            "bounds": "[start, end]",
        },
        step_seconds=step_seconds,
        expected_point_count=expected_points,
    )
    try:
        settings = registry.require_capability(service, "metrics")
    except CapabilityUnavailable as exc:
        return result | {"reason": exc.reason}
    promql = build_promql(service, settings, metric)
    if promql is None:
        return result | {"reason": "metric_not_supported_by_profile"}

    try:
        response = prometheus_client.query_range(
            promql,
            start=start_time.isoformat(),
            end=end_time.isoformat(),
            step_seconds=step_seconds,
        )
    except (requests.RequestException, ValueError, RecursionError) as exc:
        # Publish controlled categories, never backend URLs or exception bodies.
        reasons = {
            "metric_range_response_size_limit",
            "metric_range_redirect_refused",
            "metric_range_content_encoding_refused",
            "invalid_metric_range_content_length",
            "invalid_metric_range_json",
        }
        reason = (
            str(exc) if str(exc) in reasons else "metrics_range_unreachable_or_invalid"
        )
        return result | {
            "reason": reason,
            "truncated": True if reason == "metric_range_response_size_limit" else None,
            "query_started_at": getattr(exc, "query_started_at", started.isoformat()),
            "query_finished_at": getattr(
                exc, "query_finished_at", datetime.now(timezone.utc).isoformat()
            ),
        }
    finished = datetime.now(timezone.utc)
    try:
        timing = {}
        for key, fallback in (
            ("query_started_at", started),
            ("query_finished_at", finished),
        ):
            raw = (
                response.get(key, fallback.isoformat())
                if isinstance(response, dict) else fallback.isoformat()
            )
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                raise ValueError("invalid_query_timing")
            timing[key] = parsed.astimezone(timezone.utc)
        if timing["query_finished_at"] < timing["query_started_at"]:
            raise ValueError("invalid_query_timing")
    except (ValueError, TypeError, AttributeError, OverflowError):
        return result | {"reason": "invalid_query_timing"}
    result.update({key: value.isoformat() for key, value in timing.items()})
    parsed = _parse_metric_range_response(
        response,
        metric,
        start_time,
        end_time,
        step_seconds,
        expected_points,
    )
    result.update(parsed)
    # Profile expressions are trusted but configured label lengths still need
    # to respect the existing Runtime tool-payload limit.
    if result["status"] == "ok":
        result["promql"] = promql
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_RANGE_RESPONSE_BYTES:
        return {
            key: value for key, value in result.items()
            if key not in {"samples", "series_labels", "promql", "missing_evaluated_at"}
        } | {
            "status": "no_data",
            "reason": "metric_range_result_size_limit",
            "samples": [],
            "sample_count": 0,
            "missing_point_count": None,
            "missing_evaluated_at": [],
            "truncated": True,
        }
    return result


@tool
def query_metrics_aligned(
    service: str,
    metrics: str,
    at: str,
) -> dict:
    """比较同一已授权目标在同一评估时刻的受控指标，仅作为历史关联证据。

    Args:
        service: 已登记 metrics 能力的服务或依赖，仍受 Session 目标权限限制
        metrics: 英文逗号分隔的 2～4 个不同 MetricProfile 指标名，最多 259 字符，不接受 PromQL
        at: 带明确时区的 RFC3339 时间，最多 9 位小数，不晚于当前时间且最多回溯 15 天；向下对齐毫秒并记录偏移
    """
    result = {
        "status": "no_data",
        "service": service if isinstance(service, str) and len(service) <= 120 else None,
        "evidence_scope": "time_window",
        "use_for_current_state": False,
        "requested_metrics": [],
        "comparison_complete": False,
        "results": {},
        "consistency_warnings": [],
        "limitations": list(ALIGNED_LIMITATIONS),
        "limits": {
            "max_metrics": MAX_ALIGNED_METRICS,
            "max_lookback_days": MAX_ALIGNED_LOOKBACK_DAYS,
            "response_bytes_per_metric": MAX_ALIGNED_RESPONSE_BYTES,
            "total_timeout_seconds": ALIGNED_TOTAL_TIMEOUT_SECONDS,
        },
        "query_started_at": None,
        "query_finished_at": None,
    }
    started = datetime.now(timezone.utc)
    try:
        if not isinstance(service, str) or not 1 <= len(service) <= 120:
            raise ValueError("invalid_aligned_metric_target")
        requested = _parse_aligned_metric_names(metrics)
        evaluated_at, adjustment = parse_metric_evaluation_time(at, started)
    except ValueError as exc:
        return result | {"status": "invalid_arguments", "reason": str(exc)}
    result.update(
        requested_metrics=requested,
        requested_evaluation_time=at,
        evaluation_time=evaluated_at.isoformat(),
        timestamp_resolution="1ms",
        evaluation_time_adjustment_ns=adjustment,
    )
    try:
        settings = registry.require_capability(service, "metrics")
    except CapabilityUnavailable as exc:
        return result | {"reason": exc.reason}
    expressions = {}
    for metric in requested:
        promql = build_promql(service, settings, metric)
        if promql is None:
            return result | {
                "status": "invalid_arguments",
                "reason": "metric_not_supported_by_profile",
            }
        if (
            len(json.dumps(promql, ensure_ascii=False).encode("utf-8"))
            > MAX_ALIGNED_PROMQL_BYTES
        ):
            return result | {"reason": "metric_aligned_promql_size_limit"}
        expressions[metric] = promql

    deadline = time.monotonic() + ALIGNED_TOTAL_TIMEOUT_SECONDS
    result["query_started_at"] = started.isoformat()
    for metric, promql in expressions.items():
        if time.monotonic() >= deadline:
            observation = {
                "status": "no_data",
                "reason": "metric_aligned_total_timeout",
                "metric": metric,
                "promql": promql,
                "query_started_at": None,
                "query_finished_at": None,
                "request_executed": False,
            }
        else:
            observation = _query_aligned_metric(
                metric,
                promql,
                evaluated_at,
                deadline,
            )
        result["results"][metric] = observation
    result["query_finished_at"] = datetime.now(timezone.utc).isoformat()
    successful = sum(item["status"] == "ok" for item in result["results"].values())
    result["comparison_complete"] = successful == len(requested)
    if result["comparison_complete"]:
        result["status"] = "ok"
        rate = result["results"].get("http_request_rate", {})
        errors = result["results"].get("http_5xx_rate", {})
        if rate.get("value") == 0 and errors.get("value", 0) > 0:
            result["consistency_warnings"].append({
                "code": "zero_request_rate_with_positive_5xx_ratio",
                "metrics": ["http_request_rate", "http_5xx_rate"],
                "message": "同一评估时刻出现零请求速率与正 5xx 比例，建议进一步核查样本和后端一致性；不能据此认定根因。",
            })
    else:
        result["status"] = "incomplete" if successful else "no_data"
        result["reason"] = "aligned_comparison_incomplete"
    return result


def _parse_aligned_metric_names(metrics):
    if not isinstance(metrics, str) or not 1 <= len(metrics) <= 259:
        raise ValueError("invalid_aligned_metric_names")
    names = [name.strip() for name in metrics.split(",")]
    if not 2 <= len(names) <= MAX_ALIGNED_METRICS:
        raise ValueError("aligned_metric_count_out_of_bounds")
    if any(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None for name in names):
        raise ValueError("invalid_aligned_metric_names")
    if len(set(names)) != len(names):
        raise ValueError("duplicate_aligned_metrics")
    return names


def _query_aligned_metric(
    metric,
    promql,
    evaluated_at,
    deadline,
):
    started = datetime.now(timezone.utc)
    result = {
        "status": "no_data",
        "metric": metric,
        "promql": promql,
        "query_started_at": started.isoformat(),
        "request_executed": True,
    }
    try:
        response = prometheus_client.query_at(
            promql,
            at=evaluated_at.isoformat(),
            deadline=deadline,
        )
    except (requests.RequestException, ValueError, RecursionError) as exc:
        reasons = {
            "metric_aligned_response_size_limit",
            "metric_aligned_redirect_refused",
            "metric_aligned_content_encoding_refused",
            "invalid_metric_aligned_content_length",
            "invalid_metric_aligned_json",
        }
        return result | {
            "reason": (
                str(exc) if str(exc) in reasons
                else "metrics_aligned_unreachable_or_invalid"
            ),
            "query_started_at": getattr(exc, "query_started_at", started.isoformat()),
            "query_finished_at": getattr(
                exc, "query_finished_at", datetime.now(timezone.utc).isoformat()
            ),
        }
    finished = datetime.now(timezone.utc)
    result["query_finished_at"] = finished.isoformat()
    try:
        timing = {}
        for key, fallback in (
            ("query_started_at", started),
            ("query_finished_at", finished),
        ):
            raw = (
                response.get(key, fallback.isoformat())
                if isinstance(response, dict) else fallback.isoformat()
            )
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if stamp.utcoffset() is None:
                raise ValueError
            timing[key] = stamp.astimezone(timezone.utc)
        if timing["query_finished_at"] < timing["query_started_at"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError, OverflowError):
        return result | {"reason": "invalid_query_timing"}
    result.update({key: value.isoformat() for key, value in timing.items()})
    if time.monotonic() > deadline:
        return result | {"reason": "metric_aligned_total_timeout"}
    return result | _parse_aligned_metric_response(response, metric, evaluated_at)


def _parse_aligned_metric_response(
    response,
    metric,
    evaluated_at,
):
    try:
        if (
            not isinstance(response, dict)
            or response.get("status") != "success"
            or response.get("warnings", []) != []
            or response.get("infos", []) != []
            or not isinstance(response.get("data"), dict)
            or response["data"].get("resultType") != "vector"
            or not isinstance(response["data"].get("result"), list)
        ):
            raise ValueError("invalid_aligned_metrics_response")
        series = response["data"]["result"]
        if not series:
            raise ValueError("no_samples_at_evaluation_time")
        if len(series) != 1:
            raise ValueError("multiple_metric_series")
        item = series[0]
        if (
            not isinstance(item, dict)
            or set(item) != {"metric", "value"}
            or not isinstance(item["metric"], dict)
            or not isinstance(item["value"], list)
            or len(item["value"]) != 2
        ):
            raise ValueError("invalid_aligned_metric_sample")
        labels = item["metric"]
        if len(labels) > 32 or any(
            not isinstance(key, str) or not isinstance(value, str)
            or len(key) > 128 or len(value) > 256
            for key, value in labels.items()
        ):
            raise ValueError("invalid_aligned_metric_labels")
        if (
            len(json.dumps(labels, ensure_ascii=False).encode("utf-8"))
            > MAX_ALIGNED_LABEL_BYTES
        ):
            raise ValueError("metric_aligned_label_size_limit")
        timestamp, raw_value = item["value"]
        observed_at = parse_evaluated_at(timestamp)
        if timestamp != observed_at.timestamp():
            raise ValueError("metric_aligned_timestamp_precision_loss")
        if observed_at != evaluated_at:
            return {
                "status": "no_data",
                "reason": "metric_evaluation_time_mismatch",
                "observed_evaluated_at": observed_at.isoformat(),
            }
        value = _metric_range_value(metric, raw_value)
    except ValueError as exc:
        reason = str(exc)
        if reason == "invalid_metric_range_value":
            reason = "invalid_aligned_metric_value"
        return {"status": "no_data", "reason": reason}
    return {
        "status": "ok",
        "value": value,
        "evaluated_at": observed_at.isoformat(),
        "series_labels": labels,
    }


def _metric_range_value(metric, raw):
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise ValueError("invalid_metric_range_value")
    if isinstance(raw, str) and re.fullmatch(
        r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?",
        raw,
    ) is None:
        raise ValueError("invalid_metric_range_value")
    try:
        value = float(raw)
    except (ValueError, OverflowError):
        raise ValueError("invalid_metric_range_value") from None
    if not math.isfinite(value):
        raise ValueError("invalid_metric_range_value")
    if (
        value < 0
        or (
            metric in {"service_scrape_up", "exporter_scrape_up", "postgres_up"}
            and value not in {0, 1}
        )
        or (
            metric in {"http_5xx_rate", "postgres_connection_usage"}
            and value > 1
        )
    ):
        raise ValueError("metric_value_outside_semantic_domain")
    return value


def _parse_metric_range_response(
    response,
    metric,
    start,
    end,
    step_seconds,
    expected_points,
):
    try:
        if (
            not isinstance(response, dict)
            or response.get("status") != "success"
            or response.get("warnings", []) != []
            or response.get("infos", []) != []
            or not isinstance(response.get("data"), dict)
            or response["data"].get("resultType") != "matrix"
            or not isinstance(response["data"].get("result"), list)
        ):
            raise ValueError("invalid_metrics_range_response")
        series = response["data"]["result"]
        if len(series) > 1:
            raise ValueError("multiple_metric_series")
        samples = []
        labels = {}
        if series:
            item = series[0]
            if (
                not isinstance(item, dict)
                or set(item) != {"metric", "values"}
                or not isinstance(item["metric"], dict)
                or not isinstance(item["values"], list)
            ):
                raise ValueError("invalid_metrics_range_series")
            labels = item["metric"]
            if len(labels) > 32 or any(
                not isinstance(key, str) or not isinstance(value, str)
                or len(key) > 128 or len(value) > 256
                for key, value in labels.items()
            ):
                raise ValueError("invalid_metrics_range_labels")
            values = item["values"]
            if len(values) > MAX_RANGE_POINTS or len(values) > expected_points:
                raise ValueError("metric_range_response_point_limit")
            previous = None
            for pair in values:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise ValueError("invalid_metrics_range_sample")
                stamp = parse_evaluated_at(pair[0])
                # datetime rounds to microseconds. Never let that rounding move
                # an outside/off-grid observation onto a valid query boundary.
                if pair[0] != stamp.timestamp():
                    raise ValueError("metric_range_timestamp_precision_loss")
                if not start <= stamp <= end:
                    raise ValueError("metric_range_sample_outside_window")
                if previous is not None and stamp <= previous:
                    raise ValueError("metric_range_samples_not_strictly_ordered")
                if (stamp - start) % timedelta(seconds=step_seconds) != timedelta(0):
                    raise ValueError("metric_range_sample_outside_step_grid")
                samples.append({
                    "evaluated_at": stamp.isoformat(),
                    "value": _metric_range_value(metric, pair[1]),
                })
                previous = stamp
    except ValueError as exc:
        return {
            "status": "no_data",
            "reason": str(exc),
            "truncated": (
                True if str(exc) == "metric_range_response_point_limit" else None
            ),
        }
    observed = {sample["evaluated_at"] for sample in samples}
    expected_times = [
        (start + timedelta(seconds=index * step_seconds)).isoformat()
        for index in range(expected_points)
    ]
    missing = [stamp for stamp in expected_times if stamp not in observed]
    return {
        "status": "ok" if samples else "no_data",
        **({"reason": "no_samples_in_metric_range"} if not samples else {}),
        "series_labels": labels,
        "samples": samples,
        "sample_count": len(samples),
        "missing_point_count": len(missing),
        "missing_evaluated_at": missing,
        "truncated": False,
    }
