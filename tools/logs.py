from tools.tool_registry import tool
from clients.loki_client import LokiClient, parse_time_range
from config import LOKI_URL
from datetime import datetime,timezone
from services.registry import registry, CapabilityUnavailable
from services.metric_profiles import selector
import requests
import time


LOG_LIMIT = 50
RECENT_WINDOW_NS = 60 * 60 * 1_000_000_000


def timestamp_to_utc(timestamp):
    # 用整数拆分，避免 Unix 纳秒经 float 转换产生精度损失。
    seconds,nanoseconds=divmod(int(timestamp),1_000_000_000)
    prefix=datetime.fromtimestamp(seconds,timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{prefix}.{nanoseconds:09d}Z" if nanoseconds else f"{prefix}Z"

loki_client=LokiClient(
    base_url=LOKI_URL
)

@tool
def query_logs(service: str, start: str | None = None, end: str | None = None) -> dict:
    """
    查询指定服务的日志；可指定 UTC 告警窗口，未指定时查询最近一小时，无日志不表示健康。

    Args:
        service: 需要查询日志的服务名称
        start: 可选，与 end 成对提供，UTC ISO 8601，例如 2026-10-11T08:00:00.123456789Z 或 +00:00，包含此边界
        end: 可选，UTC ISO 8601，不包含此边界，必须晚于 start，跨度最多 24 小时；窗口日志只证明该时段的观测
    """

    try:
        start_ns, end_ns = parse_time_range(start, end)
    except ValueError as exc:
        return {
            "status": "invalid_arguments",
            "service": service,
            "reason": str(exc),
            "message": "start/end 必须成对提供 UTC ISO 8601 时间（Z 或 +00:00，最多 9 位小数），start < end，跨度最多 24 小时。",
            "logs": [],
            "log_count": 0,
        }

    try:
        settings = registry.require_capability(service, "logs")
    except CapabilityUnavailable as exc:
        return {
            "status": "no_data",
            "service": service,
            "reason": exc.reason,
            "logs": [],
            "log_count": 0,
        }
    logql = "{" + selector(settings.labels) + "}"

    explicit_window = start_ns is not None
    if not explicit_window:
        end_ns = time.time_ns()
        start_ns = end_ns - RECENT_WINDOW_NS
    query_range = {
        "start": timestamp_to_utc(start_ns),
        "end": timestamp_to_utc(end_ns),
        "start_unix_ns": str(start_ns),
        "end_unix_ns": str(end_ns),
        "bounds": "[start, end)",
        "mode": "explicit" if explicit_window else "recent",
    }
    metadata = {
        "query_range": query_range,
        "evidence_scope": "time_window" if explicit_window else "recent",
        "log_count": 0,
        "limit": LOG_LIMIT,
        "truncated": False,
        "out_of_range_count": 0,
    }

    try:
        response = loki_client.query_range(
            logql=logql,
            # One extra entry makes truncation detectable, including exactly 50 results.
            limit=LOG_LIMIT + 1,
            start=query_range["start"],
            end=query_range["end"],
        )
    except (requests.RequestException, ValueError):
        return {
            "status": "no_data",
            "service": service,
            "logs": [],
            "reason": "logs_unreachable_or_invalid",
            **metadata,
        }
    if (
        not isinstance(response, dict)
        or not isinstance(response.get("data"), dict)
        or response.get("status", "success") != "success"
        or response["data"].get("resultType", "streams") != "streams"
    ):
        return {
            "status": "no_data",
            "service": service,
            "logs": [],
            "reason": "invalid_logs_response",
            **metadata,
        }

    streams = response.get(
        "data",
        {},
    ).get(
        "result",
        [],
    )

    if not isinstance(streams, list):
        return {
            "status": "no_data",
            "service": service,
            "logs": [],
            "reason": "invalid_logs_response",
            **metadata,
        }

    if not streams:
        return {
            "status": "no_data",
            "service": service,
            "logs": [],
            "reason": "no_logs_in_query_range",
            **metadata,
        }

    logs = []

    try:
        for stream in streams:
            if not isinstance(stream, dict):
                raise ValueError("invalid_stream")
            labels = stream.get("stream", {})
            values = stream.get("values", [])
            if not isinstance(labels, dict) or not isinstance(values, list):
                raise ValueError("invalid_stream")
            for entry in values:
                if not isinstance(entry, list) or len(entry) not in {2, 3}:
                    raise ValueError("invalid_log_entry")
                timestamp, line = entry[:2]
                if (
                    not isinstance(timestamp, (str, int))
                    or isinstance(timestamp, bool)
                    or not isinstance(line, str)
                ):
                    raise ValueError("invalid_log_entry")
                if explicit_window and not start_ns <= int(timestamp) < end_ns:
                    metadata["out_of_range_count"] += 1
                    continue
                logs.append({
                    "timestamp": timestamp,
                    "timestamp_utc": timestamp_to_utc(timestamp),
                    "line": line,
                    "labels": labels,
                })
    except (ValueError, TypeError, OverflowError, OSError):
        return {
            "status": "no_data",
            "service": service,
            "reason": "invalid_logs_response",
            "logs": [],
            **metadata,
        }

    logs.sort(
        key=lambda log: int(log["timestamp"]),
        reverse=True,
    )

    metadata["log_count"] = min(len(logs), LOG_LIMIT)
    metadata["truncated"] = len(logs) > LOG_LIMIT
    if metadata["truncated"]:
        metadata["truncation_reason"] = "log_limit"
    return {
        "status": "ok" if logs else "no_data",
        "service": service,
        "logs": logs[:LOG_LIMIT],
        "logql": logql,
        **({"reason": "no_logs_in_query_range"} if not logs else {}),
        **metadata,
    }
