from models.remediation import RemediationProposal, RemediationStatus as S


class RemediationError(ValueError):
    pass


TRANSITIONS = {
    S.PENDING: {S.APPROVED, S.REJECTED},
    S.APPROVED: {S.EXECUTING},
    S.EXECUTING: {S.SUCCEEDED, S.FAILED, S.VERIFICATION_FAILED},
}


def transition(proposal, target, **updates):
    target = S(target)
    if target not in TRANSITIONS.get(proposal.status, set()):
        raise RemediationError(f"invalid_transition:{proposal.status.value}->{target.value}")
    data = proposal.model_dump()
    data.update(updates, status=target)
    return RemediationProposal.model_validate(data)
