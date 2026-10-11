"""Real SQLite transactions and isolated model/tool integration regression checks."""
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import httpx2 as httpx
import openai
from pydantic import ValidationError

from investigation.checkpoint import encode, protect, restore_context, valid_chain
from investigation.evidence_policy import cache_key, effective_records, fresh, stable
from investigation.models import HumanCommand, InvestigationSession, RuntimeLimits, SessionStatus as S
from investigation.state import LostClaim, RuntimeErrorBase, StateVersionConflict, transition
from investigation.store import RuntimeStore
from investigation.worker import Worker
from runtime_cli import main as cli
from runtime_support import FixtureAgent, SHA, call, create_runtime, create_session, response


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="durable-runtime-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = datetime.now(timezone.utc)
        self.manager = create_runtime(self.root, clock=lambda: self.now)
        self.store = self.manager.store
        self.session = create_session(self.manager)

    def submit(self, kind, payload=None, command_id=None, **kwargs):
        fields = {
            "session_id": self.session.session_id, "actor": "test-operator",
            "command_type": kind, "payload": payload or {}, **kwargs,
        }
        if command_id:
            fields["command_id"] = command_id
        return self.manager.submit(HumanCommand(**fields))

    def get(self):
        return self.store.get(self.session.session_id)

    def test_session_create_read_and_deduplicate(self):
        incident = self.manager.incident_store.get_incident(self.session.incident_id)
        self.assertEqual(self.manager.enqueue(incident).session_id, self.session.session_id)
        self.assertEqual(self.get().status, S.QUEUED)

    def test_session_strict_and_utc(self):
        data = self.session.model_dump()
        for changes in ({"bogus": 1}, {"attempt_count": -1}, {"status": "APPROVED"},
                        {"created_at": self.now.replace(tzinfo=None)}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                InvestigationSession.model_validate(data | changes)

    def test_illegal_transition(self):
        with self.assertRaises(RuntimeErrorBase):
            transition(self.session, S.COMPLETED)

    def test_terminal_cannot_resume(self):
        Worker(self.manager, FixtureAgent()).run_once()
        self.assertEqual(self.submit("resume").processing_status, "rejected")

    def test_transaction_rollback(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as db:
                self.store.metadata(db, "rollback", "bad", write=True)
                raise RuntimeError("abort")
        with self.store.transaction() as db:
            self.assertIsNone(self.store.metadata(db, "rollback"))

    def test_unknown_database_version_rejected(self):
        path = self.root / "future.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaises(RuntimeErrorBase):
            RuntimeStore(path)

    def test_corrupt_database_not_replaced(self):
        path = self.root / "corrupt.sqlite3"
        path.write_bytes(b"not a database")
        with self.assertRaises(sqlite3.Error):
            RuntimeStore(path)
        self.assertEqual(path.read_bytes(), b"not a database")

    def test_restart_reads_pending_session(self):
        restarted = create_runtime(self.root)
        self.assertEqual(restarted.store.get(self.session.session_id).status, S.QUEUED)

    def test_claim_excludes_second_worker(self):
        claim = self.store.claim("one", self.manager.limits)
        self.assertIsNotNone(claim)
        self.assertIsNone(RuntimeStore(self.store.path).claim("two", self.manager.limits))

    def test_single_worker_across_multiple_sessions(self):
        create_session(self.manager, service="inventory-service")
        self.store.claim("one", self.manager.limits)
        self.assertIsNone(self.store.claim("two", self.manager.limits))

    def test_expired_owner_is_fenced(self):
        old = self.store.claim("old", self.manager.limits)
        self.now += timedelta(seconds=121)
        self.manager.recover()
        new = self.store.claim("new", self.manager.limits)
        self.assertGreater(new.fencing_token, old.fencing_token)
        with self.store.transaction() as db, self.assertRaises(LostClaim):
            self.store.guard(db, old.session_id, "old", old.fencing_token)

    def test_cas_rejects_stale_version(self):
        with self.store.transaction() as db:
            first = self.store.get(self.session.session_id, db)
            old = first.model_copy(deep=True)
            self.store.save(first, db)
            with self.assertRaises(StateVersionConflict):
                self.store.save(old, db)

    def test_normal_completed_projection_idempotent(self):
        result = Worker(self.manager, FixtureAgent()).run_once()
        self.assertEqual(result.status, S.COMPLETED)
        self.manager.recover()
        incident = self.manager.incident_store.get_incident(result.incident_id)
        self.assertEqual(len(incident.investigations), 1)
        self.assertEqual(incident.investigations[0].session_id, result.session_id)

    def test_model_rounds_and_tools_are_durable(self):
        agent = FixtureAgent([response(answer="", calls=[call()]), response()])
        result = Worker(self.manager, agent).run_once()
        self.assertEqual(result.total_model_requests, 2)
        self.assertEqual(result.total_tool_executions, 2)

    def test_tool_cache_hits_are_separate(self):
        calls = [call(call_id="a"), call(call_id="b")]
        result = Worker(self.manager, FixtureAgent([response(answer="", calls=calls), response()])).run_once()
        self.assertEqual(result.total_tool_executions, 2)
        self.assertEqual(result.cache_hits, 1)

    def test_no_data_not_current_evidence(self):
        agent = FixtureAgent([response(answer="", calls=[call()]), response()],
                             tool_result=lambda item: {"status": "no_data"})
        result = Worker(self.manager, agent).run_once()
        incident = self.manager.incident_store.get_incident(result.incident_id)
        self.assertEqual(incident.evidence, [])

    def test_pause_resume_no_free_budget(self):
        self.submit("pause")
        self.assertEqual(self.get().status, S.PAUSED)
        self.submit("resume")
        self.assertEqual(self.get().resume_count, 1)
        Worker(self.manager, FixtureAgent()).run_once()
        self.assertEqual(self.get().total_model_requests, 1)

    def test_duplicate_command_id_is_idempotent(self):
        first = self.submit("pause", command_id="same")
        second = self.submit("pause", command_id="same")
        self.assertEqual(first, second)
        self.assertEqual(self.get().status, S.PAUSED)

    def test_command_id_conflict(self):
        self.submit("pause", command_id="same")
        with self.assertRaises(RuntimeErrorBase):
            self.submit("cancel", command_id="same")

    def test_command_version_conflict(self):
        result = self.submit("pause", expected_version=999)
        self.assertEqual(result.processing_status, "rejected")
        self.assertEqual(self.get().status, S.QUEUED)

    def test_cancel_prevents_model_execution(self):
        self.submit("cancel")
        agent = FixtureAgent()
        self.assertIsNone(Worker(self.manager, agent).run_once())
        agent.request_durable_response.assert_not_called()

    def test_message_delivery_order(self):
        self.submit("message", {"text": "第一条线索"})
        self.submit("message", {"text": "第二条线索"})
        agent = FixtureAgent()
        Worker(self.manager, agent).run_once()
        context = agent.request_durable_response.call_args.args[0][0]["content"]
        self.assertLess(context.index("第一条线索"), context.index("第二条线索"))
        self.assertIn("human_provided_unverified_context", context)

    def test_message_after_response_cancels_unstarted_tools(self):
        agent = FixtureAgent([response(answer="", calls=[call()]), response()])
        sent = False
        def hook(phase, session_id):
            nonlocal sent
            if phase == "model_checkpoint_committed" and not sent:
                sent = True
                self.submit("message", {"text": "先检查数据库连接情况。"})
        Worker(self.manager, agent, hook).run_once()
        self.assertNotIn("query_metrics", agent.executions)
        self.assertIn("先检查数据库", agent.request_durable_response.call_args.args[0][0]["content"])

    def test_pause_after_model_at_safe_point(self):
        agent = FixtureAgent([response(answer="", calls=[call()])])
        def hook(phase, session_id):
            if phase == "model_after_io":
                self.submit("pause")
        result = Worker(self.manager, agent, hook).run_once()
        self.assertEqual(result.status, S.PAUSED)
        self.assertNotIn("query_metrics", agent.executions)

    def test_message_during_tool_preserves_saved_evidence(self):
        agent = FixtureAgent([response(answer="", calls=[call()]), response()])
        sent = False
        def hook(phase, session_id):
            nonlocal sent
            if phase == "tool_after_io" and len(agent.executions) == 2 and not sent:
                sent = True
                self.submit("message", {"text": "我已经修复数据库（待验证）。"})
        Worker(self.manager, agent, hook).run_once()
        checkpoint = self.store.load_checkpoint(self.get())
        self.assertEqual(len(checkpoint["records"]), 2)
        self.assertEqual(len(checkpoint["messages"]), 1)
        self.assertFalse(any(r["name"] == "human" for r in checkpoint["records"]))

    def test_active_question_and_answer_after_restart(self):
        question = call("request_human_input", {"question": "最近是否修改连接配置？"})
        agent = FixtureAgent([response(answer="", calls=[question])])
        Worker(self.manager, agent).run_once()
        self.assertEqual(self.get().status, S.WAITING_HUMAN)
        self.manager = create_runtime(self.root, clock=lambda: self.now)
        request = self.manager.requests()[0]
        self.submit("answer", {"request_id": request.request_id, "text": "没有修改。"}, command_id="answer")
        followup = FixtureAgent()
        result = Worker(self.manager, followup).run_once()
        self.assertEqual(result.status, S.COMPLETED)
        self.assertEqual(result.total_model_requests, 2)
        self.assertIn("没有修改", followup.request_durable_response.call_args.args[0][0]["content"])
        self.assertEqual(self.manager.requests()[0].status, "consumed")

    def test_waiting_human_releases_worker(self):
        question = call("request_human_input", {"question": "业务背景？"})
        Worker(self.manager, FixtureAgent([response(answer="", calls=[question])])).run_once()
        second = create_session(self.manager, service="inventory-service")
        result = Worker(self.manager, FixtureAgent()).run_once()
        self.assertEqual(result.session_id, second.session_id)
        self.assertEqual(self.get().status, S.WAITING_HUMAN)

    def test_waiting_human_never_polls_model(self):
        question = call("request_human_input", {"question": "业务背景？"})
        agent = FixtureAgent([response(answer="", calls=[question])])
        worker = Worker(self.manager, agent)
        worker.run_once()
        for _ in range(3):
            self.assertIsNone(worker.run_once())
        self.assertEqual(agent.request_durable_response.call_count, 1)

    def test_expired_question_cannot_restore_session(self):
        question = call("request_human_input", {"question": "部署背景？"})
        Worker(self.manager, FixtureAgent([response(answer="", calls=[question])])).run_once()
        request = self.manager.requests()[0]
        self.now += timedelta(seconds=1801)
        self.manager.recover()
        result = self.submit("answer", {"request_id": request.request_id, "text": "旧回答"})
        self.assertEqual(result.processing_status, "rejected")
        self.assertEqual(self.get().status, S.PAUSED)

    def test_cancel_invalidates_request(self):
        question = call("request_human_input", {"question": "部署背景？"})
        Worker(self.manager, FixtureAgent([response(answer="", calls=[question])])).run_once()
        self.submit("cancel")
        self.assertEqual(self.manager.requests()[0].status, "cancelled")

    def test_alert_clear_invalidates_waiting_request(self):
        question = call("request_human_input", {"question": "部署背景？"})
        Worker(self.manager, FixtureAgent([response(answer="", calls=[question])])).run_once()
        self.manager.incident_store.resolve_inactive_incidents(set())
        self.manager.recover()
        self.assertEqual(self.get().status, S.CANCELLED)
        self.assertEqual(self.manager.requests()[0].status, "cancelled")

    def test_model_auth_blocks_all_sessions(self):
        error = openai.APIStatusError("blocked", response=httpx.Response(402, request=httpx.Request("POST", "https://example.test")), body=None)
        result = Worker(self.manager, FixtureAgent([error])).run_once()
        self.assertEqual(result.status, S.PAUSED)
        create_session(self.manager, service="inventory-service")
        second = FixtureAgent()
        self.assertIsNone(Worker(self.manager, second).run_once())
        second.request_durable_response.assert_not_called()

    def test_resume_never_clears_global_block_or_changes_other_sessions(self):
        for status in (401, 402, 403):
            with self.subTest(status=status), tempfile.TemporaryDirectory(prefix="global-block-") as directory:
                manager = create_runtime(directory, clock=lambda: self.now)
                first = create_session(manager)
                other = create_session(manager, service="inventory-service", severity="warning")
                error = openai.APIStatusError("blocked", response=httpx.Response(
                    status, request=httpx.Request("POST", "https://example.test")), body=None)
                paused = Worker(manager, FixtureAgent([error])).run_once()
                self.assertEqual(paused.session_id, first.session_id)
                self.assertEqual(paused.status, S.PAUSED)
                other_before = manager.store.get(other.session_id)
                resume = manager.submit(HumanCommand(session_id=first.session_id, actor="session-operator", command_type="resume"))
                self.assertEqual(resume.processing_status, "applied")
                self.assertEqual(manager.store.get(first.session_id).status, S.QUEUED)
                self.assertEqual(manager.store.get(other.session_id), other_before)
                agent = FixtureAgent()
                self.assertIsNone(Worker(manager, agent).run_once())
                self.assertEqual(agent.executions, [])
                agent.request_durable_response.assert_not_called()
                with manager.store.transaction() as db:
                    self.assertEqual(manager.store.metadata(db, "model_blocked"), "model_http_" + str(status))
                operation = manager.unblock_model("global-operator")
                self.assertEqual(operation["previous_reason"], "model_http_" + str(status))
                self.assertEqual(operation["result"], "unblocked")
                self.assertEqual(operation["actor"], "global-operator")
                self.assertEqual(operation["at"], self.now.isoformat())
                repeated = manager.unblock_model("global-operator")
                self.assertEqual(repeated["result"], "already_unblocked")
                with manager.store.transaction() as db:
                    for audit in (operation, repeated):
                        persisted = manager.store.metadata(db, audit["operation_id"])
                        self.assertEqual(persisted["result"], audit["result"])
                        self.assertEqual(persisted["actor"], "global-operator")
                    self.assertIsNone(manager.store.metadata(db, "model_blocked"))
                self.assertEqual(Worker(manager, agent).run_once().status, S.COMPLETED)
                self.assertEqual(Worker(manager, FixtureAgent()).run_once().status, S.COMPLETED)

    def test_invalid_unblock_actor_keeps_global_block(self):
        with self.store.transaction() as db:
            self.store.metadata(db, "model_blocked", "model_http_402", write=True)
        for actor in ("", " ", "operator\nforged", "a" * 101):
            with self.subTest(actor=actor), self.assertRaises(RuntimeErrorBase):
                self.manager.unblock_model(actor)
        with self.store.transaction() as db:
            self.assertEqual(self.store.metadata(db, "model_blocked"), "model_http_402")

    def test_model_transient_retry_is_finite_and_counted(self):
        error = openai.APIStatusError("temporary", response=httpx.Response(429, request=httpx.Request("POST", "https://example.test")), body=None)
        for attempt in range(3):
            result = Worker(self.manager, FixtureAgent([error])).run_once()
            self.assertEqual(result.total_model_requests, attempt + 1)
            self.now += timedelta(seconds=30)
        self.assertEqual(result.status, S.EXHAUSTED)

    def test_total_budget_cannot_be_reset_by_resume(self):
        self.manager.limits.max_resumes = 1
        self.submit("pause")
        self.submit("resume")
        self.submit("pause")
        result = self.submit("resume")
        self.assertEqual(result.processing_status, "rejected")
        self.assertEqual(self.get().resume_count, 1)

    def test_last_available_model_request_can_complete(self):
        self.manager.limits.max_model_requests = 1
        result = Worker(self.manager, FixtureAgent()).run_once()
        self.assertEqual(result.status, S.COMPLETED)

    def test_saved_final_at_total_budget_limit_can_resume(self):
        self.manager.limits.max_model_requests = 1
        def boundary(phase, session_id):
            if phase == "model_checkpoint_committed":
                self.submit("pause")
        Worker(self.manager, FixtureAgent(), boundary).run_once()
        self.assertEqual(self.submit("resume").processing_status, "applied")
        agent = FixtureAgent()
        result = Worker(self.manager, agent).run_once()
        self.assertEqual(result.status, S.COMPLETED)
        self.assertEqual(result.total_model_requests, 1)
        agent.request_durable_response.assert_not_called()

    def test_saved_final_after_crash_publishes_without_new_request(self):
        self.manager.limits.max_model_requests = 1
        class SimulatedExit(BaseException):
            pass
        def boundary(phase, session_id):
            if phase == "model_checkpoint_committed":
                raise SimulatedExit()
        with self.assertRaises(SimulatedExit):
            Worker(self.manager, FixtureAgent(), boundary).run_once()
        self.now += timedelta(seconds=121)
        agent = FixtureAgent()
        result = Worker(self.manager, agent).run_once()
        self.assertEqual(result.status, S.COMPLETED)
        agent.request_durable_response.assert_not_called()

    def test_failed_session_not_recreated_to_bypass_budget(self):
        # A classified FAILED may retry in a new Session. Its requests still
        # belong to the same Incident grant and cannot reset the two-call limit.
        self.manager.limits.max_model_requests = 2
        failed = Worker(self.manager, FixtureAgent([RuntimeErrorBase("model_empty_answer")])).run_once()
        self.assertEqual(failed.status, S.FAILED)
        self.now += timedelta(seconds=61)
        incident = self.manager.incident_store.get_incident(self.session.incident_id)
        incident.alerts[-1].observed_at = self.now
        self.manager.incident_store.save_observation(incident.fingerprint, incident)
        next_session = self.manager.enqueue(incident, True)
        self.assertNotEqual(next_session.session_id, self.session.session_id)
        result = Worker(self.manager, FixtureAgent([RuntimeErrorBase("model_empty_answer")])).run_once()
        self.assertEqual(result.status, S.EXHAUSTED)
        self.now += timedelta(seconds=600)
        self.assertEqual(self.manager.enqueue(incident, True).session_id, result.session_id)
        sessions = self.store.list_sessions(incident_id=incident.incident_id)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sum(s.total_model_requests for s in sessions), 2)
        self.assertEqual({s.budget_group_id for s in sessions}, {None})

    def test_alert_cleared_cancels_pending_task(self):
        self.manager.incident_store.resolve_inactive_incidents(set())
        self.manager.recover()
        self.assertEqual(self.get().status, S.CANCELLED)

    def test_json_failure_replayed_without_duplicate_record(self):
        original = self.manager.incident_store.apply_runtime_projection
        def fail_after_json(session, checkpoint):
            outcome = original(session, checkpoint)
            if session.status == S.COMPLETED:
                raise OSError("crash after JSON commit before SQLite ack")
            return outcome
        with patch.object(self.manager.incident_store, "apply_runtime_projection", side_effect=fail_after_json):
            Worker(self.manager, FixtureAgent()).run_once()
        self.assertEqual(self.get().status, S.COMPLETED)
        self.assertFalse(self.store.load_checkpoint(self.get()).get("projected", False))
        self.now += timedelta(seconds=6)
        self.manager.recover()
        self.assertEqual(len(self.manager.incident_store.get_incident(self.session.incident_id).investigations), 1)

    def test_incident_saved_without_session_is_reconciled(self):
        from models.incident import Incident, Alert
        orphan = Incident(incident_id="orphan", fingerprint="orphan-fp", service="inventory-service",
                          severity="warning", status="open", alerts=[Alert(service="inventory-service",
                          severity="warning", metric="http_5xx_rate", value=0.5, threshold=0.2)])
        self.manager.incident_store.save_incident(orphan.fingerprint, orphan)
        self.manager.recover()
        sessions = [s for s in self.store.list_sessions() if s.incident_id == "orphan"]
        self.assertEqual(len(sessions), 1)

    def test_pause_answer_stays_paused_until_resume(self):
        question = call("request_human_input", {"question": "部署背景？"})
        Worker(self.manager, FixtureAgent([response(answer="", calls=[question])])).run_once()
        request = self.manager.requests()[0]
        self.submit("pause")
        self.submit("answer", {"request_id": request.request_id, "text": "已补充"})
        self.assertEqual(self.get().status, S.PAUSED)
        self.submit("resume")
        self.assertEqual(Worker(self.manager, FixtureAgent()).run_once().status, S.COMPLETED)

    def test_duplicate_answer_does_not_consume_twice(self):
        question = call("request_human_input", {"question": "部署背景？"})
        Worker(self.manager, FixtureAgent([response(answer="", calls=[question])])).run_once()
        request = self.manager.requests()[0]
        payload = {"request_id": request.request_id, "text": "回答线索"}
        first = self.submit("answer", payload, command_id="answer-id")
        self.assertEqual(first, self.submit("answer", payload, command_id="answer-id"))
        self.assertEqual(self.submit("answer", payload).processing_status, "rejected")

    def test_pending_cancel_has_priority_over_resume(self):
        claim = self.store.claim("owned", self.manager.limits)
        self.submit("pause")
        self.submit("resume")
        self.submit("cancel")
        session, _ = self.manager.process_commands(claim.session_id, ("owned", claim.fencing_token))
        self.assertEqual(session.status, S.CANCELLED)
        results = [e["data"]["status"] for e in self.store.events(claim.session_id) if e["event_type"] == "command_processed"]
        self.assertEqual(results, ["applied", "rejected", "rejected"])

    def test_pause_after_final_response_resume_uses_saved_result(self):
        def hook(phase, session_id):
            if phase == "model_checkpoint_committed":
                self.submit("pause")
        first = Worker(self.manager, FixtureAgent(), hook).run_once()
        self.assertEqual(first.status, S.PAUSED)
        self.submit("resume")
        agent = FixtureAgent()
        result = Worker(self.manager, agent).run_once()
        self.assertEqual(result.status, S.COMPLETED)
        self.assertEqual(result.total_model_requests, 1)
        agent.request_durable_response.assert_not_called()

    def test_invalid_tool_and_cross_service_are_rejected(self):
        calls = [call("execute", {"service": "order-service"}, "a"),
                 call("query_metrics", {"service": "inventory-service", "metric": "http_5xx_rate"}, "b")]
        agent = FixtureAgent([response(answer="", calls=calls), response()])
        result = Worker(self.manager, agent).run_once()
        self.assertEqual(result.total_tool_executions, 1)
        self.assertEqual(agent.executions, ["get_running_version"])

    def test_failed_tool_consumes_execution_budget(self):
        agent = FixtureAgent([response(answer="", calls=[call()]), response()],
                             tool_result=lambda item: {"status": "tool_error"})
        result = Worker(self.manager, agent).run_once()
        self.assertEqual(result.total_tool_executions, 2)
        self.assertTrue(any(e["event_type"] == "tool_failed" for e in self.store.events(result.session_id)))

    def test_model_401_and_403_pause(self):
        for status in (401, 403):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                manager = create_runtime(directory)
                create_session(manager)
                error = openai.APIStatusError("blocked", response=httpx.Response(status, request=httpx.Request("POST", "https://example.test")), body=None)
                self.assertEqual(Worker(manager, FixtureAgent([error])).run_once().status, S.PAUSED)

    def test_model_5xx_and_connection_retry(self):
        for error in (
            openai.APIStatusError("temporary", response=httpx.Response(503, request=httpx.Request("POST", "https://example.test")), body=None),
            openai.APIConnectionError(request=httpx.Request("POST", "https://example.test")),
        ):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                manager = create_runtime(directory)
                create_session(manager)
                result = Worker(manager, FixtureAgent([error])).run_once()
                self.assertEqual(result.status, S.WAITING_RETRY)
                self.assertEqual(result.total_model_requests, 1)

    def test_human_request_session_limit(self):
        self.manager.limits.max_human_requests = 1
        question = call("request_human_input", {"question": "第一次？"})
        Worker(self.manager, FixtureAgent([response(answer="", calls=[question])])).run_once()
        request = self.manager.requests()[0]
        self.submit("answer", {"request_id": request.request_id, "text": "线索"})
        result = Worker(self.manager, FixtureAgent([response(answer="", calls=[question])])).run_once()
        self.assertEqual(result.status, S.FAILED)
        self.assertEqual(result.human_request_count, 1)

    def test_stale_monitor_cannot_erase_runtime_evidence(self):
        stale = self.manager.incident_store.get_incident(self.session.incident_id)
        Worker(self.manager, FixtureAgent()).run_once()
        self.manager.incident_store.save_observation(stale.fingerprint, stale)
        current = self.manager.incident_store.get_incident(stale.incident_id)
        self.assertEqual(len(current.investigations), 1)
        self.assertTrue(current.evidence)

    def test_priority_and_bounded_starvation(self):
        warning = create_session(self.manager, service="inventory-service", severity="warning")
        critical = self.store.claim("critical", self.manager.limits)
        self.assertEqual(critical.session_id, self.session.session_id)
        self.now += timedelta(seconds=121)
        self.manager.recover()
        selected = self.store.claim("aged", self.manager.limits)
        self.assertEqual(selected.session_id, warning.session_id)

    def test_checkpoint_retention_bounded(self):
        result = Worker(self.manager, FixtureAgent([response(answer="", calls=[call()]), response()])).run_once()
        with self.store.transaction() as db:
            count = db.execute("SELECT count(*) FROM investigation_checkpoints WHERE session_id=?", (result.session_id,)).fetchone()[0]
        self.assertLessEqual(count, self.manager.limits.checkpoints_to_keep)

    def test_cli_repeated_command(self):
        args = ["--store", str(self.store.path), "--incidents", str(self.root / "incidents.json"),
                "--remediations", str(self.root / "remediation.json"),
                "pause", self.session.session_id, "--actor", "local", "--command-id", "cli-id"]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli(args), 0)
            self.assertEqual(cli(args), 0)

    def test_cli_missing_session_reports_error(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli(["--store", str(self.store.path), "--incidents", str(self.root / "incidents.json"),
                                  "--remediations", str(self.root / "remediation.json"),
                                  "show-session", "missing"]), 2)

    def test_no_execution_or_approval_tool_exposed(self):
        from tools.tool_registry import tool_registry
        self.assertNotIn("disable_demo_regression", tool_registry)
        self.assertFalse(any("execute" in key or "approve" in key for key in tool_registry))
        self.submit("message", {"text": "批准执行修复"})
        Worker(self.manager, FixtureAgent()).run_once()
        self.assertEqual(self.manager.workflow(self.session.session_id)["remediations"], [])

    def _remediation_agent(self):
        from runtime_support import remediation_agent
        return remediation_agent(lambda: self.now)

    def test_runtime_creates_independent_pending_remediation(self):
        from remediation.planner import RemediationPlanner
        from remediation.store import RemediationStore
        self.manager.remediation_planner = RemediationPlanner(RemediationStore(self.root / "remediation.json"))
        result = Worker(self.manager, self._remediation_agent()).run_once()
        proposals = self.manager.remediation_planner.store.list("pending")
        self.assertEqual(result.status, S.COMPLETED)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(self.manager.remediation_planner.store.load()["actions"], [])
        self.manager.recover()
        self.assertEqual(len(self.manager.remediation_planner.store.list()), 1)

    def test_expired_dynamic_evidence_cannot_authorize_proposal(self):
        from remediation.planner import RemediationPlanner
        from remediation.store import RemediationStore
        self.manager.remediation_planner = RemediationPlanner(RemediationStore(self.root / "remediation.json"))
        self.manager.limits.max_tool_executions = 4
        agent = self._remediation_agent()
        def boundary(phase, session_id):
            if phase == "model_after_io" and agent.request_durable_response.call_count == 2:
                self.now += timedelta(seconds=31)
        Worker(self.manager, agent, boundary).run_once()
        self.assertEqual(self.manager.remediation_planner.store.list(), [])

    def test_monitor_enqueues_and_does_not_call_model(self):
        from monitor.incident_monitor import IncidentMonitor
        agent = FixtureAgent()
        monitor = IncidentMonitor(Mock(), self.manager.incident_store, 30, self.manager)
        incident = self.manager.incident_store.get_incident(self.session.incident_id)
        monitor.handle_alert(incident.alerts[-1], incident.fingerprint)
        agent.request_durable_response.assert_not_called()
        self.assertEqual(len(self.store.list_sessions()), 1)

    def test_remediation_storage_error_does_not_block_next_session(self):
        from remediation.planner import RemediationPlanner
        from remediation.store import RemediationStore, StoreError
        self.manager.remediation_planner = RemediationPlanner(RemediationStore(self.root / "remediation.json"))
        with patch.object(self.manager.remediation_planner, "propose", side_effect=StoreError("store_busy")):
            first = Worker(self.manager, self._remediation_agent()).run_once()
            second = create_session(self.manager, service="inventory-service")
            result = Worker(self.manager, FixtureAgent()).run_once()
        self.assertEqual(first.status, S.COMPLETED)
        self.assertEqual(result.session_id, second.session_id)
        self.assertEqual(result.status, S.COMPLETED)
        self.assertTrue(self.store.load_checkpoint(first)["remediation_pending"])

    def test_runtime_model_client_disables_nested_sdk_retries(self):
        from model_client.model_client import ModelClient
        sdk = Mock()
        sdk.with_options.return_value.responses.create.return_value = response()
        client = ModelClient(sdk, "fixture")
        client.call_once([{"role": "user", "content": "context"}], "system", [], timeout=15)
        sdk.with_options.assert_called_once_with(max_retries=0, timeout=15)
        sdk.with_options.return_value.responses.create.assert_called_once()

    def test_resuming_with_stale_metrics_requeries(self):
        first = FixtureAgent([response(answer="", calls=[call()])])
        def boundary(phase, session_id):
            if phase == "tool_checkpoint_committed" and "query_metrics" in first.executions:
                self.submit("pause")
        Worker(self.manager, first, boundary).run_once()
        self.now += timedelta(seconds=20)
        self.submit("resume")
        second = FixtureAgent([response(answer="", calls=[call()]), response()])
        result = Worker(self.manager, second).run_once()
        self.assertIn("query_metrics", second.executions)
        self.assertEqual(result.cache_hits, 0)

    def test_fresh_sha_source_survives_restart_without_reexecution(self):
        source = call("get_source_file", {"service": "order-service", "path": "app.py", "ref": SHA})
        first = FixtureAgent([response(answer="", calls=[source])])
        def boundary(phase, session_id):
            if phase == "tool_checkpoint_committed" and "get_source_file" in first.executions:
                self.submit("pause")
        Worker(self.manager, first, boundary).run_once()
        self.now += timedelta(seconds=20)
        self.submit("resume")
        second = FixtureAgent([response(answer="", calls=[source]), response()])
        result = Worker(self.manager, second).run_once()
        self.assertNotIn("get_source_file", second.executions)
        self.assertEqual(result.cache_hits, 1)

    def test_complete_chain_mode_rejects_unfinished_checkpoint(self):
        checkpoint = self.store.load_checkpoint(self.get())
        checkpoint["history"] = [call()]
        worker = Worker(self.manager, FixtureAgent(), provider_verified=True)
        rebuilt = worker._context(self.session.session_id, checkpoint)
        self.assertEqual(rebuilt[0]["role"], "user")
        checkpoint["history"] = [{"role": "user", "content": "verified provider chain"}]
        restored = worker._context(self.session.session_id, checkpoint)
        self.assertEqual(restored[0]["content"], "verified provider chain")


class ContextPolicyTests(unittest.TestCase):
    def test_source_redaction_preserves_ast_variable_references(self):
        import ast
        source = "database_password = os.getenv('PASSWORD')\nconnect(password=database_password)\npassword = 'hardcoded-secret'\n"
        result = protect({"status": "ok", "repository": "configured/repo", "content": source})
        ast.parse(result["content"])
        self.assertIn("password=database_password", result["content"])
        self.assertNotIn("hardcoded-secret", result["content"])
    def test_complete_chain_and_missing_output(self):
        history = [{"role": "user", "content": "incident"}, call(),
                   {"type": "function_call_output", "call_id": "call-fixture", "output": "{}"}]
        self.assertTrue(valid_chain(history))
        self.assertFalse(valid_chain(history[:-1]))

    def test_mismatched_or_duplicate_call_id(self):
        self.assertFalse(valid_chain([call(), call()]))
        self.assertFalse(valid_chain([call(), {"type": "function_call_output", "call_id": "other", "output": "{}"}]))

    def test_unverified_provider_always_rebuilds(self):
        history = [{"role": "user", "content": "old"}]
        result, strategy = restore_context(history, "verified evidence", False)
        self.assertEqual(strategy, "evidence_rebuild")
        self.assertEqual(result[0]["content"], "verified evidence")

    def test_verified_provider_accepts_only_valid_chain(self):
        history = [{"role": "user", "content": "valid"}]
        self.assertEqual(restore_context(history, "rebuilt", True)[1], "complete_chain")
        self.assertEqual(restore_context([call()], "rebuilt", True)[1], "evidence_rebuild")

    def test_sensitive_fields_and_embedded_credentials_redacted(self):
        with patch.dict("os.environ", {"TEST_API_KEY": "abc-secret-key"}):
            result = encode({"password": "private", "detail": "abc-secret-key Bearer credential"})
        self.assertNotIn("private", result)
        self.assertNotIn("abc-secret-key", result)
        self.assertNotIn("Bearer credential", result)

    def test_checkpoint_size_limit(self):
        with self.assertRaises(RuntimeErrorBase):
            encode(["x" * 65000] * 80)

    def test_sha_source_stable_but_branch_dynamic(self):
        self.assertTrue(stable("get_source_file", {"ref": SHA}))
        self.assertTrue(stable("get_commit_diff", {"commit_sha": SHA}))
        self.assertFalse(stable("get_source_file", {"ref": "main"}))

    def test_dynamic_ttl_no_data_and_instance_change(self):
        now = datetime.now(timezone.utc)
        record = {"name": "query_metrics", "arguments": {}, "result": {"status": "ok"},
                  "success": True, "collected_at": (now - timedelta(seconds=16)).isoformat(), "identity": {"sha": "old"}}
        self.assertFalse(fresh(record, now))
        record["collected_at"] = now.isoformat()
        self.assertEqual(effective_records([record], {"sha": "new"}, now), [])
        record["result"]["status"] = "no_data"
        self.assertFalse(fresh(record, now))

    def test_cache_keys_isolate_service_and_arguments(self):
        first = cache_key("order-service", "query_metrics", {"metric": "http_5xx_rate"})
        second = cache_key("inventory-service", "query_metrics", {"metric": "http_5xx_rate"})
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, cache_key("order-service", "query_metrics", {"metric": "http_latency_p95_seconds"}))


class ProcessRecoveryTests(unittest.TestCase):
    def test_cli_message_in_separate_process_while_model_waits(self):
        with tempfile.TemporaryDirectory(prefix="runtime-inbox-process-") as directory:
            manager = create_runtime(directory)
            session = create_session(manager)
            child = subprocess.Popen(
                [sys.executable, "-B", str(Path(__file__).with_name("runtime_process_fixture.py")), directory, "wait-model"],
                cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 15
                while not (Path(directory) / "ready").exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue((Path(directory) / "ready").exists())
                self.assertIsNone(manager.store.claim("second-process-owner", manager.limits))
                command = subprocess.run(
                    [sys.executable, "-B", "runtime_cli.py", "--store", str(manager.store.path),
                     "--incidents", str(Path(directory) / "incidents.json"), "send-message", session.session_id,
                     "--actor", "process-operator", "--command-id", "cross-process-message", "--text", "先检查数据库连接情况。"],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15,
                    env=dict(os.environ, PYTHONIOENCODING="cp1252"),
                )
                self.assertEqual(command.returncode, 0, (command.stdout + command.stderr).decode("utf-8", errors="replace"))
                submitted = json.loads(command.stdout.decode("utf-8"))
                self.assertEqual(submitted["payload"]["text"], "先检查数据库连接情况。")
                self.assertEqual(submitted["processing_status"], "pending")
                (Path(directory) / "release").write_text("continue")
                _, errors = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, errors.decode(errors="replace"))
                checkpoint = manager.store.load_checkpoint(manager.store.get(session.session_id))
                self.assertEqual(len(checkpoint["messages"]), 1)
                self.assertEqual(checkpoint["messages"][0]["text"], "先检查数据库连接情况。")
                self.assertFalse(any(r["name"] == "query_metrics" for r in checkpoint["records"]))
            finally:
                if child.poll() is None:
                    (Path(directory) / "release").write_text("continue")
                    child.communicate(timeout=30)

    def test_six_abrupt_exit_boundaries_and_restart(self):
        for phase in ("model_before_io", "model_after_io", "model_checkpoint_committed",
                      "tool_before_io", "tool_after_io", "tool_checkpoint_committed"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(prefix="runtime-crash-") as directory:
                manager = create_runtime(directory)
                session = create_session(manager)
                process = subprocess.run(
                    [sys.executable, "-B", str(Path(__file__).with_name("runtime_process_fixture.py")), directory, phase],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30,
                )
                self.assertEqual(process.returncode, 23, process.stderr.decode(errors="replace"))
                old = manager.store.get(session.session_id)
                checkpoint = manager.store.load_checkpoint(old)
                if phase in {"tool_before_io", "tool_after_io"}:
                    self.assertEqual([r["name"] for r in checkpoint["records"]], ["get_running_version"])
                child_tools = [json.loads(line)["name"] for line in (Path(directory) / "tool-io.jsonl").read_text().splitlines()]
                self.assertEqual(child_tools.count("get_source_file"), 1 if phase in {"tool_after_io", "tool_checkpoint_committed"} else 0)
                later = old.lease_until + timedelta(seconds=1)
                manager.store.clock = lambda: later
                source = call("get_source_file", {"service": "order-service", "path": "app.py", "ref": SHA})
                agent = FixtureAgent([response(answer="", calls=[source]), response()])
                recovered = Worker(manager, agent).run_once()
                self.assertEqual(recovered.status, S.COMPLETED)
                self.assertEqual(recovered.total_model_requests, old.total_model_requests + 2)
                self.assertEqual(agent.executions.count("get_source_file"), 0 if phase == "tool_checkpoint_committed" else 1)
                self.assertEqual(recovered.total_tool_executions, old.total_tool_executions + (1 if phase == "tool_checkpoint_committed" else 2))
                checkpoint = manager.store.load_checkpoint(recovered)
                observations = [r for r in checkpoint["records"] if r["name"] == "get_source_file"]
                self.assertEqual(len(observations), 1)
                self.assertEqual(observations[0]["result"]["value"], 0.3)
                self.assertEqual(checkpoint["result"]["answer"], "证据不足，尚未确认根因。")
                context = agent.request_durable_response.call_args_list[-1].args[0][0]["content"]
                self.assertIn(observations[0]["observation_id"], context)


if __name__ == "__main__":
    unittest.main()
