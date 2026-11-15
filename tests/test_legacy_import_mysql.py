"""Actual MySQL migration/import/verification tests, never an operator schema."""
import hashlib
import logging
from pathlib import Path
import unittest

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from db import tables
from db.config import get_engine
from db.import_legacy import import_legacy
from db.verify_consistency import verify
from incident_store import IncidentStore
from investigation.store import RuntimeStore
from legacy_support import (
    archived_snapshot, incident_document, remediation_document, runtime_snapshot,
    write_json,
)
from mysql_support import empty_database_url, remediation_digest
from remediation.store import RemediationStore, StoreError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def migrated(tmp_path, monkeypatch):
    url = empty_database_url(tmp_path / "migrated")
    monkeypatch.setenv("INCIDENT_AGENT_DATABASE_URL", url)
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    return url


def test_programmatic_migration_preserves_application_logs(tmp_path, monkeypatch):
    loggers = [
        logging.getLogger(name)
        for name in (
            "agent",
            "remediation.executor",
            "remediation.demo_action",
            "web_api.app",
        )
    ]
    url = empty_database_url(tmp_path / "logging")
    monkeypatch.setenv("INCIDENT_AGENT_DATABASE_URL", url)
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")

    for logger in loggers:
        with unittest.TestCase().assertLogs(logger, level="ERROR") as captured:
            logger.error("application_log_after_migration")
        assert captured.records[0].message == "application_log_after_migration"


def test_programmatic_migration_keeps_application_file_handler_open(
    tmp_path,
    monkeypatch,
):
    url = empty_database_url(tmp_path / "logging-handles")
    monkeypatch.setenv("INCIDENT_AGENT_DATABASE_URL", url)
    path = tmp_path / "application.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    stream = handler.stream
    logger = logging.getLogger("migration_application_file")
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "level", logging.ERROR)
    monkeypatch.setattr(logger, "propagate", False)
    try:
        logger.error("before_migration")
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        logger.error("after_migration")
        assert not stream.closed
        assert handler.stream is stream
    finally:
        handler.close()
    assert path.read_text(encoding="utf-8").splitlines() == [
        "before_migration",
        "after_migration",
    ]
    path.unlink()


@pytest.mark.parametrize("start", ("base", "0004_incident_store"))
def test_clean_and_0004_upgrade_create_single_seed_without_changing_incidents(tmp_path, monkeypatch, start):
    url = empty_database_url(tmp_path / start)
    monkeypatch.setenv("INCIDENT_AGENT_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, start)
    if start != "base":
        from models.incident import Incident
        IncidentStore(url).save_incident("preserved", Incident(
            incident_id="preserved", service="fixture", severity="warning",
            status="investigating", alerts=[],
        ))
    command.upgrade(config, "head")
    store = RemediationStore(url)
    assert store.load()["proposals"] == {}
    if start != "base":
        assert IncidentStore(url).get_active_incident("preserved").incident_id == "preserved"
    command.downgrade(config, "0004_incident_store")
    command.upgrade(config, "head")
    assert store.load()["proposals"] == {}


def test_downgrade_refuses_to_delete_existing_approvals(migrated, tmp_path):
    source = write_json(tmp_path / "remediation.json", remediation_document())
    assert import_legacy("remediation", source, migrated, apply=True)["ok"]
    store = RemediationStore(migrated)
    store.approve("rem-legacy", "fixture-approver")
    before = remediation_digest(store)
    with pytest.raises(RuntimeError, match="requires_export"):
        command.downgrade(Config(str(ROOT / "alembic.ini")), "0004_incident_store")
    assert store.get("rem-legacy").approved_by == "fixture-approver"
    assert remediation_digest(store) == before


@pytest.mark.parametrize("kind,version", (("incidents", 1), ("incidents", 2), ("remediation", 1), ("remediation", 2), ("runtime", 1)))
def test_preview_apply_repeat_verify_and_public_reads(migrated, tmp_path, kind, version):
    incidents = write_json(tmp_path / "incidents.json", incident_document(version=version if kind == "incidents" else 2))
    if kind != "incidents":
        assert import_legacy("incidents", incidents, migrated, apply=True)["ok"]
    source = incidents if kind == "incidents" else (
        write_json(tmp_path / "remediation.json", remediation_document(version=version))
        if kind == "remediation" else runtime_snapshot(tmp_path / "runtime.db")
    )
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    original = remediation_digest(RemediationStore(migrated))
    preview = import_legacy(kind, source, migrated)
    assert preview["ok"] and preview["mode"] == "dry-run" and preview["planned"] > 0
    assert remediation_digest(RemediationStore(migrated)) == original
    applied = import_legacy(kind, source, migrated, apply=True)
    assert applied["ok"] and applied["imported"] == preview["planned"], applied
    repeated = import_legacy(kind, source, migrated, apply=True)
    assert repeated["ok"] and repeated["imported"] == 0
    assert import_legacy(kind, source, migrated, verify_only=True)["ok"]
    assert verify(migrated, source=source, source_kind=kind)["ok"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert IncidentStore(migrated).get_active_incident("fp-legacy").incident_id == "inc-legacy"
    if kind == "remediation":
        assert RemediationStore(migrated).get("rem-legacy").proposal_id == "rem-legacy"
    if kind == "runtime":
        assert RuntimeStore(migrated).get("ses-legacy").incident_id == "inc-legacy"


def test_archive_body_manifest_hash_and_hydration(migrated, tmp_path):
    source, archive, raw = archived_snapshot(tmp_path)
    assert import_legacy("incidents", source, migrated, apply=True)["ok"]
    restored = IncidentStore(migrated).get_incident("inc-legacy")
    assert [item.session_id for item in restored.investigations] == ["ses-archived"]
    assert verify(migrated)["ok"]
    with get_engine(migrated).begin() as connection:
        connection.execute(update(tables.incident_history_archives).values(body="{}"))
    report = verify(migrated)
    assert not report["ok"]
    assert any(problem["code"] == "archive_checksum_or_manifest_invalid" for problem in report["problems"])
    assert archive.read_bytes() == raw


def test_conflict_never_overwrites_authority_and_partial_records_are_isolated(migrated, tmp_path):
    source = write_json(tmp_path / "incidents.json", incident_document())
    assert import_legacy("incidents", source, migrated, apply=True)["ok"]
    document = incident_document()
    document["incidents"]["inc-legacy"]["severity"] = "critical"
    good = incident_document(identifier="inc-good")["incidents"]["inc-good"]
    good["fingerprint"] = "fp-good"
    document["incidents"]["inc-good"] = good
    document["active_by_fingerprint"]["fp-good"] = "inc-good"
    write_json(source, document)
    report = import_legacy("incidents", source, migrated, apply=True)
    assert not report["ok"] and report["imported"] == 2
    assert IncidentStore(migrated).get_incident("inc-legacy").severity == "warning"
    assert IncidentStore(migrated).get_incident("inc-good") is not None


def test_fail_fast_rolls_back_entire_plan(migrated, tmp_path):
    document = incident_document()
    document["incidents"]["inc-legacy"]["history_archives"] = [{
        "sha256": "b" * 64, "session_ids": ["missing-session"],
    }]
    document["incidents"]["inc-good"] = incident_document(identifier="inc-good")["incidents"]["inc-good"]
    source = write_json(tmp_path / "incidents.json", document)
    report = import_legacy("incidents", source, migrated, apply=True, fail_fast=True)
    assert not report["ok"] and report["imported"] == 0
    assert not IncidentStore(migrated).load()["incidents"]


def test_target_must_be_at_head_before_any_write(tmp_path, monkeypatch):
    url = empty_database_url(tmp_path / "old-schema")
    monkeypatch.setenv("INCIDENT_AGENT_DATABASE_URL", url)
    command.upgrade(Config(str(ROOT / "alembic.ini")), "0004_incident_store")
    source = write_json(tmp_path / "incidents.json", incident_document())
    report = import_legacy("incidents", source, url, apply=True)
    assert not report["ok"] and report["imported"] == 0
    assert not IncidentStore(url).load()["incidents"]


def test_advisory_execution_lock_is_shared_across_two_schemas(migrated, tmp_path):
    second = empty_database_url(tmp_path / "second")
    one, two = RemediationStore(migrated), RemediationStore(second)
    with one.service_lock("fixture-shared-service"):
        with pytest.raises(StoreError, match="service_busy"):
            with two.service_lock("fixture-shared-service"):
                pass
    with two.service_lock("fixture-shared-service"):
        pass


def test_real_mysql_commit_with_lost_ack_is_unknown_and_never_replayed(migrated, tmp_path, monkeypatch):
    source = write_json(tmp_path / "incidents.json", incident_document())
    original = Connection.commit

    def lose_ack(connection):
        original(connection)
        raise SQLAlchemyError("synthetic-sensitive-commit-error")

    with monkeypatch.context() as failure:
        failure.setattr(Connection, "commit", lose_ack)
        report = import_legacy("incidents", source, migrated, apply=True)
    assert not report["ok"] and report["write_outcome"] == "unknown"
    assert report["imported"] == 0
    assert IncidentStore(migrated).get_incident("inc-legacy") is not None
    repeated = import_legacy("incidents", source, migrated, apply=True)
    assert repeated["ok"] and repeated["imported"] == 0 and repeated["unchanged"] == 2


@pytest.mark.parametrize("fail_fast", (False, True))
def test_real_mysql_savepoint_failure_is_isolated_or_entirely_rolled_back(
    migrated, tmp_path, monkeypatch, fail_fast,
):
    import db.import_legacy as importing

    document = incident_document()
    good = incident_document(identifier="inc-good")["incidents"]["inc-good"]
    good["fingerprint"] = "fp-good"
    document["incidents"]["inc-good"] = good
    document["active_by_fingerprint"]["fp-good"] = "inc-good"
    source = write_json(tmp_path / "incidents.json", document)
    original = importing._write_group

    def fail_after_insert(connection, rows):
        original(connection, rows)
        if any(values.get("incident_id") == "inc-legacy" for _, values in rows):
            raise SQLAlchemyError("synthetic-sensitive-write-error")

    monkeypatch.setattr(importing, "_write_group", fail_after_insert)
    report = import_legacy("incidents", source, migrated, apply=True, fail_fast=fail_fast)
    assert not report["ok"]
    assert report["imported"] == (0 if fail_fast else 2)
    store = IncidentStore(migrated)
    assert store.get_incident("inc-legacy") is None
    assert (store.get_incident("inc-good") is None) == fail_fast
