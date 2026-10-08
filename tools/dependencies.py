from services.health_providers import probe_health
from services.registry import registry, CapabilityUnavailable
from tools.tool_registry import tool


@tool
def get_service_dependencies(service: str) -> dict:
    """获取已注册服务依赖的下游组件，空列表表示没有配置依赖。

    Args:
        service: 服务名称
    """
    try:
        names = registry.dependencies(service)
    except CapabilityUnavailable as exc:
        return {
            "status": "no_data",
            "service": service,
            "reason": exc.reason,
        }
    return {
        "status": "ok",
        "service": service,
        "dependencies": [
            {
                "name": name,
                "type": "database"
                if registry.get(name).type == "postgresql"
                else registry.get(name).type,
            }
            for name in names
        ],
    }


@tool
def check_dependency_health(dependency: str) -> dict:
    """探测已注册依赖，PostgreSQL 保留 DNS/TCP/连接/SELECT 1 分阶段证据。

    Args:
        dependency: 依赖组件名称
    """
    try:
        settings = registry.require_capability(dependency, "health")
    except CapabilityUnavailable as exc:
        return {
            "status": "no_data",
            "dependency": dependency,
            "reason": exc.reason,
        }
    result = probe_health(settings)
    if settings.provider == "http" and result.get("status") == "ok":
        result["reachable"] = result["http_status"] == 200 and result["health"] == "ok"
    return {
        "dependency": dependency,
        "dependency_type": "database" if settings.provider == "postgresql" else "http",
        **result,
    }
