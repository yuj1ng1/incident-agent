"""P5.3c.2d authoritative facts and model claims, using only Mock Providers."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import importlib
import json
from types import SimpleNamespace

import pytest

from investigation.metric_analysis import analyze_metric_evidence
from runtime_support import response
from test_metric_analysis import AT, COLLECTED, LATER
from test_real_model_preflight import (
    fake_sdk,
    read_json,
    request_observations,
)
from test_worker_metric_analysis_growth import analyses, function_items, offline


@pytest.fixture(autouse=True)
def require_offline(offline):
    """A test cannot reach a real model, Prometheus or business service."""


@pytest.fixture
def preflight():
    return importlib.import_module("scripts.preflight_real_model")


def request_derived_payload(body):
    found = []
    for item in body["input"]:
        if item.get("role") != "user" or not isinstance(item.get("content"), str):
            continue
        try:
            payload = json.loads(item["content"].rsplit("\n", 1)[-1])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "evidence_index" in payload:
            found.append(payload)
    assert len(found) == 1
    return found[0]


def request_index(body):
    return request_derived_payload(body)["evidence_index"]


@pytest.fixture
def delivered(preflight, tmp_path):
    preflight.run_case(tmp_path)
    body = read_json(tmp_path, "request.json")
    checkpoint = read_json(tmp_path, "checkpoint.json")
    return body, checkpoint["records"], request_index(body)


def answer_from_index(index):
    """Use real references without copying the Python-owned facts into claims."""
    source_ids = [item["observation_id"] for item in index["sources"]]
    return {
        "claims": [{
            "kind": "limitation",
            "text": "此项解释仍须人工复核。",
            "observation_ids": source_ids,
        }],
    }


def validate(preflight, answer, index):
    text = answer if isinstance(answer, str) else json.dumps(
        answer, ensure_ascii=False, allow_nan=False,
    )
    return preflight.validate_model_answer(text, index)


def assert_failed(review):
    assert review["output_structure"] == "failed"
    assert review["errors"]
    assert review["manual_reasoning_review"] == "required"
    assert review["model_reasoning_correctness"] == "pending"
    assert review["strict_acceptance"] == "failed"
    assert review["answer_rewritten"] is False


def reasoning_review_for(
    claims,
    facts,
    *,
    verdict="passed",
    method="human",
):
    """Mock review attestation, not evidence that a reviewer is correct."""
    return {
        "reviewer": "offline-test-reviewer",
        "method": method,
        "claims_sha256": hashlib.sha256(json.dumps(
            claims, ensure_ascii=False, sort_keys=True, allow_nan=False,
        ).encode("utf-8")).hexdigest(),
        "authoritative_facts_sha256": hashlib.sha256(json.dumps(
            facts, ensure_ascii=False, sort_keys=True, allow_nan=False,
        ).encode("utf-8")).hexdigest(),
        "claim_reviews": [{
            "claim_index": number,
            "verdict": verdict,
            "reason": "逐项审查记录；测试只核对记录的绑定与完整性。",
            "observation_ids": deepcopy(claim["observation_ids"]),
        } for number, claim in enumerate(claims)],
    }


def test_real_mock_runtime_indexes_all_five_persisted_sources(
    preflight,
    delivered,
    tmp_path,
):
    body, records, index = delivered
    observations = request_observations(body)
    assert index["schema_version"] == 2
    assert index["provenance"] == "preflight_deterministic_derived"
    assert len(records) == len(index["sources"]) == 5
    assert {item["observation_id"] for item in index["sources"]} == set(
        observations,
    ) == {item["observation_id"] for item in records}
    assert [item["name"] for item in index["sources"]].count(
        "get_running_version",
    ) == 1
    assert read_json(tmp_path, "evidence-index.json") == index
    assert read_json(tmp_path, "request-source-records.json") == records
    envelope = request_derived_payload(body)
    assert envelope["context_origin"] == "preflight_deterministic_derived"
    assert envelope["scope"] == "independent_entry_only_not_runtime_history"
    assert envelope["raw_records"] == records
    assert body["input"][:-1] == read_json(tmp_path, "runtime-input.json")
    history = read_json(tmp_path, "checkpoint.json")["history"]
    assert "evidence_index" not in json.dumps(history)
    assert "preflight_deterministic_derived" not in json.dumps(history)
    originals = deepcopy(records), deepcopy(list(observations.values()))
    rebuilt = preflight.build_evidence_index(
        records,
        list(observations.values()),
        runtime_input=body["input"][:-1],
        source_snapshot=read_json(tmp_path, "request-source-snapshot.json"),
    )
    assert rebuilt == index
    assert (records, list(observations.values())) == originals
    for item in index["sources"]:
        raw = next(
            source for source in records
            if source["observation_id"] == item["observation_id"]
        )
        assert item["name"] == raw["name"]
        assert item["status"] == raw["result"]["status"]
        assert item["collected_at"] == raw["collected_at"]
        assert item["historical_only"] is True
        assert item["extraction_status"] == "supported"


def test_each_collection_date_is_retained_without_neighbor_inheritance(
    preflight,
    delivered,
):
    body, records, _ = delivered
    observations = list(request_observations(body).values())
    records = deepcopy(records)
    observations = deepcopy(observations)
    for number, item in enumerate(records):
        item["collected_at"] = (COLLECTED + timedelta(days=number)).isoformat()
        next(
            value for value in observations
            if value["observation_id"] == item["observation_id"]
        )["collected_at"] = item["collected_at"]
    result = preflight.build_evidence_index(records, observations)
    assert len({item["collected_at"] for item in result["sources"]}) == 5
    dates = {
        item["observation_id"]: item["collected_at"] for item in result["sources"]
    }
    assert dates == {
        item["observation_id"]: item["collected_at"] for item in records
    }


def test_version_collection_time_does_not_create_query_or_sample_time(delivered):
    _, records, index = delivered
    version = next(
        item for item in index["sources"] if item["name"] == "get_running_version"
    )
    raw = next(item for item in records if item["name"] == "get_running_version")
    assert version["collected_at"] == raw["collected_at"]
    assert version["status"] == "no_data"
    assert version["data_missing"] is True
    assert "evaluation_time" not in version
    assert "evaluated_at" not in version
    assert "evaluation_time" not in raw["result"]
    assert "evaluated_at" not in raw["result"]
    assert version["samples"] == []


def test_aligned_no_data_never_inherits_group_time_or_valid_zero(delivered):
    _, records, index = delivered
    raw = next(
        item for item in records
        if item["name"] == "query_metrics_aligned"
        and item["result"]["status"] == "incomplete"
    )
    source = next(
        item for item in index["sources"]
        if item["observation_id"] == raw["observation_id"]
    )
    assert source["evaluation_time"] == raw["result"]["evaluation_time"]
    assert source["evaluation_time"] == LATER.isoformat()
    assert source["data_missing"] is True
    missing = next(
        item for item in source["samples"] if item["metric"] == "http_5xx_rate"
    )
    zero = next(
        item for item in source["samples"] if item["metric"] == "http_request_rate"
    )
    assert missing["status"] == "no_data"
    assert "value" not in missing
    assert "evaluated_at" not in missing
    assert zero["status"] == "ok"
    assert zero["value"] == 0.0
    assert zero["evaluated_at"] == LATER.isoformat()
    for item in source["samples"]:
        original = raw["result"]["results"][item["metric"]]
        for field in ("status", "value", "evaluated_at"):
            assert (field in item) == (field in original)
            if field in original:
                assert item[field] == original[field]


def test_original_evidence_analysis_and_runtime_history_are_preserved(delivered):
    body, records, index = delivered
    payload = analyses(body["input"])[0]
    observed = request_observations(body)
    assert len(payload["metric_analysis"]["reports"]) == 1
    expected = analyze_metric_evidence(
        list(reversed(records[:4])), service="order-service",
    ).to_dict()
    assert payload["metric_analysis"]["reports"] == json.loads(json.dumps([{
        "service": "order-service", "target_role": "primary", **expected,
    }]))
    for source in records:
        result = deepcopy(source["result"])
        if source["name"] == "query_metrics_aligned":
            result["summary_truncated"] = False
        assert observed[source["observation_id"]]["result"] == result
    assert function_items(body["input"]) == []
    serialized = json.dumps(body, ensure_ascii=False)
    assert '"expected"' not in serialized
    assert '"grading"' not in serialized
    assert '"rubric"' not in serialized
    assert len(index["sources"]) == 5


def test_unknown_tool_structure_is_explicitly_unsupported(
    preflight,
    delivered,
):
    body, records, _ = delivered
    source = deepcopy(records[0])
    source["name"] = "unrecognized_read_only_tool"
    source["result"] = {"status": "no_data", "opaque": {"at": AT.isoformat()}}
    observation = deepcopy(request_observations(body)[source["observation_id"]])
    observation.update(name=source["name"], status="no_data", result=source["result"])
    index = preflight.build_evidence_index([source], [observation])
    assert index["sources"][0]["extraction_status"] == "unsupported"
    assert index["sources"][0]["samples"] == []
    assert "evaluation_time" not in index["sources"][0]
    assert "evaluated_at" not in index["sources"][0]


@pytest.mark.parametrize("mutation", [
    "missing_runtime_source", "extra_runtime_source", "duplicate_runtime_source",
    "duplicate_raw_source", "wrong_runtime_name", "wrong_runtime_collected_at",
    "wrong_runtime_arguments", "wrong_runtime_status", "missing_usage_flag",
])
def test_raw_and_runtime_source_mismatch_refuses_an_index(
    preflight,
    delivered,
    mutation,
):
    body, records, _ = delivered
    records = deepcopy(records)
    observations = deepcopy(list(request_observations(body).values()))
    target = observations[0]
    if mutation == "missing_runtime_source":
        observations.pop()
    elif mutation == "extra_runtime_source":
        extra = deepcopy(target)
        extra["observation_id"] = "obs-never-persisted"
        observations.append(extra)
    elif mutation == "duplicate_runtime_source":
        observations.append(deepcopy(target))
    elif mutation == "duplicate_raw_source":
        records.append(deepcopy(records[0]))
    elif mutation == "wrong_runtime_name":
        target["name"] = "different_tool"
    elif mutation == "wrong_runtime_collected_at":
        target["collected_at"] = "1900-01-01T00:00:00+00:00"
    elif mutation == "wrong_runtime_arguments":
        target["arguments"] = {"service": "another-service"}
    elif mutation == "wrong_runtime_status":
        target["status"] = "invented-status"
    else:
        del target["use_for_current_state"]
    with pytest.raises(ValueError, match="preflight_"):
        preflight.build_evidence_index(records, observations)


def test_authoritative_fact_view_is_mechanically_reversible_and_read_only(
    preflight,
    delivered,
):
    _, _, index = delivered
    original = deepcopy(index)
    view = preflight.build_authoritative_fact_view(index)
    assert view["schema_version"] == index["schema_version"]
    assert view["provenance"] == "preflight_authoritative_fact_view"
    assert len(view["sources"]) == len(index["sources"]) == 5
    for shown, source in zip(view["sources"], index["sources"]):
        assert shown["observation_id"] == source["observation_id"]
        for field in (
            "name", "collected_at", "status", "historical_only", "data_missing",
            "extraction_status",
        ):
            assert shown[field] == source[field]
        for field in ("evaluation_time", "evaluated_at"):
            assert shown[field] == {
                "provided": field in source,
                "value": source.get(field, "未提供"),
            }
        assert len(shown["samples"]) == len(source["samples"])
        for displayed, sample in zip(shown["samples"], source["samples"]):
            assert displayed["metric"] == sample["metric"]
            assert displayed["status"] == sample["status"]
            for field in ("value", "evaluated_at"):
                assert displayed[field] == {
                    "provided": field in sample,
                    "value": sample.get(field, "未提供"),
                }
    assert index == original
    view["sources"][0]["samples"][0]["value"]["value"] = "mutated-display-only"
    assert index == original


def test_fact_view_preserves_five_independent_collection_times(
    preflight,
    delivered,
):
    body, records, _ = delivered
    records = deepcopy(records)
    observations = deepcopy(list(request_observations(body).values()))
    for number, record in enumerate(records):
        record["collected_at"] = (COLLECTED + timedelta(days=number)).isoformat()
        next(
            item for item in observations
            if item["observation_id"] == record["observation_id"]
        )["collected_at"] = record["collected_at"]
    index = preflight.build_evidence_index(records, observations)
    view = preflight.build_authoritative_fact_view(index)
    assert len({item["collected_at"] for item in view["sources"]}) == 5
    assert {
        item["observation_id"]: item["collected_at"] for item in view["sources"]
    } == {
        item["observation_id"]: item["collected_at"] for item in records
    }


def test_fact_view_distinguishes_sample_and_query_time_without_inheritance(
    preflight,
    delivered,
):
    _, _, index = delivered
    view = preflight.build_authoritative_fact_view(index)
    instant = [item for item in view["sources"] if item["name"] == "query_metrics"]
    assert len(instant) == 2
    for source in instant:
        assert source["evaluation_time"] == {"provided": False, "value": "未提供"}
        assert source["evaluated_at"]["provided"] is True
        assert source["samples"][0]["evaluated_at"] == source["evaluated_at"]
    aligned = next(
        item for item in view["sources"]
        if item["name"] == "query_metrics_aligned" and item["status"] == "incomplete"
    )
    assert aligned["evaluation_time"] == {
        "provided": True,
        "value": LATER.isoformat(),
    }
    missing = next(
        item for item in aligned["samples"] if item["metric"] == "http_5xx_rate"
    )
    zero = next(
        item for item in aligned["samples"] if item["metric"] == "http_request_rate"
    )
    assert missing["status"] == "no_data"
    assert missing["value"] == {"provided": False, "value": "未提供"}
    assert missing["evaluated_at"] == {"provided": False, "value": "未提供"}
    assert zero["status"] == "ok"
    assert zero["value"] == {"provided": True, "value": 0.0}
    assert zero["evaluated_at"] == {"provided": True, "value": LATER.isoformat()}
    version = next(
        item for item in view["sources"] if item["name"] == "get_running_version"
    )
    assert version["collected_at"]
    assert version["status"] == "no_data"
    assert version["evaluation_time"] == {"provided": False, "value": "未提供"}
    assert version["evaluated_at"] == {"provided": False, "value": "未提供"}
    assert version["samples"] == []


def test_unsupported_index_cannot_be_presented_as_authoritative_facts(
    preflight,
    delivered,
):
    _, _, index = delivered
    index = deepcopy(index)
    index["sources"][0]["extraction_status"] = "unsupported"
    original = deepcopy(index)
    with pytest.raises(ValueError, match="preflight_unsupported_evidence_shape"):
        preflight.build_authoritative_fact_view(index)
    assert index == original
    assert_failed(validate(preflight, answer_from_index(index), index))


def test_claims_only_contract_passes_mechanical_review_without_fact_copy(
    preflight,
    delivered,
):
    _, _, index = delivered
    answer = answer_from_index(index)
    review = validate(preflight, answer, index)
    assert set(answer) == {"claims"}
    assert set(answer["claims"][0]) == {"kind", "text", "observation_ids"}
    assert review["output_structure"] == "passed"
    assert review["errors"] == []
    assert review["manual_reasoning_review"] == "required"
    assert review["model_reasoning_correctness"] == "pending"
    assert review["strict_acceptance"] == "pending"
    assert review["model_claims"] == answer["claims"]
    assert review["answer_rewritten"] is False
    facts = preflight.build_authoritative_fact_view(index)
    assert review["authoritative_facts_sha256"] == hashlib.sha256(json.dumps(
        facts, ensure_ascii=False, sort_keys=True, allow_nan=False,
    ).encode("utf-8")).hexdigest()


@pytest.mark.parametrize("kind", ["confirmed", "limitation", "hypothesis", "next_check"])
def test_each_permitted_claim_kind_is_accepted_mechanically(
    preflight,
    delivered,
    kind,
):
    _, _, index = delivered
    answer = answer_from_index(index)
    answer["claims"][0]["kind"] = kind
    review = validate(preflight, answer, index)
    assert review["output_structure"] == "passed"
    assert review["strict_acceptance"] == "pending"


def test_claim_count_text_and_next_check_limits_accept_the_exact_boundary(
    preflight,
    delivered,
):
    _, _, index = delivered
    references = [item["observation_id"] for item in index["sources"]]
    answer = {"claims": [{
        "kind": "next_check" if number < 3 else "limitation",
        "text": "界" * 240,
        "observation_ids": references,
    } for number in range(10)]}
    review = validate(preflight, answer, index)
    assert review["output_structure"] == "passed"
    assert review["strict_acceptance"] == "pending"


@pytest.mark.parametrize("mutation", [
    "sources_copy", "legacy_conclusion", "missing_claims", "empty_claims",
    "claims_not_array", "claim_not_object", "unknown_kind", "kind_not_string",
    "missing_kind",
    "missing_text", "empty_text", "whitespace_text", "text_not_string",
    "extra_claim_field", "fabricated_source", "empty_references",
    "references_not_array", "reference_not_string", "duplicate_reference",
    "too_many_references", "too_many_claims", "too_many_next_checks",
    "too_long_claim", "too_long_total",
])
def test_claim_structure_and_reference_errors_fail_acceptance(
    preflight,
    delivered,
    mutation,
):
    _, _, index = delivered
    answer = answer_from_index(index)
    claim = answer["claims"][0]
    if mutation == "sources_copy":
        answer["sources"] = deepcopy(index["sources"])
    elif mutation == "legacy_conclusion":
        answer["conclusion"] = {"confirmed": []}
    elif mutation == "missing_claims":
        del answer["claims"]
    elif mutation == "empty_claims":
        answer["claims"] = []
    elif mutation == "claims_not_array":
        answer["claims"] = {}
    elif mutation == "claim_not_object":
        answer["claims"][0] = "unstructured explanation"
    elif mutation == "unknown_kind":
        claim["kind"] = "root_cause_confirmed"
    elif mutation == "kind_not_string":
        claim["kind"] = []
    elif mutation == "missing_kind":
        del claim["kind"]
    elif mutation == "missing_text":
        del claim["text"]
    elif mutation == "empty_text":
        claim["text"] = ""
    elif mutation == "whitespace_text":
        claim["text"] = "   "
    elif mutation == "text_not_string":
        claim["text"] = 12
    elif mutation == "extra_claim_field":
        claim["evaluation_time"] = AT.isoformat()
    elif mutation == "fabricated_source":
        claim["observation_ids"] = ["obs-not-provided-to-this-request"]
    elif mutation == "empty_references":
        claim["observation_ids"] = []
    elif mutation == "references_not_array":
        claim["observation_ids"] = claim["observation_ids"][0]
    elif mutation == "reference_not_string":
        claim["observation_ids"] = [None]
    elif mutation == "duplicate_reference":
        claim["observation_ids"] *= 2
    elif mutation == "too_many_references":
        claim["observation_ids"] += ["fabricated-extra-source"]
    elif mutation == "too_many_claims":
        answer["claims"] *= 11
    elif mutation == "too_many_next_checks":
        claim["kind"] = "next_check"
        answer["claims"] *= 4
    elif mutation == "too_long_claim":
        claim["text"] = "界" * 601
    else:
        claim["text"] = "界" * 600
        answer["claims"] *= 5
    original = deepcopy(answer)
    review = validate(preflight, answer, index)
    assert_failed(review)
    assert review["model_claims"] == answer.get("claims")
    assert answer == original


@pytest.mark.parametrize("invalid", [
    "{", "[]", "null", "{}", '{"claims":[],"claims":[]}',
    '{"claims":NaN}', '{"claims":Infinity}', '{"claims":-Infinity}',
    '{"claims":[{"kind":"limitation","text":"incomplete"}',
])
def test_invalid_duplicate_or_nonfinite_json_is_rejected(
    preflight,
    delivered,
    invalid,
):
    _, _, index = delivered
    assert_failed(validate(preflight, invalid, index))


def test_duplicate_key_inside_otherwise_valid_claim_is_rejected(
    preflight,
    delivered,
):
    _, _, index = delivered
    answer = json.dumps(answer_from_index(index), ensure_ascii=False)
    duplicate = answer.replace(
        '"kind": "limitation"', '"kind": "confirmed", "kind": "limitation"', 1,
    )
    assert json.loads(duplicate) == json.loads(answer)
    assert_failed(validate(preflight, duplicate, index))


@pytest.mark.parametrize("wrong_text", [
    "此样本的查询级 evaluation_time 明确是 1900-01-01T00:00:00+00:00。",
    "版本探测 no_data 已证明业务服务不存在。",
    "历史数据已经证明当前业务恢复。",
    "已经确认数据库连接耗尽是故障根因，无须追加证据。",
    "该请求量很高且样本量充分，足以确认容量不足。",
])
def test_real_reference_does_not_make_wrong_reasoning_correct_or_mutate_facts(
    preflight,
    delivered,
    wrong_text,
):
    _, records, index = delivered
    source = next(item for item in index["sources"] if item["name"] == "query_metrics")
    answer = {"claims": [{
        "kind": "confirmed",
        "text": wrong_text,
        "observation_ids": [source["observation_id"]],
    }]}
    raw_answer = json.dumps(answer, ensure_ascii=False)
    originals = deepcopy(records), deepcopy(index), raw_answer
    facts = preflight.build_authoritative_fact_view(index)
    original_facts = deepcopy(facts)
    review = preflight.validate_model_answer(raw_answer, index)
    assert review["output_structure"] == "passed"
    assert review["manual_reasoning_review"] == "required"
    assert review["model_reasoning_correctness"] == "pending"
    assert review["strict_acceptance"] == "pending"
    report = preflight.compose_preflight_report(
        facts, review["model_claims"], review, runtime_completed=True,
    )
    assert report["authoritative_facts"] == original_facts
    displayed = next(
        item for item in report["authoritative_facts"]["sources"]
        if item["observation_id"] == source["observation_id"]
    )
    assert displayed["evaluation_time"] == {"provided": False, "value": "未提供"}
    assert displayed["evaluated_at"]["provided"] is True
    assert report["model_interpretation"]["claims"] == answer["claims"]
    assert report["model_interpretation"]["semantic_review"] == "pending_manual_review"
    assert report["acceptance"]["model_reasoning_correctness"] == "pending"
    assert report["acceptance"]["overall_strict_acceptance"] == "pending"
    assert (records, index, raw_answer) == originals
    assert facts == original_facts


def test_preflight_report_separates_runtime_facts_structure_and_reasoning(
    preflight,
    delivered,
):
    _, _, index = delivered
    facts = preflight.build_authoritative_fact_view(index)
    review = validate(preflight, answer_from_index(index), index)
    report = preflight.compose_preflight_report(
        facts, review["model_claims"], review, runtime_completed=True,
    )
    assert report["authoritative_facts"] == facts
    assert report["model_interpretation"]["mechanical_validation"] == "passed"
    assert report["model_interpretation"]["semantic_review"] == "pending_manual_review"
    assert report["model_interpretation"]["reasoning_review"] is None
    assert report["acceptance"] == {
        "runtime_investigation": "passed",
        "authoritative_fact_integrity": "passed",
        "model_output_structure": "passed",
        "model_reasoning_correctness": "pending",
        "overall_strict_acceptance": "pending",
    }


@pytest.mark.parametrize("failure", ["runtime", "structure"])
def test_report_cannot_pass_when_runtime_or_output_structure_failed(
    preflight,
    delivered,
    failure,
):
    _, _, index = delivered
    facts = preflight.build_authoritative_fact_view(index)
    answer = answer_from_index(index)
    if failure == "structure":
        answer["claims"][0]["observation_ids"] = ["fabricated-source"]
    review = validate(preflight, answer, index)
    report = preflight.compose_preflight_report(
        facts, review["model_claims"], review, runtime_completed=failure != "runtime",
    )
    assert report["acceptance"]["overall_strict_acceptance"] == "failed"
    assert report["acceptance"]["authoritative_fact_integrity"] == "passed"
    assert report["acceptance"]["model_reasoning_correctness"] == "pending"
    assert report["model_interpretation"]["claims"] == answer["claims"]


def test_failed_manual_review_retains_wrong_claim_and_authoritative_facts(
    preflight,
    delivered,
):
    _, records, index = delivered
    facts = preflight.build_authoritative_fact_view(index)
    source = next(item for item in index["sources"] if item["name"] == "query_metrics")
    answer = {"claims": [{
        "kind": "confirmed",
        "text": "该来源的查询级 evaluation_time 已提供，且历史数据证明当前恢复。",
        "observation_ids": [source["observation_id"]],
    }]}
    validation = validate(preflight, answer, index)
    attestation = reasoning_review_for(answer["claims"], facts, verdict="failed")
    attestation["claim_reviews"][0]["reason"] = (
        "来源只有样本 evaluated_at，没有查询 evaluation_time；历史证据不能证明当前恢复。"
    )
    originals = deepcopy((records, index, facts, answer, validation, attestation))
    report = preflight.compose_preflight_report(
        facts, validation["model_claims"], validation,
        runtime_completed=True, reasoning_review=attestation,
    )
    assert report["authoritative_facts"] == facts
    assert report["model_interpretation"]["claims"] == answer["claims"]
    assert report["model_interpretation"]["mechanical_validation"] == "passed"
    assert report["model_interpretation"]["semantic_review"] == "failed_manual_review"
    assert report["model_interpretation"]["reasoning_review"] == attestation
    assert report["acceptance"]["model_reasoning_correctness"] == "failed"
    assert report["acceptance"]["overall_strict_acceptance"] == "failed"
    assert (records, index, facts, answer, validation, attestation) == originals


@pytest.mark.parametrize("method", ["human", "independent_evidence_review"])
def test_complete_bound_review_can_record_acceptance_after_all_layers_pass(
    preflight,
    delivered,
    method,
):
    _, _, index = delivered
    facts = preflight.build_authoritative_fact_view(index)
    answer = answer_from_index(index)
    validation = validate(preflight, answer, index)
    attestation = reasoning_review_for(answer["claims"], facts, method=method)
    report = preflight.compose_preflight_report(
        facts, validation["model_claims"], validation,
        runtime_completed=True, reasoning_review=attestation,
    )
    expected = "passed_manual_review" if method == "human" else "passed_independent_review"
    assert report["model_interpretation"]["semantic_review"] == expected
    assert report["model_interpretation"]["reasoning_review"] == attestation
    assert report["acceptance"]["model_reasoning_correctness"] == "passed"
    assert report["acceptance"]["overall_strict_acceptance"] == "passed"
    # The checker accepts a complete attestation; it does not prove its truth.
    assert validation["model_reasoning_correctness"] == "pending"
    assert validation["manual_reasoning_review"] == "required"


@pytest.mark.parametrize("mutation", [
    "empty", "wrong_hash", "wrong_facts_hash", "missing_claim_review", "wrong_references",
    "duplicate_index", "boolean_index", "negative_index", "out_of_range_index",
    "empty_reason", "unknown_verdict", "missing_reviewer", "empty_reviewer",
    "unknown_method",
])
def test_incomplete_or_unbound_review_cannot_promote_acceptance(
    preflight,
    delivered,
    mutation,
):
    _, _, index = delivered
    facts = preflight.build_authoritative_fact_view(index)
    answer = answer_from_index(index)
    answer["claims"].append(deepcopy(answer["claims"][0]))
    validation = validate(preflight, answer, index)
    attestation = reasoning_review_for(answer["claims"], facts)
    row = attestation["claim_reviews"][0]
    if mutation == "empty":
        attestation = {}
    elif mutation == "wrong_hash":
        attestation["claims_sha256"] = "0" * 64
    elif mutation == "wrong_facts_hash":
        attestation["authoritative_facts_sha256"] = "0" * 64
    elif mutation == "missing_claim_review":
        attestation["claim_reviews"].pop()
    elif mutation == "wrong_references":
        row["observation_ids"] = ["different-source"]
    elif mutation == "duplicate_index":
        attestation["claim_reviews"][1]["claim_index"] = 0
    elif mutation == "boolean_index":
        row["claim_index"] = True
    elif mutation == "negative_index":
        row["claim_index"] = -1
    elif mutation == "out_of_range_index":
        row["claim_index"] = len(answer["claims"])
    elif mutation == "empty_reason":
        row["reason"] = "   "
    elif mutation == "unknown_verdict":
        row["verdict"] = "probably-correct"
    elif mutation == "missing_reviewer":
        del attestation["reviewer"]
    elif mutation == "empty_reviewer":
        attestation["reviewer"] = "   "
    else:
        attestation["method"] = "automatic_format_check"
    with pytest.raises(ValueError, match="preflight_incomplete_reasoning_review"):
        preflight.compose_preflight_report(
            facts, validation["model_claims"], validation,
            runtime_completed=True, reasoning_review=attestation,
        )


def test_review_cannot_override_failed_output_structure(
    preflight,
    delivered,
):
    _, _, index = delivered
    facts = preflight.build_authoritative_fact_view(index)
    answer = answer_from_index(index)
    answer["claims"][0]["observation_ids"] = ["fabricated-source"]
    validation = validate(preflight, answer, index)
    attestation = reasoning_review_for(answer["claims"], facts)
    with pytest.raises(ValueError, match="preflight_incomplete_reasoning_review"):
        preflight.compose_preflight_report(
            facts, validation["model_claims"], validation,
            runtime_completed=True, reasoning_review=attestation,
        )


def test_altered_fact_view_fails_integrity_even_with_valid_claim_structure(
    preflight,
    delivered,
):
    _, _, index = delivered
    facts = preflight.build_authoritative_fact_view(index)
    validation = validate(preflight, answer_from_index(index), index)
    original_index = deepcopy(index)
    altered = deepcopy(facts)
    source = next(item for item in altered["sources"] if item["name"] == "query_metrics")
    source["evaluation_time"] = {"provided": True, "value": AT.isoformat()}
    report = preflight.compose_preflight_report(
        altered, validation["model_claims"], validation, runtime_completed=True,
    )
    assert report["acceptance"]["authoritative_fact_integrity"] == "failed"
    assert report["acceptance"]["model_output_structure"] == "passed"
    assert report["acceptance"]["overall_strict_acceptance"] == "failed"
    assert index == original_index
    assert preflight.build_authoritative_fact_view(index) == facts


def test_review_for_original_facts_cannot_be_reused_for_changed_facts(
    preflight,
    delivered,
):
    body, records, index = delivered
    facts = preflight.build_authoritative_fact_view(index)
    answer = answer_from_index(index)
    attestation = reasoning_review_for(answer["claims"], facts)
    records = deepcopy(records)
    observations = deepcopy(list(request_observations(body).values()))
    records[0]["collected_at"] = (COLLECTED + timedelta(days=1)).isoformat()
    next(
        item for item in observations
        if item["observation_id"] == records[0]["observation_id"]
    )["collected_at"] = records[0]["collected_at"]
    changed_index = preflight.build_evidence_index(records, observations)
    changed_facts = preflight.build_authoritative_fact_view(changed_index)
    validation = validate(preflight, answer, changed_index)
    assert validation["output_structure"] == "passed"
    assert changed_facts != facts
    with pytest.raises(ValueError, match="preflight_incomplete_reasoning_review"):
        preflight.compose_preflight_report(
            changed_facts, validation["model_claims"], validation,
            runtime_completed=True, reasoning_review=attestation,
        )


def test_wrong_narrative_is_preserved_in_sdk_artifacts_without_extra_requests(
    preflight,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "mock-provider-no-real-credential")
    sdk = fake_sdk()
    captured = {}

    def answer_request(**body):
        index = request_index(body)
        source_id = next(
            item["observation_id"] for item in index["sources"]
            if item["name"] == "query_metrics"
        )
        answer = {"claims": [{
            "kind": "confirmed",
            "text": "查询级 evaluation_time 是 1900-01-01，历史数据证明当前已经恢复。",
            "observation_ids": [source_id],
        }]}
        text = json.dumps(answer, ensure_ascii=False)
        result = response(answer=text)
        raw_text = result.model_dump_json() + "\n"
        captured.update(answer=text, raw=raw_text, index=deepcopy(index))
        return SimpleNamespace(text=raw_text, parse=lambda: result)

    sdk.responses.with_raw_response.create.side_effect = answer_request
    summary = preflight.run_case(tmp_path, live=True, sdk=sdk)
    checkpoint = read_json(tmp_path, "checkpoint.json")
    assert checkpoint["result"]["answer"] == captured["answer"]
    assert (tmp_path / "response.raw.json").read_text(encoding="utf-8") == (
        captured["raw"]
    )
    assert read_json(tmp_path, "evidence-index.json") == captured["index"]
    review = validate(preflight, captured["answer"], captured["index"])
    assert review["output_structure"] == "passed"
    assert review["strict_acceptance"] == "pending"
    assert review["manual_reasoning_review"] == "required"
    assert summary["manual_reasoning_review"] == "required"
    assert summary["runtime_model_reservations"] == 1
    assert summary["tool_executions"] == 1
    assert summary["real_model_api_requests"] == 1
    body = read_json(tmp_path, "request.json")
    assert body["tools"] == []
    assert body["tool_choice"] == "none"
    assert body["max_output_tokens"] == 2048
    assert body["reasoning"] == {"effort": "none"}
    sdk.with_options.assert_called_once_with(max_retries=0, timeout=30)
    sdk.responses.with_raw_response.create.assert_called_once()


def test_incomplete_response_with_valid_claims_still_fails_and_keeps_raw_output(
    preflight,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "mock-provider-no-real-credential")
    sdk = fake_sdk()
    captured = {}

    def answer_request(**body):
        answer = answer_from_index(request_index(body))
        text = json.dumps(answer, ensure_ascii=False)
        result = response(answer=text, status="incomplete")
        raw = result.model_dump_json() + "\n"
        captured.update(answer=text, claims=deepcopy(answer["claims"]), raw=raw)
        return SimpleNamespace(text=raw, parse=lambda: result)

    sdk.responses.with_raw_response.create.side_effect = answer_request
    summary = preflight.run_case(tmp_path, live=True, sdk=sdk)
    validation = read_json(tmp_path, "answer-validation.json")
    report = read_json(tmp_path, "preflight-report.json")
    assert summary["session_status"] != "COMPLETED"
    assert validation["model_claims"] == captured["claims"]
    assert validation["output_structure"] == "failed"
    assert "response_not_completed" in validation["errors"]
    assert report["model_interpretation"]["claims"] == captured["claims"]
    assert report["model_interpretation"]["raw_answer"] == captured["answer"]
    assert report["acceptance"]["overall_strict_acceptance"] == "failed"
    assert (tmp_path / "answer-original.txt").read_text(encoding="utf-8") == (
        captured["answer"]
    )
    assert (tmp_path / "response.raw.json").read_text(encoding="utf-8") == captured["raw"]
    assert summary["runtime_model_reservations"] == 1
    assert summary["real_model_api_requests"] == 1
    assert summary["tool_executions"] == 1
    sdk.responses.with_raw_response.create.assert_called_once()
