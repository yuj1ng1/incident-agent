"""Local operator application interface. Actor is audit data, not authentication."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

def main(argv=None):
    from dotenv import load_dotenv
    load_dotenv()
    from pydantic import ValidationError
    from incident_store import IncidentStore
    from investigation.manager import InvestigationManager, live_alert_for_incident
    from investigation.models import HumanCommand
    from investigation.state import RuntimeErrorBase
    from investigation.store import DEFAULT_STORE, RuntimeStore
    from remediation.planner import RemediationPlanner
    from remediation.store import RemediationStore, StoreError
    from storage_lock import FileLockError
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="本地持久化调查管理；修复审批继续使用 remediation_cli.py")
    parser.add_argument("--store", default=str(DEFAULT_STORE))
    parser.add_argument("--incidents", default=str(Path(__file__).resolve().parent / "data" / "incidents.json"))
    parser.add_argument("--remediations", default=str(Path(__file__).resolve().parent / "data" / "remediation.json"))
    commands = parser.add_subparsers(dest="operation", required=True)
    unblock = commands.add_parser("unblock-model", help="修正认证/余额问题后显式解除全局模型阻塞；不恢复已取消 Session")
    unblock.add_argument("--actor", required=True)
    for name in ("list-sessions", "list-human-requests", "list-pending-remediations"):
        commands.add_parser(name)
    incident_view = commands.add_parser("show-incident", help="查询完整调查与 Evidence 历史，包括已归档内容")
    incident_view.add_argument("incident_id")
    for name in ("show-session", "show-events", "send-message", "pause", "resume", "cancel", "answer-human-request", "reopen"):
        sub = commands.add_parser(name)
        sub.add_argument("session_id")
        if name == "show-events":
            sub.add_argument("--limit", type=int, default=100)
        if name not in {"show-session", "show-events"}:
            sub.add_argument("--actor", required=True, help="本地操作记录，不是真实身份认证")
            sub.add_argument("--command-id", required=name == "reopen", help="重复使用相同 ID 可以安全重试")
            sub.add_argument("--expected-version", type=int)
        if name in {"send-message", "answer-human-request"}:
            sub.add_argument("--text", required=True)
        if name == "answer-human-request":
            sub.add_argument("--request-id", required=True)
        if name == "reopen":
            sub.add_argument("--reason", required=True, help="明确重新授权调查预算的理由；不授权任何修复动作")
    args = parser.parse_args(argv)
    try:
        runtime = RuntimeStore(args.store)
        from clients.prometheus_client import PrometheusClient
        from config import PROMETHEUS_URL
        manager = InvestigationManager(
            runtime, IncidentStore(args.incidents), RemediationPlanner(RemediationStore(args.remediations)),
            alert_verifier=lambda incident: live_alert_for_incident(PrometheusClient(PROMETHEUS_URL), incident),
        )
        operation = args.operation
        if operation == "list-sessions":
            output = [s.model_dump(mode="json") for s in runtime.list_sessions()]
        elif operation == "show-session":
            output = manager.workflow(args.session_id)
        elif operation == "show-incident":
            incident = manager.incident_store.get_incident(args.incident_id)
            if incident is None:
                raise RuntimeErrorBase("incident_not_found")
            output = incident.model_dump(mode="json")
        elif operation == "unblock-model":
            output = manager.unblock_model(args.actor)
        elif operation == "show-events":
            output = runtime.events(args.session_id, args.limit)
        elif operation == "list-human-requests":
            output = [r.model_dump(mode="json") for r in manager.requests()]
        elif operation == "list-pending-remediations":
            output = [p.model_dump(mode="json") for p in manager.remediation_planner.store.list("pending")]
        else:
            kind = {
                "send-message": "message",
                "answer-human-request": "answer",
            }.get(operation, operation)
            payload = {}
            if hasattr(args, "text"):
                payload["text"] = args.text
            if hasattr(args, "request_id"):
                payload["request_id"] = args.request_id
            if hasattr(args, "reason"):
                payload["reason"] = args.reason
            fields = {
                "session_id": args.session_id,
                "actor": args.actor,
                "command_type": kind,
                "payload": payload,
                "expected_version": args.expected_version,
            }
            if args.command_id:
                fields["command_id"] = args.command_id
            command = manager.submit(HumanCommand(**fields))
            output = command.model_dump(mode="json")
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 2 if command.processing_status == "rejected" else 0
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (RuntimeErrorBase, ValidationError, sqlite3.Error, ValueError, OSError, FileLockError, StoreError) as exc:
        # Validation errors may contain user payload; print the class/code only.
        code = str(exc) if isinstance(exc, (RuntimeErrorBase, StoreError, FileLockError)) else type(exc).__name__
        print(json.dumps({
            "success": False,
            "error": code,
        }, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
