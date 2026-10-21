"""Third-experiment admission and sealed response replay; never paid traffic."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from scripts import p65_readonly_investigation as p65
from scripts.p65_model_boundary import encoded, sha, validate_approval
from tests.test_p65_dynamic_budget import SECOND_SOURCE, second_frozen
from tests.test_p65_readonly_investigation import approval, prepared, response


def reseal_plan(prepared, change):
    """A self-consistent file seal cannot override the Python experiment policy."""
    path = prepared / "plan.json"
    plan = json.loads(path.read_bytes())
    change(plan)
    path.write_bytes(encoded(plan))
    manifest_path = prepared / "sha256.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["plan.json"] = sha(path.read_bytes())
    manifest_path.write_bytes(encoded(manifest))


def test_formal_five_tool_revision_requires_its_own_approval(prepared):
    plan, _, body = p65.read_prepared(prepared)
    assert plan["request_version"] == "p65_dynamic_runtime_v3_tools5"
    assert plan["offline_only"] is False
    assert plan["max_tool_executions"] == 5
    assert plan["max_model_requests"] == 3
    assert plan["expected_initial_reservation"] == {
        "model_requests": 1, "tool_executions": 1,
    }
    assert body["instructions"] == p65.budget_instructions(4, 3)
    assert plan["request_version"] in body["instructions"]
    assert len(plan["sdk_source_sha256"]) == 8
    record = approval(prepared)  # Synthetic in-memory test data, never human consent.
    validate_approval(record, plan)
    for field, value in (
        ("request_version", "p65_dynamic_runtime_v2"),
        ("max_tool_executions", 4),
    ):
        wrong = deepcopy(record)
        wrong[field] = value
        with pytest.raises(ValueError, match="new_explicit_authorization"):
            validate_approval(wrong, plan)


@pytest.mark.parametrize("field,value", [
    ("request_version", "p65_dynamic_runtime_v2"),
    ("max_tool_executions", 4), ("max_tool_executions", 6),
    ("max_tool_executions", True), ("max_model_requests", 4),
    ("max_model_requests", True), ("max_attempts", 2),
    ("max_incidents", 2), ("max_worker_run_once", 2),
    ("max_resumes", 1), ("fallback", 1), ("max_human_requests", 1),
    ("sdk_retries", 1), ("application_retries", 1),
    ("timeout_seconds", 60), ("max_output_tokens", 4096),
    ("tools", ["query_logs"]), ("historical_only", False),
    ("sdk_versions", {}), ("sdk_source_sha256", {}),
    ("first_request_sha256", "0" * 64),
])
def test_resealed_scope_tampering_stops_before_credentials_or_sdk(
    prepared,
    tmp_path,
    monkeypatch,
    field,
    value,
):
    record = approval(prepared)
    reseal_plan(prepared, lambda plan: plan.update({field: value}))
    credential = Mock(side_effect=AssertionError("must not read a key"))
    sdk = Mock(side_effect=AssertionError("must not construct SDK"))
    monkeypatch.setattr("scripts.preflight_real_model.credentials", credential)
    with pytest.raises(ValueError, match="scope_or_code_changed"):
        p65.run_prepared(
            prepared,
            tmp_path / "denied",
            live=True,
            authorization=record,
            sdk_factory=sdk,
        )
    credential.assert_not_called()
    sdk.assert_not_called()
    assert not (tmp_path / "denied").exists()


def test_explicit_synthetic_five_remains_offline_only(
    prepared,
    tmp_path,
    monkeypatch,
):
    plan, _, _ = p65.read_prepared(prepared)
    synthetic = tmp_path / "synthetic"
    p65.prepare(plan["capture_dir"], synthetic, offline_tool_budget=5)
    assert p65.read_prepared(synthetic)[0]["offline_only"] is True
    sdk = Mock()
    credential = Mock()
    monkeypatch.setattr("scripts.preflight_real_model.credentials", credential)
    with pytest.raises(ValueError, match="synthetic_budget_offline_only"):
        p65.run_prepared(
            synthetic,
            tmp_path / "denied",
            live=True,
            authorization=approval(synthetic),
            sdk_factory=sdk,
        )
    credential.assert_not_called()
    sdk.assert_not_called()
    assert not (tmp_path / "denied").exists()


def test_sealed_second_response_reaches_zero_tool_followup_with_committed_pairs(
    tmp_path,
    second_frozen,
):
    from investigation.checkpoint import valid_chain
    from investigation.store import RuntimeStore

    prepared_dir = tmp_path / "prepared"
    plan = p65.prepare(p65.DEFAULT_CAPTURE, prepared_dir)
    root = tmp_path / "run"
    report = p65.run_prepared(
        prepared_dir,
        root,
        offline_first_response=second_frozen,
        mock_responses=[response(answer="[Mock 后续回答] 真实第二轮尚未验证。")],
    )
    assert plan["offline_only"] is False  # Formal plan; this execution is replay only.
    assert report["session"]["status"] == "COMPLETED"
    assert report["session"]["total_tool_executions"] == 5
    assert report["session"]["total_model_requests"] == 2
    assert report["session"]["attempt_count"] == 1
    assert report["session"]["resume_count"] == 0
    assert report["result_trust"]["trusted"] is False
    assert report["result_trust"]["semantic_status"] == "pending_review"
    assert report["response_origins"] == [
        "real_provider_first_response_offline_replay", "scripted_mock_followup",
    ]
    assert report["real_model_sdk_requests"] == 0
    assert report["real_service_requests"] == report["remediation_executions"] == 0
    assert report["forbidden_io_attempts"] == []
    assert (root / "response-01.raw.txt").read_bytes() == second_frozen[0]
    assert json.loads((root / "response-01.sdk.json").read_bytes()) == second_frozen[1]
    sent = json.loads((root / "request-02.json").read_bytes())
    body = sent["body"]
    assert body["tools"] == []
    assert body["tool_choice"] == "none"
    assert sent["runtime_budget"]["remaining_tools"] == 0
    assert body["instructions"] == p65.budget_instructions(0, 2)
    assert valid_chain(body["input"])
    raw = json.loads(second_frozen[0])
    assert body["input"][1:1 + len(raw["output"])] == raw["output"]
    calls = [i for i in raw["output"] if i["type"] == "function_call"]
    outputs = body["input"][1 + len(raw["output"]):]
    assert len(calls) == len(outputs) == 4
    boundaries = json.loads((root / "worker-boundaries.json").read_bytes())
    before_second = next(
        b for b in boundaries if b["phase"] == "model_before_io"
        and b["session"]["total_model_requests"] == 2
    )
    cp = before_second["checkpoint"]
    assert cp["pending"] == []
    assert sent["checkpoint_sha256"] == sha(encoded(cp))
    for call, output in zip(calls, outputs, strict=True):
        assert output["call_id"] == call["call_id"]
        assert output in cp["history"]
        record = next(r for r in cp["records"] if r["call_id"] == call["call_id"])
        assert record["arguments"] == json.loads(call["arguments"])
        assert json.loads(output["output"]) == record["result"]
    logs = next(r for r in cp["records"] if r["name"] == "query_logs")
    _, package, frozen = p65.read_prepared(prepared_dir)
    assert logs["result"] == package["observations"]["query_logs"]["result"]
    assert logs["result"]["historical_only"] is True
    assert logs["result"]["use_for_current_state"] is False
    first = json.loads((root / "request-01.json").read_bytes())
    assert first["body"] == frozen
    assert first["sha256"] == plan["first_request_sha256"]
    reopened = RuntimeStore(root / "runtime.sqlite3", read_only=True)
    saved = reopened.get(report["session"]["session_id"])
    assert reopened.load_checkpoint(saved) == report["checkpoint"]
    assert "[Mock" in (root / "answer-original.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("relative", [
    "data/p65-prepared-20261019-01",
    "data/p65-dynamic-budget-offline-20261019-01/v2-prepared-4",
    "data/p65-dynamic-budget-offline-20261019-01/v2-prepared-5",
])
def test_old_formal_and_synthetic_plans_cannot_enter_new_revision(
    tmp_path,
    relative,
):
    old = p65.ROOT / relative
    if not old.exists():
        pytest.skip("operator historical plan unavailable")
    before = {p.name: p.read_bytes() for p in old.iterdir()}
    sdk = Mock()
    authorization = json.loads((SECOND_SOURCE / "authorization.json").read_bytes())
    with pytest.raises(ValueError, match="scope_or_code_changed"):
        p65.run_prepared(
            old,
            tmp_path / "denied",
            live=True,
            authorization=authorization,
            sdk_factory=sdk,
        )
    sdk.assert_not_called()
    assert {p.name: p.read_bytes() for p in old.iterdir()} == before
    assert not (tmp_path / "denied").exists()


def test_installed_sdk_transmits_four_committed_outputs(
    tmp_path,
    monkeypatch,
    second_frozen,
):
    """Installed SDK + in-memory transport, synthetic consent/key/ledger only."""
    import httpx2
    from openai import OpenAI

    prepared_dir = tmp_path / "prepared"
    p65.prepare(p65.DEFAULT_CAPTURE, prepared_dir)
    bodies = []
    final = response(answer="[Mock SDK 后轮] 真实 DeepSeek 接收行为尚未验证。")

    def transport(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx2.Response(
                200,
                content=second_frozen[0],
                headers={"content-type": "application/json"},
            )
        return httpx2.Response(200, json=final.model_dump(mode="json"))

    def factory(**kwargs):
        hooks = kwargs["http_client"].event_hooks
        kwargs["http_client"].close()
        kwargs["http_client"] = httpx2.Client(
            transport=httpx2.MockTransport(transport),
            event_hooks=hooks,
        )
        return OpenAI(**kwargs)

    monkeypatch.setattr(
        "scripts.preflight_real_model.credentials",
        lambda: ("synthetic-third-experiment-key", "injected_unit_test"),
    )
    root = tmp_path / "run"
    report = p65.run_prepared(
        prepared_dir,
        root,
        live=True,
        authorization=approval(prepared_dir),
        sdk_factory=factory,
        consumption_dir=tmp_path / "synthetic-consumption",
    )
    assert report["session"]["status"] == "COMPLETED"
    assert len(bodies) == 2
    for number, body in enumerate(bodies, 1):
        artifact = json.loads((root / f"request-{number:02d}.json").read_bytes())
        assert body == artifact["body"]
        # The approved hash binds encoded(adapter body), not SDK wire key order.
        assert artifact["sha256"] == sha(encoded(artifact["body"]))
    assert bodies[0] == p65.read_prepared(prepared_dir)[2]
    assert bodies[1]["tools"] == []
    assert bodies[1]["tool_choice"] == "none"
    outputs = [i for i in bodies[1]["input"] if i.get("type") == "function_call_output"]
    assert len(outputs) == 4
    boundaries = json.loads((root / "worker-boundaries.json").read_bytes())
    cp = next(
        b["checkpoint"] for b in boundaries if b["phase"] == "model_before_io"
        and b["session"]["total_model_requests"] == 2
    )
    assert all(output in cp["history"] for output in outputs)
    assert report["forbidden_io_attempts"] == []
    assert report["result_trust"]["trusted"] is False
    assert report["result_trust"]["semantic_status"] == "pending_review"
