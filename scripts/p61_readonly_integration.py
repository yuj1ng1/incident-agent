"""P6.1 offline integration of the existing read-only investigation runtime.

Only HTTP response bodies and the SDK object are fixtures. Monitor, Agent,
ModelClient, ToolManager, providers, Worker, Manager and stores are real code.
No service, database, model API or recovery process is started or contacted.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
from unittest.mock import Mock, patch
from urllib.parse import urlsplit


SCENARIOS = (
    "offline_ok",
    "metric_zero",
    "metrics_no_data",
    "prometheus_unavailable",
    "loki_unavailable",
    "permissions",
    "tool_budget",
    "model_budget",
    "model_failure",
    "model_incomplete",
)
MOCK_ANSWER = (
    "[模拟模型决策，待人工审查]\n"
    "本轮只验证告警、工具解析、预算及持久化流程。所有外部响应均为离线夹具。\n"
    "健康探测结果只说明该路由返回的状态，业务状态未知。\n"
    "指标缺失和来源不可用不能解释为零或健康；近期日志不是告警窗口取证。\n"
    "尚未确认根因、真实模型推理能力或业务恢复；未执行修复。"
)


class BoundaryViolation(BaseException):
    """Escape broad application Exception handlers before forbidden I/O."""


def _write_json(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def _call(name, arguments, identifier):
    return {
        "type": "function_call",
        "id": "fc-" + identifier,
        "call_id": identifier,
        "name": name,
        "arguments": json.dumps(arguments),
    }


def _response(calls=(), *, answer="", status="completed"):
    from openai.types.responses import Response

    output = list(calls)
    if answer:
        output.append({
            "type": "message",
            "id": "msg-p61",
            "role": "assistant",
            "status": "completed",
            "content": [{
                "type": "output_text",
                "text": answer,
                "annotations": [],
            }],
        })
    return Response.model_validate({
        "id": "resp-p61-mock",
        "created_at": 0,
        "model": "p61-scripted-fixture",
        "object": "response",
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "status": status,
    })


def _script(scenario, replay=False):
    from config import MAX_AGENT_LOOPS, MAX_TOOL_CALLS

    calls = [
        _call("query_metrics", {
            "service": "order-service",
            "metric": "http_5xx_rate",
        }, "metric"),
        _call("query_logs", {"service": "order-service"}, "logs"),
        _call("check_service_health", {"service": "order-service"}, "health"),
    ]
    if scenario == "permissions":
        calls += [
            _call("execute_remediation", {"service": "order-service"}, "repair"),
            _call("check_service_health", {
                "service": "inventory-service",
            }, "foreign-target"),
        ]
    elif scenario == "tool_budget":
        # Unsupported metrics are no_data, hence never successful cache hits.
        calls = [
            _call("query_metrics", {
                "service": "order-service",
                "metric": "unsupported_p61_" + str(index),
            }, "budget-" + str(index))
            for index in range(MAX_TOOL_CALLS + 1)
        ]
    elif scenario == "model_budget":
        return [
            _response([_call("execute_remediation", {}, "round-" + str(index))])
            for index in range(MAX_AGENT_LOOPS)
        ]
    final = _response(answer=(
        "[模拟模型决策，待人工审查]\n"
        "本轮重放已捕获的工具原件，未再次采集服务；原始采集时间与回放时间分开保存。\n"
        "回放结果不能证明当前状态，缺项未使用Mock补齐。\n"
        "原件为recent的日志仍只是近期查询，不是告警窗口取证。\n"
        "尚未确认根因、真实模型推理能力或业务恢复；未执行修复。"
        if replay else MOCK_ANSWER
    ))
    if scenario == "model_failure":
        final = ValueError("p61_simulated_interpretation_failure")
    elif scenario == "model_incomplete":
        final = _response(status="incomplete")
    return [_response(calls), final]


def run_case(
    root,
    scenario="offline_ok",
    *,
    observations=None,
):
    """Create one new artifact directory; fail rather than reuse any path.

    This function is synchronous and temporarily patches process-wide transport
    boundaries. Run it in a dedicated process, never alongside production work.
    """
    if scenario not in SCENARIOS:
        raise ValueError("unknown_p61_scenario")
    if observations is not None and (
        not isinstance(observations, dict) or scenario != "offline_ok"
    ):
        raise ValueError("replay_requires_observation_object_and_default_scenario")
    captured = deepcopy(observations)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    transport = []
    forbidden_attempts = []

    def forbidden(*args, **kwargs):
        forbidden_attempts.append("forbidden_network_sdk_database_or_process")
        raise BoundaryViolation("p61_external_io_forbidden")

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {
            "INCIDENT_AGENT_LOG_PATH": str(root / "agent.log"),
        }))
        # Existing application imports may already have a production handler.
        # Replace this process's handler temporarily without closing that handler.
        case_logger = logging.getLogger("agent")
        log_handler = logging.FileHandler(
            root / "agent.log", mode="x", encoding="utf-8",
        )
        stack.callback(log_handler.close)
        stack.enter_context(patch.object(case_logger, "handlers", [log_handler]))
        stack.enter_context(patch.object(case_logger, "propagate", False))
        # Install guards before importing application modules or creating stores.
        stack.enter_context(patch("socket.socket.connect", forbidden))
        stack.enter_context(patch("socket.socket.connect_ex", forbidden))
        stack.enter_context(patch("socket.create_connection", forbidden))
        stack.enter_context(patch("socket.getaddrinfo", forbidden))
        stack.enter_context(patch("requests.sessions.Session.request", forbidden))
        stack.enter_context(patch("openai.OpenAI", forbidden))
        stack.enter_context(patch("openai.AsyncOpenAI", forbidden))
        stack.enter_context(patch("psycopg.connect", forbidden))
        stack.enter_context(patch("subprocess.Popen", forbidden))
        stack.enter_context(patch("threading.Thread.start", forbidden))

        import requests
        from agent.agent import Agent
        from alert_policy import ALERT_POLICIES
        from clients.prometheus_client import PrometheusClient
        from config import MAX_AGENT_LOOPS, MAX_TOOL_CALLS
        from incident_store import IncidentStore
        from investigation.checkpoint import valid_chain
        from investigation.manager import InvestigationManager
        from investigation.models import Checkpoint, SessionStatus
        from investigation.scheduler import Scheduler
        from investigation.store import RuntimeStore
        from investigation.worker import Worker
        from monitor.incident_monitor import IncidentMonitor
        from prompts.agent_prompt import agent_prompt
        from services.metric_profiles import build_promql
        from services.registry import registry
        from tools.tool_registry import tool_registry

        stack.enter_context(patch(
            "remediation.executor.ActionExecutor.execute", forbidden,
        ))
        stack.enter_context(patch("recovery.worker.RecoveryWorker.start", forbidden))
        settings = registry.require_capability("order-service", "metrics")
        expected_query = build_promql("order-service", settings, "http_5xx_rate")
        policy = ALERT_POLICIES["HighHttp5xxRate"]
        raw_alert = {
            "state": "firing",
            "labels": {
                "alertname": policy.alertname,
                "service": "order-service",
                "metric": policy.metric,
                "signal_type": policy.signal_type,
                "severity": policy.severity,
            },
            "annotations": {
                "threshold": "0.2",
                "unit": policy.unit,
                "direction": policy.direction,
                "window": policy.window,
                "minimum_requests": str(policy.minimum_requests),
            },
            # The fixture intentionally has no activeAt / alert window.
            "value": "0.27",
        }

        def offline_get(url, **kwargs):
            parsed = urlsplit(url)
            endpoint = (parsed.hostname, parsed.port, parsed.path)
            allowed = {
                ("localhost", 9090, "/api/v1/alerts"): "synthetic_alert",
                ("localhost", 9090, "/api/v1/query"): "mock_prometheus",
                ("localhost", 3100, "/loki/api/v1/query_range"): "mock_loki",
                ("localhost", 8001, "/health"): "mock_health",
                ("localhost", 8001, "/version"): "mock_version",
            }
            source = allowed.get(endpoint)
            if source is None or parsed.query or parsed.username or parsed.password:
                return forbidden()
            row = {
                "source": source,
                "url": url,
                "parameters": deepcopy(kwargs.get("params", {})),
                "query_started_at": datetime.now(timezone.utc).isoformat(),
                "evidence_origin": "offline_http_fixture",
                "actual_network_request": False,
            }
            transport.append(row)
            if source == "mock_prometheus" and kwargs.get("params") != {
                "query": expected_query,
            }:
                return forbidden()
            if source == "mock_loki" and kwargs.get("params", {}).get("query") != (
                '{service="order-service"}'
            ):
                return forbidden()
            if source == "mock_prometheus" and scenario == "prometheus_unavailable":
                row["error"] = "ConnectionError"
                row["query_finished_at"] = datetime.now(timezone.utc).isoformat()
                raise requests.ConnectionError("p61_fixture_prometheus_unavailable")
            if source == "mock_loki" and scenario == "loki_unavailable":
                row["error"] = "ConnectionError"
                row["query_finished_at"] = datetime.now(timezone.utc).isoformat()
                raise requests.ConnectionError("p61_fixture_loki_unavailable")
            if source == "synthetic_alert":
                body = {"status": "success", "data": {"alerts": [raw_alert]}}
            elif source == "mock_prometheus":
                values = [] if scenario == "metrics_no_data" else [{
                    "metric": {},
                    "value": [
                        time.time(),
                        "0" if scenario == "metric_zero" else "0.27",
                    ],
                }]
                body = {
                    "status": "success",
                    "data": {"resultType": "vector", "result": values},
                }
            elif source == "mock_loki":
                params = kwargs["params"]
                body = {
                    "status": "success",
                    "data": {"resultType": "streams", "result": [{
                        "stream": {"service": "order-service"},
                        "values": [[
                            str(int(params["end"]) - 1_000_000_000),
                            "P6.1 offline fixture; business state is unknown",
                        ]],
                    }]},
                }
            elif source == "mock_health":
                body = {"health": "ok", "message": "P6.1 offline health fixture"}
            else:
                body = {
                    "service": "order-service",
                    "commit_sha": "a" * 40,
                    "process_started_at": started,
                    "runtime_flags": {
                        "simulate_code_regression": False,
                        "simulate_slow_request": False,
                    },
                }
            result = requests.Response()
            result.status_code = 200
            result.url = url
            result._content = json.dumps(body).encode("utf-8")
            result.encoding = "utf-8"
            row.update(
                status_code=result.status_code,
                body=deepcopy(body),
                query_finished_at=datetime.now(timezone.utc).isoformat(),
            )
            return result

        stack.enter_context(patch("requests.get", offline_get))
        if captured is not None:
            expected = {
                "query_metrics": {
                    "service": "order-service",
                    "metric": "http_5xx_rate",
                },
                "query_logs": {"service": "order-service"},
                "check_service_health": {"service": "order-service"},
                "get_running_version": {"service": "order-service"},
            }

            def replay_tool(name):
                def replay(**arguments):
                    if arguments != expected[name]:
                        return forbidden()
                    observation = captured.get(name)
                    if observation is None:
                        return {
                            "status": "no_data",
                            "service": "order-service",
                            "reason": "observation_not_collected",
                            "evidence_scope": "time_window",
                            "use_for_current_state": False,
                        }
                    if (
                        observation.get("arguments") != arguments
                        or not isinstance(observation.get("result"), dict)
                        or not isinstance(observation.get("collected_at"), str)
                    ):
                        raise BoundaryViolation(
                            "p61_replay_source_mismatched_or_invalid"
                        )
                    # The envelope is deliberately non-current. Otherwise Runtime
                    # would renew Health/Version TTL from its replay collection.
                    return {
                        "status": "no_data",
                        "service": "order-service",
                        "reason": "captured_observation_replay_not_current",
                        "evidence_scope": "time_window",
                        "use_for_current_state": False,
                        "captured_at": observation["collected_at"],
                        "source": observation.get("source"),
                        "captured_result": deepcopy(observation["result"]),
                    }

                return replay

            for name in expected:
                stack.enter_context(patch.dict(
                    tool_registry[name], {"func": replay_tool(name)},
                ))
        stores = root / "stores"
        stores.mkdir()
        manager = InvestigationManager(
            RuntimeStore(stores / "runtime.sqlite3"),
            IncidentStore(stores / "incidents.json"),
        )
        sdk = Mock(name="P61MockSdk")
        sdk.with_options.return_value = sdk
        planned = _script(scenario, replay=captured is not None)
        requests_seen = []
        active_session = None

        def mock_create(**body):
            saved = manager.store.get(active_session.session_id)
            assert saved.status == SessionStatus.RUNNING
            assert saved.total_model_requests == len(requests_seen) + 1
            assert saved.lease_until > manager.store.clock()
            row = {
                "decision_origin": "scripted_mock_sdk",
                "real_sdk_request": False,
                "reserved_session": saved.model_dump(mode="json"),
                "body": deepcopy(body),
            }
            requests_seen.append(row)
            assert planned, "unexpected_mock_model_request"
            result = planned.pop(0)
            if isinstance(result, Exception):
                row["error"] = type(result).__name__
                raise result
            row["response"] = result.model_dump(mode="json", exclude_none=True)
            return result

        sdk.responses.create.side_effect = mock_create
        agent = Agent(sdk, agent_prompt)
        scheduler = Scheduler(Worker(manager, agent))
        monitor = IncidentMonitor(
            PrometheusClient("http://localhost:9090"),
            manager.incident_store,
            0,
            manager,
        )
        snapshot = monitor.process_alerts_once()
        assert snapshot.complete and len(snapshot.alerts) == 1
        active_session = manager.store.list_sessions()[0]
        assert active_session.status == SessionStatus.QUEUED
        # A duplicate observation must match the existing Incident and Session.
        monitor.process_alerts_once()
        assert len(manager.store.list_sessions()) == 1
        assert len(manager.incident_store.active_incidents()) == 1
        scheduler.tick()
        saved = manager.store.get(active_session.session_id)
        checkpoint = manager.store.load_checkpoint(saved)
        Checkpoint.model_validate(checkpoint)
        assert valid_chain(checkpoint.get("history", []))
        assert not planned
        assert saved.total_model_requests == len(requests_seen)
        assert saved.total_tool_executions == len(checkpoint["records"])
        assert saved.current_attempt_rounds <= MAX_AGENT_LOOPS
        assert saved.current_attempt_tool_calls <= MAX_TOOL_CALLS
        assert not forbidden_attempts
        assert manager.remediation_planner is None
        assert not checkpoint.get("remediation_pending")
        incident = manager.incident_store.get_incident(saved.incident_id)
        assert incident.alert_state == "firing"
        assert incident.recovery_verified_at is None
        assert incident.recovery_verification is None
        assert incident.alerts[-1].starts_at is None
        reopened = RuntimeStore(manager.store.path, read_only=True)
        assert reopened.get(saved.session_id) == saved
        assert reopened.load_checkpoint(saved) == checkpoint
        events = reopened.events(saved.session_id, limit=1000)
        assert sum(
            row["event_type"] == "model_reserved" for row in events
        ) == len(requests_seen)
        assert sum(
            row["event_type"] == "tool_reserved" for row in events
        ) == len(checkpoint["records"])
        evidence = {item.observation_id: item for item in incident.evidence}
        for record in checkpoint["records"]:
            if record["success"] and record["result"].get("status") == "ok":
                projected = evidence[record["observation_id"]]
                assert json.loads(projected.detail) == record["result"]
        report = {
            "scenario": (
                "captured_observation_replay" if captured is not None else scenario
            ),
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "evidence_origin": (
                "captured_observation_replay_and_scripted_mock_sdk"
                if captured is not None
                else "offline_http_fixtures_and_scripted_mock_sdk"
            ),
            "real_model_sdk_requests": 0,
            "mock_decision_rounds": saved.total_model_requests,
            "real_service_observations": 0,
            "captured_observations": captured,
            "replay_started_at": started if captured is not None else None,
            "business_state": "unknown",
            "business_recovery_verified": False,
            "alert_window": None,
            "log_query_scope": "recent_not_alert_window",
            "provider_verified": False,
            "authorization": {
                "remediation_planner": None,
                "remediation_execution": False,
                "database_connections": 0,
                "background_tasks_started": 0,
                "forbidden_io_attempts": forbidden_attempts,
            },
            "checks": {
                "duplicate_alert_matched": True,
                "checkpoint_schema_valid": True,
                "function_call_chain_valid": True,
                "reopened_store_matches": True,
                "successful_evidence_projection_matches": True,
                "reservation_counters_match": True,
            },
            "limits": manager.limits.model_dump(mode="json"),
            "attempt_limits": {"models": MAX_AGENT_LOOPS, "tools": MAX_TOOL_CALLS},
            "session": saved.model_dump(mode="json"),
            "incident": incident.model_dump(mode="json"),
            "checkpoint": checkpoint,
            "events": events,
            "http_fixtures": transport,
            "mock_model_requests": requests_seen,
            "review_required": True,
            "investigation_report": checkpoint.get("result", {}).get("answer") or (
                "模拟解释未完成；不得据此确认根因或业务恢复。"
            ),
        }
        _write_json(root / "report.json", report)
        manifest = {}
        for path in (
            root / "report.json",
            manager.store.path,
            manager.incident_store.file_path,
        ):
            path = Path(path)
            manifest[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        _write_json(root / "sha256.json", manifest)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--scenario", choices=SCENARIOS, default="offline_ok")
    parser.add_argument(
        "--observations",
        type=Path,
        help="Optional captured tool-observation JSON; never re-fetches",
    )
    arguments = parser.parse_args()
    observations = (
        json.loads(arguments.observations.read_text(encoding="utf-8"))
        if arguments.observations else None
    )
    result = run_case(
        arguments.output_dir,
        arguments.scenario,
        observations=observations,
    )
    print(json.dumps({
        "report": str(arguments.output_dir.resolve() / "report.json"),
        "session_status": result["session"]["status"],
        "mock_model_requests": result["session"]["total_model_requests"],
        "tool_executions": result["session"]["total_tool_executions"],
        "real_model_sdk_requests": result["real_model_sdk_requests"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
