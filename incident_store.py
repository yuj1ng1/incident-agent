"""MySQL Incident store with per-Incident row locks and durable archives."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json

from sqlalchemy import delete, inspect, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import OperationalError

from db import tables
from db.config import database_url as configured_database_url, get_engine
from models.incident import Evidence, HistoryArchive, Incident
from models.investigation import InvestigationRecord
from models.recovery import (
    RecoveryAttempt,
    RecoveryPolicy,
    RecoveryReceipt,
    RecoveryVerification,
    aware_utc,
    utc_now,
)


class RuntimeProjectionResult(str, Enum):
    APPLIED = "applied"
    UNCHANGED = "unchanged"
    ARCHIVED = "archived"
    MISSING = "missing"


def _utc_naive(value):
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_iso(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat()


def _document(value):
    return json.loads(value) if isinstance(value, str) else value


def _archive_bytes(body):
    return json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _archive_session_ids(value):
    return json.loads(value) if isinstance(value, str) else list(value)


class IncidentStore:
    def __init__(
        self,
        database_url=None,
        runtime_history_to_keep=3,
        *,
        read_only=False,
        clock=utc_now,
    ):
        if not isinstance(runtime_history_to_keep, int) or runtime_history_to_keep < 1:
            raise ValueError("runtime_history_retention_must_be_positive")
        self.database_url = database_url or configured_database_url()
        self.engine = get_engine(self.database_url)
        self.read_only = read_only
        self.clock = clock
        self.runtime_history_to_keep = runtime_history_to_keep
        try:
            available = inspect(self.engine).has_table("incidents")
        except OperationalError:
            if read_only:
                raise FileNotFoundError("incident_store_not_found") from None
            raise
        if not available:
            if read_only:
                raise FileNotFoundError("incident_store_not_found")
            raise ValueError("incident_store_not_migrated")

    @contextmanager
    def transaction(self):
        if self.read_only:
            raise PermissionError("incident_store_read_only")
        with self.engine.begin() as db:
            yield db

    @contextmanager
    def read(self):
        connection = self.engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        )
        try:
            connection.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
            connection.commit()
            with connection.begin():
                yield connection
        finally:
            if connection.in_transaction():
                connection.rollback()
            try:
                connection.exec_driver_sql("SET SESSION TRANSACTION READ WRITE")
                connection.commit()
            finally:
                connection.close()

    @staticmethod
    def create_empty_data():
        return {
            "version": 2,
            "incidents": {},
            "active_by_fingerprint": {},
        }

    def load(self):
        with self.read() as db:
            incidents = {}
            for row in db.execute(
                select(
                    tables.incidents.c.incident_id,
                    tables.incidents.c.data,
                ).order_by(tables.incidents.c.created_seq)
            ).mappings():
                incidents[row["incident_id"]] = _document(row["data"])
            active = {
                row["fingerprint"]: row["incident_id"]
                for row in db.execute(
                    select(
                        tables.incident_active.c.fingerprint,
                        tables.incident_active.c.incident_id,
                    )
                ).mappings()
            }
        if not incidents and not active:
            # Keep the same shape as the legacy empty JSON file.
            return self.create_empty_data()
        if any(
            not isinstance(value, dict) or "incident_id" not in value
            for value in incidents.values()
        ):
            raise ValueError("unsupported_or_corrupt_incident_store")
        return {
            "version": 2,
            "incidents": incidents,
            "active_by_fingerprint": active,
        }

    def save(self, data):
        """Legacy whole-store write used by imports and isolated tools."""
        if (
            isinstance(data, dict)
            and "version" not in data
            and all(
                isinstance(value, dict) and "incident_id" in value
                for value in data.values()
            )
        ):
            migrated = self.create_empty_data()
            for fingerprint, value in data.items():
                migrated["incidents"][value["incident_id"]] = value
                if value.get("status") != "resolved":
                    migrated["active_by_fingerprint"][fingerprint] = value["incident_id"]
            data = migrated
        if (
            not isinstance(data, dict)
            or data.get("version") != 2
            or not isinstance(data.get("incidents"), dict)
            or not isinstance(data.get("active_by_fingerprint"), dict)
        ):
            raise ValueError("unsupported_or_corrupt_incident_store")
        incidents = {
            str(key): Incident.model_validate(value)
            for key, value in data["incidents"].items()
        }
        with self.transaction() as db:
            existing = {
                row["incident_id"]
                for row in db.execute(
                    select(tables.incidents.c.incident_id).with_for_update()
                ).mappings()
            }
            for incident_id in sorted(existing - set(incidents)):
                db.execute(
                    delete(tables.incident_history_archives).where(
                        tables.incident_history_archives.c.incident_id
                        == incident_id
                    )
                )
                db.execute(
                    delete(tables.incident_active).where(
                        tables.incident_active.c.incident_id == incident_id
                    )
                )
                db.execute(
                    delete(tables.incidents).where(
                        tables.incidents.c.incident_id == incident_id
                    )
                )
            db.execute(delete(tables.incident_active))
            for incident_id, incident in incidents.items():
                self._upsert_incident(db, incident, incident.fingerprint)
            for fingerprint, incident_id in data["active_by_fingerprint"].items():
                if incident_id not in incidents:
                    raise ValueError("incident_store_identity_mismatch")
                db.execute(
                    tables.incident_active.insert().values(
                        fingerprint=fingerprint,
                        incident_id=incident_id,
                        updated_at=_utc_naive(self.clock()),
                    )
                )

    def active_incidents(self, include_history=True):
        with self.read() as db:
            rows = db.execute(
                select(tables.incidents)
                .join(
                    tables.incident_active,
                    tables.incident_active.c.incident_id
                    == tables.incidents.c.incident_id,
                )
                .order_by(tables.incidents.c.created_seq)
            ).mappings()
            return [
                self._incident(row, include_history, db)
                for row in rows
            ]

    def get_incident(self, incident_id, include_history=True):
        with self.read() as db:
            row = db.execute(
                select(tables.incidents).where(
                    tables.incidents.c.incident_id == incident_id
                )
            ).mappings().first()
            return self._incident(row, include_history, db) if row else None

    def list_incidents(self, *, status=None, service=None, limit=20, offset=0):
        """Bounded newest-first query; archives are never opened by a list view."""
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("incident_pagination_invalid")
        with self.read() as db:
            statement = select(tables.incidents)
            if status is not None:
                statement = statement.where(tables.incidents.c.status == status)
            if service is not None:
                statement = statement.where(tables.incidents.c.service == service)
            rows = db.execute(
                statement.order_by(tables.incidents.c.created_seq.desc())
                .limit(limit)
                .offset(offset)
            ).mappings()
            return [self._incident(row, False, db) for row in rows]

    def _incident(self, row, include_history, db=None):
        incident = Incident.model_validate(_document(row["data"]))
        if incident.incident_id != row["incident_id"]:
            raise ValueError("incident_store_identity_mismatch")
        if include_history and incident.history_archives:
            investigations, evidence = [], []
            seen = set()
            for reference in incident.history_archives:
                if seen.intersection(reference.session_ids):
                    raise ValueError("duplicate_incident_history_session")
                seen.update(reference.session_ids)
                archived = self._load_archive(
                    incident.incident_id, reference, db,
                )
                investigations.extend(archived["investigations"])
                evidence.extend(archived["evidence"])

            def stamp(value):
                if value is None:
                    return datetime.min.replace(tzinfo=timezone.utc)
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone(timedelta(hours=8)))
                return value.astimezone(timezone.utc)

            incident.investigations = sorted(
                investigations + incident.investigations,
                key=lambda record: stamp(record.investigated_at),
            )
            incident.evidence = sorted(
                evidence + incident.evidence,
                key=lambda item: stamp(item.collected_at),
            )
        return incident

    def _load_archive(self, incident_id, reference, db=None):
        if db is None:
            with self.read() as connection:
                return self._load_archive(incident_id, reference, connection)
        row = db.execute(
            select(
                tables.incident_history_archives.c.body,
                tables.incident_history_archives.c.session_ids,
                tables.incident_history_archives.c.incident_id,
            ).where(
                tables.incident_history_archives.c.sha256 == reference.sha256,
            )
        ).mappings().first()
        if row is None:
            raise FileNotFoundError("incident_history_archive_unavailable")
        body_text = row["body"]
        if hashlib.sha256(body_text.encode("utf-8")).hexdigest() != reference.sha256:
            raise ValueError("incident_history_archive_checksum_mismatch")
        body = json.loads(body_text)
        session_ids = _archive_session_ids(row["session_ids"])
        if (
            not isinstance(body, dict)
            or set(body) != {"schema_version", "incident_id", "investigations", "evidence"}
            or body["schema_version"] != 1
            or body["incident_id"] != incident_id
            or row["incident_id"] != incident_id
            or session_ids != reference.session_ids
            or not isinstance(body["investigations"], list)
            or not isinstance(body["evidence"], list)
        ):
            raise ValueError("incident_history_archive_identity_mismatch")
        investigations = [
            InvestigationRecord.model_validate(value)
            for value in body["investigations"]
        ]
        evidence = [Evidence.model_validate(value) for value in body["evidence"]]
        if (
            [record.session_id for record in investigations] != reference.session_ids
            or len(set(reference.session_ids)) != len(reference.session_ids)
            or any(
                item.runtime_session_id not in reference.session_ids
                for item in evidence
            )
        ):
            raise ValueError("incident_history_archive_records_mismatch")
        return {"investigations": investigations, "evidence": evidence}

    def _archive_history(self, incident, db):
        archived_ids = {
            session_id
            for reference in incident.history_archives
            for session_id in reference.session_ids
        }
        included = {
            record.session_id
            for record in incident.investigations
            if record.session_id in archived_ids
        }
        if included or any(
            item.runtime_session_id in archived_ids for item in incident.evidence
        ):
            expected_records, expected_evidence = [], []
            for reference in incident.history_archives:
                body = self._load_archive(incident.incident_id, reference, db)
                expected_records.extend(body["investigations"])
                expected_evidence.extend(body["evidence"])
            actual_records = [
                record
                for record in incident.investigations
                if record.session_id in archived_ids
            ]
            actual_evidence = [
                item
                for item in incident.evidence
                if item.runtime_session_id in archived_ids
            ]

            def contents(records):
                return sorted(
                    json.dumps(record.model_dump(mode="json"), sort_keys=True)
                    for record in records
                )

            if (
                contents(actual_records) != contents(expected_records)
                or contents(actual_evidence) != contents(expected_evidence)
            ):
                raise ValueError("archived_incident_history_is_immutable")
        incident.investigations = [
            record
            for record in incident.investigations
            if record.session_id not in archived_ids
        ]
        incident.evidence = [
            item
            for item in incident.evidence
            if item.runtime_session_id not in archived_ids
        ]
        runtime_records = [
            record for record in incident.investigations if record.session_id
        ]
        older = runtime_records[:-self.runtime_history_to_keep]
        if not older:
            return
        session_ids = [record.session_id for record in older]
        evidence = [
            item
            for item in incident.evidence
            if item.runtime_session_id in session_ids
        ]
        body = {
            "schema_version": 1,
            "incident_id": incident.incident_id,
            "investigations": [
                record.model_dump(mode="json") for record in older
            ],
            "evidence": [
                item.model_dump(mode="json") for item in evidence
            ],
        }
        content = _archive_bytes(body)
        sha256 = hashlib.sha256(content).hexdigest()
        reference = HistoryArchive(sha256=sha256, session_ids=session_ids)
        db.execute(
            mysql_insert(tables.incident_history_archives)
            .values(
                sha256=sha256,
                incident_id=incident.incident_id,
                session_ids=json.dumps(session_ids, ensure_ascii=False),
                body=content.decode("utf-8"),
                created_at=_utc_naive(self.clock()),
            )
            .prefix_with("IGNORE")
        )
        self._load_archive(incident.incident_id, reference, db)
        incident.history_archives.append(reference)
        incident.investigations = [
            record
            for record in incident.investigations
            if record.session_id not in session_ids
        ]
        incident.evidence = [
            item
            for item in incident.evidence
            if item.runtime_session_id not in session_ids
        ]

    def _upsert_incident(self, db, incident, fingerprint):
        incident.fingerprint = fingerprint
        state = incident.recovery_verification
        retry_at = state.next_retry_at if state is not None else None
        values = {
            "incident_id": incident.incident_id,
            "fingerprint": fingerprint,
            "service": incident.service,
            "status": incident.status,
            "alert_state": incident.alert_state,
            "cleared_at": _utc_naive(incident.cleared_at),
            "recovery_verified_at": _utc_naive(incident.recovery_verified_at),
            "recovery_status": state.status if state is not None else None,
            "recovery_next_retry_at": _utc_naive(retry_at),
            "data": incident.model_dump_json(),
            "updated_at": _utc_naive(self.clock()),
        }
        existing = db.execute(
            select(tables.incidents.c.created_seq).where(
                tables.incidents.c.incident_id == incident.incident_id
            )
        ).first()
        if existing is None:
            db.execute(tables.incidents.insert().values(**values))
        else:
            db.execute(
                update(tables.incidents)
                .where(tables.incidents.c.incident_id == incident.incident_id)
                .values(**values)
            )

    def _save_incident_in_tx(self, db, fingerprint, incident):
        incident = Incident.model_validate(incident.model_dump())
        previous_row = db.execute(
            select(tables.incidents.c.data)
            .where(tables.incidents.c.incident_id == incident.incident_id)
            .with_for_update()
        ).mappings().first()
        if previous_row is not None:
            previous = Incident.model_validate(_document(previous_row["data"]))
            if any(
                reference not in incident.history_archives
                for reference in previous.history_archives
            ):
                raise ValueError("incident_history_manifest_conflict")
            # Recovery fields belong to the atomic claim/receipt methods. A
            # stale Monitor or Runtime snapshot cannot erase them.
            incident.recovery_verification = previous.recovery_verification
            incident.recovery_verified_at = previous.recovery_verified_at
            incident.recovery_evidence = previous.recovery_evidence
        self._archive_history(incident, db)
        self._upsert_incident(db, incident, fingerprint)
        if incident.status == "resolved":
            db.execute(
                delete(tables.incident_active).where(
                    tables.incident_active.c.fingerprint == fingerprint,
                    tables.incident_active.c.incident_id == incident.incident_id,
                )
            )
        else:
            db.execute(
                delete(tables.incident_active).where(
                    tables.incident_active.c.incident_id == incident.incident_id,
                    tables.incident_active.c.fingerprint != fingerprint,
                )
            )
            statement = mysql_insert(tables.incident_active).values(
                fingerprint=fingerprint,
                incident_id=incident.incident_id,
                updated_at=_utc_naive(self.clock()),
            )
            db.execute(
                statement.on_duplicate_key_update(
                    incident_id=statement.inserted.incident_id,
                    updated_at=statement.inserted.updated_at,
                )
            )

    def save_observation(self, fingerprint, incident):
        """Merge monitor observations with the latest Worker-owned fields."""
        with self.transaction() as db:
            row = db.execute(
                select(tables.incidents)
                .where(tables.incidents.c.incident_id == incident.incident_id)
                .with_for_update()
            ).mappings().first()
            if row is not None:
                latest = Incident.model_validate(_document(row["data"]))
                latest.alerts = incident.alerts
                latest.alert_state = incident.alert_state
                latest.related_alerts = incident.related_alerts
                latest.severity = incident.severity
                incident = latest
            self._save_incident_in_tx(db, fingerprint, incident)

    def apply_runtime_projection(self, session, checkpoint):
        """Return an explicit durable outcome; missing data is never success."""
        from investigation.models import SessionStatus as S

        with self.transaction() as db:
            row = db.execute(
                select(tables.incidents)
                .where(tables.incidents.c.incident_id == session.incident_id)
                .with_for_update()
            ).mappings().first()
            if row is None:
                return RuntimeProjectionResult.MISSING
            incident = Incident.model_validate(_document(row["data"]))
            if (
                incident.fingerprint != session.alert_fingerprint
                or incident.service != session.service
            ):
                raise ValueError("incident_projection_identity_mismatch")
            previous_data = incident.model_dump()
            projected_record = self._runtime_record(session, checkpoint)
            projected_evidence = self._runtime_evidence(session, checkpoint)
            references = [
                reference
                for reference in incident.history_archives
                if session.session_id in reference.session_ids
            ]
            if references:
                if len(references) != 1:
                    raise ValueError("duplicate_incident_history_session")
                try:
                    archived = self._load_archive(
                        incident.incident_id, references[0], db,
                    )
                except FileNotFoundError:
                    raise ValueError(
                        "incident_history_archive_unavailable"
                    ) from None
                actual = next(
                    record
                    for record in archived["investigations"]
                    if record.session_id == session.session_id
                )
                if projected_record is None or actual != projected_record:
                    raise ValueError(
                        "incident_history_archive_projection_mismatch"
                    )
                known = {
                    item.observation_id: item
                    for item in archived["evidence"]
                    if item.runtime_session_id == session.session_id
                }
                if any(
                    known.get(item.observation_id) != item
                    for item in projected_evidence
                ):
                    raise ValueError(
                        "incident_history_archive_projection_mismatch"
                    )
                return RuntimeProjectionResult.ARCHIVED
            if session.budget_group_id == session.session_id and not session.attempt_count:
                incident.consecutive_investigation_failures = 0
                incident.investigation_suspended_reason = None
                incident.last_attempted_at = None
            known = {
                item.observation_id: item
                for item in incident.evidence
                if item.observation_id
            }
            for item in projected_evidence:
                previous = known.get(item.observation_id)
                if previous is not None and previous != item:
                    raise ValueError("incident_projection_evidence_mismatch")
                if previous is None:
                    incident.evidence.append(item)
                    known[item.observation_id] = item
            if session.attempt_count:
                incident.last_attempted_at = session.completed_at or session.updated_at
            incident.investigation_suspended_reason = (
                session.last_error_category
                if session.status == S.PAUSED
                else (
                    "exhausted_requires_human_reauthorization"
                    if session.status == S.EXHAUSTED
                    else (
                        "cancelled_requires_human_reopen"
                        if session.status == S.CANCELLED
                        else None
                    )
                )
            )
            if session.status == S.FAILED:
                from reinvestigation import RETRYABLE_FAILURES

                if session.last_error_category not in RETRYABLE_FAILURES:
                    incident.investigation_suspended_reason = (
                        "non_retryable_failure_requires_human_reopen"
                    )
            previous = next(
                (
                    record
                    for record in incident.investigations
                    if record.session_id == session.session_id
                ),
                None,
            )
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
                self._save_incident_in_tx(db, session.alert_fingerprint, incident)
                return RuntimeProjectionResult.APPLIED
            return RuntimeProjectionResult.UNCHANGED

    @staticmethod
    def _runtime_record(session, checkpoint):
        from investigation.models import SessionStatus as S

        if session.status not in {S.COMPLETED, S.EXHAUSTED, S.FAILED}:
            return None
        result = checkpoint.get("result")
        if not result and session.status in {S.FAILED, S.EXHAUSTED}:
            result = {
                "status": "model_incomplete",
                "answer": "调查停止："
                + (session.last_error_category or session.status.value),
            }
        if not result:
            raise ValueError("completed_projection_result_missing")
        return InvestigationRecord(
            session_id=session.session_id,
            investigated_at=session.completed_at or session.updated_at,
            status=result["status"],
            alert_value=checkpoint.get("investigated_value"),
            answer=result["answer"],
            model_rounds=session.total_model_requests,
            tool_calls=session.total_tool_executions,
            tool_trace=checkpoint.get("trace", []),
        )

    @staticmethod
    def _runtime_evidence(session, checkpoint):
        return [
            Evidence(
                source=record["name"],
                summary=f"{record['name']}查询结果（采集时观测）",
                detail=json.dumps(record["result"], ensure_ascii=False),
                collected_at=record["collected_at"],
                runtime_session_id=session.session_id,
                observation_id=record["observation_id"],
            )
            for record in checkpoint.get("records", [])
            if record.get("success")
            and record.get("result", {}).get("status") == "ok"
        ]

    def get_or_migrate_active(self, alert, fingerprint):
        with self.transaction() as db:
            incident = self._active_incident_in_tx(db, fingerprint)
            if incident is not None or alert.identity_labels:
                return incident
            if alert.alertname != "HighHttp5xxRate":
                return None
            from fingerprint import legacy_fingerprint

            old_fingerprint = legacy_fingerprint(alert)
            old = self._active_incident_in_tx(db, old_fingerprint)
            if (
                old is None
                or not old.alerts
                or any(
                    event.source != "legacy"
                    or event.service != alert.service
                    or event.metric != alert.metric
                    for event in old.alerts
                )
            ):
                return None
            old.fingerprint = fingerprint
            self._upsert_incident(db, old, fingerprint)
            db.execute(
                delete(tables.incident_active).where(
                    tables.incident_active.c.fingerprint == old_fingerprint
                )
            )
            statement = mysql_insert(tables.incident_active).values(
                fingerprint=fingerprint,
                incident_id=old.incident_id,
                updated_at=_utc_naive(self.clock()),
            )
            db.execute(
                statement.on_duplicate_key_update(
                    incident_id=statement.inserted.incident_id,
                    updated_at=statement.inserted.updated_at,
                )
            )
            return old

    def _active_incident_in_tx(self, db, fingerprint):
        row = db.execute(
            select(tables.incident_active.c.incident_id)
            .where(tables.incident_active.c.fingerprint == fingerprint)
            .with_for_update()
        ).first()
        if row is None:
            return None
        incident_row = db.execute(
            select(tables.incidents)
            .where(tables.incidents.c.incident_id == row[0])
            .with_for_update()
        ).mappings().first()
        if incident_row is None or incident_row["status"] == "resolved":
            return None
        return Incident.model_validate(_document(incident_row["data"]))

    def _recovery_context_current(self, db, incident):
        if (
            incident.status != "resolved"
            or incident.alert_state != "cleared"
            or not incident.fingerprint
            or incident.cleared_at is None
        ):
            return False
        active = db.execute(
            select(tables.incident_active.c.incident_id).where(
                tables.incident_active.c.fingerprint == incident.fingerprint
            )
        ).first()
        if active is not None:
            return False
        newest = db.execute(
            select(tables.incidents.c.incident_id)
            .where(tables.incidents.c.fingerprint == incident.fingerprint)
            .order_by(tables.incidents.c.created_seq.desc())
            .limit(1)
        ).first()
        return newest is not None and newest[0] == incident.incident_id

    def recovery_candidates(self):
        with self.read() as db:
            rows = db.execute(
                select(tables.incidents).where(
                    tables.incidents.c.status == "resolved",
                    tables.incidents.c.alert_state == "cleared",
                    tables.incidents.c.recovery_verified_at.is_(None),
                )
            ).mappings()
            result = []
            for row in rows:
                incident = Incident.model_validate(_document(row["data"]))
                state = incident.recovery_verification
                if state is not None and (
                    state.status in {"pending", "running"}
                    or state.next_retry_at is not None
                ):
                    result.append(incident.incident_id)
            return result

    def _save_recovery(self, db, incident, state):
        if (
            state.status != "running"
            and state.attempts
            and state.attempts[-1].outcome == "running"
        ):
            attempt = state.attempts[-1]
            attempt.outcome = "insufficient"
            attempt.reason = state.reason
            attempt.completed_at = aware_utc(self.clock())
        incident.recovery_verification = RecoveryVerification.model_validate(
            state.model_dump()
        )
        self._upsert_incident(db, incident, incident.fingerprint)

    def claim_recovery_verification(
        self,
        incident_id,
        policy: RecoveryPolicy | None,
    ):
        """Commit the attempt before I/O; crashes consume a bounded attempt."""
        with self.transaction() as db:
            row = db.execute(
                select(tables.incidents)
                .where(tables.incidents.c.incident_id == incident_id)
                .with_for_update()
            ).mappings().first()
            if row is None:
                raise ValueError("incident_store_identity_mismatch")
            incident = Incident.model_validate(_document(row["data"]))
            if incident.incident_id != incident_id:
                raise ValueError("incident_store_identity_mismatch")
            if incident.recovery_verified_at is not None:
                return None
            state = incident.recovery_verification
            if state is None:
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
                self._save_recovery(db, incident, state)
                return None
            if (
                not self._recovery_context_current(db, incident)
                or state.cleared_at != incident.cleared_at
            ):
                if state.status != "superseded":
                    state.status, state.reason, state.next_retry_at = (
                        "superseded",
                        "alert_changed",
                        None,
                    )
                    self._save_recovery(db, incident, state)
                return None
            if policy is not None:
                policy = RecoveryPolicy.model_validate(policy.model_dump())
                if (
                    state.policy is None
                    and state.status == "pending"
                    and not state.attempts
                ):
                    state.policy = policy
            if policy is None or state.policy is None:
                state.status, state.reason, state.next_retry_at = (
                    "insufficient",
                    "recovery_capability_not_configured",
                    None,
                )
                self._save_recovery(db, incident, state)
                return None
            if state.policy != policy:
                state.status, state.reason, state.next_retry_at = (
                    "insufficient",
                    "configuration_changed",
                    None,
                )
                self._save_recovery(db, incident, state)
                return None
            if state.status in {"verified", "superseded"}:
                return None
            if state.status == "running":
                attempt = state.attempts[-1]
                if attempt.lease_until > now:
                    return None
                attempt.outcome, attempt.reason, attempt.completed_at = (
                    "abandoned",
                    "lease_expired",
                    now,
                )
                state.status, state.reason = "insufficient", "lease_expired"
            elif state.status != "pending" and state.next_retry_at is None:
                return None
            if len(state.attempts) >= policy.max_attempts:
                state.next_retry_at = None
                self._save_recovery(db, incident, state)
                return None
            if state.next_retry_at is not None and state.next_retry_at > now:
                return None
            from uuid import uuid4

            state.attempts.append(
                RecoveryAttempt(
                    token=uuid4().hex,
                    started_at=now,
                    lease_until=now + timedelta(seconds=policy.lease_seconds),
                )
            )
            state.status, state.reason, state.next_retry_at = "running", None, None
            self._save_recovery(db, incident, state)
            return incident

    @staticmethod
    def _validate_recovery_receipt(incident, state, receipt, now, *, success):
        attempt, policy = state.attempts[-1], state.policy
        if (
            receipt.incident_id != incident.incident_id
            or receipt.service != incident.service
            or receipt.cleared_at != incident.cleared_at
            or receipt.cleared_at != state.cleared_at
            or receipt.claim_token != attempt.token
            or receipt.target_id != policy.target_id
            or receipt.config_digest != policy.config_digest
            or receipt.started_at != attempt.started_at
        ):
            raise ValueError("recovery_receipt_identity_mismatch")
        stamps = [receipt.completed_at] + [
            item.collected_at for item in receipt.samples
        ]
        if any(
            not 0 <= (now - stamp).total_seconds() <= policy.evidence_max_age_seconds
            for stamp in stamps
        ):
            raise ValueError("recovery_evidence_expired_or_future")
        if success:
            expected = [item.model_dump() for item in policy.required_checks]
            if (
                receipt.outcome != "success"
                or len(receipt.samples) != policy.successes_required
                or any(
                    item.http_status != policy.expected_status
                    or [
                        check.model_dump(exclude={"matched"})
                        for check in item.checks
                    ]
                    != expected
                    or not all(check.matched for check in item.checks)
                    for item in receipt.samples
                )
            ):
                raise ValueError("recovery_success_conditions_unconfirmed")
        elif len(receipt.samples) > policy.successes_required:
            raise ValueError("recovery_probe_count_exceeds_policy")

    def record_recovery_verification(
        self,
        incident_id,
        evidence,
        verified_at,
        *,
        claim_token=None,
    ):
        verified_at = aware_utc(verified_at)
        if verified_at is None or not claim_token or not evidence or len(evidence) != 1:
            raise ValueError("claimed_timed_recovery_receipt_required")
        item = Evidence.model_validate(evidence[0])
        if len(item.detail) > 32768:
            raise ValueError("recovery_receipt_too_large")
        receipt = RecoveryReceipt.model_validate_json(item.detail)
        if (
            item.source != "recovery_http"
            or item.observation_id != claim_token
            or item.runtime_session_id is not None
            or item.collected_at != verified_at
            or receipt.completed_at != verified_at
            or receipt.claim_token != claim_token
        ):
            raise ValueError("recovery_evidence_identity_mismatch")
        with self.transaction() as db:
            row = db.execute(
                select(tables.incidents)
                .where(tables.incidents.c.incident_id == incident_id)
                .with_for_update()
            ).mappings().first()
            if row is None:
                raise ValueError("incident_store_identity_mismatch")
            incident = Incident.model_validate(_document(row["data"]))
            state = incident.recovery_verification
            if incident.recovery_verified_at is not None:
                known = next(
                    (
                        old
                        for old in incident.recovery_evidence
                        if old.observation_id == claim_token
                    ),
                    None,
                )
                if known and RecoveryReceipt.model_validate_json(known.detail) == receipt:
                    return False
                raise ValueError("recovery_already_verified")
            now = aware_utc(self.clock())
            if (
                not self._recovery_context_current(db, incident)
                or state is None
                or state.status != "running"
                or not state.attempts
                or state.attempts[-1].token != claim_token
                or state.attempts[-1].lease_until <= now
            ):
                raise ValueError("recovery_claim_or_alert_changed")
            self._validate_recovery_receipt(incident, state, receipt, now, success=True)
            incident.recovery_evidence.append(
                Evidence(
                    source="recovery_http",
                    summary="注册业务探测的 HTTP 状态及全部业务条件已满足。",
                    detail=receipt.model_dump_json(),
                    collected_at=verified_at,
                    observation_id=claim_token,
                )
            )
            incident.recovery_verified_at = verified_at
            attempt = state.attempts[-1]
            attempt.outcome, attempt.reason, attempt.completed_at = (
                "success",
                "conditions_met",
                now,
            )
            state.status, state.reason, state.next_retry_at = (
                "verified",
                "conditions_met",
                None,
            )
            self._save_recovery(db, incident, state)
            return True

    def finish_recovery_verification(
        self,
        incident_id,
        claim_token,
        *,
        receipt=None,
        reason="internal_probe_error",
    ):
        """Retain failed/unknown observations; never change alert or approval state."""
        with self.transaction() as db:
            row = db.execute(
                select(tables.incidents)
                .where(tables.incidents.c.incident_id == incident_id)
                .with_for_update()
            ).mappings().first()
            if row is None:
                raise ValueError("incident_store_identity_mismatch")
            incident = Incident.model_validate(_document(row["data"]))
            state = incident.recovery_verification
            if (
                state is None
                or state.status != "running"
                or not state.attempts
                or state.attempts[-1].token != claim_token
            ):
                return False
            now, attempt = aware_utc(self.clock()), state.attempts[-1]
            outcome = "insufficient"
            if (
                not self._recovery_context_current(db, incident)
                or state.cleared_at != incident.cleared_at
            ):
                state.status, reason, receipt = "superseded", "alert_changed", None
            elif attempt.lease_until <= now:
                outcome, reason, receipt = "abandoned", "lease_expired", None
            elif receipt is not None:
                try:
                    receipt = RecoveryReceipt.model_validate(receipt.model_dump())
                    self._validate_recovery_receipt(
                        incident, state, receipt, now, success=False,
                    )
                    if receipt.outcome == "success":
                        raise ValueError(
                            "success_requires_record_recovery_verification"
                        )
                    outcome, reason = receipt.outcome, receipt.reason
                except ValueError:
                    receipt, reason = None, "invalid_evidence"
            attempt.outcome, attempt.reason, attempt.completed_at, attempt.receipt = (
                outcome,
                reason,
                now,
                receipt,
            )
            state.reason = reason
            if state.status != "superseded":
                state.status = "failed" if outcome == "failed" else "insufficient"
            state.next_retry_at = (
                now + timedelta(seconds=state.policy.retry_seconds)
                if state.status != "superseded"
                and len(state.attempts) < state.policy.max_attempts
                else None
            )
            self._save_recovery(db, incident, state)
            return True

    def resolve_inactive_incidents(
        self,
        active_fingerprints,
        eligible_fingerprints=None,
        *,
        expected_incidents=None,
    ):
        with self.transaction() as db:
            rows = db.execute(
                select(tables.incident_active).with_for_update()
            ).mappings().all()
            resolved = []
            for row in rows:
                fingerprint = row["fingerprint"]
                incident_id = row["incident_id"]
                if fingerprint in active_fingerprints:
                    continue
                if (
                    eligible_fingerprints is not None
                    and fingerprint not in eligible_fingerprints
                ):
                    continue
                incident_row = db.execute(
                    select(tables.incidents)
                    .where(tables.incidents.c.incident_id == incident_id)
                    .with_for_update()
                ).mappings().first()
                if incident_row is None:
                    db.execute(
                        delete(tables.incident_active).where(
                            tables.incident_active.c.fingerprint == fingerprint
                        )
                    )
                    continue
                incident = Incident.model_validate(_document(incident_row["data"]))
                if expected_incidents is not None:
                    expected = expected_incidents.get(fingerprint)
                    if (
                        expected is None
                        or incident.incident_id != expected.incident_id
                        or incident.service != expected.service
                        or incident.alert_state != expected.alert_state
                        or incident.alerts != expected.alerts
                    ):
                        continue
                incident.status = "resolved"
                incident.alert_state = "cleared"
                cleared_at = aware_utc(self.clock())
                incident.cleared_at = cleared_at
                if incident.recovery_verified_at is None:
                    if incident.recovery_verification is None:
                        incident.recovery_verification = RecoveryVerification(
                            cleared_at=cleared_at,
                        )
                self._upsert_incident(db, incident, fingerprint)
                db.execute(
                    delete(tables.incident_active).where(
                        tables.incident_active.c.fingerprint == fingerprint
                    )
                )
                resolved.append(incident_id)
            return resolved

    def resume_investigation(
        self,
        incident_id,
        *,
        manager=None,
        actor=None,
        reason=None,
        command_id=None,
    ):
        """Operator convenience delegates to the durable, audited reopen command."""
        if manager is None or manager.incident_store is not self:
            raise ValueError("resume_requires_runtime_manager_and_audited_reopen")
        from investigation.models import HumanCommand

        with manager.store.read() as db:
            latest = manager.store.latest_session(incident_id, db)
            if command_id:
                existing = manager.store.command(db, command_id)
                if existing:
                    target = manager.store.get(existing.session_id, db)
                    if target.incident_id != incident_id:
                        raise ValueError("reopen_command_incident_conflict")
                    latest = target
        if latest is None:
            raise ValueError("incident_has_no_runtime_session")
        fields = {
            "session_id": latest.session_id,
            "actor": actor,
            "command_type": "reopen",
            "payload": {"reason": reason},
        }
        if command_id:
            fields["command_id"] = command_id
        return manager.submit(HumanCommand(**fields))

    def get_active_incident(self, fingerprint, include_history=True):
        with self.read() as db:
            incident_id = db.execute(
                select(tables.incident_active.c.incident_id).where(
                    tables.incident_active.c.fingerprint == fingerprint
                )
            ).scalar_one_or_none()
            if incident_id is None:
                return None
            row = db.execute(
                select(tables.incidents).where(
                    tables.incidents.c.incident_id == incident_id
                )
            ).mappings().first()
            if row is None or row["status"] == "resolved":
                return None
            return self._incident(row, include_history, db)

    def save_incident(self, fingerprint, incident):
        with self.transaction() as db:
            self._save_incident_in_tx(db, fingerprint, incident)
