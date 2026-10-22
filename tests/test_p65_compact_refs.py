"""S3 offline samples only; no converted answer is Provider original output."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import p65_answer_review as s1
from scripts import p65_claim_quality as s2
from scripts import p65_readonly_investigation as p65
from scripts.p65_model_boundary import encoded, sha, validate_approval
from tests.test_p65_readonly_investigation import approval, prepared
from tests.test_p65_third_preparation import reseal_plan


FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = json.loads((FIXTURES / "p65_s2/corpus.json").read_bytes())
IDENTITY = {"id", "type", "call_id", "tool", "arguments"}
METADATA = {
    "observation_id": "obs-forged",
    "outer_status": "ok",
    "captured_status": "ok",
    "historical_evidence_id": "p63:forged",
    "source": "invented-source",
    "originals": ["never-read.json"],
    "captured_at": "2026-10-22T01:00:00Z",
    "evaluated_at": "2026-10-22T01:00:00Z",
    "event_window": {"start": "invented", "end": "invented"},
}


def compact_copy(document):
    """Test-only projection of handcrafted samples, never Provider answers."""
    result = deepcopy(document)
    result["schema_version"] = s1.COMPACT_SCHEMA_VERSION
    result["citations"] = [
        {key: value for key, value in row.items() if key in (
            IDENTITY if row["type"] == "tool_call"
            else {"id", "type", "input_path"}
        )}
        for row in result["citations"]
    ]
    return result


@pytest.fixture
def sample():
    case = next(c for c in CORPUS["cases"] if c["id"] == "D_complete_legal")
    return (
        compact_copy(case["answer"]),
        deepcopy(CORPUS["records"]),
        deepcopy(CORPUS["input_context"]),
    )


def evaluate(sample):
    document, records, context = sample
    answer = encoded(document).decode("utf-8")
    checked = s1.review_answer(answer, records, context)
    return checked, s2.evaluate_claim_quality(answer, records, context, checked)


def codes(sample):
    return set(evaluate(sample)[1]["issue_codes"])


@pytest.mark.parametrize("case", [
    c for c in CORPUS["cases"]
    if c["id"] not in {"A_forged_observation", "A_other_metric_source"}
], ids=lambda c: c["id"])
def test_v1_v2_preserve_binding_fields_support_and_gate(case):
    """Metadata forgery has a stricter v2 rejection, tested separately below."""
    old = deepcopy(case["answer"])
    new = compact_copy(old)
    records, context = CORPUS["records"], CORPUS["input_context"]
    before = deepcopy((old, new, records, context))
    checked1, report1 = evaluate((old, records, context))
    checked2, report2 = evaluate((new, records, context))
    assert checked1["resolved_citations"] == checked2["resolved_citations"]
    assert checked1["issues"] == checked2["issues"]
    assert report1["claims"] == report2["claims"]
    assert report1["issue_codes"] == report2["issue_codes"]
    assert report1["acceptance_gate"] == report2["acceptance_gate"]
    assert not report2["acceptance_gate"]["automatic_acceptance_allowed"]
    assert (old, new, records, context) == before


@pytest.mark.parametrize("field", ["call_id", "tool", "arguments"])
def test_compact_tool_identity_fields_are_required(sample, field):
    del sample[0]["citations"][0][field]
    assert "answer_contract_missing_or_invalid" in codes(sample)
    assert not evaluate(sample)[0]["resolved_citations"]


@pytest.mark.parametrize("call_id,error", [
    ("call-unknown", "unknown_call_id"),
    ("p63:metric-http_requests_total:5da1ed71e3060dd4",
     "historical_id_used_as_call_id"),
    ("obs-call-total", "unknown_call_id"),
])
def test_non_runtime_call_ids_are_rejected(sample, call_id, error):
    sample[0]["citations"][0]["call_id"] = call_id
    assert error in codes(sample)


@pytest.mark.parametrize("field,value,error", [
    ("tool", "query_logs", "tool_name_mismatch"),
    ("arguments", {"service": "different"}, "tool_arguments_mismatch"),
    ("arguments", {"service": "sample-service", "metric": "http_5xx_rate"},
     "tool_arguments_mismatch"),
    ("arguments", {}, "tool_arguments_mismatch"),
    ("call_id", "call-rate", "tool_arguments_mismatch"),
])
def test_identity_and_wrong_metric_binding_remain_strict(sample, field, value, error):
    sample[0]["citations"][0][field] = value
    checked, report = evaluate(sample)
    assert error in report["issue_codes"]
    assert report["claims"][0]["evidence_support"] == "citation_invalid"
    assert not checked["result_trust"]["trusted"]


def test_tool_argument_equality_distinguishes_boolean_integer_and_float(sample):
    citation = sample[0]["citations"][0]
    sample[1][0]["arguments"]["selector"] = 1
    citation["arguments"]["selector"] = True
    assert "tool_arguments_mismatch" in codes(sample)
    citation["arguments"]["selector"] = 1.0
    assert "tool_arguments_mismatch" in codes(sample)
    citation["arguments"]["selector"] = 1
    assert not codes(sample)


@pytest.mark.parametrize("field,value", list(METADATA.items()) + [
    ("replay_collected_at", None), ("historical_query_started_at", None),
    ("historical_query_finished_at", None), ("input_path", ["known_gaps"]),
    ("trusted", True),
])
def test_v2_rejects_all_extra_metadata_even_if_true_or_null(sample, field, value):
    sample[0]["citations"][0][field] = value
    checked, report = evaluate(sample)
    assert "answer_contract_missing_or_invalid" in report["issue_codes"]
    assert checked["resolved_citations"] == {}
    assert report["claims"] == []


def test_v2_rejects_even_exactly_matching_record_metadata(sample):
    sample[0]["citations"][0].update(s1.provenance(sample[1][0]))
    sample[0]["citations"][0]["observation_id"] = sample[1][0]["observation_id"]
    assert "answer_contract_missing_or_invalid" in codes(sample)


@pytest.mark.parametrize("field", list(METADATA))
def test_v1_still_requires_each_legacy_provenance_field(sample, field):
    doc = deepcopy(CORPUS["cases"][-1]["answer"])
    if field == "observation_id":
        # Legacy hidden Runtime observation_id may be omitted, as before S3.
        doc["citations"][0][field] = "forged-observation"
        error = "observation_id_mismatch"
    else:
        del doc["citations"][0][field]
        error = "missing_provenance_field"
    assert error in codes((doc, sample[1], sample[2]))


@pytest.mark.parametrize("where,field", [
    ("answer", "unknown"), ("claim", "unknown"),
    ("claim", "kind"), ("claim", "use"), ("claim", "refs"),
])
def test_strict_answer_and_claim_contracts(sample, where, field):
    value = sample[0] if where == "answer" else sample[0]["claims"][0]
    if field == "unknown":
        value[field] = None
    else:
        del value[field]
    assert "answer_contract_missing_or_invalid" in codes(sample)


@pytest.mark.parametrize("field", ["field_path", "value"])
def test_field_value_still_requires_its_path_and_value(sample, field):
    del sample[0]["claims"][0][field]
    assert codes(sample) & {
        "field_path_missing", "field_declaration_requires_one_source_and_value",
    }


@pytest.mark.parametrize("duplicate,error", [
    ("citation", "duplicate_citation_id"), ("call", "duplicate_call_id"),
    ("claim", "duplicate_claim_id"), ("ref", "duplicate_claim_reference"),
    ("record", "ambiguous_runtime_records"),
    ("observation", "ambiguous_runtime_records"),
    ("call_identity", "ambiguous_runtime_records"),
])
def test_duplicate_identities_fail_closed(sample, duplicate, error):
    doc, records, _ = sample
    if duplicate in {"citation", "call"}:
        row = deepcopy(doc["citations"][0])
        if duplicate == "call":
            row["id"] = "another-citation"
        doc["citations"].append(row)
    elif duplicate == "claim":
        doc["claims"].append(deepcopy(doc["claims"][0]))
    elif duplicate == "ref":
        doc["claims"][0]["refs"] *= 2
    else:
        row = deepcopy(records[0])
        if duplicate == "observation":
            row["call_id"] = "another-call"
        if duplicate == "call_identity":
            row["observation_id"] = "another-observation"
        records.append(row)
    assert error in codes(sample)


def test_rejected_first_citation_cannot_hide_duplicate_id(sample):
    doc = sample[0]
    valid = deepcopy(doc["citations"][0])
    doc["citations"][0]["call_id"] = "unknown"
    doc["citations"].append(valid)
    checked, _ = evaluate(sample)
    assert "duplicate_citation_id" in codes(sample)
    assert not checked["resolved_citations"]


@pytest.mark.parametrize("path", [
    [], ["missing"], ["real_firing_observed", 0], ["evidence_state", -1],
    ["automatic_version_probe", "missing"], [True],
])
def test_bad_input_paths_do_not_create_tool_evidence(sample, path):
    sample[0]["citations"][0] = {
        "id": "total", "type": "input_context", "input_path": path,
    }
    assert codes(sample) & {
        "answer_contract_missing_or_invalid", "source_field_missing",
    }


@pytest.mark.parametrize("field,value", list(METADATA.items()) + [
    ("call_id", "call-total"), ("tool", "query_metrics"), ("arguments", {}),
])
def test_input_context_cannot_smuggle_identity_or_source(sample, field, value):
    sample[0]["citations"][0] = {
        "id": "total", "type": "input_context", "input_path": ["evidence_state"],
        field: value,
    }
    assert "answer_contract_missing_or_invalid" in codes(sample)


def test_input_context_remains_only_a_supplied_declaration(sample):
    doc, _, context = sample
    context["lookalike"] = deepcopy(sample[1][0]["result"])
    doc["citations"][0] = {
        "id": "total", "type": "input_context", "input_path": ["lookalike"],
    }
    checked, report = evaluate(sample)
    assert not report["issue_codes"]
    row = report["claims"][0]
    assert row["field_check"]["status"] == "field_verified"
    assert row["matched_call_ids"] == []
    assert row["actual_evidence_sources"][0]["authority_scope"] == (
        "supplied_input_declaration_only"
    )
    assert "observation_id" not in checked["resolved_citations"]["total"]
    doc["claims"][0]["use"] = "log_content"
    assert "logs_not_read" in codes(sample)


@pytest.mark.parametrize("field", [
    "source", "originals", "captured_at", "sanitized_projection", "evaluated_at",
])
def test_missing_metadata_stays_unknown_and_cannot_support_a_field(sample, field):
    result = sample[1][0]["result"]
    parent = result["captured_result"] if field == "evaluated_at" else result
    del parent[field]
    checked, report = evaluate(sample)
    assert not report["issue_codes"]
    source = checked["resolved_citations"]["total"]
    key = "historical_evidence_id" if field == "sanitized_projection" else field
    assert source[key] == ([] if field == "originals" else None)
    sample[0]["claims"][0]["field_path"] = (
        ["captured_result", field] if field == "evaluated_at" else [field]
    )
    sample[0]["claims"][0]["value"] = None
    assert "source_field_missing" in codes(sample)


@pytest.mark.parametrize("field", [
    "call_id", "observation_id", "name", "arguments", "result", "collected_at",
])
def test_incomplete_runtime_identity_or_payload_is_blocked(sample, field):
    del sample[1][0][field]
    assert codes(sample) & {"ambiguous_runtime_records", "invalid_runtime_record"}
    assert not evaluate(sample)[0]["resolved_citations"]


@pytest.mark.parametrize("field,value", [
    ("source", ["source-a", "source-b"]),
    ("originals", [{"missing_path": "x"}]),
    ("captured_result", None), ("sanitized_projection", []),
    ("captured_at", {}), ("status", True),
])
def test_malformed_or_ambiguous_source_metadata_is_reported(sample, field, value):
    sample[1][0]["result"][field] = value
    assert "invalid_runtime_record" in codes(sample)


@pytest.mark.parametrize("value,path,error", [
    (False, ["captured_result", "value"], "source_field_value_mismatch"),
    (13, ["captured_result", "value"], "source_field_value_mismatch"),
    (0, ["captured_result", "absent"], "source_field_missing"),
])
def test_literal_field_mismatch_or_absence_is_not_verified(sample, value, path, error):
    sample[0]["claims"][0].update(value=value, field_path=path)
    assert error in codes(sample)
    assert evaluate(sample)[1]["claims"][0]["field_check"]["status"] != (
        "field_verified"
    )


@pytest.mark.parametrize("use,error", [
    ("valid_zero", "no_valid_zero"), ("log_content", "logs_not_read"),
    ("fault_count", "independent_fault_count_not_established"),
    ("root_cause", "unverified_fact_in_historical_scope"),
    ("repair", "unverified_fact_in_historical_scope"),
    ("current_health", "unverified_fact_in_historical_scope"),
    ("current_recovery", "unverified_fact_in_historical_scope"),
])
def test_unsupported_explicit_facts_remain_blocked(sample, use, error):
    sample[0]["claims"][0]["use"] = use
    assert error in codes(sample)


def test_historical_no_data_zero_and_denied_logs_are_not_success(sample):
    doc, records, _ = sample
    records[0]["result"]["captured_result"].update(status="no_data", value=0.0)
    doc["claims"] = [{
        "id": "c", "text": "离线反例：有效零值。", "kind": "fact",
        "refs": ["total"], "use": "valid_zero",
    }]
    assert "no_valid_zero" in codes(sample)
    doc["citations"][0].update(
        call_id=records[2]["call_id"], tool=records[2]["name"],
        arguments=deepcopy(records[2]["arguments"]),
    )
    doc["claims"][0].update(text="离线反例：已读取日志正文。", use="log_content")
    checked, report = evaluate(sample)
    assert "logs_not_read" in report["issue_codes"]
    logs = checked["resolved_citations"]["total"]
    assert logs["source"] is None and logs["originals"] == []
    assert logs["captured_at"] is None and logs["captured_status"] is None
    assert logs["event_window"] == {k: records[2]["arguments"][k] for k in (
        "start", "end",
    )}


@pytest.mark.parametrize("use,index", [("valid_zero", 0), ("log_content", 3)])
def test_actual_historical_zero_and_log_body_still_meet_declared_requirements(
    sample,
    use,
    index,
):
    doc, records, _ = sample
    records[0]["result"]["captured_result"]["value"] = 0.0
    record = records[index]
    doc["citations"][0].update(
        call_id=record["call_id"], tool=record["name"],
        arguments=deepcopy(record["arguments"]),
    )
    doc["claims"] = [{
        "id": "c", "text": "离线人工样本：历史观测，非当前健康。",
        "kind": "fact", "refs": ["total"], "use": use,
    }]
    checked, report = evaluate(sample)
    assert not report["issue_codes"]
    assert report["claims"][0]["evidence_support"] == "declared_requirement_met_only"
    assert not report["acceptance_gate"]["automatic_acceptance_allowed"]
    assert not checked["result_trust"]["trusted"]


@pytest.mark.parametrize("text", [
    "证据封存证明服务冻结。", "数据库根因已确认。", "修复已经完成。",
    "当前服务健康。", "业务已经恢复。", "下面提供无关的旅行建议。",
])
def test_valid_structure_cannot_certify_mislabelled_or_off_topic_prose(sample, text):
    sample[0]["claims"][0].update(text=text, use="observation")
    checked, report = evaluate(sample)
    assert not report["issue_codes"]  # Open prose is not a keyword classifier.
    assert not report["claims"][0]["text_semantically_verified"]
    assert report["acceptance_gate"]["manual_semantic_review_required"]
    assert not report["acceptance_gate"]["automatic_acceptance_allowed"]
    assert checked["result_trust"]["semantic_status"] == "pending_review"
    assert not checked["result_trust"]["trusted"]


def test_purity_determinism_output_isolation_and_strict_json(sample, monkeypatch):
    before = deepcopy(sample)

    def forbidden(*args, **kwargs):
        raise AssertionError("pure_review_must_not_open_files_or_stores")

    with monkeypatch.context() as guard:
        for target in ("builtins.open", "io.open", "os.open", "sqlite3.connect"):
            guard.setattr(target, forbidden)
        checked, report = evaluate(sample)
        assert evaluate(sample) == (checked, report)
        assert json.loads(json.dumps(report, allow_nan=False)) == report
        assert json.loads(json.dumps(checked, allow_nan=False)) == checked
    checked["resolved_citations"]["total"]["value"]["status"] = "forged"
    checked["semantic_review"].append("forged")
    assert sample == before
    assert evaluate(sample)[1] == report


@pytest.mark.parametrize("answer", [
    '{"schema_version":"x","schema_version":"y"}',
    '{"value":NaN}', '{"value":1e999}', '[]', '{}',
])
def test_malformed_json_or_unknown_version_is_rejected(sample, answer):
    result = s1.review_answer(answer, sample[1], sample[2])
    assert any(i["code"] == "answer_contract_missing_or_invalid"
               for i in result["issues"])
    assert not result["result_trust"]["trusted"]


def test_capacity_measures_actual_v2_bytes_and_preserves_claims(sample):
    old = deepcopy(CORPUS["cases"][-1]["answer"])
    new = compact_copy(old)
    old_size = s2.measure_answer_capacity(encoded(old).decode("utf-8"))
    new_size = s2.measure_answer_capacity(encoded(new).decode("utf-8"))
    assert new_size["original_utf8_bytes"] < old_size["original_utf8_bytes"]
    assert new_size["compact_full_json_bytes"] < old_size["compact_full_json_bytes"]
    assert old_size["citation_field_counts"] == [14]
    assert new_size["citation_field_counts"] == [5]
    assert new["claims"] == old["claims"]
    assert new_size["exact_deepseek_tokens"] is None
    assert new_size["configured_max_output_tokens"] == 2048
    assert not new_size["budget_changed"]
    assert new_size["identity_only_projection_valid_under_declared_schema"]
    assert not old_size["identity_only_projection_valid_under_declared_schema"]


def test_new_version_keeps_budgets_and_rejects_v4_approval(prepared):
    plan, _, body = p65.read_prepared(prepared)
    assert plan["request_version"] == "p65_s1_semantics_v5_compact_refs_tools5"
    assert body["max_output_tokens"] == 2048
    assert plan["max_tool_executions"] == 5 and plan["max_model_requests"] == 3
    old = approval(prepared)  # Synthetic test data, never operator authorization.
    old["request_version"] = "p65_s1_semantics_v4_tools5"
    with pytest.raises(ValueError, match="new_explicit_authorization"):
        validate_approval(old, plan)


def test_resealed_v4_plan_stops_before_credentials_and_sdk(
    prepared,
    tmp_path,
    monkeypatch,
):
    old = approval(prepared)
    old["request_version"] = "p65_s1_semantics_v4_tools5"
    reseal_plan(prepared, lambda p: p.update(request_version=old["request_version"]))
    credential, sdk = Mock(), Mock()
    monkeypatch.setattr("scripts.preflight_real_model.credentials", credential)
    with pytest.raises(ValueError, match="scope_or_code_changed"):
        p65.run_prepared(
            prepared, tmp_path / "denied", live=True,
            authorization=old, sdk_factory=sdk,
        )
    credential.assert_not_called()
    sdk.assert_not_called()
    assert not (tmp_path / "denied").exists()


@pytest.fixture(scope="module")
def real_case():
    if not s1.DEFAULT_RUN.exists():
        pytest.skip("sealed operator artifacts absent; no Provider substitute")
    return s1.load_real_case()


def test_equivalent_handcrafted_v1_v2_on_independently_verified_records(real_case):
    original, records, context = real_case
    old = json.loads((FIXTURES / "p65_s1/human_answer.json").read_bytes())
    new = compact_copy(old)
    checked1, report1 = evaluate((old, records, context))
    checked2, report2 = evaluate((new, records, context))
    assert not report1["issue_codes"] and not report2["issue_codes"]
    assert checked1["resolved_citations"] == checked2["resolved_citations"]
    assert report1["claims"] == report2["claims"]
    assert report1["acceptance_gate"] == report2["acceptance_gate"]
    assert sum(r["field_check"]["status"] == "field_verified"
               for r in report2["claims"]) == 5
    assert sha(original.encode("utf-8")) == s1.ORIGINAL_ANSWER_SHA256
    original_check = s1.review_answer(original, records, context)
    assert sum(i["code"] == "historical_id_used_as_call_id"
               for i in original_check["issues"]) == 3


def test_compact_cli_still_authenticates_real_case_before_review(
    real_case,
    tmp_path,
    monkeypatch,
    capsys,
):
    old = json.loads((FIXTURES / "p65_s1/human_answer.json").read_bytes())
    path = tmp_path / "handcrafted-v2-not-provider.json"
    path.write_bytes(encoded(compact_copy(old)))
    monkeypatch.setattr("sys.argv", ["p65_claim_quality", "--answer", str(path)])
    s2.main()
    report = json.loads(capsys.readouterr().out)
    assert not report["issue_codes"]
    assert not report["acceptance_gate"]["automatic_acceptance_allowed"]
    assert report["source_authentication"] == (
        "caller_required_not_established_by_evaluator"
    )
