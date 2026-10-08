"""Trusted semantic catalog. Trigger thresholds come from rule annotations."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ReinvestigationPolicy:
    cooldown_seconds: int = 300
    retry_seconds: int = 60
    absolute_change: float | None = None
    relative_change: float | None = None
    minimum_interval_seconds: int = 30
    maximum_failed_attempts: int = 3


@dataclass(frozen=True)
class AlertPolicy:
    alertname: str
    category: str
    signal_type: str
    metric: str | None
    unit: str
    direction: str
    metric_profile: str
    identity_labels: tuple[str, ...]
    investigation_hints: tuple[str, ...]
    reinvestigation: ReinvestigationPolicy
    window: str | None = None
    minimum_requests: int | None = None
    severity: str = "critical"


ALERT_POLICIES = {
    "HighHttp5xxRate": AlertPolicy(
        alertname="HighHttp5xxRate",
        category="availability",
        signal_type="numeric",
        metric="http_5xx_rate",
        unit="ratio",
        direction="higher_is_worse",
        metric_profile="http_service",
        identity_labels=(),
        investigation_hints=(
            "Check http_5xx_rate, request rate, logs and business reachability.",
            "Follow registered dependencies or code only when evidence supports it.",
        ),
        reinvestigation=ReinvestigationPolicy(
            absolute_change=0.2,
            relative_change=0.5,
        ),
        window="1m",
        minimum_requests=20,
    ),
    "ServiceDown": AlertPolicy(
        alertname="ServiceDown",
        category="availability",
        signal_type="state",
        metric="service_scrape_up",
        unit="boolean",
        direction="lower_is_worse",
        metric_profile="http_service",
        identity_labels=("job", "instance"),
        investigation_hints=(
            "Check service_scrape_up, service health and target reachability.",
            "up=0 means scrape failure; it does not prove process termination.",
            "Do not reuse earlier application errors as proof of the current scrape failure.",
        ),
        reinvestigation=ReinvestigationPolicy(),
    ),
    "DatabaseUnavailable": AlertPolicy(
        alertname="DatabaseUnavailable",
        category="dependency",
        signal_type="state",
        metric="postgres_up",
        unit="boolean",
        direction="lower_is_worse",
        metric_profile="postgresql",
        identity_labels=("job", "instance"),
        investigation_hints=(
            "Check exporter_scrape_up before interpreting postgres_up.",
            "Use database DNS, TCP, connection and SELECT 1 probe stages.",
            "Exporter database access failure alone does not identify the cause.",
        ),
        reinvestigation=ReinvestigationPolicy(),
    ),
    "PostgresExporterDown": AlertPolicy(
        alertname="PostgresExporterDown",
        severity="warning",
        category="monitoring",
        signal_type="state",
        metric="exporter_scrape_up",
        unit="boolean",
        direction="lower_is_worse",
        metric_profile="postgresql",
        identity_labels=("job", "instance"),
        investigation_hints=(
            "Check exporter_scrape_up and check_scrape_target labels, health and lastError.",
            "Missing pg_up is unavailable evidence, not a database failure.",
        ),
        reinvestigation=ReinvestigationPolicy(),
    ),
    "HighHttpLatency": AlertPolicy(
        alertname="HighHttpLatency",
        severity="warning",
        category="latency",
        signal_type="numeric",
        metric="http_latency_p95_seconds",
        unit="seconds",
        direction="higher_is_worse",
        metric_profile="http_service",
        identity_labels=(),
        investigation_hints=(
            "Check Histogram P95, request rate, logs and running version.",
            "Inspect only registered dependencies; an empty dependency list is not PostgreSQL.",
            "A slow mode flag plus a high P95 is a hypothesis until the runtime path is checked.",
        ),
        reinvestigation=ReinvestigationPolicy(
            absolute_change=0.5,
            relative_change=0.5,
        ),
        window="1m",
        minimum_requests=20,
    ),
}


def policy_for(alert):
    return ALERT_POLICIES.get(alert.alertname)
