"""Production composition and lifecycle; importing this module is inert."""


def main():
    import os
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv()

    # Load configuration and register read-only tools after the environment.
    from openai import OpenAI
    import tools
    from agent.agent import Agent
    from clients.prometheus_client import PrometheusClient
    from config import PROMETHEUS_URL, ALERT_POLL_INTERVAL_SECONDS
    from incident_store import IncidentStore
    from monitor.incident_monitor import IncidentMonitor
    from prompts.agent_prompt import agent_prompt
    from remediation.planner import RemediationPlanner
    from remediation.store import RemediationStore
    from investigation.store import RuntimeStore
    from investigation.manager import InvestigationManager, live_alert_for_incident
    from investigation.worker import Worker
    from investigation.scheduler import Scheduler
    from recovery.worker import RecoveryWorker

    with OpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
        timeout=60,
        max_retries=0,
    ) as client:
        agent = Agent(client=client, agent_prompt=agent_prompt)
        prometheus_client = PrometheusClient(base_url=PROMETHEUS_URL)
        incident_store = IncidentStore(Path(__file__).resolve().parent / "data" / "incidents.json")
        runtime_manager = InvestigationManager(
            RuntimeStore(),
            incident_store,
            RemediationPlanner(RemediationStore()),
            alert_verifier=lambda incident: live_alert_for_incident(prometheus_client, incident),
        )
        scheduler = Scheduler(Worker(runtime_manager, agent))
        recovery_worker = RecoveryWorker(incident_store)
        monitor = IncidentMonitor(
            prometheus_client=prometheus_client,
            incident_store=incident_store,
            poll_interval=ALERT_POLL_INTERVAL_SECONDS,
            investigation_manager=runtime_manager,
            recovery_worker=recovery_worker,
        )
        scheduler.start()
        try:
            recovery_worker.start()
            monitor.run()
        finally:
            recovery_worker.stop()
            scheduler.stop()


if __name__ == "__main__":
    main()
