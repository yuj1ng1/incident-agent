from tools.tool_registry import tool
from clients.service_client import ServiceClient
from config import SERVICE_URLS

@tool
def check_service_health(service:str)->dict:
    """
    查询指定服务当前的健康状态。

    Args:
        service: 需要检查健康状态的服务名称
    """
    base_url=SERVICE_URLS.get(service)
    if base_url is None:
        return {
            "status":"no_data",
            "service":service,
            "message":"没有找到该服务的访问地址"
        }
    client=ServiceClient(base_url=base_url)
    result=client.health()
    body=result["body"]
    return {
        "status":"ok",
        "service":service,
        "health":body.get(
            "health",
            "unknown"
        ),
        "http_status":result["http_status"],
        "message":body.get(
            "message",
            ""
        )
    }
