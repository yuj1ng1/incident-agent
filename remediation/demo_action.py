"""One fixed runtime operation. No model-controlled command/path/env/PID."""
import json
import os
from pathlib import Path
import subprocess
import time

from models.remediation import ActionResult, utc_now
from remediation.approval import approval_digest
from remediation.planner import parse_time
from remediation.state import RemediationError
from remediation.verification import DemoClient
from remediation.windows_process import ProcessHandle, belongs_to_demo

DEMO_ROOT = Path(r"D:\prometheus-demo")
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
LISTENER_SCRIPT = """
$ErrorActionPreference = 'Stop'
$listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object LocalPort -eq 8001)
ConvertTo-Json -Compress -InputObject @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
"""
SYSTEM_ENVIRONMENT_KEYS = (
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
)


def validate_parameters(proposal):
    if proposal.service != "order-service":
        raise RemediationError("service_not_allowed")
    if proposal.parameters != {"service": "order-service"}:
        raise RemediationError("invalid_action_parameters")


class DemoRuntime:
    def __init__(self, client=None):
        self.client = client or DemoClient()

    def listener(self):
        try:
            result = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", LISTENER_SCRIPT],
                capture_output=True, encoding="utf-8", timeout=10, check=True)
            pids = json.loads(result.stdout)
            if not isinstance(pids, list) or len(pids) != 1 or type(pids[0]) is not int or pids[0] <= 0:
                raise RemediationError("demo_not_listening" if pids == [] else "process_identity_unconfirmed")
            with ProcessHandle(pids[0]) as handle:
                snapshot = handle.snapshot()
            if not belongs_to_demo(snapshot, DEMO_ROOT):
                raise RemediationError("process_identity_unconfirmed")
            return snapshot
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            if isinstance(exc, RemediationError):
                raise
            raise RemediationError("process_identity_unconfirmed") from None

    def repository_ready(self, expected_sha):
        try:
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=DEMO_ROOT, check=True,
                capture_output=True, text=True, timeout=5).stdout.strip()
            dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--",
                "*.py", "*.ps1", "requirements.txt"], cwd=DEMO_ROOT, check=True,
                capture_output=True, text=True, timeout=5).stdout.strip()
            if head != expected_sha:
                raise RemediationError("runtime_changed_since_proposal")
            if dirty:
                raise RemediationError("demo_runtime_code_dirty")
            if not (DEMO_ROOT / "start_demo.ps1").is_file() or not (DEMO_ROOT / ".venv/Scripts/python.exe").is_file():
                raise RemediationError("demo_startup_unavailable")
        except (OSError, subprocess.SubprocessError):
            raise RemediationError("demo_repository_unconfirmed") from None

    def stop(self, identity):
        # Re-acquire the port and compare its creation time, argv, executable, and cwd.
        if self.listener() != identity:
            raise RemediationError("process_identity_unconfirmed")
        with ProcessHandle(identity["pid"], terminate=True) as handle:
            handle.stop_verified(identity, DEMO_ROOT)

    def start(self):
        # Intentionally do not inherit API keys, tokens, or database credentials.
        env = {key: os.environ[key] for key in SYSTEM_ENVIRONMENT_KEYS if key in os.environ}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            process = subprocess.Popen([str(POWERSHELL), "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", str(DEMO_ROOT / "start_demo.ps1")],
                cwd=DEMO_ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            raise RemediationError("demo_start_failed_manual_intervention_required") from None
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RemediationError("demo_start_failed_manual_intervention_required")
            try:
                version = self.client.version()
                if version["runtime_flags"]["simulate_code_regression"] is False:
                    return version
            except RemediationError:
                pass
            time.sleep(0.25)
        raise RemediationError("demo_start_timeout_manual_intervention_required")


def disable_demo_regression(proposal, runtime=None):
    started = utc_now()
    runtime = runtime or DemoRuntime()
    stopped = False
    details = {}
    try:
        if (proposal.status.value != "executing" or not proposal.approved_by or not proposal.approved_at
                or proposal.approval_digest != approval_digest(proposal)):
            raise RemediationError("approval_unconfirmed")
        validate_parameters(proposal)
        version = runtime.client.version()
        if version["commit_sha"] != proposal.expected_commit_sha:
            raise RemediationError("runtime_changed_since_proposal")
        if version["runtime_flags"]["simulate_code_regression"] is False:
            return ActionResult(proposal_id=proposal.proposal_id, action_type=proposal.action_type,
                started_at=started, success=True, status="already_remediated",
                summary="运行开关已经关闭，未停止或重启进程。")
        if parse_time(version["process_started_at"]) != proposal.expected_process_started_at:
            raise RemediationError("runtime_changed_since_proposal")
        runtime.repository_ready(proposal.expected_commit_sha)
        identity = runtime.listener()
        # Check again immediately before stop, after process/repository inspection.
        if runtime.client.version() != version:
            raise RemediationError("runtime_changed_since_proposal")
        runtime.repository_ready(proposal.expected_commit_sha)
        details = {"old_pid": identity["pid"], "process_identity_confirmed": True,
                   "commit_sha": proposal.expected_commit_sha}
        runtime.stop(identity)
        stopped = True
        runtime.start()
        new_identity = runtime.listener()
        details.update(new_pid=new_identity["pid"], old_demo_stopped=True)
        return ActionResult(proposal_id=proposal.proposal_id, action_type=proposal.action_type,
            started_at=started, success=True, status="action_completed",
            summary="已用原 start_demo.ps1 正常模式重新启动 Demo，等待独立恢复验证。", details=details)
    except RemediationError as exc:
        details["old_demo_stopped"] = stopped
        return ActionResult(proposal_id=proposal.proposal_id, action_type=proposal.action_type,
            started_at=started, success=False, status=str(exc),
            summary="固定动作失败；若旧 Demo 已停止，需要人工检查启动。", details=details)
    except Exception:
        return ActionResult(proposal_id=proposal.proposal_id, action_type=proposal.action_type,
            started_at=started, success=False, status="action_internal_error",
            summary="执行失败，需要人工检查。", details={"old_demo_stopped": stopped})
