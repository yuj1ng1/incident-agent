from investigation.models import SessionStatus as S


class RuntimeErrorBase(Exception):
    """Safe, stable error code suitable for CLI and audit."""


class LostClaim(RuntimeErrorBase):
    pass


class AttemptToolBudgetExhausted(RuntimeErrorBase):
    """A soft per-attempt limit; session limits and fencing are separate."""


TERMINAL = {S.COMPLETED, S.EXHAUSTED, S.FAILED, S.CANCELLED}
TRANSITIONS = {
    S.CREATED: {S.QUEUED, S.CANCELLED, S.FAILED},
    S.QUEUED: {S.RUNNING, S.PAUSED, S.CANCELLED, S.EXHAUSTED, S.FAILED},
    S.RUNNING: {
        S.QUEUED, S.WAITING_RETRY, S.WAITING_HUMAN, S.PAUSED,
        S.COMPLETED, S.EXHAUSTED, S.FAILED, S.CANCELLED,
    },
    S.WAITING_RETRY: {S.QUEUED, S.PAUSED, S.CANCELLED, S.EXHAUSTED, S.FAILED},
    S.WAITING_HUMAN: {S.QUEUED, S.PAUSED, S.CANCELLED, S.EXHAUSTED, S.FAILED},
    S.PAUSED: {S.QUEUED, S.CANCELLED, S.EXHAUSTED, S.FAILED},
}


def transition(session, target):
    target = S(target)
    if target not in TRANSITIONS.get(session.status, set()):
        raise RuntimeErrorBase(f"illegal_transition:{session.status.value}:{target.value}")
    session.status = target


def exhausted(session, limits, now):
    return (
        session.total_model_requests >= limits.max_model_requests
        or session.total_tool_executions >= limits.max_tool_executions
        or session.resume_count > limits.max_resumes
        or (now - session.created_at).total_seconds() >= limits.max_duration_seconds
    )


def budget_exhaustion(usage, limits, now, *, new_attempt=False, resource=None):
    """Usage is derived from Session counters in the same authorization group."""
    if resource in {None, "model"} and usage["model_requests"] >= limits.max_model_requests:
        return "incident_model_budget_exhausted"
    if resource in {None, "tool"} and usage["tool_executions"] >= limits.max_tool_executions:
        return "incident_tool_budget_exhausted"
    if (now - usage["started_at"]).total_seconds() >= limits.max_duration_seconds:
        return "incident_duration_exhausted"
    # The Nth granted resume can run; Manager checks before granting N+1.
    if usage["resumes"] > limits.max_resumes:
        return "incident_resume_budget_exhausted"
    if new_attempt and usage["attempts"] >= limits.max_attempts:
        return "incident_attempt_budget_exhausted"
    return None
