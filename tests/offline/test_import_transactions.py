"""Offline SQLAlchemy transaction tests; MySQL integration remains separate."""
from contextlib import AbstractContextManager
import hashlib
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.exc import SQLAlchemyError

from db import tables
import db.import_legacy as importing
from db.legacy_sources import encode
import db.verify_consistency as checking
from legacy_support import (
    incident_document, legacy_runtime_with_display_gap, remediation_document,
    runtime_snapshot, write_json,
)
from remediation.store import empty_document


@compiles(MEDIUMTEXT, "sqlite")
def text_test_double(element, compiler, **kwargs):
    return "TEXT"


class ConnectionDouble(AbstractContextManager):
    def __init__(self, native):
        self.native = native

    def __enter__(self):
        # SQLite's legacy driver otherwise releases an outermost SAVEPOINT
        # immediately. Explicit BEGIN gives this test a real outer transaction.
        self.native.exec_driver_sql("BEGIN")
        return self

    def __exit__(self, *args):
        self.native.rollback()
        self.native.exec_driver_sql("PRAGMA query_only=OFF")
        self.native.close()

    def execution_options(self, **options):
        return self

    def exec_driver_sql(self, statement, *args, **kwargs):
        if statement == "SET TRANSACTION READ ONLY":
            statement = "PRAGMA query_only=ON"
        return self.native.exec_driver_sql(statement, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.native, name)


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://")
    tables.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('0005_remediation_store')")
        connection.execute(tables.runtime_coordination.insert().values(name="worker_claim"))
        from datetime import datetime
        connection.execute(tables.remediation_state.insert().values(
            id=1, revision=0, data=encode(empty_document()), updated_at=datetime(2026, 1, 1),
        ))

    class EngineDouble:
        def connect(self):
            return ConnectionDouble(engine.connect())

    original_read = checking.read_target
    original_head = checking.schema_head
    for module in (checking, importing):
        monkeypatch.setattr(module, "get_engine", lambda url=None: EngineDouble())
        monkeypatch.setattr(module, "read_target", lambda connection, *args, **kwargs:
                            original_read(connection.native, *args, **kwargs))
        monkeypatch.setattr(module, "schema_head", lambda connection:
                            original_head(connection.native))
    yield engine
    engine.dispose()


@pytest.mark.parametrize("kind", ("incidents", "remediation", "runtime"))
def test_apply_is_atomic_repeatable_and_verify_only_is_readonly(database, tmp_path, kind):
    source = (
        runtime_snapshot(tmp_path / "runtime.db") if kind == "runtime"
        else write_json(tmp_path / "source.json", incident_document() if kind == "incidents" else remediation_document())
    )
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    preview = importing.import_legacy(kind, source)
    assert preview["ok"] and preview["planned"] > 0, preview
    with database.connect() as connection:
        table = tables.metadata.tables[next(iter(preview["counts"]))]
        assert len(connection.execute(table.select()).all()) == (1 if kind == "remediation" else 0)
    applied = importing.import_legacy(kind, source, apply=True)
    assert applied["ok"] and applied["imported"] == preview["planned"], applied
    repeated = importing.import_legacy(kind, source, apply=True)
    assert repeated["ok"] and repeated["imported"] == 0, repeated
    assert importing.import_legacy(kind, source, verify_only=True)["ok"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_one_corrupt_incident_does_not_prevent_a_valid_group_from_committing(database, tmp_path):
    document = incident_document()
    document["incidents"]["inc-legacy"]["history_archives"] = [{
        "sha256": "c" * 64, "session_ids": ["missing"],
    }]
    document["incidents"]["inc-good"] = incident_document(identifier="inc-good")["incidents"]["inc-good"]
    source = write_json(tmp_path / "source.json", document)
    report = importing.import_legacy("incidents", source, apply=True)
    assert not report["ok"] and report["imported"] == 1, report
    with database.connect() as connection:
        assert connection.execute(tables.incidents.select()).mappings().one()["incident_id"] == "inc-good"


def test_fail_fast_does_not_partially_commit_valid_groups(database, tmp_path):
    document = incident_document()
    document["incidents"]["inc-legacy"]["history_archives"] = [{
        "sha256": "c" * 64, "session_ids": ["missing"],
    }]
    document["incidents"]["inc-good"] = incident_document(identifier="inc-good")["incidents"]["inc-good"]
    source = write_json(tmp_path / "source.json", document)
    report = importing.import_legacy("incidents", source, apply=True, fail_fast=True)
    assert not report["ok"] and report["imported"] == 0
    with database.connect() as connection:
        assert not connection.execute(tables.incidents.select()).all()


def test_source_and_target_differences_have_nonzero_result_and_redacted_diagnostics(database, tmp_path):
    source = write_json(tmp_path / "incidents.json", incident_document())
    assert importing.import_legacy("incidents", source, apply=True)["ok"]
    with database.begin() as connection:
        connection.execute(tables.incident_active.delete())
    report = importing.import_legacy("incidents", source, verify_only=True)
    assert not report["ok"]
    assert any(problem["code"] == "target_record_missing" for problem in report["problems"])


def test_missing_schema_head_and_missing_table_fail_before_writing(database, tmp_path):
    source = write_json(tmp_path / "incidents.json", incident_document())
    with database.begin() as connection:
        connection.exec_driver_sql("UPDATE alembic_version SET version_num='0004_incident_store'")
    assert not importing.import_legacy("incidents", source, apply=True)["ok"]
    with database.begin() as connection:
        assert not connection.execute(tables.incidents.select()).all()
        connection.exec_driver_sql("UPDATE alembic_version SET version_num='0005_remediation_store'")
        connection.exec_driver_sql("DROP TABLE remediation_state")
    assert not importing.import_legacy("incidents", source, apply=True)["ok"]


def test_cli_defaults_to_preview_and_returns_nonzero_for_differences(database, tmp_path, capsys):
    source = write_json(tmp_path / "incidents.json", incident_document())
    command = ["import-incidents-json", "--source", str(source)]
    assert importing.main(command) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["mode"] == "dry-run" and preview["imported"] == 0
    assert importing.main(command + ["--verify-only"]) == 1
    capsys.readouterr()
    assert importing.main(command + ["--apply"]) == 0
    capsys.readouterr()
    assert checking.main(["--source", str(source), "--source-kind", "incidents"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]


@pytest.mark.parametrize("fail_fast", (False, True))
def test_savepoint_failure_preserves_only_confirmed_groups(database, tmp_path, monkeypatch, fail_fast):
    document = incident_document()
    document["incidents"]["inc-good"] = incident_document(identifier="inc-good")["incidents"]["inc-good"]
    source = write_json(tmp_path / "source.json", document)
    write = importing._write_group

    def fail_second_group(connection, rows):
        write(connection, rows)
        if any(row.get("incident_id") == "inc-good" for _, row in rows):
            raise SQLAlchemyError("synthetic-sensitive-diagnostic")

    monkeypatch.setattr(importing, "_write_group", fail_second_group)
    report = importing.import_legacy("incidents", source, apply=True, fail_fast=fail_fast)
    assert not report["ok"]
    assert "synthetic-sensitive-diagnostic" not in json.dumps(report)
    assert report["imported"] == (0 if fail_fast else 2)
    with database.connect() as connection:
        saved = connection.execute(tables.incidents.select()).mappings().all()
    assert len(saved) == (0 if fail_fast else 1)


def test_lost_commit_acknowledgement_reports_uncertainty_and_repeat_is_safe(database, tmp_path, monkeypatch):
    source = write_json(tmp_path / "incidents.json", incident_document())

    def lost_ack(connection):
        connection.native.commit()
        raise SQLAlchemyError("synthetic-sensitive-commit-error")

    with monkeypatch.context() as fault:
        fault.setattr(ConnectionDouble, "commit", lost_ack, raising=False)
        report = importing.import_legacy("incidents", source, apply=True)
    assert not report["ok"] and report["write_outcome"] == "unknown"
    assert "synthetic-sensitive-commit-error" not in json.dumps(report)
    repeated = importing.import_legacy("incidents", source, apply=True)
    assert repeated["ok"] and repeated["imported"] == 0


def test_legacy_runtime_entry_point_previews_and_programmatic_apply_is_idempotent(database, tmp_path, capsys):
    from db.import_sqlite import import_snapshot, main

    source = runtime_snapshot(tmp_path / "runtime.db")
    assert main(["--source", str(source)]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "dry-run"
    first = import_snapshot(source)
    second = import_snapshot(source)
    assert first == second
    assert first["investigation_sessions"] == 1


def test_old_display_statistics_are_preserved_while_strict_verification_reports_gaps(database, tmp_path):
    incident = write_json(tmp_path / "incidents.json", incident_document())
    assert importing.import_legacy("incidents", incident, apply=True)["ok"]
    source = legacy_runtime_with_display_gap(tmp_path / "runtime.db")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    report = importing.import_legacy("runtime", source, apply=True)
    assert report["ok"] and not report["consistent"], report
    assert len(report["problems"]) == 2
    assert all(problem["severity"] == "warning" for problem in report["problems"])
    with database.connect() as connection:
        raw = connection.execute(tables.investigation_checkpoints.select()).mappings().one()["data"]
    saved = json.loads(raw)
    assert saved["result"]["model_rounds"] == 0
    assert saved["budget"]["model_requests"] == 1
    strict = importing.import_legacy("runtime", source, verify_only=True)
    assert not strict["ok"] and len(strict["problems"]) == 2
    assert not checking.verify()["ok"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("entry_point", ("import", "verify", "runtime"))
def test_cli_rejects_report_overwriting_its_source_before_import(
    database, tmp_path, entry_point,
):
    source = (
        runtime_snapshot(tmp_path / "source.db") if entry_point == "runtime"
        else write_json(tmp_path / "source.json", incident_document())
    )
    before = source.read_bytes()
    if entry_point == "import":
        main = importing.main
        args = ["import-incidents-json", "--apply"]
    elif entry_point == "runtime":
        from db.import_sqlite import main
        args = ["--apply"]
    else:
        main = checking.main
        args = ["--source-kind", "incidents"]
    with pytest.raises(SystemExit) as stopped:
        main([*args, "--source", str(source), "--report", str(source)])
    assert stopped.value.code == 2
    assert source.read_bytes() == before
    with database.connect() as connection:
        assert not connection.execute(tables.incidents.select()).all()
        assert not connection.execute(tables.investigation_sessions.select()).all()


@pytest.mark.parametrize("existing", (False, True))
def test_cli_rejects_report_inside_archive_source_directory(database, tmp_path, existing):
    from legacy_support import archived_snapshot

    source, archive, raw = archived_snapshot(tmp_path)
    report = archive if existing else archive.parent / "new-report.json"
    with pytest.raises(SystemExit) as stopped:
        importing.main([
            "import-incidents-json", "--source", str(source), "--apply",
            "--report", str(report),
        ])
    assert stopped.value.code == 2
    assert archive.read_bytes() == raw
    with database.connect() as connection:
        assert not connection.execute(tables.incidents.select()).all()


@pytest.mark.parametrize("archive_alias", (False, True))
def test_report_hardlink_alias_is_rejected_before_any_write(database, tmp_path, archive_alias):
    from legacy_support import archived_snapshot

    source, archive, _ = archived_snapshot(tmp_path)
    protected = archive if archive_alias else source
    before = protected.read_bytes()
    report = tmp_path / "report-alias.json"
    try:
        report.hardlink_to(protected)
    except PermissionError:
        pytest.skip("host forbids hard links; this alias case requires OS support")
    with pytest.raises(SystemExit) as stopped:
        importing.main([
            "import-incidents-json", "--source", str(source), "--apply",
            "--report", str(report),
        ])
    assert stopped.value.code == 2
    assert protected.read_bytes() == before
    with database.connect() as connection:
        assert not connection.execute(tables.incidents.select()).all()


@pytest.mark.parametrize("sidecar", ("-wal", "-shm", "-journal"))
def test_runtime_report_cannot_overwrite_any_source_sidecar(database, tmp_path, sidecar):
    source = runtime_snapshot(tmp_path / "runtime.db")
    protected = tmp_path / (source.name + sidecar)
    protected.write_bytes(b"source-sidecar-must-not-be-overwritten")
    before = protected.read_bytes()
    with pytest.raises(SystemExit) as stopped:
        importing.main([
            "import-runtime-sqlite", "--source", str(source), "--apply",
            "--report", str(protected),
        ])
    assert stopped.value.code == 2
    assert protected.read_bytes() == before
