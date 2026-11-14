"""Legacy decoding, planning and diagnostic privacy without a MySQL server."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import shutil
import sqlite3

import pytest

from db.import_legacy import plan_import
from db.legacy_sources import Snapshot, decode, read_source
from db.verify_consistency import analyze, compare_source, item_digest, summarize
from legacy_support import (
    archived_snapshot, incident_document, legacy_runtime_with_display_gap,
    remediation_document, runtime_snapshot, STAMP, write_json,
)


@pytest.mark.parametrize("version", (1, 2))
def test_incident_v1_v2_decode_and_active_mapping(tmp_path, version):
    path = write_json(tmp_path / "incidents.json", incident_document(version=version))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    source = read_source("incidents", path)
    assert not analyze(source)
    assert source.rows["incidents"][0]["fingerprint"] == "fp-legacy"
    assert source.rows["incidents"][0]["created_seq"] == 1
    assert source.rows["incident_active"][0]["incident_id"] == "inc-legacy"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("version", (1, 2))
def test_remediation_v1_v2_preserve_fields_order_ids_and_audit(tmp_path, version):
    document = remediation_document(version=version)
    source = read_source("remediation", write_json(tmp_path / "remediation.json", document))
    assert not analyze(source)
    assert decode(source.rows["remediation_state"][0]["data"]) == document
    assert list(decode(source.rows["remediation_state"][0]["data"])["proposals"]) == ["rem-legacy"]


@pytest.mark.parametrize("raw", ('{broken', '{"version":2,"version":1}', '{"version":NaN}', '[]'))
def test_corrupt_or_duplicate_json_is_rejected(tmp_path, raw):
    path = tmp_path / "remediation.json"
    path.write_text(raw, encoding="utf-8")
    source = read_source("remediation", path)
    assert source.problems and not source.rows["remediation_state"]


def test_missing_archive_quarantines_only_its_incident(tmp_path):
    document = incident_document()
    bad = document["incidents"]["inc-legacy"]
    bad["history_archives"] = [{"sha256": "a" * 64, "session_ids": ["ses-archive"]}]
    healthy = incident_document(identifier="inc-good")["incidents"]["inc-good"]
    healthy["fingerprint"] = "fp-good"
    document["incidents"]["inc-good"] = healthy
    document["active_by_fingerprint"]["fp-good"] = "inc-good"
    source = read_source("incidents", write_json(tmp_path / "incidents.json", document))
    problems = analyze(source)
    operations, _, _ = plan_import(source, Snapshot("target"), problems)
    assert {row["incident_id"] for name, row in operations if name == "incidents"} == {"inc-good"}
    assert any(problem.code == "archive_missing_or_invalid" for problem in problems)


def test_duplicate_active_incident_mapping_is_reported_before_writes(tmp_path):
    document = incident_document()
    document["active_by_fingerprint"]["another-fingerprint"] = "inc-legacy"
    source = read_source("incidents", write_json(tmp_path / "incidents.json", document))
    problems = analyze(source)
    assert any(problem.code == "active_mapping_conflict" for problem in problems)
    assert not plan_import(source, Snapshot("target"), problems)[0]


def test_runtime_sqlite_is_read_only_and_partial_missing_tables_are_reported(tmp_path):
    path = runtime_snapshot(tmp_path / "runtime.db", missing=("human_requests",))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    source = read_source("runtime", path)
    assert len(source.rows["investigation_sessions"]) == 1
    assert any(problem.code == "source_table_missing" and problem.table == "human_requests"
               for problem in source.problems)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("sidecar", ("wal", "journal"))
def test_nonempty_sqlite_sidecar_requires_a_consistent_backup(tmp_path, sidecar):
    path = runtime_snapshot(tmp_path / "runtime.db")
    (tmp_path / ("runtime.db-" + sidecar)).write_bytes(b"incomplete-copy")
    snapshot = read_source("runtime", path)
    assert any(problem.code == "sqlite_snapshot_requires_backup_without_" + sidecar
               for problem in snapshot.problems)
    assert not any(snapshot.rows.values())


def test_main_file_copied_without_committed_wal_is_not_a_complete_snapshot(tmp_path):
    path = runtime_snapshot(tmp_path / "live.db")
    with sqlite3.connect(path) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO runtime_metadata VALUES ('committed-in-wal','true')")
        writer.commit()
        incomplete = tmp_path / "incomplete.db"
        shutil.copyfile(path, incomplete)
        before = incomplete.read_bytes()
        snapshot = read_source("runtime", incomplete)
    assert any(problem.code == "sqlite_wal_mode_requires_consistent_backup"
               for problem in snapshot.problems)
    assert not any(snapshot.rows.values())
    assert incomplete.read_bytes() == before


def test_runtime_document_corruption_does_not_leak_payload(tmp_path):
    path = runtime_snapshot(tmp_path / "runtime.db")
    marker = "private-token-do-not-leak"
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE investigation_sessions SET data=?", ('{"token":"' + marker,))
    source = read_source("runtime", path)
    report = summarize(source, analyze(source))
    assert not report["ok"]
    assert marker not in json.dumps(report)


def test_source_order_is_preserved_and_import_plan_is_idempotent(tmp_path):
    source = read_source("runtime", runtime_snapshot(tmp_path / "runtime.db"))
    assert not analyze(source)
    first, unchanged, _ = plan_import(source, Snapshot("target"), [])
    assert first and unchanged == 0
    target = deepcopy(source)
    target.rows["investigation_sessions"][0]["active_incident_id"] = "inc-legacy"
    operations, unchanged, skipped = plan_import(source, target, [])
    assert not operations and skipped == 0
    assert unchanged == sum(len(rows) for rows in source.rows.values())
    assert not compare_source(source, target)


def test_changed_runtime_budget_and_deleted_ids_are_detected(tmp_path):
    source = read_source("runtime", runtime_snapshot(tmp_path / "runtime.db"))
    target = deepcopy(source)
    data = decode(target.rows["investigation_sessions"][0]["data"])
    data["total_model_requests"] = 1
    target.rows["investigation_sessions"][0]["data"] = json.dumps(data)
    assert any(problem.code == "session_event_budget_mismatch" for problem in analyze(target))
    assert any(problem.code == "target_record_differs" for problem in compare_source(source, target))
    target.rows["investigation_sessions"].clear()
    assert any(problem.code == "orphan_foreign_key" for problem in analyze(target))
    assert any(problem.code == "target_record_missing" for problem in compare_source(source, target))


def test_target_conflict_never_replaces_existing_authority(tmp_path):
    source = read_source("remediation", write_json(tmp_path / "rem.json", remediation_document()))
    target = deepcopy(source)
    data = decode(target.rows["remediation_state"][0]["data"])
    data["proposals"]["rem-legacy"]["reason"] = "existing operator decision"
    target.rows["remediation_state"][0]["data"] = json.dumps(data)
    before = deepcopy(target)
    problems = []
    assert not plan_import(source, target, problems)[0]
    assert any(problem.code == "target_conflict" for problem in problems)
    assert target == before


def test_report_does_not_echo_sensitive_ids_or_validation_fields(tmp_path):
    document = remediation_document()
    marker = "sensitive-credential-in-id-and-parameters"
    value = document["proposals"].pop("rem-legacy")
    value["proposal_id"] = marker
    value["parameters"] = {"password": marker}
    document["proposals"][marker] = value
    source = read_source("remediation", write_json(tmp_path / "sensitive.json", document))
    report = summarize(source, analyze(source))
    assert not report["ok"]
    assert marker not in json.dumps(report)
    assert "record_sha256" in json.dumps(report)


def test_digest_ignores_sql_column_order_and_computed_columns(tmp_path):
    source = read_source("runtime", runtime_snapshot(tmp_path / "runtime.db"))
    row = source.rows["investigation_sessions"][0]
    reordered = dict(reversed(list(row.items())))
    reordered["active_incident_id"] = "inc-legacy"
    assert item_digest("investigation_sessions", row) == item_digest("investigation_sessions", reordered)


def test_v1_queue_defaults_are_derived_from_durable_creation_time(tmp_path):
    path = runtime_snapshot(tmp_path / "runtime.db")
    with sqlite3.connect(path) as connection:
        data = json.loads(connection.execute("SELECT data FROM investigation_sessions").fetchone()[0])
        for key in ("queued_at", "priority", "fencing_token", "budget_group_id"):
            data.pop(key)
        connection.execute("UPDATE investigation_sessions SET data=?", (json.dumps(data),))
    one, two = read_source("runtime", path), read_source("runtime", path)
    assert not analyze(one)
    assert not compare_source(one, two)
    saved = decode(one.rows["investigation_sessions"][0]["data"])
    assert saved["queued_at"] == saved["created_at"]


def test_duplicate_active_sessions_are_quarantined_before_a_write_plan(tmp_path):
    path = runtime_snapshot(tmp_path / "runtime.db")
    with sqlite3.connect(path) as connection:
        data = json.loads(connection.execute("SELECT data FROM investigation_sessions").fetchone()[0])
        data["session_id"] = "ses-another-active"
        connection.execute("INSERT INTO investigation_sessions VALUES (?, ?, ?, ?, ?)", (
            data["session_id"], data["incident_id"], data["status"], data["state_version"], json.dumps(data),
        ))
    source = read_source("runtime", path)
    problems = analyze(source)
    assert sum(problem.code == "duplicate_active_runtime_incident" for problem in problems) == 2
    assert not plan_import(source, Snapshot("target"), problems)[0]


def test_unknown_incident_fields_are_retained_in_the_authoritative_document(tmp_path):
    document = incident_document()
    document["incidents"]["inc-legacy"]["future_metadata"] = {"note": "retained"}
    source = read_source("incidents", write_json(tmp_path / "incidents.json", document))
    assert not analyze(source)
    assert decode(source.rows["incidents"][0]["data"])["future_metadata"] == {"note": "retained"}


def test_missing_audit_identity_and_time_are_reported_instead_of_regenerated(tmp_path):
    document = remediation_document(version=1)
    document["audit"][0].pop("event_id")
    document["audit"][0].pop("occurred_at")
    source = read_source("remediation", write_json(tmp_path / "remediation.json", document))
    codes = {problem.code for problem in analyze(source)}
    assert {"remediation_record_id_missing", "remediation_record_timestamp_missing"} <= codes


def test_v1_audit_without_optional_details_is_preserved_and_readable(tmp_path):
    document = remediation_document(version=1)
    document["audit"][0].pop("details")
    source = read_source("remediation", write_json(tmp_path / "remediation.json", document))
    assert not analyze(source)
    assert "details" not in decode(source.rows["remediation_state"][0]["data"])["audit"][0]


@pytest.mark.parametrize("command_id", ([], {"secret": "private-value"}, "", 7))
def test_invalid_audit_command_identity_is_redacted_instead_of_crashing(tmp_path, command_id):
    document = remediation_document()
    document["audit"][0]["details"]["command_id"] = command_id
    source = read_source("remediation", write_json(tmp_path / "remediation.json", document))
    problems = analyze(source)
    assert any(problem.code == "remediation_command_id_invalid" for problem in problems)
    assert not plan_import(source, Snapshot("target"), problems)[0]
    assert "private-value" not in json.dumps(summarize(source, problems))


def test_archive_checksum_manifest_and_history_counts(tmp_path):
    source, archive, raw = archived_snapshot(tmp_path)
    snapshot = read_source("incidents", source)
    assert not analyze(snapshot)
    assert summarize(snapshot, [])['history_counts'] == {"hot": 0, "archived": 1, "total": 1}
    target = deepcopy(snapshot)
    target.rows["incident_history_archives"][0]["body"] = "{}"
    assert any(problem.code == "archive_checksum_or_manifest_invalid" for problem in analyze(target))
    assert archive.read_bytes() == raw


def test_corrupt_archive_file_is_not_importable_and_does_not_change_source(tmp_path):
    source, archive, _ = archived_snapshot(tmp_path)
    archive.write_text('{"private_token":"synthetic-private-value"}', encoding="utf-8")
    before = archive.read_bytes()
    snapshot = read_source("incidents", source)
    problems = analyze(snapshot)
    assert not plan_import(snapshot, Snapshot("target"), problems)[0]
    assert "synthetic-private-value" not in json.dumps(summarize(snapshot, problems))
    assert archive.read_bytes() == before


def test_non_numeric_display_values_are_not_echoed_in_diagnostics():
    from db.legacy_sources import Problem

    marker = "private-token-in-statistics"
    problem = Problem(
        "checkpoint_result_display_count_mismatch", "investigation_checkpoints",
        field="model_rounds", expected=1, actual=marker,
    )
    assert problem.public()["actual_type"] == "str"
    assert marker not in json.dumps(problem.public())


def test_phase_only_events_are_reported_as_legacy_identity_gaps(tmp_path):
    path = legacy_runtime_with_display_gap(tmp_path / "runtime.db")
    problems = analyze(read_source("runtime", path))
    codes = {problem.code for problem in problems}
    assert "legacy_checkpoint_event_identity_unavailable" in codes
    assert "checkpoint_saved_event_missing" not in codes
    assert all(problem.severity == "warning" for problem in problems)


def test_missing_saved_event_is_an_error_and_quarantines_the_session(tmp_path):
    path = legacy_runtime_with_display_gap(tmp_path / "runtime.db")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM investigation_events WHERE event_type='checkpoint_saved'",
        )
    source = read_source("runtime", path)
    problems = analyze(source)
    missing = [problem for problem in problems
               if problem.code == "checkpoint_saved_event_missing"]
    assert len(missing) == 1 and missing[0].severity == "error"
    assert not plan_import(source, Snapshot("target"), problems)[0]


@pytest.mark.parametrize("payload", (
    {"checkpoint_id": "cp-unknown", "phase": "response_saved"},
    {"checkpoint_id": None, "phase": "response_saved"},
    {"checkpoint_id": "cp-legacy", "phase": "tools_executed"},
))
def test_invalid_saved_event_reference_is_never_accepted_as_legacy(tmp_path, payload):
    path = legacy_runtime_with_display_gap(tmp_path / "runtime.db")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE investigation_events SET data=? WHERE event_type='checkpoint_saved'",
            (json.dumps(payload),),
        )
    source = read_source("runtime", path)
    problems = analyze(source)
    assert any(problem.code == "event_sequence_or_budget_invalid"
               and problem.severity == "error" for problem in problems)
    assert not plan_import(source, Snapshot("target"), problems)[0]


def test_current_saved_event_with_matching_identity_phase_and_budget_passes(tmp_path):
    path = legacy_runtime_with_display_gap(tmp_path / "runtime.db")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE investigation_events SET data=? WHERE event_type='checkpoint_saved'",
            (json.dumps({"checkpoint_id": "cp-legacy", "phase": "response_saved"}),),
        )
        checkpoint = json.loads(connection.execute(
            "SELECT data FROM investigation_checkpoints",
        ).fetchone()[0])
        checkpoint["result"]["model_rounds"] = 1
        connection.execute(
            "UPDATE investigation_checkpoints SET data=?",
            (json.dumps(checkpoint),),
        )
    assert not analyze(read_source("runtime", path))


def test_one_legacy_saved_event_cannot_cover_two_checkpoints(tmp_path):
    path = legacy_runtime_with_display_gap(tmp_path / "runtime.db")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO investigation_checkpoints "
            "SELECT 'cp-second',session_id,created_at,data FROM investigation_checkpoints",
        )
        session = json.loads(connection.execute(
            "SELECT data FROM investigation_sessions",
        ).fetchone()[0])
        session["checkpoint_id"] = "cp-second"
        connection.execute(
            "UPDATE investigation_sessions SET data=?",
            (json.dumps(session),),
        )
    problems = analyze(read_source("runtime", path))
    assert sum(problem.code == "legacy_checkpoint_event_identity_unavailable"
               for problem in problems) == 1
    assert any(problem.code == "checkpoint_saved_event_missing"
               and problem.severity == "error" for problem in problems)


def test_pruned_checkpoint_audit_before_retained_window_is_allowed(tmp_path):
    path = legacy_runtime_with_display_gap(tmp_path / "runtime.db")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO investigation_events "
            "SELECT 3,'evt-pruned',session_id,event_type,?,actor,? "
            "FROM investigation_events WHERE event_type='checkpoint_saved'",
            ((STAMP - timedelta(seconds=1)).isoformat(), json.dumps({
                "checkpoint_id": "cp-pruned", "phase": "response_saved",
            })),
        )
    problems = analyze(read_source("runtime", path))
    assert all(problem.severity == "warning" for problem in problems)
    assert not any(problem.code == "event_sequence_or_budget_invalid"
                   for problem in problems)


def test_old_phase_only_event_cannot_hide_a_newer_missing_checkpoint_event(tmp_path):
    path = legacy_runtime_with_display_gap(tmp_path / "runtime.db")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE investigation_events SET timestamp=? "
            "WHERE event_type='checkpoint_saved'",
            ((STAMP - timedelta(seconds=1)).isoformat(),),
        )
    problems = analyze(read_source("runtime", path))
    assert any(problem.code == "checkpoint_saved_event_missing"
               and problem.severity == "error" for problem in problems)
