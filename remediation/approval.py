"""Human decisions, independent of the Agent ToolManager."""
import hashlib
import json

from models.remediation import RemediationStatus, utc_now
from remediation.state import RemediationError, transition


def actor_name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise RemediationError("human_identity_required")
    if any(ord(c) < 32 for c in value):
        raise RemediationError("invalid_human_identity")
    return value


def approval_digest(proposal):
    payload = proposal.model_dump(mode="json", include={
        "proposal_id", "incident_id", "service", "action_type", "parameters",
        "summary", "reason", "risk", "expected_commit_sha", "expected_process_started_at",
    })
    # Preserve v1 approval hashes; new retry lineage is covered when present.
    if proposal.retry_of is not None:
        payload["retry_of"] = proposal.retry_of
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def approve_proposal(proposal, approved_by):
    actor = actor_name(approved_by)
    return transition(proposal, RemediationStatus.APPROVED, approved_by=actor,
                      approved_at=utc_now(), approval_digest=approval_digest(proposal))


def reject_proposal(proposal, rejected_by, reason):
    actor = actor_name(rejected_by)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise RemediationError("rejection_reason_required")
    return transition(proposal, RemediationStatus.REJECTED, rejected_by=actor,
                      rejected_at=utc_now(), rejection_reason=reason)
