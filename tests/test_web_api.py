"""HTTP behavior against temporary JSON/SQLite, with no live model or repair."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from incident_store import IncidentStore
from investigation.models import SessionStatus as S
from investigation.state import RuntimeErrorBase
from investigation.store import RuntimeStore
from investigation.worker import Worker
from models.incident import Evidence, HistoryArchive
from runtime_support import FixtureAgent, call, create_runtime, create_session, response
from web_api.app import create_app
from web_api.__main__ import main as web_main

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def api(tmp_path):
    manager = create_runtime(tmp_path)
    session = create_session(manager)
    app = create_app(incident_path=manager.incident_store.file_path, runtime_path=manager.store.path)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        yield manager, session, client


def test_health_missing_resources_docs_and_read_only_methods(api):
    manager, session, client = api
    assert client.get("/healthz").json() == {"status": "ok", "read_only": True}
    assert client.get("/api/incidents/missing").status_code == 404
    assert client.get("/api/sessions/missing").status_code == 404
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    for path in ("/api/incidents", "/api/sessions", f"/api/sessions/{session.session_id}"):
        assert client.post(path, json={"command_type": "cancel"}).status_code == 405
    assert client.get("/healthz", headers={"host": "untrusted.example"}).status_code == 400


def test_queries_match_existing_persisted_result_and_budget(api):
    manager, session, client = api
    completed = Worker(manager, FixtureAgent([response(answer="明确的调查结论")])).run_once()
    incident = client.get(f"/api/incidents/{session.incident_id}").json()
    detail = client.get(f"/api/sessions/{session.session_id}").json()
    assert incident["incident_id"] == session.incident_id
    assert incident["recent_investigations"][0]["answer"] == "明确的调查结论"
    assert incident["history_included"] is False
    assert detail["session"]["status"] == "COMPLETED"
    assert detail["session"]["total_model_requests"] == completed.total_model_requests == 1
    assert detail["session"]["total_tool_executions"] == completed.total_tool_executions == 1
    assert detail["incident_budget_usage"]["model_requests"] == 1
    assert detail["limits"]["max_model_requests"] == manager.limits.max_model_requests
    assert detail["investigation_result"]["answer"] == "明确的调查结论"
    assert detail["projection"] == {"projected": True, "blocked": False, "error_category": None, "retry_at": None}
    assert detail["proposal_generation"]["status"] == "not_pending"
    assert detail["human_requests"] == []


def test_human_request_and_proposal_generation_are_queryable(api):
    manager, session, client = api
    agent = FixtureAgent([response(answer="", calls=[call("request_human_input", {"question": "何时发布的版本？"})])])
    waiting = Worker(manager, agent).run_once()
    assert waiting.status == S.WAITING_HUMAN
    detail = client.get(f"/api/sessions/{session.session_id}").json()
    assert detail["human_requests"][0]["question"] == "何时发布的版本？"
    assert detail["human_requests"][0]["status"] == "pending"
    with manager.store.transaction() as db:
        current = manager.store.get(session.session_id, db)
        checkpoint = manager.store.load_checkpoint(current, db)
        checkpoint.update(remediation_pending=True, remediation_blocked=True,
                          remediation_blocked_reason="non_retryable_error", remediation_attempts=2,
                          remediation_error_category="PermissionError", proposal_evidence_gap="missing_version")
        manager.store.checkpoint(current, checkpoint, db)
        manager.store.save(current, db)
    state = client.get(f"/api/sessions/{session.session_id}").json()["proposal_generation"]
    assert state["status"] == "blocked" and state["attempts"] == 2
    assert state["error_category"] == "PermissionError"
    assert state["evidence_gap"] == "missing_version"


@pytest.mark.parametrize("endpoint", ["incidents", "sessions"])
def test_bounded_pagination_filters_and_stable_order(api, endpoint):
    manager, first, client = api
    second = create_session(manager, service="inventory-service")
    third = create_session(manager, service="order-service", suffix="third")
    page = client.get(f"/api/{endpoint}", params={"limit": 1}).json()
    id_field = "incident_id" if endpoint == "incidents" else "session_id"
    assert page["items"][0][id_field] == getattr(third, id_field)
    assert page["has_more"] is True
    next_page = client.get(f"/api/{endpoint}", params={"limit": 1, "offset": 1}).json()
    assert next_page["items"][0][id_field] == getattr(second, id_field)
    assert client.get(f"/api/{endpoint}", params={"offset": 10000}).json()["items"] == []
    status = "investigating" if endpoint == "incidents" else "QUEUED"
    filtered = client.get(f"/api/{endpoint}", params={"status": status}).json()
    assert len(filtered["items"]) == 3
    empty_status = "resolved" if endpoint == "incidents" else "COMPLETED"
    assert client.get(f"/api/{endpoint}", params={"status": empty_status}).json()["items"] == []
    if endpoint == "sessions":
        assert len(client.get("/api/sessions", params={"incident_id": first.incident_id}).json()["items"]) == 1


@pytest.mark.parametrize("endpoint", ["incidents", "sessions"])
@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1},
                                   {"offset": 10001}, {"limit": "no"}, {"status": "invalid"}])
def test_pagination_boundaries(api, endpoint, params):
    assert api[2].get(f"/api/{endpoint}", params=params).status_code == 422


def test_raw_secrets_and_model_tool_data_are_excluded(api, monkeypatch):
    manager, session, client = api
    monkeypatch.setenv("TEST_API_KEY", "configured-secret-987654")
    secret = "configured-secret-987654 password='database secret' api_key=old-secret ghp_12345678901234567890 postgresql://alice:dbpass@localhost/db"
    agent = FixtureAgent([response(answer=secret + "\n" + "结果" * 5000)])
    completed = Worker(manager, agent).run_once()
    incident = manager.incident_store.get_incident(session.incident_id, include_history=False)
    incident.evidence.append(Evidence(source="fixture", summary=secret, detail="raw-tool-secret-123"))
    incident.alerts[0].labels["api_key"] = "raw-label-secret-123"
    manager.incident_store.save_incident(incident.fingerprint, incident)
    with manager.store.transaction() as db:
        current = manager.store.get(completed.session_id, db)
        payload = manager.store.load_checkpoint(current, db)
        payload["history"] = [{"raw_model_request": "raw-model-secret-123"}]
        payload["messages"].append({"text": "raw-human-secret-123"})
        manager.store.checkpoint(current, payload, db)
        manager.store.save(current, db)
    for path in (f"/api/incidents/{session.incident_id}", f"/api/sessions/{session.session_id}"):
        result = client.get(path)
        assert result.status_code == 200
        for forbidden in ("configured-secret-987654", "database secret", "old-secret", "ghp_12345678901234567890",
                          "dbpass", "raw-tool-secret-123", "raw-label-secret-123", "raw-model-secret-123", "raw-human-secret-123",
                          "owner_token", "fencing_token", "tool_trace", "checkpoint_summary"):
            assert forbidden not in result.text
    answer = client.get(f"/api/sessions/{session.session_id}").json()["investigation_result"]
    assert answer["answer_truncated"] is True and len(answer["answer"]) == 8000


@pytest.mark.parametrize("corruption", ["json", "json_shape", "incident", "sqlite", "schema", "checkpoint", "session"])
def test_store_corruption_is_a_safe_failure(api, corruption, caplog):
    manager, session, client = api
    if corruption in {"json", "json_shape", "incident"}:
        if corruption == "json":
            manager.incident_store.file_path.write_text('{"password":"hidden-error-secret"', encoding="utf-8")
        else:
            data = manager.incident_store.load()
            if corruption == "json_shape":
                data["incidents"] = []
            else:
                data["incidents"][session.incident_id]["alerts"] = "hidden-error-secret"
            manager.incident_store.file_path.write_text(json.dumps(data), encoding="utf-8")
        path = "/api/incidents"
    else:
        path = f"/api/sessions/{session.session_id}"
        if corruption == "sqlite":
            manager.store.path.write_bytes(b"not SQLite hidden-error-secret")
        else:
            if corruption == "checkpoint":
                with manager.store.transaction() as db:
                    current = manager.store.get(session.session_id, db)
                    manager.store.checkpoint(current, manager.store.load_checkpoint(current, db), db)
                    manager.store.save(current, db)
            with sqlite3.connect(manager.store.path) as db:
                if corruption == "schema":
                    db.execute("PRAGMA user_version=999")
                elif corruption == "session":
                    db.execute("UPDATE investigation_sessions SET data=?", ('{"password":"hidden-error-secret"}',))
                else:
                    current = manager.store.get(session.session_id)
                    assert current.checkpoint_id is not None
                    db.execute("DELETE FROM investigation_checkpoints WHERE checkpoint_id=?", (current.checkpoint_id,))
    result = client.get(path)
    assert result.status_code == 500 and result.json() == {"detail": "store_read_failed"}
    assert "hidden-error-secret" not in result.text + caplog.text
    assert "Traceback" not in result.text
    assert "Read-only API query failed category=" in caplog.text


def test_detail_never_opens_history_archives(api):
    manager, session, client = api
    incident = manager.incident_store.get_incident(session.incident_id, include_history=False)
    incident.history_archives = [HistoryArchive(sha256="a" * 64, session_ids=["ses-archived"])]
    # Direct fixture write deliberately references an absent archive.
    data = manager.incident_store.load()
    data["incidents"][incident.incident_id] = incident.model_dump(mode="json")
    manager.incident_store.file_path.write_text(json.dumps(data), encoding="utf-8")
    with patch.object(IncidentStore, "_load_archive", side_effect=AssertionError("archive must not be read")):
        result = client.get(f"/api/incidents/{session.incident_id}")
        assert result.status_code == 200 and result.json()["archived_session_count"] == 1
        assert client.get("/api/incidents").status_code == 200


def test_creation_and_startup_do_not_create_files_or_business_objects(tmp_path):
    root = tmp_path / "absent"
    with patch.object(RuntimeStore, "__init__", side_effect=AssertionError("startup opened SQLite")), \
            patch.object(IncidentStore, "__init__", side_effect=AssertionError("startup opened JSON")), \
            patch("openai.OpenAI", side_effect=AssertionError("startup created model client")), \
            patch("investigation.scheduler.Scheduler.__init__", side_effect=AssertionError("startup scheduler")), \
            patch("investigation.worker.Worker.__init__", side_effect=AssertionError("startup worker")), \
            patch("monitor.incident_monitor.IncidentMonitor.__init__", side_effect=AssertionError("startup monitor")):
        app = create_app(incident_path=root / "incidents.json", runtime_path=root / "runtime.sqlite3")
        with TestClient(app, base_url="http://127.0.0.1") as client:
            assert client.get("/healthz").status_code == 200
            assert client.get("/docs").status_code == 200
    assert not root.exists()


def test_import_is_inert_in_a_fresh_process(tmp_path):
    code = """
import pathlib, sys, threading
sys.path.insert(0, sys.argv[1])
from incident_store import IncidentStore
from investigation.store import RuntimeStore
def forbidden(*args, **kwargs):
    raise AssertionError('import opened a store or started a thread')
IncidentStore.__init__ = forbidden
RuntimeStore.__init__ = forbidden
threading.Thread.start = forbidden
import web_api.app, web_api.__main__
assert 'openai' not in sys.modules
assert 'investigation.worker' not in sys.modules
assert 'investigation.scheduler' not in sys.modules
assert 'monitor.incident_monitor' not in sys.modules
assert not list(pathlib.Path.cwd().iterdir())
print('inert import verified')
"""
    child = subprocess.run([sys.executable, "-B", "-c", code, str(ROOT)], cwd=tmp_path,
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr


def test_missing_stores_are_unavailable_and_not_created(tmp_path):
    app = create_app(incident_path=tmp_path / "incidents.json", runtime_path=tmp_path / "runtime.sqlite3")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/healthz").status_code == 200
        for path in ("/api/incidents", "/api/sessions"):
            result = client.get(path)
            assert result.status_code == 503 and result.json() == {"detail": "store_unavailable"}
    assert not list(tmp_path.iterdir())


def test_store_read_only_mode_and_request_hashes(api):
    manager, session, client = api
    paths = (manager.store.path, manager.incident_store.file_path)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    files_before = set(paths[0].parent.rglob("*"))
    readonly = RuntimeStore(paths[0], read_only=True)
    with pytest.raises(RuntimeErrorBase, match="read_only"):
        with readonly.transaction():
            pass
    with readonly.read() as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM investigation_sessions")
    incidents = IncidentStore(paths[1], read_only=True)
    with pytest.raises(PermissionError, match="read_only"):
        incidents.save(incidents.create_empty_data())
    for path in ("/api/incidents", "/api/sessions", f"/api/incidents/{session.incident_id}", f"/api/sessions/{session.session_id}"):
        assert client.get(path).status_code == 200
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    assert set(paths[0].parent.rglob("*")) == files_before


def test_concurrent_readers_observe_consistent_uncommitted_writer_snapshot(api):
    manager, session, client = api
    ready, release = threading.Event(), threading.Event()

    def writer():
        with manager.store.transaction() as db:
            current = manager.store.get(session.session_id, db)
            current.total_model_requests = 1
            manager.store.save(current, db)
            ready.set()
            assert release.wait(15)

    with ThreadPoolExecutor(max_workers=5) as pool:
        future = pool.submit(writer)
        try:
            assert ready.wait(10)
            reads = [pool.submit(client.get, f"/api/sessions/{session.session_id}") for _ in range(4)]
            for read in reads:
                result = read.result(timeout=10)
                assert result.status_code == 200
                detail = result.json()
                assert detail["session"]["total_model_requests"] == detail["incident_budget_usage"]["model_requests"] == 0
        finally:
            release.set()
        future.result(timeout=10)
    assert client.get(f"/api/sessions/{session.session_id}").json()["session"]["total_model_requests"] == 1


@pytest.mark.parametrize("argv,port", [([], 8010), (["--port", "8011"], 8011),
                                     (["--port", "1"], 1), (["--port", "65535"], 65535)])
def test_launcher_fixes_loopback_and_does_not_trust_proxy_headers(argv, port):
    with patch("uvicorn.run") as run:
        web_main(argv)
    run.assert_called_once_with("web_api.app:app", host="127.0.0.1", port=port, proxy_headers=False)


@pytest.mark.parametrize("argv", [["--port", "0"], ["--port", "65536"], ["--port", "text"],
                                  ["--host", "0.0.0.0"]])
def test_launcher_rejects_invalid_ports_and_public_bindings(argv):
    with patch("uvicorn.run") as run, pytest.raises(SystemExit) as stopped:
        web_main(argv)
    assert stopped.value.code == 2
    run.assert_not_called()


def test_incident_open_filter_is_rejected_without_reading_stores(api):
    with patch("web_api.app.RuntimeQueries.incidents", side_effect=AssertionError("invalid status opened store")):
        result = api[2].get("/api/incidents", params={"status": "open"})
    assert result.status_code == 422


def test_legacy_data_is_mapped_without_migration_or_backfill(api):
    manager, session, client = api
    incident = manager.incident_store.get_incident(session.incident_id, include_history=False)
    # Legacy v1 JSON and the original SQLite v1 database are read in place.
    manager.incident_store.file_path.write_text(json.dumps({incident.fingerprint: incident.model_dump(mode="json")}), encoding="utf-8")
    with sqlite3.connect(manager.store.path) as db:
        db.execute("DELETE FROM runtime_metadata WHERE key='work_outboxes_v1'")
        db.execute("DROP INDEX sessions_budget_group")
    paths = (manager.incident_store.file_path, manager.store.path)
    before = {p: p.read_bytes() for p in paths}
    assert client.get("/api/incidents").json()["items"][0]["incident_id"] == session.incident_id
    assert client.get(f"/api/sessions/{session.session_id}").json()["session"]["status"] == "QUEUED"
    assert before == {p: p.read_bytes() for p in paths}


def test_old_plaintext_answers_are_redacted_without_rewriting_store(api):
    from models.investigation import InvestigationRecord
    from investigation.models import utc_now

    manager, session, client = api
    data = manager.incident_store.load()
    record = InvestigationRecord(investigated_at=utc_now(), status="completed",
                                 answer='{"api_key":"old-unconfigured-value"} API Key:other-unconfigured-value password="db password"')
    data["incidents"][session.incident_id]["investigations"] = [record.model_dump(mode="json")]
    manager.incident_store.file_path.write_text(json.dumps(data), encoding="utf-8")
    before = manager.incident_store.file_path.read_bytes()
    result = client.get(f"/api/incidents/{session.incident_id}")
    assert result.status_code == 200
    for secret in ("old-unconfigured-value", "other-unconfigured-value", "db password"):
        assert secret not in result.text
    assert manager.incident_store.file_path.read_bytes() == before
