"""Opt-in, bounded /order traffic, separate from P6.1 evidence collection.

Without --execute this prints an offline plan and performs no I/O. An operator
must separately authorize the SELECT, counters, dependency state and log writes,
and verify local process ownership before supplying the expected /version identity.
No Agent, SDK, database client, monitor or remediation is started here.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import requests

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.p61_collect_observations import _utc_now, _write_json


BUSINESS_URL = "http://localhost:8001/order"
VERSION_URL = "http://localhost:8001/version"
MAX_REQUESTS = 30
MAX_SECONDS = 60
REQUEST_INTERVAL_SECONDS = 1
REQUEST_TIMEOUT = (2, 3)
TOTAL_REQUEST_SECONDS = 8
MAX_RESPONSE_BYTES = 262144
MAX_AUTHORIZATION_BYTES = 65536


def traffic_plan():
    return {
        "mode": "offline_plan_only",
        "method": "GET",
        "business_url": BUSINESS_URL,
        "max_business_requests": MAX_REQUESTS,
        "max_seconds": MAX_SECONDS,
        "serial_interval_seconds": REQUEST_INTERVAL_SECONDS,
        "timeout_connect_read_seconds": list(REQUEST_TIMEOUT),
        "authorization_required": True,
        "business_route_requests": 0,
        "side_effects": [
            "PostgreSQL SELECT and connection",
            "request counter and latency histogram updates",
            "dependency status gauge update and append-only application logging",
        ],
        "deadline_semantics": (
            "Live execution must use the CLI supervisor, which kills its own child "
            "at the overall wall-clock deadline, including child startup. Internal "
            "HTTP checks are cooperative; cleanup/report writing can finish later."
        ),
    }


def _validate_target(method, url):
    if method != "GET" or url not in (BUSINESS_URL, VERSION_URL):
        raise ValueError("p62a_target_or_method_forbidden")


def _validate_identity(identity):
    if not isinstance(identity, dict):
        raise ValueError("p62a_verified_identity_required")
    if type(identity.get("verified_process_id")) is not int:
        raise ValueError("p62a_verified_process_id_required")
    if identity["verified_process_id"] <= 0:
        raise ValueError("p62a_verified_process_id_required")
    ownership_note = identity.get("ownership_note")
    if not isinstance(ownership_note, str) or not ownership_note.strip():
        raise ValueError("p62a_ownership_note_required")
    stamp = identity.get("process_started_at")
    try:
        started = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("p62a_verified_start_time_required") from None
    if started.tzinfo is None:
        raise ValueError("p62a_verified_start_time_requires_timezone")
    if "commit_sha" not in identity or (
        identity["commit_sha"] is not None
        and (
            not isinstance(identity["commit_sha"], str)
            or not re.fullmatch(r"[0-9a-f]{40}", identity["commit_sha"])
        )
    ):
        raise ValueError("p62a_expected_full_commit_sha_or_explicit_unknown_required")


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
    """Retain observed bytes, including partial bodies; never retry or redirect."""
    _validate_target("GET", url)
    began = clock()
    if began >= deadline:
        raise requests.Timeout("p62a_total_deadline_before_request")
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
        "body_complete": False,
    }
    records.append(record)
    body = bytearray()
    response = None
    request_deadline = min(deadline, began + TOTAL_REQUEST_SECONDS)
    remaining = request_deadline - began
    try:
        # A fresh clean session is still cleaned before each call: response cookies
        # must never become credentials for the next business/version request.
        session.trust_env = False
        session.auth = None
        session.cookies.clear()
        session.proxies.clear()
        session.headers.clear()
        session.hooks = {"response": []}
        record["request_executed"] = True
        response = session.request(
            "GET",
            url,
            timeout=tuple(min(value, remaining) for value in REQUEST_TIMEOUT),
            allow_redirects=False,
            stream=True,
            proxies={},
            headers={"Accept-Encoding": "identity"},
        )
        record["http_status"] = response.status_code
        record["response_url"] = response.url
        record["response_content_type"] = response.headers.get("Content-Type")
        if response.url != url:
            raise ValueError("p62a_response_source_changed")
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ValueError("p62a_compressed_response_refused")
        length = response.headers.get("Content-Length")
        if length is not None and (
            not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES
        ):
            raise ValueError("p62a_invalid_or_oversized_response")
        # Single-byte reads also check an active trickle instead of waiting for a
        # whole buffered chunk. A blocked read can exceed the cooperative deadline.
        for chunk in response.iter_content(chunk_size=1):
            if clock() >= request_deadline:
                raise requests.Timeout("p62a_total_request_deadline")
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                raise ValueError("p62a_oversized_response")
            body.extend(chunk)
        if clock() >= request_deadline:
            raise requests.Timeout("p62a_total_request_deadline")
        record["body_complete"] = True
        if response.status_code != 200:
            raise requests.HTTPError("p62a_non_200_stop", response=response)
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            raise ValueError("p62a_json_object_required")
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
        _write_json(root / (identifier + ".json"), record)


def _check_version(value, identity):
    flags = value.get("runtime_flags")
    if (
        value.get("service") != "order-service"
        or "commit_sha" not in value
        or value.get("commit_sha") != identity["commit_sha"]
        or value.get("process_started_at") != identity["process_started_at"]
        or not isinstance(flags, dict)
        or flags.get("simulate_code_regression") is not False
        or flags.get("simulate_slow_request") is not False
    ):
        raise ValueError("p62a_process_identity_or_fault_flag_changed")


def run_business_baseline(
    output_dir,
    *,
    authorized=False,
    authorization_note="",
    verified_identity=None,
    count=MAX_REQUESTS,
    max_seconds=MAX_SECONDS,
    method="GET",
    url=BUSINESS_URL,
    clock=time.monotonic,
    sleep=time.sleep,
    _supervisor_deadline=None,
):
    """Internal child/trusted Mock API; live execution must use supervised CLI.

    verified_identity is an operator attestation from external process/port checks;
    the HTTP identity check cannot prove PID ownership or eliminate the precheck race.
    This function's cooperative HTTP deadline alone is not a hard wall-clock bound.
    """
    _validate_run_arguments(
        authorized=authorized,
        authorization_note=authorization_note,
        verified_identity=verified_identity,
        count=count,
        max_seconds=max_seconds,
        method=method,
        url=url,
    )
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    started = clock()
    deadline = started + max_seconds
    if _supervisor_deadline is not None:
        deadline = min(deadline, _supervisor_deadline)
    records = []
    report = {
        "mode": "explicitly_authorized_direct_business_traffic",
        "started_at": _utc_now(),
        "authorization_note": authorization_note,
        "verified_identity": verified_identity,
        "running_version": verified_identity["commit_sha"] or "unknown",
        "code_causal_claims_verified": False,
        "requested_count": count,
        "max_seconds": max_seconds,
        "business_route_requests": 0,
        "successful_business_responses": 0,
        "status": "stopped",
        "business_state": "unknown",
        "business_recovery_verified": False,
        "real_model_sdk_requests": 0,
        "agent_tool_calls": 0,
        "remediation_executions": 0,
        "request_executed_semantics": (
            "Entered local Session.request; does not prove server receipt."
        ),
        "limitations": [
            "Internal HTTP deadline is cooperative; live use requires CLI supervisor.",
            "Process ownership is externally attested; /version has a precheck race.",
            "Direct business HTTP is separate from Agent collection and replay.",
            "HTTP 200 alone does not establish business health or recovery.",
            "Unknown runtime SHA cannot establish a code/version causal link.",
            "Raw local bodies may contain database rows; summaries omit row values.",
        ],
    }
    _write_json(root / "authorization.json", {
        "authorization_note": authorization_note,
        "verified_identity": verified_identity,
        "plan": traffic_plan(),
        "requested_count": count,
        "max_seconds": max_seconds,
    })
    try:
        with requests.Session() as session:
            # Disable retries explicitly, including connect retries.
            session.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))
            for number in range(1, count + 1):
                if number > 1:
                    remaining = deadline - clock()
                    if remaining <= REQUEST_INTERVAL_SECONDS:
                        raise requests.Timeout("p62a_traffic_duration_limit")
                    sleep(REQUEST_INTERVAL_SECONDS)
                before = _get(
                    session, root, records,
                    url=VERSION_URL, phase="identity_before", request_number=number,
                    deadline=deadline, clock=clock,
                )
                _check_version(before, verified_identity)
                value = _get(
                    session, root, records,
                    url=BUSINESS_URL, phase="business", request_number=number,
                    deadline=deadline, clock=clock,
                )
                if (
                    value.get("service") != "order-service"
                    or value.get("dependency") != "order-db"
                    or "order" not in value
                    or (
                        value["order"] is not None
                        and not isinstance(value["order"], dict)
                    )
                ):
                    raise ValueError("p62a_unexpected_business_response_structure")
                _write_json(root / f"business-{number:03d}-summary.json", {
                    "request_number": number,
                    "source_request_id": records[-1]["id"],
                    "service": "order-service",
                    "dependency": "order-db",
                    "order_value_type": type(value["order"]).__name__,
                    "row_values_included": False,
                })
                report["successful_business_responses"] += 1
                after = _get(
                    session, root, records,
                    url=VERSION_URL, phase="identity_after", request_number=number,
                    deadline=deadline, clock=clock,
                )
                _check_version(after, verified_identity)
            report["status"] = "traffic_completed_observations_pending"
    except (requests.RequestException, ValueError) as exc:
        report["stop_error_type"] = type(exc).__name__
        # Fixed local validation messages are safe; provider exception text is not.
        if isinstance(exc, ValueError) and str(exc).startswith("p62a_"):
            report["stop_reason"] = str(exc)
    finally:
        report["business_route_requests"] = sum(
            row["phase"] == "business" and row["request_executed"]
            for row in records
        )
        report["finished_at"] = _utc_now()
        report["elapsed_seconds"] = max(0, clock() - started)
        report["http_requests"] = records
        _write_json(root / "traffic_report.json", report)
        _write_json(root / "sha256.json", {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.iterdir())
        })
    return report


def _validate_run_arguments(
    *,
    authorized=False,
    authorization_note="",
    verified_identity=None,
    count=MAX_REQUESTS,
    max_seconds=MAX_SECONDS,
    method="GET",
    url=BUSINESS_URL,
):
    if authorized is not True or not authorization_note.strip():
        raise PermissionError("p62a_separate_business_traffic_authorization_required")
    _validate_target(method, url)
    if url != BUSINESS_URL:
        raise ValueError("p62a_business_url_required")
    _validate_identity(verified_identity)
    if type(count) is not int or not 1 <= count <= MAX_REQUESTS:
        raise ValueError("p62a_request_count_out_of_bounds")
    if type(max_seconds) not in (int, float) or not 0 < max_seconds <= MAX_SECONDS:
        raise ValueError("p62a_duration_out_of_bounds")


def _verified_child_report(root, count):
    manifest = json.loads((root / "sha256.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or "traffic_report.json" not in manifest:
        raise ValueError("p62a_child_manifest_missing")
    for name, digest in manifest.items():
        if Path(name).name != name or hashlib.sha256(
            (root / name).read_bytes()
        ).hexdigest() != digest:
            raise ValueError("p62a_child_manifest_mismatch")
    value = json.loads((root / "traffic_report.json").read_text(encoding="utf-8"))
    observed = sum(
        row.get("phase") == "business" and row.get("request_executed") is True
        for row in value["http_requests"]
    )
    if (
        type(value.get("business_route_requests")) is not int
        or value["business_route_requests"] != observed
        or not 0 <= observed <= count
        or value.get("requested_count") != count
        or value.get("mode") != "explicitly_authorized_direct_business_traffic"
        or value.get("status") not in (
            "stopped", "traffic_completed_observations_pending",
        )
        or (
            value.get("status") == "traffic_completed_observations_pending"
            and (
                observed != count
                or value.get("successful_business_responses") != count
            )
        )
    ):
        raise ValueError("p62a_child_report_invalid")
    return value


def run_supervised_baseline(output_dir, *, clock=time.monotonic, **arguments):
    """Own exactly one helper; only this path permits live CLI traffic."""
    _validate_run_arguments(**arguments)
    return _run_owned_child_supervisor(
        output_dir,
        arguments=arguments,
        child_script=Path(__file__).resolve(),
        mode="p62a_owned_child_supervisor",
        verify_report=_verified_child_report,
        count_fields={"business_route_requests": "business_request_count_status"},
        plan=traffic_plan(),
        clock=clock,
    )


def _run_owned_child_supervisor(
    output_dir,
    *,
    arguments,
    child_script,
    mode,
    verify_report,
    count_fields,
    plan,
    clock=time.monotonic,
):
    """Shared lifecycle for one owned helper; callers validate before entering."""
    root = Path(output_dir).resolve()
    count = arguments.get("count", MAX_REQUESTS)
    max_seconds = arguments.get("max_seconds", MAX_SECONDS)
    started = clock()
    deadline = started + max_seconds
    payload = {
        "output_dir": str(root / "traffic"),
        "supervisor_pid": os.getpid(),
        "parent_deadline_monotonic": deadline,
        "arguments": arguments,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, indent=2, allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_AUTHORIZATION_BYTES:
        raise ValueError("p62a_authorization_payload_size_limit")
    root.mkdir(parents=True, exist_ok=False)
    authorization_path = root / "authorization.json"
    with authorization_path.open("xb") as handle:
        handle.write(encoded)
    record = {
        "mode": mode,
        "started_at": _utc_now(),
        "requested_count": count,
        "max_seconds": max_seconds,
        "authorization_file": authorization_path.name,
        "authorization_sha256": hashlib.sha256(encoded).hexdigest(),
        "status": "child_failed",
        "originals_may_be_incomplete": True,
        "child_pid": None,
        "child_exit_code": None,
        "deadline_exceeded": False,
        "child_killed_by_supervisor": False,
        "business_state": "unknown",
        "business_recovery_verified": False,
        "real_model_sdk_requests": 0,
        "limitations": [
            plan["deadline_semantics"],
            "Killing the client cannot revoke a request already received by the server.",
            "Timeout/failure may interrupt raw evidence writes; count remains unknown.",
        ],
    }
    for field, status_field in count_fields.items():
        record[field] = None
        record[status_field] = "unknown"
    child = None
    try:
        child = subprocess.Popen(
            [
                sys.executable, str(child_script),
                "--_authorized-child", str(authorization_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        record["child_pid"] = child.pid
        remaining = deadline - clock()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(child.args, max_seconds)
        child.wait(timeout=remaining)
        record["child_exit_code"] = child.returncode
        if clock() >= deadline:
            raise subprocess.TimeoutExpired(child.args, max_seconds)
        if child.returncode == 0:
            report = verify_report(root / "traffic", count)
            record.update(
                status=(
                    "traffic_completed_observations_pending"
                    if report["status"] == "traffic_completed_observations_pending"
                    else "child_stopped"
                ),
                originals_may_be_incomplete=False,
                child_report="traffic/traffic_report.json",
            )
            for field, status_field in count_fields.items():
                record[field] = report[field]
                record[status_field] = "verified_child_report"
    except subprocess.TimeoutExpired:
        record["status"] = "deadline_exceeded"
        record["deadline_exceeded"] = True
    except (OSError, ValueError, TypeError, KeyError) as exc:
        record["error_type"] = type(exc).__name__
    finally:
        if child is not None and child.poll() is None:
            # Only the newly created Popen object is signalled, never a PID search.
            child.kill()
            record["child_killed_by_supervisor"] = True
            child.wait()
        if child is not None:
            record["child_exit_code"] = child.returncode
        record["finished_at"] = _utc_now()
        record["elapsed_seconds_including_cleanup"] = max(0, clock() - started)
        _write_json(root / "supervisor.json", record)
        _write_json(root / "sha256.json", {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()
        })
    return record


def _read_child_authorization(authorization_path):
    path = Path(authorization_path)
    if path.name != "authorization.json" or path.is_symlink():
        raise ValueError("p62a_invalid_child_authorization_path")
    with path.open("rb") as handle:
        encoded = handle.read(MAX_AUTHORIZATION_BYTES + 1)
    if len(encoded) > MAX_AUTHORIZATION_BYTES:
        raise ValueError("p62a_authorization_payload_size_limit")
    payload = json.loads(encoded)
    if payload.get("supervisor_pid") != os.getppid():
        raise PermissionError("p62a_child_requires_own_supervisor")
    if Path(payload["output_dir"]).resolve() != path.resolve().parent / "traffic":
        raise ValueError("p62a_child_output_path_mismatch")
    return payload


def _child_main(authorization_path):
    payload = _read_child_authorization(authorization_path)
    arguments = payload["arguments"]
    if "parent_deadline_monotonic" in payload:
        arguments = {
            **arguments,
            "_supervisor_deadline": payload["parent_deadline_monotonic"],
        }
    run_business_baseline(payload["output_dir"], **arguments)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--_authorized-child", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--authorization-note", default="")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--verified-process-id", type=int)
    parser.add_argument("--verified-process-started-at")
    parser.add_argument("--verified-commit-sha")
    parser.add_argument("--ownership-note", default="")
    parser.add_argument("--count", type=int, default=MAX_REQUESTS)
    parser.add_argument("--max-seconds", type=float, default=MAX_SECONDS)
    arguments = parser.parse_args()
    if arguments._authorized_child:
        if len(sys.argv) != 3:
            parser.error("private child accepts only its supervisor authorization file")
        _child_main(arguments._authorized_child)
        return
    if not arguments.execute:
        print(json.dumps(traffic_plan(), ensure_ascii=False, indent=2))
        return
    if arguments.output_dir is None:
        parser.error("--execute requires a new --output-dir")
    if arguments.verified_commit_sha is None:
        parser.error("supply --verified-commit-sha FULL_SHA or explicit unknown")
    report = run_supervised_baseline(
        arguments.output_dir,
        authorized=True,
        authorization_note=arguments.authorization_note,
        verified_identity={
            "verified_process_id": arguments.verified_process_id,
            "process_started_at": arguments.verified_process_started_at,
            "commit_sha": (
                None if arguments.verified_commit_sha == "unknown"
                else arguments.verified_commit_sha
            ),
            "ownership_note": arguments.ownership_note,
        },
        count=arguments.count,
        max_seconds=arguments.max_seconds,
    )
    print(json.dumps({
        "report": str(arguments.output_dir.resolve() / "supervisor.json"),
        "status": report["status"],
        "business_route_requests": report["business_route_requests"],
    }, ensure_ascii=False))
    if report["status"] != "traffic_completed_observations_pending":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
