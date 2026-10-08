"""Prometheus adapter: isolate bad entries and preserve snapshot trust metadata."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math

from alert_policy import ALERT_POLICIES
from models.incident import AlertEvent
from services.registry import registry
from services.metric_profiles import METRIC_PROFILES


@dataclass
class ParseResult:
    outcome: str
    alert: AlertEvent | None = None
    reason: str | None = None


@dataclass
class AlertSnapshot:
    alerts: list[AlertEvent] = field(default_factory=list)
    ignored: list[AlertEvent] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    complete: bool = False


def finite(value):
    if isinstance(value, bool):
        raise ValueError("invalid_number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite_number")
    return number


def normalize_prometheus_alert(raw, service_registry=registry):
    try:
        if not isinstance(raw, dict):
            raise ValueError("invalid_entry")
        state = raw.get("state")
        if state not in {"firing", "pending", "inactive"}:
            raise ValueError("invalid_state")
        labels = raw.get("labels")
        annotations = raw.get("annotations")
        if not isinstance(labels, dict) or not isinstance(annotations, dict):
            raise ValueError("missing_labels_or_annotations")
        if any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()
        ):
            raise ValueError("invalid_labels")
        policy = ALERT_POLICIES.get(labels.get("alertname"))
        if policy is None:
            raise ValueError("unregistered_alertname")
        profile = service_registry.get(labels.get("service"))
        if profile is None:
            raise ValueError("unknown_service")
        if profile.metrics and profile.metrics.profile == "postgresql":
            if labels.get("job") != profile.metrics.job:
                raise ValueError("monitoring_target_mismatch")
        if (
            profile.metrics is None
            or profile.metrics.profile != policy.metric_profile
            or policy.metric not in METRIC_PROFILES[profile.metrics.profile]
            or labels.get("metric") != policy.metric
            or labels.get("signal_type") != policy.signal_type
            or labels.get("severity") not in {"critical", "warning", "info"}
            or annotations.get("unit") != policy.unit
            or annotations.get("direction") != policy.direction
            or annotations.get("window") != policy.window
        ):
            raise ValueError("policy_mismatch")
        minimum = annotations.get("minimum_requests")
        if policy.minimum_requests is not None and minimum != str(
            policy.minimum_requests
        ):
            raise ValueError("sample_guard_mismatch")
        value = finite(raw["value"])
        if raw.get("activeAt") is not None and not isinstance(raw["activeAt"], str):
            raise ValueError("invalid_timestamp")
        threshold = finite(annotations["threshold"])
        if policy.unit == "ratio" and not (0 <= value <= 1 and 0 <= threshold <= 1):
            raise ValueError("invalid_ratio")
        if policy.unit == "seconds" and (value < 0 or threshold < 0):
            raise ValueError("invalid_duration")
        if policy.signal_type == "state" and threshold != 1:
            raise ValueError("invalid_state_threshold")
        event = AlertEvent(
            alertname=policy.alertname,
            service=profile.name,
            severity=labels["severity"],
            signal_type=policy.signal_type,
            metric=policy.metric,
            value=value,
            threshold=threshold,
            unit=policy.unit,
            direction=policy.direction,
            source="prometheus",
            state=state,
            labels=labels,
            identity_labels=policy.identity_labels,
            starts_at=raw.get("activeAt"),
            observed_at=datetime.now(timezone.utc),
            window=policy.window,
            minimum_requests=policy.minimum_requests,
        )
        if state in {"firing", "pending"}:
            breached = (
                value > threshold
                if policy.direction == "higher_is_worse"
                else value < threshold
            )
            if not breached:
                raise ValueError("value_does_not_match_active_signal")
        return ParseResult("valid" if state == "firing" else "ignored", event)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        safe_reasons = {
            "invalid_entry",
            "invalid_state",
            "missing_labels_or_annotations",
            "invalid_labels",
            "unregistered_alertname",
            "unknown_service",
            "monitoring_target_mismatch",
            "policy_mismatch",
            "sample_guard_mismatch",
            "invalid_timestamp",
            "invalid_ratio",
            "invalid_duration",
            "invalid_state_threshold",
            "value_does_not_match_active_signal",
            "invalid_number",
            "nonfinite_number",
        }
        reason = (
            str(exc)
            if type(exc) is ValueError and str(exc) in safe_reasons
            else "invalid_fields"
        )
        return ParseResult("invalid", reason=reason)


def parse_prometheus_alert(raw_alert):
    result = normalize_prometheus_alert(raw_alert)
    return result.alert if result.outcome == "valid" else None


def normalize_snapshot(response, service_registry=registry):
    snapshot = AlertSnapshot()
    if (
        not isinstance(response, dict)
        or response.get("status") != "success"
        or not isinstance(response.get("data"), dict)
        or not isinstance(response["data"].get("alerts"), list)
    ):
        snapshot.invalid.append("invalid_snapshot_envelope")
        return snapshot
    snapshot.complete = not bool(response.get("warnings"))
    for raw in response["data"]["alerts"]:
        result = normalize_prometheus_alert(raw, service_registry)
        if result.outcome == "valid":
            snapshot.alerts.append(result.alert)
        elif result.outcome == "ignored":
            snapshot.ignored.append(result.alert)
        else:
            snapshot.invalid.append(result.reason)
            snapshot.complete = False
    return snapshot
