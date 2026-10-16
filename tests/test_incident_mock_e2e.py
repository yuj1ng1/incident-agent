"""P5.3c.1: actual durable investigation flow, scripted model answers only.

All metric numbers are fixed historical/test observations, not new measurements.
The preset answer proves transport/persistence, never real model reasoning.
"""
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import httpx2
import openai
import pytest

from config import MAX_AGENT_LOOPS, MAX_TOOL_CALLS
from investigation.checkpoint import valid_chain
from investigation.evidence_policy import fresh
from investigation.metric_analysis import (
    MAX_ANALYSIS_OUTPUT_BYTES,
    analyze_metric_evidence,
)
from investigation.models import SessionStatus
from investigation.state import LostClaim
from p53c1_support import (
    PRESET_ANSWER,
    attach_worker,
    finish_and_reopen,
    latest_analysis,
    source_call,
    start_runtime,
)
from runtime_support import call, response
from test_metric_analysis import AT, COLLECTED, LATER, aligned, history, instant
from test_worker_metric_analysis import report_codes
from test_worker_metric_analysis_growth import (
    analyses,
    function_items,
    json_text,
    offline,
)


@pytest.fixture(autouse=True)
def require_offline(offline):
    """Reuse the existing requests/socket guard for every new test."""


@pytest.fixture
def investigation(tmp_path, request):
    runs = []

    def create(script, sources=(), **kwargs):
        run = start_runtime(tmp_path, script, sources, **kwargs)
        runs.append(run)
        return run

    yield create
    if directory := os.environ.get("INCIDENT_P53C1_REPORT_DIR"):
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        for index, run in enumerate(runs):
            saved = run.manager.store.get(run.session_id)
            checkpoint = run.manager.store.load_checkpoint(saved)
            (output / f"{request.node.name}-{index}.json").write_text(
                json_text({
                    "mock_answer_is_not_reasoning_evidence": True,
                    "clock": run.clock.now.isoformat(),
                    "requests": run.requests,
                    "executions": run.executions,
                    "final_session": saved.model_dump(mode="json"),
                    "final_checkpoint": checkpoint,
                    "events": run.manager.store.events(run.session_id, limit=1000),
                }),
                encoding="utf-8",
            )


def final_response():
    return response(answer=PRESET_ANSWER)


def assert_historical_sources(run, checkpoint):
    metric_records = [
        item for item in checkpoint["records"]
        if item["name"].startswith("query_metric")
    ]
    analysis = latest_analysis(run)
    assert analysis["evidence_scope"] == "historical_analysis"
    assert analysis["use_for_current_state"] is False
    assert set(analysis["reports"][0]["source_observation_ids"]) == {
        item["observation_id"] for item in metric_records
    }
    current_ids = set(run.requests[-1]["current_observation_ids"])
    assert not current_ids.intersection(
        item["observation_id"] for item in metric_records
    )
    for report in analysis["reports"]:
        assert report["use_for_current_state"] is False
        assert all(
            item["use_for_current_state"] is False
            for item in report["confirmed_observations"]
        )
    for record in metric_records:
        expected = run.sources[record["call_id"]]
        assert all(record["result"][key] == value for key, value in expected.items())
    return analysis["reports"][0]


@pytest.mark.parametrize("second_complete", [False, True])
def test_a_two_historical_snapshots_complete_the_incident(
    investigation,
    second_complete,
):
    first = aligned("snapshot-t1")
    second = aligned(
        "snapshot-t2", LATER, (0.0, 0.0 if second_complete else None, 1.0),
    )
    run = investigation([
        response(answer="", calls=[source_call(first)]),
        response(answer="", calls=[source_call(second)]),
        final_response(),
    ], [first, second])
    created = run.manager.store.get(run.session_id)
    assert created.status == SessionStatus.QUEUED
    assert run.manager.incident_store.get_incident(run.incident_id)
    assert run.worker.run_once().status == SessionStatus.COMPLETED
    checkpoint = finish_and_reopen(run, models=3, tools=3)
    report = assert_historical_sources(run, checkpoint)
    assert report["status"] == ("complete" if second_complete else "partial")
    assert "time_not_aligned" not in report_codes(report)
    assert "numeric_consistency_question" not in report_codes(
        report, "temporal_findings",
    )
    assert report["hypotheses_to_verify"] == []
    by_call = {item["call_id"]: item for item in checkpoint["records"]}
    first_id = by_call[first.observation_id]["observation_id"]
    second_id = by_call[second.observation_id]["observation_id"]
    complete = [
        item for item in report["temporal_findings"]
        if item["code"] == "aligned_comparison_complete"
    ]
    assert len(complete) == (2 if second_complete else 1)
    observed = {
        (item["observation_id"], item["metric"]): item["samples"][0]["value"]
        for item in report["confirmed_observations"]
    }
    assert observed[first_id, "http_request_rate"] == 0.41821223361699034
    assert observed[first_id, "http_5xx_rate"] == 1.0
    assert observed[second_id, "http_request_rate"] == 0.0
    assert observed[second_id, "service_scrape_up"] == 1.0
    if not second_complete:
        assert (second_id, "http_5xx_rate") not in observed
        missing = by_call[second.observation_id]["result"]["results"]["http_5xx_rate"]
        assert missing["status"] == "no_data" and "value" not in missing
        incomplete = next(
            item for item in report["temporal_findings"]
            if item["code"] == "aligned_comparison_incomplete"
        )
        assert incomplete["missing_metrics"] == ["http_5xx_rate"]
        assert incomplete["source_observation_ids"] == [second_id]
    assert [len(analyses(row["body"]["input"])) for row in run.requests] == [0, 1, 1]
    assert all(
        not any(runtime_marker(item) for item in row["body"]["input"])
        for row in run.requests
    )


def runtime_marker(item):
    # Parsing reuses the same shared helper as the existing growth regressions.
    return any(
        payload.get("context_origin") == "runtime_derived_metric_analysis"
        for payload in analyses([item])
    )


def test_b_independent_historical_metrics_are_not_a_same_time_contradiction(
    investigation,
):
    sources = [
        instant("errors-t1", "http_5xx_rate", 1.0, AT),
        instant("rate-t2", "http_request_rate", 0.0, LATER),
    ]
    # query_metrics has no historical-at argument. These are injected historical
    # outputs for analysis, not a claim that its real API supports such a query.
    run = investigation([
        response(answer="", calls=[source_call(item) for item in sources]),
        final_response(),
    ], sources)
    run.worker.run_once()
    checkpoint = finish_and_reopen(run, models=2, tools=3)
    report = assert_historical_sources(run, checkpoint)
    assert "time_not_aligned" in report_codes(report)
    assert "evaluation_times_differ" in report_codes(report, "temporal_findings")
    assert "numeric_consistency_question" not in report_codes(
        report, "temporal_findings",
    )
    assert report["hypotheses_to_verify"] == []
    assert "obtain_same_evaluation_metrics" in report_codes(report, "suggested_checks")


def test_c_ranges_preserve_the_observed_change_interval_and_all_gaps(investigation):
    rate = history("request-trend")  # 0.7273 is the supplied approximation.
    errors = history(
        "5xx-gaps", metric="http_5xx_rate", expected_points=29,
        values={0: 1.0, 1: 1.0, 28: 1.0},
    )
    run = investigation([
        response(answer="", calls=[source_call(rate), source_call(errors)]),
        final_response(),
    ], [rate, errors])
    run.worker.run_once()
    checkpoint = finish_and_reopen(run, models=2, tools=3)
    report = assert_historical_sources(run, checkpoint)
    trends = {
        item["metric"]: item for item in report["temporal_findings"]
        if item["code"] == "range_trend"
    }
    rate_trend = trends["http_request_rate"]
    assert rate_trend["first"] == rate.result["samples"][0]
    assert rate_trend["last"] == rate.result["samples"][-1]
    assert rate_trend["changes"] == [{
        "before": rate.result["samples"][0],
        "after": rate.result["samples"][-1],
        "missing_points_between": 0,
    }]
    gaps = trends["http_5xx_rate"]
    assert (
        gaps["expected_point_count"], gaps["sample_count"], gaps["missing_point_count"],
    ) == (29, 3, 26)
    assert gaps["missing_evaluated_at"] == errors.result["missing_evaluated_at"]
    assert gaps["observed_change_count"] == 0
    assert report["hypotheses_to_verify"] == []


@pytest.mark.parametrize("failure", ["tool_error", "not_executed"])
def test_d_preset_answer_cannot_change_missing_or_failed_evidence(
    investigation,
    failure,
):
    good = instant("valid-history", value=0.41821223361699034)
    missing = instant("missing-history", metric="http_5xx_rate")
    missing.result.clear()
    missing.result.update(
        status="no_data", service="order-service", metric="http_5xx_rate",
        reason="no_samples_at_evaluation_time",
    )
    if failure == "tool_error":
        failed = instant("failed-history", metric="service_scrape_up")
        failed.result.clear()
        failed.result.update(
            status="tool_error", service="order-service",
            metric="service_scrape_up", reason="fixture_tool_failure",
        )
    else:
        failed = aligned("failed-history", values=(None, None, None))
        # The existing helper models partial success; all missing members need
        # the real tool's no_data envelope, rather than an inconsistent status.
        failed.result["status"] = "no_data"
        for item in failed.result["results"].values():
            item.update(
                request_executed=False,
                query_started_at=None,
                query_finished_at=None,
                reason="deadline_before_io",
            )
    run = investigation([
        response(
            answer="",
            calls=[source_call(item) for item in (good, missing, failed)],
        ),
        final_response(),
    ], [good, missing, failed])
    run.worker.run_once()
    checkpoint = finish_and_reopen(run, models=2, tools=4)
    report = assert_historical_sources(run, checkpoint)
    confirmed = report["confirmed_observations"]
    assert len(confirmed) == 1
    assert confirmed[0]["samples"][0]["value"] == 0.41821223361699034
    expected = (
        "failed_or_unexecuted_observation" if failure == "tool_error"
        else "request_not_executed"
    )
    assert {"missing_metric_sample", expected} <= report_codes(report)
    assert report["hypotheses_to_verify"] == []
    rejected = next(
        item for item in checkpoint["records"]
        if item["call_id"] == failed.observation_id
    )
    assert "value" not in rejected["result"]
    if failure == "not_executed":
        # The Mock tool invocation is budgeted even though its backend never ran.
        assert all(
            item["request_executed"] is False
            and item["query_started_at"] is item["query_finished_at"] is None
            for item in rejected["result"]["results"].values()
        )


@pytest.mark.parametrize("revised", [False, True])
def test_e_real_model_boundary_keeps_one_latest_analysis_and_exact_pairs(
    investigation,
    revised,
):
    source = instant("chain-first", at=COLLECTED)
    calls = [
        source_call(source, name)
        for name in ("chain-first", "chain-cache-1", "chain-cache-2")
    ]
    run = investigation([
        *(response(answer="", calls=[item]) for item in calls),
        final_response(),
    ], [source])
    expected = {}
    if revised:
        original = run.worker._metric_analysis_context

        def reanalyze(service, records):
            result = original(service, records)  # Always execute the real analyzer.
            if result is not None:
                result["reason"] = f"fixture_revision_{len(run.requests) + 1}"
                expected[len(run.requests) + 1] = json.loads(json_text(result))
            return result

        # Test-only revision marker verifies replacement transport for the same
        # raw observations, not a new metric fact or production analysis rule.
        run.worker._metric_analysis_context = reanalyze
    run.worker.run_once()
    checkpoint = finish_and_reopen(run, models=4, tools=2, hits=2)
    source_ids = set()
    for index, row in enumerate(run.requests):
        messages = row["body"]["input"]
        assert valid_chain(messages)
        projected = analyses(messages)
        assert sum(runtime_marker(item) for item in messages) == (index > 0)
        assert len(projected) == (index > 0)
        pairs = function_items(messages)
        expected_pairs = []
        for original_call in calls[:index]:
            saved_call = next(
                item for item in pairs if item.get("type") == "function_call"
                and item["call_id"] == original_call["call_id"]
            )
            assert saved_call == original_call
            output = next(
                item for item in pairs
                if item.get("type") == "function_call_output"
                and item["call_id"] == original_call["call_id"]
            )
            assert json.loads(output["output"]) == checkpoint["records"][-1]["result"]
            expected_pairs.extend([saved_call, output])
        assert pairs == expected_pairs
        if index:
            analysis = projected[-1]["metric_analysis"]
            source_ids.update(analysis["reports"][0]["source_observation_ids"])
            assert analysis["use_for_current_state"] is False
            assert function_items(row["checkpoint"]["history"]) == pairs
            if revised:
                assert analysis == expected[index + 1]
    assert len(source_ids) == 1
    assert valid_chain(checkpoint["history"])
    assert len(function_items(checkpoint["history"])) == 6
    if revised:
        assert analyses(checkpoint["history"])[0]["metric_analysis"] == expected[4]
        assert expected[2] != expected[3] != expected[4]


def test_f_retry_keeps_old_rebuild_but_replaces_marked_analysis(investigation):
    rate = instant("before-retry", at=COLLECTED)
    errors = instant("after-retry", metric="http_5xx_rate", at=COLLECTED)
    run = investigation([
        response(answer="", calls=[source_call(rate)]),
        TimeoutError("controlled_provider_timeout"),
        response(answer="", calls=[source_call(errors)]),
        response(answer="", calls=[source_call(errors, "retry-cache")]),
        final_response(),
    ], [rate, errors])

    def boundary(phase, session_id):
        if phase == "model_checkpoint_committed" and len(run.requests) >= 3:
            run.clock.now += timedelta(seconds=1)

    run.boundary = boundary
    run.worker.boundary_hook = boundary
    waiting = run.worker.run_once()
    assert waiting.status == SessionStatus.WAITING_RETRY
    failed_checkpoint = run.manager.store.load_checkpoint(waiting)
    raw_before = deepcopy(failed_checkpoint["records"])
    assert "result" not in failed_checkpoint
    assert (waiting.total_model_requests, waiting.total_tool_executions) == (2, 2)
    run.clock.now = waiting.next_retry_at
    attach_worker(run, reopen=True).run_once()
    checkpoint = finish_and_reopen(run, models=5, tools=4, hits=1, attempts=2)
    saved = run.manager.store.get(run.session_id)
    assert (saved.current_attempt_rounds, saved.current_attempt_tool_calls) == (3, 2)
    assert all(item in checkpoint["records"] for item in raw_before)
    initial_rebuild = run.requests[2]["body"]["input"][0]
    old = analyses([initial_rebuild])[0]
    assert "recovery" in old and "context_origin" not in old
    for row in run.requests[3:]:
        messages = row["body"]["input"]
        assert messages[0] == initial_rebuild
        payloads = analyses(messages)
        assert len(payloads) == 2
        assert sum(runtime_marker(item) for item in messages) == 1
        latest = payloads[-1]
        assert latest["context_origin"] == "runtime_derived_metric_analysis"
        assert latest["context_collected_at"] > old["context_collected_at"]
        assert latest["metric_analysis"] != old["metric_analysis"]
        assert all(
            item["metric_analysis"]["use_for_current_state"] is False
            for item in payloads
        )
        assert valid_chain(messages)
    assert len(analyses(checkpoint["history"])) == 2
    assert sum(runtime_marker(item) for item in checkpoint["history"]) == 1
    assert [
        item["call_id"] for item in function_items(checkpoint["history"])
        if item["type"] == "function_call"
    ] == ["after-retry", "retry-cache"]


def test_large_history_reaches_the_analyzer_as_all_120_raw_points(investigation):
    source = history(
        "large-history", expected_points=120,
        values={index: float(index % 2) for index in range(120)},
    )
    run = investigation([
        response(answer="", calls=[source_call(source)]), final_response(),
    ], [source])
    run.worker.run_once()
    checkpoint = finish_and_reopen(run, models=2, tools=2)
    report = assert_historical_sources(run, checkpoint)
    assert len(report["confirmed_observations"][0]["samples"]) == 120
    finding = report["temporal_findings"][0]
    assert finding["sample_count"] == finding["expected_point_count"] == 120
    assert finding["observed_change_count"] == 119
    assert finding["changes_truncated"] is True
    assert finding["missing_point_count"] == 0
    payload = analyses(run.requests[-1]["body"]["input"])[0]
    summary = next(
        item for item in payload["historical_observations"]
        if item["name"] == source.name
    )["result"]
    assert summary["summary_point_count"] == 12 and summary["summary_truncated"] is True
    assert all(item in source.result["samples"] for item in summary["samples"])


def test_authorized_dependency_has_its_own_historical_report(investigation):
    primary = history("primary-range")
    dependency = history(
        "dependency-range", metric="postgres_connections", values={0: 3.0, 1: 2.0},
    )
    dependency.result["service"] = "order-db"
    run = investigation([
        response(answer="", calls=[source_call(primary), source_call(dependency)]),
        final_response(),
    ], [primary, dependency])
    run.worker.run_once()
    checkpoint = finish_and_reopen(run, models=2, tools=3)
    reports = {item["service"]: item for item in latest_analysis(run)["reports"]}
    assert set(reports) == {"order-service", "order-db"}
    for service, role in (("order-service", "primary"), ("order-db", "dependency")):
        report = reports[service]
        assert report["target_role"] == role
        assert report["use_for_current_state"] is False
        assert all(
            item["service"] == service for item in report["confirmed_observations"]
        )
        assert set(report["source_observation_ids"]) == {
            item["observation_id"] for item in checkpoint["records"]
            if item["name"] == "query_metric_range"
            and item["arguments"]["service"] == service
        }


def test_analysis_input_selection_exposes_exclusions_and_keeps_saved_raw_records(
    investigation,
):
    sources = [instant(f"large-record-{index}") for index in range(6)]
    for source in sources:
        source.result["fixture_metadata"] = ["x" * 8000] * 7
    run = investigation([
        response(answer="", calls=[source_call(item) for item in sources]),
        final_response(),
    ], sources)
    run.worker.run_once()
    checkpoint = finish_and_reopen(run, models=2, tools=7)
    analysis = latest_analysis(run)
    assert analysis["status"] == "partial"
    assert analysis["excluded_record_counts"]["input_bytes_limit"] == 2
    assert analysis["selected_record_count"] == 4
    assert analysis["selected_input_bytes"] <= analysis["limits"]["input_bytes"]
    assert len(analysis["reports"][0]["confirmed_observations"]) == 4
    assert len(checkpoint["records"]) == 7
    for item in checkpoint["records"][1:]:
        assert item["result"]["fixture_metadata"] == ["x" * 8000] * 7
    assert analysis["use_for_current_state"] is False


def test_analysis_context_size_limit_is_explicit_and_does_not_erase_raw_evidence(
    investigation,
):
    sources = [
        history(
            "capacity-primary-rate", expected_points=120,
            values={index: float(index % 2) for index in range(120)},
        ),
        history(
            "capacity-primary-latency", metric="http_latency_p95_seconds",
            expected_points=120,
            values={index: float(index % 2) for index in range(120)},
        ),
        history(
            "capacity-dependency", metric="postgres_connections",
            expected_points=120,
            values={index: float(index % 2) for index in range(120)},
        ),
    ]
    sources[-1].result["service"] = "order-db"
    run = investigation([
        response(answer="", calls=[source_call(item) for item in sources]),
        final_response(),
    ], sources)
    run.worker.run_once()
    checkpoint = finish_and_reopen(run, models=2, tools=4)
    analysis = latest_analysis(run)
    assert analysis["status"] == "insufficient_evidence"
    assert analysis["reason"] == "metric_analysis_context_size_limit"
    assert analysis["reports"] == [] and analysis["use_for_current_state"] is False
    assert analysis["selected_sample_slots"] == 360
    # Each authorized group can be analyzed; their combined context exceeds the
    # original limit. No limit is lowered and no measurement is interpolated.
    reports = []
    for service in ("order-service", "order-db"):
        selected = [
            item for item in checkpoint["records"]
            if item["name"] == "query_metric_range"
            and item["arguments"]["service"] == service
        ]
        report = analyze_metric_evidence(selected, service=service).to_dict()
        assert report["status"] != "insufficient_evidence"
        assert len(json_text(report).encode()) <= MAX_ANALYSIS_OUTPUT_BYTES
        reports.append(report)
    assert len(json_text(reports).encode()) > MAX_ANALYSIS_OUTPUT_BYTES
    assert all(
        len(item["result"]["samples"]) == 120
        for item in checkpoint["records"] if item["name"] == "query_metric_range"
    )


@pytest.mark.parametrize(
    "fallback", ["no_data", "analysis_input", "analysis_exception"],
)
def test_analysis_failure_is_delivered_without_invented_measurements(
    investigation,
    monkeypatch,
    fallback,
):
    source = history("unavailable-analysis")
    if fallback == "no_data":
        source = history("unavailable-analysis", expected_points=29, values={})
    elif fallback == "analysis_input":
        source.result["fixture_metadata"] = "x" * 8193
    else:
        def fail_analysis(*args, **kwargs):
            raise ValueError("untrusted_fixture_error")

        monkeypatch.setattr(
            "investigation.worker.analyze_metric_evidence",
            fail_analysis,
        )
    run = investigation([
        response(answer="", calls=[source_call(source)]), final_response(),
    ], [source])
    run.worker.run_once()
    checkpoint = finish_and_reopen(run, models=2, tools=2)
    analysis = latest_analysis(run)
    assert analysis["status"] == "insufficient_evidence"
    assert analysis["use_for_current_state"] is False
    assert analysis["evidence_scope"] == "historical_analysis"
    assert "untrusted_fixture_error" not in json_text(analysis)
    if fallback == "analysis_exception":
        assert analysis["reason"] == "metric_analysis_failed"
        assert analysis["reports"] == []
    else:
        report = analysis["reports"][0]
        assert report["confirmed_observations"] == []
        expected = (
            "range_has_gaps" if fallback == "no_data"
            else "analysis_input_limit_exceeded"
        )
        assert expected in report_codes(report)
    assert checkpoint["records"][-1]["result"] == source.result


def test_expiry_rebuilds_context_without_promoting_history_or_replaying_tools(
    investigation,
):
    source = instant("expiring", at=COLLECTED)
    run = investigation([
        response(answer="", calls=[source_call(source)]), final_response(),
    ], [source])

    def expire(phase, session_id):
        if phase == "tool_checkpoint_committed" and len(run.requests) == 1:
            run.clock.now += timedelta(seconds=16)

    run.worker.boundary_hook = expire
    run.worker.run_once()
    checkpoint = finish_and_reopen(run, models=2, tools=2)
    report = assert_historical_sources(run, checkpoint)
    assert report["confirmed_observations"]
    assert run.requests[-1]["current_observation_ids"] == []
    messages = run.requests[-1]["body"]["input"]
    assert function_items(messages) == []
    payload = analyses(messages)[0]
    assert "recovery" in payload
    assert all(item["expired"] for item in payload["historical_observations"])
    assert not any(fresh(item, run.clock.now) for item in checkpoint["records"])


@pytest.mark.parametrize("failure", ["controlled_error", "context_limit"])
def test_provider_failure_preserves_saved_evidence_and_reports_failed_status(
    investigation,
    failure,
):
    source = history("before-provider-error")
    error = ValueError("controlled_model_failure")
    if failure == "context_limit":
        error = openai.BadRequestError(
            "fixture_context_length_exceeded",
            response=httpx2.Response(
                400,
                request=httpx2.Request("POST", "https://provider.invalid/responses"),
            ),
            body={"error": {"code": "context_length_exceeded"}},
        )
    run = investigation([
        response(answer="", calls=[source_call(source)]), error,
    ], [source])
    run.worker.run_once()
    checkpoint = finish_and_reopen(
        run, models=2, tools=2, status=SessionStatus.FAILED,
    )
    assert_historical_sources(run, checkpoint)
    assert PRESET_ANSWER not in checkpoint["result"]["answer"]
    assert checkpoint["records"][-1]["result"] == source.result
    # Failed requests must not persist their uncommitted rebuilt model input.
    assert checkpoint["history"] == run.requests[-1]["checkpoint"]["history"]
    assert analyses(checkpoint["history"]) == []
    expected = "model_http_400" if failure == "context_limit" else "ValueError"
    assert expected in checkpoint["result"]["answer"]


@pytest.mark.parametrize("target", ["inventory-service", "execute_remediation"])
def test_worker_rejects_unapproved_targets_and_repair_calls(investigation, target):
    source = history("authorized-history")
    forbidden = (
        call(
            "query_metrics",
            {"service": target, "metric": "http_request_rate"},
            "rejected",
        )
        if target == "inventory-service"
        else call(target, {"service": "order-service"}, "rejected")
    )
    run = investigation([
        response(answer="", calls=[source_call(source), forbidden]), final_response(),
    ], [source])
    run.worker.run_once()
    checkpoint = finish_and_reopen(run, models=2, tools=2)
    assert_historical_sources(run, checkpoint)
    events = run.manager.store.events(run.session_id, limit=1000)
    assert sum(item["event_type"] == "tool_rejected" for item in events) == 1
    assert not any(item["call_id"] == "rejected" for item in checkpoint["records"])
    payload = analyses(run.requests[-1]["body"]["input"])[0]
    rejected = next(
        item for item in payload["recent_function_call_results"]
        if item["call_id"] == "rejected"
    )
    assert rejected["result"] == {"status": "invalid_or_unallowed_tool_call"}
    assert rejected["observation_id"] is None


def test_suggestions_cannot_bypass_the_original_thirteen_tool_budget(investigation):
    sources = []
    for index in range(MAX_TOOL_CALLS + 1):
        source = instant(f"missing-{index}")
        source.result.clear()
        source.result.update(
            status="no_data", service="order-service", metric="http_request_rate",
        )
        sources.append(source)
    run = investigation([
        response(answer="", calls=[source_call(item) for item in sources]),
        final_response(),
    ], sources)
    run.worker.run_once()
    checkpoint = finish_and_reopen(run, models=2, tools=MAX_TOOL_CALLS)
    assert latest_analysis(run)["reports"][0]["suggested_checks"]
    assert run.requests[-1]["body"]["tools"] == []
    rejected = [item for item in checkpoint["trace"] if item.get("executed") is False]
    assert len(rejected) == 2  # Bootstrap version probe consumed the first slot.
    assert all(item["status"] == "tool_limit_reached" for item in rejected)
    pairs = function_items(checkpoint["history"])
    assert sum(item["type"] == "function_call" for item in pairs) == MAX_TOOL_CALLS + 1
    assert sum(
        item["type"] == "function_call_output" for item in pairs
    ) == MAX_TOOL_CALLS + 1
    assert valid_chain(checkpoint["history"])


def test_attempt_model_limit_stops_before_an_eighth_provider_request(investigation):
    source = instant("model-limit-0", at=COLLECTED)
    run = investigation([
        response(answer="", calls=[source_call(source, f"model-limit-{index}")])
        for index in range(MAX_AGENT_LOOPS)
    ], [source])
    run.worker.run_once()
    checkpoint = finish_and_reopen(
        run, models=MAX_AGENT_LOOPS, tools=2,
        hits=MAX_AGENT_LOOPS - 1, status=SessionStatus.EXHAUSTED,
    )
    assert run.requests[-1]["body"]["tools"] == []
    assert "attempt_budget_exhausted" in checkpoint["result"]["answer"]
    assert len(analyses(checkpoint["history"])) == 1


def test_proposal_refresh_is_counted_after_the_saved_final_answer(investigation):
    run = investigation([final_response()])
    planner = Mock()
    planner.plan.return_value = object()
    planner.propose.return_value = None
    run.manager.remediation_planner = planner
    before_refresh = {}

    def capture(phase, session_id):
        if phase == "model_checkpoint_committed":
            saved = run.manager.store.get(session_id)
            before_refresh.update(
                models=saved.total_model_requests,
                tools=saved.total_tool_executions,
                checkpoint=deepcopy(run.manager.store.load_checkpoint(saved)),
            )

    run.worker.boundary_hook = capture
    run.worker.run_once()
    checkpoint = finish_and_reopen(run, models=1, tools=2)
    assert (before_refresh["models"], before_refresh["tools"]) == (1, 1)
    assert checkpoint["history"] == before_refresh["checkpoint"]["history"]
    assert [item["name"] for item in run.executions] == [
        "get_running_version", "get_running_version",
    ]
    assert run.executions[-1]["call_id"].startswith("proposal-refresh-")
    assert checkpoint["records"][0] == before_refresh["checkpoint"]["records"][0]
    planner.propose.assert_called_once()


@pytest.mark.parametrize("resource", ["model", "tool"])
def test_session_budget_exhaustion_saves_actual_totals(investigation, resource):
    first = instant("budget-first", at=COLLECTED)
    if resource == "model":
        script = [response(answer="", calls=[source_call(first)])]
        sources = [first]
    else:
        second = instant("budget-second", metric="http_5xx_rate", at=COLLECTED)
        script = [response(answer="", calls=[source_call(first), source_call(second)])]
        sources = [first, second]
    run = investigation(script, sources)
    if resource == "model":
        run.manager.limits.max_model_requests = 1
    else:
        run.manager.limits.max_tool_executions = 2
    run.worker.run_once()
    checkpoint = finish_and_reopen(
        run, models=1, tools=2, status=SessionStatus.EXHAUSTED,
    )
    assert f"{resource}_budget_exhausted" in checkpoint["result"]["answer"]


def test_failure_after_retry_preserves_session_totals(investigation):
    first = instant("failure-before-retry", at=COLLECTED)
    second = instant("failure-after-retry", metric="http_5xx_rate", at=COLLECTED)
    run = investigation([
        response(answer="", calls=[source_call(first)]),
        TimeoutError("controlled_timeout"),
        response(answer="", calls=[source_call(second)]),
        ValueError("controlled_failure"),
    ], [first, second])
    waiting = run.worker.run_once()
    assert waiting.status == SessionStatus.WAITING_RETRY
    assert (waiting.total_model_requests, waiting.total_tool_executions) == (2, 2)
    run.clock.now = waiting.next_retry_at
    attach_worker(run, reopen=True).run_once()
    finish_and_reopen(
        run, models=4, tools=4, attempts=2, status=SessionStatus.FAILED,
    )
    saved = run.manager.store.get(run.session_id)
    assert (saved.current_attempt_rounds, saved.current_attempt_tool_calls) == (2, 2)


@pytest.mark.parametrize("resource, saved_counts", [
    ("model", (0, 0)), ("tool", (1, 1)),
])
@pytest.mark.parametrize("phase", ["result_saved", "terminal_save"])
def test_saved_final_recovers_without_repeating_model_or_tools(
    investigation,
    monkeypatch,
    resource,
    saved_counts,
    phase,
):
    class Crash(BaseException):
        pass

    run = investigation([final_response()])
    if resource == "model":
        run.manager.limits.max_model_requests = 1
    else:
        run.manager.limits.max_tool_executions = 1
    limits = run.manager.limits

    def crash(when, session_id):
        if when == "model_checkpoint_committed":
            raise Crash()

    original_save = run.manager.store.save

    def crash_terminal_save(session, db):
        if session.status == SessionStatus.COMPLETED:
            staged = run.manager.store.load_checkpoint(session, db)
            assert (
                staged["result"]["model_rounds"], staged["result"]["tool_calls"],
            ) == (1, 1)
            assert (
                run.manager.store.get(run.session_id, db).status
                == SessionStatus.RUNNING
            )
            raise Crash()
        return original_save(session, db)

    with monkeypatch.context() as patch:
        if phase == "result_saved":
            run.worker.boundary_hook = crash
        else:
            patch.setattr(run.manager.store, "save", crash_terminal_save)
        with pytest.raises(Crash):
            run.worker.run_once()
    interrupted = run.manager.store.get(run.session_id)
    before = run.manager.store.load_checkpoint(interrupted)
    assert interrupted.status == SessionStatus.RUNNING
    assert before["result"]["answer"] == PRESET_ANSWER
    if saved_counts != (0, 0):
        # Also recover a previously populated result without dropping its totals.
        before["result"].update(
            model_rounds=saved_counts[0], tool_calls=saved_counts[1],
        )
        run.worker._save(
            run.session_id,
            (interrupted.owner_token, interrupted.fencing_token),
            before,
        )
    assert (
        before["result"]["model_rounds"], before["result"]["tool_calls"],
    ) == saved_counts
    run.clock.now += timedelta(seconds=limits.lease_seconds + 1)
    attach_worker(run, reopen=True)
    run.manager.limits = limits
    assert run.worker.run_once().status == SessionStatus.COMPLETED
    checkpoint = finish_and_reopen(run, models=1, tools=1)
    assert checkpoint["history"] == before["history"]
    assert checkpoint["records"] == before["records"]
    assert checkpoint["trace"] == before["trace"]
    assert checkpoint["result"] == before["result"] | {
        "model_rounds": 1, "tool_calls": 1,
    }


def test_crash_after_raw_evidence_commit_recovers_with_fencing_and_no_fake_output(
    investigation,
):
    class Crash(BaseException):
        pass

    source = history("committed-before-crash")
    run = investigation([
        response(answer="", calls=[source_call(source)]), final_response(),
    ], [source])

    def crash(phase, session_id):
        if phase == "tool_checkpoint_committed" and len(run.requests) == 1:
            raise Crash()

    run.worker.boundary_hook = crash
    with pytest.raises(Crash):
        run.worker.run_once()
    saved = run.manager.store.get(run.session_id)
    old_claim = (saved.owner_token, saved.fencing_token)
    before = run.manager.store.load_checkpoint(saved)
    metric = before["records"][-1]
    assert metric["result"] == source.result
    assert not valid_chain(before["history"])
    assert [item["call_id"] for item in before["pending"]] == [source.observation_id]
    run.boundary = None
    attach_worker(run, reopen=True)
    assert run.worker.run_once() is None  # The original unexpired lease still owns it.
    run.clock.now += timedelta(seconds=121)
    attach_worker(run, reopen=True).run_once()
    checkpoint = finish_and_reopen(run, models=2, tools=3)
    final = run.manager.store.get(run.session_id)
    assert final.fencing_token > old_claim[1]
    assert final.resume_count == 1
    assert metric in checkpoint["records"]
    assert not any(
        item["call_id"] == source.observation_id
        for item in function_items(checkpoint["history"])
    )
    assert function_items(run.requests[-1]["body"]["input"]) == []
    assert_historical_sources(run, checkpoint)
    assert valid_chain(checkpoint["history"])
    with pytest.raises(LostClaim), run.manager.store.transaction() as db:
        run.manager.store.guard(db, run.session_id, *old_claim)


def test_completed_incident_is_readable_in_a_fresh_python_process(investigation):
    source = history("process-reopen-history")
    run = investigation([
        response(answer="", calls=[source_call(source)]), final_response(),
    ], [source])
    run.worker.run_once()
    checkpoint = finish_and_reopen(run, models=2, tools=2)
    script = """
import json
from pathlib import Path
import socket
import sys
import requests

def forbidden(*args, **kwargs):
    raise AssertionError("child_external_network_forbidden")

requests.sessions.Session.request = forbidden
socket.socket.connect = forbidden
socket.create_connection = forbidden

from incident_store import IncidentStore
from investigation.manager import InvestigationManager
from investigation.store import RuntimeStore

root = Path(sys.argv[1])
store = RuntimeStore(root / "runtime.sqlite3", read_only=True)
session = store.get(sys.argv[2])
checkpoint = store.load_checkpoint(session)
incidents = IncidentStore(root / "incidents.json")
manager = InvestigationManager(store, incidents)
incident = incidents.get_incident(session.incident_id)
print(json.dumps({
    "session": session.model_dump(mode="json"),
    "checkpoint": checkpoint,
    "result": manager.workflow(session.session_id)["investigation_result"],
    "answers": [item.answer for item in incident.investigations],
}, ensure_ascii=False))
"""
    child = subprocess.run(
        [sys.executable, "-B", "-c", script, str(run.root), run.session_id],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=60,
        check=True,
    )
    actual = json.loads(child.stdout)
    assert actual["checkpoint"] == checkpoint
    assert actual["result"] == checkpoint["result"]
    assert actual["session"]["status"] == "COMPLETED"
    assert actual["answers"] == [PRESET_ANSWER]
