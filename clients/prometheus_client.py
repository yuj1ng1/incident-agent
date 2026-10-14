from datetime import datetime, timedelta, timezone
import json
import math
import re
import time

import requests


MAX_EVALUATION_AGE_SECONDS = 15
ALLOWED_CLOCK_SKEW_SECONDS = 5
# Exclusive UTC limit: 10000-01-01, outside Python datetime's supported range.
MAX_UNIX_TIMESTAMP = 253402300800
MAX_RANGE_SECONDS = 6 * 60 * 60
MIN_RANGE_STEP_SECONDS = 15
MAX_RANGE_STEP_SECONDS = 300
MAX_RANGE_POINTS = 120
MAX_RANGE_RESPONSE_BYTES = 65536
RANGE_TIMEOUT_SECONDS = 5
MAX_ALIGNED_METRICS = 4
MAX_ALIGNED_LOOKBACK_DAYS = 15
MAX_ALIGNED_RESPONSE_BYTES = 16384
ALIGNED_TIMEOUT_SECONDS = 5
ALIGNED_TOTAL_TIMEOUT_SECONDS = 10
_RANGE_TIME = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.([0-9]{1,9}))?(Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _parse_metric_time_nanoseconds(value, reason):
    match = _RANGE_TIME.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(reason)
    try:
        stamp = datetime.fromisoformat(match[1] + match[3].replace("Z", "+00:00"))
        # RFC3339 -00:00 denotes an unknown offset, not an explicit timezone.
        if value.endswith("-00:00"):
            raise ValueError
        stamp = stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError(reason) from None
    delta = stamp - _EPOCH
    return (
        (delta.days * 86400 + delta.seconds) * 1_000_000_000
        + int((match[2] or "0").ljust(9, "0"))
    )


def parse_metric_evaluation_time(at, now):
    """Validate the original instant before explicitly flooring to milliseconds."""
    nanoseconds = _parse_metric_time_nanoseconds(at, "invalid_metric_evaluation_time")
    if nanoseconds < 0:
        raise ValueError("metric_evaluation_time_out_of_bounds")
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("invalid_metric_reference_time")
    delta = now.astimezone(timezone.utc) - _EPOCH
    now_ns = (
        (delta.days * 86400 + delta.seconds) * 1_000_000_000
        + delta.microseconds * 1000
    )
    if nanoseconds > now_ns:
        raise ValueError("metric_evaluation_time_in_future")
    if now_ns - nanoseconds > MAX_ALIGNED_LOOKBACK_DAYS * 86400 * 1_000_000_000:
        raise ValueError("metric_evaluation_time_exceeds_15_days")
    return (
        _EPOCH + timedelta(milliseconds=nanoseconds // 1_000_000),
        -(nanoseconds % 1_000_000),
    )


def parse_metric_range(
    start,
    end,
    step_seconds,
    now,
):
    """Validate before I/O; use Prometheus's millisecond evaluation resolution."""
    parsed = []
    for value in (start, end):
        nanoseconds = _parse_metric_time_nanoseconds(
            value,
            "invalid_metric_range_time_format",
        )
        if nanoseconds < 0:
            raise ValueError("metric_range_time_out_of_bounds")
        parsed.append(nanoseconds)
    start_ns, end_ns = parsed
    now_delta = now - _EPOCH
    now_ns = (
        (now_delta.days * 86400 + now_delta.seconds) * 1_000_000_000
        + now_delta.microseconds * 1000
    )
    if start_ns >= end_ns:
        raise ValueError("metric_range_start_not_before_end")
    if end_ns > now_ns:
        raise ValueError("metric_range_in_future")
    if end_ns - start_ns > MAX_RANGE_SECONDS * 1_000_000_000:
        raise ValueError("metric_range_exceeds_6_hours")
    if (
        type(step_seconds) is not int
        or not MIN_RANGE_STEP_SECONDS <= step_seconds <= MAX_RANGE_STEP_SECONDS
    ):
        raise ValueError("metric_range_step_out_of_bounds")
    # Keep the caller's original interval separately in the tool result. Sending
    # millisecond boundaries inside the caller's interval avoids accepting a
    # server-rounded point before the requested start or miscounting the endpoint.
    start_time = _EPOCH + timedelta(milliseconds=(start_ns + 999999) // 1_000_000)
    end_time = _EPOCH + timedelta(milliseconds=end_ns // 1_000_000)
    if start_time >= end_time:
        raise ValueError("metric_range_below_timestamp_resolution")
    expected_points = (end_time - start_time) // timedelta(seconds=step_seconds) + 1
    if expected_points > MAX_RANGE_POINTS:
        raise ValueError("metric_range_point_limit_exceeded")
    return start_time, end_time, expected_points


def _range_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid_metric_range_json")
        result[key] = value
    return result


def _range_json_constant(value):
    raise ValueError("invalid_metric_range_json")


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

    def query_range(
        self,
        promql,
        start,
        end,
        step_seconds,
    ):
        """Internal transport for profile-built PromQL; never a model tool.

        Reject oversized/partial transport responses instead of truncating JSON.
        Local request times cannot be replaced by fields from the server.
        """
        started = self.clock().astimezone(timezone.utc)
        start_time, end_time, _ = parse_metric_range(
            start,
            end,
            step_seconds,
            started,
        )
        return self._bounded_query(
            "query_range",
            {
                "query": promql,
                "start": start_time.isoformat(),
                "end": end_time.isoformat(),
                "step": step_seconds,
            },
            started,
            timeout_seconds=RANGE_TIMEOUT_SECONDS,
            response_limit=MAX_RANGE_RESPONSE_BYTES,
            category="metric_range",
        )

    def query_at(
        self,
        promql,
        at,
        *,
        deadline=None,
    ):
        """Internal historical instant query; PromQL must come from a profile.

        The caller can share one monotonic deadline across several requests.
        Returned sample alignment is validated by the metric tool.
        """
        started = self.clock().astimezone(timezone.utc)
        evaluated_at, _ = parse_metric_evaluation_time(at, started)
        return self._bounded_query(
            "query",
            {
                "query": promql,
                "time": evaluated_at.isoformat(),
                "timeout": f"{ALIGNED_TIMEOUT_SECONDS}s",
            },
            started,
            timeout_seconds=ALIGNED_TIMEOUT_SECONDS,
            response_limit=MAX_ALIGNED_RESPONSE_BYTES,
            category="metric_aligned",
            deadline=deadline,
        ) | {"evaluation_time": evaluated_at.isoformat()}

    def _bounded_query(
        self,
        endpoint,
        params,
        started,
        *,
        timeout_seconds,
        response_limit,
        category,
        deadline=None,
    ):
        """Shared bounded transport; preserve the range query's wire contract."""
        finished = None
        began = time.monotonic()
        request_deadline = began + timeout_seconds
        if deadline is not None:
            request_deadline = min(request_deadline, deadline)
        try:
            remaining = request_deadline - began
            if remaining <= 0:
                raise requests.Timeout(f"{category}_request_timeout")
            with requests.get(
                f"{self.base_url}/api/v1/{endpoint}",
                params=params,
                timeout=remaining,
                stream=True,
                allow_redirects=False,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            ) as response:
                response.raise_for_status()
                if 300 <= response.status_code < 400:
                    raise ValueError(f"{category}_redirect_refused")
                # Compression can expand inside the HTTP library before our size
                # check; request identity encoding and reject compressed bodies.
                encoding = response.headers.get("Content-Encoding", "identity")
                if encoding.lower() != "identity":
                    raise ValueError(f"{category}_content_encoding_refused")
                length = response.headers.get("Content-Length")
                if length is not None:
                    if not isinstance(length, str) or not length.isdecimal():
                        raise ValueError(f"invalid_{category}_content_length")
                    if int(length) > response_limit:
                        raise ValueError(f"{category}_response_size_limit")
                body = bytearray()
                # Single-byte reads let a monotonic deadline bound trickling
                # responses, instead of waiting for a large chunk to fill up.
                for chunk in response.iter_content(chunk_size=1):
                    if time.monotonic() > request_deadline:
                        raise requests.Timeout(f"{category}_request_timeout")
                    if len(body) + len(chunk) > response_limit:
                        raise ValueError(f"{category}_response_size_limit")
                    body.extend(chunk)
                finished = self.clock().astimezone(timezone.utc)
                if time.monotonic() > request_deadline:
                    raise requests.Timeout(f"{category}_request_timeout")
            result = json.loads(
                body,
                object_pairs_hook=_range_json_object,
                parse_constant=_range_json_constant,
            )
            if not isinstance(result, dict):
                raise ValueError(f"invalid_{category}_json")
        except (requests.RequestException, ValueError, RecursionError) as exc:
            if category != "metric_range" and str(exc) == "invalid_metric_range_json":
                exc = ValueError(f"invalid_{category}_json")
            exc.query_started_at = started.isoformat()
            exc.query_finished_at = (
                finished or self.clock().astimezone(timezone.utc)
            ).isoformat()
            raise exc
        return result | {
            "query_started_at": started.isoformat(),
            "query_finished_at": finished.isoformat(),
        }

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
