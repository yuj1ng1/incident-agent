"""Deterministic structured evidence gate; no text-to-action or extra model call."""
import ast
from datetime import datetime, timedelta, timezone
import json
import re

from models.remediation import RemediationProposal, Risk, utc_now

ERROR = "simulated application code regression"
REPOSITORY = "yuj1ng1/prometheus-demo"


def parse_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("UTC required")
    return parsed.astimezone(timezone.utc)


def code_supports_regression(content):
    try:
        tree = ast.parse(content)
    except (SyntaxError, TypeError, ValueError):
        return False
    for function in tree.body:
        if isinstance(function, ast.FunctionDef) and function.name == "order":
            for node in ast.walk(function):
                if (isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                        and node.test.id == "SIMULATE_CODE_REGRESSION"):
                    for statement in node.body:
                        if (isinstance(statement, ast.Raise) and isinstance(statement.exc, ast.Call)
                                and isinstance(statement.exc.func, ast.Name)
                                and statement.exc.func.id == "RuntimeError"
                                and len(statement.exc.args) == 1
                                and isinstance(statement.exc.args[0], ast.Constant)
                                and statement.exc.args[0].value == ERROR):
                            return True
    return False


class RemediationPlanner:
    def __init__(self, store):
        self.store = store

    def plan(self, incident, result, evidence=None):
        # Other monitoring signals/candidate relations cannot authorize this action.
        if (
            incident.alerts
            and incident.alerts[-1].alertname not in {"legacy", "HighHttp5xxRate"}
        ):
            return None
        if not result.success or result.status.value != "completed" or incident.service != "order-service":
            return None
        grouped = {}
        for item in incident.evidence if evidence is None else evidence:
            try:
                body = json.loads(item.detail)
            except (ValueError, TypeError):
                continue
            if isinstance(body, dict) and body.get("status") == "ok":
                grouped.setdefault(item.source, []).append(body)
        versions = grouped.get("get_running_version", [])
        if not versions:
            return None
        version = versions[-1]
        sha = version.get("commit_sha")
        if (version.get("service") != incident.service
                or version.get("runtime_flags", {}).get("simulate_code_regression") is not True
                or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)):
            return None
        try:
            started = parse_time(version["process_started_at"])
        except (ValueError, TypeError, KeyError, AttributeError):
            return None
        now = utc_now()
        log_ok = False
        for response in grouped.get("query_logs", []):
            if response.get("service") != incident.service:
                continue
            for log in response.get("logs", []):
                try:
                    stamp = parse_time(log["timestamp_utc"])
                    line = log["line"]
                    # Match complete fields, not substrings such as NotRuntimeError.
                    log_ok |= (max(started, now - timedelta(minutes=10)) <= stamp <= now
                        and re.search(r"(?:^|\s)error_type=application_error(?:\s|$)", line) is not None
                        and re.search(r"(?:^|\s)exception_type=RuntimeError(?:\s|$)", line) is not None
                        and ('error_message="' + ERROR + '"') in line)
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
        source_ok = any(
            body.get("service") == incident.service and body.get("repository") == REPOSITORY
            and body.get("path") == "app.py" and body.get("ref") == sha
            and body.get("truncated") is False and code_supports_regression(body.get("content"))
            for body in grouped.get("get_source_file", []))
        database_ok = any(body.get("dependency") == "order-db"
            and body.get("reachable") is True and body.get("database_connected") is True
            and body.get("query_ok") is True for body in grouped.get("check_dependency_health", []))
        if not (log_ok and source_ok and database_ok):
            return None
        return RemediationProposal(incident_id=incident.incident_id, service=incident.service,
            summary="关闭受控应用回归开关并重新启动 order-service",
            reason="当前运行开关、启动后的现场异常、当前版本源码和数据库可访问证据共同确认受控故障路径。",
            action_type="disable_demo_regression", parameters={"service": "order-service"},
            incident_fingerprint=incident.fingerprint,
            risk=Risk.LOW, expected_commit_sha=sha, expected_process_started_at=started,
            evidence_sources=["get_running_version", "query_logs", "get_source_file", "check_dependency_health"])

    def propose(self, incident, result, evidence=None):
        proposal = self.plan(incident, result, evidence)
        return self.store.create(proposal) if proposal else None
