"""Preview/apply immutable legacy snapshots without overwriting target records."""
import argparse
from collections import defaultdict
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from db import tables
from db.config import get_engine
from db.legacy_sources import (
    Problem, decode, digest, public_problems, read_source, row_key, unit_for,
)
from db.verify_consistency import (
    analyze, compare_source, emit_report, ensure_report_destination, item_digest,
    read_target, schema_head, summarize,
)
from investigation.state import TERMINAL
from remediation.store import empty_document


ORDER = (
    "investigation_sessions", "investigation_checkpoints", "investigation_commands",
    "human_messages", "human_requests", "investigation_events", "runtime_metadata",
    "runtime_work", "incidents", "incident_history_archives", "incident_active",
    "remediation_state",
)
KINDS = {
    "import-runtime-sqlite": "runtime",
    "import-incidents-json": "incidents",
    "import-remediation-json": "remediation",
}


class ImportAborted(Exception):
    pass


def plan_import(source, target, problems):
    """Validate the complete plan before executing any INSERT or UPDATE."""
    invalid = {problem.unit for problem in problems
               if problem.severity == "error" and problem.unit is not None}
    operations, unchanged, conflicted = [], 0, 0
    for name, rows in source.rows.items():
        existing = {row_key(name, row): row for row in target.rows.get(name, [])}
        sequences = {
            column: {other.get(column): key for key, other in existing.items()}
            for column in ("sequence", "created_seq")
        }
        for row in rows:
            key = row_key(name, row)
            old = existing.get(key)
            if old is not None:
                try:
                    identical = item_digest(name, old) == item_digest(name, row)
                    empty = name == "remediation_state" and old["revision"] == 0 and (
                        decode(old["data"]) == empty_document()
                    )
                except (ValueError, TypeError, KeyError):
                    identical, empty = False, False
                if identical:
                    unchanged += 1
                    continue
                if not empty:
                    unit = unit_for(name, row)
                    invalid.add(unit)
                    problems.append(Problem("target_conflict", name, key, unit))
                    conflicted += 1
                    continue
            # Explicit sequence IDs preserve the ordering of the stopped source.
            # They may never displace a different target row with that sequence.
            for sequence in ("sequence", "created_seq"):
                if sequence in row and row[sequence] in sequences[sequence] and (
                    sequences[sequence][row[sequence]] != key
                ):
                    unit = unit_for(name, row)
                    invalid.add(unit)
                    problems.append(
                        Problem(
                            "target_sequence_conflict",
                            name,
                            key,
                            unit,
                        )
                    )
            if name == "incident_active" and any(
                other["incident_id"] == row["incident_id"] and other["fingerprint"] != row["fingerprint"]
                for other in existing.values()
            ):
                unit = unit_for(name, row)
                invalid.add(unit)
                problems.append(
                    Problem(
                        "target_active_mapping_conflict",
                        name,
                        key,
                        unit,
                    )
                )
            active_conflict = name == "investigation_sessions" and row["status"] not in TERMINAL and any(
                other["incident_id"] == row["incident_id"] and other["status"] not in TERMINAL
                and other["session_id"] != row["session_id"] for other in existing.values()
            )
            pending_conflict = name == "human_requests" and row["status"] == "pending" and any(
                other["session_id"] == row["session_id"] and other["status"] == "pending"
                and other["request_id"] != row["request_id"] for other in existing.values()
            )
            if active_conflict or pending_conflict:
                unit = unit_for(name, row)
                invalid.add(unit)
                problems.append(
                    Problem(
                        "target_active_or_pending_conflict",
                        name,
                        key,
                        unit,
                    )
                )
            operations.append((name, row))
    accepted = [(name, row) for name, row in operations if unit_for(name, row) not in invalid]
    skipped = conflicted + len(operations) - len(accepted)
    return accepted, unchanged, skipped


def _write_group(connection, rows):
    for name, values in sorted(rows, key=lambda pair: ORDER.index(pair[0])):
        table = tables.metadata.tables[name]
        if name == "remediation_state":
            # Only the untouched migration seed is replaceable. A CAS also
            # protects against a writer racing after the preview comparison.
            result = connection.execute(
                update(table).where(table.c.id == 1, table.c.revision == 0)
                .values(**{key: value for key, value in values.items() if key != "id"})
            )
            if result.rowcount != 1:
                raise ImportAborted()
        else:
            connection.execute(table.insert().values(**values))


def import_legacy(
    kind,
    source,
    database_url=None,
    *,
    apply=False,
    verify_only=False,
    history_dir=None,
    fail_fast=False,
):
    if apply and verify_only:
        raise ValueError("apply_and_verify_only_are_mutually_exclusive")
    snapshot = read_source(kind, source, history_dir=history_dir)
    problems = analyze(snapshot)
    report = summarize(snapshot, problems)
    report.update({
        "mode": "apply" if apply else "verify-only" if verify_only else "dry-run",
        "source_kind": kind,
        "source_file_count": len(snapshot.file_hashes),
        "source_manifest_sha256": digest(sorted(snapshot.file_hashes)),
        "planned": 0, "imported": 0, "unchanged": 0, "skipped": 0,
        "write_outcome": "not_started" if apply else "not_requested",
    })
    try:
        engine = get_engine(database_url)
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            # Preview/verify cannot create schema, seed rows, or lock files.
            if not apply:
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            report["schema_version"] = schema_head(connection)
            target = read_target(connection, snapshot.rows.keys(), lock=apply)
            problems.extend(analyze(target))
            if verify_only:
                problems.extend(compare_source(snapshot, target))
            else:
                operations, unchanged, skipped = plan_import(snapshot, target, problems)
                report.update(planned=len(operations), unchanged=unchanged, skipped=skipped)
                if apply:
                    if fail_fast and any(problem.severity == "error" for problem in problems):
                        raise ImportAborted()
                    groups = defaultdict(list)
                    for name, values in operations:
                        groups[unit_for(name, values)].append((name, values))
                    written = []
                    for unit, rows in groups.items():
                        try:
                            with connection.begin_nested():
                                _write_group(connection, rows)
                            written.extend(rows)
                        except (SQLAlchemyError, ImportAborted):
                            problems.append(
                                Problem(
                                    "group_write_failed",
                                    rows[0][0],
                                    unit,
                                    unit,
                                )
                            )
                            report["skipped"] += len(rows)
                            if fail_fast:
                                raise ImportAborted() from None
                    # Verify every successful row inside the transaction before
                    # COMMIT; a server coercion must never count as a good copy.
                    after = read_target(connection, snapshot.rows.keys())
                    actual = {
                        name: {row_key(name, row): row for row in rows}
                        for name, rows in after.rows.items()
                    }
                    for name, row in written:
                        saved = actual[name].get(row_key(name, row))
                        if saved is None or item_digest(name, saved) != item_digest(name, row):
                            problems.append(
                                Problem(
                                    "post_write_verification_failed",
                                    name,
                                    row_key(name, row),
                                )
                            )
                            raise ImportAborted()
                    report["write_outcome"] = "unknown"
                    connection.commit()
                    report["imported"] = len(written)
                    report["write_outcome"] = "committed"
            report["target_counts"] = {name: len(rows) for name, rows in target.rows.items()}
    except ImportAborted:
        report["imported"] = 0
        report["write_outcome"] = "not_committed"
        problems.append(Problem("import_transaction_rolled_back", "database"))
    except (SQLAlchemyError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        report["imported"] = 0
        code = str(exc) if isinstance(exc, RuntimeError) and str(exc) in {
            "target_schema_not_at_head", "target_tables_missing",
            "database_url_not_configured", "database_url_invalid",
            "mysql_pymysql_database_url_required",
        } else "target_unavailable_or_schema_invalid"
        problems.append(Problem(code, "database"))
    report["consistent"] = not problems
    report["ok"] = report["consistent"] if verify_only else not any(
        problem.severity == "error" for problem in problems
    )
    report["problems"] = public_problems(problems)
    report["source_items"] = [
        {"table": name, "record_sha256": digest(row_key(name, row)),
         "document_sha256": item_digest(name, row)}
        for name, rows in snapshot.rows.items() for row in rows
    ]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="预览、导入或只读核对历史数据副本；默认不写库")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in KINDS:
        command = commands.add_parser(name)
        command.add_argument("--source", required=True, type=Path)
        command.add_argument("--target", default=None, help="MySQL URL；默认从环境变量读取")
        mode = command.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--apply", action="store_true")
        mode.add_argument("--verify-only", action="store_true")
        command.add_argument("--fail-fast", action="store_true")
        command.add_argument("--report", type=Path)
        if name == "import-incidents-json":
            command.add_argument("--history-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        ensure_report_destination(
            args.report,
            args.source,
            history_dir=(getattr(args, "history_dir", None) or args.source.parent / "history")
            if KINDS[args.command] == "incidents" else None,
        )
    except ValueError as exc:
        parser.error(str(exc))
    report = import_legacy(
        KINDS[args.command], args.source, args.target,
        apply=args.apply, verify_only=args.verify_only,
        history_dir=getattr(args, "history_dir", None), fail_fast=args.fail_fast,
    )
    emit_report(report, args.report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
