"""Real local W2 processes plus explicit failure injection; no external services."""
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from models.remediation import ExecutionOutcome as O, RemediationStatus as S
from remediation.approval import approval_digest
import remediation.compensation as compensation
import remediation.executor as execution
from remediation.isolated_demo import IsolatedDemoVerifier
from remediation.state import RemediationError
from remediation.store import RemediationStore
from remediation.windows_process import ProcessHandle
from scripts.w2_demo import create_demo

ROOT = Path(__file__).resolve().parents[1]
DEMO_PYTHON = Path(r"D:\prometheus-demo\.venv\Scripts\python.exe")
pytestmark = pytest.mark.skipif(
    os.name != "nt" or not DEMO_PYTHON.is_file(), reason="W2 Windows local Demo required",
)


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.fixture(scope="module")
def bundles(tmp_path_factory):
    root = tmp_path_factory.mktemp("w2-build")
    for name in ("first", "second"):
        subprocess.run([
            str(DEMO_PYTHON), "-I", "-B", str(ROOT / "scripts/w2_demo_build.py"),
            r"D:\prometheus-demo", str(root / name),
        ], check=True, timeout=30, capture_output=True)
    for release in "AB":
        first = json.loads((root / "first/build.json").read_text())[release]
        second = json.loads((root / "second/build.json").read_text())[release]
        assert Path(first["path"]).read_bytes() == Path(second["path"]).read_bytes()
    return root / "first"


def copy_bundles(source, root):
    shutil.copytree(source, root / "artifacts")
    build_path = root / "artifacts/build.json"
    build = json.loads(build_path.read_text(encoding="utf-8"))
    for release in build.values():
        release["path"] = str(root / "artifacts" / Path(release["path"]).name)
    build_path.write_text(json.dumps(build), encoding="utf-8")


@pytest.fixture
def case(tmp_path, bundles, monkeypatch):
    copy_bundles(bundles, tmp_path)
    # create_demo is deliberately opt-in. Restore process registries after each case.
    for module, name in (
        (execution, "ACTION_REGISTRY"), (execution, "SERVICE_LOCK"),
        (execution, "validate_parameters"), (compensation, "COMPENSATION_REGISTRY"),
    ):
        monkeypatch.setattr(module, name, getattr(module, name))
    app, runtime, store = create_demo(tmp_path, free_port())
    tokens = json.loads((tmp_path / "operators.json").read_text(encoding="utf-8"))
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 32100)) as client:
        def post(
            identifier,
            operation,
            role,
            command=None,
            expected=200,
        ):
            record = store.get(identifier)
            response = client.post(f"/api/remediations/{identifier}/{operation}",
                headers={"Authorization": "Bearer " + tokens[role]}, json={
                    "command_id": command or operation + "-" + identifier,
                    "proposal_digest": approval_digest(record),
                })
            assert response.status_code == expected, response.text
            return response.json()

        def deploy():
            post("rem-w2-deploy-b", "approve", "deploy-approver")
            result = post("rem-w2-deploy-b", "execute", "executor")
            assert result["status"] == "verification_failed"
            assert result["verifications"][0]["outcome"] == "failed"
            assert next(c for c in result["verifications"][0]["checks"]
                        if c["name"] == "business")["http_status"] == 500
            return result["compensation"]["compensation_proposal_id"]

        try:
            yield SimpleNamespace(root=tmp_path, runtime=runtime, store=store,
                                  client=client, post=post, deploy=deploy)
        finally:
            runtime.close()


def test_real_artifact_process_rollback_and_web_evidence(case):
    assert case.runtime.validated_a.success
    source_pid = case.runtime.current["process"].pid
    assert case.runtime.releases["A"].sha256 != case.runtime.releases["B"].sha256
    child = case.deploy()
    case.post(child, "execute", "executor", expected=409)
    case.post(child, "approve", "requester", expected=403)
    case.post(child, "approve", "rollback-approver")
    result = case.post(child, "execute", "executor", command="restore-once")
    assert result["status"] == "succeeded"
    assert result["demo_recovery"]["environment"] == "isolated_real_demo"
    assert result["demo_recovery"]["source_release_id"] == "B"
    assert result["demo_recovery"]["release_id"] == "A"
    assert result["demo_recovery"]["artifact_source"] == str(case.runtime.releases["A"].path)
    assert result["actions"][0]["release_id"] == "A"
    assert result["actions"][0]["pid"] != source_pid
    assert result["verifications"][0]["outcome"] == "passed"
    assert result["verifications"][0]["evidence_status"] == "bound"
    assert result["approved_by"] == "w2-rollback-approver"
    assert result["related_proposals"][0]["proposal_id"] == "rem-w2-deploy-b"
    effects = len(case.runtime.owned)
    repeat = case.post(child, "execute", "executor", command="restore-once")
    assert repeat["actions"] == result["actions"] and len(case.runtime.owned) == effects == 3
    assert all(item["process"].poll() is not None for item in case.runtime.owned[:-1])


@pytest.mark.parametrize("failure", ["missing", "hash", "identity", "deployed_file"])
def test_execution_preflight_blocks_without_stopping_current_instance(case, failure):
    child = case.deploy()
    case.post(child, "approve", "rollback-approver")
    if failure == "missing":
        case.runtime.releases["A"].path.unlink()
    elif failure == "hash":
        with case.runtime.releases["A"].path.open("ab") as out:
            out.write(b"tampered")
    elif failure == "identity":
        case.runtime.stop_owned(case.runtime.current)
        case.runtime.start(case.runtime.releases["B"])
    else:
        path = case.runtime.current["directory"] / "inventory_app.py"
        path.write_bytes(path.read_bytes() + b"\n# changed after approval\n")
    current = case.runtime.current["process"]
    effects = len(case.runtime.owned)
    result = case.post(child, "execute", "executor")
    assert result["status"] == "failed" and result["execution_outcome"] == "not_executed"
    assert result["actions"][0]["reason"] in {
        "demo_artifact_missing", "demo_artifact_hash_mismatch",
        "compensation_current_identity_changed", "demo_deployed_files_changed",
    }
    assert current.poll() is None and len(case.runtime.owned) == effects
    assert result["verifications"] == []


@pytest.mark.parametrize("field", ["parameters", "target", "source_identity"])
def test_approval_binds_target_parameters_and_instance(case, field):
    child = case.deploy()
    case.post(child, "approve", "rollback-approver")
    with case.store.transaction() as data:
        proposal = data["proposals"][child]
        if field == "parameters":
            proposal["parameters"]["artifact_source"] = str(case.root / "untrusted.zip")
        elif field == "target":
            proposal["compensation"]["target"]["artifact_sha256"] = "0" * 64
        else:
            proposal["compensation"]["source_identity"]["configuration_digest"] = "0" * 64
    case.post(child, "execute", "executor", expected=409)
    assert len(case.runtime.owned) == 2 and case.store.get(child).status == S.APPROVED


def test_successful_action_followed_by_real_business_unavailability_is_not_recovery(case):
    child = case.deploy()
    case.post(child, "approve", "rollback-approver")

    def stop_before_verification(phase, identifier):
        if phase == "action_committed":
            case.runtime.stop_owned(case.runtime.current)

    action = execution.ActionExecutor(
        case.store, runtime=case.runtime, verifier=IsolatedDemoVerifier(case.runtime),
        boundary_hook=stop_before_verification,
    ).execute(child, "w2-executor")
    assert action.success and action.execution_outcome == O.EXECUTED
    assert case.store.get(child).status == S.VERIFICATION_FAILED
    assert case.store.load()["verifications"][-1]["outcome"] == "no_data"


def test_missing_process_identity_never_authorizes_termination(case):
    owned = case.runtime.current
    snapshot = owned.pop("snapshot")
    try:
        with pytest.raises(RemediationError, match="demo_spawn_identity_unconfirmed"):
            case.runtime.stop_owned(owned)
        assert owned["process"].poll() is None
    finally:
        owned["snapshot"] = snapshot


@pytest.mark.parametrize("error,outcome,reason", [
    (TimeoutError(), "timeout", "demo_observation_timeout"),
    (RemediationError("demo_service_identity_changed"), "mismatch",
     "demo_service_identity_changed"),
])
def test_independent_verifier_preserves_failure_meaning(
    case,
    monkeypatch,
    error,
    outcome,
    reason,
):
    # Inject transport/identity failures; this test does not claim real timeouts.
    def fail_inspection():
        raise error

    monkeypatch.setattr(case.runtime, "inspect", fail_inspection)
    result = IsolatedDemoVerifier(case.runtime).observe(
        "rem-w2-deploy-b", case.runtime.releases["A"],
    )
    assert result.outcome.value == outcome and not result.success
    assert result.checks["observation"]["reason"] == reason
    with case.store.transaction() as data:
        data["verifications"].append(result.model_dump(mode="json"))
    public = case.client.get("/api/remediations/rem-w2-deploy-b").json()
    assert public["verifications"][0]["outcome"] == outcome
    assert public["verifications"][0]["checks"][0]["reason"] == reason


def stop_crash_owned_processes(root):
    """Parent test verifies saved OS birth, full argv and isolated cwd before stop."""
    snapshots = json.loads((root / "owned-processes.json").read_text(encoding="utf-8"))
    stopped = []
    for snapshot in snapshots:
        try:
            handle = ProcessHandle(snapshot["pid"], terminate=True)
        except RemediationError:
            continue
        with handle:
            # Exited PIDs may be reused: a mismatch is skipped, never terminated.
            if handle.snapshot() != snapshot:
                continue
            directory = Path(snapshot["cwd"]).resolve()
            assert directory.parent == root and directory.name.startswith("deployment-")
            assert Path(snapshot["argv"][4]).resolve() == directory / "launch.py"
            assert snapshot["argv"][1:4] == ["-I", "-S", "-B"]
            assert handle.kernel.TerminateProcess(handle.handle, 0)
            assert handle.kernel.WaitForSingleObject(handle.handle, 5000) == 0
            stopped.append(snapshot["pid"])
    (root / "crash-cleanup.json").write_text(json.dumps(stopped), encoding="utf-8")
    return stopped


@pytest.mark.parametrize("phase", [
    "executing_committed", "after_stop_before_start", "action_returned", "action_committed",
])
def test_real_executor_crash_never_blindly_replays(
    tmp_path,
    bundles,
    phase,
    monkeypatch,
):
    copy_bundles(bundles, tmp_path)
    port = free_port()
    result = subprocess.run([
        sys.executable, "-B", str(ROOT / "tests/w2_crash_worker.py"),
        str(tmp_path), str(port), phase,
    ], capture_output=True, text=True, timeout=35)
    try:
        assert result.returncode == 73, result.stderr
        saved = json.loads((tmp_path / "crash-target.json").read_text())
        store = RemediationStore(tmp_path / "remediation.json")
        proposal = store.get(saved["proposal_id"])
        assert proposal.status == S.EXECUTING
        data_before = (tmp_path / "owned-processes.json").read_bytes()
        monkeypatch.setattr(execution, "SERVICE_LOCK", tmp_path / "recovery.lock")
        # Restart with an empty production registry: executing is still non-replayable.
        with pytest.raises(RemediationError, match="proposal_not_approved:executing"):
            execution.ActionExecutor(store).execute(proposal.proposal_id, "recovery-operator")
        assert (tmp_path / "owned-processes.json").read_bytes() == data_before
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            if phase == "after_stop_before_start":
                with pytest.raises((ConnectionError, TimeoutError)):
                    connection.request("GET", "/version")
                version = None
            else:
                connection.request("GET", "/version")
                version = json.loads(connection.getresponse().read())
        finally:
            connection.close()
        if version is not None:
            assert version["release_id"] == ("B" if phase == "executing_committed" else "A")
        actions = [a for a in store.load()["actions"] if a["proposal_id"] == proposal.proposal_id]
        assert len(actions) == int(phase == "action_committed")
        assert store.execution_outcome(store.load(), proposal) == (
            O.EXECUTED if phase == "action_committed" else O.UNKNOWN
        )
    finally:
        stop_crash_owned_processes(tmp_path)


def test_imports_do_not_register_demo_in_normal_process():
    code = """
import sys
sys.path.insert(0, sys.argv[1])
import remediation.executor as execution
import remediation.compensation as compensation
before = (execution.ACTION_REGISTRY, compensation.COMPENSATION_REGISTRY)
import remediation.isolated_demo
import scripts.w2_demo
assert execution.ACTION_REGISTRY is before[0]
assert compensation.COMPENSATION_REGISTRY is before[1]
assert 'w2_inventory_deploy_b' not in execution.ACTION_REGISTRY
assert not compensation.COMPENSATION_REGISTRY
"""
    subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT)],
                   check=True, capture_output=True, timeout=10)
