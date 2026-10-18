"""Offline-only safety tests: all HTTP and helper processes are mocked."""
import hashlib
import io
import json
import subprocess
import sys

import pytest
import requests

from scripts import p62a_business_baseline as baseline
from scripts import p62b_error_experiment as experiment


IDENTITY = {
    "verified_process_id": 1234,
    "process_started_at": "2026-10-18T01:00:00+00:00",
    "commit_sha": "a" * 40,
    "ownership_note": "Mock-only isolated process ownership",
}
VERSION = {
    "service": "order-service",
    "process_started_at": IDENTITY["process_started_at"],
    "commit_sha": IDENTITY["commit_sha"],
    "runtime_flags": {
        "simulate_code_regression": False,
        "simulate_slow_request": False,
    },
}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def authorization(count=1, max_seconds=60):
    return {
        "schema_version": 1,
        "purpose": "p62b_error_experiment",
        "explicitly_authorized": True,
        "one_shot": True,
        "run_id": "mock-offline-only",
        "authorized_at": "2026-10-18T01:01:00+00:00",
        "authorization_note": "Mock authorization, no real requests",
        "verified_identity": IDENTITY.copy(),
        "target": {
            "service": "order-service", "method": "GET",
            "url": experiment.ERROR_URL, "port": 8001, "process_id": 1234,
        },
        "expected_http_status": 500,
        "expected_effects": list(experiment.EXPECTED_EFFECTS),
        "stop_conditions": list(experiment.STOP_CONDITIONS),
        "post_experiment_checks": list(experiment.POSTCHECKS),
        "max_fault_requests": count,
        "max_seconds": max_seconds,
        "preflight": {
            "isolated_ownership_verified": True,
            "port_process_verified": True,
            "runtime_flags_verified": True,
            "concurrent_tests_absent": True,
            "checked_at": "2026-10-18T01:00:59+00:00",
        },
    }


def response(url, *, status=None, value=None):
    result = requests.Response()
    result.status_code = status if status is not None else (
        500 if url == experiment.ERROR_URL else 200
    )
    result.url = url
    if value is None:
        value = b"Internal Server Error" if url == experiment.ERROR_URL else VERSION
    result.raw = io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())
    return result


@pytest.fixture(autouse=True)
def no_real_io(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("P6.2b offline tests must never send HTTP or launch real helpers")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(
        experiment, "AUTHORIZATION_CONSUMPTION_DIR", tmp_path / "consumed",
    )


def run(tmp_path, monkeypatch, sender=None, **changes):
    calls = []
    clock = changes.pop("clock", Clock())

    def send(session, method, url, **kwargs):
        assert session.get_adapter(url).max_retries.total == 0
        assert session.trust_env is False
        assert session.auth is None
        assert not session.cookies and not session.proxies
        assert not session.headers and session.hooks == {"response": []}
        assert method == "GET"
        assert kwargs["allow_redirects"] is False
        assert kwargs["proxies"] == {}
        assert kwargs["headers"] == {"Accept-Encoding": "identity"}
        assert kwargs["stream"] is True
        calls.append(url)
        return sender(url, len(calls)) if sender else response(url)

    monkeypatch.setattr(requests.sessions.Session, "request", send)
    count = changes.pop("count", 1)
    max_seconds = changes.pop("max_seconds", 60)
    report = experiment.run_error_experiment(
        tmp_path / "traffic", authorization=authorization(count, max_seconds),
        count=count, max_seconds=max_seconds, clock=clock, sleep=clock.sleep,
        **changes,
    )
    return report, calls, clock


def test_default_cli_offline_no_files_or_processes(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [experiment.__file__])
    experiment.main()
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "offline_plan_only"
    assert result["business_route_requests"] == 0
    assert list(tmp_path.iterdir()) == []


def test_missing_authorization_denied_before_output(tmp_path):
    with pytest.raises(PermissionError):
        experiment.run_error_experiment(tmp_path / "blocked")
    assert not (tmp_path / "blocked").exists()


@pytest.mark.parametrize("field,value", [
    ("explicitly_authorized", False), ("purpose", "p62a_order_baseline"),
    ("expected_http_status", 200), ("expected_effects", []),
    ("stop_conditions", []), ("post_experiment_checks", []),
    ("one_shot", False), ("preflight", {}),
])
def test_specific_authorization_cannot_be_replaced_by_order_approval(tmp_path, field, value):
    auth = authorization()
    auth[field] = value
    with pytest.raises((PermissionError, ValueError)):
        experiment.run_error_experiment(tmp_path / "blocked", authorization=auth, count=1)
    assert not (tmp_path / "blocked").exists()


@pytest.mark.parametrize("changes", [
    {"count": 31}, {"count": True}, {"max_seconds": 61},
    {"max_seconds": float("nan")}, {"max_seconds": float("inf")},
    {"method": "POST"}, {"url": "http://127.0.0.1:8001/error"},
    {"url": baseline.BUSINESS_URL},
])
def test_bounds_and_exact_target_are_fail_closed(tmp_path, changes):
    with pytest.raises((PermissionError, ValueError)):
        experiment.run_error_experiment(
            tmp_path / "blocked", authorization=authorization(), **changes,
        )
    assert not (tmp_path / "blocked").exists()


def test_expected_500_preserves_raw_hashes_utc_and_both_identity_checks(tmp_path, monkeypatch):
    def sender(url, number):
        result = response(url)
        result.headers.update({
            "Content-Type": "text/plain" if number == 2 else "application/json",
            "Connection": "close",
            "Set-Cookie": "must-not-be-archived",
        })
        return result

    report, calls, _ = run(tmp_path, monkeypatch, sender)
    assert calls == [experiment.VERSION_URL, experiment.ERROR_URL, experiment.VERSION_URL]
    assert report["status"] == "traffic_completed_observations_pending"
    assert report["business_route_requests"] == report["expected_500_responses"] == 1
    assert report["identity_verification_requests"] == 2
    assert report["completed_verified_fault_cycles"] == 1
    assert report["business_recovery_verified"] is False
    assert report["real_model_sdk_requests"] == report["remediation_executions"] == 0
    root = tmp_path / "traffic"
    for row in report["http_requests"]:
        assert row["query_started_at"].endswith("+00:00")
        assert row["query_finished_at"].endswith("+00:00")
        assert row["response_headers"]["Connection"] == "close"
        assert row["response_headers"]["Content-Type"] == row["response_content_type"]
        assert "Set-Cookie" not in row["response_headers"]
        assert hashlib.sha256((root / row["raw_body_file"]).read_bytes()).hexdigest() == (
            row["raw_body_sha256"]
        )
    assert experiment._verified_child_report(root, 1) == report


@pytest.mark.parametrize("status", [200, 404, 302])
def test_unexpected_fault_status_stops_without_retry_or_after_check(tmp_path, monkeypatch, status):
    report, calls, _ = run(
        tmp_path, monkeypatch,
        lambda url, number: response(url, status=status if number == 2 else 200),
        count=3,
    )
    assert len(calls) == 2
    assert report["status"] == "stopped"
    assert report["business_route_requests"] == 1
    assert report["identity_verification_requests"] == 1
    assert report["expected_500_responses"] == 0
    assert report["stop_error_type"] == "HTTPError"
    experiment._verified_child_report(tmp_path / "traffic", 3)


@pytest.mark.parametrize("phase_number", [1, 3])
def test_identity_change_stops_at_before_or_after_check(tmp_path, monkeypatch, phase_number):
    report, calls, _ = run(
        tmp_path, monkeypatch,
        lambda url, number: response(url, value={**VERSION, "commit_sha": "b" * 40})
        if number == phase_number else response(url), count=3,
    )
    assert len(calls) == phase_number
    assert report["business_route_requests"] == (1 if phase_number == 3 else 0)
    assert report["stop_reason"] == "p62a_process_identity_or_fault_flag_changed"


def test_request_timeout_is_entered_once_and_stops(tmp_path, monkeypatch):
    def sender(url, number):
        if number == 2:
            raise requests.Timeout("mock timeout")
        return response(url)

    report, calls, _ = run(tmp_path, monkeypatch, sender, count=3)
    assert len(calls) == 2
    assert report["business_route_requests"] == 1
    assert report["expected_500_responses"] == 0
    assert report["http_requests"][-1]["http_status"] is None
    assert report["stop_error_type"] == "Timeout"


def test_response_target_change_stops_and_raw_record_is_not_certified(tmp_path, monkeypatch):
    def sender(url, number):
        result = response(url)
        if number == 2:
            result.url = "http://localhost:8001/order"
        return result

    report, calls, _ = run(tmp_path, monkeypatch, sender, count=3)
    assert len(calls) == 2
    assert report["stop_reason"] == "p62b_response_source_changed"
    with pytest.raises(ValueError):
        experiment._verified_child_report(tmp_path / "traffic", 3)


def test_max_30_and_reserved_final_round_are_respected(tmp_path, monkeypatch):
    report, calls, clock = run(tmp_path, monkeypatch, count=30)
    assert report["business_route_requests"] == 30
    assert report["identity_verification_requests"] == 60
    assert len(calls) == 90 and clock.now == 29
    assert report["elapsed_seconds"] < 60


def test_parent_deadline_and_full_round_reserve_stop_before_new_requests(tmp_path, monkeypatch):
    clock = Clock()

    def sender(url, number):
        clock.now += 2
        return response(url)

    report, calls, _ = run(
        tmp_path, monkeypatch, sender, clock=clock, count=30, _supervisor_deadline=40,
    )
    assert calls == [experiment.VERSION_URL, experiment.ERROR_URL, experiment.VERSION_URL] * 2
    assert clock.now == 13
    assert report["stop_reason"] == "p62b_insufficient_full_round_budget"
    assert report["business_route_requests"] == 2
    assert report["request_count_status"] == "final_child_report"


def test_deadline_interrupt_keeps_both_totals_unknown(tmp_path, monkeypatch):
    clock = Clock()

    def sender(url, number):
        if number == 2:
            clock.now = 60
            raise requests.Timeout("deadline interruption")
        return response(url)

    report, calls, _ = run(tmp_path, monkeypatch, sender, clock=clock)
    assert len(calls) == 2
    assert report["business_route_requests"] is None
    assert report["identity_verification_requests"] is None
    assert report["request_count_status"] == "unknown"
    with pytest.raises(ValueError):
        experiment._verified_child_report(tmp_path / "traffic", 1)


def test_tampered_original_is_rejected(tmp_path, monkeypatch):
    run(tmp_path, monkeypatch)
    (tmp_path / "traffic" / "http-002.body").write_bytes(b"tampered")
    with pytest.raises(ValueError):
        experiment._verified_child_report(tmp_path / "traffic", 1)


def test_supervisor_reuses_owned_helper_and_consumes_authorization_once(tmp_path, monkeypatch):
    clock = Clock()
    children = []
    auth_path = tmp_path / "user-authorization.json"
    auth_path.write_text(json.dumps(authorization()), encoding="utf-8")

    class Child:
        def __init__(self, args, **kwargs):
            assert args[:3] == [sys.executable, str(experiment.Path(experiment.__file__).resolve()),
                                "--_authorized-child"]
            assert kwargs["shell"] is False
            assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
            self.args = args
            self.pid = 24680
            self.returncode = None
            self.killed = False
            children.append(self)
            clock.now += 3

        def wait(self, timeout=None):
            if timeout is None:
                return self.returncode
            assert timeout == 57
            payload = json.loads(experiment.Path(self.args[3]).read_bytes())
            assert payload["parent_deadline_monotonic"] == 60
            monkeypatch.setattr(requests.sessions.Session, "request",
                                lambda session, method, url, **kwargs: response(url))
            experiment.run_error_experiment(
                payload["output_dir"], clock=clock, sleep=clock.sleep,
                _supervisor_deadline=payload["parent_deadline_monotonic"],
                **payload["arguments"],
            )
            self.returncode = 0

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

    monkeypatch.setattr(subprocess, "Popen", Child)
    result = experiment.run_supervised_error_experiment(
        tmp_path / "supervised", authorization_file=auth_path, count=1, clock=clock,
    )
    assert len(children) == 1 and children[0].killed is False
    assert result["business_route_requests"] == result["expected_500_responses"] == 1
    assert result["identity_verification_requests"] == 2
    assert result["business_request_count_status"] == "verified_child_report"
    assert result["mode"] == "p62b_owned_error_child_supervisor"
    with pytest.raises(FileExistsError):
        experiment.run_supervised_error_experiment(
            tmp_path / "second", authorization_file=auth_path, count=1, clock=clock,
        )
    assert len(children) == 1 and not (tmp_path / "second").exists()


@pytest.mark.parametrize("mode", ["timeout", "missing_report", "invalid_report"])
def test_supervisor_unknown_totals_and_only_own_child_killed(tmp_path, monkeypatch, mode):
    clock = Clock()
    auth_path = tmp_path / "user-authorization.json"
    auth_path.write_text(json.dumps(authorization()), encoding="utf-8")
    children = []

    class Child:
        def __init__(self, args, **kwargs):
            self.args, self.pid, self.returncode = args, 24680, None
            self.killed = False
            children.append(self)

        def wait(self, timeout=None):
            if timeout is None:
                return self.returncode
            if mode == "timeout":
                clock.now += timeout
                raise subprocess.TimeoutExpired(self.args, timeout)
            payload = json.loads(experiment.Path(self.args[3]).read_bytes())
            root = experiment.Path(payload["output_dir"])
            root.mkdir()
            if mode == "invalid_report":
                (root / "sha256.json").write_text("{}", encoding="utf-8")
            self.returncode = 0

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

    monkeypatch.setattr(subprocess, "Popen", Child)
    result = experiment.run_supervised_error_experiment(
        tmp_path / "supervised", authorization_file=auth_path, count=1, clock=clock,
    )
    assert len(children) == 1
    assert children[0].killed is (mode == "timeout")
    assert result["business_route_requests"] is None
    assert result["identity_verification_requests"] is None
    assert result["expected_500_responses"] is None
    assert result["business_request_count_status"] == "unknown"
    assert result["identity_request_count_status"] == "unknown"
    assert result["originals_may_be_incomplete"] is True


def test_child_requires_shared_parent_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(baseline.os, "getppid", lambda: 1234)
    path = tmp_path / "authorization.json"
    path.write_text(json.dumps({
        "supervisor_pid": 1234, "output_dir": str(tmp_path / "traffic"),
        "arguments": {"authorization": authorization(), "count": 1, "max_seconds": 60},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="parent_deadline"):
        experiment._child_main(path)
    assert not (tmp_path / "traffic").exists()


def test_same_authorization_copied_to_another_path_is_consumed_once(tmp_path, monkeypatch):
    accepted = []
    monkeypatch.setattr(
        baseline, "_run_owned_child_supervisor",
        lambda *args, **kwargs: accepted.append(kwargs) or {},
    )
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps(authorization()), encoding="utf-8")
    second.write_bytes(first.read_bytes())
    experiment.run_supervised_error_experiment(
        tmp_path / "first-run", authorization_file=first, count=1,
    )
    with pytest.raises(FileExistsError):
        experiment.run_supervised_error_experiment(
            tmp_path / "second-run", authorization_file=second, count=1,
        )
    assert len(accepted) == 1
    assert not (tmp_path / "second-run").exists()
