import os

import requests

from clients.database_client import DatabaseClient
from clients.service_client import ServiceClient


def probe_health(settings):
    if settings.provider == "http":
        try:
            response = ServiceClient(settings.base_url).health(
                endpoint=settings.endpoint
            )
        except (requests.RequestException, ValueError):
            return {
                "status": "no_data",
                "reason": "health_unreachable_or_invalid",
            }
        body = response["body"]
        if not isinstance(body, dict):
            return {
                "status": "no_data",
                "reason": "invalid_health_response",
            }
        return {
            "status": "ok",
            "health": body.get("health", "unknown"),
            "http_status": response["http_status"],
            "message": body.get("message", ""),
        }
    password = os.getenv(settings.password_env)
    if not password:
        return {
            "status": "no_data",
            "reason": "database_credentials_not_configured",
        }
    result = DatabaseClient(
        host=settings.host,
        port=settings.port,
        database=settings.database,
        user=settings.user,
        password=password,
    ).health()
    # Driver exception messages can contain connection credentials; keep only probe stages.
    safe = {
        k: result[k]
        for k in (
            "dns_resolved",
            "tcp_reachable",
            "database_connected",
            "query_ok",
            "failure_stage",
            "error_type",
        )
        if k in result
    }
    reachable = result["database_connected"] and result["query_ok"]
    return {
        "status": "ok",
        "health": "ok" if reachable else "unhealthy",
        "reachable": reachable,
        **safe,
    }
