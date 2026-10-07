"""Immutable remediation records; changed records come from the state machine."""
from datetime import datetime, timezone
from enum import Enum
from typing import Any
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


class VerificationResult(Record):
    proposal_id: str
    verified_at: datetime = Field(default_factory=utc_now)
    success: bool
    checks: dict[str, Any]
    summary: str = Field(max_length=1000)


class RemediationAuditEvent(Record):
    event_id: str = Field(default_factory=lambda: "audit-" + uuid4().hex)
    proposal_id: str
    occurred_at: datetime = Field(default_factory=utc_now)
    actor: str = Field(min_length=1, max_length=200)
    event: str
    action_type: str
    details: dict[str, Any] = Field(default_factory=dict)
