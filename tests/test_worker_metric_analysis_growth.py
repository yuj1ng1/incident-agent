"""P5.3b.1 measurements retained as P5.3b.2 replacement regressions.

Every scenario makes at most seven calls to an in-memory Mock Provider through
the real Agent/ModelClient boundary. All stores and optional measurements are
temporary. Reference tokenizer counts are not DeepSeek usage or billing tokens.
"""
from collections import Counter
from copy import deepcopy
from datetime import timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from agent.agent import Agent
from config import MAX_AGENT_LOOPS, MAX_TOOL_CALLS
from investigation.checkpoint import MAX_CHECKPOINT_BYTES, encode, valid_chain
from investigation.evidence_policy import effective_records
from investigation.models import Checkpoint, SessionStatus
from investigation.state import RuntimeErrorBase
from investigation.store import RuntimeStore
from investigation.worker import Worker
from prompts.agent_prompt import agent_prompt
from runtime_support import FixtureAgent, call, create_runtime, create_session, response
from test_metric_analysis import COLLECTED, history


def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def analyses(messages):
    """Recognize both the rebuild field and Runtime's supplemental input."""
    found = []
    for item in messages:
        if item.get("role") != "user" or not isinstance(item.get("content"), str):
            continue
        try:
            payload = json.loads(item["content"].rsplit("\n", 1)[-1])
        except json.JSONDecodeError:
            continue
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("metric_analysis"), dict)
        ):
            found.append(payload)
    return found


def function_items(messages):
    return [
        item for item in messages
        if item.get("type") in {"function_call", "function_call_output"}
    ]


def checkpoint_measurement(manager, session_id):
    saved = manager.store.get(session_id)
    with manager.store.read() as db:
        text = db.execute(
            "SELECT data FROM investigation_checkpoints WHERE checkpoint_id=?",
            (saved.checkpoint_id,),
        ).fetchone()[0]
        retained = db.execute(
            "SELECT COUNT(*), SUM(length(CAST(data AS BLOB))) "
            "FROM investigation_checkpoints WHERE session_id=?",
            (session_id,),
        ).fetchone()
    checkpoint = json.loads(text)
    Checkpoint.model_validate(checkpoint)
    reopened = RuntimeStore(manager.store.path, read_only=True)
    assert reopened.load_checkpoint(reopened.get(session_id)) == checkpoint
    assert encode(checkpoint) == text
    return {
        "bytes": len(text.encode("utf-8")),
        "retained_count": retained[0],
        "retained_bytes": retained[1],
        "analysis_messages": len(analyses(checkpoint.get("history", []))),
        "phase": checkpoint["phase"],
        "reopened_equal": True,
    }


@pytest.fixture
def offline(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("external_network_forbidden"))
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    yield
    forbidden.assert_not_called()


@pytest.fixture(scope="module")
def reference_encoder():
    """Use an already verified local cache only; never download a tokenizer."""
    if importlib.util.find_spec("tiktoken") is None:
        return None
    url = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
    expected = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
    cache = os.environ.get(
        "TIKTOKEN_CACHE_DIR",
        os.environ.get(
            "DATA_GYM_CACHE_DIR",
            str(Path(tempfile.gettempdir()) / "data-gym-cache"),
        ),
    )
    if not cache:
        return None
    path = Path(cache) / hashlib.sha1(url.encode()).hexdigest()
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        return None
    import tiktoken

    return tiktoken.get_encoding("o200k_base")


def run_scenario(
    root,
    scenario,
    reference_encoder,
    *,
    analysis_transform=None,
):
    clock = SimpleNamespace(now=COLLECTED)
    holder = SimpleNamespace(manager=create_runtime(root, clock=lambda: clock.now))
    session = create_session(holder.manager)
    rows = []
    executions = []
    provider = Mock()
    provider.with_options.return_value = provider
    historical = scenario == "historical"
    name = "query_metric_range" if historical else "query_metrics"
    arguments = {"service": session.service, "metric": "http_request_rate"}
    if historical:
        sample = history().result
        arguments.update(
            start=sample["requested_range"]["start"],
            end=sample["requested_range"]["end"],
            step_seconds=sample["step_seconds"],
        )

    def tool_result(item):
        if item.name == "get_running_version":
            return {"status": "ok", "service": session.service, "commit_sha": "a" * 40}
        assert item.name == name
        if historical:
            return deepcopy(sample)
        timestamp = clock.now.isoformat()
        return {
            "status": "ok",
            "service": session.service,
            "metric": arguments["metric"],
            "value": 0.0,
            "evaluated_at": timestamp,
            "query_started_at": timestamp,
            "query_finished_at": timestamp,
        }

    def request(**body):
        body = deepcopy(body)
        manager = holder.manager
        saved = manager.store.get(session.session_id)
        checkpoint = manager.store.load_checkpoint(saved)
        current = effective_records(
            checkpoint["records"], checkpoint["identity"], clock.now, session.service,
        )
        projected = analyses(body["input"])
        fingerprints = Counter(
            hashlib.sha256(json_text(item["metric_analysis"]).encode()).hexdigest()
            for item in projected
        )
        text = json_text(body["input"])
        request_text = json_text(body)
        remaining_tools = min(
            MAX_TOOL_CALLS - saved.current_attempt_tool_calls,
            manager.limits.max_tool_executions - saved.total_tool_executions,
        )
        remaining_rounds = min(
            MAX_AGENT_LOOPS - saved.current_attempt_rounds + 1,
            manager.limits.max_model_requests - saved.total_model_requests + 1,
        )
        assert f"剩余工具执行次数 {remaining_tools}" in body["instructions"]
        assert f"剩余模型响应轮数（含本轮） {remaining_rounds}" in body["instructions"]
        row = {
            "round": len(rows) + 1,
            "attempt": saved.attempt_count,
            "attempt_round": saved.current_attempt_rounds,
            "analysis_messages": len(projected),
            "runtime_supplements": sum(
                item.get("context_origin") == "runtime_derived_metric_analysis"
                for item in projected
            ),
            "identical_analysis_copies_beyond_first": sum(
                n - 1 for n in fingerprints.values()
            ),
            "analysis_fingerprints": dict(fingerprints),
            "source_observation_ids": [
                sorted({
                    source for report in item["metric_analysis"].get("reports", [])
                    for source in report["source_observation_ids"]
                }) for item in projected
            ],
            "rebuild_user_message_fingerprints": [
                hashlib.sha256(json_text(item).encode()).hexdigest()
                for item in body["input"]
                if any("recovery" in payload for payload in analyses([item]))
            ],
            "input_json_bytes": len(text.encode("utf-8")),
            "request_json_bytes": len(request_text.encode("utf-8")),
            "reference_o200k_input_json_tokens": (
                len(reference_encoder.encode_ordinary(text))
                if reference_encoder else None
            ),
            "reference_o200k_request_json_tokens": (
                len(reference_encoder.encode_ordinary(request_text))
                if reference_encoder else None
            ),
            "provider_usage_tokens": None,
            "valid_input_chain": valid_chain(body["input"]),
            "valid_saved_chain": valid_chain(checkpoint.get("history", [])),
            "chain_fresh": all(
                record in current for record in checkpoint["records"]
                if record["result"].get("status") == "ok"
                or record["name"] == "query_metrics_aligned"
            ),
            "function_calls": [
                item["call_id"] for item in body["input"]
                if item.get("type") == "function_call"
            ],
            "function_outputs": [
                item["call_id"] for item in body["input"]
                if item.get("type") == "function_call_output"
            ],
            "model_requests": saved.total_model_requests,
            "tool_executions": saved.total_tool_executions,
            "cache_hits": saved.cache_hits,
            "tool_schema_count": len(body["tools"]),
            "remaining_tools": remaining_tools,
            "remaining_attempt_rounds_including_this": remaining_rounds,
            "checkpoint_before_request": checkpoint_measurement(
                manager, session.session_id,
            ),
        }
        assert row["function_calls"] == row["function_outputs"]
        assert row["valid_input_chain"]
        # Complete-chain inputs must retain each real item byte-for-byte.
        if (
            row["valid_saved_chain"] and row["chain_fresh"]
            and scenario != "unverified"
        ):
            retained = body["input"][:-1] if row["runtime_supplements"] else body["input"]
            assert retained == [
                item for item in checkpoint["history"]
                if not Worker._is_runtime_metric_analysis_message(item)
            ]
            assert function_items(body["input"]) == function_items(
                checkpoint["history"],
            )
        rows.append(row)
        if scenario == "retry" and len(rows) == 4:
            row["outcome"] = "TimeoutError"
            raise TimeoutError("fixture provider timeout")
        row["outcome"] = "completed"
        return response() if len(rows) == 7 else response(
            answer="", calls=[call(name, arguments, f"metric-{len(rows)}")],
        )

    provider.responses.create.side_effect = request

    def boundary(phase, session_id):
        if phase == "model_checkpoint_committed":
            rows[-1]["checkpoint_after_response"] = checkpoint_measurement(
                holder.manager, session_id,
            )
            if scenario == "expired" and len(rows) == 3:
                clock.now += timedelta(seconds=16)

    while True:
        fixture = FixtureAgent(tool_result=tool_result, clock=lambda: clock.now)
        fixture.agent_prompt = agent_prompt
        # Keep the actual instruction/schema construction and ModelClient call.
        fixture.request_durable_response = (
            Agent.request_durable_response.__get__(fixture)
        )
        fixture.model_client.client = provider
        worker = Worker(
            holder.manager, fixture, boundary_hook=boundary,
            provider_verified=scenario != "unverified",
        )
        if scenario == "disabled":
            worker._metric_analysis_context = Mock(return_value=None)
        if analysis_transform is not None:
            original_analysis = worker._metric_analysis_context

            def transformed_analysis(service, records):
                return analysis_transform(
                    original_analysis(service, records), len(rows) + 1,
                )

            worker._metric_analysis_context = transformed_analysis
        finished = worker.run_once()
        executions.extend(fixture.executions)
        if finished.status != SessionStatus.WAITING_RETRY:
            break
        assert scenario == "retry" and len(rows) == 4
        rows[-1]["checkpoint_after_failure"] = checkpoint_measurement(
            holder.manager, session.session_id,
        )
        saved = holder.manager.store.get(session.session_id)
        failed_history = holder.manager.store.load_checkpoint(saved)["history"]
        assert valid_chain(failed_history)
        assert [
            item["call_id"] for item in failed_history
            if item.get("type") == "function_call"
        ] == ["metric-1", "metric-2", "metric-3"]
        clock.now = finished.next_retry_at
        holder.manager = create_runtime(root, clock=lambda: clock.now)

    assert finished.status == SessionStatus.COMPLETED
    assert provider.responses.create.call_count == 7
    assert finished.total_model_requests == 7
    assert all(
        call.kwargs == {"max_retries": 0, "timeout": 60}
        for call in provider.with_options.call_args_list
    )
    checkpoint = holder.manager.store.load_checkpoint(finished)
    assert valid_chain(checkpoint["history"])
    assert all(row["checkpoint_before_request"]["retained_count"] <= 4 for row in rows)
    report = {
        "scenario": scenario,
        "serialization": (
            "json.dumps(ensure_ascii=False, allow_nan=False); not SDK wire bytes"
        ),
        "tokenizer": (
            "o200k_base cached, reference JSON text only"
            if reference_encoder else None
        ),
        "rows": rows,
        "final": checkpoint_measurement(holder.manager, session.session_id),
        "attempts": finished.attempt_count,
        "model_requests": finished.total_model_requests,
        "tool_executions": finished.total_tool_executions,
        "cache_hits": finished.cache_hits,
        "executions": executions,
        "retained_function_items": function_items(checkpoint["history"]),
    }
    output = os.environ.get("INCIDENT_P53B1_REPORT_DIR")
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        (Path(output) / f"{scenario}.json").write_text(
            json_text(report), encoding="utf-8",
        )
    return report, holder.manager, finished, checkpoint


@pytest.mark.parametrize(
    "scenario, counts, tools, hits",
    [
        # P5.3b.1 measured 0,1,2,3,4,5,6; complete-chain supplements now replace.
        ("fresh", [0, 1, 1, 1, 1, 1, 1], 2, 5),
        ("disabled", [0, 0, 0, 0, 0, 0, 0], 2, 5),
        ("unverified", [0, 1, 1, 1, 1, 1, 1], 2, 5),
        ("historical", [0, 1, 1, 1, 1, 1, 1], 2, 5),
        ("expired", [0, 1, 1, 1, 1, 1, 1], 3, 4),
        # The unmarked rebuild user message remains intact after a new attempt.
        ("retry", [0, 1, 1, 1, 1, 2, 2], 3, 4),
    ],
)
def test_seven_round_context_growth_characterization(
    tmp_path,
    offline,
    reference_encoder,
    scenario,
    counts,
    tools,
    hits,
):
    report, _, _, checkpoint = run_scenario(tmp_path, scenario, reference_encoder)
    assert [row["analysis_messages"] for row in report["rows"]] == counts
    assert report["tool_executions"] == tools
    assert report["cache_hits"] == hits
    assert len(report["executions"]) == tools
    if scenario == "retry":
        assert report["rows"][-1]["tool_schema_count"] > 0
    else:
        assert report["rows"][-1]["tool_schema_count"] == 0
    if scenario in {"fresh", "disabled"}:
        for row in report["rows"]:
            expected = [f"metric-{n}" for n in range(1, row["round"])]
            assert row["function_calls"] == expected
            assert row["chain_fresh"]
            assert row["valid_saved_chain"] == (row["round"] > 1)
        assert report["rows"][-1]["identical_analysis_copies_beyond_first"] == 0
    if scenario == "fresh":
        assert all(
            len(set(map(tuple, row["source_observation_ids"]))) == 1
            for row in report["rows"][1:]
        )
        assert len(function_items(checkpoint["history"])) == 12
        assert report["final"]["analysis_messages"] == 1
        assert [row["runtime_supplements"] for row in report["rows"]] == counts
    if scenario == "retry":
        assert report["attempts"] == 2
        assert [row["attempt_round"] for row in report["rows"]] == [1, 2, 3, 4, 1, 2, 3]
        assert report["rows"][3]["checkpoint_after_failure"]["analysis_messages"] == 1
        assert report["rows"][4]["function_calls"] == []
        assert report["rows"][5]["function_calls"] == ["metric-5"]
        assert report["rows"][6]["function_calls"] == ["metric-5", "metric-6"]
        assert [row["runtime_supplements"] for row in report["rows"]] == [
            0, 1, 1, 1, 0, 1, 1,
        ]
        metric_records = [
            record for record in checkpoint["records"]
            if record["name"] == "query_metrics"
        ]
        assert len(metric_records) == 1
        assert {
            source for row in report["rows"]
            for ids in row["source_observation_ids"] for source in ids
        } == {metric_records[0]["observation_id"]}


@pytest.mark.parametrize("change", ["revised", "absent"])
def test_worker_delivers_latest_analysis_with_same_observation_and_persists_it(
    tmp_path,
    offline,
    reference_encoder,
    change,
):
    expected = {}

    def transform(analysis, round_number):
        if analysis is not None:
            analysis = deepcopy(analysis)
            if change == "absent" and round_number >= 4:
                analysis = None
            else:
                analysis["reason"] = f"fixture_reanalysis_round_{round_number}"
        expected[round_number] = analysis
        return analysis

    report, _, _, checkpoint = run_scenario(
        tmp_path, "fresh", reference_encoder, analysis_transform=transform,
    )
    for row in report["rows"]:
        latest = expected[row["round"]]
        assert row["runtime_supplements"] == (latest is not None)
        assert row["analysis_messages"] == (latest is not None)
        assert row["analysis_fingerprints"] == (
            {hashlib.sha256(json_text(latest).encode()).hexdigest(): 1}
            if latest is not None else {}
        )
        assert row["valid_input_chain"]
        assert row["function_calls"] == [
            f"metric-{n}" for n in range(1, row["round"])
        ]
    sources = {
        source for row in report["rows"]
        for ids in row["source_observation_ids"] for source in ids
    }
    assert len(sources) == 1
    # Model input and reopened Checkpoints contain the JSON form of dataclass
    # reports, whose in-memory tuples serialize as arrays.
    latest = json.loads(json_text(expected[7]))
    assert [item["metric_analysis"] for item in analyses(checkpoint["history"])] == (
        [latest] if latest is not None else []
    )
    assert len(function_items(checkpoint["history"])) == 12
    assert (report["model_requests"], report["tool_executions"], report["cache_hits"]) == (
        7, 2, 5,
    )


def test_retry_preserves_original_rebuild_user_while_latest_runtime_input_replaces(
    tmp_path,
    monkeypatch,
    offline,
    reference_encoder,
):
    expected = {}

    def transform(analysis, round_number):
        if analysis is not None:
            analysis = deepcopy(analysis)
            analysis["reason"] = f"fixture_reanalysis_round_{round_number}"
        expected[round_number] = analysis
        return analysis

    if output := os.environ.get("INCIDENT_P53B1_REPORT_DIR"):
        monkeypatch.setenv(
            "INCIDENT_P53B1_REPORT_DIR", str(Path(output) / "revised-retry"),
        )
    report, _, _, checkpoint = run_scenario(
        tmp_path, "retry", reference_encoder, analysis_transform=transform,
    )
    assert report["attempts"] == 2
    assert [row["analysis_messages"] for row in report["rows"]] == [
        0, 1, 1, 1, 1, 2, 2,
    ]
    assert [row["runtime_supplements"] for row in report["rows"]] == [
        0, 1, 1, 1, 0, 1, 1,
    ]
    original_rebuild = report["rows"][4]["rebuild_user_message_fingerprints"]
    assert len(original_rebuild) == 1
    assert all(
        row["rebuild_user_message_fingerprints"] == original_rebuild
        for row in report["rows"][5:]
    )
    for row in report["rows"][5:]:
        assert row["analysis_fingerprints"] == {
            hashlib.sha256(json_text(expected[5]).encode()).hexdigest(): 1,
            hashlib.sha256(json_text(expected[row["round"]]).encode()).hexdigest(): 1,
        }
        assert row["valid_input_chain"] and row["chain_fresh"]
    final = analyses(checkpoint["history"])
    assert "context_origin" not in final[0] and "recovery" in final[0]
    assert final[0]["metric_analysis"] == json.loads(json_text(expected[5]))
    assert final[1]["context_origin"] == "runtime_derived_metric_analysis"
    assert final[1]["metric_analysis"] == json.loads(json_text(expected[7]))
    assert final[0]["metric_analysis"] != final[1]["metric_analysis"]
    assert report["rows"][5]["function_calls"] == ["metric-5"]
    assert report["rows"][6]["function_calls"] == ["metric-5", "metric-6"]
    assert len(function_items(checkpoint["history"])) == 4
    assert (report["model_requests"], report["tool_executions"], report["cache_hits"]) == (
        7, 3, 4,
    )


def test_synthetic_checkpoint_margin_is_recovered_by_replacing_repeated_analysis(
    tmp_path,
    offline,
    reference_encoder,
):
    report, manager, finished, current = run_scenario(
        tmp_path, "fresh", reference_encoder,
    )
    assert len(analyses(current["history"])) == 1
    # Reconstruct P5.3b.1's six supplements on a temporary copy only, then use
    # the production context builder to clean that legacy complete chain.
    repeated = deepcopy(current)
    supplement = next(
        item for item in repeated["history"]
        if Worker._is_runtime_metric_analysis_message(item)
    )
    repeated["history"] += [deepcopy(supplement) for _ in range(5)]
    reduced = deepcopy(repeated)
    worker = Worker(
        manager, FixtureAgent(clock=lambda: COLLECTED), provider_verified=True,
    )
    reduced["history"] = worker._context(finished.session_id, reduced)
    assert len(analyses(reduced["history"])) == 1
    assert len(analyses(repeated["history"])) == 6
    assert function_items(reduced["history"]) == function_items(repeated["history"])
    assert valid_chain(reduced["history"])
    extra = len(encode(repeated).encode()) - len(encode(reduced).encode())
    assert extra > 0
    target = MAX_CHECKPOINT_BYTES - extra // 2
    padding = []
    while True:
        size = len(encode(reduced).encode())
        overhead = len(json_text({"role": "user", "content": ""}).encode()) + 2
        if target - size < overhead:
            break
        item = {"role": "user", "content": "x" * min(60000, target - size - overhead)}
        padding.append(item)
        reduced["history"].insert(len(padding) - 1, item)
    repeated["history"] = padding + repeated["history"]
    assert valid_chain(repeated["history"])
    with manager.store.transaction() as db:
        manager.store.checkpoint(finished, reduced, db)
        manager.store.save(finished, db)
    saved_id = finished.checkpoint_id
    with pytest.raises(RuntimeErrorBase, match="^checkpoint_size_limit$"):
        with manager.store.transaction() as db:
            manager.store.checkpoint(finished, repeated, db)
            manager.store.save(finished, db)
    reopened = RuntimeStore(manager.store.path, read_only=True)
    assert reopened.get(finished.session_id).checkpoint_id == saved_id
    assert reopened.load_checkpoint(reopened.get(finished.session_id)) == reduced
    if output := os.environ.get("INCIDENT_P53B1_REPORT_DIR"):
        (Path(output) / "checkpoint_margin.json").write_text(json_text({
            "synthetic_prior_history": True,
            "model_requests": report["model_requests"],
            "limit_bytes": MAX_CHECKPOINT_BYTES,
            "reduced_checkpoint_bytes": len(encode(reduced).encode()),
            "repeated_checkpoint_bytes": len(json_text(repeated).encode()),
            "duplicate_delta_bytes": extra,
            "production_context_replaced_legacy_supplements": True,
            "normal_run_analysis_messages": report["final"]["analysis_messages"],
            "rejected": "checkpoint_size_limit",
            "previous_checkpoint_preserved": True,
        }), encoding="utf-8")
