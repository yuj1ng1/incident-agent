import time

from agent_logger.logger_config import logger
from alert_parser import normalize_snapshot
from alert_correlation import correlate_alerts
from alert_recovery import can_clear, rules_are_current
from fingerprint import generate_alert_fingerprint
from incident_factory import create_incident_from_alert
from reinvestigation import should_reinvestigate


class IncidentMonitor:
    def __init__(
        self,
        prometheus_client,
        incident_store,
        poll_interval,
        investigation_manager,
        normalizer=normalize_snapshot,
        recovery_worker=None,
    ):
        self.prometheus_client = prometheus_client
        self.incident_store = incident_store
        self.poll_interval = poll_interval
        self.normalizer = normalizer
        self.last_snapshot = None
        self.investigation_manager = investigation_manager
        self.recovery_worker = recovery_worker

    def process_alerts_once(self):
        snapshot = self.normalizer(self.prometheus_client.get_alerts())
        self.last_snapshot = snapshot
        related = correlate_alerts(snapshot.alerts)
        current = set()
        for alert in snapshot.alerts:
            fingerprint = generate_alert_fingerprint(alert)
            if fingerprint in current:
                continue
            current.add(fingerprint)
            try:
                self.handle_alert(alert, fingerprint, related.get(fingerprint, []))
            except Exception:
                logger.exception("Single alert processing failed; other alerts continue")
                snapshot.complete = False
        # A pending signal still exceeds its threshold: do not call it recovered.
        for alert in snapshot.ignored:
            if alert.state == "pending":
                fingerprint = generate_alert_fingerprint(alert)
                current.add(fingerprint)
                incident = self.incident_store.get_active_incident(fingerprint, include_history=False)
                if incident is not None:
                    incident.alert_state = "pending"
                    self.incident_store.save_observation(fingerprint, incident)
        if not snapshot.complete:
            logger.warning("Untrusted/incomplete alert snapshot; clearing skipped")
            return snapshot
        inactive = [
            incident
            for incident in self.incident_store.active_incidents(include_history=False)
            if incident.fingerprint not in current
        ]
        rules = rules_are_current(self.prometheus_client) if inactive else {}
        eligible = set()
        for incident in inactive:
            if incident.alerts and can_clear(
                incident.alerts[-1], self.prometheus_client, rules
            ):
                eligible.add(incident.fingerprint)
        cleared = self.incident_store.resolve_inactive_incidents(
            current, eligible, expected_incidents={item.fingerprint: item for item in inactive},
        )
        for incident_id in cleared:
            logger.info(
                "Alert cleared; business recovery unverified incident=%s", incident_id
            )
            print(f"告警已解除；业务恢复尚未验证 incident={incident_id}")
        if self.recovery_worker is not None:
            self.recovery_worker.wake()
        return snapshot

    def handle_alert(self, alert, fingerprint, related=None):
        incident = self.incident_store.get_or_migrate_active(alert, fingerprint)
        if incident is None:
            incident = create_incident_from_alert(alert, fingerprint)
            logger.info(
                f"创建新 Incident incident_id={incident.incident_id} service={incident.service}"
            )
            investigate = True
        else:
            investigate = should_reinvestigate(incident, alert)
            # Keep the latest observation even while investigation is suppressed.
            if investigate:
                incident.alerts.append(alert)
            else:
                incident.alerts[-1] = alert
        incident.alert_state = alert.state
        incident.related_alerts = related or []
        self.incident_store.save_observation(fingerprint, incident)
        # The Manager owns the unified status/Policy/budget decision and records
        # refusals. Monitoring must not silently suppress a failed Session.
        self.investigation_manager.enqueue(incident, reinvestigate=True)

    def run(self):
        try:
            while True:
                logger.info("开始新一轮告警检查")
                try:
                    self.process_alerts_once()
                except Exception:
                    logger.exception("告警轮询失败")
                logger.info("本轮告警检查完成 next_check=%ss", self.poll_interval)
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            print("\nIncident Monitor 已停止")
