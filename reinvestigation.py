import math
from datetime import datetime, timedelta, timezone
from alert_policy import ReinvestigationPolicy, policy_for

from config import (
    REINVESTIGATE_COOLDOWN_MINUTES,
    HTTP_5XX_RATE_INCREASE_THRESHOLD
)

RETRYABLE_FAILURES = {"model_empty_answer", "model_invalid_call_ids"}


def has_alert_worsened(incident,alert):
    policy = policy_for(alert)
    if policy is not None:
        if alert.signal_type != "numeric":
            return (
                incident.last_investigated_state is not None
                and alert.state != incident.last_investigated_state
            )
        if incident.last_investigated_value is None or alert.value is None:
            return incident.last_investigated_value is None
        previous = incident.last_investigated_value
        change = alert.value - previous
        if policy.direction == "lower_is_worse":
            change = -change
        absolute = policy.reinvestigation.absolute_change
        relative = policy.reinvestigation.relative_change
        return (
            absolute is not None
            and (change >= absolute or math.isclose(change, absolute, abs_tol=1e-12))
        ) or (
            relative is not None
            and previous != 0
            and change / abs(previous) >= relative
        )
    if incident.last_investigated_value is None:
        return True

    if alert.metric=="http_5xx_rate":
        increase=(
            alert.value
            - incident.last_investigated_value
        )

        return (
            increase
            >= HTTP_5XX_RATE_INCREASE_THRESHOLD
            or math.isclose(
                increase,
                HTTP_5XX_RATE_INCREASE_THRESHOLD,
                rel_tol=0.0,
                abs_tol=1e-12
            )
        )

    return False


def utc_timestamp(value):
    # Old IncidentStore timestamps were local Beijing time without a zone.
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone(timedelta(hours=8)))
    return value.astimezone(timezone.utc)


def reinvestigation_reason(incident, alert, now=None, session=None):
    """None allows investigation; every refusal has a stable queryable reason."""
    now = utc_timestamp(now or datetime.now(timezone.utc))
    if incident.status == "resolved" or incident.alert_state != "firing" or alert.state != "firing":
        return "incident_not_active"
    policy = policy_for(alert)
    strategy = policy.reinvestigation if policy else ReinvestigationPolicy(
        cooldown_seconds=REINVESTIGATE_COOLDOWN_MINUTES * 60,
    )
    if session is not None:
        from investigation.models import SessionStatus as S
        if session.status == S.CANCELLED:
            return "cancelled_requires_human_reopen"
        if session.status == S.EXHAUSTED:
            return "exhausted_requires_human_reauthorization"
        if session.status not in {S.COMPLETED, S.FAILED}:
            return "existing_session_" + session.status.value.lower()
        if session.status == S.FAILED and session.last_error_category not in RETRYABLE_FAILURES:
            return "non_retryable_failure_requires_human_reopen"
    if incident.investigation_suspended_reason:
        return "investigation_suspended:" + incident.investigation_suspended_reason
    if incident.consecutive_investigation_failures >= strategy.maximum_failed_attempts:
        return "policy_failure_limit"
    if session is not None and session.status.value == "FAILED":
        delay = strategy.retry_seconds * 2 ** max(0, incident.consecutive_investigation_failures - 1)
        attempted_at = session.completed_at or session.updated_at
        if (now - utc_timestamp(attempted_at)).total_seconds() < delay:
            return "failure_backoff"
        return None
    if incident.last_attempted_at is not None:
        last_attempt = utc_timestamp(incident.last_attempted_at)
        last_success = utc_timestamp(incident.last_investigated_at) if incident.last_investigated_at else None
        wait = strategy.retry_seconds if last_success is None or last_attempt > last_success else strategy.minimum_interval_seconds
        if (now - last_attempt).total_seconds() < wait:
            return "policy_minimum_interval"
        if last_success and (now - last_success).total_seconds() < strategy.minimum_interval_seconds:
            return "policy_minimum_interval"
    if incident.last_investigated_at is None:
        return None
    if has_alert_worsened(incident, alert):
        return None
    if (now - utc_timestamp(incident.last_investigated_at)).total_seconds() >= strategy.cooldown_seconds:
        return None
    return "policy_cooldown"


def should_reinvestigate(incident, alert, now=None):
    return reinvestigation_reason(incident, alert, now) is None
