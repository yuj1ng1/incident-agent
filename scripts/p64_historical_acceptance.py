"""Read-only P6.3 failure acceptance; no model, collection or human approval.

Run this entry in a dedicated process. The existing P6.2b guards temporarily
replace process-wide transports. Imported Agent review is an input to rejection,
never a signature or a general natural-language semantic grading algorithm.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from investigation.result_trust import project_trust
from scripts import p63_evidence
from scripts import p62b_evidence
from scripts.preflight_real_model import json_sha256, validate_model_answer


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIVE = ROOT / "data/p63-live-20261018-01"
DEFAULT_REVIEW = ROOT / "data/p63-live-review-20261018-01"
REJECTED_VERDICTS = {"factual_error", "insufficient_local_citation"}


def _sha(encoded):
    return hashlib.sha256(encoded).hexdigest()


def _epoch_seconds(value):
    """Compare Loki nanoseconds without datetime's microsecond truncation."""
    match = re.fullmatch(r"(.+T\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|\+00:00)", value)
    if match is None:
        raise ValueError("p64_unsupported_utc_timestamp")
    base = datetime.fromisoformat(match[1] + "+00:00")
    seconds = int((base - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds())
    return Decimal(seconds) + Decimal("0." + (match[2] or "0"))


def _answer_text(response):
    output = response["output"]
    if (
        response.get("status") != "completed"
        or response.get("error") is not None
        or response.get("incomplete_details") is not None
        or not output
        or any(item["type"] != "message" for item in output)
    ):
        raise ValueError("p64_original_response_not_completed_answer")
    return "".join(
        item["text"]
        for message in output for item in message["content"]
        if item["type"] == "output_text"
    )


def _measurements(context):
    """Project original observations, independent of claim text and opinions."""
    sources = context["evidence_index"]["sources"]
    http = [item for item in sources if item["name"] == "captured_http_response"]
    logs = next(item for item in sources if item["name"] == "query_logs")
    before = next(
        item for item in sources if item["name"] == "captured_metrics_exposition"
    )
    after = next(
        item for item in sources
        if item["name"] == "query_metrics"
        and item["value"]["metric"] == "http_requests_total"
    )
    start = _epoch_seconds(context["fault_window"]["start"])
    end = _epoch_seconds(context["fault_window"]["end"])
    return {
        "http_500_response_count": len(http),
        "http_500_references": [item["observation_id"] for item in http],
        "fault_window": context["fault_window"],
        "loki_query_window": logs["value"]["query_range"],
        "log_timestamps": [{
            "loki_timestamp_utc": entry["loki_timestamp_utc"],
            "body_event_time_utc": entry["body_event_time_utc"],
            "body_event_time_precision": entry["body_event_time_precision"],
            "loki_in_fault_window": (
                start <= _epoch_seconds(entry["loki_timestamp_utc"]) < end
            ),
            "loki_seconds_after_fault_end": str(
                _epoch_seconds(entry["loki_timestamp_utc"]) - end
            ),
        } for entry in logs["value"]["entries"]],
        "counter_before": before,
        "counter_after": after,
        "counter_scope": "service_total_not_independent_500_counter",
        "counter_growth_within_fault_window": "not_verified",
        "use_for_current_state": False,
    }


def _project(live_dir, review_dir, capture_dir):
    live_root, live = p63_evidence._verified_files(live_dir)
    review_root, reviews = p63_evidence._verified_files(review_dir)
    capture_root, originals = p63_evidence._verified_files(capture_dir)
    context = p63_evidence.build_context(capture_root)
    read = p63_evidence._json
    answer_bytes = live["answer-original.txt"]
    answer = answer_bytes.decode("utf-8")
    request = read(live["request.json"])
    raw, sdk = read(live["response.raw.json"]), read(live["response.sdk.json"])
    review = read(reviews["semantic-review.json"])
    original_contract = read(live["claim-review.json"])["contract_check"]
    if (
        json.loads(request["input"][0]["content"]) != context
        or read(live["summary.json"])["request_sha256"] != _sha(live["request.json"])
        or read(live["response-integrity.json"])["wire_text_sha256"]
        != _sha(live["response.raw.json"])
        or read(live["response-integrity.json"])["credential_redacted"] is not False
        or any(raw[key] != sdk[key] for key in ("id", "model", "status", "output"))
        or _answer_text(raw).encode("utf-8") != answer_bytes
        or _answer_text(sdk).encode("utf-8") != answer_bytes
    ):
        raise ValueError("p64_original_answer_or_source_binding_mismatch")
    bindings = {
        "original_answer_sha256": _sha(answer_bytes),
        "raw_response_sha256": _sha(live["response.raw.json"]),
        "request_sha256": _sha(live["request.json"]),
        "live_manifest_sha256": _sha(live["sha256.json"]),
    }
    if any(review.get(key) != digest for key, digest in bindings.items()):
        raise ValueError("p64_review_original_binding_mismatch")
    if (
        review.get("human_review") != {
            "status": "required", "decision": None, "reviewer": None,
        }
        or not isinstance(review.get("reviewers"), list)
        or not review["reviewers"]
        or any(not isinstance(name, str) or not name for name in review["reviewers"])
    ):
        raise ValueError("p64_agent_review_cannot_record_human_approval")
    contract = validate_model_answer(answer, context["evidence_index"])
    if contract != original_contract:
        raise ValueError("p64_original_contract_rebuild_mismatch")
    claims = contract["model_claims"]
    if review.get("claim_count") != len(claims) or len(review["claims"]) != len(claims):
        raise ValueError("p64_review_claim_count_mismatch")
    findings = []
    for index, (claim, row) in enumerate(zip(claims, review["claims"])):
        if (
            row.get("claim_index") != index
            or row.get("display_number") != index + 1
            or row.get("original_claim") != claim
            or row.get("answer_rewritten") is not False
            or row.get("human_review_status") != "required"
            or not isinstance(row.get("review_opinion"), str)
            or not row["review_opinion"].strip()
        ):
            raise ValueError("p64_review_claim_binding_mismatch")
        paths = row["supporting_or_counterexample_originals"]
        if not paths or any(path not in originals for path in paths):
            raise ValueError("p64_review_original_missing")
        findings.append({
            "claim_index": index,
            "display_number": index + 1,
            "original_claim": claim,
            "claim_sha256": json_sha256(claim),
            "original_references": list(claim["observation_ids"]),
            "agent_verdict": row["agent_verdict"],
            "review_opinion": row["review_opinion"],
            "blocks_acceptance": row["agent_verdict"] in REJECTED_VERDICTS,
            "review_origin": {
                "kind": "agent",
                "reviewers": review["reviewers"],
                "path": "semantic-review.json",
                "json_pointer": f"/claims/{index}",
                "sha256": _sha(reviews["semantic-review.json"]),
            },
            "review_basis_originals": [
                {"path": path, "sha256": _sha(originals[path])} for path in paths
            ],
            "human_review_status": "pending_review",
            "human_decision": None,
            "answer_rewritten": False,
        })
    reasons = [
        f"claim_{row['display_number']}:{row['agent_verdict']}"
        for row in findings if row["blocks_acceptance"]
    ]
    trust = project_trust(
        answer,
        mechanical_status=contract["output_structure"],
        mechanical_scope="frozen_references_and_hashes",
        rejection_reasons=reasons,
        review_origin="caller_supplied_agent_review",
    )
    return {
        "schema_version": 1,
        "case": "P6.3_original_failure",
        "scope": "isolated_historical_acceptance_not_runtime_mutation",
        "process_status": sdk["status"],
        "process_status_origin": "historical_sdk_response_not_session_transition",
        "trust": trust.model_dump(mode="json"),
        "original_bindings": bindings,
        "input_inventories": [{
            "role": role,
            "root": str(root),
            "manifest_sha256": _sha(files["sha256.json"]),
            "files": [{"path": path, "sha256": _sha(encoded), "bytes": len(encoded)}
                      for path, encoded in sorted(files.items())],
        } for role, root, files in (
            ("original_model_run", live_root, live),
            ("agent_review_input", review_root, reviews),
            ("historical_evidence", capture_root, originals),
        )],
        "original_answer": answer,
        "mechanical_validation": contract,
        "findings": findings,
        "evidence_measurements": _measurements(context),
        "evidence_index": context["evidence_index"],
        "real_model_sdk_requests": 0,
        "business_requests": 0,
        "remediation_executions": 0,
        "use_for_current_state": False,
        "limits": [
            "Agent review input is not a human signature or automatic semantic proof.",
            "Local hashes verify consistency, not independent source authenticity.",
            "Mechanical contract/reference success does not establish claim support.",
            "Original answers and citations are preserved; "
            "no answer repair is performed.",
        ],
    }


def build_acceptance(
    live_dir=DEFAULT_LIVE,
    review_dir=DEFAULT_REVIEW,
    capture_dir=p63_evidence.DEFAULT_CAPTURE,
):
    """Verify sealed inputs and return a conservative, independent projection."""
    with ExitStack() as stack:
        p62b_evidence._guards(stack, offline=True)
        return _project(live_dir, review_dir, capture_dir)


def run_acceptance(
    output_dir,
    *,
    live_dir=DEFAULT_LIVE,
    review_dir=DEFAULT_REVIEW,
    capture_dir=p63_evidence.DEFAULT_CAPTURE,
):
    output = Path(output_dir).resolve()
    for source in (live_dir, review_dir, capture_dir):
        source = Path(source).resolve()
        if output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError("p64_acceptance_output_overlaps_originals")
    report = build_acceptance(live_dir, review_dir, capture_dir)
    output.mkdir(parents=True, exist_ok=False)
    encoded = (
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    (output / "acceptance.json").write_bytes(encoded)
    (output / "sha256.json").write_text(
        json.dumps({"acceptance.json": _sha(encoded)}, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-dir", type=Path, default=DEFAULT_LIVE)
    parser.add_argument("--review-dir", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument(
        "--capture-dir", type=Path, default=p63_evidence.DEFAULT_CAPTURE,
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_acceptance(
        args.output_dir,
        live_dir=args.live_dir,
        review_dir=args.review_dir,
        capture_dir=args.capture_dir,
    )
    print(json.dumps({"trust": report["trust"], "output": str(args.output_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
