"""MySQL transactions contain proposals, decisions, results, and audit together."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from db import tables
from db.config import database_url as configured_database_url, get_engine

from models.remediation import (
    ActionResult, ExecutionOutcome, RemediationAuditEvent, RemediationProposal,
    RemediationStatus as S, VerificationResult,
)
from remediation.approval import (
    actor_name, approval_digest, approve_proposal, reject_proposal,
)
from remediation.state import RemediationError
MAX_MANUAL_RETRIES = 2


class StoreError(RemediationError):
    pass


def empty_document():
    return {
        "version": 2,
        "proposals": {},
        "actions": [],
        "verifications": [],
        "audit": [],
    }


def validate_document(data):
    """Validate v1/v2 without rewriting legacy fields or object order."""
    try:
        if not isinstance(data, dict) or set(data) != set(empty_document()):
            raise ValueError()
        if type(data["version"]) is not int or data["version"] not in {1, 2}:
            raise ValueError()
        if not isinstance(data["proposals"], dict):
            raise ValueError()
        for key, value in data["proposals"].items():
            if RemediationProposal.model_validate(value).proposal_id != key:
                raise ValueError()
        for key, cls in (
            ("actions", ActionResult),
            ("verifications", VerificationResult),
            ("audit", RemediationAuditEvent),
        ):
            if not isinstance(data[key], list):
                raise ValueError()
            for value in data[key]:
                cls.model_validate(value)
        json.dumps(data, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, ValidationError):
        raise StoreError("remediation_store_corrupt_or_unreadable") from None
    return data


class RemediationStore:
    def __init__(self, database_url=None, *, read_only=False):
        try:
            self.database_url = database_url or configured_database_url()
            url = make_url(self.database_url)
            if url.drivername != "mysql+pymysql" or not url.database:
                raise ValueError()
            self.engine = get_engine(self.database_url)
        except (ArgumentError, ValueError, RuntimeError):
            raise StoreError("remediation_database_url_required") from None
        self.read_only = read_only
        # Store objects are shared by ASGI threads. Never put a connection in an
        # ordinary instance attribute: another thread could commit its document.
        self._transaction = ContextVar("remediation_transaction", default=None)

    def load(self):
        try:
            with self.engine.connect() as connection:
                row = connection.execute(
                    select(tables.remediation_state).where(
                        tables.remediation_state.c.id == 1,
                    )
                ).mappings().first()
            return self._decode(row)
        except SQLAlchemyError:
            raise StoreError("remediation_store_unavailable") from None

    @staticmethod
    def _decode(row):
        if row is None:
            raise StoreError("remediation_store_unavailable")
        try:
            data = json.loads(row["data"])
        except (ValueError, TypeError):
            raise StoreError("remediation_store_corrupt_or_unreadable") from None
        return validate_document(data)

    def _save(self, data):
        active = self._transaction.get()
        if active is None:
            raise StoreError("remediation_transaction_required")
        connection, revision = active
        # Lazy v1 -> v2 migration remains part of the committing transaction.
        data["version"] = 2
        validate_document(data)
        try:
            result = connection.execute(
                update(tables.remediation_state)
                .where(
                    tables.remediation_state.c.id == 1,
                    tables.remediation_state.c.revision == revision,
                )
                .values(
                    revision=revision + 1,
                    data=json.dumps(data, ensure_ascii=False, allow_nan=False),
                    updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
            )
            if result.rowcount != 1:
                raise StoreError("remediation_revision_conflict")
        except SQLAlchemyError:
            raise StoreError("remediation_store_write_failed") from None

    @contextmanager
    def transaction(self):
        if self.read_only:
            raise StoreError("remediation_store_read_only")
        if self._transaction.get() is not None:
            raise StoreError("remediation_nested_transaction")
        try:
            with self.engine.begin() as connection:
                row = connection.execute(
                    select(tables.remediation_state)
                    .where(tables.remediation_state.c.id == 1)
                    .with_for_update()
                ).mappings().first()
                data = self._decode(row)
                token = self._transaction.set((connection, row["revision"]))
                try:
                    yield data
                    self._save(data)
                finally:
                    self._transaction.reset(token)
        except SQLAlchemyError:
            # Never replay a yielded mutation or an uncertain COMMIT.
            raise StoreError("remediation_store_unavailable") from None

    @contextmanager
    def service_lock(self, name, *, timeout=0):
        """Serialize physical actions across processes and schemas on this server."""
        if self.read_only:
            raise StoreError("remediation_store_read_only")
        if type(timeout) is not int or timeout < 0:
            raise StoreError("remediation_lock_timeout_invalid")
        key = "ia_remediation_" + hashlib.sha256(str(name).encode()).hexdigest()[:48]
        try:
            with self.engine.connect() as connection:
                try:
                    acquired = connection.exec_driver_sql(
                        "SELECT GET_LOCK(%s, %s)", (key, timeout),
                    ).scalar()
                except SQLAlchemyError:
                    # The server may have acquired the session lock before its
                    # reply was lost. Never return that session to the pool.
                    connection.invalidate()
                    raise
                if acquired == 0:
                    raise StoreError("remediation_service_busy")
                if acquired != 1:
                    connection.invalidate()
                    raise StoreError("remediation_store_unavailable")
                try:
                    yield
                finally:
                    try:
                        released = connection.exec_driver_sql(
                            "SELECT RELEASE_LOCK(%s)", (key,),
                        ).scalar()
                        if released != 1:
                            connection.invalidate()
                            raise StoreError("remediation_service_lock_lost")
                    except SQLAlchemyError:
                        # A pooled session must never retain an advisory lock.
                        connection.invalidate()
                        raise
        except SQLAlchemyError:
            raise StoreError("remediation_store_unavailable") from None

    @staticmethod
    def event(data, proposal, event, actor, **details):
        record = RemediationAuditEvent(proposal_id=proposal.proposal_id,
            action_type=proposal.action_type, actor=actor, event=event, details=details)
        data["audit"].append(record.model_dump(mode="json"))

    @staticmethod
    def put(data, proposal):
        data["proposals"][proposal.proposal_id] = proposal.model_dump(mode="json")

    @staticmethod
    def from_data(data, proposal_id):
        if proposal_id not in data["proposals"]:
            raise StoreError("proposal_not_found")
        return RemediationProposal.model_validate(data["proposals"][proposal_id])

    def get(self, proposal_id):
        return self.from_data(self.load(), proposal_id)

    def list(self, status=None):
        return [RemediationProposal.model_validate(p) for p in self.load()["proposals"].values()
                if status is None or p["status"] == status]

    @staticmethod
    def execution_outcome(data, proposal):
        if proposal.closure is not None:
            return proposal.closure.execution_outcome
        if proposal.status in {S.PENDING, S.APPROVED, S.REJECTED}:
            return ExecutionOutcome.NOT_EXECUTED
        results = [ActionResult.model_validate(item) for item in data["actions"]
                   if item["proposal_id"] == proposal.proposal_id]
        return results[-1].execution_outcome if results else ExecutionOutcome.UNKNOWN

    def attention(self):
        return [p for p in self.list() if p.status in {
            S.PENDING, S.APPROVED, S.EXECUTING, S.FAILED, S.VERIFICATION_FAILED,
        } or (p.closure and p.closure.execution_outcome == ExecutionOutcome.UNKNOWN)]

    def create(self, proposal):
        if proposal.status.value != "pending":
            raise StoreError("new_proposal_must_be_pending")
        expected_started_at = proposal.model_dump(mode="json")[
            "expected_process_started_at"
        ]
        with self.transaction() as data:
            # The latest explicit retry is authoritative for duplicate planning.
            for old in reversed(list(data["proposals"].values())):
                if (
                    old["service"] == proposal.service
                    and old["action_type"] == proposal.action_type
                    and old["parameters"] == proposal.parameters
                    and old["expected_commit_sha"] == proposal.expected_commit_sha
                    and old["expected_process_started_at"] == expected_started_at
                ):
                    return RemediationProposal.model_validate(old)
            if proposal.proposal_id in data["proposals"]:
                raise StoreError("proposal_id_exists")
            self.put(data, proposal)
            self.event(data, proposal, "proposal_created", "remediation_planner")
        return proposal

    def retry(self, proposal_id, by, reason, observation):
        """Create a separate pending proposal only after a bounded human decision."""
        by = actor_name(by)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise RemediationError("retry_reason_required_max_1000")
        denial = None
        with self.transaction() as data:
            old = self.from_data(data, proposal_id)
            if old.compensation is not None:
                raise RemediationError("compensation_retry_forbidden")
            successor = next((self.from_data(data, key) for key, item in data["proposals"].items()
                              if item.get("retry_of") == proposal_id), None)
            if successor:
                self.event(data, old, "retry_skipped", by, reason="successor_exists",
                           successor=successor.proposal_id)
                return successor
            if old.status not in {S.REJECTED, S.FAILED}:
                denial = "retry_requires_rejected_or_failed"
            elif self.execution_outcome(data, old) != ExecutionOutcome.NOT_EXECUTED:
                denial = "retry_execution_outcome_not_confirmed_not_executed"
            lineage = old
            depth = 0
            seen = {old.proposal_id}
            while lineage.retry_of and denial is None:
                if lineage.retry_of in seen:
                    denial = "retry_lineage_invalid"
                    break
                seen.add(lineage.retry_of)
                lineage = self.from_data(data, lineage.retry_of)
                depth += 1
            if depth >= MAX_MANUAL_RETRIES:
                denial = "manual_retry_limit_reached"
            if denial:
                self.event(data, old, "retry_denied", by, reason=denial)
            else:
                fields = old.model_dump(include={
                    "incident_id", "service", "summary", "reason", "action_type", "parameters", "risk",
                    "expected_commit_sha", "expected_process_started_at", "evidence_sources",
                })
                proposal = RemediationProposal(**fields, retry_of=old.proposal_id)
                self.put(data, proposal)
                self.event(data, old, "retry_requested", by, reason=reason,
                           successor=proposal.proposal_id, observation=observation)
                self.event(data, proposal, "proposal_created", by, retry_of=old.proposal_id)
        if denial:
            raise RemediationError(denial)
        return proposal

    @staticmethod
    def check_command(
        data,
        proposal,
        event_name,
        actor,
        command_id,
        digest,
        reason=None,
    ):
        """Optional Web command idempotency leaves legacy CLI decisions unchanged."""
        if command_id is None:
            return False
        actor_name(command_id)
        if digest != approval_digest(proposal):
            raise RemediationError("proposal_digest_mismatch")
        for event in data["audit"]:
            details = event.get("details", {})
            if (details.get("assessment") or {}).get("command_id") == command_id:
                raise RemediationError("command_id_conflict")
            if details.get("command_id") == command_id:
                if (event["proposal_id"], event["event"], event["actor"],
                        details.get("proposal_digest"), details.get("reason")) != (
                        proposal.proposal_id, event_name, actor, digest, reason):
                    raise RemediationError("command_id_conflict")
                return True
        return False

    def request_compensation(
        self,
        proposal_id,
        by,
        reason,
        command_id,
        *,
        expected_digest=None,
        target_id=None,
    ):
        from remediation.compensation import evaluate, record_request
        with self.transaction() as data:
            original = self.from_data(data, proposal_id)
            if expected_digest is not None and approval_digest(original) != expected_digest:
                raise RemediationError("proposal_digest_mismatch")
            if target_id is not None:
                _, _, target, _ = evaluate(data, original)
                if target is None or target.target_id != target_id:
                    raise RemediationError("compensation_target_changed")
            return record_request(
                data, self, original, by, reason, command_id, "operator_request",
            )

    def approve(
        self,
        proposal_id,
        by,
        *,
        command_id=None,
        expected_digest=None,
    ):
        by = actor_name(by)
        with self.transaction() as data:
            original = self.from_data(data, proposal_id)
            from remediation.compensation import check_compensation_actor
            check_compensation_actor(data, original, by, "approve")
            if self.check_command(data, original, "approved", by, command_id, expected_digest):
                return original
            if original.compensation is not None:
                from remediation.compensation import execution_contract
                execution_contract(data, original)
            proposal = approve_proposal(original, by)
            self.put(data, proposal)
            self.event(data, proposal, "approved", by,
                proposal_digest=approval_digest(proposal), authorization_result="approved", **(
                {"command_id": command_id}
                if command_id else {}
            ))
        return proposal

    def reject(
        self,
        proposal_id,
        by,
        reason,
        *,
        command_id=None,
        expected_digest=None,
    ):
        by = actor_name(by)
        with self.transaction() as data:
            original = self.from_data(data, proposal_id)
            from remediation.compensation import check_compensation_actor
            check_compensation_actor(data, original, by, "approve")
            if self.check_command(data, original, "rejected", by, command_id, expected_digest, reason):
                return original
            proposal = reject_proposal(original, by, reason)
            self.put(data, proposal)
            self.event(data, proposal, "rejected", by, reason=reason,
                proposal_digest=approval_digest(proposal), authorization_result="rejected", **(
                {"command_id": command_id}
                if command_id else {}
            ))
        return proposal
