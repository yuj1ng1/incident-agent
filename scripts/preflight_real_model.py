"""P5.3c.2c isolated preflight; default mode never constructs a network client.

Paid mode requires explicit approval flags. No production entry
point or background service is imported. Artifacts stay in a fresh OS temp dir.
"""
import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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
复核固定历史案例。A/B 中重复数值不是独立佐证。本次以如下两部分 JSON
替代通用报告，只输出一个可解析对象，不用代码围栏，不输出内部推理过程。
第一部分 sources：覆盖 evidence_index 全部来源，每项仅含 observation_id、
collected_at、has_evaluation_time（原始查询字段是否存在）、evaluation_time、
status、samples。samples 每项仅含 metric、status、value、evaluated_at；
无指标的来源用 []。时间逐字保留，原始缺失字段在回答中用 null，null 不是零。
collected_at 只表示采集；evaluation_time 只表示查询目标；evaluated_at
只归属于该具体样本，缺失时不继承查询或邻项时间。
第二部分 conclusion：confirmed、time_comparison、missing_vs_zero、unconfirmed、
next_checks 五个非空数组，每项仅含 text（简短中文）和 observation_ids（实际
来源 ID 数组）。分别说明事实、可比及不可比时间、有效零与缺失、未确认根因、
最多三项进一步取证方向。每项就近引用支持它的来源，不把历史当当前状态，
不把 no_data 当服务不存在或恢复，不添加无基线支持的定性判断。
conclusion 总计约 250～350 汉字；不复述整个索引，不执行工具或修复。
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


def build_evidence_index(records, observations):
    """Extract fields, preserving absence. Never infer times or metric analysis.

    historical_only means Runtime disallows current-state use, including missing
    version evidence; it does not classify that version probe as a metric.
    Unknown tools/shapes remain explicit unsupported entries, not guessed data.
    """
    ids = [item["observation_id"] for item in records]
    observed_ids = [item["observation_id"] for item in observations]
    if (
        any(not isinstance(item, str) or not item for item in ids)
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
    return {
        "schema_version": 1,
        "provenance": "preflight_deterministic_derived",
        "sources": sources,
    }


def validate_model_answer(answer, evidence_index):
    """Check mechanical facts only. Never rewrite answers or grade reasoning."""
    errors = []
    if any(
        item["extraction_status"] != "supported"
        for item in evidence_index["sources"]
    ):
        errors.append("unsupported_evidence_index")

    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate_json_key")
            obj[key] = value
        return obj

    def invalid_constant(value):
        raise ValueError("nonfinite_json_number")

    def same(actual, expected):
        if type(expected) in (int, float):
            return (
                type(actual) in (int, Decimal)
                and (type(actual) is int or actual.is_finite())
                and actual == Decimal(str(expected))
            )
        return type(actual) is type(expected) and actual == expected

    try:
        parsed = json.loads(
            answer, object_pairs_hook=unique_object, parse_constant=invalid_constant,
            parse_float=Decimal,
        )
    except (ValueError, TypeError, RecursionError, InvalidOperation):
        parsed = None
        errors.append("invalid_json")
    if not isinstance(parsed, dict) or set(parsed) != {"sources", "conclusion"}:
        errors.append("invalid_root_structure")
    else:
        expected = {item["observation_id"]: item for item in evidence_index["sources"]}
        rows = parsed["sources"]
        seen = set()
        if not isinstance(rows, list):
            errors.append("sources_not_array")
            rows = []
        fields = {
            "observation_id", "collected_at", "has_evaluation_time",
            "evaluation_time", "status", "samples",
        }
        for row in rows:
            if not isinstance(row, dict) or set(row) != fields:
                errors.append("invalid_source_structure")
                continue
            source_id = row["observation_id"]
            if not isinstance(source_id, str) or source_id not in expected:
                errors.append("unknown_observation_id")
                continue
            if source_id in seen:
                errors.append("duplicate_observation_id")
            seen.add(source_id)
            source = expected[source_id]
            facts = {
                "collected_at": source["collected_at"],
                "has_evaluation_time": "evaluation_time" in source,
                "evaluation_time": source.get("evaluation_time"),
                "status": source["status"],
            }
            for key, value in facts.items():
                if not same(row[key], value):
                    errors.append(f"{source_id}.{key}:mismatch")
            samples = row["samples"]
            wanted = {item["metric"]: item for item in source["samples"]}
            found = set()
            if not isinstance(samples, list):
                errors.append(f"{source_id}.samples:not_array")
                samples = []
            for sample in samples:
                if not isinstance(sample, dict) or set(sample) != {
                    "metric", "status", "value", "evaluated_at",
                }:
                    errors.append(f"{source_id}.sample:invalid_structure")
                    continue
                metric = sample["metric"]
                if (
                    not isinstance(metric, str)
                    or metric not in wanted
                    or metric in found
                ):
                    errors.append(f"{source_id}.sample:unknown_or_duplicate_metric")
                    continue
                found.add(metric)
                for key in ("status", "value", "evaluated_at"):
                    if not same(sample[key], wanted[metric].get(key)):
                        errors.append(f"{source_id}.{metric}.{key}:mismatch")
            if found != set(wanted):
                errors.append(f"{source_id}.samples:missing_metrics")
        if seen != set(expected):
            errors.append("missing_observation_ids")
        conclusion = parsed["conclusion"]
        sections = {
            "confirmed", "time_comparison", "missing_vs_zero", "unconfirmed",
            "next_checks",
        }
        if not isinstance(conclusion, dict) or set(conclusion) != sections:
            errors.append("invalid_conclusion_structure")
        else:
            for section in sections:
                statements = conclusion[section]
                if not isinstance(statements, list) or not statements:
                    errors.append(f"{section}:nonempty_array_required")
                    continue
                if section == "next_checks" and len(statements) > 3:
                    errors.append("too_many_next_checks")
                for statement in statements:
                    if not isinstance(statement, dict) or set(statement) != {
                        "text", "observation_ids",
                    }:
                        errors.append(f"{section}:invalid_statement")
                        continue
                    refs = statement["observation_ids"]
                    if (
                        not isinstance(statement["text"], str)
                        or not statement["text"].strip()
                    ):
                        errors.append(f"{section}:empty_text")
                    if not isinstance(refs, list) or not refs or any(
                        not isinstance(ref, str) or ref not in expected for ref in refs
                    ):
                        errors.append(f"{section}:invalid_references")
    return {
        "fact_check": "failed" if errors else "passed",
        "errors": errors,
        "manual_reasoning_review": "required",
        "strict_acceptance": "failed" if errors else "pending",
        "answer_rewritten": False,
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
        self.load_records = None
        self.evidence_index = None
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
        if self.load_records is not None:
            # The bootstrap version record and model reservation have already
            # committed. Read this request's actual persisted records now.
            records = self.load_records()
            self.evidence_index = build_evidence_index(
                records, runtime_observations(body["input"]),
            )
            write_json(self.root, "evidence-index.json", self.evidence_index)
            write_json(self.root, "request-source-records.json", records)
            write_json(self.root, "runtime-input.json", body["input"])
            if any(
                item["extraction_status"] != "supported"
                for item in self.evidence_index["sources"]
            ):
                raise ValueError("preflight_unsupported_evidence_shape")
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
            if self.evidence_index is not None:
                self.answer_validation = validate_model_answer(
                    response.output_text, self.evidence_index,
                )
                if response.status != "completed":
                    self.answer_validation["fact_check"] = "failed"
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
    provider.load_records = lambda: deepcopy(manager.store.load_checkpoint(
        manager.store.get(session.session_id),
    )["records"])
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
        "manual_reasoning_review": "pending" if live else "not_run",
        "structured_fact_check": (
            provider.answer_validation["fact_check"]
            if provider.answer_validation else "not_available"
        ),
        "strict_acceptance": (
            provider.answer_validation["strict_acceptance"]
            if provider.answer_validation else "failed"
        ),
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
    root = Path(tempfile.mkdtemp(prefix="incident-p53c2c-"))
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
    facts_valid = not args.run_live or summary["structured_fact_check"] == "passed"
    return 0 if complete and facts_valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
