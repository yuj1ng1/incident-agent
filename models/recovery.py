"""Bounded recovery receipts and claims; independent of investigation Sessions."""
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now():
    return datetime.now(timezone.utc)


def aware_utc(value):
    if value is not None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recovery_timestamp_requires_timezone")
        return value.astimezone(timezone.utc)
    return value


Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Token = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Reason = Literal[
    "conditions_met", "recovery_capability_not_configured", "clear_time_invalid",
    "http_status_mismatch", "business_condition_mismatch", "redirect_refused",
    "timeout", "connection_failed", "invalid_response", "response_too_large",
    "content_encoding_refused", "invalid_evidence", "lease_expired", "alert_changed",
    "configuration_changed", "internal_probe_error",
]


class RecoveryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class RecoveryCondition(RecoveryModel):
    kind: Literal["http_status", "json_equals", "nonempty_object"]
    field: str = Field(default="", pattern=r"^(?:[a-zA-Z_][a-zA-Z0-9_]{0,79})?$")

    @model_validator(mode="after")
    def field_identity(self):
        if (self.kind == "http_status") != (self.field == ""):
            raise ValueError("recovery_condition_field_invalid")
        return self


class RecoveryCheck(RecoveryCondition):
    matched: bool = Field(strict=True)


class RecoverySample(RecoveryModel):
    collected_at: datetime
    http_status: int | None = Field(default=None, ge=100, le=599, strict=True)
    checks: list[RecoveryCheck] = Field(default_factory=list, max_length=21)
    outcome: Literal["success", "failed", "insufficient"]
    reason: Reason

    _utc = field_validator("collected_at")(aware_utc)

    @model_validator(mode="after")
    def success_conditions(self):
        if self.outcome == "success" and (
            self.reason != "conditions_met" or not self.checks
            or not all(check.matched for check in self.checks)
            or self.http_status is None or not 200 <= self.http_status < 300
        ):
            raise ValueError("successful_recovery_sample_requires_matching_checks")
        return self


class RecoveryReceipt(RecoveryModel):
    schema_version: Literal[1] = 1
    probe_type: Literal["http_business"] = "http_business"
    incident_id: Identifier
    service: Identifier
    target_id: Identifier
    config_digest: Digest
    claim_token: Token
    cleared_at: datetime
    started_at: datetime
    completed_at: datetime
    outcome: Literal["success", "failed", "insufficient"]
    reason: Reason
    samples: list[RecoverySample] = Field(min_length=1, max_length=5)

    _utc = field_validator("cleared_at", "started_at", "completed_at")(aware_utc)

    @model_validator(mode="after")
    def timed_observations(self):
        if (self.started_at < self.cleared_at or self.completed_at < self.started_at
                or any(not self.started_at <= item.collected_at <= self.completed_at for item in self.samples)):
            raise ValueError("recovery_observation_time_invalid")
        if self.outcome == "success" and (
            self.reason != "conditions_met" or any(item.outcome != "success" for item in self.samples)
        ):
            raise ValueError("successful_recovery_receipt_requires_successful_samples")
        if self.outcome != self.samples[-1].outcome or self.reason != self.samples[-1].reason:
            raise ValueError("recovery_receipt_outcome_mismatch")
        return self


class RecoveryPolicy(RecoveryModel):
    target_id: Identifier
    config_digest: Digest
    expected_status: int = Field(ge=200, le=299, strict=True)
    required_checks: list[RecoveryCondition] = Field(min_length=2, max_length=21)
    successes_required: int = Field(ge=1, le=5, strict=True)
    max_attempts: int = Field(ge=1, le=5, strict=True)
    retry_seconds: int = Field(ge=1, le=300, strict=True)
    evidence_max_age_seconds: int = Field(ge=1, le=300, strict=True)
    lease_seconds: int = Field(ge=1, le=120, strict=True)

    @model_validator(mode="after")
    def unique_required_checks(self):
        checks = [(item.kind, item.field) for item in self.required_checks]
        if (len(checks) != len(set(checks)) or checks[0] != ("http_status", "")
                or ("json_equals", "service") not in checks):
            raise ValueError("recovery_policy_requires_unique_status_and_service_checks")
        return self


class RecoveryAttempt(RecoveryModel):
    token: Token
    started_at: datetime
    lease_until: datetime
    completed_at: datetime | None = None
    outcome: Literal["running", "success", "failed", "insufficient", "abandoned"] = "running"
    reason: Reason | None = None
    receipt: RecoveryReceipt | None = None

    _utc = field_validator("started_at", "lease_until", "completed_at")(aware_utc)

    @model_validator(mode="after")
    def coherent_attempt(self):
        if self.lease_until <= self.started_at:
            raise ValueError("recovery_lease_invalid")
        if self.outcome == "running":
            if self.completed_at is not None or self.reason is not None or self.receipt is not None:
                raise ValueError("running_recovery_attempt_has_completion")
        elif self.completed_at is None or self.completed_at < self.started_at or self.reason is None:
            raise ValueError("recovery_attempt_completion_missing")
        if self.outcome == "success" and (self.reason != "conditions_met" or self.receipt is not None):
            raise ValueError("successful_receipt_belongs_in_recovery_evidence")
        return self


class RecoveryVerification(RecoveryModel):
    schema_version: Literal[1] = 1
    cleared_at: datetime | None
    status: Literal["pending", "running", "verified", "failed", "insufficient", "superseded"] = "pending"
    policy: RecoveryPolicy | None = None
    attempts: list[RecoveryAttempt] = Field(default_factory=list, max_length=5)
    next_retry_at: datetime | None = None
    reason: Reason | None = None

    _utc = field_validator("cleared_at", "next_retry_at")(aware_utc)

    @model_validator(mode="after")
    def coherent_claim(self):
        if self.attempts and (self.policy is None or len(self.attempts) > self.policy.max_attempts):
            raise ValueError("recovery_attempt_budget_invalid")
        if self.status == "running" and (
            self.cleared_at is None or not self.attempts or self.attempts[-1].outcome != "running"
        ):
            raise ValueError("running_recovery_claim_missing")
        if self.status != "running" and any(item.outcome == "running" for item in self.attempts):
            raise ValueError("inactive_recovery_claim_has_running_attempt")
        if any(item.outcome == "running" for item in self.attempts[:-1]):
            raise ValueError("recovery_has_multiple_running_attempts")
        if self.status == "verified" and (
            not self.attempts or self.attempts[-1].outcome != "success" or self.next_retry_at is not None
        ):
            raise ValueError("verified_recovery_claim_invalid")
        if self.next_retry_at is not None and (self.status not in {"failed", "insufficient"}
                or not self.attempts or len(self.attempts) >= self.policy.max_attempts):
            raise ValueError("recovery_retry_without_budget")
        return self
