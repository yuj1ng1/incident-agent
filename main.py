from models.incident import Alert,Incident,Evidence
from tools.logs import query_logs
from tools.metrics import query_metrics
from tools.tool_registry import tool_registry
from openai import OpenAI
from dotenv import load_dotenv
from prompts import agent_prompt
from agent.agent import Agent
import os
import json



alert=Alert(
    service="order-service",
    metric="http_5xx_rate",
    value=0.27,
    threshold=0.20,
    severity="critical"
)
evidence=Evidence(
    source = "logs",
    summary = "发现数据库连接超时",
    detail = "database connection timeout",
)
incident=Incident(
    incident_id="inc-001",
    service="order-service",
    severity="critical",
    status="investigating",
    alerts=[alert],
    evidence=[evidence]
)


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

while True:
    user_prompt=input("你:")
    if user_prompt.lower() in ["exit","esc","quit"]:
        break
    result=agent.investigate(incident)
    print(result)

for evidence in incident.evidence:
    print(evidence)