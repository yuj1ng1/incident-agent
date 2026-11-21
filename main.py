"""Production composition and lifecycle; importing this module is inert."""


def main():
    from contextlib import nullcontext
    import os

    from dotenv import load_dotenv

    load_dotenv()

    # Load configuration and register read-only tools after the environment.
    from openai import OpenAI
    import tools
    from agent.agent import Agent
    from alert_parser import normalize_snapshot
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

    mode = os.getenv("INCIDENT_AGENT_MODEL_MODE", "deepseek")
    if mode == "demo_replay":
        from model_client.demo_replay import DemoReplay

        model_client = nullcontext(DemoReplay())
        input_token_counter = lambda body: 0
    elif mode == "deepseek":
        from model_client.token_counter import build_deepseek_input_token_counter

        input_token_counter = build_deepseek_input_token_counter()
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise SystemExit("deepseek_api_key_required")
        model_client = OpenAI(
            api_key=api_key,
            base_url="https://api.deepseek.com",
            timeout=60,
            max_retries=0,
        )
    else:
        raise SystemExit("invalid_incident_agent_model_mode")

    with model_client as client:
        agent = Agent(
            client=client,
            agent_prompt=agent_prompt,
            input_token_counter=input_token_counter,
        )
        prometheus_client = PrometheusClient(base_url=PROMETHEUS_URL)
        incident_store = IncidentStore()
        runtime_manager = InvestigationManager(
            RuntimeStore(),
            incident_store,
            RemediationPlanner(RemediationStore()),
            alert_verifier=lambda incident: live_alert_for_incident(prometheus_client, incident),
        )
        scheduler = Scheduler(Worker(runtime_manager, agent))
        recovery_worker = RecoveryWorker(incident_store)
        normalizer = (
            (
                lambda response: normalize_snapshot(
                    response,
                    default_origin="demo_replay",
                    default_origin_note=(
                        "本地 Compose 演示回放环境（demo_replay），"
                        "由受控故障触发告警并使用离线回放模型"
                    ),
                )
            )
            if mode == "demo_replay"
            else normalize_snapshot
        )
        monitor = IncidentMonitor(
            prometheus_client=prometheus_client,
            incident_store=incident_store,
            poll_interval=ALERT_POLL_INTERVAL_SECONDS,
            investigation_manager=runtime_manager,
            normalizer=normalizer,
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
