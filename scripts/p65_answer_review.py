"""Advisory offline checks for P6.5 answers; never execute or accept a Session.

Structured declarations are checked literally. Text, topic labels and citation
coverage still require independent semantic review, including on a clean result.
"""
import argparse
from copy import deepcopy
from pathlib import Path
import re
import sqlite3
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from investigation.checkpoint import valid_chain
from investigation.result_trust import project_trust
from scripts.p63_evidence import _verified_files
from scripts.p64_historical_acceptance import _answer_text
from scripts.preflight_evidence_refs import _parse_json, json_sha256
from scripts.p65_model_boundary import encoded, sha, validate_response_schema


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN = ROOT / "data/p65-third-live-tools5-241dca6-20261022-01"
ORIGINAL_MANIFEST_SHA256 = (
    "ce0ce8dfe6af08d23410f392250081d011a45a552a61daa324d7ae6afa368f88"
)
ORIGINAL_ANSWER_SHA256 = (
    "ad84a451320c641e02c083169ac9f54fb8b85cd449bc5c3e914c8eed6f6bd4f8"
)
SCHEMA_VERSION = "p65_s1_answer_v1"
COMPACT_SCHEMA_VERSION = "p65_s1_answer_v2_compact_refs"
SEMANTIC_REVIEW = [
    "需要语义审查：封存材料是否被误解为服务冻结，假设是否替代调查结论。",
    "需要语义审查：文字与声明的 use/kind、字段和引用是否一致、覆盖全部重要事实。",
    "需要语义审查：本轮取得内容与全部历史材料的覆盖范围是否混用。",
    "需要语义审查：采集、回放、事件、评价/回看窗口与累计 counter 是否被混淆。",
    "需要语义审查：根因、故障次数、运行身份、当前健康与恢复是否被无依据地确认。",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Citation(StrictModel):
    id: str = Field(min_length=1)
    type: Literal["tool_call", "input_context"]
    call_id: str | None = None
    tool: str | None = None
    arguments: dict | None = None
    observation_id: str | None = None
    input_path: list[str | int] | None = None
    outer_status: str | None = None
    captured_status: str | None = None
    historical_evidence_id: str | None = None
    source: str | None = None
    originals: list[str] | None = None
    captured_at: str | None = None
    evaluated_at: str | None = None
    event_window: dict | None = None


class Claim(StrictModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: Literal["fact", "hypothesis", "limitation"]
    refs: list[str]
    use: Literal[
        "observation", "field_value", "log_content", "valid_zero", "fault_count",
        "root_cause", "repair", "current_health", "current_recovery", "interpretation",
    ]
    field_path: list[str | int] | None = None
    value: object = None


class Answer(StrictModel):
    schema_version: Literal["p65_s1_answer_v1"]
    citations: list[Citation]
    claims: list[Claim] = Field(min_length=1)


class CompactToolCitation(StrictModel):
    id: str = Field(min_length=1)
    type: Literal["tool_call"]
    call_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: dict


class InputCitation(StrictModel):
    id: str = Field(min_length=1)
    type: Literal["input_context"]
    input_path: list[str | int] = Field(min_length=1)


class CompactAnswer(StrictModel):
    schema_version: Literal["p65_s1_answer_v2_compact_refs"]
    citations: list[Annotated[
        CompactToolCitation | InputCitation, Field(discriminator="type"),
    ]]
    claims: list[Claim] = Field(min_length=1)


def parse_answer(answer):
    """Select an exact contract; never fill v1 fields from the v2 contract."""
    parsed = _parse_json(answer)
    contract = (
        CompactAnswer
        if isinstance(parsed, dict)
        and parsed.get("schema_version") == COMPACT_SCHEMA_VERSION
        else Answer
    )
    return contract.model_validate(parsed)


def select(value, path):
    """Resolve exact JSON keys/indices; a present null differs from absence."""
    if not path:
        raise ValueError("field_path_missing")
    for part in path:
        if isinstance(value, dict) and type(part) is str and part in value:
            value = value[part]
        elif isinstance(value, list) and type(part) is int and 0 <= part < len(value):
            value = value[part]
        else:
            raise ValueError("source_field_missing")
    return value


def provenance(record):
    """Project only persisted fields; absent metadata stays null/empty.

    A scope-denied call has no captured_result or historical source. Absence
    never implies success and must not be filled from a filename, prose or HEAD.
    This projection does not authenticate the supplied record.
    """
    result = record["result"]
    captured = result.get("captured_result", {})
    args = record["arguments"]
    return {
        "outer_status": result.get("status"),
        "captured_status": captured.get("status"),
        "historical_evidence_id": result.get("sanitized_projection", {}).get(
            "observation_id",
        ),
        "source": result.get("source"),
        "originals": [item["path"] for item in result.get("originals", [])],
        "captured_at": result.get("captured_at"),
        "evaluated_at": captured.get("evaluated_at"),
        "event_window": (
            {key: args.get(key) for key in ("start", "end")}
            if record["name"] == "query_logs" else None
        ),
    }


def resolve_tool_record(record):
    """Validate the local shape and copy record data, without granting trust."""
    if (
        not isinstance(record.get("name"), str) or not record["name"].strip()
        or not isinstance(record.get("arguments"), dict)
        or not isinstance(record.get("result"), dict)
        or not isinstance(record.get("collected_at"), str)
        or not record["collected_at"].strip()
    ):
        raise ValueError("invalid_runtime_record")
    try:
        expected = provenance(record)
        # Reuse strict field types, not the model's claims about provenance.
        Citation.model_validate({"id": "record", "type": "tool_call", **expected})
        captured = record["result"].get("captured_result", {})
        resolved = {
            "type": "tool_call",
            "value": record["result"],
            "call_id": record["call_id"],
            "tool": record["name"],
            "arguments": record["arguments"],
            "observation_id": record["observation_id"],
            "replay_collected_at": record["collected_at"],
            "historical_query_started_at": captured.get("query_started_at"),
            "historical_query_finished_at": captured.get("query_finished_at"),
            **expected,
        }
        json_sha256(resolved)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError("invalid_runtime_record") from exc
    return deepcopy(resolved)


def review_answer(
    answer,
    records,
    input_context,
):
    """Return advice only. Supplied records must be independently authenticated.

    The sealed-case loader below does that binding for the fixed real failure.
    This pure function does not authenticate arbitrary caller-supplied records.
    A repeated claim may reuse a citation; duplicate citation entries/call IDs
    are ambiguous and rejected. A p63 ID remains valid historical provenance.
    """
    issues = []
    resolved = {}

    def issue(code, location, detail=""):
        issues.append({"code": code, "location": location, "detail": detail})

    by_call = {}
    seen_obs = set()
    ambiguous_calls = set()
    for index, record in enumerate(records):
        call = record.get("call_id")
        obs = record.get("observation_id")
        if (
            not isinstance(call, str) or not call.strip()
            or not isinstance(obs, str) or not obs.strip()
        ):
            issue("ambiguous_runtime_records", f"records/{index}")
            continue
        if call in by_call or obs in seen_obs:
            issue("ambiguous_runtime_records", f"records/{index}")
            ambiguous_calls.add(call)
            ambiguous_calls.update(
                key for key, row in by_call.items()
                if row["observation_id"] == obs
            )
        if call in by_call:
            continue
        by_call[call] = record
        seen_obs.add(obs)

    try:
        document = parse_answer(answer)
    except (ValueError, TypeError, ValidationError):
        issue("answer_contract_missing_or_invalid", "answer")
        # Only recognize explicit identifier labels, never infer prose truth.
        for match in re.finditer(
            r"\bcall(?:_id)?\s*[:=]?\s*`([A-Za-z0-9_:.-]+)`", answer,
        ):
            identifier = match[1]
            if identifier.startswith("p63:"):
                issue(
                    "historical_id_used_as_call_id", f"answer@{match.start()}",
                    identifier,
                )
            elif identifier not in by_call:
                issue("unknown_call_id", f"answer@{match.start()}", identifier)
        document = None

    if document is not None:
        cited_calls = set()
        citation_ids = set()
        for citation in document.citations:
            loc = f"citations/{citation.id}"
            if citation.id in citation_ids:
                issue("duplicate_citation_id", loc)
                continue
            citation_ids.add(citation.id)
            if citation.type == "input_context":
                if citation.model_fields_set - {"id", "type", "input_path"}:
                    issue("input_context_has_tool_identity", loc)
                try:
                    value = select(input_context, citation.input_path)
                except ValueError as exc:
                    issue(str(exc), loc)
                    continue
                resolved[citation.id] = {
                    "type": citation.type,
                    "input_path": deepcopy(citation.input_path),
                    "value": deepcopy(value),
                }
                continue
            if isinstance(citation, Citation) and citation.input_path is not None:
                issue("tool_has_input_path", loc)
            if not citation.call_id:
                issue("missing_call_id", loc)
                continue
            if citation.call_id.startswith("p63:"):
                issue("historical_id_used_as_call_id", loc, citation.call_id)
                continue
            record = by_call.get(citation.call_id)
            if record is None:
                issue("unknown_call_id", loc, citation.call_id)
                continue
            if citation.call_id in cited_calls:
                issue("duplicate_call_id", loc, citation.call_id)
            cited_calls.add(citation.call_id)
            if citation.call_id in ambiguous_calls:
                issue("ambiguous_runtime_records", loc, citation.call_id)
                continue
            try:
                source = resolve_tool_record(record)
            except ValueError as exc:
                issue(str(exc), loc)
                continue
            if citation.tool != record["name"]:
                issue("tool_name_mismatch", loc)
            if json_sha256(citation.arguments) != json_sha256(record["arguments"]):
                issue("tool_arguments_mismatch", loc)
            if isinstance(citation, Citation):
                if citation.observation_id not in (None, record["observation_id"]):
                    issue("observation_id_mismatch", loc)
                for key, value in provenance(record).items():
                    if key not in citation.model_fields_set:
                        issue("missing_provenance_field", loc, key)
                    elif json_sha256(getattr(citation, key)) != json_sha256(value):
                        issue("provenance_mismatch", loc, key)
            resolved[citation.id] = source

        claim_ids = set()
        used = set()
        for claim in document.claims:
            loc = f"claims/{claim.id}"
            if claim.id in claim_ids:
                issue("duplicate_claim_id", loc)
            claim_ids.add(claim.id)
            if len(set(claim.refs)) != len(claim.refs):
                issue("duplicate_claim_reference", loc)
            if claim.kind == "fact" and not claim.refs:
                issue("fact_without_reference", loc)
            sources = []
            for ref in claim.refs:
                used.add(ref)
                if ref not in resolved:
                    issue("unknown_citation_id", loc, ref)
                else:
                    sources.append(resolved[ref])
            if claim.use == "field_value":
                try:
                    if len(sources) != 1 or "value" not in claim.model_fields_set:
                        raise ValueError(
                            "field_declaration_requires_one_source_and_value",
                        )
                    value = select(sources[0]["value"], claim.field_path)
                    if json_sha256(value) != json_sha256(claim.value):
                        raise ValueError("source_field_value_mismatch")
                except ValueError as exc:
                    issue(str(exc), loc)
            if claim.kind != "fact":
                continue
            if claim.use in {
                "root_cause", "repair", "current_health", "current_recovery",
            }:
                issue("unverified_fact_in_historical_scope", loc, claim.use)
            if claim.use == "fault_count":
                issue("independent_fault_count_not_established", loc)
            if claim.use in {"log_content", "valid_zero"}:
                # Input declarations cannot establish successful tool execution.
                supported = False
                for source in sources:
                    if source["type"] != "tool_call":
                        continue
                    captured = source["value"].get("captured_result", {})
                    if claim.use == "log_content":
                        supported |= (
                            source["tool"] == "query_logs"
                            and captured.get("status") == "ok"
                            and isinstance(captured.get("logs"), list)
                            and bool(captured["logs"])
                        )
                    else:
                        supported |= (
                            source["tool"] == "query_metrics"
                            and captured.get("status") == "ok"
                            and type(captured.get("value")) in (int, float)
                            and captured["value"] == 0
                        )
                if not supported:
                    issue("logs_not_read" if claim.use == "log_content"
                          else "no_valid_zero", loc)
        for ref in resolved.keys() - used:
            issue("unused_citation", f"citations/{ref}")

    codes = sorted({item["code"] for item in issues})
    return {
        "mode": "offline_advisory_only",
        "answer_sha256": sha(answer.encode("utf-8")),
        "declaration_check": "failed" if issues else "passed",
        "issues": issues,
        "resolved_citations": resolved,
        "semantic_review": list(SEMANTIC_REVIEW),
        "semantic_status": "pending_review",
        "result_trust": project_trust(
            answer, rejection_reasons=codes,
            review_origin="caller_supplied_agent_review",
        ).model_dump(mode="json"),
        "runtime_state_written": False,
    }


def load_real_case(run_dir=DEFAULT_RUN):
    """Verify fixed original bytes and read SQLite using immutable read-only mode.

    The fixed manifest digest anchors this failure independently of a local
    reseal. No RuntimeStore/Worker/SDK, resume, authorization or write is used.
    """
    root, files = _verified_files(run_dir)
    if sha(files["sha256.json"]) != ORIGINAL_MANIFEST_SHA256:
        raise ValueError("fixed_real_case_manifest_changed")
    def read(name):
        return _parse_json(files[name].decode("utf-8"))

    summary = read("summary.json")
    requests = [read(f"request-{n:02}.json") for n in (1, 2)]
    responses = [read(f"response-{n:02}.raw.txt") for n in (1, 2)]
    for number, (request, raw) in enumerate(zip(requests, responses, strict=True), 1):
        integrity = read(f"response-{number:02}.integrity.json")
        sdk = read(f"response-{number:02}.sdk.json")
        completion = read(f"completion-{number:02}.json")
        if (
            request["sha256"] != sha(encoded(request["body"]))
            or integrity["wire_text_sha256"]
            != sha(files[f"response-{number:02}.raw.txt"])
            or integrity["credential_redacted"] is not False
            or validate_response_schema(raw)[0].model_dump(mode="json") != sdk
            or completion["response_origin"] != "live_provider"
            or completion["response_id"] != raw["id"]
        ):
            raise ValueError("real_request_response_binding_mismatch")
    answer = files["answer-original.txt"].decode("utf-8")
    if (
        sha(files["answer-original.txt"]) != ORIGINAL_ANSWER_SHA256
        or answer != _answer_text(responses[1])
        or answer != summary["checkpoint"]["result"]["answer"]
    ):
        raise ValueError("real_original_answer_changed")
    before = next(
        row["checkpoint"] for row in read("worker-boundaries.json")
        if row["phase"] == "model_before_io"
        and row["session"]["total_model_requests"] == 2
    )
    chain = requests[1]["body"]["input"]
    calls = [item for item in responses[0]["output"] if item["type"] == "function_call"]
    outputs = [item for item in chain if item.get("type") == "function_call_output"]
    if (
        not valid_chain(chain) or before["pending"]
        or len(calls) != len(outputs) or len(calls) != 4
        or chain != requests[0]["body"]["input"] + responses[0]["output"] + outputs
        or sha(encoded(before)) != requests[1]["checkpoint_sha256"]
        or before["records"] != summary["checkpoint"]["records"]
    ):
        raise ValueError("real_committed_chain_mismatch")
    records = before["records"]
    if len({r["call_id"] for r in records}) != len(records):
        raise ValueError("real_duplicate_record_call_id")
    for call, output in zip(calls, outputs, strict=True):
        record = next(r for r in records if r["call_id"] == call["call_id"])
        if (
            output["call_id"] != call["call_id"] or output not in before["history"]
            or record["name"] != call["name"]
            or record["arguments"] != _parse_json(call["arguments"])
            or record["result"] != _parse_json(output["output"])
        ):
            raise ValueError("real_tool_output_binding_mismatch")
    context = _parse_json(chain[0]["content"])
    if context["automatic_version_probe"] != records[0]["result"]:
        raise ValueError("real_probe_context_mismatch")
    connection = sqlite3.connect(
        (root / "runtime.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True,
    )
    try:
        session_id = summary["session"]["session_id"]
        session = connection.execute(
            "SELECT data FROM investigation_sessions WHERE session_id=?", (session_id,),
        ).fetchone()
        checkpoints = [
            _parse_json(row[0]) for row in connection.execute(
                "SELECT data FROM investigation_checkpoints WHERE session_id=?",
                (session_id,),
            )
        ]
        if (
            connection.execute("PRAGMA integrity_check").fetchone() != ("ok",)
            or session is None or _parse_json(session[0]) != summary["session"]
            or before not in checkpoints or summary["checkpoint"] not in checkpoints
        ):
            raise ValueError("real_sqlite_checkpoint_mismatch")
    finally:
        connection.close()
    return answer, records, context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--answer", type=Path)
    args = parser.parse_args()
    answer, records, context = load_real_case(args.run_dir)
    if args.answer:
        answer = args.answer.read_bytes().decode("utf-8")
    print(encoded(review_answer(answer, records, context)).decode("utf-8"), end="")


if __name__ == "__main__":
    main()
