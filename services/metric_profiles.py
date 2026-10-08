"""Trusted templates describe metric families, never individual service names."""
import json


def selector(labels):
    return ",".join(
        f"{key}={json.dumps(value, ensure_ascii=False)}"
        for key, value in labels.items()
    )


HTTP_METRICS = ("http_requests_total", "http_request_rate", "http_5xx_rate")
POSTGRES_METRICS = (
    "postgres_up",
    "postgres_connections",
    "postgres_max_connections",
    "postgres_connection_usage",
    "postgres_active_sessions",
    "postgres_idle_sessions",
    "postgres_idle_in_transaction",
    "postgres_lock_waits",
)
METRIC_PROFILES = {
    "http_service": HTTP_METRICS,
    "postgresql": POSTGRES_METRICS,
}


def build_promql(service, settings, metric):
    if metric not in METRIC_PROFILES[settings.profile]:
        return None
    if settings.profile == "http_service":
        labels = settings.labels or {"service": service}
        selected = selector(labels)
        all_requests = f"demo_http_requests_total{{{selected}}}"
        error_labels = {
            **labels,
            "status": "500",
        }
        errors = f"demo_http_requests_total{{{selector(error_labels)}}}"
        return {
            "http_requests_total": f"sum({all_requests})",
            "http_request_rate": f"sum(rate({all_requests}[1m]))",
            "http_5xx_rate": f"sum(rate({errors}[1m]))/sum(rate({all_requests}[1m]))",
        }[metric]
    job_filter = selector({"job": settings.job})
    database_filter = selector(
        {
            "job": settings.job,
            "datname": settings.database,
        }
    )
    return {
        "postgres_up": f"pg_up{{{job_filter}}}",
        "postgres_connections": f"sum(pg_stat_database_numbackends{{{database_filter}}})",
        "postgres_max_connections": f"pg_settings_max_connections{{{job_filter}}}",
        "postgres_connection_usage": (
            f'sum by (job, instance) (pg_stat_database_numbackends{{{job_filter},datname!=""}})'
            f" / on (job, instance) pg_settings_max_connections{{{job_filter}}}"
        ),
        "postgres_active_sessions": f'sum(pg_stat_activity_count{{{database_filter},state="active"}})',
        "postgres_idle_sessions": f'sum(pg_stat_activity_count{{{database_filter},state="idle"}})',
        "postgres_idle_in_transaction": f'sum(pg_stat_activity_count{{{database_filter},state="idle in transaction"}})',
        "postgres_lock_waits": (
            f'(sum by (job, instance) (pg_stat_activity_count{{{database_filter},wait_event_type="Lock"}})'
            f" or on (job, instance) (0 * sum by (job, instance) (pg_stat_activity_count{{{database_filter}}})))"
            f" and on (job, instance) (pg_up{{{job_filter}}} == 1)"
        ),
    }[metric]
