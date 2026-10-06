import json
import time
from tools.tool_registry import tool_registry
from agent_logger.logger_config import logger

class ToolManager:
    def get_tool_schemas(self):
        return [info["schema"] for info in tool_registry.values()]
    
    def run_tool(self,item):
        tool_info=tool_registry.get(item.name)
        if tool_info is None:
            result={
                "status":"tool_not_found",
                "message":f"没有找到工具：{item.name}"
            }
            output_item=self.build_output_item(item=item,result=result)
            return False,output_item,result
        try:
            arguments=json.loads(item.arguments)
        except json.JSONDecodeError as e:
            logger.error(
                f"工具参数解析失败 "
                f"name={item.name} "
                f"arguments={item.arguments} "
                f"error={e}"
                )
                
            result={
                "status":"invalid_arguments",
                "message":f"工具{item.name}的参数格式错误"
            }
            output_item=self.build_output_item(item=item,result=result)
            return False,output_item,result
        tool_func=tool_info["func"]
        start_time=time.perf_counter()
        try:
            result=tool_func(**arguments)
        except Exception as e:
            cost_time=time.perf_counter()-start_time
            logger.error(
                f"工具执行失败 "
                f"name={item.name} "
                f"args={arguments} "
                f"time={cost_time:.2f}s "
                f"error={e}"
            )
            result={
                "status":"tool_error",
                "message":f"工具{item.name}执行失败：{e}"
            }

            output_item=self.build_output_item(item=item,result=result)
            return False,output_item,result
        output_item=self.build_output_item(item=item,result=result)
        cost_time=time.perf_counter()-start_time
        logger.info(
            f"工具调用成功 "
            f"name={item.name} "
            f"args={arguments} "
            f"time={cost_time:.2f}s"
        )
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