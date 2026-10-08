from services.health_providers import probe_health
from services.registry import registry, CapabilityUnavailable
from tools.tool_registry import tool


@tool
def check_service_health(service: str) -> dict:
    """查询指定服务的健康状态，按已配置 health provider 探测。

    Args:
        service: 需要检查健康状态的服务名称
    """
    try:
        settings = registry.require_capability(service, "health")
    except CapabilityUnavailable as exc:
        return {
            "status": "no_data",
            "service": service,
            "reason": exc.reason,
        }
    return {
        "service": service,
        **probe_health(settings),
    }
