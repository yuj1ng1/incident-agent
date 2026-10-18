"""Case-scoped frozen P6.2b evidence projection; no collection or model calls.

Run in a dedicated process: the reused P6.2b verification guards temporarily
replace process-wide transports. Hashes prove local consistency, not independent
authenticity. Derived reference IDs are not Runtime observation identities.
"""
from contextlib import ExitStack
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re

from scripts import p62b_error_experiment as experiment
from scripts import p62b_evidence as evidence


DEFAULT_CAPTURE = (
    Path(__file__).resolve().parents[1]
    / "data" / "p62b-first-error-20261018-01"
)
SCOPE = "real_model_historical_evidence_analysis"
REFERENCE_NAMESPACE = "derived_frozen_evidence_references_not_runtime_observation_ids"


def _sha(encoded):
    return hashlib.sha256(encoded).hexdigest()


def _json(encoded):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("p63_duplicate_json_key")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("p63_nonfinite_json_value")

    return json.loads(encoded, object_pairs_hook=unique, parse_constant=invalid)


def _stamp(value):
    parsed = evidence._utc(value)
    return parsed.isoformat()


def _verified_files(capture_dir):
    supplied = Path(capture_dir).absolute()
    for path in (supplied, *supplied.parents):
        if path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()
        ):
            raise ValueError("p63_linked_capture_forbidden")
    root = supplied.resolve(strict=True)
    files = {}
    for path in root.rglob("*"):
        if path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()
        ):
            raise ValueError("p63_linked_original_forbidden")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    if "sha256.json" not in files:
        raise ValueError("p63_missing_capture_manifest")
    for name, encoded in files.items():
        if PurePosixPath(name).name != "sha256.json":
            continue
        manifest = _json(encoded)
        if not isinstance(manifest, dict) or not manifest:
            raise ValueError("p63_invalid_manifest")
        parent = PurePosixPath(name).parent
        prefix = "" if str(parent) == "." else str(parent) + "/"
        actual = {
            key[len(prefix):] for key in files
            if key.startswith(prefix) and key != name
        }
        # The historical Runtime manifests used Windows separators. Its inner
        # seal predates the final agent log and lock; the root seal binds both.
        normalized = {
            relative.replace("\\", "/"): digest
            for relative, digest in manifest.items()
        }
        if any("\\" in relative for relative in manifest) and name not in {
            "runtime-replay/sha256.json", "runtime-replay/runtime/sha256.json",
        }:
            raise ValueError("p63_noncanonical_manifest_path")
        allowed_late_files = (
            {"agent.log", "stores/incidents.json.lock"}
            if name == "runtime-replay/runtime/sha256.json" else set()
        )
        if (
            len(normalized) != len(manifest)
            or actual - set(normalized) != allowed_late_files
            or set(normalized) - actual
        ):
            raise ValueError("p63_manifest_file_set_mismatch")
        for relative, digest in normalized.items():
            path = PurePosixPath(relative)
            if (
                not relative or path.is_absolute() or ".." in path.parts
                or path.as_posix() != relative or "\\" in relative
                or ":" in relative or relative == "sha256.json"
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or _sha(files[prefix + relative]) != digest
            ):
                raise ValueError("p63_manifest_hash_or_path_invalid")
    return root, files


def _version(value):
    if (
        not isinstance(value, dict) or value.get("service") != "order-service"
        or value.get("commit_sha") is not None
        or value.get("short_sha") is not None
        or value.get("runtime_flags") != {
            "simulate_code_regression": False,
            "simulate_slow_request": False,
        }
        or any(
            type(flag) is not bool
            for flag in value.get("runtime_flags", {}).values()
        )
    ):
        raise ValueError("p63_unexpected_frozen_version")
    return {
        "service": "order-service",
        "process_started_at": _stamp(value["process_started_at"]),
        "commit_sha": None,
        "runtime_flags": {
            "simulate_code_regression": False,
            "simulate_slow_request": False,
        },
    }


def build_context(capture_dir=DEFAULT_CAPTURE):
    """Verify sealed originals and return an explicitly historical allowlist.

    Only the fixed case layout is supported. Unknown fields are never copied;
    unsupported source shapes fail closed. No current timestamp is generated.
    """
    root, files = _verified_files(capture_dir)

    def read(name):
        return _json(files[name])

    supervisor = read("experiment/supervisor.json")
    with ExitStack() as stack:
        evidence._guards(stack, offline=True)
        traffic = experiment._verified_child_report(
            root / "experiment" / "traffic", supervisor["requested_count"],
        )
        collected = evidence.verify_capture(root / "observations")
    if (
        supervisor.get("mode") != "p62b_owned_error_child_supervisor"
        or supervisor.get("status") != "child_stopped"
        or supervisor.get("business_request_count_status") != "verified_child_report"
        or supervisor.get("business_route_requests")
        != traffic["business_route_requests"]
        or supervisor.get("identity_verification_requests")
        != traffic["identity_verification_requests"]
        or supervisor.get("expected_500_responses") != traffic["expected_500_responses"]
        or supervisor.get("business_recovery_verified") is not False
        or supervisor.get("real_model_sdk_requests") != 0
    ):
        raise ValueError("p63_supervisor_binding_invalid")
    rows = traffic["http_requests"]
    faults = [row for row in rows if row["phase"] == "fault"]
    versions = [row for row in rows if row["phase"] != "fault"]
    if (
        len(faults) != 5 or len(versions) != 10
        or any(row.get("body_complete") is not True for row in rows)
        or any(row.get("http_status") != 200 for row in versions)
        or any(row.get("response_url") != row["url"] for row in rows)
    ):
        raise ValueError("p63_case_request_count_mismatch")
    fault_window = {
        "start": faults[0]["query_started_at"],
        "end": faults[-1]["query_finished_at"],
    }
    if (
        collected["fault_window"] != fault_window
        or not evidence._utc(supervisor["started_at"])
        <= evidence._utc(traffic["started_at"])
        <= evidence._utc(fault_window["start"])
        <= evidence._utc(fault_window["end"])
        <= evidence._utc(traffic["finished_at"])
        <= evidence._utc(supervisor["finished_at"])
        <= evidence._utc(collected["started_at"])
    ):
        raise ValueError("p63_fault_collection_windows_unbound")
    sources = []

    def add(label, name, when, value, originals, *, status="ok", missing=False):
        if status not in {"ok", "no_data"}:
            raise ValueError("p63_invalid_projected_status")
        projected = json.dumps(
            value, ensure_ascii=False, sort_keys=True, allow_nan=False,
        ).encode("utf-8")
        sources.append({
            "observation_id": f"p63:{label}:{_sha(projected)[:16]}",
            "name": name,
            "collected_at": _stamp(when),
            "timestamp_basis": {
                "frozen_experiment_authorization": "design_record_written_at",
                "captured_http_response": "http_response_finished_at",
                "captured_version_checks": "last_identity_response_finished_at",
                "captured_metrics_exposition": "http_response_finished_at",
                "historical_mock_runtime_replay": "mock_replay_finished_at",
            }.get(name, "original_collection_envelope_collected_at"),
            "status": status,
            "historical_only": True,
            "extraction_status": "supported",
            "data_missing": missing,
            "samples": [],
            "value": value,
            "originals": [
                {"path": path, "sha256": _sha(files[path])}
                for path in originals
            ],
        })

    authorization = read("authorization.json")
    if (
        authorization["target"]["service"] != "order-service"
        or authorization["target"]["method"] != "GET"
        or authorization["target"]["url"] != experiment.ERROR_URL
        or authorization.get("expected_http_status") != 500
        or authorization.get("expected_effects") != list(experiment.EXPECTED_EFFECTS)
    ):
        raise ValueError("p63_experiment_design_invalid")
    add("design", "frozen_experiment_authorization", authorization["authorized_at"], {
        "experiment_type": "controlled_synthetic_http_500_test_endpoint",
        "service": "order-service", "method": "GET", "route": "/error",
        "expected_http_status": 500,
        "expected_effects": list(experiment.EXPECTED_EFFECTS),
        "semantics": "experiment_design_not_deployed_source_or_business_failure_proof",
    }, ["authorization.json"])
    for row in faults:
        body_name = "experiment/traffic/" + row["raw_body_file"]
        if row["http_status"] != 500 or files[body_name] != b"Internal Server Error":
            raise ValueError("p63_unexpected_fault_response")
        add(row["id"], "captured_http_response", row["query_finished_at"], {
            "service": "order-service", "method": "GET", "route": "/error",
            "query_started_at": _stamp(row["query_started_at"]),
            "query_finished_at": _stamp(row["query_finished_at"]),
            "http_status": 500, "body_complete": row["body_complete"],
            "body": "Internal Server Error",
        }, ["experiment/traffic/" + row["id"] + ".json", body_name])
    version_values = []
    version_originals = []
    for row in versions:
        body_name = "experiment/traffic/" + row["raw_body_file"]
        version_values.append({
            "phase": row["phase"],
            "query_started_at": _stamp(row["query_started_at"]),
            "query_finished_at": _stamp(row["query_finished_at"]),
            "reported_version": _version(read(body_name)),
        })
        version_originals.extend([
            "experiment/traffic/" + row["id"] + ".json", body_name,
        ])
    if any(item["reported_version"] != version_values[0]["reported_version"]
           for item in version_values):
        raise ValueError("p63_version_changed_in_frozen_rounds")
    add("versions", "captured_version_checks", versions[-1]["query_finished_at"], {
        "count": len(versions), "checks": version_values,
        "identity_scope": "service_self_reported_process_start_and_flags",
    }, version_originals)

    preflight = read("preflight/preflight.json")
    baseline_record = preflight["http_requests"][1]
    if (
        baseline_record != read("preflight/http-002.json")
        or baseline_record.get("url") != "http://localhost:8001/metrics"
        or baseline_record.get("http_status") != 200
        or baseline_record.get("body_complete") is not True
        or baseline_record.get("raw_body_sha256")
        != _sha(files["preflight/http-002.body"])
        or baseline_record.get("captured_bytes")
        != len(files["preflight/http-002.body"])
        or evidence._utc(baseline_record["query_finished_at"])
        > evidence._utc(fault_window["start"])
    ):
        raise ValueError("p63_baseline_original_unbound")
    counters = []
    for line in files["preflight/http-002.body"].decode("utf-8").splitlines():
        if not line.startswith("demo_http_requests_total{"):
            continue
        match = re.fullmatch(
            r'demo_http_requests_total\{service="order-service",'
            r'status="([1-5][0-9]{2})"\} ([0-9]+(?:\.[0-9]+)?)',
            line,
        )
        if not match:
            raise ValueError("p63_unsupported_baseline_counter")
        counters.append({"status": match[1], "value": float(match[2])})
    if not counters or not all(math.isfinite(item["value"]) for item in counters):
        raise ValueError("p63_missing_finite_baseline_counter")
    add("counter-before", "captured_metrics_exposition",
        baseline_record["query_finished_at"], {
        "service": "order-service", "metric": "http_requests_total",
        "series": counters, "aggregate_value": sum(item["value"] for item in counters),
        "scope": "service_total_not_independent_500_counter",
        "query_started_at": _stamp(baseline_record["query_started_at"]),
    }, ["preflight/http-002.json", "preflight/http-002.body"])

    for key, envelope in collected["observations"].items():
        if envelope["tool"] != "query_metrics":
            continue
        result = envelope["result"]
        value = {
            "service": "order-service", "metric": envelope["arguments"]["metric"],
            "status": result["status"],
            "query_started_at": _stamp(envelope["query_started_at"]),
            "collected_at": _stamp(envelope["collected_at"]),
        }
        if result["status"] == "ok":
            value.update({
                "value": result["value"],
                "evaluated_at": _stamp(result["evaluated_at"]),
                "promql": result["promql"],
            })
        if key == "http_requests_total":
            value["scope"] = "service_total_not_independent_500_counter"
        identifier = envelope["request_ids"][0]
        add("metric-" + value["metric"], "query_metrics",
            envelope["collected_at"], value, [
            "observations/" + identifier + ".json",
            "observations/" + identifier + ".body",
        ], status=result["status"], missing=result["status"] == "no_data")

    logs = collected["observations"]["query_logs"]
    log_values = []
    for entry in collected["log_correlation"]["entries"]:
        if not re.fullmatch(
            r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ERROR "
            r"service=order-service status=500 request failed",
            entry["line"],
        ):
            raise ValueError("p63_log_outside_minimal_allowlist")
        log_values.append({
            "line": entry["line"],
            "loki_timestamp_utc": entry["loki_timestamp_utc"],
            "body_event_time_utc": entry["body_event_time_utc"],
            "body_event_time_precision": "seconds",
            "candidate_in_fault_window": entry["candidate_in_fault_window"],
        })
    add("logs", "query_logs", logs["collected_at"], {
        "service": "order-service", "status": logs["result"]["status"],
        "logql": '{service="order-service"}', "entries": log_values,
        "query_range": collected["loki_query_window"],
        "verified_body_utc_offset": collected["verified_log_utc_offset"],
        "association": "limited_time_content_count_only_not_per_request_identity",
        "route_identity_present": False, "shared_request_id_present": False,
    }, ["observations/http-008.json", "observations/http-008.body",
        "observations/collection_report.json"])

    snapshots = collected["prometheus_snapshots"]
    alerts = snapshots["alerts"]["result"]["data"]["alerts"]
    selected = [
        item for item in alerts
        if item["labels"].get("service") == "order-service"
    ]
    rules = collected["alert_summary"]["high_http_5xx_rules"]
    add("alerts", "captured_prometheus_alert_snapshot",
        snapshots["alerts"]["collected_at"], {
        "service": "order-service", "alertname": "HighHttp5xxRate",
        "complete_snapshot": collected["alert_summary"]["snapshot_complete"],
        "selected_alert_count": len(selected),
        "state_at_snapshot": collected["alert_summary"]["order_service_state"],
        "state_basis": collected["alert_summary"]["state_basis"],
        "non_case_firing_present": any(
            item["labels"].get("service") != "order-service"
            and item.get("state") == "firing" for item in alerts
        ),
        "rules_collected_at": _stamp(snapshots["rules"]["collected_at"]),
        "rules": [{
            "state": item["rule"]["state"], "health": item["rule"]["health"],
            "duration_seconds": item["rule"]["duration"],
            "last_evaluation": item["rule"]["lastEvaluation"],
            "scope": "rule_all_series_not_order_service",
        } for item in rules],
        "coverage": "single_late_snapshot_not_entire_fault_window",
    }, ["observations/http-001.json", "observations/http-001.body",
        "observations/http-002.json", "observations/http-002.body"])
    version = collected["observations"]["get_running_version"]
    add("running-version", "get_running_version", version["collected_at"], {
        **_version(read("observations/http-009.body")),
        "status": version["result"]["status"], "reason": "commit_unknown",
    }, ["observations/http-009.json", "observations/http-009.body"],
        status="no_data", missing=True)

    replay = read("runtime-replay/report.json")
    runtime = read("runtime-replay/runtime/report.json")
    if (
        replay["captured_observations"] != collected["observations"]
        or replay["full_real_alert_snapshot"] != snapshots["alerts"]["result"]
        or replay["fault_window"] != fault_window
        or replay["alert_input_origin"] != "synthetic_alert_input"
        or replay["model_origin"] != "scripted_mock_sdk_not_real_deepseek"
        or runtime["evidence_origin"]
        != "captured_observation_replay_and_scripted_mock_sdk"
        or replay["session"] != runtime["session"]
        or any(replay[key] != 0 for key in (
            "real_model_sdk_requests", "business_route_requests",
            "remediation_executions",
        ))
        or replay["business_recovery_verified"] is not False
        or replay["use_for_current_state"] is not False
    ):
        raise ValueError("p63_runtime_replay_binding_invalid")
    add("runtime", "historical_mock_runtime_replay", runtime["finished_at"], {
        "alert_input_origin": "synthetic_alert_input",
        "model_origin": "scripted_mock_sdk_not_real_deepseek",
        "session_status": replay["session"]["status"],
        "mock_model_rounds": runtime["mock_decision_rounds"],
        "tool_executions": replay["session"]["total_tool_executions"],
        "real_model_sdk_requests": 0, "new_business_requests": 0,
        "remediation_executions": 0, "business_recovery_verified": False,
        "scope": "persisted_isolated_mock_replay_only",
    }, ["runtime-replay/report.json", "runtime-replay/runtime/report.json"])
    fault_end = evidence._utc(fault_window["end"])
    return {
        "schema_version": 1, "scope": SCOPE,
        "capture_manifest_sha256": _sha(files["sha256.json"]),
        "reference_namespace": REFERENCE_NAMESPACE,
        "extraction_status_semantics": (
            "supported_means_this_case_projection_is_extractable_only; "
            "not_P5_tool_support_or_natural_language_semantic_acceptance"
        ),
        "evidence_index": {"schema_version": 1, "sources": sources},
        "fault_window": {**fault_window, "bounds": "[start, end)"},
        "collection_window": {
            "start": collected["started_at"], "end": collected["finished_at"],
            "first_http_started_at": collected["http_requests"][0]["query_started_at"],
            "entry_delay_after_last_fault_seconds": (
                evidence._utc(collected["started_at"]) - fault_end
            ).total_seconds(),
            "first_http_delay_after_last_fault_seconds": (
                evidence._utc(collected["http_requests"][0]["query_started_at"])
                - fault_end
            ).total_seconds(),
        },
        "known_gaps": [
            "No aligned in-fault-window metric series or continuous alert history.",
            "Service-aggregate counter is not an independent status-500 counter.",
            "Equal counter delta and fault count do not exclude concurrent traffic or prove exclusive attribution.",
            "Logs lack route and shared request identifiers.",
            "Running commit and deployed-source identity remain unknown.",
            "Frozen design documents route intent; deployed source is not captured.",
            "No /order business verification or recovery execution in this capture.",
            "No real model result exists in the historical Mock Runtime replay.",
            "Local hashes do not independently authenticate external observations.",
        ],
    }
