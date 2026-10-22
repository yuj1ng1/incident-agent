"""Offline declarations and the unchanged real failure; no provider execution."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import p65_answer_review as review
from scripts.p65_model_boundary import encoded, sha


FIXTURES = Path(__file__).parent / "fixtures/p65_s1"


def tool_citation(record, identifier="metric"):
    return {
        "id": identifier,
        "type": "tool_call",
        "call_id": record["call_id"],
        "tool": record["name"],
        "arguments": deepcopy(record["arguments"]),
        "observation_id": None,  # Hidden from the Provider; reviewer resolves it.
        **review.provenance(record),
    }


@pytest.fixture
def sample():
    """Entirely synthetic unit evidence and manually written test answer."""
    records = [{
        "call_id": "call-unit-metric",
        "observation_id": "obs-unit-metric",
        "name": "query_metrics",
        "arguments": {"service": "order-service", "metric": "http_requests_total"},
        "collected_at": "2026-10-22T00:00:00Z",
        "result": {
            "status": "no_data",
            "historical_only": True,
            "use_for_current_state": False,
            "captured_at": "2026-10-18T11:54:54Z",
            "source": "synthetic-fixture-only",
            "originals": [{"path": "observations/unit.json"}],
            "captured_result": {
                "status": "ok", "value": 13.0,
                "evaluated_at": "2026-10-18T11:54:53Z",
            },
            "sanitized_projection": {"observation_id": "p63:unit:0123456789abcdef"},
        },
    }, {
        "call_id": "call-unit-logs",
        "observation_id": "obs-unit-logs",
        "name": "query_logs",
        "arguments": {
            "service": "order-service", "start": "2026-10-18T11:52:00Z",
            "end": "2026-10-18T11:54:00Z",
        },
        "collected_at": "2026-10-22T00:00:01Z",
        "result": {
            "status": "no_data", "reason": "outside_approved_frozen_evidence",
        },
    }]
    context = {"known_gaps": ["输入背景：日志缺少请求身份。"], "real_firing_observed": False}
    document = {
        "schema_version": review.SCHEMA_VERSION,
        "citations": [tool_citation(records[0])],
        "claims": [{
            "id": "c1", "text": "人工样本：历史累计请求值为13.0，非故障次数。",
            "kind": "fact", "refs": ["metric"], "use": "field_value",
            "field_path": ["captured_result", "value"], "value": 13.0,
        }],
    }
    return document, records, context


def evaluate(sample):
    document, records, context = sample
    return review.review_answer(encoded(document).decode("utf-8"), records, context)


def codes(result):
    return {item["code"] for item in result["issues"]}


def test_legal_historical_reference_and_hidden_runtime_id_are_accepted(sample):
    result = evaluate(sample)
    assert result["declaration_check"] == "passed"
    citation = result["resolved_citations"]["metric"]
    assert citation["observation_id"] == "obs-unit-metric"
    assert citation["historical_evidence_id"].startswith("p63:")
    assert citation["outer_status"] == "no_data"
    assert citation["captured_status"] == "ok"
    assert citation["replay_collected_at"] != citation["captured_at"]
    assert result["semantic_review"]
    assert result["result_trust"]["trusted"] is False
    assert result["result_trust"]["semantic_status"] == "pending_review"
    assert result["result_trust"]["mechanical_scope"] == "not_checked"


@pytest.mark.parametrize("field,value,error", [
    ("call_id", None, "missing_call_id"),
    ("call_id", "call-forged", "unknown_call_id"),
    ("call_id", "p63:unit:0123456789abcdef", "historical_id_used_as_call_id"),
    ("call_id", "call-unit-logs", "tool_name_mismatch"),
    ("tool", "query_logs", "tool_name_mismatch"),
    ("arguments", {"service": "other-service"}, "tool_arguments_mismatch"),
    ("observation_id", "obs-unit-logs", "observation_id_mismatch"),
    ("observation_id", "p63:unit:0123456789abcdef", "observation_id_mismatch"),
    ("outer_status", "ok", "provenance_mismatch"),
    ("captured_status", "no_data", "provenance_mismatch"),
    ("historical_evidence_id", "p63:other:0123456789abcdef", "provenance_mismatch"),
    ("source", "fabricated", "provenance_mismatch"),
    ("originals", ["logs-never-read.json"], "provenance_mismatch"),
    ("captured_at", "2026-10-22T00:00:00Z", "provenance_mismatch"),
    ("evaluated_at", "2026-10-18T11:54:54Z", "provenance_mismatch"),
    ("event_window", {"start": "invented"}, "provenance_mismatch"),
])
def test_cross_association_and_fabrication_are_reported(sample, field, value, error):
    sample[0]["citations"][0][field] = value
    assert error in codes(evaluate(sample))


def test_missing_provenance_and_call_id_are_reported(sample):
    del sample[0]["citations"][0]["call_id"]
    assert "missing_call_id" in codes(evaluate(sample))
    sample[0]["citations"][0]["call_id"] = "call-unit-metric"
    del sample[0]["citations"][0]["captured_status"]
    assert "missing_provenance_field" in codes(evaluate(sample))


@pytest.mark.parametrize(
    "duplicate", ["citation", "call", "claim", "claim_ref", "record"],
)
def test_duplicate_identifiers_are_not_silently_overwritten(sample, duplicate):
    doc, records, _ = sample
    expected = {
        "citation": "duplicate_citation_id", "call": "duplicate_call_id",
        "claim": "duplicate_claim_id", "claim_ref": "duplicate_claim_reference",
        "record": "ambiguous_runtime_records",
    }[duplicate]
    if duplicate in {"citation", "call"}:
        copied = deepcopy(doc["citations"][0])
        if duplicate == "call":
            copied["id"] = "metric2"
        doc["citations"].append(copied)
    elif duplicate == "claim":
        doc["claims"].append(deepcopy(doc["claims"][0]))
    elif duplicate == "claim_ref":
        doc["claims"][0]["refs"].append("metric")
    else:
        records.append(deepcopy(records[0]))
    assert expected in codes(evaluate(sample))


def test_claims_can_reuse_one_citation_without_duplicate_failure(sample):
    copied = deepcopy(sample[0]["claims"][0])
    copied["id"] = "c2"
    sample[0]["claims"].append(copied)
    assert not evaluate(sample)["issues"]


@pytest.mark.parametrize("refs,error", [
    ([], "fact_without_reference"), (["forged-citation"], "unknown_citation_id"),
])
def test_missing_and_forged_claim_references(sample, refs, error):
    sample[0]["claims"][0]["refs"] = refs
    assert error in codes(evaluate(sample))


def test_input_background_requires_no_tool_call(sample):
    doc, _, _ = sample
    doc["citations"] = [{
        "id": "context", "type": "input_context", "input_path": ["known_gaps", 0],
    }]
    doc["claims"] = [{
        "id": "c1", "text": "输入背景称日志缺少请求身份，非本轮日志观测。",
        "kind": "fact", "refs": ["context"], "use": "field_value",
        "field_path": None, "value": None,
    }]
    # A whole input field may be cited without an artificial child selector.
    doc["claims"][0]["use"] = "observation"
    assert not evaluate(sample)["issues"]
    doc["citations"][0]["call_id"] = "call-invented-for-context"
    assert "input_context_has_tool_identity" in codes(evaluate(sample))


@pytest.mark.parametrize("use,error", [
    ("log_content", "logs_not_read"), ("valid_zero", "no_valid_zero"),
    ("fault_count", "independent_fault_count_not_established"),
    ("root_cause", "unverified_fact_in_historical_scope"),
    ("repair", "unverified_fact_in_historical_scope"),
    ("current_health", "unverified_fact_in_historical_scope"),
    ("current_recovery", "unverified_fact_in_historical_scope"),
])
def test_unverified_structured_facts_do_not_become_evidence(sample, use, error):
    claim = sample[0]["claims"][0]
    claim["use"] = use
    assert error in codes(evaluate(sample))
    claim["kind"] = "hypothesis"
    assert error not in codes(evaluate(sample))


def test_rejected_logs_have_no_body_but_preserve_actual_requested_range(sample):
    doc, records, _ = sample
    doc["citations"] = [tool_citation(records[1], "logs")]
    doc["claims"] = [{
        "id": "logs", "text": "本轮未取得日志正文。", "kind": "fact",
        "refs": ["logs"], "use": "field_value",
        "field_path": ["reason"], "value": "outside_approved_frozen_evidence",
    }]
    result = evaluate(sample)
    assert not result["issues"]
    assert result["resolved_citations"]["logs"]["event_window"] == {
        "start": records[1]["arguments"]["start"],
        "end": records[1]["arguments"]["end"],
    }
    doc["claims"][0].update(text="已成功读取日志正文。", use="log_content")
    assert "logs_not_read" in codes(evaluate(sample))


@pytest.mark.parametrize("value", [None, False, 13.0])
def test_no_data_or_nonzero_is_not_valid_zero(sample, value):
    doc, records, _ = sample
    records[0]["result"]["captured_result"].update(
        status="no_data", value=value,
    )
    doc["citations"] = [tool_citation(records[0])]
    doc["claims"][0]["use"] = "valid_zero"
    assert "no_valid_zero" in codes(evaluate(sample))


def test_actual_historical_zero_and_read_logs_are_not_rejected(sample):
    doc, records, _ = sample
    records[0]["result"]["captured_result"]["value"] = 0.0
    doc["claims"][0].update(use="valid_zero", value=0.0)
    assert not evaluate(sample)["issues"]
    records[1]["result"]["captured_result"] = {
        "status": "ok", "logs": [{"line": "synthetic historical log"}],
    }
    doc["citations"] = [tool_citation(records[1], "logs")]
    doc["claims"] = [{
        "id": "c1", "text": "人工样本：取得历史日志正文。", "kind": "fact",
        "refs": ["logs"], "use": "log_content",
    }]
    assert not evaluate(sample)["issues"]


@pytest.mark.parametrize("value,path", [
    (False, ["captured_result", "value"]),
    (0, ["captured_result", "missing"]),
    (None, ["captured_result", "status"]),
])
def test_literal_field_support_is_checked_with_strict_json_equality(
    sample,
    value,
    path,
):
    sample[0]["claims"][0].update(value=value, field_path=path)
    assert codes(evaluate(sample)) & {
        "source_field_value_mismatch", "source_field_missing",
    }


def test_false_and_zero_are_distinct_and_null_is_present(sample):
    doc, records, _ = sample
    records[0]["result"]["captured_result"]["value"] = 0
    doc["claims"][0]["value"] = False
    assert "source_field_value_mismatch" in codes(evaluate(sample))
    records[0]["result"]["captured_result"]["value"] = None
    doc["claims"][0]["value"] = None
    assert not evaluate(sample)["issues"]


@pytest.mark.parametrize("text", [
    '{"schema_version":"x","schema_version":"y"}',
    '{"number":NaN}', '{"number":1e999}', '[]', '{}',
])
def test_invalid_json_contract_is_advisory_rejection(sample, text):
    result = review.review_answer(text, sample[1], sample[2])
    assert "answer_contract_missing_or_invalid" in codes(result)
    assert result["result_trust"]["trusted"] is False


def test_mislabelled_natural_language_still_requires_semantic_review(sample):
    claim = sample[0]["claims"][0]
    claim.update(text="数据库根因已确认且当前业务已经恢复。", use="observation")
    # No keyword classifier pretends to understand this mismatched declaration.
    result = evaluate(sample)
    assert not result["issues"]
    assert result["semantic_review"]
    assert result["result_trust"]["trusted"] is False
    assert result["result_trust"]["semantic_status"] == "pending_review"


@pytest.fixture(scope="module")
def real_case():
    if not review.DEFAULT_RUN.exists():
        pytest.skip("sealed operator artifacts absent; no Provider fixture substituted")
    return review.load_real_case()


def test_original_three_historical_ids_are_mislabelled_calls(real_case):
    answer, records, context = real_case
    result = review.review_answer(answer, records, context)
    errors = [
        i for i in result["issues"]
        if i["code"] == "historical_id_used_as_call_id"
    ]
    assert len(errors) == 3
    assert {i["detail"] for i in errors} == {
        r["result"]["sanitized_projection"]["observation_id"] for r in records[2:]
    }
    assert sha(answer.encode("utf-8")) == review.ORIGINAL_ANSWER_SHA256
    # The original did not claim a successful log read.
    assert "logs_not_read" not in codes(result)
    assert "independent_fault_count_not_established" not in codes(result)


def test_human_correct_answer_is_accepted_but_remains_untrusted(real_case):
    _, records, context = real_case
    answer = (FIXTURES / "human_answer.json").read_bytes().decode("utf-8")
    result = review.review_answer(answer, records, context)
    assert not result["issues"]
    assert "人工编写测试样本" in answer
    assert result["result_trust"]["semantic_status"] == "pending_review"
    assert result["result_trust"]["trusted"] is False
    assert result["runtime_state_written"] is False


@pytest.mark.parametrize("index", range(8))
def test_fixed_semantic_review_cases_are_bound_to_original_lines(real_case, index):
    answer, _, _ = real_case
    manifest = json.loads((FIXTURES / "failure_cases.json").read_bytes())
    assert manifest["original_answer_sha256"] == sha(answer.encode("utf-8"))
    case = manifest["cases"][index]
    assert case["excerpt"] in answer.splitlines()[case["line"] - 1]
    assert case["disposition"] in {
        "deterministic_error", "semantic_review_required", "preserved_constraint",
    }
    result = review.review_answer(answer, real_case[1], real_case[2])
    assert len(result["semantic_review"]) == 5
    if case["disposition"] == "deterministic_error":
        assert "historical_id_used_as_call_id" in codes(result)
    else:
        # Binding a reviewer annotation is not automatic prose verification.
        assert result["result_trust"]["trusted"] is False


def test_s1_task_and_initial_input_change_without_expanding_scope():
    from scripts import p65_readonly_investigation as p65
    from tests.test_p65_readonly_investigation import synthetic_package

    body = p65.first_request(synthetic_package())
    initial = json.loads(body["input"][0]["content"])
    assert p65.REQUEST_VERSION == "p65_s1_semantics_v5_compact_refs_tools5"
    assert p65.MAX_MODELS == 3 and p65.MAX_TOOLS == 5
    assert body["max_output_tokens"] == 2048
    expected_task = "调查已经封存的 order-service 历史观测记录中反映的 HTTP 错误现象"
    assert expected_task in initial["task"]
    for required in (
        "不代表服务、进程或请求冻结", "受控合成 HTTP 500 输入",
        "不是实际 Prometheus firing", "数据库根因",
    ):
        assert required in initial["task"]
    assert "p65_s1_answer_v2_compact_refs" in body["instructions"]
    assert "本轮未取得日志正文" in body["instructions"]
    assert "hypothesis" in body["instructions"]
    assert "外层和 captured_result 状态分别保留" in body["instructions"]


def test_fixed_original_request_and_session_remain_v3(real_case):
    source = review.DEFAULT_RUN
    before = {
        p.name: (p.stat().st_mtime_ns, sha(p.read_bytes()))
        for p in source.iterdir() if p.is_file()
    }
    answer, records, context = review.load_real_case()
    review.review_answer(answer, records, context)
    first = json.loads((source / "request-01.json").read_bytes())
    summary = json.loads((source / "summary.json").read_bytes())
    assert first["request_version"] == "p65_dynamic_runtime_v3_tools5"
    assert first["sha256"] == (
        "d9cfac4386a1adfc2d565820e70bc03fc910cdf987020ab168df5481f2ff76a1"
    )
    assert summary["session"]["total_model_requests"] == 2
    assert summary["session"]["total_tool_executions"] == 5
    assert summary["result_trust"]["semantic_status"] == "pending_review"
    assert before == {
        p.name: (p.stat().st_mtime_ns, sha(p.read_bytes()))
        for p in source.iterdir() if p.is_file()
    }


def test_old_v3_plan_and_authorization_fail_before_any_paid_boundary(
    tmp_path,
    monkeypatch,
):
    from scripts import p65_readonly_investigation as p65

    frozen = review.ROOT / "data/p65-third-tools5-241dca6-20261021-01/prepared"
    if not frozen.exists():
        pytest.skip("operator v3 prepared package absent")
    credential = Mock(side_effect=AssertionError("no credentials"))
    sdk = Mock(side_effect=AssertionError("no SDK"))
    monkeypatch.setattr("scripts.preflight_real_model.credentials", credential)
    with pytest.raises(ValueError, match="scope_or_code_changed"):
        p65.run_prepared(
            frozen,
            tmp_path / "denied",
            live=True,
            authorization={"request_version": "p65_dynamic_runtime_v3_tools5"},
            sdk_factory=sdk,
        )
    credential.assert_not_called()
    sdk.assert_not_called()
    assert not (tmp_path / "denied").exists()
