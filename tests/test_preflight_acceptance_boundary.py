"""Offline reproduction of legacy report acceptance and provenance boundaries.

These tests preserve the v1/v2 report contract. A passing Mock attestation tests
its binding only; it does not certify a reviewer's judgment or real-world truth.
All sources are synthetic in-memory data; no Runtime or database is started.
"""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from scripts import preflight_real_model as preflight
from scripts.preflight_evidence_refs import INCIDENT_CONTEXT_PREFIX
from test_worker_metric_analysis_growth import offline


@pytest.fixture(autouse=True)
def require_offline(offline, monkeypatch):
    """Reject accidental clients as well as the shared network guard."""
    import openai

    constructor = Mock(side_effect=AssertionError("model_client_forbidden"))
    monkeypatch.setattr(openai, "OpenAI", constructor)
    yield
    constructor.assert_not_called()


@pytest.fixture(params=[1, 2], ids=["v1", "v2"])
def source_case(request):
    records = [{
        "observation_id": "offline-version-result",
        "name": "get_running_version",
        "arguments": {"service": "archive-worker"},
        "collected_at": "2025-02-01T09:00:00+00:00",
        "identity": {"unverified_instance": "offline-instance"},
        "result": {"status": "no_data"},
    }]
    observations = [{
        "observation_id": records[0]["observation_id"],
        "name": records[0]["name"],
        "arguments": deepcopy(records[0]["arguments"]),
        "collected_at": records[0]["collected_at"],
        "status": "no_data",
        "use_for_current_state": False,
    }]
    rebuilt = {
        "recovery": "offline_fixture",
        "historical_observations": observations,
        "failed_or_missing_observations": [],
        "metric_analysis": None,
        "context_collected_at": "2025-02-01T09:00:01+00:00",
    }
    runtime_input = [{
        "role": "user",
        "content": (
            INCIDENT_CONTEXT_PREFIX
            + json.dumps({})
            + "\n"
            + json.dumps(rebuilt)
        ),
    }]
    source_snapshot = {
        "session_id": "offline-session",
        "checkpoint_id": "offline-checkpoint",
        "records": records,
    }
    external = {
        "runtime_input": runtime_input,
        "source_snapshot": source_snapshot,
    }
    index = preflight.build_evidence_index(
        records,
        observations,
        **external if request.param == 2 else {},
    )
    claims = [{
        "kind": "limitation",
        "text": "此历史版本工具未返回可认证的部署版本。",
        "observation_ids": [records[0]["observation_id"]],
    }]
    return {"index": index, "claims": claims, **external}


def validate_case(case, *, external):
    sources = {
        "runtime_input": case["runtime_input"],
        "source_snapshot": case["source_snapshot"],
    } if external else {}
    return preflight.validate_model_answer(
        json.dumps({"claims": case["claims"]}, ensure_ascii=False),
        case["index"],
        **sources,
    )


def mock_attestation(claims, facts, *, verdict="passed"):
    return {
        "reviewer": "offline-boundary-reviewer",
        "method": "independent_evidence_review",
        "claims_sha256": preflight.json_sha256(claims),
        "authoritative_facts_sha256": preflight.json_sha256(facts),
        "claim_reviews": [{
            "claim_index": number,
            "verdict": verdict,
            "reason": "合成夹具只核对审查声明绑定；不代表实际语义审查。",
            "observation_ids": deepcopy(claim["observation_ids"]),
        } for number, claim in enumerate(claims)],
    }


@pytest.mark.parametrize("external", [False, True], ids=["internal", "external"])
def test_legacy_overall_acceptance_does_not_require_external_rebuild(
    source_case,
    external,
):
    before = deepcopy(source_case)
    validation = validate_case(source_case, external=external)
    facts = preflight.build_authoritative_fact_view(source_case["index"])
    review = mock_attestation(source_case["claims"], facts)
    report = preflight.compose_preflight_report(
        facts,
        source_case["claims"],
        validation,
        runtime_completed=True,
        reasoning_review=review,
    )
    expected = (
        "passed_external_rebuild" if external
        else "trusted_index_internal_integrity_only"
    )
    assert validation["reference_source_check"] == expected
    assert report["model_interpretation"]["reference_source_check"] == expected
    assert report["acceptance"]["authoritative_fact_integrity"] == "passed"
    assert report["acceptance"]["overall_strict_acceptance"] == "passed"
    assert report["scope"] == "independent_preflight_only"
    assert validation["model_reasoning_correctness"] == "pending"
    assert source_case == before


def test_missing_source_check_marker_is_not_a_gate_in_the_legacy_report(source_case):
    validation = validate_case(source_case, external=False)
    del validation["reference_source_check"]
    facts = preflight.build_authoritative_fact_view(source_case["index"])
    report = preflight.compose_preflight_report(
        facts,
        source_case["claims"],
        validation,
        runtime_completed=True,
        reasoning_review=mock_attestation(source_case["claims"], facts),
    )
    assert report["model_interpretation"]["reference_source_check"] == "not_recorded"
    assert report["acceptance"]["overall_strict_acceptance"] == "passed"


@pytest.mark.parametrize("semantic", ["pending", "failed"])
def test_external_rebuild_does_not_override_unaccepted_reasoning(
    source_case,
    semantic,
):
    validation = validate_case(source_case, external=True)
    facts = preflight.build_authoritative_fact_view(source_case["index"])
    review = None if semantic == "pending" else mock_attestation(
        source_case["claims"], facts, verdict="failed",
    )
    report = preflight.compose_preflight_report(
        facts,
        source_case["claims"],
        validation,
        runtime_completed=True,
        reasoning_review=review,
    )
    assert validation["reference_source_check"] == "passed_external_rebuild"
    assert validation["output_structure"] == "passed"
    assert report["acceptance"]["model_reasoning_correctness"] == semantic
    assert report["acceptance"]["overall_strict_acceptance"] == semantic


def test_corrupted_fact_view_fails_even_after_external_rebuild(source_case):
    validation = validate_case(source_case, external=True)
    facts = preflight.build_authoritative_fact_view(source_case["index"])
    review = mock_attestation(source_case["claims"], facts)
    facts["sources"][0]["status"] = "ok"
    report = preflight.compose_preflight_report(
        facts,
        source_case["claims"],
        validation,
        runtime_completed=True,
    )
    assert validation["reference_source_check"] == "passed_external_rebuild"
    assert report["acceptance"]["authoritative_fact_integrity"] == "failed"
    assert report["acceptance"]["overall_strict_acceptance"] == "failed"
    with pytest.raises(ValueError, match="preflight_incomplete_reasoning_review"):
        preflight.compose_preflight_report(
            facts,
            source_case["claims"],
            validation,
            runtime_completed=True,
            reasoning_review=review,
        )


def test_external_source_mismatch_cannot_be_reported_as_validated(source_case):
    facts = preflight.build_authoritative_fact_view(source_case["index"])
    source_case["source_snapshot"]["records"][0]["result"]["status"] = "ok"
    validation = validate_case(source_case, external=True)
    report = preflight.compose_preflight_report(
        facts,
        source_case["claims"],
        validation,
        runtime_completed=True,
    )
    assert validation["reference_source_check"] == "failed"
    assert validation["output_structure"] == "failed"
    assert "invalid_reference_context" in validation["errors"]
    assert report["acceptance"]["overall_strict_acceptance"] == "failed"


@pytest.mark.parametrize(("mode", "expected"), [
    ("external", "passed"),
    ("internal", "pending"),
    ("missing_source_marker", "pending"),
    ("similar_source_marker", "pending"),
    ("pending_semantics", "pending"),
    ("failed_semantics", "failed"),
    ("failed_source", "failed"),
])
def test_explicit_external_acceptance_preserves_legacy_report_and_fails_closed(
    source_case,
    mode,
    expected,
):
    from scripts.preflight_support_constraints import external_evidence_acceptance

    validation = validate_case(source_case, external=mode != "internal")
    facts = preflight.build_authoritative_fact_view(source_case["index"])
    review = None if mode == "pending_semantics" else mock_attestation(
        source_case["claims"],
        facts,
        verdict="failed" if mode == "failed_semantics" else "passed",
    )
    report = preflight.compose_preflight_report(
        facts,
        source_case["claims"],
        validation,
        runtime_completed=True,
        reasoning_review=review,
    )
    if mode == "missing_source_marker":
        del report["model_interpretation"]["reference_source_check"]
    elif mode == "similar_source_marker":
        report["model_interpretation"]["reference_source_check"] = (
            "passed_external_rebuild_unverified"
        )
    elif mode == "failed_source":
        report["model_interpretation"]["reference_source_check"] = "failed"
    before = deepcopy(report)
    result = external_evidence_acceptance(report)
    assert result == {
        "scope": "independent_preflight_only",
        "reference_source_check": report["model_interpretation"].get(
            "reference_source_check", "not_recorded",
        ),
        "external_evidence_acceptance": expected,
        "production_state_authority": False,
    }
    assert report == before
    if mode in {"internal", "missing_source_marker", "similar_source_marker"}:
        assert report["acceptance"]["overall_strict_acceptance"] == "passed"


@pytest.mark.parametrize("scope", [
    None,
    "production",
    "independent_preflight_only_unverified",
])
def test_external_acceptance_rejects_other_or_similar_report_scopes(scope):
    from scripts.preflight_support_constraints import external_evidence_acceptance

    report = {
        "scope": scope,
        "model_interpretation": {
            "reference_source_check": "passed_external_rebuild",
        },
        "acceptance": {"overall_strict_acceptance": "passed"},
    }
    result = external_evidence_acceptance(report)
    assert result["external_evidence_acceptance"] == "failed"
    assert result["production_state_authority"] is False


@pytest.mark.parametrize("layer", [
    "runtime_investigation",
    "authoritative_fact_integrity",
    "model_output_structure",
    "model_reasoning_correctness",
])
def test_external_acceptance_checks_each_layer_despite_inconsistent_overall(layer):
    from scripts.preflight_support_constraints import external_evidence_acceptance

    report = {
        "scope": "independent_preflight_only",
        "model_interpretation": {
            "reference_source_check": "passed_external_rebuild",
        },
        "acceptance": {
            "runtime_investigation": "passed",
            "authoritative_fact_integrity": "passed",
            "model_output_structure": "passed",
            "model_reasoning_correctness": "passed",
            "overall_strict_acceptance": "passed",
        },
    }
    report["acceptance"][layer] = "pending"
    assert external_evidence_acceptance(report)["external_evidence_acceptance"] == (
        "pending"
    )
    report["acceptance"][layer] = "failed"
    assert external_evidence_acceptance(report)["external_evidence_acceptance"] == (
        "failed"
    )


@pytest.mark.parametrize(("text", "verdict", "reason"), [
    (
        "该历史版本工具未返回部署版本。",
        "passed",
        "唯一被引记录明确返回 no_data，文本仅陈述这条历史结果的缺失。",
    ),
    (
        "该历史版本工具已证明当前部署成功。",
        "failed",
        "唯一被引记录返回 no_data，不能认证当前部署身份或证明部署成功。",
    ),
])
def test_handwritten_semantic_oracle_separates_supported_and_unsupported_claims(
    source_case,
    text,
    verdict,
    reason,
):
    """Handwritten oracles test report binding, not an automatic semantic grader."""
    from scripts.preflight_support_constraints import external_evidence_acceptance

    source_case["claims"][0]["text"] = text
    validation = validate_case(source_case, external=True)
    facts = preflight.build_authoritative_fact_view(source_case["index"])
    pending = preflight.compose_preflight_report(
        facts,
        source_case["claims"],
        validation,
        runtime_completed=True,
    )
    assert validation["reference_source_check"] == "passed_external_rebuild"
    assert validation["output_structure"] == "passed"
    assert pending["acceptance"]["overall_strict_acceptance"] == "pending"
    assert external_evidence_acceptance(pending)["external_evidence_acceptance"] == (
        "pending"
    )
    review = mock_attestation(source_case["claims"], facts, verdict=verdict)
    review["method"] = "human"
    review["claim_reviews"][0]["reason"] = reason
    report = preflight.compose_preflight_report(
        facts,
        source_case["claims"],
        validation,
        runtime_completed=True,
        reasoning_review=review,
    )
    assert report["acceptance"]["overall_strict_acceptance"] == verdict
    assert report["model_interpretation"]["reasoning_review"] == review
    assert external_evidence_acceptance(report)["external_evidence_acceptance"] == (
        verdict
    )
