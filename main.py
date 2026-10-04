from models.incident import Alert,Incident,Evidence
from tools.logs import query_logs
from tools.metrics import query_metrics
from openai import OpenAI
from dotenv import load_dotenv
from prompts import agent_prompt
from tools.tool_registry import tool_registry
from tools.tool_manager import ToolManager
import os
import json

tool_manager=ToolManager()
tools=tool_manager.get_tool_schemas()

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
    base_url="https://api.deepseek.com"
)
history=[]

while True:
    user_prompt=input("请输入")
    history.append(
    {
        "role":"user",
        "content":user_prompt
    }
)
    for loop_num in range(1,6):
        response=client.responses.create(
            model="deepseek-flash",
            instructions=agent_prompt,
            input=history,
            tools=tools
        )
        
        function_calls=[item for item in response.output if item.type=="function_call"]
        if not function_calls:
            ans=response.output_text
            history.append(
                {
                    "role":"assistant",
                    "content":ans
                }
            )
            print(ans)
            break
        history.extend(response.output)
        for item in function_calls:
                result=tool_manager.run_tool(item)
                output_item={
                "type": "function_call_output",
                "call_id": item.call_id,
                "output": str(result)
                }
                history.append(output_item)
