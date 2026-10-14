"""Only collected ok results can be current evidence; human input stays separate."""
from datetime import datetime
import hashlib
import json
import re

from clients.prometheus_client import evaluation_is_fresh
from investigation.models import utc_now
from services.registry import registry

TTL = {
    "query_metrics": 15,
    # Collection TTL permits reuse of this exact window, never current health.
    "query_metric_range": 30,
    "check_service_health": 15,
    "check_dependency_health": 15,
    "check_scrape_target": 15,
    "get_running_version": 15,
    "query_logs": 30,
    "get_recent_code_changes": 60,
    "get_service_dependencies": 300,
    "get_service_capabilities": 300,
    # Mutable refs (including short SHAs) expire; full SHAs are handled by stable().
    "get_source_file": 60,
    "get_commit_diff": 60,
}


def stable(name, arguments):
    ref = arguments.get("ref") if name == "get_source_file" else arguments.get("commit_sha")
    return name in {"get_source_file", "get_commit_diff"} and bool(
        isinstance(ref, str) and re.fullmatch(r"[0-9a-fA-F]{40}", ref)
    )


def cache_key(service, name, arguments, identity=None):
    profile = registry.get(service)
    target_name = arguments.get("dependency")
    if target_name is None and arguments.get("service", service) != service:
        target_name = arguments["service"]
    target = registry.get(target_name)
    config = [
        profile.model_dump(mode="json") if profile else None,
        target.model_dump(mode="json") if target else None,
    ]
    content = [service, name, arguments, config, None if stable(name, arguments) else identity]
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()


def fresh(record, now=None):
    now = now or utc_now()
    if (
        record.get("name") not in TTL
        or record.get("result", {}).get("status") != "ok"
        or not record.get("success")
    ):
        return False
    try:
        collected = datetime.fromisoformat(record["collected_at"].replace("Z", "+00:00"))
        age = (now - collected).total_seconds()
    except (ValueError, TypeError, KeyError):
        return False
    if age < 0:
        return False
    if record["name"] == "query_metrics":
        if record["result"].get("evidence_scope") == "time_window":
            return False
        # Saving/replaying a result cannot renew the Prometheus observation.
        # Legacy results remain readable, but cannot prove current metric state.
        try:
            evaluated = datetime.fromisoformat(
                record["result"]["evaluated_at"].replace("Z", "+00:00")
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            return False
        if not evaluation_is_fresh(evaluated, now):
            return False
    return stable(record["name"], record["arguments"]) or age <= TTL[record["name"]]


def effective_records(records, identity=None, now=None, service=None):
    """Current-state evidence, distinct from cacheable immutable reference material."""
    return [
        record for record in records
        if fresh(record, now)
        # A newly fetched alert window remains evidence about that interval.
        # Its collection TTL permits caching, not a claim about current health.
        and not (
            record["name"] == "query_metric_range"
            or record["result"].get("evidence_scope") == "time_window"
            or (
                record["name"] == "query_logs" and (
                    record["arguments"].get("start") is not None
                    or record["arguments"].get("end") is not None
                )
            )
        )
        and (
            (
                stable(record["name"], record["arguments"])
                and ((identity or {}).get("commit_sha") or "").lower() == (
                    record["arguments"].get("ref") if record["name"] == "get_source_file"
                    else record["arguments"].get("commit_sha")
                ).lower()
            )
            or (
                record["name"] not in {"get_source_file", "get_commit_diff"}
                and record.get("identity") == identity
            )
        )
        and (service is None or record.get("cache_key") == cache_key(
            service, record["name"], record["arguments"], record.get("identity")
        ))
    ]
