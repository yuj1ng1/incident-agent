"""Log summaries through the Worker; model and Loki responses stay isolated."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from investigation.models import SessionStatus as S
from investigation.worker import Worker
from runtime_support import FixtureAgent, SHA, call, create_runtime, create_session, response
from tools.logs import query_logs, timestamp_to_utc


def logs_result(count=6):
    seconds = int(datetime(2026, 10, 11, 8, tzinfo=timezone.utc).timestamp())
    logs = []
    for index in range(count, 0, -1):
        timestamp = str((seconds + index) * 1_000_000_000 + 123456789)
        line = (
            "path=/order status=500 error_type=application_error "
            "exception_type=RuntimeError"
            if index > 3 else "status=500 message=HTTP_server_error"
        )
        logs.append({
            "timestamp": timestamp,
            "timestamp_utc": timestamp_to_utc(timestamp),
            "line": f"{line} request_id=request-{index}",
            "labels": {"service": "order-service"},
        })
    return {"status": "ok", "service": "order-service", "logs": logs}


def test_log_summary_keeps_newest_three_and_preserves_input():
    result = logs_result()
    original = deepcopy(result)

    summary = Worker._observation_summary(result, tool_name="query_logs")

    assert summary["logs"] == original["logs"][:3]
    assert summary["logs"] != original["logs"][-3:]
    assert summary["status"] == "ok"
    assert summary["service"] == "order-service"
    assert summary["summary_truncated"] is True
    assert result == original


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_short_log_summary_keeps_all_without_truncation(count):
    result = logs_result(count)

    assert Worker._observation_summary(result, tool_name="query_logs") == result


def test_log_summary_still_marks_truncated_line():
    result = logs_result(1)
    result["logs"][0]["line"] = "x" * 1001

    summary = Worker._observation_summary(result, tool_name="query_logs")

    assert summary["summary_truncated"] is True
    assert summary["logs"][0]["line"] == "x" * 1000 + " [summary truncated]"
    assert summary["logs"][0]["timestamp_utc"] == result["logs"][0]["timestamp_utc"]


@pytest.mark.parametrize("tool_name", [None, "query_metrics", "get_recent_code_changes"])
def test_other_tool_lists_keep_tail_even_when_named_logs(tool_name):
    result = {"status": "ok", "logs": list(range(6)), "changes": list(range(6))}

    summary = Worker._observation_summary(result, tool_name=tool_name)

    assert summary["logs"] == [3, 4, 5]
    assert summary["changes"] == [3, 4, 5]
    assert summary["summary_truncated"] is True


def test_query_logs_only_changes_top_level_logs_list():
    result = logs_result() | {
        "other": list(range(6)),
        "metadata": {"logs": list(range(6))},
    }

    summary = Worker._observation_summary(result, tool_name="query_logs")

    assert summary["logs"] == result["logs"][:3]
    assert summary["other"] == [3, 4, 5]
    assert summary["metadata"]["logs"] == [3, 4, 5]


@pytest.mark.parametrize("age_seconds", [0, 31], ids=["current", "expired"])
def test_worker_model_context_uses_newest_logs_and_persists_full_evidence(tmp_path, age_seconds):
    clock = SimpleNamespace(now=datetime.now(timezone.utc))
    manager = create_runtime(tmp_path, clock=lambda: clock.now)
    session = create_session(manager)
    expected_logs = logs_result()["logs"]
    changes = [{"sha": str(index), "message": f"change-{index}"} for index in range(6)]
    loki_response = {"data": {"result": [{
        "stream": {"service": "order-service"},
        "values": [[log["timestamp"], log["line"]] for log in reversed(expected_logs)],
    }]}}

    def tool_result(item):
        if item.name == "query_logs":
            return query_logs("order-service")
        if item.name == "get_recent_code_changes":
            return {"status": "ok", "service": "order-service", "changes": changes}
        return {
            "status": "ok", "service": "order-service", "commit_sha": SHA,
            "process_started_at": "2026-10-11T00:00:00Z",
        }

    calls = [
        call("query_logs", {"service": "order-service"}, "logs"),
        call("get_recent_code_changes", {"service": "order-service"}, "changes"),
    ]
    agent = FixtureAgent([response(answer="", calls=calls), response()], tool_result)

    def expire_logs(phase, session_id):
        if phase == "tool_checkpoint_committed" and agent.executions[-1] == "query_logs":
            clock.now += timedelta(seconds=age_seconds)

    with patch("tools.logs.loki_client.query_range", return_value=loki_response), patch.object(
        agent, "build_incident_prompt", wraps=agent.build_incident_prompt,
    ) as build_prompt:
        completed = Worker(manager, agent, expire_logs).run_once()

    assert completed.status == S.COMPLETED
    assert agent.request_durable_response.call_count == 2
    context = agent.request_durable_response.call_args_list[1].args[0][0]["content"]
    rebuilt = json.loads(context.rsplit("\n", 1)[1])
    pairs = {item["name"]: item for item in rebuilt["recent_function_call_results"]}
    assert pairs["query_logs"]["result"]["logs"] == expected_logs[:3]
    assert pairs["query_logs"]["result"]["summary_truncated"] is True
    assert pairs["query_logs"]["use_for_current_state"] == (age_seconds == 0)
    assert pairs["get_recent_code_changes"]["result"]["changes"] == changes[-3:]
    for log in expected_logs[:3]:
        assert log["line"] in context
    for log in expected_logs[3:]:
        assert log["line"] not in context

    prompt_incident = build_prompt.call_args_list[1].args[0]
    current_logs = [item for item in prompt_incident.evidence if item.source == "query_logs"]
    current_changes = next(item for item in prompt_incident.evidence if item.source == "get_recent_code_changes")
    assert json.loads(current_changes.detail)["changes"] == changes
    if age_seconds == 0:
        assert len(current_logs) == 1
        detail = json.loads(current_logs[0].detail)
        assert detail["logs"] == expected_logs[:3]
        assert detail["summary_truncated"] is True
        assert rebuilt["historical_observations"] == []
    else:
        assert current_logs == []
        historical = rebuilt["historical_observations"]
        # The version probe's 15-second TTL also expires during the 31-second jump.
        assert len(historical) == 2
        assert {item["name"] for item in historical} == {"get_running_version", "query_logs"}
        log_observation = next(item for item in historical if item["name"] == "query_logs")
        assert log_observation["result"]["logs"] == expected_logs[:3]
        assert log_observation["result"]["summary_truncated"] is True
        assert log_observation["expired"] is True
        assert log_observation["use_for_current_state"] is False

    checkpoint = manager.store.load_checkpoint(completed)
    record = next(item for item in checkpoint["records"] if item["name"] == "query_logs")
    assert record["result"]["logs"] == expected_logs
    assert "summary_truncated" not in record["result"]
    assert pairs["query_logs"]["observation_id"] == record["observation_id"]
    incident = manager.incident_store.get_incident(session.incident_id)
    saved_logs = next(item for item in incident.evidence if item.source == "query_logs")
    assert json.loads(saved_logs.detail)["logs"] == expected_logs
    assert changes == [{"sha": str(index), "message": f"change-{index}"} for index in range(6)]
