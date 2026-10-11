"""Isolated subprocess oracle for recovery claims and late receipt writes."""
import json
import os
from pathlib import Path
import sys
import time
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from incident_store import IncidentStore
from models.incident import Evidence
from models.recovery import RecoveryReceipt
from recovery.verifier import RecoveryVerifier, recovery_policy
from storage_lock import FileLockError


def main():
    mode, path, incident_id, stamp = sys.argv[1:5]
    clock = lambda: datetime.fromisoformat(stamp)
    if mode == "hold_read":
        ready, release = map(Path, sys.argv[5:7])
        with open(path, "r", encoding="utf-8") as source:
            ready.write_text("ready", encoding="utf-8")
            deadline = time.monotonic() + 10
            while not release.exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError("fixture_release_timeout")
                time.sleep(0.01)
            value = json.load(source)["incidents"][incident_id]
            print(json.dumps({"old_snapshot_has_recovery": bool(value.get("recovery_verification"))}), flush=True)
        return
    for _ in range(100):
        try:
            store = IncidentStore(path, clock=clock)
            if mode in {"claim", "claim_exit"}:
                settings = RecoveryVerifier().settings("order-service")
                incident = store.claim_recovery_verification(incident_id, recovery_policy(settings))
                result = {"token": incident.recovery_verification.attempts[-1].token if incident else None}
            else:
                receipt = RecoveryReceipt.model_validate_json(Path(sys.argv[5]).read_text(encoding="utf-8"))
                evidence = Evidence(source="recovery_http", summary="fixture", detail=receipt.model_dump_json(),
                                    collected_at=receipt.completed_at, observation_id=receipt.claim_token)
                try:
                    accepted = store.record_recovery_verification(incident_id, [evidence], receipt.completed_at,
                                                                 claim_token=receipt.claim_token)
                    result = {"accepted": accepted}
                except ValueError:
                    result = {"accepted": False}
            print(json.dumps(result), flush=True)
            if mode == "claim_exit":
                os._exit(17)
            return
        except FileLockError:
            time.sleep(0.01)
    raise RuntimeError("fixture_lock_timeout")


if __name__ == "__main__":
    main()
