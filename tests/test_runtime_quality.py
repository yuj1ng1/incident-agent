"""Quality regressions through the durable Runtime; no model/network charges."""
from contextlib import closing, contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent.agent import Agent
from config import MAX_AGENT_LOOPS, MAX_TOOL_CALLS
from investigation.checkpoint import valid_chain
from investigation.evidence_policy import TTL, cache_key, effective_records, fresh, stable
from investigation.models import HumanCommand, SessionStatus as S
from investigation.state import AttemptToolBudgetExhausted, LostClaim, RuntimeErrorBase
from investigation.worker import Worker
from runtime_support import FixtureAgent, SHA, call, create_runtime, create_session, response
from tools.tool_registry import tool_registry
from investigation.scheduler import Scheduler
from runtime_cli import main as cli


class RuntimeQualityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="runtime-quality-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = datetime.now(timezone.utc)
        self.manager = create_runtime(self.root, clock=lambda: self.now)
        self.store = self.manager.store
        self.session = create_session(self.manager)

    def saved(self):
        session = self.store.get(self.session.session_id)
        return session, self.store.load_checkpoint(session)

    def seed_budget(self, tools=0, rounds=0):
        with self.store.transaction() as db:
            session = self.store.get(self.session.session_id, db)
            session.attempt_count = 1
            session.current_attempt_tool_calls = session.total_tool_executions = tools
            session.current_attempt_rounds = session.total_model_requests = rounds
            self.store.save(session, db)

    def tool_item(self, **kwargs):
        return SimpleNamespace(**call(**kwargs))

    def test_two_remaining_three_calls_keep_outputs_and_complete_without_retry(self):
        self.seed_budget(tools=10)  # Probe consumes one, leaving two of thirteen.
        calls = [call(call_id="metric"), call("query_logs", {"service": "order-service"}, "logs"),
                 call("check_service_health", {"service": "order-service"}, "health")]
        agent = FixtureAgent([response(answer="", calls=calls), response(answer="保留阶段性结论")])
        result = Worker(self.manager, agent).run_once()
        self.assertEqual(result.status, S.COMPLETED)
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(result.total_tool_executions, 13)
        self.assertEqual(agent.executions, ["get_running_version", "query_metrics", "query_logs"])
        second_context = json.dumps(agent.request_durable_response.call_args_list[1].args[0], ensure_ascii=False)
        for identifier in ("metric", "logs", "health"):
            self.assertIn(identifier, second_context)
        self.assertIn("tool_limit_reached", second_context)
        self.assertIn("attempt_tool_budget_exhausted", second_context)
        _, checkpoint = self.saved()
        self.assertEqual(len(checkpoint["records"]), 3)
        self.assertEqual(checkpoint["result"]["answer"], "保留阶段性结论")
        self.assertFalse(any(e["event_type"] == "waiting_retry" for e in self.store.events(result.session_id)))

    def test_complete_chain_pairs_every_denied_call(self):
        self.seed_budget(tools=11)
        calls = [call(call_id="one"), call("query_logs", {"service": "order-service"}, "two"),
                 call("check_service_health", {"service": "order-service"}, "three")]
        agent = FixtureAgent([response(answer="", calls=calls), response()])
        Worker(self.manager, agent, provider_verified=True).run_once()
        history = agent.request_durable_response.call_args_list[1].args[0]
        self.assertTrue(valid_chain(history))
        outputs = {i["call_id"]: json.loads(i["output"]) for i in history if i.get("type") == "function_call_output"}
        self.assertEqual(set(outputs), {"one", "two", "three"})
        self.assertEqual(outputs["one"]["status"], "ok")
        self.assertEqual(outputs["two"]["status"], "tool_limit_reached")
        self.assertEqual(outputs["three"]["status"], "tool_limit_reached")

    def test_rejected_malformed_call_can_be_rebuilt_without_losing_result_pair(self):
        invalid = call(call_id="invalid-json")
        invalid["arguments"] = "{"
        agent = FixtureAgent([response(answer="", calls=[invalid]), response(answer="根据已有观测继续回答")])
        result = Worker(self.manager, agent).run_once()
        self.assertEqual(result.status, S.COMPLETED)
        self.assertEqual(agent.executions, ["get_running_version"])
        context = agent.request_durable_response.call_args_list[1].args[0][0]["content"]
        self.assertIn("invalid-json", context)
        self.assertIn("invalid_or_unallowed_tool_call", context)

    def test_repeated_arguments_keep_each_calls_original_observation_and_freshness(self):
        for age in (0, 16):
            with self.subTest(age=age), tempfile.TemporaryDirectory() as directory:
                manager = create_runtime(directory, clock=lambda: self.now)
                create_session(manager)
                agent = FixtureAgent([response(answer="", calls=[call(call_id="older"), call(call_id="newer")]), response()])
                def age_first(phase, session_id):
                    if phase == "tool_checkpoint_committed" and len(agent.executions) == 2:
                        self.now += timedelta(seconds=age)
                result = Worker(manager, agent, age_first).run_once()
                context = agent.request_durable_response.call_args_list[1].args[0][0]["content"]
                rebuilt = json.loads(context.rsplit("\n", 1)[1])
                pairs = {i["call_id"]: i for i in rebuilt["recent_function_call_results"]}
                records = {r["call_id"]: r for r in manager.store.load_checkpoint(result)["records"]}
                newer = records["newer"] if age else records["older"]
                self.assertEqual(pairs["older"]["observation_id"], records["older"]["observation_id"])
                self.assertEqual(pairs["newer"]["observation_id"], newer["observation_id"])
                self.assertEqual(pairs["older"]["use_for_current_state"], age == 0)
                self.assertTrue(pairs["newer"]["use_for_current_state"])
                self.assertEqual(result.total_tool_executions, 3 if age else 2)

    def test_real_thirteen_boundary_prompt_and_reservations(self):
        for failed_probe in (False, True):
            with self.subTest(failed_probe=failed_probe), tempfile.TemporaryDirectory() as directory:
                manager = create_runtime(directory, clock=lambda: self.now)
                create_session(manager)
                # Distinct args prevent cache hits; parameter validation still runs.
                calls = [call(arguments={"service": "order-service", "metric": f"metric-{n}"}, call_id=f"c-{n}") for n in range(13)]
                agent = FixtureAgent([response(answer="", calls=calls), response()])
                if failed_probe:
                    agent.tool_result = lambda item: {"status": "no_data", "reason": "version_timeout"} if item.name == "get_running_version" else {"status": "ok", "value": 1}
                result = Worker(manager, agent).run_once()
                self.assertEqual(result.status, S.COMPLETED)
                self.assertEqual(result.total_tool_executions, 13)
                self.assertEqual(agent.executions.count("query_metrics"), 12)
                self.assertEqual(agent.request_durable_response.call_args_list[0].args[2], 1)
                self.assertEqual(agent.request_durable_response.call_args_list[1].args[2], 13)
                self.assertEqual(len([e for e in manager.store.events(result.session_id) if e["event_type"] == "tool_reserved"]), 13)

    def test_real_agent_prompt_reports_twelve_two_and_zero_remaining(self):
        agent = Agent(Mock(), "只读")
        agent.model_client.call_once = Mock(return_value=response())
        for consumed, remaining in ((1, 12), (11, 2), (13, 0)):
            with self.subTest(consumed=consumed):
                agent.request_durable_response([], 1, consumed, [], 1)
                request = agent.model_client.call_once.call_args
                self.assertIn(f"剩余工具执行次数 {remaining}；", request.args[1])
                self.assertEqual(bool(request.args[2]), remaining > 0)
        self.assertEqual(agent.model_client.call_once.call_count, 3)

    def test_thirteenth_reservation_succeeds_and_fourteenth_does_not_mutate_budget(self):
        self.seed_budget(tools=12)
        claimed = self.store.claim("boundary-test", self.manager.limits)
        checkpoint = self.store.load_checkpoint(claimed)
        claim = ("boundary-test", claimed.fencing_token)
        worker = Worker(self.manager, FixtureAgent())
        reserved = worker._reserve(claimed.session_id, claim, checkpoint, "tool")
        self.assertEqual(reserved.current_attempt_tool_calls, 13)
        self.assertEqual(reserved.total_tool_executions, 13)
        with self.assertRaises(AttemptToolBudgetExhausted):
            worker._reserve(claimed.session_id, claim, checkpoint, "tool")
        saved = self.store.get(claimed.session_id)
        self.assertEqual(saved.current_attempt_tool_calls, 13)
        self.assertEqual(saved.total_tool_executions, 13)
        self.assertEqual(len([e for e in self.store.events(saved.session_id) if e["event_type"] == "tool_reserved"]), 1)

    def test_real_agent_prompt_and_thirteen_distinct_calls_share_same_budget(self):
        fixture = FixtureAgent()
        agent = Agent(Mock(), "只读")
        agent.tool_manager.run_tool = fixture.run_fixture_tool
        calls = [call(arguments={"service": "order-service", "metric": f"metric-{i}"}, call_id=f"call-{i}") for i in range(13)]
        agent.model_client.call_once = Mock(side_effect=[response(answer="", calls=calls), response()])
        result = Worker(self.manager, agent, provider_verified=True).run_once()
        requests = agent.model_client.call_once.call_args_list
        self.assertEqual(len(requests), 2)
        self.assertIn("剩余工具执行次数 12；", requests[0].args[1])
        self.assertIn("剩余工具执行次数 0；", requests[1].args[1])
        self.assertEqual(requests[1].args[2], [])
        self.assertEqual(result.total_tool_executions, 13)
        self.assertEqual(fixture.executions.count("query_metrics"), 12)
        outputs = {item["call_id"]: json.loads(item["output"]) for item in requests[1].args[0]
                   if item.get("type") == "function_call_output"}
        self.assertEqual(len(outputs), 13)
        self.assertEqual(outputs["call-12"]["status"], "tool_limit_reached")

    def test_session_tool_budget_and_lost_claim_are_not_soft_limits(self):
        self.manager.limits.max_tool_executions = 1
        result = Worker(self.manager, FixtureAgent([response(answer="", calls=[call()])])).run_once()
        self.assertEqual(result.status, S.EXHAUSTED)
        self.assertEqual(result.total_tool_executions, 1)
        worker = Worker(self.manager, FixtureAgent())
        with self.assertRaises(LostClaim):
            worker._tool(result.session_id, ("stale", 0), self.store.load_checkpoint(result), self.tool_item())

    def test_last_attempt_model_round_does_not_start_another_attempt(self):
        self.seed_budget(rounds=MAX_AGENT_LOOPS - 1)
        agent = FixtureAgent([response(answer="", calls=[call()])])
        result = Worker(self.manager, agent).run_once()
        self.assertEqual(result.status, S.EXHAUSTED)
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(result.current_attempt_rounds, MAX_AGENT_LOOPS)
        self.assertEqual(agent.request_durable_response.call_count, 1)

    def test_every_registered_tool_has_explicit_policy_and_unknown_is_never_fresh(self):
        self.assertEqual(set(tool_registry), set(TTL))
        record = {"name": "unknown", "arguments": {}, "result": {"status": "ok"},
                  "success": True, "collected_at": self.now.isoformat()}
        self.assertFalse(fresh(record, self.now))
        for name in ("get_source_file", "get_commit_diff"):
            field = "ref" if name == "get_source_file" else "commit_sha"
            for ref in ("main", SHA[:7], SHA):
                with self.subTest(name=name, ref=ref):
                    self.assertEqual(stable(name, {field: ref}), ref == SHA)
        with patch.dict(TTL, {name: ttl for name, ttl in TTL.items() if name != "check_scrape_target"}, clear=True):
            with self.assertRaisesRegex(RuntimeErrorBase, "tool_evidence_policy_missing"):
                Worker(self.manager, FixtureAgent())

    def test_wrong_running_sha_is_reference_not_current_source(self):
        record = {"name": "get_source_file", "arguments": {"ref": "b" * 40},
                  "result": {"status": "ok"}, "success": True,
                  "collected_at": self.now.isoformat(), "identity": {"commit_sha": SHA}}
        self.assertTrue(fresh(record, self.now))
        self.assertEqual(effective_records([record], {"commit_sha": SHA}, self.now), [])

    def test_dependency_service_configuration_change_invalidates_current_record(self):
        from services.registry import registry, ServiceRegistry
        arguments = {"service": "order-db", "metric": "postgres_up"}
        identity = {"commit_sha": SHA}
        record = {"name": "query_metrics", "arguments": arguments, "result": {"status": "ok"},
                  "success": True, "collected_at": self.now.isoformat(), "identity": identity,
                  "cache_key": cache_key("order-service", "query_metrics", arguments, identity)}
        self.assertEqual(effective_records([record], identity, self.now, "order-service"), [record])
        definitions = [registry.get(name).model_dump(mode="json") for name in registry.names()]
        next(p for p in definitions if p["name"] == "order-db")["metrics"]["job"] = "new-postgres-job"
        with patch("investigation.evidence_policy.registry", ServiceRegistry(definitions)):
            self.assertTrue(fresh(record, self.now))
            self.assertEqual(effective_records([record], identity, self.now, "order-service"), [])

    def test_duration_boundary_rejects_even_a_cached_tool_without_execution(self):
        self.manager.limits.max_duration_seconds = 1
        self.now = self.store.get(self.session.session_id).created_at
        worker = Worker(self.manager, FixtureAgent())
        session = self.store.claim(worker.owner, self.manager.limits)
        claim = (worker.owner, session.fencing_token)
        checkpoint = self.store.load_checkpoint(session)
        item = self.tool_item(name="query_metrics", arguments={"service": "order-service", "metric": "http_5xx_rate"})
        worker._tool(session.session_id, claim, checkpoint, item)
        self.now += timedelta(seconds=1)
        with self.assertRaisesRegex(RuntimeErrorBase, "session_duration_exhausted"):
            worker._tool(session.session_id, claim, checkpoint, item)
        self.assertEqual(worker.agent.executions, ["query_metrics"])
        self.assertEqual(self.store.get(session.session_id).total_tool_executions, 1)

    def test_expired_observation_keeps_fields_and_failure_keeps_reason(self):
        agent = FixtureAgent([response(answer="", calls=[call("check_dependency_health", {"dependency": "order-db"}, "db")]), response()])
        agent.tool_result = lambda item: {"status": "no_data", "reason": "commit_unknown"} if item.name == "get_running_version" else {"status": "ok", "dependency": "order-db", "reachable": True}
        def boundary(phase, session_id):
            if phase == "tool_checkpoint_committed" and len(agent.executions) == 2:
                self.now += timedelta(seconds=16)
        result = Worker(self.manager, agent, boundary).run_once()
        context = agent.request_durable_response.call_args_list[1].args[0][0]["content"]
        self.assertIn('"reachable": true', context)
        self.assertIn('"use_for_current_state": false', context)
        self.assertIn("commit_unknown", context)
        self.assertEqual(len(self.store.load_checkpoint(result)["records"]), 2)

    def test_json_projection_write_failure_is_backed_off_and_other_session_runs(self):
        first = Worker(self.manager, FixtureAgent()).run_once()
        with self.store.transaction() as db:
            session = self.store.get(first.session_id, db)
            checkpoint = self.store.load_checkpoint(session, db)
            checkpoint["projected"] = False
            self.store.checkpoint(session, checkpoint, db)
            self.store.save(session, db)
        second = create_session(self.manager, service="inventory-service")
        original = self.manager.incident_store.apply_runtime_projection
        failures = []
        def failing_projection(session, checkpoint):
            if session.session_id == first.session_id:
                failures.append(session.session_id)
                raise PermissionError("isolated projection write denied")
            return original(session, checkpoint)
        with patch.object(self.manager.incident_store, "apply_runtime_projection", side_effect=failing_projection):
            result = Worker(self.manager, FixtureAgent()).run_once()
            self.assertEqual(result.session_id, second.session_id)
            self.assertEqual(result.status, S.COMPLETED)
            self.manager.recover()
            self.assertEqual(len(failures), 1)
        _, checkpoint = self.saved()
        self.assertFalse(checkpoint["projected"])
        self.assertIsNotNone(checkpoint["projection_retry_at"])
        self.now += timedelta(seconds=301)
        self.manager.recover()
        self.assertTrue(self.saved()[1]["projected"])

    def test_store_corruption_is_not_isolated_as_projection_failure(self):
        with patch.object(self.store, "list_sessions", side_effect=sqlite3.DatabaseError("corrupt")):
            with self.assertRaises(sqlite3.DatabaseError):
                Worker(self.manager, FixtureAgent()).run_once()

    def test_projection_does_not_acknowledge_a_concurrently_saved_checkpoint(self):
        completed = Worker(self.manager, FixtureAgent()).run_once()
        with self.store.transaction() as db:
            session = self.store.get(completed.session_id, db)
            checkpoint = self.store.load_checkpoint(session, db)
            checkpoint["projected"] = False
            self.store.checkpoint(session, checkpoint, db)
            self.store.save(session, db)
        project = self.manager.incident_store.apply_runtime_projection
        def concurrent_checkpoint(session, checkpoint):
            outcome = project(session, checkpoint)
            with self.store.transaction() as db:
                newer = self.store.get(session.session_id, db)
                payload = self.store.load_checkpoint(newer, db)
                payload["messages"].append({"text": "并发保存的线索"})
                self.store.checkpoint(newer, payload, db)
                self.store.save(newer, db)
            return outcome
        with patch.object(self.manager.incident_store, "apply_runtime_projection", side_effect=concurrent_checkpoint):
            self.manager.sync_incident(completed.session_id)
        _, checkpoint = self.saved()
        self.assertFalse(checkpoint["projected"])
        self.assertEqual(checkpoint["messages"][0]["text"], "并发保存的线索")
        self.manager.sync_incident(completed.session_id)
        self.assertTrue(self.saved()[1]["projected"])

    def test_recover_and_commands_reuse_one_transaction_connection(self):
        original = self.store.transaction
        depth = 0
        @contextmanager
        def checked_transaction():
            nonlocal depth
            self.assertEqual(depth, 0, "nested SQLite connection")
            depth += 1
            try:
                with original() as db:
                    yield db
            finally:
                depth -= 1
        with patch.object(self.store, "transaction", checked_transaction):
            self.manager.submit(HumanCommand(session_id=self.session.session_id, actor="test", command_type="pause"))
            self.manager.recover()
            self.manager.submit(HumanCommand(session_id=self.session.session_id, actor="test", command_type="resume"))
            Worker(self.manager, FixtureAgent()).run_once()
            self.manager.workflow(self.session.session_id)

    def test_prompt_uses_minimum_of_attempt_and_session_budget(self):
        self.manager.limits.max_tool_executions = 3
        self.manager.limits.max_model_requests = 2
        agent = Agent(Mock(), "只读")
        fixture = FixtureAgent()
        agent.tool_manager.run_tool = fixture.run_fixture_tool
        agent.model_client.call_once = Mock(side_effect=[response(answer="", calls=[call()]), response()])
        result = Worker(self.manager, agent).run_once()
        self.assertEqual(result.status, S.COMPLETED)
        requests = agent.model_client.call_once.call_args_list
        self.assertIn("剩余工具执行次数 2", requests[0].args[1])
        self.assertIn("剩余模型响应轮数（含本轮） 2", requests[0].args[1])
        self.assertIn("剩余工具执行次数 1", requests[1].args[1])
        self.assertIn("剩余模型响应轮数（含本轮） 1", requests[1].args[1])
        self.assertEqual(requests[1].args[2], [])

    def test_resume_probe_uses_existing_attempt_and_total_counters(self):
        self.seed_budget(tools=5, rounds=2)
        for kind in ("pause", "resume"):
            self.manager.submit(HumanCommand(session_id=self.session.session_id, actor="test", command_type=kind))
        agent = FixtureAgent()
        result = Worker(self.manager, agent).run_once()
        self.assertEqual(result.total_tool_executions, 6)
        self.assertEqual(result.total_model_requests, 3)
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(agent.request_durable_response.call_args.args[2], 6)

    def test_proposal_refresh_failure_or_budget_race_preserves_answer(self):
        for failure in ("no_data", "duration", "tools"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                manager = create_runtime(directory, clock=lambda: self.now)
                session = create_session(manager)
                planner = Mock()
                planner.plan.return_value = object()
                manager.remediation_planner = planner
                agent = FixtureAgent([response(answer="已经可靠保存的回答")])
                worker = Worker(manager, agent)
                original = worker._tool
                def refresh(*args, **kwargs):
                    if args[2].get("result"):
                        if failure == "no_data":
                            agent.tool_result = lambda item: {"status": "no_data", "reason": "version_timeout"}
                        elif failure == "duration":
                            # Less than lease duration, beyond duration; fencing remains valid.
                            manager.limits.max_duration_seconds = 1
                            self.now += timedelta(seconds=2)
                        else:
                            manager.limits.max_tool_executions = 1
                    return original(*args, **kwargs)
                worker._tool = refresh
                result = worker.run_once()
                checkpoint = manager.store.load_checkpoint(result)
                self.assertEqual(result.status, S.COMPLETED)
                self.assertEqual(checkpoint["result"]["answer"], "已经可靠保存的回答")
                self.assertIsNotNone(checkpoint["proposal_evidence_gap"])
                self.assertEqual(result.attempt_count, 1)
                self.assertEqual(result.total_tool_executions, 2 if failure == "no_data" else 1)
                planner.propose.assert_not_called()
                self.assertEqual(manager.incident_store.get_incident(session.incident_id).consecutive_investigation_failures, 0)

    def test_proposal_refresh_does_not_catch_lost_claim(self):
        planner = Mock()
        planner.plan.return_value = object()
        self.manager.remediation_planner = planner
        agent = FixtureAgent()
        worker = Worker(self.manager, agent)
        original = worker._tool
        def expired(*args, **kwargs):
            if args[2].get("result"):
                self.now += timedelta(seconds=121)
            return original(*args, **kwargs)
        worker._tool = expired
        result = worker.run_once()
        self.assertEqual(result.status, S.RUNNING)
        self.assertEqual(agent.executions, ["get_running_version"])
        self.assertTrue(self.store.load_checkpoint(result)["result"]["success"])
        planner.propose.assert_not_called()

    def test_final_answer_at_tool_boundary_is_completed_without_proposal(self):
        self.seed_budget(tools=12)
        planner = Mock()
        planner.plan.return_value = object()
        self.manager.remediation_planner = planner
        result = Worker(self.manager, FixtureAgent([response(answer="达到工具上限仍保留回答")])).run_once()
        checkpoint = self.store.load_checkpoint(result)
        self.assertEqual(result.status, S.COMPLETED)
        self.assertEqual(result.total_tool_executions, 13)
        self.assertEqual(checkpoint["result"]["answer"], "达到工具上限仍保留回答")
        self.assertEqual(checkpoint["proposal_evidence_gap"], "proposal_refresh_budget_unavailable")
        planner.propose.assert_not_called()

    def test_terminal_notification_matches_persisted_result_and_survives_restart(self):
        agent = FixtureAgent([response(answer="完整调查回答")])
        scheduler = Scheduler(Worker(self.manager, agent))
        output = io.StringIO()
        with redirect_stdout(output):
            completed = scheduler.tick()
            Scheduler(Worker(self.manager, FixtureAgent())).tick()
        self.assertEqual(output.getvalue().count("[Investigation Completed]"), 1)
        self.assertIn(completed.session_id, output.getvalue())
        self.assertIn("完整调查回答", output.getvalue())
        saved = self.manager.incident_store.get_incident(completed.incident_id)
        self.assertEqual(saved.investigations[0].answer, "完整调查回答")
        self.assertEqual(saved.investigations[0].tool_calls, completed.total_tool_executions)

    def test_display_failure_keeps_persistence_and_can_be_retried(self):
        scheduler = Scheduler(Worker(self.manager, FixtureAgent()))
        with patch("builtins.print", side_effect=OSError("stdout unavailable")):
            result = scheduler.tick()
        self.assertEqual(result.status, S.COMPLETED)
        self.assertEqual(len(self.manager.incident_store.get_incident(result.incident_id).investigations), 1)
        output = io.StringIO()
        with redirect_stdout(output):
            scheduler.tick()
        self.assertIn("[Investigation Completed]", output.getvalue())

    def test_terminal_io_has_no_sqlite_transaction(self):
        scheduler = Scheduler(Worker(self.manager, FixtureAgent()))
        original = self.store.transaction
        depth = 0
        @contextmanager
        def track():
            nonlocal depth
            depth += 1
            try:
                with original() as db:
                    yield db
            finally:
                depth -= 1
        def display(*args, **kwargs):
            self.assertEqual(depth, 0)
            # An independent connection can write while stdout is executing.
            with closing(sqlite3.connect(self.store.path, timeout=0.1)) as db:
                with db:
                    db.execute("UPDATE runtime_metadata SET value=value")
        with patch.object(self.store, "transaction", track), patch("builtins.print", side_effect=display):
            scheduler.tick()

    def test_human_request_prompt_and_cli_complete_result(self):
        question = call("request_human_input", {"question": "最近是否改过数据库配置？"}, "question")
        output = io.StringIO()
        with redirect_stdout(output):
            waiting = Scheduler(Worker(self.manager, FixtureAgent([response(answer="", calls=[question])]))).tick()
        self.assertEqual(waiting.status, S.WAITING_HUMAN)
        self.assertIn("[Human Input Required]", output.getvalue())
        self.assertIn(self.manager.requests()[0].request_id, output.getvalue())
        self.assertIn("最近是否改过数据库配置？", output.getvalue())
        args = ["--store", str(self.store.path), "--incidents", str(self.root / "incidents.json"),
                "--remediations", str(self.root / "remediation.json"), "show-session", waiting.session_id]
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli(args), 0)
        view = json.loads(output.getvalue())
        self.assertEqual(view["human_requests"][0]["status"], "pending")
        self.assertIsNone(view["investigation_result"])
        self.manager.submit(HumanCommand(session_id=waiting.session_id, actor="operator", command_type="answer",
                                        payload={"request_id": self.manager.requests()[0].request_id, "text": "没有修改"}))
        Worker(self.manager, FixtureAgent([response(answer="已经保存的全部报告")])).run_once()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli(args), 0)
        view = json.loads(output.getvalue())
        self.assertEqual(view["investigation_result"]["answer"], "已经保存的全部报告")
        self.assertEqual(view["session"]["total_model_requests"], 2)
        self.assertEqual(view["human_requests"][0]["status"], "consumed")

    def test_auth_block_cancel_requires_explicit_operator_unblock(self):
        import httpx2 as httpx
        import openai
        error = openai.APIStatusError("sensitive", response=httpx.Response(401, request=httpx.Request("POST", "https://example.test")), body=None)
        blocked = Worker(self.manager, FixtureAgent([error])).run_once()
        self.manager.submit(HumanCommand(session_id=blocked.session_id, actor="operator", command_type="cancel"))
        second = create_session(self.manager, service="inventory-service")
        agent = FixtureAgent()
        self.assertIsNone(Worker(self.manager, agent).run_once())
        agent.request_durable_response.assert_not_called()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli(["--store", str(self.store.path), "--incidents", str(self.root / "incidents.json"),
                                  "--remediations", str(self.root / "remediation.json"), "unblock-model", "--actor", "operator"]), 0)
        result = Worker(self.manager, agent).run_once()
        self.assertEqual(result.session_id, second.session_id)
        self.assertEqual(result.status, S.COMPLETED)
        self.assertEqual(self.saved()[0].status, S.CANCELLED)
        self.assertTrue(any(e["event_type"] == "model_unblocked" for e in self.store.events(blocked.session_id)))

    def test_no_data_logging_is_not_success_and_secret_arguments_are_not_logged(self):
        from tools.tool_manager import ToolManager
        item = self.tool_item(arguments={"service": "order-service", "metric": "password=do-not-log"})
        with patch.dict(tool_registry["query_metrics"], {"func": Mock(return_value={"status": "no_data", "reason": "service_unreachable"})}):
            with self.assertLogs("agent", level="INFO") as logs:
                ToolManager().run_tool(item, session_id="ses-test")
        output = "\n".join(logs.output)
        self.assertIn("status=no_data", output)
        self.assertIn("executed=True", output)
        self.assertIn("session=ses-test", output)
        self.assertNotIn("工具调用成功", output)
        self.assertNotIn("do-not-log", output)
        with patch.dict(tool_registry["query_metrics"], {"func": Mock(side_effect=RuntimeError("password=do-not-log"))}):
            with self.assertLogs("agent", level="ERROR") as logs:
                _, _, result = ToolManager().run_tool(item)
        self.assertEqual(result["reason"], "RuntimeError")
        self.assertNotIn("do-not-log", str(result) + "\n".join(logs.output))

    def test_negative_version_cache_is_short_and_never_evidence(self):
        agent = FixtureAgent([response(answer="", calls=[call("get_running_version", {"service": "order-service"}, "version")]), response()],
                             lambda item: {"status": "no_data", "reason": "version_connection_refused"})
        result = Worker(self.manager, agent).run_once()
        self.assertEqual(result.total_tool_executions, 1)
        self.assertEqual(result.cache_hits, 1)
        self.assertEqual(self.manager.incident_store.get_incident(result.incident_id).evidence, [])
        with tempfile.TemporaryDirectory() as directory:
            manager = create_runtime(directory, clock=lambda: self.now)
            create_session(manager)
            again = FixtureAgent([response(answer="", calls=[call("get_running_version", {"service": "order-service"}, "version")]), response()],
                                 lambda item: {"status": "no_data", "reason": "version_timeout"})
            def advance(phase, session_id):
                if phase == "model_after_io" and again.request_durable_response.call_count == 1:
                    self.now += timedelta(seconds=5)
            result = Worker(manager, again, advance).run_once()
            self.assertEqual(result.total_tool_executions, 2)
            self.assertEqual(result.cache_hits, 0)

    def test_version_cause_classification_keeps_strict_identity_and_sha(self):
        import requests
        from tools.version import get_running_version
        cases = [(requests.Timeout("private"), "version_timeout"),
                 (requests.ConnectionError(ConnectionRefusedError(10061, "private")), "version_connection_refused"),
                 (requests.ConnectionError("private"), "version_connection_error"),
                 (requests.RequestException("private"), "version_request_error"),
                 (ValueError("private"), "invalid_version_response")]
        for error, reason in cases:
            with self.subTest(reason=reason), patch("tools.version.ServiceClient.version", side_effect=error):
                result = get_running_version("order-service")
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["status"], "no_data")
                self.assertNotIn("private", str(result))
        body = {"service": "order-service", "process_started_at": self.now.isoformat(),
                "runtime_flags": {"simulate_code_regression": False, "simulate_slow_request": False}}
        for changes, reason in [({}, "commit_field_missing"), ({"commit_sha": None}, "commit_unknown"),
                                ({"commit_sha": SHA[:7]}, "invalid_commit_sha"),
                                ({"commit_sha": SHA, "service": "another"}, "version_service_identity_unconfirmed")]:
            with self.subTest(reason=reason), patch("tools.version.ServiceClient.version", return_value={"http_status": 200, "body": body | changes}):
                self.assertEqual(get_running_version("order-service")["reason"], reason)


class ProcessQualityTests(unittest.TestCase):
    def test_two_process_claim_race_and_old_process_fencing(self):
        with tempfile.TemporaryDirectory(prefix="runtime-claim-race-") as directory:
            root = Path(directory)
            manager = create_runtime(root)
            session = create_session(manager)
            fixture = Path(__file__).with_name("runtime_process_fixture.py")
            children = [subprocess.Popen([sys.executable, "-B", str(fixture), directory, "claim-contender", owner],
                                        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        for owner in ("one", "two")]
            def wait_files(names):
                deadline = time.monotonic() + 30
                while not all((root / name).exists() for name in names):
                    self.assertLess(time.monotonic(), deadline, "child barrier timed out")
                    time.sleep(0.02)
            try:
                wait_files(["ready-one", "ready-two"])
                (root / "claim-release").write_text("start")
                wait_files(["claim-one.json", "claim-two.json"])
                claims = [json.loads((root / ("claim-" + owner + ".json")).read_text()) for owner in ("one", "two")]
                self.assertEqual(sum(c is not None for c in claims), 1)
                old = manager.store.get(session.session_id)
                later = old.lease_until + timedelta(seconds=1)
                (root / "clock.txt").write_text(later.isoformat())
                manager.store.clock = lambda: later
                manager.recover()
                replacement = manager.store.claim("replacement", manager.limits)
                self.assertGreater(replacement.fencing_token, old.fencing_token)
                (root / "fence-release").write_text("try stale write")
                for child in children:
                    _, errors = child.communicate(timeout=30)
                    self.assertEqual(child.returncode, 0, errors.decode(errors="replace"))
                self.assertEqual((root / "fence-result").read_text(), "old_owner_fenced")
                current = manager.store.get(session.session_id)
                self.assertEqual(current.owner_token, "replacement")
                self.assertEqual(current.fencing_token, replacement.fencing_token)
            finally:
                (root / "clock.txt").write_text(manager.store.clock().isoformat())
                (root / "claim-release").write_text("release")
                (root / "fence-release").write_text("release")
                for child in children:
                    if child.poll() is None:
                        child.communicate(timeout=30)


if __name__ == "__main__":
    unittest.main()
