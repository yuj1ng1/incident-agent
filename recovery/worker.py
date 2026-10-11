"""Two bounded background consumers reuse IncidentStore's durable claims."""
import logging
import threading

from models.incident import Evidence
from recovery.verifier import RecoveryVerifier, recovery_policy
from services.registry import CapabilityUnavailable
from storage_lock import FileLockError

logger = logging.getLogger(__name__)


class RecoveryWorker:
    def __init__(self, incident_store, verifier=None, *, interval=1, workers=2):
        if not 1 <= workers <= 4 or interval <= 0:
            raise ValueError("recovery_worker_limits_invalid")
        self.incident_store = incident_store
        self.verifier = verifier or RecoveryVerifier()
        self.interval = interval
        self.workers = workers
        self.stop_requested = threading.Event()
        self.wakeup = threading.Event()
        self.threads = []

    def run_once(self):
        for incident_id in self.incident_store.recovery_candidates():
            if self.stop_requested.is_set():
                return None
            current = self.incident_store.get_incident(incident_id, include_history=False)
            if current is None:
                continue
            try:
                policy = recovery_policy(self.verifier.settings(current.service))
            except CapabilityUnavailable:
                policy = None
            try:
                incident = self.incident_store.claim_recovery_verification(incident_id, policy)
            except FileLockError:
                return None
            if incident is None:
                continue
            token = incident.recovery_verification.attempts[-1].token
            try:
                receipt = self.verifier.verify(incident)
            except Exception as exc:
                # Never log exception text: network/JSON errors can contain secrets.
                logger.warning("Business recovery probe failed category=%s", type(exc).__name__)
                self.incident_store.finish_recovery_verification(incident_id, token)
            else:
                if receipt.outcome == "success":
                    evidence = Evidence(
                        source="recovery_http", summary="注册业务恢复探测",
                        detail=receipt.model_dump_json(), collected_at=receipt.completed_at,
                        observation_id=token,
                    )
                    try:
                        self.incident_store.record_recovery_verification(
                            incident_id, [evidence], receipt.completed_at, claim_token=token,
                        )
                    except ValueError:
                        self.incident_store.finish_recovery_verification(incident_id, token, reason="invalid_evidence")
                else:
                    self.incident_store.finish_recovery_verification(incident_id, token, receipt=receipt)
            return incident_id
        return None

    def wake(self):
        self.wakeup.set()

    def _run(self):
        while not self.stop_requested.is_set():
            try:
                processed = self.run_once()
            except (OSError, FileLockError, ValueError, KeyError) as exc:
                logger.warning("Recovery state retained; retry deferred category=%s", type(exc).__name__)
                processed = None
            except Exception as exc:
                logger.error("Recovery worker failed category=%s", type(exc).__name__)
                processed = None
            if processed is None:
                self.wakeup.wait(self.interval)
                self.wakeup.clear()

    def start(self):
        if self.threads:
            raise RuntimeError("recovery_worker_already_started")
        for number in range(self.workers):
            thread = threading.Thread(target=self._run, name=f"business-recovery-{number}", daemon=False)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        self.stop_requested.set()
        self.wake()
        for thread in self.threads:
            thread.join()
