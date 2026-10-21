"""Offline protocol checks; local original files are never committed fixtures."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from scripts import p65_readonly_investigation as p65
from scripts.p65_model_boundary import (
    BoundedResponses, encoded, validate_response_schema,
)
from scripts.p65_protocol_replay import DEFAULT_SOURCE, read_frozen
from tests.test_p65_readonly_investigation import metric, prepared, response, run


@pytest.fixture
def payload():
    value = response([metric("synthetic-call")]).model_dump(mode="json")
    value["usage"] = {
        "input_tokens": 2,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 1,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 3,
    }
    return value


@pytest.mark.parametrize("representation", ["missing", "null", "reported"])
def test_only_cache_write_metadata_can_remain_unknown(payload, representation):
    details = payload["usage"]["input_tokens_details"]
    if representation != "missing":
        details["cache_write_tokens"] = None if representation == "null" else 7
    before = encoded(payload)
    validated, notes = validate_response_schema(payload)
    assert encoded(payload) == before
    assert validated.usage.input_tokens_details.cache_write_tokens == (
        7 if representation == "reported" else None
    )
    if representation == "reported":
        assert notes == []
    else:
        assert notes == [{
            "path": "usage.input_tokens_details.cache_write_tokens",
            "reason": "provider_field_not_reported",
            "representation": representation,
            "value_known": False,
        }]


@pytest.mark.parametrize("field", [
    "id", "created_at", "model", "object", "output", "parallel_tool_calls",
    "tool_choice", "tools",
])
def test_every_other_required_response_field_still_required(payload, field):
    del payload[field]
    with pytest.raises(ValidationError):
        validate_response_schema(payload)


@pytest.mark.parametrize("path,value", [
    (("id",), 123),
    (("created_at",), "123"),
    (("parallel_tool_calls",), 1),
    (("object",), "completion"),
    (("usage", "input_tokens"), "2"),
    (("usage", "output_tokens_details", "reasoning_tokens"), False),
    (("usage", "input_tokens_details", "cached_tokens"), None),
    (("usage", "input_tokens_details", "cache_write_tokens"), "0"),
    (("usage", "input_tokens_details", "cache_write_tokens"), False),
    (("usage", "input_tokens_details", "cache_write_tokens"), 0.5),
])
def test_wrong_types_are_not_coerced_or_hidden_by_compatibility(payload, path, value):
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        validate_response_schema(payload)


@pytest.mark.parametrize("path", [
    ("usage", "input_tokens"),
    ("usage", "input_tokens_details", "cached_tokens"),
    ("usage", "output_tokens_details", "reasoning_tokens"),
    ("output", 0, "arguments"),
    ("output", 0, "call_id"),
    ("output", 0, "name"),
])
def test_nested_required_fields_still_required(payload, path):
    target = payload
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    with pytest.raises(ValidationError):
        validate_response_schema(payload)


@pytest.mark.parametrize("changes", [
    {"id": ""}, {"id": "  "}, {"model": "wrong-model"},
    {"status": "incomplete"}, {"status": "failed"},
    {"error": {"code": "server_error", "message": "synthetic"}},
    {"incomplete_details": {"reason": "max_output_tokens"}},
    {"output": []},
])
def test_semantic_protocol_errors_still_stop(payload, tmp_path, changes):
    payload.update(changes)
    validated, _ = validate_response_schema(payload)
    adapter = BoundedResponses(tmp_path, {}, Mock())
    adapter.requests = 1
    with pytest.raises(ValueError, match="p65_"):
        adapter._validate_response(validated)


@pytest.mark.parametrize("change", [
    {"type": "unknown_output"}, {"call_id": 1}, {"arguments": {}},
])
def test_malformed_output_structure_rejected(payload, change):
    payload["output"][0].update(change)
    with pytest.raises(ValidationError):
        validate_response_schema(payload)


def test_sdk_supported_but_experiment_unsupported_output_still_rejected(payload, tmp_path):
    payload["output"] = [{
        "type": "function_call_output", "id": "synthetic-output-item",
        "call_id": "synthetic-orphan", "output": "no_data", "status": "completed",
    }]
    validated, _ = validate_response_schema(payload)
    adapter = BoundedResponses(tmp_path, {}, Mock())
    with pytest.raises(ValueError, match="unsupported_response_item"):
        adapter._validate_response(validated)


@pytest.mark.parametrize("arguments", [
    "{", "[]", '{"service":"order-service","service":"other"}',
    '{"metric":NaN}',
])
def test_bad_json_rejected_even_with_unreported_cache_writes(payload, tmp_path, arguments):
    payload["output"][0]["arguments"] = arguments
    validated, _ = validate_response_schema(payload)
    adapter = BoundedResponses(tmp_path, {}, Mock())
    with pytest.raises(ValueError, match="invalid_call_arguments"):
        adapter._validate_response(validated)


@pytest.mark.parametrize("kind", ["duplicate", "unknown_tool", "empty_call_id"])
def test_illegal_or_duplicate_calls_stop_before_worker(prepared, tmp_path, payload, kind):
    if kind == "duplicate":
        payload["output"].append(deepcopy(payload["output"][0]))
    elif kind == "unknown_tool":
        payload["output"][0]["name"] = "execute_remediation"
    else:
        payload["output"][0]["call_id"] = ""
    validated, _ = validate_response_schema(payload)
    report = run(prepared, tmp_path, [validated])
    assert report["session"]["status"] == "FAILED"
    assert report["session"]["total_tool_executions"] == 1
    assert report["real_model_sdk_requests"] == 0
    assert report["forbidden_io_attempts"] == []


@pytest.mark.parametrize("corruption", [
    "pending", "missing", "duplicate", "orphan", "altered", "unrecorded",
])
def test_next_input_requires_complete_committed_bound_outputs(tmp_path, payload, corruption):
    previous, _ = validate_response_schema(payload)
    call = previous.output[0]
    output = {"type": "function_call_output", "call_id": call.call_id,
              "output": '{"status":"no_data"}'}
    checkpoint = {
        "pending": [],
        "history": [call.model_dump(mode="json", exclude_none=True), output],
        "records": [{"call_id": call.call_id, "name": call.name,
                     "observation_id": "synthetic-obs", "result": {"status": "no_data"}}],
        "trace": [],
    }
    if corruption == "pending":
        checkpoint["pending"] = [{"call_id": call.call_id}]
    elif corruption == "missing":
        checkpoint["history"].pop()
    elif corruption == "duplicate":
        checkpoint["history"].append(deepcopy(output))
    elif corruption == "orphan":
        checkpoint["history"].append(output | {"call_id": "not-requested"})
    elif corruption == "altered":
        output["output"] = '{"status":"ok","value":42}'
    else:
        checkpoint["records"] = []
    adapter = BoundedResponses(tmp_path, {}, Mock())
    adapter.previous = previous
    adapter.previous_input = [{"role": "user", "content": "synthetic"}]
    before = deepcopy(checkpoint)
    with pytest.raises(ValueError, match="p65_"):
        adapter._input(checkpoint)
    assert checkpoint == before


@pytest.fixture
def frozen():
    if not DEFAULT_SOURCE.exists():
        pytest.skip("operator local sealed first Provider response unavailable")
    return read_frozen(DEFAULT_SOURCE)


def test_actual_original_has_exactly_one_schema_error_and_retains_output(frozen):
    from openai.types.responses import Response

    wire, sdk = frozen
    raw = json.loads(wire)
    for value, error_type in ((raw, "missing"), (sdk, "int_type")):
        before = encoded(value)
        with pytest.raises(ValidationError) as caught:
            Response.model_validate(value, strict=True)
        assert [(e["loc"], e["type"]) for e in caught.value.errors()] == [
            (("usage", "input_tokens_details", "cache_write_tokens"), error_type),
        ]
        validated, notes = validate_response_schema(value)
        assert encoded(value) == before
        assert notes[0]["reason"] == "provider_field_not_reported"
        assert validated.usage.input_tokens_details.cache_write_tokens is None
        assert [i.model_dump(mode="json", exclude_none=True)
                for i in validated.output] == raw["output"]


def test_actual_first_response_uses_original_worker_budget_and_protocol(tmp_path, frozen):
    from investigation.checkpoint import valid_chain
    from investigation.store import RuntimeStore

    prepared_dir = tmp_path / "prepared"
    p65.prepare(p65.DEFAULT_CAPTURE, prepared_dir, offline_tool_budget=4)
    report = p65.run_prepared(
        prepared_dir,
        tmp_path / "run",
        offline_first_response=frozen,
        mock_responses=p65.mock_plan("no_tools"),
    )
    root = tmp_path / "run"
    raw = json.loads(frozen[0])
    calls = [i for i in raw["output"] if i["type"] == "function_call"]
    assert report["model_selected_calls"] == frozen[1]["output"][1:]
    assert report["session"]["status"] == "COMPLETED"
    assert report["session"]["total_model_requests"] == 2
    assert report["session"]["total_tool_executions"] == 4
    assert report["session"]["cache_hits"] == 0
    assert len(report["checkpoint"]["records"]) == 4
    assert report["response_origins"] == [
        "real_provider_first_response_offline_replay", "scripted_mock_followup",
    ]
    assert report["real_model_sdk_requests"] == 0
    assert report["real_service_requests"] == report["remediation_executions"] == 0
    assert report["forbidden_io_attempts"] == []
    assert report["result_trust"]["trusted"] is False
    assert report["result_trust"]["semantic_status"] == "pending_review"
    assert (root / "response-01.raw.txt").read_bytes() == frozen[0]
    assert json.loads((root / "response-01.sdk.json").read_bytes()) == frozen[1]
    completion = json.loads((root / "completion-01.json").read_bytes())
    assert completion["usage"] == frozen[1]["usage"]
    assert completion["compatibility"][0]["representation"] == "missing"
    assert completion["compatibility"][1]["representation"] == "null"
    boundaries = json.loads((root / "worker-boundaries.json").read_bytes())
    before_followup = next(b for b in boundaries if b["phase"] == "model_before_io"
                           and b["session"]["total_model_requests"] == 2)
    checkpoint = before_followup["checkpoint"]
    assert checkpoint["pending"] == []
    assert valid_chain(checkpoint["history"])
    sent = json.loads((root / "request-02.json").read_bytes())["body"]["input"]
    assert sent[1:5] == raw["output"]  # Commentary and all calls, original order.
    assert valid_chain(sent)
    for call, output in zip(calls, sent[5:], strict=True):
        assert output["type"] == "function_call_output"
        assert output["call_id"] == call["call_id"]
        assert output in checkpoint["history"]
        record = next(r for r in checkpoint["records"] if r["call_id"] == call["call_id"])
        assert record["arguments"] == json.loads(call["arguments"])
        assert record["success"] is True  # Python ran; no_data is not valid evidence.
        assert json.loads(output["output"]) == record["result"]
    log_result = report["checkpoint"]["records"][-1]["result"]
    assert log_result["reason"] == "outside_approved_frozen_evidence"
    assert log_result["status"] == "no_data"
    assert "captured_result" not in log_result and "logs" not in log_result
    assert len([b for b in boundaries if b["phase"] == "tool_after_io"]) == 4
    assert len([e for e in report["events"] if e["event_type"] == "tool_reserved"]) == 4
    reopened = RuntimeStore(root / "runtime.sqlite3", read_only=True)
    saved = reopened.get(report["session"]["session_id"])
    assert reopened.load_checkpoint(saved) == report["checkpoint"]
    assert read_frozen(DEFAULT_SOURCE) == frozen


def test_real_first_batch_cannot_allow_a_fifth_execution(tmp_path, frozen):
    prepared_dir = tmp_path / "prepared"
    p65.prepare(p65.DEFAULT_CAPTURE, prepared_dir, offline_tool_budget=4)
    report = p65.run_prepared(
        prepared_dir,
        tmp_path / "run",
        offline_first_response=frozen,
        mock_responses=[response([metric("scripted-fifth-call")])],
    )
    assert report["session"]["status"] == "EXHAUSTED"
    assert report["session"]["total_tool_executions"] == 4
    assert len(report["checkpoint"]["records"]) == 4
    assert not any(r["call_id"] == "scripted-fifth-call"
                   for r in report["checkpoint"]["records"])
    assert not (tmp_path / "run/request-03.json").exists()
    assert report["real_model_sdk_requests"] == 0


def test_real_consumed_approval_and_changed_code_do_not_grant_new_execution(tmp_path, frozen):
    old_prepared = p65.ROOT / "data/p65-prepared-20261019-01"
    approval = json.loads((DEFAULT_SOURCE / "authorization.json").read_bytes())
    ledger = p65.ROOT / "data/p65-paid-consumption" / (
        approval["plan_sha256"] + ".consumed.json"
    )
    before = ledger.read_bytes()
    sdk = Mock(side_effect=AssertionError("must never construct SDK"))
    with pytest.raises(ValueError, match="scope_or_code_changed"):
        p65.run_prepared(old_prepared, tmp_path / "blocked", live=True,
                         authorization=approval, sdk_factory=sdk)
    sdk.assert_not_called()
    assert ledger.read_bytes() == before


def test_replay_cannot_be_combined_with_paid_mode(tmp_path, frozen):
    sdk = Mock()
    with pytest.raises(ValueError, match="replay_cannot_enter_sdk"):
        p65.run_prepared(tmp_path / "absent", tmp_path / "blocked", live=True,
                         offline_first_response=frozen, sdk_factory=sdk)
    sdk.assert_not_called()


def test_frozen_sdk_mismatch_stops_before_any_selected_tool(prepared, tmp_path, frozen):
    wire, sdk = frozen
    changed = deepcopy(sdk)
    changed["output"][1]["call_id"] += "-altered"
    report = p65.run_prepared(
        prepared, tmp_path / "run", offline_first_response=(wire, changed),
        mock_responses=p65.mock_plan("no_tools"),
    )
    assert report["session"]["status"] == "FAILED"
    assert report["session"]["total_tool_executions"] == 1
    assert report["real_model_sdk_requests"] == 0
    assert not (tmp_path / "run/request-02.json").exists()
