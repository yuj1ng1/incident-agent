"""Lifecycle, lossless retention and bounded idle-work regression tests."""
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from incident_store import IncidentStore
from investigation.manager import live_alert_for_incident
from investigation.models import HumanCommand, InvestigationSession, SessionStatus as S
from investigation.scheduler import Scheduler
from investigation.state import RuntimeErrorBase, transition
from investigation.store import RuntimeStore
from investigation.worker import Worker
from models.incident import Evidence
from models.investigation import InvestigationRecord
from reinvestigation import should_reinvestigate
from runtime_support import FixtureAgent, call, create_runtime, create_session, response

ROOT = Path(__file__).resolve().parents[1]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="runtime-stability-")))
        self.now = datetime.now(timezone.utc)
        self.manager = create_runtime(self.root, clock=lambda: self.now)
        self.store = self.manager.store
        self.session = create_session(self.manager)
        self.verifier = Mock(side_effect=lambda incident: incident.alerts[-1].model_copy(update={"observed_at": self.now}))
        self.manager.alert_verifier = self.verifier

    def incident(self):
        return self.manager.incident_store.get_incident(self.session.incident_id)

    def current_alert(self, value=None):
        incident = self.incident()
        incident.alerts[-1].observed_at = self.now
        if value is not None:
            incident.alerts[-1].value = value
        self.manager.incident_store.save_observation(incident.fingerprint, incident)
        return self.incident()

    def command(self, kind="reopen", command_id="reopen-1", **changes):
        return HumanCommand(session_id=self.session.session_id, actor="operator-z", command_type=kind,
                            command_id=command_id, payload={"reason": "配置已修正，重新授权有限调查"} if kind == "reopen" else {}, **changes)

    def fail(self, category="model_empty_answer"):
        return Worker(self.manager, FixtureAgent([RuntimeErrorBase(category)])).run_once()

    def refusal(self):
        return self.manager.workflow(self.store.list_sessions(incident_id=self.session.incident_id)[-1].session_id)["reinvestigation_refusal"]

    def test_retryable_failed_uses_exponential_policy_backoff_and_keeps_history(self):
        first = self.fail()
        self.assertEqual(first.status, S.FAILED)
        self.assertEqual(self.incident().consecutive_investigation_failures, 1)
        self.now += timedelta(seconds=59)
        self.manager.enqueue(self.current_alert(), True)
        self.assertEqual(self.refusal(), "failure_backoff")
        self.now += timedelta(seconds=1)
        second = self.manager.enqueue(self.current_alert(), True)
        self.assertNotEqual(second.session_id, first.session_id)
        self.fail()
        self.assertEqual(self.incident().consecutive_investigation_failures, 2)
        self.now += timedelta(seconds=119)
        self.manager.enqueue(self.current_alert(), True)
        self.assertEqual(self.refusal(), "failure_backoff")
        self.now += timedelta(seconds=1)
        third = self.manager.enqueue(self.current_alert(), True)
        self.assertNotEqual(third.session_id, second.session_id)
        failed = self.fail()
        self.assertEqual(failed.status, S.FAILED)
        self.now += timedelta(seconds=300)
        self.manager.enqueue(self.current_alert(), True)
        self.assertEqual(self.refusal(), "policy_failure_limit")
        self.assertEqual(len(self.store.list_sessions(incident_id=first.incident_id)), 3)
        self.assertEqual(len(self.incident().investigations), 3)
        self.assertEqual(len({e.observation_id for e in self.incident().evidence}), 3)

    def test_repeated_429_exhausts_one_session_and_other_incident_can_progress(self):
        class RateLimit(Exception):
            status_code = 429
        agent = FixtureAgent([RateLimit(), RateLimit(), RateLimit()])
        worker = Worker(self.manager, agent)
        self.assertEqual(worker.run_once().status, S.WAITING_RETRY)
        other = create_session(self.manager, "inventory-service")
        self.assertEqual(Worker(self.manager, FixtureAgent()).run_once().session_id, other.session_id)
        self.now += timedelta(seconds=6)
        self.assertEqual(worker.run_once().status, S.WAITING_RETRY)
        self.now += timedelta(seconds=11)
        result = worker.run_once()
        self.assertEqual((result.status, result.attempt_count, result.total_model_requests), (S.EXHAUSTED, 3, 3))
        for _ in range(3):
            self.now += timedelta(seconds=300)
            self.manager.enqueue(self.current_alert(), True)
        self.assertEqual(self.refusal(), "exhausted_requires_human_reauthorization")
        self.assertEqual(agent.request_durable_response.call_count, 3)
        self.assertEqual(len(self.store.list_sessions(incident_id=result.incident_id)), 1)
        rejects = [e for e in self.store.events(result.session_id) if e["event_type"] == "reinvestigation_rejected"]
        self.assertEqual(len(rejects), 1)

    def test_unknown_failed_requires_audited_reopen_and_same_id_is_idempotent(self):
        failed = Worker(self.manager, FixtureAgent([ValueError("invalid configuration")])).run_once()
        self.now += timedelta(seconds=300)
        self.manager.enqueue(self.current_alert(), True)
        self.assertEqual(self.refusal(), "non_retryable_failure_requires_human_reopen")
        first = self.manager.submit(self.command())
        again = self.manager.submit(self.command())
        self.assertEqual((first.processing_status, first.result), ("applied", again.result))
        self.assertEqual(first, again)
        self.verifier.assert_called_once()
        renewed = self.store.get(first.result)
        self.assertEqual(renewed.budget_group_id, renewed.session_id)
        self.assertNotEqual(renewed.session_id, failed.session_id)
        self.assertEqual(self.incident().consecutive_investigation_failures, 0)
        self.assertEqual(len(self.store.list_sessions()), 2)
        audit = next(e for e in self.store.events(failed.session_id) if e["event_type"] == "investigation_reopened")
        self.assertEqual(audit["actor"], "operator-z")
        self.assertEqual(audit["data"]["command_id"], "reopen-1")
        self.assertEqual(audit["data"]["reason"], "配置已修正，重新授权有限调查")
        self.assertEqual(audit["data"]["previous_usage"]["model_requests"], 1)
        self.assertEqual(audit["data"]["authorized_limits"]["max_model_requests"], 21)
        result = Worker(self.manager, FixtureAgent()).run_once()
        self.assertEqual((result.status, result.total_model_requests), (S.COMPLETED, 1))
        self.assertEqual(len(self.incident().investigations), 2)

    def test_exhausted_grant_can_only_be_renewed_by_human_command(self):
        self.manager.limits.max_model_requests = 1
        exhausted = self.fail()
        self.assertEqual(exhausted.status, S.EXHAUSTED)
        self.manager.enqueue(self.current_alert(), True)
        self.assertEqual(len(self.store.list_sessions()), 1)
        grant = self.manager.submit(self.command())
        result = Worker(self.manager, FixtureAgent()).run_once()
        self.assertEqual(grant.result, result.session_id)
        self.assertEqual(result.status, S.COMPLETED)
        self.assertEqual(sum(s.total_model_requests for s in self.store.list_sessions()), 2)
        self.assertEqual(self.store.get(exhausted.session_id).total_model_requests, 1)

    def test_cancelled_does_not_revive_on_poll_but_human_can_reopen_active_incident(self):
        self.manager.submit(self.command("cancel", "cancel-1"))
        self.manager.recover()
        self.manager.enqueue(self.current_alert(), True)
        self.assertEqual(self.refusal(), "cancelled_requires_human_reopen")
        self.assertEqual(len(self.store.list_sessions()), 1)
        grant = self.manager.submit(self.command())
        self.assertEqual(grant.processing_status, "applied")
        self.assertEqual(self.store.get(self.session.session_id).status, S.CANCELLED)
        self.assertEqual(self.store.get(grant.result).status, S.QUEUED)

    def test_reopen_refuses_resolved_missing_unverified_or_stale_alert(self):
        for mode in ("resolved", "missing", "no_firing", "stale", "failed_query", "unconfigured"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                manager = create_runtime(directory, clock=lambda: self.now)
                session = create_session(manager)
                Worker(manager, FixtureAgent([ValueError("bad")])).run_once()
                incident = manager.incident_store.get_incident(session.incident_id)
                alert = incident.alerts[-1].model_copy(update={"observed_at": self.now})
                manager.alert_verifier = Mock(return_value=alert)
                if mode == "resolved":
                    manager.incident_store.resolve_inactive_incidents(set())
                elif mode == "missing":
                    data = manager.incident_store.load()
                    data["incidents"].pop(session.incident_id)
                    data["active_by_fingerprint"].clear()
                    manager.incident_store.save(data)
                elif mode == "no_firing":
                    manager.alert_verifier.return_value = None
                elif mode == "stale":
                    manager.alert_verifier.return_value = alert.model_copy(update={"observed_at": self.now - timedelta(seconds=61)})
                elif mode == "failed_query":
                    manager.alert_verifier.side_effect = TimeoutError("private details")
                else:
                    manager.alert_verifier = None
                command = manager.submit(HumanCommand(session_id=session.session_id, actor="operator", command_type="reopen", payload={"reason": "retry"}))
                self.assertEqual(command.processing_status, "rejected")
                self.assertEqual(len(manager.store.list_sessions()), 1)
                self.assertNotIn("private details", command.result)

    def test_reopen_latest_check_actor_reason_version_and_command_conflict(self):
        self.fail("unexpected_configuration")
        for actor in ("", "  ", "operator\nforged"):
            with self.assertRaises(ValueError):
                HumanCommand(session_id=self.session.session_id, actor=actor, command_type="reopen", payload={"reason": "fix"})
        with self.assertRaisesRegex(RuntimeErrorBase, "reason_required"):
            self.manager.submit(HumanCommand(session_id=self.session.session_id, actor="operator", command_type="reopen"))
        rejected = self.manager.submit(self.command(command_id="stale-version", expected_version=0))
        self.assertEqual(rejected.result, "command_state_version_conflict")
        self.assertEqual(len(self.store.list_sessions()), 1)
        self.manager.submit(self.command())
        rejected = self.manager.submit(self.command(command_id="another-reopen"))
        self.assertEqual(rejected.result, "reopen_requires_latest_session")
        with self.assertRaisesRegex(RuntimeErrorBase, "command_id_conflict"):
            self.manager.submit(self.command().model_copy(update={"payload": {"reason": "different"}}))
        self.assertEqual(len(self.store.list_sessions()), 2)

    def test_pause_waiting_human_keep_original_session_and_pending_answer(self):
        question = call("request_human_input", {"question": "部署情况？"}, "question")
        waiting = Worker(self.manager, FixtureAgent([response(answer="", calls=[question])])).run_once()
        request = self.manager.requests(waiting.session_id)[0]
        for status in (S.WAITING_HUMAN, S.PAUSED):
            if status == S.PAUSED:
                self.manager.submit(self.command("pause", "pause-1"))
            self.manager.enqueue(self.current_alert(), True)
            self.assertEqual(len(self.store.list_sessions()), 1)
            self.assertEqual(self.refusal(), "existing_session_" + status.value.lower())
            rejected = self.manager.submit(self.command(command_id="reopen-" + status.value))
            self.assertEqual(rejected.processing_status, "rejected")
            self.assertEqual(self.manager.requests(waiting.session_id)[0].request_id, request.request_id)
        answer = HumanCommand(session_id=waiting.session_id, actor="operator", command_type="answer",
                              payload={"request_id": request.request_id, "text": "背景线索"})
        self.assertEqual(self.manager.submit(answer).processing_status, "applied")
        self.assertEqual(self.store.get(waiting.session_id).status, S.PAUSED)
        self.manager.submit(self.command("resume", "resume-1"))
        self.assertEqual(Worker(self.manager, FixtureAgent()).run_once().status, S.COMPLETED)
        self.assertEqual(self.manager.requests(waiting.session_id)[0].status, "consumed")

    def test_completed_reinvestigation_uses_cooldown_and_cumulative_attempt_limit(self):
        first = Worker(self.manager, FixtureAgent()).run_once()
        self.now += timedelta(seconds=299)
        self.manager.enqueue(self.current_alert(), True)
        self.assertEqual(self.refusal(), "policy_cooldown")
        self.now += timedelta(seconds=1)
        second = self.manager.enqueue(self.current_alert(), True)
        self.assertNotEqual(first.session_id, second.session_id)
        Worker(self.manager, FixtureAgent()).run_once()
        self.now += timedelta(seconds=300)
        self.manager.enqueue(self.current_alert(), True)
        Worker(self.manager, FixtureAgent()).run_once()
        self.now += timedelta(seconds=300)
        self.manager.enqueue(self.current_alert(), True)
        self.assertEqual(self.refusal(), "incident_attempt_budget_exhausted")
        self.assertEqual(len(self.store.list_sessions()), 3)
        last = self.store.list_sessions()[-1]
        granted = self.manager.submit(HumanCommand(session_id=last.session_id, actor="operator-z", command_type="reopen",
                                                  payload={"reason": "持续 firing，人工重新授权调查"}))
        self.assertEqual(granted.processing_status, "applied")
        self.assertEqual(Worker(self.manager, FixtureAgent()).run_once().status, S.COMPLETED)

    def test_cumulative_budget_is_isolated_between_incidents(self):
        self.manager.limits.max_model_requests = 1
        self.fail()
        other = create_session(self.manager, "inventory-service")
        agent = FixtureAgent()
        result = Worker(self.manager, agent).run_once()
        self.assertEqual((result.session_id, result.status, result.total_model_requests), (other.session_id, S.COMPLETED, 1))
        agent.request_durable_response.assert_called_once()

    def test_alert_cleared_after_reopen_cancels_before_any_external_call(self):
        self.fail("bad_configuration")
        granted = self.manager.submit(self.command())
        self.manager.incident_store.resolve_inactive_incidents(set())
        agent = FixtureAgent()
        self.assertIsNone(Worker(self.manager, agent).run_once())
        self.assertEqual(self.store.get(granted.result).status, S.CANCELLED)
        agent.request_durable_response.assert_not_called()
        self.assertEqual(agent.executions, [])

    def test_direct_incident_resume_delegates_to_audited_runtime_command(self):
        self.fail("bad_configuration")
        command = self.manager.incident_store.resume_investigation(
            self.session.incident_id, manager=self.manager, actor="operator-z",
            reason="修正错误后重新授权", command_id="store-reopen",
        )
        self.assertEqual(command.processing_status, "applied")
        self.assertEqual(self.manager.get_command("store-reopen").result, command.result)
        self.assertEqual(self.incident().alert_state, "firing")
        self.assertEqual(len(self.incident().evidence), 1)
        again = self.manager.incident_store.resume_investigation(
            self.session.incident_id, manager=self.manager, actor="operator-z",
            reason="修正错误后重新授权", command_id="store-reopen",
        )
        self.assertEqual(command, again)
        self.assertEqual(len(self.store.list_sessions()), 2)

    def test_last_permitted_recovery_runs_and_next_crash_is_exhausted(self):
        self.manager.limits.max_resumes = 1
        old = self.store.claim("owner", self.manager.limits)
        self.now += timedelta(seconds=121)
        self.manager.recover()
        recovered = self.store.get(old.session_id)
        self.assertEqual((recovered.status, recovered.resume_count), (S.QUEUED, 1))
        second = self.store.claim("owner-2", self.manager.limits)
        self.now += timedelta(seconds=121)
        self.manager.recover()
        exhausted = self.store.get(second.session_id)
        self.assertEqual((exhausted.status, exhausted.resume_count), (S.EXHAUSTED, 1))
        self.assertEqual(exhausted.last_error_category, "session_resume_budget_exhausted")

    def test_legacy_aware_and_naive_cooldown_use_same_beijing_timestamp(self):
        incident = self.incident()
        now = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)
        for value in (datetime(2026, 10, 10, 17, 55), datetime(2026, 10, 10, 9, 55, tzinfo=timezone.utc)):
            incident.last_investigated_at = value
            incident.last_investigated_value = 0.3
            self.assertTrue(should_reinvestigate(incident, incident.alerts[-1], now))
            self.assertFalse(should_reinvestigate(incident, incident.alerts[-1], now - timedelta(seconds=1)))


class EntryImportTests(unittest.TestCase):
    def run_child(self, root, mode="imports"):
        env = os.environ.copy()
        for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN"):
            env.pop(name, None)
        return subprocess.run([sys.executable, "-B", str(ROOT / "tests" / "import_entry_fixture.py"), str(root), mode],
                              cwd=root, env=env, capture_output=True, text=True, timeout=20)

    def test_all_entry_imports_are_inert_in_isolated_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "incidents.json").write_text('{"sentinel":"keep"}')
            (root / "runtime.sqlite3").write_bytes(b"import must not open this invalid store")
            child = self.run_child(root)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(json.loads(child.stdout), {"imports": ["main", "runtime_cli", "remediation_cli",
                                                                   "scripts.validate_durable_runtime", "scripts.validate_multi_signal"],
                                                       "unchanged_files": 2, "threads": 1})

    def test_import_oracle_rejects_network_thread_file_and_model_attempts(self):
        for mode in ("probe-network", "probe-thread", "probe-write", "probe-model"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                child = self.run_child(Path(directory), mode)
                self.assertNotEqual(child.returncode, 0)
                self.assertIn("entry_", child.stderr)
                self.assertEqual(list(Path(directory).iterdir()), [])


class HistoryRetentionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="incident-history-")))
        self.now = datetime.now(timezone.utc)
        self.manager = create_runtime(self.root, clock=lambda: self.now)
        self.session = create_session(self.manager)
        self.store = self.manager.incident_store

    def history(self, count=12):
        incident = self.store.get_incident(self.session.incident_id)
        incident.evidence = [Evidence(source="legacy", summary="legacy only in JSON", detail="legacy-original")]
        incident.investigations = [InvestigationRecord(
            investigated_at=self.now - timedelta(days=1), status="completed", answer="legacy original",
        )]
        for i in range(count):
            sid = "history-session-" + str(i)
            stamp = self.now + timedelta(seconds=i)
            incident.investigations.append(InvestigationRecord(session_id=sid, investigated_at=stamp,
                                                               status="completed", answer="answer-" + str(i),
                                                               model_rounds=2, tool_calls=1))
            incident.evidence.append(Evidence(source="query_logs", summary="historical observation",
                                              detail=f"original-{i}:" + "e" * 10000, collected_at=stamp,
                                              runtime_session_id=sid, observation_id="history-obs-" + str(i)))
        return incident

    def test_hot_history_is_bounded_and_all_original_records_remain_queryable(self):
        incident = self.history()
        expected_evidence = {e.observation_id or "legacy": e.model_dump(mode="json") for e in incident.evidence}
        expected_records = {r.session_id or "legacy": r.model_dump(mode="json") for r in incident.investigations}
        original_size = len(incident.model_dump_json().encode("utf-8"))
        self.store.save_incident(incident.fingerprint, incident)
        hot = self.store.get_incident(incident.incident_id, include_history=False)
        self.assertEqual([r.session_id for r in hot.investigations], [None, "history-session-9", "history-session-10", "history-session-11"])
        self.assertEqual(len(hot.evidence), 4)
        self.assertEqual(len(hot.history_archives), 1)
        self.assertLess(len(hot.model_dump_json().encode("utf-8")), original_size // 2)
        restored = IncidentStore(self.store.file_path).get_incident(incident.incident_id)
        self.assertEqual({e.observation_id or "legacy": e.model_dump(mode="json") for e in restored.evidence}, expected_evidence)
        self.assertEqual({r.session_id or "legacy": r.model_dump(mode="json") for r in restored.investigations}, expected_records)
        self.assertEqual(self.store.load()["version"], 2)
        self.assertEqual(restored.evidence[-1].detail[:12], "original-11:")

    def test_historical_query_in_separate_cli_process_includes_archived_evidence(self):
        incident = self.history()
        self.store.save_incident(incident.fingerprint, incident)
        child = subprocess.run([
            sys.executable, "-B", str(ROOT / "runtime_cli.py"), "--store", str(self.root / "runtime.sqlite3"),
            "--incidents", str(self.store.file_path), "--remediations", str(self.root / "remediation.json"),
            "show-incident", incident.incident_id,
        ], cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(child.returncode, 0, child.stderr)
        view = json.loads(child.stdout)
        self.assertEqual(len(view["evidence"]), 13)
        self.assertEqual({r["answer"] for r in view["investigations"]}, {"legacy original"} | {"answer-" + str(i) for i in range(12)})
        old = next(e for e in view["evidence"] if e["observation_id"] == "history-obs-0")
        self.assertEqual(old["detail"], "original-0:" + "e" * 10000)

    def test_hydrated_history_can_be_saved_when_observation_times_reorder_it(self):
        incident = self.history()
        incident.evidence[1].collected_at = self.now + timedelta(minutes=1)
        expected = {e.observation_id or "legacy": e.model_dump(mode="json") for e in incident.evidence}
        self.store.save_incident(incident.fingerprint, incident)
        hydrated = self.store.get_incident(incident.incident_id)
        self.assertEqual(hydrated.evidence[-1].observation_id, "history-obs-0")
        hydrated.related_alerts = [{"reason": "new monitoring observation"}]
        self.store.save_incident(hydrated.fingerprint, hydrated)
        restored = self.store.get_incident(incident.incident_id)
        self.assertEqual({e.observation_id or "legacy": e.model_dump(mode="json") for e in restored.evidence}, expected)
        self.assertEqual(restored.related_alerts, [{"reason": "new monitoring observation"}])

    def test_archive_failure_preserves_hot_history_and_replay_is_lossless(self):
        incident = self.history()
        self.store.runtime_history_to_keep = 20
        self.store.save_incident(incident.fingerprint, incident)
        original = self.store.file_path.read_bytes()
        self.store.runtime_history_to_keep = 3
        replace = os.replace
        def fail_archive(source, target):
            if Path(target).parent.name == "history":
                raise OSError("isolated archive failure")
            return replace(source, target)
        with patch("incident_store.os.replace", side_effect=fail_archive):
            with self.assertRaises(OSError):
                self.store.save_incident(incident.fingerprint, incident)
        self.assertEqual(self.store.file_path.read_bytes(), original)
        self.assertEqual(len(self.store.get_incident(incident.incident_id).evidence), 13)
        self.store.save_incident(incident.fingerprint, incident)
        self.assertEqual(len(self.store.get_incident(incident.incident_id).evidence), 13)

    def test_manifest_commit_failure_leaves_recoverable_hot_and_archive_data(self):
        incident = self.history()
        self.store.runtime_history_to_keep = 20
        self.store.save_incident(incident.fingerprint, incident)
        original = self.store.file_path.read_bytes()
        self.store.runtime_history_to_keep = 3
        with patch.object(self.store, "save", side_effect=OSError("crash before hot commit")):
            with self.assertRaises(OSError):
                self.store.save_incident(incident.fingerprint, incident)
        self.assertEqual(self.store.file_path.read_bytes(), original)
        archive_files = list((self.root / "history").glob("*.json"))
        self.assertEqual(len(archive_files), 1)
        self.store.save_incident(incident.fingerprint, incident)
        self.assertEqual(list((self.root / "history").glob("*.json")), archive_files)
        self.assertEqual(len(self.store.get_incident(incident.incident_id).evidence), 13)

    def test_missing_or_corrupt_archive_fails_closed_instead_of_returning_empty_history(self):
        incident = self.history()
        self.store.save_incident(incident.fingerprint, incident)
        ref = self.store.get_incident(incident.incident_id, include_history=False).history_archives[0]
        path = self.root / "history" / (ref.sha256 + ".json")
        original = path.read_bytes()
        path.write_bytes(original + b"corrupt")
        with self.assertRaisesRegex(ValueError, "checksum_mismatch"):
            self.store.get_incident(incident.incident_id)
        path.write_bytes(original)
        hidden = path.with_suffix(".unavailable")
        path.rename(hidden)
        try:
            with self.assertRaises(FileNotFoundError):
                self.store.get_incident(incident.incident_id)
        finally:
            hidden.rename(path)

    def test_legacy_v1_history_is_not_archived_without_session_identity(self):
        incident = self.history(0)
        payload = {incident.fingerprint: incident.model_dump(mode="json")}
        self.store.file_path.write_text(json.dumps(payload), encoding="utf-8")
        restored = self.store.get_incident(incident.incident_id)
        self.store.save_incident(incident.fingerprint, restored)
        self.assertEqual(self.store.load()["version"], 2)
        self.assertEqual(self.store.get_incident(incident.incident_id).evidence, incident.evidence)
        self.assertEqual(self.store.get_incident(incident.incident_id).investigations, incident.investigations)
        self.assertFalse((self.root / "history").exists())

    def test_archived_observation_cannot_be_duplicated_by_projection_replay(self):
        incident = self.history()
        self.store.save_incident(incident.fingerprint, incident)
        old = InvestigationSession(session_id="history-session-0", incident_id=incident.incident_id,
                                   alert_fingerprint=incident.fingerprint, service=incident.service, status=S.COMPLETED)
        before = self.store.file_path.read_bytes()
        self.store.apply_runtime_projection(old, {"records": [], "result": {"success": True, "status": "completed", "answer": "duplicate"}})
        self.assertEqual(self.store.file_path.read_bytes(), before)
        self.assertEqual(len(self.store.get_incident(incident.incident_id).investigations), 13)

    def test_stale_manifest_and_changes_to_immutable_history_are_rejected(self):
        stale = self.history()
        self.store.save_incident(stale.fingerprint, stale)
        with self.assertRaisesRegex(ValueError, "manifest_conflict"):
            self.store.save_incident(stale.fingerprint, stale)
        full = self.store.get_incident(stale.incident_id)
        archived = next(r for r in full.investigations if r.session_id == "history-session-0")
        archived.answer = "silently changed historical fact"
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.store.save_incident(full.fingerprint, full)

    def test_latest_checkpoint_keeps_all_observations_when_old_versions_are_pruned(self):
        self.manager.limits.checkpoints_to_keep = 1
        agent = FixtureAgent([response(answer="", calls=[call(call_id="metric")]), response(answer="persisted final")])
        completed = Worker(self.manager, agent).run_once()
        reopened = RuntimeStore(self.manager.store.path, clock=lambda: self.now)
        payload = reopened.load_checkpoint(reopened.get(completed.session_id))
        self.assertEqual({r["name"] for r in payload["records"]}, {"get_running_version", "query_metrics"})
        self.assertEqual(payload["result"]["answer"], "persisted final")
        with reopened.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM investigation_checkpoints WHERE session_id=?", (completed.session_id,)).fetchone()[0], 1)
        events = reopened.events(completed.session_id, 1000)
        self.assertTrue(all(e["data"].get("checkpoint_id") for e in events if e["event_type"] == "checkpoint_saved"))
        refs = {e.observation_id for e in self.store.get_incident(completed.incident_id).evidence}
        self.assertEqual(refs, {r["observation_id"] for r in payload["records"]})


class RuntimeWorkTests(unittest.TestCase):
    def test_duplicate_reopen_from_two_processes_grants_exactly_one_new_budget(self):
        from alert_parser import normalize_snapshot
        from fingerprint import generate_alert_fingerprint
        from incident_factory import create_incident_from_alert
        with tempfile.TemporaryDirectory(prefix="reopen-process-") as directory:
            root = Path(directory)
            raw = {"status": "success", "data": {"alerts": [{
                "state": "firing", "labels": {"alertname": "HighHttp5xxRate", "service": "order-service",
                                               "severity": "critical", "metric": "http_5xx_rate", "signal_type": "numeric"},
                "annotations": {"threshold": "0.2", "unit": "ratio", "direction": "higher_is_worse",
                                "window": "1m", "minimum_requests": "20"},
                "activeAt": datetime.now(timezone.utc).isoformat(), "value": "0.3",
            }]}}
            alert = normalize_snapshot(raw).alerts[0]
            manager = create_runtime(root)
            incident = create_incident_from_alert(alert, generate_alert_fingerprint(alert))
            manager.incident_store.save_incident(incident.fingerprint, incident)
            session = manager.enqueue(incident)
            Worker(manager, FixtureAgent([ValueError("invalid")])).run_once()
            (root / "live-alert-fixture.json").write_text(json.dumps(raw), encoding="utf-8")
            children = [subprocess.Popen([sys.executable, "-B", str(ROOT / "tests" / "reopen_process_fixture.py"),
                                          str(root), session.session_id, owner], cwd=root, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, encoding="utf-8") for owner in ("one", "two")]
            try:
                deadline = time.monotonic() + 20
                while not all((root / ("ready-reopen-" + owner)).exists() for owner in ("one", "two")):
                    if time.monotonic() >= deadline:
                        self.fail("Both independent CLI processes must reach the barrier")
                    time.sleep(0.02)
                (root / "release-reopen").write_text("release")
                views = []
                for child in children:
                    output, errors = child.communicate(timeout=30)
                    self.assertEqual(child.returncode, 0, errors)
                    views.append(json.loads(output))
                self.assertEqual({v["command_id"] for v in views}, {"same-reopen-command"})
                command = manager.get_command("same-reopen-command")
                self.assertEqual(command.processing_status, "applied")
                self.assertEqual(len(manager.store.list_sessions()), 2)
                self.assertEqual(manager.store.get(command.result).budget_group_id, command.result)
                grants = [e for e in manager.store.events(command.result) if e["event_type"] == "budget_reauthorized"]
                self.assertEqual(len(grants), 1)
                self.assertEqual(grants[0]["actor"], "process-operator")
                with manager.store.read() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM investigation_commands").fetchone()[0], 1)
            finally:
                (root / "release-reopen").write_text("release")
                for child in children:
                    if child.poll() is None:
                        child.communicate(timeout=30)

    def test_warm_idle_work_and_write_transactions_do_not_grow_with_terminal_history(self):
        measured = []
        for count in (0, 1000):
            with self.subTest(terminal_sessions=count), tempfile.TemporaryDirectory() as directory:
                manager = create_runtime(directory)
                create_session(manager)
                scheduler = Scheduler(Worker(manager, FixtureAgent()))
                with redirect_stdout(io.StringIO()):
                    scheduler.tick()
                with manager.store.transaction() as db:
                    for i in range(count):
                        session = InvestigationSession(incident_id="old-" + str(i), alert_fingerprint="old-fp-" + str(i),
                                                       service="order-service", status=S.COMPLETED)
                        manager.store.insert(session, db)
                        manager.store.checkpoint(session, {"schema_version": 1, "phase": "ready", "records": [],
                                                          "projected": True}, db, acknowledge=True)
                        manager.store.save(session, db)
                writes = 0
                original = manager.store.transaction
                @contextmanager
                def track_writes():
                    nonlocal writes
                    writes += 1
                    with original() as db:
                        yield db
                with patch.object(manager.store, "transaction", track_writes), \
                     patch.object(manager.store, "_session", wraps=manager.store._session) as hydrated, \
                     patch.object(manager.store, "load_checkpoint", wraps=manager.store.load_checkpoint) as checkpoints:
                    for _ in range(3):
                        self.assertIsNone(scheduler.tick())
                measured.append((writes, hydrated.call_count, checkpoints.call_count))
                self.assertEqual(writes, 0)
                self.assertEqual(checkpoints.call_count, 0)
                self.assertEqual(hydrated.call_count, 3)
        self.assertEqual(measured, [(0, 3, 0), (0, 3, 0)])

    def test_read_queries_use_one_select_without_n_plus_one_or_writer_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = create_runtime(directory)
            session = create_session(manager)
            statements = []
            with manager.store.read() as db:
                db.set_trace_callback(statements.append)
                sessions = manager.store.list_sessions(db)
                self.assertEqual([s.session_id for s in sessions], [session.session_id])
                other = sqlite3.connect(manager.store.path, timeout=0.1, isolation_level=None)
                try:
                    other.execute("BEGIN IMMEDIATE")  # Reads have no RESERVED writer lock.
                    other.rollback()
                finally:
                    other.close()
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("UPDATE runtime_metadata SET value=value")
            self.assertEqual(len([s for s in statements if s.startswith("SELECT")]), 1)

    def test_pending_projection_and_command_survive_restart_and_outbox_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = create_runtime(directory)
            session = create_session(manager)
            manager.submit(HumanCommand(session_id=session.session_id, actor="operator", command_type="pause"))
            with manager.store.transaction() as db:
                command = HumanCommand(session_id=session.session_id, actor="operator", command_type="message", payload={"text": "pending durable clue"})
                db.execute("INSERT INTO investigation_commands(command_id,session_id,status,data) VALUES (?,?,?,?)",
                           (command.command_id, session.session_id, "pending", command.model_dump_json()))
                db.execute("DELETE FROM runtime_metadata WHERE key='work_outboxes_v1' OR key LIKE 'pending_projection:%' OR key LIKE 'pending_notification:%'")
            restarted = create_runtime(directory)
            restarted.recover()
            self.assertEqual(restarted.get_command(command.command_id).processing_status, "applied")
            view = restarted.workflow(session.session_id)
            self.assertEqual(view["human_messages"][0]["text"], "pending durable clue")
            self.assertEqual(view["session"]["status"], "PAUSED")
            self.assertEqual(restarted.store.pending_work("projection"), [])
            with restarted.store.read() as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(len(restarted.store.pending_work("notification")), 1)


if __name__ == "__main__":
    unittest.main()
