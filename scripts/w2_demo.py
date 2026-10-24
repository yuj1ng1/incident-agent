"""Opt-in loopback W2 workbench. No model, production Store or normal handler.

Start a new TEMP directory, build the inventory bundles, validate A, and expose
the existing approval API. STOP file / Ctrl+C shuts down only owned processes.
"""
import argparse
import functools
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from types import MappingProxyType

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def create_demo(root, port):
    from incident_store import IncidentStore
    from investigation.store import RuntimeStore
    from models.incident import Incident
    from models.remediation import RemediationProposal, Risk
    from remediation.isolated_demo import (
        DEPLOY, SERVICE, DemoRelease, IsolatedDemoRuntime, IsolatedDemoVerifier,
    )
    import remediation.compensation as compensation
    import remediation.executor as execution
    from remediation.store import RemediationStore
    from web_api.app import create_app
    from web_api.auth import WebAuthorization

    root = Path(root).resolve()
    if any((root / name).exists() for name in (
        "remediation.json", "owned-processes.json", "baseline-A.json",
    )):
        raise ValueError("new_experiment_directory_required_use_review_for_saved_evidence")
    build = json.loads((root / "artifacts" / "build.json").read_text(encoding="utf-8"))
    runtime = IsolatedDemoRuntime(
        root, {key: DemoRelease.from_build(value) for key, value in build.items()}, port,
    )
    try:
        runtime.start(runtime.releases["A"])
        verifier = IsolatedDemoVerifier(runtime)
        runtime.validated_a = verifier.observe("baseline-A", runtime.releases["A"])
        if not runtime.validated_a.success:
            raise RuntimeError("baseline_a_business_verification_failed")
        (root / "baseline-A.json").write_text(
            runtime.validated_a.model_dump_json(indent=2), encoding="utf-8",
        )
        contract = runtime.contract()
        # Registration is confined to this explicitly started disposable process.
        execution.ACTION_REGISTRY = MappingProxyType({DEPLOY: runtime.change})
        execution.validate_parameters = runtime.validate_parameters
        execution.SERVICE_LOCK = root / "isolated-execution.lock"
        compensation.COMPENSATION_REGISTRY = MappingProxyType({(SERVICE, DEPLOY): contract})

        incidents = IncidentStore(root / "incidents.json")
        incidents.save_incident("w2-isolated-inventory", Incident(
            incident_id="inc-w2-isolated-inventory", service=SERVICE,
            severity="warning", status="investigating", alerts=[],
        ))
        sessions = RuntimeStore(root / "runtime.sqlite3")
        store = RemediationStore(root / "remediation.json")
        proposal = store.create(RemediationProposal(
            proposal_id="rem-w2-deploy-b", incident_id="inc-w2-isolated-inventory",
            incident_fingerprint="w2-isolated-inventory", service=SERVICE,
            summary="隔离真实 Demo：受控部署 B，申请恢复至实测正常的 A",
            reason="A/B 共用精确 Git 源码，B 是库存错误配置变更；仅本次 TEMP 实例。",
            action_type=DEPLOY, parameters=runtime.parameters(runtime.releases["B"]),
            risk=Risk.MEDIUM,
            expected_commit_sha=runtime.validated_a.observed_identity.commit_sha,
            expected_process_started_at=runtime.validated_a.observed_identity.process_started_at,
        ))
        tokens = {role: secrets.token_urlsafe(36) for role in (
            "requester", "deploy-approver", "rollback-approver", "executor",
        )}
        auth = WebAuthorization(json.dumps([
            {"actor": "w2-" + role,
             "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
             "permissions": ["approve" if role.endswith("approver")
                             else "execute" if role == "executor" else "request"]}
            for role, token in tokens.items()
        ]))
        (root / "operators.json").write_text(json.dumps(tokens), encoding="utf-8")
        (root / "identifiers.json").write_text(json.dumps({
            "original": proposal.proposal_id, "instance_id": runtime.instance,
        }, indent=2), encoding="utf-8")
        app = create_app(
            incident_path=incidents.file_path, runtime_path=sessions.path,
            remediation_path=store.file_path, authorization=auth,
            executor_factory=functools.partial(
                execution.ActionExecutor, runtime=runtime, verifier=verifier,
            ),
        )
        return app, runtime, store
    except BaseException:
        runtime.close()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="隔离真实库存 Demo；停止后不自动重放动作。")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--port", type=int, default=18020)
    parser.add_argument("--app-port", type=int, default=18120)
    parser.add_argument("--review", action="store_true")
    args = parser.parse_args(argv)
    root = (args.root or Path(tempfile.mkdtemp(prefix="incident-w2-")) / "experiment").resolve()
    if (not root.is_relative_to(Path(tempfile.gettempdir()).resolve())
            or root == Path(tempfile.gettempdir()).resolve()
            or args.port == args.app_port or not 1024 <= args.port <= 65535):
        parser.error("isolated_temporary_root_and_distinct_ports_required")
    os.environ["INCIDENT_AGENT_LOG_PATH"] = str(root.parent / "w2-agent.log")
    runtime = None
    if args.review:
        from web_api.app import create_app
        from web_api.auth import WebAuthorization
        app = create_app(
            incident_path=root / "incidents.json", runtime_path=root / "runtime.sqlite3",
            remediation_path=root / "remediation.json", authorization=WebAuthorization("[]"),
        )
    else:
        if root.exists():
            parser.error("new_experiment_directory_required_use_review_for_saved_evidence")
        root.mkdir(parents=True, exist_ok=False)
        subprocess.run([
            str(Path(r"D:\prometheus-demo\.venv\Scripts\python.exe")), "-I", "-B",
            str(ROOT / "scripts" / "w2_demo_build.py"),
            r"D:\prometheus-demo", str(root / "artifacts"),
        ], check=True, timeout=30)
        app, runtime, store = create_demo(root, args.app_port)
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False,
    ))

    def stop_on_file():
        stop_file = root / ("REVIEW_STOP" if args.review else "STOP")
        while not server.should_exit:
            if stop_file.exists():
                server.should_exit = True
                return
            time.sleep(0.2)

    threading.Thread(target=stop_on_file, daemon=True).start()
    print(json.dumps({"root": str(root), "api_port": args.port,
                      "app_port": args.app_port, "review_only": args.review}), flush=True)
    try:
        server.run()
    finally:
        if runtime is not None:
            runtime.close()


if __name__ == "__main__":
    main()
