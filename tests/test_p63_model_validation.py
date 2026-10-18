"""P6.3 offline boundary checks; injected SDKs never send model requests.

The historical-case tests use operator-local sealed originals when available.
They skip when that ignored capture is absent; no real capture is a Git fixture.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import httpx2
import openai
from openai.types.responses import ResponseUsage
import pytest
import requests

from runtime_support import call, response
from scripts import p63_evidence as evidence
from scripts import p63_model_validation as validation


CAPTURE = Path(__file__).resolve().parents[1] / "data/p62b-first-error-20261018-01"


def read_json(root, name):
    return json.loads((root / name).read_bytes())


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(root):
    return {
        item.relative_to(root).as_posix(): (item.read_bytes(), item.stat().st_mtime_ns)
        for item in root.rglob("*") if item.is_file()
    }


def artifact_text(root):
    return "\n".join(
        item.read_text(encoding="utf-8")
        for item in root.rglob("*")
        if item.is_file() and item.suffix in {".json", ".txt"}
    )


def reseal(root):
    """Update only the outer manifest of a deliberately modified local copy."""
    write_json(root / "sha256.json", {
        item.relative_to(root).as_posix(): sha(item)
        for item in sorted(root.rglob("*"))
        if item.is_file() and item != root / "sha256.json"
    })


def authorization(summary, **changes):
    value = {
        "purpose": "p63_historical_evidence_analysis",
        "explicitly_authorized": True,
        "request_sha256": summary["request_sha256"],
        "capture_manifest_sha256": summary["capture_manifest_sha256"],
        "model": "deepseek-flash",
        "max_requests": 1,
        "max_retries": 0,
        "authorized_at": datetime.now(timezone.utc).isoformat(),
        "authorization_note": "Offline test fixture; fake SDK only, no human paid approval.",
    }
    value.update(changes)
    return value


def claim_answer(
    context,
    *,
    text="离线夹具结论，语义仍需人工核查。",
    references=True,
):
    return json.dumps({"claims": [{
        "kind": "limitation",
        "text": text,
        "observation_ids": (
            [context["evidence_index"]["sources"][0]["observation_id"]]
            if references else []
        ),
    }]}, ensure_ascii=False)


def fake_sdk(
    result,
    *,
    failure=None,
    raw_text=None,
):
    sdk = Mock()
    sdk.with_options.return_value = sdk
    raw = SimpleNamespace(
        text=raw_text if raw_text is not None else result.model_dump_json(),
        parse=lambda: result,
    )
    sdk.responses.with_raw_response.create.return_value = raw
    sdk.responses.with_raw_response.create.side_effect = failure
    return sdk


@pytest.fixture(autouse=True)
def prohibit_external_actions(monkeypatch):
    """Even the simulated paid branch cannot reach business or provider I/O."""
    def forbidden(*args, **kwargs):
        pytest.fail("P6.3 tests must not collect evidence, call a model or execute repair")

    from remediation.executor import ActionExecutor
    from tools.tool_manager import ToolManager

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(ToolManager, "run_tool", forbidden)
    monkeypatch.setattr(ActionExecutor, "execute", forbidden)


@pytest.fixture
def capture(tmp_path):
    if not (CAPTURE / "sha256.json").is_file():
        pytest.skip("operator-local frozen P6.2b originals are not available")
    root = tmp_path / "capture"
    shutil.copytree(CAPTURE, root)
    return root


@pytest.fixture
def prepared(capture, tmp_path):
    root = tmp_path / "prepared"
    summary = validation.prepare(capture, root)
    return root, summary, read_json(root, "context.json")


def assert_no_effects(summary):
    assert summary["business_requests"] == 0
    assert summary["remediation_executions"] == 0
    assert summary["sdk_retries"] == 0
    assert summary["application_retries"] == 0
    assert summary["manual_review"] == "required"
    assert summary["semantic_acceptance"] == "pending"


def test_prepare_produces_exact_reviewable_request_and_preserves_originals(
    capture,
    tmp_path,
):
    before = snapshot(capture)
    first, second = tmp_path / "first", tmp_path / "second"
    summary = validation.prepare(capture, first)
    validation.prepare(capture, second)
    assert summary["scope"] == "real_model_historical_evidence_analysis"
    assert summary["status"] == "authorization_required"
    assert summary["real_model_sdk_requests"] == 0
    assert_no_effects(summary)
    assert summary["request_sha256"] == sha(first / "request.json")
    assert (first / "request.sha256").read_text().strip() == summary["request_sha256"]
    for name, digest in read_json(first, "sha256.json").items():
        assert sha(first / name) == digest
    body = read_json(first, "request.json")
    assert body["model"] == "deepseek-flash"
    assert body["max_output_tokens"] == 2048
    assert body["tools"] == [] and body["tool_choice"] == "none"
    assert body["reasoning"] == {"effort": "none"}
    assert json.loads(body["input"][0]["content"]) == read_json(first, "context.json")
    # Preparing again does not renew the historical context or outgoing payload.
    assert (first / "context.json").read_bytes() == (second / "context.json").read_bytes()
    assert (first / "request.json").read_bytes() == (second / "request.json").read_bytes()
    assert snapshot(capture) == before


def test_context_preserves_fault_collection_no_data_zero_and_alert_scope(capture):
    context = evidence.build_context(capture)
    fault_end = datetime.fromisoformat(context["fault_window"]["end"])
    collection_start = datetime.fromisoformat(context["collection_window"]["start"])
    assert collection_start > fault_end
    delay = context["collection_window"]["entry_delay_after_last_fault_seconds"]
    assert delay == pytest.approx(84.026778)
    sources = context["evidence_index"]["sources"]
    assert all(item["historical_only"] is True for item in sources)
    http = [item for item in sources if item["name"] == "captured_http_response"]
    assert len(http) == 5
    assert all(item["value"]["route"] == "/error" for item in http)
    assert all(item["value"]["http_status"] == 500 for item in http)
    metrics = {
        item["value"]["metric"]: item
        for item in sources if item["name"] == "query_metrics"
    }
    for name in ("http_5xx_rate", "http_latency_p95_seconds"):
        assert metrics[name]["status"] == "no_data"
        assert metrics[name]["data_missing"] is True
        assert "value" not in metrics[name]["value"]
    assert metrics["http_request_rate"]["status"] == "ok"
    assert metrics["http_request_rate"]["value"]["value"] == 0.0
    assert metrics["http_request_rate"]["data_missing"] is False
    assert metrics["http_requests_total"]["value"]["value"] == 13.0
    assert metrics["http_requests_total"]["value"]["scope"] == (
        "service_total_not_independent_500_counter"
    )
    alert = next(
        item["value"] for item in sources
        if item["name"] == "captured_prometheus_alert_snapshot"
    )
    assert alert["state_at_snapshot"] == "inactive"
    assert alert["coverage"] == "single_late_snapshot_not_entire_fault_window"
    assert alert["non_case_firing_present"] is True
    assert all(item["scope"] == "rule_all_series_not_order_service" for item in alert["rules"])
    runtime = next(
        item["value"] for item in sources
        if item["name"] == "historical_mock_runtime_replay"
    )
    assert runtime["alert_input_origin"] == "synthetic_alert_input"
    assert runtime["model_origin"] == "scripted_mock_sdk_not_real_deepseek"
    assert runtime["business_recovery_verified"] is False
    assert runtime["real_model_sdk_requests"] == 0
    version = next(item for item in sources if item["name"] == "get_running_version")
    assert version["status"] == "no_data"
    assert version["value"]["commit_sha"] is None


@pytest.mark.parametrize("name", [
    "experiment/traffic/http-002.body",
    "observations/http-003.body",
    "runtime-replay/report.json",
])
def test_tampered_originals_are_rejected_before_request_preparation(
    capture,
    tmp_path,
    name,
):
    path = capture / name
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="manifest|hash"):
        validation.prepare(capture, tmp_path / "must-not-prepare")
    assert not (tmp_path / "must-not-prepare").exists()


def test_unknown_sensitive_source_fields_are_not_sent(capture, tmp_path):
    original = read_json(capture, "authorization.json")
    original.update({
        "api_key": "never-send-source-key",
        "environment": {"DEEPSEEK_API_KEY": "never-send-env-value"},
        "database_rows": [{"private_order": "never-send-business-row"}],
        "unrelated_note": "never-send-unknown-field",
    })
    write_json(capture / "authorization.json", original)
    reseal(capture)
    root = tmp_path / "prepared"
    validation.prepare(capture, root)
    serialized = (root / "request.json").read_text(encoding="utf-8")
    assert all(marker not in serialized for marker in (
        "never-send-source-key", "never-send-env-value",
        "never-send-business-row", "never-send-unknown-field", "DEEPSEEK_API_KEY",
    ))


@pytest.mark.parametrize("value", [
    {"api_key": "fixture"},
    {"nested": [{"password": "fixture"}]},
    {"environment": {}},
    {"database_rows": []},
    {"access_token": "fixture"},
    {"refresh_token": "fixture"},
    {"note": "Bearer fixture-secret"},
    {"note": "sk-1234567890123456"},
])
def test_sensitive_projected_fields_or_values_fail_closed(value):
    with pytest.raises(ValueError, match="p63_sensitive"):
        validation.request_body(value)


def test_authorization_gate_precedes_credentials_and_sdk(
    tmp_path,
    monkeypatch,
):
    from scripts import preflight_real_model

    # The paid gate remains testable even on hosts lacking operator-local data.
    root = tmp_path / "prepared-metadata"
    root.mkdir()
    write_json(root / "summary.json", {
        "capture_dir": str(tmp_path / "unavailable-historical-capture"),
    })
    credentials = Mock(side_effect=AssertionError("must not read credentials"))
    factory = Mock(side_effect=AssertionError("must not construct SDK"))
    monkeypatch.setattr(preflight_real_model, "credentials", credentials)
    result = validation.run_prepared(root, tmp_path / "blocked", live=True, sdk_factory=factory)
    assert result["status"] == "authorization_required"
    assert result["real_model_sdk_requests"] == 0
    assert_no_effects(result)
    credentials.assert_not_called()
    factory.assert_not_called()


def test_run_output_cannot_modify_the_bound_capture_copy(prepared, capture):
    root, _, _ = prepared
    before = snapshot(capture)
    output = capture / "forbidden-output"
    with pytest.raises(ValueError, match="p63_run_overlaps_input"):
        validation.run_prepared(root, output)
    assert not output.exists()
    assert snapshot(capture) == before


def test_production_entry_cannot_select_a_fresh_consumption_ledger(prepared, tmp_path):
    root, summary, _ = prepared
    output = tmp_path / "forbidden-ledger-run"
    with pytest.raises(ValueError, match="p63_custom_ledger_requires_injected_test_sdk"):
        validation.run_prepared(
            root, output, live=True, authorization=authorization(summary),
            consumption_dir=tmp_path / "fresh-ledger",
        )
    assert not output.exists()


@pytest.mark.parametrize("changes", [
    {"purpose": "p53c_real_model_preflight"},
    {"explicitly_authorized": False},
    {"request_sha256": "0" * 64},
    {"capture_manifest_sha256": "0" * 64},
    {"max_requests": 2},
    {"max_retries": 1},
    {"max_requests": True},
    {"authorized_at": "2026-10-18T12:00:00"},
    {"authorization_note": ""},
])
def test_wrong_old_or_incomplete_authorization_never_constructs_sdk(
    prepared,
    tmp_path,
    monkeypatch,
    changes,
):
    from scripts import preflight_real_model

    root, summary, _ = prepared
    credentials = Mock(side_effect=AssertionError("must not read credentials"))
    factory = Mock(side_effect=AssertionError("must not construct SDK"))
    monkeypatch.setattr(preflight_real_model, "credentials", credentials)
    result = validation.run_prepared(
        root, tmp_path / "rejected", live=True,
        authorization=authorization(summary, **changes), sdk_factory=factory,
    )
    assert result["status"] == "failed"
    assert result["real_model_sdk_requests"] == 0
    credentials.assert_not_called()
    factory.assert_not_called()


@pytest.mark.parametrize("tamper", [
    "request_bytes", "resealed_context", "source_after_preparation",
])
def test_prepared_and_source_tampering_fail_before_sdk(
    prepared,
    capture,
    tmp_path,
    monkeypatch,
    tamper,
):
    from scripts import preflight_real_model

    root, summary, _ = prepared
    if tamper == "request_bytes":
        path = root / "request.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif tamper == "resealed_context":
        value = read_json(root, "context.json")
        value["known_gaps"] = []
        write_json(root / "context.json", value)
        reseal(root)
    else:
        path = capture / "experiment/traffic/http-002.body"
        path.write_bytes(path.read_bytes() + b" ")
    credentials = Mock(side_effect=AssertionError("must not read credentials"))
    factory = Mock(side_effect=AssertionError("must not construct SDK"))
    monkeypatch.setattr(preflight_real_model, "credentials", credentials)
    result = validation.run_prepared(
        root, tmp_path / "tampered", live=True,
        authorization=authorization(summary), sdk_factory=factory,
    )
    assert result["status"] == "failed"
    assert result["real_model_sdk_requests"] == 0
    credentials.assert_not_called()
    factory.assert_not_called()


def test_default_mock_keeps_manual_review_and_historical_sources(
    prepared,
    capture,
    tmp_path,
):
    root, _, _ = prepared
    before = snapshot(capture)
    result = validation.run_prepared(root, tmp_path / "mock")
    assert result["status"] == "mock_completed_manual_review_required"
    assert result["real_model_sdk_requests"] == 0
    assert_no_effects(result)
    review = read_json(tmp_path / "mock", "claim-review.json")
    assert review["contract_check"]["reference_source_check"] == (
        "trusted_index_internal_integrity_only"
    )
    assert review["contract_check"]["strict_acceptance"] == "pending"
    assert review["claims"][0]["review_status"] == "pending_human_review"
    assert "Mock" in (tmp_path / "mock/answer-original.txt").read_text(encoding="utf-8")
    assert snapshot(capture) == before


@pytest.mark.parametrize("mode", [
    "incomplete", "missing_references", "tool_call", "empty", "wrong_model",
])
def test_bad_mock_response_retains_original_failure_and_never_dispatches_tools(
    prepared,
    tmp_path,
    mode,
):
    root, _, context = prepared
    answer = claim_answer(context, references=mode != "missing_references")
    result_response = response(
        answer="" if mode == "empty" else answer,
        status="incomplete" if mode == "incomplete" else "completed",
        calls=[call("query_metrics")] if mode == "tool_call" else (),
    ).model_copy(update={
        "model": "unexpected-model" if mode == "wrong_model" else "deepseek-flash",
    })
    output = tmp_path / "bad-response"
    result = validation.run_prepared(root, output, mock_response=result_response)
    assert result["status"] == "failed"
    assert result["real_model_sdk_requests"] == 0
    assert_no_effects(result)
    assert read_json(output, "response.sdk.json")["status"] == result_response.status
    answer_original = (output / "answer-original.txt").read_text(encoding="utf-8")
    assert answer_original == result_response.output_text
    review = read_json(output, "claim-review.json")
    assert review["contract_check"]["strict_acceptance"] == "failed"
    assert review["contract_check"]["errors"]
    if mode == "missing_references":
        assert "invalid_claim_references" in review["contract_check"]["errors"]
    elif mode in {"incomplete", "tool_call", "wrong_model"}:
        assert "response_incomplete_error_or_unexpected_output" in (
            review["contract_check"]["errors"]
        )
    else:
        assert "invalid_json" in review["contract_check"]["errors"]


def test_injected_sdk_is_one_bounded_request_and_copied_package_cannot_replay(
    prepared,
    tmp_path,
    monkeypatch,
):
    from scripts import preflight_real_model

    root, summary, context = prepared
    result_response = response(answer=claim_answer(context)).model_copy(update={
        "model": "deepseek-flash",
        "usage": ResponseUsage.model_validate({
            "input_tokens": 123,
            "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 0},
            "output_tokens": 45, "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 168,
        }),
    })
    sdk = fake_sdk(result_response)
    factory = Mock(return_value=sdk)
    monkeypatch.setattr(
        preflight_real_model, "credentials",
        lambda: ("offline-test-key", "test-fixture"),
    )
    ledger = tmp_path / "consumption"
    output = tmp_path / "injected-sdk"
    approval = authorization(summary)
    result = validation.run_prepared(
        root, output, live=True, authorization=approval,
        sdk_factory=factory, consumption_dir=ledger,
    )
    assert result["status"] == "model_completed_manual_review_required"
    assert result["real_model_sdk_requests"] == 1
    assert_no_effects(result)
    sdk.responses.with_raw_response.create.assert_called_once_with(
        **read_json(root, "request.json"),
    )
    sdk.with_options.assert_called_once_with(max_retries=0, timeout=30)
    kwargs = factory.call_args.kwargs
    assert kwargs["max_retries"] == 0 and kwargs["timeout"] == 30
    assert kwargs["base_url"] == "https://api.deepseek.com"
    assert read_json(output, "response.sdk.json") == result_response.model_dump(mode="json")
    assert result["usage"] == result_response.usage.model_dump(mode="json")
    assert datetime.fromisoformat(result["request_started_at"]).tzinfo is not None
    assert datetime.fromisoformat(result["request_finished_at"]) >= (
        datetime.fromisoformat(result["request_started_at"])
    )
    assert read_json(output, "claim-review.json")["claims"][0]["semantic_support"] == "pending"
    assert read_json(output, "summary.json")["request_sha256"] == summary["request_sha256"]
    sdk.close.assert_called_once()
    clone = tmp_path / "copied-package"
    shutil.copytree(root, clone)
    replay_factory = Mock(side_effect=AssertionError("consumed authorization cannot start SDK"))
    replay = validation.run_prepared(
        clone, tmp_path / "replay", live=True, authorization=approval,
        sdk_factory=replay_factory, consumption_dir=ledger,
    )
    assert replay["status"] == "failed"
    assert replay["real_model_sdk_requests"] == 0
    replay_factory.assert_not_called()
    kwargs["http_client"].close()


def test_sdk_exception_is_retained_without_retry_or_sensitive_error_text(
    prepared,
    tmp_path,
    monkeypatch,
):
    from scripts import preflight_real_model

    root, summary, context = prepared
    sdk = fake_sdk(
        response(answer=claim_answer(context)),
        failure=RuntimeError("never-save-provider-message offline-test-key"),
    )
    factory = Mock(return_value=sdk)
    monkeypatch.setattr(
        preflight_real_model, "credentials",
        lambda: ("offline-test-key", "test-fixture"),
    )
    output = tmp_path / "sdk-error"
    result = validation.run_prepared(
        root, output, live=True, authorization=authorization(summary),
        sdk_factory=factory, consumption_dir=tmp_path / "consumption",
    )
    assert result["status"] == "failed"
    assert result["real_model_sdk_requests"] == 1
    assert_no_effects(result)
    sdk.responses.with_raw_response.create.assert_called_once()
    assert result["error"]["category"] == "RuntimeError"
    assert "never-save-provider-message" not in artifact_text(output)
    assert "offline-test-key" not in artifact_text(output)
    sdk.close.assert_called_once()
    factory.call_args.kwargs["http_client"].close()


def test_http_api_error_preserves_sanitized_original_body_and_hash(
    prepared,
    tmp_path,
    monkeypatch,
):
    from scripts import preflight_real_model

    root, summary, context = prepared
    wire = '{"error":{"message":"provider failure offline-test-key"}}'
    error = openai.InternalServerError(
        "never-save-exception-message",
        response=httpx2.Response(
            500, text=wire,
            request=httpx2.Request("POST", "https://provider.invalid/responses"),
        ),
        body={"error": {"message": "provider failure offline-test-key"}},
    )
    sdk = fake_sdk(response(answer=claim_answer(context)), failure=error)
    factory = Mock(return_value=sdk)
    monkeypatch.setattr(
        preflight_real_model, "credentials",
        lambda: ("offline-test-key", "test-fixture"),
    )
    output = tmp_path / "http-api-error"
    result = validation.run_prepared(
        root, output, live=True, authorization=authorization(summary),
        sdk_factory=factory, consumption_dir=tmp_path / "consumption",
    )
    assert result["status"] == "failed"
    assert result["error"]["http_status"] == 500
    assert result["real_model_sdk_requests"] == 1
    assert_no_effects(result)
    sdk.responses.with_raw_response.create.assert_called_once()
    assert (output / "error-response.raw.txt").read_text(encoding="utf-8") == wire.replace(
        "offline-test-key", "[credential redacted]",
    )
    assert read_json(output, "error-response-integrity.json") == {
        "wire_text_sha256": hashlib.sha256(wire.encode("utf-8")).hexdigest(),
        "credential_redacted": True,
    }
    assert "never-save-exception-message" not in artifact_text(output)
    assert "offline-test-key" not in artifact_text(output)
    factory.call_args.kwargs["http_client"].close()
