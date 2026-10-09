import json
import time
import re
from tools.tool_registry import tool_registry
from agent_logger.logger_config import logger

class ToolManager:
    def get_tool_schemas(self):
        return [info["schema"] for info in tool_registry.values()]
    
    @staticmethod
    def log_result(name, service, status, reason=None, duration=0, *, executed=False, cached=False, session_id=None):
        def safe(value):
            return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value) else "-"
        level = 40 if status == "tool_error" else (30 if not executed and not cached else 20)
        logger.log(level,
                   "工具结果 session=%s name=%s service=%s status=%s reason=%s duration=%.3fs executed=%s cached=%s",
                   safe(session_id), safe(name), safe(service), safe(status), safe(reason), duration, executed, cached)

    def run_tool(self,item, *, session_id=None):
        """The boolean reports Python execution; result.status decides evidence validity."""
        tool_info=tool_registry.get(item.name)
        if tool_info is None:
            result={
                "status":"tool_not_found",
                "message":f"没有找到工具：{item.name}"
            }
            output_item=self.build_output_item(item=item,result=result)
            self.log_result(item.name, None, result["status"], executed=False, session_id=session_id)
            return False,output_item,result
        try:
            arguments=json.loads(item.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("arguments_must_be_object")
        except (ValueError, TypeError):
                
            result={
                "status":"invalid_arguments",
                "message":f"工具{item.name}的参数格式错误"
            }
            output_item=self.build_output_item(item=item,result=result)
            self.log_result(item.name, None, result["status"], executed=False, session_id=session_id)
            return False,output_item,result
        tool_func=tool_info["func"]
        start_time=time.perf_counter()
        try:
            result=tool_func(**arguments)
        except Exception as e:
            cost_time=time.perf_counter()-start_time
            result={
                "status":"tool_error",
                "reason":type(e).__name__,
            }
            self.log_result(item.name, arguments.get("service", arguments.get("dependency")), result["status"],
                            result["reason"], cost_time, executed=True, session_id=session_id)

            output_item=self.build_output_item(item=item,result=result)
            return False,output_item,result
        output_item=self.build_output_item(item=item,result=result)
        cost_time=time.perf_counter()-start_time
        self.log_result(item.name, arguments.get("service", arguments.get("dependency")),
                        result.get("status", "invalid_tool_result") if isinstance(result, dict) else "invalid_tool_result",
                        result.get("reason") if isinstance(result, dict) else None,
                        cost_time, executed=True, session_id=session_id)
        return True,output_item,result

    def build_output_item(self,item,result):
        if isinstance(result,(dict,list)):
            output=json.dumps(
                result,
                ensure_ascii=False
            )
        else:
            output=str(result)
        return {
            "type":"function_call_output",
            "call_id":item.call_id,
            "output":output
        }
