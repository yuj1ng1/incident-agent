import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps
import threading

from storage_lock import FileLock

from models.incident import Incident


def locked_update(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
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
    def __init__(self,file_path):
        self.file_path=Path(file_path)
        self._thread_lock = threading.RLock()
        self._lock_depth = threading.local()

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

    def active_incidents(self):
        data = self.load()
        return [
            Incident.model_validate(data["incidents"][incident_id])
            for incident_id in data["active_by_fingerprint"].values()
            if incident_id in data["incidents"]
        ]

    def get_incident(self, incident_id):
        value = self.load()["incidents"].get(incident_id)
        return Incident.model_validate(value) if value is not None else None

    @locked_update
    def save_observation(self, fingerprint, incident):
        """Merge monitor observations with the latest Worker-owned fields."""
        latest = self.get_incident(incident.incident_id)
        if latest is not None:
            latest.alerts = incident.alerts
            latest.alert_state = incident.alert_state
            latest.related_alerts = incident.related_alerts
            latest.severity = incident.severity
            incident = latest
        self.save_incident(fingerprint, incident)

    @locked_update
    def apply_runtime_projection(self, session, checkpoint):
        """Idempotent outbox consumer; never overwrite monitor alert lifecycle."""
        from models.incident import Evidence
        from models.investigation import InvestigationRecord
        from investigation.models import SessionStatus as S
        incident = self.get_incident(session.incident_id)
        if incident is None:
            return
        previous_data = incident.model_dump()
        known = {item.observation_id for item in incident.evidence if item.observation_id}
        for record in checkpoint.get("records", []):
            if (
                record.get("success")
                and record.get("result", {}).get("status") == "ok"
                and record["observation_id"] not in known
            ):
                incident.evidence.append(Evidence(
                    source=record["name"],
                    summary=f"{record['name']}查询结果（采集时观测）",
                    detail=json.dumps(record["result"], ensure_ascii=False),
                    collected_at=record["collected_at"],
                    runtime_session_id=session.session_id,
                    observation_id=record["observation_id"],
                ))
                known.add(record["observation_id"])
        if session.attempt_count:
            incident.last_attempted_at = session.updated_at
        incident.investigation_suspended_reason = (
            session.last_error_category if session.status == S.PAUSED else None
        )
        result = checkpoint.get("result")
        if (
            result
            and session.status in {S.COMPLETED, S.EXHAUSTED, S.FAILED}
            and not any(r.session_id == session.session_id for r in incident.investigations)
        ):
            stamp = session.completed_at or session.updated_at
            incident.investigations.append(InvestigationRecord(
                session_id=session.session_id,
                investigated_at=stamp,
                status=result["status"],
                alert_value=checkpoint.get("investigated_value"),
                answer=result["answer"],
                model_rounds=session.total_model_requests,
                tool_calls=session.total_tool_executions,
                tool_trace=checkpoint.get("trace", []),
            ))
            if result["success"]:
                incident.consecutive_investigation_failures = 0
                incident.last_investigated_at = stamp
                incident.last_investigated_value = checkpoint.get("investigated_value")
                incident.last_investigated_state = checkpoint.get("investigated_state")
            else:
                incident.consecutive_investigation_failures += 1
        if incident.model_dump() != previous_data:
            self.save_incident(session.alert_fingerprint, incident)

    @locked_update
    def get_or_migrate_active(self, alert, fingerprint):
        incident = self.get_active_incident(fingerprint)
        if incident is not None or alert.identity_labels:
            return incident
        # Only migrate unambiguous service-aggregated legacy HTTP alerts.
        if alert.alertname != "HighHttp5xxRate":
            return None
        from fingerprint import legacy_fingerprint
        old_fingerprint = legacy_fingerprint(alert)
        old = self.get_active_incident(old_fingerprint)
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

    @locked_update
    def resume_investigation(self, incident_id):
        """Operator call only, never an Agent tool; does not clear alert/recovery state."""
        data = self.load()
        incident = Incident.model_validate(data["incidents"][incident_id])
        incident.consecutive_investigation_failures = 0
        incident.investigation_suspended_reason = None
        incident.last_attempted_at = None
        data["incidents"][incident_id] = incident.model_dump(mode="json")
        self.save(data)

    def get_active_incident(self, fingerprint):
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

        return Incident.model_validate(
            incident_data
        )
    
    @locked_update
    def save_incident(self,fingerprint,incident):
        incident = Incident.model_validate(incident.model_dump())
        data=self.load()

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
