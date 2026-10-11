from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from datetime import datetime, timezone
from typing import Literal
import math
from models.investigation import InvestigationRecord
from models.recovery import RecoveryVerification

class AlertEvent(BaseModel):
    """Normalized event; legacy defaults support old constructors/Store records only."""
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    service: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    alertname: str = "legacy"
    signal_type: Literal["numeric", "state", "event"] = "numeric"
    source: str = "legacy"
    state: Literal["inactive", "pending", "firing", "cleared"] = "firing"
    metric: str | None = None
    value: float | None = None
    threshold: float | None = None
    unit: str | None = None
    direction: Literal["higher_is_worse", "lower_is_worse", "event"] | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    identity_labels: tuple[str, ...] = ()
    starts_at: datetime | None = None
    observed_at: datetime | None = None
    window: str | None = None
    minimum_requests: int | None = None

    @field_validator("value", "threshold", mode="before")
    @classmethod
    def finite_number(cls, value):
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("boolean is not a numeric measurement")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("finite measurement required")
        return number

    @field_validator("starts_at", "observed_at")
    @classmethod
    def aware_utc(cls, value):
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("event timestamp requires timezone")
            return value.astimezone(timezone.utc)
        return value

    @model_validator(mode="after")
    def semantics(self):
        if self.source == "legacy" and self.signal_type == "numeric" and (
            self.metric is None
            or self.value is None
            or self.threshold is None
        ):
            raise ValueError("legacy numeric alert fields are incomplete")
        if self.source != "legacy":
            if self.alertname == "legacy" or not self.alertname.strip():
                raise ValueError("explicit alertname required")
            if self.signal_type in {"numeric", "state"} and (
                self.metric is None
                or self.value is None
                or self.threshold is None
                or self.unit is None
                or self.direction not in {"higher_is_worse", "lower_is_worse"}
            ):
                raise ValueError("numeric/state semantics are incomplete")
            if self.signal_type == "state" and self.value not in {0, 1}:
                raise ValueError("state signal must be 0 or 1")
            if any(not self.labels.get(key) for key in self.identity_labels):
                raise ValueError("required identity label missing")
        return self


Alert = AlertEvent

class Evidence(BaseModel):
    source:str
    summary:str
    detail:str
    collected_at: datetime | None = None
    runtime_session_id: str | None = None
    observation_id: str | None = None

class HistoryArchive(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_ids: list[str] = Field(min_length=1)


class Incident(BaseModel):
    incident_id:str
    fingerprint:str|None=None

    service:str
    severity:str
    status:str

    alerts:list[Alert]

    evidence:list[Evidence]=Field(default_factory=list)

    investigations:list[InvestigationRecord]=Field(
        default_factory=list
    )
    last_investigated_at:datetime|None=None
    last_investigated_value:float|None=None
    last_attempted_at: datetime | None = None
    last_investigated_state: str | None = None
    alert_state: str = "firing"
    cleared_at: datetime | None = None
    recovery_verified_at: datetime | None = None
    recovery_evidence: list[Evidence] = Field(default_factory=list)
    recovery_verification: RecoveryVerification | None = None
    related_alerts: list[dict] = Field(default_factory=list)
    consecutive_investigation_failures: int = Field(default=0, ge=0)
    investigation_suspended_reason: str | None = None
    history_archives: list[HistoryArchive] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def legacy_resolved_state(cls, value):
        if (
            isinstance(value, dict)
            and "alert_state" not in value
            and value.get("status") == "resolved"
        ):
            return {
                **value,
                "alert_state": "cleared",
            }
        return value

    @field_validator(
        "evidence",
        "investigations",
        mode="before"
    )
    @classmethod
    def null_to_emptylist(cls,value):
        if value is None:
            return []
        return value
