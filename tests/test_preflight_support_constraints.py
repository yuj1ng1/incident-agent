"""Synthetic support declarations: structural checks never certify claim meaning."""
from copy import deepcopy
import json

import pytest

from scripts.preflight_evidence_refs import (
    INCIDENT_CONTEXT_PREFIX,
    build_reference_context,
    json_sha256,
)
from scripts.preflight_support_constraints import (
    assess_query_preconditions,
    check_collection_absence,
    check_field_support,
)
from test_worker_metric_analysis_growth import offline


@pytest.fixture(autouse=True)
def require_offline(offline):
    """Reject accidental model, monitoring and business-service network calls."""


def make_data(records=None, visible_ids=None):
    if records is None:
        records = [{
            "observation_id": f"offline-observation-{index}",
            "name": "query_metrics",
            "arguments": {
                "service": "offline-shop",
                "metric": metric,
            },
            "collected_at": "2026-01-02T03:04:05+00:00",
            "result": {"status": "ok"},
            "identity": identity,
        } for index, (metric, identity) in enumerate([
            ("errors", {"commit_sha": "a" * 40}),
            ("latency", {"unverified_instance": "offline-instance"}),
            ("traffic", {"commit_sha": "a" * 40}),
        ])]
    records = deepcopy(records)
    if visible_ids is None:
        visible_ids = [record["observation_id"] for record in records]
    metadata = {
        "alert_event": {
            "service": "offline-shop",
            "starts_at": None,
            "observed_at": None,
            "window": None,
        },
        "capabilities": {
            "logs": True,
            "health": True,
            "dependencies": ["offline-db"],
        },
    }
    rebuilt = {
        "recovery": "offline fixture",
        "historical_observations": [{
            "observation_id": record["observation_id"],
            "name": record["name"],
            "arguments": deepcopy(record["arguments"]),
            "collected_at": record["collected_at"],
            "status": record["result"]["status"],
        } for record in records if record["observation_id"] in visible_ids],
        "failed_or_missing_observations": [],
        "metric_analysis": None,
        "context_collected_at": "2026-01-02T03:05:00+00:00",
    }
    runtime_input = [{
        "role": "user",
        "content": (
            INCIDENT_CONTEXT_PREFIX
            + json.dumps(metadata)
            + "\nPersisted evidence only.\n"
            + json.dumps(rebuilt)
        ),
    }]
    source_snapshot = {
        "session_id": "offline-session",
        "checkpoint_id": "offline-checkpoint",
        "identity": None,
        "records": deepcopy(records),
    }
    return {
        "records": records,
        "runtime_input": runtime_input,
        "source_snapshot": source_snapshot,
        "context": build_reference_context(records, runtime_input, source_snapshot),
    }


def source_for(data, source_type, location=None):
    return next(
        source for source in data["context"]["context_sources"]
        if source["source_type"] == source_type
        and (location is None or source["source_location"] == location)
    )


def claim_for(source, text="该持久化来源记录了一个历史字段。"):
    return {
        "kind": "limitation",
        "text": text,
        "observation_ids": [],
        "context_refs": [{
            "type": source["source_type"],
            "id": source["reference_id"],
        }],
    }


def field_declaration(claim, source, **changes):
    return {
        "claim_sha256": json_sha256(claim),
        "reference_id": source["reference_id"],
        "field_path": ["commit_sha"],
        "expected_value": "a" * 40,
        "use": "persisted_field",
        **changes,
    }


def absence_declaration(data, claim, source, **changes):
    return {
        "claim_sha256": json_sha256(claim),
        "reference_id": source["reference_id"],
        "session_id": data["source_snapshot"]["session_id"],
        "checkpoint_id": data["source_snapshot"]["checkpoint_id"],
        "selector": {
            "name": "query_logs",
            "arguments": {"service": "offline-shop"},
        },
        "scope": "snapshot_saved_results",
        **changes,
    }


def check(checker, data, claim, declaration):
    return checker(
        data["context"],
        claim,
        declaration,
        records=data["records"],
        runtime_input=data["runtime_input"],
        source_snapshot=data["source_snapshot"],
    )


def assert_mechanical(result, expected):
    assert result["mechanical_check"] == expected
    assert result["semantic_review"] == "required"
    assert bool(result["errors"]) is (expected == "failed")


@pytest.mark.parametrize("use", [
    "persisted_field", "historical_identity_association",
])
def test_exact_historical_identity_field_can_be_declared(use):
    data = make_data()
    source = source_for(data, "identity_context")
    claim = claim_for(source)
    before = deepcopy((data, claim))
    declaration = field_declaration(claim, source, use=use)
    result = check(check_field_support, data, claim, declaration)
    assert_mechanical(result, "passed")
    assert result["declaration_sha256"] == json_sha256(declaration)
    assert (data, claim) == before


def test_valid_unverified_identity_cannot_supply_another_records_commit():
    data = make_data()
    source = source_for(
        data, "identity_context", "source_snapshot/records/1/identity",
    )
    claim = claim_for(source)
    assert any("commit_sha" in record["identity"] for record in data["records"])
    assert_mechanical(
        check(check_field_support, data, claim, field_declaration(claim, source)),
        "failed",
    )


def test_commit_absent_from_every_identity_is_not_filled_from_background():
    records = make_data()["records"]
    for index, record in enumerate(records):
        record["identity"] = {"unverified_instance": f"offline-{index}"}
    data = make_data(records)
    source = source_for(data, "identity_context")
    claim = claim_for(source)
    assert_mechanical(
        check(check_field_support, data, claim, field_declaration(claim, source)),
        "failed",
    )


@pytest.mark.parametrize("identity_state", ["missing", "null", "empty"])
def test_missing_identity_does_not_invent_a_field_reference(identity_state):
    original = make_data()
    old_source = source_for(original, "identity_context")
    records = original["records"]
    for record in records:
        if identity_state == "missing":
            record.pop("identity")
        else:
            record["identity"] = None if identity_state == "null" else {}
    data = make_data(records)
    assert not any(
        source["source_type"] == "identity_context"
        for source in data["context"]["context_sources"]
    )
    claim = claim_for(old_source)
    assert_mechanical(
        check(
            check_field_support, data, claim,
            field_declaration(claim, old_source),
        ),
        "failed",
    )


def test_equal_values_at_different_locations_do_not_replace_cited_instance():
    data = make_data()
    first = source_for(data, "identity_context", "source_snapshot/records/0/identity")
    other = source_for(data, "identity_context", "source_snapshot/records/2/identity")
    assert first["value"] == other["value"]
    assert first["reference_id"] != other["reference_id"]
    first_claim = claim_for(first)
    assert_mechanical(
        check(
            check_field_support, data, first_claim,
            field_declaration(first_claim, other),
        ),
        "failed",
    )
    other_claim = claim_for(other)
    assert_mechanical(
        check(
            check_field_support, data, other_claim,
            field_declaration(other_claim, other),
        ),
        "passed",
    )


def test_historical_commit_cannot_be_declared_as_current_deployment_identity():
    data = make_data()
    source = source_for(data, "identity_context")
    claim = claim_for(source)
    declaration = field_declaration(claim, source, use="current_deployment")
    assert_mechanical(check(check_field_support, data, claim, declaration), "failed")


@pytest.mark.parametrize("value,expected,outcome", [
    (0, 0, "passed"),
    (False, False, "passed"),
    (0, False, "failed"),
    (False, 0, "failed"),
    ("0", 0, "failed"),
])
def test_field_values_preserve_json_type_and_falsy_values(value, expected, outcome):
    records = make_data()["records"]
    records[0]["identity"] = {"revision": {"parts": [value]}}
    data = make_data(records)
    source = source_for(data, "identity_context")
    claim = claim_for(source)
    declaration = field_declaration(
        claim, source,
        field_path=["revision", "parts", 0],
        expected_value=expected,
    )
    assert_mechanical(check(check_field_support, data, claim, declaration), outcome)


@pytest.mark.parametrize("path", [[], ["missing"], ["commit_sha", 0], [True]])
def test_invalid_field_path_cannot_become_a_successful_support(path):
    data = make_data()
    source = source_for(data, "identity_context")
    claim = claim_for(source)
    declaration = field_declaration(claim, source, field_path=path)
    assert_mechanical(check(check_field_support, data, claim, declaration), "failed")


def test_present_null_field_is_distinct_from_missing_or_verified_identity():
    records = make_data()["records"]
    records[0]["identity"] = {"commit_sha": None}
    data = make_data(records)
    source = source_for(data, "identity_context")
    claim = claim_for(source, "该历史 identity 的 commit_sha 字段值为 null。")
    declaration = field_declaration(claim, source, expected_value=None)
    result = check(check_field_support, data, claim, declaration)
    assert_mechanical(result, "passed")
    assert result["current_deployment_verified"] is False


@pytest.mark.parametrize("kind", [
    "confirmed", "limitation", "hypothesis", "next_check",
])
def test_existing_field_does_not_certify_arbitrary_claim_text_or_kind(kind):
    data = make_data()
    source = source_for(data, "identity_context")
    claim = claim_for(source, "这个历史字段证明所有地区当前都已恢复服务。")
    claim["kind"] = kind
    declaration = field_declaration(claim, source)
    result = check(check_field_support, data, claim, declaration)
    assert_mechanical(result, "passed")
    assert result["semantic_review"] == "required"


@pytest.mark.parametrize("checker_name", ["field", "collection"])
def test_declaration_is_bound_to_exact_unchanged_claim(checker_name):
    data = make_data()
    if checker_name == "field":
        source = source_for(data, "identity_context")
        claim = claim_for(source)
        checker = check_field_support
        declaration = field_declaration(claim, source)
    else:
        source = source_for(data, "evidence_collection_scope")
        claim = claim_for(source)
        checker = check_collection_absence
        declaration = absence_declaration(data, claim, source)
    claim["text"] += " 已更改。"
    assert_mechanical(check(checker, data, claim, declaration), "failed")


def test_complete_snapshot_can_show_missing_saved_result_only():
    data = make_data()
    source = source_for(data, "evidence_collection_scope")
    claim = claim_for(source, "该 checkpoint 快照未保存日志查询结果。")
    before = deepcopy((data, claim))
    declaration = absence_declaration(data, claim, source)
    assert_mechanical(
        check(check_collection_absence, data, claim, declaration), "passed",
    )
    assert (data, claim) == before


def test_successful_absence_result_is_bound_to_exact_selector_declaration():
    data = make_data()
    source = source_for(data, "evidence_collection_scope")
    claim = claim_for(source)
    results = []
    for name in ("query_logs", "query_health"):
        declaration = absence_declaration(data, claim, source, selector={
            "name": name,
            "arguments": {"service": "offline-shop"},
        })
        result = check(check_collection_absence, data, claim, declaration)
        assert_mechanical(result, "passed")
        assert result["declaration_sha256"] == json_sha256(declaration)
        results.append(result)
    assert results[0]["claim_sha256"] == results[1]["claim_sha256"]
    assert results[0]["catalog_sha256"] == results[1]["catalog_sha256"]
    assert results[0]["declaration_sha256"] != results[1]["declaration_sha256"]


@pytest.mark.parametrize("source_type", ["observation", "capability_context"])
def test_observation_or_declared_capability_cannot_prove_collection_gap(source_type):
    data = make_data()
    if source_type == "observation":
        source = {"reference_id": data["records"][0]["observation_id"]}
        claim = {
            "kind": "limitation",
            "text": "整个快照未保存日志。",
            "observation_ids": [source["reference_id"]],
        }
    else:
        source = source_for(data, source_type)
        claim = claim_for(source)
        assert source["value"]["logs"] is True
    declaration = absence_declaration(data, claim, source)
    assert_mechanical(
        check(check_collection_absence, data, claim, declaration), "failed",
    )


@pytest.mark.parametrize("scope", ["preflight_executions", "real_world_absence"])
def test_snapshot_cannot_establish_preflight_execution_or_real_world_absence(scope):
    data = make_data()
    source = source_for(data, "evidence_collection_scope")
    claim = claim_for(source)
    declaration = absence_declaration(data, claim, source, scope=scope)
    assert_mechanical(
        check(check_collection_absence, data, claim, declaration), "failed",
    )


@pytest.mark.parametrize("scope,outcome", [
    ("snapshot_saved_results", "passed"),
    ("runtime_visible_results", "passed"),
    ("runtime_complete_results", "failed"),
])
def test_partial_runtime_does_not_change_snapshot_membership(scope, outcome):
    data = make_data(visible_ids=["offline-observation-0"])
    source = source_for(data, "evidence_collection_scope")
    assert source["value"]["runtime_observation_coverage"] == "partial"
    claim = claim_for(source)
    declaration = absence_declaration(data, claim, source, scope=scope)
    assert_mechanical(
        check(check_collection_absence, data, claim, declaration), outcome,
    )


@pytest.mark.parametrize("scope,outcome", [
    ("snapshot_saved_results", "failed"),
    ("runtime_visible_results", "passed"),
    ("runtime_complete_results", "failed"),
])
def test_omitted_saved_log_result_is_not_a_snapshot_gap(scope, outcome):
    records = make_data()["records"]
    records[1]["name"] = "query_logs"
    data = make_data(records, visible_ids=["offline-observation-0"])
    source = source_for(data, "evidence_collection_scope")
    claim = claim_for(source)
    declaration = absence_declaration(data, claim, source, scope=scope)
    assert_mechanical(
        check(check_collection_absence, data, claim, declaration), outcome,
    )


@pytest.mark.parametrize("status", ["ok", "no_data", "error"])
def test_any_saved_result_status_counts_as_collection_membership(status):
    records = make_data()["records"]
    records[0]["name"] = "query_logs"
    records[0]["result"]["status"] = status
    data = make_data(records)
    source = source_for(data, "evidence_collection_scope")
    claim = claim_for(source)
    declaration = absence_declaration(data, claim, source)
    assert_mechanical(
        check(check_collection_absence, data, claim, declaration), "failed",
    )


def test_collection_selector_is_exact_tool_with_argument_subset():
    data = make_data()
    source = source_for(data, "evidence_collection_scope")
    claim = claim_for(source)
    for selector, outcome in [
        ({"name": "query_metrics", "arguments": {"service": "offline-shop"}},
         "failed"),
        ({"name": "query_metrics", "arguments": {"service": "another-shop"}},
         "passed"),
        ({"name": "query_metric", "arguments": {"service": "offline-shop"}},
         "passed"),
    ]:
        declaration = absence_declaration(data, claim, source, selector=selector)
        assert_mechanical(
            check(check_collection_absence, data, claim, declaration), outcome,
        )


@pytest.mark.parametrize("selected_value,outcome", [
    (False, "failed"),
    (0, "passed"),
])
def test_collection_argument_selector_preserves_json_value_types(
    selected_value,
    outcome,
):
    records = make_data()["records"]
    records[0]["arguments"]["include_empty"] = False
    data = make_data(records)
    source = source_for(data, "evidence_collection_scope")
    claim = claim_for(source)
    declaration = absence_declaration(data, claim, source, selector={
        "name": "query_metrics",
        "arguments": {"include_empty": selected_value},
    })
    assert_mechanical(
        check(check_collection_absence, data, claim, declaration), outcome,
    )


@pytest.mark.parametrize("binding_key", ["session_id", "checkpoint_id"])
def test_collection_scope_cannot_be_mixed_between_sessions_or_checkpoints(binding_key):
    data = make_data()
    source = source_for(data, "evidence_collection_scope")
    claim = claim_for(source)
    declaration = absence_declaration(data, claim, source)
    declaration[binding_key] = "different-offline-binding"
    assert_mechanical(
        check(check_collection_absence, data, claim, declaration), "failed",
    )


def test_external_source_rebuild_rejects_self_consistent_wrong_snapshot():
    data = make_data()
    source = source_for(data, "evidence_collection_scope")
    claim = claim_for(source)
    declaration = absence_declaration(data, claim, source)
    data["source_snapshot"]["checkpoint_id"] = "another-checkpoint"
    assert_mechanical(
        check(check_collection_absence, data, claim, declaration), "failed",
    )


def test_false_universal_claim_with_narrow_declaration_still_requires_review():
    data = make_data()
    source = source_for(data, "evidence_collection_scope")
    claim = claim_for(source, "现实世界不存在任何日志，因而没有故障。")
    declaration = absence_declaration(data, claim, source)
    result = check(check_collection_absence, data, claim, declaration)
    assert_mechanical(result, "passed")
    assert result["semantic_review"] == "required"


def trusted_window():
    return {
        "start": "2026-01-02T03:00:00+00:00",
        "end": "2026-01-02T03:10:00+00:00",
    }


@pytest.mark.parametrize("alert", [
    {"starts_at": None, "observed_at": None, "window": None},
    {"evaluation_time": "2026-01-02T03:05:00+00:00", "window": None},
    {"starts_at": "2026-01-02T03:00:00+00:00", "window": None},
])
def test_missing_alert_window_stays_non_executable_investigation_direction(alert):
    before = deepcopy(alert)
    result = assess_query_preconditions(alert, parameters_complete=True)
    assert result["recommendation"] == "direction_only"
    assert result["motivation"] == "not_assessed"
    assert result["missing_preconditions"]
    assert result["execution_allowed"] is False
    assert result["authorization_recorded"] is False
    assert alert == before


def test_locally_invented_window_cannot_replace_missing_alert_range():
    result = assess_query_preconditions(
        {"window": None}, trusted_window(),
        parameters_complete=True,
        execution_authorized=True,
    )
    assert result["recommendation"] == "direction_only"
    assert result["missing_preconditions"]
    assert result["execution_allowed"] is False


@pytest.mark.parametrize("invalid_window", [
    {"start": "2026-01-02T03:00:00", "end": "2026-01-02T03:10:00"},
    {"start": "2026-01-02T03:10:00Z", "end": "2026-01-02T03:00:00Z"},
    {"start": "2026-01-02T03:00:00Z", "end": "2026-01-02T03:00:00Z"},
    {"start": "2026-01-02T03:00:00Z"},
])
def test_alert_window_requires_explicit_ordered_timezone_aware_bounds(invalid_window):
    result = assess_query_preconditions(
        {"window": invalid_window}, invalid_window,
        parameters_complete=True,
        execution_authorized=True,
    )
    assert result["recommendation"] == "direction_only"
    assert result["missing_preconditions"]
    assert result["execution_allowed"] is False


@pytest.mark.parametrize("parameters_complete", [False, True])
@pytest.mark.parametrize("execution_authorized", [False, True])
def test_trusted_time_and_candidate_parameters_never_grant_tool_authority(
    parameters_complete,
    execution_authorized,
):
    window = trusted_window()
    result = assess_query_preconditions(
        {"window": window}, deepcopy(window),
        parameters_complete=parameters_complete,
        execution_authorized=execution_authorized,
    )
    assert result["recommendation"] == (
        "candidate_operation" if parameters_complete else "direction_only"
    )
    assert result["execution_allowed"] is False
    assert result["authorization_recorded"] is execution_authorized
    if not parameters_complete or not execution_authorized:
        assert result["missing_preconditions"]
    else:
        assert result["missing_preconditions"] == []


def test_query_window_must_be_the_supplied_alert_range():
    window = trusted_window()
    changed = {**window, "end": "2026-01-02T03:20:00+00:00"}
    result = assess_query_preconditions(
        {"window": window}, changed,
        parameters_complete=True,
        execution_authorized=True,
    )
    assert result["recommendation"] == "direction_only"
    assert result["missing_preconditions"]
    assert result["execution_allowed"] is False
