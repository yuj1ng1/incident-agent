"""Small HTTP query adapter over public store/manager methods; no SQL here."""
from pathlib import Path
import re

from incident_store import IncidentStore
from investigation.checkpoint import protect
from investigation.manager import InvestigationManager
from investigation.state import RuntimeIntegrityError
from investigation.store import RuntimeStore
from web_api.schemas import (
    AlertSummary, HumanRequestView, IncidentDetail, IncidentPage, IncidentSummary,
    InvestigationAnswer, InvestigationHistory, Projection, ProposalGeneration, SessionDetail, SessionPage,
    SessionSummary,
)

ROOT = Path(__file__).resolve().parents[1]


def public_text(value, limit=200):
    if value is None:
        return None
    # Reuse Runtime credential masking, also covering older stored free text.
    value = protect(value)
    value = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,})\b", "[redacted]", value)
    value = re.sub(r"(?i)([a-z][a-z0-9+.-]*://[^\s/@:]+:)[^\s/@]+(@)", r"\1[redacted]\2", value)
    value = re.sub(
        r'''(?i)((?:password|passwd|api[ _-]?key|token|authorization|secret|pat)["']?\s*[=:]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)''',
        r"\1[redacted]", value,
    )
    return value[:limit]


def answer_view(result):
    text = public_text(result["answer"], limit=65536)
    return InvestigationAnswer(
        success=result.get("success"),
        status=result["status"], answer=text[:8000], answer_truncated=len(text) > 8000,
        model_rounds=result.get("model_rounds", 0), tool_calls=result.get("tool_calls", 0),
    )


def incident_summary(incident):
    return IncidentSummary(
        incident_id=public_text(incident.incident_id, 120),
        service=public_text(incident.service), severity=public_text(incident.severity, 100),
        status=public_text(incident.status, 100), alert_state=public_text(incident.alert_state, 100),
        last_investigated_at=incident.last_investigated_at, cleared_at=incident.cleared_at,
        recovery_verified_at=incident.recovery_verified_at,
        consecutive_investigation_failures=incident.consecutive_investigation_failures,
        investigation_suspended_reason=public_text(incident.investigation_suspended_reason),
    )


def session_summary(session):
    # Owner/fencing/checkpoint identity and arbitrary extra fields stay internal.
    fields = {name: getattr(session, name) for name in SessionSummary.model_fields}
    for name in ("session_id", "incident_id", "service", "last_error_category"):
        fields[name] = public_text(fields[name], 120 if name.endswith("_id") else 200)
    return SessionSummary(**fields)


class RuntimeQueries:
    def __init__(self, incident_path=ROOT / "data" / "incidents.json", runtime_path=ROOT / "data" / "runtime.sqlite3"):
        # Storing paths is inert. Each request opens existing stores read-only.
        self.incident_path = Path(incident_path)
        self.runtime_path = Path(runtime_path)

    def incidents(self, *, limit, offset, status):
        store = IncidentStore(self.incident_path, read_only=True)
        items = store.list_incidents(limit=limit + 1, offset=offset, status=status)
        return IncidentPage(items=[incident_summary(i) for i in items[:limit]],
                            limit=limit, offset=offset, has_more=len(items) > limit)

    def incident(self, incident_id):
        store = IncidentStore(self.incident_path, read_only=True)
        incident = store.get_incident(incident_id, include_history=False)
        if incident is None:
            return None
        if incident.incident_id != incident_id:
            raise ValueError("incident_store_identity_mismatch")
        return IncidentDetail(
            **incident_summary(incident).model_dump(),
            recent_alerts=[AlertSummary(
                alertname=public_text(a.alertname), state=public_text(a.state, 100),
                metric=public_text(a.metric), value=a.value, threshold=a.threshold,
                observed_at=a.observed_at,
            ) for a in incident.alerts[-3:]],
            recent_investigations=[InvestigationHistory(
                **answer_view(r.model_dump()).model_dump(),
                session_id=public_text(r.session_id, 120), investigated_at=r.investigated_at,
            ) for r in incident.investigations[-3:]],
            evidence_count=len(incident.evidence),
            archived_session_count=sum(len(ref.session_ids) for ref in incident.history_archives),
        )

    def sessions(self, *, limit, offset, status, incident_id):
        store = RuntimeStore(self.runtime_path, read_only=True)
        items = store.list_sessions(limit=limit + 1, offset=offset, newest_first=True,
                                    statuses={status} if status else None, incident_id=incident_id)
        return SessionPage(items=[session_summary(s) for s in items[:limit]],
                           limit=limit, offset=offset, has_more=len(items) > limit)

    def session(self, session_id):
        store = RuntimeStore(self.runtime_path, read_only=True)
        # workflow only needs RuntimeStore when proposal contents are excluded.
        manager = InvestigationManager(store, incident_store=None)
        try:
            workflow = manager.workflow(session_id, include_remediations=False)
        except RuntimeIntegrityError as exc:
            if str(exc) == "session_not_found":
                return None
            raise
        if workflow["session"]["session_id"] != session_id:
            raise RuntimeIntegrityError("runtime_session_identity_mismatch")
        from investigation.models import InvestigationSession
        return SessionDetail(
            session=session_summary(InvestigationSession.model_validate(workflow["session"])),
            limits=workflow["limits"], incident_budget_usage=workflow["incident_budget_usage"],
            investigation_result=answer_view(workflow["investigation_result"]) if workflow["investigation_result"] else None,
            projection=Projection(
                projected=workflow["projected"], blocked=workflow["projection_blocked"],
                error_category=public_text(workflow["projection_error_category"]),
                retry_at=workflow["projection_retry_at"],
            ),
            proposal_generation=ProposalGeneration(
                status=workflow["remediation_status"], pending=workflow["remediation_pending"],
                attempts=workflow["remediation_attempts"], blocked=workflow["remediation_blocked"],
                blocked_reason=public_text(workflow["remediation_blocked_reason"]),
                error_category=public_text(workflow["remediation_error_category"]),
                retry_at=workflow["remediation_retry_at"], evidence_gap=public_text(workflow["proposal_evidence_gap"]),
            ),
            human_requests=[HumanRequestView(
                request_id=public_text(r["request_id"], 120), status=r["status"],
                question=public_text(r["question"], 2000), created_at=r["created_at"], expires_at=r["expires_at"],
            ) for r in workflow["human_requests"]],
        )
