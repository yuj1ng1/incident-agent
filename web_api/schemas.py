"""Explicit public fields: no raw checkpoint, tool output or operator messages."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from investigation.models import RuntimeLimits, SessionStatus
from investigation.result_trust import ResultTrust
from knowledge.chunker import DEFAULT_MAX_MARKDOWN_BYTES
from models.investigation import InvestigationStatus
from models.recovery import Reason as RecoveryReason
from web_api.auth import Permission


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Health(PublicModel):
    status: Literal["ok"] = "ok"
    read_only: bool = True


SignalStatus = Literal["ok", "no_data", "error"]
OriginStatus = Literal["demo_replay", "synthetic", "unconfirmed"]
SignalMetric = Literal[
    "http_5xx_rate",
    "http_latency_p95_seconds",
    "process_cpu_cores",
    "process_resident_memory_bytes",
]


class SignalWindow(PublicModel):
    start: datetime
    end: datetime
    step_seconds: int


class MetricSample(PublicModel):
    evaluated_at: datetime
    value: float


class MetricSignal(PublicModel):
    metric: SignalMetric
    status: SignalStatus
    reason: str | None
    samples: list[MetricSample] = Field(max_length=120)
    infos: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(max_length=8)


class LogSample(PublicModel):
    timestamp: str
    line: str


class LogSignal(PublicModel):
    status: SignalStatus
    reason: str | None
    entries: list[LogSample] = Field(max_length=50)
    truncated: bool


class IncidentSignals(PublicModel):
    window: SignalWindow | None
    metrics: list[MetricSignal] = Field(max_length=4)
    logs: LogSignal


class OperatorView(PublicModel):
    actor: str
    permissions: list[Permission]


class RemediationSummary(PublicModel):
    proposal_id: str
    incident_id: str
    service: str
    summary: str
    action_type: str
    risk: Literal["low", "medium", "high"]
    status: Literal["pending", "approved", "rejected", "executing", "succeeded", "failed", "verification_failed", "closed"]
    created_at: datetime
    execution_outcome: Literal["not_executed", "executed", "unknown"]
    original_proposal_id: str | None


class RemediationPage(PublicModel):
    items: list[RemediationSummary] = Field(max_length=100)
    limit: int
    offset: int
    has_more: bool


class CompensationTargetView(PublicModel):
    target_id: str
    service: str
    commit_sha: str
    artifact_sha256: str
    configuration_digest: str
    preconditions: str
    side_effects: str
    verification_policy: str
    data_compatibility: str


class CompensationView(PublicModel):
    status: Literal["blocked", "approval_required", "already_requested"]
    reason: str
    target: CompensationTargetView | None
    compensation_proposal_id: str | None


class ActionView(PublicModel):
    action_id: str
    started_at: datetime
    finished_at: datetime
    success: bool
    status: str
    execution_outcome: Literal["not_executed", "executed", "unknown"]
    reason: str | None = None
    resulting_commit_sha: str | None = None
    resulting_configuration_digest: str | None = None
    release_id: str | None = None
    pid: int | None = None


class VerificationCheckView(PublicModel):
    name: str
    success: bool | None
    http_status: int | None
    reason: str | None = None
    release_id: str | None = None
    artifact_sha256: str | None = None
    pid: int | None = None


class DemoRecoveryView(PublicModel):
    environment: Literal["isolated_real_demo"] = "isolated_real_demo"
    artifact_sha256: str
    source_commit: str
    target_commit: str
    release_id: str
    source_release_id: str | None = None
    instance_id: str
    configuration_digest: str
    configuration_summary: str


class CompensationEvidenceView(PublicModel):
    original_proposal_id: str
    original_summary: str
    original_action_type: str
    original_digest: str
    verification_id: str | None
    verified_at: datetime | None
    expires_at: datetime | None
    checked_at: datetime
    max_age_seconds: int
    status: Literal["bound", "legacy", "stale", "mismatch", "manual_observation", "missing"]
    outcome: Literal["passed", "failed", "no_data", "timeout", "stale", "mismatch", "unknown"]
    valid: bool


class VerificationView(PublicModel):
    verification_id: str
    verified_at: datetime
    purpose: Literal["post_action", "manual_check"]
    outcome: Literal["passed", "failed", "no_data", "timeout", "stale", "mismatch", "unknown"]
    historical_success: bool
    evidence_status: Literal["bound", "legacy", "stale", "mismatch", "manual_observation"]
    action_id: str | None
    checks: list[VerificationCheckView]


class AuditView(PublicModel):
    event_id: str
    occurred_at: datetime
    event: str
    actor: str
    reason: str | None
    proposal_digest: str | None
    command_id: str | None
    authorization_result: Literal["approved", "rejected"] | None


class CompensationRequestView(CompensationView):
    assessment_id: str
    command_id: str
    requested_by: str
    request_reason: str
    evaluated_at: datetime
    trigger: Literal["verification_failed", "operator_request"]


class RemediationDetail(RemediationSummary):
    reason: str
    parameters: dict[str, str]
    hidden_parameter_count: int
    proposal_digest: str
    expected_commit_sha: str
    approved_by: str | None
    approved_at: datetime | None
    approval_valid: bool
    rejected_by: str | None
    rejected_at: datetime | None
    rejection_reason: str | None
    closure_reason: str | None
    compensation: CompensationView
    target: CompensationTargetView | None
    related_proposals: list[RemediationSummary]
    actions: list[ActionView]
    verifications: list[VerificationView]
    requests: list[CompensationRequestView]
    audit: list[AuditView]
    demo_recovery: DemoRecoveryView | None = None
    source_evidence: CompensationEvidenceView | None
    requested_by: str | None


class RemediationCommand(PublicModel):
    command_id: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9_.:-]+$")
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CompensationRequest(RemediationCommand):
    reason: str = Field(min_length=1, max_length=1000)
    target_id: str | None = Field(default=None, max_length=80)


class ReasonCommand(RemediationCommand):
    reason: str = Field(min_length=1, max_length=1000)


class InvestigationAnswer(PublicModel):
    success: bool | None
    status: InvestigationStatus
    answer: str = Field(max_length=8000)
    answer_truncated: bool
    model_rounds: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    result_trust: ResultTrust
    origin_status: OriginStatus = "unconfirmed"
    evidence_origin: str | None = Field(max_length=120)
    origin_note: str | None = Field(max_length=200)


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
    origin_status: OriginStatus = "unconfirmed"
    evidence_origin: str | None = Field(max_length=120)
    origin_note: str | None = Field(max_length=200)


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


class RunbookCitationView(PublicModel):
    document_id: str = Field(min_length=1, max_length=120)
    version_id: str = Field(min_length=1, max_length=120)
    chunk_id: str = Field(min_length=1, max_length=120)
    service: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    heading_path: str = Field(max_length=500)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    citation: str = Field(min_length=1, max_length=300)
    snippet: str | None = Field(max_length=600)
    score: float | None


class SessionDetail(PublicModel):
    session: SessionSummary
    limits: RuntimeLimits | None
    limits_source: Literal["session_checkpoint", "unrecorded"]
    incident_budget_usage: BudgetUsage
    origin_status: OriginStatus = "unconfirmed"
    evidence_origin: str | None = Field(max_length=120)
    origin_note: str | None = Field(max_length=200)
    investigation_result: InvestigationAnswer | None
    projection: Projection
    proposal_generation: ProposalGeneration
    human_requests: list[HumanRequestView] = Field(max_length=10)
    runbook_citations: list[RunbookCitationView] = Field(
        default=[],
        max_length=20,
    )


KnowledgeVersionStatus = Literal["draft", "published", "archived"]
KnowledgePublishStatus = Literal["published", "unpublished"]
KnowledgeSearchMethod = Literal["hybrid_rrf", "mysql_ngram", "vector_cosine"]
KnowledgeSearchStatus = Literal["ok", "no_match", "unavailable"]


def _reject_path_traversal_id(value: str | None) -> str | None:
    if value is None:
        return None
    if value in {".", ".."} or ".." in value:
        raise ValueError("invalid_identifier")
    return value


class KnowledgeVersionSummary(PublicModel):
    version_id: str = Field(min_length=1, max_length=120)
    status: KnowledgeVersionStatus
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class KnowledgeDocumentSummary(PublicModel):
    document_id: str = Field(min_length=1, max_length=120)
    service: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    source: str = Field(min_length=1, max_length=300)
    published_version_id: str | None = Field(max_length=120)
    published_status: KnowledgePublishStatus
    version_count: int = Field(ge=0)
    versions: list[KnowledgeVersionSummary] = Field(max_length=100)
    created_at: datetime
    updated_at: datetime


class KnowledgeDocumentPage(PublicModel):
    items: list[KnowledgeDocumentSummary] = Field(max_length=100)
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0, le=10000)
    has_more: bool


class KnowledgeChunkItem(PublicModel):
    chunk_id: str = Field(min_length=1, max_length=120)
    ordinal: int = Field(ge=0)
    heading_path: str = Field(max_length=500)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    chunk_text: str
    embedding_model: str | None = Field(max_length=120)
    embedding_dim: int | None = Field(ge=1)


class KnowledgeVersionDetail(PublicModel):
    document_id: str = Field(min_length=1, max_length=120)
    service: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    source: str = Field(min_length=1, max_length=300)
    published_version_id: str | None = Field(max_length=120)
    version_id: str = Field(min_length=1, max_length=120)
    status: KnowledgeVersionStatus
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    raw_markdown: str
    chunks: list[KnowledgeChunkItem]


class KnowledgeImportResponse(PublicModel):
    document: KnowledgeDocumentSummary
    version: KnowledgeVersionSummary
    chunk_count: int = Field(ge=1)
    idempotent: bool


class KnowledgeSearchHit(PublicModel):
    document_id: str = Field(min_length=1, max_length=120)
    version_id: str = Field(min_length=1, max_length=120)
    chunk_id: str = Field(min_length=1, max_length=120)
    service: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    source: str = Field(min_length=1, max_length=300)
    heading_path: str = Field(max_length=500)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    snippet: str = Field(max_length=1200)
    score: float
    lexical_rank: int | None = Field(ge=1)
    vector_rank: int | None = Field(ge=1)
    exact_match: bool
    citation: str = Field(min_length=1, max_length=300)


class KnowledgeSearchResponse(PublicModel):
    status: KnowledgeSearchStatus
    service: str = Field(min_length=1, max_length=200)
    query: str = Field(max_length=2000)
    method: KnowledgeSearchMethod
    hits: list[KnowledgeSearchHit] = Field(max_length=20)
    candidate_count: int = Field(ge=0)
    reason: str | None = Field(max_length=200)


class KnowledgeDocumentImportRequest(PublicModel):
    service: str = Field(min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=300)
    source: str = Field(min_length=1, max_length=300)
    raw_markdown: str = Field(
        min_length=1,
        max_length=DEFAULT_MAX_MARKDOWN_BYTES,
    )
    document_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=r"^[a-zA-Z0-9_.:-]+$",
    )
    publish: bool = True

    @field_validator("document_id", mode="after")
    @classmethod
    def _validate_document_id(cls, value: str | None) -> str | None:
        return _reject_path_traversal_id(value)


class KnowledgeVersionCreateRequest(PublicModel):
    raw_markdown: str | None = Field(
        default=None,
        min_length=1,
        max_length=DEFAULT_MAX_MARKDOWN_BYTES,
    )
    title: str | None = Field(default=None, max_length=300)
    source: str | None = Field(default=None, min_length=1, max_length=300)
    publish: bool = True
    publish_version_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=r"^[a-zA-Z0-9_.:-]+$",
    )

    @field_validator("publish_version_id", mode="after")
    @classmethod
    def _validate_publish_version_id(cls, value: str | None) -> str | None:
        return _reject_path_traversal_id(value)

    @model_validator(mode="after")
    def _validate_exclusive_mode(self):
        has_markdown = self.raw_markdown is not None
        has_publish_id = self.publish_version_id is not None
        if has_markdown == has_publish_id:
            raise ValueError("specify_either_raw_markdown_or_publish_version_id")
        return self


class KnowledgeSearchRequest(PublicModel):
    service: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=20)
    method: KnowledgeSearchMethod = "hybrid_rrf"
