"""Explicit public fields: no raw checkpoint, tool output or operator messages."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from investigation.models import RuntimeLimits, SessionStatus
from investigation.result_trust import ResultTrust
from models.investigation import InvestigationStatus
from models.recovery import Reason as RecoveryReason


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Health(PublicModel):
    status: Literal["ok"] = "ok"
    read_only: Literal[True] = True


class InvestigationAnswer(PublicModel):
    success: bool | None
    status: InvestigationStatus
    answer: str = Field(max_length=8000)
    answer_truncated: bool
    model_rounds: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    result_trust: ResultTrust


class InvestigationHistory(InvestigationAnswer):
    session_id: str | None = Field(max_length=120)
    investigated_at: datetime


class IncidentSummary(PublicModel):
    incident_id: str = Field(max_length=120)
    service: str = Field(max_length=200)
    severity: str = Field(max_length=100)
    status: str = Field(max_length=100)
    alert_state: str = Field(max_length=100)
    last_investigated_at: datetime | None
    cleared_at: datetime | None
    recovery_verified_at: datetime | None
    consecutive_investigation_failures: int = Field(ge=0)
    investigation_suspended_reason: str | None = Field(max_length=200)


class AlertSummary(PublicModel):
    alertname: str = Field(max_length=200)
    state: str = Field(max_length=100)
    metric: str | None = Field(max_length=200)
    value: float | None
    threshold: float | None
    observed_at: datetime | None


class RecoveryCheckView(PublicModel):
    kind: Literal["http_status", "json_equals", "nonempty_object"]
    field: str = Field(max_length=80)
    matched: bool = Field(strict=True)


class RecoveryAttemptView(PublicModel):
    number: int = Field(ge=1, le=5)
    started_at: datetime
    completed_at: datetime | None
    outcome: Literal["running", "success", "failed", "insufficient", "abandoned"]
    reason: RecoveryReason | None
    http_status: int | None = Field(ge=100, le=599)
    checks: list[RecoveryCheckView] = Field(max_length=21)


class RecoveryVerificationView(PublicModel):
    status: Literal[
        "alert_firing", "pending", "running", "verified", "failed",
        "insufficient", "superseded", "not_recorded", "legacy_only",
    ]
    target_id: str | None = Field(max_length=120)
    attempt_count: int = Field(ge=0, le=5)
    max_attempts: int | None = Field(ge=1, le=5)
    last_attempt_at: datetime | None
    next_retry_at: datetime | None
    reason: RecoveryReason | None
    last_http_status: int | None = Field(ge=100, le=599)
    verified_at: datetime | None
    attempts: list[RecoveryAttemptView] = Field(max_length=5)


class IncidentDetail(IncidentSummary):
    recovery_verification: RecoveryVerificationView
    recent_alerts: list[AlertSummary] = Field(max_length=3)
    recent_investigations: list[InvestigationHistory] = Field(max_length=3)
    evidence_count: int = Field(ge=0)
    archived_session_count: int = Field(ge=0)
    history_included: Literal[False] = False


class IncidentPage(PublicModel):
    items: list[IncidentSummary] = Field(max_length=100)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0, le=10000)
    has_more: bool


class SessionSummary(PublicModel):
    session_id: str = Field(max_length=120)
    incident_id: str = Field(max_length=120)
    service: str = Field(max_length=200)
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    state_version: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    current_attempt_rounds: int = Field(ge=0)
    current_attempt_tool_calls: int = Field(ge=0)
    total_model_requests: int = Field(ge=0)
    total_tool_executions: int = Field(ge=0)
    resume_count: int = Field(ge=0)
    last_error_category: str | None = Field(max_length=200)
    next_retry_at: datetime | None


class SessionPage(PublicModel):
    items: list[SessionSummary] = Field(max_length=100)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0, le=10000)
    has_more: bool


class BudgetUsage(PublicModel):
    attempts: int = Field(ge=0)
    model_requests: int = Field(ge=0)
    tool_executions: int = Field(ge=0)
    resumes: int = Field(ge=0)
    human_requests: int = Field(ge=0)
    started_at: datetime


class Projection(PublicModel):
    projected: bool
    blocked: bool
    error_category: str | None = Field(max_length=200)
    retry_at: datetime | None


class ProposalGeneration(PublicModel):
    status: Literal["not_pending", "waiting_retry", "blocked"]
    pending: bool
    attempts: int = Field(ge=0)
    blocked: bool
    blocked_reason: str | None = Field(max_length=200)
    error_category: str | None = Field(max_length=200)
    retry_at: datetime | None
    evidence_gap: str | None = Field(max_length=200)


class HumanRequestView(PublicModel):
    request_id: str = Field(max_length=120)
    status: Literal["pending", "answered", "consumed", "expired", "cancelled"]
    question: str = Field(max_length=2000)
    created_at: datetime
    expires_at: datetime


class SessionDetail(PublicModel):
    session: SessionSummary
    limits: RuntimeLimits
    incident_budget_usage: BudgetUsage
    investigation_result: InvestigationAnswer | None
    projection: Projection
    proposal_generation: ProposalGeneration
    human_requests: list[HumanRequestView] = Field(max_length=10)
