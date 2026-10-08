import math
from datetime import datetime, timedelta, timezone
from alert_policy import policy_for

from config import (
    REINVESTIGATE_COOLDOWN_MINUTES,
    HTTP_5XX_RATE_INCREASE_THRESHOLD
)


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


def should_reinvestigate(incident,alert):
    policy = policy_for(alert)
    if policy is not None:
        now = datetime.now(timezone.utc)
        if (
            incident.investigation_suspended_reason
            or (
                incident.consecutive_investigation_failures
                >= policy.reinvestigation.maximum_failed_attempts
            )
        ):
            return False
        # Legacy local timestamps had no zone: use the configured project zone.
        from zoneinfo import ZoneInfo

        def elapsed(value):
            if value.tzinfo is None:
                value = value.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            return (now - value).total_seconds()

        strategy = policy.reinvestigation
        if incident.last_attempted_at is not None:
            wait = strategy.minimum_interval_seconds
            if (
                incident.last_investigated_at is None
                or elapsed(incident.last_attempted_at) < elapsed(incident.last_investigated_at)
            ):
                wait = strategy.retry_seconds
            elif elapsed(incident.last_investigated_at) < strategy.minimum_interval_seconds:
                return False
            if elapsed(incident.last_attempted_at) < wait:
                return False
        if incident.last_investigated_at is None:
            return True
        return (
            has_alert_worsened(incident, alert)
            or elapsed(incident.last_investigated_at) >= strategy.cooldown_seconds
        )
    if incident.last_investigated_at is None:
        return True

    if has_alert_worsened(
        incident,
        alert
    ):
        return True

    cooldown=timedelta(
        minutes=REINVESTIGATE_COOLDOWN_MINUTES
    )

    elapsed=(
        datetime.now()
        - incident.last_investigated_at
    )

    return elapsed>=cooldown
