"""P5.3c.1 recording seams over the existing offline Runtime fixtures."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

from agent.agent import Agent
from config import MAX_AGENT_LOOPS, MAX_TOOL_CALLS
from incident_store import IncidentStore
from investigation.evidence_policy import effective_records
from investigation.manager import InvestigationManager
from investigation.models import Checkpoint, SessionStatus
from investigation.store import RuntimeStore
from investigation.worker import Worker
from models.investigation import InvestigationResult, InvestigationStatus
from prompts.agent_prompt import agent_prompt
from runtime_support import FixtureAgent, SHA, call, create_runtime, create_session
from test_metric_analysis import AT, COLLECTED, record
from test_worker_metric_analysis_growth import analyses, function_items
from web_api.queries import RuntimeQueries


PRESET_ANSWER = (
    "[Mock 预设回答，仅验证传递与落盘]\n"
    "已确认历史事实：以本次保存的原始指标观测及其评估时间为准。\n"
    "限制：时间错位、缺失数据和工具失败均不能作为当前健康证据。\n"
    "尚不能确认根因，不能据此确认计数器重置、数据库故障或代码回归。\n"
    "后续建议检查原始采样、抓取记录与计数器；建议不会自动执行。"
)


def source_call(source, call_id=None):
    arguments = record(source)["arguments"]
    arguments["service"] = source.result["service"]
    return call(source.name, arguments, call_id or source.observation_id)


def start_runtime(root, script, sources=(), *, verified=True, boundary=None):
    """Mock only external I/O; Agent, ModelClient and Runtime remain real."""
    clock = SimpleNamespace(now=COLLECTED)
    manager = create_runtime(root, clock=lambda: clock.now)
    session = create_session(manager)
    run = SimpleNamespace(
        root=root,
        clock=clock,
        manager=manager,
        session_id=session.session_id,
        incident_id=session.incident_id,
        requests=[],
        executions=[],
        unexpected_tools=[],
        verified=verified,
        boundary=boundary,
        script=list(script),
        sources={source.observation_id: deepcopy(source.result) for source in sources},
        provider=Mock(),
    )
    run.provider.with_options.return_value = run.provider

    def request(**body):
        saved = run.manager.store.get(run.session_id)
        checkpoint = run.manager.store.load_checkpoint(saved)
        # The callback runs after the actual model reservation and before a
        # scripted response/exception, including requests that eventually fail.
        assert saved.status == SessionStatus.RUNNING
        assert saved.lease_until > run.clock.now
        tools_left = min(
            MAX_TOOL_CALLS - saved.current_attempt_tool_calls,
            run.manager.limits.max_tool_executions - saved.total_tool_executions,
        )
        rounds_left = min(
            MAX_AGENT_LOOPS - saved.current_attempt_rounds + 1,
            run.manager.limits.max_model_requests - saved.total_model_requests + 1,
        )
        assert f"剩余工具执行次数 {tools_left}" in body["instructions"]
        assert f"剩余模型响应轮数（含本轮） {rounds_left}" in body["instructions"]
        row = {
            "body": deepcopy(body),
            "session": saved.model_dump(mode="json"),
            "checkpoint": deepcopy(checkpoint),
            "current_observation_ids": [
                item["observation_id"] for item in effective_records(
                    checkpoint["records"], checkpoint.get("identity"),
                    run.clock.now, saved.service,
                )
            ],
        }
        run.requests.append(row)
        assert run.script, "unexpected_extra_model_request"
        result = run.script.pop(0)
        if isinstance(result, Exception):
            row["outcome"] = type(result).__name__
            raise result
        row["outcome"] = "completed"
        row["response"] = result.model_dump(mode="json", exclude_none=True)
        return result

    run.provider.responses.create.side_effect = request
    attach_worker(run)
    return run


def attach_worker(run, *, reopen=False):
    if reopen:
        run.manager = create_runtime(run.root, clock=lambda: run.clock.now)

    def tool_result(item):
        if item.name == "get_running_version":
            result = {
                "status": "ok",
                "service": "order-service",
                "commit_sha": SHA,
                "process_started_at": AT.isoformat(),
            }
        elif item.call_id in run.sources:
            result = deepcopy(run.sources[item.call_id])
        else:
            run.unexpected_tools.append(item.call_id)
            raise AssertionError("unexpected_tool_execution")
        run.executions.append({
            "name": item.name,
            "call_id": item.call_id,
            "arguments": item.arguments,
            "result": deepcopy(result),
        })
        return result

    run.agent = FixtureAgent(tool_result=tool_result, clock=lambda: run.clock.now)
    run.agent.agent_prompt = agent_prompt
    run.agent.request_durable_response = (
        Agent.request_durable_response.__get__(run.agent)
    )
    run.agent.model_client.client = run.provider
    run.worker = Worker(
        run.manager,
        run.agent,
        provider_verified=run.verified,
        boundary_hook=run.boundary,
    )
    return run.worker


def latest_analysis(run):
    payloads = analyses(run.requests[-1]["body"]["input"])
    assert payloads
    return payloads[-1]["metric_analysis"]


def finish_and_reopen(
    run,
    *,
    models,
    tools,
    hits=0,
    attempts=1,
    status=SessionStatus.COMPLETED,
):
    saved = run.manager.store.get(run.session_id)
    assert saved.status == status
    assert (
        saved.total_model_requests, saved.total_tool_executions,
        saved.cache_hits, saved.attempt_count,
    ) == (models, tools, hits, attempts)
    assert len(run.requests) == run.provider.responses.create.call_count == models
    assert len(run.executions) == tools
    assert run.unexpected_tools == []
    assert run.script == []
    assert all(
        item.kwargs == {
            "max_retries": 0,
            "timeout": run.manager.limits.model_timeout_seconds,
        }
        for item in run.provider.with_options.call_args_list
    )
    assert saved.owner_token is None and saved.lease_until is None
    assert saved.human_request_count == 0
    checkpoint = run.manager.store.load_checkpoint(saved)
    Checkpoint.model_validate(checkpoint)
    result = InvestigationResult.model_validate(checkpoint["result"])
    assert (result.model_rounds, result.tool_calls) == (models, tools)
    assert (
        checkpoint["budget"]["model_requests"],
        checkpoint["budget"]["tool_executions"],
    ) == (models, tools)
    assert result.success is (status == SessionStatus.COMPLETED)
    if result.success:
        assert result.status == InvestigationStatus.COMPLETED
        assert result.answer == PRESET_ANSWER
    assert not checkpoint.get("remediation_pending")
    assert "metric_analysis" not in checkpoint
    # Version probes are real Runtime executions, not model function calls.
    executed_ids = {item["call_id"] for item in run.executions}
    assert {item["call_id"] for item in checkpoint["records"]} == executed_ids
    assert len(checkpoint["records"]) == tools
    for item in function_items(checkpoint.get("history", [])):
        assert not item["call_id"].startswith("runtime-probe-")
    original_calls = {
        item["call_id"]: item
        for row in run.requests
        for item in row.get("response", {}).get("output", [])
        if item["type"] == "function_call"
    }
    for item in function_items(checkpoint.get("history", [])):
        if item["type"] == "function_call":
            assert item == original_calls[item["call_id"]]
    restarted = RuntimeStore(run.manager.store.path, read_only=True)
    assert restarted.get(run.session_id) == saved
    assert restarted.load_checkpoint(restarted.get(run.session_id)) == checkpoint
    view = InvestigationManager(
        restarted,
        IncidentStore(run.root / "incidents.json"),
        limits=run.manager.limits,
    ).workflow(run.session_id)
    assert view["investigation_result"] == checkpoint["result"]
    api = RuntimeQueries(
        incident_path=run.root / "incidents.json",
        runtime_path=restarted.path,
    ).session(run.session_id)
    assert (
        api.investigation_result.model_rounds,
        api.investigation_result.tool_calls,
    ) == (models, tools)
    assert view["session"]["status"] == status.value
    assert view["human_requests"] == view["human_messages"] == []
    assert view["remediations"] == []
    assert view["approval_authority"] == "existing_remediation_store"
    incident = IncidentStore(run.root / "incidents.json").get_incident(run.incident_id)
    saved_answer = next(
        item for item in incident.investigations if item.session_id == run.session_id
    )
    assert saved_answer.answer == result.answer
    assert saved_answer.model_rounds == models
    assert saved_answer.tool_calls == tools
    assert saved_answer.status == result.status
    projected_evidence = {
        item.observation_id: item for item in incident.evidence
        if item.runtime_session_id == run.session_id
    }
    for record in checkpoint["records"]:
        if record["success"] and record["result"].get("status") == "ok":
            evidence = projected_evidence[record["observation_id"]]
            assert json.loads(evidence.detail) == record["result"]
    assert checkpoint["projected"] is True
    events = restarted.events(run.session_id, limit=1000)
    assert sum(item["event_type"] == "model_reserved" for item in events) == models
    assert sum(item["event_type"] == "tool_reserved" for item in events) == tools
    assert sum(item["event_type"] == "tool_cache_hit" for item in events) == hits
    assert not any(
        item["event_type"].startswith(("human_request_", "remediation_"))
        for item in events
    )
    return checkpoint
