"""P6.2a provenance verification uses mocked HTTP and no model endpoint."""
from datetime import datetime, timezone
import hashlib
import io
import json
import runpy
import time

import pytest
import requests

from scripts.p61_collect_observations import run_collection
from scripts.p62a_verify_evidence import verify_collection


def _response(body):
    result = requests.Response()
    result.status_code = 200
    result.url = "http://localhost:9090/-/ready"
    result.raw = io.BytesIO(json.dumps(body).encode("utf-8"))
    return result


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def _seal(root):
    _write(root / "sha256.json", {
        item.name: hashlib.sha256(item.read_bytes()).hexdigest()
        for item in root.iterdir() if item.name != "sha256.json"
    })


@pytest.fixture
def collection(tmp_path, monkeypatch):
    runs = []

    def create(*, metric_missing=False, logs_unavailable=False):
        def send(session, method, url, **kwargs):
            assert method == "GET"

            def captured(body):
                response = _response(body)
                response.url = requests.Request(
                    "GET", url, params=kwargs.get("params"),
                ).prepare().url
                return response

            if url.endswith("/query"):
                return captured({
                    "status": "success",
                    "data": {"resultType": "vector", "result": [] if metric_missing else [{
                        "metric": {}, "value": [time.time(), "0"],
                    }]},
                })
            if url.endswith("/query_range"):
                if logs_unavailable:
                    raise requests.ConnectionError("mock_loki_unavailable")
                return captured({
                    "status": "success",
                    "data": {"resultType": "streams", "result": []},
                })
            if url.endswith("/health"):
                return captured({"health": "ok", "message": "offline fixture"})
            if url.endswith("/version"):
                return captured({
                    "service": "order-service", "commit_sha": "a" * 40,
                    "process_started_at": datetime.now(timezone.utc).isoformat(),
                    "runtime_flags": {
                        "simulate_code_regression": False,
                        "simulate_slow_request": False,
                    },
                })
            if url.endswith("/targets"):
                return captured({"status": "success", "data": {"activeTargets": []}})
            return captured({"ready": True})

        monkeypatch.setattr(requests.sessions.Session, "request", send)
        root = tmp_path / ("collection-" + str(len(runs)))
        run_collection(root)
        runs.append(root)

        def no_transport(*args, **kwargs):
            raise AssertionError("verification_must_not_send_http")

        monkeypatch.setattr(requests.sessions.Session, "request", no_transport)
        return root

    return create


def test_real_parsers_reproduce_captured_zero_and_empty_loki_without_io(collection):
    root = collection()
    original = {item.name: item.read_bytes() for item in root.iterdir()}
    result = verify_collection(root)
    assert result["verified"]
    assert result["http_record_count"] == 14
    assert len(result["observation_checks"]) == 10
    assert sum(row["raw_body_reparsed"] for row in result["observation_checks"]) == 4
    assert result["observations"]["query_metrics"]["result"]["value"] == 0.0
    logs = result["observations"]["query_logs"]["result"]
    assert logs["reason"] == "no_logs_in_query_range"
    assert logs["status"] == "no_data"
    assert not result["use_for_current_state"]
    assert not result["business_recovery_verified"]
    assert result["real_http_requests"] == result["real_model_sdk_requests"] == 0
    assert {item.name: item.read_bytes() for item in root.iterdir()} == original


def test_missing_metrics_and_loki_failure_remain_distinct_from_zero_or_empty(collection):
    result = verify_collection(collection(metric_missing=True, logs_unavailable=True))
    metric = result["observations"]["query_metrics"]["result"]
    assert metric["status"] == "no_data" and "value" not in metric
    logs = result["observations"]["query_logs"]["result"]
    assert logs["status"] == "no_data"
    assert logs["reason"] == "logs_unreachable_or_invalid"


def test_raw_hash_tampering_is_rejected_before_parsing(collection):
    root = collection()
    (root / "http-005.body").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="evidence_hash_mismatch"):
        verify_collection(root)


@pytest.mark.parametrize("mutation", ["source", "arguments", "request_ids", "result"])
def test_resealed_observation_mismatch_is_not_hidden_by_valid_hashes(collection, mutation):
    root = collection()
    path = root / "observations.json"
    observations = json.loads(path.read_text(encoding="utf-8"))
    metric = observations["query_metrics"]
    if mutation == "source":
        metric["source"] = "http://localhost:8001/order"
    elif mutation == "arguments":
        metric["arguments"]["metric"] = "service_scrape_up"
    elif mutation == "request_ids":
        metric["request_ids"] = ["http-006"]
    else:
        metric["result"]["value"] = 0.5
    _write(path, observations)
    _seal(root)
    with pytest.raises(ValueError, match="mismatch|does_not_match"):
        verify_collection(root)


def test_report_request_and_standalone_record_must_match(collection):
    root = collection()
    path = root / "collection_report.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["http_requests"][4]["query_parameters"] = {"query": "up"}
    _write(path, report)
    _seal(root)
    with pytest.raises(ValueError, match="http_record_report_mismatch"):
        verify_collection(root)


def test_response_url_cannot_claim_a_different_source(collection):
    root = collection()
    report_path = root / "collection_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    row = report["http_requests"][4]
    row["response_url"] = "http://localhost:8001/order"
    _write(root / "http-005.json", row)
    _write(report_path, report)
    _seal(root)
    with pytest.raises(ValueError, match="captured_response_url_mismatch"):
        verify_collection(root)


def test_original_query_time_cannot_be_refreshed_during_verification(collection):
    root = collection()
    path = root / "observations.json"
    observations = json.loads(path.read_text(encoding="utf-8"))
    observations["query_metrics"]["result"]["query_finished_at"] = "2030-01-01T00:00:00Z"
    _write(path, observations)
    _seal(root)
    with pytest.raises(ValueError, match="metric_result_time_mismatch"):
        verify_collection(root)


def test_supplemental_source_and_metric_arguments_are_checked(collection):
    root = collection()
    path = root / "supplemental_observations.json"
    observations = json.loads(path.read_text(encoding="utf-8"))
    observations[0]["arguments"]["service"] = "order-db"
    _write(path, observations)
    _seal(root)
    with pytest.raises(ValueError, match="unsupported_captured_metric"):
        verify_collection(root)


@pytest.mark.parametrize("unsafe", ["../outside.json", "..\\outside.json", "C:\\outside.json"])
def test_manifest_path_escape_is_refused(collection, unsafe):
    root = collection()
    manifest = json.loads((root / "sha256.json").read_text())
    manifest[unsafe] = "0" * 64
    _write(root / "sha256.json", manifest)
    with pytest.raises(ValueError):
        verify_collection(root)


def test_symlink_file_cannot_substitute_for_captured_original(collection, tmp_path):
    root = collection()
    body = root / "http-005.body"
    outside = tmp_path / "outside.body"
    outside.write_bytes(body.read_bytes())
    body.unlink()
    try:
        body.symlink_to(outside)
    except OSError as exc:
        pytest.skip("host does not permit creating the symlink fixture: " + type(exc).__name__)
    with pytest.raises(ValueError, match="unsafe_or_missing_evidence_file"):
        verify_collection(root)


def test_import_performs_no_file_access_or_http(monkeypatch):
    import scripts.p62a_verify_evidence as verifier

    def forbidden(*args, **kwargs):
        pytest.fail("import must not inspect evidence or perform I/O")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(verifier.Path, "read_text", forbidden)
    monkeypatch.setattr(verifier.Path, "mkdir", forbidden)
    runpy.run_path(verifier.__file__, run_name="p62a_verifier_import_test")
