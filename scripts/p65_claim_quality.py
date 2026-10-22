"""P6.5-S2 offline advice about declarations, never natural-language truth.

Records and input context must come from independently verified sources (the
S1 sealed loader is used by the CLI). Recomputing S1 binds supplied checks to
these inputs; it does not authenticate arbitrary caller-supplied evidence.
No production trust, approval, budget or Runtime state is read or written here.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path

from pydantic import ValidationError

from scripts import p65_answer_review as s1
from scripts.p65_model_boundary import MAX_OUTPUT_TOKENS
from scripts.preflight_evidence_refs import _parse_json, json_sha256


REPORT_VERSION = "p65_s2_quality_v1"
SUPPORT_ERRORS = {
    "unverified_fact_in_historical_scope",
    "independent_fault_count_not_established", "logs_not_read", "no_valid_zero",
}


def _field_check(
    claim,
    sources,
    citation_invalid,
):
    """Only an explicit JSON path/value is checked, independently of prose/use."""
    declared = (
        claim.use == "field_value" or claim.field_path is not None
        or "value" in claim.model_fields_set
    )
    result = {
        "status": "not_declared", "field_path": claim.field_path,
        "declared_value": deepcopy(claim.value), "actual_value_present": False,
    }
    if not declared:
        return result, []
    if citation_invalid:
        result["status"] = "not_checked_invalid_citation"
        return result, []
    if len(sources) != 1 or "value" not in claim.model_fields_set:
        result["status"] = "insufficient_evidence"
        return result, ["field_declaration_requires_one_source_and_value"]
    try:
        value = s1.select(sources[0]["value"], claim.field_path)
    except ValueError as exc:
        result["status"] = "insufficient_evidence"
        return result, [str(exc)]
    result.update(actual_value_present=True, actual_value=deepcopy(value))
    if json_sha256(value) != json_sha256(claim.value):
        result["status"] = "contradicted_by_field"
        return result, ["source_field_value_mismatch"]
    result["status"] = "field_verified"
    return result, []


def decide_acceptance(report):
    """S2 has no automatic acceptance path, even for an empty/forged report.

    This function issues offline advice only; it cannot record formal review
    or change the production ResultTrust projection. Validate the decision
    envelope only; this does not authenticate the report's claims/evidence.
    """
    blockers = set()
    if not isinstance(report, dict):
        blockers.add("quality_report_must_be_object")
        report = {}
    issue_codes = report.get("issue_codes")
    if isinstance(issue_codes, list) and all(
        isinstance(code, str) and code.strip() for code in issue_codes
    ):
        blockers.update(issue_codes)
    else:
        blockers.add("quality_report_issue_codes_missing_or_invalid")
    if report.get("report_version") != REPORT_VERSION:
        blockers.add("quality_report_missing_or_invalid")
    if report.get("s1_declaration_check") != "passed":
        blockers.add("s1_check_not_passed")
    blockers.add("independent_semantic_review_not_performed")
    return {
        "automatic_acceptance_allowed": False,
        "manual_semantic_review_required": True,
        "decision_origin": "offline_advisory_only",
        "formal_approval": "not_performed",
        "blocking_reasons": sorted(blockers),
    }


def evaluate_claim_quality(
    answer,
    records,
    input_context,
    s1_result,
):
    """Evaluate exact declared relations, with mandatory semantic review.

    kind/use select a declared relationship to check, never authority. In
    particular observation + 'confirmed root cause' text is NOT understood or
    certified. Even field_verified only verifies that one path/value relation.
    Caller labels, model confidence and Provider origin cannot grant trust.
    """
    checked = s1.review_answer(answer, records, input_context)
    issues = deepcopy(checked["issues"])
    try:
        s1_matches = json_sha256(s1_result) == json_sha256(checked)
    except (ValueError, TypeError):
        s1_matches = False
    if not s1_matches:
        issues.append({
            "code": "s1_result_not_bound_to_inputs", "location": "s1_result",
            "detail": "supplied S1 result differs from offline recomputation",
        })
    rows = []
    try:
        document = s1.Answer.model_validate(_parse_json(answer))
    except (ValueError, TypeError, ValidationError):
        document = None
    if document is not None:
        resolved = checked["resolved_citations"]
        global_invalid = any(
            i["code"] == "ambiguous_runtime_records" for i in issues
        )
        for claim in document.claims:
            locations = {f"claims/{claim.id}"} | {
                f"citations/{ref}" for ref in claim.refs
            }
            relevant = [i for i in issues if i["location"] in locations]
            codes = {i["code"] for i in relevant}
            citation_invalid = global_invalid or any(
                i["location"].startswith("citations/")
                or i["code"] in {"unknown_citation_id", "duplicate_claim_reference"}
                for i in relevant
            )
            if global_invalid:
                codes.add("ambiguous_runtime_records")
            sources = [resolved[ref] for ref in claim.refs if ref in resolved]
            field, errors = _field_check(claim, sources, citation_invalid)
            codes.update(errors)
            for code in errors:
                entry = {
                    "code": code, "location": f"claims/{claim.id}", "detail": "",
                }
                if not any(
                    i["code"] == code and i["location"] == entry["location"]
                    for i in issues
                ):
                    issues.append(entry)
            states = ["semantic_review_required"]
            if citation_invalid:
                states.append("citation_invalid")
            if field["status"] in {
                "field_verified", "contradicted_by_field", "insufficient_evidence",
            }:
                states.append(field["status"])
            if codes & SUPPORT_ERRORS or "fact_without_reference" in codes:
                states.append("insufficient_evidence")
            # Sources are the actual S1-resolved values, never the model's
            # claimed provenance. An invalid match stays explicitly unverified.
            evidence = [{
                **deepcopy(source),
                "citation_id": ref,
                "relationship_verified": not citation_invalid,
                "authority_scope": (
                    "supplied_input_declaration_only"
                    if source["type"] == "input_context"
                    else "persisted_historical_tool_result_only"
                ),
            } for ref in claim.refs if (source := resolved.get(ref)) is not None]
            if citation_invalid:
                support = "citation_invalid"
            elif field["status"] == "contradicted_by_field":
                support = "contradicted_by_field"
            elif "insufficient_evidence" in states:
                support = "insufficient_evidence"
            elif field["status"] == "field_verified":
                support = "field_relation_only"
            elif claim.kind == "fact" and claim.use in {"valid_zero", "log_content"}:
                support = "declared_requirement_met_only"
            else:
                support = "not_semantically_evaluated"
            rows.append({
                "claim_id": claim.id, "text": claim.text, "kind": claim.kind,
                "use": claim.use, "citation_ids": list(claim.refs),
                "matched_call_ids": sorted({
                    source["call_id"] for source in sources if "call_id" in source
                }),
                "actual_evidence_sources": evidence,
                "field_check": field, "evidence_support": support,
                "evaluation_states": sorted(set(states)),
                "issue_codes": sorted(codes),
                "manual_semantic_review_required": True,
                "text_semantically_verified": False,
            })
    report = {
        "report_version": REPORT_VERSION,
        "decision_origin": "offline_advisory_only",
        "answer_sha256": checked["answer_sha256"],
        "records_sha256": json_sha256(records),
        "input_context_sha256": json_sha256(input_context),
        "source_authentication": "caller_required_not_established_by_evaluator",
        "s1_declaration_check": checked["declaration_check"],
        "s1_result_matches_recomputation": s1_matches,
        "claims": rows, "issues": issues,
        "issue_codes": sorted({i["code"] for i in issues}),
        "semantic_scope": "no_general_natural_language_judgment",
        "runtime_state_written": False,
    }
    report["acceptance_gate"] = decide_acceptance(report)
    return report


def measure_answer_capacity(answer):
    """Byte sizes only. Identity-only projection is NOT valid under current v4.

    No same-model tokenizer is implemented or claimed; Provider usage from a
    different answer cannot establish this answer's exact token count.
    """
    document = s1.Answer.model_validate(_parse_json(answer))
    full = _parse_json(answer)
    minimal = deepcopy(full)
    for citation in minimal["citations"]:
        keep = {"id", "type", "call_id", "tool", "arguments", "input_path"}
        for key in list(citation):
            if key not in keep:
                del citation[key]

    def byte_size(value):
        return len(json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8"))

    return {
        "original_utf8_bytes": len(answer.encode("utf-8")),
        "compact_full_json_bytes": byte_size(full),
        "citation_count": len(document.citations),
        "claim_count": len(document.claims),
        "full_citations_compact_bytes": byte_size(full["citations"]),
        "identity_only_citations_compact_bytes": byte_size(minimal["citations"]),
        "identity_only_answer_compact_bytes": byte_size(minimal),
        "identity_only_projection_valid_under_current_v4": False,
        "exact_deepseek_tokens": None,
        "exact_token_basis": "no_verified_same_model_tokenizer_or_usage_for_sample",
        "configured_max_output_tokens": MAX_OUTPUT_TOKENS,
        "budget_changed": False,
        "capacity_conclusion": "risk_requires_separate_token_validation",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=s1.DEFAULT_RUN)
    parser.add_argument("--answer", type=Path)
    args = parser.parse_args()
    answer, records, context = s1.load_real_case(args.run_dir)
    if args.answer:
        answer = args.answer.read_bytes().decode("utf-8")
    checked = s1.review_answer(answer, records, context)
    report = evaluate_claim_quality(answer, records, context, checked)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
