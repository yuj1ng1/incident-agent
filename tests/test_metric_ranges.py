"""Bounded historical metrics through mocked HTTP and isolated SQLite Runtime."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from agent.agent import Agent
from clients.prometheus_client import MAX_RANGE_RESPONSE_BYTES, PrometheusClient
from investigation.evidence_policy import cache_key, effective_records, fresh
from investigation.models import RuntimeLimits, SessionStatus
from investigation.state import LostClaim
from investigation.worker import Worker
from runtime_support import SHA, call, create_runtime, create_session, response
from services.metric_profiles import HTTP_METRICS, POSTGRES_METRICS, build_promql
from services.registry import ServiceRegistry, registry
from tools.metrics import query_metric_range
from tools.tool_registry import tool_registry


NOW = datetime(2026, 10, 12, 2, tzinfo=timezone.utc)
FINISHED = NOW + timedelta(milliseconds=400)
START = NOW - timedelta(minutes=10)
END = START + timedelta(minutes=1)
WINDOW = {
    "service": "order-service",
    "metric": "http_5xx_rate",
    "start": START.isoformat(),
    "end": END.isoformat(),
    "step_seconds": 15,
}


def matrix(values=None, labels=None):
    if values is None:
        values = [
            [(START + timedelta(seconds=i * 15)).timestamp(), "0.1"]
            for i in range(5)
        ]
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": labels or {}, "values": values}],
        },
    }


def http_response(payload=None):
    result = requests.Response()
    result.status_code = 200
    result._content_consumed = True
    result._content = json.dumps(payload if payload is not None else matrix()).encode()
    return result


@pytest.fixture
def prometheus():
    client = PrometheusClient(
        "http://prometheus.invalid",
        clock=Mock(side_effect=[NOW, FINISHED]),
    )
    reply = http_response()
    with (
        patch("clients.prometheus_client.requests.get", return_value=reply) as get,
        patch("tools.metrics.prometheus_client", client),
        patch("tools.metrics.datetime", wraps=datetime) as clock,
    ):
        clock.now.return_value = NOW
        yield SimpleNamespace(client=client, reply=reply, get=get, clock=clock)


def set_payload(prometheus, payload):
    prometheus.reply._content = json.dumps(payload).encode()


def assert_rejected(result, status="no_data"):
    assert result["status"] == status
    assert result["evidence_scope"] == "time_window"
    assert result["use_for_current_state"] is False
    assert result["samples"] == []
    assert result["sample_count"] == 0
    assert "value" not in result
    assert "evaluated_at" not in result


def test_historical_points_and_local_request_times(prometheus):
    payload = matrix() | {"query_started_at": "forged", "query_finished_at": "forged"}
    set_payload(prometheus, payload)

    result = query_metric_range(**WINDOW)

    assert result["status"] == "ok"
    assert result["sample_count"] == result["expected_point_count"] == 5
    assert result["samples"][0] == {"evaluated_at": START.isoformat(), "value": 0.1}
    assert result["samples"][-1]["evaluated_at"] == END.isoformat()
    assert result["missing_point_count"] == 0
    assert result["missing_evaluated_at"] == []
    assert result["point_limit"] == 120
    assert result["truncated"] is False
    assert result["evidence_scope"] == "time_window"
    assert result["use_for_current_state"] is False
    assert result["query_started_at"] == NOW.isoformat()
    assert result["query_finished_at"] == FINISHED.isoformat()
    assert result["requested_range"] == {"start": WINDOW["start"], "end": WINDOW["end"]}
    assert result["query_range"] == {
        "start": START.isoformat(), "end": END.isoformat(), "bounds": "[start, end]",
    }
    assert result["step_seconds"] == 15
    prometheus.get.assert_called_once_with(
        "http://prometheus.invalid/api/v1/query_range",
        params={
            "query": result["promql"], "start": START.isoformat(),
            "end": END.isoformat(), "step": 15,
        },
        timeout=5,
        stream=True,
        allow_redirects=False,
        headers={"Accept": "application/json", "Accept-Encoding": "identity"},
    )


@pytest.mark.parametrize("offset", [8, -5, 0])
def test_explicit_timezone_is_converted_to_utc(prometheus, offset):
    zone = timezone(timedelta(hours=offset))
    arguments = WINDOW | {
        "start": START.astimezone(zone).isoformat(),
        "end": END.astimezone(zone).isoformat(),
    }
    result = query_metric_range(**arguments)
    assert result["status"] == "ok"
    assert result["query_range"]["start"] == START.isoformat()
    assert result["query_range"]["end"] == END.isoformat()
    assert result["requested_range"]["start"] == arguments["start"]


def test_fractional_times_report_original_and_actual_millisecond_interval(prometheus):
    start = START.replace(microsecond=123456)
    end = END.replace(microsecond=124999)
    values = [
        [(START + timedelta(seconds=i * 15, milliseconds=124)).timestamp(), "0"]
        for i in range(5)
    ]
    set_payload(prometheus, matrix(values))

    result = query_metric_range(
        **(WINDOW | {"start": start.isoformat(), "end": end.isoformat()})
    )

    assert result["status"] == "ok"
    assert result["query_range"]["start"] == (
        START.replace(microsecond=124000).isoformat()
    )
    assert result["query_range"]["end"] == END.replace(microsecond=124000).isoformat()
    assert result["requested_range"] == {
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    assert result["sample_count"] == 5


@pytest.mark.parametrize("value", [
    None, True, 1791770400, "", "2026-10-12", "2026-10-12T01:50:00",
    "2026-10-12 01:50:00Z", "2026-10-12T01:50:00-00:00",
    "2026-13-12T01:50:00Z", "2026-02-30T01:50:00Z",
    "2026-10-12T25:50:00Z", "2026-10-12T01:50:00+24:00",
    "2026-10-12T01:50:00+01:60", "2026-10-12T01:50:00.1234567890Z",
    "1969-12-31T23:59:59Z", "0001-01-01T00:00:00+08:00",
])
@pytest.mark.parametrize("field", ["start", "end"])
def test_invalid_or_ambiguous_time_is_rejected_before_io(prometheus, field, value):
    result = query_metric_range(**(WINDOW | {field: value}))
    assert_rejected(result, "invalid_arguments")
    prometheus.get.assert_not_called()


@pytest.mark.parametrize("updates,reason", [
    ({"start": END.isoformat()}, "metric_range_start_not_before_end"),
    (
        {"start": (END + timedelta(seconds=1)).isoformat()},
        "metric_range_start_not_before_end",
    ),
    ({"end": (NOW + timedelta(milliseconds=1)).isoformat()}, "metric_range_in_future"),
    (
        {
            "start": (END - timedelta(hours=6, milliseconds=1)).isoformat(),
            "step_seconds": 300,
        },
        "metric_range_exceeds_6_hours",
    ),
    (
        {"start": (END - timedelta(seconds=1800)).isoformat()},
        "metric_range_point_limit_exceeded",
    ),
    (
        {
            "start": START.replace(microsecond=1).isoformat(),
            "end": START.replace(microsecond=2).isoformat(),
        },
        "metric_range_below_timestamp_resolution",
    ),
    ({"service": []}, "invalid_metric_range_target"),
    ({"metric": {}}, "invalid_metric_range_target"),
])
def test_window_limits_are_applied_before_request(prometheus, updates, reason):
    result = query_metric_range(**(WINDOW | updates))
    assert_rejected(result, "invalid_arguments")
    assert result["reason"] == reason
    prometheus.get.assert_not_called()


@pytest.mark.parametrize("step", [14, 301, 0, -15, True, False, 15.0, "15", None])
def test_step_is_a_bounded_integer(prometheus, step):
    result = query_metric_range(**(WINDOW | {"step_seconds": step}))
    assert_rejected(result, "invalid_arguments")
    prometheus.get.assert_not_called()


@pytest.mark.parametrize("span,step,count", [
    (119 * 15, 15, 120), (6 * 3600, 300, 73), (6 * 3600, 182, 119), (16, 15, 2),
])
def test_maximum_window_and_inclusive_point_limit(
    prometheus,
    span,
    step,
    count,
):
    start = NOW - timedelta(seconds=span)
    values = [
        [(start + timedelta(seconds=i * step)).timestamp(), "0"]
        for i in range(count)
    ]
    set_payload(prometheus, matrix(values))
    result = query_metric_range(**(WINDOW | {
        "start": start.isoformat(), "end": NOW.isoformat(), "step_seconds": step,
    }))
    assert result["status"] == "ok"
    assert result["expected_point_count"] == result["sample_count"] == count
    assert result["missing_point_count"] == 0


@pytest.mark.parametrize("series_count", [0, 1])
def test_empty_result_preserves_full_gap_semantics(prometheus, series_count):
    payload = matrix([])
    if not series_count:
        payload["data"]["result"] = []
    set_payload(prometheus, payload)
    result = query_metric_range(**WINDOW)
    assert_rejected(result)
    assert result["reason"] == "no_samples_in_metric_range"
    assert result["missing_point_count"] == 5
    assert len(result["missing_evaluated_at"]) == 5
    assert result["truncated"] is False


def test_multiple_series_are_rejected_even_with_identical_labels(prometheus):
    payload = matrix()
    payload["data"]["result"] *= 2
    set_payload(prometheus, payload)
    result = query_metric_range(**WINDOW)
    assert_rejected(result)
    assert result["reason"] == "multiple_metric_series"
    assert result["missing_point_count"] is None


def test_missing_points_are_reported_without_invented_zeroes(prometheus):
    values = [[START.timestamp(), "0"], [END.timestamp(), "0.2"]]
    set_payload(prometheus, matrix(values))
    result = query_metric_range(**WINDOW)
    assert result["status"] == "ok"
    assert [point["value"] for point in result["samples"]] == [0.0, 0.2]
    assert result["missing_point_count"] == 3
    assert result["missing_evaluated_at"] == [
        (START + timedelta(seconds=i * 15)).isoformat() for i in (1, 2, 3)
    ]


@pytest.mark.parametrize("value", [
    None, True, False, [], {}, "bad", "NaN", "Infinity", "-Inf", "1e309",
    " 0.1 ", float("nan"), float("inf"), -float("inf"), 10**500,
])
def test_invalid_and_nonfinite_values_reject_the_entire_series(prometheus, value):
    values = matrix()["data"]["result"][0]["values"]
    values[-1][1] = value
    set_payload(prometheus, matrix(values))
    assert_rejected(query_metric_range(**WINDOW))


@pytest.mark.parametrize("service,metric,value", [
    ("order-service", "http_5xx_rate", "1.01"),
    ("order-service", "http_5xx_rate", "-0.01"),
    ("order-service", "service_scrape_up", "0.5"),
    ("order-service", "http_latency_p95_seconds", "-1"),
    ("order-service", "http_requests_total", "-1"),
    ("order-service", "http_request_rate", "-1"),
    ("order-db", "postgres_connection_usage", "1.01"),
    ("order-db", "postgres_connections", "-1"),
    ("order-db", "postgres_up", "2"),
    ("order-db", "exporter_scrape_up", "0.5"),
])
def test_values_obey_metric_semantic_domains(
    prometheus,
    service,
    metric,
    value,
):
    set_payload(prometheus, matrix([[START.timestamp(), value]]))
    result = query_metric_range(**(WINDOW | {"service": service, "metric": metric}))
    assert_rejected(result)
    assert result["reason"] == "metric_value_outside_semantic_domain"


@pytest.mark.parametrize("stamp", [
    None, True, False, "bad", "1791770400", [], {}, -1, 253402300800,
    float("nan"), float("inf"), -float("inf"), 10**500,
    (START - timedelta(milliseconds=1)).timestamp(),
    (END + timedelta(milliseconds=1)).timestamp(),
    (START + timedelta(seconds=1)).timestamp(),
])
def test_invalid_outside_window_and_off_grid_timestamps(prometheus, stamp):
    set_payload(prometheus, matrix([[stamp, "0.1"]]))
    assert_rejected(query_metric_range(**WINDOW))


@pytest.mark.parametrize("values", [
    [[START.timestamp(), "0"], [START.timestamp(), "0.1"]],
    [[END.timestamp(), "0"], [START.timestamp(), "0.1"]],
    [[START.timestamp(), "0"], []],
    [[START.timestamp(), "0"], [END.timestamp()]],
    [[START.timestamp(), "0"], None],
    [[START.timestamp(), "0"], [END.timestamp(), "0", "extra"]],
])
def test_duplicates_disorder_and_missing_sample_fields_are_not_repaired(
    prometheus,
    values,
):
    set_payload(prometheus, matrix(values))
    assert_rejected(query_metric_range(**WINDOW))


@pytest.mark.parametrize("count", [6, 121])
def test_response_point_count_is_independently_bounded(prometheus, count):
    set_payload(prometheus, matrix([[START.timestamp(), "0"]] * count))
    result = query_metric_range(**WINDOW)
    assert_rejected(result)
    assert result["reason"] == "metric_range_response_point_limit"
    assert result["truncated"] is True


@pytest.mark.parametrize("payload", [
    None, [], {"status": "error"}, {"status": "success", "data": []},
    {"status": "success", "data": {"resultType": "vector", "result": []}},
    {"status": "success", "data": {"result": []}},
    {"status": "success", "data": {"resultType": "matrix", "result": {}}},
    {"status": "success", "data": {"resultType": "matrix", "result": [None]}},
    {"status": "success", "data": {"resultType": "matrix", "result": [{"values": []}]}},
    matrix() | {"warnings": ["partial result"]},
    matrix() | {"infos": ["mixed samples omitted"]},
])
def test_malformed_nonmatrix_and_partial_responses_are_not_evidence(
    prometheus,
    payload,
):
    set_payload(prometheus, payload)
    assert_rejected(query_metric_range(**WINDOW))


@pytest.mark.parametrize("labels", [
    None, [], {"a": 1}, {"a": "x" * 257}, {"x" * 129: "a"},
    {str(i): "x" for i in range(33)},
])
def test_invalid_or_oversized_labels_are_rejected(prometheus, labels):
    payload = matrix()
    payload["data"]["result"][0]["metric"] = labels
    set_payload(prometheus, payload)
    assert_rejected(query_metric_range(**WINDOW))


@pytest.mark.parametrize("error", [
    requests.Timeout, requests.ConnectionError, requests.HTTPError,
])
def test_network_failure_preserves_attempt_times_and_redacts_details(prometheus, error):
    prometheus.get.side_effect = error("private backend credentials")
    result = query_metric_range(**WINDOW)
    assert_rejected(result)
    assert result["query_started_at"] == NOW.isoformat()
    assert result["query_finished_at"] == FINISHED.isoformat()
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("body", [
    b"not JSON", b'{"status": "success", "status": "error"}',
    b'{"a": NaN}', b"\xff", b"[" * 2000,
])
def test_invalid_json_is_rejected_with_request_timing(prometheus, body):
    prometheus.reply._content = body
    result = query_metric_range(**WINDOW)
    assert_rejected(result)
    assert result["query_started_at"] == NOW.isoformat()
    assert result["query_finished_at"] == FINISHED.isoformat()


@pytest.mark.parametrize("mode", ["content_length", "stream", "lying_length"])
def test_oversized_response_is_rejected_before_json_decode(prometheus, mode):
    if mode == "content_length":
        prometheus.reply.headers["Content-Length"] = str(MAX_RANGE_RESPONSE_BYTES + 1)
        with patch.object(prometheus.reply, "iter_content") as read:
            result = query_metric_range(**WINDOW)
        read.assert_not_called()
    else:
        prometheus.reply._content = b" " * (MAX_RANGE_RESPONSE_BYTES + 1)
        if mode == "lying_length":
            prometheus.reply.headers["Content-Length"] = "1"
        result = query_metric_range(**WINDOW)
    assert_rejected(result)
    assert result["reason"] == "metric_range_response_size_limit"
    assert result["truncated"] is True


def test_exact_response_size_limit_remains_usable(prometheus):
    padding = MAX_RANGE_RESPONSE_BYTES - len(prometheus.reply.content)
    prometheus.reply._content += b" " * padding
    prometheus.reply.headers["Content-Length"] = str(MAX_RANGE_RESPONSE_BYTES)
    assert query_metric_range(**WINDOW)["status"] == "ok"


@pytest.mark.parametrize("headers,status,reason", [
    ({"Content-Length": "bad"}, 200, "invalid_metric_range_content_length"),
    ({"Content-Encoding": "gzip"}, 200, "metric_range_content_encoding_refused"),
    ({}, 302, "metric_range_redirect_refused"),
    ({}, 500, "metrics_range_unreachable_or_invalid"),
])
def test_transport_failures_are_closed_without_reading_the_body(
    prometheus,
    headers,
    status,
    reason,
):
    prometheus.reply.headers.update(headers)
    prometheus.reply.status_code = status
    with patch.object(prometheus.reply, "iter_content") as read:
        result = query_metric_range(**WINDOW)
    read.assert_not_called()
    assert_rejected(result)
    assert result["reason"] == reason


def test_slow_trickling_response_has_a_monotonic_deadline(prometheus):
    with patch("clients.prometheus_client.time.monotonic", side_effect=[0, 6]):
        result = query_metric_range(**WINDOW)
    assert_rejected(result)
    assert result["reason"] == "metrics_range_unreachable_or_invalid"
    assert result["query_finished_at"] == FINISHED.isoformat()


@pytest.mark.parametrize("service,metric", [
    ("missing", "http_5xx_rate"), ("order-service", 'up{job="other"}'),
    ("order-db", "http_request_rate"), ("order-service", "postgres_up"),
])
def test_unregistered_services_or_unsupported_metrics_never_reach_http(
    prometheus,
    service,
    metric,
):
    result = query_metric_range(**(WINDOW | {"service": service, "metric": metric}))
    assert_rejected(result)
    assert result["query_started_at"] is None
    prometheus.get.assert_not_called()


def test_service_without_metrics_capability_is_rejected(prometheus):
    minimal = ServiceRegistry({"minimal": {"name": "minimal", "type": "http"}})
    with patch("tools.metrics.registry", minimal):
        result = query_metric_range(**(WINDOW | {"service": "minimal"}))
    assert_rejected(result)
    assert result["reason"] == "metrics_capability_not_configured"
    prometheus.get.assert_not_called()


@pytest.mark.parametrize("service,metric", [
    ("order-service", metric) for metric in HTTP_METRICS
] + [("order-db", metric) for metric in POSTGRES_METRICS])
def test_http_and_postgres_reuse_the_exact_existing_profile_formulas(
    prometheus,
    service,
    metric,
):
    set_payload(prometheus, matrix([[START.timestamp(), "1"]]))
    result = query_metric_range(**(WINDOW | {"service": service, "metric": metric}))
    assert result["status"] == "ok"
    assert result["promql"] == build_promql(
        service,
        registry.require_capability(service, "metrics"),
        metric,
    )
    assert result["samples"][0]["value"] == 1.0


def test_model_schema_has_five_required_parameters_without_promql():
    schema = tool_registry["query_metric_range"]["schema"]["parameters"]
    assert schema["required"] == ["service", "metric", "start", "end", "step_seconds"]
    assert set(schema["properties"]) == set(WINDOW)
    assert schema["properties"]["step_seconds"]["type"] == "integer"
    assert all(
        schema["properties"][key]["type"] == "string"
        for key in WINDOW if key != "step_seconds"
    )
    assert "query_range" not in tool_registry


def range_record(result, arguments=None):
    arguments = arguments or WINDOW
    return {
        "name": "query_metric_range",
        "arguments": arguments,
        "result": result,
        "success": True,
        "collected_at": NOW.isoformat(),
        "identity": {"commit_sha": SHA},
        "cache_key": cache_key(
            "order-service", "query_metric_range", arguments, {"commit_sha": SHA},
        ),
    }


def test_historical_cache_ttl_does_not_apply_instant_sample_freshness(prometheus):
    result = query_metric_range(**WINDOW)
    record = range_record(result)
    assert fresh(record, NOW + timedelta(seconds=30))
    assert not fresh(record, NOW + timedelta(seconds=30, microseconds=1))
    assert not fresh(record, NOW - timedelta(seconds=1))
    assert effective_records([record], record["identity"], NOW, "order-service") == []
    without_scope = record | {"result": {"status": "ok"}}
    assert effective_records([without_scope], record["identity"], NOW) == []
    masquerading = record | {
        "name": "query_metrics",
        "result": result | {"evaluated_at": NOW.isoformat(), "value": 0.0},
    }
    assert not fresh(masquerading, NOW)
    assert effective_records([masquerading], record["identity"], NOW) == []


def test_cache_keys_include_target_metric_window_step_profile_and_identity():
    identity = {"commit_sha": SHA, "process_started_at": START.isoformat()}
    key = cache_key("order-service", "query_metric_range", WINDOW, identity)
    variants = [
        WINDOW | {"service": "inventory-service"},
        WINDOW | {"metric": "http_request_rate"},
        WINDOW | {"start": (START + timedelta(seconds=15)).isoformat()},
        WINDOW | {"end": (END + timedelta(seconds=15)).isoformat()},
        WINDOW | {"step_seconds": 30},
    ]
    assert all(
        cache_key("order-service", "query_metric_range", arguments, identity) != key
        for arguments in variants
    )
    assert cache_key("inventory-service", "query_metric_range", WINDOW, identity) != key
    assert cache_key("order-service", "query_metrics", WINDOW, identity) != key
    assert cache_key(
        "order-service", "query_metric_range", WINDOW,
        identity | {"commit_sha": "b" * 40},
    ) != key
    assert cache_key(
        "order-service", "query_metric_range", WINDOW,
        identity | {"process_started_at": END.isoformat()},
    ) != key
    changed = ServiceRegistry({"order-service": {
        "name": "order-service", "type": "http",
        "metrics": {
            "provider": "prometheus",
            "profile": "http_service",
            "labels": {"service": "changed"},
        },
    }})
    with patch("investigation.evidence_policy.registry", changed):
        assert cache_key("order-service", "query_metric_range", WINDOW, identity) != key


@pytest.mark.parametrize("age", [0, 31])
def test_full_checkpoint_cache_and_model_context_separate_historical_and_current(
    tmp_path,
    prometheus,
    age,
):
    store_time = SimpleNamespace(now=NOW)
    manager = create_runtime(tmp_path, clock=lambda: store_time.now)
    create_session(manager)
    agent = Agent(Mock(), "只读调查，历史序列不能证明当前健康。")
    agent.request_durable_response = Mock(side_effect=[
        response(answer="", calls=[call("query_metric_range", WINDOW, "range-first")]),
        response(answer="", calls=[
            call("query_metric_range", WINDOW, "range-second"),
            call(
                "query_metrics",
                {"service": "order-service", "metric": "http_5xx_rate"},
                "instant",
            ),
        ]),
        response(),
    ])
    version = {
        "status": "ok", "service": "order-service", "commit_sha": SHA,
        "process_started_at": (NOW - timedelta(hours=1)).isoformat(),
    }
    def boundary(phase, session_id):
        if phase == "tool_checkpoint_committed" and prometheus.get.call_count == 1:
            store_time.now = NOW + timedelta(seconds=age)
            if age:
                prometheus.client.clock.side_effect = [store_time.now, store_time.now]

    with (
        patch.dict(
            tool_registry["get_running_version"], {"func": Mock(return_value=version)},
        ),
        patch.dict(tool_registry["query_metrics"], {"func": lambda **kwargs: {
            "status": "ok", "value": 0.0, "evaluated_at": store_time.now.isoformat(),
        }}),
    ):
        completed = Worker(
            manager, agent, boundary_hook=boundary, provider_verified=True,
        ).run_once()

    assert completed.status == SessionStatus.COMPLETED
    assert completed.cache_hits == (0 if age else 1)
    assert completed.total_tool_executions == (4 if age else 3)
    assert prometheus.get.call_count == (2 if age else 1)
    checkpoint = manager.store.load_checkpoint(completed)
    assert checkpoint["schema_version"] == 1
    records = [
        record for record in checkpoint["records"]
        if record["name"] == "query_metric_range"
    ]
    assert len(records) == (2 if age else 1)
    assert records[0]["result"]["query_started_at"] == NOW.isoformat()
    assert records[0]["result"]["query_finished_at"] == FINISHED.isoformat()
    assert records[0]["result"]["sample_count"] == 5
    assert records[0]["result"]["samples"][0]["evaluated_at"] == START.isoformat()
    context = agent.request_durable_response.call_args_list[-1].args[0][0]["content"]
    details = json.loads(context.rsplit("\n", 1)[-1])
    historical = [
        item for item in details["historical_observations"]
        if item["name"] == "query_metric_range"
    ]
    assert historical
    assert all(item["use_for_current_state"] is False for item in historical)
    assert all(item["result"]["evidence_scope"] == "time_window" for item in historical)
    assert not historical[-1]["expired"]
    call_results = details["recent_function_call_results"]
    assert all(
        not item["use_for_current_state"] for item in call_results
        if item["name"] == "query_metric_range"
    )
    current_call = next(
        item for item in call_results if item["name"] == "query_metrics"
    )
    assert current_call["use_for_current_state"]
    assert all(record["name"] != "query_metric_range" for record in effective_records(
        checkpoint["records"], checkpoint["identity"], store_time.now, "order-service"
    ))
    # Open the same temporary DB anew: full samples, not the model summary, survive.
    from investigation.store import RuntimeStore
    reopened = RuntimeStore(manager.store.path, clock=lambda: store_time.now)
    restored = reopened.load_checkpoint(reopened.get(completed.session_id))
    assert restored["records"] == checkpoint["records"]


def test_summary_preserves_extremes_window_gaps_and_original_checkpoint(prometheus):
    start = NOW - timedelta(seconds=119 * 15)
    values = [
        [(start + timedelta(seconds=i * 15)).timestamp(), "0.1"]
        for i in range(120) if i not in {3, 4, 5, 6}
    ]
    values[24][1] = "0.9"
    set_payload(prometheus, matrix(values))
    result = query_metric_range(
        **(WINDOW | {"start": start.isoformat(), "end": NOW.isoformat()})
    )
    original = deepcopy(result)
    summary = Worker._observation_summary(result, "query_metric_range")
    assert result == original
    assert result["sample_count"] == 116
    assert summary["summary_point_count"] == 12
    assert summary["summary_truncated"] is True
    assert summary["statistics"]["max"] == 0.9
    assert summary["samples"][0] == result["samples"][0]
    assert summary["samples"][-1] == result["samples"][-1]
    assert summary["missing_point_count"] == 4
    assert summary["missing_timestamps_truncated"] is True
    assert len(json.dumps(summary).encode()) < 5000
    for key in (
        "query_range", "requested_range", "step_seconds", "query_started_at",
        "query_finished_at", "evidence_scope",
    ):
        assert summary[key] == result[key]


@pytest.mark.parametrize("arguments", [
    WINDOW | {"promql": "up"}, WINDOW | {"step_seconds": True},
    WINDOW | {"service": "inventory-service"},
])
def test_worker_permissions_and_schema_reject_before_http(
    tmp_path,
    prometheus,
    arguments,
):
    manager = create_runtime(tmp_path, clock=lambda: NOW)
    create_session(manager)
    session = manager.store.claim("range-test-owner", manager.limits)
    checkpoint = manager.store.load_checkpoint(session)
    worker = Worker(manager, Agent(Mock(), "只读"))
    result, cached = worker._tool(
        session.session_id, (session.owner_token, session.fencing_token), checkpoint,
        SimpleNamespace(
            name="query_metric_range",
            arguments=json.dumps(arguments),
            call_id="rejected",
        ),
    )
    assert result["status"] == "invalid_or_unallowed_tool_call"
    assert not cached
    assert checkpoint["records"] == []
    assert manager.store.get(session.session_id).total_tool_executions == 0
    prometheus.get.assert_not_called()


def test_history_query_uses_existing_tool_budget(tmp_path, prometheus):
    manager = create_runtime(
        tmp_path,
        clock=lambda: NOW,
        limits=RuntimeLimits(max_tool_executions=1),
    )
    create_session(manager)
    agent = Agent(Mock(), "只读")
    agent.request_durable_response = Mock(return_value=response(
        answer="", calls=[call("query_metric_range", WINDOW)],
    ))
    with patch.dict(tool_registry["get_running_version"], {
        "func": Mock(return_value={"status": "no_data"}),
    }):
        completed = Worker(manager, agent).run_once()
    assert completed.status == SessionStatus.EXHAUSTED
    assert completed.total_tool_executions == 1
    prometheus.get.assert_not_called()


def test_lost_lease_prevents_history_io(tmp_path, prometheus):
    manager = create_runtime(tmp_path, clock=lambda: NOW)
    create_session(manager)
    session = manager.store.claim("range-test-owner", manager.limits)
    checkpoint = manager.store.load_checkpoint(session)
    with pytest.raises(LostClaim):
        Worker(manager, Agent(Mock(), "只读"))._tool(
            session.session_id, ("different-owner", session.fencing_token), checkpoint,
            SimpleNamespace(
                name="query_metric_range",
                arguments=json.dumps(WINDOW),
                call_id="lost",
            ),
        )
    prometheus.get.assert_not_called()


@pytest.mark.parametrize("bad_time", [
    None, True, "bad", NOW.replace(tzinfo=None).isoformat(),
    "0001-01-01T00:00:00+08:00", (NOW - timedelta(seconds=1)).isoformat(),
])
def test_invalid_client_request_timing_is_not_accepted(prometheus, bad_time):
    payload = matrix() | {
        "query_started_at": NOW.isoformat(),
        "query_finished_at": bad_time,
    }
    with patch.object(prometheus.client, "query_range", return_value=payload):
        result = query_metric_range(**WINDOW)
    assert_rejected(result)
    assert result["reason"] == "invalid_query_timing"
    prometheus.get.assert_not_called()


@pytest.mark.parametrize("updates", [
    {"start": END.isoformat()}, {"step_seconds": 14},
    {"end": (NOW + timedelta(seconds=1)).isoformat()},
    {"start": (END - timedelta(seconds=1800)).isoformat()},
])
def test_low_level_client_also_rejects_unbounded_range_before_http(prometheus, updates):
    arguments = {
        key: value for key, value in (WINDOW | updates).items()
        if key in {"start", "end", "step_seconds"}
    }
    with pytest.raises(ValueError):
        prometheus.client.query_range("trusted-profile-expression", **arguments)
    prometheus.get.assert_not_called()


@pytest.mark.parametrize("value", ["0", "0.0", 0, 0.0, "1e-1", "1.0e+0"])
def test_zero_and_numeric_formats_remain_real_observations(prometheus, value):
    set_payload(prometheus, matrix([[START.timestamp(), value]]))
    result = query_metric_range(**WINDOW)
    assert result["status"] == "ok"
    assert result["sample_count"] == 1
    assert result["samples"][0]["value"] == float(value)


@pytest.mark.parametrize("case", ["missing_values", "histograms", "nonlist_values"])
def test_float_series_format_is_strict(prometheus, case):
    payload = matrix()
    series = payload["data"]["result"][0]
    if case == "missing_values":
        del series["values"]
    elif case == "histograms":
        series["histograms"] = []
    else:
        series["values"] = {}
    set_payload(prometheus, payload)
    assert_rejected(query_metric_range(**WINDOW))


def test_full_120_point_checkpoint_uses_bounded_model_context(tmp_path, prometheus):
    start = NOW - timedelta(seconds=119 * 15)
    values = [
        [(start + timedelta(seconds=index * 15)).timestamp(), "0.1"]
        for index in range(120)
    ]
    values[57][1] = "0.9"
    set_payload(prometheus, matrix(values))
    arguments = WINDOW | {"start": start.isoformat(), "end": NOW.isoformat()}
    manager = create_runtime(tmp_path, clock=lambda: NOW)
    create_session(manager)
    agent = Agent(Mock(), "只读调查")
    agent.request_durable_response = Mock(side_effect=[
        response(answer="", calls=[call("query_metric_range", arguments)]), response(),
    ])
    with patch.dict(tool_registry["get_running_version"], {
        "func": Mock(return_value={"status": "ok", "commit_sha": SHA}),
    }):
        completed = Worker(manager, agent, provider_verified=True).run_once()
    assert completed.status == SessionStatus.COMPLETED
    checkpoint = manager.store.load_checkpoint(completed)
    record = next(
        item for item in checkpoint["records"] if item["name"] == "query_metric_range"
    )
    assert len(record["result"]["samples"]) == 120
    context = agent.request_durable_response.call_args_list[1].args[0][0]["content"]
    details = json.loads(context.rsplit("\n", 1)[-1])
    summary = next(
        item["result"] for item in details["historical_observations"]
        if item["name"] == "query_metric_range"
    )
    assert summary["sample_count"] == 120
    assert summary["summary_point_count"] == 12
    assert summary["statistics"]["max"] == 0.9
    assert summary["summary_truncated"] is True
    assert len(json.dumps(summary).encode()) < 5000
    assert all(
        not item["use_for_current_state"]
        for item in details["recent_function_call_results"]
    )


def test_total_call_summary_limit_keeps_range_time_and_scope(tmp_path, prometheus):
    result = query_metric_range(**WINDOW)
    manager = create_runtime(tmp_path, clock=lambda: NOW)
    session = create_session(manager)
    worker = Worker(manager, Agent(Mock(), "只读"))
    checkpoint = manager.store.load_checkpoint(session)
    history = []
    for index in range(5):
        is_range = index == 4
        name = "query_metric_range" if is_range else "get_recent_code_changes"
        arguments = WINDOW if is_range else {"service": "order-service"}
        output = result if is_range else {
            "status": "ok",
            **{
                f"detail_{field}": str(index) * (
                    600 if index == 3 and field == 3 else 1000
                ) for field in range(4)
            },
        }
        item = call(name, arguments, str(index))
        history.extend([
            item,
            {
                "type": "function_call_output",
                "call_id": item["call_id"],
                "output": json.dumps(output),
            },
        ])
        checkpoint["records"].append(range_record(output, arguments) | {
            "name": name, "call_id": item["call_id"],
            "observation_id": "range-summary-" + str(index),
            "cache_key": cache_key(session.service, name, arguments),
            "identity": None,
        })
    checkpoint["history"] = history
    context = worker._context(session.session_id, checkpoint)[0]["content"]
    details = json.loads(context.rsplit("\n", 1)[-1])
    summary = details["recent_function_call_results"][-1]["result"]
    assert summary["summary_truncated"] is True
    for key in (
        "query_range", "requested_range", "step_seconds", "sample_count",
        "query_started_at", "query_finished_at", "evidence_scope", "use_for_current_state",
    ):
        assert summary[key] == result[key]
    final_call = details["recent_function_call_results"][-1]
    assert final_call["use_for_current_state"] is False


def test_nanosecond_input_rounds_inward_without_float_loss(prometheus):
    start = START.strftime("%Y-%m-%dT%H:%M:%S") + ".123000001Z"
    end = END.strftime("%Y-%m-%dT%H:%M:%S") + ".124999999Z"
    set_payload(prometheus, matrix([
        [(START + timedelta(seconds=index * 15, milliseconds=124)).timestamp(), "0"]
        for index in range(5)
    ]))
    result = query_metric_range(**(WINDOW | {"start": start, "end": end}))
    assert result["status"] == "ok"
    assert result["requested_range"] == {"start": start, "end": end}
    assert result["query_range"]["start"] == (
        START.replace(microsecond=124000).isoformat()
    )
    assert result["query_range"]["end"] == END.replace(microsecond=124000).isoformat()


@pytest.mark.parametrize("updates,reason", [
    (
        {"end": NOW.strftime("%Y-%m-%dT%H:%M:%S") + ".000000001Z"},
        "metric_range_in_future",
    ),
    (
        {
            "start": (END - timedelta(hours=6)).isoformat(),
            "end": END.strftime("%Y-%m-%dT%H:%M:%S") + ".000000001Z",
            "step_seconds": 300,
        },
        "metric_range_exceeds_6_hours",
    ),
])
def test_nanosecond_precision_does_not_weaken_window_limits(
    prometheus,
    updates,
    reason,
):
    result = query_metric_range(**(WINDOW | updates))
    assert_rejected(result, "invalid_arguments")
    assert result["reason"] == reason
    prometheus.get.assert_not_called()


@pytest.mark.parametrize("field", ["warnings", "infos"])
@pytest.mark.parametrize("value", [None, False, {}, ""])
def test_falsy_but_malformed_api_annotations_are_rejected(prometheus, field, value):
    set_payload(prometheus, matrix() | {field: value})
    assert_rejected(query_metric_range(**WINDOW))


@pytest.mark.parametrize("timestamp", [
    math.nextafter(START.timestamp(), -math.inf),
    math.nextafter(END.timestamp(), math.inf),
])
def test_float_rounding_cannot_move_an_outside_point_onto_the_boundary(
    prometheus,
    timestamp,
):
    set_payload(prometheus, matrix([[timestamp, "0.1"]]))
    result = query_metric_range(**WINDOW)
    assert_rejected(result)
