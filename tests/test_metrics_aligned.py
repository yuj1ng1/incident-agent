"""Aligned historical evidence through mocked HTTP and temporary SQLite only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from agent.agent import Agent
from clients.prometheus_client import MAX_ALIGNED_RESPONSE_BYTES, PrometheusClient
from investigation.evidence_policy import cache_key, effective_records, fresh
from investigation.models import RuntimeLimits, SessionStatus
from investigation.state import LostClaim
from investigation.store import RuntimeStore
from investigation.worker import Worker
from runtime_support import SHA, call, create_runtime, create_session, response
from services.metric_profiles import HTTP_METRICS, POSTGRES_METRICS, build_promql
from services.registry import ServiceRegistry, registry
from tools.metrics import query_metrics_aligned
from tools.tool_registry import tool_registry


NOW = datetime(2026, 10, 14, 2, tzinfo=timezone.utc)
AT = datetime(2026, 10, 11, 10, 48, 55, tzinfo=timezone.utc)
NAMES = [
    "http_request_rate",
    "http_5xx_rate",
    "service_scrape_up",
    "http_requests_total",
]
ARGUMENTS = {
    "service": "order-service",
    "metrics": ",".join(NAMES[:3]),
    "at": AT.isoformat(),
}


def vector(
    timestamp=AT.timestamp(),
    value="1",
    labels=None,
):
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": labels or {}, "value": [timestamp, value]}],
        },
    }


def http_response(payload):
    reply = requests.Response()
    reply.status_code = 200
    reply._content_consumed = True
    reply._content = json.dumps(payload).encode()
    return reply


@pytest.fixture
def prometheus():
    client = PrometheusClient("http://prometheus.invalid", clock=lambda: NOW)
    pending = []

    def reply(*args, **kwargs):
        stamp = datetime.fromisoformat(kwargs["params"]["time"]).timestamp()
        payload = pending.pop(0) if pending else vector(stamp)
        if isinstance(payload, Exception):
            raise payload
        return (
            payload if isinstance(payload, requests.Response)
            else http_response(payload)
        )

    with (
        patch("clients.prometheus_client.requests.get", side_effect=reply) as get,
        patch("tools.metrics.prometheus_client", client),
        patch("tools.metrics.datetime", wraps=datetime) as clock,
    ):
        clock.now.return_value = NOW
        yield SimpleNamespace(client=client, get=get, pending=pending)


def assert_incomplete(result, metric=NAMES[1]):
    assert result["status"] == "incomplete"
    assert result["comparison_complete"] is False
    assert result["evidence_scope"] == "time_window"
    assert result["use_for_current_state"] is False
    assert result["results"][metric]["status"] == "no_data"
    assert "value" not in result["results"][metric]
    assert result["results"][NAMES[0]]["status"] == "ok"
    assert result["consistency_warnings"] == []


@pytest.mark.parametrize("count", [2, 3, 4])
def test_all_http_requests_use_one_validated_evaluation_time(prometheus, count):
    result = query_metrics_aligned(
        **(ARGUMENTS | {"metrics": ", ".join(NAMES[:count])})
    )
    assert result["status"] == "ok"
    assert result["comparison_complete"] is True
    assert result["requested_metrics"] == NAMES[:count]
    assert list(result["results"]) == NAMES[:count]
    assert result["evaluation_time"] == AT.isoformat()
    assert result["use_for_current_state"] is False
    assert len(result["limitations"]) == 3
    assert prometheus.get.call_count == count
    for metric, request in zip(NAMES, prometheus.get.call_args_list):
        item = result["results"][metric]
        assert item["value"] == 1.0
        assert item["evaluated_at"] == result["evaluation_time"]
        assert item["query_started_at"] == NOW.isoformat()
        assert item["query_finished_at"] == NOW.isoformat()
        assert item["request_executed"] is True
        assert request.args == ("http://prometheus.invalid/api/v1/query",)
        assert request.kwargs["params"] == {
            "query": build_promql(
                "order-service",
                registry.require_capability("order-service", "metrics"),
                metric,
            ),
            "time": AT.isoformat(),
            "timeout": "5s",
        }
        assert 0 < request.kwargs["timeout"] <= 5
        assert request.kwargs["stream"] is True
        assert request.kwargs["allow_redirects"] is False
        assert request.kwargs["headers"]["Accept-Encoding"] == "identity"
    assert len(json.dumps(result).encode()) < 65536


@pytest.mark.parametrize("metrics", [
    None, True, [], "", "http_request_rate", ",http_5xx_rate",
    "http_request_rate,", "http_request_rate,,http_5xx_rate",
    "http_request_rate,  ,http_5xx_rate", "http_request_rate,http_request_rate",
    "http_request_rate, http_request_rate ", ",".join(HTTP_METRICS),
    "a" * 260, "a" * 65 + ",http_request_rate",
    'http_request_rate,up{job="other"}', "http_request_rate,sum(up)",
    "http_request_rate;http_5xx_rate", "http_request_rate，http_5xx_rate",
])
def test_invalid_names_never_reach_prometheus(prometheus, metrics):
    result = query_metrics_aligned(**(ARGUMENTS | {"metrics": metrics}))
    assert result["status"] == "invalid_arguments"
    assert result["comparison_complete"] is False
    assert result["results"] == {}
    prometheus.get.assert_not_called()


@pytest.mark.parametrize("updates,reason", [
    ({"service": "missing"}, "service_not_registered"),
    ({"service": "order-db"}, "metric_not_supported_by_profile"),
    ({"metrics": "http_request_rate,postgres_up"}, "metric_not_supported_by_profile"),
    ({"metrics": "http_request_rate,unknown"}, "metric_not_supported_by_profile"),
])
def test_entire_target_and_profile_are_preflighted(
    prometheus,
    updates,
    reason,
):
    result = query_metrics_aligned(**(ARGUMENTS | updates))
    assert result["reason"] == reason
    assert result["results"] == {}
    prometheus.get.assert_not_called()


@pytest.mark.parametrize("service", [None, True, [], "", "s" * 121])
def test_invalid_service_types_are_controlled(prometheus, service):
    result = query_metrics_aligned(**(ARGUMENTS | {"service": service}))
    assert result["status"] == "invalid_arguments"
    prometheus.get.assert_not_called()


def test_missing_capability_is_not_a_backend_success(prometheus):
    minimal = ServiceRegistry({"minimal": {"name": "minimal", "type": "http"}})
    with patch("tools.metrics.registry", minimal):
        result = query_metrics_aligned(**(ARGUMENTS | {"service": "minimal"}))
    assert result["reason"] == "metrics_capability_not_configured"
    prometheus.get.assert_not_called()


@pytest.mark.parametrize("offset", [8, -5, 0])
@pytest.mark.parametrize("microseconds", [0, 300000, 123456])
def test_timezone_fraction_and_actual_millisecond_time(
    prometheus,
    offset,
    microseconds,
):
    requested = AT.replace(microsecond=microseconds).astimezone(
        timezone(timedelta(hours=offset))
    ).isoformat()
    result = query_metrics_aligned(**(ARGUMENTS | {"at": requested}))
    actual = AT.replace(microsecond=microseconds // 1000 * 1000).isoformat()
    assert result["status"] == "ok"
    assert result["evaluation_time"] == actual
    assert result["requested_evaluation_time"] == requested
    assert result["evaluation_time_adjustment_ns"] == -(microseconds % 1000) * 1000
    assert result["timestamp_resolution"] == "1ms"
    assert {
        request.kwargs["params"]["time"]
        for request in prometheus.get.call_args_list
    } == {actual}


@pytest.mark.parametrize("at", [
    None, True, 1791715735, "", "NaN", "2026-10-11",
    "2026-10-11T10:48:55", "2026-10-11 10:48:55Z",
    "2026-10-11T10:48:55-00:00", "2026-10-11T10:48:55UTC",
    "2026-10-11T10:48:55+24:00", "2026-10-11T10:48:55+01:60",
    "2026-02-30T10:48:55Z", "2026-13-11T10:48:55Z",
    "2026-10-11T24:48:55Z", "2026-10-11T10:48:60Z",
    "2026-10-11T10:48:55.1234567890Z", "1969-12-31T23:59:59.999Z",
    "0001-01-01T00:00:00+08:00", "9999-12-31T23:59:59-08:00",
    (NOW + timedelta(milliseconds=1)).isoformat(),
    "2026-10-14T02:00:00.000000001Z",
    (NOW - timedelta(days=30, milliseconds=1)).isoformat(),
    "2026-09-29T01:59:59.999999999Z",
])
def test_invalid_original_time_is_rejected_before_rounding_or_io(prometheus, at):
    result = query_metrics_aligned(**(ARGUMENTS | {"at": at}))
    assert result["status"] == "invalid_arguments"
    assert result["results"] == {}
    prometheus.get.assert_not_called()


@pytest.mark.parametrize("at", [
    "2026-10-14T02:00:00Z", "2026-09-29T02:00:00Z",
    "2026-10-11T10:48:55.300000001Z",
])
def test_time_boundaries_and_nanoseconds_are_explicit(prometheus, at):
    result = query_metrics_aligned(**(ARGUMENTS | {"at": at}))
    assert result["status"] == "ok"
    assert result["evaluation_time_adjustment_ns"] == (-1 if at.endswith("001Z") else 0)
    assert result["use_for_current_state"] is False


def test_unix_epoch_is_representable_with_a_valid_lookback(prometheus):
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    prometheus.client.clock = lambda: epoch + timedelta(days=1)
    with patch("tools.metrics.datetime", wraps=datetime) as clock:
        clock.now.return_value = epoch + timedelta(days=1)
        result = query_metrics_aligned(**(ARGUMENTS | {"at": epoch.isoformat()}))
    assert result["status"] == "ok"
    assert result["evaluation_time"] == epoch.isoformat()


def test_two_fixed_historical_comparisons_never_mix_observations(prometheus):
    later = AT + timedelta(milliseconds=300)
    prometheus.pending.extend([
        vector(value="0.41821223361699034"), vector(value="1"), vector(),
        vector(later.timestamp(), "0"),
        {"status": "success", "data": {"resultType": "vector", "result": []}},
        vector(later.timestamp()),
    ])
    first = query_metrics_aligned(**(ARGUMENTS | {"at": "2026-10-11T10:48:55.000Z"}))
    second = query_metrics_aligned(**(ARGUMENTS | {"at": "2026-10-11T10:48:55.300Z"}))
    assert first["status"] == "ok"
    assert first["results"][NAMES[0]]["value"] == pytest.approx(0.41821223361699034)
    assert first["results"][NAMES[1]]["value"] == 1.0
    assert_incomplete(second)
    assert second["results"][NAMES[0]]["value"] == 0.0
    assert second["results"][NAMES[1]]["reason"] == "no_samples_at_evaluation_time"
    assert first["evaluation_time"] != second["evaluation_time"]
    requested_times = [
        request.kwargs["params"]["time"]
        for request in prometheus.get.call_args_list
    ]
    assert requested_times == [AT.isoformat()] * 3 + [later.isoformat()] * 3


@pytest.mark.parametrize("stamp", [
    None, True, "1791715735", -1, 253402300800, float("nan"), float("inf"),
    10**500, (AT + timedelta(milliseconds=1)).timestamp(),
    math.nextafter(AT.timestamp(), -math.inf),
    math.nextafter(AT.timestamp(), math.inf),
])
def test_bad_or_mismatched_sample_time_cannot_enter_aligned_values(prometheus, stamp):
    prometheus.pending.extend([vector(), vector(stamp), vector()])
    result = query_metrics_aligned(**ARGUMENTS)
    assert_incomplete(result)
    assert result["results"][NAMES[2]]["status"] == "ok"
    if stamp == (AT + timedelta(milliseconds=1)).timestamp():
        failed = result["results"][NAMES[1]]
        assert failed["reason"] == "metric_evaluation_time_mismatch"
        assert failed["observed_evaluated_at"] != result["evaluation_time"]


@pytest.mark.parametrize("payload", [
    None, [], {"status": "error"}, {"status": "success", "data": []},
    {"status": "success", "data": {"resultType": "matrix", "result": []}},
    {"status": "success", "data": {"resultType": "vector", "result": {}}},
    {"status": "success", "data": {"resultType": "vector", "result": [None]}},
    {"status": "success", "data": {"resultType": "vector", "result": [{"value": []}]}},
    vector() | {"warnings": ["partial result"]},
    vector() | {"infos": ["omitted samples"]},
])
def test_malformed_response_keeps_other_real_observations(prometheus, payload):
    prometheus.pending.extend([vector(), payload])
    assert_incomplete(query_metrics_aligned(**ARGUMENTS))


@pytest.mark.parametrize("field", ["warnings", "infos"])
@pytest.mark.parametrize("value", [None, False, {}, ""])
def test_malformed_annotations_are_not_treated_as_complete(
    prometheus,
    field,
    value,
):
    prometheus.pending.extend([vector(), vector() | {field: value}])
    assert_incomplete(query_metrics_aligned(**ARGUMENTS))


@pytest.mark.parametrize("case", [
    "multiple", "histogram", "short_pair", "extra_pair", "bad_labels",
])
def test_ambiguous_or_unsupported_series_are_rejected(prometheus, case):
    payload = vector()
    item = payload["data"]["result"][0]
    if case == "multiple":
        payload["data"]["result"] *= 2
    elif case == "histogram":
        item["histogram"] = []
    elif case == "short_pair":
        item["value"] = [AT.timestamp()]
    elif case == "extra_pair":
        item["value"].append("extra")
    else:
        item["metric"] = {"invalid": 1}
    prometheus.pending.extend([vector(), payload])
    assert_incomplete(query_metrics_aligned(**ARGUMENTS))


@pytest.mark.parametrize("value", [
    None, True, False, [], {}, "bad", "NaN", "Infinity", "1e309", " 0.1 ",
    float("nan"), float("inf"), -float("inf"), 10**500, "-1", "1.01",
])
def test_invalid_values_never_become_success_evidence(prometheus, value):
    prometheus.pending.extend([vector(), vector(value=value)])
    assert_incomplete(query_metrics_aligned(**ARGUMENTS))


@pytest.mark.parametrize("value", ["0", "0.0", 0, 0.0, "1e-1"])
def test_legal_zero_and_decimal_values_remain_observations(prometheus, value):
    prometheus.pending.extend([vector(value=value), vector(value=value)])
    result = query_metrics_aligned(**ARGUMENTS)
    assert result["status"] == "ok"
    assert result["results"][NAMES[0]]["value"] == float(value)
    assert result["results"][NAMES[1]]["value"] == float(value)


def test_candidate_numeric_warning_requires_complete_alignment(prometheus):
    prometheus.pending.extend([vector(value="0"), vector(value="1"), vector()])
    result = query_metrics_aligned(**ARGUMENTS)
    assert result["status"] == "ok"
    warning = result["consistency_warnings"][0]
    assert warning["code"] == "zero_request_rate_with_positive_5xx_ratio"
    assert "不能据此认定根因" in warning["message"]


@pytest.mark.parametrize("error", [
    requests.Timeout, requests.ConnectionError, requests.HTTPError,
])
def test_partial_network_failure_preserves_times_without_private_details(
    prometheus,
    error,
):
    failure = error("private credentials and backend URL")
    prometheus.pending.extend([vector(), failure])
    result = query_metrics_aligned(**ARGUMENTS)
    assert_incomplete(result)
    assert prometheus.get.call_count == 3
    assert failure._request_attempted is True
    assert result["results"][NAMES[1]]["request_executed"] is True
    assert result["results"][NAMES[1]]["query_started_at"] == NOW.isoformat()
    assert result["results"][NAMES[1]]["query_finished_at"] == NOW.isoformat()
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("body", [
    b"not JSON", b'{"status":"success","status":"error"}',
    b'{"a": NaN}', b'{"a": Infinity}', b"\xff", b"[" * 2000,
])
def test_invalid_json_does_not_crash_the_tool(prometheus, body):
    reply = http_response({})
    reply._content = body
    prometheus.pending.extend([vector(), reply])
    assert_incomplete(query_metrics_aligned(**ARGUMENTS))


@pytest.mark.parametrize("mode", ["header", "stream", "lying_header"])
def test_response_size_is_bounded_independently_for_each_request(prometheus, mode):
    reply = http_response(vector())
    if mode == "header":
        reply.headers["Content-Length"] = str(MAX_ALIGNED_RESPONSE_BYTES + 1)
    else:
        reply._content = b" " * (MAX_ALIGNED_RESPONSE_BYTES + 1)
        if mode == "lying_header":
            reply.headers["Content-Length"] = "1"
    prometheus.pending.extend([vector(), reply])
    result = query_metrics_aligned(**ARGUMENTS)
    assert_incomplete(result)
    assert result["results"][NAMES[1]]["reason"] == "metric_aligned_response_size_limit"


def test_four_maximum_sized_http_bodies_still_produce_a_bounded_result(prometheus):
    for _ in range(4):
        reply = http_response(vector(labels={"instance": "valid"}))
        reply._content += b" " * (MAX_ALIGNED_RESPONSE_BYTES - len(reply.content))
        reply.headers["Content-Length"] = str(MAX_ALIGNED_RESPONSE_BYTES)
        prometheus.pending.append(reply)
    result = query_metrics_aligned(**(ARGUMENTS | {"metrics": ",".join(NAMES)}))
    assert result["status"] == "ok"
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 65536


@pytest.mark.parametrize("headers,status,reason", [
    ({"Content-Length": "bad"}, 200, "invalid_metric_aligned_content_length"),
    ({"Content-Encoding": "gzip"}, 200, "metric_aligned_content_encoding_refused"),
    ({}, 302, "metric_aligned_redirect_refused"),
    ({}, 500, "metrics_aligned_unreachable_or_invalid"),
])
def test_transport_safety_limits_do_not_discard_other_metrics(
    prometheus,
    headers,
    status,
    reason,
):
    reply = http_response(vector())
    reply.headers.update(headers)
    reply.status_code = status
    prometheus.pending.extend([vector(), reply])
    with patch.object(reply, "iter_content") as read:
        result = query_metrics_aligned(**ARGUMENTS)
    read.assert_not_called()
    assert_incomplete(result)
    assert prometheus.get.call_count == 3
    assert result["results"][NAMES[1]]["request_executed"] is True
    assert result["results"][NAMES[1]]["reason"] == reason


def test_total_deadline_shrinks_timeouts_and_stops_unsent_requests(prometheus):
    monotonic = Mock(return_value=0.0)
    calls = 0

    def reply(*args, **kwargs):
        nonlocal calls
        calls += 1
        monotonic.return_value = calls * 4 if calls < 3 else 10.1
        return http_response(vector())

    prometheus.get.side_effect = reply
    with patch("clients.prometheus_client.time.monotonic", monotonic):
        result = query_metrics_aligned(**(ARGUMENTS | {"metrics": ",".join(NAMES)}))
    assert result["status"] == "incomplete"
    assert prometheus.get.call_count == 3
    assert [
        request.kwargs["timeout"] for request in prometheus.get.call_args_list
    ] == [5, 5, 2]
    assert result["results"][NAMES[0]]["status"] == "ok"
    assert result["results"][NAMES[1]]["status"] == "ok"
    assert result["results"][NAMES[2]]["status"] == "no_data"
    assert result["results"][NAMES[2]]["request_executed"] is True
    assert result["results"][NAMES[3]]["reason"] == "metric_aligned_total_timeout"
    assert result["results"][NAMES[3]]["request_executed"] is False
    assert result["results"][NAMES[3]]["query_started_at"] is None
    assert result["results"][NAMES[3]]["query_finished_at"] is None


@pytest.mark.parametrize("expired_at", [10.0, 10.1])
def test_deadline_expires_between_caller_and_transport_checks(
    prometheus,
    expired_at,
):
    # Deadline=10; the caller passes at 9.9, but transport sees expiry.
    monotonic = Mock(side_effect=[0.0, 9.9, expired_at, expired_at, expired_at])
    with (
        patch("clients.prometheus_client.time.monotonic", monotonic),
        patch.object(
            prometheus.client,
            "query_at",
            wraps=prometheus.client.query_at,
        ) as query,
    ):
        result = query_metrics_aligned(**ARGUMENTS)
    query.assert_called_once()
    prometheus.get.assert_not_called()
    assert result["status"] == "no_data"
    assert result["comparison_complete"] is False
    assert result["results"][NAMES[0]]["reason"] == (
        "metrics_aligned_unreachable_or_invalid"
    )
    assert result["results"][NAMES[1]]["reason"] == "metric_aligned_total_timeout"
    for item in result["results"].values():
        assert item["request_executed"] is False
        assert item["query_started_at"] is None
        assert item["query_finished_at"] is None


def test_client_time_validation_failure_is_not_a_request_attempt(prometheus):
    prometheus.client.clock = lambda: AT - timedelta(seconds=1)
    result = query_metrics_aligned(**ARGUMENTS)
    prometheus.get.assert_not_called()
    assert result["status"] == "no_data"
    for item in result["results"].values():
        assert item["request_executed"] is False
        assert item["query_started_at"] is None
        assert item["query_finished_at"] is None


def test_all_missing_is_not_reported_as_a_success(prometheus):
    empty = {"status": "success", "data": {"resultType": "vector", "result": []}}
    prometheus.pending.extend([empty] * 3)
    result = query_metrics_aligned(**ARGUMENTS)
    assert result["status"] == "no_data"
    assert result["comparison_complete"] is False
    assert len(result["results"]) == 3
    assert all("value" not in item for item in result["results"].values())
    assert prometheus.get.call_count == 3
    assert all(item["request_executed"] is True for item in result["results"].values())


def test_server_fields_cannot_forge_local_request_times(prometheus):
    prometheus.pending.extend([
        vector() | {
            "query_started_at": "forged",
            "query_finished_at": "forged",
            "request_executed": False,
            "_request_attempted": False,
        },
    ])
    result = query_metrics_aligned(**ARGUMENTS)
    assert result["results"][NAMES[0]]["request_executed"] is True
    assert result["results"][NAMES[0]]["query_started_at"] == NOW.isoformat()
    assert result["results"][NAMES[0]]["query_finished_at"] == NOW.isoformat()


@pytest.mark.parametrize("bad_time", [
    None, True, "bad", NOW.replace(tzinfo=None).isoformat(),
])
def test_invalid_client_timing_is_not_verified_evidence(prometheus, bad_time):
    payload = vector() | {
        "query_started_at": NOW.isoformat(),
        "query_finished_at": bad_time,
    }
    with patch.object(prometheus.client, "query_at", return_value=payload):
        result = query_metrics_aligned(**ARGUMENTS)
    assert result["status"] == "no_data"
    assert all(
        item["reason"] == "invalid_query_timing"
        for item in result["results"].values()
    )


@pytest.mark.parametrize("service,names", [
    ("order-service", HTTP_METRICS[:4]),
    ("order-db", POSTGRES_METRICS[:4]),
    ("order-db", POSTGRES_METRICS[4:8]),
    ("order-db", POSTGRES_METRICS[-2:]),
])
def test_both_profiles_reuse_existing_promql_without_modification(
    prometheus,
    service,
    names,
):
    result = query_metrics_aligned(**(ARGUMENTS | {
        "service": service,
        "metrics": ",".join(names),
    }))
    assert result["status"] == "ok"
    settings = registry.require_capability(service, "metrics")
    assert all(
        result["results"][name]["promql"] == build_promql(service, settings, name)
        for name in names
    )


def test_schema_uses_only_the_three_required_strings():
    schema = tool_registry["query_metrics_aligned"]["schema"]["parameters"]
    assert schema["required"] == ["service", "metrics", "at"]
    assert set(schema["properties"]) == set(ARGUMENTS)
    assert all(field["type"] == "string" for field in schema["properties"].values())
    assert "query_at" not in tool_registry


@pytest.mark.parametrize("timestamp", [
    int(AT.timestamp()), (AT + timedelta(milliseconds=300)).timestamp(),
])
def test_integer_and_fractional_sample_times_match_the_requested_instant(
    prometheus,
    timestamp,
):
    at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    prometheus.pending.extend([vector(timestamp), vector(timestamp), vector(timestamp)])
    result = query_metrics_aligned(**(ARGUMENTS | {"at": at}))
    assert result["status"] == "ok"
    assert all(item["evaluated_at"] == at for item in result["results"].values())


@pytest.mark.parametrize("at", [
    "2026-10-11T10:48:55", (NOW + timedelta(milliseconds=1)).isoformat(),
    (NOW - timedelta(days=31)).isoformat(),
])
def test_client_revalidates_time_before_http(prometheus, at):
    with pytest.raises(ValueError):
        prometheus.client.query_at("trusted-profile-expression", at)
    prometheus.get.assert_not_called()


@pytest.mark.parametrize("expired_at", [10.0, 10.1])
def test_expired_shared_deadline_does_not_start_http(prometheus, expired_at):
    with patch("clients.prometheus_client.time.monotonic", return_value=expired_at):
        with pytest.raises(requests.Timeout) as error:
            prometheus.client.query_at(
                "trusted-profile-expression",
                AT.isoformat(),
                deadline=10,
            )
    prometheus.get.assert_not_called()
    assert error.value._request_attempted is False
    assert error.value.query_started_at is None
    assert error.value.query_finished_at is None


def test_oversized_trusted_expression_is_rejected_before_all_io(prometheus):
    with patch("tools.metrics.build_promql", return_value="x" * 4097):
        result = query_metrics_aligned(**ARGUMENTS)
    assert result["status"] == "no_data"
    assert result["reason"] == "metric_aligned_promql_size_limit"
    prometheus.get.assert_not_called()


def test_oversized_labels_fail_only_the_affected_metric(prometheus):
    prometheus.pending.extend([
        vector(), vector(labels={str(index): "x" * 256 for index in range(10)}),
    ])
    result = query_metrics_aligned(**ARGUMENTS)
    assert_incomplete(result)
    assert result["results"][NAMES[1]]["reason"] == "metric_aligned_label_size_limit"


@pytest.mark.parametrize("metric,value", [
    ("http_request_rate", "-1"), ("service_scrape_up", "0.5"),
    ("http_requests_total", "-1"), ("http_latency_p95_seconds", "-1"),
])
def test_each_metric_obeys_its_existing_numeric_domain(
    prometheus,
    metric,
    value,
):
    prometheus.pending.extend([vector(), vector(value=value)])
    result = query_metrics_aligned(**(ARGUMENTS | {
        "metrics": "http_5xx_rate," + metric,
    }))
    assert result["status"] == "incomplete"
    assert result["results"][metric]["reason"] == "metric_value_outside_semantic_domain"


def aligned_record(result, arguments=None):
    arguments = arguments or ARGUMENTS
    return {
        "name": "query_metrics_aligned",
        "arguments": arguments,
        "result": result,
        "success": True,
        "collected_at": NOW.isoformat(),
        "identity": {"commit_sha": SHA},
        "cache_key": cache_key(
            "order-service", "query_metrics_aligned", arguments, {"commit_sha": SHA},
        ),
    }


def test_successful_cache_ttl_never_grants_current_health_qualification(prometheus):
    record = aligned_record(query_metrics_aligned(**ARGUMENTS))
    assert fresh(record, NOW + timedelta(seconds=30))
    assert not fresh(record, NOW + timedelta(seconds=30, microseconds=1))
    assert not fresh(record, NOW - timedelta(seconds=1))
    assert effective_records([record], record["identity"], NOW, "order-service") == []
    without_scope = record | {"result": {"status": "ok"}}
    assert effective_records([without_scope], record["identity"], NOW) == []


@pytest.mark.parametrize("attempted", [False, True])
def test_failed_request_audit_survives_checkpoint_without_changing_budget_or_cache(
    tmp_path,
    prometheus,
    attempted,
):
    manager = create_runtime(tmp_path, clock=lambda: NOW)
    create_session(manager)
    session = manager.store.claim("audit-owner", manager.limits)
    claim = (session.owner_token, session.fencing_token)
    checkpoint = manager.store.load_checkpoint(session)
    worker = Worker(manager, Agent(Mock(), "只读"))
    item = SimpleNamespace(
        name="query_metrics_aligned",
        arguments=json.dumps(ARGUMENTS),
        call_id="audit-first",
    )
    if attempted:
        prometheus.pending.extend([vector(), requests.Timeout("private timeout")])
        result, cached = worker._tool(session.session_id, claim, checkpoint, item)
    else:
        query_at = prometheus.client.query_at
        monotonic = Mock(return_value=0.0)

        def expire_before_transport(*args, **kwargs):
            monotonic.return_value = kwargs["deadline"]
            return query_at(*args, **kwargs)

        with (
            patch("clients.prometheus_client.time.monotonic", monotonic),
            patch.object(
                prometheus.client,
                "query_at",
                side_effect=expire_before_transport,
            ),
        ):
            result, cached = worker._tool(session.session_id, claim, checkpoint, item)
    assert cached is False
    assert result["status"] == ("incomplete" if attempted else "no_data")
    assert prometheus.get.call_count == (3 if attempted else 0)
    assert manager.store.get(session.session_id).total_tool_executions == 1
    assert not fresh(aligned_record(result), NOW)
    reopened = RuntimeStore(manager.store.path, clock=lambda: NOW)
    restored = reopened.load_checkpoint(reopened.get(session.session_id))
    assert restored["schema_version"] == 1
    assert restored["records"][0]["result"] == result
    failed_metric = NAMES[1] if attempted else NAMES[0]
    summary = worker._observation_summary(result, "query_metrics_aligned")
    for output in (restored["records"][0]["result"], summary):
        audit = output["results"][failed_metric]
        assert audit["request_executed"] is attempted
        assert audit["query_started_at"] == (NOW.isoformat() if attempted else None)
        assert audit["query_finished_at"] == (NOW.isoformat() if attempted else None)

    item.call_id = "audit-retry"
    successful, cached = worker._tool(session.session_id, claim, restored, item)
    assert successful["status"] == "ok"
    assert cached is False
    assert prometheus.get.call_count == (6 if attempted else 3)
    assert manager.store.get(session.session_id).total_tool_executions == 2
    assert len(restored["records"]) == 2
    item.call_id = "audit-cached"
    reused, cached = worker._tool(session.session_id, claim, restored, item)
    assert reused == successful
    assert cached is True
    assert prometheus.get.call_count == (6 if attempted else 3)
    saved = manager.store.get(session.session_id)
    assert saved.total_tool_executions == 2
    assert saved.total_model_requests == 0
    assert saved.cache_hits == 1


def test_cache_key_isolates_metric_set_time_target_profile_and_runtime_identity():
    identity = {"commit_sha": SHA, "process_started_at": AT.isoformat()}
    key = cache_key("order-service", "query_metrics_aligned", ARGUMENTS, identity)
    for updates in (
        {"metrics": ",".join(NAMES[:2])},
        {"metrics": ",".join(reversed(NAMES[:3]))},
        {"at": (AT + timedelta(milliseconds=300)).isoformat()},
        {"service": "inventory-service"},
    ):
        assert cache_key(
            "order-service", "query_metrics_aligned", ARGUMENTS | updates, identity,
        ) != key
    assert cache_key(
        "inventory-service", "query_metrics_aligned", ARGUMENTS, identity,
    ) != key
    for changed in (
        identity | {"commit_sha": "b" * 40},
        identity | {"process_started_at": NOW.isoformat()},
    ):
        assert cache_key(
            "order-service", "query_metrics_aligned", ARGUMENTS, changed,
        ) != key
    profile = registry.get("order-service")
    profile = profile.model_copy(update={
        "metrics": profile.metrics.model_copy(update={
            "labels": {"service": "changed"},
        }),
    })
    changed_registry = ServiceRegistry([profile, registry.get("order-db")])
    with patch("investigation.evidence_policy.registry", changed_registry):
        assert cache_key(
            "order-service", "query_metrics_aligned", ARGUMENTS, identity,
        ) != key


@pytest.mark.parametrize("age,missing", [(0, False), (31, False), (0, True)])
def test_full_checkpoint_cache_and_bounded_model_context(
    tmp_path,
    prometheus,
    age,
    missing,
):
    arguments = ARGUMENTS | {"metrics": ",".join(NAMES)}
    store_time = SimpleNamespace(now=NOW)
    manager = create_runtime(tmp_path, clock=lambda: store_time.now)
    create_session(manager)
    if missing:
        def reply(*args, **kwargs):
            if prometheus.get.call_count % 4 == 0:
                return http_response({
                    "status": "success",
                    "data": {"resultType": "vector", "result": []},
                })
            return http_response(vector())
        prometheus.get.side_effect = reply
    agent = Agent(Mock(), "只读历史证据不能判断当前健康。")
    agent.request_durable_response = Mock(side_effect=[
        response(answer="", calls=[call("query_metrics_aligned", arguments, "first")]),
        response(answer="", calls=[call("query_metrics_aligned", arguments, "second")]),
        response(),
    ])

    def boundary(phase, session_id):
        if phase == "tool_checkpoint_committed" and prometheus.get.call_count == 4:
            store_time.now = NOW + timedelta(seconds=age)

    with patch.dict(tool_registry["get_running_version"], {
        "func": Mock(return_value={
            "status": "ok", "service": "order-service", "commit_sha": SHA,
        }),
    }):
        completed = Worker(
            manager,
            agent,
            boundary_hook=boundary,
            provider_verified=True,
        ).run_once()
    assert completed.status == SessionStatus.COMPLETED
    cached = not age and not missing
    assert completed.cache_hits == int(cached)
    assert completed.total_tool_executions == (2 if cached else 3)
    assert prometheus.get.call_count == (4 if cached else 8)
    checkpoint = manager.store.load_checkpoint(completed)
    records = [
        item for item in checkpoint["records"]
        if item["name"] == "query_metrics_aligned"
    ]
    assert len(records) == (1 if cached else 2)
    assert checkpoint["schema_version"] == 1
    assert list(records[0]["result"]["results"]) == NAMES
    assert all("promql" in item for item in records[0]["result"]["results"].values())
    reopened = RuntimeStore(manager.store.path, clock=lambda: store_time.now)
    restored = reopened.load_checkpoint(reopened.get(completed.session_id))
    assert restored["records"] == checkpoint["records"]
    context = agent.request_durable_response.call_args_list[-1].args[0][0]["content"]
    details = json.loads(context.rsplit("\n", 1)[-1])
    group = "failed_or_missing_observations" if missing else "historical_observations"
    summaries = [
        item for item in details[group] if item["name"] == "query_metrics_aligned"
    ]
    assert summaries
    for item in summaries:
        summary = item["result"]
        assert item["use_for_current_state"] is False
        assert summary["evaluation_time"] == AT.isoformat()
        assert summary["requested_metrics"] == NAMES
        assert len(summary["results"]) == 4
        assert summary["comparison_complete"] is (not missing)
        assert summary["limitations"]
        assert summary["query_started_at"] == NOW.isoformat()
        assert len(json.dumps(summary, ensure_ascii=False).encode()) < 5000
    assert all(
        not item["use_for_current_state"]
        for item in details["recent_function_call_results"]
        if item["name"] == "query_metrics_aligned"
    )
    current = effective_records(
        checkpoint["records"], checkpoint["identity"], store_time.now, "order-service",
    )
    assert not any(item["name"] == "query_metrics_aligned" for item in current)


@pytest.mark.parametrize("updates", [
    {"service": "inventory-service"}, {"service": "missing"},
    {"promql": "up"}, {"metrics": ["http_request_rate", "http_5xx_rate"]},
])
def test_session_permissions_and_schema_reject_before_io_or_budget(
    tmp_path,
    prometheus,
    updates,
):
    manager = create_runtime(tmp_path, clock=lambda: NOW)
    create_session(manager)
    session = manager.store.claim("aligned-owner", manager.limits)
    checkpoint = manager.store.load_checkpoint(session)
    result, cached = Worker(manager, Agent(Mock(), "只读"))._tool(
        session.session_id,
        (session.owner_token, session.fencing_token),
        checkpoint,
        SimpleNamespace(
            name="query_metrics_aligned",
            arguments=json.dumps(ARGUMENTS | updates),
            call_id="rejected",
        ),
    )
    assert result["status"] == "invalid_or_unallowed_tool_call"
    assert cached is False
    assert checkpoint["records"] == []
    assert manager.store.get(session.session_id).total_tool_executions == 0
    prometheus.get.assert_not_called()


def test_registered_dependency_is_allowed_through_existing_session_boundary(
    tmp_path,
    prometheus,
):
    manager = create_runtime(tmp_path, clock=lambda: NOW)
    create_session(manager)
    session = manager.store.claim("aligned-owner", manager.limits)
    checkpoint = manager.store.load_checkpoint(session)
    arguments = ARGUMENTS | {
        "service": "order-db",
        "metrics": "postgres_up,postgres_connections",
    }
    result, cached = Worker(manager, Agent(Mock(), "只读"))._tool(
        session.session_id,
        (session.owner_token, session.fencing_token),
        checkpoint,
        SimpleNamespace(
            name="query_metrics_aligned",
            arguments=json.dumps(arguments),
            call_id="dependency",
        ),
    )
    assert result["status"] == "ok"
    assert not cached
    assert manager.store.get(session.session_id).total_tool_executions == 1
    assert prometheus.get.call_count == 2


def test_existing_cumulative_budget_cannot_be_bypassed(tmp_path, prometheus):
    manager = create_runtime(
        tmp_path,
        clock=lambda: NOW,
        limits=RuntimeLimits(max_tool_executions=1),
    )
    create_session(manager)
    agent = Agent(Mock(), "只读")
    agent.request_durable_response = Mock(return_value=response(
        answer="", calls=[call("query_metrics_aligned", ARGUMENTS)],
    ))
    with patch.dict(tool_registry["get_running_version"], {
        "func": Mock(return_value={"status": "no_data"}),
    }):
        completed = Worker(manager, agent).run_once()
    assert completed.status == SessionStatus.EXHAUSTED
    assert completed.total_tool_executions == 1
    prometheus.get.assert_not_called()


def test_lost_lease_prevents_aligned_query_io(tmp_path, prometheus):
    manager = create_runtime(tmp_path, clock=lambda: NOW)
    create_session(manager)
    session = manager.store.claim("aligned-owner", manager.limits)
    with pytest.raises(LostClaim):
        Worker(manager, Agent(Mock(), "只读"))._tool(
            session.session_id, ("different-owner", session.fencing_token),
            manager.store.load_checkpoint(session),
            SimpleNamespace(
                name="query_metrics_aligned",
                arguments=json.dumps(ARGUMENTS),
                call_id="lost",
            ),
        )
    prometheus.get.assert_not_called()


def test_summary_keeps_all_four_values_and_times_without_mutating_checkpoint(
    prometheus,
):
    result = query_metrics_aligned(**(ARGUMENTS | {"metrics": ",".join(NAMES)}))
    result["results"][NAMES[0]]["promql"] = "long metadata " * 2000
    original = deepcopy(result)
    summary = Worker._observation_summary(result, "query_metrics_aligned")
    assert result == original
    assert len(json.dumps(summary, ensure_ascii=False).encode()) < 5000
    assert summary["requested_metrics"] == NAMES
    assert list(summary["results"]) == NAMES
    assert summary["summary_truncated"] is True
    for metric in NAMES:
        assert summary["results"][metric]["value"] == result["results"][metric]["value"]
        assert summary["results"][metric]["evaluated_at"] == AT.isoformat()
        assert summary["results"][metric]["query_started_at"] == NOW.isoformat()


def test_total_context_fallback_retains_all_alignment_fields(tmp_path, prometheus):
    result = query_metrics_aligned(**(ARGUMENTS | {"metrics": ",".join(NAMES)}))
    manager = create_runtime(tmp_path, clock=lambda: NOW)
    session = create_session(manager)
    worker = Worker(manager, Agent(Mock(), "只读"))
    checkpoint = manager.store.load_checkpoint(session)
    checkpoint["history"] = []
    prior_size = 0
    for index in range(5):
        aligned = index == 4
        name = "query_metrics_aligned" if aligned else "get_recent_code_changes"
        arguments = ARGUMENTS if aligned else {"service": "order-service"}
        output = result if aligned else {
            "status": "ok",
            **{
                f"detail_{field}": "x" * (750 if index == 3 else 1000)
                for field in range(3 if index == 3 else 4)
            },
        }
        if not aligned:
            prior_size += len(json.dumps(
                worker._observation_summary(output, name), ensure_ascii=False,
            ).encode())
        item = call(name, arguments, str(index))
        checkpoint["history"].extend([
            item,
            {
                "type": "function_call_output",
                "call_id": item["call_id"],
                "output": json.dumps(output),
            },
        ])
        checkpoint["records"].append(aligned_record(output, arguments) | {
            "name": name,
            "call_id": item["call_id"],
            "observation_id": "obs-" + str(index),
            "identity": None,
            "cache_key": cache_key(session.service, name, arguments),
        })
    assert prior_size < 16000
    aligned_summary_size = len(json.dumps(
        worker._observation_summary(result, "query_metrics_aligned"),
        ensure_ascii=False,
    ).encode())
    assert prior_size + aligned_summary_size > 16000
    context = worker._context(session.session_id, checkpoint)[0]["content"]
    details = json.loads(context.rsplit("\n", 1)[-1])
    last = details["recent_function_call_results"][-1]
    assert last["use_for_current_state"] is False
    assert last["result"]["requested_metrics"] == NAMES
    assert len(last["result"]["results"]) == 4
    for key in (
        "evaluation_time", "comparison_complete", "limitations",
        "query_started_at", "query_finished_at",
    ):
        assert last["result"][key] == result[key]
