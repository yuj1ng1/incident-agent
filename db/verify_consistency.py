"""Read-only structural, history, budget and authorization consistency checks."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError

from db import tables
from db.config import get_engine
from db.legacy_sources import (
    KEYS, Problem, Snapshot, archive_document, decode, digest, public_problems,
    row_key, unit_for,
)
from investigation.models import Checkpoint, HumanCommand, HumanRequest, InvestigationSession
from investigation.models import SessionStatus as RuntimeStatus
from investigation.state import TERMINAL
from models.incident import Incident
from models.remediation import RemediationAuditEvent, RemediationProposal, RemediationStatus as S
from remediation.approval import approval_digest
from remediation.store import StoreError, validate_document

ROOT = Path(__file__).resolve().parents[1]


def schema_head(connection):
    head = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
    inspector = inspect(connection)
    if not inspector.has_table("alembic_version"):
        raise RuntimeError("target_schema_not_at_head")
    if connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalars().all() != [head]:
        raise RuntimeError("target_schema_not_at_head")
    if set(tables.metadata.tables) - set(inspector.get_table_names()):
        raise RuntimeError("target_tables_missing")
    return head


def read_target(connection, names=None, *, lock=False):
    result = Snapshot("mysql")
    available = set(inspect(connection).get_table_names())
    for name in names if names is not None else KEYS:
        result.rows[name] = []
        if name not in available:
            result.problem("target_table_missing", name)
            continue
        table = tables.metadata.tables[name]
        statement = select(table).order_by(*table.primary_key.columns)
        if lock:
            statement = statement.with_for_update()
        result.rows[name] = [dict(row) for row in connection.execute(statement).mappings()]
    return result


def _duplicates(snapshot, problems):
    for table, rows in snapshot.rows.items():
        for columns in (KEYS[table],) + tuple(
            (key,) for key in ("sequence", "created_seq") if key in tables.metadata.tables[table].c
        ):
            seen = {}
            for row in rows:
                key = tuple(row.get(column) for column in columns)
                if key in seen:
                    for duplicate in (seen[key], row):
                        problems.append(Problem("duplicate_id_or_sequence", table, key,
                                                unit_for(table, duplicate)))
                seen[key] = row
                if columns in (("sequence",), ("created_seq",)) and (
                    type(key[0]) is not int or key[0] <= 0
                ):
                    problems.append(
                        Problem(
                            "invalid_sequence",
                            table,
                            key,
                            unit_for(table, row),
                        )
                    )


def _foreign_keys(snapshot, problems):
    for name, rows in snapshot.rows.items():
        table = tables.metadata.tables[name]
        for constraint in table.foreign_key_constraints:
            elements = list(constraint.elements)
            target = elements[0].column.table.name
            if target not in snapshot.rows:
                continue
            existing = {
                tuple(row[element.column.name] for element in elements)
                for row in snapshot.rows[target]
            }
            for row in rows:
                key = tuple(row.get(element.parent.name) for element in elements)
                if any(value is None for value in key):
                    continue
                if key not in existing:
                    problems.append(Problem("orphan_foreign_key", name, row_key(name, row),
                                            unit_for(name, row)))


def _incidents(snapshot, problems):
    incidents = {}
    archives = {}
    for row in snapshot.rows.get("incident_history_archives", []):
        try:
            body, session_ids = archive_document(row["body"].encode("utf-8"), row["sha256"])
            if body["incident_id"] != row["incident_id"] or decode(row["session_ids"]) != session_ids:
                raise ValueError()
            archives[row["sha256"]] = (body, session_ids)
        except (ValueError, TypeError, KeyError, ValidationError, UnicodeError):
            problems.append(Problem("archive_checksum_or_manifest_invalid", "incident_history_archives",
                                    row["sha256"], unit_for("incident_history_archives", row)))
    for row in snapshot.rows.get("incidents", []):
        unit = unit_for("incidents", row)
        try:
            incident = Incident.model_validate(decode(row["data"]))
            if any(getattr(incident, key) != row[key] for key in (
                "incident_id", "fingerprint", "service", "status", "alert_state",
            )):
                raise ValueError()
            from db.legacy_sources import _utc
            state = incident.recovery_verification
            expected = {
                "cleared_at": _utc(incident.cleared_at),
                "recovery_verified_at": _utc(incident.recovery_verified_at),
                "recovery_status": state.status if state else None,
                "recovery_next_retry_at": _utc(state.next_retry_at) if state else None,
            }
            if any(row[key] != value for key, value in expected.items()):
                raise ValueError()
            incidents[incident.incident_id] = incident
            hot_ids = [record.session_id for record in incident.investigations if record.session_id]
            seen = set(hot_ids)
            if len(seen) != len(hot_ids):
                problems.append(
                    Problem(
                        "duplicate_history_session",
                        "incidents",
                        row["incident_id"],
                        unit,
                    )
                )
            for reference in incident.history_archives:
                archived = archives.get(reference.sha256)
                if archived is None or archived[0]["incident_id"] != incident.incident_id or (
                    archived[1] != reference.session_ids
                ):
                    problems.append(
                        Problem(
                            "archive_reference_invalid",
                            "incidents",
                            row["incident_id"],
                            unit,
                        )
                    )
                if seen.intersection(reference.session_ids):
                    problems.append(
                        Problem(
                            "duplicate_hot_or_archived_session",
                            "incidents",
                            row["incident_id"],
                            unit,
                        )
                    )
                seen.update(reference.session_ids)
        except (ValueError, TypeError, KeyError, ValidationError):
            problems.append(
                Problem(
                    "incident_projection_invalid",
                    "incidents",
                    row.get("incident_id"),
                    unit,
                )
            )
    active_ids = Counter(row["incident_id"] for row in snapshot.rows.get("incident_active", []))
    for row in snapshot.rows.get("incident_active", []):
        incident = incidents.get(row["incident_id"])
        if not incident or incident.fingerprint != row["fingerprint"] or active_ids[row["incident_id"]] != 1:
            problems.append(Problem("active_mapping_conflict", "incident_active", row["fingerprint"],
                                    unit_for("incident_active", row)))


def _runtime(snapshot, problems):
    if "runtime_coordination" in snapshot.rows and not any(
        row["name"] == "worker_claim" for row in snapshot.rows["runtime_coordination"]
    ):
        problems.append(Problem("worker_coordination_missing", "runtime_coordination"))
    sessions = {}
    checkpoints = {}
    for name, cls in (
        ("investigation_sessions", InvestigationSession),
        ("investigation_checkpoints", Checkpoint),
        ("investigation_commands", HumanCommand),
        ("human_requests", HumanRequest),
    ):
        for row in snapshot.rows.get(name, []):
            try:
                record = cls.model_validate(decode(row["data"]))
                projection = {}
                if cls is InvestigationSession:
                    projection = {key: getattr(record, key) for key in (
                        "session_id", "incident_id", "status", "state_version", "budget_group_id",
                        "priority", "fencing_token",
                    )}
                    from db.legacy_sources import _utc
                    projection.update({key: _utc(getattr(record, key))
                                       for key in ("queued_at", "lease_until")})
                    sessions[record.session_id] = record
                elif cls is Checkpoint:
                    checkpoints[row["checkpoint_id"]] = (row, record)
                elif cls is HumanCommand:
                    projection = {
                        "command_id": record.command_id, "session_id": record.session_id,
                        "status": record.processing_status, "command_type": record.command_type,
                    }
                elif cls is HumanRequest:
                    projection = {key: getattr(record, key) for key in ("request_id", "session_id", "status")}
                if any(row[key] != value for key, value in projection.items()):
                    raise ValueError()
            except (ValueError, TypeError, KeyError, ValidationError):
                problems.append(
                    Problem(
                        "runtime_projection_invalid",
                        name,
                        row_key(name, row),
                        unit_for(name, row),
                    )
                )

    pending = Counter(row["session_id"] for row in snapshot.rows.get("human_requests", [])
                      if row["status"] == "pending")
    active = Counter(session.incident_id for session in sessions.values()
                     if session.status not in TERMINAL)
    for identifier, session in sessions.items():
        if session.status not in TERMINAL and active[session.incident_id] > 1:
            problems.append(Problem("duplicate_active_runtime_incident", "investigation_sessions",
                                    identifier, ("runtime", identifier)))
    commands = {row["command_id"]: row for row in snapshot.rows.get("investigation_commands", [])}
    for row in snapshot.rows.get("human_messages", []):
        command = commands.get(row["command_id"])
        try:
            payload = decode(row["data"])
            if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
                raise ValueError()
            if payload.get("command_id") != row["command_id"]:
                raise ValueError()
            if command:
                saved_command = HumanCommand.model_validate_json(command["data"])
                if command["session_id"] != row["session_id"] or (
                    saved_command.command_type not in {"message", "answer"}
                    or saved_command.actor != payload.get("actor")
                ):
                    raise ValueError()
        except (ValueError, TypeError, KeyError, ValidationError):
            problems.append(Problem("message_command_session_mismatch", "human_messages",
                                    row["command_id"], unit_for("human_messages", row)))
    retained = defaultdict(list)
    for row, checkpoint in checkpoints.values():
        retained[row["session_id"]].append(row)
    next_checkpoint_time = {}
    for rows in retained.values():
        rows.sort(key=lambda row: row["sequence"])
        for current, following in zip(rows, rows[1:]):
            next_checkpoint_time[current["checkpoint_id"]] = following["created_at"]
    reservations = defaultdict(lambda: Counter())
    checkpoint_events = set()
    legacy_checkpoint_events = defaultdict(list)
    events = sorted(snapshot.rows.get("investigation_events", []), key=lambda row: row["sequence"])
    for row in events:
        try:
            data = decode(row["data"])
            if not isinstance(data, dict):
                raise ValueError()
            checkpoint_id = data.get("checkpoint_id")
            if checkpoint_id in checkpoints and checkpoints[checkpoint_id][0]["session_id"] != row["session_id"]:
                raise ValueError()
            if row["event_type"] == "checkpoint_saved":
                if "checkpoint_id" not in data:
                    # Old writers persisted only the phase. This can establish
                    # an audit gap, never a verified checkpoint identity.
                    if set(data) != {"phase"} or not isinstance(data["phase"], str):
                        raise ValueError()
                    legacy_checkpoint_events[(row["session_id"], data["phase"])].append(row)
                elif not checkpoint_id:
                    raise ValueError()
                elif checkpoint_id in checkpoints:
                    identity = (row["session_id"], checkpoint_id)
                    if data.get("phase") != checkpoints[checkpoint_id][1].phase or (
                        identity in checkpoint_events
                    ):
                        raise ValueError()
                    checkpoint_events.add(identity)
                else:
                    # Runtime retains only a bounded checkpoint window while
                    # keeping older audit events. An old reference may have
                    # been pruned; a missing row inside that window is invalid.
                    window = retained[row["session_id"]]
                    if not window or row["timestamp"] >= min(
                        saved["created_at"] for saved in window
                    ):
                        raise ValueError()
            if row["event_type"] in {"model_reserved", "tool_reserved"}:
                kind = "model_requests" if row["event_type"] == "model_reserved" else "tool_executions"
                reservations[row["session_id"]][kind] += 1
                count = data["count"][kind]
                if type(count) is not int or count != reservations[row["session_id"]][kind]:
                    raise ValueError()
        except (ValueError, TypeError, KeyError):
            problems.append(Problem("event_sequence_or_budget_invalid", "investigation_events",
                                    row["event_id"], unit_for("investigation_events", row)))
    for identifier, session in sessions.items():
        unit = ("runtime", identifier)
        if session.budget_group_id is not None:
            group = sessions.get(session.budget_group_id)
            if group is None or group.incident_id != session.incident_id or (
                group.budget_group_id != group.session_id
            ):
                problems.append(Problem(
                    "budget_group_missing_or_mismatched", "investigation_sessions",
                    identifier, unit,
                ))
        if pending[identifier] > 1:
            problems.append(
                Problem(
                    "duplicate_pending_human_request",
                    "human_requests",
                    identifier,
                    unit,
                )
            )
        for kind, total in (("model_requests", session.total_model_requests),
                            ("tool_executions", session.total_tool_executions)):
            if reservations[identifier][kind] != total:
                problems.append(
                    Problem(
                        "session_event_budget_mismatch",
                        "investigation_sessions",
                        identifier,
                        unit,
                        field=kind,
                        expected=total,
                        actual=reservations[identifier][kind],
                    )
                )
        saved = checkpoints.get(session.checkpoint_id)
        if session.checkpoint_id and (not saved or saved[0]["session_id"] != identifier):
            problems.append(
                Problem(
                    "session_checkpoint_missing_or_mismatched",
                    "investigation_sessions",
                    identifier,
                    unit,
                )
            )
        if saved and saved[0]["session_id"] == identifier and saved[0]["sequence"] != max(
            row["sequence"] for row, _ in checkpoints.values() if row["session_id"] == identifier
        ):
            problems.append(
                Problem(
                    "session_checkpoint_not_latest",
                    "investigation_sessions",
                    identifier,
                    unit,
                )
            )
        for row, checkpoint in checkpoints.values():
            if row["session_id"] != identifier:
                continue
            if (identifier, row["checkpoint_id"]) not in checkpoint_events:
                candidates = legacy_checkpoint_events[(identifier, checkpoint.phase)]
                end = next_checkpoint_time.get(row["checkpoint_id"])
                legacy = next((event for event in candidates if (
                    event["timestamp"] >= row["created_at"]
                    and (end is None or event["timestamp"] <= end)
                )), None)
                if legacy is not None:
                    candidates.remove(legacy)
                problems.append(Problem(
                    "legacy_checkpoint_event_identity_unavailable" if legacy is not None
                    else "checkpoint_saved_event_missing", "investigation_checkpoints",
                    row["checkpoint_id"], unit,
                    severity="warning" if legacy is not None else "error",
                    field="checkpoint_id",
                ))
            # Display totals were historically independent of the authoritative
            # Session/event budget. Preserve them and flag them for review.
            if row["checkpoint_id"] == session.checkpoint_id and checkpoint.result and (
                session.status in {
                    RuntimeStatus.COMPLETED, RuntimeStatus.FAILED, RuntimeStatus.EXHAUSTED,
                }
            ):
                for field, count in (
                    ("model_rounds", session.total_model_requests),
                    ("tool_calls", session.total_tool_executions),
                ):
                    if field in checkpoint.result and (
                        type(checkpoint.result[field]) is not int
                        or checkpoint.result[field] != count
                    ):
                        problems.append(Problem(
                            "checkpoint_result_display_count_mismatch", "investigation_checkpoints",
                            row["checkpoint_id"], unit, severity="warning",
                            field=field, expected=count, actual=checkpoint.result[field],
                        ))
            for field, maximum in (("attempt", session.attempt_count),
                                   ("model_requests", session.total_model_requests),
                                   ("tool_executions", session.total_tool_executions)):
                value = checkpoint.budget.get(field, 0)
                if type(value) is not int or value < 0 or value > maximum or (
                    row["checkpoint_id"] == session.checkpoint_id and value != maximum
                ):
                    problems.append(Problem("checkpoint_budget_mismatch", "investigation_checkpoints",
                                            row["checkpoint_id"], unit))
    for row in snapshot.rows.get("human_requests", []):
        try:
            request = HumanRequest.model_validate_json(row["data"])
            if request.answer_command_id:
                command = commands.get(request.answer_command_id)
                if not command or command["session_id"] != request.session_id:
                    raise ValueError()
        except (ValueError, TypeError, ValidationError):
            problems.append(Problem("human_request_answer_mismatch", "human_requests",
                                    row["request_id"], unit_for("human_requests", row)))


def _remediation(snapshot, problems):
    for row in snapshot.rows.get("remediation_state", []):
        unit = ("remediation", "1")
        try:
            document = validate_document(decode(row["data"]))
            if row["id"] != 1 or row["revision"] < 0:
                raise ValueError()
        except (ValueError, TypeError, KeyError, StoreError):
            problems.append(
                Problem(
                    "remediation_document_invalid",
                    "remediation_state",
                    unit=unit,
                )
            )
            continue
        proposals = document["proposals"]
        actions, verifications, audit = defaultdict(list), defaultdict(list), defaultdict(list)
        for kind, id_field, timestamp, grouped in (
            ("actions", "action_id", "finished_at", actions),
            ("verifications", "verification_id", "verified_at", verifications),
            ("audit", "event_id", "occurred_at", audit),
        ):
            seen = set()
            for item in document[kind]:
                identifier = item.get(id_field)
                if identifier is None:
                    problems.append(Problem(
                        "remediation_record_id_missing", kind, unit=unit,
                    ))
                if item.get(timestamp) is None:
                    problems.append(Problem(
                        "remediation_record_timestamp_missing", kind, identifier, unit,
                    ))
                if identifier is not None and identifier in seen:
                    problems.append(
                        Problem(
                            "duplicate_remediation_record_id",
                            kind,
                            identifier,
                            unit,
                        )
                    )
                seen.add(identifier)
                if item["proposal_id"] not in proposals:
                    problems.append(
                        Problem(
                            "orphan_remediation_record",
                            kind,
                            identifier,
                            unit,
                        )
                    )
                grouped[item["proposal_id"]].append(item)
        command_ids = {}
        for identifier, raw in proposals.items():
            proposal = RemediationProposal.model_validate(raw)
            if raw.get("created_at") is None:
                problems.append(Problem(
                    "proposal_created_timestamp_missing", "remediation_state",
                    identifier, unit,
                ))
            events = audit[identifier]
            names = [item["event"] for item in events]
            results = actions[identifier]
            checks = [
                item for item in verifications[identifier]
                if item.get("purpose", "post_action") == "post_action"
            ]
            if not events or names[0] != "proposal_created":
                problems.append(
                    Problem(
                        "proposal_creation_audit_missing",
                        "remediation_state",
                        identifier,
                        unit,
                    )
                )
            times = [
                RemediationAuditEvent.model_validate(item).occurred_at
                for item in events if item.get("occurred_at") is not None
            ]
            if times != sorted(times):
                problems.append(
                    Problem(
                        "audit_time_order_invalid",
                        "remediation_state",
                        identifier,
                        unit,
                    )
                )
            if proposal.status not in {S.PENDING, S.REJECTED}:
                if (
                    not proposal.approved_by
                    or not proposal.approved_at
                    or proposal.approval_digest != approval_digest(proposal)
                ):
                    problems.append(
                        Problem(
                            "approval_authorization_invalid",
                            "remediation_state",
                            identifier,
                            unit,
                        )
                    )
                if "approved" not in names:
                    problems.append(
                        Problem(
                            "approval_audit_missing",
                            "remediation_state",
                            identifier,
                            unit,
                        )
                    )
            if proposal.status == S.REJECTED and "rejected" not in names:
                problems.append(
                    Problem(
                        "rejection_audit_missing",
                        "remediation_state",
                        identifier,
                        unit,
                    )
                )
            if proposal.status in {S.PENDING, S.APPROVED, S.REJECTED} and results:
                problems.append(
                    Problem(
                        "unexecuted_proposal_has_action",
                        "remediation_state",
                        identifier,
                        unit,
                    )
                )
            if proposal.status in {S.EXECUTING, S.SUCCEEDED, S.FAILED, S.VERIFICATION_FAILED, S.CLOSED}:
                if "execution_started" not in names or (
                    "approved" in names
                    and names.index("approved") > names.index("execution_started")
                ):
                    problems.append(
                        Problem(
                            "execution_audit_missing_or_out_of_order",
                            "remediation_state",
                            identifier,
                            unit,
                        )
                    )
            if proposal.status == S.SUCCEEDED and (
                not results or not results[-1]["success"]
                or not checks or not checks[-1]["success"]
            ):
                problems.append(
                    Problem(
                        "success_result_mismatch",
                        "remediation_state",
                        identifier,
                        unit,
                    )
                )
            if proposal.status == S.VERIFICATION_FAILED and (
                not results or not results[-1]["success"]
                or not checks or checks[-1]["success"]
            ):
                problems.append(
                    Problem(
                        "verification_failure_result_mismatch",
                        "remediation_state",
                        identifier,
                        unit,
                    )
                )
            if proposal.status == S.FAILED and (not results or results[-1]["success"]):
                problems.append(
                    Problem(
                        "failure_result_mismatch",
                        "remediation_state",
                        identifier,
                        unit,
                    )
                )
            action_events = [event for event in names if event in {"action_succeeded", "action_failed"}]
            verification_events = [
                event for event in names
                if event in {"verification_succeeded", "verification_failed"}
            ]
            if action_events != [
                "action_succeeded" if result["success"] else "action_failed"
                for result in results
            ]:
                problems.append(
                    Problem(
                        "action_audit_result_mismatch",
                        "remediation_state",
                        identifier,
                        unit,
                    )
                )
            if verification_events != [
                "verification_succeeded" if check["success"] else "verification_failed"
                for check in checks
            ]:
                problems.append(
                    Problem(
                        "verification_audit_result_mismatch",
                        "remediation_state",
                        identifier,
                        unit,
                    )
                )
            if proposal.status == S.CLOSED and (
                proposal.closure is None or "manual_execution_closed" not in names
            ):
                problems.append(
                    Problem(
                        "manual_closure_record_missing",
                        "remediation_state",
                        identifier,
                        unit,
                    )
                )
            if proposal.retry_of and proposal.retry_of not in proposals:
                problems.append(
                    Problem(
                        "retry_parent_missing",
                        "remediation_state",
                        identifier,
                        unit,
                    )
                )
            action_ids = {item.get("action_id") for item in results}
            for verification in verifications[identifier]:
                if verification.get("action_id") and verification["action_id"] not in action_ids:
                    problems.append(
                        Problem(
                            "verification_action_missing",
                            "remediation_state",
                            identifier,
                            unit,
                        )
                    )
            for event in events:
                details = event.get("details", {})
                command_id = details.get("command_id")
                if command_id is not None and (
                    not isinstance(command_id, str)
                    or not command_id.strip()
                    or len(command_id) > 200
                ):
                    problems.append(Problem(
                        "remediation_command_id_invalid", "remediation_state",
                        event.get("event_id"), unit,
                    ))
                    continue
                if command_id and not event["event"].endswith("_repeated"):
                    identity = (identifier, event["actor"], event["event"])
                    if command_id in command_ids and command_ids[command_id] != identity:
                        problems.append(
                            Problem(
                                "duplicate_remediation_command_id",
                                "remediation_state",
                                command_id,
                                unit,
                            )
                        )
                    command_ids[command_id] = identity


def analyze(snapshot, *, cross_store=False):
    problems = list(snapshot.problems)
    _duplicates(snapshot, problems)
    _foreign_keys(snapshot, problems)
    _incidents(snapshot, problems)
    _runtime(snapshot, problems)
    _remediation(snapshot, problems)
    if "remediation_state" in snapshot.rows and not snapshot.rows["remediation_state"]:
        problems.append(Problem("remediation_state_missing", "remediation_state"))
    if cross_store:
        incidents = {row["incident_id"] for row in snapshot.rows.get("incidents", [])}
        for row in snapshot.rows.get("investigation_sessions", []):
            if row["incident_id"] not in incidents:
                problems.append(Problem("session_incident_missing", "investigation_sessions",
                                        row["session_id"], unit_for("investigation_sessions", row)))
        for row in snapshot.rows.get("remediation_state", []):
            try:
                for identifier, proposal in decode(row["data"])["proposals"].items():
                    if proposal["incident_id"] not in incidents:
                        problems.append(
                            Problem(
                                "proposal_incident_missing",
                                "remediation_state",
                                identifier,
                            )
                        )
            except (ValueError, TypeError, KeyError):
                pass  # The document diagnostic above already reports corruption.
    return problems


def item_digest(table, row):
    ignored = {"updated_at"}
    if table in {"incident_history_archives", "runtime_work"}:
        ignored.add("created_at")
    if table == "remediation_state":
        ignored.add("revision")
    result = {}
    for key, value in row.items():
        if key in ignored or key not in tables.metadata.tables[table].c or (
            tables.metadata.tables[table].c[key].computed is not None
        ):
            continue
        if key in {"data", "meta_value", "session_ids"} and isinstance(value, str):
            value = decode(value)
        if isinstance(value, datetime):
            value = value.isoformat()
        result[key] = value
    return digest({key: result[key] for key in sorted(result)})


def compare_source(source, target):
    problems = []
    for table, rows in source.rows.items():
        expected = {row_key(table, row): row for row in rows}
        actual = {row_key(table, row): row for row in target.rows.get(table, [])}
        for key in expected.keys() | actual.keys():
            if key not in actual:
                code = "target_record_missing"
            elif key not in expected:
                code = "target_record_extra"
            elif item_digest(table, expected[key]) != item_digest(table, actual[key]):
                code = "target_record_differs"
            else:
                continue
            problems.append(Problem(code, table, key))
    return problems


def summarize(snapshot, problems):
    hot = archived = 0
    for row in snapshot.rows.get("incidents", []):
        try:
            hot += len(decode(row["data"])["investigations"])
        except (ValueError, TypeError, KeyError):
            pass
    for row in snapshot.rows.get("incident_history_archives", []):
        try:
            archived += len(decode(row["body"])["investigations"])
        except (ValueError, TypeError, KeyError):
            pass
    return {
        "report_version": 1,
        "ok": not any(problem.severity == "error" for problem in problems),
        "consistent": not problems,
        "counts": {table: len(rows) for table, rows in snapshot.rows.items()},
        "history_counts": {"hot": hot, "archived": archived, "total": hot + archived},
        "problems": public_problems(problems),
    }


def verify(database_url=None, *, source=None, source_kind=None, history_dir=None):
    try:
        engine = get_engine(database_url)
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            head = schema_head(connection)
            snapshot = read_target(connection)
            problems = analyze(snapshot, cross_store=True)
            if source is not None:
                from db.legacy_sources import read_source
                legacy = read_source(source_kind, source, history_dir=history_dir)
                problems.extend(analyze(legacy))
                problems.extend(compare_source(legacy, snapshot))
            report = summarize(snapshot, problems)
            # Verification stays strict even for importable historical gaps.
            report["ok"] = report["consistent"]
            report["schema_version"] = head
            return report
    except (SQLAlchemyError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        code = str(exc) if isinstance(exc, RuntimeError) and str(exc) in {
            "target_schema_not_at_head", "target_tables_missing",
            "database_url_not_configured", "database_url_invalid",
            "mysql_pymysql_database_url_required",
        } else "target_unavailable_or_schema_invalid"
        return summarize(Snapshot("mysql"), [Problem(code, "database")])


def ensure_report_destination(destination, source=None, *, history_dir=None):
    """Reject source aliases before any database write or report creation."""
    if destination is None or source is None:
        return
    try:
        output = Path(destination).resolve()
        protected = [Path(source).resolve()]
        protected.extend(
            Path(str(protected[0]) + suffix)
            for suffix in ("-wal", "-shm", "-journal")
        )
        if history_dir is not None:
            history = Path(history_dir).resolve()
            if output.is_relative_to(history):
                raise ValueError("report_destination_overlaps_source")
            protected.extend(history.glob("*.json"))
        if output in protected or (
            output.exists() and any(
                path.exists() and output.samefile(path) for path in protected
            )
        ):
            raise ValueError("report_destination_overlaps_source")
    except OSError:
        raise ValueError("report_destination_unreadable") from None


def emit_report(report, destination=None):
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if destination:
        Path(destination).write_text(text + "\n", encoding="utf-8")
    print(text)
    print(
        f"ok={report['ok']} errors={sum(p['severity'] == 'error' for p in report['problems'])} "
        f"warnings={sum(p['severity'] == 'warning' for p in report['problems'])}",
        file=sys.stderr,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读检查 MySQL 权威数据及可选历史源副本")
    parser.add_argument("--target", default=None, help="MySQL URL；默认从环境变量读取")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--source-kind", choices=("runtime", "incidents", "remediation"))
    parser.add_argument("--history-dir", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.source is not None and args.source_kind is None:
        parser.error("--source-kind is required with --source")
    try:
        ensure_report_destination(
            args.report,
            args.source,
            history_dir=(args.history_dir or args.source.parent / "history")
            if args.source is not None and args.source_kind == "incidents" else None,
        )
    except ValueError as exc:
        parser.error(str(exc))
    report = verify(args.target, source=args.source, source_kind=args.source_kind, history_dir=args.history_dir)
    emit_report(report, args.report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
