"""Candidate relations only: keep incident identity and lifecycle independent."""
from fingerprint import generate_alert_fingerprint
from services.registry import registry


def correlate_alerts(alerts, service_registry=registry, window_seconds=300):
    related = {generate_alert_fingerprint(alert): [] for alert in alerts}
    for alert in alerts:
        for other in alerts:
            if generate_alert_fingerprint(alert) == generate_alert_fingerprint(other):
                continue
            if alert.starts_at is None or other.starts_at is None:
                continue
            if (
                abs((alert.starts_at - other.starts_at).total_seconds())
                > window_seconds
            ):
                continue
            profile = service_registry.get(alert.service)
            other_profile = service_registry.get(other.service)
            if profile is None or other_profile is None:
                continue
            if alert.service == other.service:
                basis = "same_registered_service_and_time_window"
            elif (
                other.service in profile.dependencies
                or alert.service in other_profile.dependencies
            ):
                basis = "explicit_registered_dependency_and_time_window"
            else:
                continue
            related[generate_alert_fingerprint(alert)].append(
                {
                    "fingerprint": generate_alert_fingerprint(other),
                    "alertname": other.alertname,
                    "service": other.service,
                    "state": other.state,
                    "value": other.value,
                    "starts_at": other.starts_at.isoformat(),
                    "relationship": "candidate",
                    "basis": basis,
                    "shared_root_cause_confirmed": False,
                }
            )
    return related
