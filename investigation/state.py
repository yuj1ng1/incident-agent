from investigation.models import SessionStatus as S


class RuntimeErrorBase(Exception):
    """Safe, stable error code suitable for CLI and audit."""


class LostClaim(RuntimeErrorBase):
    pass


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
