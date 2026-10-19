"""Completion and even mechanically valid references never grant semantic trust."""
from copy import deepcopy
import hashlib
import json

from fastapi.testclient import TestClient
import pytest

from investigation.result_trust import project_result_trust, project_trust
from investigation.worker import Worker
from runtime_support import FixtureAgent, create_runtime, create_session, response
from web_api.app import create_app
from web_api.queries import answer_view


def test_schema_pass_does_not_claim_reference_or_semantic_support():
    result = {
        "success": True,
        "status": "completed",
        "answer": "没有证据的结论：已恢复且确认代码根因。",
        "result_trust": {"semantic_status": "accepted", "trusted": True},
    }
    original = deepcopy(result)
    trust = project_result_trust(result)
    assert result == original
    assert trust.mechanical_status == "passed"
    assert trust.mechanical_scope == "result_schema_only"
    assert trust.semantic_status == "pending_review"
    assert trust.trusted is False
    assert trust.human_approval == "not_recorded"


def test_agent_rejection_blocks_trust_even_when_all_references_exist():
    trust = project_trust(
        "原答",
        mechanical_status="passed",
        mechanical_scope="frozen_references_and_hashes",
        rejection_reasons=["claim_support_failed"],
        review_origin="caller_supplied_agent_review",
    )
    assert trust.semantic_status == "rejected"
    assert trust.human_approval == "not_recorded"
    assert trust.trusted is False
    assert project_trust(
        "原答",
        mechanical_status="passed",
        mechanical_scope="frozen_references_and_hashes",
    ).semantic_status == "pending_review"
    with pytest.raises(TypeError):
        project_trust("原答", semantic_status="accepted")


def test_missing_and_invalid_result_fail_closed():
    assert project_result_trust(None).mechanical_status == "not_checked"
    invalid = project_result_trust({"answer": "保留错误原答"})
    assert invalid.mechanical_status == "failed"
    assert invalid.semantic_status == "rejected"
    assert invalid.rejection_reasons == ["invalid_result_schema"]


def test_public_hash_binds_full_answer_before_redaction_and_truncation():
    answer = "未审查。" * 3000
    view = answer_view({"success": True, "status": "completed", "answer": answer})
    assert view.answer_truncated
    assert view.result_trust.answer_sha256 == hashlib.sha256(
        answer.encode("utf-8"),
    ).hexdigest()
    assert view.result_trust.semantic_status == "pending_review"


def test_worker_completion_projects_pending_trust_on_all_public_result_paths(tmp_path):
    manager = create_runtime(tmp_path)
    session = create_session(manager)
    Worker(manager, FixtureAgent([response(answer="无证据仍宣称业务已恢复")])).run_once()
    with manager.store.read() as db:
        persisted = manager.store.get(session.session_id, db)
        checkpoint = manager.store.load_checkpoint(persisted, db)
    before = json.dumps(checkpoint, sort_keys=True, ensure_ascii=False)
    workflow = manager.workflow(session.session_id)
    assert workflow["session"]["status"] == "COMPLETED"
    assert workflow["investigation_result"]["success"] is True
    assert workflow["result_trust"]["semantic_status"] == "pending_review"
    assert workflow["result_trust"]["trusted"] is False
    with TestClient(create_app(
        incident_path=manager.incident_store.file_path,
        runtime_path=manager.store.path,
    ), base_url="http://127.0.0.1") as client:
        detail = client.get(f"/api/sessions/{session.session_id}").json()
        history = client.get(f"/api/incidents/{session.incident_id}").json()
    for answer in (
        detail["investigation_result"], history["recent_investigations"][0],
    ):
        assert answer["answer"] == "无证据仍宣称业务已恢复"
        assert answer["result_trust"] == workflow["result_trust"]
    with manager.store.read() as db:
        after = manager.store.load_checkpoint(manager.store.get(session.session_id, db), db)
    assert json.dumps(after, sort_keys=True, ensure_ascii=False) == before
