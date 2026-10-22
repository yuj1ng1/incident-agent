"""The only production entry to actions: persisted approval -> claim -> act -> verify."""
from types import MappingProxyType
from pathlib import Path
import logging

from models.remediation import (
    ActionResult, ExecutionOutcome, ManualClosure, RemediationStatus as S,
    ServiceIdentity, VerificationOutcome as V, VerificationResult, utc_now,
)
from remediation.approval import actor_name, approval_digest
from remediation.compensation import contract_for, execution_contract, record_request
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
        self._verifier_override = verifier is not None
        self.boundary_hook = boundary_hook

    def _boundary(self, phase, proposal_id):
        # Test-only observation seam; production never terminates processes here.
        if self.boundary_hook is not None:
            self.boundary_hook(phase, proposal_id)

    def execute(
        self,
        proposal_id,
        actor,
        *,
        command_id=None,
        expected_digest=None,
    ):
        actor = actor_name(actor)
        # Serializes actions for the service even if proposals/stores differ.
        with FileLock(SERVICE_LOCK):
            with self.store.transaction() as data:
                proposal = self.store.from_data(data, proposal_id)
                if self.store.check_command(
                    data, proposal, "execution_started", actor, command_id, expected_digest,
                ):
                    return ActionResult(
                        proposal_id=proposal_id, action_type=proposal.action_type,
                        started_at=utc_now(), success=False, status="execution_already_requested",
                        summary="执行请求已持久化；请读取当前记录，不重放动作。",
                        execution_outcome=ExecutionOutcome.NOT_EXECUTED,
                    )
                if proposal.status == S.SUCCEEDED:
                    self.store.event(data, proposal, "execution_skipped", actor, reason="already_completed")
                    return ActionResult(proposal_id=proposal_id, action_type=proposal.action_type,
                        started_at=utc_now(), success=False, status="already_completed",
                        summary="该 proposal 历史状态为已完成，本次未执行动作、未重新验证。",
                        details={"verification_performed": False, "historical_status": proposal.status.value},
                        execution_outcome=ExecutionOutcome.NOT_EXECUTED)
                rejection = None
                contract = None
                if proposal.status != S.APPROVED:
                    rejection = "proposal_not_approved:" + proposal.status.value
                elif proposal.compensation is None and proposal.action_type not in ACTION_REGISTRY:
                    rejection = "action_not_allowed"
                elif not proposal.approved_by or not proposal.approved_at or proposal.approval_digest != approval_digest(proposal):
                    rejection = "approval_unconfirmed_or_proposal_changed"
                else:
                    try:
                        if proposal.compensation is not None:
                            contract = execution_contract(data, proposal)
                        else:
                            validate_parameters(proposal)
                    except RemediationError as exc:
                        rejection = str(exc)
                if rejection:
                    self.store.event(data, proposal, "execution_denied", actor, reason=rejection)
                else:
                    proposal = transition(proposal, S.EXECUTING, executed_at=utc_now())
                    self.store.put(data, proposal)
                    self.store.event(data, proposal, "execution_started", actor, **(
                        {"command_id": command_id, "proposal_digest": expected_digest}
                        if command_id else {}
                    ))
            # Exit transaction first: durable executing state is required before side effects.
            if rejection:
                raise RemediationError(rejection)
            self._boundary("executing_committed", proposal_id)
            preflight_failed = False
            if contract is not None:
                try:
                    identity = ServiceIdentity.model_validate(contract.inspect_identity(proposal, self.runtime))
                    if identity != proposal.compensation.source_identity:
                        raise RemediationError("compensation_current_identity_changed")
                except Exception as exc:
                    preflight_failed = True
                    action = ActionResult(
                        proposal_id=proposal_id, action_type=proposal.action_type,
                        started_at=proposal.executed_at, success=False,
                        status="compensation_precondition_failed",
                        summary="补偿目标的当前身份或前置条件未通过，未执行补偿。",
                        details={"exception_type": type(exc).__name__},
                        execution_outcome=ExecutionOutcome.NOT_EXECUTED,
                    )
            try:
                if not preflight_failed:
                    handler = contract.handler if contract else ACTION_REGISTRY[proposal.action_type]
                    action = ActionResult.model_validate(handler(proposal, self.runtime))
                    if action.proposal_id != proposal_id or action.action_type != proposal.action_type:
                        raise RemediationError("handler_result_mismatch")
                    if contract and action.execution_outcome == ExecutionOutcome.UNKNOWN:
                        action = action.model_copy(update={"success": False, "status": "execution_unknown"})
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
                if not action.success and not (
                    contract and action.execution_outcome == ExecutionOutcome.UNKNOWN
                ):
                    self.store.put(data, transition(proposal, S.FAILED))
            self._boundary("action_committed", proposal_id)
            if not action.success:
                return action.model_copy(update={"details": action.details | {"verification_performed": False}})
            verification_started = utc_now()
            try:
                verifier = self.verifier if self._verifier_override or not contract else contract.verifier
                verification = VerificationResult.model_validate(verifier.verify(proposal))
                if verification.proposal_id != proposal_id or verification.purpose != "post_action":
                    raise RemediationError("verification_identity_mismatch")
                if contract and verification.outcome == V.UNKNOWN:
                    # Legacy boolean-only attestations cannot fulfil a new compensation contract.
                    verification = verification.model_copy(update={"success": False})
                if verification.outcome != V.UNKNOWN and (
                    verification.started_at is None
                    or not verification_started <= verification.started_at <= verification.verified_at <= utc_now()
                    or verification.success != (verification.outcome == V.PASSED)
                ):
                    verification = verification.model_copy(update={"success": False, "outcome": V.MISMATCH})
                if verification.outcome in {V.PASSED, V.FAILED} and (
                    verification.observed_identity is None
                    or (contract and action.resulting_identity is None)
                    or (action.resulting_identity is not None
                        and verification.observed_identity != action.resulting_identity)
                    or verification.observed_identity.service != proposal.service
                    or (contract and verification.outcome == V.PASSED and (
                        verification.observed_identity.commit_sha != contract.target.commit_sha
                        or verification.observed_identity.configuration_digest != contract.target.configuration_digest
                    ))
                ):
                    verification = verification.model_copy(update={"success": False, "outcome": V.MISMATCH})
                verification = verification.model_copy(update={
                    "action_id": action.action_id, "proposal_digest": approval_digest(proposal),
                })
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
                if not final.compensation and verification.outcome == V.FAILED and not verification.success:
                    # Same atomic commit as the failure: restart cannot lose the assessment.
                    record_request(
                        data, self.store, final, "remediation_verifier",
                        "独立业务验证明确失败，评估安全补偿。",
                        "verification:" + verification.verification_id, "verification_failed",
                    )
            # Invocation-specific reference; the persisted verification remains authoritative.
            return action.model_copy(update={"details": action.details | {
                "verification_performed": True, "verification_id": verification.verification_id,
            }})

    def _review_target(
        self,
        proposal_id,
        actor,
        *,
        allow_unregistered_compensation=False,
    ):
        proposal = self.store.get(proposal_id)
        try:
            if proposal.compensation is not None:
                original = self.store.get(proposal.compensation.original_proposal_id)
                contract = contract_for(original)
                if contract is None or contract.digest() != proposal.compensation.contract_digest:
                    if not allow_unregistered_compensation:
                        raise RemediationError("safe_compensation_target_not_registered")
            elif proposal.action_type not in ACTION_REGISTRY:
                raise RemediationError("action_not_allowed")
            else:
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
            verifier = self.verifier
            if proposal.compensation and not self._verifier_override:
                original = self.store.get(proposal.compensation.original_proposal_id)
                contract = contract_for(original)
                if contract is None or contract.digest() != proposal.compensation.contract_digest:
                    return VerificationResult(
                        proposal_id=proposal.proposal_id,
                        success=False,
                        purpose="manual_check",
                        outcome=V.NO_DATA,
                        started_at=started,
                        checks={"observation": {"success": False}},
                        summary="补偿契约不可用，未发起业务探测；仅允许人工收口，历史结果保持未知。",
                    )
                verifier = contract.verifier
            verification = VerificationResult.model_validate(verifier.verify(proposal))
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

    def inspect_execution(
        self,
        proposal_id,
        actor,
        *,
        command_id=None,
        expected_digest=None,
    ):
        """Fresh read-only checks describe current health, never historical action success."""
        actor = actor_name(actor)
        with FileLock(SERVICE_LOCK):
            proposal = self._review_target(
                proposal_id, actor, allow_unregistered_compensation=True,
            )
            if command_id is not None:
                with self.store.transaction() as data:
                    proposal = self.store.from_data(data, proposal_id)
                    if self.store.check_command(
                        data, proposal, "manual_check_requested", actor, command_id, expected_digest,
                    ):
                        return {"proposal": proposal.model_dump(mode="json"), "already_requested": True}
                    self.store.event(data, proposal, "manual_check_requested", actor,
                                     command_id=command_id, proposal_digest=expected_digest)
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
            proposal = self._review_target(
                proposal_id, actor, allow_unregistered_compensation=True,
            )
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
            if proposal.compensation is not None:
                raise RemediationError("compensation_retry_forbidden")
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
