"""Atomic JSON transactions contain state, results, and audit. No raw logs/secrets."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile

from pydantic import ValidationError

from models.remediation import (
    ActionResult, ExecutionOutcome, RemediationAuditEvent, RemediationProposal,
    RemediationStatus as S, VerificationResult,
)
from remediation.approval import actor_name, approve_proposal, reject_proposal
from remediation.state import RemediationError
from storage_lock import FileLock as SharedFileLock, FileLockError

DEFAULT_STORE = Path(__file__).resolve().parents[1] / "data" / "remediation.json"
MAX_MANUAL_RETRIES = 2


class StoreError(RemediationError):
    pass


class FileLock(SharedFileLock):
    def __enter__(self):
        try:
            return super().__enter__()
        except FileLockError:
            raise StoreError("store_busy") from None


class RemediationStore:
    def __init__(self, file_path=DEFAULT_STORE):
        self.file_path = Path(file_path)

    def load(self):
        # Windows readers must release their handles before a writer replaces JSON.
        with FileLock(str(self.file_path) + ".lock"):
            return self._load_unlocked()

    def _load_unlocked(self):
        if not self.file_path.exists():
            return {"version": 2, "proposals": {}, "actions": [], "verifications": [], "audit": []}
        try:
            data = json.loads(self.file_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) != {"version", "proposals", "actions", "verifications", "audit"}:
                raise ValueError()
            if data["version"] not in {1, 2} or not isinstance(data["proposals"], dict):
                raise ValueError()
            for key, value in data["proposals"].items():
                if RemediationProposal.model_validate(value).proposal_id != key:
                    raise ValueError()
            for key, cls in (("actions", ActionResult), ("verifications", VerificationResult),
                             ("audit", RemediationAuditEvent)):
                if not isinstance(data[key], list):
                    raise ValueError()
                for value in data[key]:
                    cls.model_validate(value)
            return data
        except (OSError, ValueError, TypeError, ValidationError):
            raise StoreError("remediation_store_corrupt_or_unreadable") from None

    def _save(self, data):
        temp_path = None
        try:
            # Lazy, atomic v1 -> v2 migration; reads never rewrite old records.
            data["version"] = 2
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.file_path.parent,
                                             prefix=self.file_path.name + ".", suffix=".tmp", delete=False) as out:
                temp_path = Path(out.name)
                json.dump(data, out, ensure_ascii=False, indent=2, allow_nan=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp_path, self.file_path)
        except (OSError, ValueError):
            raise StoreError("remediation_store_write_failed") from None
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()

    @contextmanager
    def transaction(self):
        with FileLock(str(self.file_path) + ".lock"):
            data = self._load_unlocked()
            yield data
            self._save(data)

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

    def approve(self, proposal_id, by):
        with self.transaction() as data:
            proposal = approve_proposal(self.from_data(data, proposal_id), by)
            self.put(data, proposal)
            self.event(data, proposal, "approved", by)
        return proposal

    def reject(self, proposal_id, by, reason):
        with self.transaction() as data:
            proposal = reject_proposal(self.from_data(data, proposal_id), by, reason)
            self.put(data, proposal)
            self.event(data, proposal, "rejected", by, reason=reason)
        return proposal
