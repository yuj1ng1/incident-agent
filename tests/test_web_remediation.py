"""In-process ASGI writes with backend identities and isolated persisted records."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from compensation_support import FixtureVerifier
from incident_store import IncidentStore
from models.incident import Incident
from remediation.approval import approval_digest
from remediation.executor import ActionExecutor
from remediation.store import RemediationStore
from test_compensation import comp, child, run_original
from web_api.app import create_app
from web_api.auth import WebAuthorization

TOKENS = {"requester": "r" * 40, "approver": "a" * 40, "executor": "e" * 40}


@pytest.fixture
def web(comp):
    incident_path = comp.root / "incidents.json"
    incidents = IncidentStore(incident_path)
    incidents.save_incident("fixture-fingerprint", Incident(
        incident_id=comp.original.incident_id, service=comp.original.service,
        severity="critical", status="investigating", alerts=[],
    ))
    auth = WebAuthorization(json.dumps([
        {"actor": actor, "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
         "permissions": [{"requester": "request", "approver": "approve", "executor": "execute"}[actor]]}
        for actor, token in TOKENS.items()
    ]))
    app = create_app(
        incident_path=incident_path, runtime_path=comp.root / "absent.sqlite3",
        remediation_path=comp.store.file_path, authorization=auth,
    )
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 32100)) as client:
        yield comp, client, incidents


def headers(actor):
    return {"Authorization": "Bearer " + TOKENS[actor]}


def command(comp, identifier, cmd="command", **extra):
    return {"command_id": cmd, "proposal_digest": approval_digest(comp.store.get(identifier)), **extra}


def test_reads_are_allowlisted_linked_and_do_not_probe_or_change_records(web):
    comp, client, _ = web
    run_original(comp)
    pending = child(comp)
    before = comp.store.file_path.read_bytes()
    comp.monkeypatch.setattr("remediation.executor.ActionExecutor.execute", Mock(side_effect=AssertionError("read executed")))
    original = client.get(f"/api/remediations/{comp.original.proposal_id}").json()
    detail = client.get(f"/api/remediations/{pending.proposal_id}").json()
    assert original["compensation"]["compensation_proposal_id"] == pending.proposal_id
    assert detail["original_proposal_id"] == comp.original.proposal_id
    assert original["verifications"][0]["outcome"] == "failed"
    assert len(client.get("/api/remediations?limit=1").json()["items"]) == 1
    assert client.get("/api/remediations?status=invalid").status_code == 422
    assert comp.store.file_path.read_bytes() == before


@pytest.mark.parametrize("operation", ["compensation-requests", "approve", "reject", "execute", "inspect-execution", "close-execution"])
def test_unauthenticated_writes_never_modify_store(web, operation):
    comp, client, _ = web
    before = comp.store.file_path.read_bytes()
    result = client.post(f"/api/remediations/{comp.original.proposal_id}/{operation}", json={})
    assert result.status_code == 401
    assert comp.store.file_path.read_bytes() == before


@pytest.mark.parametrize("operation", ["approve", "execute", "reject", "close-execution"])
def test_requester_cannot_approve_or_execute_even_with_body_role(web, operation):
    comp, client, _ = web
    result = client.post(f"/api/remediations/{comp.original.proposal_id}/{operation}", headers=headers("requester"),
                         json=command(comp, comp.original.proposal_id, role="administrator", approved_by="trusted", reason="test"))
    assert result.status_code == 403


def test_default_and_invalid_auth_configuration_disable_writes_and_create_no_store(tmp_path):
    for configuration in ("", "[]", "broken", '{"token":"do-not-expose"}'):
        app = create_app(remediation_path=tmp_path / "absent.json", authorization=WebAuthorization(configuration))
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 32100)) as client:
            assert client.get("/healthz").json()["read_only"]
            assert client.post("/api/remediations/missing/approve", json={}, headers=headers("approver")).status_code == 503
            assert client.get("/api/remediations").status_code == 503
    assert list(tmp_path.iterdir()) == []


def test_authenticated_identity_permissions_and_origin_are_backend_owned(web):
    _, client, _ = web
    result = client.get("/api/operator", headers=headers("requester"))
    assert result.json() == {"actor": "requester", "permissions": ["request"]}
    assert client.get("/api/operator", headers={**headers("approver"), "Origin": "https://hostile.example"}).status_code == 403
    assert client.get("/api/operator", headers={"Authorization": "Bearer " + "x" * 40}).status_code == 401
    assert client.get("/healthz").json()["read_only"] is False
    preflight = client.options("/api/remediations/missing/approve", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    })
    assert preflight.status_code == 200
    assert "access-control-allow-credentials" not in preflight.headers


def test_manual_application_approval_execution_and_independent_verification(web):
    comp, client, _ = web
    comp.executor.verifier = FixtureVerifier(comp.source)
    run_original(comp)
    original = comp.original.proposal_id
    body = command(comp, original, "apply", reason="undo a fixture", target_id=comp.contract.target.target_id)
    response = client.post(f"/api/remediations/{original}/compensation-requests", headers=headers("requester"), json=body)
    assert response.status_code == 200, response.text
    identifier = response.json()["compensation"]["compensation_proposal_id"]
    assert comp.store.get(identifier).status.value == "pending"
    assert client.post(f"/api/remediations/{original}/compensation-requests", headers=headers("requester"), json=body).status_code == 200
    approved = client.post(f"/api/remediations/{identifier}/approve", headers=headers("approver"), json=command(comp, identifier, "approve"))
    assert approved.status_code == 200, approved.text
    assert approved.json()["approved_by"] == "approver"
    assert client.post(f"/api/remediations/{identifier}/approve", headers=headers("approver"), json=command(comp, identifier, "approve")).status_code == 200
    executed = client.post(f"/api/remediations/{identifier}/execute", headers=headers("executor"), json=command(comp, identifier, "execute"))
    assert executed.status_code == 200, executed.text
    assert executed.json()["status"] == "succeeded" and executed.json()["verifications"][0]["outcome"] == "passed"
    duplicate = client.post(f"/api/remediations/{identifier}/execute", headers=headers("executor"), json=command(comp, identifier, "execute"))
    assert duplicate.status_code == 200
    assert (comp.root / "effects").read_text().splitlines() == ["effect"]


def test_missing_safe_target_returns_persisted_block_and_no_executable_child(web):
    comp, client, _ = web
    comp.monkeypatch.setattr("remediation.compensation.COMPENSATION_REGISTRY", {})
    run_original(comp)
    response = client.post(f"/api/remediations/{comp.original.proposal_id}/compensation-requests", headers=headers("requester"),
                           json=command(comp, comp.original.proposal_id, "blocked-request", reason="please assess"))
    assert response.status_code == 200
    assert response.json()["requests"][-1]["status"] == "blocked"
    assert response.json()["compensation"]["reason"] == "safe_compensation_target_not_registered"
    assert len(comp.store.list()) == 1


def test_state_conflict_rejection_tampering_and_original_approval_do_not_execute(web):
    comp, client, _ = web
    run_original(comp)
    pending = child(comp)
    denied = client.post(f"/api/remediations/{pending.proposal_id}/execute", headers=headers("executor"), json=command(comp, pending.proposal_id))
    assert denied.status_code == 409 and denied.json()["detail"] == "proposal_not_approved:pending"
    forged = command(comp, pending.proposal_id, "approve", approved=True, approved_by="forged", role="admin", secret="do-not-echo")
    invalid = client.post(f"/api/remediations/{pending.proposal_id}/approve", headers=headers("approver"), json=forged)
    assert invalid.status_code == 422 and "do-not-echo" not in invalid.text
    rejected = client.post(f"/api/remediations/{pending.proposal_id}/reject", headers=headers("approver"), json=command(comp, pending.proposal_id, "reject", reason="unsafe"))
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert client.post(f"/api/remediations/{pending.proposal_id}/execute", headers=headers("executor"), json=command(comp, pending.proposal_id)).status_code == 409
    assert not (comp.root / "effects").exists()


def test_old_digest_missing_and_archived_incidents_fail_closed(web):
    comp, client, incidents = web
    run_original(comp)
    pending = child(comp)
    body = command(comp, pending.proposal_id)
    body["proposal_digest"] = "f" * 64
    assert client.post(f"/api/remediations/{pending.proposal_id}/approve", headers=headers("approver"), json=body).status_code == 409
    assert client.post("/api/remediations/missing/approve", headers=headers("approver"), json=body).status_code == 404
    incident = incidents.get_incident(pending.incident_id)
    incident.status = "archived"
    incidents.save_incident("fixture-fingerprint", incident)
    assert client.post(f"/api/remediations/{pending.proposal_id}/approve", headers=headers("approver"), json=command(comp, pending.proposal_id)).status_code == 409
    assert comp.store.get(pending.proposal_id).status.value == "pending"


def test_concurrent_web_application_and_approval_create_one_authorization(web):
    comp, client, _ = web
    comp.executor.verifier = FixtureVerifier(comp.source)
    run_original(comp)
    identifier = comp.original.proposal_id
    body = command(comp, identifier, "same-request", reason="undo")
    def apply(number):
        return client.post(f"/api/remediations/{identifier}/compensation-requests", headers=headers("requester"), json=body).status_code
    with ThreadPoolExecutor(2) as pool:
        statuses = list(pool.map(apply, [1, 2]))
    assert set(statuses) <= {200, 409} and 200 in statuses and len(comp.store.list()) == 2
    pending = child(comp)
    approve_body = command(comp, pending.proposal_id, "same-approval")
    def approve(number):
        return client.post(f"/api/remediations/{pending.proposal_id}/approve", headers=headers("approver"), json=approve_body).status_code
    with ThreadPoolExecutor(2) as pool:
        statuses = list(pool.map(approve, [1, 2]))
    assert set(statuses) <= {200, 409} and 200 in statuses
    assert len([e for e in comp.store.load()["audit"] if e["event"] == "approved" and e["proposal_id"] == pending.proposal_id]) == 1


def test_sensitive_raw_results_urls_headers_and_parameters_are_not_exposed(web):
    comp, client, _ = web
    run_original(comp)
    marker = "sensitive-private-material"
    with comp.store.transaction() as data:
        original = data["proposals"][comp.original.proposal_id]
        original["parameters"]["authorization"] = marker
        original["reason"] = "token=" + marker + " https://private.example/path/" + marker
        data["actions"][0]["details"] = {"headers": {"Authorization": marker}, "raw_response": marker}
        data["verifications"][0]["checks"]["raw"] = {"body": marker}
    response = client.get(f"/api/remediations/{comp.original.proposal_id}")
    assert response.status_code == 200 and marker not in response.text
    assert "private.example" not in response.text
    assert "raw_response" not in response.text and "Authorization" not in response.text


def test_missing_store_get_has_no_files_or_locks_and_read_only_rejects_writes(tmp_path):
    path = tmp_path / "nested" / "absent.json"
    with TestClient(create_app(remediation_path=path, authorization=WebAuthorization("")), base_url="http://127.0.0.1") as client:
        assert client.get("/api/remediations").status_code == 503
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(Exception, match="read_only"):
        with RemediationStore(path, read_only=True).transaction():
            pass
