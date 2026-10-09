"""Crash only this test child; effects and stores are confined to its temporary root."""
import os
from pathlib import Path
import sys
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.remediation import RemediationProposal, VerificationResult, utc_now
from remediation.executor import ActionExecutor
from remediation.state import RemediationError
from remediation.store import RemediationStore


if __name__ == "__main__":
    root = Path(sys.argv[1])
    phase = sys.argv[2]
    store = RemediationStore(root / "remediation.json")
    if phase == "hold-read":
        original = Path.read_text

        def hold(path, *args, **kwargs):
            if path != store.file_path:
                return original(path, *args, **kwargs)
            with path.open(encoding="utf-8") as handle:
                (root / "reader-ready").touch()
                sys.stdin.readline()
                return handle.read()

        with patch.object(Path, "read_text", hold):
            store.load()
        print("reader-completed", flush=True)
        raise SystemExit(0)
    if phase == "approve":
        (root / ("writer-ready-" + sys.argv[3])).touch()
        sys.stdin.readline()
        try:
            store.approve(sys.argv[4], "test-writer-" + sys.argv[3])
        except RemediationError as exc:
            print(str(exc), flush=True)
            raise SystemExit(3)
        raise SystemExit(0)
    if phase not in {"executing_committed", "action_returned", "action_committed"}:
        raise SystemExit("unsupported_test_phase")
    proposal = store.create(RemediationProposal(
        incident_id="test-crash", service="order-service", summary="test", reason="test evidence",
        action_type="disable_demo_regression", parameters={"service": "order-service"}, risk="low",
        expected_commit_sha="a" * 40, expected_process_started_at=utc_now(),
    ))
    store.approve(proposal.proposal_id, "test-approver")
    runtime = Mock()
    runtime.client.version.return_value = {
        "service": "order-service", "commit_sha": proposal.expected_commit_sha,
        "process_started_at": proposal.expected_process_started_at.isoformat(),
        "runtime_flags": {"simulate_code_regression": True},
    }
    runtime.listener.return_value = {"pid": 123}  # No native process API is used.
    runtime.stop.side_effect = lambda identity: (root / "test-effect").write_text("executed", encoding="utf-8")
    verifier = Mock()
    verifier.verify.side_effect = lambda p: VerificationResult(
        proposal_id=p.proposal_id, success=True, checks={}, summary="test observation",
    )

    def crash(actual_phase, proposal_id):
        if actual_phase == phase:
            (root / "callback-reached").write_text(actual_phase, encoding="utf-8")
            os._exit(73)

    with patch("remediation.executor.SERVICE_LOCK", root / "service.lock"):
        ActionExecutor(store, runtime, verifier, boundary_hook=crash).execute(proposal.proposal_id, "test-executor")
    raise SystemExit("crash_callback_not_reached")
