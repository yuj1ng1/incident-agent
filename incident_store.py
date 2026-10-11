import json
import hashlib
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps
import threading
from enum import Enum

from storage_lock import FileLock

from models.incident import Evidence, HistoryArchive, Incident
from models.investigation import InvestigationRecord


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
    def __init__(self,file_path, runtime_history_to_keep=3, *, read_only=False):
        if not isinstance(runtime_history_to_keep, int) or runtime_history_to_keep < 1:
            raise ValueError("runtime_history_retention_must_be_positive")
        self.file_path=Path(file_path)
        self.read_only = read_only
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
        content=self.file_path.read_text(
            encoding="utf-8"
        )

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
            os.replace(temporary, self.file_path)
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

    @locked_update
    def record_recovery_verification(self, incident_id, evidence, verified_at):
        if verified_at.tzinfo is None or not evidence:
            raise ValueError("explicit_timed_recovery_evidence_required")
        data = self.load()
        incident = Incident.model_validate(data["incidents"][incident_id])
        if incident.alert_state != "cleared":
            raise ValueError("alert_not_cleared")
        incident.recovery_verified_at = verified_at.astimezone(timezone.utc)
        incident.recovery_evidence = evidence
        data["incidents"][incident_id] = incident.model_dump(mode="json")
        self.save(data)

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
            previous_archives = Incident.model_validate(existing).history_archives
            if any(reference not in incident.history_archives for reference in previous_archives):
                raise ValueError("incident_history_manifest_conflict")
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
    def resolve_inactive_incidents(self,active_fingerprints, eligible_fingerprints=None):
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

            incident_data["status"]="resolved"
            incident_data["alert_state"] = "cleared"
            incident_data["cleared_at"] = datetime.now(timezone.utc).isoformat()
            # Clearing the monitoring signal does not attest business recovery.
            incident_data.setdefault("recovery_verified_at", None)

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
