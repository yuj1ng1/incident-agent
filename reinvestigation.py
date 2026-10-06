import math
from datetime import datetime,timedelta

from config import (
    REINVESTIGATE_COOLDOWN_MINUTES,
    HTTP_5XX_RATE_INCREASE_THRESHOLD
)


def has_alert_worsened(incident,alert):
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
