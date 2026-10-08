"""Atomic JSON transactions contain state, results, and audit. No raw logs/secrets."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile

from pydantic import ValidationError

from models.remediation import (
    ActionResult, RemediationAuditEvent, RemediationProposal, VerificationResult,
)
from remediation.approval import approve_proposal, reject_proposal
from remediation.state import RemediationError

DEFAULT_STORE = Path(__file__).resolve().parents[1] / "data" / "remediation.json"


class StoreError(RemediationError):
    pass


class FileLock:
    """Nonblocking OS lock: a crash releases it; never guess that a lock is stale."""
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = self.path.open("a+b")
        except OSError:
            raise StoreError("remediation_store_lock_unavailable") from None
        try:
            if os.fstat(self.handle.fileno()).st_size == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise StoreError("store_busy") from None
        return self

    def __exit__(self, *args):
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()


class RemediationStore:
    def __init__(self, file_path=DEFAULT_STORE):
        self.file_path = Path(file_path)

    def load(self):
        if not self.file_path.exists():
            return {"version": 1, "proposals": {}, "actions": [], "verifications": [], "audit": []}
        try:
            data = json.loads(self.file_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) != {"version", "proposals", "actions", "verifications", "audit"}:
                raise ValueError()
            if data["version"] != 1 or not isinstance(data["proposals"], dict):
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
            data = self.load()
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

    def create(self, proposal):
        if proposal.status.value != "pending":
            raise StoreError("new_proposal_must_be_pending")
        expected_started_at = proposal.model_dump(mode="json")[
            "expected_process_started_at"
        ]
        with self.transaction() as data:
            for old in data["proposals"].values():
                if (
                    old["service"] == proposal.service
                    and old["action_type"] == proposal.action_type
                    and old["parameters"] == proposal.parameters
                    and old["expected_commit_sha"] == proposal.expected_commit_sha
                    and old["expected_process_started_at"] == expected_started_at
                ):
                    return RemediationProposal.model_validate(old)
                if (old["incident_id"], old["action_type"], old["expected_commit_sha"]) == (
                        proposal.incident_id, proposal.action_type, proposal.expected_commit_sha):
                    return RemediationProposal.model_validate(old)
            if proposal.proposal_id in data["proposals"]:
                raise StoreError("proposal_id_exists")
            self.put(data, proposal)
            self.event(data, proposal, "proposal_created", "remediation_planner")
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
