from tools.tool_registry import tool
from clients.loki_client import LokiClient
from config import LOKI_URL

loki_client=LokiClient(
    base_url=LOKI_URL
)

@tool
def query_logs(service:str)->dict:
    """
    查询指定服务的近期日志。

    Args:
        service: 需要查询日志的服务名称
    """

    logql=f'{{service="{service}"}}'

    response=loki_client.query_range(
        logql=logql,
        limit=50
    )

    streams=response.get(
        "data",{}
    ).get(
        "result",[]
    )

    if not streams:
        return {
            "status":"no_data",
            "service":service,
            "logs":[]
        }

    logs=[]

    for stream in streams:
        labels=stream.get(
            "stream",{}
        )

        for timestamp,line in stream.get(
            "values",[]
        ):
            logs.append(
                {
                    "timestamp":timestamp,
                    "line":line,
                    "labels":labels
                }
            )

    logs.sort(
        key=lambda log:int(log["timestamp"]),
        reverse=True
    )

    return {
        "status":"ok",
        "service":service,
        "logs":logs[:50],
        "logql":logql
    }