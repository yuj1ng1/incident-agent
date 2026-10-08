"""Local operator application interface. Actor is audit data, not authentication."""
import argparse
import json
from pathlib import Path
import sqlite3

from pydantic import ValidationError

from incident_store import IncidentStore
from investigation.manager import InvestigationManager
from investigation.models import HumanCommand
from investigation.state import RuntimeErrorBase
from investigation.store import DEFAULT_STORE, RuntimeStore
from remediation.planner import RemediationPlanner
from remediation.store import RemediationStore, StoreError
from storage_lock import FileLockError


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地持久化调查管理；修复审批继续使用 remediation_cli.py")
    parser.add_argument("--store", default=str(DEFAULT_STORE))
    parser.add_argument("--incidents", default=str(Path(__file__).resolve().parent / "data" / "incidents.json"))
    parser.add_argument("--remediations", default=str(Path(__file__).resolve().parent / "data" / "remediation.json"))
    commands = parser.add_subparsers(dest="operation", required=True)
    for name in ("list-sessions", "list-human-requests", "list-pending-remediations"):
        commands.add_parser(name)
    for name in ("show-session", "show-events", "send-message", "pause", "resume", "cancel", "answer-human-request"):
        sub = commands.add_parser(name)
        sub.add_argument("session_id")
        if name == "show-events":
            sub.add_argument("--limit", type=int, default=100)
        if name not in {"show-session", "show-events"}:
            sub.add_argument("--actor", required=True, help="本地操作记录，不是真实身份认证")
            sub.add_argument("--command-id", help="重复使用相同 ID 可以安全重试")
            sub.add_argument("--expected-version", type=int)
        if name in {"send-message", "answer-human-request"}:
            sub.add_argument("--text", required=True)
        if name == "answer-human-request":
            sub.add_argument("--request-id", required=True)
    args = parser.parse_args(argv)
    try:
        runtime = RuntimeStore(args.store)
        manager = InvestigationManager(runtime, IncidentStore(args.incidents), RemediationPlanner(RemediationStore(args.remediations)))
        operation = args.operation
        if operation == "list-sessions":
            output = [s.model_dump(mode="json") for s in runtime.list_sessions()]
        elif operation == "show-session":
            output = manager.workflow(args.session_id)
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
