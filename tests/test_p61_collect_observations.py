"""Only mocked HTTP transport is used to verify the opt-in P6.1 collector."""
from datetime import datetime, timezone
import hashlib
import io
import json
import runpy

import pytest
import requests

from scripts.p61_collect_observations import (
    MAX_RESPONSE_BYTES,
    REQUEST_TIMEOUT,
    ReadOnlyBoundaryViolation,
    _capture_http,
    run_collection,
)


def response(body=b"ready", status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    result.url = "http://localhost:9090/-/ready"
    result.raw = io.BytesIO(body)
    result.headers.update(headers or {})
    return result


def json_response(body):
    return response(json.dumps(body).encode("utf-8"))


@pytest.mark.parametrize("method,url", [
    ("POST", "http://localhost:8001/health"),
    ("GET", "http://localhost:8001/order"),
    ("GET", "http://localhost:8001/admin/toggle"),
    ("GET", "http://localhost:8002/health"),
    ("GET", "http://example.com:9090/api/v1/query"),
    ("GET", "http://127.0.0.1:9090/api/v1/query"),
    ("GET", "http://localhost:9090/api/v1/query?query=up"),
    ("GET", "http://user:password@localhost:8001/health"),
    ("GET", "https://localhost:8001/health"),
])
def test_boundary_rejects_before_transport(tmp_path, monkeypatch, method, url):
    attempted = []
    monkeypatch.setattr(requests.sessions.Session, "request", lambda *a, **k: attempted.append(k))
    with _capture_http(tmp_path) as records:
        with pytest.raises(ReadOnlyBoundaryViolation):
            requests.request(method, url)
    assert attempted == []
    assert records[0]["request_executed"] is False
    assert records[0]["error_type"] == "ReadOnlyBoundaryViolation"


def test_raw_bytes_query_and_hash_are_preserved_and_transport_is_bounded(
    tmp_path,
    monkeypatch,
):
    body = b'{"status":"success","raw":"\\u4e2d"}\n'
    sent = []

    def send(session, method, url, **kwargs):
        sent.append((session.trust_env, session.auth, kwargs))
        return response(body)

    monkeypatch.setattr(requests.sessions.Session, "request", send)
    monkeypatch.setenv("HTTP_PROXY", "http://external-proxy.invalid:8080")
    with _capture_http(tmp_path) as records:
        result = requests.get("http://localhost:9090/api/v1/query", params={"query": "up"})
    assert result.content == body
    assert sent[0][0:2] == (False, None)
    options = sent[0][2]
    assert options["proxies"] == {}
    assert options["timeout"] == REQUEST_TIMEOUT
    assert options["allow_redirects"] is False
    assert options["stream"] is True
    row = records[0]
    assert row["query_parameters"] == {"query": "up"}
    assert row["query_started_at"] <= row["query_finished_at"]
    assert row["body_complete"] is True
    assert (tmp_path / row["raw_body_file"]).read_bytes() == body
    assert row["raw_body_sha256"] == hashlib.sha256(body).hexdigest()
    assert json.loads((tmp_path / "http-001.json").read_text()) == row


def test_redirect_is_retained_and_refused_without_second_request(tmp_path, monkeypatch):
    attempts = []

    def send(*args, **kwargs):
        attempts.append(kwargs)
        return response(b"moved", 302, {"Location": "http://example.com/write"})

    monkeypatch.setattr(requests.sessions.Session, "request", send)
    with _capture_http(tmp_path) as records:
        with pytest.raises(requests.HTTPError, match="p61_redirect_refused"):
            requests.get("http://localhost:8001/health")
    assert len(attempts) == 1
    assert records[0]["http_status"] == 302
    assert records[0]["response_headers"]["Location"] == "http://example.com/write"
    assert records[0]["body_complete"] is True
    assert (tmp_path / "http-001.body").read_bytes() == b"moved"


@pytest.mark.parametrize("headers,body", [
    ({"Content-Length": str(MAX_RESPONSE_BYTES + 1)}, b"oversized"),
    ({"Content-Encoding": "gzip"}, b"compressed"),
    ({}, b"x" * (MAX_RESPONSE_BYTES + 1)),
], ids=["content-length", "compressed", "streamed-size"])
def test_oversized_or_compressed_data_cannot_reach_tools(
    tmp_path,
    monkeypatch,
    headers,
    body,
):
    monkeypatch.setattr(
        requests.sessions.Session, "request", lambda *a, **k: response(body, headers=headers),
    )
    with _capture_http(tmp_path) as records:
        with pytest.raises(ValueError):
            requests.get("http://localhost:8001/metrics")
    assert records[0]["body_complete"] is False
    assert records[0]["captured_bytes"] <= MAX_RESPONSE_BYTES
    assert records[0]["error_type"] == "ValueError"


def test_connection_failure_retains_attempt_and_error_type(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise requests.ConnectionError("fixture with no network")

    monkeypatch.setattr(requests.sessions.Session, "request", fail)
    with _capture_http(tmp_path) as records:
        with pytest.raises(requests.ConnectionError):
            requests.get("http://localhost:3100/ready")
    assert records[0]["request_executed"] is True
    assert records[0]["http_status"] is None
    assert records[0]["error_type"] == "ConnectionError"
    assert "raw_body_file" not in records[0]


def test_real_tool_parsers_preserve_recent_empty_logs_no_data_and_manifest(
    tmp_path,
    monkeypatch,
):
    def send(session, method, url, **kwargs):
        assert method == "GET"
        if url.endswith("/query"):
            return json_response({
                "status": "success",
                "data": {"resultType": "vector", "result": []},
            })
        if url.endswith("/query_range"):
            return json_response({
                "status": "success",
                "data": {"resultType": "streams", "result": []},
            })
        if url.endswith("/health"):
            return json_response({"health": "ok"})
        if url.endswith("/version"):
            return json_response({
                "service": "order-service",
                "process_started_at": datetime.now(timezone.utc).isoformat(),
                "commit_sha": "a" * 40,
                "runtime_flags": {
                    "simulate_code_regression": False,
                    "simulate_slow_request": False,
                },
            })
        if url.endswith("/targets"):
            return json_response({"status": "success", "data": {"activeTargets": []}})
        return response()

    monkeypatch.setattr(requests.sessions.Session, "request", send)
    target = tmp_path / "new-run"
    report = run_collection(target)
    observations = json.loads((target / "observations.json").read_text(encoding="utf-8"))
    logs = observations["query_logs"]["result"]
    assert logs["status"] == "no_data"
    assert logs["reason"] == "no_logs_in_query_range"
    assert logs["evidence_scope"] == logs["query_range"]["mode"] == "recent"
    assert observations["query_metrics"]["result"]["status"] == "no_data"
    assert "value" not in observations["query_metrics"]["result"]
    assert observations["check_service_health"]["result"]["health"] == "ok"
    assert report["business_state"] == "unknown"
    assert not report["business_recovery_verified"]
    assert report["real_model_sdk_requests"] == 0
    assert report["database_connections"] == report["business_route_requests"] == 0
    assert len(report["http_requests"]) == 14
    manifest = json.loads((target / "sha256.json").read_text())
    for name, digest in manifest.items():
        assert hashlib.sha256((target / name).read_bytes()).hexdigest() == digest
    assert all(
        row["request_ids"] and row["source"]
        for row in observations.values()
    )


def test_existing_output_directory_is_untouched(tmp_path):
    marker = tmp_path / "keep.txt"
    marker.write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        run_collection(tmp_path)
    assert marker.read_text(encoding="utf-8") == "existing"
    assert list(tmp_path.iterdir()) == [marker]


def test_import_creates_no_output_and_makes_no_request(monkeypatch):
    import scripts.p61_collect_observations as collector

    def forbidden(*args, **kwargs):
        pytest.fail("import must not create directories or make HTTP requests")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(collector.Path, "mkdir", forbidden)
    runpy.run_path(collector.__file__, run_name="p61_import_safety_test")
