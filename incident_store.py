import json
import hashlib
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import wraps
import threading
import time
from enum import Enum
from uuid import uuid4

from storage_lock import FileLock

from models.incident import Evidence, HistoryArchive, Incident
from models.investigation import InvestigationRecord
from models.recovery import (
    RecoveryAttempt, RecoveryPolicy, RecoveryReceipt, RecoveryVerification, aware_utc, utc_now,
)


class RuntimeProjectionResult(str, Enum):
    APPLIED = "applied"
    UNCHANGED = "unchanged"
    ARCHIVED = "archived"
    MISSING = "missing"


def locked_update(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if self.read_only:
            raise PermissionError("incident_store_read_only")
        with self._thread_lock:
            if getattr(self._lock_depth, "active", False):
                return method(self, *args, **kwargs)
            with FileLock(str(self.file_path) + ".lock"):
                self._lock_depth.active = True
                try:
                    return method(self, *args, **kwargs)
                finally:
                    self._lock_depth.active = False
    return wrapped


class IncidentStore:
    def __init__(self,file_path, runtime_history_to_keep=3, *, read_only=False, clock=utc_now):
        if not isinstance(runtime_history_to_keep, int) or runtime_history_to_keep < 1:
            raise ValueError("runtime_history_retention_must_be_positive")
        self.file_path=Path(file_path)
        self.read_only = read_only
        self.clock = clock
        self.runtime_history_to_keep = runtime_history_to_keep
        self._thread_lock = threading.RLock()
        self._lock_depth = threading.local()

        if read_only:
            if not self.file_path.is_file():
                raise FileNotFoundError("incident_store_not_found")
            return

        self.file_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        self._initialize()

    @locked_update
    def _initialize(self):
        if not self.file_path.exists():
            self.save(self.create_empty_data())

    def create_empty_data(self):
        return {
            "version":2,
            "incidents":{},
            "active_by_fingerprint":{}
        }

    def load(self):
        # Coordinate readers sharing this Store with its atomic writer. Parsing
        # operates on an already closed, complete file snapshot.
        with self._thread_lock:
            content = self.file_path.read_text(encoding="utf-8")

        data = json.loads(content)
        if isinstance(data, dict) and data.get("version") == 2:
            if not isinstance(data.get("incidents"), dict) or not isinstance(data.get("active_by_fingerprint"), dict):
                raise ValueError("unsupported_or_corrupt_incident_store")
            return data
        # Original v1 used a fingerprint -> incident mapping. Keep every ID/record.
        if isinstance(data, dict) and all(
            isinstance(value, dict) and "incident_id" in value
            for value in data.values()
        ):
            migrated = self.create_empty_data()
            for fingerprint, value in data.items():
                migrated["incidents"][value["incident_id"]] = value
                if value.get("status") != "resolved":
                    migrated["active_by_fingerprint"][fingerprint] = value["incident_id"]
            return migrated
        raise ValueError("unsupported_or_corrupt_incident_store")

    @locked_update
    def save(self,data):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.file_path.parent,
                suffix=".tmp",
                delete=False,
            ) as output:
                temporary = Path(output.name)
                json.dump(data, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            for attempt in range(3):
                try:
                    os.replace(temporary, self.file_path)
                    break
                except PermissionError as exc:
                    # A separate Windows reader may briefly deny replacement.
                    # Bound only this local commit retry; never repeat HTTP here.
                    if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32} or attempt == 2:
                        raise
                    time.sleep(0.01)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def active_incidents(self, include_history=True):
        data = self.load()
        return [
            self._incident(data["incidents"][incident_id], include_history)
            for incident_id in data["active_by_fingerprint"].values()
            if incident_id in data["incidents"]
        ]

    def get_incident(self, incident_id, include_history=True):
        value = self.load()["incidents"].get(incident_id)
        return self._incident(value, include_history) if value is not None else None

    def list_incidents(self, *, status=None, limit=20, offset=0):
        """Bounded newest-first query; archives are never opened by a list view."""
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("incident_pagination_invalid")
        incidents = []
        for incident_id, value in self.load()["incidents"].items():
            incident = self._incident(value, include_history=False)
            if incident.incident_id != incident_id:
                raise ValueError("incident_store_identity_mismatch")
            if status is None or incident.status == status:
                incidents.append(incident)
        # The JSON store preserves creation insertion order, including legacy v1.
        return list(reversed(incidents))[offset:offset + limit]

    def _incident(self, value, include_history):
        incident = Incident.model_validate(value)
        if include_history and incident.history_archives:
            investigations, evidence = [], []
            seen = set()
            for reference in incident.history_archives:
                if seen.intersection(reference.session_ids):
                    raise ValueError("duplicate_incident_history_session")
                seen.update(reference.session_ids)
                archived = self._load_archive(incident.incident_id, reference)
                investigations.extend(archived["investigations"])
                evidence.extend(archived["evidence"])
            def stamp(value):
                if value is None:
                    return datetime.min.replace(tzinfo=timezone.utc)
                if value.tzinfo is None:
                    from datetime import timedelta
                    value = value.replace(tzinfo=timezone(timedelta(hours=8)))
                return value.astimezone(timezone.utc)
            incident.investigations = sorted(investigations + incident.investigations,
                                            key=lambda r: stamp(r.investigated_at))
            incident.evidence = sorted(evidence + incident.evidence, key=lambda e: stamp(e.collected_at))
        return incident

    def _load_archive(self, incident_id, reference):
        path = self.file_path.parent / "history" / (reference.sha256 + ".json")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise ValueError("incident_history_archive_checksum_mismatch")
        body = json.loads(content)
        if (not isinstance(body, dict)
                or set(body) != {"schema_version", "incident_id", "investigations", "evidence"}
                or body["schema_version"] != 1 or body["incident_id"] != incident_id
                or not isinstance(body["investigations"], list) or not isinstance(body["evidence"], list)):
            raise ValueError("incident_history_archive_identity_mismatch")
        investigations = [InvestigationRecord.model_validate(r) for r in body["investigations"]]
        evidence = [Evidence.model_validate(e) for e in body["evidence"]]
        if ([r.session_id for r in investigations] != reference.session_ids
                or len(set(reference.session_ids)) != len(reference.session_ids)
                or any(e.runtime_session_id not in reference.session_ids for e in evidence)):
            raise ValueError("incident_history_archive_records_mismatch")
        return {"investigations": investigations, "evidence": evidence}

    def _archive_history(self, incident):
        archived_ids = {sid for ref in incident.history_archives for sid in ref.session_ids}
        # Public reads hydrate immutable history. Reject attempts to alter it;
        # routine hot-path writes do not reopen every old archive.
        included = {r.session_id for r in incident.investigations if r.session_id in archived_ids}
        if included or any(e.runtime_session_id in archived_ids for e in incident.evidence):
            expected_records, expected_evidence = [], []
            for ref in incident.history_archives:
                body = self._load_archive(incident.incident_id, ref)
                expected_records.extend(body["investigations"])
                expected_evidence.extend(body["evidence"])
            actual_records = [r for r in incident.investigations if r.session_id in archived_ids]
            actual_evidence = [e for e in incident.evidence if e.runtime_session_id in archived_ids]
            # Full-history reads sort by observation time; immutability compares
            # complete contents independently of that presentation order.
            def contents(records):
                return sorted(json.dumps(r.model_dump(mode="json"), sort_keys=True) for r in records)
            if (contents(actual_records) != contents(expected_records)
                    or contents(actual_evidence) != contents(expected_evidence)):
                raise ValueError("archived_incident_history_is_immutable")
        incident.investigations = [r for r in incident.investigations if r.session_id not in archived_ids]
        incident.evidence = [e for e in incident.evidence if e.runtime_session_id not in archived_ids]
        runtime_records = [r for r in incident.investigations if r.session_id]
        older = runtime_records[:-self.runtime_history_to_keep]
        if not older:
            return
        session_ids = [r.session_id for r in older]
        evidence = [e for e in incident.evidence if e.runtime_session_id in session_ids]
        body = {"schema_version": 1, "incident_id": incident.incident_id,
                "investigations": [r.model_dump(mode="json") for r in older],
                "evidence": [e.model_dump(mode="json") for e in evidence]}
        content = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        reference = HistoryArchive(sha256=hashlib.sha256(content).hexdigest(), session_ids=session_ids)
        path = self.file_path.parent / "history" / (reference.sha256 + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Archive first, fsync and validate it, then atomically commit the hot
        # manifest. A crash can leave an unreferenced file, never missing history.
        if not path.exists():
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as output:
                    temporary = Path(output.name)
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
        self._load_archive(incident.incident_id, reference)
        incident.history_archives.append(reference)
        incident.investigations = [r for r in incident.investigations if r.session_id not in session_ids]
        incident.evidence = [e for e in incident.evidence if e.runtime_session_id not in session_ids]

    @locked_update
    def save_observation(self, fingerprint, incident):
        """Merge monitor observations with the latest Worker-owned fields."""
        latest = self.get_incident(incident.incident_id, include_history=False)
        if latest is not None:
            latest.alerts = incident.alerts
            latest.alert_state = incident.alert_state
            latest.related_alerts = incident.related_alerts
            latest.severity = incident.severity
            incident = latest
        self.save_incident(fingerprint, incident)

    @locked_update
    def apply_runtime_projection(self, session, checkpoint):
        """Return an explicit durable outcome; missing data is never success."""
        from investigation.models import SessionStatus as S
        incident = self.get_incident(session.incident_id, include_history=False)
        if incident is None:
            return RuntimeProjectionResult.MISSING
        if incident.fingerprint != session.alert_fingerprint or incident.service != session.service:
            raise ValueError("incident_projection_identity_mismatch")
        previous_data = incident.model_dump()
        projected_record = self._runtime_record(session, checkpoint)
        projected_evidence = self._runtime_evidence(session, checkpoint)
        references = [ref for ref in incident.history_archives if session.session_id in ref.session_ids]
        if references:
            if len(references) != 1:
                raise ValueError("duplicate_incident_history_session")
            try:
                archived = self._load_archive(incident.incident_id, references[0])
            except FileNotFoundError:
                raise ValueError("incident_history_archive_unavailable") from None
            actual = next(r for r in archived["investigations"] if r.session_id == session.session_id)
            if projected_record is None or actual != projected_record:
                raise ValueError("incident_history_archive_projection_mismatch")
            known = {e.observation_id: e for e in archived["evidence"] if e.runtime_session_id == session.session_id}
            if any(known.get(e.observation_id) != e for e in projected_evidence):
                raise ValueError("incident_history_archive_projection_mismatch")
            return RuntimeProjectionResult.ARCHIVED
        if session.budget_group_id == session.session_id and not session.attempt_count:
            incident.consecutive_investigation_failures = 0
            incident.investigation_suspended_reason = None
            incident.last_attempted_at = None
            # The verified reopen snapshot belongs to the authorization audit;
            # Monitor alone owns current alerts, observed_at and lifecycle state.
        known = {item.observation_id: item for item in incident.evidence if item.observation_id}
        for evidence in projected_evidence:
            previous = known.get(evidence.observation_id)
            if previous is not None and previous != evidence:
                raise ValueError("incident_projection_evidence_mismatch")
            if previous is None:
                incident.evidence.append(evidence)
                known[evidence.observation_id] = evidence
        if session.attempt_count:
            incident.last_attempted_at = session.completed_at or session.updated_at
        incident.investigation_suspended_reason = (
            session.last_error_category if session.status == S.PAUSED else (
                "exhausted_requires_human_reauthorization" if session.status == S.EXHAUSTED else (
                    "cancelled_requires_human_reopen" if session.status == S.CANCELLED else None
                )
            )
        )
        if session.status == S.FAILED:
            from reinvestigation import RETRYABLE_FAILURES
            if session.last_error_category not in RETRYABLE_FAILURES:
                incident.investigation_suspended_reason = "non_retryable_failure_requires_human_reopen"
        previous = next((r for r in incident.investigations if r.session_id == session.session_id), None)
        if previous is not None and previous != projected_record:
            raise ValueError("incident_projection_result_mismatch")
        if projected_record is not None and previous is None:
            incident.investigations.append(projected_record)
            if (checkpoint.get("result") or {}).get("success"):
                incident.consecutive_investigation_failures = 0
                incident.last_investigated_at = projected_record.investigated_at
                incident.last_investigated_value = checkpoint.get("investigated_value")
                incident.last_investigated_state = checkpoint.get("investigated_state")
            else:
                incident.consecutive_investigation_failures += 1
        if incident.model_dump() != previous_data:
            self.save_incident(session.alert_fingerprint, incident)
            return RuntimeProjectionResult.APPLIED
        return RuntimeProjectionResult.UNCHANGED

    @staticmethod
    def _runtime_record(session, checkpoint):
        from investigation.models import SessionStatus as S
        if session.status not in {S.COMPLETED, S.EXHAUSTED, S.FAILED}:
            return None
        result = checkpoint.get("result")
        if not result and session.status in {S.FAILED, S.EXHAUSTED}:
            result = {"status": "model_incomplete",
                      "answer": "调查停止：" + (session.last_error_category or session.status.value)}
        if not result:
            raise ValueError("completed_projection_result_missing")
        return InvestigationRecord(
            session_id=session.session_id, investigated_at=session.completed_at or session.updated_at,
            status=result["status"], alert_value=checkpoint.get("investigated_value"),
            answer=result["answer"], model_rounds=session.total_model_requests,
            tool_calls=session.total_tool_executions, tool_trace=checkpoint.get("trace", []),
        )

    @staticmethod
    def _runtime_evidence(session, checkpoint):
        return [Evidence(
            source=r["name"], summary=f"{r['name']}查询结果（采集时观测）",
            detail=json.dumps(r["result"], ensure_ascii=False), collected_at=r["collected_at"],
            runtime_session_id=session.session_id, observation_id=r["observation_id"],
        ) for r in checkpoint.get("records", [])
            if r.get("success") and r.get("result", {}).get("status") == "ok"]

    @locked_update
    def get_or_migrate_active(self, alert, fingerprint):
        incident = self.get_active_incident(fingerprint, include_history=False)
        if incident is not None or alert.identity_labels:
            return incident
        # Only migrate unambiguous service-aggregated legacy HTTP alerts.
        if alert.alertname != "HighHttp5xxRate":
            return None
        from fingerprint import legacy_fingerprint
        old_fingerprint = legacy_fingerprint(alert)
        old = self.get_active_incident(old_fingerprint, include_history=False)
        if old is None or not old.alerts or any(
            event.source != "legacy"
            or event.service != alert.service
            or event.metric != alert.metric
            for event in old.alerts
        ):
            return None
        data = self.load()
        old.fingerprint = fingerprint
        data["incidents"][old.incident_id] = old.model_dump(mode="json")
        data["active_by_fingerprint"].pop(old_fingerprint, None)
        data["active_by_fingerprint"][fingerprint] = old.incident_id
        self.save(data)
        return old

    @staticmethod
    def _recovery_context_current(data, incident):
        if (incident.status != "resolved" or incident.alert_state != "cleared"
                or not incident.fingerprint or incident.cleared_at is None):
            return False
        if data["active_by_fingerprint"].get(incident.fingerprint) is not None:
            return False
        # Re-firing creates a new Incident. Even if that successor has already
        # cleared, an old in-flight probe must not attest the preceding episode.
        newest = next((key for key, value in reversed(data["incidents"].items())
                       if value.get("fingerprint") == incident.fingerprint), None)
        return newest == incident.incident_id

    def recovery_candidates(self):
        """Scan durable jobs only; an unregistered historical clear is not a job."""
        result = []
        for key, value in self.load()["incidents"].items():
            if (value.get("status") != "resolved" or value.get("alert_state", "cleared") != "cleared"
                    or value.get("recovery_verified_at") is not None):
                continue
            incident = Incident.model_validate(value)
            if incident.incident_id != key:
                raise ValueError("incident_store_identity_mismatch")
            state = incident.recovery_verification
            if state is not None and (
                state.status in {"pending", "running"}
                or state.next_retry_at is not None
            ):
                result.append(key)
        return result

    def _save_recovery(self, data, incident, state):
        if state.status != "running" and state.attempts and state.attempts[-1].outcome == "running":
            attempt = state.attempts[-1]
            attempt.outcome, attempt.reason, attempt.completed_at = "insufficient", state.reason, aware_utc(self.clock())
        incident.recovery_verification = RecoveryVerification.model_validate(state.model_dump())
        data["incidents"][incident.incident_id] = incident.model_dump(mode="json")
        self.save(data)

    @locked_update
    def claim_recovery_verification(
        self,
        incident_id,
        policy: RecoveryPolicy | None,
    ):
        """Commit the attempt before I/O; crashes consume a bounded attempt."""
        data = self.load()
        incident = Incident.model_validate(data["incidents"][incident_id])
        if incident.incident_id != incident_id:
            raise ValueError("incident_store_identity_mismatch")
        if incident.recovery_verified_at is not None:
            return None
        state = incident.recovery_verification
        if state is None:
            # Missing durable intent must not become an implicit upgrade backfill.
            return None
        now = aware_utc(self.clock())
        try:
            cleared_at = aware_utc(incident.cleared_at)
        except ValueError:
            cleared_at = None
        if cleared_at is None or cleared_at > now:
            state.status = "insufficient"
            state.reason = "clear_time_invalid"
            state.next_retry_at = None
            self._save_recovery(data, incident, state)
            return None
        if not self._recovery_context_current(data, incident) or state.cleared_at != incident.cleared_at:
            if state.status != "superseded":
                state.status, state.reason, state.next_retry_at = "superseded", "alert_changed", None
                self._save_recovery(data, incident, state)
            return None
        if policy is not None:
            policy = RecoveryPolicy.model_validate(policy.model_dump())
            # Clearing persists intent without importing service configuration.
            # Freeze the trusted policy when the first attempt is claimed.
            if state.policy is None and state.status == "pending" and not state.attempts:
                state.policy = policy
        if policy is None or state.policy is None:
            state.status, state.reason, state.next_retry_at = "insufficient", "recovery_capability_not_configured", None
            self._save_recovery(data, incident, state)
            return None
        if state.policy != policy:
            state.status, state.reason, state.next_retry_at = "insufficient", "configuration_changed", None
            self._save_recovery(data, incident, state)
            return None
        if state.status in {"verified", "superseded"}:
            return None
        if state.status == "running":
            attempt = state.attempts[-1]
            if attempt.lease_until > now:
                return None
            attempt.outcome, attempt.reason, attempt.completed_at = "abandoned", "lease_expired", now
            state.status, state.reason = "insufficient", "lease_expired"
        elif state.status != "pending" and state.next_retry_at is None:
            return None
        if len(state.attempts) >= policy.max_attempts:
            state.next_retry_at = None
            self._save_recovery(data, incident, state)
            return None
        if state.next_retry_at is not None and state.next_retry_at > now:
            return None
        state.attempts.append(RecoveryAttempt(
            token=uuid4().hex, started_at=now,
            lease_until=now + timedelta(seconds=policy.lease_seconds),
        ))
        state.status, state.reason, state.next_retry_at = "running", None, None
        self._save_recovery(data, incident, state)
        return incident

    @staticmethod
    def _validate_recovery_receipt(incident, state, receipt, now, *, success):
        attempt, policy = state.attempts[-1], state.policy
        if (receipt.incident_id != incident.incident_id or receipt.service != incident.service
                or receipt.cleared_at != incident.cleared_at or receipt.cleared_at != state.cleared_at
                or receipt.claim_token != attempt.token or receipt.target_id != policy.target_id
                or receipt.config_digest != policy.config_digest or receipt.started_at != attempt.started_at):
            raise ValueError("recovery_receipt_identity_mismatch")
        stamps = [receipt.completed_at] + [item.collected_at for item in receipt.samples]
        if any(not 0 <= (now - stamp).total_seconds() <= policy.evidence_max_age_seconds for stamp in stamps):
            raise ValueError("recovery_evidence_expired_or_future")
        if success:
            expected = [item.model_dump() for item in policy.required_checks]
            if (receipt.outcome != "success" or len(receipt.samples) != policy.successes_required
                    or any(item.http_status != policy.expected_status
                           or [check.model_dump(exclude={"matched"}) for check in item.checks] != expected
                           or not all(check.matched for check in item.checks) for item in receipt.samples)):
                raise ValueError("recovery_success_conditions_unconfirmed")
        elif len(receipt.samples) > policy.successes_required:
            raise ValueError("recovery_probe_count_exceeds_policy")

    @locked_update
    def record_recovery_verification(self, incident_id, evidence, verified_at, *, claim_token=None):
        """Accept only a fresh, complete receipt for the current durable claim."""
        verified_at = aware_utc(verified_at)
        if verified_at is None or not claim_token or not evidence or len(evidence) != 1:
            raise ValueError("claimed_timed_recovery_receipt_required")
        item = Evidence.model_validate(evidence[0])
        if len(item.detail) > 32768:
            raise ValueError("recovery_receipt_too_large")
        receipt = RecoveryReceipt.model_validate_json(item.detail)
        if (item.source != "recovery_http" or item.observation_id != claim_token
                or item.runtime_session_id is not None or item.collected_at != verified_at
                or receipt.completed_at != verified_at or receipt.claim_token != claim_token):
            raise ValueError("recovery_evidence_identity_mismatch")
        data = self.load()
        incident = Incident.model_validate(data["incidents"][incident_id])
        state = incident.recovery_verification
        if incident.recovery_verified_at is not None:
            known = next((old for old in incident.recovery_evidence if old.observation_id == claim_token), None)
            if known and RecoveryReceipt.model_validate_json(known.detail) == receipt:
                return False
            raise ValueError("recovery_already_verified")
        now = aware_utc(self.clock())
        if (not self._recovery_context_current(data, incident) or state is None or state.status != "running"
                or not state.attempts or state.attempts[-1].token != claim_token
                or state.attempts[-1].lease_until <= now):
            raise ValueError("recovery_claim_or_alert_changed")
        self._validate_recovery_receipt(incident, state, receipt, now, success=True)
        # Rebuild the allowlisted evidence instead of persisting caller free text.
        incident.recovery_evidence.append(Evidence(
            source="recovery_http", summary="注册业务探测的 HTTP 状态及全部业务条件已满足。",
            detail=receipt.model_dump_json(), collected_at=verified_at, observation_id=claim_token,
        ))
        incident.recovery_verified_at = verified_at
        attempt = state.attempts[-1]
        attempt.outcome, attempt.reason, attempt.completed_at = "success", "conditions_met", now
        state.status, state.reason, state.next_retry_at = "verified", "conditions_met", None
        self._save_recovery(data, incident, state)
        return True

    @locked_update
    def finish_recovery_verification(self, incident_id, claim_token, *, receipt=None, reason="internal_probe_error"):
        """Retain failed/unknown observations; never change alert or approval state."""
        data = self.load()
        incident = Incident.model_validate(data["incidents"][incident_id])
        state = incident.recovery_verification
        if (state is None or state.status != "running" or not state.attempts
                or state.attempts[-1].token != claim_token):
            return False
        now, attempt = aware_utc(self.clock()), state.attempts[-1]
        outcome = "insufficient"
        if not self._recovery_context_current(data, incident) or state.cleared_at != incident.cleared_at:
            state.status, reason, receipt = "superseded", "alert_changed", None
        elif attempt.lease_until <= now:
            outcome, reason, receipt = "abandoned", "lease_expired", None
        elif receipt is not None:
            try:
                receipt = RecoveryReceipt.model_validate(receipt.model_dump())
                self._validate_recovery_receipt(incident, state, receipt, now, success=False)
                if receipt.outcome == "success":
                    raise ValueError("success_requires_record_recovery_verification")
                outcome, reason = receipt.outcome, receipt.reason
            except ValueError:
                receipt, reason = None, "invalid_evidence"
        attempt.outcome, attempt.reason, attempt.completed_at, attempt.receipt = outcome, reason, now, receipt
        state.reason = reason
        if state.status != "superseded":
            state.status = "failed" if outcome == "failed" else "insufficient"
        state.next_retry_at = (
            now + timedelta(seconds=state.policy.retry_seconds)
            if state.status != "superseded" and len(state.attempts) < state.policy.max_attempts else None
        )
        self._save_recovery(data, incident, state)
        return True

    def resume_investigation(self, incident_id, *, manager=None, actor=None, reason=None, command_id=None):
        """Operator convenience delegates to the durable, audited reopen command."""
        if manager is None or manager.incident_store is not self:
            raise ValueError("resume_requires_runtime_manager_and_audited_reopen")
        from investigation.models import HumanCommand
        with manager.store.read() as db:
            latest = manager.store.latest_session(incident_id, db)
            if command_id:
                previous = db.execute("SELECT data FROM investigation_commands WHERE command_id=?", (command_id,)).fetchone()
                if previous:
                    existing = HumanCommand.model_validate_json(previous[0])
                    target = manager.store.get(existing.session_id, db)
                    if target.incident_id != incident_id:
                        raise ValueError("reopen_command_incident_conflict")
                    latest = target
        if latest is None:
            raise ValueError("incident_has_no_runtime_session")
        fields = {"session_id": latest.session_id, "actor": actor, "command_type": "reopen",
                  "payload": {"reason": reason}}
        if command_id:
            fields["command_id"] = command_id
        return manager.submit(HumanCommand(**fields))

    def get_active_incident(self, fingerprint, include_history=True):
        data=self.load()

        incident_id=data[
            "active_by_fingerprint"
        ].get(
            fingerprint
        )

        if incident_id is None:
            return None

        incident_data=data[
            "incidents"
        ].get(
            incident_id
        )

        if incident_data is None:
            return None

        if incident_data.get(
            "status"
        )=="resolved":
            return None

        return self._incident(incident_data, include_history)
    
    @locked_update
    def save_incident(self,fingerprint,incident):
        incident = Incident.model_validate(incident.model_dump())
        data=self.load()
        existing = data["incidents"].get(incident.incident_id)
        if existing:
            previous = Incident.model_validate(existing)
            previous_archives = previous.history_archives
            if any(reference not in incident.history_archives for reference in previous_archives):
                raise ValueError("incident_history_manifest_conflict")
            # Recovery fields belong to the atomic claim/receipt methods. A stale
            # Monitor or Runtime snapshot cannot erase an attempt or its evidence.
            incident.recovery_verification = previous.recovery_verification
            incident.recovery_verified_at = previous.recovery_verified_at
            incident.recovery_evidence = previous.recovery_evidence
        self._archive_history(incident)

        incident.fingerprint=fingerprint

        data["incidents"][
            incident.incident_id
        ]=incident.model_dump(
            mode="json"
        )

        if incident.status=="resolved":
            current_incident_id=data[
                "active_by_fingerprint"
            ].get(
                fingerprint
            )

            if current_incident_id==incident.incident_id:
                data[
                    "active_by_fingerprint"
                ].pop(
                    fingerprint,
                    None
                )

        else:
            data[
                "active_by_fingerprint"
            ][fingerprint]=incident.incident_id

        self.save(data)

    @locked_update
    def resolve_inactive_incidents(
        self,
        active_fingerprints,
        eligible_fingerprints=None,
        *,
        expected_incidents=None,
    ):
        data=self.load()

        resolved_incidents=[]

        active_map=data[
            "active_by_fingerprint"
        ]

        for fingerprint,incident_id in list(
            active_map.items()
        ):
            if fingerprint in active_fingerprints:
                continue
            if eligible_fingerprints is not None and fingerprint not in eligible_fingerprints:
                continue

            incident_data=data[
                "incidents"
            ].get(
                incident_id
            )

            if incident_data is None:
                active_map.pop(
                    fingerprint,
                    None
                )
                continue

            if expected_incidents is not None:
                expected = expected_incidents.get(fingerprint)
                latest = Incident.model_validate(incident_data)
                if (expected is None or latest.incident_id != expected.incident_id
                        or latest.service != expected.service or latest.alert_state != expected.alert_state
                        or latest.alerts != expected.alerts):
                    # Prometheus queries run outside the file lock. A concurrent
                    # fresh observation must win over the older clearing decision.
                    continue

            incident_data["status"]="resolved"
            incident_data["alert_state"] = "cleared"
            cleared_at = aware_utc(self.clock())
            incident_data["cleared_at"] = cleared_at.isoformat()
            # Clearing the monitoring signal does not attest business recovery.
            incident_data.setdefault("recovery_verified_at", None)
            # Commit intent with the clear so a restart can resume this episode,
            # while legacy records without intent remain historical observations.
            if incident_data.get("recovery_verification") is None:
                incident_data["recovery_verification"] = RecoveryVerification(
                    cleared_at=cleared_at,
                ).model_dump(mode="json")

            resolved_incidents.append(
                incident_id
            )

            active_map.pop(
                fingerprint,
                None
            )

        if resolved_incidents:
            self.save(data)

        return resolved_incidents
