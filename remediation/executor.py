"""The only production entry to actions: persisted approval -> claim -> act -> verify."""
from types import MappingProxyType
from pathlib import Path

from models.remediation import ActionResult, RemediationStatus as S, VerificationResult, utc_now
from remediation.approval import actor_name, approval_digest
from remediation.demo_action import disable_demo_regression, validate_parameters
from remediation.state import RemediationError, transition
from remediation.store import FileLock
from remediation.verification import DemoVerifier

ACTION_REGISTRY = MappingProxyType({"disable_demo_regression": disable_demo_regression})
SERVICE_LOCK = Path(__file__).resolve().parents[1] / "data" / "order-service-remediation.lock"


class ActionExecutor:
    def __init__(self, store, runtime=None, verifier=None):
        self.store = store
        self.runtime = runtime
        self.verifier = verifier or DemoVerifier()

    def execute(self, proposal_id, actor):
        actor = actor_name(actor)
        # Serializes actions for the service even if proposals/stores differ.
        with FileLock(SERVICE_LOCK):
            with self.store.transaction() as data:
                proposal = self.store.from_data(data, proposal_id)
                if proposal.status == S.SUCCEEDED:
                    self.store.event(data, proposal, "execution_skipped", actor, reason="already_completed")
                    return ActionResult(proposal_id=proposal_id, action_type=proposal.action_type,
                        started_at=utc_now(), success=True, status="already_completed",
                        summary="该 proposal 已完成，未再次执行动作。")
                rejection = None
                if proposal.status != S.APPROVED:
                    rejection = "proposal_not_approved:" + proposal.status.value
                elif proposal.action_type not in ACTION_REGISTRY:
                    rejection = "action_not_allowed"
                elif not proposal.approved_by or not proposal.approved_at or proposal.approval_digest != approval_digest(proposal):
                    rejection = "approval_unconfirmed_or_proposal_changed"
                else:
                    try:
                        validate_parameters(proposal)
                    except RemediationError as exc:
                        rejection = str(exc)
                if rejection:
                    self.store.event(data, proposal, "execution_denied", actor, reason=rejection)
                else:
                    proposal = transition(proposal, S.EXECUTING, executed_at=utc_now())
                    self.store.put(data, proposal)
                    self.store.event(data, proposal, "execution_started", actor)
            # Exit transaction first: durable executing state is required before side effects.
            if rejection:
                raise RemediationError(rejection)
            try:
                action = ACTION_REGISTRY[proposal.action_type](proposal, self.runtime)
            except Exception:
                action = ActionResult(proposal_id=proposal_id, action_type=proposal.action_type,
                    started_at=proposal.executed_at, success=False, status="handler_failed",
                    summary="执行器捕获固定 handler 异常，需要人工检查。")
            # Persist physical action result before verification; crash remains executing, never retried.
            with self.store.transaction() as data:
                data["actions"].append(action.model_dump(mode="json"))
                self.store.event(data, proposal, "action_succeeded" if action.success else "action_failed",
                                 actor, status=action.status)
                if not action.success:
                    self.store.put(data, transition(proposal, S.FAILED))
            if not action.success:
                return action
            try:
                verification = self.verifier.verify(proposal)
            except Exception:
                verification = VerificationResult(proposal_id=proposal_id, success=False,
                    checks={"verification": {"success": False}}, summary="验证异常，需要人工检查。")
            with self.store.transaction() as data:
                data["verifications"].append(verification.model_dump(mode="json"))
                self.store.event(data, proposal, "verification_succeeded" if verification.success else "verification_failed",
                                 "demo_verifier", checks=verification.checks)
                final = transition(proposal, S.SUCCEEDED if verification.success else S.VERIFICATION_FAILED)
                self.store.put(data, final)
                self.store.event(data, final, "remediation_" + final.status.value, actor)
            return action
