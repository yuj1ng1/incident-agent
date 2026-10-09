"""The only production entry to actions: persisted approval -> claim -> act -> verify."""
from types import MappingProxyType
from pathlib import Path
import logging

from models.remediation import (
    ActionResult, ExecutionOutcome, ManualClosure, RemediationStatus as S, VerificationResult, utc_now,
)
from remediation.approval import actor_name, approval_digest
from remediation.demo_action import disable_demo_regression, validate_parameters
from remediation.state import RemediationError, transition
from remediation.store import FileLock
from remediation.verification import DemoVerifier

ACTION_REGISTRY = MappingProxyType({"disable_demo_regression": disable_demo_regression})
SERVICE_LOCK = Path(__file__).resolve().parents[1] / "data" / "order-service-remediation.lock"
logger = logging.getLogger(__name__)


class ActionExecutor:
    def __init__(self, store, runtime=None, verifier=None, boundary_hook=None):
        self.store = store
        self.runtime = runtime
        self.verifier = verifier or DemoVerifier()
        self.boundary_hook = boundary_hook

    def _boundary(self, phase, proposal_id):
        # Test-only observation seam; production never terminates processes here.
        if self.boundary_hook is not None:
            self.boundary_hook(phase, proposal_id)

    def execute(self, proposal_id, actor):
        actor = actor_name(actor)
        # Serializes actions for the service even if proposals/stores differ.
        with FileLock(SERVICE_LOCK):
            with self.store.transaction() as data:
                proposal = self.store.from_data(data, proposal_id)
                if proposal.status == S.SUCCEEDED:
                    self.store.event(data, proposal, "execution_skipped", actor, reason="already_completed")
                    return ActionResult(proposal_id=proposal_id, action_type=proposal.action_type,
                        started_at=utc_now(), success=False, status="already_completed",
                        summary="该 proposal 历史状态为已完成，本次未执行动作、未重新验证。",
                        details={"verification_performed": False, "historical_status": proposal.status.value},
                        execution_outcome=ExecutionOutcome.NOT_EXECUTED)
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
            self._boundary("executing_committed", proposal_id)
            try:
                action = ACTION_REGISTRY[proposal.action_type](proposal, self.runtime)
            except Exception as exc:
                logger.error("remediation_handler_failed proposal=%s exception_type=%s", proposal_id, type(exc).__name__)
                action = ActionResult(proposal_id=proposal_id, action_type=proposal.action_type,
                    started_at=proposal.executed_at, success=False, status="handler_failed",
                    summary="执行器捕获固定 handler 异常，需要人工检查。",
                    details={"exception_type": type(exc).__name__})
            self._boundary("action_returned", proposal_id)
            # Persist physical action result before verification; crash remains executing, never retried.
            with self.store.transaction() as data:
                data["actions"].append(action.model_dump(mode="json"))
                if action.details.get("exception_type"):
                    self.store.event(data, proposal, "handler_exception", actor,
                                     exception_type=action.details["exception_type"],
                                     execution_outcome=action.execution_outcome.value)
                self.store.event(data, proposal, "action_succeeded" if action.success else "action_failed",
                                 actor, status=action.status)
                if not action.success:
                    self.store.put(data, transition(proposal, S.FAILED))
            self._boundary("action_committed", proposal_id)
            if not action.success:
                return action
            try:
                verification = self.verifier.verify(proposal)
            except Exception as exc:
                logger.error("remediation_verifier_failed proposal=%s exception_type=%s", proposal_id, type(exc).__name__)
                verification = VerificationResult(proposal_id=proposal_id, success=False,
                    checks={"verification": {"success": False, "exception_type": type(exc).__name__}},
                    summary="验证异常，需要人工检查。")
            with self.store.transaction() as data:
                data["verifications"].append(verification.model_dump(mode="json"))
                self.store.event(data, proposal, "verification_succeeded" if verification.success else "verification_failed",
                                 "demo_verifier", checks=verification.checks)
                final = transition(proposal, S.SUCCEEDED if verification.success else S.VERIFICATION_FAILED)
                self.store.put(data, final)
                self.store.event(data, final, "remediation_" + final.status.value, actor)
            return action

    def _review_target(self, proposal_id, actor):
        proposal = self.store.get(proposal_id)
        try:
            if proposal.action_type not in ACTION_REGISTRY:
                raise RemediationError("action_not_allowed")
            validate_parameters(proposal)
        except RemediationError as exc:
            with self.store.transaction() as data:
                self.store.event(data, proposal, "manual_review_denied", actor, reason=str(exc))
            raise
        return proposal

    def _observe(self, proposal, actor):
        with self.store.transaction() as data:
            self.store.event(data, proposal, "manual_check_started", actor)
        started = utc_now()
        try:
            verification = VerificationResult.model_validate(self.verifier.verify(proposal))
            if (verification.proposal_id != proposal.proposal_id
                    or not started <= verification.verified_at <= utc_now()):
                raise RemediationError("manual_check_result_not_fresh_or_mismatched")
            verification = verification.model_copy(update={"purpose": "manual_check"})
        except Exception as exc:
            logger.error("remediation_manual_check_failed proposal=%s exception_type=%s",
                         proposal.proposal_id, type(exc).__name__)
            verification = VerificationResult(
                proposal_id=proposal.proposal_id, success=False, purpose="manual_check",
                checks={"observation": {"success": False, "exception_type": type(exc).__name__}},
                summary="本次可信核查未完成；不能推断历史动作结果。",
            )
        return verification

    def inspect_execution(self, proposal_id, actor):
        """Fresh read-only checks describe current health, never historical action success."""
        actor = actor_name(actor)
        with FileLock(SERVICE_LOCK):
            proposal = self._review_target(proposal_id, actor)
            verification = self._observe(proposal, actor)
            with self.store.transaction() as data:
                proposal = self.store.from_data(data, proposal_id)
                data["verifications"].append(verification.model_dump(mode="json"))
                outcome = self.store.execution_outcome(data, proposal)
                self.store.event(data, proposal, "manual_check_completed", actor,
                                 verification_id=verification.verification_id,
                                 current_checks_passed=verification.success, execution_outcome=outcome.value)
            return {"proposal": proposal.model_dump(mode="json"), "execution_outcome": outcome.value,
                    "current_observation": verification.model_dump(mode="json")}

    def close_execution(self, proposal_id, actor, reason, command_id):
        actor = actor_name(actor)
        command_id = actor_name(command_id)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise RemediationError("closure_reason_required_max_1000")
        with FileLock(SERVICE_LOCK):
            proposal = self._review_target(proposal_id, actor)
            denial = None
            with self.store.transaction() as data:
                proposal = self.store.from_data(data, proposal_id)
                if proposal.status == S.CLOSED and proposal.closure:
                    old = proposal.closure
                    if (old.command_id, old.actor, old.reason) == (command_id, actor, reason):
                        self.store.event(data, proposal, "manual_closure_repeated", actor, command_id=command_id)
                        return {"proposal": proposal.model_dump(mode="json"), "already_closed": True,
                                "verification_performed": False}
                    denial = "closure_command_conflict"
                elif proposal.status != S.EXECUTING:
                    denial = "closure_requires_executing_proposal"
                elif any(p.get("closure", {}).get("command_id") == command_id
                         for p in data["proposals"].values() if p.get("closure")):
                    denial = "closure_command_id_exists"
                if denial:
                    self.store.event(data, proposal, "manual_closure_denied", actor,
                                     reason=denial, command_id=command_id)
            if denial:
                raise RemediationError(denial)
            verification = self._observe(proposal, actor)
            with self.store.transaction() as data:
                current = self.store.from_data(data, proposal_id)
                if current != proposal:
                    raise RemediationError("proposal_changed_during_manual_check")
                outcome = self.store.execution_outcome(data, current)
                closure = ManualClosure(
                    command_id=command_id, actor=actor, reason=reason, execution_outcome=outcome,
                    action_ids=[a["action_id"] for a in data["actions"] if a["proposal_id"] == proposal_id],
                    verification_id=verification.verification_id,
                )
                closed = transition(current, S.CLOSED, closure=closure)
                data["verifications"].append(verification.model_dump(mode="json"))
                self.store.put(data, closed)
                self.store.event(data, closed, "manual_execution_closed", actor,
                                 **closure.model_dump(mode="json", exclude={"actor"}),
                                 current_checks_passed=verification.success)
            return {"proposal": closed.model_dump(mode="json"), "already_closed": False,
                    "verification_performed": True, "current_observation": verification.model_dump(mode="json")}

    def retry_proposal(self, proposal_id, actor, reason):
        """No action is replayed: a new pending proposal requires a new approval."""
        from remediation.planner import parse_time
        from remediation.verification import DemoClient
        actor = actor_name(actor)
        with FileLock(SERVICE_LOCK):
            proposal = self._review_target(proposal_id, actor)
            with self.store.transaction() as data:
                self.store.event(data, proposal, "retry_check_started", actor)
            try:
                client = self.runtime.client if self.runtime else DemoClient()
                version = client.version()
                if (version["service"] != proposal.service
                        or version["commit_sha"] != proposal.expected_commit_sha
                        or parse_time(version["process_started_at"]) != proposal.expected_process_started_at
                        or version["runtime_flags"]["simulate_code_regression"] is not True):
                    raise RemediationError("retry_target_changed_or_no_longer_needed")
            except Exception as exc:
                with self.store.transaction() as data:
                    self.store.event(data, proposal, "retry_denied", actor, reason="fresh_target_check_failed",
                                     exception_type=type(exc).__name__)
                raise RemediationError("retry_fresh_target_check_failed") from None
            observation = {"checked_at": utc_now().isoformat(), "version": version}
            return self.store.retry(proposal_id, actor, reason, observation)
