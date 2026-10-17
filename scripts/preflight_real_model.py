"""P5.3c.2e isolated preflight; default mode never constructs a network client.

Paid mode requires explicit approval flags. No production entry
point or background service is imported. Artifacts stay in a fresh OS temp dir.
"""
import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import logging
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODEL = "deepseek-flash"
BASE_URL = "https://api.deepseek.com"
MAX_MODEL_REQUESTS = 1
MAX_TOOL_EXECUTIONS = 1
MAX_OUTPUT_TOKENS = 2048
MAX_REQUEST_BYTES = 98304
MODEL_TIMEOUT_SECONDS = 30
WALL_TIMEOUT_SECONDS = 90
OFFLINE_ANSWER = "[离线传输占位回答；未调用真实模型，不证明模型理解指标。]"
TASK = """
复核所提供的历史证据。权威事实由 Python 根据 evidence_index 生成，
模型只输出解释、局限、假设和取证建议，不再生成 sources 事实副本。
只输出一个 JSON 对象，唯一顶层字段 claims 为 1～10 项数组，每项仅含
kind、text、observation_ids，可另含 context_refs。kind 只能为 confirmed、limitation、hypothesis、
next_check；text 为简短中文，每项最多 600 字、总计最多 2400 字，建议总计
约 250～350 汉字；next_check 最多三项进一步取证方向。observation_ids
为不重复的实际工具 observation_id 数组；context_refs 为不重复的
{type, id} 数组，type 和 id 必须对应 reference_context.context_sources 中的
source_type 和 reference_id。每项至少有一个工具或上下文引用。
告警、能力/依赖配置、身份关联和集合范围分别引用对应上下文，不归入工具 ID。
能力不代表已经执行，身份关联不认证当前部署；集合仅覆盖所注明的持久化快照，
未保存某类结果不代表现实中不存在该证据。引用存在不代表引用支持相应陈述。
只解释证据关系，不复述 collected_at、evaluation_time、evaluated_at 或
原始指标数值表。collected_at 只表示采集；evaluation_time 只表示查询目标；
evaluated_at 只归属于具体样本，缺失时不继承查询或邻项时间。
不将缺失字段当作存在，不把 no_data 当服务不存在或恢复，不把历史当当前状态，
不将假设确认为根因，不添加无基线支持的流量高低、样本量或故障原因。
重复观测不是独立佐证。每项就近引用支持它的来源，不能确认的推理保留不确定性。
不用代码围栏，不输出内部推理过程，不执行工具或修复。
"""


def runtime_observations(model_input):
    """Read only the isolated entry's actual evidence-rebuild envelope."""
    payloads = []
    for message in model_input:
        if message.get("role") != "user" or not isinstance(message.get("content"), str):
            continue
        try:
            payload = json.loads(message["content"].rsplit("\n", 1)[-1])
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and "recovery" in payload and all(
            key in payload for key in (
                "historical_observations", "failed_or_missing_observations",
                "metric_analysis", "context_collected_at",
            )
        ):
            payloads.append(payload)
    if len(payloads) != 1:
        raise ValueError("preflight_requires_one_runtime_evidence_rebuild")
    payload = payloads[0]
    return (
        payload["historical_observations"]
        + payload["failed_or_missing_observations"]
    )


def build_evidence_index(
    records,
    observations,
    *,
    runtime_input=None,
    source_snapshot=None,
):
    """Extract fields, preserving absence. Never infer times or metric analysis.

    historical_only means Runtime disallows current-state use, including missing
    version evidence; it does not classify that version probe as a metric.
    Unknown tools/shapes remain explicit unsupported entries, not guessed data.
    """
    ids = [item["observation_id"] for item in records]
    observed_ids = [item["observation_id"] for item in observations]
    if (
        any(
            not isinstance(item, str) or not item or item.startswith("context:")
            for item in ids
        )
        or len(set(ids)) != len(ids)
        or len(set(observed_ids)) != len(observed_ids)
        or set(ids) != set(observed_ids)
    ):
        raise ValueError("preflight_evidence_source_mismatch")
    observed = {item["observation_id"]: item for item in observations}
    sources = []
    for record in records:
        context = observed[record["observation_id"]]
        for key in ("name", "collected_at", "arguments"):
            if context[key] != record[key]:
                raise ValueError("preflight_runtime_source_field_mismatch")
        if type(context.get("use_for_current_state")) is not bool:
            raise ValueError("preflight_runtime_usage_missing")
        result = record["result"]
        if context["status"] != result.get("status"):
            raise ValueError("preflight_runtime_status_mismatch")
        entry = {
            "observation_id": record["observation_id"],
            "name": record["name"],
            "collected_at": record["collected_at"],
            "status": result.get("status"),
            "historical_only": not context["use_for_current_state"],
            "data_missing": None,
            "extraction_status": "supported",
            "samples": [],
        }
        for key in ("evaluation_time", "evaluated_at"):
            if key in result:
                entry[key] = deepcopy(result[key])
        samples = []
        if record["name"] == "query_metrics_aligned" and isinstance(
            result.get("results"), dict,
        ):
            samples = list(result["results"].items())
            requested = result.get("requested_metrics")
            if (
                not samples
                or not isinstance(requested, list)
                or set(requested) != set(result["results"])
                or result.get("status") not in {"ok", "incomplete", "no_data"}
            ):
                entry["extraction_status"] = "unsupported"
        elif record["name"] == "query_metrics" and isinstance(
            result.get("metric"), str,
        ):
            samples = [(result["metric"], result)]
            if result.get("status") not in {"ok", "no_data"}:
                entry["extraction_status"] = "unsupported"
        elif (
            record["name"] != "get_running_version"
            or result.get("status") != "no_data"
        ):
            # Only the local no-identity version result is supported here.
            # This experiment is not a general version-result interpreter.
            entry["extraction_status"] = "unsupported"
        for metric, sample in samples:
            if (
                not isinstance(sample, dict)
                or sample.get("metric") != metric
                or not isinstance(sample.get("status"), str)
            ):
                entry["extraction_status"] = "unsupported"
                continue
            extracted = {"metric": metric, "status": sample["status"]}
            for key in ("value", "evaluated_at"):
                if key in sample:
                    extracted[key] = deepcopy(sample[key])
            entry["samples"].append(extracted)
            if sample["status"] == "ok":
                value = sample.get("value")
                if type(value) not in (int, float) or (
                    type(value) is float and not math.isfinite(value)
                ):
                    entry["extraction_status"] = "unsupported"
            elif sample["status"] == "no_data" and (
                "value" in sample or "evaluated_at" in sample
            ):
                # Retain the contradictory raw fields, but refuse paid use.
                entry["extraction_status"] = "unsupported"
        if not isinstance(entry["status"], str):
            entry["extraction_status"] = "unsupported"
        if entry["extraction_status"] == "supported":
            entry["data_missing"] = entry["status"] == "no_data" or any(
                item["status"] == "no_data" for item in entry["samples"]
            )
        else:
            entry["unsupported_reason"] = "tool_or_result_shape_not_supported"
        sources.append(entry)
    index = {
        "schema_version": 1,
        "provenance": "preflight_deterministic_derived",
        "sources": sources,
    }
    if runtime_input is not None or source_snapshot is not None:
        from scripts.preflight_evidence_refs import build_reference_context

        index["reference_context"] = build_reference_context(
            records, runtime_input, source_snapshot,
        )
        index["schema_version"] = 2
        for source in sources:
            source["source_type"] = "observation"
            source["evidence_permissions"] = {
                "may_support": ["persisted_tool_observation_fields"],
                "use_for_current_state": not source["historical_only"],
                "cannot_establish": ["claim_semantic_support_by_id_alone"],
            }
    return index


def verify_index_references(
    evidence_index,
    *,
    runtime_input=None,
    source_snapshot=None,
):
    """Recheck catalogue integrity; external sources also recheck provenance.

    Without external inputs the caller trusts the Python-built index, as in v1.
    A digest is not a signature or proof that a supplied source is authentic.
    """
    version = evidence_index.get("schema_version")
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("preflight_invalid_evidence_schema")
    ids = [source["observation_id"] for source in evidence_index["sources"]]
    if (
        any(
            not isinstance(ref, str) or not ref or ref.startswith("context:")
            for ref in ids
        )
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("preflight_invalid_observation_namespace")
    if version == 1:
        if "reference_context" in evidence_index:
            raise ValueError("preflight_invalid_evidence_schema")
        if runtime_input is not None or source_snapshot is not None:
            if runtime_input is None or not isinstance(source_snapshot, dict):
                raise ValueError("preflight_reference_external_sources_incomplete")
            rebuilt = build_evidence_index(
                source_snapshot["records"], runtime_observations(runtime_input),
            )
            if rebuilt != evidence_index:
                raise ValueError("preflight_evidence_source_binding_mismatch")
        return
    if any(
        source.get("source_type") != "observation"
        or type(source.get("historical_only")) is not bool
        or source.get("evidence_permissions") != {
            "may_support": ["persisted_tool_observation_fields"],
            "use_for_current_state": not source["historical_only"],
            "cannot_establish": ["claim_semantic_support_by_id_alone"],
        }
        for source in evidence_index["sources"]
    ):
        raise ValueError("preflight_invalid_observation_type")
    from scripts.preflight_evidence_refs import verify_reference_context

    verify_reference_context(
        evidence_index.get("reference_context"),
        ids,
        records=(
            source_snapshot.get("records")
            if isinstance(source_snapshot, dict) else None
        ),
        runtime_input=runtime_input,
        source_snapshot=source_snapshot,
    )
    if runtime_input is not None and source_snapshot is not None:
        rebuilt = build_evidence_index(
            source_snapshot["records"],
            runtime_observations(runtime_input),
            runtime_input=runtime_input,
            source_snapshot=source_snapshot,
        )
        if rebuilt != evidence_index:
            raise ValueError("preflight_evidence_source_binding_mismatch")


def build_authoritative_fact_view(evidence_index):
    """Project verified evidence only; provided=False explicitly means absent.

    A present null remains provided=True. No neighboring field is a fallback.
    Unsupported extraction is rejected rather than presented as trusted facts.
    """
    verify_index_references(evidence_index)
    if not evidence_index["sources"] or any(
        source["extraction_status"] != "supported"
        for source in evidence_index["sources"]
    ):
        raise ValueError("preflight_unsupported_evidence_shape")

    def field(source, key):
        return {
            "provided": key in source,
            "value": deepcopy(source[key]) if key in source else "未提供",
        }

    sources = deepcopy(evidence_index["sources"])
    for original, source in zip(evidence_index["sources"], sources):
        for key in ("evaluation_time", "evaluated_at"):
            source[key] = field(original, key)
        for raw_sample, sample in zip(original["samples"], source["samples"]):
            for key in ("value", "evaluated_at"):
                sample[key] = field(raw_sample, key)
    view = {
        "schema_version": evidence_index["schema_version"],
        "provenance": "preflight_authoritative_fact_view",
        "sources": sources,
    }
    if "reference_context" in evidence_index:
        view["reference_context"] = deepcopy(evidence_index["reference_context"])
    return view


def json_sha256(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_model_answer(
    answer,
    evidence_index,
    *,
    runtime_input=None,
    source_snapshot=None,
):
    """Validate the claims contract, never natural-language truth or support."""
    errors = []
    try:
        verify_index_references(
            evidence_index,
            runtime_input=runtime_input,
            source_snapshot=source_snapshot,
        )
    except (ValueError, TypeError, KeyError):
        errors.append("invalid_reference_context")
    supported = bool(evidence_index["sources"]) and all(
        item["extraction_status"] == "supported"
        for item in evidence_index["sources"]
    )
    if not supported:
        errors.append("unsupported_evidence_index")
    supported = supported and "invalid_reference_context" not in errors

    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate_json_key")
            obj[key] = value
        return obj

    def invalid_constant(value):
        raise ValueError("nonfinite_json_number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite_json_number")
        return number

    try:
        parsed = json.loads(
            answer,
            object_pairs_hook=unique_object,
            parse_constant=invalid_constant,
            parse_float=finite_float,
        )
    except (ValueError, TypeError, RecursionError, OverflowError):
        parsed = None
        errors.append("invalid_json")
    claims = parsed.get("claims") if isinstance(parsed, dict) else None
    if not isinstance(parsed, dict) or set(parsed) != {"claims"}:
        errors.append("invalid_root_structure")
    if not isinstance(claims, list) or not 1 <= len(claims) <= 10:
        errors.append("invalid_claim_count")
    if isinstance(claims, list):
        expected = {item["observation_id"] for item in evidence_index["sources"]}
        contexts = {}
        if "invalid_reference_context" not in errors:
            contexts = {
                item["reference_id"]: item["source_type"]
                for item in evidence_index.get("reference_context", {}).get(
                    "context_sources", [],
                )
            }
        next_checks = 0
        text_length = 0
        for claim in claims:
            if not isinstance(claim, dict) or set(claim) not in (
                {"kind", "text", "observation_ids"},
                {"kind", "text", "observation_ids", "context_refs"},
            ):
                errors.append("invalid_claim_structure")
                continue
            kind = claim["kind"]
            if not isinstance(kind, str) or kind not in {
                "confirmed", "limitation", "hypothesis", "next_check",
            }:
                errors.append("invalid_claim_kind")
            next_checks += kind == "next_check"
            text = claim["text"]
            if not isinstance(text, str) or not text.strip():
                errors.append("empty_or_invalid_claim_text")
            else:
                text_length += len(text)
                if len(text) > 600:
                    errors.append("claim_text_too_long")
            refs = claim["observation_ids"]
            if (
                not isinstance(refs, list)
                or len(refs) > len(expected)
                or any(not isinstance(ref, str) or ref not in expected for ref in refs)
            ):
                errors.append("invalid_claim_references")
            elif len(set(refs)) != len(refs):
                errors.append("duplicate_claim_references")
            context_refs = claim.get("context_refs", [])
            if (
                not isinstance(context_refs, list)
                or (
                    "context_refs" in claim
                    and not 1 <= len(context_refs) <= len(contexts)
                )
                or any(
                    not isinstance(ref, dict)
                    or set(ref) != {"type", "id"}
                    or not isinstance(ref["id"], str)
                    or not isinstance(ref["type"], str)
                    or ref["id"] not in contexts
                    or contexts[ref["id"]] != ref["type"]
                    for ref in context_refs
                )
            ):
                errors.append("invalid_claim_context_references")
            elif len({ref["id"] for ref in context_refs}) != len(context_refs):
                errors.append("duplicate_claim_context_references")
            if refs == [] and context_refs == []:
                errors.append("invalid_claim_references")
        if next_checks > 3:
            errors.append("too_many_next_checks")
        if text_length > 2400:
            errors.append("total_claim_text_too_long")
    return {
        "output_structure": "failed" if errors else "passed",
        "errors": errors,
        "reference_source_check": (
            "failed" if "invalid_reference_context" in errors
            else "passed_external_rebuild"
            if runtime_input is not None and source_snapshot is not None
            else "trusted_index_internal_integrity_only"
        ),
        "manual_reasoning_review": "required",
        "model_reasoning_correctness": "pending",
        "strict_acceptance": "failed" if errors else "pending",
        "answer_rewritten": False,
        "model_claims": deepcopy(claims),
        "authoritative_facts_sha256": (
            json_sha256(build_authoritative_fact_view(evidence_index))
            if supported else None
        ),
    }


def compose_preflight_report(
    fact_view,
    model_claims,
    validation,
    *,
    runtime_completed=False,
    reasoning_review=None,
):
    """Combine independent layers; passing structure never certifies reasoning.

    Optional review is an explicit, evidence-based attestation bound to these
    exact claims. This helper checks its completeness, not the review's truth.
    It has no production acceptance or Incident-state authority.
    """
    if model_claims != validation.get("model_claims"):
        raise ValueError("preflight_claims_validation_mismatch")
    mechanical = validation["output_structure"]
    expected_facts = validation.get("authoritative_facts_sha256")
    actual_facts = json_sha256(fact_view) if fact_view is not None else None
    fact_integrity = (
        "passed" if expected_facts is not None and actual_facts == expected_facts
        else "failed"
    )
    reasoning = "pending"
    semantic_review = "pending_manual_review"
    if reasoning_review is not None:
        review = reasoning_review
        digest = json_sha256(model_claims)
        if (
            mechanical != "passed"
            or fact_integrity != "passed"
            or not isinstance(review, dict)
            or set(review) != {
                "reviewer", "method", "claims_sha256",
                "authoritative_facts_sha256", "claim_reviews",
            }
            or not isinstance(review["reviewer"], str)
            or not review["reviewer"].strip()
            or not isinstance(review["method"], str)
            or review["method"] not in {"human", "independent_evidence_review"}
            or review["claims_sha256"] != digest
            or review["authoritative_facts_sha256"] != actual_facts
            or not isinstance(review["claim_reviews"], list)
            or len(review["claim_reviews"]) != len(model_claims)
        ):
            raise ValueError("preflight_incomplete_reasoning_review")
        seen = set()
        failed = False
        for row in review["claim_reviews"]:
            if not isinstance(row, dict) or set(row) not in (
                {"claim_index", "verdict", "reason", "observation_ids"},
                {"claim_index", "verdict", "reason", "observation_ids", "context_refs"},
            ):
                raise ValueError("preflight_incomplete_reasoning_review")
            number = row["claim_index"]
            if (
                type(number) is not int
                or not 0 <= number < len(model_claims)
                or number in seen
                or not isinstance(row["verdict"], str)
                or row["verdict"] not in {"passed", "failed"}
                or not isinstance(row["reason"], str)
                or not row["reason"].strip()
                or row["observation_ids"] != model_claims[number]["observation_ids"]
                or (
                    ("context_refs" in row)
                    != ("context_refs" in model_claims[number])
                )
                or row.get("context_refs") != model_claims[number].get("context_refs")
            ):
                raise ValueError("preflight_incomplete_reasoning_review")
            seen.add(number)
            failed |= row["verdict"] == "failed"
        reasoning = "failed" if failed else "passed"
        suffix = (
            "manual_review" if review["method"] == "human" else "independent_review"
        )
        semantic_review = f"{reasoning}_{suffix}"
    runtime = "passed" if runtime_completed else "failed"
    states = (runtime, fact_integrity, mechanical, reasoning)
    overall = (
        "failed" if "failed" in states
        else "passed" if all(state == "passed" for state in states)
        else "pending"
    )
    return {
        "scope": "independent_preflight_only",
        "authoritative_facts": deepcopy(fact_view),
        "model_interpretation": {
            "claims": deepcopy(model_claims),
            "mechanical_validation": mechanical,
            "mechanical_errors": deepcopy(validation["errors"]),
            "reference_source_check": validation.get(
                "reference_source_check", "not_recorded",
            ),
            "manual_reasoning_review": "required",
            "semantic_review": semantic_review,
            "reasoning_review": deepcopy(reasoning_review),
        },
        "acceptance": {
            "runtime_investigation": runtime,
            "authoritative_fact_integrity": fact_integrity,
            "model_output_structure": mechanical,
            "model_reasoning_correctness": reasoning,
            "overall_strict_acceptance": overall,
        },
    }


def write_json(
    root,
    name,
    value,
):
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    write_text(root, name, text)


def redact_credential(text):
    # Preserve usage/token fields; only remove a configured credential VALUE if
    # a provider unexpectedly echoes it. Never save HTTP request headers.
    key, _ = credentials()
    if key:
        text = text.replace(key, "[credential redacted]")
    return text


def write_text(
    root,
    name,
    text,
):
    (Path(root) / name).write_text(redact_credential(text), encoding="utf-8")


def credentials():
    from dotenv import dotenv_values

    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key, "process_environment"
    key = (dotenv_values(ROOT / ".env", interpolate=False).get(
        "DEEPSEEK_API_KEY"
    ) or "").strip()
    return key, ".env" if key else "missing"


def build_case():
    from test_metric_analysis import AT, LATER, aligned, instant, record

    sources = [
        aligned("case-a-t1"),
        aligned("case-a-t2", LATER, (0.0, None, 1.0)),
        instant("case-b-errors-t1", "http_5xx_rate", 1.0, AT),
        instant("case-b-rate-t2", "http_request_rate", 0.0, LATER),
    ]
    records = [record(source) for source in sources]
    for item in records:
        item["cache_key"] = hashlib.sha256(json.dumps({
            "name": item["name"], "arguments": item["arguments"],
        }, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "schema_version": 1,
        "provenance": "P5.3c.1 A incomplete + B; imported local test fixtures",
        "not_live_prometheus": True,
        "fixture_request_executed_is_not_this_run_io": True,
        "records": records,
        "groups": {
            "A": ["case-a-t1", "case-a-t2"],
            "B": ["case-b-errors-t1", "case-b-rate-t2"],
        },
        # Kept out of the model request: this is the human review rubric.
        "expected": {
            "a": "T1 rate=0.41821223361699034, 5xx=1, scrape=1; "
                 "T2 +300ms rate=0, 5xx=no_data, scrape=1",
            "b": "independent T1 5xx=1 and T2 rate=0 are not same-time evidence",
            "zero": "finite 0.0 is an observation; no_data must not be zero-filled",
            "alignment": "A snapshots are each aligned; B has different times; "
                         "shared evaluation time is not scrape atomicity",
            "root_cause": "counter reset, database fault and code regression unverified",
            "current_health": "historical observations cannot establish current health/recovery",
            "citations": [source.observation_id for source in sources],
            "grading": "manual review required; COMPLETED is not a reasoning pass",
        },
    }


class BoundedProvider:
    """Test-only SDK boundary; no retries, second request or tool selection."""

    def __init__(
        self,
        sdk,
        root,
        live,
    ):
        self.sdk = sdk
        self.root = Path(root)
        self.live = live
        self.responses = self
        self.requests = 0
        self.real_requests = 0
        self.options = None
        self.load_snapshot = None
        self.source_snapshot = None
        self.runtime_input = None
        self.evidence_index = None
        self.fact_view = None
        self.answer_validation = None
        self.deadline = time.monotonic() + WALL_TIMEOUT_SECONDS

    def with_options(self, *, max_retries, timeout):
        if max_retries != 0 or timeout != MODEL_TIMEOUT_SECONDS:
            raise ValueError("unsafe_sdk_options")
        self.options = {"max_retries": max_retries, "timeout": timeout}
        return self

    def create(self, **body):
        if self.requests >= MAX_MODEL_REQUESTS:
            raise ValueError("preflight_model_request_limit")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("preflight_deadline")
        if self.options is None or body.get("model") != MODEL or body.get("tools") != []:
            raise ValueError("preflight_request_not_allowed")
        if set(body) != {"model", "input", "instructions", "tools"}:
            raise ValueError("preflight_request_fields_not_allowed")
        body = deepcopy(body)
        if self.load_snapshot is not None:
            # The bootstrap version record and model reservation have already
            # committed. Read this request's actual persisted records now.
            self.source_snapshot = self.load_snapshot()
            records = self.source_snapshot["records"]
            self.runtime_input = deepcopy(body["input"])
            self.evidence_index = build_evidence_index(
                records, runtime_observations(body["input"]),
                runtime_input=self.runtime_input,
                source_snapshot=self.source_snapshot,
            )
            verify_index_references(
                self.evidence_index,
                runtime_input=self.runtime_input,
                source_snapshot=self.source_snapshot,
            )
            write_json(self.root, "evidence-index.json", self.evidence_index)
            write_json(self.root, "request-source-records.json", records)
            write_json(self.root, "request-source-snapshot.json", self.source_snapshot)
            write_json(self.root, "runtime-input.json", body["input"])
            if any(
                item["extraction_status"] != "supported"
                for item in self.evidence_index["sources"]
            ):
                raise ValueError("preflight_unsupported_evidence_shape")
            self.fact_view = build_authoritative_fact_view(self.evidence_index)
            write_json(self.root, "authoritative-facts.json", self.fact_view)
            body["input"].append({
                "role": "user",
                "content": json.dumps({
                    "context_origin": "preflight_deterministic_derived",
                    "scope": "independent_entry_only_not_runtime_history",
                    "field_absence": "omitted index fields were absent in raw results",
                    "raw_records": records,
                    "evidence_index": self.evidence_index,
                }, ensure_ascii=False, allow_nan=False),
            })
        body.update(
            max_output_tokens=MAX_OUTPUT_TOKENS,
            reasoning={"effort": "none"},
            tool_choice="none",
        )
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("preflight_input_size_limit")
        write_json(self.root, "request.json", body)
        self.requests += 1
        write_json(self.root, "request-attempt.json", {
            "mode": "paid" if self.live else "offline",
            "sdk_entry_count": self.requests,
            "network_receipt_and_billing": "unknown" if self.live else "not_applicable",
        })
        try:
            if self.live:
                client = self.sdk.with_options(**self.options)
                self.real_requests += 1
                raw = client.responses.with_raw_response.create(**body)
                write_text(self.root, "response.raw.json", raw.text)
                response = raw.parse()
            else:
                from runtime_support import response as offline_response

                response = offline_response(answer=OFFLINE_ANSWER)
            write_json(self.root, "response.sdk.json", response.model_dump(mode="json"))
            if any(item.type not in {"message", "reasoning"} for item in response.output):
                raise ValueError("preflight_unexpected_model_tool")
            # The Runtime also persists the returned answer. Remove credential
            # echoes there without dropping usage or other raw response fields.
            serialized = response.model_dump_json()
            clean = redact_credential(serialized)
            if clean != serialized:
                response = type(response).model_validate_json(clean)
            write_text(self.root, "answer-original.txt", response.output_text)
            if self.evidence_index is not None:
                self.answer_validation = validate_model_answer(
                    response.output_text, self.evidence_index,
                    runtime_input=self.runtime_input,
                    source_snapshot=self.source_snapshot,
                )
                if response.status != "completed":
                    self.answer_validation["output_structure"] = "failed"
                    self.answer_validation["strict_acceptance"] = "failed"
                    self.answer_validation["errors"].append("response_not_completed")
                write_json(self.root, "answer-validation.json", self.answer_validation)
            return response
        except Exception as exc:
            write_json(self.root, "provider-error.json", {
                "category": type(exc).__name__,
                "http_status": getattr(exc, "status_code", None),
                "no_retry": True,
                "billing": "unknown" if self.live else "not_applicable",
            })
            # Worker classifies by status/type, never raw provider error text.
            # RuntimeErrorBase strings originate only from our stable codes.
            from investigation.state import RuntimeErrorBase

            if isinstance(exc, RuntimeErrorBase):
                raise RuntimeError("provider_runtime_error") from None
            raise


def isolated_tool(item, *, session_id=None):
    if item.name != "get_running_version" or json.loads(item.arguments) != {
        "service": "order-service"
    }:
        raise ValueError("preflight_tool_not_allowed")
    # There is no current process identity in a historical fixture test.
    result = {
        "status": "no_data",
        "service": "order-service",
        "reason": "historical_fixture_has_no_current_runtime_identity",
    }
    from tools.tool_manager import ToolManager

    return True, ToolManager.build_output_item(None, item, result), result


def run_case(
    root,
    *,
    live=False,
    sdk=None,
):
    root = Path(root).resolve()
    if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()) or root.is_relative_to(ROOT):
        raise ValueError("preflight_requires_os_temporary_directory")
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("preflight_requires_empty_directory")
    # Setting the environment cannot redirect an already initialized logger.
    # CLI always uses a fresh child; direct embedding must not reuse a real log.
    for handler in logging.getLogger("agent").handlers:
        if not isinstance(handler, logging.FileHandler) or not Path(
            handler.baseFilename
        ).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise ValueError("preflight_requires_isolated_logger_or_fresh_process")
    os.environ["INCIDENT_AGENT_LOG_PATH"] = str(root / "agent.log")
    os.environ["OPENAI_LOG"] = "error"
    for path in (ROOT, ROOT / "tests"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    # All imports that initialize the Agent logger happen after isolation.
    from agent.agent import Agent
    from investigation.models import RuntimeLimits
    from investigation.worker import Worker
    from prompts.agent_prompt import agent_prompt
    from runtime_support import create_runtime, create_session

    key, key_source = credentials()
    if live and not key:
        raise ValueError("deepseek_api_key_missing")
    if live and sdk is None:
        from openai import OpenAI
        import httpx2

        # No proxy inheritance or redirected credential-bearing requests.
        sdk = OpenAI(
            api_key=key,
            base_url=BASE_URL,
            max_retries=0,
            timeout=MODEL_TIMEOUT_SECONDS,
            http_client=httpx2.Client(trust_env=False, follow_redirects=False),
        )
    limits = RuntimeLimits(
        max_attempts=1,
        max_model_requests=MAX_MODEL_REQUESTS,
        max_tool_executions=MAX_TOOL_EXECUTIONS,
        max_resumes=0,
        max_human_requests=0,
        model_timeout_seconds=MODEL_TIMEOUT_SECONDS,
        max_duration_seconds=WALL_TIMEOUT_SECONDS,
    )
    case = build_case()
    write_json(root, "case.json", case)
    provider = BoundedProvider(sdk, root, live)
    manager = create_runtime(root, limits=limits)
    session = create_session(manager)

    def load_request_snapshot():
        # Session identity and records must come from the same read transaction.
        with manager.store.transaction() as db:
            saved = manager.store.get(session.session_id, db)
            checkpoint = manager.store.load_checkpoint(saved, db)
            snapshot = {
                "session_id": saved.session_id,
                "checkpoint_id": saved.checkpoint_id,
                "records": deepcopy(checkpoint["records"]),
            }
            if "identity" in checkpoint:
                snapshot["identity"] = deepcopy(checkpoint["identity"])
            return snapshot

    provider.load_snapshot = load_request_snapshot
    # Import raw historical inputs, not invented model tool calls/outputs or
    # executions. The event and artifact record this provenance explicitly.
    with manager.store.transaction() as db:
        saved = manager.store.get(session.session_id, db)
        checkpoint = manager.store.load_checkpoint(saved, db)
        checkpoint["records"] = deepcopy(case["records"])
        manager.store.checkpoint(saved, checkpoint, db, limits.checkpoints_to_keep)
        manager.store.save(saved, db)
        manager.store.event(db, saved.session_id, "preflight_historical_fixture_imported")
    agent = Agent(client=provider, agent_prompt=agent_prompt + TASK)
    agent.tool_manager.run_tool = isolated_tool
    agent.tool_manager.get_tool_schemas = lambda: []
    worker = Worker(manager, agent, provider_verified=False)
    # Deny all network in default mode, even accidental future tool fallbacks.
    def no_network(*args, **kwargs):
        raise AssertionError("preflight_offline_network_forbidden")

    try:
        with ExitStack() as stack:
            if not live:
                stack.enter_context(patch.object(socket.socket, "connect", no_network))
                stack.enter_context(patch.object(socket, "create_connection", no_network))
            saved = worker.run_once()
    finally:
        if live and sdk is not None:
            sdk.close()
    saved = manager.store.get(session.session_id)
    checkpoint = manager.store.load_checkpoint(saved)
    write_json(root, "checkpoint.json", checkpoint)
    write_json(root, "events.json", manager.store.events(saved.session_id, limit=1000))
    validation = provider.answer_validation or {
        "output_structure": "failed",
        "errors": ["model_response_not_available"],
        "model_claims": None,
        "authoritative_facts_sha256": (
            json_sha256(provider.fact_view) if provider.fact_view is not None else None
        ),
    }
    report = compose_preflight_report(
        provider.fact_view,
        validation["model_claims"],
        validation,
        runtime_completed=saved.status.value == "COMPLETED",
    )
    # Preserve malformed JSON too; parsed claims alone cannot represent it.
    answer_path = root / "answer-original.txt"
    report["model_interpretation"]["raw_answer"] = (
        answer_path.read_text(encoding="utf-8") if answer_path.is_file() else None
    )
    write_json(root, "preflight-report.json", report)
    summary = {
        "mode": "paid" if live else "offline_preflight",
        "model": MODEL,
        "base_url": BASE_URL,
        "key_configured": bool(key),
        "key_source": key_source,
        "real_model_api_requests": provider.real_requests,
        "api_request_counter_semantics": "local SDK create entry, not server receipt or billing",
        "provider_request_entries": provider.requests,
        "session_status": saved.status.value,
        "error_category": saved.last_error_category,
        "runtime_model_reservations": saved.total_model_requests,
        "tool_executions": saved.total_tool_executions,
        "limits": limits.model_dump(mode="json"),
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_request_bytes": MAX_REQUEST_BYTES,
        "prompt_sha256": hashlib.sha256(agent_prompt.encode("utf-8")).hexdigest(),
        "packages": {name: importlib.metadata.version(name) for name in (
            "openai", "httpx2", "pydantic", "python-dotenv", "pytest"
        )},
        "manual_reasoning_review": "required",
        "acceptance": report["acceptance"],
        "strict_acceptance": report["acceptance"]["overall_strict_acceptance"],
        "cli_exit_does_not_certify_reasoning": True,
        "production_data_used": False,
        "prometheus_used": False,
        "remediation_planner": None,
        "artifact_directory": str(root),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(root, "summary.json", summary)
    return summary


def child_main(root, *, live=False):
    """Retain a safe startup failure when no Worker report could be written."""
    try:
        run_case(root, live=live)
    except Exception as exc:
        known_codes = {
            "deepseek_api_key_missing",
            "preflight_requires_empty_directory",
            "preflight_requires_os_temporary_directory",
            "preflight_requires_isolated_logger_or_fresh_process",
        }
        code = str(exc) if isinstance(exc, ValueError) else None
        error = {
            "category": type(exc).__name__,
            "reason": code if code in known_codes else "initialization_or_artifact_failure",
            "no_retry": True,
        }
        # The supervisor supplied a newly minted OS temp directory. Still check
        # containment before writing any diagnostic after a path rejection.
        target = Path(root).resolve()
        if (
            code != "preflight_requires_empty_directory"
            and target.is_dir()
            and target.is_relative_to(Path(tempfile.gettempdir()).resolve())
            and not target.is_relative_to(ROOT)
        ):
            (target / "startup-error.json").write_text(
                json.dumps(error), encoding="utf-8",
            )
        return 2
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--approve-paid-model-run", action="store_true")
    args = parser.parse_args()
    if args.run_live != args.approve_paid_model_run:
        parser.error("paid mode requires both flags AND prior explicit human approval")
    root = Path(tempfile.mkdtemp(prefix="incident-p53c2e-"))
    # Supervise only our own child; terminate it on total deadline. Retain the
    # temp files (including request intent) after failure for manual inspection.
    code = (
        "import sys; from pathlib import Path; "
        "sys.path.insert(0,sys.argv[1]); "
        "from scripts.preflight_real_model import child_main; "
        "sys.exit(child_main(Path(sys.argv[2]),live=sys.argv[3]=='paid'))"
    )
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", code, str(ROOT), str(root),
         "paid" if args.run_live else "offline"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "OPENAI_LOG": "error"},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        child.wait(timeout=WALL_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        child.terminate()
        child.wait(timeout=10)
        write_json(root, "supervisor-stop.json", {
            "reason": "wall_timeout_or_keyboard_interrupt",
            "billing": "unknown" if args.run_live else "not_applicable",
            "no_retry": True,
        })
        print(json.dumps({"stopped": True, "artifact_directory": str(root)}))
        return 2
    path = root / "summary.json"
    if child.returncode or not path.is_file():
        startup = root / "startup-error.json"
        reason = (
            json.loads(startup.read_text(encoding="utf-8"))["reason"]
            if startup.is_file() else "child_failed"
        )
        write_json(root, "supervisor-stop.json", {
            "reason": reason, "no_retry": True,
        })
        print(json.dumps({
            "failed": True, "reason": reason, "artifact_directory": str(root),
        }))
        return 2
    summary = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    complete = summary["session_status"] == "COMPLETED"
    checks = summary["acceptance"]
    mechanical_valid = not args.run_live or (
        checks["model_output_structure"] == "passed"
        and checks["authoritative_fact_integrity"] == "passed"
    )
    # Semantic review is a separate explicit artifact, never implied by exit 0.
    return 0 if complete and mechanical_valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
