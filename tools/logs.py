from tools.tool_registry import tool

@tool
def query_logs(service:str)->str:
    """
    查询指定服务的近期日志，用于排查服务异常。

    Args:
        service: 需要查询日志的服务名称
    """
    if service=="order-service":
        return """
        15:01:12 ERROR database connection timeout
        15:01:13 ERROR failed to acquire database connection
        15:01:14 ERROR request returned 500
        """

    return "No relevant error logs found."