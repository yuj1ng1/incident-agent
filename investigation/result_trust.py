"""Read-only result qualification, independent of execution and approval state.

There is deliberately no acceptance operation here. Caller-supplied Agent
reviews can block trust, but cannot authenticate facts or grant human approval.
An authenticated, answer-bound acceptance workflow remains future work.
"""
from enum import Enum
import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from models.investigation import InvestigationRecord, InvestigationResult


class SemanticStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    REJECTED = "rejected"
    ACCEPTED = "accepted"


class ResultTrust(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mechanical_status: Literal["not_checked", "passed", "failed"]
    mechanical_scope: Literal[
        "not_checked", "result_schema_only", "frozen_references_and_hashes",
    ]
    semantic_status: SemanticStatus
    trusted: Literal[False] = False
    human_approval: Literal["not_recorded"] = "not_recorded"
    answer_sha256: str | None
    review_origin: Literal["none", "caller_supplied_agent_review"] = "none"
    rejection_reasons: list[str]


def project_trust(
    answer: str | None,
    *,
    mechanical_status="not_checked",
    mechanical_scope="not_checked",
    rejection_reasons=(),
    review_origin="none",
) -> ResultTrust:
    """Qualify supplied checks conservatively; never infer semantic acceptance.

    A hash binds the exact UTF-8 answer, not its authenticity or currentness.
    Rejection reasons are supplied review input, not facts extracted from prose.
    """
    reasons = list(rejection_reasons)
    rejected = mechanical_status == "failed" or bool(reasons)
    return ResultTrust(
        mechanical_status=mechanical_status,
        mechanical_scope=mechanical_scope,
        semantic_status=(
            SemanticStatus.REJECTED if rejected else SemanticStatus.PENDING_REVIEW
        ),
        answer_sha256=(
            hashlib.sha256(answer.encode("utf-8")).hexdigest()
            if answer is not None else None
        ),
        review_origin=review_origin,
        rejection_reasons=reasons,
    )


def project_result_trust(
    result: dict | None,
    *,
    history_record=False,
) -> ResultTrust:
    """Runtime/public default: only validate the existing result schema.

    Do not import a persisted or model-supplied trust label. Runtime answers do
    not require the isolated P5 claims contract, so references/hashes of their
    evidence remain unchecked here even when the result schema passes.
    """
    if result is None:
        return project_trust(None)
    try:
        schema = InvestigationRecord if history_record else InvestigationResult
        validated = schema.model_validate(result)
    except ValidationError:
        answer = result.get("answer") if isinstance(result, dict) else None
        return project_trust(
            answer if isinstance(answer, str) else None,
            mechanical_status="failed",
            mechanical_scope="result_schema_only",
            rejection_reasons=["invalid_result_schema"],
        )
    return project_trust(
        validated.answer,
        mechanical_status="passed",
        mechanical_scope="result_schema_only",
    )
