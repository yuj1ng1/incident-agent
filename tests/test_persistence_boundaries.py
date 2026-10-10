"""Real temporary stores, controlled clocks, threads and isolated child processes."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from investigation.models import HumanCommand, SessionStatus as S
from investigation.scheduler import Scheduler
from investigation.state import LostClaim, RuntimeErrorBase
from investigation.worker import Worker
from models.investigation import InvestigationRecord
from runtime_support import FixtureAgent, call, create_runtime, create_session, response
from tools.tool_manager import ToolManager
from tools.tool_registry import tool_registry

ROOT = Path(__file__).resolve().parents[1]


class PersistenceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="persistence-boundary-")))
        self.now = datetime.now(timezone.utc)
        self.manager = create_runtime(self.root, clock=lambda: self.now)
        self.store = self.manager.store
        self.incidents = self.manager.incident_store
        self.session = create_session(self.manager)

    def saved(self):
        session = self.store.get(self.session.session_id)
        return session, self.store.load_checkpoint(session)

    def pending(self):
        with self.store.transaction() as db:
            session = self.store.get(self.session.session_id, db)
            checkpoint = self.store.load_checkpoint(session, db)
            checkpoint.pop("projection_digest", None)
            checkpoint["projected"] = False
            self.store.checkpoint(session, checkpoint, db)
            self.store.save(session, db)

    def finish(self):
        return Worker(self.manager, FixtureAgent()).run_once()

    def archive(self):
        self.finish()
        incident = self.incidents.get_incident(self.session.incident_id)
        for index in range(4):
            incident.investigations.append(InvestigationRecord(
                session_id="newer-" + str(index), investigated_at=self.now + timedelta(seconds=index + 1),
                status="completed", answer="newer", model_rounds=1, tool_calls=0))
        self.incidents.save_incident(incident.fingerprint, incident)
        self.pending()
        return self.incidents.get_incident(incident.incident_id, include_history=False).history_archives[0]

    def lost_owner(self, inside_failure):
        ready, release, progressed = threading.Event(), threading.Event(), threading.Event()
        agent = FixtureAgent([response(), response()])
        fired = False

        def boundary(phase, session_id):
            nonlocal fired
            if not fired and phase == "tool_after_io":
                fired = True
                ready.set()
                if not release.wait(15):
                    raise TimeoutError("test barrier")
                if inside_failure:
                    raise TimeoutError("model/IO failure after lease expiry")

        worker = Worker(self.manager, agent, boundary)
        scheduler = Scheduler(worker, interval=0.001)
        original_tick = scheduler.tick
        results = []

        def tick():
            result = original_tick()
            results.append(result)
            if len(results) == 2:
                progressed.set()
                scheduler.stop_requested.set()
            return result

        with patch.object(scheduler, "tick", side_effect=tick), redirect_stdout(io.StringIO()):
            scheduler.start()
            try:
                self.assertTrue(ready.wait(15))
                self.now += timedelta(seconds=121)
                other_manager = create_runtime(self.root, clock=lambda: self.now)
                replacement = Worker(other_manager, FixtureAgent()).run_once()
                self.assertEqual(replacement.status, S.COMPLETED)
                before = self.saved()
                json_before = self.incidents.file_path.read_bytes()
                other = create_session(self.manager, service="inventory-service")
                release.set()
                self.assertTrue(progressed.wait(5), "lost claim permanently stopped Scheduler")
                self.assertEqual(results[-1].session_id, other.session_id)
                self.assertEqual(results[-1].status, S.COMPLETED)
                after = self.saved()
                self.assertEqual(after[0].checkpoint_id, before[0].checkpoint_id)
                self.assertEqual(after[1], before[1])
                before_incident = json.loads(json_before)["incidents"][self.session.incident_id]
                self.assertEqual(self.incidents.load()["incidents"][self.session.incident_id], before_incident)
                self.assertTrue(any(e["event_type"] == "worker_claim_lost"
                                    for e in self.store.events(self.session.session_id)))
            finally:
                release.set()
                scheduler.stop()

    def test_run_loses_real_claim_without_publishing_stale_evidence(self):
        self.lost_owner(False)

    def test_failure_loses_real_claim_and_other_session_still_runs(self):
        self.lost_owner(True)

    def test_stale_cas_is_distinct_from_normal_claim_loss(self):
        with self.store.transaction() as db:
            first = self.store.get(self.session.session_id, db)
            stale = first.model_copy(deep=True)
            self.store.save(first, db)
            with self.assertRaises(RuntimeErrorBase) as error:
                self.store.save(stale, db)
            self.assertNotIsInstance(error.exception, LostClaim)

    def test_corrupt_database_stops_scheduler_without_replacing_bytes(self):
        self.store.path.write_bytes(b"isolated corrupt sqlite")
        before = self.store.path.read_bytes()
        scheduler = Scheduler(Worker(self.manager, FixtureAgent()))
        scheduler.run()
        self.assertTrue(scheduler.stop_requested.is_set())
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_corruption_during_run_is_not_saved_as_investigation_failure(self):
        worker = Worker(self.manager, FixtureAgent())
        with patch.object(worker, "_run", side_effect=sqlite3.DatabaseError("isolated corruption")):
            with self.assertRaises(sqlite3.DatabaseError):
                worker.run_once()
        self.assertEqual(self.saved()[0].status, S.RUNNING)
        self.assertFalse(any(e["event_type"] == "investigation_failed"
                             for e in self.store.events(self.session.session_id)))

    def test_missing_incident_is_quarantined_without_ack_or_hot_retry(self):
        data = self.incidents.load()
        data["incidents"].pop(self.session.incident_id)
        data["active_by_fingerprint"].clear()
        self.incidents.save(data)
        with patch.object(self.incidents, "apply_runtime_projection", wraps=self.incidents.apply_runtime_projection) as project:
            for _ in range(5):
                self.manager.recover()
        _, checkpoint = self.saved()
        self.assertFalse(checkpoint.get("projected"))
        self.assertTrue(checkpoint.get("projection_blocked"))
        self.assertEqual(checkpoint.get("projection_error_category"), "incident_not_found")
        self.assertEqual(project.call_count, 1)
        self.assertEqual(self.store.pending_work("projection"), [])
        restarted = create_runtime(self.root, clock=lambda: self.now)
        restarted.recover()
        self.assertTrue(restarted.workflow(self.session.session_id)["projection_blocked"])
        other = create_session(self.manager, service="inventory-service")
        self.assertEqual(Worker(self.manager, FixtureAgent()).run_once().session_id, other.session_id)

    def test_archived_identical_result_is_verified_before_ack(self):
        self.archive()
        before = self.incidents.file_path.read_bytes()
        with patch.object(self.incidents, "_load_archive", wraps=self.incidents._load_archive) as archive:
            self.manager.sync_incident(self.session.session_id)
        self.assertEqual(archive.call_count, 1)
        self.assertTrue(self.saved()[1]["projected"])
        self.assertEqual(self.incidents.file_path.read_bytes(), before)
        self.assertEqual(len(self.incidents.get_incident(self.session.incident_id).investigations), 5)

    def test_corrupt_archive_cannot_be_acknowledged(self):
        reference = self.archive()
        path = self.root / "history" / (reference.sha256 + ".json")
        path.write_bytes(path.read_bytes() + b"corrupt")
        with self.assertRaisesRegex(ValueError, "checksum_mismatch"):
            self.manager.sync_incident(self.session.session_id)
        self.assertFalse(self.saved()[1]["projected"])

    def test_missing_archive_fails_closed(self):
        reference = self.archive()
        path = self.root / "history" / (reference.sha256 + ".json")
        path.rename(path.with_suffix(".unavailable"))
        with self.assertRaisesRegex(ValueError, "archive_unavailable"):
            self.manager.sync_incident(self.session.session_id)
        self.assertFalse(self.saved()[1]["projected"])

    def test_archived_conflicting_result_fails_closed(self):
        self.archive()
        with self.store.transaction() as db:
            session = self.store.get(self.session.session_id, db)
            checkpoint = self.store.load_checkpoint(session, db)
            checkpoint["result"]["answer"] = "conflicting answer"
            self.store.checkpoint(session, checkpoint, db)
            self.store.save(session, db)
        with self.assertRaisesRegex(ValueError, "archive_projection_mismatch"):
            self.manager.sync_incident(self.session.session_id)
        self.assertFalse(self.saved()[1]["projected"])

    def test_reopen_snapshot_is_audit_evidence_and_preserves_newer_monitor_state(self):
        Worker(self.manager, FixtureAgent([ValueError("fixture failure")])).run_once()
        original_alert = self.incidents.get_incident(self.session.incident_id).alerts[-1]
        snapshot_a = original_alert.model_copy(update={"observed_at": self.now, "value": 0.4})
        self.manager.alert_verifier = lambda incident: snapshot_a
        with patch.object(self.manager, "sync_incident"):
            command = self.manager.submit(HumanCommand(session_id=self.session.session_id,
                actor="operator", command_type="reopen", payload={"reason": "fix verified"}))
        self.assertEqual(command.processing_status, "applied")
        incident = self.incidents.get_incident(self.session.incident_id)
        snapshot_b = original_alert.model_copy(update={"observed_at": self.now + timedelta(seconds=1), "value": 0.9})
        incident.alerts[-1] = snapshot_b
        self.incidents.save_observation(incident.fingerprint, incident)
        self.manager.sync_incident(command.result)
        projected = self.incidents.get_incident(self.session.incident_id)
        self.assertEqual(projected.alerts[-1], snapshot_b)
        self.assertEqual(projected.fingerprint, incident.fingerprint)
        self.assertEqual(projected.alert_state, "firing")
        self.assertEqual(projected.status, incident.status)
        self.assertEqual(projected.consecutive_investigation_failures, 0)
        self.assertIsNone(projected.investigation_suspended_reason)
        checkpoint = self.store.load_checkpoint(self.store.get(command.result))
        self.assertEqual(checkpoint["reopen_alert"], snapshot_a.model_dump(mode="json"))
        event = next(e for e in self.store.events(command.result) if e["event_type"] == "budget_reauthorized")
        self.assertEqual(event["data"]["verified_alert"], snapshot_a.model_dump(mode="json"))

    def test_concurrent_projection_version_race_retains_newer_checkpoint(self):
        self.finish()
        self.pending()
        ready, release = threading.Event(), threading.Event()
        project = self.incidents.apply_runtime_projection
        failures = []

        def concurrent_projection(session, checkpoint):
            outcome = project(session, checkpoint)
            ready.set()
            if not release.wait(15):
                raise TimeoutError("projection barrier")
            return outcome

        def sync():
            try:
                self.manager.sync_incident(self.session.session_id)
            except Exception as exc:
                failures.append(exc)

        with patch.object(self.incidents, "apply_runtime_projection", side_effect=concurrent_projection):
            thread = threading.Thread(target=sync)
            thread.start()
            try:
                self.assertTrue(ready.wait(15))
                other_manager = create_runtime(self.root, clock=lambda: self.now)
                with other_manager.store.transaction() as db:
                    session = other_manager.store.get(self.session.session_id, db)
                    checkpoint = other_manager.store.load_checkpoint(session, db)
                    checkpoint["messages"].append({"text": "concurrent human context"})
                    other_manager.store.checkpoint(session, checkpoint, db)
                    other_manager.store.save(session, db)
                newer = self.saved()[0].checkpoint_id
            finally:
                release.set()
                thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.saved()[0].checkpoint_id, newer)
        self.assertFalse(self.saved()[1]["projected"])
        self.assertEqual(self.saved()[1]["messages"], [{"text": "concurrent human context"}])
        self.assertTrue(any(e["event_type"] == "incident_projection_superseded"
                            for e in self.store.events(self.session.session_id)))
        other = create_session(self.manager, service="inventory-service")
        self.assertEqual(Worker(self.manager, FixtureAgent()).run_once().session_id, other.session_id)
        self.assertTrue(self.saved()[1]["projected"])

    def test_stale_worker_cannot_publish_after_fence_replacement(self):
        worker = Worker(self.manager, FixtureAgent())
        session = self.store.claim(worker.owner, self.manager.limits)
        old_claim = (worker.owner, session.fencing_token)
        self.now += timedelta(seconds=121)
        self.manager.recover()
        replacement = Worker(self.manager, FixtureAgent())
        newer = self.store.claim(replacement.owner, self.manager.limits)
        checkpoint = self.store.load_checkpoint(newer)
        replacement._new_attempt(newer.session_id, (replacement.owner, newer.fencing_token), checkpoint)
        before = self.saved()
        with patch.object(self.incidents, "apply_runtime_projection", wraps=self.incidents.apply_runtime_projection) as project:
            with self.assertRaises(LostClaim):
                self.manager.sync_incident(session.session_id, claim=old_claim)
        project.assert_not_called()
        self.assertEqual(self.saved(), before)

    def test_new_observation_and_terminal_error_are_still_projected(self):
        worker = Worker(self.manager, FixtureAgent())
        session = self.store.claim(worker.owner, self.manager.limits)
        claim = (worker.owner, session.fencing_token)
        checkpoint = self.store.load_checkpoint(session)
        worker._new_attempt(session.session_id, claim, checkpoint)
        self.manager.sync_incident(session.session_id)
        checkpoint = self.saved()[1]
        with patch.object(self.store, "checkpoint", wraps=self.store.checkpoint) as writes, \
             patch.object(self.incidents, "apply_runtime_projection", wraps=self.incidents.apply_runtime_projection) as projects, \
             patch.object(self.incidents, "save", wraps=self.incidents.save) as saves:
            worker._tool(session.session_id, claim, checkpoint, SimpleNamespace(**call()))
            self.manager.sync_incident(session.session_id)
        self.assertEqual((writes.call_count, projects.call_count, saves.call_count,
                          len(self.incidents.get_incident(session.incident_id).evidence)), (3, 1, 1, 1))
        checkpoint = self.saved()[1]
        worker._failure(session.session_id, claim, checkpoint, ValueError("fixture error"))
        self.assertFalse(self.saved()[1]["projected"])
        self.manager.sync_incident(session.session_id)
        incident = self.incidents.get_incident(session.incident_id)
        self.assertEqual(len(incident.investigations), 1)
        self.assertEqual(incident.consecutive_investigation_failures, 1)
        self.assertEqual(incident.investigation_suspended_reason, "non_retryable_failure_requires_human_reopen")

    def test_real_database_corruption_during_model_response_stops_scheduler(self):
        corrupted = b"database corrupted at isolated response boundary"

        def corrupt(*args, **kwargs):
            self.store.path.write_bytes(corrupted)
            return response()

        agent = FixtureAgent()
        agent.request_durable_response.side_effect = corrupt
        scheduler = Scheduler(Worker(self.manager, agent))
        scheduler.run()
        self.assertTrue(scheduler.stop_requested.is_set())
        self.assertEqual(self.store.path.read_bytes(), corrupted)
        self.assertEqual(agent.request_durable_response.call_count, 1)

    def test_persisted_state_version_mismatch_still_stops_scheduler(self):
        with self.store.transaction() as db:
            db.execute("UPDATE investigation_sessions SET state_version=state_version+1 WHERE session_id=?",
                       (self.session.session_id,))
        scheduler = Scheduler(Worker(self.manager, FixtureAgent()))
        scheduler.run()
        self.assertTrue(scheduler.stop_requested.is_set())

    def test_unconfirmed_projection_return_cannot_ack(self):
        self.finish()
        self.pending()
        before = self.saved()
        with patch.object(self.incidents, "apply_runtime_projection", return_value=None):
            with self.assertRaisesRegex(RuntimeErrorBase, "unconfirmed_incident_projection"):
                self.manager.sync_incident(self.session.session_id)
        self.assertEqual(self.saved(), before)

    def test_missing_projection_notifies_operator_after_previous_terminal_display(self):
        scheduler = Scheduler(Worker(self.manager, FixtureAgent()))
        with redirect_stdout(io.StringIO()):
            scheduler.tick()
        self.pending()
        data = self.incidents.load()
        data["incidents"].pop(self.session.incident_id)
        data["active_by_fingerprint"].clear()
        self.incidents.save(data)
        output = io.StringIO()
        with redirect_stdout(output):
            scheduler.tick()
            scheduler.tick()
        self.assertEqual(output.getvalue().count("Incident 投影已隔离，需人工核查：incident_not_found"), 1)

    def test_human_request_answer_and_final_result_remain_projected(self):
        question = call("request_human_input", {"question": "Which deployment?"}, "human-question")
        waiting = Worker(self.manager, FixtureAgent([response(answer="", calls=[question])])).run_once()
        self.assertEqual(waiting.status, S.WAITING_HUMAN)
        self.assertTrue(self.saved()[1]["projected"])
        request = self.manager.requests(waiting.session_id)[0]
        self.manager.submit(HumanCommand(session_id=waiting.session_id, actor="operator",
            command_type="answer", payload={"request_id": request.request_id, "text": "unverified deployment context"}))
        completed = Worker(self.manager, FixtureAgent()).run_once()
        self.assertEqual(completed.status, S.COMPLETED)
        checkpoint = self.saved()[1]
        self.assertTrue(checkpoint["projected"])
        self.assertEqual(checkpoint["messages"][0]["text"], "unverified deployment context")
        self.assertEqual(self.manager.requests(waiting.session_id)[0].status, "consumed")
        self.assertEqual(len(self.incidents.get_incident(waiting.incident_id).investigations), 1)

    def test_phase_only_checkpoints_do_not_reproject_or_save_json(self):
        worker = Worker(self.manager, FixtureAgent())
        session = self.store.claim(worker.owner, self.manager.limits)
        claim = (worker.owner, session.fencing_token)
        checkpoint = self.store.load_checkpoint(session)
        worker._new_attempt(session.session_id, claim, checkpoint)
        self.manager.sync_incident(session.session_id)
        checkpoint = self.saved()[1]
        observations_before = len(self.incidents.get_incident(session.incident_id).evidence)
        with patch.object(self.store, "checkpoint", wraps=self.store.checkpoint) as writes, \
             patch.object(self.incidents, "apply_runtime_projection", wraps=self.incidents.apply_runtime_projection) as projects, \
             patch.object(self.incidents, "save", wraps=self.incidents.save) as saves:
            for _ in range(5):
                self.now += timedelta(seconds=1)
                checkpoint["phase"] = "model_started"
                checkpoint["history"] = [{"role": "user", "content": "rebuild-only context"}]
                worker._save(session.session_id, claim, checkpoint)
                self.manager.sync_incident(session.session_id)
        observations_after = len(self.incidents.get_incident(session.incident_id).evidence)
        self.assertEqual((writes.call_count, projects.call_count, saves.call_count,
                          observations_after - observations_before), (5, 0, 0, 0))

    def test_failed_fixture_tool_reports_failure(self):
        agent = FixtureAgent(tool_result=lambda item: {"status": "tool_error", "reason": "TimeoutError"})
        item = type("Item", (), {"name": "query_metrics", "arguments": "{}", "call_id": "failed"})()
        self.assertFalse(agent.tool_manager.run_tool(item)[0])

    def test_real_tool_manager_failure_retains_budget_but_no_valid_evidence(self):
        agent = FixtureAgent()
        agent.tool_manager = ToolManager()
        function = dict(tool_registry["get_running_version"], func=lambda **args: (_ for _ in ()).throw(TimeoutError()))
        with patch.dict(tool_registry, {"get_running_version": function}):
            completed = Worker(self.manager, agent).run_once()
        checkpoint = self.store.load_checkpoint(completed)
        self.assertEqual(completed.total_tool_executions, 1)
        self.assertEqual(checkpoint["records"][0]["result"]["status"], "tool_error")
        self.assertFalse(checkpoint["records"][0]["success"])
        self.assertEqual(self.incidents.get_incident(completed.incident_id).evidence, [])

    def test_process_crash_after_json_commit_retries_without_duplicate_or_model(self):
        child = subprocess.run([sys.executable, "-B", str(ROOT / "tests" / "projection_process_fixture.py"),
            str(self.root), "crash", self.now.isoformat()], cwd=self.root,
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(child.returncode, 23, child.stderr)
        self.assertFalse(self.saved()[1].get("projected"))
        before = self.incidents.file_path.read_bytes()
        restarted = create_runtime(self.root, clock=lambda: self.now)
        agent = FixtureAgent()
        self.assertIsNone(Worker(restarted, agent).run_once())
        agent.request_durable_response.assert_not_called()
        self.assertTrue(self.saved()[1]["projected"])
        self.assertEqual(self.incidents.file_path.read_bytes(), before)
        self.assertEqual(len(self.incidents.get_incident(self.session.incident_id).investigations), 1)

    def test_two_actual_process_writers_preserve_monitor_and_worker_fields(self):
        children = [subprocess.Popen([sys.executable, "-B", str(ROOT / "tests" / "projection_process_fixture.py"),
            str(self.root), mode, self.session.incident_id], cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            for mode in ("monitor", "worker")]
        try:
            deadline = time.monotonic() + 15
            while not all((self.root / ("ready-" + mode)).exists() for mode in ("monitor", "worker")):
                if time.monotonic() > deadline:
                    self.fail("writer startup timeout")
                time.sleep(0.01)
            (self.root / "release").write_text("release")
            for child in children:
                output, error = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, error)
                self.assertEqual(json.loads(output)["writes"], 30)
            incident = self.incidents.get_incident(self.session.incident_id)
            self.assertEqual(incident.alerts[-1].value, 30)
            self.assertEqual(incident.related_alerts, [{"writer": "monitor", "index": 29}])
            self.assertEqual({e.observation_id for e in incident.evidence},
                             {"writer-obs-" + str(i) for i in range(30)})
        finally:
            (self.root / "release").write_text("release")
            for child in children:
                if child.poll() is None:
                    child.communicate(timeout=30)
