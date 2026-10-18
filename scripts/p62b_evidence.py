"""P6.2b bounded read-only capture and offline Mock-SDK Runtime replay.

The default CLI only reads a sealed local capture. Live capture is explicit and
never allows a business route. Run in a dedicated process: guards are local to
the process and temporarily replace transports and existing tool functions.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
from unittest.mock import patch

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import p61_collect_observations as capture
from scripts.p61_readonly_integration import BoundaryViolation, run_case
from scripts.p62a_verify_evidence import (
    _captured_response, _expected_request, _file, _interval, _reparse_primary, _utc,
)


SERVICE = "order-service"
MAX_COLLECTION_REQUESTS = 9
LOKI_INGESTION_GRACE_SECONDS = 10
FILTER_RULE = (
    'Keep only alerts with labels.service == "order-service" and '
    'labels.alertname == "HighHttp5xxRate" and state == "firing"; '
    "retain the full original snapshot, including excluded inventory-service."
)
METRICS = (
    "http_5xx_rate", "http_requests_total", "http_request_rate",
    "http_latency_p95_seconds", "service_scrape_up",
)


def _write(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def _seal(root):
    _write(root / "sha256.json", {
        item.name: hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(root.iterdir()) if item.is_file()
    })


def _forbidden(*args, **kwargs):
    raise BoundaryViolation("p62b_network_model_database_or_remediation_forbidden")


def _guards(stack, *, offline):
    targets = [
        "openai.OpenAI", "openai.AsyncOpenAI", "psycopg.connect",
        "subprocess.Popen", "threading.Thread.start",
    ]
    if offline:
        targets += [
            "socket.socket.connect", "socket.socket.connect_ex",
            "socket.create_connection", "socket.getaddrinfo",
            "requests.sessions.Session.request",
        ]
    for target in targets:
        stack.enter_context(patch(target, _forbidden))


def summarize_alerts(alerts, rules):
    """Keep returned fields intact; rule-wide state is not a service state."""
    from alert_parser import normalize_snapshot

    valid_alerts = (
        isinstance(alerts, dict) and alerts.get("status") == "success"
        and isinstance(alerts.get("data"), dict)
        and isinstance(alerts["data"].get("alerts"), list)
    )
    items = alerts["data"]["alerts"] if valid_alerts else []
    valid_alerts = valid_alerts and normalize_snapshot(alerts).complete
    selected = [deepcopy(item) for item in items if (
        isinstance(item, dict) and isinstance(item.get("labels"), dict)
        and item["labels"].get("service") == SERVICE
        and item["labels"].get("alertname") == "HighHttp5xxRate"
    )]
    states = {item.get("state") for item in selected} if valid_alerts else set()
    if not valid_alerts or not states <= {"inactive", "pending", "firing"}:
        state = "unknown"
    else:
        state = next((s for s in ("firing", "pending", "inactive") if s in states), "inactive")
    selected_rules = []
    if (
        isinstance(rules, dict) and rules.get("status") == "success"
        and isinstance(rules.get("data"), dict)
        and isinstance(rules["data"].get("groups"), list)
    ):
        for group in rules["data"]["groups"]:
            if not isinstance(group, dict) or not isinstance(group.get("rules"), list):
                continue
            for rule in group["rules"]:
                if isinstance(rule, dict) and rule.get("name") == "HighHttp5xxRate":
                    selected_rules.append({
                        "group": {
                            key: deepcopy(value) for key, value in group.items()
                            if key != "rules"
                        },
                        "rule": deepcopy(rule),
                        "scope": "rule_all_series_not_order_service",
                    })
    return {
        "order_service_state": state,
        "snapshot_complete": valid_alerts,
        "state_basis": (
            "no_data" if not valid_alerts else "returned_service_alert_state"
            if selected else "absence_from_complete_alert_snapshot"
        ),
        "order_service_alerts": selected,
        "high_http_5xx_rules": selected_rules,
        "evaluation_fields": "Only API-returned fields are retained; none are invented.",
    }


def _loki_query_window(start, end):
    _, finished = _interval(start, end)
    return {
        "start": start,
        "end": (finished + timedelta(seconds=LOKI_INGESTION_GRACE_SECONDS)).isoformat(),
        "bounds": "[start, end)",
    }


def correlate_logs(result, start, end, *, log_utc_offset=None):
    """Preserve event text and Loki time; timestamps alone are not identities."""
    began, finished = _interval(start, end)
    query_range = result.get("query_range")
    query_began = query_finished = None
    if isinstance(query_range, dict):
        try:
            query_began, query_finished = _interval(
                query_range.get("start"), query_range.get("end"),
            )
        except ValueError:
            pass
    offset = None
    if log_utc_offset is not None:
        if not re.fullmatch(r"[+-](?:[01][0-9]|2[0-3]):[0-5][0-9]", log_utc_offset):
            raise ValueError("invalid_verified_log_utc_offset")
        sign = 1 if log_utc_offset[0] == "+" else -1
        offset = timezone(sign * timedelta(
            hours=int(log_utc_offset[1:3]), minutes=int(log_utc_offset[4:6]),
        ))
    rows = []
    for entry in result.get("logs", []):
        line = entry.get("line", "")
        match = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:[,.]\d+)?\s", line)
        event_text = match[1] if match else None
        event_utc = None
        if event_text and offset is not None:
            try:
                event_utc = datetime.strptime(event_text, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=offset,
                ).astimezone(timezone.utc)
            except ValueError:
                pass
        try:
            loki_utc = _utc(entry.get("timestamp_utc"))
        except ValueError:
            loki_utc = None
        content_matches = bool(
            entry.get("labels", {}).get("service") == SERVICE
            and re.search(r"\bservice=order-service\b", line)
            and re.search(r"\bstatus=500\b", line)
            and "request failed" in line
        )
        # A second-resolution event represents [event, event + 1s).
        body_in_fault_window = bool(
            event_utc is not None and event_utc < finished
            and event_utc + timedelta(seconds=1) > began
        )
        loki_in_query_window = bool(
            loki_utc is not None and query_began is not None
            and query_began <= loki_utc < query_finished
        )
        rows.append({
            "line": line,
            "labels": deepcopy(entry.get("labels", {})),
            "loki_timestamp": entry.get("timestamp"),
            "loki_timestamp_utc": entry.get("timestamp_utc"),
            "body_event_time_text": event_text,
            "body_event_time_utc": event_utc.isoformat() if event_utc else None,
            "body_event_time_offset": log_utc_offset,
            "body_event_time_precision": "seconds" if event_text else "unknown",
            "content_matches_order_service_500": content_matches,
            "body_event_in_fault_window": body_in_fault_window,
            "loki_timestamp_in_query_window": loki_in_query_window,
            "candidate_in_fault_window": bool(
                content_matches and body_in_fault_window and loki_in_query_window
            ),
            "route_identity": "not_present_in_demo_error_log",
            "request_identity": "no_shared_request_id",
        })
    return {
        "status": result.get("status", "no_data"),
        "fault_window": {"start": start, "end": end, "bounds": "[start, end)"},
        "logql": result.get("logql"),
        "query_range": deepcopy(query_range),
        "loki_query_window": (
            {"start": query_range["start"], "end": query_range["end"], "bounds": "[start, end)"}
            if query_began is not None else None
        ),
        "entries": rows,
        "candidate_count": sum(row["candidate_in_fault_window"] for row in rows),
        "association": "limited_time_content_count_only_not_per_request_identity",
        "limitations": [
            "Demo /error text has no path or request_id; matching 500 text is only a candidate.",
            "Loki timestamps are not substituted for body event times.",
            "The separate Loki range allows only a fixed 10s ingestion grace after the fault window.",
            "No wait or retry is added; logs ingested after the single query may remain absent.",
            "A missing or invalid actual Loki query range cannot support a correlated candidate.",
            "A local body timestamp remains unconverted without a verified UTC offset.",
            "Missing logs do not prove absence of a fault or business recovery.",
        ],
    }


def collect_readonly(
    output_dir,
    start,
    end,
    *,
    confirmed_local_isolation=False,
    log_utc_offset=None,
):
    """Explicit bounded reads; never sends /error, /order, health or DB probes."""
    if confirmed_local_isolation is not True:
        raise ValueError("verified_local_isolation_required_before_collection")
    began, finished = _interval(start, end)
    if not timedelta(0) < finished - began <= timedelta(minutes=2):
        raise ValueError("fault_evidence_window_must_be_at_most_two_minutes")
    loki_window = _loki_query_window(start, end)
    from clients.prometheus_client import PrometheusClient
    from tools.logs import query_logs
    from tools.metrics import query_metrics
    from tools.version import get_running_version

    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    observations = {}
    payloads = {}
    started = capture._utc_now()
    with ExitStack() as stack:
        _guards(stack, offline=False)
        allowed = {key: set(value) for key, value in capture.ALLOWED_PATHS.items()}
        allowed[("localhost", 9090)].add("/api/v1/rules")
        # Replacement is scoped; the P6.1 global allowlist is never mutated.
        stack.enter_context(patch.object(capture, "ALLOWED_PATHS", allowed))
        records = stack.enter_context(capture._capture_http(root))

        def observe(name, function, arguments):
            first = len(records)
            queried = capture._utc_now()
            result = function(**arguments)
            current = records[first:]
            if len(current) != 1 or len(records) > MAX_COLLECTION_REQUESTS:
                raise BoundaryViolation("p62b_collection_request_bound_violated")
            return {
                "tool": name,
                "arguments": arguments,
                "result": result,
                "query_started_at": queried,
                "collected_at": capture._utc_now(),
                "source": current[0]["url"],
                "request_ids": [current[0]["id"]],
                "provenance": "real_tool_collection",
            }

        client = PrometheusClient("http://localhost:9090")
        for name, method in (("alerts", client.get_alerts), ("rules", client.get_rules)):
            try:
                payloads[name] = observe(name, method, {})
            except (requests.RequestException, ValueError):
                row = records[-1]
                payloads[name] = {
                    "tool": name,
                    "arguments": {},
                    "result": None,
                    "status": "no_data",
                    "query_started_at": row["query_started_at"],
                    "collected_at": capture._utc_now(),
                    "source": row["url"],
                    "request_ids": [row["id"]],
                    "provenance": "real_tool_collection",
                }
        for metric in METRICS:
            envelope = observe("query_metrics", query_metrics, {
                "service": SERVICE, "metric": metric,
            })
            observations["query_metrics" if metric == "http_5xx_rate" else metric] = envelope
        observations["query_logs"] = observe("query_logs", query_logs, {
            "service": SERVICE, "start": loki_window["start"], "end": loki_window["end"],
        })
        observations["get_running_version"] = observe("get_running_version", get_running_version, {
            "service": SERVICE,
        })
    report = {
        "started_at": started,
        "finished_at": capture._utc_now(),
        "provenance": "real_readonly_collection",
        "fault_window": {"start": start, "end": end},
        "loki_query_window": loki_window,
        "loki_ingestion_grace_seconds": LOKI_INGESTION_GRACE_SECONDS,
        "verified_log_utc_offset": log_utc_offset,
        "http_requests": records,
        "observations": observations,
        "prometheus_snapshots": payloads,
        "alert_summary": summarize_alerts(
            payloads["alerts"].get("result"), payloads["rules"].get("result"),
        ),
        "log_correlation": correlate_logs(
            observations["query_logs"]["result"], start, end, log_utc_offset=log_utc_offset,
        ),
        "business_route_requests": 0,
        "real_model_sdk_requests": 0,
        "remediation_executions": 0,
        "business_recovery_verified": False,
    }
    _write(root / "collection_report.json", report)
    _seal(root)
    return report


def verify_capture(path):
    """Check originals offline; hashes establish consistency, not authenticity."""
    supplied = Path(path).absolute()
    if any(item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction())
           for item in (supplied, *supplied.parents)):
        raise ValueError("symlink_collection_directory_forbidden")
    root = supplied.resolve(strict=True)
    manifest = json.loads(_file(root, "sha256.json").read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or set(manifest) | {"sha256.json"} != {p.name for p in root.iterdir()}
    ):
        raise ValueError("manifest_file_set_mismatch")
    for name, digest in manifest.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid_evidence_digest")
        if hashlib.sha256(_file(root, name).read_bytes()).hexdigest() != digest:
            raise ValueError("evidence_hash_mismatch")
    report = json.loads(_file(root, "collection_report.json").read_text(encoding="utf-8"))
    began, finished = _interval(report["started_at"], report["finished_at"])
    rows = report["http_requests"]
    if not isinstance(rows, list) or len(rows) > MAX_COLLECTION_REQUESTS:
        raise ValueError("invalid_collection_request_count")
    required = {"collection_report.json"}
    for index, row in enumerate(rows, 1):
        identifier = f"http-{index:03d}"
        record = json.loads(
            _file(root, identifier + ".json").read_text(encoding="utf-8"),
        )
        if row.get("id") != identifier or row != record:
            raise ValueError("http_record_identity_mismatch")
        required.add(identifier + ".json")
        if not began <= _utc(row["query_started_at"]) <= _utc(row["query_finished_at"]) <= finished:
            raise ValueError("request_time_outside_collection")
        from urllib.parse import urlsplit
        target = urlsplit(row["url"])
        allowed = {
            ("localhost", 9090, "/api/v1/alerts"),
            ("localhost", 9090, "/api/v1/rules"),
            ("localhost", 9090, "/api/v1/query"),
            ("localhost", 3100, "/loki/api/v1/query_range"),
            ("localhost", 8001, "/version"),
        }
        if (str(row.get("method", "")).upper() != "GET" or target.scheme != "http"
                or target.netloc != f"localhost:{target.port}"
                or (target.hostname, target.port, target.path) not in allowed
                or target.query or target.fragment or row.get("request_executed") is not True):
            raise ValueError("forbidden_captured_target")
        if (
            (row.get("http_status") is not None and "response_url" not in row)
            or ("response_url" in row and row["response_url"] != requests.Request(
                "GET", row["url"], params=row.get("query_parameters"),
            ).prepare().url)
        ):
            raise ValueError("captured_response_url_mismatch")
        if "raw_body_file" in row:
            name = identifier + ".body"
            body = _file(root, name).read_bytes()
            if (
                row["raw_body_file"] != name
                or row.get("raw_body_sha256") != hashlib.sha256(body).hexdigest()
                or row.get("captured_bytes") != len(body)
            ):
                raise ValueError("raw_body_hash_or_size_mismatch")
            required.add(name)
        elif row.get("body_complete") or not row.get("error_type"):
            raise ValueError("missing_raw_body")
    if required != set(manifest):
        raise ValueError("unbound_manifest_file")
    if report.get("provenance") != "real_readonly_collection":
        raise ValueError("invalid_collection_provenance")
    with ExitStack() as stack:
        _guards(stack, offline=True)
        _verify_envelopes(root, report)
    return report


def _verify_envelopes(root, report):
    from clients.loki_client import parse_time_range
    from services.metric_profiles import selector
    from services.registry import registry
    from tools.logs import LOG_LIMIT, query_logs

    rows = {row["id"]: row for row in report["http_requests"]}
    used = set()
    began, finished = _interval(report["started_at"], report["finished_at"])
    window = report["fault_window"]
    loki_window = _loki_query_window(window["start"], window["end"])
    if (
        report.get("loki_query_window") != loki_window
        or report.get("loki_ingestion_grace_seconds") != LOKI_INGESTION_GRACE_SECONDS
    ):
        raise ValueError("loki_query_window_must_match_fixed_ingestion_grace")
    start_ns, end_ns = parse_time_range(loki_window["start"], loki_window["end"])
    observations = report["observations"]
    expected = {
        "query_metrics" if metric == "http_5xx_rate" else metric: (
            "query_metrics", {"service": SERVICE, "metric": metric},
        ) for metric in METRICS
    }
    expected.update({
        "query_logs": (
            "query_logs", {
                "service": SERVICE,
                "start": loki_window["start"],
                "end": loki_window["end"],
            },
        ),
        "get_running_version": ("get_running_version", {"service": SERVICE}),
    })
    snapshots = report["prometheus_snapshots"]
    if not isinstance(observations, dict) or set(observations) != set(expected):
        raise ValueError("unexpected_observation_set")
    if not isinstance(snapshots, dict) or set(snapshots) != {"alerts", "rules"}:
        raise ValueError("unexpected_snapshot_set")
    expected.update({name: (name, {}) for name in snapshots})
    envelopes = [*observations.items(), *snapshots.items()]
    for key, envelope in envelopes:
        if (
            not isinstance(envelope, dict)
            or (envelope.get("tool"), envelope.get("arguments")) != expected[key]
        ):
            raise ValueError("observation_tool_or_arguments_mismatch")
        identifiers = envelope.get("request_ids")
        if (envelope.get("provenance") != "real_tool_collection"
                or not isinstance(identifiers, list) or len(identifiers) != 1
                or identifiers[0] not in rows or identifiers[0] in used):
            raise ValueError("invalid_observation_request_binding")
        row = rows[identifiers[0]]
        used.add(identifiers[0])
        if (envelope.get("source") != row["url"]
                or not began <= _utc(envelope["query_started_at"])
                <= _utc(row["query_started_at"]) <= _utc(row["query_finished_at"])
                <= _utc(envelope["collected_at"]) <= finished):
            raise ValueError("observation_source_or_time_mismatch")
        name = envelope["tool"]
        if name in {"alerts", "rules"}:
            url = "http://localhost:9090/api/v1/" + name
            params = {"type": "alert"} if name == "rules" else None
            if row["url"] != url or row.get("query_parameters") != params:
                raise ValueError("snapshot_request_mismatch")
            try:
                response = _captured_response(root, row)
                response.raise_for_status()
                parsed = response.json()
            except (requests.RequestException, ValueError):
                parsed = None
        elif name == "query_logs":
            if envelope["arguments"] != {
                "service": SERVICE, "start": loki_window["start"], "end": loki_window["end"],
            }:
                raise ValueError("log_query_window_mismatch")
            logql = "{" + selector(registry.require_capability(SERVICE, "logs").labels) + "}"
            params = {"query": logql, "limit": LOG_LIMIT + 1, "direction": "backward",
                      "start": str(start_ns), "end": str(end_ns)}
            if (row["url"] != "http://localhost:3100/loki/api/v1/query_range"
                    or row.get("query_parameters") != params):
                raise ValueError("log_request_parameters_mismatch")

            def captured_get(url, **kwargs):
                if url != row["url"] or kwargs.get("params") != params:
                    return _forbidden()
                return _captured_response(root, row)

            with patch("requests.get", captured_get):
                parsed = query_logs(**envelope["arguments"])
        elif name in {"query_metrics", "get_running_version"}:
            expected_url, expected_params = _expected_request(envelope)
            if (
                row["url"] != expected_url
                or row.get("query_parameters") != expected_params
            ):
                raise ValueError("observation_request_mismatch")
            parsed = _reparse_primary(root, envelope, row)
        else:
            raise ValueError("unsupported_captured_observation")
        if parsed != envelope["result"]:
            raise ValueError("parsed_result_does_not_match_raw_original")
    if used != set(rows):
        raise ValueError("unbound_http_request")
    if report["alert_summary"] != summarize_alerts(
        snapshots["alerts"]["result"], snapshots["rules"]["result"],
    ):
        raise ValueError("alert_summary_does_not_match_original")
    if report["log_correlation"] != correlate_logs(
        observations["query_logs"]["result"], window["start"], window["end"],
        log_utc_offset=report.get("verified_log_utc_offset"),
    ):
        raise ValueError("log_correlation_does_not_match_original")


def run_runtime_replay(output_dir, *, collection_dir=None):
    """Reuse the P6.1 bootstrap; only saved evidence and Mock SDK are allowed."""
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    with ExitStack() as stack:
        _guards(stack, offline=True)
        collected = (
            verify_capture(collection_dir) if collection_dir is not None else None
        )
        observations = deepcopy(collected["observations"]) if collected else {}
        source = collected["prometheus_snapshots"]["alerts"] if collected else {}
        raw = deepcopy(source.get("result"))
        summary = summarize_alerts(raw, None)
        selected = [
            item for item in summary["order_service_alerts"]
            if item.get("state") == "firing"
        ] if summary["order_service_state"] == "firing" else []
        origin = "real_firing_alert_snapshot_replay" if selected else "synthetic_alert_input"
        filtered = deepcopy(raw) if selected else None
        if filtered is not None:
            filtered["data"]["alerts"] = deepcopy(selected)
        with tempfile.TemporaryDirectory(prefix="p62b-runtime-") as temporary:
            runtime_root = Path(temporary) / "case"
            runtime = run_case(
                runtime_root,
                observations=observations,
                alert_snapshot=filtered,
                alert_source=source.get("source") if filtered else None,
                alert_collected_at=source.get("collected_at") if filtered else None,
            )
            shutil.copytree(runtime_root, root / "runtime")
        report = {
            "replay_started_at": runtime["started_at"],
            "alert_input_origin": origin,
            "full_real_alert_snapshot": raw,
            "alert_snapshot_collected_at": source.get("collected_at"),
            "isolation_filter_rule": FILTER_RULE,
            "isolated_alert_snapshot": (
                filtered if filtered is not None
                else runtime["http_fixtures"][0]["body"]
            ),
            "synthetic_fallback_reason": (
                "no_captured_order_service_firing" if not selected else None
            ),
            "captured_observations": observations,
            "fault_window": deepcopy(collected["fault_window"]) if collected else None,
            "loki_query_window": deepcopy(collected["loki_query_window"]) if collected else None,
            "model_origin": "scripted_mock_sdk_not_real_deepseek",
            "runtime_report": "runtime/report.json",
            "log_query_scope": runtime["log_query_scope"],
            "log_query_window": runtime["alert_window"],
            "log_query_window_basis": runtime["alert_window_basis"],
            "session": runtime["session"],
            "checkpoint": runtime["checkpoint"],
            "incident": runtime["incident"],
            "real_model_sdk_requests": 0,
            "business_route_requests": 0,
            "remediation_executions": 0,
            "recovery_worker_started": False,
            "business_recovery_verified": False,
            "use_for_current_state": False,
            "review_required": True,
            "temporary_store_isolation": True,
        }
        _write(root / "report.json", report)
        manifest = {str(item.relative_to(root)): hashlib.sha256(item.read_bytes()).hexdigest()
                    for item in sorted(root.rglob("*")) if item.is_file()}
        _write(root / "sha256.json", manifest)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--collection-dir", type=Path)
    parser.add_argument("--collect-readonly", action="store_true")
    parser.add_argument("--confirmed-local-isolation", action="store_true")
    parser.add_argument("--fault-start")
    parser.add_argument("--fault-end")
    parser.add_argument("--verified-log-utc-offset")
    args = parser.parse_args()
    if args.collect_readonly:
        result = collect_readonly(args.output_dir, args.fault_start, args.fault_end,
                                  confirmed_local_isolation=args.confirmed_local_isolation,
                                  log_utc_offset=args.verified_log_utc_offset)
    else:
        result = run_runtime_replay(args.output_dir, collection_dir=args.collection_dir)
    print(json.dumps({"report_directory": str(args.output_dir.resolve()),
                      "real_model_sdk_requests": result["real_model_sdk_requests"],
                      "business_route_requests": result["business_route_requests"]}))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
