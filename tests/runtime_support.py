"""Isolated fixtures; no network or repair action is invoked."""
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import Mock

from openai.types.responses import Response

import tools
from agent.agent import Agent
from fingerprint import generate_alert_fingerprint
from incident_factory import create_incident_from_alert
from incident_store import IncidentStore
from investigation.manager import InvestigationManager
from investigation.models import RuntimeLimits
from investigation.store import RuntimeStore
from models.incident import Alert

SHA = "a" * 40


def response(answer="证据不足，尚未确认根因。", calls=(), status="completed"):
    output = list(calls)
    if answer:
        output.append({
            "type": "message", "id": "msg-fixture", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": answer, "annotations": []}],
        })
    return Response.model_validate({
        "id": "resp-fixture", "created_at": 0, "model": "fixture", "object": "response",
        "output": output, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "status": status,
    })


def call(name="query_metrics", arguments=None, call_id="call-fixture"):
    return {
        "type": "function_call", "id": "fc-" + call_id, "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments or {"service": "order-service", "metric": "http_5xx_rate"}),
    }


class FixtureAgent(Agent):
    def __init__(self, responses=None, tool_result=None):
        super().__init__(Mock(), "只读证据调查，不执行修复。")
        self.request_durable_response = Mock(side_effect=responses or [response()])
        self.executions = []
        self.tool_result = tool_result
        self.tool_manager.run_tool = self.run_fixture_tool

    def run_fixture_tool(self, item, *, session_id=None):
        self.executions.append(item.name)
        if self.tool_result is not None:
            try:
                result = self.tool_result(item)
            except Exception as exc:
                result = {"status": "tool_error", "reason": type(exc).__name__}
        elif item.name == "get_running_version":
            result = {
                "status": "ok", "service": "order-service", "commit_sha": SHA,
                "process_started_at": "2026-10-08T00:00:00Z",
                "runtime_flags": {"simulate_code_regression": False},
            }
        else:
            result = {"status": "ok", "value": 0.3, "service": json.loads(item.arguments).get("service")}
        success = result.get("status") not in {"tool_error", "tool_not_found", "invalid_arguments", "invalid_tool_result"}
        return success, self.tool_manager.build_output_item(item, result), result


def create_runtime(root, clock=None, limits=None):
    root = Path(root)
    incidents = IncidentStore(root / "incidents.json")
    store = RuntimeStore(root / "runtime.sqlite3", **({"clock": clock} if clock else {}))
    manager = InvestigationManager(store, incidents, limits=limits or RuntimeLimits())
    return manager


def create_session(manager, service="order-service", severity="critical", suffix=""):
    alert = Alert(service=service, severity=severity, metric="http_5xx_rate", value=0.3, threshold=0.2)
    fp = generate_alert_fingerprint(alert) + suffix
    incident = create_incident_from_alert(alert, fp)
    manager.incident_store.save_incident(fp, incident)
    return manager.enqueue(incident)


def remediation_agent(clock):
    """Real planner gates receive deterministic version/source/log/database observations."""
    from datetime import timedelta
    from remediation.planner import ERROR
    def result(item):
        now = clock()
        if item.name == "get_running_version":
            return {"status": "ok", "service": "order-service", "commit_sha": SHA,
                    "process_started_at": (now - timedelta(minutes=1)).isoformat(),
                    "runtime_flags": {"simulate_code_regression": True}}
        if item.name == "query_logs":
            return {"status": "ok", "service": "order-service", "logs": [{
                "timestamp_utc": now.isoformat(),
                "line": f'error_type=application_error exception_type=RuntimeError error_message="{ERROR}"',
            }]}
        if item.name == "get_source_file":
            return {"status": "ok", "service": "order-service", "repository": "yuj1ng1/prometheus-demo",
                    "path": "app.py", "ref": SHA, "truncated": False,
                    "content": 'def order():\n    if SIMULATE_CODE_REGRESSION:\n        raise RuntimeError("' + ERROR + '")\n'}
        return {"status": "ok", "dependency": "order-db", "reachable": True,
                "database_connected": True, "query_ok": True}
    calls = [call("query_logs", {"service": "order-service"}, "log"),
             call("get_source_file", {"service": "order-service", "path": "app.py", "ref": SHA}, "source"),
             call("check_dependency_health", {"dependency": "order-db"}, "dependency")]
    return FixtureAgent([response(answer="", calls=calls), response()], result)
