"""Safety and lifecycle tests. Never stop a process or call an external service."""
from contextlib import redirect_stdout
from datetime import timedelta
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from models.incident import Evidence, Incident, Alert
from models.investigation import InvestigationResult, InvestigationStatus
from models.remediation import (
    ActionResult, RemediationProposal, RemediationStatus as S, Risk, VerificationResult, utc_now,
)
from remediation.approval import approve_proposal, reject_proposal
from remediation.demo_action import DemoRuntime, disable_demo_regression
from remediation.executor import ACTION_REGISTRY, ActionExecutor
from remediation.planner import RemediationPlanner
from remediation.state import RemediationError, transition
from remediation.store import FileLock, RemediationStore, StoreError
from remediation.verification import DemoClient, DemoVerifier
from remediation.windows_process import belongs_to_demo
from remediation_cli import main as cli

SHA = "a" * 40
START = utc_now() - timedelta(minutes=2)
ROOT = Path(r"D:\prometheus-demo")


def proposal(**changes):
    data = dict(incident_id="inc-test", service="order-service", summary="Disable regression",
        reason="Confirmed evidence", action_type="disable_demo_regression",
        parameters={"service": "order-service"}, risk="low", expected_commit_sha=SHA,
        expected_process_started_at=START)
    return RemediationProposal(**(data | changes))


def executing(**changes):
    return transition(approve_proposal(proposal(**changes), "actual-operator"), S.EXECUTING)


def version(flag=True, sha=SHA):
    return {"service": "order-service", "commit_sha": sha, "process_started_at": START.isoformat(),
            "runtime_flags": {"simulate_code_regression": flag}}


def action(p, success=True, status="action_completed"):
    return ActionResult(proposal_id=p.proposal_id, action_type=p.action_type,
                        started_at=utc_now(), success=success, status=status, summary="bounded")


def fake_runtime(flag=True):
    runtime = Mock()
    runtime.client.version.return_value = version(flag)
    runtime.listener.return_value = {"pid": 123}
    runtime.start.return_value = version(False)
    return runtime


class ModelAndStateTests(unittest.TestCase):
    def test_default_pending(self):
        self.assertEqual(proposal().status, S.PENDING)

    def test_required_fields(self):
        for field in ("incident_id", "service", "summary", "reason", "action_type", "risk", "expected_commit_sha", "expected_process_started_at"):
            data = proposal().model_dump()
            del data[field]
            with self.subTest(field=field), self.assertRaises(ValidationError):
                RemediationProposal.model_validate(data)

    def test_risk_enum(self):
        for risk in Risk:
            self.assertEqual(proposal(risk=risk).risk, risk)
        with self.assertRaises(ValidationError):
            proposal(risk="automatically_safe")

    def test_dates_are_utc(self):
        p = approve_proposal(proposal(), "human")
        for field in ("created_at", "approved_at", "expected_process_started_at"):
            self.assertEqual(getattr(p, field).utcoffset(), timedelta(0))
            self.assertTrue(p.model_dump(mode="json")[field].endswith("Z"))

    def test_naive_timestamp_rejected(self):
        with self.assertRaises(ValidationError):
            proposal(created_at=START.replace(tzinfo=None))

    def test_pending_approved(self):
        self.assertEqual(approve_proposal(proposal(), "human").status, S.APPROVED)

    def test_pending_rejected(self):
        self.assertEqual(reject_proposal(proposal(), "human", "no").status, S.REJECTED)

    def test_approved_executing(self):
        self.assertEqual(executing().status, S.EXECUTING)

    def test_executing_succeeded(self):
        self.assertEqual(transition(executing(), S.SUCCEEDED).status, S.SUCCEEDED)

    def test_executing_failed(self):
        self.assertEqual(transition(executing(), S.FAILED).status, S.FAILED)

    def test_executing_verification_failed(self):
        self.assertEqual(transition(executing(), S.VERIFICATION_FAILED).status, S.VERIFICATION_FAILED)

    def test_illegal_transitions(self):
        for source in S:
            for target in S:
                allowed = {S.PENDING: {S.APPROVED, S.REJECTED}, S.APPROVED: {S.EXECUTING},
                    S.EXECUTING: {S.SUCCEEDED, S.FAILED, S.VERIFICATION_FAILED, S.CLOSED}}
                if target not in allowed.get(source, set()):
                    with self.subTest(source=source, target=target), self.assertRaises(RemediationError):
                        transition(proposal(status=source), target)

    def test_repeat_approve_rejected(self):
        with self.assertRaises(RemediationError):
            approve_proposal(approve_proposal(proposal(), "first"), "second")

    def test_actual_identity_recorded(self):
        self.assertEqual(approve_proposal(proposal(), "zp1222").approved_by, "zp1222")

    def test_blank_identity_rejected(self):
        for actor in ("", " ", "model\nactor", None):
            with self.subTest(actor=actor), self.assertRaises(RemediationError):
                approve_proposal(proposal(), actor)

    def test_rejection_identity_and_reason(self):
        p = reject_proposal(proposal(), "human", "keep investigating")
        self.assertEqual(p.rejected_by, "human")
        self.assertEqual(p.rejection_reason, "keep investigating")
        self.assertIsNone(p.approved_by)

    def test_rejection_reason_required(self):
        with self.assertRaises(RemediationError):
            reject_proposal(proposal(), "human", "")

    def test_model_cannot_assign_status(self):
        p = proposal()
        with self.assertRaises(ValidationError):
            p.status = S.APPROVED

    def test_extra_fields_rejected(self):
        with self.assertRaises(ValidationError):
            proposal(command="powershell arbitrary")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "remediation.json"
        self.store = RemediationStore(self.path)
        self.p = self.store.create(proposal())

    def test_proposal_roundtrip(self):
        self.assertEqual(self.store.get(self.p.proposal_id), self.p)

    def test_creation_audit(self):
        self.assertEqual(self.store.load()["audit"][0]["event"], "proposal_created")

    def test_approve_roundtrip_audit(self):
        self.store.approve(self.p.proposal_id, "human")
        self.assertEqual(self.store.get(self.p.proposal_id).approved_by, "human")
        self.assertEqual(self.store.load()["audit"][-1]["event"], "approved")

    def test_reject_roundtrip_audit(self):
        self.store.reject(self.p.proposal_id, "human", "no")
        self.assertEqual(self.store.get(self.p.proposal_id).rejection_reason, "no")
        self.assertEqual(self.store.load()["audit"][-1]["actor"], "human")

    def test_results_roundtrip(self):
        a = action(self.p)
        v = VerificationResult(proposal_id=self.p.proposal_id, success=True, checks={}, summary="ok")
        with self.store.transaction() as data:
            data["actions"].append(a.model_dump(mode="json"))
            data["verifications"].append(v.model_dump(mode="json"))
        data = RemediationStore(self.path).load()
        self.assertEqual(ActionResult.model_validate(data["actions"][0]), a)
        self.assertEqual(VerificationResult.model_validate(data["verifications"][0]), v)

    def test_corrupt_json_fails_closed_unchanged(self):
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(StoreError, "corrupt"):
            self.store.approve(self.p.proposal_id, "human")
        self.assertEqual(self.path.read_text(), "{broken")

    def test_wrong_schema_fails_closed(self):
        self.path.write_text('{"version":1}', encoding="utf-8")
        with self.assertRaises(StoreError):
            self.store.load()

    def test_invalid_nested_proposal_fails_closed(self):
        data = self.store.load()
        data["proposals"][self.p.proposal_id]["status"] = "auto_approved"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(StoreError):
            self.store.load()

    def test_atomic_replace_called(self):
        import os
        with patch("remediation.store.os.replace", wraps=os.replace) as replace:
            self.store.approve(self.p.proposal_id, "human")
        source, target = replace.call_args.args
        self.assertEqual(source.parent, target.parent)
        self.assertEqual(target, self.path)
        self.assertFalse(source.exists())

    def test_failed_replace_preserves_previous_json(self):
        before = self.path.read_bytes()
        with patch("remediation.store.os.replace", side_effect=OSError("sensitive output")):
            with self.assertRaisesRegex(StoreError, "write_failed"):
                self.store.approve(self.p.proposal_id, "human")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_os_lock_blocks_second_writer(self):
        with FileLock(str(self.path) + ".lock"):
            with self.assertRaisesRegex(StoreError, "busy"):
                self.store.approve(self.p.proposal_id, "human")

    def test_duplicate_incident_version_deduped(self):
        duplicate = self.store.create(proposal())
        self.assertEqual(duplicate.proposal_id, self.p.proposal_id)
        self.assertEqual(len(self.store.load()["audit"]), 1)

    def test_no_preapproved_creation(self):
        with self.assertRaises(StoreError):
            self.store.create(approve_proposal(proposal(incident_id="other"), "human"))


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        StoreTests.setUp(self)
        self.addCleanup(patch.stopall)
        patch("remediation.executor.SERVICE_LOCK", Path(self.temp.name) / "service.lock").start()
        self.runtime = fake_runtime()
        self.verifier = Mock()
        self.verifier.verify.return_value = VerificationResult(proposal_id=self.p.proposal_id,
            success=True, checks={"test": {"success": True}}, summary="verified")
        self.executor = ActionExecutor(self.store, self.runtime, self.verifier)

    def test_pending_cannot_execute(self):
        with self.assertRaisesRegex(RemediationError, "not_approved:pending"):
            self.executor.execute(self.p.proposal_id, "human")
        self.runtime.stop.assert_not_called()
        self.assertEqual(self.store.load()["audit"][-1]["event"], "execution_denied")

    def test_rejected_cannot_execute(self):
        self.store.reject(self.p.proposal_id, "human", "no")
        with self.assertRaisesRegex(RemediationError, "not_approved:rejected"):
            self.executor.execute(self.p.proposal_id, "human")
        self.runtime.stop.assert_not_called()

    def test_unknown_action_rejected(self):
        other = self.store.create(proposal(incident_id="other", action_type="run_shell"))
        self.store.approve(other.proposal_id, "human")
        with self.assertRaisesRegex(RemediationError, "not_allowed"):
            self.executor.execute(other.proposal_id, "human")
        self.runtime.stop.assert_not_called()

    def test_arbitrary_command_parameter_rejected(self):
        other = self.store.create(proposal(incident_id="other", parameters={"service": "order-service", "command": "bad"}))
        self.store.approve(other.proposal_id, "human")
        with self.assertRaisesRegex(RemediationError, "invalid_action_parameters"):
            self.executor.execute(other.proposal_id, "human")
        self.runtime.stop.assert_not_called()

    def test_other_service_rejected(self):
        other = self.store.create(proposal(incident_id="other", service="database"))
        self.store.approve(other.proposal_id, "human")
        with self.assertRaisesRegex(RemediationError, "service_not_allowed"):
            self.executor.execute(other.proposal_id, "human")

    def test_approved_allowlisted_action_and_verification(self):
        self.store.approve(self.p.proposal_id, "human")
        result = self.executor.execute(self.p.proposal_id, "human")
        self.assertTrue(result.success)
        self.runtime.stop.assert_called_once()
        self.verifier.verify.assert_called_once()
        data = self.store.load()
        self.assertEqual(self.store.get(self.p.proposal_id).status, S.SUCCEEDED)
        self.assertEqual(len(data["actions"]), 1)
        self.assertEqual(len(data["verifications"]), 1)
        self.assertEqual([e["event"] for e in data["audit"]], ["proposal_created", "approved",
            "execution_started", "action_succeeded", "verification_succeeded", "remediation_succeeded"])

    def test_verification_failure_not_succeeded(self):
        self.store.approve(self.p.proposal_id, "human")
        self.verifier.verify.return_value = VerificationResult(proposal_id=self.p.proposal_id,
            success=False, checks={"order": {"success": False}}, summary="failed")
        result = self.executor.execute(self.p.proposal_id, "human")
        self.assertTrue(result.success)  # physical action != recovery
        self.assertEqual(self.store.get(self.p.proposal_id).status, S.VERIFICATION_FAILED)

    def test_verifier_exception_not_succeeded(self):
        self.store.approve(self.p.proposal_id, "human")
        self.verifier.verify.side_effect = RuntimeError("secret")
        self.executor.execute(self.p.proposal_id, "human")
        self.assertEqual(self.store.get(self.p.proposal_id).status, S.VERIFICATION_FAILED)
        self.assertNotIn("secret", self.path.read_text())

    def test_action_failure_no_verification(self):
        self.store.approve(self.p.proposal_id, "human")
        self.runtime.stop.side_effect = RemediationError("demo_stop_failed")
        result = self.executor.execute(self.p.proposal_id, "human")
        self.assertFalse(result.success)
        self.verifier.verify.assert_not_called()
        self.assertEqual(self.store.get(self.p.proposal_id).status, S.FAILED)

    def test_succeeded_not_reexecuted(self):
        self.store.approve(self.p.proposal_id, "human")
        self.executor.execute(self.p.proposal_id, "human")
        again = self.executor.execute(self.p.proposal_id, "human")
        self.assertEqual(again.status, "already_completed")
        self.assertFalse(again.success)
        self.assertFalse(again.details["verification_performed"])
        self.runtime.stop.assert_called_once()
        self.verifier.verify.assert_called_once()

    def test_repeat_approve_no_execution(self):
        self.store.approve(self.p.proposal_id, "human")
        with self.assertRaises(RemediationError):
            self.store.approve(self.p.proposal_id, "human")
        self.runtime.stop.assert_not_called()

    def test_approval_digest_detects_changed_plan(self):
        self.store.approve(self.p.proposal_id, "human")
        with self.store.transaction() as data:
            data["proposals"][self.p.proposal_id]["expected_commit_sha"] = "b" * 40
        with self.assertRaisesRegex(RemediationError, "proposal_changed"):
            self.executor.execute(self.p.proposal_id, "human")
        self.runtime.stop.assert_not_called()

    def test_forged_status_without_approval_rejected(self):
        with self.store.transaction() as data:
            data["proposals"][self.p.proposal_id]["status"] = "approved"
        with self.assertRaisesRegex(RemediationError, "approval_unconfirmed"):
            self.executor.execute(self.p.proposal_id, "human")

    def test_executing_after_crash_not_reexecuted(self):
        approved = self.store.approve(self.p.proposal_id, "human")
        with self.store.transaction() as data:
            self.store.put(data, transition(approved, S.EXECUTING))
        with self.assertRaisesRegex(RemediationError, "not_approved:executing"):
            self.executor.execute(self.p.proposal_id, "human")
        self.runtime.stop.assert_not_called()

    def test_durable_claim_write_failure_prevents_side_effect(self):
        self.store.approve(self.p.proposal_id, "human")
        with patch.object(self.store, "_save", side_effect=StoreError("write_failed")):
            with self.assertRaises(StoreError):
                self.executor.execute(self.p.proposal_id, "human")
        self.runtime.stop.assert_not_called()

    def test_another_execution_holds_service_lock(self):
        self.store.approve(self.p.proposal_id, "human")
        with FileLock(Path(self.temp.name) / "service.lock"):
            with self.assertRaises(StoreError):
                self.executor.execute(self.p.proposal_id, "human")
        self.runtime.stop.assert_not_called()

    def test_cli_list_and_show(self):
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli(["--store", str(self.path), "list"]), 0)
        self.assertEqual(json.loads(output.getvalue())[0]["status"], "pending")
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli(["--store", str(self.path), "show", self.p.proposal_id]), 0)
        self.assertEqual(json.loads(output.getvalue())["proposal"]["proposal_id"], self.p.proposal_id)

    def test_cli_approve_has_no_action(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli(["--store", str(self.path), "approve", self.p.proposal_id, "--by", "human"]), 0)
        self.assertEqual(self.store.get(self.p.proposal_id).status, S.APPROVED)
        self.runtime.stop.assert_not_called()

    def test_cli_reject_execute_denied(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli(["--store", str(self.path), "reject", self.p.proposal_id,
                                  "--by", "human", "--reason", "no"]), 0)
            self.assertEqual(cli(["--store", str(self.path), "execute", self.p.proposal_id]), 2)
        self.runtime.stop.assert_not_called()


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.p = executing()
        self.runtime = fake_runtime()

    def test_true_flag_restarts(self):
        result = disable_demo_regression(self.p, self.runtime)
        self.assertTrue(result.success)
        self.runtime.stop.assert_called_once()
        self.runtime.start.assert_called_once()

    def test_false_flag_already_remediated(self):
        self.runtime.client.version.return_value = version(False)
        result = disable_demo_regression(self.p, self.runtime)
        self.assertEqual(result.status, "already_remediated")
        self.runtime.stop.assert_not_called()
        self.runtime.start.assert_not_called()

    def test_changed_commit_even_with_false_flag_refused(self):
        self.runtime.client.version.return_value = version(False, "b" * 40)
        result = disable_demo_regression(self.p, self.runtime)
        self.assertEqual(result.status, "runtime_changed_since_proposal")
        self.runtime.stop.assert_not_called()

    def test_changed_process_refused(self):
        self.runtime.client.version.return_value = version() | {"process_started_at": utc_now().isoformat()}
        self.assertEqual(disable_demo_regression(self.p, self.runtime).status, "runtime_changed_since_proposal")
        self.runtime.stop.assert_not_called()

    def test_state_changes_during_preflight_refused(self):
        self.runtime.client.version.side_effect = [version(), version(False)]
        self.assertEqual(disable_demo_regression(self.p, self.runtime).status, "runtime_changed_since_proposal")
        self.runtime.stop.assert_not_called()

    def test_unconfirmed_process_never_stopped(self):
        self.runtime.listener.side_effect = RemediationError("process_identity_unconfirmed")
        self.assertEqual(disable_demo_regression(self.p, self.runtime).status, "process_identity_unconfirmed")
        self.runtime.stop.assert_not_called()

    def test_no_listener_never_stopped(self):
        self.runtime.listener.side_effect = RemediationError("demo_not_listening")
        self.assertEqual(disable_demo_regression(self.p, self.runtime).status, "demo_not_listening")
        self.runtime.stop.assert_not_called()

    def test_start_failure_recorded_no_fallback(self):
        self.runtime.start.side_effect = RemediationError("demo_start_failed_manual_intervention_required")
        result = disable_demo_regression(self.p, self.runtime)
        self.assertFalse(result.success)
        self.assertTrue(result.details["old_demo_stopped"])
        self.runtime.start.assert_called_once()

    def test_unexpected_error_not_leaked(self):
        self.runtime.stop.side_effect = RuntimeError("GITHUB_TOKEN=secret-value")
        result = disable_demo_regression(self.p, self.runtime)
        self.assertEqual(result.status, "action_internal_error")
        self.assertNotIn("secret-value", result.model_dump_json())

    def test_direct_pending_handler_refused(self):
        self.assertEqual(disable_demo_regression(proposal(), self.runtime).status, "approval_unconfirmed")
        self.runtime.stop.assert_not_called()

    def test_handler_validates_service_itself(self):
        self.assertEqual(disable_demo_regression(executing(service="other"), self.runtime).status, "service_not_allowed")
        self.runtime.stop.assert_not_called()

    def test_handler_validates_extra_parameters(self):
        for key in ("command", "pid", "python_path", "env", "code"):
            with self.subTest(key=key):
                p = executing(parameters={"service": "order-service", key: "unsafe"})
                self.assertEqual(disable_demo_regression(p, self.runtime).status, "invalid_action_parameters")
        self.runtime.stop.assert_not_called()

    def test_dirty_source_prevents_stop(self):
        self.runtime.repository_ready.side_effect = RemediationError("demo_runtime_code_dirty")
        self.assertEqual(disable_demo_regression(self.p, self.runtime).status, "demo_runtime_code_dirty")
        self.runtime.stop.assert_not_called()


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.identity = {"pid": 12, "creation": 100, "cwd": str(ROOT),
            "exe": r"D:\conda\envs\agentlearn\python.exe",
            "argv": [r"D:\conda\envs\agentlearn\python.exe", "app.py"]}

    def test_correct_demo_identity(self):
        self.assertTrue(belongs_to_demo(self.identity, ROOT))

    def test_other_working_directory_refused(self):
        self.assertFalse(belongs_to_demo(self.identity | {"cwd": r"D:\other"}, ROOT))

    def test_other_script_refused(self):
        other = self.identity | {"argv": [self.identity["exe"], "other.py"]}
        self.assertFalse(belongs_to_demo(other, ROOT))

    def test_python_dash_c_refused(self):
        other = self.identity | {"argv": [self.identity["exe"], "-c", "arbitrary"]}
        self.assertFalse(belongs_to_demo(other, ROOT))

    def test_absolute_other_app_refused(self):
        other = self.identity | {"argv": [self.identity["exe"], r"D:\other\app.py"]}
        self.assertFalse(belongs_to_demo(other, ROOT))

    def test_no_port_listener(self):
        with patch("remediation.demo_action.subprocess.run", return_value=SimpleNamespace(stdout="[]")):
            with self.assertRaisesRegex(RemediationError, "not_listening"):
                DemoRuntime().listener()

    def test_multiple_listeners_refused(self):
        with patch("remediation.demo_action.subprocess.run", return_value=SimpleNamespace(stdout="[1,2]")):
            with self.assertRaisesRegex(RemediationError, "identity_unconfirmed"):
                DemoRuntime().listener()

    def test_port_correct_demo(self):
        handle = Mock()
        handle.__enter__ = Mock(return_value=handle)
        handle.__exit__ = Mock()
        handle.snapshot.return_value = self.identity
        with patch("remediation.demo_action.subprocess.run", return_value=SimpleNamespace(stdout="[12]")), \
                patch("remediation.demo_action.ProcessHandle", return_value=handle):
            self.assertEqual(DemoRuntime().listener(), self.identity)

    def test_port_unrelated_program_refused(self):
        handle = Mock()
        handle.__enter__ = Mock(return_value=handle)
        handle.__exit__ = Mock()
        handle.snapshot.return_value = self.identity | {"cwd": r"D:\other"}
        with patch("remediation.demo_action.subprocess.run", return_value=SimpleNamespace(stdout="[12]")), \
                patch("remediation.demo_action.ProcessHandle", return_value=handle):
            with self.assertRaisesRegex(RemediationError, "identity_unconfirmed"):
                DemoRuntime().listener()

    def test_pid_changes_before_stop_refused(self):
        runtime = DemoRuntime()
        with patch.object(runtime, "listener", return_value=self.identity | {"creation": 101}), \
                patch("remediation.demo_action.ProcessHandle") as handle:
            with self.assertRaises(RemediationError):
                runtime.stop(self.identity)
            handle.assert_not_called()

    def test_clean_repository_same_head(self):
        with patch("remediation.demo_action.subprocess.run", side_effect=[SimpleNamespace(stdout=SHA), SimpleNamespace(stdout=""), SimpleNamespace(returncode=0)]) as run, \
                patch("pathlib.Path.is_file", return_value=True):
            DemoRuntime().repository_ready(SHA)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args.args[0][1:3], ["-I", "-c"])

    def test_local_head_changed_refused(self):
        with patch("remediation.demo_action.subprocess.run", side_effect=[SimpleNamespace(stdout="b" * 40), SimpleNamespace(stdout="")]):
            with self.assertRaisesRegex(RemediationError, "runtime_changed"):
                DemoRuntime().repository_ready(SHA)

    def test_uncommitted_runtime_source_refused(self):
        with patch("remediation.demo_action.subprocess.run", side_effect=[SimpleNamespace(stdout=SHA), SimpleNamespace(stdout=" M app.py")]):
            with self.assertRaisesRegex(RemediationError, "code_dirty"):
                DemoRuntime().repository_ready(SHA)

    def test_start_uses_only_fixed_script_and_no_secrets(self):
        client = Mock()
        client.version.return_value = version(False)
        child = Mock()
        child.poll.return_value = None
        with patch("remediation.demo_action.subprocess.Popen", return_value=child) as launch, \
                patch.dict("os.environ", {"GITHUB_TOKEN": "never-expose", "ORDER_DB_PASSWORD": "never-expose"}):
            DemoRuntime(client).start()
        args, kwargs = launch.call_args
        self.assertEqual(args[0][-2:], ["-File", str(ROOT / "start_demo.ps1")])
        self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
        self.assertNotIn("ORDER_DB_PASSWORD", kwargs["env"])
        self.assertFalse(kwargs.get("shell", False))

    def test_start_exited_requires_human(self):
        child = Mock()
        child.poll.return_value = 1
        with patch("remediation.demo_action.subprocess.Popen", return_value=child):
            with self.assertRaisesRegex(RemediationError, "manual_intervention"):
                DemoRuntime().start()

    def test_start_timeout_bounded_no_retry_launch(self):
        child = Mock()
        child.poll.return_value = None
        with patch("remediation.demo_action.subprocess.Popen", return_value=child) as launch, \
                patch("remediation.demo_action.time.monotonic", side_effect=[0, 13]):
            with self.assertRaisesRegex(RemediationError, "start_timeout"):
                DemoRuntime().start()
        launch.assert_called_once()

    def test_native_handle_creation_change_prevents_termination(self):
        from remediation.windows_process import ProcessHandle
        handle = object.__new__(ProcessHandle)
        handle.kernel = Mock()
        handle.handle = "held-native-handle"
        handle.snapshot = Mock(return_value=self.identity | {"creation": 101})
        with self.assertRaises(RemediationError):
            handle.stop_verified(self.identity, ROOT)
        handle.kernel.TerminateProcess.assert_not_called()

    def test_native_handle_only_terminates_matching_demo(self):
        from remediation.windows_process import ProcessHandle
        handle = object.__new__(ProcessHandle)
        handle.kernel = Mock()
        handle.handle = "held-native-handle"
        handle.snapshot = Mock(return_value=self.identity)
        handle.kernel.WaitForSingleObject.return_value = 0
        handle.stop_verified(self.identity, ROOT)
        handle.kernel.TerminateProcess.assert_called_once_with("held-native-handle", 0)


class RuntimePlannerIntegrationTests(unittest.TestCase):
    def setUp(self):
        from runtime_support import create_runtime, create_session, remediation_agent
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = create_runtime(self.temp.name)
        self.session = create_session(self.manager)
        self.agent = remediation_agent(self.manager.store.clock)
        self.planner = Mock()
        self.planner.plan.return_value = None
        self.planner.propose.return_value = None
        self.manager.remediation_planner = self.planner
        incident = self.manager.incident_store.get_incident(self.session.incident_id)
        incident.evidence.append(Evidence(source="old", summary="old", detail="{}"))
        self.manager.incident_store.save_incident(incident.fingerprint, incident)

    def test_planner_receives_only_current_runtime_evidence(self):
        from investigation.worker import Worker
        result = Worker(self.manager, self.agent).run_once()
        sources = {e.source for e in self.planner.propose.call_args.args[2]}
        self.assertNotIn("old", sources)
        self.assertEqual(sources, {"get_running_version", "query_logs", "get_source_file", "check_dependency_health"})
        self.assertEqual(len(self.manager.incident_store.get_incident(result.incident_id).investigations), 1)

    def test_planner_store_error_keeps_completed_answer_pending_projection(self):
        from investigation.worker import Worker
        self.planner.propose.side_effect = StoreError("store_busy")
        result = Worker(self.manager, self.agent).run_once()
        checkpoint = self.manager.store.load_checkpoint(result)
        self.assertEqual(result.status.value, "COMPLETED")
        self.assertTrue(checkpoint["result"]["success"])
        self.assertTrue(checkpoint["remediation_pending"])
        self.assertEqual(len(self.manager.incident_store.get_incident(result.incident_id).investigations), 1)

    def test_failed_investigation_does_not_plan(self):
        from investigation.worker import Worker
        self.agent.request_durable_response.side_effect = [ValueError("invalid model response")]
        result = Worker(self.manager, self.agent).run_once()
        self.assertEqual(result.status.value, "FAILED")
        self.planner.propose.assert_not_called()


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.responses = [(200, version(False)), (200, None), (200, None)]

    def verify(self):
        self.client.get.side_effect = self.responses
        return DemoVerifier(self.client).verify(proposal())

    def test_three_immediate_checks_pass(self):
        result = self.verify()
        self.assertTrue(result.success)
        self.assertEqual([call.args[0] for call in self.client.get.call_args_list], ["/version", "/order", "/health"])

    def test_flag_still_true_fails(self):
        self.responses[0] = (200, version(True))
        self.assertFalse(self.verify().success)

    def test_order_not_200_fails(self):
        self.responses[1] = (500, None)
        self.assertFalse(self.verify().success)

    def test_health_not_200_fails(self):
        self.responses[2] = (503, None)
        self.assertFalse(self.verify().success)

    def test_changed_commit_fails(self):
        self.responses[0] = (200, version(False, "b" * 40))
        self.assertFalse(self.verify().success)

    def test_missing_version_fails(self):
        self.responses[0] = (None, None)
        self.assertFalse(self.verify().success)

    def test_version_wrong_service_fails(self):
        self.responses[0] = (200, version(False) | {"service": "other"})
        self.assertFalse(self.verify().success)

    def test_version_reader_filters_secrets(self):
        client = DemoClient()
        with patch.object(client, "get", return_value=(200, version() | {"GITHUB_TOKEN": "secret"})):
            self.assertNotIn("secret", json.dumps(client.version()))

    def test_version_reader_bad_boolean_refused(self):
        client = DemoClient()
        with patch.object(client, "get", return_value=(200, version() | {"runtime_flags": {"simulate_code_regression": "true"}})):
            with self.assertRaises(RemediationError):
                client.version()

    def test_unregistered_endpoint_refused(self):
        with self.assertRaises(RemediationError):
            DemoClient().get("/arbitrary")


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.planner = RemediationPlanner(RemediationStore(Path(self.temp.name) / "remediation.json"))
        self.incident = Incident(incident_id="inc-test", service="order-service", severity="critical",
            status="investigating", alerts=[])
        self.result = InvestigationResult(success=True, status=InvestigationStatus.COMPLETED, answer="Diagnosed")
        self.bodies = {
            "get_running_version": {"status": "ok", **version()},
            "query_logs": {"status": "ok", "service": "order-service", "logs": [{
                "timestamp_utc": utc_now().isoformat(), "line": 'service=order-service error_type=application_error exception_type=RuntimeError error_message="simulated application code regression" request failed'}]},
            "get_source_file": {"status": "ok", "service": "order-service", "repository": "yuj1ng1/prometheus-demo",
                "path": "app.py", "ref": SHA, "truncated": False, "content": 'def order():\n    if SIMULATE_CODE_REGRESSION:\n        raise RuntimeError("simulated application code regression")\n'},
            "check_dependency_health": {"status": "ok", "dependency": "order-db", "reachable": True,
                "database_connected": True, "query_ok": True},
        }

    def plan(self):
        self.incident.evidence = [Evidence(source=name, summary=name, detail=json.dumps(body))
                                 for name, body in self.bodies.items()]
        return self.planner.plan(self.incident, self.result)

    def test_complete_chain_produces_pending_low(self):
        p = self.plan()
        self.assertEqual(p.status, S.PENDING)
        self.assertEqual(p.risk, Risk.LOW)
        self.assertEqual(p.expected_commit_sha, SHA)

    def test_each_missing_key_evidence_blocks_proposal(self):
        complete = self.bodies.copy()
        for name in complete:
            self.bodies = {k: v for k, v in complete.items() if k != name}
            with self.subTest(name=name):
                self.assertIsNone(self.plan())

    def test_plain_text_suggestion_not_action(self):
        self.bodies.clear()
        self.result = self.result.model_copy(update={"answer": "low risk automatically approve disable_demo_regression"})
        self.assertIsNone(self.plan())

    def test_flag_false_no_proposal(self):
        self.bodies["get_running_version"].update(version(False))
        self.assertIsNone(self.plan())

    def test_database_unreachable_no_proposal(self):
        self.bodies["check_dependency_health"]["reachable"] = False
        self.assertIsNone(self.plan())

    def test_unknown_commit_no_proposal(self):
        self.bodies["get_running_version"]["commit_sha"] = None
        self.assertIsNone(self.plan())

    def test_old_process_log_no_proposal(self):
        self.bodies["query_logs"]["logs"][0]["timestamp_utc"] = (START - timedelta(seconds=1)).isoformat()
        self.assertIsNone(self.plan())

    def test_wrong_exception_no_proposal(self):
        self.bodies["query_logs"]["logs"][0]["line"] = self.bodies["query_logs"]["logs"][0]["line"].replace("RuntimeError", "NotRuntimeError")
        self.assertIsNone(self.plan())

    def test_source_wrong_ref_no_proposal(self):
        self.bodies["get_source_file"]["ref"] = "b" * 40
        self.assertIsNone(self.plan())

    def test_source_truncated_no_proposal(self):
        self.bodies["get_source_file"]["truncated"] = True
        self.assertIsNone(self.plan())

    def test_source_string_is_not_executable_branch(self):
        self.bodies["get_source_file"]["content"] = 'sample = "if SIMULATE_CODE_REGRESSION: raise RuntimeError"'
        self.assertIsNone(self.plan())

    def test_incomplete_investigation_no_proposal(self):
        self.result = InvestigationResult(success=False, status=InvestigationStatus.MODEL_INCOMPLETE, answer="")
        self.assertIsNone(self.plan())

    def test_other_service_no_proposal(self):
        self.incident.service = "other"
        self.assertIsNone(self.plan())

    def test_persisted_proposal_never_action(self):
        self.plan()
        p = self.planner.propose(self.incident, self.result)
        data = self.planner.store.load()
        self.assertEqual(data["proposals"][p.proposal_id]["status"], "pending")
        self.assertEqual(data["actions"], [])

    def test_only_current_round_evidence_used(self):
        self.plan()
        self.assertIsNone(self.planner.plan(self.incident, self.result, []))


class RegistryTests(unittest.TestCase):
    def test_fixed_action_registry(self):
        self.assertEqual(set(ACTION_REGISTRY), {"disable_demo_regression"})
        self.assertIs(ACTION_REGISTRY["disable_demo_regression"], disable_demo_regression)

    def test_registry_immutable(self):
        with self.assertRaises(TypeError):
            ACTION_REGISTRY["run_shell"] = lambda: None

    def test_actions_not_agent_tools(self):
        from tools.tool_registry import tool_registry
        self.assertNotIn("disable_demo_regression", tool_registry)
        self.assertTrue({"query_logs", "query_metrics", "get_source_file", "get_running_version"} <= set(tool_registry))

    def test_model_function_call_cannot_reach_action(self):
        from tools.tool_manager import ToolManager
        item = SimpleNamespace(name="disable_demo_regression", arguments='{"service":"order-service"}', call_id="test")
        success, _, result = ToolManager().run_tool(item)
        self.assertFalse(success)
        self.assertEqual(result["status"], "tool_not_found")
