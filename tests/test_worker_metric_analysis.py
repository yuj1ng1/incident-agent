"""P5.3b context projection over temporary SQLite stores and fixture tools/models."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from investigation.checkpoint import MAX_CHECKPOINT_BYTES, valid_chain
from investigation.evidence_policy import cache_key, effective_records
from investigation.metric_analysis import (
    MAX_ANALYSIS_INPUT_BYTES,
    MAX_ANALYSIS_OBSERVATIONS,
    MAX_ANALYSIS_SAMPLES,
    analyze_metric_evidence,
)
from investigation.models import Checkpoint, SessionStatus
from investigation.store import RuntimeStore
from investigation.worker import MAX_METRIC_ANALYSIS_CONTEXT_BYTES, Worker
from runtime_support import (
    FixtureAgent,
    SHA,
    call,
    create_runtime,
    create_session,
    response,
)
from test_metric_analysis import AT, COLLECTED, LATER, aligned, history, instant, record


def raw(source, target="order-service", session_service="order-service"):
    item = deepcopy(record(source))
    item["arguments"]["service"] = target
    item["result"]["service"] = target
    item["cache_key"] = cache_key(
        session_service, item["name"], item["arguments"], item["identity"],
    )
    return item


def loaded_context(tmp_path, records, *, verified=False, service="order-service"):
    manager = create_runtime(tmp_path, clock=lambda: COLLECTED)
    session = create_session(manager, service=service)
    checkpoint = manager.store.load_checkpoint(session)
    checkpoint["records"] = records
    checkpoint["identity"] = records[0]["identity"] if records else None
    with manager.store.transaction() as db:
        manager.store.checkpoint(session, checkpoint, db)
        manager.store.save(session, db)
    checkpoint = manager.store.load_checkpoint(manager.store.get(session.session_id))
    agent = FixtureAgent(clock=lambda: COLLECTED)
    agent.build_incident_prompt = Mock(wraps=agent.build_incident_prompt)
    worker = Worker(manager, agent, provider_verified=verified)
    return manager, session, checkpoint, worker, agent


def details(worker, session, checkpoint):
    messages = worker._context(session.session_id, checkpoint)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    return json.loads(messages[0]["content"].rsplit("\n", 1)[-1])


def report_codes(report, field="limitations"):
    return {item["code"] for item in report[field]}


def test_complete_snapshots_keep_separate_times_sources_and_historical_scope(tmp_path):
    inputs = [raw(aligned("first")), raw(aligned("second", LATER, (0.0, 0.0, 1.0)))]
    _, session, checkpoint, worker, agent = loaded_context(tmp_path, inputs)
    payload = details(worker, session, checkpoint)["metric_analysis"]
    assert payload["status"] == "complete"
    assert payload["use_for_current_state"] is False
    assert payload["evidence_scope"] == "historical_analysis"
    report = payload["reports"][0]
    assert report["service"] == session.service
    assert report["target_role"] == "primary"
    assert report["source_observation_ids"] == ["first", "second"]
    assert len(report["confirmed_observations"]) == 6
    assert all(
        item["use_for_current_state"] is False
        for item in report["confirmed_observations"]
    )
    assert all(
        item["evidence_scope"] == "time_window"
        for item in report["confirmed_observations"]
    )
    assert "time_not_aligned" not in report_codes(report)
    assert agent.build_incident_prompt.call_args.args[0].evidence == []
    agent.request_durable_response.assert_not_called()


def test_misaligned_instants_remain_partial_without_a_joint_numeric_question(tmp_path):
    inputs = [
        raw(instant("errors", "http_5xx_rate", 1.0, AT)),
        raw(instant("rate", "http_request_rate", 0.0, LATER)),
    ]
    _, session, checkpoint, worker, _ = loaded_context(tmp_path, inputs)
    report = details(worker, session, checkpoint)["metric_analysis"]["reports"][0]
    assert report["status"] == "partial"
    assert "time_not_aligned" in report_codes(report)
    assert "numeric_consistency_question" not in report_codes(
        report, "temporal_findings",
    )
    assert report["hypotheses_to_verify"] == []


def test_same_time_independent_point_does_not_mix_historical_snapshots(tmp_path):
    extra = instant("extra", at=LATER)
    extra.result.update(evidence_scope="time_window", use_for_current_state=False)
    inputs = [raw(aligned("first")), raw(aligned("second", LATER)), raw(extra)]
    _, session, checkpoint, worker, _ = loaded_context(tmp_path, inputs)
    report = details(worker, session, checkpoint)["metric_analysis"]["reports"][0]
    assert "time_not_aligned" not in report_codes(report)
    assert "evaluation_times_differ" not in report_codes(report, "temporal_findings")


def test_missing_incomplete_and_sparse_history_never_fill_zeroes(tmp_path):
    missing = instant("missing")
    missing.result.clear()
    missing.result.update(
        status="no_data",
        service="order-service",
        metric="http_request_rate",
    )
    sparse = history("sparse", expected_points=29, values={0: 1.0, 1: 1.0, 28: 1.0})
    inputs = [raw(missing), raw(aligned(values=(0.0, None, 1.0))), raw(sparse)]
    _, session, checkpoint, worker, _ = loaded_context(tmp_path, inputs)
    payload = details(worker, session, checkpoint)["metric_analysis"]
    assert payload["status"] == "partial"
    report = payload["reports"][0]
    assert {"missing_metric_sample", "range_has_gaps"} <= report_codes(report)
    assert not any(
        item["observation_id"] == "missing"
        for item in report["confirmed_observations"]
    )
    trend = next(
        item for item in report["temporal_findings"] if item["code"] == "range_trend"
    )
    assert trend["missing_point_count"] == len(trend["missing_evaluated_at"]) == 26
    assert trend["sample_count"] == 3
    incomplete = next(
        item for item in report["temporal_findings"]
        if item["code"] == "aligned_comparison_incomplete"
    )
    assert incomplete["missing_metrics"] == ["http_5xx_rate"]


def test_large_mixed_checkpoint_analyzes_only_full_raw_metric_records(
    tmp_path,
    monkeypatch,
):
    metric = raw(history(
        expected_points=120,
        values={index: 0.0 for index in range(120)},
    ))
    logs = []
    for index in range(30):
        item = raw(instant("log-" + str(index)))
        item.update(
            name="query_logs",
            arguments={"service": "order-service"},
            result={"status": "ok", "logs": [{"line": "x" * 1000}] * 30},
        )
        logs.append(item)
    inputs = logs[:15] + [metric] + logs[15:]
    manager, session, checkpoint, worker, _ = loaded_context(tmp_path, inputs)
    size = len(json.dumps(checkpoint).encode())
    assert MAX_ANALYSIS_INPUT_BYTES < size < MAX_CHECKPOINT_BYTES
    spy = Mock(wraps=analyze_metric_evidence)
    monkeypatch.setattr("investigation.worker.analyze_metric_evidence", spy)
    original = deepcopy(checkpoint["records"])
    payload = details(worker, session, checkpoint)["metric_analysis"]
    selected = spy.call_args.args[0]
    assert selected == [metric]
    assert len(selected[0]["result"]["samples"]) == 120
    assert "summary_truncated" not in selected[0]["result"]
    assert len(payload["reports"][0]["confirmed_observations"][0]["samples"]) == 120
    assert checkpoint["records"] == original
    saved = manager.store.load_checkpoint(manager.store.get(session.session_id))
    assert saved["records"] == original


def test_primary_dependency_and_unauthorized_targets_are_isolated(tmp_path):
    main = raw(instant("primary"))
    dependency = raw(instant("dependency", "postgres_up", 1.0), "order-db")
    foreign = raw(instant("foreign"), "inventory-service")
    mismatch = raw(instant("mismatch"))
    mismatch["result"]["service"] = "order-db"
    _, session, checkpoint, worker, _ = loaded_context(
        tmp_path, [main, dependency, foreign, mismatch],
    )
    payload = details(worker, session, checkpoint)["metric_analysis"]
    assert payload["status"] == "partial"
    assert payload["excluded_record_counts"]["unauthorized_target"] == 1
    assert payload["excluded_record_counts"]["target_mismatch"] == 1
    reports = {item["service"]: item for item in payload["reports"]}
    assert set(reports) == {"order-service", "order-db"}
    assert reports["order-service"]["target_role"] == "primary"
    assert reports["order-db"]["target_role"] == "dependency"
    for target, source_id in (("order-service", "primary"), ("order-db", "dependency")):
        assert reports[target]["source_observation_ids"] == [source_id]
        assert all(
            item["service"] == target
            for item in reports[target]["confirmed_observations"]
        )


def test_dependency_authorization_is_directional(tmp_path):
    inputs = [raw(instant(), session_service="order-db")]
    _, session, checkpoint, worker, _ = loaded_context(
        tmp_path, inputs, service="order-db",
    )
    payload = details(worker, session, checkpoint)["metric_analysis"]
    assert payload["status"] == "insufficient_evidence"
    assert payload["reports"] == []
    assert payload["excluded_record_counts"]["unauthorized_target"] == 1


def test_runtime_identity_changes_are_preserved_and_not_combined(tmp_path):
    first = raw(instant("errors", "http_5xx_rate", 1.0))
    second = raw(instant("rate", "http_request_rate", 0.0))
    second["identity"] = {"commit_sha": "b" * 40}
    _, session, checkpoint, worker, _ = loaded_context(tmp_path, [first, second])
    report = details(worker, session, checkpoint)["metric_analysis"]["reports"][0]
    assert "runtime_identities_differ" in report_codes(report)
    assert "numeric_consistency_question" not in report_codes(
        report, "temporal_findings",
    )
    identities = {
        item["runtime_identity_key"] for item in report["confirmed_observations"]
    }
    assert len(identities) == 2


@pytest.mark.parametrize("limit", ["records", "samples", "bytes"])
def test_selection_bounds_are_global_and_skip_whole_records(
    tmp_path,
    monkeypatch,
    limit,
):
    if limit == "records":
        inputs = [
            raw(instant("point-" + str(i)))
            for i in range(MAX_ANALYSIS_OBSERVATIONS + 5)
        ]
    elif limit == "samples":
        inputs = [
            raw(history(
                str(i),
                expected_points=120,
                values={p: 0.0 for p in range(120)},
            ))
            for i in range(MAX_ANALYSIS_SAMPLES // 120 + 1)
        ]
    else:
        inputs = [raw(instant("point-" + str(i))) for i in range(10)]
        for item in inputs:
            item["result"]["metadata"] = ["x" * 8000] * 4
    _, session, checkpoint, worker, _ = loaded_context(tmp_path, inputs)
    spy = Mock(wraps=analyze_metric_evidence)
    monkeypatch.setattr("investigation.worker.analyze_metric_evidence", spy)
    original = deepcopy(checkpoint["records"])
    payload = details(worker, session, checkpoint)["metric_analysis"]
    assert payload["selected_record_count"] <= MAX_ANALYSIS_OBSERVATIONS
    assert payload["selected_sample_slots"] <= MAX_ANALYSIS_SAMPLES
    assert payload["selected_input_bytes"] <= MAX_ANALYSIS_INPUT_BYTES
    reason = {
        "records": "record_limit",
        "samples": "sample_limit",
        "bytes": "input_bytes_limit",
    }[limit]
    assert payload["excluded_record_counts"][reason] > 0
    selected = [
        item for invocation in spy.call_args_list for item in invocation.args[0]
    ]
    input_size = len(json.dumps(selected, ensure_ascii=False).encode())
    assert input_size <= MAX_ANALYSIS_INPUT_BYTES
    assert all(item in original for item in selected)
    assert selected[0]["observation_id"] == inputs[-1]["observation_id"]
    assert checkpoint["records"] == original
    output_size = len(json.dumps(payload, ensure_ascii=False).encode())
    assert output_size <= MAX_METRIC_ANALYSIS_CONTEXT_BYTES


def test_one_oversized_record_is_not_passed_to_analyzer(tmp_path, monkeypatch):
    item = raw(instant())
    item["result"]["metadata"] = ["x" * 8000] * 40
    _, session, checkpoint, worker, _ = loaded_context(tmp_path, [item])
    spy = Mock(wraps=analyze_metric_evidence)
    monkeypatch.setattr("investigation.worker.analyze_metric_evidence", spy)
    payload = details(worker, session, checkpoint)["metric_analysis"]
    assert payload["status"] == "insufficient_evidence"
    assert payload["excluded_record_counts"]["input_bytes_limit"] == 1
    assert payload["reports"] == []
    spy.assert_not_called()


def test_record_limit_is_shared_across_primary_and_dependency(tmp_path, monkeypatch):
    inputs = []
    for index in range(MAX_ANALYSIS_OBSERVATIONS + 6):
        dependency = index % 2 == 0
        inputs.append(raw(
            instant(str(index), "postgres_up" if dependency else "http_request_rate"),
            "order-db" if dependency else "order-service",
        ))
    _, session, checkpoint, worker, _ = loaded_context(tmp_path, inputs)
    spy = Mock(wraps=analyze_metric_evidence)
    monkeypatch.setattr("investigation.worker.analyze_metric_evidence", spy)
    payload = details(worker, session, checkpoint)["metric_analysis"]
    assert payload["selected_record_count"] == MAX_ANALYSIS_OBSERVATIONS
    assert payload["excluded_record_counts"]["record_limit"] == 6
    assert spy.call_count == 2
    assert sum(len(invocation.args[0]) for invocation in spy.call_args_list) == 64
    for invocation in spy.call_args_list:
        assert all(
            item["arguments"]["service"] == invocation.kwargs["service"]
            for item in invocation.args[0]
        )


@pytest.mark.parametrize("limit", ["input", "analyzer_output", "context_output"])
def test_analyzer_and_context_limits_have_explicit_safe_fallbacks(
    tmp_path,
    monkeypatch,
    limit,
):
    inputs = [raw(instant())]
    if limit == "input":
        inputs[0]["result"]["metadata"] = "x" * 8193
    elif limit == "analyzer_output":
        inputs = [raw(instant(str(i))) for i in range(64)]
        for item in inputs:
            item["result"]["promql"] = "q" * 1024
    else:
        monkeypatch.setattr(
            "investigation.worker.MAX_METRIC_ANALYSIS_CONTEXT_BYTES", 2000,
        )
        inputs = [raw(aligned())]
    _, session, checkpoint, worker, _ = loaded_context(tmp_path, inputs)
    payload = details(worker, session, checkpoint)["metric_analysis"]
    assert payload["status"] == "insufficient_evidence"
    if limit == "context_output":
        assert payload["reason"] == "metric_analysis_context_size_limit"
        assert payload["reports"] == []
        assert len(json.dumps(payload, ensure_ascii=False).encode()) <= 2000
    else:
        report = payload["reports"][0]
        code = (
            "analysis_input_limit_exceeded" if limit == "input"
            else "analysis_output_limit_exceeded"
        )
        assert code in report_codes(report)
        assert report["confirmed_observations"] == []


@pytest.mark.parametrize("error", [ValueError, RuntimeError, TypeError])
def test_optional_analysis_errors_do_not_change_session_or_call_a_model(
    tmp_path,
    monkeypatch,
    error,
):
    manager, session, checkpoint, worker, agent = loaded_context(
        tmp_path, [raw(instant())],
    )
    before = manager.store.get(session.session_id).model_dump()
    original = deepcopy(checkpoint["records"])
    monkeypatch.setattr(
        "investigation.worker.analyze_metric_evidence",
        Mock(side_effect=error("untrusted error text")),
    )
    payload = details(worker, session, checkpoint)["metric_analysis"]
    assert payload["status"] == "insufficient_evidence"
    assert payload["reason"] == "metric_analysis_failed"
    assert "untrusted error text" not in json.dumps(payload)
    assert manager.store.get(session.session_id).model_dump() == before
    assert checkpoint["records"] == original
    agent.request_durable_response.assert_not_called()
    assert agent.executions == []


def test_verified_fresh_valid_chain_receives_runtime_analysis_and_keeps_outputs(
    tmp_path,
):
    item = raw(instant(at=COLLECTED))
    _, session, checkpoint, worker, _ = loaded_context(tmp_path, [item], verified=True)
    checkpoint["history"] = [
        {"role": "user", "content": "old context without analysis"},
        call(item["name"], item["arguments"], item["call_id"]),
        {
            "type": "function_call_output",
            "call_id": item["call_id"],
            "output": json.dumps(item["result"]),
        },
    ]
    old = deepcopy(checkpoint["history"])
    assert valid_chain(old)
    current = effective_records(
        checkpoint["records"], checkpoint["identity"], COLLECTED, session.service,
    )
    assert current == [item]
    messages = worker._context(session.session_id, checkpoint)
    assert messages[:-1] == old
    assert valid_chain(messages)
    assert messages[-1]["role"] == "user"
    payload = json.loads(messages[-1]["content"])
    assert payload["context_origin"] == "runtime_derived_metric_analysis"
    report = payload["metric_analysis"]["reports"][0]
    assert report["source_observation_ids"] == [item["observation_id"]]
    assert checkpoint["history"] == old
    assert messages[-2]["call_id"] == item["call_id"]


@pytest.mark.parametrize("verified", [False, True])
@pytest.mark.parametrize("complete", [False, True])
def test_repeated_context_does_not_mutate_records_chain_pending_or_persist_analysis(
    tmp_path,
    verified,
    complete,
):
    manager, session, checkpoint, worker, agent = loaded_context(
        tmp_path, [raw(history())], verified=verified,
    )
    item = checkpoint["records"][0]
    checkpoint["history"] = [call(item["name"], item["arguments"], item["call_id"])]
    if complete:
        checkpoint["history"].append({
            "type": "function_call_output",
            "call_id": item["call_id"],
            "output": json.dumps(item["result"]),
        })
    checkpoint["pending"] = [] if complete else [checkpoint["history"][0]]
    old = deepcopy(checkpoint)
    saved_before = manager.store.load_checkpoint(manager.store.get(session.session_id))
    effective_before = effective_records(
        checkpoint["records"], checkpoint["identity"], COLLECTED, session.service,
    )
    first = details(worker, session, checkpoint)
    assert details(worker, session, checkpoint) == first
    for key in ("records", "history", "pending", "phase", "identity"):
        assert checkpoint.get(key) == old.get(key)
    assert "metric_analysis" not in checkpoint
    Checkpoint.model_validate(checkpoint)
    assert manager.store.load_checkpoint(
        manager.store.get(session.session_id),
    ) == saved_before
    assert effective_records(
        checkpoint["records"], checkpoint["identity"], COLLECTED, session.service,
    ) == effective_before
    assert agent.build_incident_prompt.call_args.args[0].evidence == []


@pytest.mark.parametrize("enabled", [False, True, "failed"])
def test_real_worker_loop_keeps_model_tool_and_cache_counts(
    tmp_path,
    monkeypatch,
    enabled,
):
    network = Mock(side_effect=AssertionError("network_forbidden"))
    monkeypatch.setattr(requests, "get", network)
    if not enabled:
        monkeypatch.setattr(Worker, "_metric_analysis_context", lambda *args: None)
    elif enabled == "failed":
        monkeypatch.setattr(
            "investigation.worker.analyze_metric_evidence",
            Mock(side_effect=RuntimeError("local analysis failure")),
        )
    manager = create_runtime(tmp_path, clock=lambda: COLLECTED)
    create_session(manager)
    source = aligned(values=(0.0, 1.0, 1.0))
    arguments = raw(source)["arguments"]

    def tool_result(item):
        if item.name == "query_metrics_aligned":
            return deepcopy(source.result)
        assert item.name == "get_running_version"
        return {"status": "ok", "service": "order-service", "commit_sha": SHA}

    agent = FixtureAgent([
        response(answer="", calls=[call(source.name, arguments, "first")]),
        response(answer="", calls=[call(source.name, arguments, "cached")]),
        response(),
    ], tool_result, clock=lambda: COLLECTED)
    finished = Worker(manager, agent, provider_verified=True).run_once()
    assert finished.status == SessionStatus.COMPLETED
    assert finished.total_model_requests == 3
    assert finished.total_tool_executions == 2
    assert finished.cache_hits == 1
    assert agent.executions == ["get_running_version", "query_metrics_aligned"]
    checkpoint = manager.store.load_checkpoint(finished)
    assert checkpoint["schema_version"] == 1
    assert "metric_analysis" not in checkpoint
    assert len(checkpoint["records"]) == 2
    if enabled:
        messages = agent.request_durable_response.call_args_list[-1].args[0]
        content = messages[0]["content"]
        payload = json.loads(content.rsplit("\n", 1)[-1])["metric_analysis"]
        if enabled == "failed":
            assert payload["reason"] == "metric_analysis_failed"
        else:
            report = payload["reports"][0]
            assert report["hypotheses_to_verify"]
            assert report["suggested_checks"]
    network.assert_not_called()


def test_saved_metric_recovers_after_crash_without_replay_or_fabricated_output(
    tmp_path,
):
    class Crash(BaseException):
        pass

    clock = SimpleNamespace(now=COLLECTED)
    manager = create_runtime(tmp_path, clock=lambda: clock.now)
    session = create_session(manager)
    source = aligned()
    arguments = raw(source)["arguments"]
    agent = FixtureAgent([
        response(answer="", calls=[call(source.name, arguments, "persisted")]),
    ], lambda item: deepcopy(source.result) if item.name == source.name else {
        "status": "ok", "service": "order-service", "commit_sha": SHA,
    })

    def boundary(phase, session_id):
        if phase == "tool_checkpoint_committed" and agent.executions[-1] == source.name:
            raise Crash()

    with pytest.raises(Crash):
        Worker(manager, agent, boundary_hook=boundary).run_once()
    stored = manager.store.load_checkpoint(manager.store.get(session.session_id))
    metric = deepcopy(stored["records"][-1])
    assert stored["pending"]
    assert not valid_chain(stored["history"])
    assert not any(
        item.get("type") == "function_call_output" for item in stored["history"]
    )
    clock.now += timedelta(seconds=121)
    restarted = create_runtime(tmp_path, clock=lambda: clock.now)
    next_agent = FixtureAgent(clock=lambda: clock.now)
    finished = Worker(restarted, next_agent, provider_verified=True).run_once()
    assert finished.status == SessionStatus.COMPLETED
    assert finished.total_model_requests == 2
    # Two version probes plus one saved metric call.
    assert finished.total_tool_executions == 3
    assert next_agent.executions == ["get_running_version"]
    messages = next_agent.request_durable_response.call_args.args[0]
    assert not any(item.get("type") == "function_call_output" for item in messages)
    payload = json.loads(messages[0]["content"].rsplit("\n", 1)[-1])
    report = payload["metric_analysis"]["reports"][0]
    assert report["source_observation_ids"] == [metric["observation_id"]]
    reopened = RuntimeStore(restarted.store.path, clock=lambda: clock.now)
    checkpoint = reopened.load_checkpoint(reopened.get(finished.session_id))
    assert metric in checkpoint["records"]
    assert checkpoint["pending"] == []


def test_metric_analysis_preserves_human_wait_without_extra_model_or_tool_calls(
    tmp_path,
):
    manager = create_runtime(tmp_path, clock=lambda: COLLECTED)
    create_session(manager)
    source = aligned(values=(0.0, 1.0, 1.0))
    agent = FixtureAgent([
        response(answer="", calls=[call(
            source.name, raw(source)["arguments"], "metric",
        )]),
        response(answer="", calls=[call(
            "request_human_input", {"question": "请补充部署背景。"}, "human",
        )]),
    ], lambda item: deepcopy(source.result) if item.name == source.name else {
        "status": "ok", "service": "order-service", "commit_sha": SHA,
    })
    worker = Worker(manager, agent, provider_verified=True)
    waiting = worker.run_once()
    assert waiting.status == SessionStatus.WAITING_HUMAN
    assert waiting.human_request_count == 1
    assert waiting.total_model_requests == 2
    assert waiting.total_tool_executions == 2
    content = agent.request_durable_response.call_args.args[0][0]["content"]
    assert json.loads(content.rsplit("\n", 1)[-1])["metric_analysis"]["reports"]
    assert worker.run_once() is None
    saved = manager.store.get(waiting.session_id)
    assert saved.status == SessionStatus.WAITING_HUMAN
    assert saved.total_model_requests == 2
    assert saved.total_tool_executions == 2
    assert agent.executions == ["get_running_version", "query_metrics_aligned"]
