"""Read-only legacy snapshots and redacted diagnostics for migration tools."""
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from pydantic import ValidationError
from sqlalchemy import BigInteger, Integer, SmallInteger

from db import tables
from db.import_sqlite import ROWS, WORK_PREFIXES, row_values
from investigation.models import InvestigationSession, SessionStatus
from investigation.state import TERMINAL
from models.incident import Evidence, HistoryArchive, Incident
from models.investigation import InvestigationRecord
from remediation.store import StoreError, validate_document


RUNTIME_TABLES = tuple(table.name for table, _, _ in ROWS) + (
    "runtime_metadata", "runtime_work",
)
INCIDENT_TABLES = ("incidents", "incident_active", "incident_history_archives")
KEYS = {
    "investigation_sessions": ("session_id",),
    "investigation_checkpoints": ("checkpoint_id",),
    "investigation_commands": ("command_id",),
    "human_messages": ("command_id",),
    "human_requests": ("request_id",),
    "investigation_events": ("event_id",),
    "runtime_metadata": ("meta_key",),
    "runtime_work": ("kind", "session_id"),
    "runtime_coordination": ("name",),
    "incidents": ("incident_id",),
    "incident_active": ("fingerprint",),
    "incident_history_archives": ("sha256",),
    "remediation_state": ("id",),
}


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def digest(value):
    try:
        raw = encode(value)
    except (ValueError, TypeError):
        raw = repr(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("nonfinite_json_number")


def decode(raw):
    return json.loads(
        raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant,
    )


def row_key(table, row):
    return tuple(row[key] for key in KEYS[table])


def unit_for(table, row):
    if table == "remediation_state":
        return ("remediation", "1")
    if table in INCIDENT_TABLES:
        return ("incident", row.get("incident_id"))
    if "session_id" in row:
        return ("runtime", row["session_id"])
    return (table, row.get("meta_key"))


@dataclass
class Problem:
    code: str
    table: str
    record: object = None
    unit: tuple | None = None
    severity: str = "error"
    field: str | None = None
    expected: int | None = None
    actual: int | None = None

    def public(self):
        result = {"code": self.code, "table": self.table, "severity": self.severity}
        if self.record is not None:
            # IDs, file names and validation inputs can themselves contain secrets.
            result["record_sha256"] = digest(self.record)
        if self.field is not None:
            result["field"] = self.field
        for name in ("expected", "actual"):
            value = getattr(self, name)
            if type(value) is int:
                result[name] = value
            elif value is not None:
                result[name + "_type"] = type(value).__name__
        return result


def public_problems(problems):
    """Report one finding when source and target contain the same legacy gap."""
    unique = {}
    for problem in problems:
        record = problem.public()
        unique.setdefault(encode(record), record)
    return list(unique.values())


@dataclass
class Snapshot:
    kind: str
    rows: dict[str, list[dict]] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)
    file_hashes: list[str] = field(default_factory=list)

    def problem(self, code, table, record=None, *, unit=None, severity="error"):
        self.problems.append(Problem(code, table, record, unit, severity))

    def add(self, table, row):
        # Reject values that MySQL would truncate before opening a write txn.
        for column in tables.metadata.tables[table].columns:
            value = row.get(column.name)
            if column.computed is not None:
                continue
            if not column.nullable and value is None:
                self.problem("required_column_missing", table, row_key(table, row),
                             unit=unit_for(table, row))
                return
            length = getattr(column.type, "length", None)
            too_long = length and isinstance(value, str) and len(value) > length
            limits = {"MEDIUMTEXT": 16777215, "TEXT": 65535}
            if isinstance(value, str) and type(column.type).__name__ in limits:
                too_long = len(value.encode("utf-8")) > limits[type(column.type).__name__]
            if too_long:
                self.problem("column_value_too_long", table, row_key(table, row),
                             unit=unit_for(table, row))
                return
            if isinstance(column.type, Integer) and isinstance(value, int):
                bits = 64 if isinstance(column.type, BigInteger) else (
                    16 if isinstance(column.type, SmallInteger) else 32
                )
                if not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
                    self.problem("integer_value_out_of_range", table, row_key(table, row),
                                 unit=unit_for(table, row))
                    return
        self.rows.setdefault(table, []).append(row)


def read_file(path, snapshot):
    raw = Path(path).read_bytes()
    snapshot.file_hashes.append(hashlib.sha256(raw).hexdigest())
    return raw


def _utc(value):
    if value is None:
        return None
    # Incident v1 used local naive timestamps. Match IncidentStore hydration.
    if value.utcoffset() is None:
        from datetime import timedelta
        value = value.replace(tzinfo=timezone(timedelta(hours=8)))
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def archive_document(raw, sha256):
    """Validate exactly the bytes and manifest IncidentStore will later read."""
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError("archive_checksum_mismatch")
    body = decode(raw.decode("utf-8"))
    if not isinstance(body, dict) or set(body) != {
        "schema_version", "incident_id", "investigations", "evidence",
    } or type(body["schema_version"]) is not int or body["schema_version"] != 1:
        raise ValueError("archive_schema_invalid")
    if not isinstance(body["incident_id"], str) or not body["incident_id"]:
        raise ValueError("archive_identity_invalid")
    investigations = [InvestigationRecord.model_validate(row) for row in body["investigations"]]
    evidence = [Evidence.model_validate(row) for row in body["evidence"]]
    session_ids = [row.session_id for row in investigations]
    if not session_ids or None in session_ids or len(set(session_ids)) != len(session_ids):
        raise ValueError("archive_session_ids_invalid")
    if any(row.runtime_session_id not in session_ids for row in evidence):
        raise ValueError("archive_evidence_session_mismatch")
    return body, session_ids


def read_incidents(source, *, history_dir=None):
    source = Path(source).resolve(strict=True)
    result = Snapshot("incidents", {table: [] for table in INCIDENT_TABLES})
    try:
        data = decode(read_file(source, result))
        if not isinstance(data, dict):
            raise ValueError()
        if "version" not in data:
            records, active = {}, {}
            for fingerprint, value in data.items():
                if not isinstance(value, dict) or not isinstance(value.get("incident_id"), str):
                    result.problem("incident_invalid", "incidents", fingerprint)
                    continue
                identifier = value["incident_id"]
                if identifier in records:
                    result.problem("duplicate_incident_id", "incidents", identifier,
                                   unit=("incident", identifier))
                records[identifier] = value
                if value.get("status") != "resolved":
                    active[fingerprint] = identifier
                value = dict(value)
                if value.get("fingerprint") not in (None, fingerprint):
                    result.problem("incident_fingerprint_conflict", "incidents", identifier,
                                   unit=("incident", identifier))
                value["fingerprint"] = fingerprint
                records[identifier] = value
        elif type(data["version"]) is int and data["version"] == 2 and set(data) == {
            "version", "incidents", "active_by_fingerprint",
        }:
            records, active = data["incidents"], data["active_by_fingerprint"]
            if not isinstance(records, dict) or not isinstance(active, dict):
                raise ValueError()
        else:
            raise ValueError()
    except (ValueError, TypeError, KeyError, UnicodeError):
        result.problem("incidents_json_invalid", "incidents")
        return result

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    reverse = {}
    for fingerprint, identifier in active.items():
        if not isinstance(identifier, str) or not isinstance(fingerprint, str) or not fingerprint:
            result.problem("active_mapping_invalid", "incident_active", fingerprint)
            continue
        if identifier in reverse:
            result.problem("active_mapping_conflict", "incident_active", identifier,
                           unit=("incident", identifier))
        reverse[identifier] = fingerprint
        if identifier not in records:
            result.problem("active_incident_missing", "incident_active", identifier,
                           unit=("incident", identifier))
        result.add("incident_active", {
            "fingerprint": fingerprint, "incident_id": identifier, "updated_at": now,
        })

    for sequence, (identifier, value) in enumerate(records.items(), start=1):
        try:
            incident = Incident.model_validate(value)
            if incident.incident_id != identifier:
                raise ValueError()
            if identifier in reverse:
                if incident.fingerprint not in (None, reverse[identifier]):
                    raise ValueError()
                incident.fingerprint = reverse[identifier]
            state = incident.recovery_verification
            result.add("incidents", {
                "created_seq": sequence,
                "incident_id": identifier,
                "fingerprint": incident.fingerprint,
                "service": incident.service,
                "status": incident.status,
                "alert_state": incident.alert_state,
                "cleared_at": _utc(incident.cleared_at),
                "recovery_verified_at": _utc(incident.recovery_verified_at),
                "recovery_status": state.status if state else None,
                "recovery_next_retry_at": _utc(state.next_retry_at) if state else None,
                "data": encode({**value, **incident.model_dump(mode="json")}),
                "updated_at": now,
            })
        except (ValueError, TypeError, ValidationError):
            result.problem("incident_invalid", "incidents", identifier,
                           unit=("incident", identifier))

    history = Path(history_dir or source.parent / "history").resolve()
    references = {}
    for row in result.rows["incidents"]:
        incident = Incident.model_validate_json(row["data"])
        for reference in incident.history_archives:
            references.setdefault(reference.sha256, []).append((incident.incident_id, reference))
    archive_paths = {path.stem: path for path in history.glob("*.json")}
    for sha256 in sorted(set(archive_paths) | references.keys()):
        owners = references.get(sha256, [])
        try:
            HistoryArchive(sha256=sha256, session_ids=["format-check"])
            path = archive_paths[sha256].resolve(strict=True)
            if not path.is_relative_to(history):
                raise ValueError()
            raw = read_file(path, result)
            body, session_ids = archive_document(raw, sha256)
            if any(owner != body["incident_id"] or ref.session_ids != session_ids
                   for owner, ref in owners):
                raise ValueError()
            result.add("incident_history_archives", {
                "sha256": sha256,
                "incident_id": body["incident_id"],
                "session_ids": encode(session_ids),
                "body": raw.decode("utf-8"),
                "created_at": now,
            })
            if not owners:
                result.problem("unreferenced_archive_retained", "incident_history_archives", sha256,
                               severity="warning")
        except (OSError, ValueError, TypeError, KeyError, ValidationError):
            for owner, _ in owners:
                result.problem("archive_missing_or_invalid", "incident_history_archives", sha256,
                               unit=("incident", owner))
            if not owners:
                result.problem("archive_invalid", "incident_history_archives", sha256)
    return result


def read_remediation(source):
    result = Snapshot("remediation", {"remediation_state": []})
    try:
        data = validate_document(decode(read_file(source, result)))
        result.add("remediation_state", {
            "id": 1,
            "revision": 1,
            "data": encode(data),
            "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
        })
    except (ValueError, TypeError, StoreError, UnicodeError):
        result.problem("remediation_json_invalid", "remediation_state", unit=("remediation", "1"))
    return result


def _read_runtime_metadata(source, result, available):
    if "runtime_metadata" not in available:
        result.problem("source_table_missing", "runtime_metadata")
        return
    metadata = {}
    work = set()
    for row in source.execute("SELECT key, value FROM runtime_metadata ORDER BY key"):
        key, raw = row
        try:
            if not isinstance(key, str):
                raise ValueError()
            value = decode(raw)
            metadata[key] = value
            prefix, separator, session_id = key.partition(":")
            if separator and prefix in WORK_PREFIXES:
                work.add((WORK_PREFIXES[prefix], session_id))
            else:
                result.add("runtime_metadata", {"meta_key": key, "meta_value": encode(value)})
        except (ValueError, TypeError):
            result.problem("metadata_invalid", "runtime_metadata", key)
    if not metadata.get("work_outboxes_v1"):
        checkpoints = {row["checkpoint_id"]: decode(row["data"])
                       for row in result.rows["investigation_checkpoints"]}
        for row in result.rows["investigation_sessions"]:
            session = InvestigationSession.model_validate_json(row["data"])
            checkpoint = checkpoints.get(session.checkpoint_id, {})
            if checkpoint.get("projection_blocked"):
                work.add(("blocked_projection", session.session_id))
            elif not checkpoint.get("projected") or (
                checkpoint.get("remediation_pending") and not checkpoint.get("remediation_blocked")
            ):
                work.add(("projection", session.session_id))
            if session.status in TERMINAL | {SessionStatus.WAITING_HUMAN, SessionStatus.PAUSED}:
                work.add(("notification", session.session_id))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for kind, session_id in sorted(work):
        result.add("runtime_work", {"kind": kind, "session_id": session_id, "created_at": now})


def read_runtime(source_path):
    source_path = Path(source_path).resolve(strict=True)
    result = Snapshot("runtime", {table: [] for table in RUNTIME_TABLES})
    # A stopped snapshot is required. Refuse a WAL sidecar rather than copying
    # an apparently complete .db file that omits committed WAL transactions.
    wal = Path(str(source_path) + "-wal")
    if wal.is_file() and wal.stat().st_size:
        result.problem("sqlite_snapshot_requires_backup_without_wal", "runtime")
        return result
    journal = Path(str(source_path) + "-journal")
    if journal.is_file() and journal.stat().st_size:
        result.problem("sqlite_snapshot_requires_backup_without_journal", "runtime")
        return result
    with source_path.open("rb") as source_file:
        header = source_file.read(20)
    if header[18:20] == b"\x02\x02":
        # A main-file-only WAL copy may pass quick_check while silently losing
        # committed transactions. Require a backup normalized to DELETE mode.
        result.problem("sqlite_wal_mode_requires_consistent_backup", "runtime")
        return result
    before = hashlib.sha256(source_path.read_bytes()).hexdigest()
    result.file_hashes.append(before)
    with closing(sqlite3.connect(
        source_path.as_uri() + "?mode=ro&immutable=1", uri=True,
    )) as source:
        source.row_factory = sqlite3.Row
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")
        if source.execute("PRAGMA user_version").fetchone()[0] != 1:
            result.problem("unsupported_runtime_schema", "runtime")
            return result
        if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            result.problem("sqlite_integrity_failed", "runtime")
            return result
        available = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, projection, order in ROWS:
            if table.name not in available:
                result.problem("source_table_missing", table.name)
                continue
            try:
                for row in source.execute(f"SELECT {projection} FROM {table.name} ORDER BY {order}"):
                    try:
                        decode(row["data"])
                        values = row_values(table, row)
                        if set(values) - set(table.c.keys()):
                            raise ValueError()
                        result.add(table.name, values)
                    except (ValueError, TypeError, KeyError, ValidationError):
                        values = dict(row)
                        result.problem("runtime_record_invalid", table.name,
                                       [values.get(key) for key in KEYS[table.name]],
                                       unit=unit_for(table.name, values))
            except sqlite3.DatabaseError:
                result.problem("source_table_unreadable", table.name)
        try:
            _read_runtime_metadata(source, result, available)
        except sqlite3.DatabaseError:
            result.problem("source_table_unreadable", "runtime_metadata")
    if hashlib.sha256(source_path.read_bytes()).hexdigest() != before:
        result.problem("source_changed_during_read", "runtime")
        result.rows = {table: [] for table in RUNTIME_TABLES}
    return result


def read_source(kind, source, *, history_dir=None):
    try:
        if kind == "runtime":
            return read_runtime(source)
        if kind == "incidents":
            return read_incidents(source, history_dir=history_dir)
        if kind == "remediation":
            return read_remediation(source)
        raise ValueError("unsupported_source_kind")
    except (OSError, sqlite3.Error, ValueError, TypeError):
        result = Snapshot(kind)
        result.problem("source_unreadable", kind)
        return result
