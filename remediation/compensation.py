"""Deterministic compensation admission; no model, discovery, or implicit inverse.

The production registry is intentionally empty. A deployment must supply a
reviewed, immutable contract with a precise artifact, bounded identity probe,
guarded handler and independent verifier before any compensation can execute.
"""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from types import MappingProxyType
from typing import Callable

from models.remediation import (
    ActionResult,
    CompensationAssessment,
    CompensationLink,
    CompensationTarget,
    ExecutionOutcome,
    RemediationProposal,
    RemediationStatus as S,
    Risk,
    ServiceIdentity,
    VerificationOutcome as V,
    VerificationResult,
    utc_now,
)
from remediation.approval import actor_name, approval_digest
from remediation.state import RemediationError


@dataclass(frozen=True)
class CompensationContract:
    contract_id: str
    original_action_type: str
    action_type: str
    target: CompensationTarget
    parameters: MappingProxyType
    risk: Risk
    inspect_identity: Callable
    handler: Callable
    verifier: object
    evidence_max_age_seconds: int = 300

    def digest(self):
        body = {
            "contract_id": self.contract_id,
            "original_action_type": self.original_action_type,
            "action_type": self.action_type,
            "target": self.target.model_dump(mode="json"),
            "parameters": dict(self.parameters),
            "risk": self.risk.value,
            "evidence_max_age_seconds": self.evidence_max_age_seconds,
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


# No reverse of disable_demo_regression: re-enabling a fault is unsafe.
COMPENSATION_REGISTRY = MappingProxyType({})


def contract_for(proposal):
    return COMPENSATION_REGISTRY.get((proposal.service, proposal.action_type))


def _source_facts(
    data,
    original,
    trigger,
    *,
    ignore_proposal_id=None,
):
    """Only persisted, bound, explicit business observations qualify."""
    if original.compensation is not None:
        raise RemediationError("compensation_recursion_forbidden")
    if original.status == S.EXECUTING:
        raise RemediationError("original_execution_unknown")
    if original.status == S.FAILED:
        actions = [ActionResult.model_validate(a) for a in data["actions"]
                   if a["proposal_id"] == original.proposal_id]
        if not actions or actions[-1].execution_outcome == ExecutionOutcome.UNKNOWN:
            raise RemediationError("original_execution_unknown")
        if actions[-1].execution_outcome == ExecutionOutcome.NOT_EXECUTED:
            raise RemediationError("original_not_executed")
        raise RemediationError("original_action_failed")
    if original.status not in {S.SUCCEEDED, S.VERIFICATION_FAILED}:
        if original.status in {S.PENDING, S.APPROVED, S.REJECTED}:
            raise RemediationError("original_not_executed")
        raise RemediationError("original_state_not_eligible")
    actions = [ActionResult.model_validate(a) for a in data["actions"]
               if a["proposal_id"] == original.proposal_id]
    if len(actions) != 1 or actions[0].execution_outcome == ExecutionOutcome.UNKNOWN:
        raise RemediationError("original_execution_unknown")
    action = actions[0]
    if action.execution_outcome == ExecutionOutcome.NOT_EXECUTED:
        raise RemediationError("original_not_executed")
    if not action.success:
        raise RemediationError("original_action_failed")
    if (not original.approved_by or not original.approved_at
            or original.approval_digest != approval_digest(original)
            or action.action_type != original.action_type
            or original.executed_at is None
            or not original.executed_at <= action.started_at <= action.finished_at):
        raise RemediationError("original_binding_mismatch")
    results = [VerificationResult.model_validate(v) for v in data["verifications"]
               if v["proposal_id"] == original.proposal_id
               and v.get("purpose", "post_action") == "post_action"]
    if len(results) != 1:
        raise RemediationError("verification_not_confirmed")
    verification = results[0]
    if verification.outcome not in {V.FAILED, V.PASSED}:
        raise RemediationError("verification_" + verification.outcome.value)
    if trigger == "verification_failed" and (
        original.status != S.VERIFICATION_FAILED
        or verification.outcome != V.FAILED or verification.success
    ):
        raise RemediationError("explicit_business_failure_required")
    if verification.success != (verification.outcome == V.PASSED):
        raise RemediationError("verification_binding_mismatch")
    if (original.status == S.SUCCEEDED) != (verification.outcome == V.PASSED):
        raise RemediationError("verification_binding_mismatch")
    identity = verification.observed_identity
    if (verification.action_id != action.action_id
            or verification.proposal_digest != approval_digest(original)
            or verification.started_at is None
            or not action.finished_at <= verification.started_at <= verification.verified_at
            or identity is None or identity != action.resulting_identity
            or identity.service != original.service
            or identity.commit_sha != original.expected_commit_sha):
        raise RemediationError("verification_binding_mismatch")
    for item in data["proposals"].values():
        if item["proposal_id"] in {original.proposal_id, ignore_proposal_id}:
            continue
        if item["service"] != original.service:
            continue
        if item.get("executed_at") and item["executed_at"] > original.model_dump(mode="json")["executed_at"]:
            raise RemediationError("conflicting_operation")
        if item["status"] in {S.PENDING.value, S.APPROVED.value, S.EXECUTING.value}:
            raise RemediationError("conflicting_operation")
        if item["status"] in {S.FAILED.value, S.CLOSED.value}:
            other_actions = [a for a in data["actions"]
                             if a["proposal_id"] == item["proposal_id"]]
            if not other_actions or other_actions[-1].get("execution_outcome", "unknown") == "unknown":
                raise RemediationError("conflicting_unknown_operation")
    return action, verification, identity


def eligible_contract(
    data,
    original,
    trigger,
    *,
    ignore_proposal_id=None,
):
    action, verification, identity = _source_facts(
        data, original, trigger, ignore_proposal_id=ignore_proposal_id,
    )
    contract = contract_for(original)
    if contract is None:
        raise RemediationError("safe_compensation_target_not_registered")
    if (contract.target.service != original.service
            or contract.original_action_type != original.action_type
            or not contract.contract_id or not contract.action_type
            or not 1 <= contract.evidence_max_age_seconds <= 300
            or not callable(contract.inspect_identity) or not callable(contract.handler)
            or not callable(getattr(contract.verifier, "verify", None))):
        raise RemediationError("compensation_contract_invalid")
    age = utc_now() - verification.verified_at
    if not timedelta(0) <= age <= timedelta(seconds=contract.evidence_max_age_seconds):
        raise RemediationError("verification_stale")
    return contract, action, verification, identity


def evaluate(
    data,
    original,
    trigger="operator_request",
):
    """Read-only preview; never performs a probe or records a request."""
    children = [p for p in data["proposals"].values()
                if (p.get("compensation") or {}).get("original_proposal_id") == original.proposal_id]
    if children:
        return ("already_requested", "compensation_already_exists",
                CompensationTarget.model_validate(children[0]["compensation"]["target"]),
                children[0]["proposal_id"])
    try:
        contract, _, _, _ = eligible_contract(data, original, trigger)
        return "approval_required", "independent_approval_required", contract.target, None
    except RemediationError as exc:
        return "blocked", str(exc), None, None


def record_request(
    data,
    store,
    original,
    actor,
    reason,
    command_id,
    trigger,
):
    """Called within the existing Store transaction, including after verification."""
    actor = actor_name(actor)
    command_id = actor_name(command_id)
    if trigger not in {"verification_failed", "operator_request"}:
        raise RemediationError("invalid_compensation_trigger")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise RemediationError("compensation_reason_required")
    for event in data["audit"]:
        old = event["details"].get("assessment")
        if event["details"].get("command_id") == command_id:
            raise RemediationError("command_id_conflict")
        if event["event"] == "compensation_evaluated" and old and old["command_id"] == command_id:
            assessment = CompensationAssessment.model_validate(old)
            if (assessment.original_proposal_id, assessment.requested_by,
                    assessment.request_reason, assessment.trigger) != (
                    original.proposal_id, actor, reason, trigger):
                raise RemediationError("command_id_conflict")
            return assessment
    status, denial, target, child_id = evaluate(data, original, trigger)
    if status == "approval_required":
        contract, action, verification, identity = eligible_contract(data, original, trigger)
        link = CompensationLink(
            original_proposal_id=original.proposal_id,
            original_digest=approval_digest(original),
            original_action_id=action.action_id,
            original_verification_id=verification.verification_id,
            source_identity=identity,
            contract_digest=contract.digest(),
            target=contract.target,
            trigger=trigger,
            command_id=command_id,
        )
        child = RemediationProposal(
            incident_id=original.incident_id,
            incident_fingerprint=original.incident_fingerprint,
            service=original.service,
            summary="补偿申请：" + contract.target.target_id,
            reason=reason,
            action_type=contract.action_type,
            parameters=dict(contract.parameters),
            risk=contract.risk,
            expected_commit_sha=identity.commit_sha,
            expected_process_started_at=identity.process_started_at,
            compensation=link,
        )
        child_id = child.proposal_id
        store.put(data, child)
        store.event(data, child, "proposal_created", actor, original_proposal_id=original.proposal_id)
    assessment = CompensationAssessment(
        original_proposal_id=original.proposal_id,
        trigger=trigger,
        command_id=command_id,
        requested_by=actor,
        request_reason=reason,
        status=status,
        reason=denial,
        compensation_proposal_id=child_id,
        target=target,
    )
    store.event(data, original, "compensation_evaluated", actor,
                assessment=assessment.model_dump(mode="json"))
    return assessment


def execution_contract(data, proposal):
    link = proposal.compensation
    if link.original_proposal_id not in data["proposals"]:
        raise RemediationError("original_proposal_not_found")
    original = RemediationProposal.model_validate(data["proposals"][link.original_proposal_id])
    contract, action, verification, identity = eligible_contract(
        data, original, link.trigger, ignore_proposal_id=proposal.proposal_id,
    )
    if (link.original_digest != approval_digest(original)
            or link.original_action_id != action.action_id
            or link.original_verification_id != verification.verification_id
            or link.source_identity != identity or link.contract_digest != contract.digest()
            or link.target != contract.target or proposal.service != original.service
            or proposal.incident_id != original.incident_id
            or proposal.incident_fingerprint != original.incident_fingerprint
            or proposal.action_type != contract.action_type
            or proposal.parameters != dict(contract.parameters) or proposal.risk != contract.risk
            or proposal.expected_commit_sha != identity.commit_sha
            or proposal.expected_process_started_at != identity.process_started_at):
        raise RemediationError("compensation_binding_mismatch")
    return contract
