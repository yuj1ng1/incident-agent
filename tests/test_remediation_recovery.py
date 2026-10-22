"""Recovery, real Windows file sharing, and loopback observations in isolated fixtures."""
from contextlib import redirect_stdout
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import requests

from models.remediation import ActionResult, ExecutionOutcome as O, RemediationStatus as S, VerificationResult, utc_now
from remediation.approval import approval_digest
from remediation.demo_action import DemoRuntime
from remediation.executor import ActionExecutor
from remediation.state import RemediationError, transition
from remediation.store import FileLock, MAX_MANUAL_RETRIES, RemediationStore, StoreError
from remediation.verification import DemoClient, DemoVerifier
from remediation_cli import main as cli
from test_remediation import SHA, START, fake_runtime, proposal, version


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="remediation-recovery-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = RemediationStore(self.root / "remediation.json")
        self.p = self.store.create(proposal())
        self.runtime = fake_runtime()
        self.client = Mock()
        self.client.get.side_effect = lambda endpoint: (200, version(False)) if endpoint == "/version" else (
            200, {"service": "order-service", "dependency": "order-db", "order": {"id": 1}},
        )
        self.verifier = DemoVerifier(self.client)
        self.executor = ActionExecutor(self.store, self.runtime, self.verifier)
        locking = patch("remediation.executor.SERVICE_LOCK", self.root / "service.lock")
        locking.start()
        self.addCleanup(locking.stop)

    def claim(self):
        approved = self.store.approve(self.p.proposal_id, "approver")
        with self.store.transaction() as data:
            executing = transition(approved, S.EXECUTING, executed_at=utc_now())
            self.store.put(data, executing)
            self.store.event(data, executing, "execution_started", "executor")

    def test_unknown_execution_closes_without_inventing_action_or_success(self):
        self.claim()
        result = self.executor.close_execution(self.p.proposal_id, "operator", "结果无法确认，人工接管", "close-1")
        closed = self.store.get(self.p.proposal_id)
        self.assertEqual(closed.status, S.CLOSED)
        self.assertEqual(closed.closure.execution_outcome, O.UNKNOWN)
        self.assertTrue(result["current_observation"]["success"])
        self.assertEqual(result["current_observation"]["purpose"], "manual_check")
        self.assertEqual(closed.closure.actor, "operator")
        self.assertEqual(closed.closure.reason, "结果无法确认，人工接管")
        self.assertIsNotNone(closed.closure.closed_at.tzinfo)
        data = self.store.load()
        self.assertEqual(data["actions"], [])
        self.assertEqual(data["verifications"][0]["verification_id"], closed.closure.verification_id)
        self.assertEqual(data["audit"][-1]["event"], "manual_execution_closed")
        self.assertEqual(data["audit"][-1]["details"]["execution_outcome"], "unknown")
        self.runtime.stop.assert_not_called()
        with self.assertRaisesRegex(RemediationError, "not_approved:closed"):
            self.executor.execute(self.p.proposal_id, "operator")
        with self.assertRaises(RemediationError):
            self.store.approve(self.p.proposal_id, "operator")
        with self.assertRaises(RemediationError):
            self.executor.retry_proposal(self.p.proposal_id, "operator", "不能重放")
        self.runtime.stop.assert_not_called()
        self.assertIn(closed, self.store.attention())

    def test_duplicate_close_has_no_new_observation_or_action_and_conflicts_are_audited(self):
        self.claim()
        first = self.executor.close_execution(self.p.proposal_id, "operator", "unknown", "close-1")
        second = self.executor.close_execution(self.p.proposal_id, "operator", "unknown", "close-1")
        self.assertEqual(first["proposal"], second["proposal"])
        self.assertTrue(second["already_closed"])
        self.assertFalse(second["verification_performed"])
        self.assertEqual(self.client.get.call_count, 3)
        self.assertEqual(len(self.store.load()["verifications"]), 1)
        with self.assertRaisesRegex(RemediationError, "closure_command_conflict"):
            self.executor.close_execution(self.p.proposal_id, "other", "unknown", "close-1")
        self.assertEqual(self.store.load()["audit"][-1]["event"], "manual_closure_denied")
        self.assertEqual(self.client.get.call_count, 3)

    def test_inspect_is_fresh_read_only_and_failure_preserves_unknown(self):
        self.claim()
        first = self.executor.inspect_execution(self.p.proposal_id, "operator")
        self.client.get.side_effect = lambda endpoint: (None, None)
        second = self.executor.inspect_execution(self.p.proposal_id, "operator")
        self.assertTrue(first["current_observation"]["success"])
        self.assertFalse(second["current_observation"]["success"])
        self.assertNotEqual(first["current_observation"]["verification_id"], second["current_observation"]["verification_id"])
        self.assertEqual(second["execution_outcome"], "unknown")
        self.assertEqual(self.client.get.call_count, 6)
        self.assertEqual(self.store.get(self.p.proposal_id).status, S.EXECUTING)
        self.assertEqual(self.store.load()["actions"], [])

    def test_stale_or_wrong_proposal_verifier_result_is_not_accepted(self):
        self.claim()
        for identifier in (self.p.proposal_id, "other"):
            verifier = Mock()
            verifier.verify.return_value = VerificationResult(
                proposal_id=identifier, verified_at=utc_now() - timedelta(hours=1),
                success=True, checks={"untrusted": True}, summary="stale",
            )
            result = ActionExecutor(self.store, verifier=verifier).inspect_execution(self.p.proposal_id, "operator")
            self.assertFalse(result["current_observation"]["success"])
            self.assertNotIn("untrusted", result["current_observation"]["checks"])
            self.assertEqual(result["execution_outcome"], "unknown")
            verifier.verify.assert_called_once()

    def test_active_executor_lock_prevents_manual_closure(self):
        self.claim()
        with FileLock(self.root / "service.lock"):
            with self.assertRaisesRegex(StoreError, "busy"):
                self.executor.close_execution(self.p.proposal_id, "operator", "unknown", "close-1")
        self.client.get.assert_not_called()
        self.assertEqual(self.store.get(self.p.proposal_id).status, S.EXECUTING)

    def test_action_result_write_failure_leaves_unknown_and_never_replays(self):
        self.store.approve(self.p.proposal_id, "approver")
        original = self.store._save
        writes = []

        def fail_action_result(data):
            writes.append(len(data["actions"]))
            if len(writes) == 2:
                raise StoreError("write_failed")
            original(data)

        with patch.object(self.store, "_save", side_effect=fail_action_result):
            with self.assertRaises(StoreError):
                self.executor.execute(self.p.proposal_id, "executor")
        self.assertEqual(writes, [0, 1])
        self.runtime.stop.assert_called_once()
        self.client.get.assert_not_called()
        self.assertEqual(self.store.load()["actions"], [])
        self.assertEqual(self.store.get(self.p.proposal_id).status, S.EXECUTING)
        self.executor.close_execution(self.p.proposal_id, "operator", "disk failure", "close-1")
        self.assertEqual(self.store.get(self.p.proposal_id).closure.execution_outcome, O.UNKNOWN)
        self.runtime.stop.assert_called_once()

    def test_closure_save_failure_is_atomic_and_retry_does_not_reexecute(self):
        self.claim()
        original = self.store._save

        def fail_closure(data):
            if data["proposals"][self.p.proposal_id]["status"] == "closed":
                raise StoreError("write_failed")
            original(data)

        with patch.object(self.store, "_save", side_effect=fail_closure):
            with self.assertRaises(StoreError):
                self.executor.close_execution(self.p.proposal_id, "operator", "unknown", "close-1")
        self.assertEqual(self.store.get(self.p.proposal_id).status, S.EXECUTING)
        self.assertEqual(self.store.load()["verifications"], [])
        self.executor.close_execution(self.p.proposal_id, "operator", "unknown", "close-1")
        self.assertEqual(self.store.get(self.p.proposal_id).status, S.CLOSED)
        self.runtime.stop.assert_not_called()

    def test_handler_exception_has_safe_log_and_audit_with_unknown_effect(self):
        self.store.approve(self.p.proposal_id, "approver")
        self.runtime.stop.side_effect = RuntimeError("password=must-not-appear")
        with self.assertLogs("remediation.demo_action", level="ERROR") as logs:
            result = self.executor.execute(self.p.proposal_id, "executor")
        self.assertEqual(result.execution_outcome, O.UNKNOWN)
        data = self.store.load()
        event = next(e for e in data["audit"] if e["event"] == "handler_exception")
        self.assertEqual(event["details"]["exception_type"], "RuntimeError")
        self.assertNotIn("must-not-appear", json.dumps(data) + str(logs.output))
        self.assertEqual(self.store.get(self.p.proposal_id).status, S.FAILED)
        with self.assertRaisesRegex(RemediationError, "outcome_not_confirmed"):
            self.executor.retry_proposal(self.p.proposal_id, "operator", "no automatic retry")

    def test_executor_catches_escaping_handler_exception_with_safe_audit(self):
        self.store.approve(self.p.proposal_id, "approver")
        handler = Mock(side_effect=RuntimeError("credential=must-not-appear"))
        with patch("remediation.executor.ACTION_REGISTRY", {"disable_demo_regression": handler}), \
                self.assertLogs("remediation.executor", level="ERROR") as logs:
            result = self.executor.execute(self.p.proposal_id, "executor")
        handler.assert_called_once()
        self.assertEqual(result.status, "handler_failed")
        self.assertEqual(result.execution_outcome, O.UNKNOWN)
        data = self.store.load()
        self.assertTrue(any(e["event"] == "handler_exception" for e in data["audit"]))
        self.assertNotIn("must-not-appear", json.dumps(data) + str(logs.output))
        self.client.get.assert_not_called()

    def test_rejected_retry_is_pending_bounded_idempotent_and_requires_new_approval(self):
        self.store.reject(self.p.proposal_id, "approver", "initial decision")
        latest = self.p
        for number in range(MAX_MANUAL_RETRIES):
            new = self.executor.retry_proposal(latest.proposal_id, "operator", "new decision")
            repeated = self.executor.retry_proposal(latest.proposal_id, "operator", "new decision")
            self.assertEqual(new, repeated)
            self.assertEqual(new.status, S.PENDING)
            self.assertIsNone(new.approved_by)
            self.assertIsNone(new.approval_digest)
            self.assertEqual(self.store.create(proposal()).proposal_id, new.proposal_id)
            with self.assertRaisesRegex(RemediationError, "not_approved:pending"):
                self.executor.execute(new.proposal_id, "operator")
            self.store.reject(new.proposal_id, "approver", "still declined")
            latest = new
        with self.assertRaisesRegex(RemediationError, "manual_retry_limit_reached"):
            self.executor.retry_proposal(latest.proposal_id, "operator", "bounded")
        self.assertEqual(len(self.store.list()), MAX_MANUAL_RETRIES + 1)
        self.runtime.stop.assert_not_called()
        self.assertEqual(self.store.load()["actions"], [])

    def test_confirmed_pre_action_failure_allows_separate_approval_and_unknown_does_not(self):
        self.store.approve(self.p.proposal_id, "approver")
        self.runtime.repository_ready.side_effect = RemediationError("demo_startup_environment_unverified")
        failed = self.executor.execute(self.p.proposal_id, "executor")
        self.assertEqual(failed.execution_outcome, O.NOT_EXECUTED)
        new = self.executor.retry_proposal(self.p.proposal_id, "operator", "environment repaired")
        self.assertEqual(new.status, S.PENDING)
        self.runtime.stop.assert_not_called()
        self.store.approve(new.proposal_id, "new-approver")
        self.runtime.repository_ready.side_effect = None
        result = self.executor.execute(new.proposal_id, "executor")
        self.assertEqual(result.execution_outcome, O.EXECUTED)
        self.runtime.stop.assert_called_once()
        self.assertEqual(self.store.get(self.p.proposal_id).status, S.FAILED)

    def test_new_process_for_same_incident_is_not_permanently_deduped(self):
        self.store.reject(self.p.proposal_id, "operator", "old process")
        new = self.store.create(proposal(expected_process_started_at=START + timedelta(minutes=5)))
        self.assertNotEqual(new.proposal_id, self.p.proposal_id)
        self.assertEqual(self.store.create(proposal(expected_process_started_at=START + timedelta(minutes=5))), new)

    def test_legacy_v1_reads_and_approval_digest_survive_atomic_migration(self):
        approved = self.store.approve(self.p.proposal_id, "approver")
        data = self.store.load()
        data["version"] = 1
        for record in data["proposals"].values():
            record.pop("retry_of")
            record.pop("closure")
        before = json.dumps(data).encode()
        self.store.file_path.write_bytes(before)  # Legacy fixture only, never operator recovery.
        loaded = RemediationStore(self.store.file_path).get(approved.proposal_id)
        self.assertEqual(loaded.approval_digest, approval_digest(loaded))
        self.assertEqual(self.store.file_path.read_bytes(), before)
        self.executor.execute(loaded.proposal_id, "executor")
        self.assertEqual(self.store.load()["version"], 2)
        self.assertEqual(self.store.get(loaded.proposal_id).status, S.SUCCEEDED)

    def test_list_and_close_cli_show_executing_and_unknown_closed_records(self):
        self.claim()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli(["--store", str(self.store.file_path), "list"]), 0)
        self.assertEqual(json.loads(output.getvalue())[0]["status"], "executing")
        with patch("remediation.executor.DemoVerifier", return_value=self.verifier):
            for repeat in range(2):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cli(["--store", str(self.store.file_path), "close-execution", self.p.proposal_id,
                                          "--by", "operator", "--reason", "unknown", "--command-id", "close-1"]), 0)
                self.assertEqual(json.loads(output.getvalue())["already_closed"], bool(repeat))
        self.assertEqual(self.client.get.call_count, 3)

    def test_cli_show_uses_one_snapshot_when_execution_starts_after_read(self):
        original = RemediationStore.load
        reached = []

        def start_after_read(store):
            snapshot = original(store)
            if not reached:
                reached.append(True)
                self.claim()
            return snapshot

        with patch.object(RemediationStore, "load", start_after_read), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli(["--store", str(self.store.file_path), "show", self.p.proposal_id]), 0)
        self.assertEqual(reached, [True])
        snapshot = json.loads(output.getvalue())
        self.assertEqual(snapshot["proposal"]["status"], "pending")
        self.assertEqual(snapshot["execution_outcome"], "not_executed")
        self.assertEqual(self.store.get(self.p.proposal_id).status, S.EXECUTING)


class ProcessRecoveryTests(unittest.TestCase):
    fixture = Path(__file__).with_name("remediation_process_fixture.py")

    def test_cli_chinese_output_is_utf8_on_legacy_windows_console(self):
        with tempfile.TemporaryDirectory(prefix="remediation-cli-encoding-") as directory:
            store = RemediationStore(Path(directory) / "remediation.json")
            p = store.create(proposal())
            command = subprocess.run(
                [sys.executable, "-B", "remediation_cli.py", "--store", str(store.file_path),
                 "reject", p.proposal_id, "--by", "测试操作者", "--reason", "需补充可信证据"],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15,
                env=dict(os.environ, PYTHONIOENCODING="cp1252"),
            )
            self.assertEqual(command.returncode, 0, (command.stdout + command.stderr).decode("utf-8", errors="replace"))
            output = json.loads(command.stdout.decode("utf-8"))
            self.assertEqual(output["status"], "rejected")
            self.assertEqual(output["rejected_by"], "测试操作者")
            self.assertEqual(output["rejection_reason"], "需补充可信证据")
            self.assertEqual(store.get(p.proposal_id).rejection_reason, output["rejection_reason"])
            self.assertEqual(store.load()["actions"], [])

    def test_real_crashes_at_three_boundaries_never_replay_actions(self):
        for phase in ("executing_committed", "action_returned", "action_committed"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(prefix="remediation-child-") as directory:
                root = Path(directory)
                child = subprocess.run([sys.executable, "-B", str(self.fixture), directory, phase],
                                       capture_output=True, text=True, timeout=20)
                self.assertEqual(child.returncode, 73, child.stderr)
                self.assertEqual((root / "callback-reached").read_text(), phase)
                self.assertEqual((root / "test-effect").exists(), phase != "executing_committed")
                store = RemediationStore(root / "remediation.json")
                p = store.list()[0]
                self.assertEqual(p.status, S.EXECUTING)
                before = store.load()
                self.assertEqual(len(before["actions"]), int(phase == "action_committed"))
                self.assertEqual(before["verifications"], [])
                client = Mock()
                client.get.side_effect = lambda endpoint: (200, {
                    "service": p.service, "commit_sha": p.expected_commit_sha,
                    "runtime_flags": {"simulate_code_regression": False},
                }) if endpoint == "/version" else (200, None)
                runtime = Mock()
                with patch("remediation.executor.SERVICE_LOCK", root / "service.lock"):
                    executor = ActionExecutor(store, runtime, DemoVerifier(client))
                    with self.assertRaisesRegex(RemediationError, "not_approved:executing"):
                        executor.execute(p.proposal_id, "operator")
                    executor.close_execution(p.proposal_id, "operator", "process crash", "close-crash")
                    executor.close_execution(p.proposal_id, "operator", "process crash", "close-crash")
                    with self.assertRaisesRegex(RemediationError, "not_approved:closed"):
                        executor.execute(p.proposal_id, "operator")
                saved = store.load()
                self.assertEqual(saved["actions"], before["actions"])
                self.assertEqual(client.get.call_count, 3)
                self.assertEqual(store.get(p.proposal_id).closure.execution_outcome,
                                 O.EXECUTED if phase == "action_committed" else O.UNKNOWN)
                runtime.stop.assert_not_called()
                runtime.start.assert_not_called()

    def wait_ready(self, path, child):
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.02)
        self.assertTrue(path.exists(), "test child did not reach the real file operation")

    def finish_child(self, child):
        if child.poll() is None:
            child.terminate()  # Only the exact subprocess created by this test.
        child.communicate(timeout=5)

    def test_real_cross_process_reader_blocks_writer_then_releases_handle(self):
        with tempfile.TemporaryDirectory(prefix="remediation-lock-") as directory:
            root = Path(directory)
            store = RemediationStore(root / "remediation.json")
            p = store.create(proposal())
            child = subprocess.Popen([sys.executable, "-B", str(self.fixture), directory, "hold-read"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.wait_ready(root / "reader-ready", child)
                with self.assertRaisesRegex(StoreError, "busy"):
                    store.approve(p.proposal_id, "operator")
                stdout, stderr = child.communicate(input="release\n", timeout=10)
                self.assertEqual(child.returncode, 0, stderr)
                self.assertIn("reader-completed", stdout)
                self.assertEqual(store.get(p.proposal_id).status, S.PENDING)
                store.approve(p.proposal_id, "operator")
                self.assertEqual(store.get(p.proposal_id).status, S.APPROVED)
            finally:
                self.finish_child(child)

    def test_two_real_writers_cannot_duplicate_approval(self):
        with tempfile.TemporaryDirectory(prefix="remediation-writers-") as directory:
            root = Path(directory)
            store = RemediationStore(root / "remediation.json")
            p = store.create(proposal())
            children = [subprocess.Popen([sys.executable, "-B", str(self.fixture), directory, "approve", str(i), p.proposal_id],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                        for i in range(2)]
            try:
                for i, child in enumerate(children):
                    self.wait_ready(root / ("writer-ready-" + str(i)), child)
                for child in children:
                    child.stdin.write("approve\n")
                    child.stdin.flush()
                for child in children:
                    child.communicate(timeout=10)
                self.assertEqual(sorted(child.returncode for child in children), [0, 3])
                self.assertEqual(store.get(p.proposal_id).status, S.APPROVED)
                self.assertEqual(len([e for e in store.load()["audit"] if e["event"] == "approved"]), 1)
            finally:
                for child in children:
                    self.finish_child(child)


class EnvironmentTests(unittest.TestCase):
    def test_missing_venv_dependencies_fail_before_service_stop(self):
        responses = [Mock(stdout=SHA), Mock(stdout=""), subprocess.CalledProcessError(1, "isolated-import", stderr="secret")]
        with patch("remediation.demo_action.subprocess.run", side_effect=responses) as run, \
                patch("pathlib.Path.is_file", return_value=True):
            with self.assertRaisesRegex(RemediationError, "startup_environment_unverified"):
                DemoRuntime().repository_ready(SHA)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args.args[0][1:3], ["-I", "-c"])

    def test_real_loopback_http_ignores_environment_proxy(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.path)
                body = json.dumps(version(False)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:" + str(server.server_port)
            proxies = {"HTTP_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": "",
                       "http_proxy": "http://127.0.0.1:1", "all_proxy": "http://127.0.0.1:1", "no_proxy": ""}
            with patch.dict(os.environ, proxies), patch("remediation.verification.DEMO_URL", url):
                with self.assertRaises(requests.RequestException):
                    requests.get(url + "/version", timeout=0.5)
                self.assertEqual(DemoClient().version()["commit_sha"], SHA)
            self.assertEqual(received, ["/version"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
