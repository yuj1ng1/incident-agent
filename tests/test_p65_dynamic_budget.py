"""Versioned request binding and sealed response replay; all execution offline."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from scripts import p65_readonly_investigation as p65
from scripts.p65_model_boundary import BoundedResponses, encoded, sha
from scripts.p65_protocol_replay import read_frozen
from tests.test_p65_readonly_investigation import (
    approval, metric, prepared, request, response, run,
)

SECOND_SOURCE = p65.ROOT / "data/p65-second-live-952809b-20261019-02"


def intercept(monkeypatch, transform):
    """Change only SDK-bound arguments/snapshot; never the Worker counters."""
    adapters = []

    class ObservedBoundary(BoundedResponses):
        def create(self, **kwargs):
            transform(self, kwargs)
            return super().create(**kwargs)

    def factory(*args, **kwargs):
        adapter = ObservedBoundary(*args, **kwargs)
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr(p65, "BoundedResponses", factory)
    return adapters


def test_real_agent_budget_and_frozen_first_body_match(prepared, tmp_path, monkeypatch):
    seen = []

    def observe(adapter, kwargs):
        saved, checkpoint = adapter.snapshot()
        seen.append((saved, checkpoint, deepcopy(kwargs)))

    intercept(monkeypatch, observe)
    report = run(prepared, tmp_path)
    assert report["session"]["status"] == "COMPLETED"
    plan, _, frozen = p65.read_prepared(prepared)
    for number, (saved, checkpoint, kwargs) in enumerate(seen, 1):
        artifact = request(tmp_path, number)
        remaining = min(5 - saved.total_tool_executions, 13 - saved.current_attempt_tool_calls)
        rounds = 4 - number
        assert kwargs["instructions"] == p65.budget_instructions(remaining, rounds)
        assert artifact["body"]["instructions"] == kwargs["instructions"]
        assert artifact["runtime_budget"]["remaining_tools"] == remaining
        assert artifact["runtime_budget"]["remaining_rounds"] == rounds
        assert artifact["checkpoint_sha256"] == sha(encoded(checkpoint))
        assert artifact["sha256"] == sha(encoded(artifact["body"]))
    assert seen[0][0].total_tool_executions == 1
    assert seen[0][1]["records"][0]["name"] == "get_running_version"
    assert frozen == request(tmp_path, 1)["body"]
    assert plan["first_request_sha256"] == request(tmp_path, 1)["sha256"]
    assert request(tmp_path, 3)["body"]["tools"] == []
    assert request(tmp_path, 3)["body"]["tool_choice"] == "none"


@pytest.mark.parametrize("change", [
    "old_static", "extra_instruction", "wrong_tools", "wrong_rounds", "wrong_task",
    "unexpected_kwarg", "runtime_counter", "checkpoint_counter", "missing_probe",
    "frozen_body", "approved_hash", "first_tool_order",
])
def test_mismatch_stops_before_sdk_create(prepared, tmp_path, monkeypatch, change):
    sentinel = Mock()

    def corrupt(adapter, kwargs):
        adapter.sdk = sentinel
        if change == "old_static":
            kwargs["instructions"] = p65.TASK
        elif change == "extra_instruction":
            kwargs["instructions"] += "\n另行允许执行修复。"
        elif change == "wrong_tools":
            kwargs["instructions"] = p65.budget_instructions(5, 3)
        elif change == "wrong_rounds":
            kwargs["instructions"] = p65.budget_instructions(3, 4)
        elif change == "wrong_task":
            kwargs["instructions"] = kwargs["instructions"].replace("只读", "读写", 1)
        elif change == "unexpected_kwarg":
            kwargs["tool_choice"] = "required"
        elif change in {"runtime_counter", "checkpoint_counter", "missing_probe"}:
            original = adapter.snapshot

            def changed_snapshot():
                saved, checkpoint = original()
                if change == "runtime_counter":
                    saved.total_tool_executions = 0
                elif change == "checkpoint_counter":
                    checkpoint["budget"]["model_requests"] = 0
                else:
                    checkpoint["records"] = []
                return saved, checkpoint

            adapter.snapshot = changed_snapshot
        elif change == "frozen_body":
            adapter.first_request["max_output_tokens"] += 1
        elif change == "approved_hash":
            adapter.expected_first_sha256 = "0" * 64
        else:
            kwargs["tools"].reverse()

    adapters = intercept(monkeypatch, corrupt)
    report = run(prepared, tmp_path)
    assert report["session"]["status"] == "FAILED"
    assert report["adapter_requests"] == adapters[0].sdk_entries == 0
    assert not (tmp_path / "run/request-01.json").exists()
    sentinel.with_options.assert_not_called()
    assert report["session"]["total_tool_executions"] == 1


@pytest.mark.parametrize("change", [
    "new_tool", "new_field", "removed_field", "duplicate", "not_list",
])
def test_dynamic_schema_cannot_expand_scope(prepared, tmp_path, monkeypatch, change):
    def corrupt(adapter, kwargs):
        tools = deepcopy(kwargs["tools"])
        if change == "new_tool":
            tools[0]["name"] = "execute_remediation"
        elif change == "new_field":
            tools[0]["parameters"]["properties"]["shell"] = {"type": "string"}
        elif change == "removed_field":
            tools[0]["parameters"]["properties"].pop("service")
        elif change == "duplicate":
            tools.append(deepcopy(tools[0]))
        else:
            tools = {}
        kwargs["tools"] = tools

    intercept(monkeypatch, corrupt)
    report = run(prepared, tmp_path)
    assert report["session"]["status"] == "FAILED"
    assert report["adapter_requests"] == 0


def test_loose_agent_schema_never_replaces_frozen_schema(prepared, tmp_path, monkeypatch):
    def loosen(adapter, kwargs):
        kwargs["tools"] = deepcopy(kwargs["tools"])
        for schema in kwargs["tools"]:
            schema["description"] = "arbitrary incoming description is not exported"
            params = schema["parameters"]
            params.pop("additionalProperties")
            params["properties"]["service"].pop("enum")

    intercept(monkeypatch, loosen)
    report = run(prepared, tmp_path)
    assert report["session"]["status"] == "COMPLETED"
    _, _, frozen = p65.read_prepared(prepared)
    assert request(tmp_path, 1)["body"]["tools"] == frozen["tools"]
    for schema in request(tmp_path, 2)["body"]["tools"]:
        assert schema["parameters"]["additionalProperties"] is False
        assert schema["parameters"]["properties"]["service"]["enum"] == ["order-service"]
        assert "冻结历史材料" in schema["description"]


@pytest.mark.parametrize("total,calls,expected", [
    (3, 1, "COMPLETED"), (3, 2, "COMPLETED"), (3, 3, "EXHAUSTED"),
    (4, 3, "COMPLETED"), (4, 4, "EXHAUSTED"), (1, 0, "COMPLETED"),
])
def test_synthetic_budget_is_isolated_and_enforced(
    prepared, tmp_path, total, calls, expected,
):
    plan, _, _ = p65.read_prepared(prepared)
    synthetic = tmp_path / "synthetic-budget"
    p65.prepare(plan["capture_dir"], synthetic, offline_tool_budget=total)
    first = response([
        metric(f"synthetic-{i}", metric=f"synthetic_metric_{i}", service="order-service")
        for i in range(calls)
    ], answer="[离线合成预算 Mock]" if not calls else "")
    report = p65.run_prepared(
        synthetic, tmp_path / "run", mock_responses=[
            first, response(answer="[离线合成预算 Mock] 只验证协议，不认可根因。"),
        ],
    )
    assert report["session"]["status"] == expected
    assert report["session"]["total_tool_executions"] == min(calls + 1, total)
    assert report["offline_only"] is True
    assert report["real_model_sdk_requests"] == 0
    assert report["forbidden_io_attempts"] == []
    if calls > total - 1:
        assert report["adapter_requests"] == 1
        assert not (tmp_path / "run/request-02.json").exists()
        assert len(report["checkpoint"]["pending"]) == calls - total + 1
    elif calls:
        body = request(tmp_path, 2)["body"]
        assert len([i for i in body["input"] if i.get("type") == "function_call_output"]) == calls
        assert bool(body["tools"]) is (calls + 1 < total)
        assert body["tool_choice"] == ("auto" if body["tools"] else "none")
    else:
        assert request(tmp_path, 1)["body"]["tools"] == []
    sdk = Mock()
    with pytest.raises(ValueError, match="synthetic_budget_offline_only"):
        p65.run_prepared(synthetic, tmp_path / "must-not-exist", live=True,
                         authorization=approval(prepared), sdk_factory=sdk)
    sdk.assert_not_called()
    assert not (tmp_path / "must-not-exist").exists()
    assert p65.read_prepared(prepared)[0]["max_tool_executions"] == 5


@pytest.fixture
def second_frozen():
    if not SECOND_SOURCE.exists():
        pytest.skip("operator sealed second response unavailable")
    return read_frozen(SECOND_SOURCE)


@pytest.mark.parametrize("total", [4, 5, 6])
def test_sealed_second_real_response_replays_exactly_with_synthetic_limits(
    tmp_path, second_frozen, total,
):
    prepared_dir = tmp_path / "prepared"
    p65.prepare(p65.DEFAULT_CAPTURE, prepared_dir, offline_tool_budget=total)
    report = p65.run_prepared(
        prepared_dir, tmp_path / "run", offline_first_response=second_frozen,
        mock_responses=[response(answer="[明确 Mock 后续响应] 只验证完整输出往返。")],
    )
    wire, sdk = second_frozen
    raw = json.loads(wire)
    calls = [i for i in raw["output"] if i["type"] == "function_call"]
    assert len(calls) == 4
    assert (tmp_path / "run/response-01.raw.txt").read_bytes() == wire
    assert json.loads((tmp_path / "run/response-01.sdk.json").read_bytes()) == sdk
    assert report["model_selected_calls"] == [i for i in sdk["output"] if i["type"] == "function_call"]
    assert report["session"]["total_tool_executions"] == min(5, total)
    assert report["real_model_sdk_requests"] == 0
    for field in ("real_service_requests", "business_route_requests", "remediation_executions"):
        assert report[field] == 0
    assert report["forbidden_io_attempts"] == []
    assert report["session"]["resume_count"] == 0
    if total == 4:
        assert report["session"]["status"] == "EXHAUSTED"
        assert report["adapter_requests"] == 1
        assert report["checkpoint"]["pending"][0]["call_id"] == calls[-1]["call_id"]
        outputs = [i for i in report["checkpoint"]["history"] if i.get("type") == "function_call_output"]
        assert len(outputs) == 3
        assert not any(r["name"] == "query_logs" for r in report["checkpoint"]["records"])
        assert not (tmp_path / "run/request-02.json").exists()
    else:
        assert report["session"]["status"] == "COMPLETED"
        body = request(tmp_path, 2)["body"]
        assert body["input"][1:1 + len(raw["output"])] == raw["output"]
        outputs = body["input"][1 + len(raw["output"]):]
        assert len(outputs) == 4
        for call, output in zip(calls, outputs, strict=True):
            assert output["call_id"] == call["call_id"]
            record = next(r for r in report["checkpoint"]["records"] if r["call_id"] == call["call_id"])
            assert json.loads(output["output"]) == record["result"]
            assert record["arguments"] == json.loads(call["arguments"])
        assert body["instructions"] == p65.budget_instructions(total - 5, 2)
        assert bool(body["tools"]) is (total > 5)
        assert body["tool_choice"] == ("auto" if total > 5 else "none")
        assert report["response_origins"][-1] == "scripted_mock_followup"
    assert read_frozen(SECOND_SOURCE) == second_frozen


def test_old_real_approval_cannot_authorize_new_first_body(tmp_path, second_frozen):
    prepared_dir = tmp_path / "prepared"
    p65.prepare(p65.DEFAULT_CAPTURE, prepared_dir)
    old = json.loads((SECOND_SOURCE / "authorization.json").read_bytes())
    plan, _, _ = p65.read_prepared(prepared_dir)
    assert old["first_request_sha256"] != plan["first_request_sha256"]
    sdk = Mock()
    with pytest.raises(ValueError, match="new_explicit_authorization"):
        p65.run_prepared(prepared_dir, tmp_path / "denied", live=True,
                         authorization=old, sdk_factory=sdk)
    sdk.assert_not_called()
    assert not (tmp_path / "denied").exists()


@pytest.mark.parametrize("round_number", [1, 2, 3])
@pytest.mark.parametrize("hazard", ["sensitive", "oversize"])
def test_every_outgoing_round_is_checked_before_sdk(
    prepared, tmp_path, monkeypatch, round_number, hazard,
):
    def corrupt(adapter, kwargs):
        if adapter.requests + 1 != round_number:
            return
        original = adapter._input

        def outgoing(checkpoint):
            return original(checkpoint) + [{
                "role": "user", "content": (
                    "Bearer synthetic-private-credential" if hazard == "sensitive"
                    else "x" * 131073
                ),
            }]

        adapter._input = outgoing

    intercept(monkeypatch, corrupt)
    report = run(prepared, tmp_path)
    assert report["session"]["status"] in {"FAILED", "EXHAUSTED"}
    assert report["adapter_requests"] == round_number - 1
    assert not (tmp_path / "run" / f"request-{round_number:02d}.json").exists()


def test_unadvertised_tool_at_zero_budget_is_rejected(prepared, tmp_path):
    calls = [metric(str(i), metric=f"synthetic-{i}", service="order-service") for i in range(4)]
    report = run(prepared, tmp_path, [response(calls), response([metric("unadvertised")])])
    assert report["session"]["status"] == "EXHAUSTED"
    assert report["session"]["total_tool_executions"] == 5
    assert report["adapter_requests"] == 2
    assert request(tmp_path, 2)["body"]["tools"] == []
    assert request(tmp_path, 2)["body"]["tool_choice"] == "none"
    completion = json.loads((tmp_path / "run/completion-02.json").read_bytes())
    assert completion["error"]["code"] == "p65_unadvertised_tool"
    assert not any(r["call_id"] == "unadvertised" for r in report["checkpoint"]["records"])


@pytest.mark.parametrize("disabled", ["zero_tools", "last_round"])
def test_agent_cannot_advertise_tools_when_disabled(
    prepared, tmp_path, monkeypatch, disabled,
):
    def corrupt(adapter, kwargs):
        target = 2 if disabled == "zero_tools" else 3
        if adapter.requests + 1 == target:
            assert kwargs["tools"] == []
            kwargs["tools"] = deepcopy(adapter.frozen_tools)

    intercept(monkeypatch, corrupt)
    if disabled == "zero_tools":
        calls = [metric(str(i), metric=f"synthetic-{i}", service="order-service") for i in range(4)]
        responses = [response(calls), response(answer="[Mock] 不应到达")]
    else:
        responses = p65.mock_plan()
    report = run(prepared, tmp_path, responses)
    target = 2 if disabled == "zero_tools" else 3
    assert report["adapter_requests"] == target - 1
    assert not (tmp_path / "run" / f"request-{target:02d}.json").exists()
    assert report["session"]["status"] in {"FAILED", "EXHAUSTED"}
