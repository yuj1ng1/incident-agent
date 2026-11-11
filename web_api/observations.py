"""Bounded, read-only signal queries around a stored alert observation."""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import get_args

from starlette.concurrency import run_in_threadpool

from models.incident import Incident
from reinvestigation import utc_timestamp
from web_api.queries import public_text
from web_api.schemas import IncidentSignals, LogSignal, MetricSignal, SignalMetric, SignalWindow


METRICS = get_args(SignalMetric)
NO_DATA = {
    "service_not_registered",
    "metrics_capability_not_configured",
    "logs_capability_not_configured",
    "metric_not_supported_by_profile",
    "no_samples_in_metric_range",
    "no_logs_in_query_range",
}
LINE_LIMIT = 1200


def alert_window(incident: Incident, now: datetime) -> SignalWindow | None:
    alert_time = next((
        alert.observed_at or alert.starts_at
        for alert in reversed(incident.alerts)
        if alert.observed_at or alert.starts_at
    ), None)
    anchor = incident.cleared_at if incident.alert_state == "cleared" else None
    anchor = anchor or alert_time
    if anchor is None:
        return None
    anchor = min(utc_timestamp(anchor), now).replace(microsecond=0)
    return SignalWindow(
        start=anchor - timedelta(minutes=8),
        end=min(now.replace(microsecond=0), anchor + timedelta(minutes=2)),
        step_seconds=15,
    )


def signal_status(result):
    if result["status"] == "ok":
        return "ok", None
    reason = result.get("reason", "query_failed")
    return ("no_data" if reason in NO_DATA else "error"), reason


async def incident_signals(incident: Incident) -> IncidentSignals:
    from tools.logs import query_logs
    from tools.metrics import query_metric_range

    window = alert_window(incident, datetime.now(timezone.utc))
    if window is None:
        return IncidentSignals(
            window=None,
            metrics=[],
            logs=LogSignal(
                status="no_data", reason="alert_time_not_recorded",
                entries=[], truncated=False,
            ),
        )
    start, end = window.start.isoformat(), window.end.isoformat()
    log_result, *metric_results = await asyncio.gather(
        run_in_threadpool(query_logs, incident.service, start, end),
        *(run_in_threadpool(
            query_metric_range, incident.service, metric, start, end, window.step_seconds,
        ) for metric in METRICS),
    )
    metrics = []
    for metric, result in zip(METRICS, metric_results, strict=True):
        status, reason = signal_status(result)
        metrics.append(MetricSignal(
            metric=metric, status=status, reason=reason, samples=result["samples"],
        ))
    status, reason = signal_status(log_result)
    logs = log_result.get("logs", [])
    return IncidentSignals(
        window=window,
        metrics=metrics,
        logs=LogSignal(
            status=status,
            reason=reason,
            entries=[{
                "timestamp": log["timestamp_utc"],
                "line": public_text(log["line"], LINE_LIMIT),
            } for log in logs],
            truncated=log_result.get("truncated", False)
            or any(len(log["line"]) > LINE_LIMIT for log in logs),
        ),
    )
