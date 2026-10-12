"""Prometheus time reliability through tools, cache and temporary Checkpoints."""
from datetime import datetime, timedelta, timezone
import inspect
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from agent.agent import Agent
from clients.prometheus_client import PrometheusClient
from investigation.evidence_policy import cache_key, fresh
from investigation.models import SessionStatus
from investigation.worker import Worker
from runtime_support import SHA, call, create_runtime, create_session, response
from services.metric_profiles import HTTP_METRICS, POSTGRES_METRICS
from tools.metrics import query_metrics
from tools.tool_registry import tool_registry


NOW = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)
FINISHED = NOW + timedelta(milliseconds=400)


def vector(timestamp=NOW.timestamp(), value="0.41821223361699034"):
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [timestamp, value]}],
        },
    }


@pytest.fixture
def prometheus():
    clock = Mock(side_effect=[NOW, FINISHED])
    client = PrometheusClient("http://prometheus.invalid", clock=clock)
    http_response = Mock()
    http_response.json.return_value = vector()
    with patch(
        "clients.prometheus_client.requests.get",
        return_value=http_response,
    ) as request, patch("tools.metrics.prometheus_client", client):
        yield SimpleNamespace(
            client=client,
            clock=clock,
            response=http_response,
            request=request,
        )


def assert_no_sample(result):
    assert result["status"] == "no_data"
    assert "value" not in result
    assert "evaluated_at" not in result
    assert result["query_started_at"] == NOW.isoformat()
    assert result["query_finished_at"] == FINISHED.isoformat()


@pytest.mark.parametrize(
    "timestamp",
    [int(NOW.timestamp()), NOW.timestamp() + 0.123456],
)
def test_integer_and_fractional_evaluation_times(prometheus, timestamp):
    prometheus.response.json.return_value = vector(timestamp)

    result = query_metrics(service="order-service", metric="http_request_rate")

    assert result == {
        "status": "ok",
        "service": "order-service",
        "metric": "http_request_rate",
        "value": 0.41821223361699034,
        "promql": 'sum(rate(demo_http_requests_total{service="order-service"}[1m]))',
        "evaluated_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        "query_started_at": NOW.isoformat(),
        "query_finished_at": FINISHED.isoformat(),
    }
    prometheus.request.assert_called_once_with(
        "http://prometheus.invalid/api/v1/query",
        params={"query": result["promql"]},
        timeout=5,
    )


@pytest.mark.parametrize("value", ["0", "0.0", 0, 0.0])
def test_zero_is_measured_data(prometheus, value):
    prometheus.response.json.return_value = vector(value=value)

    result = query_metrics("order-service", "http_request_rate")

    assert result["status"] == "ok"
    assert result["value"] == 0.0
    assert "evaluated_at" in result


@pytest.mark.parametrize(
    "timestamp",
    [
        None, True, False, "invalid", "1791770400", "NaN", "Infinity",
        [], {}, float("nan"), float("inf"), -float("inf"),
        -1, 253402300800, 1e100, -1e100, 10**500,
    ],
)
def test_invalid_timestamp_is_no_data(prometheus, timestamp):
    prometheus.response.json.return_value = vector(timestamp)

    result = query_metrics("order-service", "http_request_rate")

    assert_no_sample(result)
    assert result["reason"] == "invalid_metrics_timestamp"


@pytest.mark.parametrize("sample", [[], ["1"], None, {}, {"1": "1"}])
def test_missing_timestamp_or_malformed_sample_is_no_data(prometheus, sample):
    prometheus.response.json.return_value["data"]["result"][0]["value"] = sample

    assert_no_sample(query_metrics("order-service", "http_request_rate"))


@pytest.mark.parametrize("age_seconds", [0, 0.4, 5, 15, -0.75, -5])
def test_freshness_accepts_network_delay_and_clock_skew(prometheus, age_seconds):
    timestamp = (FINISHED - timedelta(seconds=age_seconds)).timestamp()
    prometheus.response.json.return_value = vector(timestamp)

    assert query_metrics("order-service", "http_request_rate")["status"] == "ok"


@pytest.mark.parametrize("age_seconds", [15.000001, 60, -5.000001, -3600])
def test_expired_or_future_evaluation_is_no_data(prometheus, age_seconds):
    timestamp = (FINISHED - timedelta(seconds=age_seconds)).timestamp()
    prometheus.response.json.return_value = vector(timestamp)

    assert_no_sample(query_metrics("order-service", "http_request_rate"))


@pytest.mark.parametrize("count", [0, 2])
def test_empty_and_multiple_results_have_no_evaluation_time(prometheus, count):
    sample = prometheus.response.json.return_value["data"]["result"][0]
    prometheus.response.json.return_value["data"]["result"] = [sample] * count

    assert_no_sample(query_metrics("order-service", "http_request_rate"))


@pytest.mark.parametrize(
    "value",
    [True, False, None, "bad", "NaN", "Infinity", "-Inf", {}, [], 10**500],
)
def test_invalid_measurement_is_no_data(prometheus, value):
    prometheus.response.json.return_value = vector(value=value)

    assert_no_sample(query_metrics("order-service", "http_request_rate"))


@pytest.mark.parametrize("error", [requests.ConnectionError, requests.Timeout])
def test_request_failure_preserves_attempt_times_without_leaking(prometheus, error):
    prometheus.request.side_effect = error("sensitive connection details")

    result = query_metrics("order-service", "http_request_rate")

    assert_no_sample(result)
    assert result["reason"] == "metrics_unreachable_or_invalid"
    assert "sensitive" not in json.dumps(result)


@pytest.mark.parametrize("failure", ["http", "json"])
def test_http_and_json_errors_preserve_response_receipt_time(prometheus, failure):
    error = (
        requests.HTTPError("sensitive")
        if failure == "http" else ValueError("bad JSON")
    )
    if failure == "http":
        prometheus.response.raise_for_status.side_effect = error
    else:
        prometheus.response.json.side_effect = error

    assert_no_sample(query_metrics("order-service", "http_request_rate"))


def test_client_still_raises_the_original_exception(prometheus):
    error = requests.Timeout("connection failed")
    prometheus.request.side_effect = error

    with pytest.raises(requests.Timeout) as caught:
        prometheus.client.query("up")

    assert caught.value is error
    assert error.query_started_at == NOW.isoformat()
    assert error.query_finished_at == FINISHED.isoformat()


def test_local_client_timing_cannot_be_overridden_by_server_fields(prometheus):
    prometheus.response.json.return_value.update(
        query_started_at="forged",
        query_finished_at="forged",
    )

    result = query_metrics("order-service", "http_request_rate")

    assert result["query_started_at"] == NOW.isoformat()
    assert result["query_finished_at"] == FINISHED.isoformat()


@pytest.mark.parametrize("failure", [False, True])
def test_plain_mock_query_keeps_compatible_call_and_measured_timing(failure):
    query = Mock(return_value=vector())
    if failure:
        query.side_effect = requests.Timeout("private details")
    with patch("tools.metrics.prometheus_client.query", query), patch(
        "tools.metrics.datetime", wraps=datetime
    ) as clock:
        clock.now.side_effect = [NOW, FINISHED]

        result = query_metrics("order-service", "http_request_rate")

    assert result["query_started_at"] == NOW.isoformat()
    assert result["query_finished_at"] == FINISHED.isoformat()
    if failure:
        assert_no_sample(result)
    else:
        assert result["evaluated_at"] == NOW.isoformat()
    assert len(query.call_args.args) == 1
    assert query.call_args.kwargs == {}


@pytest.mark.parametrize(
    "payload",
    [
        None, [], {"status": "error"},
        {"status": "success", "data": []},
        {"status": "success", "data": {"result": {}}},
        {"status": "success", "data": {"result": [None]}},
        vector() | {"warnings": ["partial data"]},
        {"status": "success", "data": {"resultType": "matrix", "result": []}},
    ],
)
def test_invalid_response_is_controlled_and_keeps_attempt_timing(prometheus, payload):
    prometheus.response.json.return_value = payload

    result = query_metrics("order-service", "http_request_rate")
    assert result["status"] == "no_data"
    assert "evaluated_at" not in result
    assert "value" not in result
    # Non-object JSON cannot carry client timing, so the tool records its own
    # actual call interval instead of inventing Prometheus response metadata.
    if isinstance(payload, dict):
        assert_no_sample(result)
    else:
        assert result["query_started_at"] <= result["query_finished_at"]


@pytest.mark.parametrize(
    "timestamp",
    [None, "bad", NOW.replace(tzinfo=None).isoformat()],
)
def test_invalid_client_timing_is_not_accepted(timestamp):
    payload = vector() | {
        "query_started_at": NOW.isoformat(),
        "query_finished_at": timestamp,
    }
    with patch("tools.metrics.prometheus_client.query", return_value=payload):
        result = query_metrics("order-service", "http_request_rate")
    assert result["status"] == "no_data"
    assert result["reason"] == "invalid_query_timing"
    assert "evaluated_at" not in result


@pytest.mark.parametrize(
    "method,params",
    [
        ("get_alerts", None),
        ("get_rules", {"type": "alert"}),
        ("get_targets", {"state": "active"}),
    ],
)
def test_other_client_endpoints_keep_request_and_response_contract(
    prometheus,
    method,
    params,
):
    payload = {"status": "success", "data": {"items": []}}
    prometheus.response.json.return_value = payload

    assert getattr(prometheus.client, method)() == payload

    arguments = {"timeout": 5}
    if params is not None:
        arguments["params"] = params
    prometheus.request.assert_called_once_with(
        "http://prometheus.invalid/api/v1/" + method.removeprefix("get_"),
        **arguments,
    )
    prometheus.clock.assert_not_called()


@pytest.mark.parametrize(
    "service,metric",
    [("order-service", metric) for metric in HTTP_METRICS]
    + [("order-db", metric) for metric in POSTGRES_METRICS],
)
def test_http_and_postgres_metric_profiles_keep_working(
    prometheus,
    service,
    metric,
):
    prometheus.response.json.return_value = vector(value="1")

    result = query_metrics(service, metric)

    assert result["status"] == "ok"
    assert result["value"] == 1.0
    assert result["service"] == service
    assert result["metric"] == metric
    assert result["evaluated_at"] == NOW.isoformat()


def test_tool_schema_and_required_arguments_stay_compatible():
    parameters = tool_registry["query_metrics"]["schema"]["parameters"]

    assert list(inspect.signature(query_metrics).parameters) == ["service", "metric"]
    assert parameters["required"] == ["service", "metric"]
    assert set(parameters["properties"]) == {"service", "metric"}
    assert all(item["type"] == "string" for item in parameters["properties"].values())


def test_unknown_service_and_unsupported_metric_do_not_issue_requests(prometheus):
    for service, metric in [
        ("unregistered-service", "http_request_rate"),
        ("order-db", "http_request_rate"),
        ("order-service", "postgres_up"),
    ]:
        result = query_metrics(service, metric)
        assert result["status"] == "no_data"
        assert "evaluated_at" not in result
        assert "query_started_at" not in result
        assert "query_finished_at" not in result
    prometheus.request.assert_not_called()


def metric_record(evaluated_at, collected_at=NOW):
    return {
        "name": "query_metrics",
        "arguments": {"service": "order-service", "metric": "http_request_rate"},
        "result": {
            "status": "ok",
            "value": 0.0,
            "evaluated_at": evaluated_at,
        },
        "success": True,
        "collected_at": collected_at.isoformat(),
    }


def test_recently_saved_expired_evaluation_is_not_fresh():
    record = metric_record((NOW - timedelta(seconds=16)).isoformat())

    assert not fresh(record, NOW)


def test_legacy_metric_without_evaluation_time_is_not_current():
    record = metric_record(NOW.isoformat())
    del record["result"]["evaluated_at"]

    assert not fresh(record, NOW)


@pytest.mark.parametrize(
    "evaluated_at",
    [None, True, "bad", NOW.replace(tzinfo=None).isoformat(), "10000-01-01T00:00:00Z"],
)
def test_malformed_or_naive_cached_evaluation_is_not_fresh(evaluated_at):
    assert not fresh(metric_record(evaluated_at), NOW)


def test_cache_respects_both_evaluation_age_and_unchanged_collection_ttl():
    record = metric_record(NOW.isoformat())
    assert fresh(record, NOW + timedelta(seconds=15))
    assert not fresh(record, NOW + timedelta(seconds=15, microseconds=1))
    assert fresh(metric_record((NOW + timedelta(seconds=5)).isoformat()), NOW)
    assert not fresh(metric_record((NOW + timedelta(seconds=6)).isoformat()), NOW)
    assert not fresh(metric_record(NOW.isoformat(), NOW - timedelta(seconds=16)), NOW)
    assert not fresh(metric_record(NOW.isoformat(), NOW + timedelta(seconds=1)), NOW)


@pytest.mark.parametrize("age_seconds", [0, 16])
def test_worker_checkpoint_cache_and_model_context_preserve_original_times(
    tmp_path,
    prometheus,
    age_seconds,
):
    store_clock = SimpleNamespace(now=FINISHED + timedelta(seconds=1))
    manager = create_runtime(tmp_path, clock=lambda: store_clock.now)
    create_session(manager)
    agent = Agent(Mock(), "只读调查，时间来自工具观测。")
    agent.request_durable_response = Mock(side_effect=[
        response(answer="", calls=[call(call_id="first")]),
        response(answer="", calls=[call(call_id="second")]),
        response(),
    ])
    version = {
        "status": "ok",
        "service": "order-service",
        "commit_sha": SHA,
        "process_started_at": (NOW - timedelta(minutes=1)).isoformat(),
    }
    expected_times = {
        "evaluated_at": NOW.isoformat(),
        "query_started_at": NOW.isoformat(),
        "query_finished_at": FINISHED.isoformat(),
    }

    def boundary(phase, session_id):
        if phase == "tool_checkpoint_committed" and prometheus.request.call_count == 1:
            store_clock.now += timedelta(seconds=age_seconds)
            if age_seconds:
                # A later request observes a later evaluation, never a rewritten cache.
                prometheus.clock.side_effect = [store_clock.now, store_clock.now]
                prometheus.response.json.return_value = vector(
                    store_clock.now.timestamp()
                )

    with patch.dict(
        tool_registry["get_running_version"],
        {"func": Mock(return_value=version)},
    ):
        session = Worker(manager, agent, boundary_hook=boundary).run_once()

    assert session.status == SessionStatus.COMPLETED
    checkpoint = manager.store.load_checkpoint(session)
    assert checkpoint["schema_version"] == 1
    records = [
        record for record in checkpoint["records"]
        if record["name"] == "query_metrics"
    ]
    assert len(records) == (2 if age_seconds else 1)
    for key, value in expected_times.items():
        assert records[0]["result"][key] == value
    assert records[0]["collected_at"] == (FINISHED + timedelta(seconds=1)).isoformat()
    assert session.cache_hits == (0 if age_seconds else 1)
    assert prometheus.request.call_count == (2 if age_seconds else 1)

    # Both evidence-rebuilt contexts include the actual temporal fields.
    context = agent.request_durable_response.call_args_list[1].args[0][0]["content"]
    for key, value in expected_times.items():
        assert json.dumps(value) in context
        assert key in context
    details = json.loads(context.split("\n")[-1])
    if age_seconds:
        historical = next(
            item for item in details["historical_observations"]
            if item["name"] == "query_metrics"
        )
        assert historical["expired"] is True
        assert historical["use_for_current_state"] is False
        assert historical["result"]["evaluated_at"] == expected_times["evaluated_at"]
    else:
        call_results = details["recent_function_call_results"]
        first_result = next(
            item["result"] for item in call_results if item["call_id"] == "first"
        )
        assert first_result == records[0]["result"]
        assert call_results[-1]["use_for_current_state"] is True


def test_oversized_metric_summary_preserves_time_metadata():
    timing = {
        "evaluated_at": NOW.isoformat(),
        "query_started_at": NOW.isoformat(),
        "query_finished_at": (NOW + timedelta(milliseconds=400)).isoformat(),
    }
    result = {
        "status": "ok",
        "service": "order-service",
        "metric": "http_request_rate",
        "value": 0.0,
        "promql": 'sum(rate(demo_http_requests_total{service="order-service"}[1m]))',
        **timing,
        **{f"extra_{index}": "x" * 1000 for index in range(10)},
    }

    summary = Worker._observation_summary(result, "query_metrics")

    for key, value in timing.items():
        assert summary.get(key) == value
    assert summary["promql"] == result["promql"]


def test_total_call_summary_limit_preserves_metric_times(tmp_path):
    manager = create_runtime(tmp_path, clock=lambda: NOW)
    session = create_session(manager)
    worker = Worker(manager, Agent(Mock(), "只读证据调查。"))
    checkpoint = manager.store.load_checkpoint(session)
    history = []
    for index in range(5):
        metric_call = index == 4
        name = "query_metrics" if metric_call else "check_service_health"
        arguments = {"service": "order-service"}
        if metric_call:
            arguments["metric"] = "http_request_rate"
            result = metric_record(NOW.isoformat())["result"] | {
                "query_started_at": NOW.isoformat(),
                "query_finished_at": NOW.isoformat(),
            }
        else:
            # Four summaries fit just under 16 KB; the metric crosses the cap.
            result = {"status": "ok"} | {
                f"detail_{field}": str(index) * (
                    600 if index == 3 and field == 3 else 1000
                ) for field in range(4)
            }
        item = call(name, arguments, str(index))
        history.extend([
            item,
            {
                "type": "function_call_output",
                "call_id": item["call_id"],
                "output": json.dumps(result),
            },
        ])
        checkpoint["records"].append({
            "name": name,
            "arguments": arguments,
            "call_id": item["call_id"],
            "result": result,
            "success": True,
            "collected_at": NOW.isoformat(),
            "observation_id": "observation-" + str(index),
            "cache_key": cache_key(session.service, name, arguments),
            "identity": None,
        })
    checkpoint["history"] = history

    context = worker._context(session.session_id, checkpoint)[0]["content"]
    details = json.loads(context.split("\n")[-1])
    metric_summary = details["recent_function_call_results"][-1]["result"]

    assert metric_summary["summary_truncated"] is True
    for field in ("evaluated_at", "query_started_at", "query_finished_at"):
        assert metric_summary[field] == NOW.isoformat()
