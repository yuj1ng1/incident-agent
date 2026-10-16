"""Publication races use real temporary stores and explicit synchronization."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import threading
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from investigation.models import RuntimeLimits, SessionStatus as S
from investigation.state import transition
from investigation.worker import Worker
from models.investigation import InvestigationRecord
from runtime_support import FixtureAgent, call, create_runtime, create_session
from web_api.queries import RuntimeQueries


def test_recovery_snapshot_cannot_publish_after_worker_checkpoint(tmp_path):
    manager = create_runtime(tmp_path, clock=lambda: datetime.now(timezone.utc))
    session = create_session(manager)
    store = manager.store
    worker = Worker(manager, FixtureAgent())
    claimed = store.claim(worker.owner, manager.limits)
    claim = (worker.owner, claimed.fencing_token)
    payload = store.load_checkpoint(claimed)
    worker._new_attempt(session.session_id, claim, payload)
    worker._tool(session.session_id, claim, payload, SimpleNamespace(**call()))
    before = manager.incident_store.file_path.read_bytes()
    snapshot_ready, release = threading.Event(), threading.Event()
    original_read = store.read
    failures = []

    @contextmanager
    def read_barrier():
        with original_read() as db:
            yield db
        if threading.current_thread().name == "recovery-projection":
            snapshot_ready.set()
            if not release.wait(10):
                raise TimeoutError("snapshot barrier")

    def project():
        try:
            manager.sync_incident(session.session_id)
        except Exception as exc:
            failures.append(exc)

    with patch.object(store, "read", side_effect=read_barrier), patch.object(
        manager.incident_store, "apply_runtime_projection",
        wraps=manager.incident_store.apply_runtime_projection,
    ) as publication:
        thread = threading.Thread(target=project, name="recovery-projection")
        thread.start()
        try:
            assert snapshot_ready.wait(10)
            payload["messages"].append({"text": "new worker context"})
            newer = worker._save(session.session_id, claim, payload)
        finally:
            release.set()
            thread.join(10)
        assert not thread.is_alive()
        assert not failures
        publication.assert_not_called()

    assert manager.incident_store.file_path.read_bytes() == before
    latest = store.get(session.session_id)
    assert latest.checkpoint_id == newer.checkpoint_id
    assert not store.load_checkpoint(latest)["projected"]
    assert "incident_projection_superseded" in [e["event_type"] for e in store.events(session.session_id)]
    manager.sync_incident(session.session_id)
    projected = manager.incident_store.get_incident(session.incident_id, include_history=False)
    assert len(projected.evidence) == 1
    assert store.load_checkpoint(store.get(session.session_id))["projected"]


@pytest.mark.parametrize("with_claim", [False, True])
def test_publication_keeps_writer_fencing_until_json_commit(tmp_path, with_claim):
    manager = create_runtime(tmp_path)
    session = create_session(manager)
    claimed = manager.store.claim("fixture-worker", manager.limits)
    project = manager.incident_store.apply_runtime_projection

    def locked_publication(current, checkpoint):
        # A second real SQLite connection cannot replace the snapshot mid-write.
        with sqlite3.connect(manager.store.path, timeout=0, isolation_level=None) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        return project(current, checkpoint)

    with patch.object(manager.incident_store, "apply_runtime_projection", side_effect=locked_publication):
        manager.sync_incident(session.session_id, claim=("fixture-worker", claimed.fencing_token) if with_claim else None)
    assert manager.store.load_checkpoint(manager.store.get(session.session_id))["projected"]


@pytest.mark.parametrize("missing_result", [{}, {"result": None}], ids=["absent", "null"])
def test_exhausted_projection_without_result_is_failed_and_idempotent(tmp_path, missing_result):
    manager = create_runtime(tmp_path)
    session = create_session(manager)
    with manager.store.transaction() as db:
        current = manager.store.get(session.session_id, db)
        transition(current, S.EXHAUSTED)
        current.attempt_count = 1
        current.total_model_requests = 1
        current.last_error_category = "incident_model_budget_exhausted"
        current.completed_at = manager.store.clock()
        checkpoint = manager.store.load_checkpoint(current, db) | missing_result
        manager.store.checkpoint(current, checkpoint, db)
        manager.store.save(current, db)

    manager.sync_incident(session.session_id)

    incident = manager.incident_store.get_incident(session.incident_id)
    assert incident.status == "investigating" and incident.alert_state == "firing"
    assert len(incident.investigations) == 1
    record = incident.investigations[0]
    assert record.session_id == session.session_id
    assert record.status.value == "model_incomplete"
    assert record.answer == "调查停止：incident_model_budget_exhausted"
    assert incident.consecutive_investigation_failures == 1
    assert incident.last_investigated_at is None
    assert incident.investigation_suspended_reason == "exhausted_requires_human_reauthorization"
    current = manager.store.get(session.session_id)
    checkpoint = manager.store.load_checkpoint(current)
    assert checkpoint["projected"] and not checkpoint.get("result")
    assert current.status == S.EXHAUSTED
    before = (manager.store.path.read_bytes(), manager.incident_store.file_path.read_bytes())
    manager.recover()
    manager.sync_incident(session.session_id)
    assert before == (manager.store.path.read_bytes(), manager.incident_store.file_path.read_bytes())


def test_expired_claim_exhaustion_projects_without_model_result(tmp_path):
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    manager = create_runtime(tmp_path, clock=lambda: now, limits=RuntimeLimits(max_resumes=0))
    session = create_session(manager)
    worker = Worker(manager, FixtureAgent())
    claimed = manager.store.claim(worker.owner, manager.limits)
    claim = (worker.owner, claimed.fencing_token)
    checkpoint = manager.store.load_checkpoint(claimed)
    worker._new_attempt(session.session_id, claim, checkpoint)
    worker._reserve(session.session_id, claim, checkpoint, "model")
    now += timedelta(seconds=manager.limits.lease_seconds + 1)

    manager.recover()

    exhausted = manager.store.get(session.session_id)
    assert exhausted.status == S.EXHAUSTED
    assert exhausted.owner_token is None and exhausted.lease_until is None
    assert not manager.store.load_checkpoint(exhausted).get("result")
    incident = manager.incident_store.get_incident(session.incident_id)
    assert len(incident.investigations) == incident.consecutive_investigation_failures == 1
    assert incident.investigations[0].answer == "调查停止：session_resume_budget_exhausted"
    worker.agent.request_durable_response.assert_not_called()


def test_recovery_exhaustion_freezes_the_saved_result_totals(tmp_path):
    class Crash(BaseException):
        pass

    clock = SimpleNamespace(now=datetime.now(timezone.utc))
    manager = create_runtime(
        tmp_path,
        clock=lambda: clock.now,
        limits=RuntimeLimits(max_resumes=0),
    )
    session = create_session(manager)

    def crash(phase, session_id):
        if phase == "model_checkpoint_committed":
            raise Crash()

    agent = FixtureAgent()
    with pytest.raises(Crash):
        Worker(manager, agent, boundary_hook=crash).run_once()
    interrupted = manager.store.get(session.session_id)
    before = manager.store.load_checkpoint(interrupted)
    clock.now += timedelta(seconds=manager.limits.lease_seconds + 1)
    manager.recover()
    saved = manager.store.get(session.session_id)
    checkpoint = manager.store.load_checkpoint(saved)
    assert saved.status == S.EXHAUSTED
    assert saved.last_error_category == "session_resume_budget_exhausted"
    assert checkpoint["result"] == before["result"] | {
        "model_rounds": 1, "tool_calls": 1,
    }
    assert checkpoint["history"] == before["history"]
    assert checkpoint["records"] == before["records"]
    assert agent.request_durable_response.call_count == 1
    assert agent.executions == ["get_running_version"]
    unused = FixtureAgent()
    assert Worker(manager, unused).run_once() is None
    unused.request_durable_response.assert_not_called()
    assert unused.executions == []
    view = RuntimeQueries(
        incident_path=manager.incident_store.file_path,
        runtime_path=manager.store.path,
    ).session(session.session_id)
    assert (
        view.investigation_result.model_rounds,
        view.investigation_result.tool_calls,
    ) == (1, 1)
    incident = manager.incident_store.get_incident(session.incident_id)
    assert (
        incident.investigations[0].model_rounds,
        incident.investigations[0].tool_calls,
    ) == (1, 1)


@pytest.mark.parametrize("archived", [False, True])
def test_legacy_terminal_counts_are_immutable_during_projection(tmp_path, archived):
    now = datetime.now(timezone.utc)
    manager = create_runtime(tmp_path, clock=lambda: now)
    session = create_session(manager)
    store = manager.store
    legacy_result = {
        "success": False,
        "status": "model_incomplete",
        "answer": "调查停止：ValueError",
        "model_rounds": 0,
        "tool_calls": 0,
    }
    with store.transaction() as db:
        current = store.get(session.session_id, db)
        transition(current, S.FAILED)
        current.total_model_requests = 3
        current.total_tool_executions = 4
        current.attempt_count = 1
        current.completed_at = now
        # Model a row already committed by the old version. This is not a new
        # terminal transition; projection must keep its original result bytes.
        store.save(current, db)
        checkpoint = store.load_checkpoint(current, db)
        checkpoint["result"] = dict(legacy_result)
        store.checkpoint(current, checkpoint, db)
        store.save(current, db)
    manager.sync_incident(session.session_id)
    if archived:
        incident = manager.incident_store.get_incident(session.incident_id)
        for index in range(4):
            incident.investigations.append(InvestigationRecord(
                session_id=f"newer-{index}",
                investigated_at=now + timedelta(seconds=index + 1),
                status="completed",
                answer="newer",
            ))
        manager.incident_store.save_incident(incident.fingerprint, incident)
        hot = manager.incident_store.get_incident(
            session.incident_id, include_history=False,
        )
        assert session.session_id in hot.history_archives[0].session_ids
    archives = {
        path: path.read_bytes() for path in (tmp_path / "history").glob("*.json")
    }
    with store.transaction() as db:
        current = store.get(session.session_id, db)
        checkpoint = store.load_checkpoint(current, db)
        checkpoint["projected"] = False
        store.checkpoint(current, checkpoint, db)
        store.save(current, db)
    manager.sync_incident(session.session_id)
    saved = store.get(session.session_id)
    assert store.load_checkpoint(saved)["result"] == legacy_result
    assert store.load_checkpoint(saved)["projected"]
    incident = manager.incident_store.get_incident(session.incident_id)
    record = next(
        r for r in incident.investigations if r.session_id == session.session_id
    )
    assert (record.model_rounds, record.tool_calls) == (3, 4)
    assert all(path.read_bytes() == content for path, content in archives.items())
    before = (store.path.read_bytes(), manager.incident_store.file_path.read_bytes())
    manager.recover()
    manager.sync_incident(session.session_id)
    assert before == (
        store.path.read_bytes(), manager.incident_store.file_path.read_bytes(),
    )
