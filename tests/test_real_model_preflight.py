"""P5.3c.2 safety and persistence checks, without a real model request."""
from copy import deepcopy
from datetime import timedelta
import importlib
import json
import logging
from pathlib import Path
import re
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


def request_observations(body):
    payloads = analyses(body["input"])
    assert len(payloads) == 1
    return {
        item["observation_id"]: item
        for field in ("historical_observations", "failed_or_missing_observations")
        for item in payloads[0][field]
    }


def review_citations(answer, records):
    """Check source IDs only; no natural-language statement is graded as true.

    Known IDs must occur as complete tokens. Unknown IDs can be detected only
    when explicitly written as ``[observation_id=...]``; other citation formats,
    attribution, timing, quantifiers and meaning always need human review.
    """
    source_ids = {item["observation_id"] for item in records}
    cited = {
        source_id for source_id in source_ids
        if re.search(
            rf"(?<![A-Za-z0-9_-]){re.escape(source_id)}(?![A-Za-z0-9_-])",
            answer,
        )
    }
    explicit = set(re.findall(r"\[observation_id=([A-Za-z0-9_-]+)\]", answer))
    missing = source_ids - cited
    unknown = explicit - source_ids
    failed = bool(missing or unknown)
    return {
        "cited_observation_ids": sorted(cited),
        "missing_observation_ids": sorted(missing),
        "unknown_explicit_observation_ids": sorted(unknown),
        "citation_check": "failed" if failed else "complete",
        "semantic_review": "required",
        "strict_acceptance": "failed" if failed else "pending",
    }


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
    assert summary["manual_reasoning_review"] == "required"
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
    snapshot = read_json(root, "request-source-snapshot.json")
    assert snapshot["records"] == checkpoint["records"]
    assert snapshot["session_id"]
    assert snapshot["checkpoint_id"]
    assert snapshot["identity"] == checkpoint["identity"]
    version = checkpoint["records"][-1]
    assert version["name"] == "get_running_version"
    assert version["arguments"] == {"service": "order-service"}
    assert version["result"]["status"] == "no_data"
    assert "commit_sha" not in version["result"]
    assert version["collected_at"] != COLLECTED.isoformat()
    assert "evaluated_at" not in version["result"]
    assert "evaluation_time" not in version["result"]
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
    assert body["tool_choice"] == "none"
    assert preflight.TASK in body["instructions"]
    assert "约 250～350 汉字" in body["instructions"]
    assert "最多三项进一步取证方向" in body["instructions"]
    assert "缺失时不继承查询或邻项时间" in body["instructions"]
    assert len(json.dumps(body, ensure_ascii=False).encode()) <= 98304
    payloads = analyses(body["input"])
    observations = request_observations(body)
    assert set(observations) == {
        item["observation_id"] for item in checkpoint["records"]
    }
    for source in checkpoint["records"]:
        observed = observations[source["observation_id"]]
        expected_result = source["result"]
        if source["name"] == "query_metrics_aligned":
            expected_result = {**expected_result, "summary_truncated": False}
        assert observed["result"] == expected_result
        assert observed["arguments"] == source["arguments"]
        assert observed["collected_at"] == source["collected_at"]
        assert observed["status"] == source["result"]["status"]
        assert observed["use_for_current_state"] is False
    version_in_request = observations[version["observation_id"]]
    assert version_in_request["collected_at"] == version["collected_at"]
    assert "evaluated_at" not in version_in_request
    assert "evaluation_time" not in version_in_request
    assert "evaluated_at" not in version_in_request["result"]
    assert "evaluation_time" not in version_in_request["result"]
    second_snapshot = observations[checkpoint["records"][1]["observation_id"]]
    assert second_snapshot["result"]["evaluation_time"] == LATER.isoformat()
    missing_sample = second_snapshot["result"]["results"]["http_5xx_rate"]
    assert missing_sample["status"] == "no_data"
    assert "value" not in missing_sample
    assert "evaluated_at" not in missing_sample
    zero_sample = second_snapshot["result"]["results"]["http_request_rate"]
    assert zero_sample["status"] == "ok"
    assert zero_sample["value"] == 0.0
    assert zero_sample["evaluated_at"] == LATER.isoformat()
    assert COLLECTED.isoformat() != zero_sample["evaluated_at"]
    analysis = payloads[0]["metric_analysis"]
    assert analysis["use_for_current_state"] is False
    assert analysis["evidence_scope"] == "historical_analysis"
    assert analysis["selected_record_count"] == 4
    assert not any(analysis["excluded_record_counts"].values())
    assert analysis["status"] == "partial"
    expected_report = analyze_metric_evidence(
        list(reversed(checkpoint["records"][:4])), service="order-service",
    ).to_dict()
    assert analysis["reports"] == json.loads(json.dumps([{
        "service": "order-service", "target_role": "primary", **expected_report,
    }]))
    assert set(analysis["reports"][0]["source_observation_ids"]) == {
        item["observation_id"] for item in checkpoint["records"][:4]
    }
    assert "expected" not in json.dumps(body)
    assert all(
        value not in json.dumps(body, ensure_ascii=False)
        for name, value in read_json(root, "case.json")["expected"].items()
        if name != "citations"
    )
    events = read_json(root, "events.json")
    assert any(
        item["event_type"] == "checkpoint_saved"
        and item["data"]["checkpoint_id"] == snapshot["checkpoint_id"]
        for item in events
    )
    assert sum(item["event_type"] == "model_reserved" for item in events) == 1
    assert sum(item["event_type"] == "tool_reserved" for item in events) == 1
    assert not any(
        item["event_type"].startswith(("human_request_", "remediation_"))
        for item in events
    )
    assert secret not in artifact_text(root)


def test_generic_task_rules_reach_request_without_case_answers(
    preflight,
    tmp_path,
):
    preflight.run_case(tmp_path)
    body = read_json(tmp_path, "request.json")
    task = preflight.TASK
    assert task in body["instructions"]
    required_rules = (
        "不再生成 sources 事实副本",
        "唯一顶层字段 claims",
        "引用存在不代表引用支持相应陈述",
        "collected_at 只表示采集",
        "evaluation_time 只表示查询目标",
        "缺失时不继承查询或邻项时间",
        "不把历史当当前状态",
        "不把 no_data 当服务不存在或恢复",
        "不添加无基线支持的流量高低、样本量或故障原因",
    )
    assert all(rule in body["instructions"] for rule in required_rules)
    assert not any(f"{number}. " in task for number in range(1, 11))
    for field in ("confirmed", "limitation", "hypothesis", "next_check"):
        assert field in task
    assert "约 250～350 汉字" in task
    assert re.search(r"\d{4}-\d{2}-\d{2}", task) is None
    case = read_json(tmp_path, "case.json")
    assert all(item["observation_id"] not in task for item in case["records"])
    serialized = json.dumps(body, ensure_ascii=False)
    assert '"expected"' not in serialized
    assert '"grading"' not in serialized
    assert all(
        value not in serialized
        for name, value in case["expected"].items()
        if name != "citations"
    )


def test_request_preserves_each_historical_collection_date_without_inheritance(
    preflight,
    tmp_path,
    monkeypatch,
):
    case = deepcopy(preflight.build_case())
    original_results = deepcopy([item["result"] for item in case["records"]])
    for index, item in enumerate(case["records"]):
        item["collected_at"] = (
            COLLECTED + timedelta(days=index % 3)
        ).isoformat()
    monkeypatch.setattr(preflight, "build_case", lambda: deepcopy(case))
    preflight.run_case(tmp_path)
    body = read_json(tmp_path, "request.json")
    observations = request_observations(body)
    checkpoint = read_json(tmp_path, "checkpoint.json")
    assert len({item["collected_at"][:10] for item in case["records"]}) == 3
    assert [item["result"] for item in checkpoint["records"][:4]] == original_results
    for source in case["records"]:
        observed = observations[source["observation_id"]]
        assert observed["collected_at"] == source["collected_at"]
        expected = source["result"]
        if source["name"] == "query_metrics_aligned":
            expected = {**expected, "summary_truncated": False}
        assert observed["result"] == expected
        assert observed["use_for_current_state"] is False
    version = checkpoint["records"][-1]
    observed_version = observations[version["observation_id"]]
    assert observed_version["collected_at"] == version["collected_at"]
    assert "evaluated_at" not in observed_version["result"]
    assert "evaluation_time" not in observed_version["result"]
    payload = analyses(body["input"])[0]
    assert payload["metric_analysis"]["use_for_current_state"] is False
    assert payload["metric_analysis"]["evidence_scope"] == "historical_analysis"
    assert "不能证明当前健康" in payload["evidence_usage"]


@pytest.mark.parametrize(
    "answer_kind,expected_citations,expected_acceptance",
    [
        ("omitted_version", "failed", "failed"),
        ("unsupported_all_records", "complete", "pending"),
        ("correctly_scoped", "complete", "pending"),
        ("fabricated_source", "failed", "failed"),
        ("partial_source_id", "failed", "failed"),
    ],
)
def test_completed_answers_keep_raw_text_and_require_semantic_review(
    preflight,
    tmp_path,
    monkeypatch,
    answer_kind,
    expected_citations,
    expected_acceptance,
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-key")
    captured = {}
    sdk = fake_sdk()

    def answer_request(**body):
        observations = request_observations(body)
        version = next(
            item for item in observations.values()
            if item["name"] == "get_running_version"
        )
        metric_ids = sorted(set(observations) - {version["observation_id"]})
        metric_refs = " ".join(
            f"[observation_id={source_id}]" for source_id in metric_ids
        )
        version_ref = f"[observation_id={version['observation_id']}]"
        if answer_kind == "omitted_version":
            answer = f"历史指标 {metric_refs}。版本探测 no_data，版本未知。"
        elif answer_kind == "unsupported_all_records":
            answer = (
                f"所有记录采集时间均为 {COLLECTED.isoformat()}，"
                f"评估时间均为 {AT.isoformat()}。{metric_refs} {version_ref}"
            )
        elif answer_kind == "correctly_scoped":
            answer = (
                f"四条历史指标记录采集于 {COLLECTED.isoformat()}：{metric_refs}。"
                f"它们的样本评估时点按各自字段区分；缺失样本未给出评估时间。"
                f"版本探测采集于 {version['collected_at']}，"
                f"状态 no_data，未提供 evaluated_at 或 evaluation_time，"
                f"其评估时间未知 {version_ref}。当前健康与根因均未确认。"
            )
        elif answer_kind == "fabricated_source":
            answer = (
                f"历史指标 {metric_refs}，版本未知 {version_ref}。"
                "额外证据 [observation_id=obs-fabricated-source]。"
            )
        else:
            answer = (
                f"历史指标 {metric_refs}，版本未知 "
                f"[observation_id={version['observation_id'][:-1]}]。"
            )
        result = response(answer=answer)
        raw_text = result.model_dump_json() + "\n"
        captured.update(answer=answer, raw_text=raw_text, version=version)
        return SimpleNamespace(text=raw_text, parse=lambda: result)

    sdk.responses.with_raw_response.create.side_effect = answer_request
    summary = preflight.run_case(tmp_path, live=True, sdk=sdk)
    checkpoint = read_json(tmp_path, "checkpoint.json")
    saved_answer = checkpoint["result"]["answer"]
    review = review_citations(saved_answer, checkpoint["records"])
    assert summary["session_status"] == "COMPLETED"
    assert summary["manual_reasoning_review"] == "required"
    assert review["citation_check"] == expected_citations
    assert review["semantic_review"] == "required"
    assert review["strict_acceptance"] == expected_acceptance
    assert saved_answer == captured["answer"]
    assert (tmp_path / "response.raw.json").read_text(encoding="utf-8") == (
        captured["raw_text"]
    )
    if answer_kind in {"omitted_version", "partial_source_id"}:
        assert review["missing_observation_ids"] == [
            captured["version"]["observation_id"],
        ]
    elif answer_kind == "fabricated_source":
        assert review["unknown_explicit_observation_ids"] == ["obs-fabricated-source"]
    else:
        assert not review["missing_observation_ids"]
        assert not review["unknown_explicit_observation_ids"]
    sdk.responses.with_raw_response.create.assert_called_once()


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
    assert summary["manual_reasoning_review"] == "required"
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
    assert preflight.TASK in requests_seen[0]["instructions"]
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
