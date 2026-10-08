import time
from datetime import datetime, timezone

from agent_logger.logger_config import logger
from alert_parser import normalize_snapshot
from alert_correlation import correlate_alerts
from alert_recovery import can_clear, rules_are_current
from fingerprint import generate_alert_fingerprint
from incident_factory import create_incident_from_alert
from models.investigation import InvestigationRecord
from reinvestigation import should_reinvestigate


class IncidentMonitor:
    def __init__(
        self,
        prometheus_client,
        agent,
        incident_store,
        poll_interval,
        remediation_planner=None,
        normalizer=normalize_snapshot,
        investigation_manager=None,
    ):
        self.prometheus_client = prometheus_client
        self.agent = agent
        self.incident_store = incident_store
        self.poll_interval = poll_interval
        self.remediation_planner = remediation_planner
        self.normalizer = normalizer
        self.last_snapshot = None
        self.model_blocked_reason = None
        self.investigation_manager = investigation_manager

    def process_alerts_once(self):
        snapshot = self.normalizer(self.prometheus_client.get_alerts())
        self.last_snapshot = snapshot
        related = correlate_alerts(snapshot.alerts)
        current = set()
        cache_started = time.monotonic()
        # One bounded read-only cache per poll, discarded before the next poll.
        if hasattr(self.agent, "poll_tool_cache"):
            self.agent.poll_tool_cache = {}
        try:
            for alert in snapshot.alerts:
                # Serialized investigations may take minutes; do not reuse an old
                # poll observation in a later incident after thirty seconds.
                if time.monotonic() - cache_started > 30:
                    if hasattr(self.agent, "poll_tool_cache"):
                        self.agent.poll_tool_cache = {}
                    cache_started = time.monotonic()
                fingerprint = generate_alert_fingerprint(alert)
                if fingerprint in current:
                    continue
                current.add(fingerprint)
                try:
                    self.handle_alert(alert, fingerprint, related.get(fingerprint, []))
                except Exception:
                    logger.exception(
                        "Single alert processing failed; other alerts continue"
                    )
                    snapshot.complete = False
        finally:
            if hasattr(self.agent, "poll_tool_cache"):
                self.agent.poll_tool_cache = None
        # A pending signal still exceeds its threshold: do not call it recovered.
        for alert in snapshot.ignored:
            if alert.state == "pending":
                fingerprint = generate_alert_fingerprint(alert)
                current.add(fingerprint)
                incident = self.incident_store.get_active_incident(fingerprint)
                if incident is not None:
                    incident.alert_state = "pending"
                    if self.investigation_manager is not None:
                        self.incident_store.save_observation(fingerprint, incident)
                    else:
                        self.incident_store.save_incident(fingerprint, incident)
        if not snapshot.complete:
            logger.warning("Untrusted/incomplete alert snapshot; clearing skipped")
            return snapshot
        inactive = [
            incident
            for incident in self.incident_store.active_incidents()
            if incident.fingerprint not in current
        ]
        rules = rules_are_current(self.prometheus_client) if inactive else {}
        eligible = set()
        for incident in inactive:
            if incident.alerts and can_clear(
                incident.alerts[-1], self.prometheus_client, rules
            ):
                eligible.add(incident.fingerprint)
        cleared = self.incident_store.resolve_inactive_incidents(current, eligible)
        for incident_id in cleared:
            logger.info(
                "Alert cleared; business recovery unverified incident=%s", incident_id
            )
            print(f"告警已解除；业务恢复尚未验证 incident={incident_id}")
        return snapshot

    def handle_alert(self, alert, fingerprint, related=None):
        incident = self.incident_store.get_or_migrate_active(alert, fingerprint)
        if incident is None:
            incident = create_incident_from_alert(alert, fingerprint)
            logger.info(
                f"创建新 Incident incident={incident.incident_id} service={incident.service}"
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
        if self.model_blocked_reason:
            incident.investigation_suspended_reason = self.model_blocked_reason
            investigate = False
        if self.investigation_manager is not None:
            self.incident_store.save_observation(fingerprint, incident)
        else:
            self.incident_store.save_incident(fingerprint, incident)
        if investigate:
            if self.investigation_manager is not None:
                self.investigation_manager.enqueue(incident, reinvestigate=True)
            else:
                self.investigate_incident(incident, alert, fingerprint)

    def investigate_incident(self, incident, alert, fingerprint):
        evidence_before = len(incident.evidence)
        incident.last_attempted_at = datetime.now(timezone.utc)
        self.incident_store.save_incident(fingerprint, incident)
        try:
            result = self.agent.investigate(incident)
        except Exception as exc:
            incident.consecutive_investigation_failures += 1
            if getattr(exc, "status_code", None) in {401, 402, 403}:
                self.model_blocked_reason = f"model_api_http_{exc.status_code}"
                incident.investigation_suspended_reason = self.model_blocked_reason
            logger.exception(
                "Incident investigation failed incident=%s", incident.incident_id
            )
            self.incident_store.save_incident(fingerprint, incident)
            return
        investigated_at = datetime.now(timezone.utc)
        incident.investigations.append(
            InvestigationRecord(
                investigated_at=investigated_at,
                status=result.status,
                alert_value=alert.value,
                answer=result.answer,
                model_rounds=result.model_rounds,
                tool_calls=result.tool_calls,
                tool_trace=result.tool_trace,
            )
        )
        if result.success:
            incident.consecutive_investigation_failures = 0
            incident.last_investigated_at = investigated_at
            incident.last_investigated_value = alert.value
            incident.last_investigated_state = alert.state
        else:
            incident.consecutive_investigation_failures += 1
            logger.warning(
                "Incident 调查未正常完成 incident=%s status=%s",
                incident.incident_id,
                result.status.value,
            )
        print(result.answer)
        self.incident_store.save_incident(fingerprint, incident)
        if result.success and self.remediation_planner is not None:
            try:
                proposal = self.remediation_planner.propose(
                    incident,
                    result,
                    incident.evidence[evidence_before:],
                )
                if proposal is not None:
                    print(
                        f"Remediation proposal={proposal.proposal_id} "
                        f"incident={proposal.incident_id} status={proposal.status.value} "
                        f"action={proposal.action_type} risk={proposal.risk.value}\n"
                        f"{proposal.summary}\n{proposal.reason}\n"
                        "等待独立 CLI 人工审批；Monitor 不执行修复。"
                    )
            except Exception:
                logger.warning("Remediation 规划或存储失败；调查已保存，Monitor 继续运行")

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
