"""Prometheus adapter: isolate bad entries and preserve snapshot trust metadata."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from typing import Literal

from alert_policy import ALERT_POLICIES
from models.incident import AlertEvent
from services.registry import registry
from services.metric_profiles import METRIC_PROFILES

OriginStatus = Literal["demo_replay", "synthetic", "unconfirmed"]
ORIGIN_KEYS = (
    "evidence_origin",
    "origin",
    "alert_origin",
    "alert_input_origin",
)
EXPLICIT_NOTE_KEYS = (
    "origin_note",
    "evidence_origin_note",
)
DESCRIPTIVE_ANNOTATION_KEYS = (
    "description",
    "note",
    "summary",
)
RULE_SUMMARY_TEMPLATES = (
    "HTTP 5xx ratio exceeds threshold",
    "business target scrape failed",
    "exporter cannot access PostgreSQL",
    "PostgreSQL exporter scrape failed",
    "HTTP P95 latency exceeds threshold",
)
INCIDENT_CONTEXT_PREFIX = (
    "\n    统一调查上下文（Python 校验；Policy 提示和候选关联不是根因）：\n    "
)


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


def _first_nonempty_str(mapping, keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_standard_rule_summary(text: str) -> bool:
    stripped = text.strip()
    return any(stripped.endswith(suffix) for suffix in RULE_SUMMARY_TEMPLATES)


def qualify_alert_origin(alert: AlertEvent | dict | None) -> dict[str, str | None]:
    """Qualify alert provenance conservatively without inferring live authenticity."""
    if alert is None:
        return {
            "origin_status": "unconfirmed",
            "evidence_origin": None,
            "origin_note": None,
        }
    if isinstance(alert, dict):
        source = alert.get("source")
        labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
        annotations = (
            alert.get("annotations")
            if isinstance(alert.get("annotations"), dict)
            else {}
        )
    else:
        source = getattr(alert, "source", None)
        labels = getattr(alert, "labels", None) or {}
        annotations = getattr(alert, "annotations", None) or {}

    raw_origin = _first_nonempty_str(labels, ORIGIN_KEYS) or _first_nonempty_str(
        annotations,
        ORIGIN_KEYS,
    )
    if raw_origin is None and isinstance(source, str):
        lower_source = source.strip().lower()
        if lower_source.startswith("synthetic") or lower_source == "demo_replay":
            raw_origin = source.strip()

    explicit_note = _first_nonempty_str(
        annotations,
        EXPLICIT_NOTE_KEYS,
    ) or _first_nonempty_str(
        labels,
        EXPLICIT_NOTE_KEYS,
    )
    descriptive_note = None
    for key in DESCRIPTIVE_ANNOTATION_KEYS:
        value = annotations.get(key)
        if (
            isinstance(value, str)
            and value.strip()
            and not _is_standard_rule_summary(value)
        ):
            descriptive_note = value.strip()
            break

    candidate_note = explicit_note or descriptive_note
    lower_origin = (raw_origin or "").lower()
    lower_note = (candidate_note or "").lower()

    if lower_origin.startswith("synthetic") or "synthetic" in lower_origin:
        origin_status: OriginStatus = "synthetic"
        evidence_origin = raw_origin
    elif (
        lower_origin in {"demo_replay", "replay"}
        or lower_origin.startswith("demo_")
        or "demo_replay" in lower_origin
    ):
        origin_status = "demo_replay"
        evidence_origin = raw_origin
    elif candidate_note and any(
        token in lower_note for token in ("合成", "synthetic")
    ):
        origin_status = "synthetic"
        evidence_origin = raw_origin or "synthetic_alert"
    elif candidate_note and any(
        token in lower_note for token in ("演示回放", "demo_replay")
    ):
        origin_status = "demo_replay"
        evidence_origin = raw_origin or "demo_replay"
    else:
        origin_status = "unconfirmed"
        evidence_origin = None

    if explicit_note:
        origin_note = explicit_note
    elif origin_status in {"synthetic", "demo_replay"} and descriptive_note:
        origin_note = descriptive_note
    else:
        origin_note = None

    return {
        "origin_status": origin_status,
        "evidence_origin": evidence_origin,
        "origin_note": origin_note,
    }


def extract_checkpoint_alert(checkpoint: dict | None) -> dict | None:
    """Read the alert snapshot recorded in a Runtime checkpoint without store writes."""
    if not isinstance(checkpoint, dict):
        return None
    reopen_alert = checkpoint.get("reopen_alert")
    if isinstance(reopen_alert, dict):
        return reopen_alert
    history = checkpoint.get("history")
    if not isinstance(history, list):
        return None
    for item in history:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.startswith(
            INCIDENT_CONTEXT_PREFIX
        ):
            continue
        first_line = content[len(INCIDENT_CONTEXT_PREFIX):].split("\n", 1)[0]
        try:
            metadata = json.loads(first_line)
        except (ValueError, TypeError, RecursionError):
            continue
        if isinstance(metadata, dict) and isinstance(
            metadata.get("alert_event"),
            dict,
        ):
            return metadata["alert_event"]
    return None


def qualify_investigation_origin(
    alert_origin: dict[str, str | None] | None = None,
    *,
    result: dict | None = None,
    records: list | tuple = (),
) -> dict[str, str | None]:
    """Qualify session/investigation origin from alert provenance and replay records."""
    base = alert_origin or {
        "origin_status": "unconfirmed",
        "evidence_origin": None,
        "origin_note": None,
    }
    has_demo_calls = isinstance(records, (list, tuple)) and any(
        isinstance(r, dict) and r.get("call_id") in {"demo-rate", "demo-logs"}
        for r in records
    )
    answer = result.get("answer") if isinstance(result, dict) else None
    has_demo_answer = isinstance(answer, str) and answer.startswith("演示回放。")
    if base.get("origin_status") == "demo_replay" or has_demo_calls or has_demo_answer:
        return {
            "origin_status": "demo_replay",
            "evidence_origin": base.get("evidence_origin") or "demo_replay",
            "origin_note": (
                base.get("origin_note")
                or "演示回放模式（DemoReplay 离线回放模型响应，非真实模型推理）"
            ),
        }
    if base.get("origin_status") == "synthetic":
        return {
            "origin_status": "synthetic",
            "evidence_origin": base.get("evidence_origin") or "synthetic_alert",
            "origin_note": base.get("origin_note"),
        }
    if isinstance(records, (list, tuple)):
        for record in records:
            if not isinstance(record, dict):
                continue
            tool_result = record.get("result")
            reason = (
                tool_result.get("reason")
                if isinstance(tool_result, dict)
                else None
            )
            if isinstance(reason, str) and reason.startswith("offline_synthetic"):
                return {
                    "origin_status": "synthetic",
                    "evidence_origin": reason,
                    "origin_note": base.get("origin_note"),
                }
    return {
        "origin_status": "unconfirmed",
        "evidence_origin": None,
        "origin_note": None,
    }


def normalize_prometheus_alert(
    raw,
    service_registry=registry,
    *,
    default_origin: str | None = None,
    default_origin_note: str | None = None,
):
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
        if any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in annotations.items()
        ):
            raise ValueError("invalid_annotations")
        if policy.unit == "ratio" and not (0 <= value <= 1 and 0 <= threshold <= 1):
            raise ValueError("invalid_ratio")
        if policy.unit == "seconds" and (value < 0 or threshold < 0):
            raise ValueError("invalid_duration")
        if policy.signal_type == "state" and threshold != 1:
            raise ValueError("invalid_state_threshold")
        normalized_labels = dict(labels)
        normalized_annotations = dict(annotations)
        if (
            isinstance(default_origin, str)
            and default_origin.strip()
            and not _first_nonempty_str(normalized_labels, ORIGIN_KEYS)
            and not _first_nonempty_str(normalized_annotations, ORIGIN_KEYS)
        ):
            normalized_labels["evidence_origin"] = default_origin.strip()
        if (
            isinstance(default_origin_note, str)
            and default_origin_note.strip()
            and not _first_nonempty_str(normalized_annotations, EXPLICIT_NOTE_KEYS)
            and not _first_nonempty_str(normalized_labels, EXPLICIT_NOTE_KEYS)
        ):
            normalized_annotations["origin_note"] = default_origin_note.strip()
        event = AlertEvent(
            alertname=policy.alertname,
            service=profile.name,
            severity=normalized_labels["severity"],
            signal_type=policy.signal_type,
            metric=policy.metric,
            value=value,
            threshold=threshold,
            unit=policy.unit,
            direction=policy.direction,
            source="prometheus",
            state=state,
            labels=normalized_labels,
            annotations=normalized_annotations,
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
            "invalid_annotations",
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


def normalize_snapshot(
    response,
    service_registry=registry,
    *,
    default_origin: str | None = None,
    default_origin_note: str | None = None,
):
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
        result = normalize_prometheus_alert(
            raw,
            service_registry,
            default_origin=default_origin,
            default_origin_note=default_origin_note,
        )
        if result.outcome == "valid":
            snapshot.alerts.append(result.alert)
        elif result.outcome == "ignored":
            snapshot.ignored.append(result.alert)
        else:
            snapshot.invalid.append(result.reason)
            snapshot.complete = False
    return snapshot
