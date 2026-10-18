"""Offline support declarations, separate from the unchanged v1/v2 claims.

Callers supply declarations; no facts are extracted from claim text or kind.
Passing checks means only that the declared structural relation holds against
the supplied external inputs. Neither hashes nor a declaration authenticate
those inputs, cover every assertion in a sentence, or certify semantic support.
There is no I/O, execution authority, or production-state integration here.
Use support checks after validate_model_answer; they do not repeat or replace
the v1/v2 claims contract validator or the independent semantic review.
"""
from datetime import datetime

from scripts.preflight_evidence_refs import json_sha256, verify_reference_context


def _result(errors):
    return {
        "mechanical_check": "failed" if errors else "passed",
        "semantic_review": "required",
        "declaration_origin": "caller_supplied_not_extracted",
        "errors": errors,
    }


def _declared_source(
    context,
    claim,
    declaration,
    records,
    runtime_input,
    source_snapshot,
):
    if any(value is None for value in (records, runtime_input, source_snapshot)):
        raise ValueError("external_inputs_required")
    verify_reference_context(
        context,
        [record["observation_id"] for record in records],
        records=records,
        runtime_input=runtime_input,
        source_snapshot=source_snapshot,
    )
    if declaration["claim_sha256"] != json_sha256(claim):
        raise ValueError("claim_binding_mismatch")
    source = next((
        item for item in context["context_sources"]
        if item["reference_id"] == declaration["reference_id"]
    ), None)
    if source is None:
        raise ValueError("declared_source_missing")
    if {"type": source["source_type"], "id": source["reference_id"]} not in (
        claim.get("context_refs", [])
    ):
        raise ValueError("declared_source_not_cited")
    return source


def check_field_support(
    context,
    claim,
    declaration,
    *,
    records,
    runtime_input,
    source_snapshot,
):
    """Check an exact cited instance/path/value, never the whole claim's truth.

    Paths are JSON key/index arrays, relative to a context source's value.
    A present null is a value, not a missing field or verified identity.
    Identity catalogs describe persisted metadata, never current deployment.
    """
    try:
        if not isinstance(declaration, dict) or set(declaration) != {
            "claim_sha256", "reference_id", "field_path", "expected_value", "use",
        }:
            raise ValueError("invalid_field_declaration")
        source = _declared_source(
            context, claim, declaration, records, runtime_input, source_snapshot,
        )
        use = declaration["use"]
        if use not in ("persisted_field", "historical_identity_association"):
            raise ValueError("unsupported_field_use")
        if use == "historical_identity_association" and (
            source["source_type"] != "identity_context"
        ):
            raise ValueError("identity_source_required")
        path = declaration["field_path"]
        if not isinstance(path, list) or not path or any(
            type(part) not in (str, int) for part in path
        ):
            raise ValueError("invalid_field_path")
        value = source["value"]
        for part in path:
            if isinstance(value, dict) and type(part) is str and part in value:
                value = value[part]
            elif isinstance(value, list) and type(part) is int and (
                0 <= part < len(value)
            ):
                value = value[part]
            else:
                raise ValueError("source_field_missing")
        # JSON equality must distinguish false/0 and nested equivalents too.
        if json_sha256(value) != json_sha256(declaration["expected_value"]):
            raise ValueError("source_field_value_mismatch")
        return {
            **_result([]),
            "claim_sha256": declaration["claim_sha256"],
            "declaration_sha256": json_sha256(declaration),
            "catalog_sha256": context["catalog_sha256"],
            "reference_id": source["reference_id"],
            "source_location": source["source_location"],
            "field_path": list(path),
            "support_scope": use,
            "current_deployment_verified": False,
        }
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        code = str(exc) if isinstance(exc, ValueError) else "invalid_support_input"
        return _result([code])


def check_collection_absence(
    context,
    claim,
    declaration,
    *,
    records,
    runtime_input,
    source_snapshot,
):
    """Check absence of saved records matching a tool/argument subset.

    Tool names and argument values match exactly; no aliases, metric families,
    or capability-to-execution inference. A saved no_data/error result is still
    a saved result. Snapshot membership is not this run's execution ledger.
    """
    try:
        if not isinstance(declaration, dict) or set(declaration) != {
            "claim_sha256", "reference_id", "session_id", "checkpoint_id",
            "selector", "scope",
        }:
            raise ValueError("invalid_collection_declaration")
        source = _declared_source(
            context, claim, declaration, records, runtime_input, source_snapshot,
        )
        if source["source_type"] != "evidence_collection_scope":
            raise ValueError("collection_source_required")
        if any(
            declaration[key] != context["binding"][key]
            for key in ("session_id", "checkpoint_id")
        ):
            raise ValueError("collection_binding_mismatch")
        scope = declaration["scope"]
        if scope not in (
            "snapshot_saved_results", "runtime_visible_results",
            "runtime_complete_results",
        ):
            raise ValueError("unsupported_collection_scope")
        value = source["value"]
        if scope == "runtime_complete_results" and (
            value["runtime_observation_coverage"] != "complete"
        ):
            raise ValueError("runtime_collection_incomplete")
        selector = declaration["selector"]
        if not isinstance(selector, dict) or set(selector) != {
            "name", "arguments",
        } or (
            not isinstance(selector["name"], str) or not selector["name"]
            or not isinstance(selector["arguments"], dict)
            or any(not isinstance(key, str) for key in selector["arguments"])
        ):
            raise ValueError("invalid_collection_selector")
        selected_records = value["records"]
        if scope == "runtime_visible_results":
            selected_records = [
                record for record in selected_records
                if record["observation_id"] in value["runtime_visible_observation_ids"]
            ]
        for record in selected_records:
            if record["name"] == selector["name"] and all(
                key in record["arguments"]
                and json_sha256(record["arguments"][key]) == json_sha256(expected)
                for key, expected in selector["arguments"].items()
            ):
                raise ValueError("matching_saved_result_exists")
        return {
            **_result([]),
            "claim_sha256": declaration["claim_sha256"],
            "declaration_sha256": json_sha256(declaration),
            "catalog_sha256": context["catalog_sha256"],
            "support_scope": scope,
            "runtime_observation_coverage": value["runtime_observation_coverage"],
            "real_world_absence_established": False,
            "preflight_nonexecution_established": False,
        }
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        code = str(exc) if isinstance(exc, ValueError) else "invalid_support_input"
        return _result([code])


def assess_query_preconditions(
    alert_event,
    query_window=None,
    *,
    parameters_complete=False,
    execution_authorized=False,
):
    """Classify a proposed query without deriving a window or granting authority.

    The caller must independently verify alert provenance. Only an explicit
    start/end window present verbatim in that alert is supported by this small
    experiment. Relative windows need a separately reviewed future design.
    Parameter completeness and authorization are caller declarations, not
    validated ToolManager inputs or an approval checked against a real ledger.
    """
    explicit_window = False
    try:
        if (
            isinstance(alert_event, dict)
            and isinstance(query_window, dict)
            and set(query_window) == {"start", "end"}
            and json_sha256(query_window) == json_sha256(alert_event.get("window"))
        ):
            start = datetime.fromisoformat(query_window["start"])
            end = datetime.fromisoformat(query_window["end"])
            explicit_window = (
                start.utcoffset() is not None
                and end.utcoffset() is not None
                and start < end
            )
    except (ValueError, TypeError, OverflowError):
        explicit_window = False
    missing = []
    if not explicit_window:
        missing.append("trusted_alert_window")
    if parameters_complete is not True:
        missing.append("complete_query_parameters")
    if execution_authorized is not True:
        missing.append("execution_authorization")
    return {
        "recommendation": (
            "candidate_operation"
            if explicit_window and parameters_complete is True
            else "direction_only"
        ),
        "motivation": "not_assessed",
        "missing_preconditions": missing,
        "semantic_review": "required",
        "parameter_completeness": "caller_declared",
        "authorization_recorded": execution_authorized is True,
        "execution_allowed": False,
    }


def external_evidence_acceptance(report):
    """Expose an additional preflight gate, preserving legacy report semantics.

    This projects a report, not an attestation verifier or source authenticator.
    Call it only on a report composed by the existing checked workflow.
    """
    acceptance = report.get("acceptance", {})
    source_check = report.get("model_interpretation", {}).get(
        "reference_source_check", "not_recorded",
    )
    states = [acceptance.get(key) for key in (
        "runtime_investigation", "authoritative_fact_integrity",
        "model_output_structure", "model_reasoning_correctness",
        "overall_strict_acceptance",
    )]
    if report.get("scope") != "independent_preflight_only":
        verdict = "failed"
    elif "failed" in states or source_check == "failed":
        verdict = "failed"
    elif all(state == "passed" for state in states) and (
        source_check == "passed_external_rebuild"
    ):
        verdict = "passed"
    else:
        verdict = "pending"
    return {
        "scope": "independent_preflight_only",
        "reference_source_check": source_check,
        "external_evidence_acceptance": verdict,
        "production_state_authority": False,
    }
