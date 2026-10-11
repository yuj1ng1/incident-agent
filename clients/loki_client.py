from datetime import datetime, timezone
import re

import requests


MAX_LOG_QUERY_SPAN_NS = 24 * 60 * 60 * 1_000_000_000
_UTC_TIME = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.([0-9]{1,9}))?(?:Z|\+00:00)"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def utc_to_nanoseconds(value: str) -> int:
    """Parse a UTC ISO 8601 timestamp without rounding its fractional seconds."""
    match = _UTC_TIME.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("invalid_log_time_format")
    try:
        parsed = datetime.strptime(match[1], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError("invalid_log_time_format") from None
    delta = parsed - _EPOCH
    nanoseconds = (
        (delta.days * 86400 + delta.seconds) * 1_000_000_000
        + int((match[2] or "0").ljust(9, "0"))
    )
    # Loki/Go represents Unix nanoseconds with a signed 64-bit integer.
    if not -(2**63) <= nanoseconds <= 2**63 - 1:
        raise ValueError("log_time_out_of_range")
    return nanoseconds


def parse_time_range(start: str | None, end: str | None) -> tuple[int | None, int | None]:
    if start is None and end is None:
        return None, None
    if start is None or end is None:
        raise ValueError("log_time_range_requires_start_and_end")
    start_ns = utc_to_nanoseconds(start)
    end_ns = utc_to_nanoseconds(end)
    if start_ns >= end_ns:
        raise ValueError("log_time_range_start_not_before_end")
    if end_ns - start_ns > MAX_LOG_QUERY_SPAN_NS:
        raise ValueError("log_time_range_exceeds_24_hours")
    return start_ns, end_ns


class LokiClient:
    def __init__(self,base_url):
        self.base_url=base_url

    def query_range(self,logql,limit=50,start: str | None=None,end: str | None=None):
        start_ns, end_ns = parse_time_range(start, end)
        params = {
            "query": logql,
            "limit": limit,
            "direction": "backward",
        }
        if start_ns is not None:
            params.update(start=str(start_ns), end=str(end_ns))
        response=requests.get(
            f"{self.base_url}/loki/api/v1/query_range",
            params=params,
            timeout=5
        )
        response.raise_for_status()
        return response.json()
