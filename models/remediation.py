"""Immutable remediation records; changed records come from the state machine."""
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now():
    return datetime.now(timezone.utc)


class RemediationStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    VERIFICATION_FAILED = "verification_failed"
    CLOSED = "closed"


class ExecutionOutcome(str, Enum):
    NOT_EXECUTED = "not_executed"
    EXECUTED = "executed"
    UNKNOWN = "unknown"


class VerificationOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NO_DATA = "no_data"
    TIMEOUT = "timeout"
    STALE = "stale"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def utc_dates(cls, value):
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timestamps must include a timezone")
            return value.astimezone(timezone.utc)
        return value


class ManualClosure(Record):
    command_id: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    closed_at: datetime = Field(default_factory=utc_now)
    execution_outcome: ExecutionOutcome
    action_ids: list[str]
    verification_id: str


class ServiceIdentity(Record):
    service: str = Field(min_length=1, max_length=200)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    process_started_at: datetime
    configuration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CompensationTarget(Record):
    target_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
    service: str = Field(min_length=1, max_length=200)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    preconditions: str = Field(min_length=1, max_length=1000)
    side_effects: str = Field(min_length=1, max_length=1000)
    verification_policy: str = Field(min_length=1, max_length=1000)
    data_compatibility: str = Field(min_length=1, max_length=1000)


class CompensationLink(Record):
    original_proposal_id: str
    original_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_action_id: str
    original_verification_id: str
    source_identity: ServiceIdentity
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    target: CompensationTarget
    trigger: Literal["verification_failed", "operator_request"]
    command_id: str = Field(min_length=1, max_length=200)


class CompensationAssessment(Record):
    assessment_id: str = Field(default_factory=lambda: "comp-" + uuid4().hex)
    original_proposal_id: str
    trigger: Literal["verification_failed", "operator_request"]
    command_id: str = Field(min_length=1, max_length=200)
    requested_by: str = Field(min_length=1, max_length=200)
    request_reason: str = Field(min_length=1, max_length=1000)
    evaluated_at: datetime = Field(default_factory=utc_now)
    status: Literal["blocked", "approval_required", "already_requested"]
    reason: str = Field(max_length=200)
    compensation_proposal_id: str | None = None
    target: CompensationTarget | None = None


class RemediationProposal(Record):
    proposal_id: str = Field(default_factory=lambda: "rem-" + uuid4().hex)
    incident_id: str = Field(min_length=1)
    service: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=2000)
    action_type: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    risk: Risk
    status: RemediationStatus = RemediationStatus.PENDING
    created_at: datetime = Field(default_factory=utc_now)
    approved_at: datetime | None = None
    approved_by: str | None = None
    rejected_at: datetime | None = None
    rejected_by: str | None = None
    rejection_reason: str | None = None
    executed_at: datetime | None = None
    expected_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_process_started_at: datetime
    evidence_sources: list[str] = Field(default_factory=list)
    approval_digest: str | None = None
    retry_of: str | None = None
    closure: ManualClosure | None = None
    incident_fingerprint: str | None = None
    compensation: CompensationLink | None = None


class ActionResult(Record):
    action_id: str = Field(default_factory=lambda: "act-" + uuid4().hex)
    proposal_id: str
    action_type: str
    started_at: datetime
    finished_at: datetime = Field(default_factory=utc_now)
    success: bool
    status: str
    summary: str = Field(max_length=1000)
    details: dict[str, Any] = Field(default_factory=dict)
    # Old v1 results without an explicit outcome remain unknown.
    execution_outcome: ExecutionOutcome = ExecutionOutcome.UNKNOWN
    resulting_identity: ServiceIdentity | None = None


class VerificationResult(Record):
    verification_id: str = Field(default_factory=lambda: "verify-" + uuid4().hex)
    proposal_id: str
    verified_at: datetime = Field(default_factory=utc_now)
    success: bool
    checks: dict[str, Any]
    summary: str = Field(max_length=1000)
    purpose: Literal["post_action", "manual_check"] = "post_action"
    # Legacy success=False is deliberately not promoted to a business failure.
    outcome: VerificationOutcome = VerificationOutcome.UNKNOWN
    started_at: datetime | None = None
    action_id: str | None = None
    proposal_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observed_identity: ServiceIdentity | None = None


class RemediationAuditEvent(Record):
    event_id: str = Field(default_factory=lambda: "audit-" + uuid4().hex)
    proposal_id: str
    occurred_at: datetime = Field(default_factory=utc_now)
    actor: str = Field(min_length=1, max_length=200)
    event: str
    action_type: str
    details: dict[str, Any] = Field(default_factory=dict)
