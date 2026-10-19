"""New coverage is multi-round composition; existing P6.1 tests own budgets."""
import hashlib
import json

import pytest

from scripts.p64_offline_investigation import (
    MOCK_ANSWER,
    UNSUPPORTED_ANSWER,
    run_case,
)
from scripts.p61_readonly_integration import BoundaryViolation


def records(report, name):
    return [row for row in report["checkpoint"]["records"] if row["name"] == name]


def context(report, index):
    content = report["mock_model_requests"][index]["body"]["input"][0]["content"]
    return json.loads(content[content.rfind('\n{"recovery":') + 1:])


def test_three_rounds_real_worker_modelclient_toolmanager_and_saved_boundaries(tmp_path):
    root = tmp_path / "multi"
    report = run_case(root)
    assert report["session"]["status"] == "COMPLETED"
    assert report["session"]["total_model_requests"] == 3
    assert report["session"]["total_tool_executions"] == 3
    calls = [
        [item["name"] for item in request["response"]["output"]
         if item["type"] == "function_call"]
        for request in report["mock_model_requests"]
    ]
    assert calls == [["query_metrics"], ["query_logs"], []]
    assert context(report, 1)["recent_function_call_results"][0]["call_id"] == "p64-metric"
    boundaries = json.loads((root / "worker_boundaries.json").read_text(encoding="utf-8"))
    # Unverified providers use B reconstruction, so the final Checkpoint is
    # not a provider-verified complete Responses chain. Earlier committed
    # call/output pairs remain independently auditable in these snapshots.
    rounds = {
        row["session"]["total_model_requests"]: row["checkpoint"]
        for row in boundaries if row["boundary"] == "model_before_io"
    }
    for index, identifier in ((2, "p64-metric"), (3, "p64-logs")):
        items = [
            (row["type"], row["call_id"])
            for row in rounds[index]["history"]
            if row.get("type", "").startswith("function_call")
        ]
        assert items == [
            ("function_call", identifier), ("function_call_output", identifier),
        ]
    third_input = report["mock_model_requests"][2]["body"]["input"][0]["content"]
    for name in ("query_metrics", "query_logs"):
        assert records(report, name)[0]["observation_id"] in third_input
    assert {row["checkpoint"]["phase"] for row in boundaries} == {
        "model_started", "response_saved", "tool_started", "tool_saved",
    }
    assert any(row["event_type"] == "investigation_completed" for row in report["events"])
    assert report["checkpoint"]["result"]["answer"] == MOCK_ANSWER
    assert report["result_trust"]["semantic_status"] == "pending_review"
    assert not report["result_trust"]["trusted"]
    assert report["human_approval"] == "not_recorded"
    assert report["real_model_requests"] == report["real_service_requests"] == 0
    manifest = json.loads((root / "sha256.json").read_text(encoding="utf-8"))
    for name, digest in manifest.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest


def test_no_data_reaches_next_model_without_fabricated_metric_value(tmp_path):
    report = run_case(tmp_path / "missing", "no_data")
    result = records(report, "query_metrics")[0]["result"]
    assert result["status"] == "no_data" and "value" not in result
    received = context(report, 1)["recent_function_call_results"][0]["result"]
    assert received == result
    assert report["session"]["status"] == "COMPLETED"
    assert report["result_trust"]["semantic_status"] == "pending_review"


def test_tool_exception_preserves_failure_through_next_round_and_checkpoint(tmp_path):
    report = run_case(tmp_path / "failed-tool", "tool_failure")
    failed = records(report, "query_logs")[0]
    assert failed["success"] is False
    assert failed["result"] == {"status": "tool_error", "reason": "RuntimeError"}
    received = next(row for row in context(report, 2)["recent_function_call_results"]
                    if row["call_id"] == "p64-logs")
    assert received["result"] == failed["result"]
    assert received["use_for_current_state"] is False
    assert any(row["event_type"] == "tool_failed" for row in report["events"])


def test_rejections_across_rounds_never_dispatch_foreign_illegal_or_repair(tmp_path):
    report = run_case(tmp_path / "permissions", "permissions")
    rejected = {row["data"]["tool"] for row in report["events"]
                if row["event_type"] == "tool_rejected"}
    assert rejected == {"unregistered_p64_tool", "query_metrics", "execute_remediation"}
    assert [row["name"] for row in report["checkpoint"]["records"]] == [
        "get_running_version", "query_metrics", "query_logs",
    ]
    assert report["session"]["total_tool_executions"] == 3
    assert report["remediation_executions"] == 0
    assert report["forbidden_io_attempts"] == []


def test_saved_tool_recovery_keeps_records_fences_old_claim_and_expires_metrics(tmp_path):
    report = run_case(tmp_path / "recovery", "saved_tool_recovery")
    recovery = report["recovery"]
    assert recovery["performed"] and recovery["old_claim_rejected"]
    assert recovery["clock_advance_only_in_isolated_store"]
    assert recovery["new_claim"]["fencing_token"] > recovery["old_claim"]["fencing_token"]
    assert len(records(report, "query_metrics")) == 1
    assert len(records(report, "get_running_version")) == 2
    interrupted = recovery["interrupted_checkpoint"]
    metric = records(report, "query_metrics")[0]
    assert interrupted["records"][-1] == metric
    assert interrupted["pending"][0]["call_id"] == "p64-metric"
    assert not any(row.get("type") == "function_call_output" and row["call_id"] == "p64-metric"
                   for row in interrupted["history"])
    historical = context(report, 1)["historical_observations"]
    old_metric = next(
        row for row in historical
        if row["observation_id"] == metric["observation_id"]
    )
    assert old_metric["expired"] and not old_metric["use_for_current_state"]
    assert metric["observation_id"] not in {
        row["observation_id"] for row in context(report, 1)["recent_function_call_results"]
    }
    assert report["session"]["total_tool_executions"] == 4
    assert report["session"]["total_model_requests"] == 3
    assert any(row["event_type"] == "unfinished_calls_abandoned_on_recovery"
               for row in report["events"])
    assert report["result_trust"]["semantic_status"] == "pending_review"


def test_unknown_version_remains_explicit_and_mock_cannot_establish_code_cause(tmp_path):
    report = run_case(tmp_path / "version", "unknown_version")
    version = records(report, "get_running_version")[0]
    assert version["result"]["status"] == "no_data"
    assert "commit_sha" not in version["result"]
    assert "unverified_instance" in report["checkpoint"]["identity"]
    assert not records(report, "get_source_file")
    assert report["result_trust"]["semantic_status"] == "pending_review"


def test_unsupported_answer_completes_but_never_becomes_accepted(tmp_path):
    report = run_case(tmp_path / "unsupported", "unsupported_final")
    assert report["session"]["status"] == "COMPLETED"
    assert report["checkpoint"]["result"]["answer"] == UNSUPPORTED_ANSWER
    assert report["result_trust"]["semantic_status"] == "pending_review"
    assert not report["result_trust"]["trusted"]
    assert report["business_recovery_verified"] is False


def test_existing_artifacts_are_never_reused_or_overwritten(tmp_path):
    target = tmp_path / "keep"
    target.mkdir()
    original = target / "original.txt"
    original.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        run_case(target)
    assert original.read_bytes() == b"original"
    assert list(target.iterdir()) == [original]


def test_mock_tool_cannot_open_even_loopback_network(tmp_path, monkeypatch):
    import socket
    from tools.tool_registry import tool_registry

    def attempted_network(**arguments):
        socket.create_connection(("127.0.0.1", 1))
        raise AssertionError("network_guard_did_not_stop_tool")

    monkeypatch.setitem(tool_registry["query_metrics"], "func", attempted_network)
    with pytest.raises(BoundaryViolation, match="external_io_forbidden"):
        run_case(tmp_path / "blocked-network")
