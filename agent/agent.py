from tools.tool_manager import ToolManager
from agent_logger.logger_config import logger
from model_client.model_client import ModelClient
from models.incident import Evidence
class Agent:
    def __init__(self,client,agent_prompt):
        self.agent_prompt=agent_prompt
        self.history=[]
        self.tool_mananger=ToolManager()
        self.max_loop_round=5
        self.model_retries=3
        self.model_client=ModelClient(
            client=client,
            model_name="deepseek-flash",
            retries=self.model_retries
            )

    def investigate(self,incident):
        self.history=[]
        incident_prompt=self.build_incident_prompt(incident)
        self.history.append(
            {
                "role":"user",
                "content":incident_prompt
            }
        )

        for loop_num in range(1,self.max_loop_round+1):
            logger.info(
                f"Agent loop开始 "
                f"round={loop_num}/{self.max_loop_round}"
            )
            response=self.model_client.call_model(
                input_data=self.history,
                instructions=self.agent_prompt,
                tools=self.tool_mananger.get_tool_schemas()
            )

            function_calls=[item 
                            for item in response.output 
                            if item.type=="function_call"
                            ]
            if not function_calls:
                answer=response.output_text
                logger.info(
                    f"Agent loop完成 "
                    f"round={loop_num} "
                    f"result=final_answer"
                )
                self.history.append(
                    {
                    "role":"assistant",
                    "content":answer
                    }
                )
                return answer
            logger.info(
                f"模型请求调用工具 "
                f"round={loop_num} "
                f"count={len(function_calls)}"
            )
            self.history.extend(response.output)

            for item in function_calls:
                logger.info(
                    f"准备执行工具 "
                    f"name={item.name} "
                    f"call_id={item.call_id}"
                )
                success,output_item,result=self.tool_mananger.run_tool(item)
                self.history.append(output_item)
                if success:
                    evidence=Evidence(
                        source=item.name,
                        summary=f"{item.name}查询结果",
                        detail=str(result)
                    )
                    incident.evidence.append(evidence)
            logger.info(
                f"Agent loop完成 "
                f"round={loop_num} "
                f"result=continue"
            )

        logger.warning(
            f"Agent达到最大循环轮数 "
            f"max_round={self.max_loop_round}"
        )

        return "agent已达到最大轮数"


    def build_incident_prompt(self,incident):
        return f"""
    当前故障：

    Incident ID：{incident.incident_id}
    服务：{incident.service}
    严重程度：{incident.severity}
    状态：{incident.status}

    告警：
    {incident.alerts}

    已有证据：
    {incident.evidence}
    """