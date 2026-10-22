"""Loopback-only Web acceptance fixture. Never imports Workers or real clients.

Every invocation creates a new directory under system TEMP. Synthetic contracts
exist only in this process; importing this module does not register any action.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
from types import MappingProxyType

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def create_demo(root):
    from incident_store import IncidentStore
    from investigation.models import Checkpoint, InvestigationSession, SessionStatus
    from investigation.store import RuntimeStore
    from models.incident import Alert, Incident
    from models.investigation import InvestigationRecord, InvestigationResult
    from models.remediation import (
        ActionResult, CompensationTarget, ExecutionOutcome, RemediationProposal,
        RemediationStatus, Risk, ServiceIdentity, VerificationOutcome,
        VerificationResult, utc_now,
    )
    from remediation.approval import approval_digest
    import remediation.compensation as compensation
    import remediation.executor as execution
    from remediation.state import transition
    from remediation.store import RemediationStore
    from web_api.app import create_app
    from web_api.auth import WebAuthorization
    from fastapi import HTTPException

    root = Path(root).resolve()
    if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError("demo_requires_system_temporary_directory")
    root.mkdir(parents=True, exist_ok=False)
    faults_path = root / 'http-faults.json'
    faults_path.write_text('{}', encoding='utf-8')
    incidents = IncidentStore(root / "incidents.json")
    runtime = RuntimeStore(root / "runtime.sqlite3")
    store = RemediationStore(root / "remediation.json")
    # This process cannot dispatch any ordinary production repair action.
    execution.ACTION_REGISTRY = MappingProxyType({})
    execution.SERVICE_LOCK = root / "synthetic-service.lock"
    contracts = {}
    sources = {}

    class FixtureVerifier:
        def __init__(self, state_path, identity):
            self.state_path = state_path
            self.identity = identity

        def verify(self, proposal):
            started = utc_now()
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            passed = state.get("configuration") == "safe-v1"
            return VerificationResult(
                proposal_id=proposal.proposal_id,
                started_at=started,
                success=passed,
                outcome=VerificationOutcome.PASSED if passed else VerificationOutcome.FAILED,
                observed_identity=self.identity,
                checks={"business": {"success": passed}},
                summary="合成测试：独立读取临时文件中的业务断言，非真实业务恢复。",
            )

    def contract_for(service, source, unknown=False):
        state_path = root / (service + ".json")
        state_path.write_text(json.dumps({"configuration": "bad-v2", "effects": 0}), encoding="utf-8")
        target = CompensationTarget(
            target_id="synthetic-safe-v1",
            service=service,
            commit_sha="b" * 40,
            artifact_sha256="e" * 64,
            configuration_digest="d" * 64,
            preconditions="合成测试：精确匹配本次临时夹具身份；保留 300 秒证据有效期。",
            side_effects="仅将本次 TEMP 下的合成 JSON 配置改为 safe-v1，累加文件计数。",
            verification_policy="独立重新读取临时文件并断言 configuration=safe-v1。",
            data_compatibility="合成 JSON schema 不变；不连接数据库或真实业务。",
        )
        resulting = source.model_copy(update={
            "commit_sha": target.commit_sha,
            "configuration_digest": target.configuration_digest,
        })

        def handler(proposal, unused_runtime):
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state.update(configuration="safe-v1", effects=state["effects"] + 1)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            return ActionResult(
                proposal_id=proposal.proposal_id,
                action_type=proposal.action_type,
                started_at=utc_now(),
                success=not unknown,
                status="synthetic_unknown" if unknown else "synthetic_file_updated",
                execution_outcome=ExecutionOutcome.UNKNOWN if unknown else ExecutionOutcome.EXECUTED,
                resulting_identity=resulting,
                summary="合成测试：仅操作本次临时文件。",
            )

        return compensation.CompensationContract(
            contract_id="synthetic-w1-v1",
            original_action_type="synthetic_original",
            action_type="synthetic_restore_config",
            target=target,
            parameters=MappingProxyType({"service": service, "target_id": target.target_id}),
            risk=Risk.LOW,
            inspect_identity=lambda proposal, unused_runtime: source,
            handler=handler,
            verifier=FixtureVerifier(state_path, resulting),
        )

    for scenario in "ABCDE":
        service = f"synthetic-{scenario.lower()}-service"
        sources[scenario] = ServiceIdentity(
            service=service,
            commit_sha="a" * 40,
            process_started_at=utc_now(),
            configuration_digest="c" * 64,
        )
        if scenario in "BCE":
            contracts[(service, "synthetic_original")] = contract_for(
                service, sources[scenario], unknown=scenario == "E",
            )
    compensation.COMPENSATION_REGISTRY = MappingProxyType(contracts)

    labels = {
        "A": "验证失败，缺少安全补偿目标",
        "B": "验证失败，独立补偿审批与执行",
        "C": "人工主动申请回滚，仅创建提案",
        "D": "权限不足，后端拒绝审批",
        "E": "执行结果未知，禁止再次执行",
    }
    identifiers = {}
    for scenario, label in labels.items():
        source = sources[scenario]
        incident_id = f"inc-synthetic-{scenario.lower()}"
        session_id = f"ses-synthetic-{scenario.lower()}"
        fingerprint = f"synthetic-w1-{scenario.lower()}"
        answer = f"## 合成调查记录 · 场景 {scenario}\n\n{label}。\n\n这是预置测试记录，没有调用模型或监控系统。调查完成不等于业务恢复。"
        incident = Incident(
            incident_id=incident_id,
            service=source.service,
            severity="critical" if scenario in "ABE" else "warning",
            status="investigating",
            alerts=[Alert(
                service=source.service, severity="warning", metric="synthetic_error_ratio",
                value=0.3, threshold=0.1,
            )],
            investigations=[InvestigationRecord(
                session_id=session_id, investigated_at=utc_now(),
                status="completed", answer=answer,
            )],
        )
        incidents.save_incident(fingerprint, incident)
        session = InvestigationSession(
            session_id=session_id, incident_id=incident_id,
            alert_fingerprint=fingerprint, service=source.service,
            status=SessionStatus.COMPLETED, completed_at=utc_now(),
        )
        with runtime.transaction() as db:
            runtime.insert(session, db)
            runtime.checkpoint(session, Checkpoint(
                phase="response_saved", projected=True,
                result=InvestigationResult(success=True, status="completed", answer=answer).model_dump(mode="json"),
            ).model_dump(mode="json"), db, acknowledge=True)
            runtime.save(session, db)

        original = store.create(RemediationProposal(
            proposal_id=f"rem-synthetic-{scenario.lower()}",
            incident_id=incident_id, incident_fingerprint=fingerprint,
            service=source.service, summary=f"合成测试 {scenario}：{label}",
            reason="W1 浏览器验收夹具；所有事实与版本均为合成。",
            action_type="synthetic_original", parameters={"service": source.service},
            risk=Risk.LOW, expected_commit_sha=source.commit_sha,
            expected_process_started_at=source.process_started_at,
        ))
        identifiers[scenario] = original.proposal_id
        if scenario == "D":
            continue
        approved = store.approve(original.proposal_id, "synthetic-original-approver")
        executing = transition(approved, RemediationStatus.EXECUTING, executed_at=utc_now())
        action = ActionResult(
            proposal_id=original.proposal_id, action_type=original.action_type,
            started_at=utc_now(), success=True, status="synthetic_original_record",
            execution_outcome=ExecutionOutcome.EXECUTED, resulting_identity=source,
            summary="合成历史记录：未执行真实修复。",
        )
        passed = scenario == "C"
        verification = VerificationResult(
            proposal_id=original.proposal_id, started_at=utc_now(),
            success=passed, outcome=VerificationOutcome.PASSED if passed else VerificationOutcome.FAILED,
            observed_identity=source, action_id=action.action_id,
            proposal_digest=approval_digest(original), checks={"business": {"success": passed}},
            summary="合成历史业务观测。",
        )
        final = transition(executing, RemediationStatus.SUCCEEDED if passed else RemediationStatus.VERIFICATION_FAILED)
        with store.transaction() as data:
            store.put(data, final)
            data["actions"].append(action.model_dump(mode="json"))
            data["verifications"].append(verification.model_dump(mode="json"))
            store.event(data, final, "synthetic_history_seeded", "synthetic-fixture")
            if not passed:
                assessment = compensation.record_request(
                    data, store, final, "synthetic-verifier", "合成业务验证明确失败。",
                    f"seed-{scenario}", "verification_failed",
                )
                if assessment.compensation_proposal_id:
                    identifiers[scenario + "_compensation"] = assessment.compensation_proposal_id
        if scenario == "E":
            child_id = identifiers["E_compensation"]
            store.approve(child_id, "synthetic-unknown-approver")
            execution.ActionExecutor(store).execute(child_id, "synthetic-unknown-executor")

    tokens = {role: secrets.token_urlsafe(36) for role in ("requester", "approver", "executor")}
    class FixtureAuthorization(WebAuthorization):
        def authenticate(self, request):
            operator = super().authenticate(request)
            # A test can revoke access after the browser has connected. This
            # only adds a denial; it cannot grant any additional permission.
            faults = json.loads(faults_path.read_text(encoding='utf-8'))
            if operator.actor in faults.get('denied_actors', []):
                raise HTTPException(403, 'operator_permission_denied')
            return operator

    auth = FixtureAuthorization(json.dumps([
        {"actor": "synthetic-" + role,
         "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
         "permissions": [permission]}
        for (role, token), permission in zip(tokens.items(), ("request", "approve", "execute"))
    ]))
    # Credentials are local, temporary and never included in URLs or the bundle.
    (root / "operators.json").write_text(json.dumps(tokens), encoding="utf-8")
    (root / "scenarios.json").write_text(json.dumps(identifiers, indent=2), encoding="utf-8")
    app = create_app(
        incident_path=incidents.file_path, runtime_path=runtime.path,
        remediation_path=store.file_path, authorization=auth,
    )

    @app.middleware('http')
    async def synthetic_transport_faults(request, call_next):
        # Test-only transport controls are a local TEMP file, never an API.
        faults = json.loads(faults_path.read_text(encoding='utf-8'))
        if request.method == faults.get('method') and request.url.path == faults.get('path'):
            delay = faults.get('delay_seconds', 0)
            if type(delay) in (int, float) and 0 <= delay <= 12:
                await asyncio.sleep(delay)
        return await call_next(request)

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="合成测试专用 Web API；不读取项目 data。")
    parser.add_argument("--port", type=int, default=18010)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("invalid_port")
    sys.dont_write_bytecode = True

    def isolation_guard(event, arguments):
        if event in {"socket.connect", "subprocess.Popen", "os.system"}:
            raise RuntimeError("synthetic_demo_forbids_outbound_connections_and_processes")

    # Windows builds its internal asyncio socket pair with a loopback connect.
    # Create that pair before denying every subsequent outbound connection.
    demo_loop = asyncio.new_event_loop()
    sys.addaudithook(isolation_guard)
    temporary = Path(tempfile.mkdtemp(prefix="incident-w1-demo-"))
    os.environ['INCIDENT_AGENT_LOG_PATH'] = str(temporary / 'agent.log')
    app = create_demo(temporary / "fixture")
    print(f"合成测试环境（非生产）：{temporary / 'fixture'}", flush=True)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False,
                access_log=False, loop=lambda: demo_loop)


if __name__ == "__main__":
    main()
