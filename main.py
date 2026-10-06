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


load_dotenv()


client=OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
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


monitor=IncidentMonitor(
    prometheus_client=prometheus_client,
    agent=agent,
    incident_store=incident_store,
    poll_interval=ALERT_POLL_INTERVAL_SECONDS
)


monitor.run()