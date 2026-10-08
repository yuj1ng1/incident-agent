import re
from datetime import datetime, timezone

import requests

from clients.service_client import ServiceClient
from services.registry import registry, CapabilityUnavailable
from tools.tool_registry import tool


@tool
def get_running_version(service: str) -> dict:
    """读取配置服务 /version 报告的运行 commit、UTC 进程启动时间和允许公开的开关。

    Args:
        service: ServiceRegistry 中已配置的服务名称
    """
    result = {
        "status": "no_data",
        "service": service,
    }
    try:
        settings = registry.require_capability(service, "runtime")
    except CapabilityUnavailable as exc:
        return {
            **result,
            "reason": exc.reason,
        }
    try:
        response = ServiceClient(settings.base_url).version(endpoint=settings.endpoint)
    except requests.RequestException:
        return {
            **result,
            "reason": "version_unreachable",
        }
    except ValueError:
        return {
            **result,
            "reason": "invalid_version_response",
        }
    if response["http_status"] != 200:
        return {
            **result,
            "reason": "version_http_error",
            "http_status": response["http_status"],
        }
    body = response["body"]
    if not isinstance(body, dict) or body.get("service") != service:
        return {
            **result,
            "reason": "invalid_version_response",
        }
    flags = body.get("runtime_flags")
    started = body.get("process_started_at")
    try:
        parsed = datetime.fromisoformat(started.replace("Z", "+00:00"))
        if (
            parsed.tzinfo is None
            or parsed.utcoffset() != timezone.utc.utcoffset(parsed)
        ):
            raise ValueError("process start must be UTC")
    except (ValueError, AttributeError, TypeError):
        return {
            **result,
            "reason": "invalid_process_started_at",
        }
    if (
        not isinstance(flags, dict)
        or any(
            type(flags.get(k)) is not bool
            for k in settings.allowed_flags
        )
    ):
        return {
            **result,
            "reason": "invalid_runtime_flags",
        }
    # allowlist 同样用于客户端，不透传服务误返回的敏感环境变量。
    result.update(
        {
            "process_started_at": started,
            "runtime_flags": {k: flags[k] for k in settings.allowed_flags},
            "commit_sha": None,
            "short_sha": None,
        }
    )
    sha = body.get("commit_sha")
    if (
        sha is None
        or sha == ""
        or sha == "unknown"
    ):
        return {
            **result,
            "reason": "commit_unknown",
        }
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        return {
            **result,
            "reason": "invalid_commit_sha",
        }
    return {
        **result,
        "status": "ok",
        "commit_sha": sha.lower(),
        "short_sha": sha[:7].lower(),
    }
