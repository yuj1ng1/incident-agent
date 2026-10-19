"""Offline P6.3 frozen-answer acceptance; source captures stay operator-local."""
import hashlib
import json
from pathlib import Path
import shutil
import socket

import pytest
import requests

from scripts import p64_historical_acceptance as acceptance


def _read(path):
    return json.loads(path.read_bytes())


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _snapshot(root):
    return {
        path.relative_to(root).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns,
        ) for path in root.rglob("*") if path.is_file()
    }


def _reseal(root):
    _write(root / "sha256.json", {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.iterdir() if path.is_file() and path.name != "sha256.json"
    })


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("P6.4 historical acceptance cannot send requests or execute tools")

    from tools.tool_manager import ToolManager
    from remediation.executor import ActionExecutor

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(ToolManager, "run_tool", forbidden)
    monkeypatch.setattr(ActionExecutor, "execute", forbidden)


@pytest.fixture
def inputs(tmp_path):
    sources = (
        acceptance.DEFAULT_LIVE,
        acceptance.DEFAULT_REVIEW,
        acceptance.p63_evidence.DEFAULT_CAPTURE,
    )
    if any(not (path / "sha256.json").is_file() for path in sources):
        pytest.skip("operator-local frozen P6.3 answer/review/P6.2b capture absent")
    copies = tuple(tmp_path / name for name in ("live", "review", "capture"))
    for source, target in zip(sources, copies):
        shutil.copytree(source, target)
    return copies


def test_original_failure_has_mechanical_success_and_five_bound_rejections(
    inputs,
    tmp_path,
):
    live, review, capture = inputs
    before = [_snapshot(root) for root in inputs]
    output = tmp_path / "projection"
    report = acceptance.run_acceptance(
        output, live_dir=live, review_dir=review, capture_dir=capture,
    )
    assert report["process_status"] == "completed"
    assert report["mechanical_validation"]["output_structure"] == "passed"
    assert report["mechanical_validation"]["strict_acceptance"] == "pending"
    assert report["trust"]["semantic_status"] == "rejected"
    assert report["trust"]["trusted"] is False
    assert report["trust"]["human_approval"] == "not_recorded"
    assert report["original_answer"].encode("utf-8") == (
        live / "answer-original.txt"
    ).read_bytes()
    assert report["original_bindings"]["original_answer_sha256"] == (
        "f35bf0377b544e254be67bb33e41cf9e1f30d190bd3859c3023ae9d8e3a28581"
    )
    rejected = [
        row["display_number"] for row in report["findings"]
        if row["blocks_acceptance"]
    ]
    assert rejected == [1, 3, 5, 7, 8]
    original_claims = _read(live / "answer-original.txt")["claims"]
    for claim, finding in zip(original_claims, report["findings"]):
        assert finding["original_claim"] == claim
        assert finding["original_references"] == claim["observation_ids"]
        assert finding["review_origin"]["kind"] == "agent"
        assert finding["human_decision"] is None
        assert finding["human_review_status"] == "pending_review"
        for original in finding["review_basis_originals"]:
            encoded = (capture / original["path"]).read_bytes()
            assert hashlib.sha256(encoded).hexdigest() == original["sha256"]
    facts = report["evidence_measurements"]
    assert facts["http_500_response_count"] == 5
    assert len(facts["http_500_references"]) == 5
    assert facts["log_timestamps"][0]["loki_in_fault_window"] is False
    assert facts["log_timestamps"][0]["loki_seconds_after_fault_end"] == "0.102841582"
    assert facts["counter_before"]["value"]["aggregate_value"] == 8.0
    assert facts["counter_after"]["value"]["value"] == 13.0
    assert facts["counter_growth_within_fault_window"] == "not_verified"
    assert all(report[name] == 0 for name in (
        "real_model_sdk_requests", "business_requests", "remediation_executions",
    ))
    assert before == [_snapshot(root) for root in inputs]
    assert _read(output / "acceptance.json") == report
    digest = hashlib.sha256((output / "acceptance.json").read_bytes()).hexdigest()
    assert digest == _read(output / "sha256.json")["acceptance.json"]


@pytest.mark.parametrize("tamper", [
    "answer_bytes", "raw_sdk_disagreement", "review_claim", "fake_human",
    "evidence_bytes",
])
def test_tampering_cannot_rebind_review_or_upgrade_trust(inputs, tmp_path, tamper):
    live, review, capture = inputs
    if tamper == "answer_bytes":
        answer = live / "answer-original.txt"
        answer.write_bytes(answer.read_bytes() + b" ")
        _reseal(live)
    elif tamper == "raw_sdk_disagreement":
        sdk = _read(live / "response.sdk.json")
        sdk["output"][0]["content"][0]["text"] += " "
        _write(live / "response.sdk.json", sdk)
        _reseal(live)
    elif tamper in {"review_claim", "fake_human"}:
        value = _read(review / "semantic-review.json")
        if tamper == "review_claim":
            value["claims"][0]["original_claim"]["observation_ids"].reverse()
        else:
            value["human_review"] = {
                "status": "accepted", "decision": "accepted", "reviewer": "/root",
            }
        _write(review / "semantic-review.json", value)
        _reseal(review)
    else:
        body = capture / "experiment/traffic/http-002.body"
        body.write_bytes(body.read_bytes() + b" ")
    output = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="p63_|p64_"):
        acceptance.run_acceptance(
            output, live_dir=live, review_dir=review, capture_dir=capture,
        )
    assert not output.exists()


def test_removing_agent_failures_never_manufactures_acceptance(inputs):
    live, review, capture = inputs
    value = _read(review / "semantic-review.json")
    for claim in value["claims"]:
        claim["agent_verdict"] = "supported"
    _write(review / "semantic-review.json", value)
    _reseal(review)
    report = acceptance.build_acceptance(live, review, capture)
    assert report["trust"]["semantic_status"] == "pending_review"
    assert report["trust"]["trusted"] is False
    assert report["trust"]["human_approval"] == "not_recorded"


def test_output_cannot_overlap_any_original(inputs):
    live, review, capture = inputs
    before = [_snapshot(root) for root in inputs]
    with pytest.raises(ValueError, match="overlaps_originals"):
        acceptance.run_acceptance(
            capture / "forbidden",
            live_dir=live,
            review_dir=review,
            capture_dir=capture,
        )
    assert before == [_snapshot(root) for root in inputs]
