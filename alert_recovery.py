"""Clearing a signal requires valid present data; never equate absence with zero."""
from datetime import datetime, timezone
import math

from services.metric_profiles import build_promql, selector
from services.registry import registry


def rules_are_current(client):
    try:
        response = client.get_rules()
        if response.get("status") != "success" or response.get("warnings"):
            return {}
        rules = {}
        now = datetime.now(timezone.utc)
        for group in response["data"]["groups"]:
            for rule in group["rules"]:
                if rule.get("type") != "alerting" or rule.get("health") != "ok":
                    continue
                stamp = datetime.fromisoformat(
                    rule["lastEvaluation"].replace("Z", "+00:00")
                )
                if stamp.tzinfo is None or not 0 <= (now - stamp).total_seconds() <= 30:
                    continue
                rules[rule["name"]] = rule
        return rules
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        return {}


def present_value(client, expression, max_age=30):
    try:
        response = client.query(expression)
        if response.get("status") != "success" or response.get("warnings"):
            return None
        samples = response["data"]["result"]
        if not isinstance(samples, list) or len(samples) != 1:
            return None
        stamp, raw_value = samples[0]["value"]
        if isinstance(stamp, bool) or isinstance(raw_value, bool):
            return None
        age = datetime.now(timezone.utc).timestamp() - float(stamp)
        value = float(raw_value)
        if not math.isfinite(value) or not 0 <= age <= max_age:
            return None
        return value
    except (
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
        OSError,
        OverflowError,
    ):
        return None


def can_clear(alert, client, current_rules):
    if alert.source != "prometheus":
        return False
    rule = current_rules.get(alert.alertname)
    if rule is None:
        return False
    annotations = rule.get("annotations", {})
    try:
        if (
            float(annotations.get("threshold")) != alert.threshold
            or annotations.get("unit") != alert.unit
            or annotations.get("direction") != alert.direction
        ):
            return False
    except (TypeError, ValueError):
        return False
    profile = registry.get(alert.service)
    if profile is None or profile.metrics is None:
        return False
    settings = profile.metrics
    target = (
        {"job": settings.job}
        if settings.profile == "postgresql"
        else (settings.labels or {"service": alert.service})
    )
    target = {
        **target,
        **{key: alert.labels[key] for key in alert.identity_labels},
    }
    # For aggregated HTTP alerts, every selected target must be present and healthy.
    up = present_value(client, f"min(up{{{selector(target)}}})")
    if up != 1:
        return False
    scrape_age = present_value(
        client, f"max(time() - timestamp(up{{{selector(target)}}}))"
    )
    if scrape_age is None or scrape_age > 30:
        return False
    expression = build_promql(alert.service, settings, alert.metric)
    if alert.signal_type == "state":
        metric_name = "pg_up" if alert.metric == "postgres_up" else "up"
        expression = f"{metric_name}{{{selector(target)}}}"
    value = present_value(client, expression)
    if value is None:
        return False
    if alert.direction == "higher_is_worse":
        return value <= alert.threshold
    return value >= alert.threshold
