"""Crash an isolated compensation fixture; only its temporary file is changed."""
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compensation_support import fixture_contract
from remediation.executor import ActionExecutor
from remediation.store import RemediationStore

if __name__ == "__main__":
    root = Path(sys.argv[1])
    store = RemediationStore(root / "remediation.json")
    proposal = store.get(sys.argv[2])
    contract = fixture_contract(proposal.compensation.source_identity, effect_path=root / "effects")
    # A restarted test process must register the identical contract, including digest.
    def crash(phase, proposal_id):
        if phase == sys.argv[3]:
            os._exit(73)
    with patch("remediation.compensation.COMPENSATION_REGISTRY", {
        (proposal.service, contract.original_action_type): contract,
    }), patch("remediation.executor.SERVICE_LOCK", root / "service.lock"):
        ActionExecutor(store, boundary_hook=crash).execute(proposal.proposal_id, "fixture-child")
