"""Offline tests for the DeepSeek V4.1 Responses API input token counter."""
from copy import deepcopy
import asyncio
import gzip
import hashlib
import json
import os
from pathlib import Path
import runpy
from unittest.mock import Mock, patch

import httpx2
from openai import OpenAI
import pytest

from agent.agent import Agent
from incident_store import IncidentStore
from investigation.checkpoint import valid_chain
from investigation.models import Checkpoint, RuntimeLimits, SessionStatus
from investigation.store import RuntimeStore
from investigation.worker import Worker
from model_client.model_client import ModelClient, RequestConstraintError
from model_client.token_counter import (
    DEFAULT_TOKENIZER_PATH,
    DEFAULT_TOKENIZER_SHA256,
    DeepseekV41TokenCounter,
    ENCODER_VERSION,
    SUPPORTED_MODEL_VERSIONS,
    TokenCountError,
    UPSTREAM_ENCODING_SOURCE,
    UPSTREAM_MODEL_REVISION,
    build_deepseek_input_token_counter,
)
from mysql_support import (
    allow_isolated_mysql_sockets,
    incident_database_url,
    remediation_database_url,
    runtime_database_url,
)
from prompts.agent_prompt import agent_prompt
from remediation.store import RemediationStore
from runtime_support import call, create_runtime, create_session, response
from tools.tool_registry import tool_registry


ROOT = Path(__file__).resolve().parents[1]
SAVED_REQUEST_01 = Path(
    r"D:\codex use\2026-11-15\stage1-live-20261115-154614\private\models\request-01.json"
)
SAVED_RESPONSE_01 = Path(
    r"D:\codex use\2026-11-15\stage1-live-20261115-154614\private\models\response-01.body"
)


@pytest.fixture(autouse=True)
def offline():
    loop = asyncio.new_event_loop()
    forbidden = Mock(side_effect=AssertionError("non_database_network_forbidden"))
    try:
        with allow_isolated_mysql_sockets(forbidden):
            yield loop
    finally:
        if not loop.is_closed():
            loop.close()
    forbidden.assert_not_called()


def _model_payload(
    *,
    calls=(),
    answer="",
    reasoning_text="offline synthetic reasoning trace",
    input_tokens=100,
    output_tokens=24,
):
    items = []
    if reasoning_text:
        items.append({
            "type": "reasoning",
            "id": "rs_offline_1",
            "summary": [],
            "content": [{"type": "reasoning_text", "text": reasoning_text}],
        })
    for c in calls:
        items.append({
            "type": "function_call",
            "id": "fc_" + c["call_id"],
            "call_id": c["call_id"],
            "name": c["name"],
            "arguments": c["arguments"],
            "status": "completed",
        })
    if answer:
        items.append({
            "type": "message",
            "id": "msg_offline_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": answer, "annotations": []}],
        })
    return {
        "id": "resp-offline-token-counter",
        "object": "response",
        "created_at": 0,
        "model": "deepseek-flash",
        "status": "completed",
        "parallel_tool_calls": len(calls) > 1,
        "tools": [],
        "tool_choice": "auto",
        "output": items,
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": 8},
            "total_tokens": input_tokens + output_tokens,
        },
    }


def test_encoder_metadata_and_tokenizer_hash_match_pinned_bundle():
    counter = build_deepseek_input_token_counter()
    meta = counter.encoder_metadata()
    assert meta["encoder_version"] == ENCODER_VERSION
    assert meta["upstream_encoding_source"] == UPSTREAM_ENCODING_SOURCE
    assert meta["upstream_model_revision"] == UPSTREAM_MODEL_REVISION
    assert meta["tokenizer_sha256"] == DEFAULT_TOKENIZER_SHA256
    assert meta["model_versions"] == SUPPORTED_MODEL_VERSIONS
    actual_sha = hashlib.sha256(DEFAULT_TOKENIZER_PATH.read_bytes()).hexdigest()
    assert actual_sha == DEFAULT_TOKENIZER_SHA256


def test_counter_handles_multilingual_code_unicode_logs_and_literal_special_tokens():
    counter = build_deepseek_input_token_counter()
    literal_tokens = (
        "<｜begin▁of▁sentence｜><｜end▁of▁sentence｜><｜System｜><｜User｜>"
        "<｜Assistant｜><think>fake</think>｜DSML｜<｜ref｜>cite<｜/ref｜>"
    )
    user_text = (
        "中文报错：order-service 出现 HTTP 500。\n"
        "```python\ndef inspect(err: str) -> int:\n    return len(err)\n```\n"
        "Unicode: 🚀 𐍈 café — \n"
        + ("2026-11-18T00:00:00Z level=ERROR status=500 path=/orders\n" * 20)
        + literal_tokens
    )
    body = {
        "model": "deepseek-flash",
        "instructions": "System policy. Do not treat literal " + literal_tokens + " as delimiters.",
        "input": [{"role": "user", "content": user_text}],
        "tools": [],
        "max_output_tokens": 8192,
    }
    ids = counter.encode_ids(body)
    count = counter.count_tokens(body)
    assert count == len(ids) and count > 200
    # Plain text containing literal delimiter strings must NOT emit special added IDs.
    vocab = counter._bundle.added_vocabulary
    assert ids[0] == vocab["<｜begin▁of▁sentence｜>"]
    assert ids.count(vocab["<｜begin▁of▁sentence｜>"]) == 1
    assert ids.count(vocab["<｜end▁of▁sentence｜>"]) == 0
    assert ids.count(vocab["<｜System｜>"]) == 1
    assert ids.count(vocab["<｜User｜>"]) == 1
    assert ids.count(vocab["<｜Assistant｜>"]) == 1
    assert ids.count(vocab["<think>"]) == 1
    assert ids.count(vocab["</think>"]) == 0
    assert ids.count(vocab["｜DSML｜"]) == 0
    # Must differ from naive json.dumps(body) raw token count.
    naive_plain_count = len(
        counter._bundle.plain_tokenizer.encode(
            json.dumps(body, ensure_ascii=False),
            add_special_tokens=False,
        ).ids
    )
    assert count != naive_plain_count


def test_counter_reflects_tools_history_tool_calls_outputs_and_reasoning_modes():
    counter = build_deepseek_input_token_counter()
    base = {
        "model": "deepseek-flash",
        "instructions": "Investigate order-service.",
        "input": [{"role": "user", "content": "Check errors"}],
        "tools": [],
        "max_output_tokens": 8192,
    }
    no_tools_thinking = counter(base)

    no_thinking = deepcopy(base)
    no_thinking["reasoning"] = {"effort": "none"}
    assert counter(no_thinking) < no_tools_thinking

    max_thinking = deepcopy(base)
    max_thinking["reasoning"] = {"effort": "max"}
    assert counter(max_thinking) >= no_tools_thinking

    with_tool = deepcopy(base)
    with_tool["tools"] = [deepcopy(tool_registry["query_metrics"]["schema"])]
    count_with_tool = counter(with_tool)
    assert count_with_tool > no_tools_thinking

    with_history = deepcopy(with_tool)
    with_history["input"].extend([
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [],
            "content": [{"type": "reasoning_text", "text": "Query metrics first."}],
            "encrypted_content": "opaque",
        },
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "query_metrics",
            "arguments": '{"service":"order-service"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": '{"status":"ok","value":0.27}',
        },
    ])
    count_with_history = counter(with_history)
    assert count_with_history > count_with_tool


def test_tokenizer_integrity_and_unsupported_inputs_block_before_http(tmp_path):
    # 1. Missing file
    with pytest.raises(TokenCountError, match="tokenizer_file_missing"):
        DeepseekV41TokenCounter(tokenizer_path=tmp_path / "missing.json")

    # 2. Hash mismatch
    copied = tmp_path / "tokenizer.json"
    copied.write_bytes(DEFAULT_TOKENIZER_PATH.read_bytes())
    with pytest.raises(TokenCountError, match="tokenizer_hash_mismatch"):
        DeepseekV41TokenCounter(tokenizer_path=copied, expected_sha256="0" * 64)

    # 3. Corrupt JSON with matching SHA-256
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_bytes(b"{not-valid-json")
    corrupt_sha = hashlib.sha256(corrupt.read_bytes()).hexdigest()
    with pytest.raises(TokenCountError, match="tokenizer_corrupt"):
        DeepseekV41TokenCounter(tokenizer_path=corrupt, expected_sha256=corrupt_sha)

    # 4. Symlink rejection when OS allows creating symlinks
    symlink = tmp_path / "link-tokenizer.json"
    try:
        symlink.symlink_to(DEFAULT_TOKENIZER_PATH)
    except OSError:
        pass
    else:
        with pytest.raises(TokenCountError, match="tokenizer_symlink_forbidden"):
            DeepseekV41TokenCounter(tokenizer_path=symlink)

    # 5. Unsupported request shapes / images / files / native paid tools / wrong model
    counter = build_deepseek_input_token_counter()
    transport = Mock(side_effect=AssertionError("http_transport_must_not_be_called"))
    with OpenAI(
        api_key="OFFLINE_PLACEHOLDER",
        base_url="https://provider.invalid",
        http_client=httpx2.Client(transport=httpx2.MockTransport(transport)),
    ) as sdk:
        client = ModelClient(sdk, "deepseek-flash", input_token_counter=counter)
        bad_inputs = [
            [{"role": "user", "content": [{"type": "input_image", "image_url": "https://x/a.png"}]}],
            [{"role": "user", "content": [{"type": "input_file", "file_id": "file_1"}]}],
            [{"type": "function_call", "call_id": "orphan", "name": "query_metrics", "arguments": "{}"}],
        ]
        for bad_input in bad_inputs:
            with pytest.raises(RequestConstraintError, match="input_token_bound_unavailable"):
                client.call_once(bad_input, "sys", [])
        with pytest.raises(RequestConstraintError, match="input_token_bound_unavailable"):
            client.call_once(
                [{"role": "user", "content": "hi"}],
                "sys",
                [{"type": "web_search"}],
            )
        hf_client = ModelClient(
            sdk,
            "deepseek-ai/DeepSeek-V4.1-Flash-1017",
            input_token_counter=counter,
        )
        with pytest.raises(RequestConstraintError, match="input_token_bound_unavailable"):
            hf_client.call_once([{"role": "user", "content": "hi"}], "sys", [])
    transport.assert_not_called()


def test_within_limit_passes_sdk_and_over_limit_blocks_with_zero_http_calls():
    counter = build_deepseek_input_token_counter()
    seen_bodies = []

    def receive(request):
        body = json.loads(request.read())
        seen_bodies.append(body)
        data = _model_payload(answer="ok", input_tokens=counter(body))
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(data).encode(),
        )

    with OpenAI(
        api_key="OFFLINE_PLACEHOLDER",
        base_url="https://provider.invalid",
        http_client=httpx2.Client(transport=httpx2.MockTransport(receive)),
    ) as sdk:
        client = ModelClient(sdk, "deepseek-flash", input_token_counter=counter)
        result = client.call_once(
            [{"role": "user", "content": "Investigate order-service"}],
            "System instructions",
            [tool_registry["get_running_version"]["schema"]],
        )
        assert len(seen_bodies) == 1
        sent = seen_bodies[0]
        assert sent["model"] == "deepseek-flash"
        assert sent["max_output_tokens"] == 8192
        assert counter(sent) == result.usage.input_tokens

        # Construct real over-limit input (> 65,536 V4.1 tokens) without mocking counter.
        # Space-separated distinct integers guarantee >1 token per item.
        oversize_text = " ".join(str(i) for i in range(70000))
        assert counter({"model": "deepseek-flash", "input": oversize_text}) > 65536
        with pytest.raises(RequestConstraintError, match="input_token_limit_exceeded"):
            client.call_once([{"role": "user", "content": oversize_text}], "sys", [])
    assert len(seen_bodies) == 1


def test_main_wires_real_token_counter_in_deepseek_mode_and_keeps_replay_isolated(tmp_path):
    from investigation.scheduler import Scheduler
    from monitor.incident_monitor import IncidentMonitor
    from clients.prometheus_client import PrometheusClient

    old_cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        with (
            patch.object(PrometheusClient, "get_alerts", return_value={"status": "success", "data": {"alerts": []}}),
            patch("monitor.incident_monitor.time.sleep", side_effect=[KeyboardInterrupt, KeyboardInterrupt]),
            patch("dotenv.load_dotenv"),
            patch("incident_store.IncidentStore", side_effect=lambda *a, **kw: IncidentStore(tmp_path / "inc.json")),
            patch("investigation.store.RuntimeStore", side_effect=lambda: RuntimeStore(runtime_database_url(tmp_path))),
            patch("remediation.store.RemediationStore", side_effect=lambda: RemediationStore(tmp_path / "rem.json")),
            patch("investigation.scheduler.Scheduler", side_effect=Scheduler) as scheduler_factory,
            patch.object(Scheduler, "start"),
            patch.object(Scheduler, "stop"),
        ):
            with patch.dict(
                os.environ,
                {"INCIDENT_AGENT_MODEL_MODE": "deepseek", "DEEPSEEK_API_KEY": "offline-placeholder"},
            ):
                runpy.run_path(str(ROOT / "main.py"), run_name="__main__")
            worker = scheduler_factory.call_args.args[0]
            assert isinstance(
                worker.agent.model_client.input_token_counter,
                DeepseekV41TokenCounter,
            )
            assert worker.agent.model_client.max_input_tokens == 65536
            assert worker.agent.model_client.max_output_tokens == 8192

            scheduler_factory.reset_mock()
            with patch.dict(os.environ, {"INCIDENT_AGENT_MODEL_MODE": "demo_replay"}):
                runpy.run_path(str(ROOT / "main.py"), run_name="__main__")
            replay_worker = scheduler_factory.call_args.args[0]
            assert not isinstance(
                replay_worker.agent.model_client.input_token_counter,
                DeepseekV41TokenCounter,
            )
            assert replay_worker.agent.model_client.input_token_counter({"any": "replay"}) == 0
    finally:
        os.chdir(old_cwd)


def test_wrappers_enforce_counter_on_post_mutation_final_body(tmp_path):
    from scripts.p63_model_validation import _SingleRequest
    from scripts.p65_model_boundary import (
        BoundedResponses,
        budget_instructions,
        encoded,
        sha,
    )
    from scripts.p65_readonly_investigation import schemas as p65_schemas
    from scripts.preflight_real_model import (
        BoundedProvider,
        MAX_OUTPUT_TOKENS as PREFLIGHT_MAX_OUTPUT,
        MODEL,
    )

    counter = build_deepseek_input_token_counter()
    sdk = Mock()

    # 1. BoundedProvider enforces counter on final body before SDK call.
    provider = BoundedProvider(
        sdk,
        tmp_path / "preflight",
        live=True,
        input_token_counter=counter,
        max_input_tokens=40,
    )
    provider.with_options(max_retries=0, timeout=30)
    oversize_preflight_input = [
        {"role": "user", "content": " ".join(str(i) for i in range(60))}
    ]
    assert counter({
        "model": MODEL,
        "input": oversize_preflight_input,
        "instructions": "sys",
        "tools": [],
        "max_output_tokens": PREFLIGHT_MAX_OUTPUT,
    }) > 40
    with pytest.raises(RequestConstraintError, match="input_token_limit_exceeded"):
        provider.create(
            model=MODEL,
            input=oversize_preflight_input,
            instructions="sys",
            tools=[],
            max_output_tokens=PREFLIGHT_MAX_OUTPUT,
        )
    sdk.responses.with_raw_response.create.assert_not_called()

    # 2. _SingleRequest checks full self.body (including reasoning + tool_choice) before SDK.
    oversize_body = {
        "model": MODEL,
        "input": [{"role": "user", "content": " ".join(str(i) for i in range(120))}],
        "instructions": "sys",
        "tools": [],
        "max_output_tokens": 8192,
        "reasoning": {"effort": "none"},
        "tool_choice": "none",
    }
    single = _SingleRequest(
        sdk,
        oversize_body,
        tmp_path / "p63",
        live=True,
        mock_response=None,
        input_token_counter=counter,
        max_input_tokens=60,
    )
    with pytest.raises(RequestConstraintError, match="input_token_limit_exceeded"):
        single.create(
            model=MODEL,
            input=oversize_body["input"],
            instructions=oversize_body["instructions"],
            tools=[],
            max_output_tokens=8192,
        )
    sdk.responses.with_raw_response.create.assert_not_called()

    # 3. BoundedResponses checks rebuilt final body before SDK.
    p65_instr = budget_instructions(4, 3)
    p65_tools = p65_schemas()
    first_req = {
        "model": MODEL,
        "instructions": p65_instr,
        "input": [
            {
                "role": "user",
                "content": json.dumps({"automatic_version_probe": {"status": "no_data"}}),
            }
        ],
        "tools": p65_tools,
        "max_output_tokens": 8192,
        "tool_choice": "auto",
    }
    checkpoint = {
        "phase": "model_started",
        "budget": {"attempt": 1, "model_requests": 1, "tool_executions": 1},
        "records": [{"observation_id": "o1", "name": "get_running_version", "result": {"status": "no_data"}}],
        "history": [],
        "pending": [],
        "trace": [],
    }
    from investigation.models import InvestigationSession

    session = InvestigationSession(
        session_id="ses-wrapper-check",
        incident_id="inc-wrapper-check",
        alert_fingerprint="fp-wrapper-check",
        service="order-service",
        status=SessionStatus.RUNNING,
        attempt_count=1,
        human_request_count=0,
        current_attempt_rounds=1,
        current_attempt_tool_calls=1,
        total_model_requests=1,
        total_tool_executions=1,
    )
    p65_dir = tmp_path / "p65"
    p65_dir.mkdir(parents=True, exist_ok=True)
    bounded = BoundedResponses(
        p65_dir,
        first_req,
        snapshot=lambda: (session, checkpoint),
        sdk=sdk,
        expected_first_sha256=sha(encoded(first_req)),
        input_token_counter=counter,
        max_input_tokens=30,
    )
    with pytest.raises(RequestConstraintError, match="input_token_limit_exceeded"):
        bounded.create(
            model=MODEL,
            input=[],
            instructions=p65_instr,
            tools=p65_tools,
            max_output_tokens=8192,
        )
    sdk.responses.with_raw_response.create.assert_not_called()


def test_three_round_runtime_delivery_with_real_token_counter(tmp_path, monkeypatch):
    counter = build_deepseek_input_token_counter()
    limits = RuntimeLimits(
        max_attempts=1, max_model_requests=5, max_tool_executions=8,
        max_resumes=0, max_duration_seconds=300, model_timeout_seconds=30,
    )
    manager = create_runtime(tmp_path, limits=limits)
    session = create_session(manager)
    for tool_name in ("get_running_version", "query_metrics", "query_logs"):
        monkeypatch.setitem(
            tool_registry[tool_name],
            "func",
            lambda _name=tool_name, **kwargs: {
                "status": "no_data",
                "reason": "offline_synthetic_stub",
                "tool": _name,
                "args": kwargs,
            },
        )

    sent_bodies = []
    pre_send_counts = []
    sdk_usages = []
    r1_calls = [call("get_service_dependencies", {"service": "order-service"}, "call-deps-1")]
    r2_calls = [call("query_metrics", {"service": "order-service", "metric": "http_5xx_rate"}, "call-metrics-2")]

    def receive(request):
        body = json.loads(request.read())
        assert body["model"] == "deepseek-flash"
        assert body["max_output_tokens"] == 8192
        assert valid_chain(body["input"])
        counted = counter(body)
        assert counted <= 65536
        sent_bodies.append(body)
        pre_send_counts.append(counted)
        round_no = len(sent_bodies)
        if round_no == 1:
            data = _model_payload(calls=r1_calls, input_tokens=counted + 5, output_tokens=20)
        elif round_no == 2:
            outputs = {
                i["call_id"]: i
                for i in body["input"]
                if i.get("type") == "function_call_output"
            }
            assert "call-deps-1" in outputs
            data = _model_payload(calls=r2_calls, input_tokens=counted + 7, output_tokens=22)
        else:
            outputs = {
                i["call_id"]: i
                for i in body["input"]
                if i.get("type") == "function_call_output"
            }
            assert "call-metrics-2" in outputs
            data = _model_payload(
                answer="离线合成三轮交付结束，外部证据均为 offline_synthetic_stub。",
                input_tokens=counted + 9,
                output_tokens=30,
            )
        sdk_usages.append(data["usage"])
        raw = json.dumps(data).encode("utf-8")
        if round_no % 2 == 1:
            return httpx2.Response(
                200,
                headers={"content-type": "application/json", "content-encoding": "gzip"},
                content=gzip.compress(raw),
            )
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            content=raw,
        )

    with OpenAI(
        api_key="OFFLINE_NON_CREDENTIAL",
        base_url="https://provider.invalid",
        http_client=httpx2.Client(transport=httpx2.MockTransport(receive)),
    ) as sdk:
        agent = Agent(
            sdk,
            "离线三轮验证。",
            input_token_counter=counter,
        )
        finished = Worker(manager, agent, provider_verified=True).run_once()

    assert finished.status == SessionStatus.COMPLETED
    assert finished.total_model_requests == 3
    assert finished.total_tool_executions == 3  # initial probe + 2 model calls
    assert len(pre_send_counts) == 3
    # Distinct pre-send token counts vs server-reported usage numbers are preserved.
    assert pre_send_counts != [u["input_tokens"] for u in sdk_usages]
    assert pre_send_counts[1] > pre_send_counts[0]
    checkpoint = manager.store.load_checkpoint(finished)
    Checkpoint.model_validate(checkpoint)
    assert checkpoint["execution_limits"] == limits.model_dump(mode="json")
    assert valid_chain(checkpoint["history"])
    call_ids = {r.get("call_id") for r in checkpoint["records"] if r.get("call_id")}
    assert {"call-deps-1", "call-metrics-2"}.issubset(call_ids)


def test_saved_stage1_request_and_followup_compatibility():
    if not SAVED_REQUEST_01.exists() or not SAVED_RESPONSE_01.exists():
        pytest.skip("operator stage1 private capture not present in checkout")
    counter = build_deepseek_input_token_counter()
    req1 = json.loads(SAVED_REQUEST_01.read_text(encoding="utf-8"))
    resp1 = json.loads(SAVED_RESPONSE_01.read_text(encoding="utf-8"))
    counted_round1 = counter(req1)
    assert counted_round1 <= 65536
    assert counted_round1 == resp1["usage"]["input_tokens"]

    # Construct an offline Round-2 request from saved response-01 output items
    # paired with offline placeholder outputs (never modifying saved files).
    req2 = deepcopy(req1)
    for item in resp1["output"]:
        if item.get("type") == "reasoning":
            req2["input"].append({
                "type": "reasoning",
                "id": item["id"],
                "summary": item.get("summary", []),
                "content": item.get("content", []),
                "encrypted_content": item.get("encrypted_content"),
            })
        elif item.get("type") == "function_call":
            req2["input"].append({
                "type": "function_call",
                "id": item["id"],
                "call_id": item["call_id"],
                "name": item["name"],
                "arguments": item["arguments"],
            })
    for item in resp1["output"]:
        if item.get("type") == "function_call":
            req2["input"].append({
                "type": "function_call_output",
                "call_id": item["call_id"],
                "output": '{"status":"no_data","reason":"offline_synthetic_followup"}',
            })
    counted_round2 = counter(req2)
    assert counted_round2 > counted_round1
    assert counted_round2 <= 65536
