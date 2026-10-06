import math
from tools.tool_registry import tool
from clients.prometheus_client import PrometheusClient
from config import PROMETHEUS_URL


prometheus_client=PrometheusClient(
    base_url=PROMETHEUS_URL
)


@tool
def query_metrics(service:str,metric:str)->dict:
    """
    查询指定服务的监控指标。

    Args:
        service: 需要查询指标的服务名称
        metric: 需要查询的指标名称，目前支持 http_requests_total、http_request_rate 和 http_5xx_rate
    """

    promql_map={
        "http_request_rate":
            (
                f'sum(rate('
                f'demo_http_requests_total{{'
                f'service="{service}"'
                f'}}[1m]))'
            ),
        "http_requests_total":
            (
                f'sum('
                f'demo_http_requests_total{{service="{service}"}}'
                f')'
            ),

        "http_5xx_rate":
            (
                f'sum(rate('
                f'demo_http_requests_total{{'
                f'service="{service}",status="500"'
                f'}}[1m]))'
                f'/'
                f'sum(rate('
                f'demo_http_requests_total{{'
                f'service="{service}"'
                f'}}[1m]))'
            )
    }

    promql=promql_map.get(metric)

    if promql is None:
        return {
            "status":"no_data",
            "service":service,
            "metric":metric,
            "message":"当前还不支持该指标"
        }

    response=prometheus_client.query(promql)

    results=response.get(
        "data",{}
    ).get(
        "result",[]
    )

    if not results:
        return {
            "status":"no_data",
            "service":service,
            "metric":metric
        }

    value=float(
        results[0]["value"][1]
    )

    if not math.isfinite(value):
        return {
            "status":"no_data",
            "service":service,
            "metric":metric,
            "message":"Prometheus 返回的指标值不是有限数值"
        }

    return {
        "status":"ok",
        "service":service,
        "metric":metric,
        "value":value,
        "promql":promql
    }
