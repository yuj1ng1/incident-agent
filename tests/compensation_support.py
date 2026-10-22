"""Offline contracts only. Never registered by application code."""
from pathlib import Path
from types import MappingProxyType

from models.remediation import (
    ActionResult, CompensationTarget, ExecutionOutcome as O, Risk,
    ServiceIdentity, VerificationOutcome as V, VerificationResult, utc_now,
)
from remediation.compensation import CompensationContract


def identity(service="order-service", sha="a" * 40, configuration="c" * 64):
    return ServiceIdentity(
        service=service, commit_sha=sha, process_started_at=utc_now(),
        configuration_digest=configuration,
    )


class FixtureVerifier:
    def __init__(self, observed, outcome=V.PASSED):
        self.observed = observed
        self.outcome = outcome

    def verify(self, proposal):
        return VerificationResult(
            proposal_id=proposal.proposal_id, started_at=utc_now(),
            success=self.outcome == V.PASSED, outcome=self.outcome,
            observed_identity=self.observed,
            checks={"business": {"success": self.outcome == V.PASSED}},
            summary="Offline fixture business observation",
        )


def fixture_contract(source, *, effect_path=None, outcome=V.PASSED, execution_outcome=O.EXECUTED):
    target = CompensationTarget(
        target_id="fixture-config-v1", service=source.service,
        commit_sha="b" * 40, artifact_sha256="e" * 64,
        configuration_digest="d" * 64,
        preconditions="Match exact observed fixture process, version and configuration.",
        side_effects="Update an isolated test file once; no business systems.",
        verification_policy="Independent fixture business assertion.",
        data_compatibility="Fixture data schema unchanged; no database mutation.",
    )
    result_identity = ServiceIdentity(
        service=target.service, commit_sha=target.commit_sha,
        configuration_digest=target.configuration_digest, process_started_at=utc_now(),
    )

    def handler(proposal, runtime):
        if effect_path is not None:
            with Path(effect_path).open("a", encoding="utf-8") as output:
                output.write("effect\n")
        return ActionResult(
            proposal_id=proposal.proposal_id, action_type=proposal.action_type,
            started_at=utc_now(), success=execution_outcome == O.EXECUTED,
            status="fixture_effect", summary="Offline fixture action",
            execution_outcome=execution_outcome, resulting_identity=result_identity,
        )

    return CompensationContract(
        contract_id="offline-fixture-v1", original_action_type="disable_demo_regression",
        action_type="restore_fixture_config", target=target,
        parameters=MappingProxyType({"service": source.service, "target_id": target.target_id}),
        risk=Risk.MEDIUM, inspect_identity=lambda p, runtime: source,
        handler=handler, verifier=FixtureVerifier(result_identity, outcome),
    )


def original_handler(source):
    def handler(proposal, runtime):
        return ActionResult(
            proposal_id=proposal.proposal_id, action_type=proposal.action_type,
            started_at=utc_now(), success=True, status="fixture_original",
            summary="Offline original action", execution_outcome=O.EXECUTED,
            resulting_identity=source,
        )
    return handler
