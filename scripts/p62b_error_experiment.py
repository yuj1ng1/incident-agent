"""Offline by default, one-shot supervised GET /error experiment.

HTTP 500 is expected only for the fixed local test route. The live CLI requires
a fresh, structured authorization file; it never starts an Agent or model SDK.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sys
import time

import requests

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import p62a_business_baseline as baseline
from scripts.p61_collect_observations import _utc_now, _write_json


ERROR_URL = "http://localhost:8001/error"
VERSION_URL = baseline.VERSION_URL
MAX_REQUESTS = 30
MAX_SECONDS = 60
REQUEST_TIMEOUT = baseline.REQUEST_TIMEOUT
TOTAL_REQUEST_SECONDS = baseline.TOTAL_REQUEST_SECONDS
MAX_RESPONSE_BYTES = baseline.MAX_RESPONSE_BYTES
MAX_AUTHORIZATION_BYTES = baseline.MAX_AUTHORIZATION_BYTES
AUTHORIZATION_CONSUMPTION_DIR = (
    Path(__file__).resolve().parents[1] / "data" / "p62b-error-authorizations"
)
FINALIZATION_RESERVE_SECONDS = 2
REQUEST_INTERVAL_SECONDS = 1
PHASE_URLS = {
    "identity_before": VERSION_URL,
    "fault": ERROR_URL,
    "identity_after": VERSION_URL,
}
STOP_CONDITIONS = (
    "unexpected_http_status", "timeout", "identity_or_flags_changed",
    "target_mismatch", "request_or_duration_limit",
)
POSTCHECKS = (
    "stop_fault_traffic", "owned_helper_exited", "demo_identity_unchanged",
    "readonly_prometheus_loki", "no_order_requests", "no_recovery_claim",
)
EXPECTED_EFFECTS = (
    "http_500", "order_service_error_logs", "request_counter_update",
    "latency_histogram_update",
)


def experiment_plan():
    return {
        "mode": "offline_plan_only",
        "method": "GET",
        "business_url": ERROR_URL,
        "version_url": VERSION_URL,
        "service": "order-service",
        "port": 8001,
        "expected_http_status": 500,
        "max_business_requests": MAX_REQUESTS,
        "max_seconds": MAX_SECONDS,
        "business_route_requests": 0,
        "identity_verification_requests": 0,
        "authorization_required": True,
        "one_shot_authorization": True,
        "identity_checks": "version_before_and_after_every_fault_request",
        "side_effects": list(EXPECTED_EFFECTS),
        "stop_conditions": list(STOP_CONDITIONS),
        "post_experiment_checks": list(POSTCHECKS),
        "round_reserve_seconds": 3 * TOTAL_REQUEST_SECONDS,
        "finalization_reserve_seconds": FINALIZATION_RESERVE_SECONDS,
        "real_model_sdk_requests": 0,
        "remediation_executions": 0,
        "deadline_semantics": (
            "The existing P6.2a supervisor owns one helper and enforces the parent "
            "absolute monotonic deadline, including startup. Each full round keeps "
            "both identity checks and reserves report time; interrupted totals stay "
            "unknown. Stopping traffic does not recover the fixed-500 route."
        ),
    }


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("p62b_timezone_timestamp_required") from None
    if parsed.tzinfo is None:
        raise ValueError("p62b_timezone_timestamp_required")
    return parsed


def _validate_target(method, url):
    if method != "GET" or url not in (ERROR_URL, VERSION_URL):
        raise ValueError("p62b_target_or_method_forbidden")


def _validate_authorization(authorization, *, count, max_seconds):
    if not isinstance(authorization, dict) or (
        authorization.get("explicitly_authorized") is not True
    ):
        raise PermissionError("p62b_new_explicit_fault_authorization_required")
    if (
        authorization.get("schema_version") != 1
        or authorization.get("purpose") != "p62b_error_experiment"
        or authorization.get("one_shot") is not True
        or not isinstance(authorization.get("authorization_note"), str)
        or not authorization["authorization_note"].strip()
        or not isinstance(authorization.get("run_id"), str)
        or not authorization["run_id"].strip()
    ):
        raise PermissionError("p62b_specific_fault_authorization_required")
    _timestamp(authorization.get("authorized_at"))
    identity = authorization.get("verified_identity")
    baseline._validate_identity(identity)
    if authorization.get("target") != {
        "service": "order-service", "method": "GET", "url": ERROR_URL,
        "port": 8001, "process_id": identity["verified_process_id"],
    }:
        raise ValueError("p62b_authorized_target_mismatch")
    if (
        authorization.get("expected_http_status") != 500
        or set(authorization.get("expected_effects", [])) != set(EXPECTED_EFFECTS)
        or set(authorization.get("stop_conditions", [])) != set(STOP_CONDITIONS)
        or set(authorization.get("post_experiment_checks", [])) != set(POSTCHECKS)
    ):
        raise PermissionError("p62b_effects_stop_and_postcheck_authorization_required")
    if (
        type(count) is not int or not 1 <= count <= MAX_REQUESTS
        or type(max_seconds) not in (int, float)
        or not 0 < max_seconds <= MAX_SECONDS
        or authorization.get("max_fault_requests") != count
        or authorization.get("max_seconds") != max_seconds
    ):
        raise ValueError("p62b_authorized_budget_mismatch")
    preflight = authorization.get("preflight")
    if not isinstance(preflight, dict) or any(
        preflight.get(name) is not True for name in (
            "isolated_ownership_verified", "port_process_verified",
            "runtime_flags_verified", "concurrent_tests_absent",
        )
    ):
        raise PermissionError("p62b_isolated_environment_preflight_required")
    if _timestamp(preflight.get("checked_at")) > _timestamp(
        authorization["authorized_at"]
    ):
        raise ValueError("p62b_preflight_must_precede_authorization")


def _validate_run_arguments(
    *,
    authorization=None,
    count=MAX_REQUESTS,
    max_seconds=MAX_SECONDS,
    method="GET",
    url=ERROR_URL,
):
    _validate_target(method, url)
    if url != ERROR_URL:
        raise ValueError("p62b_error_url_required")
    _validate_authorization(authorization, count=count, max_seconds=max_seconds)


def _get(
    session,
    root,
    records,
    *,
    url,
    phase,
    request_number,
    deadline,
    clock,
):
    """Archive raw bytes for every locally entered request, without retry."""
    _validate_target("GET", url)
    if PHASE_URLS.get(phase) != url:
        raise ValueError("p62b_phase_target_mismatch")
    began = clock()
    if began >= deadline:
        raise requests.Timeout("p62b_total_deadline_before_request")
    identifier = f"http-{len(records) + 1:03d}"
    record = {
        "id": identifier,
        "request_number": request_number,
        "phase": phase,
        "method": "GET",
        "url": url,
        "query_started_at": _utc_now(),
        "request_executed": False,
        "http_status": None,
        "expected_http_status": 500 if phase == "fault" else 200,
        "body_complete": False,
    }
    records.append(record)
    body = bytearray()
    response = None
    request_deadline = min(deadline, began + TOTAL_REQUEST_SECONDS)
    remaining = request_deadline - began
    try:
        session.trust_env = False
        session.auth = None
        session.cookies.clear()
        session.proxies.clear()
        session.headers.clear()
        session.hooks = {"response": []}
        record["request_executed"] = True
        response = session.request(
            "GET", url,
            timeout=tuple(min(value, remaining) for value in REQUEST_TIMEOUT),
            allow_redirects=False,
            stream=True,
            proxies={},
            headers={"Accept-Encoding": "identity"},
        )
        record["http_status"] = response.status_code
        record["response_url"] = response.url
        record["response_content_type"] = response.headers.get("Content-Type")
        record["response_headers"] = {
            name: response.headers[name]
            for name in (
                "Content-Type", "Content-Length", "Content-Encoding",
                "Connection", "Location", "Date",
            )
            if name in response.headers
        }
        if response.url != url:
            raise ValueError("p62b_response_source_changed")
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ValueError("p62b_compressed_response_refused")
        length = response.headers.get("Content-Length")
        if length is not None and (
            not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES
        ):
            raise ValueError("p62b_invalid_or_oversized_response")
        for chunk in response.iter_content(chunk_size=1):
            if clock() >= request_deadline:
                raise requests.Timeout("p62b_total_request_deadline")
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                raise ValueError("p62b_oversized_response")
            body.extend(chunk)
        if clock() >= request_deadline:
            raise requests.Timeout("p62b_total_request_deadline")
        record["body_complete"] = True
        if response.status_code != record["expected_http_status"]:
            raise requests.HTTPError("p62b_unexpected_http_status_stop", response=response)
        if phase == "fault":
            return None
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            raise ValueError("p62b_version_json_object_required")
        return parsed
    except (requests.RequestException, ValueError) as exc:
        record["error_type"] = type(exc).__name__
        raise
    finally:
        if response is not None:
            response.close()
        record["query_finished_at"] = _utc_now()
        record["elapsed_seconds"] = max(0, clock() - began)
        raw_name = identifier + ".body"
        with (root / raw_name).open("xb") as handle:
            handle.write(body)
        record["raw_body_file"] = raw_name
        record["raw_body_sha256"] = hashlib.sha256(body).hexdigest()
        record["captured_bytes"] = len(body)
        with (root / (identifier + ".json")).open("x", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2, allow_nan=False)


def run_error_experiment(
    output_dir,
    *,
    authorization=None,
    count=MAX_REQUESTS,
    max_seconds=MAX_SECONDS,
    method="GET",
    url=ERROR_URL,
    clock=time.monotonic,
    sleep=time.sleep,
    _supervisor_deadline=None,
):
    """Internal child / trusted Mock entry; live traffic uses the supervisor."""
    _validate_run_arguments(
        authorization=authorization, count=count, max_seconds=max_seconds,
        method=method, url=url,
    )
    if _supervisor_deadline is not None and (
        type(_supervisor_deadline) not in (int, float)
        or not math.isfinite(_supervisor_deadline)
    ):
        raise ValueError("p62b_invalid_parent_deadline")
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    started = clock()
    deadline = min(started + max_seconds, _supervisor_deadline) if (
        _supervisor_deadline is not None
    ) else started + max_seconds
    traffic_deadline = deadline - FINALIZATION_RESERVE_SECONDS
    identity = authorization["verified_identity"]
    records = []
    report = {
        "mode": "explicitly_authorized_direct_error_traffic",
        "started_at": _utc_now(),
        "authorization": authorization,
        "verified_identity": identity,
        "running_version": identity["commit_sha"] or "unknown",
        "requested_count": count,
        "max_seconds": max_seconds,
        "business_route_requests": 0,
        "identity_verification_requests": 0,
        "expected_500_responses": 0,
        "completed_verified_fault_cycles": 0,
        "status": "stopped",
        "deadline_exceeded": False,
        "business_state": "unknown",
        "business_recovery_verified": False,
        "real_model_sdk_requests": 0,
        "agent_tool_calls": 0,
        "remediation_executions": 0,
        "request_executed_semantics": (
            "Entered local Session.request; does not prove server receipt."
        ),
        "limitations": [
            "Expected 500 does not establish a Prometheus firing alert.",
            "Process ownership is externally attested; /version has a precheck race.",
            "No shared request_id: log association is limited to time/content/count.",
            "Stopping fault traffic does not recover the fixed-500 route.",
            "No /order business validation or real model inference is performed.",
        ],
    }
    _write_json(root / "authorization.json", authorization)
    try:
        with requests.Session() as session:
            session.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))
            for number in range(1, count + 1):
                interval = REQUEST_INTERVAL_SECONDS if number > 1 else 0
                # Reserve a complete before/fault/after round and report time.
                if traffic_deadline - clock() <= 3 * TOTAL_REQUEST_SECONDS + interval:
                    report["stop_reason"] = "p62b_insufficient_full_round_budget"
                    break
                if interval:
                    sleep(interval)
                before = _get(
                    session, root, records,
                    url=VERSION_URL, phase="identity_before", request_number=number,
                    deadline=traffic_deadline, clock=clock,
                )
                baseline._check_version(before, identity)
                _get(
                    session, root, records,
                    url=ERROR_URL, phase="fault", request_number=number,
                    deadline=traffic_deadline, clock=clock,
                )
                report["expected_500_responses"] += 1
                after = _get(
                    session, root, records,
                    url=VERSION_URL, phase="identity_after", request_number=number,
                    deadline=traffic_deadline, clock=clock,
                )
                baseline._check_version(after, identity)
                report["completed_verified_fault_cycles"] += 1
            else:
                report["status"] = "traffic_completed_observations_pending"
    except (requests.RequestException, ValueError) as exc:
        report["stop_error_type"] = type(exc).__name__
        if isinstance(exc, ValueError) and str(exc).startswith(("p62a_", "p62b_")):
            report["stop_reason"] = str(exc)
    finally:
        report["deadline_exceeded"] = clock() >= traffic_deadline
        counts = {
            "business_route_requests": sum(
                row["phase"] == "fault" and row["request_executed"]
                for row in records
            ),
            "identity_verification_requests": sum(
                row["phase"] != "fault" and row["request_executed"]
                for row in records
            ),
        }
        if report["deadline_exceeded"]:
            report["status"] = "deadline_exceeded"
            report.update({name: None for name in counts})
            report["request_count_status"] = "unknown"
        else:
            report.update(counts)
            report["request_count_status"] = "final_child_report"
        report["finished_at"] = _utc_now()
        report["elapsed_seconds"] = max(0, clock() - started)
        report["http_requests"] = records
        _write_json(root / "traffic_report.json", report)
        _write_json(root / "sha256.json", {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.iterdir())
        })
    return report


def _verified_child_report(root, count):
    manifest = json.loads((root / "sha256.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or "traffic_report.json" not in manifest:
        raise ValueError("p62b_child_manifest_missing")
    actual = {path.name for path in root.iterdir() if path.name != "sha256.json"}
    if set(manifest) != actual:
        raise ValueError("p62b_child_manifest_file_set_mismatch")
    for name, digest in manifest.items():
        path = root / name
        if Path(name).name != name or path.is_symlink() or (
            hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise ValueError("p62b_child_manifest_mismatch")
    value = json.loads((root / "traffic_report.json").read_text(encoding="utf-8"))
    if (
        value.get("mode") != "explicitly_authorized_direct_error_traffic"
        or value.get("requested_count") != count
        or value.get("deadline_exceeded") is not False
        or value.get("request_count_status") != "final_child_report"
        or value.get("status") not in (
            "stopped", "traffic_completed_observations_pending",
        )
    ):
        raise ValueError("p62b_child_report_invalid")
    _validate_authorization(
        value["authorization"], count=count, max_seconds=value["max_seconds"],
    )
    rows = value["http_requests"]
    if not isinstance(rows, list) or len(rows) > 3 * count:
        raise ValueError("p62b_child_request_records_invalid")
    fault_count = identity_count = expected_500 = 0
    expected_phases = tuple(PHASE_URLS)
    prior_finish = _timestamp(value["started_at"])
    for index, row in enumerate(rows):
        identifier = f"http-{index + 1:03d}"
        phase = expected_phases[index % 3]
        if (
            row.get("id") != identifier or row.get("phase") != phase
            or row.get("request_number") != index // 3 + 1
            or row.get("method") != "GET" or row.get("url") != PHASE_URLS[phase]
            or row.get("request_executed") is not True
            or row.get("raw_body_file") != identifier + ".body"
            or row.get("expected_http_status") != (500 if phase == "fault" else 200)
            or json.loads((root / (identifier + ".json")).read_text("utf-8")) != row
        ):
            raise ValueError("p62b_child_request_records_invalid")
        body = (root / row["raw_body_file"]).read_bytes()
        began, finished = _timestamp(row["query_started_at"]), _timestamp(
            row["query_finished_at"]
        )
        if (
            row["raw_body_sha256"] != hashlib.sha256(body).hexdigest()
            or row["captured_bytes"] != len(body)
            or not prior_finish <= began <= finished
            or ("response_url" in row and row["response_url"] != row["url"])
            or ("error_type" in row and index != len(rows) - 1)
        ):
            raise ValueError("p62b_child_original_integrity_invalid")
        prior_finish = finished
        fault_count += phase == "fault"
        identity_count += phase != "fault"
        expected_500 += (
            phase == "fault" and row.get("http_status") == 500
            and row.get("body_complete") is True and "error_type" not in row
        )
    if (
        type(value.get("business_route_requests")) is not int
        or type(value.get("identity_verification_requests")) is not int
        or value["business_route_requests"] != fault_count
        or value["identity_verification_requests"] != identity_count
        or value.get("expected_500_responses") != expected_500
        or not prior_finish <= _timestamp(value["finished_at"])
        or value.get("real_model_sdk_requests") != 0
        or value.get("remediation_executions") != 0
        or (
            value["status"] == "traffic_completed_observations_pending"
            and (
                len(rows) != 3 * count or expected_500 != count
                or value.get("completed_verified_fault_cycles") != count
            )
        )
    ):
        raise ValueError("p62b_child_count_or_time_invalid")
    return value


def run_supervised_error_experiment(
    output_dir,
    *,
    authorization_file,
    count=MAX_REQUESTS,
    max_seconds=MAX_SECONDS,
    clock=time.monotonic,
):
    """Consume one new explicit authorization and reuse P6.2a supervision."""
    original_path = Path(authorization_file)
    if original_path.is_symlink():
        raise ValueError("p62b_authorization_symlink_refused")
    path = original_path.resolve()
    with path.open("rb") as handle:
        encoded = handle.read(MAX_AUTHORIZATION_BYTES + 1)
    if len(encoded) > MAX_AUTHORIZATION_BYTES:
        raise ValueError("p62b_authorization_size_limit")
    authorization = json.loads(encoded)
    arguments = {
        "authorization": authorization, "count": count, "max_seconds": max_seconds,
    }
    _validate_run_arguments(**arguments)
    if Path(output_dir).exists():
        raise FileExistsError("p62b_new_output_directory_required")
    # A stable run identity prevents reuse after copying or renaming the file.
    # A failed attempt still consumes the authorization.
    AUTHORIZATION_CONSUMPTION_DIR.mkdir(parents=True, exist_ok=True)
    run_digest = hashlib.sha256(authorization["run_id"].encode("utf-8")).hexdigest()
    consumed = AUTHORIZATION_CONSUMPTION_DIR / (run_digest + ".consumed.json")
    with consumed.open("x", encoding="utf-8") as handle:
        json.dump({
            "run_id": authorization["run_id"],
            "authorization_sha256": hashlib.sha256(encoded).hexdigest(),
            "consumed_at": _utc_now(),
            "output_dir": str(Path(output_dir).resolve()),
        }, handle, indent=2)
    return baseline._run_owned_child_supervisor(
        output_dir,
        arguments=arguments,
        child_script=Path(__file__).resolve(),
        mode="p62b_owned_error_child_supervisor",
        verify_report=_verified_child_report,
        count_fields={
            "business_route_requests": "business_request_count_status",
            "identity_verification_requests": "identity_request_count_status",
            "expected_500_responses": "expected_500_count_status",
        },
        plan=experiment_plan(),
        clock=clock,
    )


def _child_main(authorization_path):
    payload = baseline._read_child_authorization(authorization_path)
    deadline = payload.get("parent_deadline_monotonic")
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("p62b_parent_deadline_required")
    run_error_experiment(
        payload["output_dir"], _supervisor_deadline=deadline, **payload["arguments"],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--_authorized-child", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--authorization-file", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--count", type=int, default=MAX_REQUESTS)
    parser.add_argument("--max-seconds", type=float, default=MAX_SECONDS)
    arguments = parser.parse_args()
    if arguments._authorized_child:
        if len(sys.argv) != 3:
            parser.error("private child accepts only its supervisor authorization file")
        _child_main(arguments._authorized_child)
        return
    if not arguments.execute:
        print(json.dumps(experiment_plan(), ensure_ascii=False, indent=2))
        return
    if arguments.authorization_file is None or arguments.output_dir is None:
        parser.error("--execute requires --authorization-file and a new --output-dir")
    report = run_supervised_error_experiment(
        arguments.output_dir, authorization_file=arguments.authorization_file,
        count=arguments.count, max_seconds=arguments.max_seconds,
    )
    print(json.dumps({
        "report": str(arguments.output_dir.resolve() / "supervisor.json"),
        "status": report["status"],
        "business_route_requests": report["business_route_requests"],
        "identity_verification_requests": report["identity_verification_requests"],
    }, ensure_ascii=False))
    if report["status"] not in (
        "traffic_completed_observations_pending", "child_stopped",
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
