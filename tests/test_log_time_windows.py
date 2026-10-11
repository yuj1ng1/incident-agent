"""UTC log windows through Loki, ToolManager and the durable Worker; no external I/O."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from clients.loki_client import LokiClient, parse_time_range, utc_to_nanoseconds
from investigation.evidence_policy import cache_key, effective_records, fresh
from investigation.models import SessionStatus as S
from investigation.state import RuntimeErrorBase
from investigation.worker import Worker
from runtime_support import FixtureAgent, SHA, call, create_runtime, create_session, response
from tools.logs import query_logs, timestamp_to_utc
from tools.tool_manager import ToolManager
from tools.tool_registry import tool_registry


BASE_NS = 1791705600000000000  # 2026-10-11T08:00:00Z
START = "2026-10-11T08:00:00.123456789Z"
END = "2026-10-11T08:10:00.123456790+00:00"
WINDOW = {"service": "order-service", "start": START, "end": END}
INVALID_WINDOWS = [
    ({"start": START}, "log_time_range_requires_start_and_end"),
    ({"end": END}, "log_time_range_requires_start_and_end"),
    ({"start": START, "end": None}, "log_time_range_requires_start_and_end"),
    ({"start": None, "end": END}, "log_time_range_requires_start_and_end"),
    ({"start": "", "end": END}, "invalid_log_time_format"),
    ({"start": "yesterday", "end": END}, "invalid_log_time_format"),
    ({"start": "2026-02-30T08:00:00Z", "end": END}, "invalid_log_time_format"),
    ({"start": "2026-10-11T08:00:00", "end": END}, "invalid_log_time_format"),
    ({"start": "2026-10-11", "end": END}, "invalid_log_time_format"),
    ({"start": "2026-10-11 08:00:00Z", "end": END}, "invalid_log_time_format"),
    ({"start": "2026-10-11T08:00:00+08:00", "end": END}, "invalid_log_time_format"),
    ({"start": "2026-10-11T08:00:00-00:00", "end": END}, "invalid_log_time_format"),
    ({"start": "2026-10-11T08:00:00.1234567890Z", "end": END}, "invalid_log_time_format"),
    ({"start": "2026-10-11T08:00:00Z ", "end": END}, "invalid_log_time_format"),
    ({"start": 1791705600, "end": END}, "invalid_log_time_format"),
    ({"start": START, "end": False}, "invalid_log_time_format"),
    ({"start": END, "end": START}, "log_time_range_start_not_before_end"),
    ({"start": START, "end": START}, "log_time_range_start_not_before_end"),
    ({"start": START, "end": "2026-10-12T08:00:00.123456790Z"}, "log_time_range_exceeds_24_hours"),
    ({"start": "3000-01-01T00:00:00Z", "end": "3000-01-01T00:01:00Z"}, "log_time_out_of_range"),
]


def loki_response(values=(), labels=None):
    return {"status": "success", "data": {"resultType": "streams", "result": [{
        "stream": labels or {"service": "order-service"},
        "values": [[str(stamp), line] for stamp, line in values],
    }]}}


@pytest.mark.parametrize("value, expected", [
    ("1970-01-01T00:00:00Z", 0),
    ("1970-01-01T00:00:01.1Z", 1_100_000_000),
    ("1970-01-01T00:00:01.123456Z", 1_123_456_000),
    ("1969-12-31T23:59:59.999999999Z", -1),
    (START, BASE_NS + 123456789),
    (END, BASE_NS + 600_123456790),
    ("2026-10-11T08:00:00.123456789+00:00", BASE_NS + 123456789),
    ("2262-04-11T23:47:16.854775807Z", 2**63 - 1),
])
def test_exact_nanosecond_conversion(value, expected):
    assert utc_to_nanoseconds(value) == expected
    assert utc_to_nanoseconds(timestamp_to_utc(expected)) == expected


def test_one_nanosecond_window_and_exactly_24_hours_are_valid():
    assert parse_time_range(START, "2026-10-11T08:00:00.123456790Z") == (
        BASE_NS + 123456789, BASE_NS + 123456790,
    )
    assert parse_time_range(START, "2026-10-12T08:00:00.123456789Z") == (
        BASE_NS + 123456789, BASE_NS + 86400_123456789,
    )


@pytest.mark.parametrize("arguments, reason", INVALID_WINDOWS)
def test_invalid_windows_return_errors_before_loki_io(arguments, reason):
    with patch("tools.logs.loki_client.query_range") as query:
        result = query_logs("order-service", **arguments)
    assert result["status"] == "invalid_arguments"
    assert result["reason"] == reason
    assert result["logs"] == []
    assert result["log_count"] == 0
    query.assert_not_called()


@pytest.mark.parametrize("arguments, reason", INVALID_WINDOWS)
def test_loki_client_rejects_invalid_windows_before_http(arguments, reason):
    with patch("clients.loki_client.requests.get") as get:
        with pytest.raises(ValueError, match=reason):
            LokiClient("http://loki").query_range('{service="order-service"}', **arguments)
    get.assert_not_called()


def test_loki_client_legacy_parameters_remain_compatible():
    http_response = Mock()
    http_response.json.return_value = {"data": {"result": []}}
    with patch("clients.loki_client.requests.get", return_value=http_response) as get:
        assert LokiClient("http://loki").query_range("selector") == http_response.json.return_value
    get.assert_called_once_with(
        "http://loki/loki/api/v1/query_range",
        params={"query": "selector", "limit": 50, "direction": "backward"}, timeout=5,
    )
    http_response.raise_for_status.assert_called_once_with()


def test_loki_request_carries_exact_integer_nanoseconds():
    with patch("clients.loki_client.requests.get") as get:
        LokiClient("http://loki").query_range("selector", limit=51, start=START, end=END)
    assert get.call_args.kwargs["params"] == {
        "query": "selector", "limit": 51, "direction": "backward",
        "start": str(BASE_NS + 123456789), "end": str(BASE_NS + 600_123456790),
    }
    assert get.call_args.kwargs["timeout"] == 5


def test_service_only_call_pins_the_recent_hour_and_reports_actual_bounds():
    with patch("tools.logs.time.time_ns", return_value=BASE_NS), patch(
        "tools.logs.loki_client.query_range", return_value=loki_response([(BASE_NS - 1, "latest")]),
    ) as query:
        result = query_logs("order-service")
    assert result["status"] == "ok"
    assert result["evidence_scope"] == "recent"
    assert result["query_range"] == {
        "start": "2026-10-11T07:00:00Z", "end": "2026-10-11T08:00:00Z",
        "start_unix_ns": str(BASE_NS - 3600_000000000), "end_unix_ns": str(BASE_NS),
        "bounds": "[start, end)", "mode": "recent",
    }
    query.assert_called_once_with(
        logql='{service="order-service"}', limit=51,
        start="2026-10-11T07:00:00Z", end="2026-10-11T08:00:00Z",
    )
    assert result["log_count"] == 1
    assert result["truncated"] is False


def test_window_filters_boundaries_sorts_all_streams_and_preserves_log_fields():
    start_ns, end_ns = parse_time_range(START, END)
    labels = {"service": "order-service", "instance": "demo-a"}
    body = loki_response([
        (start_ns - 1, "historical outside"), (start_ns, "at start"),
        (end_ns, "at exclusive end"), (end_ns + 1, "later outside"),
    ], labels)
    body["data"]["result"].append({
        "stream": {"service": "order-service", "instance": "demo-b"},
        "values": [[str(end_ns - 1), "RuntimeError latest"], [str(start_ns + 1), "next"]],
    })
    original = deepcopy(body)
    with patch("tools.logs.loki_client.query_range", return_value=body) as query:
        result = query_logs(**WINDOW)
    assert [log["line"] for log in result["logs"]] == ["RuntimeError latest", "next", "at start"]
    assert result["logs"][-1] == {
        "timestamp": str(start_ns), "timestamp_utc": START, "line": "at start", "labels": labels,
    }
    assert result["log_count"] == 3
    assert result["out_of_range_count"] == 3
    assert result["evidence_scope"] == "time_window"
    assert result["query_range"]["end"] == "2026-10-11T08:10:00.123456790Z"
    assert query.call_args.kwargs["logql"] == '{service="order-service"}'
    assert body == original


@pytest.mark.parametrize("body", [
    {"data": {"result": []}}, loki_response(), loki_response([(BASE_NS, "outside")]),
])
def test_empty_window_is_no_data_with_query_metadata_not_health(body):
    with patch("tools.logs.loki_client.query_range", return_value=body):
        result = query_logs(**WINDOW)
    assert result["status"] == "no_data"
    assert result["reason"] == "no_logs_in_query_range"
    assert result["logs"] == []
    assert result["log_count"] == 0
    assert result["query_range"]["start_unix_ns"] == str(BASE_NS + 123456789)
    assert "healthy" not in result
    assert result["truncated"] is False


@pytest.mark.parametrize("count", [49, 50, 51, 52])
def test_log_limit_and_overflow_entry_report_truncation(count):
    values = [(BASE_NS + 123456789 + index, str(index)) for index in range(count)]
    with patch("tools.logs.loki_client.query_range", return_value=loki_response(values)) as query:
        result = query_logs(**WINDOW)
    assert len(result["logs"]) == result["log_count"] == min(count, 50)
    assert result["limit"] == 50
    assert result["truncated"] == (count > 50)
    assert [log["line"] for log in result["logs"]] == [str(i) for i in range(count - 1, max(-1, count - 51), -1)]
    assert query.call_args.kwargs["limit"] == 51
    if count > 50:
        assert result["truncation_reason"] == "log_limit"


@pytest.mark.parametrize("body", [
    {"status": "error", "data": {"result": []}},
    {"data": {"resultType": "matrix", "result": []}},
    {"data": {"result": [None]}},
    {"data": {"result": [{"values": [["bad", "line"]]}]}},
    {"data": {"result": [{"values": [[1.2, "line"]]}]}},
])
def test_malformed_window_responses_are_not_successful_evidence(body):
    with patch("tools.logs.loki_client.query_range", return_value=body):
        result = query_logs(**WINDOW)
    assert result["status"] == "no_data"
    assert result["reason"] == "invalid_logs_response"
    assert result["logs"] == []


def test_unregistered_service_and_model_logql_do_not_bypass_registry():
    with patch("tools.logs.loki_client.query_range") as query:
        assert query_logs("unregistered", START, END)["reason"] == "service_not_registered"
        with pytest.raises(TypeError):
            query_logs(**WINDOW, logql='{service=~".*"}')
    query.assert_not_called()


def test_schema_and_worker_accept_optional_strings_and_keep_target_permissions(tmp_path):
    worker = Worker(create_runtime(tmp_path), FixtureAgent())
    schema = tool_registry["query_logs"]["schema"]["parameters"]
    assert schema["required"] == ["service"]
    assert set(schema["properties"]) == {"service", "start", "end"}
    assert schema["properties"]["start"]["type"] == "string"
    assert schema["properties"]["end"]["type"] == "string"
    worker._validate_tool("query_logs", {"service": "order-service"}, "order-service")
    worker._validate_tool("query_logs", WINDOW, "order-service")
    for bad, reason in [
        (WINDOW | {"start": None}, "invalid_tool_argument_type"),
        (WINDOW | {"end": 1791705600}, "invalid_tool_argument_type"),
        (WINDOW | {"end": True}, "invalid_tool_argument_type"),
        (WINDOW | {"logql": "anything"}, "invalid_tool_arguments"),
        ({"start": START, "end": END}, "invalid_tool_arguments"),
        (WINDOW | {"service": "inventory-service"}, "tool_target_outside_session"),
    ]:
        with pytest.raises(RuntimeErrorBase, match=reason):
            worker._validate_tool("query_logs", bad, "order-service")


def test_tool_manager_returns_explicit_partial_window_error_without_loki_io():
    item = SimpleNamespace(**call("query_logs", {"service": "order-service", "start": START}))
    with patch("tools.logs.loki_client.query_range") as query:
        executed, output, result = ToolManager().run_tool(item)
    assert executed is True  # Python validation ran; no external request occurred.
    assert result["status"] == "invalid_arguments"
    assert result["reason"] == "log_time_range_requires_start_and_end"
    assert json.loads(output["output"]) == result
    query.assert_not_called()


def test_cache_keys_distinguish_windows_default_and_runtime_identity():
    identity = {"commit_sha": SHA}
    key = cache_key("order-service", "query_logs", WINDOW, identity)
    variants = [
        WINDOW | {"start": "2026-10-11T08:00:00.123456790Z"},
        WINDOW | {"end": "2026-10-11T08:10:00.123456791Z"},
        {"service": "order-service"},
    ]
    assert all(key != cache_key("order-service", "query_logs", args, identity) for args in variants)
    assert key != cache_key("order-service", "query_logs", WINDOW, {"commit_sha": "b" * 40})
    assert key != cache_key("inventory-service", "query_logs", WINDOW, identity)


def test_window_is_cacheable_for_30_seconds_but_never_current_state():
    now = datetime(2026, 10, 11, 9, tzinfo=timezone.utc)
    identity = {"commit_sha": SHA}
    record = {
        "name": "query_logs", "arguments": WINDOW,
        "result": {"status": "ok", "evidence_scope": "time_window"},
        "success": True, "collected_at": now.isoformat(), "identity": identity,
        "cache_key": cache_key("order-service", "query_logs", WINDOW, identity),
    }
    assert fresh(record, now + timedelta(seconds=30))
    assert not fresh(record, now + timedelta(seconds=30, microseconds=1))
    assert effective_records([record], identity, now, "order-service") == []
    for status in ("no_data", "invalid_arguments", "tool_error"):
        assert not fresh(record | {"result": {"status": status}}, now)
    recent_args = {"service": "order-service"}
    recent = record | {
        "arguments": recent_args, "result": {"status": "ok", "evidence_scope": "recent"},
        "cache_key": cache_key("order-service", "query_logs", recent_args, identity),
    }
    assert effective_records([recent], identity, now, "order-service") == [recent]
    # Older persisted observations without new result metadata still use their arguments.
    assert effective_records([record | {"result": {"status": "ok"}}], identity, now) == []


def test_simulated_incident_window_through_worker_http_cache_and_model_context(tmp_path):
    now = datetime(2026, 10, 11, 9, tzinfo=timezone.utc)
    manager = create_runtime(tmp_path, clock=lambda: now)
    session = create_session(manager)
    incident = manager.incident_store.get_incident(session.incident_id)
    incident.alerts[-1].starts_at = datetime(2026, 10, 11, 8, 5, tzinfo=timezone.utc)
    incident.alerts[-1].observed_at = datetime(2026, 10, 11, 8, 10, tzinfo=timezone.utc)
    incident.alerts[-1].window = "5m"
    manager.incident_store.save_incident(incident.fingerprint, incident)
    calls = [
        call("query_logs", WINDOW, "window-a"), call("query_logs", WINDOW, "window-a-cached"),
        call("query_logs", WINDOW | {"end": "2026-10-11T08:04:00Z"}, "window-b"),
    ]
    agent = FixtureAgent([response(answer="", calls=calls), response()])
    fixture_tool = agent.tool_manager.run_tool
    real_tools = ToolManager()

    def execute(item, *, session_id=None):
        if item.name == "query_logs":
            return real_tools.run_tool(item, session_id=session_id)
        return fixture_tool(item, session_id=session_id)

    agent.tool_manager.run_tool = execute
    all_logs = [
        (BASE_NS - 3600_000000000, "historical RuntimeError outside alert window"),
        (BASE_NS + 180_000000000, "window /order request"),
        (BASE_NS + 300_000000000, "RuntimeError key alert evidence"),
        (BASE_NS + 660_000000000, "later outside alert window"),
    ]

    def http_get(url, *, params, timeout):
        assert url.endswith("/loki/api/v1/query_range")
        assert params["query"] == '{service="order-service"}'
        assert params["direction"] == "backward"
        assert timeout == 5
        selected = [(stamp, line) for stamp, line in all_logs
                    if int(params["start"]) <= stamp < int(params["end"])]
        return Mock(json=Mock(return_value=loki_response(selected)))

    with patch("clients.loki_client.requests.get", side_effect=http_get) as get:
        completed = Worker(manager, agent).run_once()
    assert completed.status == S.COMPLETED
    assert get.call_count == 2  # Same window reused; different end executed.
    assert completed.cache_hits == 1
    assert completed.total_tool_executions == 3  # Version probe plus two distinct windows.
    checkpoint = manager.store.load_checkpoint(completed)
    records = [record for record in checkpoint["records"] if record["name"] == "query_logs"]
    assert [log["line"] for log in records[0]["result"]["logs"]] == [
        "RuntimeError key alert evidence", "window /order request",
    ]
    assert [log["line"] for log in records[1]["result"]["logs"]] == ["window /order request"]
    context = agent.request_durable_response.call_args_list[1].args[0][0]["content"]
    rebuilt = json.loads(context.rsplit("\n", 1)[1])
    pairs = [pair for pair in rebuilt["recent_function_call_results"] if pair["name"] == "query_logs"]
    assert len(pairs) == 3
    assert all(pair["use_for_current_state"] is False for pair in pairs)
    assert pairs[0]["observation_id"] == pairs[1]["observation_id"]
    assert "RuntimeError key alert evidence" in context
    assert "historical RuntimeError outside" not in context
    assert "later outside" not in context
    historical = [record for record in rebuilt["historical_observations"] if record["name"] == "query_logs"]
    assert len(historical) == 2
    assert all(record["expired"] is False and record["use_for_current_state"] is False for record in historical)
    assert all(record["result"]["query_range"]["mode"] == "explicit" for record in historical)


def test_oversized_log_summary_retains_window_and_truncation_metadata():
    with patch("tools.logs.loki_client.query_range", return_value=loki_response([
        (BASE_NS + 123456789 + i, "x" * 1001) for i in range(51)
    ], {"service": "order-service", "extra": "y" * 1001})):
        result = query_logs(**WINDOW)
    original = deepcopy(result)
    summary = Worker._observation_summary(result, "query_logs")
    assert summary["summary_truncated"] is True
    for key in ("query_range", "evidence_scope", "log_count", "limit", "truncated", "truncation_reason"):
        assert summary[key] == result[key]
    assert result == original
