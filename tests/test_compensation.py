"""Deterministic admission and lifecycle, using temporary JSON and no network."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock

import pytest

from compensation_support import FixtureVerifier, fixture_contract, identity, original_handler
from models.remediation import ExecutionOutcome as O, RemediationStatus as S, VerificationOutcome as V, utc_now
from models.remediation import VerificationResult
from remediation.approval import approval_digest
from remediation.compensation import evaluate
from remediation.executor import ActionExecutor
from remediation.state import RemediationError
from remediation.store import FileLock, RemediationStore, StoreError
from test_remediation import proposal


@pytest.fixture
def comp(tmp_path, monkeypatch):
    source = identity()
    contract = fixture_contract(source, effect_path=tmp_path / "effects")
    store = RemediationStore(tmp_path / "remediation.json")
    monkeypatch.setattr("remediation.verification.DemoClient.get", Mock(side_effect=AssertionError("offline_fixture_must_not_probe")))
    monkeypatch.setattr("remediation.executor.SERVICE_LOCK", tmp_path / "service.lock")
    monkeypatch.setattr("remediation.compensation.COMPENSATION_REGISTRY", MappingProxyType({
        (source.service, contract.original_action_type): contract,
    }))
    monkeypatch.setattr("remediation.executor.ACTION_REGISTRY", {
        "disable_demo_regression": original_handler(source),
    })
    original = store.create(proposal())
    store.approve(original.proposal_id, "original-approver")
    executor = ActionExecutor(store, verifier=FixtureVerifier(source, V.FAILED))
    return SimpleNamespace(
        root=tmp_path, store=store, original=original, source=source,
        contract=contract, executor=executor, monkeypatch=monkeypatch,
    )


def run_original(comp):
    comp.executor.execute(comp.original.proposal_id, "fixture-executor")
    return comp.store.get(comp.original.proposal_id)


def child(comp):
    return next(p for p in comp.store.list() if p.compensation)


def register(comp, contract):
    comp.monkeypatch.setattr("remediation.compensation.COMPENSATION_REGISTRY", {
        (comp.source.service, comp.contract.original_action_type): contract,
    })


def test_failed_verification_atomically_creates_separate_unapproved_proposal(comp):
    original = run_original(comp)
    pending = child(comp)
    assert original.status == S.VERIFICATION_FAILED and pending.status == S.PENDING
    assert original.approved_by == "original-approver" and pending.approved_by is None
    assert pending.compensation.original_proposal_id == original.proposal_id
    data = comp.store.load()
    assert len(data["actions"]) == len(data["verifications"]) == 1
    assert data["audit"][-1]["details"]["assessment"]["status"] == "approval_required"
    with pytest.raises(RemediationError, match="not_approved"):
        ActionExecutor(comp.store).execute(pending.proposal_id, "operator")
    assert not (comp.root / "effects").exists()


def test_no_production_target_is_registered_or_invented(comp):
    comp.monkeypatch.setattr("remediation.compensation.COMPENSATION_REGISTRY", {})
    run_original(comp)
    data = comp.store.load()
    assert len(data["proposals"]) == 1
    assessment = data["audit"][-1]["details"]["assessment"]
    assert assessment["status"] == "blocked"
    assert assessment["reason"] == "safe_compensation_target_not_registered"
    request = comp.store.request_compensation(comp.original.proposal_id, "operator", "need rollback", "manual-1")
    assert request.status == "blocked" and request.target is None


@pytest.mark.parametrize("outcome", [V.NO_DATA, V.TIMEOUT, V.STALE, V.MISMATCH, V.UNKNOWN])
def test_inconclusive_verification_never_triggers_automatic_compensation(comp, outcome):
    comp.executor.verifier = FixtureVerifier(comp.source, outcome)
    run_original(comp)
    data = comp.store.load()
    assert len(data["proposals"]) == 1
    assert not any(e["event"] == "compensation_evaluated" for e in data["audit"])
    assessment = comp.store.request_compensation(comp.original.proposal_id, "operator", "inspect", "request")
    assert assessment.status == "blocked"


@pytest.mark.parametrize("outcome", [O.NOT_EXECUTED, O.UNKNOWN])
def test_no_reverse_side_effect_for_unexecuted_or_unknown_original(comp, outcome):
    handler = original_handler(comp.source)
    comp.monkeypatch.setattr("remediation.executor.ACTION_REGISTRY", {
        "disable_demo_regression": lambda p, runtime: handler(p, runtime).model_copy(update={"execution_outcome": outcome}),
    })
    run_original(comp)
    assert len(comp.store.list()) == 1
    reason = comp.store.load()["audit"][-1]["details"]["assessment"]["reason"]
    assert reason == ("original_not_executed" if outcome == O.NOT_EXECUTED else "original_execution_unknown")


def test_manual_path_from_success_does_not_inherit_original_approval(comp):
    comp.executor.verifier = FixtureVerifier(comp.source)
    original = run_original(comp)
    assert original.status == S.SUCCEEDED and len(comp.store.list()) == 1
    request = comp.store.request_compensation(original.proposal_id, "requester", "undo", "manual-success")
    pending = comp.store.get(request.compensation_proposal_id)
    assert pending.status == S.PENDING and pending.approved_at is None
    assert pending.compensation.trigger == "operator_request"


@pytest.mark.parametrize("field,value", [
    ("service", "inventory-service"), ("expected_commit_sha", "f" * 40),
    ("parameters", {"service": "order-service", "extra": True}), ("incident_fingerprint", "changed"),
])
def test_changed_source_identity_or_parameters_blocks_child_approval(comp, field, value):
    run_original(comp)
    pending = child(comp)
    with comp.store.transaction() as data:
        data["proposals"][comp.original.proposal_id][field] = value
    with pytest.raises(RemediationError, match="mismatch"):
        comp.store.approve(pending.proposal_id, "approver")


def test_stale_verification_rejected_and_missing_target_does_not_override_unknown(comp):
    run_original(comp)
    pending = child(comp)
    with comp.store.transaction() as data:
        data["verifications"][0]["verified_at"] = (utc_now() - timedelta(hours=1)).isoformat()
        data["verifications"][0]["started_at"] = (utc_now() - timedelta(hours=1, seconds=1)).isoformat()
    with pytest.raises(RemediationError):
        comp.store.approve(pending.proposal_id, "approver")


@pytest.mark.parametrize("alteration", ["service", "action", "digest", "identity"])
def test_unbound_or_wrong_verification_cannot_authorize_compensation(comp, alteration):
    run_original(comp)
    pending = child(comp)
    with comp.store.transaction() as data:
        verification = data["verifications"][0]
        if alteration == "service":
            verification["observed_identity"]["service"] = "other-service"
        elif alteration == "identity":
            verification["observed_identity"]["configuration_digest"] = "f" * 64
        elif alteration == "action":
            verification["action_id"] = "other-action"
        else:
            verification["proposal_digest"] = "f" * 64
    with pytest.raises(RemediationError, match="verification_binding_mismatch"):
        comp.store.approve(pending.proposal_id, "approver")


def test_fresh_but_explicitly_old_bound_observation_is_stale(comp):
    run_original(comp)
    pending = child(comp)
    with comp.store.transaction() as data:
        for field in ("started_at", "finished_at"):
            from datetime import datetime
            data["actions"][0][field] = (datetime.fromisoformat(data["actions"][0][field]) - timedelta(hours=1)).isoformat()
        for field in ("started_at", "verified_at"):
            data["verifications"][0][field] = (datetime.fromisoformat(data["verifications"][0][field]) - timedelta(hours=1)).isoformat()
        original = data["proposals"][comp.original.proposal_id]
        original["executed_at"] = (datetime.fromisoformat(original["executed_at"]) - timedelta(hours=1)).isoformat()
    with pytest.raises(RemediationError, match="verification_stale"):
        comp.store.approve(pending.proposal_id, "approver")


def test_http_200_without_business_object_is_no_data_and_timeout_is_distinct():
    from remediation.verification import DemoVerifier
    from test_remediation import version
    client = Mock()
    client.last_failure = None
    client.get.side_effect = [(200, version(False)), (200, None), (200, None)]
    result = DemoVerifier(client).verify(proposal())
    assert not result.success and result.outcome == V.NO_DATA
    client.last_failure = "timeout"
    client.get.side_effect = [(200, version(False)), (None, None), (200, None)]
    result = DemoVerifier(client).verify(proposal())
    assert not result.success and result.outcome == V.TIMEOUT


def test_actual_verifier_distinguishes_explicit_business_failure_and_mismatched_version():
    from remediation.verification import DemoVerifier
    from test_remediation import version
    client = Mock()
    client.last_failure = None
    client.get.side_effect = [(200, version(False)), (500, {"service": "order-service", "error_type": "application_error"}), (200, None)]
    result = DemoVerifier(client).verify(proposal())
    assert not result.success and result.outcome == V.FAILED and result.observed_identity is not None
    client.get.side_effect = [(200, version(False, sha="b" * 40)), (500, {"service": "order-service"}), (200, None)]
    assert DemoVerifier(client).verify(proposal()).outcome == V.MISMATCH


def test_duplicate_manual_requests_and_commands_never_duplicate_child(comp):
    run_original(comp)
    first = comp.store.request_compensation(comp.original.proposal_id, "requester", "undo", "duplicate")
    again = comp.store.request_compensation(comp.original.proposal_id, "requester", "undo", "duplicate")
    assert first == again and first.compensation_proposal_id == child(comp).proposal_id
    with pytest.raises(RemediationError, match="command_id_conflict"):
        comp.store.request_compensation(comp.original.proposal_id, "other", "undo", "duplicate")
    assert len(comp.store.list()) == 2


def test_approval_binds_every_compensation_field_and_old_approval_cannot_execute(comp):
    run_original(comp)
    pending = child(comp)
    approved = comp.store.approve(pending.proposal_id, "compensation-approver")
    with comp.store.transaction() as data:
        data["proposals"][pending.proposal_id]["parameters"]["target_id"] = "different"
    with pytest.raises(RemediationError, match="proposal_changed"):
        ActionExecutor(comp.store).execute(approved.proposal_id, "operator")
    assert not (comp.root / "effects").exists()


def test_rejected_compensation_never_executes_or_retries(comp):
    run_original(comp)
    pending = child(comp)
    comp.store.reject(pending.proposal_id, "approver", "not safe")
    with pytest.raises(RemediationError, match="not_approved:rejected"):
        ActionExecutor(comp.store).execute(pending.proposal_id, "executor")
    with pytest.raises(RemediationError, match="compensation_retry_forbidden"):
        comp.store.retry(pending.proposal_id, "operator", "again", {})


def test_contract_or_current_identity_changed_after_approval_blocks_execution(comp):
    run_original(comp)
    pending = comp.store.approve(child(comp).proposal_id, "approver")
    register(comp, replace(comp.contract, contract_id="changed"))
    with pytest.raises(RemediationError, match="binding_mismatch"):
        ActionExecutor(comp.store).execute(pending.proposal_id, "operator")
    register(comp, replace(comp.contract, inspect_identity=lambda p, runtime: comp.source.model_copy(update={"configuration_digest": "f" * 64})))
    result = ActionExecutor(comp.store).execute(pending.proposal_id, "operator")
    assert result.execution_outcome == O.NOT_EXECUTED and not result.success
    assert not (comp.root / "effects").exists()


@pytest.mark.parametrize("outcome", [V.PASSED, V.FAILED, V.NO_DATA])
def test_compensation_uses_same_executor_and_independent_verifier_without_recursion(comp, outcome):
    register(comp, fixture_contract(comp.source, effect_path=comp.root / "effects", outcome=outcome))
    run_original(comp)
    pending = comp.store.approve(child(comp).proposal_id, "comp-approver")
    result = ActionExecutor(comp.store).execute(pending.proposal_id, "executor")
    assert result.execution_outcome == O.EXECUTED
    assert (comp.root / "effects").read_text().splitlines() == ["effect"]
    assert comp.store.get(pending.proposal_id).status == (S.SUCCEEDED if outcome == V.PASSED else S.VERIFICATION_FAILED)
    assert len(comp.store.list()) == 2 and len(comp.store.load()["verifications"]) == 2
    if outcome == V.PASSED:
        ActionExecutor(comp.store).execute(pending.proposal_id, "operator")
    else:
        with pytest.raises(RemediationError):
            ActionExecutor(comp.store).execute(pending.proposal_id, "operator")
    assert (comp.root / "effects").read_text().splitlines() == ["effect"]


def test_unknown_compensation_is_not_replayed_or_claimed_as_success(comp):
    register(comp, fixture_contract(comp.source, execution_outcome=O.UNKNOWN))
    run_original(comp)
    approved = comp.store.approve(child(comp).proposal_id, "approver")
    result = ActionExecutor(comp.store).execute(approved.proposal_id, "operator")
    assert not result.success and comp.store.get(approved.proposal_id).status == S.EXECUTING
    assert comp.store.execution_outcome(comp.store.load(), comp.store.get(approved.proposal_id)) == O.UNKNOWN
    with pytest.raises(RemediationError):
        ActionExecutor(comp.store).execute(approved.proposal_id, "operator")
    ActionExecutor(comp.store).close_execution(approved.proposal_id, "operator", "unknown effect", "close-unknown")
    assert comp.store.get(approved.proposal_id).closure.execution_outcome == O.UNKNOWN


def test_removed_contract_does_not_block_manual_takeover_or_invent_health(comp):
    register(comp, fixture_contract(comp.source, execution_outcome=O.UNKNOWN))
    run_original(comp)
    approved = comp.store.approve(child(comp).proposal_id, "approver")
    ActionExecutor(comp.store).execute(approved.proposal_id, "operator")
    comp.monkeypatch.setattr("remediation.compensation.COMPENSATION_REGISTRY", {})
    result = ActionExecutor(comp.store).close_execution(
        approved.proposal_id, "operator", "contract revoked, manual takeover", "close-revoked",
    )
    assert result["current_observation"]["outcome"] == "no_data"
    assert result["current_observation"]["success"] is False
    assert comp.store.get(approved.proposal_id).closure.execution_outcome == O.UNKNOWN


def test_legacy_true_verifier_cannot_attest_compensation_recovery(comp):
    verifier = Mock()
    verifier.verify.side_effect = lambda p: VerificationResult(
        proposal_id=p.proposal_id, success=True, checks={}, summary="Legacy boolean only",
    )
    register(comp, replace(comp.contract, verifier=verifier))
    run_original(comp)
    approved = comp.store.approve(child(comp).proposal_id, "approver")
    ActionExecutor(comp.store).execute(approved.proposal_id, "operator")
    assert comp.store.get(approved.proposal_id).status == S.VERIFICATION_FAILED
    verification = comp.store.load()["verifications"][-1]
    assert verification["outcome"] == "unknown" and not verification["success"]


def test_compensation_requires_action_identity_for_independent_verification_binding(comp):
    handler = comp.contract.handler
    register(comp, replace(comp.contract, handler=lambda p, runtime: handler(p, runtime).model_copy(update={"resulting_identity": None})))
    run_original(comp)
    approved = comp.store.approve(child(comp).proposal_id, "approver")
    ActionExecutor(comp.store).execute(approved.proposal_id, "operator")
    assert comp.store.get(approved.proposal_id).status == S.VERIFICATION_FAILED
    assert comp.store.load()["verifications"][-1]["outcome"] == "mismatch"


def test_concurrent_requests_approvals_and_executions_cannot_duplicate_effect(comp):
    comp.executor.verifier = FixtureVerifier(comp.source)
    run_original(comp)
    barrier = threading.Barrier(2)

    def request(number):
        barrier.wait()
        try:
            return comp.store.request_compensation(comp.original.proposal_id, "operator", "undo", f"req-{number}").status
        except StoreError as exc:
            return str(exc)
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(request, [1, 2]))
    assert "approval_required" in outcomes and len(comp.store.list()) == 2
    pending = child(comp)

    def approve(number):
        try:
            return comp.store.approve(pending.proposal_id, "approver").status
        except RemediationError:
            return "denied"
    with ThreadPoolExecutor(2) as pool:
        approvals = list(pool.map(approve, [1, 2]))
    assert approvals.count(S.APPROVED) == 1
    def execute(number):
        try:
            return ActionExecutor(comp.store).execute(pending.proposal_id, "operator").status
        except RemediationError:
            return "denied"
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(execute, [1, 2]))
    assert (comp.root / "effects").read_text().splitlines() == ["effect"]


def test_failed_atomic_claim_commit_prevents_compensation_handler(comp):
    run_original(comp)
    approved = comp.store.approve(child(comp).proposal_id, "approver")
    comp.monkeypatch.setattr(comp.store, "_save", Mock(side_effect=StoreError("write_failed")))
    with pytest.raises(StoreError):
        ActionExecutor(comp.store).execute(approved.proposal_id, "operator")
    assert not (comp.root / "effects").exists()


def test_legacy_records_keep_digest_and_unknown_verification_without_rewrite(comp):
    data = comp.store.load()
    for p in data["proposals"].values():
        p.pop("compensation"); p.pop("incident_fingerprint")
    data["version"] = 1
    raw = json.dumps(data).encode()
    comp.store.file_path.write_bytes(raw)
    loaded = RemediationStore(comp.store.file_path, read_only=True).get(comp.original.proposal_id)
    assert approval_digest(loaded) == loaded.approval_digest
    assert comp.store.file_path.read_bytes() == raw


def test_read_only_queries_share_existing_os_lock_without_writing_or_creating_one(comp):
    lock_path = Path(str(comp.store.file_path) + ".lock")
    before = lock_path.read_bytes()
    with FileLock(lock_path, read_only=True):
        with pytest.raises(StoreError, match="store_busy"):
            comp.store.approve(comp.original.proposal_id, "approver")
    assert lock_path.read_bytes() == before
    readonly = RemediationStore(comp.store.file_path, read_only=True)
    assert readonly.get(comp.original.proposal_id).proposal_id == comp.original.proposal_id
    legacy_path = comp.root / "legacy-without-lock.json"
    legacy_path.write_bytes(comp.store.file_path.read_bytes())
    RemediationStore(legacy_path, read_only=True).load()
    assert not Path(str(legacy_path) + ".lock").exists()


@pytest.mark.parametrize("phase", ["executing_committed", "action_returned", "action_committed"])
def test_real_compensation_child_crash_never_blindly_replays(comp, phase):
    run_original(comp)
    approved = comp.store.approve(child(comp).proposal_id, "approver")
    result = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).with_name("compensation_process_fixture.py")),
         str(comp.root), approved.proposal_id, phase],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 73, result.stderr
    restored = RemediationStore(comp.store.file_path)
    assert restored.get(approved.proposal_id).status == S.EXECUTING
    assert len([a for a in restored.load()["actions"] if a["proposal_id"] == approved.proposal_id]) == int(phase == "action_committed")
    with pytest.raises(RemediationError, match="not_approved:executing"):
        ActionExecutor(restored).execute(approved.proposal_id, "operator")
    assert (comp.root / "effects").exists() == (phase != "executing_committed")
    ActionExecutor(restored).close_execution(approved.proposal_id, "operator", "manual takeover", "close")
    assert restored.get(approved.proposal_id).status == S.CLOSED
    assert restored.get(approved.proposal_id).closure.execution_outcome == (O.EXECUTED if phase == "action_committed" else O.UNKNOWN)
