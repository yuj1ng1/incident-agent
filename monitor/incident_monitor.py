import time
from datetime import datetime

from agent_logger.logger_config import logger
from alert_parser import parse_prometheus_alert
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
        poll_interval
    ):
        self.prometheus_client=prometheus_client#依赖注入
        self.agent=agent
        self.incident_store=incident_store
        self.poll_interval=poll_interval

    def process_alerts_once(self):
        response=self.prometheus_client.get_alerts()

        if (
            not isinstance(response,dict)
            or response.get("status")!="success"
        ):
            logger.warning(
                "Prometheus 告警响应无效，本轮跳过告警处理和恢复判断"
            )
            return

        data=response.get("data")
        if (
            not isinstance(data,dict)
            or "alerts" not in data
            or not isinstance(data["alerts"],list)
        ):
            logger.warning(
                "Prometheus 告警快照结构不完整，本轮跳过告警处理和恢复判断"
            )
            return

        raw_alerts=data["alerts"]

        current_fingerprints=set()

        for raw_alert in raw_alerts:
            alert=parse_prometheus_alert(
                raw_alert
            )

            if alert is None:
                continue

            fingerprint=generate_alert_fingerprint(
                alert
            )

            current_fingerprints.add(
                fingerprint
            )

            self.handle_alert(
                alert=alert,
                fingerprint=fingerprint
            )

        resolved_incidents=(
            self.incident_store.resolve_inactive_incidents(
                current_fingerprints
            )
        )

        for incident_id in resolved_incidents:
            logger.info(
                f"Incident 已恢复 "
                f"incident={incident_id}"
            )

            print(
                f"告警已经恢复，Incident 已标记为 resolved "
                f"incident={incident_id}"
            )

    def handle_alert(
        self,
        alert,
        fingerprint
    ):
        incident=self.incident_store.get_active_incident(
            fingerprint
        )

        if incident is not None:
            if not should_reinvestigate(
                incident,
                alert
            ):
                logger.info(
                    f"Incident 暂不需要重新调查 "
                    f"incident={incident.incident_id}"
                )

                return

            logger.info(
                f"Incident 开始重新调查 "
                f"incident={incident.incident_id}"
            )

            incident.alerts.append(
                alert
            )

        else:
            incident=create_incident_from_alert(
                alert,
                fingerprint
            )

            logger.info(
                f"创建新 Incident "
                f"incident={incident.incident_id} "
                f"service={incident.service}"
            )

        self.incident_store.save_incident(
            fingerprint,
            incident
        )

        self.investigate_incident(
            incident=incident,
            alert=alert,
            fingerprint=fingerprint
        )

    def investigate_incident(
        self,
        incident,
        alert,
        fingerprint
    ):
        try:
            result=self.agent.investigate(
                incident
            )

        except Exception as e:
            logger.exception(
                f"Incident 调查异常 "
                f"incident={incident.incident_id} "
                f"error={e}"
            )

            self.incident_store.save_incident(
                fingerprint,
                incident
            )

            return

        investigated_at=datetime.now()

        record=InvestigationRecord(
            investigated_at=investigated_at,
            status=result.status,
            alert_value=alert.value,
            answer=result.answer
        )

        incident.investigations.append(
            record
        )

        if not result.success:
            logger.warning(
                f"Incident 调查未正常完成 "
                f"incident={incident.incident_id} "
                f"status={result.status.value}"
            )

            print(result.answer)

            self.incident_store.save_incident(
                fingerprint,
                incident
            )

            return

        print(result.answer)

        incident.last_investigated_at=investigated_at
        incident.last_investigated_value=alert.value

        self.incident_store.save_incident(
            fingerprint,
            incident
        )
    def run(self):
        try:
            while True:
                logger.info(
                    "开始新一轮告警检查"
                )

                try:
                    self.process_alerts_once()

                except Exception as e:
                    logger.exception(
                        f"告警轮询失败 "
                        f"error={e}"
                    )

                logger.info(
                    f"本轮告警检查完成 "
                    f"next_check={self.poll_interval}s"
                )

                time.sleep(
                    self.poll_interval
                )

        except KeyboardInterrupt:
            print(
                "\nIncident Monitor 已停止"
            )
