"""Review claims exercised with isolated stores and deterministic observations."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from investigation.models import Checkpoint, HumanCommand, InvestigationSession, SessionStatus as S
from investigation.scheduler import Scheduler
from investigation.state import LostClaim
from investigation.worker import Worker
from remediation.planner import RemediationPlanner
from remediation.store import RemediationStore, StoreError
from runtime_cli import main as runtime_cli
from runtime_support import FixtureAgent, SHA, create_runtime, create_session, remediation_agent, response


@pytest.fixture
def runtime(tmp_path):
    clock = SimpleNamespace(now=datetime.now(timezone.utc))
    manager = create_runtime(tmp_path, clock=lambda: clock.now)
    session = create_session(manager)
    return manager, session, clock


@pytest.mark.parametrize("interruption", ["lease", "message", "cancel", "cleared", "yield", "saved_final"])
def test_finish_refresh_interruption_semantics(runtime, interruption):
    manager, session, clock = runtime
    planner = Mock()
    planner.plan.return_value = object()
    planner.propose.return_value = None
    manager.remediation_planner = planner
    agent = FixtureAgent([response(answer="已经保存的最终调查结论")])
    worker = Worker(manager, agent)
    injected = False

    def boundary(phase, session_id):
        nonlocal injected
        if injected or phase != "tool_after_io" or len(agent.executions) != 2:
            return
        injected = True
        if interruption in {"lease", "saved_final"}:
            clock.now += timedelta(seconds=manager.limits.lease_seconds + 1)
        elif interruption in {"message", "cancel"}:
            command = "message" if interruption == "message" else "cancel"
            manager.submit(HumanCommand(
                session_id=session_id, actor="fixture-operator", command_type=command,
                payload={"text": "补证期间的新线索"} if command == "message" else {},
            ))
        elif interruption == "cleared":
            incident = manager.incident_store.get_incident(session.incident_id)
            incident.alert_state = "cleared"
            manager.incident_store.save_incident(incident.fingerprint, incident)
        else:
            worker.stop_requested = lambda: True

    worker.boundary_hook = boundary
    result = worker.run_once()
    assert injected
    checkpoint = manager.store.load_checkpoint(result)
    events = [e["event_type"] for e in manager.store.events(session.session_id)]
    assert result.attempt_count == 1
    assert result.total_model_requests == 1
    assert result.last_error_category is None
    planner.propose.assert_not_called()
    if interruption in {"lease", "saved_final"}:
        assert result.status == S.RUNNING
        assert events.count("worker_claim_lost") == 1
        assert checkpoint["result"]["answer"] == "已经保存的最终调查结论"
        assert len(checkpoint["records"]) == 1  # The stale owner cannot save its refresh.
        stale_claim = (worker.owner, result.fencing_token)
        before = result.model_dump()
        with pytest.raises(LostClaim):
            worker._save(result.session_id, stale_claim, checkpoint)
        assert manager.store.get(result.session_id).model_dump() == before
        manager.remediation_planner = None
        next_agent = FixtureAgent()
        recovered = Worker(manager, next_agent).run_once()
        assert recovered.status == S.COMPLETED
        assert recovered.attempt_count == 1
        assert recovered.total_model_requests == 1
        assert recovered.total_tool_executions == 2
        next_agent.request_durable_response.assert_not_called()
        assert next_agent.executions == []
        assert manager.store.load_checkpoint(recovered)["result"] == checkpoint["result"]
        assert "worker_recovered" in [e["event_type"] for e in manager.store.events(result.session_id)]
    else:
        assert "worker_claim_lost" not in events
        assert result.owner_token is None and result.lease_until is None
        if interruption == "message":
            assert result.status == S.QUEUED
            assert not checkpoint.get("result")
            assert checkpoint["messages"][0]["text"] == "补证期间的新线索"
            assert "human_context_changed_before_completion" in events
            next_agent = FixtureAgent([response(answer="考虑新线索后的结论")])
            manager.remediation_planner = None
            continued = Worker(manager, next_agent).run_once()
            assert continued.status == S.COMPLETED and continued.attempt_count == 1
            assert manager.store.load_checkpoint(continued)["result"]["answer"] == "考虑新线索后的结论"
        else:
            assert checkpoint["result"]["answer"] == "已经保存的最终调查结论"
            expected = S.QUEUED if interruption == "yield" else S.CANCELLED
            assert result.status == expected
            expected_event = {"cancel": "investigation_cancelled", "cleared": "alert_cleared",
                              "yield": "worker_shutdown_at_safe_point"}[interruption]
            assert expected_event in events
            manager.remediation_planner = None
            next_agent = FixtureAgent()
            continued = Worker(manager, next_agent).run_once()
            if interruption == "yield":
                assert continued.status == S.COMPLETED and continued.attempt_count == 1
            else:
                assert continued is None
            next_agent.request_durable_response.assert_not_called()
    incident = manager.incident_store.get_incident(session.incident_id)
    assert incident.consecutive_investigation_failures == 0


def test_planning_runs_twice_and_version_is_reconfirmed(runtime, tmp_path):
    manager, session, clock = runtime
    planner = RemediationPlanner(RemediationStore(tmp_path / "remediation.json"))
    planner.plan = Mock(wraps=planner.plan)
    manager.remediation_planner = planner
    agent = remediation_agent(manager.store.clock)
    with patch("remediation.planner.utc_now", manager.store.clock):
        completed = Worker(manager, agent).run_once()
    assert completed.status == S.COMPLETED
    assert planner.plan.call_count == 2
    assert agent.executions.count("get_running_version") == 2
    assert completed.total_tool_executions == 5
    proposals = planner.store.list()
    assert len(proposals) == 1 and proposals[0].status.value == "pending"
    assert proposals[0].expected_commit_sha == SHA


@pytest.mark.parametrize("changed", ["sha", "process", "flag"])
def test_fresh_ttl_does_not_replace_final_instance_check(runtime, tmp_path, changed):
    manager, session, clock = runtime
    planner = RemediationPlanner(RemediationStore(tmp_path / "remediation.json"))
    manager.remediation_planner = planner
    agent = remediation_agent(manager.store.clock)
    original = agent.tool_result
    versions = 0

    def observation(item):
        nonlocal versions
        result = original(item)
        if item.name == "get_running_version":
            versions += 1
            if versions == 2:
                if changed == "sha":
                    result["commit_sha"] = "b" * 40
                elif changed == "process":
                    result["process_started_at"] = clock.now.isoformat()
                else:
                    result["runtime_flags"]["simulate_code_regression"] = False
        return result

    agent.tool_result = observation
    with patch("remediation.planner.utc_now", manager.store.clock):
        completed = Worker(manager, agent).run_once()
    assert completed.status == S.COMPLETED and versions == 2
    assert planner.store.list() == []
    assert manager.store.load_checkpoint(completed)["result"]["success"]


def test_two_incidents_race_with_real_processes_and_fencing(tmp_path):
    manager = create_runtime(tmp_path)
    first = create_session(manager)
    second = create_session(manager, service="inventory-service")
    assert first.incident_id != second.incident_id
    assert all(s.status == S.QUEUED for s in manager.store.list_sessions())
    fixture = Path(__file__).with_name("runtime_process_fixture.py")
    children = [subprocess.Popen(
        [sys.executable, "-B", str(fixture), str(tmp_path), "claim-contender", owner],
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) for owner in ("one", "two")]

    def wait_files(names):
        deadline = time.monotonic() + 30
        while not all((tmp_path / name).exists() for name in names):
            assert time.monotonic() < deadline, "child barrier timed out"
            time.sleep(0.02)

    try:
        wait_files(["ready-one", "ready-two"])
        (tmp_path / "claim-release").write_text("start")
        wait_files(["claim-one.json", "claim-two.json"])
        claims = [json.loads((tmp_path / f"claim-{owner}.json").read_text()) for owner in ("one", "two")]
        assert sum(c is not None for c in claims) == 1
        winner = next(c for c in claims if c)
        old = manager.store.get(winner["session_id"])
        live = [s for s in manager.store.list_sessions() if s.status == S.RUNNING and s.lease_until > manager.store.clock()]
        assert len(live) == 1
        assert len(manager.store.list_sessions(statuses={S.QUEUED})) == 1
        later = old.lease_until + timedelta(seconds=1)
        (tmp_path / "clock.txt").write_text(later.isoformat())
        manager.store.clock = lambda: later
        manager.recover()
        assert manager.store.get(old.session_id).fencing_token > old.fencing_token
        replacement = manager.store.claim("replacement", manager.limits)
        assert replacement is not None
        before = [s.model_dump() for s in manager.store.list_sessions()]
        (tmp_path / "fence-release").write_text("try stale write")
        for child in children:
            _, errors = child.communicate(timeout=30)
            assert child.returncode == 0, errors.decode(errors="replace")
        assert (tmp_path / "fence-result").read_text() == "old_owner_fenced"
        assert [s.model_dump() for s in manager.store.list_sessions()] == before
        assert manager.store.claim("third-worker", manager.limits) is None
        assert len([s for s in manager.store.list_sessions() if s.status == S.RUNNING and s.lease_until > later]) == 1
    finally:
        (tmp_path / "clock.txt").write_text(manager.store.clock().isoformat())
        (tmp_path / "claim-release").write_text("release")
        (tmp_path / "fence-release").write_text("release")
        for child in children:
            if child.poll() is None:
                child.communicate(timeout=30)


def test_unique_index_rejects_second_nonterminal_session(runtime):
    manager, first, clock = runtime
    duplicate = InvestigationSession(
        incident_id=first.incident_id, alert_fingerprint=first.alert_fingerprint,
        service=first.service, status=S.QUEUED,
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        with manager.store.transaction() as db:
            manager.store.insert(duplicate, db)
    assert [s.session_id for s in manager.store.list_sessions()] == [first.session_id]
    with manager.store.read() as db:
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='one_active_incident'").fetchone()[0]


def test_expired_remediation_retry_remains_operator_visible(runtime, tmp_path):
    manager, session, clock = runtime
    planner = RemediationPlanner(RemediationStore(tmp_path / "remediation.json"))
    manager.remediation_planner = planner
    with patch.object(planner.store, "_save", side_effect=StoreError("remediation_store_write_failed")), \
            patch("remediation.planner.utc_now", manager.store.clock):
        completed = Worker(manager, remediation_agent(manager.store.clock)).run_once()
        assert manager.store.load_checkpoint(completed)["remediation_pending"]
        clock.now += timedelta(seconds=31)
        manager.recover()
    workflow = manager.workflow(session.session_id)
    assert workflow["session"]["status"] == "COMPLETED"
    assert workflow["investigation_result"]["success"]
    assert workflow["remediation_pending"]
    assert workflow["remediation_blocked"]
    assert workflow["remediation_blocked_reason"] == "evidence_expired"
    assert workflow["remediation_attempts"] == 1
    assert workflow["remediation_retry_at"] is None


def test_unreadable_remediation_store_does_not_hide_saved_conclusion(runtime, tmp_path):
    manager, session, clock = runtime
    path = tmp_path / "remediation.json"
    planner = RemediationPlanner(RemediationStore(path))
    manager.remediation_planner = planner
    path.write_text("broken JSON", encoding="utf-8")
    with patch("remediation.planner.utc_now", manager.store.clock):
        completed = Worker(manager, remediation_agent(manager.store.clock)).run_once()
    workflow = manager.workflow(session.session_id)
    assert workflow["session"]["status"] == "COMPLETED"
    assert workflow["investigation_result"]["success"]
    assert workflow["remediation_store_error_category"] == "remediation_store_corrupt_or_unreadable"
    assert workflow["remediation_blocked"] and workflow["remediation_pending"]
    assert workflow["remediation_error_category"] == "remediation_store_corrupt_or_unreadable"
    assert workflow["remediation_retry_at"] is None
    output = io.StringIO()
    with redirect_stdout(output):
        code = runtime_cli([
            "--store", str(manager.store.path), "--incidents", str(tmp_path / "incidents.json"),
            "--remediations", str(path), "show-session", session.session_id,
        ])
    assert code == 0
    assert json.loads(output.getvalue())["investigation_result"] == workflow["investigation_result"]


@pytest.mark.parametrize("base", [5, 200])
def test_persistent_write_failure_is_bounded_across_restarts(runtime, tmp_path, monkeypatch, base):
    from investigation.evidence_policy import TTL
    # Isolate retry scheduling from expiry; the preceding test keeps production TTLs.
    for name in ("get_running_version", "query_logs", "check_dependency_health"):
        monkeypatch.setitem(TTL, name, 3600)
    manager, session, clock = runtime
    manager.limits.retry_base_seconds = base
    manager.remediation_planner = RemediationPlanner(RemediationStore(tmp_path / "remediation.json"))
    output = io.StringIO()
    with patch.object(RemediationPlanner, "propose", side_effect=StoreError("remediation_store_write_failed")) as propose, \
            patch("remediation.planner.utc_now", lambda: clock.now):
        completed = Worker(manager, remediation_agent(manager.store.clock)).run_once()
        usage = manager.workflow(session.session_id)["incident_budget_usage"]
        for attempt in range(1, 7):
            workflow = manager.workflow(session.session_id)
            assert workflow["remediation_attempts"] == attempt
            assert workflow["remediation_error_category"] == "remediation_store_write_failed"
            assert workflow["session"]["status"] == "COMPLETED"
            assert workflow["investigation_result"]["success"]
            assert workflow["incident_budget_usage"] == usage
            scheduler = Scheduler(Worker(manager, FixtureAgent()))
            with redirect_stdout(output):
                scheduler._notify_states()
                scheduler._notify_states()
            if attempt == 6:
                assert workflow["remediation_blocked"]
                assert workflow["remediation_blocked_reason"] == "attempt_limit"
                assert workflow["remediation_retry_at"] is None
                assert manager.store.pending_work("projection") == []
                break
            delay = min(300, base * 2 ** (attempt - 1))
            retry = datetime.fromisoformat(workflow["remediation_retry_at"])
            assert (retry - clock.now).total_seconds() == delay
            clock.now = retry - timedelta(microseconds=1)
            manager.recover()
            assert manager.workflow(session.session_id)["remediation_attempts"] == attempt
            clock.now = retry
            manager = create_runtime(tmp_path, clock=lambda: clock.now, limits=manager.limits)
            manager.remediation_planner = RemediationPlanner(RemediationStore(tmp_path / "remediation.json"))
            manager.recover()
        checkpoint = manager.store.load_checkpoint(manager.store.get(session.session_id))
        for _ in range(3):
            clock.now += timedelta(hours=1)
            manager = create_runtime(tmp_path, clock=lambda: clock.now, limits=manager.limits)
            manager.remediation_planner = RemediationPlanner(RemediationStore(tmp_path / "remediation.json"))
            manager.recover()
            assert manager.store.load_checkpoint(manager.store.get(session.session_id)) == checkpoint
        assert propose.call_count == 6
    assert output.getvalue().count("[Remediation Proposal Status]") == 3  # First, persistent, blocked.
    assert "remediation_store_write_failed" in output.getvalue()
    assert "attempt_limit" in output.getvalue()
    data = manager.remediation_planner.store.load()
    assert data["proposals"] == {} and data["actions"] == [] and data["verifications"] == []
    assert len(manager.incident_store.get_incident(completed.incident_id).investigations) == 1


def test_transient_write_failure_recovers_without_reinvestigation(runtime, tmp_path):
    manager, session, clock = runtime
    planner = RemediationPlanner(RemediationStore(tmp_path / "remediation.json"))
    manager.remediation_planner = planner
    original = planner.store._save
    writes = 0

    def write(data):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise StoreError("remediation_store_write_failed")
        original(data)

    with patch.object(planner.store, "_save", write), patch("remediation.planner.utc_now", manager.store.clock):
        completed = Worker(manager, remediation_agent(manager.store.clock)).run_once()
        first = manager.workflow(session.session_id)
        assert first["remediation_attempts"] == 1 and first["remediation_pending"]
        clock.now = datetime.fromisoformat(first["remediation_retry_at"])
        manager.recover()
    final = manager.workflow(session.session_id)
    assert final["remediation_attempts"] == 2
    assert not final["remediation_pending"] and not final["remediation_blocked"]
    assert final["remediation_error_category"] is None and final["remediation_retry_at"] is None
    assert final["incident_budget_usage"] == first["incident_budget_usage"]
    assert final["investigation_result"] == first["investigation_result"]
    assert [p["status"] for p in final["remediations"]] == ["pending"]
    assert manager.store.pending_work("projection") == []
    assert len(manager.incident_store.get_incident(completed.incident_id).investigations) == 1


@pytest.mark.parametrize("failure", [PermissionError("private credential"), ValueError("private credential"),
                                    StoreError("untrusted private credential")])
def test_nonretryable_failures_are_safe_and_do_not_loop(runtime, tmp_path, failure):
    manager, session, clock = runtime
    planner = RemediationPlanner(RemediationStore(tmp_path / "remediation.json"))
    manager.remediation_planner = planner
    with patch.object(planner, "propose", side_effect=failure) as propose, \
            patch("remediation.planner.utc_now", manager.store.clock):
        result = Worker(manager, remediation_agent(manager.store.clock)).run_once()
        for _ in range(3):
            clock.now += timedelta(hours=1)
            manager.recover()
        assert propose.call_count == 1
    workflow = manager.workflow(session.session_id)
    assert workflow["remediation_attempts"] == 1
    assert workflow["remediation_blocked_reason"] == "non_retryable_error"
    assert workflow["remediation_pending"] and workflow["remediation_blocked"]
    assert workflow["session"]["last_error_category"] is None
    assert result.status == S.COMPLETED
    assert "private credential" not in json.dumps(workflow)
    assert "private credential" not in json.dumps(manager.store.events(session.session_id))


def test_legacy_checkpoint_fields_are_optional_and_not_rewritten_by_query(runtime, tmp_path):
    manager, session, clock = runtime
    completed = Worker(manager, FixtureAgent()).run_once()
    checkpoint = manager.store.load_checkpoint(completed)
    for field in ("remediation_attempts", "remediation_error_category", "remediation_blocked", "remediation_blocked_reason"):
        checkpoint.pop(field, None)
    assert Checkpoint.model_validate(checkpoint).remediation_attempts == 0
    with manager.store.transaction() as db:
        latest = manager.store.get(session.session_id, db)
        manager.store.checkpoint(latest, checkpoint, db)
        manager.store.save(latest, db)
    before = manager.store.path.read_bytes()
    restarted = create_runtime(tmp_path, clock=lambda: clock.now)
    workflow = restarted.workflow(session.session_id)
    assert workflow["remediation_attempts"] == 0
    assert workflow["remediation_error_category"] is None and not workflow["remediation_blocked"]
    assert manager.store.path.read_bytes() == before
    with manager.store.read() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_retry_that_no_longer_has_candidate_does_not_erase_failed_proposal(runtime, tmp_path):
    manager, session, clock = runtime
    planner = RemediationPlanner(RemediationStore(tmp_path / "remediation.json"))
    manager.remediation_planner = planner
    with patch.object(planner.store, "_save", side_effect=StoreError("remediation_store_write_failed")), \
            patch("remediation.planner.utc_now", manager.store.clock):
        Worker(manager, remediation_agent(manager.store.clock)).run_once()
        first = manager.workflow(session.session_id)
        clock.now = datetime.fromisoformat(first["remediation_retry_at"])
        with patch.object(planner, "plan", return_value=None):
            manager.recover()
    workflow = manager.workflow(session.session_id)
    assert workflow["remediation_pending"] and workflow["remediation_blocked"]
    assert workflow["remediation_blocked_reason"] == "evidence_no_longer_eligible"
    assert workflow["remediation_error_category"] == first["remediation_error_category"]
    assert workflow["remediation_attempts"] == 2
    assert workflow["investigation_result"] == first["investigation_result"]
    assert workflow["incident_budget_usage"] == first["incident_budget_usage"]
    assert planner.store.list() == []
