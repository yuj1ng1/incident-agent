"""Isolated concurrent writers and a real process crash before projection ack."""
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from incident_store import IncidentStore
from investigation.models import InvestigationSession, SessionStatus as S
from investigation.worker import Worker
from runtime_support import FixtureAgent, create_runtime
from storage_lock import FileLockError


def main():
    root, mode, identity = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    if mode == "crash":
        manager = create_runtime(root, clock=lambda: datetime.fromisoformat(identity))
        original = manager.incident_store.apply_runtime_projection

        def project(session, checkpoint):
            outcome = original(session, checkpoint)
            if session.status == S.COMPLETED:
                os._exit(23)
            return outcome

        manager.incident_store.apply_runtime_projection = project
        Worker(manager, FixtureAgent()).run_once()
        raise AssertionError("crash boundary was not reached")

    store = IncidentStore(root / "incidents.json")
    incident = store.get_incident(identity)
    session = InvestigationSession(session_id="writer-session", incident_id=identity,
                                   alert_fingerprint=incident.fingerprint, service=incident.service)
    (root / ("ready-" + mode)).write_text("ready")
    deadline = time.monotonic() + 20
    while not (root / "release").exists():
        if time.monotonic() > deadline:
            raise TimeoutError("writer barrier")
        time.sleep(0.002)
    for index in range(30):
        if mode == "monitor":
            incident.alerts[-1].value = 1 + index
            incident.related_alerts = [{"writer": "monitor", "index": index}]
        record = {"observation_id": "writer-obs-" + str(index), "name": "query_metrics",
                  "success": True, "result": {"status": "ok", "value": index},
                  "collected_at": session.created_at.isoformat()}
        deadline = time.monotonic() + 10
        while True:
            try:
                if mode == "monitor":
                    store.save_observation(incident.fingerprint, incident)
                else:
                    store.apply_runtime_projection(session, {"records": [record]})
                break
            except FileLockError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.002)
    print(json.dumps({"writer": mode, "writes": 30}))


if __name__ == "__main__":
    main()
