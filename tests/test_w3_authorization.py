"""W3 authorization is server-owned; these are test principals, never people."""
from dataclasses import replace
from datetime import timedelta
import hashlib
import json

from fastapi.testclient import TestClient
import pytest

from compensation_support import FixtureVerifier
from models.remediation import VerificationOutcome as V
from remediation.approval import approval_digest
from remediation.executor import ActionExecutor
from remediation.state import RemediationError
from test_compensation import comp, run_original
from test_web_remediation import command, headers, web
from web_api.app import create_app
from web_api.auth import WebAuthorization


def manual_request(web):
    comp, client, _ = web
    comp.executor.automatic_compensation_request = False
    original = run_original(comp)
    assert len(comp.store.list()) == 1
    response = client.post(
        f"/api/remediations/{original.proposal_id}/compensation-requests",
        headers=headers("requester"), json=command(
            comp, original.proposal_id, "manual-request", reason="Restore reviewed fixture",
        ),
    )
    assert response.status_code == 200, response.text
    return comp.store.get(response.json()["compensation"]["compensation_proposal_id"])


def all_permissions_client(comp, actor, token):
    auth = WebAuthorization(json.dumps([{
        "actor": actor, "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "permissions": ["request", "approve", "execute"],
    }]))
    return TestClient(create_app(
        incident_path=comp.root / "incidents.json", remediation_path=comp.store.file_path,
        authorization=auth,
    ), base_url="http://127.0.0.1", client=("127.0.0.1", 32000))


@pytest.mark.parametrize("actor,operation", [
    ("requester", "approve"), ("requester", "reject"), ("requester", "execute"),
    ("approver", "execute"),
])
def test_same_principal_is_denied_even_with_every_web_permission(web, actor, operation):
    comp, _, _ = web
    pending = manual_request(web)
    comp.store.approve(pending.proposal_id, "approver")
    token = "test-only-multiple-permissions-credential"
    with all_permissions_client(comp, actor, token) as client:
        response = client.post(
            f"/api/remediations/{pending.proposal_id}/{operation}",
            headers={"Authorization": "Bearer " + token},
            json=command(comp, pending.proposal_id, "self-" + operation,
                         **({"reason": "reject"} if operation == "reject" else {})),
        )
    assert response.status_code == 409
    assert response.json()["detail"] in {
        "compensation_requester_cannot_approve", "compensation_requester_cannot_execute",
        "compensation_approver_cannot_execute",
    }
    assert not (comp.root / "effects").exists()
    assert any(e["event"] == "web_operation_denied" and e["actor"] == actor
               for e in comp.store.load()["audit"])


def test_core_store_and_executor_enforce_separation_without_web(web):
    comp, _, _ = web
    pending = manual_request(web)
    with pytest.raises(RemediationError, match="requester_cannot_approve"):
        comp.store.approve(pending.proposal_id, "requester")
    comp.store.approve(pending.proposal_id, "approver")
    for actor in ("requester", "approver"):
        with pytest.raises(RemediationError, match="cannot_execute"):
            ActionExecutor(comp.store).execute(pending.proposal_id, actor)
    assert not (comp.root / "effects").exists()


def test_requester_cannot_supply_approval_identity_or_untrusted_parameters(web):
    comp, client, _ = web
    comp.executor.automatic_compensation_request = False
    run_original(comp)
    response = client.post(
        f"/api/remediations/{comp.original.proposal_id}/compensation-requests",
        headers=headers("requester"), json=command(
            comp, comp.original.proposal_id, "forged", reason="restore",
            approved_by="sensitive-forged-identity", parameters={"target": "untrusted"},
        ),
    )
    assert response.status_code == 422 and "sensitive-forged-identity" not in response.text
    assert len(comp.store.list()) == 1


@pytest.mark.parametrize("operation,role,expected", [
    ("approve", None, 401), ("approve", "wrong", 401),
    ("approve", "requester", 403), ("execute", "approver", 403),
])
def test_missing_wrong_or_insufficient_credentials_have_no_effect(web, operation, role, expected):
    comp, client, _ = web
    pending = manual_request(web)
    before = comp.store.file_path.read_bytes()
    auth_headers = ({} if role is None else {"Authorization": "Bearer " + "x" * 40}
                    if role == "wrong" else headers(role))
    response = client.post(f"/api/remediations/{pending.proposal_id}/{operation}",
                           headers=auth_headers, json=command(comp, pending.proposal_id))
    assert response.status_code == expected
    assert comp.store.file_path.read_bytes() == before


def test_approval_is_once_bound_and_auditable_in_public_view(web):
    comp, client, _ = web
    pending = manual_request(web)
    body = command(comp, pending.proposal_id, "precise-approval")
    url = f"/api/remediations/{pending.proposal_id}/approve"
    first = client.post(url, headers=headers("approver"), json=body)
    assert first.status_code == 200
    assert client.post(url, headers=headers("approver"), json=body).status_code == 200
    assert client.post(url, headers=headers("approver"), json={
        **body, "command_id": "conflicting-second-decision",
    }).status_code == 409
    events = [e for e in first.json()["audit"] if e["event"] == "approved"]
    assert len(events) == 1 and events[0]["actor"] == "approver"
    assert events[0]["proposal_digest"] == approval_digest(pending)
    assert events[0]["command_id"] == "precise-approval"
    assert events[0]["authorization_result"] == "approved"
    assert first.json()["requested_by"] == "requester"
    assert first.json()["source_evidence"]["valid"]
    saved = comp.store.load()
    assert len([e for e in saved["audit"] if e["event"] == "approved"
                and e["proposal_id"] == pending.proposal_id]) == 1


def test_evidence_valid_at_approval_but_expired_before_execution(web, monkeypatch):
    comp, client, _ = web
    pending = manual_request(web)
    comp.store.approve(pending.proposal_id, "approver")
    verification = comp.store.load()["verifications"][0]
    from models.remediation import VerificationResult
    expired = VerificationResult.model_validate(verification).verified_at + timedelta(seconds=301)
    monkeypatch.setattr("remediation.compensation.utc_now", lambda: expired)
    monkeypatch.setattr("web_api.remediation_views.utc_now", lambda: expired)
    view = client.get(f"/api/remediations/{pending.proposal_id}").json()
    assert view["source_evidence"]["status"] == "stale"
    assert not view["source_evidence"]["valid"]
    assert view["source_evidence"]["max_age_seconds"] == 300
    response = client.post(f"/api/remediations/{pending.proposal_id}/execute",
                           headers=headers("executor"), json=command(comp, pending.proposal_id))
    assert response.status_code == 409 and response.json()["detail"] == "verification_stale"
    assert not (comp.root / "effects").exists()
    assert comp.store.get(pending.proposal_id).status.value == "approved"
    assert comp.store.load()["verifications"][0] == verification


def test_slow_instance_probe_cannot_carry_evidence_past_expiry(web, monkeypatch):
    comp, _, _ = web
    pending = manual_request(web)
    comp.store.approve(pending.proposal_id, "approver")
    from models.remediation import VerificationResult
    verified_at = VerificationResult.model_validate(comp.store.load()["verifications"][0]).verified_at

    def slow_probe(proposal, runtime):
        monkeypatch.setattr("remediation.compensation.utc_now", lambda: verified_at + timedelta(seconds=301))
        return comp.source

    monkeypatch.setattr("remediation.compensation.COMPENSATION_REGISTRY", {
        (comp.source.service, comp.contract.original_action_type): replace(comp.contract, inspect_identity=slow_probe),
    })
    action = ActionExecutor(comp.store).execute(pending.proposal_id, "executor")
    assert action.execution_outcome.value == "not_executed"
    assert action.details["reason"] == "verification_stale"
    assert not (comp.root / "effects").exists()


def test_successful_deployment_with_failed_business_verification_is_not_recovered(web, monkeypatch):
    comp, client, _ = web
    pending = manual_request(web)
    comp.store.approve(pending.proposal_id, "approver")
    monkeypatch.setattr("remediation.compensation.COMPENSATION_REGISTRY", {
        (comp.source.service, comp.contract.original_action_type): replace(
            comp.contract, verifier=FixtureVerifier(comp.contract.verifier.observed, V.FAILED),
        ),
    })
    response = client.post(f"/api/remediations/{pending.proposal_id}/execute",
                           headers=headers("executor"), json=command(comp, pending.proposal_id))
    assert response.status_code == 200
    assert response.json()["status"] == "verification_failed"
    assert response.json()["actions"][0]["success"]
    assert response.json()["verifications"][0]["outcome"] == "failed"


def test_operator_revocation_and_rotation_take_effect_only_on_new_authorization_instance(web, monkeypatch):
    comp, _, _ = web
    pending = manual_request(web)
    old_token, new_token = "old-test-credential" * 3, "new-test-credential" * 3

    def configuration(token):
        return json.dumps([{"actor": "approver", "permissions": ["approve"],
                            "token_sha256": hashlib.sha256(token.encode()).hexdigest()}])

    monkeypatch.setenv("INCIDENT_AGENT_WEB_OPERATORS", configuration(old_token))
    paths = {"incident_path": comp.root / "incidents.json", "remediation_path": comp.store.file_path}
    old_app = create_app(authorization=WebAuthorization(), **paths)
    monkeypatch.setenv("INCIDENT_AGENT_WEB_OPERATORS", configuration(new_token))
    new_app = create_app(authorization=WebAuthorization(), **paths)
    for app, expected in ((old_app, (200, 401)), (new_app, (401, 200))):
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 32000)) as client:
            statuses = tuple(client.get("/api/operator", headers={"Authorization": "Bearer " + token}).status_code
                             for token in (old_token, new_token))
            assert statuses == expected
            invalid = new_token if app is old_app else old_token
            assert client.post(f"/api/remediations/{pending.proposal_id}/approve",
                               headers={"Authorization": "Bearer " + invalid},
                               json=command(comp, pending.proposal_id, "revoked-credential")).status_code == 401
    with TestClient(new_app, base_url="http://127.0.0.1", client=("127.0.0.1", 32000)) as client:
        assert client.post(f"/api/remediations/{pending.proposal_id}/approve",
                           headers={"Authorization": "Bearer " + new_token},
                           json=command(comp, pending.proposal_id, "rotated-credential")).status_code == 200
    monkeypatch.setenv("INCIDENT_AGENT_WEB_OPERATORS", "[]")
    with TestClient(create_app(authorization=WebAuthorization()), base_url="http://127.0.0.1",
                    client=("127.0.0.1", 32000)) as client:
        assert client.get("/api/operator", headers={"Authorization": "Bearer " + new_token}).status_code == 503


def test_public_demo_projection_drops_paths_environment_and_interpreter_but_keeps_full_binding(web):
    comp, client, _ = web
    marker = "private-runtime-secret"
    with comp.store.transaction() as data:
        proposal = data["proposals"][comp.original.proposal_id]
        proposal["action_type"] = "w2_inventory_restore_a"
        proposal["parameters"].update({
            "artifact_source": r"C:\Users\private\inventory-A.zip",
            "artifact_sha256": "a" * 64, "configuration_digest": "c" * 64,
            "source_commit": "d" * 40, "target_commit": "d" * 40,
            "release_id": "A", "instance_id": "isolated-example",
            "startup_configuration": json.dumps({"interpreter": r"D:\private\python.exe",
                                                   "environment": {"PASSWORD": marker}}),
        })
        proposal["reason"] = r"artifact C:\Users\private\inventory-A.zip"
    stored = comp.store.get(comp.original.proposal_id)
    before = comp.store.file_path.read_bytes()
    response = client.get(f"/api/remediations/{comp.original.proposal_id}")
    assert response.status_code == 200
    for sensitive in ("artifact_source", "startup_configuration", "interpreter", "PASSWORD",
                      marker, "C:\\\\Users", "D:\\\\private", "inventory-A.zip"):
        assert sensitive not in response.text
    assert response.json()["demo_recovery"]["configuration_digest"] == "c" * 64
    assert response.json()["demo_recovery"]["artifact_sha256"] == "a" * 64
    assert response.json()["proposal_digest"] == approval_digest(stored)
    assert marker in stored.parameters["startup_configuration"]
    assert comp.store.file_path.read_bytes() == before
