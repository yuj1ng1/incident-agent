"""All HTTP in this module is mocked; no business traffic is authorized here."""
import hashlib
import io
import json
import runpy
import sys
import subprocess

import pytest
import requests

from scripts import p62a_business_baseline as baseline


IDENTITY = {
    "verified_process_id": 1234,
    "process_started_at": "2026-10-18T01:00:00+00:00",
    "commit_sha": "a" * 40,
    "ownership_note": "Mock-only process/port verification fixture",
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
ORDER = {
    "service": "order-service",
    "dependency": "order-db",
    "order": {"id": 999, "item": "private-row-fixture", "quantity": 4},
}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def response(
    url,
    value,
    status=200,
    headers=None,
):
    result = requests.Response()
    result.status_code = status
    result.url = url
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    result.raw = io.BytesIO(body)
    result.headers.update(headers or {})
    return result


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("P6.2a offline tests must never send real HTTP")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def run(
    tmp_path,
    monkeypatch,
    sender=None,
    **changes,
):
    calls = []

    def send(session, method, url, **kwargs):
        assert session.get_adapter(url).max_retries.total == 0
        assert not session.cookies
        assert not session.proxies
        calls.append((method, url, kwargs, session.trust_env, session.auth))
        if sender is not None:
            return sender(session, method, url, **kwargs)
        return response(url, VERSION if url == baseline.VERSION_URL else ORDER)

    monkeypatch.setattr(requests.sessions.Session, "request", send)
    clock = Clock()
    arguments = {
        "authorized": True,
        "authorization_note": "Mock authorization only, no live traffic",
        "verified_identity": IDENTITY.copy(),
        "count": 3,
        "clock": clock,
        "sleep": clock.sleep,
    }
    arguments.update(changes)
    report = baseline.run_business_baseline(tmp_path / "traffic", **arguments)
    return report, calls, clock


@pytest.mark.parametrize("authorization", [
    {},
    {"authorized": True},
    {"authorized": True, "authorization_note": "   "},
    {"authorized": "true", "authorization_note": "not a bool"},
])
def test_unauthorized_rejected_without_network_or_directory(tmp_path, authorization):
    target = tmp_path / "must-not-exist"
    with pytest.raises(PermissionError):
        baseline.run_business_baseline(target, **authorization)
    assert not target.exists()


@pytest.mark.parametrize("method,url", [
    ("POST", baseline.BUSINESS_URL),
    ("HEAD", baseline.BUSINESS_URL),
    ("get", baseline.BUSINESS_URL),
    ("GET", "http://127.0.0.1:8001/order"),
    ("GET", "http://localhost:8001/order?limit=1"),
    ("GET", "http://localhost:8001/order#fragment"),
    ("GET", "https://localhost:8001/order"),
    ("GET", "http://user:password@localhost:8001/order"),
    ("GET", "http://localhost:8001/error"),
    ("GET", baseline.VERSION_URL),
])
def test_non_whitelisted_method_or_url_never_enters_transport(tmp_path, method, url):
    with pytest.raises(ValueError):
        baseline.run_business_baseline(
            tmp_path / "forbidden",
            authorized=True,
            authorization_note="Mock-only authorization",
            verified_identity=IDENTITY,
            method=method,
            url=url,
        )
    assert not (tmp_path / "forbidden").exists()


@pytest.mark.parametrize("changes", [
    {"count": 0}, {"count": 31}, {"count": True}, {"count": 1.5},
    {"max_seconds": 0}, {"max_seconds": 61}, {"max_seconds": True},
    {"max_seconds": float("nan")}, {"max_seconds": float("inf")},
    {"verified_identity": None},
    {"verified_identity": {**IDENTITY, "ownership_note": ""}},
    {"verified_identity": {**IDENTITY, "verified_process_id": 0}},
    {"verified_identity": {**IDENTITY, "process_started_at": "2026-10-18T01:00:00"}},
    {"verified_identity": {**IDENTITY, "commit_sha": "abc"}},
])
def test_bounds_and_identity_refused_before_io(tmp_path, changes):
    arguments = {
        "authorized": True,
        "authorization_note": "Mock-only authorization",
        "verified_identity": IDENTITY,
    }
    arguments.update(changes)
    with pytest.raises(ValueError):
        baseline.run_business_baseline(tmp_path / "invalid", **arguments)
    assert not (tmp_path / "invalid").exists()


def test_default_cli_and_import_are_offline(monkeypatch, capsys):
    def no_directory(*args, **kwargs):
        pytest.fail("offline default/import must not create directories")

    monkeypatch.setattr(baseline.Path, "mkdir", no_directory)
    runpy.run_path(baseline.__file__, run_name="p62a_import_safety_test")
    monkeypatch.setattr(sys, "argv", [baseline.__file__])
    runpy.run_path(baseline.__file__, run_name="__main__")
    plan = json.loads(capsys.readouterr().out)
    assert plan["business_route_requests"] == 0
    assert plan["mode"] == "offline_plan_only"


def test_max_count_serial_raw_source_hash_and_safe_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://external.invalid:8080")
    report, calls, clock = run(tmp_path, monkeypatch, count=30)
    assert report["business_route_requests"] == 30
    assert report["successful_business_responses"] == 30
    assert clock.now == 29
    assert report["status"] == "traffic_completed_observations_pending"
    assert report["business_state"] == "unknown"
    assert report["business_recovery_verified"] is False
    assert report["real_model_sdk_requests"] == report["agent_tool_calls"] == 0
    assert len(calls) == 90
    assert [call[1] for call in calls[:3]] == [
        baseline.VERSION_URL, baseline.BUSINESS_URL, baseline.VERSION_URL,
    ]
    for method, url, options, trust_env, auth in calls:
        assert method == "GET"
        assert url in (baseline.VERSION_URL, baseline.BUSINESS_URL)
        assert options["allow_redirects"] is False
        assert options["timeout"] == baseline.REQUEST_TIMEOUT
        assert options["proxies"] == {}
        assert options["headers"] == {"Accept-Encoding": "identity"}
        assert options["stream"] is True
        assert trust_env is False and auth is None
    root = tmp_path / "traffic"
    for record in report["http_requests"]:
        raw = (root / record["raw_body_file"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == record["raw_body_sha256"]
        assert record["request_executed"] is True
        assert record["query_started_at"] <= record["query_finished_at"]
        assert json.loads((root / (record["id"] + ".json")).read_text()) == record
    summary = (root / "business-001-summary.json").read_text()
    assert "private-row-fixture" not in summary
    assert json.loads(summary)["source_request_id"] == "http-002"
    assert "private-row-fixture" not in json.dumps(report)
    manifest = json.loads((root / "sha256.json").read_text())
    for name, digest in manifest.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest


def test_duration_limit_stops_before_new_request(tmp_path, monkeypatch):
    report, calls, clock = run(tmp_path, monkeypatch, count=30, max_seconds=2)
    assert report["business_route_requests"] == 2
    assert report["status"] == "stopped"
    assert report["stop_error_type"] == "Timeout"
    assert clock.now == 1
    assert len(calls) == 6


@pytest.mark.parametrize("failure", ["500", "302", "timeout", "connection", "shape"])
def test_unexpected_business_response_stops_remaining_traffic(
    tmp_path,
    monkeypatch,
    failure,
):
    def sender(session, method, url, **kwargs):
        if url == baseline.VERSION_URL:
            return response(url, VERSION)
        if failure == "timeout":
            raise requests.Timeout("fixture no network")
        if failure == "connection":
            raise requests.ConnectionError("fixture no network")
        if failure == "shape":
            return response(url, {"service": "other-service"})
        return response(url, {"error": "fixture"}, int(failure))

    report, calls, _ = run(tmp_path, monkeypatch, sender)
    assert report["status"] == "stopped"
    assert report["business_route_requests"] == 1
    assert len(calls) == 2
    attempt = report["http_requests"][-1]
    assert attempt["phase"] == "business"
    assert attempt["request_executed"] is True
    raw = (tmp_path / "traffic" / attempt["raw_body_file"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == attempt["raw_body_sha256"]
    if failure in ("timeout", "connection"):
        assert raw == b"" and attempt["body_complete"] is False


@pytest.mark.parametrize("change", [
    {"process_started_at": "2026-10-18T01:00:01+00:00"},
    {"commit_sha": "b" * 40},
    {"service": "other-service"},
    {"runtime_flags": {
        "simulate_code_regression": True, "simulate_slow_request": False,
    }},
    {"runtime_flags": {
        "simulate_code_regression": False, "simulate_slow_request": "false",
    }},
    {"runtime_flags": {}},
])
@pytest.mark.parametrize("phase", ["before", "after"])
def test_changed_identity_or_flags_stop_before_next_business(
    tmp_path,
    monkeypatch,
    change,
    phase,
):
    versions = 0

    def sender(session, method, url, **kwargs):
        nonlocal versions
        if url == baseline.VERSION_URL:
            versions += 1
            modified = phase == "before" or versions == 2
            return response(url, {**VERSION, **change} if modified else VERSION)
        return response(url, ORDER)

    report, calls, _ = run(tmp_path, monkeypatch, sender)
    assert report["status"] == "stopped"
    assert report["stop_reason"] == "p62a_process_identity_or_fault_flag_changed"
    assert report["business_route_requests"] == (0 if phase == "before" else 1)
    assert len(calls) == (1 if phase == "before" else 3)


def test_active_trickle_keeps_partial_body_and_stops(tmp_path, monkeypatch):
    clock = Clock()

    def sender(session, method, url, **kwargs):
        result = response(url, VERSION if url == baseline.VERSION_URL else ORDER)
        if url == baseline.BUSINESS_URL:
            def trickle(chunk_size):
                assert chunk_size == 1
                for byte in b"1234567890":
                    clock.now += 1
                    yield bytes([byte])

            result.iter_content = trickle
        return result

    report, calls, _ = run(
        tmp_path, monkeypatch, sender,
        clock=clock, sleep=clock.sleep,
    )
    assert report["business_route_requests"] == 1
    assert report["stop_error_type"] == "Timeout"
    assert len(calls) == 2
    attempt = report["http_requests"][-1]
    assert attempt["body_complete"] is False
    assert (tmp_path / "traffic" / attempt["raw_body_file"]).read_bytes() == b"1234567"
    assert attempt["elapsed_seconds"] == 8


def test_identity_request_consuming_global_deadline_prevents_business(
    tmp_path,
    monkeypatch,
):
    clock = Clock()

    def sender(session, method, url, **kwargs):
        clock.now += 2
        return response(url, VERSION)

    report, calls, _ = run(
        tmp_path, monkeypatch, sender,
        clock=clock, sleep=clock.sleep, max_seconds=2,
    )
    assert len(calls) == 1
    assert report["business_route_requests"] == 0
    assert report["stop_error_type"] == "Timeout"


@pytest.mark.parametrize("headers,body", [
    ({"Content-Encoding": "gzip"}, b"compressed"),
    ({"Content-Length": str(baseline.MAX_RESPONSE_BYTES + 1)}, b"oversized"),
    ({}, b"x" * (baseline.MAX_RESPONSE_BYTES + 1)),
], ids=["compressed", "content-length", "streamed-size"])
def test_response_limits_stop_and_preserve_observed_body(
    tmp_path,
    monkeypatch,
    headers,
    body,
):
    def sender(session, method, url, **kwargs):
        return response(url, body, headers=headers)

    report, calls, _ = run(tmp_path, monkeypatch, sender)
    assert len(calls) == 1
    assert report["business_route_requests"] == 0
    assert report["status"] == "stopped"
    assert report["http_requests"][0]["captured_bytes"] <= baseline.MAX_RESPONSE_BYTES


def test_existing_output_is_never_overwritten_or_contacted(tmp_path):
    root = tmp_path / "existing"
    root.mkdir()
    marker = root / "keep.txt"
    marker.write_text("protected", encoding="utf-8")
    with pytest.raises(FileExistsError):
        baseline.run_business_baseline(
            root,
            authorized=True,
            authorization_note="Mock-only authorization",
            verified_identity=IDENTITY,
        )
    assert list(root.iterdir()) == [marker]
    assert marker.read_text() == "protected"


def test_p61_business_route_still_forbidden(tmp_path):
    from scripts.p61_collect_observations import (
        _capture_http,
        ReadOnlyBoundaryViolation,
    )

    with _capture_http(tmp_path):
        with pytest.raises(ReadOnlyBoundaryViolation):
            requests.get(baseline.BUSINESS_URL)


def test_explicit_unknown_runtime_sha_stays_unknown(tmp_path, monkeypatch):
    def sender(session, method, url, **kwargs):
        value = {**VERSION, "commit_sha": None} if url == baseline.VERSION_URL else ORDER
        return response(url, value)

    report, calls, _ = run(
        tmp_path, monkeypatch, sender,
        verified_identity={**IDENTITY, "commit_sha": None}, count=1,
    )
    assert report["business_route_requests"] == 1
    assert report["running_version"] == "unknown"
    assert report["code_causal_claims_verified"] is False
    assert len(calls) == 3


def test_missing_runtime_sha_field_is_not_explicit_unknown(tmp_path, monkeypatch):
    def sender(session, method, url, **kwargs):
        value = {key: item for key, item in VERSION.items() if key != "commit_sha"}
        return response(url, value)

    report, calls, _ = run(
        tmp_path, monkeypatch, sender,
        verified_identity={**IDENTITY, "commit_sha": None},
    )
    assert report["business_route_requests"] == 0
    assert report["stop_reason"] == "p62a_process_identity_or_fault_flag_changed"
    assert len(calls) == 1


def supervised_arguments(**changes):
    arguments = {
        "authorized": True,
        "authorization_note": "Mock-only supervisor authorization",
        "verified_identity": IDENTITY.copy(),
        "count": 1,
        "max_seconds": 10,
    }
    arguments.update(changes)
    return arguments


def mock_child(monkeypatch, clock, mode="complete", startup_seconds=0):
    children = []

    class Child:
        def __init__(self, args, **kwargs):
            assert args[:3] == [
                sys.executable, str(baseline.Path(baseline.__file__).resolve()),
                "--_authorized-child",
            ]
            assert len(args) == 4
            self.authorization_path = baseline.Path(args[3])
            assert self.authorization_path.name == "authorization.json"
            assert self.authorization_path.is_file()
            assert kwargs["shell"] is False
            assert kwargs["stdin"] == subprocess.DEVNULL
            assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
            assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
            assert "env" not in kwargs
            self.args = args
            self.pid = 24680
            self.returncode = None
            self.killed = False
            self.waited = False
            self.wait_timeout = None
            clock.now += startup_seconds
            children.append(self)

        def communicate(self, *args, **kwargs):
            pytest.fail("supervisor must never write a potentially blocking stdin pipe")

        def wait(self, timeout=None):
            if timeout is None:
                self.waited = True
                return self.returncode
            self.wait_timeout = timeout
            payload = json.loads(self.authorization_path.read_bytes())
            assert payload["supervisor_pid"] == baseline.os.getpid()
            assert payload["arguments"]["authorization_note"].startswith("Mock-only")
            root = baseline.Path(payload["output_dir"])
            root.mkdir()
            if mode == "timeout":
                (root / "partial.body").write_bytes(b"preserve partial original")
                clock.now += timeout
                raise subprocess.TimeoutExpired(self.args, timeout)
            if mode == "exit_failure":
                self.returncode = 7
                return
            count = 0 if mode == "stopped" else 1
            value = {
                "mode": "explicitly_authorized_direct_business_traffic",
                "requested_count": 1,
                "business_route_requests": count,
                "successful_business_responses": count,
                "status": (
                    "stopped" if mode == "stopped"
                    else "traffic_completed_observations_pending"
                ),
                "http_requests": [
                    {"phase": "business", "request_executed": True}
                    for _ in range(count)
                ],
            }
            if mode == "count_mismatch":
                value["business_route_requests"] = 0
            baseline._write_json(root / "traffic_report.json", value)
            baseline._write_json(root / "sha256.json", {
                "traffic_report.json": hashlib.sha256(
                    (root / "traffic_report.json").read_bytes()
                ).hexdigest() if mode != "invalid_manifest" else "invalid",
            })
            clock.now += 1
            self.returncode = 0

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

    monkeypatch.setattr(subprocess, "Popen", Child)
    return children


def test_supervisor_denies_unauthorized_before_child_or_files(tmp_path):
    with pytest.raises(PermissionError):
        baseline.run_supervised_baseline(tmp_path / "no-child")
    assert not (tmp_path / "no-child").exists()


@pytest.mark.parametrize("changes", [
    {"authorization_note": ""}, {"count": 31}, {"max_seconds": 61},
    {"method": "POST"}, {"url": "http://localhost:8001/error"},
    {"verified_identity": None},
])
def test_supervisor_validates_every_boundary_before_child(tmp_path, changes):
    with pytest.raises((PermissionError, ValueError)):
        baseline.run_supervised_baseline(
            tmp_path / "no-child", **supervised_arguments(**changes),
        )
    assert not (tmp_path / "no-child").exists()


@pytest.mark.parametrize("mode,expected,count", [
    ("complete", "traffic_completed_observations_pending", 1),
    ("stopped", "child_stopped", 0),
])
def test_supervisor_reads_verified_child_exit_and_count(
    tmp_path,
    monkeypatch,
    mode,
    expected,
    count,
):
    clock = Clock()
    children = mock_child(monkeypatch, clock, mode, startup_seconds=3)
    root = tmp_path / "supervised"
    result = baseline.run_supervised_baseline(root, clock=clock, **supervised_arguments())
    assert result["status"] == expected
    assert result["business_route_requests"] == count
    assert result["child_exit_code"] == 0
    assert result["business_request_count_status"] == "verified_child_report"
    assert result["originals_may_be_incomplete"] is False
    assert children[0].wait_timeout == 7
    assert children[0].killed is False
    manifest = json.loads((root / "sha256.json").read_text())
    for name, digest in manifest.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest


def test_supervisor_deadline_kills_only_owned_child_and_preserves_partial_evidence(
    tmp_path,
    monkeypatch,
):
    clock = Clock()
    children = mock_child(monkeypatch, clock, "timeout", startup_seconds=2)
    root = tmp_path / "deadline"
    result = baseline.run_supervised_baseline(root, clock=clock, **supervised_arguments())
    assert len(children) == 1
    assert result["child_pid"] == children[0].pid
    assert children[0].wait_timeout == 8
    assert children[0].killed is True and children[0].waited is True
    assert result["deadline_exceeded"] is True
    assert result["child_killed_by_supervisor"] is True
    assert result["business_route_requests"] is None
    assert result["business_request_count_status"] == "unknown"
    assert result["originals_may_be_incomplete"] is True
    assert result["status"] == "deadline_exceeded"
    assert (root / "traffic" / "partial.body").read_bytes() == b"preserve partial original"
    assert (root / "supervisor.json").is_file()


def test_supervisor_startup_cost_consumes_deadline_before_payload_is_sent(tmp_path, monkeypatch):
    clock = Clock()
    children = mock_child(monkeypatch, clock, startup_seconds=10)
    result = baseline.run_supervised_baseline(
        tmp_path / "startup", clock=clock, **supervised_arguments(),
    )
    assert children[0].wait_timeout is None
    assert children[0].killed is True and children[0].waited is True
    assert result["status"] == "deadline_exceeded"
    assert result["business_route_requests"] is None


@pytest.mark.parametrize("mode", ["exit_failure", "invalid_manifest", "count_mismatch"])
def test_supervisor_failed_child_never_becomes_completed_or_zero(tmp_path, monkeypatch, mode):
    clock = Clock()
    children = mock_child(monkeypatch, clock, mode)
    result = baseline.run_supervised_baseline(
        tmp_path / "failed", clock=clock, **supervised_arguments(),
    )
    assert len(children) == 1
    assert result["status"] == "child_failed"
    assert result["business_route_requests"] is None
    assert result["business_request_count_status"] == "unknown"
    assert result["originals_may_be_incomplete"] is True


def test_supervisor_preserves_existing_directory_without_child(tmp_path):
    marker = tmp_path / "keep.txt"
    marker.write_text("protected", encoding="utf-8")
    with pytest.raises(FileExistsError):
        baseline.run_supervised_baseline(tmp_path, **supervised_arguments())
    assert list(tmp_path.iterdir()) == [marker]


def test_unauthorized_execute_cli_never_creates_child(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        baseline.__file__, "--execute", "--output-dir", str(tmp_path / "blocked"),
        "--verified-commit-sha", "unknown",
    ])
    with pytest.raises(PermissionError):
        baseline.main()
    assert not (tmp_path / "blocked").exists()


def test_oversized_authorization_is_rejected_before_child_or_directory(tmp_path):
    root = tmp_path / "oversized"
    with pytest.raises(ValueError, match="p62a_authorization_payload_size_limit"):
        baseline.run_supervised_baseline(root, **supervised_arguments(
            authorization_note="x" * baseline.MAX_AUTHORIZATION_BYTES,
        ))
    assert not root.exists()


@pytest.mark.parametrize("invalid", ["parent", "size", "output"])
def test_child_rejects_invalid_authorization_before_http(tmp_path, monkeypatch, invalid):
    arguments = supervised_arguments()
    payload = {
        "output_dir": str(tmp_path / "traffic"),
        "supervisor_pid": 1234,
        "arguments": arguments,
    }
    monkeypatch.setattr(baseline.os, "getppid", lambda: 1234)
    if invalid == "parent":
        payload["supervisor_pid"] = 9876
    elif invalid == "size":
        payload["arguments"]["authorization_note"] = "x" * baseline.MAX_AUTHORIZATION_BYTES
    else:
        payload["output_dir"] = str(tmp_path / "outside")
    path = tmp_path / "authorization.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises((PermissionError, ValueError)):
        baseline._child_main(path)
    assert not (tmp_path / "traffic").exists()


def test_child_reads_bounded_authorization_file_and_reuses_internal_entry(tmp_path, monkeypatch):
    arguments = supervised_arguments()
    payload = {
        "output_dir": str(tmp_path / "traffic"),
        "supervisor_pid": 1234,
        "arguments": arguments,
    }
    path = tmp_path / "authorization.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    calls = []
    monkeypatch.setattr(baseline.os, "getppid", lambda: 1234)
    monkeypatch.setattr(
        baseline, "run_business_baseline", lambda target, **kwargs: calls.append((target, kwargs)),
    )
    baseline._child_main(path)
    assert calls == [(payload["output_dir"], arguments)]
