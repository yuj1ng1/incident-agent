"""Validated runtime records. Incident and remediation JSON remain authoritative."""
from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now():
    return datetime.now(timezone.utc)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, hide_input_in_errors=True)

    @field_validator("*", mode="after")
    @classmethod
    def utc_dates(cls, value):
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timezone_required")
            return value.astimezone(timezone.utc)
        return value


class SessionStatus(str, Enum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_RETRY = "WAITING_RETRY"
    WAITING_HUMAN = "WAITING_HUMAN"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    EXHAUSTED = "EXHAUSTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class InvestigationSession(Record):
    session_id: str = Field(default_factory=lambda: "ses-" + uuid4().hex)
    incident_id: str = Field(min_length=1)
    alert_fingerprint: str = Field(min_length=1)
    service: str = Field(min_length=1)
    # None keeps old v1 sessions in the original Incident budget. Only a persisted
    # human reopen command may grant a new group, identified by its first Session.
    budget_group_id: str | None = None
    status: SessionStatus = SessionStatus.CREATED
    priority: Literal[0, 1] = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    queued_at: datetime = Field(default_factory=utc_now)
    attempt_count: int = Field(default=0, ge=0)
    current_attempt_rounds: int = Field(default=0, ge=0)
    current_attempt_tool_calls: int = Field(default=0, ge=0)
    total_model_requests: int = Field(default=0, ge=0)
    total_tool_executions: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    resume_count: int = Field(default=0, ge=0)
    human_request_count: int = Field(default=0, ge=0)
    checkpoint_id: str | None = None
    last_error_category: str | None = None
    next_retry_at: datetime | None = None
    completed_at: datetime | None = None
    state_version: int = Field(default=0, ge=0)
    owner_token: str | None = None
    fencing_token: int = Field(default=0, ge=0)
    lease_until: datetime | None = None


class RuntimeLimits(Record):
    max_attempts: int = Field(default=3, ge=1, le=10)
    max_model_requests: int = Field(default=21, ge=1, le=100)
    max_tool_executions: int = Field(default=39, ge=1, le=200)
    max_resumes: int = Field(default=8, ge=0, le=30)
    max_duration_seconds: int = Field(default=3600, ge=1)
    lease_seconds: int = Field(default=120, ge=1)
    model_timeout_seconds: int = Field(default=60, ge=1, le=90)
    retry_base_seconds: int = Field(default=5, ge=1)
    max_human_requests: int = Field(default=3, ge=0, le=10)
    human_timeout_seconds: int = Field(default=1800, ge=1)
    starvation_seconds: int = Field(default=120, ge=1)
    checkpoints_to_keep: int = Field(default=4, ge=1, le=20)


class HumanCommand(Record):
    command_id: str = Field(default_factory=lambda: "cmd-" + uuid4().hex, min_length=1, max_length=120)
    session_id: str = Field(min_length=1)
    actor: str = Field(min_length=1, max_length=100)
    command_type: Literal["message", "pause", "resume", "cancel", "answer", "reopen"]
    payload: dict = Field(default_factory=dict)
    expected_version: int | None = Field(default=None, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    processed_at: datetime | None = None
    processing_status: Literal["pending", "applied", "rejected"] = "pending"
    result: str | None = None

    @field_validator("actor")
    @classmethod
    def operator_identity(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("operator_identity_required")
        return value


class HumanRequest(Record):
    request_id: str = Field(default_factory=lambda: "req-" + uuid4().hex)
    session_id: str
    question: str = Field(min_length=1, max_length=2000)
    request_type: Literal["clarification"] = "clarification"
    status: Literal["pending", "answered", "consumed", "expired", "cancelled"] = "pending"
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    answer_command_id: str | None = None


class ToolObservation(Record):
    observation_id: str
    name: str
    arguments: dict
    call_id: str
    result: dict
    success: bool
    collected_at: datetime
    identity: dict | None = None
    cache_key: str


class Checkpoint(Record):
    schema_version: Literal[1] = 1
    phase: Literal["ready", "model_started", "tool_started", "response_saved", "tool_saved", "waiting_human"]
    records: list[ToolObservation] = Field(default_factory=list)
    messages: list[dict] = Field(default_factory=list)
    trace: list[dict] = Field(default_factory=list)
    history: list[dict] = Field(default_factory=list)
    pending: list[dict] = Field(default_factory=list)
    identity: dict | None = None
    budget: dict[str, int] = Field(default_factory=dict)
    new_attempt: bool = False
    response_status: str | None = None
    result: dict | None = None
    investigated_value: float | None = None
    investigated_state: str | None = None
    projected: bool = False
    projection_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    projection_blocked: bool = False
    remediation_pending: bool = False
    remediation_retry_at: datetime | None = None
    proposal_evidence_gap: str | None = None
    projection_attempts: int = Field(default=0, ge=0)
    projection_retry_at: datetime | None = None
    projection_error_category: str | None = None
    reopen_alert: dict | None = None
