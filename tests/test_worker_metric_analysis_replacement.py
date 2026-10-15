"""P5.3b.2: replace Runtime supplements without rewriting real Responses items."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from investigation.checkpoint import valid_chain
from investigation.models import SessionStatus
from investigation.store import RuntimeStore
from investigation.worker import Worker
from runtime_support import FixtureAgent, call, create_runtime, create_session, response
from test_metric_analysis import COLLECTED, instant
from test_worker_metric_analysis import loaded_context, raw
from test_worker_metric_analysis_growth import analyses, function_items


def complete_context(tmp_path):
    item = raw(instant(at=COLLECTED))
    manager, session, checkpoint, worker, agent = loaded_context(
        tmp_path, [item], verified=True,
    )
    checkpoint["history"] = [
        {"role": "system", "content": "system instruction"},
        {"role": "developer", "content": "developer instruction"},
        {"role": "user", "content": "original investigation context"},
        call(item["name"], item["arguments"], item["call_id"]),
        {
            "type": "function_call_output",
            "call_id": item["call_id"],
            "output": json.dumps(item["result"]),
        },
        {"type": "reasoning", "id": "reasoning-1", "summary": []},
        {"role": "assistant", "content": "an unverified model explanation"},
    ]
    supplement = worker._context(session.session_id, checkpoint)[-1]
    return manager, session, checkpoint, worker, agent, supplement


def test_multiple_old_runtime_inputs_are_replaced_without_mutating_caller(tmp_path):
    manager, session, checkpoint, worker, agent, supplement = complete_context(tmp_path)
    original = deepcopy(checkpoint["history"])
    checkpoint["history"] = [supplement] + original[:5] + [
        deepcopy(supplement),
    ] + original[5:] + [deepcopy(supplement)]
    before = deepcopy(checkpoint["history"])
    saved = manager.store.load_checkpoint(manager.store.get(session.session_id))
    result = worker._context(session.session_id, checkpoint)
    assert result is not checkpoint["history"]
    assert result[:-1] == original
    assert len(analyses(result)) == 1
    assert function_items(result) == function_items(before)
    assert valid_chain(result)
    assert checkpoint["history"] == before
    assert manager.store.load_checkpoint(manager.store.get(session.session_id)) == saved
    agent.request_durable_response.assert_not_called()
    assert agent.executions == []


@pytest.mark.parametrize(
    "variant",
    [
        "marker_only", "nested", "quoted", "prefixed", "invalid_json",
        "content_parts", "assistant", "system", "developer", "reasoning",
        "human_metadata", "message_metadata", "extra_payload_field",
        "wrong_origin", "wrong_usage", "not_analysis", "current_state",
        "wrong_scope", "wrong_selection", "wrong_service_type", "wrong_status",
        "wrong_reports", "bad_time", "naive_time", "missing_time",
    ],
)
def test_similar_user_human_and_model_messages_are_preserved(tmp_path, variant):
    _, session, checkpoint, worker, _, supplement = complete_context(tmp_path)
    payload = json.loads(supplement["content"])
    message = deepcopy(supplement)
    if variant == "marker_only":
        payload = {"context_origin": payload["context_origin"]}
    elif variant == "nested":
        payload = {"human_provided_unverified_context": payload}
    elif variant == "quoted":
        payload = json.dumps(payload, ensure_ascii=False)
    elif variant == "prefixed":
        message["content"] = "human quoted this example:\n" + message["content"]
    elif variant == "invalid_json":
        message["content"] = '{"context_origin": "runtime_derived_metric_analysis"'
    elif variant == "content_parts":
        message["content"] = [{"type": "input_text", "text": message["content"]}]
    elif variant in {"assistant", "system", "developer"}:
        message["role"] = variant
    elif variant == "reasoning":
        message = {"type": "reasoning", "summary": [payload]}
    elif variant == "human_metadata":
        message["human_message_id"] = "human-1"
    elif variant == "message_metadata":
        message.update(type="message", id="msg-model", status="completed")
    elif variant == "extra_payload_field":
        payload["human_note"] = "quoted analysis is unverified"
    elif variant == "wrong_origin":
        payload["context_origin"] = "human_provided_unverified_context"
    elif variant == "wrong_usage":
        payload["evidence_usage"] = "a human's own explanation"
    elif variant == "not_analysis":
        payload["metric_analysis"] = None
    elif variant == "current_state":
        payload["metric_analysis"]["use_for_current_state"] = True
    elif variant == "wrong_scope":
        payload["metric_analysis"]["evidence_scope"] = "current_state"
    elif variant == "wrong_selection":
        payload["metric_analysis"]["selection"] = "human_selected"
    elif variant == "wrong_service_type":
        payload["metric_analysis"]["session_service"] = None
    elif variant == "wrong_status":
        payload["metric_analysis"]["status"] = []
    elif variant == "wrong_reports":
        payload["metric_analysis"]["reports"] = "human report"
    elif variant == "bad_time":
        payload["context_collected_at"] = "yesterday"
    elif variant == "naive_time":
        payload["context_collected_at"] = "2026-10-14T02:00:00"
    elif variant == "missing_time":
        payload.pop("context_collected_at")
    if message.get("content") == supplement["content"]:
        message["content"] = json.dumps(payload, ensure_ascii=False)
    original = deepcopy(checkpoint["history"])
    checkpoint["history"] += [supplement, message, deepcopy(supplement)]
    before = deepcopy(checkpoint["history"])
    assert valid_chain(before)
    result = worker._context(session.session_id, checkpoint)
    assert result[:-1] == original + [message]
    assert function_items(result) == function_items(before)
    assert checkpoint["history"] == before
    assert valid_chain(result)


def test_new_analysis_is_delivered_even_with_unchanged_source_observation_ids(tmp_path):
    _, session, checkpoint, worker, _, supplement = complete_context(tmp_path)
    old = json.loads(supplement["content"])["metric_analysis"]
    source_ids = old["reports"][0]["source_observation_ids"]
    # A new local analysis result can change without a new raw observation.
    latest = deepcopy(old)
    latest["reports"][0]["limitations"].append({
        "code": "fixture_updated_interpretation",
        "message": "a revised local result for the same observations",
    })
    worker._metric_analysis_context = Mock(return_value=latest)
    checkpoint["history"].append(supplement)
    before = deepcopy(checkpoint["history"])
    delivered = worker._context(session.session_id, checkpoint)
    assert len(analyses(delivered)) == 1
    actual = analyses(delivered)[0]["metric_analysis"]
    assert actual == latest and actual != old
    assert actual["reports"][0]["source_observation_ids"] == source_ids
    assert function_items(delivered) == function_items(before)
    assert checkpoint["history"] == before


@pytest.mark.parametrize("fallback", ["absent", "failed", "output_limit", "input_limit"])
def test_unavailable_or_limited_analysis_does_not_reuse_old_conclusions(
    tmp_path,
    monkeypatch,
    fallback,
):
    _, session, checkpoint, worker, _, supplement = complete_context(tmp_path)
    original = deepcopy(checkpoint["history"])
    checkpoint["history"].append(supplement)
    if fallback == "absent":
        worker._metric_analysis_context = Mock(return_value=None)
    elif fallback == "failed":
        monkeypatch.setattr(
            "investigation.worker.analyze_metric_evidence",
            Mock(side_effect=ValueError("fixture analysis failure")),
        )
    elif fallback == "output_limit":
        monkeypatch.setattr(
            "investigation.worker.MAX_METRIC_ANALYSIS_CONTEXT_BYTES", 1000,
        )
    else:
        monkeypatch.setattr("investigation.worker.MAX_ANALYSIS_INPUT_BYTES", 2)
    before = deepcopy(checkpoint["history"])
    result = worker._context(session.session_id, checkpoint)
    assert function_items(result) == function_items(before)
    assert valid_chain(result)
    assert checkpoint["history"] == before
    if fallback == "absent":
        assert result == original
        assert analyses(result) == []
    else:
        assert result[:-1] == original
        assert len(analyses(result)) == 1
        latest = analyses(result)[0]["metric_analysis"]
        assert latest["status"] == "insufficient_evidence"
        assert latest["reports"] == []
        assert latest["reason"] == {
            "failed": "metric_analysis_failed",
            "output_limit": "metric_analysis_context_size_limit",
            "input_limit": "no_authorized_metric_records_within_limits",
        }[fallback]
        assert latest["use_for_current_state"] is False


@pytest.mark.parametrize("fallback", ["unverified", "incomplete", "expired"])
def test_non_complete_chains_still_use_the_existing_evidence_rebuild(
    tmp_path,
    fallback,
):
    _, session, checkpoint, worker, _, supplement = complete_context(tmp_path)
    checkpoint["history"].append(supplement)
    if fallback == "unverified":
        worker.provider_verified = False
    elif fallback == "incomplete":
        checkpoint["history"] = checkpoint["history"][:4]
    else:
        worker.store.clock = lambda: COLLECTED + timedelta(seconds=16)
    before = deepcopy(checkpoint["history"])
    result = worker._context(session.session_id, checkpoint)
    assert len(result) == 1 and result[0]["role"] == "user"
    payload = json.loads(result[0]["content"].rsplit("\n", 1)[-1])
    assert "recovery" in payload
    assert "context_origin" not in payload
    assert payload["metric_analysis"]["use_for_current_state"] is False
    assert function_items(result) == []
    assert checkpoint["history"] == before


@pytest.mark.parametrize(
    "crash_phase",
    ["model_before_io", "model_after_io", "model_checkpoint_committed"],
)
def test_crash_at_replacement_boundaries_retains_only_committed_model_history(
    tmp_path,
    crash_phase,
):
    class Crash(BaseException):
        pass

    clock = SimpleNamespace(now=COLLECTED)
    manager = create_runtime(tmp_path, clock=lambda: clock.now)
    session = create_session(manager)
    arguments = {"service": session.service, "metric": "http_request_rate"}
    agent = FixtureAgent([
        response(answer="", calls=[call("query_metrics", arguments, "first")]),
        response(answer="", calls=[call("query_metrics", arguments, "cached")]),
        response(),
    ], clock=lambda: clock.now)

    def boundary(phase, session_id):
        saved = manager.store.get(session_id)
        if phase == crash_phase and saved.total_model_requests == 3:
            raise Crash()

    worker = Worker(manager, agent, boundary_hook=boundary, provider_verified=True)
    with pytest.raises(Crash):
        worker.run_once()
    saved = manager.store.get(session.session_id)
    reopened = RuntimeStore(manager.store.path, read_only=True)
    checkpoint = reopened.load_checkpoint(reopened.get(session.session_id))
    assert checkpoint == manager.store.load_checkpoint(saved)
    assert len(analyses(checkpoint["history"])) == 1
    assert valid_chain(checkpoint["history"])
    assert [
        item["call_id"] for item in checkpoint["history"]
        if item.get("type") == "function_call"
    ] == ["first", "cached"]
    assert saved.total_model_requests == 3
    assert saved.total_tool_executions == 2
    assert saved.cache_hits == 1
    committed = crash_phase == "model_checkpoint_committed"
    assert bool(checkpoint.get("result")) == committed
    metric = next(item for item in checkpoint["records"] if item["name"] == "query_metrics")
    # Default lease recovery also expires the instant metric's original TTL.
    clock.now += timedelta(seconds=121)
    restarted = create_runtime(tmp_path, clock=lambda: clock.now)
    next_agent = FixtureAgent(clock=lambda: clock.now)
    finished = Worker(restarted, next_agent, provider_verified=True).run_once()
    assert finished.status == SessionStatus.COMPLETED
    final = restarted.store.load_checkpoint(finished)
    assert metric in final["records"]
    assert len(analyses(final["history"])) == 1
    assert valid_chain(final["history"])
    assert finished.total_model_requests == (3 if committed else 4)
    assert finished.total_tool_executions == (2 if committed else 3)
    assert finished.cache_hits == 1
    assert next_agent.executions == ([] if committed else ["get_running_version"])
    if not committed:
        messages = next_agent.request_durable_response.call_args.args[0]
        assert function_items(messages) == []
        assert "recovery" in analyses(messages)[0]
        assert analyses(messages)[0]["metric_analysis"]["use_for_current_state"] is False
