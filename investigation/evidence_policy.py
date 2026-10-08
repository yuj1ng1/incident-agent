"""Only collected ok results can be current evidence; human input stays separate."""
from datetime import datetime
import hashlib
import json
import re

from investigation.models import utc_now
from services.registry import registry

TTL = {
    "query_metrics": 15,
    "check_service_health": 15,
    "check_dependency_health": 15,
    "get_running_version": 15,
    "query_logs": 30,
    "get_recent_code_changes": 60,
    "get_service_dependencies": 300,
    "get_service_capabilities": 300,
}


def stable(name, arguments):
    ref = arguments.get("ref") if name == "get_source_file" else arguments.get("commit_sha")
    return name in {"get_source_file", "get_commit_diff"} and bool(
        isinstance(ref, str) and re.fullmatch(r"[0-9a-fA-F]{40}", ref)
    )


def cache_key(service, name, arguments, identity=None):
    profile = registry.get(service)
    dependency = registry.get(arguments.get("dependency"))
    config = [
        profile.model_dump(mode="json") if profile else None,
        dependency.model_dump(mode="json") if dependency else None,
    ]
    content = [service, name, arguments, config, None if stable(name, arguments) else identity]
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()


def fresh(record, now=None):
    now = now or utc_now()
    if record.get("result", {}).get("status") != "ok" or not record.get("success"):
        return False
    try:
        collected = datetime.fromisoformat(record["collected_at"].replace("Z", "+00:00"))
        age = (now - collected).total_seconds()
    except (ValueError, TypeError, KeyError):
        return False
    if age < 0:
        return False
    return stable(record["name"], record["arguments"]) or age <= TTL.get(record["name"], 0)


def effective_records(records, identity=None, now=None):
    return [
        record for record in records
        if fresh(record, now)
        and (
            stable(record["name"], record["arguments"])
            or record.get("identity") == identity
        )
    ]
