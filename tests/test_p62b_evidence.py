"""P6.2b evidence and Runtime tests use response fixtures, never live services."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import socket
import time

import pytest
import requests

from scripts import p61_collect_observations
from scripts.p61_readonly_integration import run_case
from scripts.p62b_evidence import (
    collect_readonly, correlate_logs, run_runtime_replay, summarize_alerts,
    verify_capture,
)


START = "2026-10-18T08:00:00+00:00"
END = "2026-10-18T08:01:00+00:00"
LOKI_END = "2026-10-18T08:01:10+00:00"


def alert(service="order-service", state="firing"):
    return {
        "state": state,
        "labels": {"service": service, "alertname": "HighHttp5xxRate",
                   "metric": "http_5xx_rate", "signal_type": "numeric",
                   "severity": "critical"},
        "annotations": {"threshold": "0.2", "unit": "ratio",
                        "direction": "higher_is_worse", "window": "1m",
                        "minimum_requests": "20"},
        "activeAt": START,
        "value": "0.5",
    }


@pytest.fixture
def collection(tmp_path, monkeypatch):
    calls = []

    def create(*, state="firing", missing=False):
        def send(session, method, url, **kwargs):
            calls.append((method, url, deepcopy(kwargs)))
            assert method == "GET"
            assert not url.endswith(("/error", "/order"))
            assert kwargs["allow_redirects"] is False
            assert session.trust_env is False
            if url.endswith("/alerts"):
                items = [alert("inventory-service")]
                if state == "malformed":
                    items.extend([alert(), None])
                elif state != "inactive":
                    items.append(alert(state=state))
                body = {"status": "success", "data": {"alerts": items}}
            elif url.endswith("/rules"):
                body = {"status": "success", "data": {"groups": [{
                    "name": "demo-alerts", "lastEvaluation": START,
                    "evaluationTime": 0.001,
                    "rules": [{"name": "HighHttp5xxRate", "state": "firing",
                               "lastEvaluation": START, "evaluationTime": 0.0001,
                               "alerts": [alert("inventory-service")]}],
                }]}}
            elif url.endswith("/query"):
                body = {"status": "success", "data": {
                    "resultType": "vector",
                    "result": [] if missing else [{"metric": {}, "value": [time.time(), "1"]}],
                }}
            elif url.endswith("/query_range"):
                body = {"status": "success", "data": {
                    "resultType": "streams", "result": [] if missing else [{
                        "stream": {"service": "order-service"},
                        "values": [["1792310430000000000",
                                    "2026-10-18 17:00:30 ERROR service=order-service status=500 request failed"]],
                    }],
                }}
            elif url.endswith("/version"):
                body = {"service": "order-service", "commit_sha": "a" * 40,
                        "process_started_at": START,
                        "runtime_flags": {"simulate_code_regression": False,
                                          "simulate_slow_request": False}}
            else:
                raise AssertionError(url)
            response = requests.Response()
            response.status_code = 200
            response.url = requests.Request("GET", url, params=kwargs.get("params")).prepare().url
            response.raw = io.BytesIO(json.dumps(body).encode("utf-8"))
            return response

        monkeypatch.setattr(requests.sessions.Session, "request", send)
        target = tmp_path / ("capture-" + str(len(calls)))
        report = collect_readonly(
            target, START, END, confirmed_local_isolation=True,
            log_utc_offset="+09:00",
        )
        return target, report, calls

    return create


def test_collection_requires_explicit_isolation_before_any_io(tmp_path, monkeypatch):
    monkeypatch.setattr(requests.sessions.Session, "request", lambda *a, **k: pytest.fail("network"))
    with pytest.raises(ValueError, match="verified_local_isolation_required"):
        collect_readonly(tmp_path / "absent", START, END)
    assert not (tmp_path / "absent").exists()


def test_capture_preserves_full_snapshots_fields_hashes_and_explicit_window(collection):
    previous = deepcopy(p61_collect_observations.ALLOWED_PATHS)
    target, report, calls = collection()
    verified = verify_capture(target)
    assert report == verified
    assert p61_collect_observations.ALLOWED_PATHS == previous
    assert len(calls) == 9
    assert report["business_route_requests"] == report["real_model_sdk_requests"] == 0
    assert report["alert_summary"]["order_service_state"] == "firing"
    assert report["alert_summary"]["order_service_alerts"][0]["activeAt"] == START
    rules = report["alert_summary"]["high_http_5xx_rules"][0]
    assert rules["rule"]["evaluationTime"] == 0.0001
    assert rules["rule"]["lastEvaluation"] == START
    full = report["prometheus_snapshots"]["alerts"]["result"]["data"]["alerts"]
    assert {row["labels"]["service"] for row in full} == {"order-service", "inventory-service"}
    log = report["observations"]["query_logs"]
    assert log["arguments"] == {"service": "order-service", "start": START, "end": LOKI_END}
    assert report["fault_window"] == {"start": START, "end": END}
    assert report["loki_query_window"] == {
        "start": START, "end": LOKI_END, "bounds": "[start, end)",
    }
    assert report["loki_ingestion_grace_seconds"] == 10
    assert log["result"]["query_range"]["mode"] == "explicit"
    assert log["result"]["evidence_scope"] == "time_window"
    for row in report["http_requests"]:
        assert row["raw_body_sha256"] == hashlib.sha256((target / row["raw_body_file"]).read_bytes()).hexdigest()


@pytest.mark.parametrize("state", ["inactive", "pending", "firing"])
def test_service_alert_state_does_not_inherit_inventory_rule_firing(state):
    items = [alert("inventory-service")]
    if state != "inactive":
        items.append(alert(state=state))
    result = summarize_alerts({"status": "success", "data": {"alerts": items}}, None)
    assert result["order_service_state"] == state
    assert all(row["labels"]["service"] == "order-service" for row in result["order_service_alerts"])


def test_missing_snapshots_are_unknown_and_no_timestamps_are_invented():
    result = summarize_alerts(None, None)
    assert result["order_service_state"] == "unknown"
    assert result["high_http_5xx_rules"] == []
    assert result["order_service_alerts"] == []


@pytest.mark.parametrize("items", [[None], [{}], [alert("inventory-service"), None]])
def test_malformed_alert_entries_cannot_prove_inactive(items):
    result = summarize_alerts({"status": "success", "data": {"alerts": items}}, None)
    assert result["order_service_state"] == "unknown"
    assert result["state_basis"] == "no_data"
    assert result["snapshot_complete"] is False


def test_log_event_time_and_loki_time_are_distinct_and_history_excluded():
    result = {"status": "ok", "query_range": {
        "mode": "explicit", "start": START, "end": LOKI_END,
    }, "logs": [{
        "timestamp": "1792310430000000000", "timestamp_utc": "2026-10-18T08:00:30Z",
        "labels": {"service": "order-service"},
        "line": "2026-10-18 17:00:30 ERROR service=order-service status=500 request failed",
    }, {
        "timestamp": "1792310430000000000", "timestamp_utc": "2026-10-18T08:00:30Z",
        "labels": {"service": "order-service"},
        "line": "2026-10-17 17:00:30 ERROR service=order-service status=500 request failed",
    }]}
    report = correlate_logs(result, START, END, log_utc_offset="+09:00")
    assert report["candidate_count"] == 1
    assert report["entries"][0]["body_event_time_text"] == "2026-10-18 17:00:30"
    assert report["entries"][0]["body_event_time_utc"] == "2026-10-18T08:00:30+00:00"
    assert report["entries"][0]["route_identity"] == "not_present_in_demo_error_log"
    assert report["entries"][0]["request_identity"] == "no_shared_request_id"
    assert correlate_logs(result, START, END)["candidate_count"] == 0


@pytest.mark.parametrize("body_time,loki_time,expected", [
    ("2026-10-18 17:00:59", "2026-10-18T08:01:05+00:00", 1),
    ("2026-10-18 17:00:59", LOKI_END, 0),
    ("2026-10-18 17:01:02", "2026-10-18T08:01:05+00:00", 0),
])
def test_loki_ingestion_grace_does_not_extend_fault_event_window(
    body_time, loki_time, expected,
):
    result = {"status": "ok", "query_range": {
        "mode": "explicit", "start": START, "end": LOKI_END,
    }, "logs": [{
        "timestamp": str(int(datetime.fromisoformat(loki_time).timestamp()) * 10**9),
        "timestamp_utc": loki_time,
        "labels": {"service": "order-service"},
        "line": body_time + " ERROR service=order-service status=500 request failed",
    }]}
    report = correlate_logs(result, START, END, log_utc_offset="+09:00")
    assert report["candidate_count"] == expected
    assert report["fault_window"]["end"] == END
    assert report["loki_query_window"]["end"] == LOKI_END
    result.pop("query_range")
    assert correlate_logs(result, START, END, log_utc_offset="+09:00")["candidate_count"] == 0


def test_resealed_capture_cannot_expand_ingestion_grace(collection):
    target, report, _ = collection()
    report["loki_query_window"]["end"] = "2026-10-18T08:01:11+00:00"
    report_file = target / "collection_report.json"
    report_file.write_text(json.dumps(report), encoding="utf-8")
    manifest_file = target / "sha256.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest[report_file.name] = hashlib.sha256(report_file.read_bytes()).hexdigest()
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="loki_query_window_must_match_fixed_ingestion_grace"):
        verify_capture(target)


def test_capture_tampering_is_rejected_without_network(collection, monkeypatch):
    target, _, _ = collection()
    (target / "http-001.body").write_bytes(b"changed")
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network"))
    with pytest.raises(ValueError, match="evidence_hash_mismatch"):
        verify_capture(target)


def test_resealed_result_cannot_disagree_with_raw_original(collection):
    target, report, _ = collection()
    report["observations"]["query_metrics"]["result"]["value"] = 0.123
    report_file = target / "collection_report.json"
    report_file.write_text(json.dumps(report), encoding="utf-8")
    manifest_file = target / "sha256.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest[report_file.name] = hashlib.sha256(report_file.read_bytes()).hexdigest()
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="parsed_result_does_not_match_raw_original"):
        verify_capture(target)


def test_resealed_collection_time_must_contain_raw_request_interval(collection):
    target, report, _ = collection()
    report["observations"]["query_logs"]["collected_at"] = START
    report_file = target / "collection_report.json"
    report_file.write_text(json.dumps(report), encoding="utf-8")
    manifest_file = target / "sha256.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest[report_file.name] = hashlib.sha256(report_file.read_bytes()).hexdigest()
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="observation_source_or_time_mismatch"):
        verify_capture(target)


@pytest.mark.parametrize("mutation,reason", [
    ("promql", "observation_request_mismatch"),
    ("url", "observation_request_mismatch"),
    ("response_url", "captured_response_url_mismatch"),
    ("key_binding", "observation_tool_or_arguments_mismatch"),
    ("arguments", "observation_tool_or_arguments_mismatch"),
])
def test_resealed_metric_cannot_change_request_or_observation_identity(
    collection, mutation, reason,
):
    target, report, _ = collection()
    observation = report["observations"]["query_metrics"]
    row = next(item for item in report["http_requests"]
               if item["id"] == observation["request_ids"][0])
    if mutation == "promql":
        row["query_parameters"]["query"] = '{service="inventory-service"}'
    elif mutation == "url":
        row["url"] = "http://localhost:9090/api/v1/rules"
        observation["source"] = row["url"]
    elif mutation == "response_url":
        row["response_url"] = requests.Request(
            "GET", row["url"], params={"query": '{service="inventory-service"}'},
        ).prepare().url
    elif mutation == "key_binding":
        observations = report["observations"]
        observations["query_metrics"], observations["http_requests_total"] = (
            observations["http_requests_total"], observations["query_metrics"],
        )
    else:
        observation["arguments"]["metric"] = "http_requests_total"
    if mutation != "response_url":
        row["response_url"] = requests.Request(
            "GET", row["url"], params=row["query_parameters"],
        ).prepare().url
    (target / (row["id"] + ".json")).write_text(json.dumps(row), encoding="utf-8")
    (target / "collection_report.json").write_text(json.dumps(report), encoding="utf-8")
    manifest = {
        item.name: hashlib.sha256(item.read_bytes()).hexdigest()
        for item in target.iterdir() if item.name != "sha256.json"
    }
    (target / "sha256.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match=reason):
        verify_capture(target)


@pytest.mark.parametrize("state,origin", [
    ("firing", "real_firing_alert_snapshot_replay"),
    ("pending", "synthetic_alert_input"),
    ("inactive", "synthetic_alert_input"),
    ("malformed", "synthetic_alert_input"),
])
def test_existing_runtime_distinguishes_real_firing_from_synthetic_and_never_renews(
    tmp_path, collection, state, origin,
):
    target, captured, _ = collection(state=state, missing=True)
    original = deepcopy(captured)
    report = run_runtime_replay(tmp_path / "replay", collection_dir=target)
    assert report["alert_input_origin"] == origin
    assert report["session"]["status"] == "COMPLETED"
    assert report["real_model_sdk_requests"] == report["remediation_executions"] == 0
    assert report["business_route_requests"] == 0
    assert report["temporary_store_isolation"] is True
    assert report["captured_observations"] == original["observations"]
    assert len(report["full_real_alert_snapshot"]["data"]["alerts"]) >= 1
    assert report["model_origin"] == "scripted_mock_sdk_not_real_deepseek"
    assert report["log_query_scope"] == "explicit"
    assert report["log_query_window"] == {
        "start": START, "end": LOKI_END, "bounds": "[start, end)",
    }
    assert report["fault_window"] == original["fault_window"]
    assert report["loki_query_window"] == original["loki_query_window"]
    runtime = json.loads(
        (tmp_path / "replay" / "runtime" / "report.json").read_text(encoding="utf-8"),
    )
    assert runtime["alert_window"] == report["log_query_window"]
    assert "明确的起止时间" in runtime["investigation_report"]
    assert "recent" not in runtime["investigation_report"]
    if state == "firing":
        observed_at = report["incident"]["alerts"][-1]["observed_at"]
        collected_at = original["prometheus_snapshots"]["alerts"]["collected_at"]
        assert datetime.fromisoformat(
            observed_at.replace("Z", "+00:00"),
        ) == datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        starts_at = report["incident"]["alerts"][-1]["starts_at"]
        assert datetime.fromisoformat(
            starts_at.replace("Z", "+00:00"),
        ) == datetime.fromisoformat(START)
    assert not report["business_recovery_verified"]
    records = report["checkpoint"]["records"]
    assert all(item["result"]["status"] == "no_data" for item in records)
    for item in records:
        assert item["result"]["use_for_current_state"] is False
        if "captured_at" in item["result"]:
            assert item["result"]["captured_at"] == original["observations"][item["name"]]["collected_at"]
    assert verify_capture(target) == original


def test_default_runtime_is_offline_synthetic_and_missing_evidence_remains_no_data(tmp_path):
    report = run_runtime_replay(tmp_path / "default")
    assert report["alert_input_origin"] == "synthetic_alert_input"
    assert report["full_real_alert_snapshot"] is None
    assert report["incident"]["evidence"] == []
    assert all(row["result"]["reason"] == "observation_not_collected"
               for row in report["checkpoint"]["records"])


@pytest.mark.parametrize("snapshot", [
    [],
    {"status": "success", "data": {"alerts": []}},
])
def test_empty_captured_alert_cannot_be_replaced_with_synthetic(tmp_path, snapshot):
    target = tmp_path / "rejected"
    with pytest.raises(ValueError, match="captured_alert_snapshot_requires"):
        run_case(
            target,
            observations={},
            alert_snapshot=snapshot,
            alert_source="http://localhost:9090/api/v1/alerts",
            alert_collected_at=START,
        )
    assert not target.exists()
