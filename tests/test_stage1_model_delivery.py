"""Offline HTTP/SDK -> existing Runtime regression checks; no paid requests."""
import asyncio
import gzip
import json
from unittest.mock import Mock

from fastapi.testclient import TestClient
import httpx2
from openai import APIConnectionError, OpenAI
import pytest

from agent.agent import Agent
from investigation.checkpoint import valid_chain
from investigation.manager import InvestigationManager
from investigation.models import Checkpoint, RuntimeLimits, SessionStatus
from investigation.store import RuntimeStore
from investigation.worker import Worker
from model_client.model_client import ModelClient, RequestConstraintError
from mysql_support import allow_isolated_mysql_sockets
from runtime_support import call, create_runtime, create_session, response
from tools.tool_registry import tool_registry
from web_api.app import create_app


@pytest.fixture(autouse=True)
def offline():
    # Windows builds asyncio's private wakeup socketpair with loopback TCP.
    # Create it before the guard, then reuse this loop for the in-process API.
    loop = asyncio.new_event_loop()
    forbidden = Mock(side_effect=AssertionError("non_database_network_forbidden"))
    try:
        with allow_isolated_mysql_sockets(forbidden):
            yield loop
    finally:
        if not loop.is_closed():
            loop.close()
    forbidden.assert_not_called()


def fixture_bound(body):
    """Synthetic bound for a non-network fixture, not a DeepSeek tokenizer."""
    assert set(body) == {
        "model", "input", "instructions", "tools", "max_output_tokens",
    }
    return 32


def payload(*, calls=(), answer="离线合成回答，根因待验证。"):
    data = response(calls=calls, answer=answer).model_dump(mode="json")
    data["model"] = "deepseek-flash"
    data["usage"] = {
        "input_tokens": 32,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 9,
        "output_tokens_details": {"reasoning_tokens": 4},
        "total_tokens": 41,
    }
    return data


class Stream(httpx2.SyncByteStream):
    def __init__(self, raw):
        self.raw = raw
        self.closes = 0

    def __iter__(self):
        yield self.raw

    def close(self):
        self.closes += 1


@pytest.mark.parametrize("compressed", [False, True])
def test_sdk_http_body_usage_calls_and_response_lifecycle(compressed):
    data = payload(calls=[call(call_id="first"), call(call_id="second")], answer="")
    raw = json.dumps(data).encode()
    stream = Stream(gzip.compress(raw) if compressed else raw)
    requests = []

    def receive(request):
        requests.append(json.loads(request.read()))
        return httpx2.Response(
            200,
            headers={"content-type": "application/json", **(
                {"content-encoding": "gzip"} if compressed else {}
            )},
            stream=stream,
        )

    with OpenAI(
        api_key="OFFLINE_NON_CREDENTIAL",
        base_url="https://provider.invalid",
        max_retries=5,
        http_client=httpx2.Client(transport=httpx2.MockTransport(receive)),
    ) as sdk:
        client = ModelClient(
            sdk, "deepseek-flash", max_output_tokens=73,
            input_token_counter=fixture_bound,
        )
        result = client.call_once([], "offline", [], timeout=7)
    assert len(requests) == 1
    assert requests[0]["max_output_tokens"] == 73
    assert result.usage.input_tokens == 32
    assert result.usage.output_tokens == 9
    assert result.usage.output_tokens_details.reasoning_tokens == 4
    assert result.usage.total_tokens == 41
    assert [c.call_id for c in result.output] == ["first", "second"]
    assert stream.closes == 1


def test_original_response_reconstruction_reproduces_double_decoding():
    raw = json.dumps(payload()).encode()
    requests = []

    def broken(request):
        requests.append(request)
        original = httpx2.Response(
            200, headers={"content-encoding": "gzip"},
            stream=httpx2.ByteStream(gzip.compress(raw)),
        )
        return httpx2.Response(200, headers=original.headers, content=original.read())

    with OpenAI(
        api_key="OFFLINE_NON_CREDENTIAL", base_url="https://provider.invalid",
        http_client=httpx2.Client(transport=httpx2.MockTransport(broken)),
    ) as sdk, pytest.raises(APIConnectionError) as error:
        ModelClient(sdk, "deepseek-flash", input_token_counter=fixture_bound).call_once([], "", [])
    assert type(error.value.__cause__).__name__ == "DecodingError"
    assert len(requests) == 1


@pytest.mark.parametrize("bound", [None, -1, True, 1.5, "32", 65537])
def test_invalid_or_missing_input_proof_blocks_before_sdk(bound):
    sdk = Mock()
    client = ModelClient(
        sdk, "deepseek-flash",
        input_token_counter=(lambda body: bound) if bound is not None else None,
    )
    with pytest.raises(RequestConstraintError):
        client.call_once([], "all instructions", [{"type": "function"}])
    sdk.with_options.assert_not_called()
    sdk.responses.create.assert_not_called()


def test_counter_failure_is_sanitized_and_cannot_mutate_the_sent_body():
    sdk = Mock()
    sdk.with_options.return_value = sdk

    def failed(body):
        raise RuntimeError("private-provider-detail")

    client = ModelClient(sdk, "deepseek-flash", input_token_counter=failed)
    with pytest.raises(RequestConstraintError, match="input_token_bound_unavailable") as error:
        client.call_once([], "offline", [])
    assert "private-provider-detail" not in str(error.value)
    sdk.with_options.assert_not_called()

    def mutate(body):
        body["input"].append({"role": "user", "content": "untrusted mutation"})
        body["max_output_tokens"] = 99999
        return 0

    client.input_token_counter = mutate
    client.call_once([], "offline", [])
    sent = sdk.responses.create.call_args.kwargs
    assert sent["input"] == [] and sent["max_output_tokens"] == 8192


@pytest.mark.parametrize("status", [401, 403, 408, 429, 500, 503])
def test_sdk_http_errors_are_single_attempts(status):
    sent = []

    def receive(request):
        sent.append(request)
        return httpx2.Response(status, json={"error": {"message": "offline error"}})

    with OpenAI(
        api_key="OFFLINE_NON_CREDENTIAL", base_url="https://provider.invalid",
        http_client=httpx2.Client(transport=httpx2.MockTransport(receive)),
    ) as sdk, pytest.raises(Exception) as error:
        ModelClient(sdk, "deepseek-flash", input_token_counter=fixture_bound).call_once([], "", [])
    assert getattr(error.value, "status_code", None) == status
    assert len(sent) == 1


def test_multiround_sdk_agent_tools_worker_checkpoint_and_readonly_api(tmp_path, monkeypatch, offline):
    limits = RuntimeLimits(
        max_attempts=1, max_model_requests=5, max_tool_executions=9,
        max_resumes=0, max_duration_seconds=321, max_human_requests=0,
    )
    manager = create_runtime(tmp_path, limits=limits)
    session = create_session(manager)
    monkeypatch.setitem(tool_registry["get_running_version"], "func", lambda service: {
        "status": "no_data", "reason": "offline_no_runtime_probe", "service": service,
    })
    requests = []
    usages = []
    calls = [
        call("get_service_dependencies", {"service": "order-service"}, "offline-deps"),
        call("get_service_capabilities", {"service": "order-service"}, "offline-caps"),
    ]

    def receive(request):
        body = json.loads(request.read())
        assert body["max_output_tokens"] == 8192
        assert valid_chain(body["input"])
        requests.append(body)
        if len(requests) > 1:
            expected = {c["call_id"] for c in calls[:len(requests) - 1]}
            outputs = {i["call_id"]: i for i in body["input"] if i.get("type") == "function_call_output"}
            assert set(outputs) == expected
            saved = manager.store.load_checkpoint(manager.store.get(session.session_id))
            records = {r["call_id"]: r for r in saved["records"]}
            for call_id, output in outputs.items():
                assert json.loads(output["output"]) == records[call_id]["result"]
        data = payload(calls=[calls[len(requests) - 1]], answer="") if len(requests) <= 2 else payload()
        usages.append(data["usage"])
        return httpx2.Response(200, headers={"content-type": "application/json", "content-encoding": "gzip"},
                              stream=httpx2.ByteStream(gzip.compress(json.dumps(data).encode())))

    with OpenAI(
        api_key="OFFLINE_NON_CREDENTIAL", base_url="https://provider.invalid",
        http_client=httpx2.Client(transport=httpx2.MockTransport(receive)),
    ) as sdk:
        agent = Agent(sdk, "离线协议回归，不判断真实模型能力。", input_token_counter=fixture_bound)
        # This opt-in verifies only this synthetic protocol fixture.
        completed = Worker(manager, agent, provider_verified=True).run_once()
    assert completed.status == SessionStatus.COMPLETED
    assert (completed.attempt_count, completed.total_model_requests, completed.total_tool_executions) == (1, 3, 3)
    checkpoint = manager.store.load_checkpoint(completed)
    Checkpoint.model_validate(checkpoint)
    assert valid_chain(checkpoint["history"])
    assert checkpoint["pending"] == []
    assert len(checkpoint["records"]) == 3
    assert len(usages) == 3 and all(u["total_tokens"] == 41 for u in usages)
    reopened = RuntimeStore(manager.store.database_url, read_only=True)
    assert reopened.load_checkpoint(reopened.get(session.session_id)) == checkpoint
    # An independent reader retains production defaults, yet must show executor limits.
    reader = InvestigationManager(reopened, None)
    assert reader.limits.max_model_requests == 21
    assert reader.workflow(session.session_id)["limits"] == limits.model_dump(mode="json")
    with TestClient(create_app(
        incident_url=manager.incident_store.database_url,
        runtime_url=manager.store.database_url,
    ), base_url="http://127.0.0.1", backend_options={"loop_factory": lambda: offline}) as api:
        detail = api.get("/api/sessions/" + session.session_id).json()
    assert detail["limits"] == limits.model_dump(mode="json")
    assert detail["limits_source"] == "session_checkpoint"


def test_legacy_limits_are_unknown_and_reader_does_not_fill_or_write_them(tmp_path):
    manager = create_runtime(tmp_path)
    session = create_session(manager)
    with manager.store.transaction() as db:
        saved = manager.store.get(session.session_id, db)
        checkpoint = manager.store.load_checkpoint(saved, db)
        checkpoint.pop("execution_limits")
        manager.store.checkpoint(saved, checkpoint, db)
        manager.store.save(saved, db)
    before = manager.store.get(session.session_id).model_dump(mode="json")
    reader = InvestigationManager(RuntimeStore(manager.store.database_url, read_only=True), None)
    detail = reader.workflow(session.session_id)
    assert detail["limits"] is None and detail["limits_source"] == "unrecorded"
    assert manager.store.get(session.session_id).model_dump(mode="json") == before
    assert "execution_limits" not in manager.store.load_checkpoint(manager.store.get(session.session_id))


def test_changed_executor_limits_cannot_silently_resume_a_saved_grant(tmp_path):
    from investigation.models import HumanCommand
    from runtime_support import FixtureAgent

    manager = create_runtime(tmp_path)
    session = create_session(manager)

    def pause(phase, session_id):
        if phase == "model_checkpoint_committed":
            manager.submit(HumanCommand(
                session_id=session_id, actor="offline-test", command_type="pause",
            ))

    paused = Worker(manager, FixtureAgent(), boundary_hook=pause).run_once()
    assert paused.status == SessionStatus.PAUSED
    before = manager.store.load_checkpoint(paused)["execution_limits"]
    manager.limits.max_model_requests += 1
    manager.submit(HumanCommand(
        session_id=session.session_id, actor="offline-test", command_type="resume",
    ))
    agent = FixtureAgent()
    refused = Worker(manager, agent).run_once()
    assert refused.status == SessionStatus.FAILED
    assert refused.last_error_category == "execution_limits_changed"
    agent.request_durable_response.assert_not_called()
    assert manager.store.load_checkpoint(refused)["execution_limits"] == before
