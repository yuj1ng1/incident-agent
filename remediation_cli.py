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
    from models.remediation import RemediationStatus
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
            output = {"action": action.model_dump(mode="json"), "proposal_status": proposal.status.value}
            output["verification_performed"] = action.status != "already_completed" and action.success
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0 if proposal.status.value == "succeeded" and (action.success or action.status == "already_completed") else 1
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except RemediationError as exc:
        print(json.dumps({"success": False, "status": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
