"""Independent operator commands; never block the Monitor with input()."""
import argparse
import getpass
import json
import sys

def main(argv=None):
    from dotenv import load_dotenv
    load_dotenv()
    from remediation.executor import ActionExecutor
    from remediation.state import RemediationError
    from remediation.store import DEFAULT_STORE, RemediationStore
    from models.remediation import ExecutionOutcome, RemediationStatus
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Human-approved local Demo remediation")
    parser.add_argument("--store", default=str(DEFAULT_STORE), help="Local operator-selected JSON store")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="默认列出全部非终态和需要人工处理的记录")
    filters = listing.add_mutually_exclusive_group()
    filters.add_argument("--all", action="store_true")
    filters.add_argument("--status", choices=[s.value for s in RemediationStatus])
    for name in ("show", "approve", "reject", "execute", "inspect-execution", "close-execution", "retry-proposal"):
        command = commands.add_parser(name)
        command.add_argument("proposal_id")
        if name in {"approve", "reject", "inspect-execution", "close-execution", "retry-proposal"}:
            command.add_argument("--by", required=True, help="Actual human operator identity")
        if name in {"reject", "close-execution", "retry-proposal"}:
            command.add_argument("--reason", required=True)
        if name == "close-execution":
            command.add_argument("--command-id", required=True, help="相同 ID、操作者和理由可安全重试收口")
    args = parser.parse_args(argv)
    store = RemediationStore(args.store)
    try:
        if args.command == "list":
            proposals = store.list(args.status) if args.all or args.status else store.attention()
            output = [item.model_dump(mode="json") for item in proposals]
        elif args.command == "show":
            data = store.load()
            proposal = store.from_data(data, args.proposal_id)
            output = {"proposal": proposal.model_dump(mode="json"),
                **{key: [item for item in data[key] if item["proposal_id"] == args.proposal_id]
                   for key in ("actions", "verifications", "audit")}}
            output["execution_outcome"] = store.execution_outcome(data, proposal).value
        elif args.command == "approve":
            output = store.approve(args.proposal_id, args.by).model_dump(mode="json")
        elif args.command == "reject":
            output = store.reject(args.proposal_id, args.by, args.reason).model_dump(mode="json")
        elif args.command == "inspect-execution":
            output = ActionExecutor(store).inspect_execution(args.proposal_id, args.by)
        elif args.command == "close-execution":
            output = ActionExecutor(store).close_execution(args.proposal_id, args.by, args.reason, args.command_id)
        elif args.command == "retry-proposal":
            output = ActionExecutor(store).retry_proposal(args.proposal_id, args.by, args.reason).model_dump(mode="json")
        else:
            action = ActionExecutor(store).execute(args.proposal_id, getpass.getuser())
            proposal = store.get(args.proposal_id)
            verification_performed = action.details.get("verification_performed", False)
            verification = None
            if verification_performed:
                verification = next((item for item in store.load()["verifications"]
                                     if item["verification_id"] == action.details.get("verification_id")
                                     and item["proposal_id"] == args.proposal_id), None)
                if verification is None:
                    raise RemediationError("current_verification_record_missing")
            historical = action.status == "already_completed" and proposal.status == RemediationStatus.SUCCEEDED
            output = {
                "action": action.model_dump(mode="json"), "proposal_status": proposal.status.value,
                "execution_outcome": action.execution_outcome.value,
                "action_performed": (None if action.execution_outcome == ExecutionOutcome.UNKNOWN else
                                     action.execution_outcome == ExecutionOutcome.EXECUTED),
                "verification_performed": verification_performed,
                "verification": verification,
                "historical_proposal_succeeded": historical,
                "current_target_verified": verification["success"] if verification else None,
                "target_already_remediated": bool(action.status == "already_remediated" and verification and verification["success"]),
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))
            # Zero means the approved workflow is fulfilled, including a historical idempotent skip.
            fulfilled = proposal.status == RemediationStatus.SUCCEEDED and (
                historical or (action.success and verification is not None and verification["success"])
            )
            return 0 if fulfilled else 1
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except RemediationError as exc:
        print(json.dumps({"success": False, "status": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
