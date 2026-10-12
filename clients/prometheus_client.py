from datetime import datetime, timezone
import math

import requests


MAX_EVALUATION_AGE_SECONDS = 15
ALLOWED_CLOCK_SKEW_SECONDS = 5
# Exclusive UTC limit: 10000-01-01, outside Python datetime's supported range.
MAX_UNIX_TIMESTAMP = 253402300800


def parse_evaluated_at(timestamp) -> datetime:
    """Parse a Prometheus JSON sample timestamp, not a raw scrape timestamp."""
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise ValueError("invalid_metrics_timestamp")
    try:
        seconds = float(timestamp)
        if not math.isfinite(seconds) or not 0 <= seconds < MAX_UNIX_TIMESTAMP:
            raise ValueError("invalid_metrics_timestamp")
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise ValueError("invalid_metrics_timestamp") from None


def evaluation_is_fresh(evaluated_at: datetime, now: datetime) -> bool:
    """Bound evaluation age and tolerate a small server/client clock offset."""
    if evaluated_at.utcoffset() is None or now.utcoffset() is None:
        return False
    age = (now - evaluated_at).total_seconds()
    return -ALLOWED_CLOCK_SKEW_SECONDS <= age <= MAX_EVALUATION_AGE_SECONDS


class PrometheusClient:
    def __init__(
        self,
        base_url,
        *,
        clock=None,
    ):
        self.base_url = base_url
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def query(self, promql):
        """Keep the API envelope and exceptions, adding per-request UTC timing."""
        started = self.clock().astimezone(timezone.utc)
        finished = None
        try:
            response = requests.get(
                f"{self.base_url}/api/v1/query",
                params={"query": promql},
                timeout=5,
            )
            finished = self.clock().astimezone(timezone.utc)
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            # A transport failure ends the attempt without receiving a response.
            # Local attributes preserve timing without changing the raised type.
            exc.query_started_at = started.isoformat()
            exc.query_finished_at = (
                finished or self.clock().astimezone(timezone.utc)
            ).isoformat()
            raise

        if isinstance(result, dict):
            return result | {
                "query_started_at": started.isoformat(),
                "query_finished_at": finished.isoformat(),
            }
        return result

    def get_alerts(self):#查询当前告警
        response=requests.get(
            f"{self.base_url}/api/v1/alerts",
            timeout=5
        )
        response.raise_for_status()
        return response.json()

    def get_rules(self):
        response = requests.get(
            f"{self.base_url}/api/v1/rules",
            params={"type": "alert"},
            timeout=5,
        )
        response.raise_for_status()
        return response.json()

    def get_targets(self):
        response = requests.get(
            f"{self.base_url}/api/v1/targets",
            params={"state": "active"},
            timeout=5,
        )
        response.raise_for_status()
        return response.json()
