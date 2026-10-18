"""Offline integrity and parser checks for a P6.1 collection directory.

Run in a dedicated process: temporary transport guards are process-wide. This
verifies local consistency, not source authenticity or current business health.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from unittest.mock import patch

import requests


PRIMARY_TOOLS = {
    "query_metrics", "query_logs", "check_service_health", "get_running_version",
}


class OfflineBoundaryViolation(BaseException):
    """Forbidden I/O must escape ordinary tool error handling."""


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _file(root, name):
    # P6.1 emits flat filenames only. Reject Windows paths also on POSIX hosts.
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("unsafe_evidence_path")
    target = root / name
    if (
        name in {".", ".."}
        or target.is_symlink()
        or (hasattr(target, "is_junction") and target.is_junction())
        or not target.is_file()
        or target.resolve().parent != root
    ):
        raise ValueError("unsafe_or_missing_evidence_file")
    return target


def _utc(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError("not_utc")
        return parsed
    except (AttributeError, TypeError, ValueError):
        raise ValueError("invalid_evidence_utc_time") from None


def _interval(start, end):
    began, finished = _utc(start), _utc(end)
    if finished < began:
        raise ValueError("reversed_evidence_interval")
    return began, finished


def _check_target(row):
    from scripts.p61_collect_observations import ALLOWED_PATHS
    from urllib.parse import urlsplit

    target = urlsplit(row.get("url", ""))
    if (
        str(row.get("method", "")).upper() != "GET"
        or target.scheme != "http"
        or target.netloc != f"localhost:{target.port}"
        or target.path not in ALLOWED_PATHS.get((target.hostname, target.port), ())
        or target.query
        or target.fragment
        or row.get("request_executed") is not True
    ):
        raise ValueError("invalid_captured_http_target")
    if row.get("error_type") not in {
        None, "Timeout", "ConnectionError", "HTTPError", "ValueError",
    }:
        raise ValueError("unsupported_captured_transport_error")
    if "response_url" in row and row["response_url"] != requests.Request(
        "GET", row["url"], params=row.get("query_parameters"),
    ).prepare().url:
        raise ValueError("captured_response_url_mismatch")
    _interval(row.get("query_started_at"), row.get("query_finished_at"))


def _expected_request(observation):
    from config import LOKI_URL, PROMETHEUS_URL
    from services.metric_profiles import build_promql, selector
    from services.registry import registry

    name = observation.get("tool")
    arguments = observation.get("arguments")
    if not isinstance(arguments, dict) or set(arguments) != (
        {"service", "metric"} if name == "query_metrics" else {"service"}
    ):
        raise ValueError("invalid_observation_arguments")
    service = arguments["service"]
    if service not in {"order-service", "order-db"}:
        raise ValueError("unexpected_observation_service")
    if name == "query_metrics":
        settings = registry.require_capability(service, "metrics")
        expression = build_promql(service, settings, arguments["metric"])
        if expression is None:
            raise ValueError("unsupported_captured_metric")
        return PROMETHEUS_URL + "/api/v1/query", {"query": expression}
    if name == "check_scrape_target":
        registry.require_capability(service, "metrics")
        return PROMETHEUS_URL + "/api/v1/targets", {"state": "active"}
    if name == "query_logs":
        settings = registry.require_capability(service, "logs")
        result = observation.get("result", {})
        scope = result.get("query_range", {})
        if scope.get("mode") != "recent":
            raise ValueError("p61_requires_recent_log_scope")
        start, end = scope.get("start_unix_ns"), scope.get("end_unix_ns")
        if (
            not isinstance(start, str) or not start.isdecimal()
            or not isinstance(end, str) or not end.isdecimal()
            or int(end) - int(start) != 3600 * 1_000_000_000
        ):
            raise ValueError("invalid_captured_log_window")
        return LOKI_URL + "/loki/api/v1/query_range", {
            "query": "{" + selector(settings.labels) + "}",
            "limit": 51,
            "direction": "backward",
            "start": start,
            "end": end,
        }
    capability = {
        "check_service_health": "health", "get_running_version": "runtime",
    }.get(name)
    if capability is None:
        raise ValueError("unsupported_captured_tool")
    settings = registry.require_capability(service, capability)
    if settings.provider not in {"http", "http_version"}:
        raise ValueError("captured_database_probe_forbidden")
    return settings.base_url.rstrip("/") + settings.endpoint, None


def _captured_response(root, row):
    if row.get("error_type"):
        error_class = {
            "Timeout": requests.Timeout,
            "ConnectionError": requests.ConnectionError,
            "HTTPError": requests.HTTPError,
            "ValueError": ValueError,
        }.get(row["error_type"])
        if error_class is None:
            raise ValueError("unsupported_captured_transport_error")
        raise error_class("offline_captured_transport_error")
    if row.get("body_complete") is not True:
        raise ValueError("incomplete_captured_response")
    response = requests.Response()
    response.status_code = row["http_status"]
    response.url = row.get("response_url", row["url"])
    response.headers.update(row.get("response_headers", {}))
    response._content = _file(root, row["raw_body_file"]).read_bytes()
    response.encoding = "utf-8"
    return response


def _reparse_primary(root, observation, row):
    from tools.health import check_service_health
    from tools.logs import query_logs
    from tools.metrics import _parse_metrics_response, query_metrics
    from tools.version import get_running_version

    name = observation["tool"]
    arguments = observation["arguments"]
    original = observation["result"]
    expected_url, expected_params = _expected_request(observation)

    def captured_get(url, **kwargs):
        if url != expected_url or kwargs.get("params") != expected_params:
            raise OfflineBoundaryViolation("offline_parser_request_mismatch")
        return _captured_response(root, row)

    if name == "query_metrics":
        began, finished = _interval(
            original.get("query_started_at"), original.get("query_finished_at"),
        )
        if not (
            _utc(observation["query_started_at"]) <= began
            <= _utc(row["query_started_at"]) <= _utc(row["query_finished_at"])
            <= finished <= _utc(observation["collected_at"])
        ):
            raise ValueError("metric_result_time_mismatch")
        timing = {
            "query_started_at": began.isoformat(),
            "query_finished_at": finished.isoformat(),
        }
        try:
            response = _captured_response(root, row)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            exc.query_started_at = timing["query_started_at"]
            exc.query_finished_at = timing["query_finished_at"]
            with patch("tools.metrics.prometheus_client.query", side_effect=exc):
                return query_metrics(**arguments)
        return _parse_metrics_response(
            payload, arguments["service"], arguments["metric"],
            expected_params["query"], finished,
        ) | timing
    functions = {
        "query_logs": query_logs,
        "check_service_health": check_service_health,
        "get_running_version": get_running_version,
    }
    with ExitStack() as stack:
        stack.enter_context(patch("requests.get", captured_get))
        if name == "query_logs":
            stack.enter_context(patch(
                "tools.logs.time.time_ns", return_value=int(expected_params["end"]),
            ))
        return functions[name](**arguments)


def verify_collection(path):
    """Verify P6.1 artifacts without fetching services or refreshing evidence."""
    supplied = Path(path).absolute()
    if any(
        item.is_symlink()
        or (hasattr(item, "is_junction") and item.is_junction())
        for item in (supplied, *supplied.parents)
    ):
        raise ValueError("symlink_collection_directory_forbidden")
    root = supplied.resolve(strict=True)
    manifest = _json(_file(root, "sha256.json"))
    if not isinstance(manifest, dict) or "sha256.json" in manifest:
        raise ValueError("invalid_evidence_manifest")
    if {item.name for item in root.iterdir()} != set(manifest) | {"sha256.json"}:
        raise ValueError("manifest_file_set_mismatch")
    for name, digest in manifest.items():
        target = _file(root, name)
        if (
            not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or hashlib.sha256(target.read_bytes()).hexdigest() != digest
        ):
            raise ValueError("evidence_hash_mismatch")
    report = _json(_file(root, "collection_report.json"))
    observations = _json(_file(root, "observations.json"))
    supplemental = _json(_file(root, "supplemental_observations.json"))
    if not isinstance(observations, dict) or set(observations) != PRIMARY_TOOLS:
        raise ValueError("invalid_primary_observation_set")
    if not isinstance(supplemental, list):
        raise ValueError("invalid_supplemental_observations")
    for key, observation in observations.items():
        if not isinstance(observation, dict) or observation.get("tool") != key:
            raise ValueError("primary_tool_identity_mismatch")
    rows = report.get("http_requests")
    if not isinstance(rows, list):
        raise ValueError("invalid_http_records")
    indexed = {}
    required = {
        "collection_report.json", "observations.json", "supplemental_observations.json",
    }
    collection_start, collection_end = _interval(
        report.get("started_at"), report.get("finished_at"),
    )
    for index, row in enumerate(rows, 1):
        identifier = f"http-{index:03d}"
        if not isinstance(row, dict) or row.get("id") != identifier:
            raise ValueError("invalid_http_record_identity")
        record_name = identifier + ".json"
        if _json(_file(root, record_name)) != row:
            raise ValueError("http_record_report_mismatch")
        required.add(record_name)
        _check_target(row)
        if not (
            collection_start <= _utc(row["query_started_at"])
            <= _utc(row["query_finished_at"]) <= collection_end
        ):
            raise ValueError("http_time_outside_collection")
        if "raw_body_file" in row:
            if row["raw_body_file"] != identifier + ".body":
                raise ValueError("raw_body_request_identity_mismatch")
            body = _file(root, row["raw_body_file"]).read_bytes()
            if (
                row.get("raw_body_sha256") != hashlib.sha256(body).hexdigest()
                or type(row.get("captured_bytes")) is not int
                or row["captured_bytes"] != len(body)
            ):
                raise ValueError("raw_body_hash_or_size_mismatch")
            required.add(row["raw_body_file"])
        elif row.get("body_complete") or not row.get("error_type"):
            raise ValueError("missing_raw_body")
        indexed[identifier] = row
    if set(manifest) != required:
        raise ValueError("unbound_manifest_files")

    def forbidden(*args, **kwargs):
        raise OfflineBoundaryViolation("p62a_verifier_external_io_forbidden")

    checks = []
    used = set()
    with ExitStack() as stack:
        for target in (
            "socket.socket.connect", "socket.socket.connect_ex",
            "socket.create_connection", "socket.getaddrinfo",
            "requests.sessions.Session.request", "openai.OpenAI", "openai.AsyncOpenAI",
            "psycopg.connect", "subprocess.Popen", "threading.Thread.start",
        ):
            stack.enter_context(patch(target, forbidden))
        for observation in [*observations.values(), *supplemental]:
            if (
                not isinstance(observation, dict)
                or not isinstance(observation.get("result"), dict)
                or observation.get("provenance") != "real_tool_collection"
            ):
                raise ValueError("invalid_observation_envelope")
            identifiers = observation.get("request_ids")
            if (
                not isinstance(identifiers, list) or len(identifiers) != 1
                or identifiers[0] not in indexed or identifiers[0] in used
            ):
                raise ValueError("invalid_or_reused_observation_request")
            used.add(identifiers[0])
            row = indexed[identifiers[0]]
            url, parameters = _expected_request(observation)
            if (
                observation.get("source") != row["url"] or row["url"] != url
                or row.get("query_parameters") != parameters
                or observation.get("provider_request_executed") is not True
            ):
                raise ValueError("observation_source_or_arguments_mismatch")
            began, finished = _interval(
                observation.get("query_started_at"), observation.get("collected_at"),
            )
            if not (
                collection_start <= began <= _utc(row["query_started_at"])
                <= _utc(row["query_finished_at"]) <= finished <= collection_end
            ):
                raise ValueError("observation_request_time_mismatch")
            primary = any(observation is value for value in observations.values())
            if primary and _reparse_primary(root, observation, row) != observation["result"]:
                raise ValueError("captured_tool_result_does_not_match_raw_body")
            checks.append({
                "tool": observation["tool"], "arguments": observation["arguments"],
                "request_id": identifiers[0], "source": url,
                "collected_at": observation["collected_at"],
                "raw_body_reparsed": primary,
                "status": observation["result"].get("status"),
                "reason": observation["result"].get("reason"),
            })
    return {
        "verified": True,
        "collection_directory": str(root),
        "manifest_sha256": hashlib.sha256((root / "sha256.json").read_bytes()).hexdigest(),
        "hashed_file_count": len(manifest),
        "http_record_count": len(rows),
        "observation_checks": checks,
        "observations": observations,
        "real_http_requests": 0,
        "real_model_sdk_requests": 0,
        "use_for_current_state": False,
        "business_recovery_verified": False,
        "limitations": [
            "Local hashes establish consistency, not an external source signature.",
            "Primary results are reparsed at original capture times, never refreshed.",
            "Supplemental observations have hash/source/argument checks only.",
            "Historical capture and offline replay do not establish current business health.",
            "Transport exception details were not retained; unsupported categories fail closed.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection-dir", required=True, type=Path)
    arguments = parser.parse_args()
    print(json.dumps(verify_collection(arguments.collection_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
