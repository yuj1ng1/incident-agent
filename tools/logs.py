from tools.tool_registry import tool
from clients.loki_client import LokiClient
from config import LOKI_URL
from datetime import datetime,timezone


def timestamp_to_utc(timestamp):
    # 用整数拆分，避免 Unix 纳秒经 float 转换产生精度损失。
    seconds,nanoseconds=divmod(int(timestamp),1_000_000_000)
    prefix=datetime.fromtimestamp(seconds,timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{prefix}.{nanoseconds:09d}Z" if nanoseconds else f"{prefix}Z"

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
                    "timestamp_utc":timestamp_to_utc(timestamp),
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
