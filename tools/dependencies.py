from config import SERVICE_DEPENDENCIES,DEPENDENCIES
from tools.tool_registry import tool
from clients.database_client import DatabaseClient

@tool
def get_service_dependencies(service:str):
    """
    获取指定服务依赖的下游组件。

    service: 服务名称
    """

    dependencies=SERVICE_DEPENDENCIES.get(service)

    if dependencies is None:
        return {
            "status":"no_data",
            "service":service,
            "message":"没有找到该服务的依赖关系"
        }
    return {
        "status":"ok",
        "service":service,
        "dependencies":dependencies
    }

@tool
def check_dependency_health(dependency:str):
    """
    检查指定依赖当前是否可访问并能够正常响应。

    dependency: 依赖组件名称
    """

    config=DEPENDENCIES.get(dependency)

    if config is None:
        return {
            "status":"no_data",
            "dependency":dependency,
            "message":"没有找到该依赖的访问配置"
        }
    if config["type"]!="database":
        return {
            "status":"no_data",
            "dependency":dependency,
            "message":"当前暂不支持该依赖类型"
        }
    if  config["engine"]!="postgresql":
        return {
            "status":"no_data",
            "dependency":dependency,
            "message":"当前暂不支持该数据库类型"
        }
    client=DatabaseClient(
        host=config["host"],
        port=config["port"],
        database=config["database"],
        user=config["user"],
        password=config["password"]
    )
 
    healthy=client.health()
    reachable=(
        healthy["database_connected"] and healthy["query_ok"]
    )

    return {
            "status":"ok",
            "dependency":dependency,
            "dependency_type":"database",
            "reachable":reachable,
            **healthy
        }