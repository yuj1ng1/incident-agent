"""CLI action/verification semantics; every physical operation is mocked."""
from contextlib import redirect_stdout
from datetime import timedelta
import io
import json
from unittest.mock import Mock, patch

import pytest

from models.remediation import ActionResult, ExecutionOutcome, RemediationProposal, VerificationResult, utc_now
from remediation.executor import ActionExecutor
from remediation.state import RemediationError
from remediation.store import RemediationStore
from remediation_cli import main as cli


@pytest.fixture
def execution(tmp_path, monkeypatch):
    monkeypatch.setattr("remediation.executor.SERVICE_LOCK", tmp_path / "service.lock")
    store = RemediationStore(tmp_path / "remediation.json")
    proposal = store.create(RemediationProposal(
        incident_id="inc-cli", service="order-service", summary="Fixture repair", reason="Fixture evidence",
        action_type="disable_demo_regression", parameters={"service": "order-service"}, risk="low",
        expected_commit_sha="a" * 40, expected_process_started_at=utc_now() - timedelta(minutes=1),
    ))
    runtime = Mock()
    runtime.client.version.return_value = {
        "service": "order-service", "commit_sha": proposal.expected_commit_sha,
        "process_started_at": proposal.expected_process_started_at.isoformat(),
        "runtime_flags": {"simulate_code_regression": True},
    }
    runtime.listener.return_value = {"pid": 123}
    verifier = Mock()
    verifier.verify.return_value = VerificationResult(
        proposal_id=proposal.proposal_id, success=True, checks={"fixture": {"success": True}}, summary="Verified",
    )
    return store, proposal, runtime, verifier, ActionExecutor(store, runtime=runtime, verifier=verifier)


def invoke(execution):
    store, proposal, runtime, verifier, executor = execution
    output = io.StringIO()
    with patch("remediation.executor.ActionExecutor", return_value=executor), redirect_stdout(output):
        code = cli(["--store", str(store.file_path), "execute", proposal.proposal_id])
    return code, json.loads(output.getvalue())


def test_execution_success_is_independently_verified(execution):
    store, proposal, runtime, verifier, executor = execution
    store.approve(proposal.proposal_id, "operator")
    code, output = invoke(execution)
    assert code == 0 and output["proposal_status"] == "succeeded"
    assert output["execution_outcome"] == "executed" and output["action_performed"] is True
    assert output["verification_performed"] and output["verification"]["success"]
    assert output["current_target_verified"] is True
    assert not output["historical_proposal_succeeded"] and not output["target_already_remediated"]
    assert output["verification"]["verification_id"] == store.load()["verifications"][0]["verification_id"]
    runtime.stop.assert_called_once()
    verifier.verify.assert_called_once()


def test_already_completed_is_historical_success_without_fresh_observation(execution):
    store, proposal, runtime, verifier, executor = execution
    store.approve(proposal.proposal_id, "operator")
    executor.execute(proposal.proposal_id, "operator")
    actions = len(store.load()["actions"])
    verifications = len(store.load()["verifications"])
    code, output = invoke(execution)
    assert code == 0
    assert output["action"]["status"] == "already_completed"
    assert output["historical_proposal_succeeded"]
    assert output["execution_outcome"] == "not_executed" and output["action_performed"] is False
    assert not output["verification_performed"] and output["verification"] is None
    assert output["current_target_verified"] is None and not output["target_already_remediated"]
    assert len(store.load()["actions"]) == actions and len(store.load()["verifications"]) == verifications
    runtime.stop.assert_called_once()
    verifier.verify.assert_called_once()


@pytest.mark.parametrize("verified", [True, False])
def test_already_remediated_skips_action_but_performs_verification(execution, verified):
    store, proposal, runtime, verifier, executor = execution
    store.approve(proposal.proposal_id, "operator")
    runtime.client.version.return_value["runtime_flags"]["simulate_code_regression"] = False
    verifier.verify.return_value = verifier.verify.return_value.model_copy(update={"success": verified})
    code, output = invoke(execution)
    assert code == (0 if verified else 1)
    assert output["action"]["status"] == "already_remediated"
    assert output["execution_outcome"] == "not_executed" and output["action_performed"] is False
    assert output["verification_performed"] and output["verification"]["success"] == verified
    assert output["current_target_verified"] == verified
    assert output["target_already_remediated"] == verified
    assert not output["historical_proposal_succeeded"]
    runtime.stop.assert_not_called()
    runtime.start.assert_not_called()
    verifier.verify.assert_called_once()


def test_action_failure_has_no_verification(execution):
    store, proposal, runtime, verifier, executor = execution
    store.approve(proposal.proposal_id, "operator")
    runtime.repository_ready.side_effect = RemediationError("fixture_denied")
    code, output = invoke(execution)
    assert code == 1 and output["proposal_status"] == "failed"
    assert output["action_performed"] is False and output["execution_outcome"] == "not_executed"
    assert not output["verification_performed"] and output["verification"] is None
    assert output["current_target_verified"] is None
    verifier.verify.assert_not_called()
    runtime.stop.assert_not_called()


@pytest.mark.parametrize("exception", [False, True])
def test_verification_failure_does_not_relabel_physical_action(execution, exception):
    store, proposal, runtime, verifier, executor = execution
    store.approve(proposal.proposal_id, "operator")
    if exception:
        verifier.verify.side_effect = RuntimeError("private credential")
    else:
        verifier.verify.return_value = verifier.verify.return_value.model_copy(update={"success": False})
    code, output = invoke(execution)
    assert code == 1 and output["proposal_status"] == "verification_failed"
    assert output["action_performed"] is True and output["execution_outcome"] == "executed"
    assert output["verification_performed"] and not output["verification"]["success"]
    assert output["current_target_verified"] is False
    assert "private credential" not in json.dumps(output)
    runtime.stop.assert_called_once()


def test_unknown_action_outcome_is_not_claimed_as_false(execution, monkeypatch):
    store, proposal, runtime, verifier, executor = execution
    store.approve(proposal.proposal_id, "operator")
    unknown = ActionResult(
        proposal_id=proposal.proposal_id, action_type=proposal.action_type, started_at=utc_now(),
        success=False, status="handler_failed", summary="Unknown physical outcome",
        execution_outcome=ExecutionOutcome.UNKNOWN,
    )
    monkeypatch.setattr("remediation.executor.ACTION_REGISTRY", {proposal.action_type: lambda p, r: unknown})
    code, output = invoke(execution)
    assert code == 1
    assert output["execution_outcome"] == "unknown" and output["action_performed"] is None
    assert not output["verification_performed"] and output["current_target_verified"] is None
    verifier.verify.assert_not_called()


def test_unapproved_execute_still_returns_denial_code(execution):
    store, proposal, runtime, verifier, executor = execution
    code, output = invoke(execution)
    assert code == 2 and not output["success"]
    runtime.stop.assert_not_called()
    verifier.verify.assert_not_called()


def test_cli_uses_invocation_verification_instead_of_latest_manual_observation(execution):
    store, proposal, runtime, verifier, executor = execution
    store.approve(proposal.proposal_id, "operator")
    original = executor.execute

    def execute(proposal_id, actor):
        result = original(proposal_id, actor)
        unrelated = VerificationResult(
            proposal_id=proposal_id, success=False, checks={"fixture": {"success": False}},
            summary="Later manual observation", purpose="manual_check",
        )
        with store.transaction() as data:
            data["verifications"].append(unrelated.model_dump(mode="json"))
        return result

    with patch.object(executor, "execute", execute):
        code, output = invoke(execution)
    assert code == 0 and output["verification"]["success"]
    assert output["verification"]["purpose"] == "post_action"
    assert output["verification"]["verification_id"] != store.load()["verifications"][-1]["verification_id"]
