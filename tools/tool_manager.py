import json
from tool_registry import tool_registry

class ToolManager:
    def get_tool_schemas(self):
        return [info["schema"] for info in tool_registry.values()]
    
    def run_tool(self,item):
        tool_info=tool_registry.get(item.name)
        if tool_info is None:
            return f"没有找到工具：{item.name}"
        arguments=json.loads(item.arguments)
        tool_func=tool_info["func"]
        result=tool_func(**arguments)
        output_item={
            "type": "function_call_output",
            "call_id": item.call_id,
            "output": str(result)
        }
        return output_item