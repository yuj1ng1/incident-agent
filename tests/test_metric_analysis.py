"""P5.3a: fixed historical observations, never fresh measurements.

The two 2026-10-11 instants come from the P5.2c validation record. The request
rate pair and sparse 5xx series reproduce the user-supplied historical fixture;
0.7273 is its stated approximation. No live Prometheus or model is used.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest
import requests

from investigation.metric_analysis import (
    MAX_ANALYSIS_INPUT_BYTES,
    MAX_ANALYSIS_OBSERVATIONS,
    MAX_ANALYSIS_OUTPUT_BYTES,
    MAX_ANALYSIS_SAMPLES,
    MAX_REPORTED_CHANGES,
    MetricToolResult,
    RangeFinding,
    analyze_checkpoint_metrics,
    analyze_metric_evidence,
)
from investigation.models import Checkpoint, ToolObservation


SERVICE = "order-service"
AT = datetime(2026, 10, 11, 10, 48, 55, tzinfo=timezone.utc)
LATER = AT + timedelta(milliseconds=300)
COLLECTED = datetime(2026, 10, 14, 2, tzinfo=timezone.utc)
NAMES = ["http_request_rate", "http_5xx_rate", "service_scrape_up"]
SHA = "a" * 40


def instant(
    observation_id="instant-1",
    metric="http_request_rate",
    value=0.0,
    at=AT,
):
    return MetricToolResult(
        observation_id,
        "query_metrics",
        {
            "status": "ok",
            "service": SERVICE,
            "metric": metric,
            "value": value,
            "evaluated_at": at.isoformat(),
        },
    )


def aligned(
    observation_id="aligned-1",
    at=AT,
    values=(0.41821223361699034, 1.0, 1.0),
):
    results = {}
    for metric, value in zip(NAMES, values):
        item = {
            "status": "ok" if value is not None else "no_data",
            "metric": metric,
            "request_executed": True,
            "query_started_at": COLLECTED.isoformat(),
            "query_finished_at": COLLECTED.isoformat(),
        }
        if value is not None:
            item.update(value=value, evaluated_at=at.isoformat())
        else:
            item["reason"] = "no_samples_at_evaluation_time"
        results[metric] = item
    complete = all(value is not None for value in values)
    return MetricToolResult(
        observation_id,
        "query_metrics_aligned",
        {
            "status": "ok" if complete else "incomplete",
            "service": SERVICE,
            "evidence_scope": "time_window",
            "use_for_current_state": False,
            "evaluation_time": at.isoformat(),
            "requested_evaluation_time": at.isoformat(),
            "requested_metrics": list(NAMES),
            "comparison_complete": complete,
            "results": results,
            "query_started_at": COLLECTED.isoformat(),
            "query_finished_at": COLLECTED.isoformat(),
        },
    )


def history(
    observation_id="range-1",
    metric="http_request_rate",
    start=AT - timedelta(seconds=10),
    expected_points=2,
    values=None,
):
    if values is None:
        values = {0: 0.7273, 1: 0.0}
    end = start + timedelta(seconds=15 * (expected_points - 1))
    samples = [
        {
            "evaluated_at": (start + timedelta(seconds=15 * index)).isoformat(),
            "value": value,
        }
        for index, value in sorted(values.items())
    ]
    missing = [
        (start + timedelta(seconds=15 * index)).isoformat()
        for index in range(expected_points) if index not in values
    ]
    return MetricToolResult(
        observation_id,
        "query_metric_range",
        {
            "status": "ok" if samples else "no_data",
            "service": SERVICE,
            "metric": metric,
            "evidence_scope": "time_window",
            "use_for_current_state": False,
            "requested_range": {"start": start.isoformat(), "end": end.isoformat()},
            "query_range": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "bounds": "[start, end]",
            },
            "step_seconds": 15,
            "expected_point_count": expected_points,
            "sample_count": len(samples),
            "missing_point_count": len(missing),
            "missing_evaluated_at": missing,
            "samples": samples,
            "truncated": False,
        },
    )


def record(tool_result, identity=None):
    result = tool_result.result
    arguments = {"service": SERVICE}
    if tool_result.name == "query_metrics":
        arguments["metric"] = result["metric"]
    elif tool_result.name == "query_metrics_aligned":
        arguments.update(
            metrics=",".join(result["requested_metrics"]),
            at=result["requested_evaluation_time"],
        )
    else:
        arguments.update(
            metric=result["metric"],
            start=result["requested_range"]["start"],
            end=result["requested_range"]["end"],
            step_seconds=result["step_seconds"],
        )
    return {
        "observation_id": tool_result.observation_id,
        "name": tool_result.name,
        "arguments": arguments,
        "call_id": "call-" + tool_result.observation_id,
        "result": result,
        "success": True,
        "collected_at": COLLECTED.isoformat(),
        "identity": identity or {
            "commit_sha": SHA,
            "process_started_at": AT.isoformat(),
        },
        "cache_key": "c" * 64,
    }


def analyze(*observations):
    return analyze_metric_evidence(list(observations), service=SERVICE)


def codes(report, field="limitations"):
    return {item.code for item in getattr(report, field)}


@pytest.mark.parametrize("as_records", [False, True])
def test_complete_historical_snapshots_at_different_times_remain_complete(as_records):
    snapshots = [
        aligned("first", AT),
        aligned("second", LATER, (0.0, 0.0, 1.0)),
    ]
    inputs = [record(item) for item in snapshots] if as_records else snapshots
    report = analyze(*inputs)
    assert report.status == "complete"
    assert len(report.confirmed_observations) == 6
    assert "time_not_aligned" not in codes(report)
    assert "evaluation_times_differ" not in codes(report, "temporal_findings")
    assert "obtain_same_evaluation_metrics" not in codes(report, "suggested_checks")
    for code in ("aligned_comparison_complete", "strict_time_alignment"):
        groups = [item for item in report.temporal_findings if item.code == code]
        assert len(groups) == 2
        for group, snapshot in zip(groups, snapshots):
            assert group.source_observation_ids == (snapshot.observation_id,)
            assert group.evaluated_times == (snapshot.result["evaluation_time"],)
            assert group.strict_time_alignment is True
            assert set(group.metrics) == set(NAMES)


def test_two_historical_snapshots_are_separate_and_missing_is_not_zero():
    first = aligned("first", AT)
    second = aligned("second", LATER, (0.0, None, 1.0))
    report = analyze(first, second)
    assert report.status == "partial"
    assert "time_not_aligned" not in codes(report)
    assert "missing_metric_sample" in codes(report)
    assert "obtain_same_evaluation_metrics" not in codes(report, "suggested_checks")
    incomplete = [
        item for item in report.temporal_findings
        if item.code == "aligned_comparison_incomplete"
    ]
    assert len(incomplete) == 1
    assert incomplete[0].comparison_complete is False
    assert incomplete[0].strict_time_alignment is False
    assert incomplete[0].missing_metrics == ("http_5xx_rate",)
    assert incomplete[0].source_observation_ids == ("second",)
    assert incomplete[0].comparison_time == LATER.isoformat()
    groups = [
        item for item in report.temporal_findings
        if item.code == "strict_time_alignment"
    ]
    assert len(groups) == 2
    assert groups[0].evaluated_times == (AT.isoformat(),)
    assert groups[0].source_observation_ids == ("first",)
    assert groups[1].evaluated_times == (LATER.isoformat(),)
    assert groups[1].metrics == ("http_request_rate", "service_scrape_up")
    assert groups[1].source_observation_ids == ("second",)
    observed = {
        (item.observation_id, item.metric): item.samples[0].value
        for item in report.confirmed_observations
    }
    assert observed["first", "http_request_rate"] == 0.41821223361699034
    assert observed["first", "http_5xx_rate"] == 1.0
    assert observed["second", "http_request_rate"] == 0.0
    assert ("second", "http_5xx_rate") not in observed
    assert report.hypotheses_to_verify == ()
    assert report.use_for_current_state is False
    assert all(
        item.evidence_scope == "time_window" for item in report.confirmed_observations
    )
    assert all(
        item.use_for_current_state is False for item in report.confirmed_observations
    )


def test_original_misaligned_pair_has_no_contradiction_or_causal_hypothesis():
    errors = instant("errors", "http_5xx_rate", 1.0, AT)
    rate = instant("rate", "http_request_rate", 0.0, LATER)
    report = analyze(errors, rate)
    assert report.status == "partial"
    assert len(report.confirmed_observations) == 2
    assert "time_not_aligned" in codes(report)
    assert "evaluation_times_differ" in codes(report, "temporal_findings")
    assert "numeric_consistency_question" not in codes(report, "temporal_findings")
    assert "same_time_value_or_expression_conflict" not in codes(
        report, "temporal_findings",
    )
    assert "strict_time_alignment" not in codes(report, "temporal_findings")
    assert report.hypotheses_to_verify == ()
    assert "obtain_same_evaluation_metrics" in codes(report, "suggested_checks")


def test_historical_snapshot_does_not_hide_a_misaligned_independent_metric():
    separate = instant("separate", "http_request_rate", 0.0, LATER)
    separate.result.update(evidence_scope="time_window", use_for_current_state=False)
    report = analyze(aligned("snapshot", AT), separate)
    assert report.status == "partial"
    assert "time_not_aligned" in codes(report)
    assert "evaluation_times_differ" in codes(report, "temporal_findings")
    assert "obtain_same_evaluation_metrics" in codes(report, "suggested_checks")
    groups = [
        item for item in report.temporal_findings
        if item.code == "strict_time_alignment"
    ]
    assert len(groups) == 1
    assert groups[0].source_observation_ids == ("snapshot",)
    assert groups[0].evaluated_times == (AT.isoformat(),)


def test_exact_same_instant_may_raise_only_a_consistency_question():
    report = analyze(
        instant("errors", "http_5xx_rate", 1.0),
        instant("rate", "http_request_rate", 0.0),
    )
    assert "strict_time_alignment" in codes(report, "temporal_findings")
    assert "numeric_consistency_question" in codes(report, "temporal_findings")
    assert len(report.hypotheses_to_verify) == 1
    assert "尚未验证" in report.hypotheses_to_verify[0].message
    assert report.hypotheses_to_verify[0].source_observation_ids == ("errors", "rate")
    assert "inspect_counters_scrapes_and_query_times" in codes(
        report, "suggested_checks",
    )
    assert "evaluation_not_scrape_time" in codes(report)
    assert "queries_not_atomic" in codes(report)


def test_historical_change_is_a_sampling_interval_not_an_exact_event_time():
    report = analyze(history())
    trend = report.temporal_findings[0]
    assert isinstance(trend, RangeFinding)
    assert trend.first.evaluated_at == "2026-10-11T10:48:45+00:00"
    assert trend.last.evaluated_at == "2026-10-11T10:49:00+00:00"
    assert trend.first.value == trend.observed_max == 0.7273
    assert trend.last.value == trend.observed_min == 0.0
    assert trend.observed_change_count == 1
    assert trend.changes[0].before == trend.first
    assert trend.changes[0].after == trend.last
    assert trend.changes[0].missing_points_between == 0
    assert report.hypotheses_to_verify == ()


def test_three_of_twenty_nine_points_keeps_twenty_six_explicit_gaps():
    source = history(
        metric="http_5xx_rate",
        expected_points=29,
        values={0: 1.0, 1: 1.0, 28: 1.0},
    )
    report = analyze(source)
    assert report.status == "partial"
    trend = report.temporal_findings[0]
    assert trend.expected_point_count == 29
    assert trend.sample_count == 3
    assert trend.missing_point_count == len(trend.missing_evaluated_at) == 26
    assert trend.missing_evaluated_at == tuple(source.result["missing_evaluated_at"])
    assert len(report.confirmed_observations[0].samples) == 3
    assert all(point.value == 1.0 for point in report.confirmed_observations[0].samples)
    assert trend.observed_change_count == 0
    assert "range_has_gaps" in codes(report)


def test_change_across_gaps_keeps_the_full_uncertainty_interval():
    source = history(expected_points=5, values={0: 1.0, 4: 0.0})
    report = analyze(source)
    trend = report.temporal_findings[0]
    assert trend.changes[0].missing_points_between == 3
    assert (
        trend.changes[0].before.evaluated_at
        == source.result["samples"][0]["evaluated_at"]
    )
    assert (
        trend.changes[0].after.evaluated_at
        == source.result["samples"][-1]["evaluated_at"]
    )


def test_empty_valid_range_can_report_gaps_without_fabricating_values():
    report = analyze(history(expected_points=29, values={}))
    assert report.status == "insufficient_evidence"
    assert report.confirmed_observations == ()
    trend = report.temporal_findings[0]
    assert trend.missing_point_count == 29
    assert trend.first is trend.last is trend.observed_min is trend.observed_max is None


@pytest.mark.parametrize("stamp", [
    None, True, 1791715735, "", "bad", "2026-10-11T10:48:55",
    "2026-10-11T10:48:55-00:00", "2026-13-11T10:48:55Z",
    "2026-10-11T10:48:55.000000001Z", "10000-01-01T00:00:00Z",
    "1969-12-31T23:59:59Z", "2026-10-11T10:48:55+25:00",
])
def test_invalid_or_lossy_time_never_becomes_confirmed(stamp):
    source = instant()
    source.result["evaluated_at"] = stamp
    report = analyze(source)
    assert report.status == "insufficient_evidence"
    assert report.confirmed_observations == ()


@pytest.mark.parametrize("value", [
    None, True, "0", "NaN", [], {}, float("nan"), float("inf"),
    -float("inf"), -1, 10**500,
])
def test_invalid_values_never_become_confirmed(value):
    report = analyze(instant(value=value))
    assert report.status == "insufficient_evidence"
    assert report.confirmed_observations == ()


@pytest.mark.parametrize("metric,value", [
    ("http_5xx_rate", 1.1), ("service_scrape_up", 0.5),
    ("postgres_up", 2), ("postgres_connection_usage", 1.1),
])
def test_existing_semantic_domains_are_respected(metric, value):
    assert analyze(instant(metric=metric, value=value)).confirmed_observations == ()


@pytest.mark.parametrize("value", [0, 0.0, 1, 1.0, 0.41821223361699034])
def test_finite_zero_and_one_are_real_values(value):
    report = analyze(instant(value=value))
    assert report.status == "complete"
    assert report.confirmed_observations[0].samples[0].value == value


def test_absent_evaluation_time_cannot_be_replaced_by_collection_or_http_times():
    source = instant()
    del source.result["evaluated_at"]
    source.result.update(
        query_started_at=AT.isoformat(),
        query_finished_at=AT.isoformat(),
    )
    raw = record(source)
    raw["collected_at"] = AT.isoformat()
    report = analyze(raw)
    assert report.status == "insufficient_evidence"
    assert report.confirmed_observations == ()


def test_same_collection_and_http_times_do_not_align_different_evaluations():
    first = record(instant("first", "http_5xx_rate", 1.0))
    second = record(instant("second", "http_request_rate", 0.0, LATER))
    for raw in (first, second):
        raw["result"].update(
            query_started_at=COLLECTED.isoformat(),
            query_finished_at=COLLECTED.isoformat(),
        )
    report = analyze(first, second)
    assert "time_not_aligned" in codes(report)
    assert "numeric_consistency_question" not in codes(report, "temporal_findings")


def test_equivalent_timezone_representations_are_exactly_aligned():
    first = instant("first", "http_5xx_rate", 1.0)
    second = instant("second", "http_request_rate", 0.0)
    second.result["evaluated_at"] = "2026-10-11T18:48:55+08:00"
    report = analyze(first, second)
    assert "strict_time_alignment" in codes(report, "temporal_findings")
    assert "time_not_aligned" not in codes(report)


def test_one_microsecond_difference_cannot_be_rounded_into_alignment():
    report = analyze(
        instant("first", "http_5xx_rate", 1.0),
        instant("second", "http_request_rate", 0.0, AT + timedelta(microseconds=1)),
    )
    assert "time_not_aligned" in codes(report)
    assert report.hypotheses_to_verify == ()


@pytest.mark.parametrize("with_value", [False, True])
def test_no_data_is_never_a_zero(with_value):
    source = instant()
    source.result.update(status="no_data")
    source.result.pop("evaluated_at")
    if not with_value:
        source.result.pop("value")
    report = analyze(source)
    assert report.confirmed_observations == ()
    assert report.status == "insufficient_evidence"
    expected = (
        "missing_sample_has_measurement" if with_value else "missing_metric_sample"
    )
    assert expected in codes(report)


@pytest.mark.parametrize("status", [
    "tool_error", "invalid_arguments", "tool_limit_reached", "tool_not_found", None,
])
def test_failed_and_unexecuted_results_are_not_measurements(status):
    source = instant()
    source.result["status"] = status
    report = analyze(source)
    assert report.confirmed_observations == ()
    assert "failed_or_unexecuted_observation" in codes(report)


def test_success_false_cannot_certify_a_status_ok_record():
    raw = record(instant())
    raw["success"] = False
    assert analyze(raw).confirmed_observations == ()


def test_aligned_unexecuted_item_is_explicitly_missing():
    source = aligned(values=(0.0, None, 1.0))
    item = source.result["results"]["http_5xx_rate"]
    item.update(request_executed=False, query_started_at=None, query_finished_at=None)
    report = analyze(source)
    assert report.status == "partial"
    assert "request_not_executed" in codes(report)
    assert len(report.confirmed_observations) == 2


@pytest.mark.parametrize("mutation", [
    "fake_complete", "fake_top_ok", "missing_comparison_complete", "string_complete",
    "mismatched_sample", "missing_sample_time", "unexecuted_ok", "extra_metric",
    "duplicate_requested", "different_declared_time", "invalid_item_value",
    "item_target",
])
def test_untrustworthy_aligned_comparisons_cannot_certify_a_snapshot(mutation):
    source = (
        aligned(values=(0.0, None, 1.0))
        if mutation in {"fake_complete", "fake_top_ok"} else aligned()
    )
    result = source.result
    if mutation == "fake_complete":
        result["comparison_complete"] = True
    elif mutation == "fake_top_ok":
        result["status"] = "ok"
    elif mutation == "missing_comparison_complete":
        result.pop("comparison_complete")
    elif mutation == "string_complete":
        result["comparison_complete"] = "true"
    elif mutation == "extra_metric":
        result["results"]["postgres_up"] = deepcopy(result["results"][NAMES[0]])
    elif mutation == "duplicate_requested":
        result["requested_metrics"] = [NAMES[0], NAMES[0]]
    elif mutation == "different_declared_time":
        result["evaluation_time"] = LATER.isoformat()
    else:
        item = result["results"][NAMES[0]]
        if mutation == "mismatched_sample":
            item["evaluated_at"] = LATER.isoformat()
        elif mutation == "missing_sample_time":
            item.pop("evaluated_at")
        elif mutation == "unexecuted_ok":
            item.update(
                request_executed=False,
                query_started_at=None,
                query_finished_at=None,
            )
        elif mutation == "invalid_item_value":
            item["value"] = float("nan")
        elif mutation == "item_target":
            item["service"] = "another-service"
    report = analyze(source)
    assert report.status == "insufficient_evidence"
    assert report.confirmed_observations == ()
    assert "strict_time_alignment" not in codes(report, "temporal_findings")
    assert report.hypotheses_to_verify == ()


@pytest.mark.parametrize("tool_result", [instant(), aligned(), history()])
def test_cross_service_results_are_excluded(tool_result):
    source = deepcopy(tool_result)
    source.result["service"] = "another-service"
    report = analyze(source)
    assert "cross_service_evidence" in codes(report)
    assert report.confirmed_observations == ()


def test_duplicate_reference_is_rejected_without_choosing_a_winner():
    first = instant()
    second = replace(first, result=first.result | {"value": 1.0})
    for duplicate in (first, second):
        report = analyze(first, duplicate)
        assert report.status == "insufficient_evidence"
        assert "duplicate_observation_id" in codes(report)
        assert report.confirmed_observations == ()


def test_distinct_references_with_conflicting_same_time_values_are_not_resolved():
    report = analyze(
        instant("rate-1", value=0.0),
        instant("rate-2", value=1.0),
        instant("errors", "http_5xx_rate", 1.0),
    )
    assert len(report.confirmed_observations) == 3
    assert "same_time_value_or_expression_conflict" in codes(
        report, "temporal_findings",
    )
    assert report.hypotheses_to_verify == ()


def test_different_runtime_identities_cannot_form_a_joint_state():
    first = record(instant("errors", "http_5xx_rate", 1.0))
    second = record(instant("rate", value=0.0), identity={"commit_sha": "b" * 40})
    report = analyze(first, second)
    assert len(report.confirmed_observations) == 2
    assert "runtime_identities_differ" in codes(report)
    assert "strict_time_alignment" not in codes(report, "temporal_findings")
    assert report.hypotheses_to_verify == ()


@pytest.mark.parametrize("mutation", [
    "wrong_sample_count", "wrong_expected_count", "wrong_gap_count", "wrong_gap_time",
    "nonfinite_sample", "duplicate_sample", "unordered_samples", "off_grid_sample",
    "outside_window", "missing_time", "truncated", "bad_step", "historical_as_current",
    "missing_scope", "missing_range", "missing_samples", "boolean_count",
    "false_no_data",
])
def test_invalid_ranges_never_upgrade_metadata_or_samples(mutation):
    source = history(expected_points=5, values={0: 1.0, 4: 0.0})
    result = source.result
    if mutation == "wrong_sample_count":
        result["sample_count"] = 5
    elif mutation == "wrong_expected_count":
        result["expected_point_count"] = 29
    elif mutation == "wrong_gap_count":
        result["missing_point_count"] = 0
    elif mutation == "wrong_gap_time":
        result["missing_evaluated_at"][0] = AT.isoformat()
    elif mutation == "nonfinite_sample":
        result["samples"][0]["value"] = float("inf")
    elif mutation == "duplicate_sample":
        result["samples"][1] = deepcopy(result["samples"][0])
    elif mutation == "unordered_samples":
        result["samples"].reverse()
    elif mutation == "off_grid_sample":
        result["samples"][0]["evaluated_at"] = AT.isoformat()
    elif mutation == "outside_window":
        result["samples"][0]["evaluated_at"] = (AT - timedelta(minutes=1)).isoformat()
    elif mutation == "missing_time":
        result["samples"][0].pop("evaluated_at")
    elif mutation == "truncated":
        result["truncated"] = True
    elif mutation == "bad_step":
        result["step_seconds"] = True
    elif mutation == "historical_as_current":
        result["use_for_current_state"] = True
    elif mutation == "missing_scope":
        result.pop("evidence_scope")
    elif mutation == "missing_range":
        result.pop("query_range")
    elif mutation == "missing_samples":
        result.pop("samples")
    elif mutation == "boolean_count":
        result["expected_point_count"] = True
    elif mutation == "false_no_data":
        result["status"] = "no_data"
    report = analyze(source)
    assert report.status == "insufficient_evidence"
    assert report.confirmed_observations == ()


@pytest.mark.parametrize("key,value", [
    ("success", "true"), ("collected_at", "invalid"), ("cache_key", "forged"),
    ("call_id", ""), ("unexpected", "field"), ("observation_id", ""),
])
def test_invalid_checkpoint_record_is_not_trusted(key, value):
    raw = record(instant())
    raw[key] = value
    report = analyze(raw)
    assert report.status == "insufficient_evidence"
    assert report.confirmed_observations == ()


@pytest.mark.parametrize("mutation", [
    "target", "metric", "extra_argument", "aligned_at", "range_start",
])
def test_checkpoint_arguments_must_identify_the_same_observation(mutation):
    source = (
        aligned() if mutation == "aligned_at"
        else (history() if mutation == "range_start" else instant())
    )
    raw = record(source)
    if mutation == "target":
        raw["arguments"]["service"] = "another-service"
    elif mutation == "metric":
        raw["arguments"]["metric"] = "http_5xx_rate"
    elif mutation == "extra_argument":
        raw["arguments"]["promql"] = "up"
    elif mutation == "aligned_at":
        raw["arguments"]["at"] = LATER.isoformat()
    elif mutation == "range_start":
        raw["arguments"]["start"] = AT.isoformat()
    assert analyze(raw).confirmed_observations == ()


@pytest.mark.parametrize("checkpoint", [
    {}, {"schema_version": 2, "phase": "ready"},
    {"phase": "invalid"}, {"phase": "ready", "records": [{}]},
    {"phase": "ready", "unexpected": True},
    Checkpoint.model_construct(phase="invalid"),
])
def test_invalid_checkpoint_adapter_fails_closed(checkpoint):
    report = analyze_checkpoint_metrics(checkpoint, service=SERVICE)
    assert report.status == "insufficient_evidence"
    assert report.confirmed_observations == ()


def test_checkpoint_does_not_coerce_success_string_into_trusted_evidence():
    raw = record(instant())
    raw["success"] = "true"
    report = analyze_checkpoint_metrics(
        {"phase": "ready", "records": [raw]},
        service=SERVICE,
    )
    assert report.confirmed_observations == ()


def test_raw_results_records_and_checkpoint_objects_remain_unchanged():
    tools = [aligned(), history(), instant()]
    raw_records = [record(item) for item in tools]
    typed_records = [ToolObservation.model_validate(item) for item in raw_records]
    checkpoint = Checkpoint(phase="tool_saved", records=typed_records)
    original_tools = deepcopy(tools)
    original_records = deepcopy(raw_records)
    original_typed = [item.model_dump(mode="json") for item in typed_records]
    original_checkpoint = checkpoint.model_dump(mode="json")
    for inputs in (tools, raw_records, typed_records):
        report = analyze_metric_evidence(inputs, service=SERVICE)
        assert report.confirmed_observations
        detached = report.to_dict()
        detached["confirmed_observations"][0]["samples"][0]["value"] = 999.0
    report = analyze_checkpoint_metrics(checkpoint, service=SERVICE)
    assert report.confirmed_observations
    assert all(
        item.use_for_current_state is False for item in report.confirmed_observations
    )
    assert tools == original_tools
    assert raw_records == original_records
    assert [item.model_dump(mode="json") for item in typed_records] == original_typed
    assert checkpoint.model_dump(mode="json") == original_checkpoint
    assert all("summary_truncated" not in item.result for item in tools)


def test_existing_worker_summaries_are_not_raw_analysis_inputs():
    from investigation.worker import Worker

    inputs = (
        instant(), aligned(),
        history(expected_points=29, values={0: 1.0, 1: 0.0, 28: 0.1}),
    )
    for source in inputs:
        summary = Worker._observation_summary(source.result, source.name)
        summary["summary_truncated"] = True
        report = analyze(replace(source, result=summary))
        assert report.confirmed_observations == ()
        assert "summary_is_not_raw_evidence" in codes(report)


def test_checkpoint_history_stays_history_even_when_collected_recently():
    source = history()
    raw = record(source)
    raw["collected_at"] = COLLECTED.isoformat()
    before = deepcopy(raw)
    report = analyze_checkpoint_metrics(
        {"phase": "ready", "records": [raw]},
        service=SERVICE,
    )
    assert report.confirmed_observations[0].evidence_scope == "time_window"
    assert report.confirmed_observations[0].use_for_current_state is False
    assert raw == before


def test_analysis_is_deterministic_under_input_permutation():
    inputs = [aligned("a"), aligned("b", LATER, (0.0, None, 1.0))]
    assert analyze(*inputs) == analyze(*reversed(inputs))


def test_no_clock_network_or_worker_call_is_needed(monkeypatch):
    network = Mock(side_effect=AssertionError("network_not_allowed"))
    monkeypatch.setattr(requests, "get", network)
    source = history()
    assert analyze(source) == analyze(source)
    network.assert_not_called()


def test_full_one_hundred_twenty_point_history_is_bounded_and_truthful():
    source = history(
        expected_points=120,
        values={index: float(index % 2) for index in range(120)},
    )
    report = analyze(source)
    assert report.status == "partial"
    assert len(report.confirmed_observations[0].samples) == 120
    trend = report.temporal_findings[0]
    assert trend.observed_change_count == 119
    assert len(trend.changes) == MAX_REPORTED_CHANGES
    assert trend.changes_truncated is True
    output_bytes = len(json.dumps(report.to_dict(), ensure_ascii=False).encode())
    assert output_bytes <= MAX_ANALYSIS_OUTPUT_BYTES


def test_too_many_records_fail_closed_with_a_small_report():
    inputs = [instant(str(index)) for index in range(MAX_ANALYSIS_OBSERVATIONS + 1)]
    report = analyze(*inputs)
    assert report.status == "insufficient_evidence"
    assert report.confirmed_observations == ()
    assert "analysis_input_limit_exceeded" in codes(report)
    assert len(json.dumps(report.to_dict()).encode()) < 2048


def test_total_sample_limit_is_independent_of_per_range_limit():
    inputs = [
        history(
            str(index),
            expected_points=120,
            values={point: 0.0 for point in range(120)},
        )
        for index in range(MAX_ANALYSIS_SAMPLES // 120 + 1)
    ]
    report = analyze(*inputs)
    assert report.confirmed_observations == ()
    assert "analysis_sample_limit_exceeded" in codes(report)


def test_output_limit_does_not_silently_drop_facts():
    inputs = [instant(str(index)) for index in range(MAX_ANALYSIS_OBSERVATIONS)]
    for item in inputs:
        item.result["promql"] = "q" * 1024
    report = analyze(*inputs)
    assert report.status == "insufficient_evidence"
    assert "analysis_output_limit_exceeded" in codes(report)
    assert report.confirmed_observations == ()
    output_bytes = len(json.dumps(report.to_dict(), ensure_ascii=False).encode())
    assert output_bytes <= MAX_ANALYSIS_OUTPUT_BYTES


@pytest.mark.parametrize("kind", ["large_string", "deep", "cycle", "nodes", "bytes"])
def test_unbounded_inputs_stop_before_validation(kind):
    source = instant()
    if kind == "large_string":
        source.result["irrelevant"] = "x" * (MAX_ANALYSIS_INPUT_BYTES + 1)
    elif kind == "cycle":
        source.result["cycle"] = source.result
    elif kind == "deep":
        nested = source.result
        for _ in range(20):
            nested["deeper"] = {}
            nested = nested["deeper"]
    elif kind == "nodes":
        source.result["irrelevant"] = [None] * 20000
    elif kind == "bytes":
        source.result["irrelevant"] = ["x" * 8192] * 40
    report = analyze(source)
    assert report.status == "insufficient_evidence"
    assert report.confirmed_observations == ()


def test_empty_and_unsupported_collections_are_controlled():
    for inputs in ([], None, {}, iter([instant()])):
        report = analyze_metric_evidence(inputs, service=SERVICE)
        assert report.status == "insufficient_evidence"
        assert report.confirmed_observations == ()


def test_invalid_record_does_not_erase_independent_valid_observation():
    invalid = instant("bad")
    invalid.result["value"] = float("nan")
    report = analyze(invalid, instant("good"))
    assert report.status == "partial"
    assert [item.observation_id for item in report.confirmed_observations] == ["good"]
    assert report.source_observation_ids == ("bad", "good")


def test_missing_optional_expression_and_request_timing_are_not_invented():
    source = instant()
    report = analyze(source)
    fact = report.confirmed_observations[0]
    assert fact.promql is None
    assert fact.evidence_scope is None
    assert fact.samples[0].evaluated_at == AT.isoformat()
    assert "promql" not in source.result
    assert "query_started_at" not in source.result


@pytest.mark.parametrize("name", [None, True, [], {}, "unknown_tool"])
def test_malformed_tool_name_is_controlled(name):
    report = analyze(replace(instant(), name=name))
    assert report.status == "insufficient_evidence"
    assert report.confirmed_observations == ()


@pytest.mark.parametrize("collected", [0, 1791715735.0, True])
def test_checkpoint_collection_time_requires_a_real_timezone_field(collected):
    raw = record(instant())
    raw["collected_at"] = collected
    assert analyze(raw).confirmed_observations == ()


def test_duplicate_call_identity_is_not_independent_evidence():
    first = record(instant("first"))
    second = record(instant("second"))
    second["call_id"] = first["call_id"]
    report = analyze(first, second)
    assert "duplicate_call_id" in codes(report)
    assert report.confirmed_observations == ()


def test_invalid_runtime_identity_is_not_used_to_certify_a_comparison():
    raw = record(instant(), identity={"commit_sha": float("nan")})
    report = analyze(raw)
    assert "invalid_checkpoint_identity" in codes(report)
    assert report.confirmed_observations == ()


def test_range_cannot_masquerade_as_a_single_instant_measurement():
    source = history()
    source.result.update(value=0.0, evaluated_at=AT.isoformat())
    report = analyze(replace(source, name="query_metrics"))
    assert "metric_tool_shape_mismatch" in codes(report)
    assert report.confirmed_observations == ()


@pytest.mark.parametrize("schema_version", [True, "1", 1.0])
def test_checkpoint_schema_version_is_not_coerced(schema_version):
    checkpoint = {
        "phase": "ready",
        "schema_version": schema_version,
        "records": [record(instant())],
    }
    report = analyze_checkpoint_metrics(checkpoint, service=SERVICE)
    assert report.confirmed_observations == ()


def test_preflight_failure_with_null_request_times_remains_missing():
    source = aligned()
    source.result.update(
        status="no_data",
        comparison_complete=False,
        requested_metrics=[],
        results={},
        query_started_at=None,
        query_finished_at=None,
    )
    report = analyze(source)
    assert "no_valid_observation" in codes(report)
    assert report.confirmed_observations == ()


def test_existing_metrics_tools_feed_the_analyzer_without_schema_changes(monkeypatch):
    from tools.metrics import query_metric_range, query_metrics, query_metrics_aligned

    now = Mock(wraps=datetime)
    now.now.return_value = COLLECTED
    monkeypatch.setattr("tools.metrics.datetime", now)
    client = Mock()
    monkeypatch.setattr("tools.metrics.prometheus_client", client)

    def vector(at, value):
        return {
            "status": "success",
            "data": {"resultType": "vector", "result": [
                {"metric": {}, "value": [at.timestamp(), str(value)]},
            ]},
        }

    client.query.return_value = vector(COLLECTED, 0.0)
    single = query_metrics(SERVICE, "http_request_rate")
    client.query_at.side_effect = [vector(AT, 0.41821223361699034), vector(AT, 1.0)]
    comparison = query_metrics_aligned(SERVICE, ",".join(NAMES[:2]), AT.isoformat())
    start = AT - timedelta(seconds=10)
    end = start + timedelta(seconds=15)
    client.query_range.return_value = {
        "status": "success",
        "data": {"resultType": "matrix", "result": [{
            "metric": {},
            "values": [[start.timestamp(), "0.7273"], [end.timestamp(), "0"]],
        }]},
    }
    historical = query_metric_range(
        SERVICE, "http_request_rate", start.isoformat(), end.isoformat(), 15,
    )
    for index, (name, result) in enumerate([
        ("query_metrics", single),
        ("query_metrics_aligned", comparison),
        ("query_metric_range", historical),
    ]):
        report = analyze(MetricToolResult(str(index), name, result))
        assert report.status == "complete"
        assert report.confirmed_observations


def test_integer_precision_loss_cannot_hide_an_observed_value_or_change():
    report = analyze(instant(value=2**53 + 1))
    assert report.confirmed_observations == ()
    source = history(values={0: 2**53, 1: 2**53 + 1})
    assert analyze(source).confirmed_observations == ()


def test_checkpoint_cannot_certify_an_absent_requested_comparison_time():
    raw = record(aligned())
    raw["arguments"]["at"] = None
    raw["result"].pop("requested_evaluation_time")
    assert analyze(raw).confirmed_observations == ()


@pytest.mark.parametrize("field", ["evaluated_at", "promql", "service"])
def test_invalid_unicode_input_is_controlled_before_serialization(field):
    source = instant()
    source.result[field] = chr(0xD800)
    report = analyze(source)
    assert report.status == "insufficient_evidence"
    assert "invalid_input_encoding" in codes(report)
    assert report.confirmed_observations == ()
