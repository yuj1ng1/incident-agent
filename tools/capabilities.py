from services.registry import registry
from tools.tool_registry import tool


@tool
def get_service_capabilities(service: str) -> dict:
    """只读查询已注册服务的观测能力、支持指标与证据缺口，不返回连接凭据。

    Args:
        service: Incident 对应服务或已注册依赖名称
    """
    return registry.describe(service)
