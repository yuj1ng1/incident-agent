"""Independent operator commands; never block the Monitor with input()."""
import argparse
import getpass
import json

from remediation.executor import ActionExecutor
from remediation.state import RemediationError
from remediation.store import DEFAULT_STORE, RemediationStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="Human-approved local Demo remediation")
    parser.add_argument("--store", default=str(DEFAULT_STORE), help="Local operator-selected JSON store")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    for name in ("show", "approve", "reject", "execute"):
        command = commands.add_parser(name)
        command.add_argument("proposal_id")
        if name in {"approve", "reject"}:
            command.add_argument("--by", required=True, help="Actual human operator identity")
        if name == "reject":
            command.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    store = RemediationStore(args.store)
    try:
        if args.command == "list":
            output = [item.model_dump(mode="json") for item in store.list("pending")]
        elif args.command == "show":
            data = store.load()
            output = {"proposal": store.get(args.proposal_id).model_dump(mode="json"),
                **{key: [item for item in data[key] if item["proposal_id"] == args.proposal_id]
                   for key in ("actions", "verifications", "audit")}}
        elif args.command == "approve":
            output = store.approve(args.proposal_id, args.by).model_dump(mode="json")
        elif args.command == "reject":
            output = store.reject(args.proposal_id, args.by, args.reason).model_dump(mode="json")
        else:
            action = ActionExecutor(store).execute(args.proposal_id, getpass.getuser())
            proposal = store.get(args.proposal_id)
            output = {"action": action.model_dump(mode="json"), "proposal_status": proposal.status.value}
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0 if action.success and proposal.status.value == "succeeded" else 1
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except RemediationError as exc:
        print(json.dumps({"success": False, "status": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
