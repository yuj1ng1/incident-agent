"""Disposable real-process crash fixture; parent verifies and cleans owned apps."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from remediation.executor import ActionExecutor
from remediation.isolated_demo import IsolatedDemoVerifier
from scripts.w2_demo import create_demo


def main():
    root, port, phase = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    app, runtime, store = create_demo(root, port)
    executor = ActionExecutor(store, runtime=runtime, verifier=IsolatedDemoVerifier(runtime))
    store.approve("rem-w2-deploy-b", "w2-deploy-approver")
    executor.execute("rem-w2-deploy-b", "w2-executor")
    child = next(p for p in store.list() if p.compensation)
    store.approve(child.proposal_id, "w2-rollback-approver")
    (root / "crash-target.json").write_text(json.dumps({
        "proposal_id": child.proposal_id, "phase": phase,
    }), encoding="utf-8")

    def crash(boundary, identifier):
        if boundary == phase:
            os._exit(73)

    executor.boundary_hook = crash
    if phase == "after_stop_before_start":
        # This call is reached only after the real B process has stopped.
        runtime.start = lambda *args, **kwargs: os._exit(73)
    executor.execute(child.proposal_id, "w2-executor")
    raise AssertionError("crash_boundary_not_reached")


if __name__ == "__main__":
    main()
