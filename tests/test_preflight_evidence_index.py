"""P5.3c.2c deterministic evidence delivery, using only Mock Providers."""
from copy import deepcopy
from datetime import timedelta
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
    """The expected fields are local test data, never a model input rubric."""
    source_ids = [item["observation_id"] for item in index["sources"]]
    conclusion = {
        key: [{"text": "此项解释仍须人工复核。", "observation_ids": source_ids}]
        for key in (
            "confirmed", "time_comparison", "missing_vs_zero", "unconfirmed",
            "next_checks",
        )
    }
    return {
        "sources": [{
            "observation_id": item["observation_id"],
            "collected_at": item["collected_at"],
            "has_evaluation_time": "evaluation_time" in item,
            "evaluation_time": item.get("evaluation_time"),
            "status": item["status"],
            "samples": [{
                "metric": sample["metric"],
                "status": sample["status"],
                "value": sample.get("value"),
                "evaluated_at": sample.get("evaluated_at"),
            } for sample in item["samples"]],
        } for item in index["sources"]],
        "conclusion": conclusion,
    }


def validate(preflight, answer, index):
    text = answer if isinstance(answer, str) else json.dumps(
        answer, ensure_ascii=False, allow_nan=False,
    )
    return preflight.validate_model_answer(text, index)


def assert_failed(review):
    assert review["fact_check"] == "failed"
    assert review["errors"]
    assert review["manual_reasoning_review"] == "required"
    assert review["strict_acceptance"] == "failed"


def test_real_mock_runtime_indexes_all_five_persisted_sources(
    preflight,
    delivered,
    tmp_path,
):
    body, records, index = delivered
    observations = request_observations(body)
    assert index["schema_version"] == 1
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
    rebuilt = preflight.build_evidence_index(records, list(observations.values()))
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


def test_exact_structured_fields_pass_only_mechanical_review(preflight, delivered):
    _, _, index = delivered
    answer = answer_from_index(index)
    review = validate(preflight, answer, index)
    assert review["fact_check"] == "passed"
    assert review["errors"] == []
    assert review["manual_reasoning_review"] == "required"
    assert review["strict_acceptance"] == "pending"


@pytest.mark.parametrize("mutation", [
    "unified_collection_date", "invented_version_query_time",
    "invented_missing_sample_time",
    "missing_sample_zero", "omitted_source", "fabricated_source", "duplicate_source",
    "missing_source_field", "wrong_status", "wrong_query_presence", "wrong_valid_value",
    "boolean_as_number", "missing_sample", "duplicate_sample", "invented_sample",
    "missing_conclusion", "empty_conclusion", "unknown_conclusion_reference",
    "missing_conclusion_reference", "too_many_next_checks",
])
def test_structured_field_and_reference_errors_fail_acceptance(
    preflight,
    delivered,
    mutation,
):
    _, _, index = delivered
    answer = answer_from_index(index)
    version = next(item for item in answer["sources"] if not item["samples"])
    aligned = next(
        item for item in answer["sources"]
        if any(sample["status"] == "no_data" for sample in item["samples"])
    )
    missing = next(item for item in aligned["samples"] if item["status"] == "no_data")
    valid = next(item for item in aligned["samples"] if item["status"] == "ok")
    if mutation == "unified_collection_date":
        version["collected_at"] = answer["sources"][0]["collected_at"]
    elif mutation == "invented_version_query_time":
        version.update(has_evaluation_time=True, evaluation_time=AT.isoformat())
    elif mutation == "invented_missing_sample_time":
        missing["evaluated_at"] = aligned["evaluation_time"]
    elif mutation == "missing_sample_zero":
        missing["value"] = 0.0
    elif mutation == "omitted_source":
        answer["sources"].remove(version)
    elif mutation == "fabricated_source":
        version["observation_id"] = "obs-not-provided-to-this-request"
    elif mutation == "duplicate_source":
        answer["sources"].append(deepcopy(version))
    elif mutation == "missing_source_field":
        del version["collected_at"]
    elif mutation == "wrong_status":
        version["status"] = "ok"
    elif mutation == "wrong_query_presence":
        aligned["has_evaluation_time"] = False
    elif mutation == "wrong_valid_value":
        valid["value"] = 999
    elif mutation == "boolean_as_number":
        valid["value"] = False
    elif mutation == "missing_sample":
        aligned["samples"].remove(missing)
    elif mutation == "duplicate_sample":
        aligned["samples"].append(deepcopy(missing))
    elif mutation == "invented_sample":
        version["samples"].append({
            "metric": "invented", "status": "ok", "value": 0.0, "evaluated_at": None,
        })
    elif mutation == "missing_conclusion":
        del answer["conclusion"]
    elif mutation == "empty_conclusion":
        answer["conclusion"]["confirmed"] = []
    elif mutation == "unknown_conclusion_reference":
        answer["conclusion"]["confirmed"][0]["observation_ids"] = ["obs-fabricated"]
    elif mutation == "missing_conclusion_reference":
        answer["conclusion"]["confirmed"][0]["observation_ids"] = []
    else:
        answer["conclusion"]["next_checks"] *= 4
    assert_failed(validate(preflight, answer, index))


@pytest.mark.parametrize("invalid", [
    "{", "[]", "null", "{}", '{"sources":[],"conclusion":{}}',
    '{"sources":[],"sources":[],"conclusion":{}}',
    '{"sources":NaN,"conclusion":{}}',
    '{"sources":Infinity,"conclusion":{}}',
    '{"sources":-Infinity,"conclusion":{}}',
])
def test_invalid_duplicate_or_nonfinite_json_is_rejected(
    preflight,
    delivered,
    invalid,
):
    _, _, index = delivered
    assert_failed(validate(preflight, invalid, index))


def test_duplicate_key_in_otherwise_correct_answer_is_rejected(
    preflight,
    delivered,
):
    _, _, index = delivered
    answer = answer_from_index(index)
    text = json.dumps(answer, ensure_ascii=False)
    duplicate = '{"sources": ' + json.dumps(answer["sources"]) + ", " + text[1:]
    assert json.loads(duplicate) == answer
    assert_failed(validate(preflight, duplicate, index))


@pytest.mark.parametrize("nonnumeric", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_sample_values_are_rejected_after_json_parsing(
    preflight,
    delivered,
    nonnumeric,
):
    _, _, index = delivered
    answer = answer_from_index(index)
    sample = next(
        sample for item in answer["sources"] for sample in item["samples"]
        if sample["status"] == "ok"
    )
    sample["value"] = nonnumeric
    assert_failed(validate(preflight, json.dumps(answer), index))


def test_wrong_huge_integer_is_rejected_without_validator_overflow(
    preflight,
    delivered,
):
    _, _, index = delivered
    answer = answer_from_index(index)
    sample = next(
        sample for item in answer["sources"] for sample in item["samples"]
        if sample["status"] == "ok"
    )
    sample["value"] = 10 ** 500
    assert_failed(validate(preflight, answer, index))


@pytest.mark.parametrize("number", ["1e-999", "1e999", "false"])
def test_nonzero_underflow_large_exponent_or_boolean_cannot_equal_valid_zero(
    preflight,
    delivered,
    number,
):
    _, _, index = delivered
    answer = json.dumps(answer_from_index(index), ensure_ascii=False)
    assert '"value": 0.0' in answer
    corrupted = answer.replace('"value": 0.0', f'"value": {number}', 1)
    assert_failed(validate(preflight, corrupted, index))


def test_field_correct_but_wrong_narrative_remains_pending_and_unmodified(
    preflight,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "mock-provider-no-real-credential")
    sdk = fake_sdk()
    captured = {}

    def answer_request(**body):
        index = request_index(body)
        answer = answer_from_index(index)
        version_id = next(
            item["observation_id"] for item in index["sources"]
            if item["name"] == "get_running_version"
        )
        answer["conclusion"]["confirmed"] = [{
            "text": "版本探测 no_data 已证明业务服务不存在，历史数据证明当前已经恢复。",
            "observation_ids": [version_id],
        }]
        text = json.dumps(answer, ensure_ascii=False)
        result = response(answer=text)
        raw_text = result.model_dump_json() + "\n"
        captured.update(answer=text, raw=raw_text, index=index)
        return SimpleNamespace(text=raw_text, parse=lambda: result)

    sdk.responses.with_raw_response.create.side_effect = answer_request
    summary = preflight.run_case(tmp_path, live=True, sdk=sdk)
    checkpoint = read_json(tmp_path, "checkpoint.json")
    assert checkpoint["result"]["answer"] == captured["answer"]
    assert (tmp_path / "response.raw.json").read_text(encoding="utf-8") == (
        captured["raw"]
    )
    review = validate(preflight, captured["answer"], captured["index"])
    assert review["fact_check"] == "passed"
    assert review["strict_acceptance"] == "pending"
    assert review["manual_reasoning_review"] == "required"
    assert summary["manual_reasoning_review"] == "pending"
    assert summary["runtime_model_reservations"] == 1
    assert summary["tool_executions"] == 1
    sdk.responses.with_raw_response.create.assert_called_once()
