import json
import math
from tools.tool_registry import tool
from clients.prometheus_client import PrometheusClient
from config import PROMETHEUS_URL,MONITORING_TARGETS


prometheus_client=PrometheusClient(
    base_url=PROMETHEUS_URL
)


def build_postgres_promql(service:str,metric:str):
    """集中校验 PostgreSQL 监控目标，返回 (PromQL, 错误说明)。"""
    target=MONITORING_TARGETS.get(service)
    if target is None:
        return None,"没有找到该服务的监控目标配置"
    if not isinstance(target,dict) or target.get("type")!="postgresql":
        return None,"该服务不是postgresql监控目标"

    job=target.get("job")
    database=target.get("database")
    if not isinstance(job,str) or not job.strip():
        return None,"PostgreSQL 监控目标缺少有效的 job 配置"
    if not isinstance(database,str) or not database.strip():
        return None,"PostgreSQL 监控目标缺少有效的 database 配置"

    # 将配置值转成带引号的标签字符串，同时转义引号和反斜杠。
    job_filter=f'job={json.dumps(job,ensure_ascii=False)}'
    database_filter=f'{job_filter},datname={json.dumps(database,ensure_ascii=False)}'

    promql_map={
        "postgres_up":f'pg_up{{{job_filter}}}',
        "postgres_connections":
            f'sum(pg_stat_database_numbackends{{{database_filter}}})',
        "postgres_max_connections":f'pg_settings_max_connections{{{job_filter}}}',
        "postgres_connection_usage":(
            f'sum by (job, instance) ('
            f'pg_stat_database_numbackends{{{job_filter},datname!=""}}'
            f') / on (job, instance) '
            f'pg_settings_max_connections{{{job_filter}}}'
        ),
        "postgres_active_sessions":
            f'sum(pg_stat_activity_count{{{database_filter},state="active"}})',
        "postgres_idle_sessions":
            f'sum(pg_stat_activity_count{{{database_filter},state="idle"}})',
        "postgres_idle_in_transaction":
            f'sum(pg_stat_activity_count{{{database_filter},state="idle in transaction"}})',
        # 只在数据库可达且已有 activity 样本时补 0；采集不到数据仍是空向量。
        "postgres_lock_waits":(
            f'(sum by (job, instance) ('
            f'pg_stat_activity_count{{{database_filter},wait_event_type="Lock"}}'
            f') or on (job, instance) (0 * sum by (job, instance) ('
            f'pg_stat_activity_count{{{database_filter}}}'
            f'))) and on (job, instance) (pg_up{{{job_filter}}} == 1)'
        )
    }

    promql=promql_map.get(metric)
    if promql is None:
        return None,"当前还不支持该指标"
    return promql,None


@tool
def query_metrics(service:str,metric:str)->dict:
    """
    查询指定服务的 HTTP 或 PostgreSQL 监控指标，返回单个数值。

    Args:
        service: 需要查询指标的服务或依赖名称，PostgreSQL 目标从 MONITORING_TARGETS 读取
        metric: HTTP 支持 http_requests_total、http_request_rate、http_5xx_rate；PostgreSQL 支持 postgres_up、postgres_connections、postgres_max_connections、postgres_connection_usage、postgres_active_sessions、postgres_idle_sessions、postgres_idle_in_transaction、postgres_lock_waits
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

    if metric.startswith("postgres_"):
        promql,error=build_postgres_promql(service,metric)
    else:
        promql=promql_map.get(metric)
        error=None if promql is not None else "当前还不支持该指标"

    if error is not None:
        return {
            "status":"no_data",
            "service":service,
            "metric":metric,
            "message":error
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

    # 实例级指标不能把多个实例的值随意合并，也不能静默取第一条。
    if len(results)!=1:
        return {
            "status":"no_data",
            "service":service,
            "metric":metric,
            "promql":promql,
            "message":"Prometheus 返回多个结果，无法确定单个指标值，请检查监控目标和 PromQL"
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
