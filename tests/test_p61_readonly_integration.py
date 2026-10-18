"""P6.1 transport-fixture integration; every application layer stays real."""
import hashlib
import json
import logging

import pytest

from scripts.p61_readonly_integration import run_case


def records(report, name):
    return [item for item in report["checkpoint"]["records"] if item["name"] == name]


@pytest.fixture
def run(tmp_path):
    def invoke(scenario="offline_ok"):
        return run_case(tmp_path / scenario, scenario)

    return invoke


def test_complete_monitor_runtime_tools_and_reopened_artifacts(run, tmp_path):
    report = run()
    assert report["session"]["status"] == "COMPLETED"
    assert report["session"]["total_model_requests"] == 2
    assert report["session"]["total_tool_executions"] == 4
    assert records(report, "query_metrics")[0]["result"]["value"] == 0.27
    assert records(report, "query_logs")[0]["result"]["evidence_scope"] == "recent"
    assert report["incident"]["alerts"][-1]["starts_at"] is None
    assert report["real_model_sdk_requests"] == report["real_service_observations"] == 0
    assert report["business_state"] == "unknown"
    assert all(report["checks"].values())
    assert report["authorization"]["forbidden_io_attempts"] == []
    assert not report["authorization"]["remediation_execution"]
    assert all(not row["actual_network_request"] for row in report["http_fixtures"])
    assert {row["source"] for row in report["http_fixtures"]} == {
        "synthetic_alert", "mock_prometheus", "mock_loki",
        "mock_health", "mock_version",
    }
    assert all(
        row["query_started_at"] <= row["query_finished_at"]
        for row in report["http_fixtures"]
    )
    root = tmp_path / "offline_ok"
    assert json.loads((root / "report.json").read_text(encoding="utf-8")) == report
    manifest = json.loads((root / "sha256.json").read_text(encoding="utf-8"))
    assert len(manifest) == 3
    for name, digest in manifest.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest


def test_no_data_does_not_become_zero_but_finite_zero_is_valid(run):
    missing = records(run("metrics_no_data"), "query_metrics")[0]["result"]
    zero = records(run("metric_zero"), "query_metrics")[0]["result"]
    assert missing["status"] == "no_data" and "value" not in missing
    assert zero["status"] == "ok" and zero["value"] == 0.0


@pytest.mark.parametrize("scenario,failed,working", [
    ("prometheus_unavailable", "query_metrics", "query_logs"),
    ("loki_unavailable", "query_logs", "query_metrics"),
])
def test_single_source_failure_preserves_missing_status_and_other_evidence(
    run,
    scenario,
    failed,
    working,
):
    report = run(scenario)
    assert records(report, failed)[0]["result"]["status"] == "no_data"
    assert records(report, working)[0]["result"]["status"] == "ok"
    assert report["session"]["status"] == "COMPLETED"
    assert not report["business_recovery_verified"]


def test_health_ok_does_not_verify_business_or_clear_synthetic_alert(run):
    report = run()
    result = records(report, "check_service_health")[0]["result"]
    assert result["status"] == "ok" and result["health"] == "ok"
    assert report["incident"]["alert_state"] == "firing"
    assert report["incident"]["recovery_verified_at"] is None
    assert report["business_state"] == "unknown"


def test_worker_rejects_repair_and_target_outside_session_before_dispatch(run):
    report = run("permissions")
    rejected = [row for row in report["events"] if row["event_type"] == "tool_rejected"]
    assert len(rejected) == 2
    assert report["session"]["total_tool_executions"] == 4
    assert not records(report, "execute_remediation")
    assert {row["data"]["tool"] for row in rejected} == {
        "execute_remediation", "check_service_health",
    }
    assert all("inventory" not in row["url"] for row in report["http_fixtures"])


def test_original_thirteen_tool_limit_counts_runtime_version_probe(run):
    report = run("tool_budget")
    assert report["session"]["total_tool_executions"] == 13
    assert len(records(report, "get_running_version")) == 1
    assert len(records(report, "query_metrics")) == 12
    rejected = [
        row for row in report["events"]
        if row["event_type"] == "tool_budget_rejected"
    ]
    assert len(rejected) == 2
    assert report["mock_model_requests"][-1]["body"]["tools"] == []


def test_original_seven_model_limit_stops_before_eighth_mock_request(run):
    report = run("model_budget")
    assert report["session"]["status"] == "EXHAUSTED"
    assert report["session"]["total_model_requests"] == 7
    assert len(report["mock_model_requests"]) == 7
    assert report["mock_model_requests"][-1]["body"]["tools"] == []
    assert report["checkpoint"]["result"]["success"] is False
    assert not report["business_recovery_verified"]


@pytest.mark.parametrize("scenario,status", [
    ("model_failure", "FAILED"),
    ("model_incomplete", "WAITING_RETRY"),
])
def test_explanation_failure_retains_tools_and_never_claims_recovery(
    run,
    scenario,
    status,
):
    report = run(scenario)
    assert report["session"]["status"] == status
    assert len(report["checkpoint"]["records"]) == 4
    assert not report["checkpoint"].get("result", {}).get("success")
    assert report["incident"]["alert_state"] == "firing"
    assert report["incident"]["recovery_verified_at"] is None
    assert report["authorization"]["background_tasks_started"] == 0


def test_existing_output_is_rejected_without_overwriting(tmp_path):
    target = tmp_path / "keep"
    target.mkdir()
    original = target / "existing.json"
    original.write_text("preserve", encoding="utf-8")
    with pytest.raises(FileExistsError):
        run_case(target)
    assert original.read_text(encoding="utf-8") == "preserve"
    assert list(target.iterdir()) == [original]


def test_captured_replay_preserves_original_times_and_recent_scope(tmp_path):
    observations = {
        "query_logs": {
            "arguments": {"service": "order-service"},
            "result": {
                "status": "ok",
                "service": "order-service",
                "evidence_scope": "recent",
                "query_range": {"mode": "recent"},
                "logs": [{"line": "captured fixture, not a new observation"}],
            },
            "collected_at": "2026-10-01T00:00:00+00:00",
            "source": "http://localhost:3100/loki/api/v1/query_range",
        },
    }
    before = json.dumps(observations, sort_keys=True)
    report = run_case(tmp_path / "replay", observations=observations)
    assert json.dumps(observations, sort_keys=True) == before
    assert report["captured_observations"] == observations
    result = records(report, "query_logs")[0]["result"]
    assert result["captured_at"] == observations["query_logs"]["collected_at"]
    assert result["captured_result"] == observations["query_logs"]["result"]
    assert result["captured_result"]["evidence_scope"] == "recent"
    assert result["status"] == "no_data" and result["use_for_current_state"] is False
    assert report["scenario"] == "captured_observation_replay"
    assert {row["source"] for row in report["http_fixtures"]} == {"synthetic_alert"}


def test_missing_captured_source_does_not_fall_back_to_mock_provider(tmp_path):
    report = run_case(tmp_path / "missing", observations={})
    assert len(report["checkpoint"]["records"]) == 4
    assert all(item["result"]["reason"] == "observation_not_collected"
               for item in report["checkpoint"]["records"])
    assert all(item["result"]["status"] == "no_data"
               for item in report["checkpoint"]["records"])
    assert {row["source"] for row in report["http_fixtures"]} == {"synthetic_alert"}
    assert report["incident"]["evidence"] == []
    assert not report["business_recovery_verified"]


def test_existing_application_log_handler_is_preserved(tmp_path, monkeypatch):
    operator_log = tmp_path / "operator.log"
    operator_log.write_text("preserve existing log\n", encoding="utf-8")
    logger = logging.getLogger("agent")
    handler = logging.FileHandler(operator_log, encoding="utf-8")
    try:
        monkeypatch.setattr(logger, "handlers", [handler])
        run_case(tmp_path / "isolated")
        assert logger.handlers == [handler]
    finally:
        handler.close()
    assert operator_log.read_text(encoding="utf-8") == "preserve existing log\n"
    assert (tmp_path / "isolated" / "agent.log").is_file()


def test_root_log_handler_is_isolated_and_propagation_restored(tmp_path, monkeypatch):
    root_logger = logging.getLogger()
    agent_logger = logging.getLogger("agent")
    root_log = tmp_path / "root.log"
    root_log.write_text("preserve root log\n", encoding="utf-8")
    handler = logging.FileHandler(root_log, encoding="utf-8")
    try:
        monkeypatch.setattr(root_logger, "handlers", [handler])
        monkeypatch.setattr(agent_logger, "propagate", True)
        run_case(tmp_path / "isolated-root")
        assert agent_logger.propagate is True
        assert root_logger.handlers == [handler]
    finally:
        handler.close()
    assert root_log.read_text(encoding="utf-8") == "preserve root log\n"
