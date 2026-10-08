import os

from dotenv import load_dotenv
from openai import OpenAI

import tools

from agent.agent import Agent
from clients.prometheus_client import PrometheusClient
from config import (
    PROMETHEUS_URL,
    ALERT_POLL_INTERVAL_SECONDS
)
from incident_store import IncidentStore
from monitor.incident_monitor import IncidentMonitor
from prompts.agent_prompt import agent_prompt
from remediation.planner import RemediationPlanner
from remediation.store import RemediationStore
from investigation.store import RuntimeStore
from investigation.manager import InvestigationManager
from investigation.worker import Worker
from investigation.scheduler import Scheduler


load_dotenv()


client=OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
    timeout=60,
    max_retries=0
)


agent=Agent(
    client=client,
    agent_prompt=agent_prompt
)

prometheus_client=PrometheusClient(
    base_url=PROMETHEUS_URL
)

incident_store=IncidentStore(
    "data/incidents.json"
)


runtime_manager = InvestigationManager(
    RuntimeStore(),
    incident_store,
    RemediationPlanner(RemediationStore()),
)
scheduler = Scheduler(Worker(runtime_manager, agent))

monitor=IncidentMonitor(
    prometheus_client=prometheus_client,
    agent=agent,
    incident_store=incident_store,
    poll_interval=ALERT_POLL_INTERVAL_SECONDS,
    remediation_planner=runtime_manager.remediation_planner,
    investigation_manager=runtime_manager,
)


scheduler.start()
try:
    monitor.run()
finally:
    scheduler.stop()
