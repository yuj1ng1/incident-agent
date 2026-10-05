from tools.tool_registry import tool

@tool
def query_metrics(service, metric):
    """
    查询指定服务的监控指标。

    Args:
        service: 需要查询指标的服务名称
        metric: 需要查询的指标名称
    """
    if service=="order-service":
        if metric=="cpu_usage":
            return "cpu_usage: 42%"
        elif metric=="db_connection_usage":
            return "db_connection_usage: 98%"
        elif metric=="http_5xx_rate":
            return "http_5xx_rate: 27%"

    return "没有找到对应指标"