from tools.tool_manager import ToolManager
from agent_logger.logger_config import logger
from model_client.model_client import ModelClient
from models.incident import Evidence
from models.investigation import InvestigationResult,InvestigationStatus
from config import (
    MODEL_NAME,
    MAX_AGENT_LOOPS,
    MAX_TOOL_CALLS,
    MODEL_RETRIES
)
import json
class Agent:
    def __init__(self,client,agent_prompt):
        self.agent_prompt=agent_prompt
        self.history=[]
        self.tool_manager=ToolManager()
        self.max_loop_round=MAX_AGENT_LOOPS
        self.max_tool_calls=MAX_TOOL_CALLS
        self.model_retries=MODEL_RETRIES
        self.model_client=ModelClient(
            client=client,
            model_name=MODEL_NAME,
            retries=self.model_retries
            )

    def investigate(self,incident):
        self.history=[]
        tool_cache={}
        tool_call_count=0
        incident_prompt=self.build_incident_prompt(incident)
        self.history.append(
            {
                "role":"user",
                "content":incident_prompt
            }
        )

        for loop_num in range(1,self.max_loop_round+1):
            if tool_call_count>=self.max_tool_calls:
                available_tools=[]
            else:
                available_tools=self.tool_manager.get_tool_schemas()
            logger.info(
                f"Agent loop开始 "
                f"round={loop_num}/{self.max_loop_round}"
            )
            response=self.model_client.call_model(
                input_data=self.history,
                instructions=self.agent_prompt,
                tools=available_tools
            )

            response_status=getattr(response,"status",None)
            if response_status!="completed":
                logger.warning(
                    f"模型响应未正常完成 "
                    f"round={loop_num} "
                    f"status={response_status}"
                )

                return InvestigationResult(
                    success=False,
                    status=InvestigationStatus.MODEL_INCOMPLETE,
                    answer=f"模型响应未正常完成，status={response_status}"
                )

            function_calls=self.get_function_calls(response)
            if not function_calls:
                return self.handle_final_answer(response,loop_num)
            logger.info(
                f"模型请求调用工具 "
                f"round={loop_num} "
                f"count={len(function_calls)}"
            )
            self.history.extend(response.output)

            for item in function_calls:
                from_cache=False
                logger.info(
                    f"准备执行工具 "
                    f"name={item.name} "
                    f"call_id={item.call_id}"
                )
                (
                    tool_call_count,
                    success,
                    output_item,
                    result,
                    from_cache
                    )=self.handle_tool_call(
                        item=item,
                        tool_cache=tool_cache,
                        tool_call_count=tool_call_count
                        )
                self.history.append(output_item)
                self.add_evidence(
                    incident=incident,
                    item=item,
                    result=result,
                    success=success,
                    from_cache=from_cache
                )

            logger.info(
                f"Agent loop完成 "
                f"round={loop_num} "
                f"result=continue"
            )

        logger.warning(
            f"Agent达到最大循环轮数 "
            f"max_round={self.max_loop_round}"
        )

        return InvestigationResult(
                success=False,
                status=InvestigationStatus.MAX_ROUND_REACHED,
                answer="Agent已达到最大调查轮数，未正常完成调查"
            )


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

    历史调查结论：
    {incident.investigations}
    """

    def get_call_key(self,item):
        try:
            arguments=json.loads(item.arguments)

            args_key=json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True
            )

        except json.JSONDecodeError:
            args_key=item.arguments

        call_key=(item.name,args_key)

        return call_key,args_key

    def handle_tool_call(
        self,
        item,
        tool_cache,
        tool_call_count
    ):
        from_cache=False

        call_key,args_key=self.get_call_key(item)

        if call_key in tool_cache:
            result=tool_cache[call_key]

            output_item=self.tool_manager.build_output_item(item,result)

            success=True
            from_cache=True

            logger.info(
                f"命中工具缓存 "
                f"name={item.name} "
                f"args={args_key}"
            )

        elif tool_call_count>=self.max_tool_calls:
            result={
                "status":"tool_limit_reached",
                "message":"工具调用次数已达上限，请根据已有证据完成调查结论"
            }

            output_item=self.tool_manager.build_output_item(item,result)

            success=False

        else:
            tool_call_count+=1

            logger.info(
                f"工具调用计数 "
                f"count={tool_call_count}/{self.max_tool_calls} "
                f"name={item.name}"
            )

            success,output_item,result=self.tool_manager.run_tool(item)

            if success:
                tool_cache[call_key]=result

        return (
            tool_call_count,
            success,
            output_item,
            result,
            from_cache
        )

    def evidence_exists(
        self,
        incident,
        source,
        result
    ):
        new_detail=json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True
        )

        for evidence in incident.evidence:
            if evidence.source!=source:
                continue

            try:
                old_result=json.loads(
                    evidence.detail
                )

                old_detail=json.dumps(
                    old_result,
                    ensure_ascii=False,
                    sort_keys=True
                )

            except json.JSONDecodeError:
                old_detail=evidence.detail

            if old_detail==new_detail:
                return True

        return False

    def add_evidence(
        self,
        incident,
        item,
        result,
        success,
        from_cache
        ):
        if not success:
            return

        if from_cache:
            return

        if not isinstance(result,dict):
            return

        if result.get("status")!="ok":
            return

        if self.evidence_exists(
                incident=incident,
                source=item.name,
                result=result
            ):
            logger.info(
                f"跳过重复证据 "
                f"source={item.name}"
            )
            return

        evidence=Evidence(
            source=item.name,
            summary=f"{item.name}查询结果",
            detail=json.dumps(
                result,
                ensure_ascii=False
            )
        )

        incident.evidence.append(evidence)

    def get_function_calls(self,response):
        return [
            item
            for item in response.output
            if item.type=="function_call"
        ]

    def handle_final_answer(self,response,loop_num):
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

        return InvestigationResult(
                success=True,
                status=InvestigationStatus.COMPLETED,
                answer=answer
            )
