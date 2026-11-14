"""Synthetic legacy files only; never copy operator fixtures into test sources."""
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import sqlite3

from investigation.models import InvestigationSession
from models.incident import Incident
from models.investigation import InvestigationRecord
from models.remediation import RemediationAuditEvent, RemediationProposal
from remediation.store import empty_document

STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


def incident_document(*, version=2, identifier="inc-legacy"):
    incident = Incident(
        incident_id=identifier, fingerprint="fp-legacy", service="fixture-service",
        severity="warning", status="investigating", alerts=[],
    ).model_dump(mode="json")
    if version == 1:
        incident.pop("fingerprint")
        return {"fp-legacy": incident}
    return {
        "version": 2, "incidents": {identifier: incident},
        "active_by_fingerprint": {"fp-legacy": identifier},
    }


def remediation_document(*, version=2):
    proposal = RemediationProposal(
        proposal_id="rem-legacy", incident_id="inc-legacy", service="fixture-service",
        summary="Synthetic proposal", reason="Synthetic evidence", action_type="fixture",
        parameters={"target": "fixture"}, risk="low", created_at=STAMP,
        expected_commit_sha="a" * 40, expected_process_started_at=STAMP,
    )
    event = RemediationAuditEvent(
        event_id="audit-legacy", proposal_id=proposal.proposal_id,
        occurred_at=STAMP, actor="fixture-planner", event="proposal_created",
        action_type=proposal.action_type,
    )
    result = empty_document()
    result["version"] = version
    result["proposals"][proposal.proposal_id] = proposal.model_dump(mode="json")
    result["audit"].append(event.model_dump(mode="json"))
    if version == 1:
        for field in ("compensation", "incident_fingerprint", "closure", "retry_of"):
            result["proposals"][proposal.proposal_id].pop(field)
    return result


def write_json(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def runtime_snapshot(path, *, missing=()):
    """The v1 SQLite columns, independently constructed from its old contract."""
    definitions = {
        "investigation_sessions": "session_id TEXT PRIMARY KEY, incident_id TEXT, status TEXT, state_version INTEGER, data TEXT",
        "investigation_checkpoints": "checkpoint_id TEXT PRIMARY KEY, session_id TEXT, created_at TEXT, data TEXT",
        "investigation_commands": "sequence INTEGER PRIMARY KEY, command_id TEXT UNIQUE, session_id TEXT, status TEXT, data TEXT",
        "human_messages": "sequence INTEGER PRIMARY KEY, command_id TEXT UNIQUE, session_id TEXT, consumed_at TEXT, data TEXT",
        "human_requests": "request_id TEXT PRIMARY KEY, session_id TEXT, status TEXT, data TEXT",
        "investigation_events": "sequence INTEGER PRIMARY KEY, event_id TEXT UNIQUE, session_id TEXT, event_type TEXT, timestamp TEXT, actor TEXT, data TEXT",
        "runtime_metadata": "key TEXT PRIMARY KEY, value TEXT",
    }
    session = InvestigationSession(
        session_id="ses-legacy", incident_id="inc-legacy", alert_fingerprint="fp-legacy",
        service="fixture-service", created_at=STAMP, updated_at=STAMP, queued_at=STAMP,
    )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=1")
        for name, columns in definitions.items():
            if name not in missing:
                connection.execute(f"CREATE TABLE {name} ({columns})")
        if "investigation_sessions" not in missing:
            connection.execute(
                "INSERT INTO investigation_sessions VALUES (?, ?, ?, ?, ?)",
                (session.session_id, session.incident_id, session.status.value,
                 session.state_version, session.model_dump_json()),
            )
    return Path(path)


def archived_snapshot(root):
    root = Path(root)
    record = InvestigationRecord(
        session_id="ses-archived", investigated_at=STAMP,
        status="completed", answer="Synthetic history",
    ).model_dump(mode="json")
    body = {
        "schema_version": 1, "incident_id": "inc-legacy",
        "investigations": [record], "evidence": [],
    }
    raw = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()
    sha256 = hashlib.sha256(raw).hexdigest()
    history = root / "history"
    history.mkdir(parents=True)
    archive = history / (sha256 + ".json")
    archive.write_bytes(raw)
    document = incident_document()
    document["incidents"]["inc-legacy"]["history_archives"] = [{
        "sha256": sha256, "session_ids": ["ses-archived"],
    }]
    return write_json(root / "incidents.json", document), archive, raw


def legacy_runtime_with_display_gap(path):
    """Old phase-only audit and 0/0 display totals, with a correct real budget."""
    path = runtime_snapshot(path)
    with sqlite3.connect(path) as connection:
        data = json.loads(connection.execute(
            "SELECT data FROM investigation_sessions",
        ).fetchone()[0])
        data.update(
            status="COMPLETED", attempt_count=1, total_model_requests=1,
            checkpoint_id="cp-legacy", completed_at=STAMP.isoformat(),
        )
        connection.execute(
            "UPDATE investigation_sessions SET status=?,data=?",
            (data["status"], json.dumps(data)),
        )
        checkpoint = {
            "phase": "response_saved", "projected": True,
            "budget": {"attempt": 1, "model_requests": 1, "tool_executions": 0},
            "result": {
                "success": True, "status": "completed", "answer": "fixture",
                "model_rounds": 0, "tool_calls": 0,
            },
        }
        connection.execute(
            "INSERT INTO investigation_checkpoints VALUES (?,?,?,?)",
            ("cp-legacy", data["session_id"], STAMP.isoformat(), json.dumps(checkpoint)),
        )
        for sequence, kind, payload in (
            (1, "model_reserved", {"count": checkpoint["budget"]}),
            (2, "checkpoint_saved", {"phase": "response_saved"}),
        ):
            connection.execute(
                "INSERT INTO investigation_events VALUES (?,?,?,?,?,?,?)",
                (sequence, "evt-legacy-" + str(sequence), data["session_id"],
                 kind, STAMP.isoformat(), "fixture", json.dumps(payload)),
            )
    return path
