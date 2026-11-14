"""Legacy Runtime conversion helpers and a preview-first compatibility CLI."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from db import tables
from investigation.models import Checkpoint, HumanCommand, HumanRequest, InvestigationSession
from investigation.models import SessionStatus
from investigation.state import TERMINAL


ROOT = Path(__file__).resolve().parents[1]
ROWS = (
    (tables.investigation_sessions, "rowid AS created_seq, *", "rowid"),
    (tables.investigation_checkpoints, "rowid AS sequence, *", "rowid"),
    (tables.investigation_commands, "*", "sequence"),
    (tables.human_messages, "*", "sequence"),
    (tables.human_requests, "rowid AS created_seq, *", "rowid"),
    (tables.investigation_events, "*", "sequence"),
)
WORK_PREFIXES = {
    "pending_projection": "projection",
    "pending_notification": "notification",
    "blocked_projection": "blocked_projection",
}


def utc_datetime(value):
    if value is None:
        return None
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if stamp.utcoffset() is None:
        raise ValueError("legacy_runtime_timestamp_without_timezone")
    return stamp.astimezone(timezone.utc).replace(tzinfo=None)


def row_values(table, row):
    values = dict(row)
    data = json.loads(values["data"])
    record = None
    # Runtime documents are MEDIUMTEXT so object key order survives the copy.
    values["data"] = json.dumps(data, ensure_ascii=False, allow_nan=False)
    for field in {"created_at", "consumed_at", "timestamp"} & values.keys():
        values[field] = utc_datetime(values[field])
    if table is tables.investigation_sessions:
        if not data.get("created_at") or not data.get("updated_at"):
            raise ValueError("legacy_runtime_session_timestamp_missing")
        # Older v1 sessions predate queue metadata. Derive it from durable
        # creation time, never the current import time, so repeats are stable.
        data = {**data, "queued_at": data.get("queued_at", data["created_at"])}
        record = InvestigationSession.model_validate(data)
        if (record.session_id, record.incident_id, record.status, record.state_version) != (
            values["session_id"], values["incident_id"], values["status"], values["state_version"],
        ):
            raise ValueError("legacy_runtime_session_projection_mismatch")
        values.update(
            budget_group_id=record.budget_group_id,
            priority=record.priority,
            queued_at=utc_datetime(record.queued_at),
            lease_until=utc_datetime(record.lease_until),
            fencing_token=record.fencing_token,
        )
    elif table is tables.investigation_checkpoints:
        Checkpoint.model_validate(data)
    elif table is tables.investigation_commands:
        if not data.get("created_at"):
            if not values.get("created_at"):
                raise ValueError("legacy_runtime_command_timestamp_missing")
            data = {**data, "created_at": row["created_at"]}
        record = HumanCommand.model_validate(data)
        if (record.command_id, record.session_id, record.processing_status) != (
            values["command_id"], values["session_id"], values["status"],
        ):
            raise ValueError("legacy_runtime_command_projection_mismatch")
        values.update(command_type=record.command_type, created_at=utc_datetime(record.created_at))
    elif table is tables.human_requests:
        if not data.get("created_at"):
            raise ValueError("legacy_runtime_request_timestamp_missing")
        record = HumanRequest.model_validate(data)
        if (record.request_id, record.session_id, record.status) != (
            values["request_id"], values["session_id"], values["status"],
        ):
            raise ValueError("legacy_runtime_request_projection_mismatch")
        values["expires_at"] = utc_datetime(record.expires_at)
    if record is not None:
        # Keep original object-key order, append compatible defaults, and freeze
        # model defaults before future reads can regenerate timestamps.
        values["data"] = json.dumps(
            {**data, **record.model_dump(mode="json")},
            ensure_ascii=False,
            allow_nan=False,
        )
    return values


def copy_work_and_metadata(source, target):
    work = set()
    metadata = []
    for key, value in source.execute("SELECT key,value FROM runtime_metadata"):
        value = json.loads(value)
        prefix, separator, session_id = key.partition(":")
        if separator and prefix in WORK_PREFIXES:
            work.add((WORK_PREFIXES[prefix], session_id))
        else:
            metadata.append({
                "meta_key": key,
                "meta_value": json.dumps(value, ensure_ascii=False, allow_nan=False),
            })

    current = source.execute("SELECT value FROM runtime_metadata WHERE key='work_outboxes_v1'").fetchone()
    backfill = not current or not json.loads(current[0])
    for row in source.execute("SELECT data FROM investigation_sessions"):
        session = InvestigationSession.model_validate_json(row["data"])
        checkpoint = {}
        if session.checkpoint_id:
            saved = source.execute(
                "SELECT data FROM investigation_checkpoints WHERE checkpoint_id=? AND session_id=?",
                (session.checkpoint_id, session.session_id),
            ).fetchone()
            if not saved:
                raise ValueError("legacy_runtime_checkpoint_missing_or_mismatched")
            checkpoint = json.loads(saved["data"])
        if backfill:
            if checkpoint.get("projection_blocked"):
                work.add(("blocked_projection", session.session_id))
            elif not checkpoint.get("projected") or (
                checkpoint.get("remediation_pending") and not checkpoint.get("remediation_blocked")
            ):
                work.add(("projection", session.session_id))
            if session.status in TERMINAL | {SessionStatus.WAITING_HUMAN, SessionStatus.PAUSED}:
                work.add(("notification", session.session_id))

    if metadata:
        target.execute(tables.runtime_metadata.insert(), metadata)
    if work:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        target.execute(tables.runtime_work.insert(), [
            {"kind": kind, "session_id": session_id, "created_at": now}
            for kind, session_id in sorted(work)
        ])
    return {"runtime_metadata": len(metadata), "runtime_work": len(work)}


def import_snapshot(source_path: Path, database_url=None):
    """Explicit programmatic apply; preserve the old counts return shape."""
    from db.import_legacy import import_legacy

    report = import_legacy(
        "runtime", source_path, database_url, apply=True, fail_fast=True,
    )
    if not report["ok"]:
        raise RuntimeError("legacy_runtime_import_failed")
    return report["counts"]


def main(argv=None):
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="预览或导入已停写的 SQLite Runtime 副本；默认不写库")
    parser.add_argument("snapshot", type=Path, nargs="?")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--target", default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if (args.source is None) == (args.snapshot is None):
        parser.error("provide exactly one source: --source or legacy positional snapshot")
    from db.import_legacy import import_legacy
    from db.verify_consistency import emit_report, ensure_report_destination

    try:
        ensure_report_destination(args.report, args.source or args.snapshot)
    except ValueError as exc:
        parser.error(str(exc))
    report = import_legacy(
        "runtime", args.source or args.snapshot, args.target, apply=args.apply,
        verify_only=args.verify_only, fail_fast=args.fail_fast,
    )
    emit_report(report, args.report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
