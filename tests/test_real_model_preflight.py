"""P5.3c.2 safety and persistence checks, without a real model request."""
import importlib
import json
import logging
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import httpx2
import openai
import pytest

from investigation.evidence_policy import effective_records
from investigation.metric_analysis import analyze_metric_evidence
from investigation.models import Checkpoint
from runtime_support import call, response
from test_metric_analysis import AT, COLLECTED, LATER, aligned, instant, record
from test_worker_metric_analysis_growth import analyses, offline


@pytest.fixture(autouse=True)
def require_offline(offline):
    """Both the default and injected SDK paths must remain offline in tests."""


@pytest.fixture
def preflight():
    return importlib.import_module("scripts.preflight_real_model")


def read_json(root, name):
    return json.loads((root / name).read_text(encoding="utf-8"))


def artifact_text(root):
    return "\n".join(
        item.read_text(encoding="utf-8")
        for item in root.rglob("*")
        if item.is_file() and item.suffix in {".json", ".txt", ".log"}
    )


def fake_sdk(
    result=None,
    failure=None,
    *,
    raw_text=None,
):
    """Emulate the SDK raw-response boundary; no HTTP transport exists here."""
    result = result or response()
    sdk = Mock()
    sdk.with_options.return_value = sdk
    raw = SimpleNamespace(
        text=raw_text or result.model_dump_json(),
        parse=lambda: result,
    )
    sdk.responses.with_raw_response.create.side_effect = failure
    sdk.responses.with_raw_response.create.return_value = raw
    return sdk


def sdk_error(status):
    request = httpx2.Request("POST", "https://provider.invalid/responses")
    error_classes = {
        401: openai.AuthenticationError,
        402: openai.APIStatusError,
        403: openai.PermissionDeniedError,
        429: openai.RateLimitError,
        500: openai.InternalServerError,
    }
    return error_classes[status](
        "untrusted-provider-detail-never-save",
        response=httpx2.Response(status, request=request),
        body={"error": {"message": "untrusted-provider-detail-never-save"}},
    )


def test_import_is_inert_and_uses_no_project_runtime():
    code = """
import json
import sys
import scripts.preflight_real_model
forbidden = [
    name for name in (
        'config', 'agent.agent', 'investigation.worker', 'incident_store',
        'main', 'monitor.incident_monitor', 'investigation.scheduler',
    )
    if name in sys.modules
]
print(json.dumps(forbidden))
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert json.loads(completed.stdout) == []


def test_preflight_limits_are_fixed_and_separate_from_production(preflight):
    assert preflight.MAX_MODEL_REQUESTS == 1
    assert preflight.MAX_TOOL_EXECUTIONS == 1
    assert preflight.MAX_OUTPUT_TOKENS == 2048
    assert preflight.MAX_REQUEST_BYTES == 98304
    assert preflight.MODEL_TIMEOUT_SECONDS == 30
    assert preflight.WALL_TIMEOUT_SECONDS == 90


def test_first_case_retains_p53c1_snapshot_and_misalignment_sources(preflight):
    case = preflight.build_case()
    records = case["records"]
    assert len(records) == 4
    expected = [
        aligned("fixture-t1"),
        aligned("fixture-t2", LATER, (0.0, None, 1.0)),
        instant("fixture-error", "http_5xx_rate", 1.0, AT),
        instant("fixture-rate", "http_request_rate", 0.0, LATER),
    ]
    assert [item["result"] for item in records] == [
        item.result for item in expected
    ]
    assert len({item["observation_id"] for item in records}) == 4
    for item, source in zip(records, expected):
        saved = record(source)
        assert item["arguments"] == saved["arguments"]
        assert item["collected_at"] == COLLECTED.isoformat()
    assert case["expected"]
    assert effective_records(records, None, COLLECTED, "order-service") == []
    report = analyze_metric_evidence(records, service="order-service").to_dict()
    assert report["use_for_current_state"] is False
    limitations = {item["code"] for item in report["limitations"]}
    assert {"time_not_aligned", "missing_metric_sample"} <= limitations
    assert not report["hypotheses_to_verify"]
    observed = {
        (item["observation_id"], item["metric"]): item["samples"][0]["value"]
        for item in report["confirmed_observations"]
    }
    second_id = records[1]["observation_id"]
    assert observed[second_id, "http_request_rate"] == 0.0
    assert (second_id, "http_5xx_rate") not in observed
    assert all(
        item["use_for_current_state"] is False
        for item in report["confirmed_observations"]
    )


def test_default_run_uses_real_runtime_but_no_sdk_or_external_tool(
    preflight,
    tmp_path,
    monkeypatch,
):
    secret = "preflight-canary-key-not-a-real-credential"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    constructor = Mock(side_effect=AssertionError("sdk_must_not_be_constructed"))
    monkeypatch.setattr(openai, "OpenAI", constructor)
    root = tmp_path / "offline"
    summary = preflight.run_case(root)
    constructor.assert_not_called()
    assert summary == read_json(root, "summary.json")
    assert summary["session_status"] == "COMPLETED"
    assert summary["real_model_api_requests"] == 0
    assert summary["provider_request_entries"] == 1
    assert summary["runtime_model_reservations"] == 1
    assert summary["tool_executions"] == 1
    assert summary["manual_reasoning_review"] == "not_run"
    assert summary["production_data_used"] is False
    assert summary["prometheus_used"] is False
    assert summary["remediation_planner"] is None
    assert summary["limits"]["max_attempts"] == 1
    assert summary["limits"]["max_resumes"] == 0
    assert summary["limits"]["max_human_requests"] == 0
    assert (root / "runtime.sqlite3").is_file()
    assert (root / "incidents.json").is_file()
    checkpoint = read_json(root, "checkpoint.json")
    Checkpoint.model_validate(checkpoint)
    assert checkpoint["records"][:4] == read_json(root, "case.json")["records"]
    assert len(checkpoint["records"]) == 5
    version = checkpoint["records"][-1]
    assert version["name"] == "get_running_version"
    assert version["arguments"] == {"service": "order-service"}
    assert version["result"]["status"] == "no_data"
    assert "commit_sha" not in version["result"]
    assert checkpoint["budget"]["model_requests"] == 1
    assert checkpoint["budget"]["tool_executions"] == 1
    assert checkpoint["result"]["answer"] == preflight.OFFLINE_ANSWER
    assert checkpoint["result"]["model_rounds"] == 1
    assert checkpoint["result"]["tool_calls"] == 1
    assert not checkpoint.get("remediation_pending")
    body = read_json(root, "request.json")
    assert body["model"] == preflight.MODEL
    assert body["tools"] == []
    assert body["max_output_tokens"] == 2048
    assert body["reasoning"] == {"effort": "none"}
    assert len(json.dumps(body, ensure_ascii=False).encode()) <= 98304
    payloads = analyses(body["input"])
    assert len(payloads) == 1
    analysis = payloads[0]["metric_analysis"]
    assert analysis["use_for_current_state"] is False
    assert analysis["evidence_scope"] == "historical_analysis"
    assert set(analysis["reports"][0]["source_observation_ids"]) == {
        item["observation_id"] for item in checkpoint["records"][:4]
    }
    assert "expected" not in json.dumps(body)
    events = read_json(root, "events.json")
    assert sum(item["event_type"] == "model_reserved" for item in events) == 1
    assert sum(item["event_type"] == "tool_reserved" for item in events) == 1
    assert not any(
        item["event_type"].startswith(("human_request_", "remediation_"))
        for item in events
    )
    assert secret not in artifact_text(root)


def test_injected_sdk_keeps_raw_output_and_exact_evidence_references(
    preflight,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-key")
    result = response(answer="历史时间不同；case-a-t2 的 0.0 有效，5xx 缺失。根因未证实。")
    raw_text = result.model_dump_json() + "\n"
    sdk = fake_sdk(result, raw_text=raw_text)
    summary = preflight.run_case(tmp_path, live=True, sdk=sdk)
    assert summary["session_status"] == "COMPLETED"
    assert summary["real_model_api_requests"] == 1
    assert summary["manual_reasoning_review"] == "pending"
    sdk.with_options.assert_called_once_with(max_retries=0, timeout=30)
    sdk.responses.with_raw_response.create.assert_called_once()
    sdk.close.assert_called_once()
    assert sdk.responses.with_raw_response.create.call_args.kwargs == read_json(
        tmp_path, "request.json",
    )
    assert (tmp_path / "response.raw.json").read_text(encoding="utf-8") == raw_text
    assert read_json(tmp_path, "response.sdk.json") == result.model_dump(mode="json")
    checkpoint = read_json(tmp_path, "checkpoint.json")
    assert checkpoint["result"]["answer"] == result.output_text


@pytest.mark.parametrize("status", [401, 402, 403, 429, 500])
def test_http_failure_is_terminal_for_this_run_without_retry_or_error_body_leak(
    preflight,
    tmp_path,
    monkeypatch,
    status,
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-key")
    sdk = fake_sdk(failure=sdk_error(status))
    summary = preflight.run_case(tmp_path, live=True, sdk=sdk)
    expected = "PAUSED" if status in {401, 402, 403} else "EXHAUSTED"
    assert summary["session_status"] == expected
    assert summary["real_model_api_requests"] == 1
    assert summary["runtime_model_reservations"] == 1
    assert summary["tool_executions"] == 1
    sdk.with_options.assert_called_once_with(max_retries=0, timeout=30)
    sdk.responses.with_raw_response.create.assert_called_once()
    sdk.close.assert_called_once()
    error = read_json(tmp_path, "provider-error.json")
    assert error["http_status"] == status
    assert error["no_retry"] is True
    checkpoint = read_json(tmp_path, "checkpoint.json")
    assert len(checkpoint["records"]) == 5
    assert "untrusted-provider-detail-never-save" not in artifact_text(tmp_path)


@pytest.mark.parametrize("failure", ["timeout", "incomplete", "empty", "tool"])
def test_bad_model_response_cannot_execute_tools_or_start_another_round(
    preflight,
    tmp_path,
    monkeypatch,
    failure,
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-key")
    result = response()
    error = None
    if failure == "timeout":
        error = openai.APITimeoutError(
            request=httpx2.Request("POST", "https://provider.invalid/responses"),
        )
    elif failure == "incomplete":
        result = response(status="incomplete")
    elif failure == "empty":
        result = response(answer="")
    else:
        result = response(answer="", calls=[
            call("execute_remediation", {"service": "order-service"}),
        ])
    sdk = fake_sdk(result, error)
    summary = preflight.run_case(tmp_path, live=True, sdk=sdk)
    assert summary["session_status"] != "COMPLETED"
    assert summary["session_status"] != "WAITING_RETRY"
    assert summary["runtime_model_reservations"] == 1
    assert summary["real_model_api_requests"] == 1
    assert summary["tool_executions"] == 1
    sdk.responses.with_raw_response.create.assert_called_once()
    sdk.close.assert_called_once()
    checkpoint = read_json(tmp_path, "checkpoint.json")
    assert len(checkpoint["records"]) == 5
    assert not checkpoint.get("remediation_pending")
    assert not checkpoint.get("pending")
    assert not checkpoint.get("result", {}).get("success")


def permitted_body(preflight):
    return {
        "model": preflight.MODEL,
        "input": [{"role": "user", "content": "historical fixture only"}],
        "instructions": "只读历史证据判断。",
        "tools": [],
    }


@pytest.mark.parametrize(
    "mutation",
    ["oversize", "wrong_model", "tool", "output_override", "extra_field"],
)
def test_provider_rejects_unsafe_request_before_entering_sdk(
    preflight,
    tmp_path,
    mutation,
):
    sdk = fake_sdk()
    provider = preflight.BoundedProvider(sdk, tmp_path, live=True)
    provider.with_options(max_retries=0, timeout=30)
    body = permitted_body(preflight)
    if mutation == "oversize":
        body["input"][0]["content"] = "中" * (preflight.MAX_REQUEST_BYTES // 3)
    elif mutation == "wrong_model":
        body["model"] = "unapproved-model"
    elif mutation == "tool":
        body["tools"] = [{"type": "function", "name": "execute_remediation"}]
    elif mutation == "output_override":
        body["max_output_tokens"] = 100000
    else:
        body["previous_response_id"] = "unapproved-external-context"
    with pytest.raises(ValueError, match="preflight_"):
        provider.create(**body)
    sdk.with_options.assert_not_called()
    sdk.responses.with_raw_response.create.assert_not_called()
    assert provider.requests == 0
    assert provider.real_requests == 0
    assert not (tmp_path / "request.json").exists()


def test_provider_refuses_second_request_and_expired_deadline(
    preflight,
    tmp_path,
    monkeypatch,
):
    sdk = fake_sdk()
    provider = preflight.BoundedProvider(sdk, tmp_path, live=True)
    provider.with_options(max_retries=0, timeout=30)
    body = permitted_body(preflight)
    provider.create(**body)
    with pytest.raises(ValueError, match="preflight_model_request_limit"):
        provider.create(**body)
    sdk.responses.with_raw_response.create.assert_called_once()
    other = preflight.BoundedProvider(sdk, tmp_path, live=True)
    other.with_options(max_retries=0, timeout=30)
    monkeypatch.setattr(preflight.time, "monotonic", lambda: other.deadline + 1)
    with pytest.raises(TimeoutError, match="preflight_deadline"):
        other.create(**body)
    sdk.responses.with_raw_response.create.assert_called_once()


@pytest.mark.parametrize(
    "options",
    [
        {"max_retries": 1, "timeout": 30},
        {"max_retries": 0, "timeout": 60},
    ],
)
def test_provider_rejects_retry_or_timeout_override(
    preflight,
    tmp_path,
    options,
):
    provider = preflight.BoundedProvider(None, tmp_path, live=False)
    with pytest.raises(ValueError, match="unsafe_sdk_options"):
        provider.with_options(**options)


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("execute_remediation", {"service": "order-service"}),
        ("query_metrics", {"service": "order-service", "metric": "http_5xx_rate"}),
        ("get_running_version", {"service": "inventory-service"}),
        ("get_running_version", {"service": "order-service", "shell": "unapproved"}),
        ("get_running_version", {}),
    ],
)
def test_isolated_tool_rejects_unknown_tools_targets_and_extra_arguments(
    preflight,
    name,
    arguments,
):
    item = SimpleNamespace(name=name, arguments=json.dumps(arguments))
    with pytest.raises(ValueError, match="preflight_tool_not_allowed"):
        preflight.isolated_tool(item)


def test_run_case_rejects_production_or_nonempty_directory(
    preflight,
    tmp_path,
):
    with pytest.raises(ValueError, match="preflight_requires_os_temporary_directory"):
        preflight.run_case(preflight.ROOT)
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="preflight_requires_empty_directory"):
        preflight.run_case(tmp_path)
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert list(tmp_path.iterdir()) == [sentinel]


@pytest.mark.parametrize("flag", ["--run-live", "--approve-paid-model-run"])
def test_cli_single_paid_flag_is_rejected_before_launch(preflight, flag):
    completed = subprocess.run(
        [sys.executable, "-B", str(Path(preflight.__file__).resolve()), flag],
        cwd=preflight.ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 2
    assert "paid mode requires both flags" in completed.stderr
    assert completed.stdout == ""


def test_provider_key_echo_is_redacted_from_all_persisted_files(
    preflight,
    tmp_path,
    monkeypatch,
):
    secret = "preflight-credential-echo-canary-never-real-key"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    result = response(answer=f"provider accidentally echoed {secret}")
    sdk = fake_sdk(result)
    summary = preflight.run_case(tmp_path, live=True, sdk=sdk)
    assert summary["session_status"] == "COMPLETED"
    for artifact in tmp_path.rglob("*"):
        if artifact.is_file():
            assert secret.encode() not in artifact.read_bytes(), artifact.name
    answer = read_json(tmp_path, "checkpoint.json")["result"]["answer"]
    assert "[credential redacted]" in answer


def test_installed_sdk_serializes_one_bounded_request_to_mock_transport(
    preflight,
    tmp_path,
    monkeypatch,
):
    secret = "preflight-mock-transport-canary-not-real-key"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    requests_seen = []
    result = response(answer="固定历史判断；当前健康无法确认，根因待验证。")

    def transport(request):
        assert request.method == "POST"
        assert str(request.url) == "https://api.deepseek.com/responses"
        assert request.headers["Authorization"] == f"Bearer {secret}"
        requests_seen.append(json.loads(request.content))
        return httpx2.Response(200, json=result.model_dump(mode="json"))

    sdk = openai.OpenAI(
        api_key=secret,
        base_url=preflight.BASE_URL,
        max_retries=0,
        timeout=30,
        http_client=httpx2.Client(
            transport=httpx2.MockTransport(transport),
            trust_env=False,
            follow_redirects=False,
        ),
    )
    summary = preflight.run_case(tmp_path, live=True, sdk=sdk)
    assert summary["session_status"] == "COMPLETED"
    assert summary["real_model_api_requests"] == 1
    assert requests_seen == [read_json(tmp_path, "request.json")]
    assert requests_seen[0]["model"] == preflight.MODEL
    assert requests_seen[0]["max_output_tokens"] == 2048
    assert requests_seen[0]["reasoning"] == {"effort": "none"}
    assert requests_seen[0]["tools"] == []
    assert requests_seen[0]["tool_choice"] == "none"
    assert secret not in artifact_text(tmp_path)


@pytest.mark.parametrize("handler_kind", ["production_file", "stream"])
def test_run_case_rejects_existing_unisolated_logger(
    preflight,
    tmp_path,
    monkeypatch,
    handler_kind,
):
    handler = Mock(spec=(
        logging.FileHandler if handler_kind == "production_file"
        else logging.StreamHandler
    ))
    if handler_kind == "production_file":
        handler.baseFilename = str(preflight.ROOT / "agent_logger" / "agent.log")
    monkeypatch.setattr(logging.getLogger("agent"), "handlers", [handler])
    with pytest.raises(
        ValueError,
        match="preflight_requires_isolated_logger_or_fresh_process",
    ):
        preflight.run_case(tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert handler.mock_calls == []


@pytest.mark.parametrize("stop", ["timeout", "keyboard_interrupt"])
def test_supervisor_stops_only_its_own_child_and_records_no_retry(
    preflight,
    tmp_path,
    monkeypatch,
    capsys,
    stop,
):
    child = Mock()
    interruption = (
        subprocess.TimeoutExpired("owned-preflight-child", 90)
        if stop == "timeout" else KeyboardInterrupt()
    )
    child.wait.side_effect = [interruption, 0]
    launch = Mock(return_value=child)
    monkeypatch.setattr(preflight.subprocess, "Popen", launch)
    monkeypatch.setattr(preflight.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))
    monkeypatch.setattr(preflight, "credentials", lambda: ("", "missing"))
    monkeypatch.setattr(sys, "argv", ["preflight_real_model.py"])
    assert preflight.main() == 2
    launch.assert_called_once()
    assert launch.call_args.args[0][-2:] == [str(tmp_path), "offline"]
    child.terminate.assert_called_once_with()
    assert len(child.wait.call_args_list) == 2
    assert child.wait.call_args_list[0].kwargs == {"timeout": 90}
    assert child.wait.call_args_list[1].kwargs == {"timeout": 10}
    assert [item[0] for item in child.method_calls] == [
        "wait", "terminate", "wait",
    ]
    assert read_json(tmp_path, "supervisor-stop.json") == {
        "reason": "wall_timeout_or_keyboard_interrupt",
        "billing": "not_applicable",
        "no_retry": True,
    }
    output = json.loads(capsys.readouterr().out)
    assert output == {"stopped": True, "artifact_directory": str(tmp_path)}
    assert not (tmp_path / "request.json").exists()


def test_child_startup_missing_key_is_reported_without_sdk_or_network(
    preflight,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(preflight, "credentials", lambda: ("", "missing"))
    constructor = Mock(side_effect=AssertionError("sdk_must_not_be_constructed"))
    monkeypatch.setattr(openai, "OpenAI", constructor)
    assert preflight.child_main(tmp_path, live=True) == 2
    constructor.assert_not_called()
    assert read_json(tmp_path, "startup-error.json") == {
        "category": "ValueError",
        "reason": "deepseek_api_key_missing",
        "no_retry": True,
    }
    assert sorted(item.name for item in tmp_path.iterdir()) == [
        "startup-error.json",
    ]


def test_child_startup_exception_keeps_category_without_credential_or_raw_text(
    preflight,
    tmp_path,
    monkeypatch,
):
    secret = "preflight-startup-canary-never-real-key"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    run = Mock(side_effect=RuntimeError(f"untrusted startup detail {secret}"))
    monkeypatch.setattr(preflight, "run_case", run)
    assert preflight.child_main(tmp_path, live=True) == 2
    run.assert_called_once_with(tmp_path, live=True)
    assert read_json(tmp_path, "startup-error.json") == {
        "category": "RuntimeError",
        "reason": "initialization_or_artifact_failure",
        "no_retry": True,
    }
    output = artifact_text(tmp_path)
    assert secret not in output
    assert "untrusted startup detail" not in output
