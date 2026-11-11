"""Allowlisted projections of stored Remediation facts, without probes or writes."""
import re
from datetime import timedelta

from models.remediation import ActionResult, CompensationAssessment, VerificationResult, utc_now
from remediation.approval import approval_digest
from remediation.compensation import compensation_requester, contract_for, evaluate
from remediation.state import RemediationError
from web_api.queries import public_text
from web_api.schemas import (
    ActionView, AuditView, CompensationEvidenceView, CompensationRequestView, CompensationTargetView,
    CompensationView, DemoRecoveryView, RemediationDetail, RemediationSummary,
    VerificationCheckView, VerificationView,
)


def safe_text(value, limit=1000):
    if value is None:
        return None
    # Full URLs may carry secrets in paths as well as queries/userinfo.
    value = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>]+", "[url omitted]", str(value))
    value = re.sub(
        r"(?i)(?<!\w)[a-z]:[\\/][^\s<>\"']+|\\\\[^\s<>\"']+"
        r"|(?<![\w/])/(?:[^/\s<>]+/)+[^\s<>]*",
        "[local path omitted]", value,
    )
    return public_text(value, limit)


def target_view(target):
    if target is None:
        return None
    return CompensationTargetView(**{
        key: safe_text(value) for key, value in target.model_dump(mode="json").items()
    })


def evidence_status(
    verification,
    proposal,
    actions,
):
    if verification.outcome.value == "unknown":
        return "legacy"
    if verification.verified_at > utc_now():
        return "mismatch"
    if verification.purpose == "manual_check":
        return "stale" if utc_now() - verification.verified_at > timedelta(seconds=300) else "manual_observation"
    action = next((ActionResult.model_validate(a) for a in actions
                   if a["action_id"] == verification.action_id), None)
    if (action is None or verification.proposal_digest != approval_digest(proposal)
            or verification.started_at is None
            or not action.finished_at <= verification.started_at <= verification.verified_at
            or verification.observed_identity is None
            or verification.observed_identity.service != proposal.service
            or verification.observed_identity != action.resulting_identity):
        return "mismatch"
    return "stale" if utc_now() - verification.verified_at > timedelta(seconds=300) else "bound"


def summary_view(
    store,
    data,
    proposal,
):
    return RemediationSummary(
        proposal_id=safe_text(proposal.proposal_id, 120),
        incident_id=safe_text(proposal.incident_id, 120),
        service=safe_text(proposal.service, 200),
        summary=safe_text(proposal.summary),
        action_type=safe_text(proposal.action_type, 200),
        risk=proposal.risk,
        status=proposal.status,
        created_at=proposal.created_at,
        execution_outcome=store.execution_outcome(data, proposal),
        original_proposal_id=safe_text(proposal.compensation.original_proposal_id, 120)
        if proposal.compensation else None,
    )


def source_evidence_view(store, data, proposal):
    original = (store.from_data(data, proposal.compensation.original_proposal_id)
                if proposal.compensation else proposal)
    records = [VerificationResult.model_validate(v) for v in data["verifications"]
               if v["proposal_id"] == original.proposal_id
               and v.get("purpose", "post_action") == "post_action"]
    if not records and not proposal.compensation:
        return None
    verification = records[0] if len(records) == 1 else None
    actions = [a for a in data["actions"] if a["proposal_id"] == original.proposal_id]
    contract = contract_for(original)
    max_age = (contract.evidence_max_age_seconds if contract
               and 1 <= contract.evidence_max_age_seconds <= 300 else 300)
    checked_at = utc_now()
    status = evidence_status(verification, original, actions) if verification else "missing"
    expires_at = (verification.verified_at + timedelta(seconds=max_age)
                  if verification else None)
    if verification and status == "bound" and checked_at > expires_at:
        status = "stale"
    return CompensationEvidenceView(
        original_proposal_id=safe_text(original.proposal_id, 120),
        original_summary=safe_text(original.summary),
        original_action_type=safe_text(original.action_type, 200),
        original_digest=approval_digest(original),
        verification_id=verification.verification_id if verification else None,
        verified_at=verification.verified_at if verification else None,
        expires_at=expires_at, checked_at=checked_at, max_age_seconds=max_age,
        status=status, outcome=verification.outcome.value if verification else "unknown",
        valid=status == "bound" and verification.outcome.value in {"passed", "failed"},
    )


def detail_view(
    store,
    data,
    proposal,
):
    identifier = proposal.proposal_id
    status, reason, target, child_id = evaluate(data, proposal)
    actions = [a for a in data["actions"] if a["proposal_id"] == identifier]
    verifications = [VerificationResult.model_validate(v) for v in data["verifications"]
                     if v["proposal_id"] == identifier]
    events = [a for a in data["audit"] if a["proposal_id"] == identifier]
    requests = [CompensationAssessment.model_validate(e["details"]["assessment"])
                for e in events if e["event"] == "compensation_evaluated"]
    related = [store.from_data(data, key) for key, p in data["proposals"].items()
               if (p.get("compensation") or {}).get("original_proposal_id") == identifier
               or (proposal.compensation and key == proposal.compensation.original_proposal_id)]
    # Parameters are never dumped recursively, even for malformed/legacy records.
    parameters = {key: safe_text(value, 200) for key, value in proposal.parameters.items()
                  if key in {"service", "target_id"} and isinstance(value, str)}
    demo_fields = (
        "artifact_sha256", "source_commit", "target_commit",
        "release_id", "instance_id", "configuration_digest",
    )
    demo = None
    if (proposal.action_type in {"w2_inventory_deploy_b", "w2_inventory_restore_a"}
            and all(isinstance(proposal.parameters.get(key), str) for key in demo_fields)):
        demo = DemoRecoveryView(**{
            key: safe_text(proposal.parameters[key], 2000) for key in demo_fields
        }, source_release_id=safe_text(proposal.parameters.get("source_release_id"), 80),
            configuration_summary=(
                "仅本机回环；单实例；无数据库；固定隔离入口；"
                "库存错误配置=" + ("关闭" if proposal.parameters["release_id"] == "A" else "开启")
            ),
        )
    try:
        requester = compensation_requester(data, proposal)
    except RemediationError:
        requester = None
    return RemediationDetail(
        **summary_view(store, data, proposal).model_dump(),
        reason=safe_text(proposal.reason, 2000),
        parameters=parameters,
        hidden_parameter_count=len(proposal.parameters) - len(parameters),
        proposal_digest=approval_digest(proposal),
        expected_commit_sha=proposal.expected_commit_sha,
        approved_by=safe_text(proposal.approved_by, 200),
        approved_at=proposal.approved_at,
        approval_valid=bool(proposal.approved_by and proposal.approved_at
                            and proposal.approval_digest == approval_digest(proposal)),
        rejected_by=safe_text(proposal.rejected_by, 200),
        rejected_at=proposal.rejected_at,
        rejection_reason=safe_text(proposal.rejection_reason),
        closure_reason=safe_text(proposal.closure.reason) if proposal.closure else None,
        compensation=CompensationView(
            status=status, reason=reason, target=target_view(target), compensation_proposal_id=child_id,
        ),
        target=target_view(proposal.compensation.target) if proposal.compensation else None,
        related_proposals=[summary_view(store, data, p) for p in related[:100]],
        demo_recovery=demo,
        source_evidence=source_evidence_view(store, data, proposal),
        requested_by=safe_text(requester, 200),
        actions=[ActionView(
            action_id=safe_text(a["action_id"], 120), started_at=a["started_at"],
            finished_at=a["finished_at"], success=a["success"], status=safe_text(a["status"], 200),
            execution_outcome=a.get("execution_outcome", "unknown"),
            reason=safe_text(a.get("details", {}).get("reason"), 200),
            resulting_commit_sha=(a.get("resulting_identity") or {}).get("commit_sha"),
            resulting_configuration_digest=(a.get("resulting_identity") or {}).get("configuration_digest"),
            release_id=safe_text(a.get("details", {}).get("release_id"), 80),
            pid=a.get("details", {}).get("pid")
            if type(a.get("details", {}).get("pid")) is int else None,
        ) for a in actions[-100:]],
        verifications=[VerificationView(
            verification_id=safe_text(v.verification_id, 120),
            verified_at=v.verified_at, purpose=v.purpose, outcome=v.outcome,
            evidence_status=evidence_status(v, proposal, actions),
            historical_success=v.success, action_id=safe_text(v.action_id, 120),
            checks=[VerificationCheckView(
                name=name, success=check.get("success") if type(check.get("success")) is bool else None,
                http_status=check.get("http_status") if type(check.get("http_status")) is int
                and 100 <= check["http_status"] <= 599 else None,
                reason=check.get("reason") if isinstance(check.get("reason"), str)
                and re.fullmatch(r"[a-z_]{1,100}", check["reason"]) else None,
                release_id=safe_text(check.get("observed", {}).get("release_id"), 80)
                if isinstance(check.get("observed"), dict) else None,
                artifact_sha256=safe_text(check.get("observed", {}).get("artifact_sha256"), 64)
                if isinstance(check.get("observed"), dict) else None,
                pid=check["observed"].get("pid")
                if isinstance(check.get("observed"), dict)
                and type(check["observed"].get("pid")) is int else None,
            ) for name, check in v.checks.items()
                if name in {"version", "order", "health", "business", "verification", "observation"}
                and isinstance(check, dict)],
        ) for v in verifications[-100:]],
        requests=[CompensationRequestView(
            status=r.status, reason=safe_text(r.reason, 200), target=target_view(r.target),
            compensation_proposal_id=safe_text(r.compensation_proposal_id, 120),
            assessment_id=safe_text(r.assessment_id, 120), command_id=safe_text(r.command_id, 200),
            requested_by=safe_text(r.requested_by, 200), request_reason=safe_text(r.request_reason),
            evaluated_at=r.evaluated_at, trigger=r.trigger,
        ) for r in requests[-100:]],
        audit=[AuditView(
            event_id=safe_text(e["event_id"], 120), occurred_at=e["occurred_at"],
            event=safe_text(e["event"], 200), actor=safe_text(e["actor"], 200),
            reason=safe_text(e["details"].get("reason")),
            proposal_digest=e["details"].get("proposal_digest")
            if re.fullmatch(r"[0-9a-f]{64}", str(e["details"].get("proposal_digest"))) else None,
            command_id=safe_text(e["details"].get("command_id"), 200),
            authorization_result=e["details"].get("authorization_result")
            if e["details"].get("authorization_result") in {"approved", "rejected"} else None,
        ) for e in events[-100:]],
    )
