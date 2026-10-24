"""Opt-in W2 inventory recovery. Importing this module registers no action.

The owner retains Popen/native handles. No PID, path, command or release supplied
by the browser is used to select an executable target. Crashes have no replay.
"""
from dataclasses import dataclass
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
from types import MappingProxyType
import zipfile

from models.remediation import (
    ActionResult, CompensationTarget, ExecutionOutcome as O, Risk,
    ServiceIdentity, VerificationOutcome as V, VerificationResult, utc_now,
)
from remediation.compensation import CompensationContract
from remediation.state import RemediationError
from remediation.windows_process import ProcessHandle

DEPLOY = "w2_inventory_deploy_b"
RESTORE = "w2_inventory_restore_a"
SERVICE = "inventory-service"


def digest(data):
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class DemoRelease:
    path: Path
    sha256: str
    commit: str
    release_id: str
    interpreter: Path
    interpreter_sha256: str
    files: tuple[tuple[str, str], ...]
    manifest_bytes: bytes

    @classmethod
    def from_build(cls, value):
        manifest = value["manifest"]
        return cls(
            path=Path(value["path"]), sha256=value["sha256"],
            commit=manifest["source_commit"], release_id=manifest["release_id"],
            interpreter=Path(manifest["interpreter"]),
            interpreter_sha256=manifest["interpreter_sha256"],
            files=tuple(sorted(manifest["files"].items())),
            manifest_bytes=json.dumps(manifest, sort_keys=True).encode(),
        )

    def verified_bytes(self):
        if not self.path.is_file():
            raise RemediationError("demo_artifact_missing")
        data = self.path.read_bytes()
        if digest(data) != self.sha256:
            raise RemediationError("demo_artifact_hash_mismatch")
        if digest(self.interpreter.read_bytes()) != self.interpreter_sha256:
            raise RemediationError("demo_interpreter_changed")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            expected = dict(self.files) | {"release.json": digest(self.manifest_bytes)}
            if (len(archive.namelist()) != len(expected)
                    or set(archive.namelist()) != set(expected)):
                raise RemediationError("demo_artifact_contents_changed")
            for name, checksum in expected.items():
                if (Path(name).is_absolute() or ".." in Path(name).parts
                        or digest(archive.read(name)) != checksum):
                    raise RemediationError("demo_artifact_contents_changed")
        return data


class IsolatedDemoRuntime:
    def __init__(
        self,
        root,
        releases,
        port,
    ):
        self.root = Path(root).resolve()
        temporary = Path(tempfile.gettempdir()).resolve()
        if (not self.root.is_relative_to(temporary) or self.root == temporary
                or not 1024 <= port <= 65535 or set(releases) != {"A", "B"}):
            raise RemediationError("demo_isolation_required")
        self.releases = MappingProxyType(dict(releases))
        for release in releases.values():
            if (release.path != release.path.resolve()
                    or not release.path.is_relative_to(self.root / "artifacts")):
                raise RemediationError("demo_artifact_outside_isolation")
        self.port = port
        self.instance = "w2-" + secrets.token_hex(16)
        self.owned = []
        self.current = None
        self.validated_a = None

    def configuration(self, release):
        return {
            "host": "127.0.0.1", "port": self.port,
            "isolation_root": str(self.root), "instance_id": self.instance,
            "artifact_sha256": release.sha256,
            "interpreter": str(release.interpreter),
            "interpreter_sha256": release.interpreter_sha256,
            "python_arguments": "-I -S -B launch.py",
            "environment_policy": "system_paths_only; package_owned_flags",
            "database": "none",
        }

    def configuration_digest(self, release):
        return digest(json.dumps(self.configuration(release), sort_keys=True).encode())

    def parameters(self, release):
        return {
            "service": SERVICE, "target_id": "isolated-inventory-" + release.release_id,
            "artifact_source": str(release.path), "artifact_sha256": release.sha256,
            "source_commit": self.releases["B"].commit,
            "target_commit": release.commit, "release_id": release.release_id,
            "source_release_id": "B" if release.release_id == "A" else "A",
            "configuration_digest": self.configuration_digest(release),
            "instance_id": self.instance,
            "startup_configuration": json.dumps(self.configuration(release), sort_keys=True),
        }

    def _port_free(self):
        with socket.socket() as listener:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                listener.bind(("127.0.0.1", self.port))
            except OSError:
                raise RemediationError("demo_isolated_port_in_use") from None

    def _read(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read(65537)
            if len(body) > 65536:
                raise RemediationError("demo_response_too_large")
            return response.status, json.loads(body)
        finally:
            connection.close()

    def _files_unchanged(self, owned):
        release, directory = owned["release"], owned["directory"]
        for name, checksum in release.files:
            path = directory / name
            if not path.is_file() or digest(path.read_bytes()) != checksum:
                raise RemediationError("demo_deployed_files_changed")
        if (directory / "release.json").read_bytes() != release.manifest_bytes:
            raise RemediationError("demo_deployed_files_changed")
        allowed = {name for name, _ in release.files} | {"release.json"}
        for path in directory.rglob("*"):
            if path.is_file() and path.relative_to(directory).as_posix() not in allowed:
                if path.parent != directory / "logs" or path.suffix != ".log":
                    raise RemediationError("demo_unexpected_deployed_file")

    def inspect(self):
        owned = self.current
        if owned is None or owned["process"].poll() is not None:
            raise RemediationError("demo_owned_process_not_running")
        with ProcessHandle(owned["process"].pid) as handle:
            if handle.snapshot() != owned["snapshot"]:
                raise RemediationError("demo_process_identity_changed")
        self._files_unchanged(owned)
        status, version = self._read("/version")
        release = owned["release"]
        expected = {
            "service": SERVICE, "commit_sha": release.commit,
            "release_id": release.release_id, "artifact_sha256": release.sha256,
            "configuration_digest": self.configuration_digest(release),
            "instance_id": self.instance, "pid": owned["process"].pid,
            "source_files": {name: checksum for name, checksum in release.files
                             if name in {"inventory_app.py", "http_observability.py"}},
            "runtime_flags": {
                "simulate_inventory_error": release.release_id == "B",
                "simulate_slow_request": False,
            },
        }
        if status != 200 or any(version.get(k) != v for k, v in expected.items()):
            raise RemediationError("demo_service_identity_changed")
        identity = ServiceIdentity(
            service=SERVICE, commit_sha=release.commit,
            process_started_at=version["process_started_at"],
            configuration_digest=self.configuration_digest(release),
        )
        if owned.get("identity") is not None and identity != owned["identity"]:
            raise RemediationError("demo_service_identity_changed")
        return identity, version

    def start(
        self,
        release,
        verified=None,
    ):
        data = release.verified_bytes() if verified is None else verified
        if digest(data) != release.sha256:
            raise RemediationError("demo_artifact_hash_mismatch")
        self._port_free()
        directory = self.root / ("deployment-" + secrets.token_hex(8))
        directory.mkdir(exist_ok=False)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            archive.extractall(directory)
        (directory / "logs").mkdir(exist_ok=True)
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
        arguments = [str(release.interpreter), "-I", "-S", "-B",
                     str(directory / "launch.py"), str(self.port), self.instance,
                     release.sha256, self.configuration_digest(release)]
        with (directory / "logs" / "process.log").open("wb") as output:
            process = subprocess.Popen(
                arguments, cwd=directory, env=environment, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        # Popen retains the native process handle. Never reconstruct ownership from a PID.
        owned = {"process": process, "release": release, "directory": directory}
        self.owned.append(owned)
        # Windows may return from CreateProcess before PEB command-line setup.
        deadline = time.monotonic() + 3
        while True:
            try:
                with ProcessHandle(process.pid) as handle:
                    owned["snapshot"] = handle.snapshot()
                snapshot = owned["snapshot"]
                if (Path(snapshot["cwd"]).resolve() != directory
                        or Path(snapshot["exe"]).resolve() != release.interpreter
                        or snapshot["argv"] != arguments):
                    raise RemediationError("demo_spawn_identity_unconfirmed")
                break
            except RemediationError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        self.current = owned
        self._save_ownership()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RemediationError("demo_start_failed")
            try:
                identity, _ = self.inspect()
                owned["identity"] = identity
                return identity
            except (OSError, ValueError, http.client.HTTPException):
                time.sleep(0.05)
        raise RemediationError("demo_start_timeout")

    def _save_ownership(self):
        (self.root / "owned-processes.json").write_text(json.dumps([
            item["snapshot"] for item in self.owned if "snapshot" in item
        ], indent=2), encoding="utf-8")

    def stop_owned(self, owned):
        process = owned["process"]
        if process.poll() is not None:
            return
        if "snapshot" not in owned:
            raise RemediationError("demo_spawn_identity_unconfirmed")
        with ProcessHandle(process.pid) as handle:
            if handle.snapshot() != owned["snapshot"]:
                raise RemediationError("demo_process_identity_changed")
        # terminate() uses Popen's original Windows HANDLE, immune to PID reuse.
        process.terminate()
        process.wait(timeout=5)

    def close(self):
        stopped, errors = [], []
        for owned in reversed(self.owned):
            try:
                self.stop_owned(owned)
                stopped.append(owned["process"].pid)
            except (OSError, subprocess.SubprocessError, RemediationError) as exc:
                errors.append({"pid": owned["process"].pid, "reason": type(exc).__name__})
        result = {"stopped_or_already_exited": stopped, "errors": errors}
        (self.root / "cleanup.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        if errors:
            raise RemediationError("demo_cleanup_incomplete")
        return result

    def validate_parameters(self, proposal):
        expected = self.validated_a.observed_identity if self.validated_a else None
        if (proposal.service != SERVICE or proposal.action_type != DEPLOY
                or proposal.parameters != self.parameters(self.releases["B"])
                or expected is None
                or proposal.expected_commit_sha != expected.commit_sha
                or proposal.expected_process_started_at != expected.process_started_at):
            raise RemediationError("demo_fixed_contract_required")

    def change(
        self,
        proposal,
        unused_runtime,
    ):
        started = utc_now()
        changed = False
        try:
            target = self.releases["A" if proposal.compensation else "B"]
            expected = (proposal.compensation.source_identity if proposal.compensation
                        else self.validated_a.observed_identity)
            if proposal.parameters != self.parameters(target):
                raise RemediationError("demo_fixed_contract_required")
            # Read and hash the exact bytes that will be extracted, before stopping B.
            artifact = target.verified_bytes()
            identity, before = self.inspect()
            if identity != expected:
                raise RemediationError("demo_service_identity_changed")
            changed = True
            self.stop_owned(self.current)
            resulting = self.start(target, artifact)
            return ActionResult(
                proposal_id=proposal.proposal_id, action_type=proposal.action_type,
                started_at=started, success=True, status="isolated_process_replaced",
                summary="从固定制品启动新的隔离库存进程；业务状态由独立验证器判断。",
                execution_outcome=O.EXECUTED, resulting_identity=resulting,
                details={"artifact_sha256": target.sha256, "release_id": target.release_id,
                         "previous_pid": before["pid"], "pid": self.current["process"].pid},
            )
        except (OSError, ValueError, http.client.HTTPException, subprocess.SubprocessError,
                RemediationError) as exc:
            return ActionResult(
                proposal_id=proposal.proposal_id, action_type=proposal.action_type,
                started_at=started, success=False,
                status="demo_execution_unknown" if changed else "demo_execution_blocked",
                summary="隔离 Demo 恢复未确认；禁止盲目重放或重新启用错误版本。",
                execution_outcome=O.UNKNOWN if changed else O.NOT_EXECUTED,
                details={"reason": str(exc) if isinstance(exc, RemediationError)
                         else "demo_process_or_transport_failure"},
            )

    def contract(self):
        if self.validated_a is None or not self.validated_a.success:
            raise RemediationError("demo_a_not_business_verified")
        release = self.releases["A"]
        target = CompensationTarget(
            target_id="isolated-inventory-A", service=SERVICE,
            commit_sha=release.commit, artifact_sha256=release.sha256,
            configuration_digest=self.configuration_digest(release),
            preconditions="仅本次 TEMP 隔离实例；精确匹配进程、源码、配置和已审批制品；证据不超过 300 秒。",
            side_effects="停止本次拥有句柄的 B 进程，从已验证 A 制品解包并启动新进程；不操作原服务或数据库。",
            verification_policy=(
                "独立 HTTP 核对版本、制品、实例、PID、配置与源码哈希，"
                "health=200 且 inventory 精确返回 demo-item:10。"
            ),
            data_compatibility="无数据库与持久业务数据；库存为源码内静态数据；仅产生本次隔离目录日志。",
        )
        return CompensationContract(
            contract_id="isolated-inventory-recovery-v1", original_action_type=DEPLOY,
            action_type=RESTORE, target=target,
            parameters=MappingProxyType(self.parameters(release)), risk=Risk.MEDIUM,
            inspect_identity=lambda proposal, unused: self.inspect()[0],
            handler=self.change, verifier=IsolatedDemoVerifier(self),
        )


class IsolatedDemoVerifier:
    """Fresh HTTP business observations; never trusts handler success."""
    def __init__(self, runtime):
        self.runtime = runtime

    def verify(self, proposal):
        expected = self.runtime.releases["A" if proposal.compensation else "B"]
        return self.observe(proposal.proposal_id, expected)

    def observe(
        self,
        proposal_id,
        expected,
    ):
        started = utc_now()
        checks, identity = {}, None
        try:
            identity, version = self.runtime.inspect()
            matches = version["artifact_sha256"] == expected.sha256
            checks["version"] = {"success": matches, "http_status": 200,
                                 "observed": version}
            health_status, health = self.runtime._read("/health")
            checks["health"] = {"success": health_status == 200 and health.get("health") == "ok",
                                "http_status": health_status}
            status, body = self.runtime._read("/inventory")
            checks["business"] = {
                "success": status == 200 and body == {
                    "service": SERVICE, "items": [{"item": "demo-item", "quantity": 10}],
                }, "http_status": status, "body": body,
            }
            after, _ = self.runtime.inspect()
            if not matches or after != identity:
                outcome = V.MISMATCH
            else:
                outcome = V.PASSED if all(c["success"] for c in checks.values()) else V.FAILED
        except TimeoutError:
            outcome = V.TIMEOUT
            checks["observation"] = {
                "success": False, "reason": "demo_observation_timeout",
            }
        except RemediationError as exc:
            mismatches = {
                "demo_process_identity_changed", "demo_service_identity_changed",
                "demo_deployed_files_changed", "demo_unexpected_deployed_file",
            }
            outcome = V.MISMATCH if str(exc) in mismatches else V.NO_DATA
            checks["observation"] = {"success": False, "reason": str(exc)}
        except (OSError, ValueError, http.client.HTTPException):
            outcome = V.NO_DATA
            checks["observation"] = {
                "success": False, "reason": "demo_observation_unavailable",
            }
        return VerificationResult(
            proposal_id=proposal_id, started_at=started,
            success=outcome == V.PASSED, outcome=outcome, observed_identity=identity,
            checks=checks, summary="隔离真实 Demo：独立版本、健康与库存业务观测；不证明历史生产恢复能力。",
        )
