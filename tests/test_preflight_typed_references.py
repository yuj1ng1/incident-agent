"""Typed preflight sources and semantic limits, with no external requests."""
from copy import deepcopy
import json

import pytest

from scripts.preflight_evidence_refs import (
    INCIDENT_CONTEXT_PREFIX,
    build_reference_context,
    verify_reference_context,
)
from test_preflight_evidence_index import (
    assert_failed,
    delivered,
    preflight,
    reasoning_review_for,
    validate,
)
from test_real_model_preflight import read_json, request_observations
from test_worker_metric_analysis_growth import offline


@pytest.fixture(autouse=True)
def require_offline(offline):
    """Any accidental model, monitoring or business-service request must fail."""


@pytest.fixture
def typed_data(delivered, tmp_path):
    body, records, index = delivered
    return {
        "records": records,
        "observations": list(request_observations(body).values()),
        "index": index,
        "runtime_input": read_json(tmp_path, "runtime-input.json"),
        "source_snapshot": read_json(tmp_path, "request-source-snapshot.json"),
    }


def context_source(index, source_type):
    return next(
        source for source in index["reference_context"]["context_sources"]
        if source["source_type"] == source_type
    )


def claim_for(source, text="该来源仅提供有边界的元数据。"):
    return {
        "kind": "limitation",
        "text": text,
        "observation_ids": [],
        "context_refs": [{
            "type": source["source_type"],
            "id": source["reference_id"],
        }],
    }


def change_runtime(data, change_metadata=None, change_rebuilt=None):
    runtime_input = deepcopy(data["runtime_input"])
    for item in runtime_input:
        content = item.get("content", "")
        if item.get("role") != "user" or not content.startswith(
            INCIDENT_CONTEXT_PREFIX,
        ):
            continue
        first, remainder = content[len(INCIDENT_CONTEXT_PREFIX):].split("\n", 1)
        metadata = json.loads(first)
        if change_metadata is not None:
            change_metadata(metadata)
        if change_rebuilt is not None:
            prefix, last = remainder.rsplit("\n", 1)
            rebuilt = json.loads(last)
            change_rebuilt(rebuilt)
            remainder = prefix + "\n" + json.dumps(rebuilt, ensure_ascii=False)
        item["content"] = (
            INCIDENT_CONTEXT_PREFIX
            + json.dumps(metadata, ensure_ascii=False)
            + "\n"
            + remainder
        )
        return runtime_input
    raise AssertionError("actual_runtime_context_not_found")


def rebuilt_index(preflight, data, **changes):
    fields = {**data, **changes}
    return preflight.build_evidence_index(
        fields["records"],
        fields["observations"],
        runtime_input=fields["runtime_input"],
        source_snapshot=fields["source_snapshot"],
    )


def review_with_context(preflight, claim, index, verdict="passed"):
    facts = preflight.build_authoritative_fact_view(index)
    review = reasoning_review_for([claim], facts, verdict=verdict)
    review["claim_reviews"][0]["context_refs"] = deepcopy(claim["context_refs"])
    return facts, review


@pytest.mark.parametrize("source_type", [
    "alert_event", "capability_context", "identity_context",
    "evidence_collection_scope",
])
def test_each_actual_context_type_can_be_referenced_without_tool_ids(
    preflight,
    typed_data,
    source_type,
):
    index = typed_data["index"]
    source = context_source(index, source_type)
    claim = claim_for(source)
    before = deepcopy(typed_data)
    result = preflight.validate_model_answer(
        json.dumps({"claims": [claim]}, ensure_ascii=False),
        index,
        runtime_input=typed_data["runtime_input"],
        source_snapshot=typed_data["source_snapshot"],
    )
    assert source["reference_id"].startswith(f"context:{source_type}:")
    assert source["source_location"]
    assert source["evidence_permissions"]["may_support"]
    assert result["output_structure"] == "passed"
    assert result["strict_acceptance"] == "pending"
    assert result["model_claims"] == [claim]
    assert typed_data == before


def test_legacy_observation_only_claims_remain_valid_in_both_schemas(
    preflight,
    typed_data,
):
    legacy = preflight.build_evidence_index(
        typed_data["records"], typed_data["observations"],
    )
    assert legacy["schema_version"] == 1
    assert "reference_context" not in legacy
    claim = {
        "kind": "limitation",
        "text": "工具结果仍须结合其采样时间解释。",
        "observation_ids": [legacy["sources"][0]["observation_id"]],
    }
    for index in (legacy, typed_data["index"]):
        result = validate(preflight, {"claims": [claim]}, index)
        assert result["output_structure"] == "passed"
        assert result["strict_acceptance"] == "pending"


@pytest.mark.parametrize("mutation", [
    "fabricated", "observation_as_context", "context_as_observation",
    "wrong_type", "unknown_type", "duplicate_context", "extra_ref_field",
])
def test_namespace_type_and_fabricated_context_errors_are_rejected(
    preflight,
    typed_data,
    mutation,
):
    index = typed_data["index"]
    claim = claim_for(context_source(index, "alert_event"))
    ref = claim["context_refs"][0]
    if mutation == "fabricated":
        ref["id"] = "context:alert_event:" + "0" * 64
    elif mutation == "observation_as_context":
        ref["id"] = index["sources"][0]["observation_id"]
    elif mutation == "context_as_observation":
        claim["observation_ids"] = [ref["id"]]
        del claim["context_refs"]
    elif mutation == "wrong_type":
        ref["type"] = "identity_context"
    elif mutation == "unknown_type":
        ref["type"] = "tool_observation"
    elif mutation == "duplicate_context":
        claim["context_refs"].append(deepcopy(ref))
    else:
        ref["observation_id"] = index["sources"][0]["observation_id"]
    assert_failed(validate(preflight, {"claims": [claim]}, index))


def test_missing_context_fields_are_not_invented_or_replaced_by_metric_ids(
    preflight,
    typed_data,
):
    original = typed_data["index"]
    runtime_input = change_runtime(
        typed_data,
        lambda metadata: (
            metadata.pop("alert_event", None),
            metadata.pop("capabilities", None),
        ),
    )
    records = deepcopy(typed_data["records"])
    for record in records:
        record.pop("identity", None)
    snapshot = deepcopy(typed_data["source_snapshot"])
    snapshot["records"] = deepcopy(records)
    snapshot.pop("identity", None)
    index = rebuilt_index(
        preflight,
        typed_data,
        records=records,
        runtime_input=runtime_input,
        source_snapshot=snapshot,
    )
    assert {
        source["source_type"]
        for source in index["reference_context"]["context_sources"]
    } == {"evidence_collection_scope"}
    claim = claim_for(context_source(original, "alert_event"))
    # Adding a real but unrelated metric cannot repair a missing context ID.
    claim["observation_ids"] = [index["sources"][0]["observation_id"]]
    assert_failed(validate(preflight, {"claims": [claim]}, index))


def test_capability_and_dependencies_do_not_create_executions_or_log_results(
    typed_data,
):
    index = typed_data["index"]
    caps = context_source(index, "capability_context")
    scope = context_source(index, "evidence_collection_scope")
    assert "logs" in caps["value"]["capabilities"]
    assert caps["value"]["dependencies"]
    assert "query_logs" not in scope["value"]["collected_tool_names"]
    assert "check_dependency_health" not in scope["value"]["collected_tool_names"]
    assert "tool_execution" in caps["evidence_permissions"]["cannot_establish"]
    assert "dependency_health" in caps["evidence_permissions"]["cannot_establish"]
    assert "absence_of_logs_or_faults" in (
        scope["evidence_permissions"]["cannot_establish"]
    )
    assert {
        item["observation_id"] for item in scope["value"]["records"]
    } == {item["observation_id"] for item in typed_data["records"]}
    assert len(index["sources"]) == len(typed_data["records"]) == 5


@pytest.mark.parametrize(("source_type", "text"), [
    ("capability_context", "配置声明日志能力，因此日志工具已经成功执行。"),
    ("capability_context", "配置包含数据库依赖，因此数据库当前健康。"),
    ("evidence_collection_scope", "本轮未收集日志，所以现实中不存在日志证据。"),
    ("identity_context", "历史身份包含提交版本，所以当前服务已经恢复健康。"),
])
def test_legal_references_do_not_automatically_accept_wrong_natural_language(
    preflight,
    typed_data,
    source_type,
    text,
):
    index = typed_data["index"]
    claim = claim_for(context_source(index, source_type), text)
    validation = validate(preflight, {"claims": [claim]}, index)
    facts, review = review_with_context(preflight, claim, index, verdict="failed")
    review["claim_reviews"][0]["reason"] = "元数据或集合缺失无法支持该健康、执行或存在性结论。"
    pending = preflight.compose_preflight_report(
        facts, [claim], validation, runtime_completed=True,
    )
    failed = preflight.compose_preflight_report(
        facts,
        [claim],
        validation,
        runtime_completed=True,
        reasoning_review=review,
    )
    assert validation["output_structure"] == "passed"
    assert pending["acceptance"]["overall_strict_acceptance"] == "pending"
    assert failed["acceptance"]["model_reasoning_correctness"] == "failed"
    assert failed["acceptance"]["overall_strict_acceptance"] == "failed"
    assert failed["model_interpretation"]["claims"] == [claim]


def test_partial_runtime_summary_is_explicit_and_does_not_accept_universal_claim(
    preflight,
    typed_data,
):
    def omit_observation(rebuilt):
        rebuilt["historical_observations"].pop()

    runtime_input = change_runtime(typed_data, change_rebuilt=omit_observation)
    context = build_reference_context(
        typed_data["records"], runtime_input, typed_data["source_snapshot"],
    )
    index = deepcopy(typed_data["index"])
    index["reference_context"] = context
    scope = context_source(index, "evidence_collection_scope")
    assert scope["value"]["runtime_observation_coverage"] == "partial"
    assert len(scope["value"]["runtime_omitted_observation_ids"]) == 1
    assert scope["value"]["scope"] == "pre_request_checkpoint_records_only"
    assert "unbounded_universal_claims" in (
        scope["evidence_permissions"]["cannot_establish"]
    )
    claim = claim_for(scope, "所有时间和服务的全部日志证据都不存在。")
    validation = validate(preflight, {"claims": [claim]}, index)
    facts, review = review_with_context(preflight, claim, index, verdict="failed")
    assert validation["strict_acceptance"] == "pending"
    report = preflight.compose_preflight_report(
        facts,
        [claim],
        validation,
        runtime_completed=True,
        reasoning_review=review,
    )
    assert report["acceptance"]["overall_strict_acceptance"] == "failed"


@pytest.mark.parametrize("change", ["alert", "capability", "identity", "scope"])
def test_review_fact_hash_is_bound_to_every_context_source(
    preflight,
    typed_data,
    change,
):
    index = typed_data["index"]
    # Keep this legacy claim identical, isolating the authoritative facts hash.
    claim = {
        "kind": "limitation",
        "text": "该指标结果不能独立证明当前健康。",
        "observation_ids": [index["sources"][0]["observation_id"]],
    }
    facts = preflight.build_authoritative_fact_view(index)
    review = reasoning_review_for([claim], facts)
    runtime_input = deepcopy(typed_data["runtime_input"])
    snapshot = deepcopy(typed_data["source_snapshot"])
    if change == "alert":
        runtime_input = change_runtime(
            typed_data,
            lambda metadata: metadata["alert_event"].update(value=0.95),
        )
    elif change == "capability":
        runtime_input = change_runtime(
            typed_data,
            lambda metadata: metadata["capabilities"]["dependencies"].append(
                "offline-additional-dependency",
            ),
        )
    elif change == "identity":
        snapshot["identity"] = {"unverified_instance": "different-offline-instance"}
    else:
        snapshot["checkpoint_id"] += "-different-snapshot"
    changed_index = rebuilt_index(
        preflight,
        typed_data,
        runtime_input=runtime_input,
        source_snapshot=snapshot,
    )
    changed_facts = preflight.build_authoritative_fact_view(changed_index)
    validation = validate(preflight, {"claims": [claim]}, changed_index)
    assert changed_facts["sources"] == facts["sources"]
    assert changed_facts != facts
    assert validation["output_structure"] == "passed"
    with pytest.raises(ValueError, match="preflight_incomplete_reasoning_review"):
        preflight.compose_preflight_report(
            changed_facts,
            [claim],
            validation,
            runtime_completed=True,
            reasoning_review=review,
        )


@pytest.mark.parametrize("mutation", ["missing_refs", "changed_refs", "changed_hash"])
def test_context_claim_review_must_bind_exact_context_references(
    preflight,
    typed_data,
    mutation,
):
    index = typed_data["index"]
    claim = claim_for(context_source(index, "alert_event"))
    facts, review = review_with_context(preflight, claim, index)
    validation = validate(preflight, {"claims": [claim]}, index)
    if mutation == "missing_refs":
        del review["claim_reviews"][0]["context_refs"]
    elif mutation == "changed_refs":
        review["claim_reviews"][0]["context_refs"] = []
    else:
        review["claims_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="preflight_incomplete_reasoning_review"):
        preflight.compose_preflight_report(
            facts,
            [claim],
            validation,
            runtime_completed=True,
            reasoning_review=review,
        )


@pytest.mark.parametrize("mutation", [
    "catalog_hash", "source_value", "source_type", "scope_membership",
    "permission_escalation", "observation_type", "context_namespace",
])
def test_index_structural_tampering_fails_closed(
    preflight,
    typed_data,
    mutation,
):
    index = deepcopy(typed_data["index"])
    source = context_source(index, "alert_event")
    if mutation == "catalog_hash":
        index["reference_context"]["catalog_sha256"] = "0" * 64
    elif mutation == "source_value":
        source["value"]["service"] = "fabricated-service"
    elif mutation == "source_type":
        source["source_type"] = "observation"
    elif mutation == "scope_membership":
        context_source(index, "evidence_collection_scope")["value"]["records"].pop()
    elif mutation == "permission_escalation":
        source["evidence_permissions"]["may_support"].append("current_service_health")
    elif mutation == "observation_type":
        index["sources"][0]["source_type"] = "alert_event"
    else:
        index["sources"][0]["observation_id"] = source["reference_id"]
    with pytest.raises(ValueError):
        preflight.verify_index_references(index)


def test_rehashed_fabricated_metadata_is_rejected_against_actual_runtime_sources(
    preflight,
    typed_data,
):
    fabricated_runtime = change_runtime(
        typed_data,
        lambda metadata: metadata["alert_event"].update(service="fabricated-service"),
    )
    fabricated = rebuilt_index(
        preflight, typed_data, runtime_input=fabricated_runtime,
    )
    # Internal hashes are not signatures: external source replay is essential.
    preflight.verify_index_references(fabricated)
    with pytest.raises(ValueError):
        preflight.verify_index_references(
            fabricated,
            runtime_input=typed_data["runtime_input"],
            source_snapshot=typed_data["source_snapshot"],
        )
    claim = claim_for(context_source(fabricated, "alert_event"))
    result = preflight.validate_model_answer(
        json.dumps({"claims": [claim]}, ensure_ascii=False),
        fabricated,
        runtime_input=typed_data["runtime_input"],
        source_snapshot=typed_data["source_snapshot"],
    )
    assert_failed(result)


def test_scope_snapshot_cannot_be_substituted_between_sessions_or_checkpoints(
    preflight,
    typed_data,
):
    context = typed_data["index"]["reference_context"]
    snapshot = deepcopy(typed_data["source_snapshot"])
    snapshot["session_id"] += "-other-session"
    with pytest.raises(ValueError):
        verify_reference_context(
            context,
            [record["observation_id"] for record in typed_data["records"]],
            records=typed_data["records"],
            runtime_input=typed_data["runtime_input"],
            source_snapshot=snapshot,
        )


def legacy_claim(index):
    return {
        "kind": "limitation",
        "text": "该历史观测不能独立认证当前健康。",
        "observation_ids": [index["sources"][0]["observation_id"]],
    }


def test_legacy_external_sources_are_actually_rebuilt(preflight, typed_data):
    index = preflight.build_evidence_index(
        typed_data["records"], typed_data["observations"],
    )
    result = preflight.validate_model_answer(
        json.dumps({"claims": [legacy_claim(index)]}, ensure_ascii=False),
        index,
        runtime_input=typed_data["runtime_input"],
        source_snapshot=typed_data["source_snapshot"],
    )
    assert result["output_structure"] == "passed"
    assert result["reference_source_check"] == "passed_external_rebuild"
    assert result["strict_acceptance"] == "pending"


@pytest.mark.parametrize("origin", ["legacy", "downgraded_v2"])
def test_legacy_external_sources_reject_altered_sample_value(
    preflight,
    typed_data,
    origin,
):
    if origin == "legacy":
        index = preflight.build_evidence_index(
            typed_data["records"], typed_data["observations"],
        )
    else:
        index = deepcopy(typed_data["index"])
        index["schema_version"] = 1
        del index["reference_context"]
        for source in index["sources"]:
            del source["source_type"]
            del source["evidence_permissions"]
        assert index == preflight.build_evidence_index(
            typed_data["records"], typed_data["observations"],
        )
    index["sources"][0]["samples"][0]["value"] += 100
    with pytest.raises(ValueError, match="source_binding_mismatch"):
        preflight.verify_index_references(
            index,
            runtime_input=typed_data["runtime_input"],
            source_snapshot=typed_data["source_snapshot"],
        )
    result = preflight.validate_model_answer(
        json.dumps({"claims": [legacy_claim(index)]}, ensure_ascii=False),
        index,
        runtime_input=typed_data["runtime_input"],
        source_snapshot=typed_data["source_snapshot"],
    )
    assert_failed(result)
    assert result["reference_source_check"] != "passed_external_rebuild"


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("provided", ["runtime_input", "source_snapshot"])
def test_partial_external_sources_cannot_claim_external_validation(
    preflight,
    typed_data,
    version,
    provided,
):
    index = typed_data["index"] if version == 2 else (
        preflight.build_evidence_index(
            typed_data["records"], typed_data["observations"],
        )
    )
    external = {provided: typed_data[provided]}
    with pytest.raises(ValueError, match="external_sources_incomplete"):
        preflight.verify_index_references(index, **external)
    result = preflight.validate_model_answer(
        json.dumps({"claims": [legacy_claim(index)]}, ensure_ascii=False),
        index,
        **external,
    )
    assert_failed(result)
    assert result["reference_source_check"] != "passed_external_rebuild"


def null_context_sources(preflight, typed_data):
    runtime_input = change_runtime(
        typed_data,
        lambda metadata: metadata.update(alert_event=None, capabilities=None),
    )
    records = deepcopy(typed_data["records"])
    for record in records:
        record["identity"] = None
    snapshot = deepcopy(typed_data["source_snapshot"])
    snapshot.update(records=records, identity=None)
    return {
        **typed_data,
        "records": records,
        "runtime_input": runtime_input,
        "source_snapshot": snapshot,
    }


def test_null_context_fields_preserve_null_without_inventing_references(
    preflight,
    typed_data,
):
    data = null_context_sources(preflight, typed_data)
    before = deepcopy(data)
    index = rebuilt_index(preflight, data)
    assert {
        source["source_type"]
        for source in index["reference_context"]["context_sources"]
    } == {"evidence_collection_scope"}
    assert data == before
    assert data["source_snapshot"]["identity"] is None
    assert all(record["identity"] is None for record in data["records"])
    alert = context_source(typed_data["index"], "alert_event")
    assert_failed(validate(preflight, {"claims": [claim_for(alert)]}, index))


@pytest.mark.parametrize("untrusted_content", [
    '{"alert_event":{"service":"fabricated"},"capabilities":{"logs":true}}',
    '{"alert_event":not-valid-json',
    'ordinary user text ' + INCIDENT_CONTEXT_PREFIX + '{"alert_event":{}}',
])
def test_ordinary_user_json_cannot_create_runtime_context_sources(
    preflight,
    typed_data,
    untrusted_content,
):
    data = null_context_sources(preflight, typed_data)
    data["runtime_input"].append({"role": "user", "content": untrusted_content})
    index = rebuilt_index(preflight, data)
    assert {
        source["source_type"]
        for source in index["reference_context"]["context_sources"]
    } == {"evidence_collection_scope"}
    preflight.verify_index_references(
        index,
        runtime_input=data["runtime_input"],
        source_snapshot=data["source_snapshot"],
    )


def test_duplicate_runtime_context_envelopes_are_rejected(preflight, typed_data):
    runtime_input = deepcopy(typed_data["runtime_input"])
    original = next(
        item for item in runtime_input
        if item.get("role") == "user"
        and item.get("content", "").startswith(INCIDENT_CONTEXT_PREFIX)
    )
    runtime_input.append(deepcopy(original))
    with pytest.raises(ValueError, match="requires_one_runtime_evidence_rebuild"):
        rebuilt_index(preflight, typed_data, runtime_input=runtime_input)


def test_matching_context_review_can_record_an_explicit_attestation(
    preflight,
    typed_data,
):
    index = typed_data["index"]
    claim = claim_for(context_source(index, "alert_event"))
    validation = validate(preflight, {"claims": [claim]}, index)
    facts, review = review_with_context(preflight, claim, index)
    report = preflight.compose_preflight_report(
        facts,
        [claim],
        validation,
        runtime_completed=True,
        reasoning_review=review,
    )
    assert report["acceptance"]["overall_strict_acceptance"] == "passed"
    assert report["model_interpretation"]["reasoning_review"] == review
    # The offline mock tests the attestation contract, not a real reviewer.
    assert validation["strict_acceptance"] == "pending"
