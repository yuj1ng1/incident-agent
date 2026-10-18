"""Read-only P6.2a capture analysis; never sends requests or renews evidence."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import sys


PHASE_URLS = {
    "business": "http://localhost:8001/order",
    "identity_before": "http://localhost:8001/version",
    "identity_after": "http://localhost:8001/version",
}


def _read_object(encoded):
    try:
        value = json.loads(encoded)
    except (UnicodeDecodeError, ValueError):
        raise ValueError("p62b_invalid_json_original") from None
    if not isinstance(value, dict):
        raise ValueError("p62b_json_object_required")
    return value


def _timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("p62b_invalid_original_timestamp") from None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("p62b_original_timestamp_requires_timezone")
    return stamp.astimezone(timezone.utc)


def _seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("p62b_invalid_original_duration")
    return value


def _verified_originals(root):
    manifest = _read_object((root / "sha256.json").read_bytes())
    if not {"supervisor.json", "authorization.json"}.issubset(manifest):
        raise ValueError("p62b_supervisor_manifest_incomplete")
    originals = {}
    for name, digest in manifest.items():
        relative = PurePosixPath(name)
        if (
            not name
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != name
            or "\\" in name
            or ":" in name
            or name == "sha256.json"
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("p62b_invalid_manifest_entry")
        path = root.joinpath(*relative.parts)
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError("p62b_symlink_original_forbidden")
        try:
            encoded = path.read_bytes()
        except OSError:
            raise ValueError("p62b_manifest_original_missing") from None
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise ValueError("p62b_manifest_hash_mismatch")
        originals[name] = encoded
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("p62b_symlink_original_forbidden")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != set(manifest) | {"sha256.json"}:
        raise ValueError("p62b_unmanifested_original")
    return originals


def _http_record(name, originals):
    value = _read_object(originals[name])
    identifier = PurePosixPath(name).stem
    phase = value.get("phase")
    if (
        value.get("id") != identifier
        or phase not in PHASE_URLS
        or value.get("method") != "GET"
        or value.get("url") != PHASE_URLS[phase]
        or type(value.get("request_number")) is not int
        or value["request_number"] < 1
        or type(value.get("request_executed")) is not bool
        or type(value.get("body_complete")) is not bool
    ):
        raise ValueError("p62b_invalid_http_original_metadata")
    started = _timestamp(value.get("query_started_at"))
    finished = _timestamp(value.get("query_finished_at"))
    if finished < started:
        raise ValueError("p62b_reversed_original_timestamps")
    elapsed = _seconds(value.get("elapsed_seconds"))
    raw_name = identifier + ".body"
    body_key = "traffic/" + raw_name
    if value.get("raw_body_file") != raw_name:
        raise ValueError("p62b_invalid_body_reference")
    incomplete = []
    if body_key not in originals:
        incomplete.append("body_original_missing")
    else:
        body = originals[body_key]
        if (
            type(value.get("captured_bytes")) is not int
            or value["captured_bytes"] != len(body)
            or value.get("raw_body_sha256") != hashlib.sha256(body).hexdigest()
        ):
            raise ValueError("p62b_body_hash_or_length_mismatch")
    if value["body_complete"] is not True:
        incomplete.append("body_incomplete")
    if value["request_executed"] is not True:
        incomplete.append("request_not_executed")
    status = value.get("http_status")
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise ValueError("p62b_invalid_http_status")
    if status is None:
        incomplete.append("http_status_missing")
    if value.get("response_url") != PHASE_URLS[phase]:
        incomplete.append("response_source_unconfirmed")
    return {
        "id": identifier,
        "request_number": value["request_number"],
        "phase": phase,
        "http_status": status,
        "query_started_at": started.isoformat(),
        "query_finished_at": finished.isoformat(),
        "elapsed_seconds": elapsed,
        "utc_wall_interval_seconds": (finished - started).total_seconds(),
        "complete": not incomplete,
        "incomplete_reasons": incomplete,
    }


def _distribution(values):
    return {
        "count": len(values),
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
        "mean": sum(values) / len(values) if values else None,
        "sum": sum(values),
    }


def _group(records):
    return {
        "complete_records": len(records),
        "http_200_records": sum(row["http_status"] == 200 for row in records),
        "elapsed_seconds": _distribution([row["elapsed_seconds"] for row in records]),
        "utc_started_at": min(
            (row["query_started_at"] for row in records), default=None,
        ),
        "utc_finished_at": max(
            (row["query_finished_at"] for row in records), default=None,
        ),
    }


def analyze_capture(capture_dir):
    """Validate immutable local bytes and summarize only persisted observations.

    The manifest checks local consistency, not independent authenticity. No body
    values or new observation times are returned. Missing final child totals stay
    unknown even when every persisted HTTP pair is individually complete.
    """
    originals = _verified_originals(Path(capture_dir).resolve())
    supervisor = _read_object(originals["supervisor.json"])
    started = _timestamp(supervisor.get("started_at"))
    finished = _timestamp(supervisor.get("finished_at"))
    if finished < started:
        raise ValueError("p62b_reversed_supervisor_timestamps")
    count_status = supervisor.get("business_request_count_status")
    count = supervisor.get("business_route_requests")
    if (
        supervisor.get("mode") != "p62a_owned_child_supervisor"
        or count_status not in ("unknown", "verified_child_report")
        or (count_status == "unknown" and count is not None)
        or (
            count_status == "verified_child_report"
            and (type(count) is not int or count < 0)
        )
        or supervisor.get("status") not in (
            "child_failed", "deadline_exceeded", "child_stopped",
            "traffic_completed_observations_pending",
        )
    ):
        raise ValueError("p62b_invalid_supervisor_record")
    records = [
        _http_record(name, originals)
        for name in sorted(originals)
        if re.fullmatch(r"traffic/http-[0-9]+\.json", name)
    ]
    incomplete = [row for row in records if not row["complete"]]
    orphan_bodies = [
        name for name in originals
        if re.fullmatch(r"traffic/http-[0-9]+\.body", name)
        and name.removesuffix(".body") + ".json" not in originals
    ]
    complete = [row for row in records if row["complete"]]
    business = sorted(
        (row for row in complete if row["phase"] == "business"),
        key=lambda row: row["query_started_at"],
    )
    business_starts = [_timestamp(row["query_started_at"]) for row in business]
    intervals = [
        (second - first).total_seconds()
        for first, second in zip(business_starts, business_starts[1:])
    ]
    return {
        "mode": "offline_original_capture_analysis",
        "manifest_verified_files": len(originals),
        "originals_modified": False,
        "row_values_included": False,
        "supervisor": {
            "status": supervisor["status"],
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "utc_wall_interval_seconds": (finished - started).total_seconds(),
            "elapsed_seconds_including_cleanup": _seconds(
                supervisor.get("elapsed_seconds_including_cleanup"),
            ),
            "business_route_requests": count,
            "business_request_count_status": count_status,
            "originals_may_be_incomplete": supervisor.get("originals_may_be_incomplete"),
        },
        "fully_persisted_http_records": len(complete),
        "incomplete_http_records": incomplete,
        "orphan_body_files": orphan_bodies,
        "business": _group(business),
        "version": _group([row for row in complete if row["phase"] != "business"]),
        "business_start_intervals_seconds": intervals,
        "business_start_interval_statistics_seconds": _distribution(intervals),
        "complete_records": complete,
        "limitations": [
            "Persisted record counts never replace the supervisor execution total.",
            "An interrupted child may have executed requests without complete originals.",
            "Monotonic durations and UTC wall intervals use different clocks.",
            "Local SHA256 consistency does not establish independent authenticity.",
            "This analysis neither replays requests nor renews evidence timestamps.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        result = analyze_capture(arguments.capture_dir)
    except (OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "p62b_original_read_failed"
        print(json.dumps({"status": "analysis_failed", "error": message}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
