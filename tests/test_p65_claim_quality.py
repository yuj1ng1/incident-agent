"""S2 declaration checks and mandatory gate; no Provider/approval execution."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts import p65_answer_review as s1
from scripts import p65_claim_quality as s2


FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = json.loads((FIXTURES / "p65_s2/corpus.json").read_bytes())


def evaluate(
    document,
    records=None,
    context=None,
):
    records = CORPUS["records"] if records is None else records
    context = CORPUS["input_context"] if context is None else context
    answer = json.dumps(document, ensure_ascii=False)
    checked = s1.review_answer(answer, records, context)
    return s2.evaluate_claim_quality(answer, records, context, checked)


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda c: c["id"])
def test_labeled_corpus(case):
    before = deepcopy(case)
    report = evaluate(case["answer"])
    assert set(report["issue_codes"]) == set(case["expected_deterministic_codes"])
    assert report["acceptance_gate"]["automatic_acceptance_allowed"] is False
    assert report["acceptance_gate"]["manual_semantic_review_required"] is True
    assert report["acceptance_gate"]["decision_origin"] == "offline_advisory_only"
    assert report["acceptance_gate"]["formal_approval"] == "not_performed"
    assert report["runtime_state_written"] is False
    assert report["s1_result_matches_recomputation"]
    assert len(report["claims"]) == len(case["answer"]["claims"])
    for row, original in zip(report["claims"], case["answer"]["claims"], strict=True):
        assert row["claim_id"] == original["id"]
        assert row["text"] == original["text"]
        assert row["kind"] == original["kind"] and row["use"] == original["use"]
        assert row["citation_ids"] == original["refs"]
        assert row["manual_semantic_review_required"]
        assert not row["text_semantically_verified"]
        assert "semantic_review_required" in row["evaluation_states"]
    if case["rule_scope"] == "manual_label_only_open_semantics":
        # These preset annotations are deliberately NOT classifier detections.
        assert case["manual_expected_problems"]
        assert not report["issue_codes"]
        assert report["claims"][0]["evidence_support"] == "not_semantically_evaluated"
    assert case == before
    json.dumps(report, allow_nan=False)


def test_fixture_origin_is_explicit_and_not_a_signature():
    assert not CORPUS["provider_answers"] and not CORPUS["formal_human_approval"]
    assert "not_independent_human_review" in CORPUS["labels_origin"]
    for case in CORPUS["cases"]:
        assert case["sample_origin"] == CORPUS["sample_origin"]
        for claim in case["answer"]["claims"]:
            assert "非Provider回答、非正式人工批准" in claim["text"]


def test_actual_sources_are_not_replaced_by_claimed_provenance():
    case = next(c for c in CORPUS["cases"] if c["id"] == "A_other_metric_source")
    row = evaluate(case["answer"])["claims"][0]
    assert "citation_invalid" in row["evaluation_states"]
    assert row["field_check"]["status"] == "not_checked_invalid_citation"
    assert row["matched_call_ids"] == ["call-total"]
    actual = row["actual_evidence_sources"][0]
    assert actual["historical_evidence_id"] == (
        CORPUS["records"][0]["result"]["sanitized_projection"]["observation_id"]
    )
    assert not actual["relationship_verified"]


def test_field_verified_is_not_semantic_verification_and_does_not_mutate_inputs():
    case = next(c for c in CORPUS["cases"] if c["id"] == "D_complete_legal")
    before = deepcopy(CORPUS)
    report = evaluate(case["answer"])
    row = report["claims"][0]
    assert row["field_check"]["status"] == "field_verified"
    assert row["field_check"]["actual_value"] == 13.0
    assert row["evidence_support"] == "field_relation_only"
    assert not report["issue_codes"]
    report["claims"][0]["actual_evidence_sources"][0]["value"]["status"] = "forged"
    assert CORPUS == before


@pytest.mark.parametrize("text", [
    "数据库根因已确认。", "已恢复健康。", "毫无因果证据。", "普通无关键词陈述。",
    "The service was definitely frozen and deployment caused all failures.",
])
def test_prose_is_not_classified_by_keywords_or_kind(text):
    doc = deepcopy(CORPUS["cases"][-1]["answer"])
    doc["claims"][0]["text"] = text
    report = evaluate(doc)
    assert not report["issue_codes"]
    assert report["claims"][0]["field_check"]["status"] == "field_verified"
    assert not report["claims"][0]["text_semantically_verified"]
    assert not report["acceptance_gate"]["automatic_acceptance_allowed"]


@pytest.mark.parametrize("value", [False, None, 0, 27.5, "13.0"])
def test_unseen_literal_values_and_types_are_compared_exactly(value):
    doc = deepcopy(CORPUS["cases"][-1]["answer"])
    doc["claims"][0]["value"] = value
    row = evaluate(doc)["claims"][0]
    assert row["field_check"]["status"] == "contradicted_by_field"
    assert row["evidence_support"] == "contradicted_by_field"


def test_unseen_service_metric_ids_and_values_need_no_special_case():
    records = deepcopy(CORPUS["records"][:1])
    records[0].update(call_id="different-call", observation_id="different-observation")
    records[0]["arguments"] = {"service": "another-service", "metric": "other_count"}
    records[0]["result"]["captured_result"]["value"] = 97.25
    doc = deepcopy(CORPUS["cases"][-1]["answer"])
    doc["citations"][0].update(
        call_id="different-call", arguments=deepcopy(records[0]["arguments"]),
    )
    doc["claims"][0]["value"] = 97.25
    report = evaluate(doc, records)
    assert not report["issue_codes"]
    assert report["claims"][0]["field_check"]["status"] == "field_verified"


def test_fields_are_checked_even_when_use_is_observation():
    doc = deepcopy(CORPUS["cases"][-1]["answer"])
    doc["claims"][0].update(use="observation", value=98.0)
    report = evaluate(doc)
    assert report["s1_declaration_check"] == "passed"
    assert "source_field_value_mismatch" in report["issue_codes"]


@pytest.mark.parametrize("change", ["answer", "records", "context", "s1"])
def test_supplied_s1_result_must_be_bound_to_all_inputs(change):
    doc = deepcopy(CORPUS["cases"][-1]["answer"])
    records, context = deepcopy(CORPUS["records"]), deepcopy(CORPUS["input_context"])
    answer = json.dumps(doc, ensure_ascii=False)
    checked = s1.review_answer(answer, records, context)
    if change == "answer":
        doc["claims"][0]["text"] += "改写"
        answer = json.dumps(doc, ensure_ascii=False)
    elif change == "records":
        records[0]["result"]["captured_result"]["value"] = 72.0
    elif change == "context":
        # S1 only retains cited context; cite it before testing binding.
        doc["citations"].append({
            "id": "input", "type": "input_context", "input_path": ["evidence_state"],
        })
        doc["claims"].append({
            "id": "input", "text": "人工背景", "kind": "limitation",
            "use": "observation", "refs": ["input"],
        })
        answer = json.dumps(doc, ensure_ascii=False)
        checked = s1.review_answer(answer, records, context)
        context["evidence_state"] = "changed"
    else:
        checked["declaration_check"] = "invented-passed"
    report = s2.evaluate_claim_quality(answer, records, context, checked)
    assert "s1_result_not_bound_to_inputs" in report["issue_codes"]
    assert not report["s1_result_matches_recomputation"]
    assert not report["acceptance_gate"]["automatic_acceptance_allowed"]


@pytest.mark.parametrize("answer", ["{}", "[]", '{"value":NaN}', "not json"])
def test_invalid_contract_fails_closed(answer):
    checked = s1.review_answer(answer, CORPUS["records"], CORPUS["input_context"])
    report = s2.evaluate_claim_quality(
        answer, CORPUS["records"], CORPUS["input_context"], checked,
    )
    assert report["s1_declaration_check"] == "failed"
    assert report["claims"] == []
    assert not report["acceptance_gate"]["automatic_acceptance_allowed"]


@pytest.mark.parametrize("report", [
    None, [], {},
    {"report_version": s2.REPORT_VERSION, "s1_declaration_check": "passed"},
    {"trusted": True, "confidence": 1.0, "semantic_status": "accepted"},
])
def test_gate_never_grants_authority_to_supplied_or_missing_labels(report):
    decision = s2.decide_acceptance(report)
    assert not decision["automatic_acceptance_allowed"]
    assert decision["manual_semantic_review_required"]
    assert decision["formal_approval"] == "not_performed"


@pytest.fixture
def check_gate(monkeypatch):
    """Every gate case checks purity, determinism and the fixed authority limits."""
    def check(report):
        before = deepcopy(report)

        def forbidden(*args, **kwargs):
            pytest.fail("acceptance_gate_must_not_open_files_or_stores")

        with monkeypatch.context() as guard:
            for target in (
                "incident_store.IncidentStore.__init__",
                "investigation.store.RuntimeStore.__init__",
                "remediation.store.RemediationStore.__init__",
                "sqlite3.connect",
                "builtins.open",
                "io.open",
                "os.open",
            ):
                guard.setattr(target, forbidden)
            decision = s2.decide_acceptance(report)
            assert s2.decide_acceptance(report) == decision
        assert report == before
        assert decision == {
            "automatic_acceptance_allowed": False,
            "manual_semantic_review_required": True,
            "decision_origin": "offline_advisory_only",
            "formal_approval": "not_performed",
            "blocking_reasons": sorted(set(decision["blocking_reasons"])),
        }
        assert json.loads(json.dumps(decision, allow_nan=False)) == decision
        assert "independent_semantic_review_not_performed" in (
            decision["blocking_reasons"]
        )
        return decision

    return check


@pytest.mark.parametrize("report", [None, [], "error", 123, 1.5, True])
def test_gate_rejects_non_object_reports(report, check_gate):
    decision = check_gate(report)
    assert "quality_report_must_be_object" in decision["blocking_reasons"]


@pytest.mark.parametrize("report", [
    {},
    {"trusted": True, "semantic_status": "accepted"},
])
def test_gate_reports_all_missing_decision_fields(report, check_gate):
    assert check_gate(report)["blocking_reasons"] == [
        "independent_semantic_review_not_performed",
        "quality_report_issue_codes_missing_or_invalid",
        "quality_report_missing_or_invalid",
        "s1_check_not_passed",
    ]


@pytest.mark.parametrize("issue_codes", [
    None, "error", 123, 1.5, True, {}, {"error": True},
    [{}], [[]], [None], [123], [False], ["valid_error", None],
    ["valid_error", {}], ["valid_error", []], [""], [" \t\n"],
    ["valid_error", ""], (), {"valid_error"},
])
def test_gate_rejects_invalid_issue_codes_as_a_whole(issue_codes, check_gate):
    report = {
        "report_version": s2.REPORT_VERSION,
        "s1_declaration_check": "passed",
        "issue_codes": issue_codes,
    }
    assert check_gate(report)["blocking_reasons"] == [
        "independent_semantic_review_not_performed",
        "quality_report_issue_codes_missing_or_invalid",
    ]


@pytest.mark.parametrize("field,value,reason", [
    ("report_version", None, "quality_report_missing_or_invalid"),
    ("report_version", "wrong_version", "quality_report_missing_or_invalid"),
    ("report_version", [], "quality_report_missing_or_invalid"),
    ("report_version", {}, "quality_report_missing_or_invalid"),
    ("s1_declaration_check", None, "s1_check_not_passed"),
    ("s1_declaration_check", "failed", "s1_check_not_passed"),
    ("s1_declaration_check", True, "s1_check_not_passed"),
    ("s1_declaration_check", [], "s1_check_not_passed"),
    ("s1_declaration_check", {}, "s1_check_not_passed"),
])
def test_gate_rejects_invalid_version_or_s1_check(
    field,
    value,
    reason,
    check_gate,
):
    report = {
        "report_version": s2.REPORT_VERSION,
        "s1_declaration_check": "passed",
        "issue_codes": [],
        field: value,
    }
    assert check_gate(report)["blocking_reasons"] == sorted([
        "independent_semantic_review_not_performed", reason,
    ])


@pytest.mark.parametrize("field,reason", [
    ("report_version", "quality_report_missing_or_invalid"),
    ("s1_declaration_check", "s1_check_not_passed"),
    ("issue_codes", "quality_report_issue_codes_missing_or_invalid"),
])
def test_gate_rejects_missing_decision_fields(field, reason, check_gate):
    report = {
        "report_version": s2.REPORT_VERSION,
        "s1_declaration_check": "passed",
        "issue_codes": [],
    }
    del report[field]
    assert check_gate(report)["blocking_reasons"] == sorted([
        "independent_semantic_review_not_performed", reason,
    ])


@pytest.mark.parametrize("issue_codes", [
    [], ["valid_error"], ["z_error", "a_error", "z_error"],
])
def test_gate_preserves_valid_codes_without_a_closed_allowlist(issue_codes, check_gate):
    report = {
        "report_version": s2.REPORT_VERSION,
        "s1_declaration_check": "passed",
        "issue_codes": issue_codes,
    }
    assert check_gate(report)["blocking_reasons"] == sorted({
        *issue_codes, "independent_semantic_review_not_performed",
    })


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda c: c["id"])
def test_gate_keeps_original_complete_s2_report_decisions(case, check_gate):
    report = evaluate(case["answer"])
    expected = set(report["issue_codes"])
    if report["s1_declaration_check"] != "passed":
        expected.add("s1_check_not_passed")
    expected.add("independent_semantic_review_not_performed")
    assert check_gate(report)["blocking_reasons"] == sorted(expected)


def test_gate_ignores_forged_authority_in_a_complete_valid_report(check_gate):
    report = evaluate(CORPUS["cases"][-1]["answer"])
    report.update(
        trusted=True,
        semantic_status="accepted",
        automatic_acceptance_allowed=True,
        manual_semantic_review_required=False,
        decision_origin="formal_human_review",
        formal_approval="performed",
        acceptance_gate={"automatic_acceptance_allowed": True},
    )
    assert check_gate(report)["blocking_reasons"] == [
        "independent_semantic_review_not_performed",
    ]


def test_unknown_commit_is_field_verified_only_as_input_declaration():
    doc = next(c["answer"] for c in CORPUS["cases"] if c["id"] == "D_unknown_commit")
    row = evaluate(doc)["claims"][0]
    assert row["matched_call_ids"] == []
    assert row["field_check"]["actual_value_present"]
    assert row["field_check"]["actual_value"] is None
    assert row["actual_evidence_sources"][0]["authority_scope"] == (
        "supplied_input_declaration_only"
    )


def test_s1_binding_distinguishes_false_from_numeric_zero():
    answer = json.dumps(CORPUS["cases"][-1]["answer"], ensure_ascii=False)
    checked = s1.review_answer(answer, CORPUS["records"], CORPUS["input_context"])
    checked["runtime_state_written"] = 0
    report = s2.evaluate_claim_quality(
        answer, CORPUS["records"], CORPUS["input_context"], checked,
    )
    assert not report["s1_result_matches_recomputation"]


@pytest.mark.parametrize("use,index,key", [
    ("valid_zero", 0, "total"), ("log_content", 3, "history_logs"),
])
def test_valid_historical_zero_and_log_content_are_not_false_positives(
    use,
    index,
    key,
):
    records = deepcopy(CORPUS["records"])
    records[0]["result"]["captured_result"]["value"] = 0.0
    template = next(
        c["answer"] for c in CORPUS["cases"]
        if c["id"] == ("D_historical_value" if index == 0 else "C_late_logs_as_now")
    )
    doc = deepcopy(template)
    doc["claims"] = [{
        "id": "c", "text": "人工样本，非Provider回答、非正式人工批准：历史观测。",
        "kind": "fact", "refs": [key], "use": use,
    }]
    report = evaluate(doc, records)
    assert not report["issue_codes"]
    assert report["claims"][0]["evidence_support"] == "declared_requirement_met_only"
    assert not report["acceptance_gate"]["automatic_acceptance_allowed"]


@pytest.mark.parametrize("change", ["duplicate_record", "missing_path", "duplicate_id"])
def test_ambiguity_and_missing_fields_cannot_be_field_verified(change):
    records = deepcopy(CORPUS["records"])
    doc = deepcopy(CORPUS["cases"][-1]["answer"])
    if change == "duplicate_record":
        records.append(deepcopy(records[0]))
    elif change == "missing_path":
        doc["claims"][0]["field_path"] = ["missing"]
    else:
        doc["citations"].append(deepcopy(doc["citations"][0]))
    report = evaluate(doc, records)
    assert report["claims"][0]["field_check"]["status"] != "field_verified"
    assert report["issue_codes"]
    assert not report["acceptance_gate"]["automatic_acceptance_allowed"]


def test_capacity_reports_bytes_without_inventing_deepseek_token_counts():
    answer = (FIXTURES / "p65_s1/human_answer.json").read_bytes().decode("utf-8")
    report = s2.measure_answer_capacity(answer)
    assert report["original_utf8_bytes"] == len(answer.encode("utf-8"))
    assert report["citation_count"] == 6 and report["claim_count"] == 8
    assert (
        report["identity_only_answer_compact_bytes"] < report["compact_full_json_bytes"]
    )
    assert report["exact_deepseek_tokens"] is None
    assert not report["identity_only_projection_valid_under_current_v4"]
    assert not report["budget_changed"]
    assert report["configured_max_output_tokens"] == 2048


def test_smaller_identity_only_projection_is_not_current_v4_compliant():
    doc = deepcopy(CORPUS["cases"][-1]["answer"])
    identity = {"id", "type", "call_id", "tool", "arguments", "input_path"}
    doc["citations"] = [
        {key: value for key, value in citation.items() if key in identity}
        for citation in doc["citations"]
    ]
    report = evaluate(doc)
    assert "missing_provenance_field" in report["issue_codes"]
    assert not report["acceptance_gate"]["automatic_acceptance_allowed"]


@pytest.fixture(scope="module")
def real_case():
    if not s1.DEFAULT_RUN.exists():
        pytest.skip("sealed operator artifacts absent; no synthetic substitute")
    return s1.load_real_case()


def test_sealed_real_failure_is_advisory_only(real_case):
    answer, records, context = real_case
    checked = s1.review_answer(answer, records, context)
    report = s2.evaluate_claim_quality(answer, records, context, checked)
    assert report["answer_sha256"] == s1.ORIGINAL_ANSWER_SHA256
    assert "historical_id_used_as_call_id" in report["issue_codes"]
    assert not report["acceptance_gate"]["automatic_acceptance_allowed"]
    assert checked["result_trust"]["trusted"] is False


def test_s1_human_sample_remains_legal_and_untrusted(real_case):
    _, records, context = real_case
    doc = json.loads((FIXTURES / "p65_s1/human_answer.json").read_bytes())
    report = evaluate(doc, records, context)
    assert not report["issue_codes"]
    verified = [
        r for r in report["claims"] if r["field_check"]["status"] == "field_verified"
    ]
    assert len(verified) == 5
    assert not report["acceptance_gate"]["automatic_acceptance_allowed"]


def test_cli_uses_verified_loader_and_prints_only_advice(
    real_case,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr("sys.argv", ["p65_claim_quality"])
    s2.main()
    report = json.loads(capsys.readouterr().out)
    assert report["answer_sha256"] == s1.ORIGINAL_ANSWER_SHA256
    assert report["runtime_state_written"] is False
    assert not report["acceptance_gate"]["automatic_acceptance_allowed"]
