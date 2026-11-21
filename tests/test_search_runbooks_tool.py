"""Tests for search_runbooks tool, evidence policy, Worker summary, and MySQL E2E."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx2
from openai import OpenAI
import pytest

from agent.agent import Agent
from alert_parser import extract_checkpoint_alert
from db.config import get_engine
from db.tables import create_runtime_schema
from fingerprint import generate_alert_fingerprint
from incident_factory import create_incident_from_alert
from investigation.evidence_policy import TTL, cache_key, effective_records, fresh
from investigation.models import RuntimeLimits, SessionStatus
from investigation.state import RuntimeErrorBase
from investigation.worker import Worker
from knowledge.embeddings import OfflineHashEmbeddingProvider
from knowledge.evaluator import import_corpus_into_store
from knowledge.models import (
    EmbeddingError,
    KnowledgeStorageError,
    SearchHit,
    SearchResult,
)
from knowledge.retriever import KnowledgeRetriever
from knowledge.store import KnowledgeStore
from models.incident import AlertEvent, Incident
from mysql_support import empty_database_url
from runtime_support import SHA, call, create_runtime, create_session, response
import tools  # noqa: F401 - ensure all tools are registered
from tools.runbooks import (
    MAX_RUNBOOK_PAYLOAD_BYTES,
    MAX_SNIPPET_CHARS,
    build_alert_runbook_query,
    search_runbooks,
)
from tools.tool_registry import tool_registry

ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT / "knowledge" / "corpus"


def _fixture_token_bound(body):
    assert set(body) == {
        "model",
        "input",
        "instructions",
        "tools",
        "max_output_tokens",
    }
    return 64


def _http_model_response(*, calls=(), answer=""):
    data = response(answer=answer, calls=calls).model_dump(mode="json")
    data["model"] = "deepseek-flash"
    data["usage"] = {
        "input_tokens": 64,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 16,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 80,
    }
    return httpx2.Response(
        200,
        headers={"content-type": "application/json"},
        content=json.dumps(data).encode("utf-8"),
    )


def _create_prometheus_order_session(manager):
    now = manager.store.clock()
    alert = AlertEvent(
        service="order-service",
        severity="critical",
        alertname="HighHttp5xxRate",
        signal_type="numeric",
        source="prometheus",
        state="firing",
        metric="http_5xx_rate",
        value=0.35,
        threshold=0.2,
        unit="ratio",
        direction="higher_is_worse",
        labels={"service": "order-service", "severity": "critical"},
        annotations={
            "summary": "order-service HTTP 5xx ratio exceeds threshold",
            "description": "order-service /order 返回 500 比例过高",
        },
        starts_at=now - timedelta(minutes=2),
        observed_at=now,
        window="1m",
        minimum_requests=20,
    )
    fp = generate_alert_fingerprint(alert)
    incident = create_incident_from_alert(alert, fp)
    manager.incident_store.save_incident(fp, incident)
    return manager.enqueue(incident), incident


def test_search_runbooks_schema_policy_and_worker_validation(tmp_path):
    assert "search_runbooks" in tool_registry
    assert TTL["search_runbooks"] == 300

    schema = tool_registry["search_runbooks"]["schema"]
    assert schema["name"] == "search_runbooks"
    assert schema["description"]
    params = schema["parameters"]
    assert params["required"] == ["service", "query"]
    assert params["properties"]["service"]["type"] == "string"
    assert params["properties"]["query"]["type"] == "string"
    assert params["properties"]["limit"]["type"] == "integer"

    manager = create_runtime(tmp_path)
    worker = Worker(manager, Agent(Mock(), "只读"))

    worker._validate_tool(
        "search_runbooks",
        {"service": "order-service", "query": "HighHttp5xxRate"},
        "order-service",
    )
    worker._validate_tool(
        "search_runbooks",
        {"service": "order-service", "query": "5xx", "limit": 3},
        "order-service",
    )
    # Registered dependency of order-service is permitted.
    worker._validate_tool(
        "search_runbooks",
        {"service": "order-db", "query": "PG_CONN_REFUSED"},
        "order-service",
    )

    with pytest.raises(RuntimeErrorBase, match="tool_target_outside_session"):
        worker._validate_tool(
            "search_runbooks",
            {"service": "inventory-service", "query": "INV_503_BACKPRESSURE"},
            "order-service",
        )

    with pytest.raises(RuntimeErrorBase, match="invalid_tool_argument_type"):
        worker._validate_tool(
            "search_runbooks",
            {"service": "order-service", "query": "5xx", "limit": True},
            "order-service",
        )

    with pytest.raises(RuntimeErrorBase, match="invalid_tool_argument_type"):
        worker._validate_tool(
            "search_runbooks",
            {"service": "order-service", "query": "5xx", "limit": None},
            "order-service",
        )

    with pytest.raises(RuntimeErrorBase, match="invalid_tool_arguments"):
        worker._validate_tool(
            "search_runbooks",
            {"service": "order-service"},
            "order-service",
        )


def test_search_runbooks_validates_service_query_and_limit():
    unknown = search_runbooks(service="nonexistent-service", query="5xx")
    assert unknown == {
        "status": "no_data",
        "reason": "unknown_service",
        "service": "nonexistent-service",
        "hits": [],
    }

    empty_q = search_runbooks(service="order-service", query="   ")
    assert empty_q["status"] == "invalid_arguments"
    assert empty_q["reason"] == "empty_query"
    assert empty_q["hits"] == []

    long_q = search_runbooks(service="order-service", query="a" * 2001)
    assert long_q["status"] == "invalid_arguments"
    assert long_q["reason"] == "query_too_long"

    for bad_limit in (0, -2, True, False):
        bad = search_runbooks(service="order-service", query="5xx", limit=bad_limit)
        assert bad["status"] == "invalid_arguments"
        assert bad["reason"] == "invalid_limit"
        assert bad["hits"] == []

    fake_retriever = Mock()
    fake_retriever.search.return_value = SearchResult(
        status="no_match",
        service="order-service",
        query="5xx",
        method="hybrid_rrf",
    )
    with patch("tools.runbooks._build_retriever", return_value=fake_retriever):
        res = search_runbooks(service="order-service", query="5xx", limit=25)
    assert res["status"] == "no_data"
    assert res["reason"] == "no_matching_runbook"
    assert fake_retriever.search.call_args.kwargs["top_k"] == 10


def test_build_alert_runbook_query_and_agent_prompt_integration():
    now = datetime.now(timezone.utc)
    alert = AlertEvent(
        service="order-service",
        severity="critical",
        alertname="HighHttp5xxRate",
        signal_type="numeric",
        source="prometheus",
        state="firing",
        metric="http_5xx_rate",
        value=0.4,
        threshold=0.2,
        unit="ratio",
        direction="higher_is_worse",
        labels={"service": "order-service", "error_code": "ERR_CODE_REGRESSION"},
        annotations={
            "summary": "order-service HTTP 5xx ratio exceeds threshold token=supersecret123",
        },
        observed_at=now,
    )
    incident = create_incident_from_alert(alert, "fp-runbook-query")
    query = build_alert_runbook_query(incident)
    assert "order-service" in query
    assert "HighHttp5xxRate" in query
    assert "http_5xx_rate" in query
    assert "ERR_CODE_REGRESSION" in query
    assert "HTTP 5xx ratio exceeds threshold" in query
    assert "supersecret123" not in query
    assert not query.startswith("order-service HighHttp5xxRate http_5xx_rate ERR_CODE_REGRESSION order-service")

    # Legacy alert skips placeholder "legacy" alertname.
    legacy_alert = AlertEvent(
        service="order-db",
        severity="warning",
        metric="postgres_up",
        value=0.0,
        threshold=1.0,
    )
    legacy_incident = create_incident_from_alert(legacy_alert, "fp-legacy")
    legacy_query = build_alert_runbook_query(legacy_incident)
    assert legacy_query == "order-db postgres_up"
    assert "legacy" not in legacy_query

    # Empty alerts falls back to service name.
    empty_incident = Incident(
        incident_id="inc-empty",
        service="inventory-service",
        severity="warning",
        status="open",
        alerts=[],
    )
    assert build_alert_runbook_query(empty_incident) == "inventory-service"
    assert build_alert_runbook_query(None) == ""

    # Agent prompt embeds suggested_runbook_query while preserving extract_checkpoint_alert.
    agent = Agent(Mock(), "只读证据调查。")
    prompt_text = agent.build_incident_prompt(incident)
    assert "suggested_runbook_query" in prompt_text
    assert query in prompt_text
    extracted = extract_checkpoint_alert(
        {"history": [{"role": "user", "content": prompt_text}]}
    )
    assert extracted is not None
    assert extracted["alertname"] == "HighHttp5xxRate"

    # Dynamic instructions guide search_runbooks usage without replacing live evidence.
    agent.model_client.call_once = Mock(return_value=response())
    agent.request_durable_response([], 1, 1, [], 30)
    instructions = agent.model_client.call_once.call_args.args[1]
    assert "search_runbooks" in instructions
    assert "document_id@version_id#Lstart-Lend" in instructions
    assert "no_data" in instructions
    assert "tool_error" in instructions


def test_search_runbooks_error_mapping_and_payload_bounds(tmp_path):
    # Real unmigrated isolated MySQL schema reports knowledge_schema_not_migrated.
    unmigrated_url = empty_database_url(tmp_path / "unmigrated")
    with patch.dict("os.environ", {"INCIDENT_AGENT_DATABASE_URL": unmigrated_url}):
        unmigrated = search_runbooks(service="order-service", query="HighHttp5xxRate")
    assert unmigrated == {
        "status": "tool_error",
        "reason": "knowledge_schema_not_migrated",
        "service": "order-service",
        "query": "HighHttp5xxRate",
        "hits": [],
        "available": False,
    }

    cases = [
        (KnowledgeStorageError("knowledge_database_unavailable"), "knowledge_unavailable"),
        (EmbeddingError("embedding_http_status_503"), "embedding_unavailable"),
        (EmbeddingError("embedding_request_timeout"), "knowledge_timeout"),
        (TimeoutError("secret_token=do-not-leak"), "knowledge_timeout"),
    ]
    for exc, expected_reason in cases:
        with patch("tools.runbooks._build_retriever", side_effect=exc):
            res = search_runbooks(service="order-service", query="HighHttp5xxRate")
        assert res["status"] == "tool_error"
        assert res["reason"] == expected_reason
        assert res["available"] is False
        assert res["hits"] == []
        assert "do-not-leak" not in json.dumps(res, ensure_ascii=False)

    # SearchResult(status="unavailable") maps cleanly to tool_error.
    unavailable_retriever = Mock()
    unavailable_retriever.search.return_value = SearchResult(
        status="unavailable",
        service="order-service",
        query="5xx",
        method="hybrid_rrf",
        reason="embedding_provider_not_configured",
    )
    with patch("tools.runbooks._build_retriever", return_value=unavailable_retriever):
        unavail = search_runbooks(service="order-service", query="5xx")
    assert unavail["status"] == "tool_error"
    assert unavail["reason"] == "embedding_unavailable"
    assert unavail["available"] is False

    # Oversized hits stay bounded well below 65536 bytes and filter cross-service hits.
    big_hits = tuple(
        SearchHit(
            document_id=f"doc-{idx}",
            version_id=f"ver-{idx}",
            chunk_id=f"chk-{idx}",
            service="order-service" if idx < 10 else "inventory-service",
            title="订单服务排查手册 " + ("标" * 80),
            source="knowledge/corpus/order.md",
            heading_path="标题一 > 标题二 > " + ("段" * 80),
            start_line=idx * 10 + 1,
            end_line=idx * 10 + 9,
            chunk_text="排障步骤 " + ("字" * 2000),
            score=1.0 - idx * 0.05,
        )
        for idx in range(11)
    )
    big_retriever = Mock()
    big_retriever.search.return_value = SearchResult(
        status="ok",
        service="order-service",
        query="HighHttp5xxRate",
        method="hybrid_rrf",
        hits=big_hits,
        candidate_counts={"lexical": 11, "vector": 10, "vector_pool": 40},
    )
    with patch("tools.runbooks._build_retriever", return_value=big_retriever):
        bounded = search_runbooks(service="order-service", query="HighHttp5xxRate", limit=10)
    assert bounded["status"] == "ok"
    assert bounded["evidence_type"] == "reference_runbook"
    assert bounded["authoritative_for_current_state"] is False
    assert bounded["candidate_count"] == 21
    assert all(hit["document_id"] != "doc-10" for hit in bounded["hits"])
    assert all(len(hit["snippet"]) <= MAX_SNIPPET_CHARS for hit in bounded["hits"])
    encoded_bytes = len(json.dumps(bounded, ensure_ascii=False).encode("utf-8"))
    assert encoded_bytes <= MAX_RUNBOOK_PAYLOAD_BYTES
    assert encoded_bytes < 65536


def test_worker_runbook_summary_and_evidence_policy():
    now = datetime.now(timezone.utc)
    hits = [
        {
            "document_id": f"doc-{idx}",
            "version_id": f"ver-{idx}",
            "chunk_id": f"chk-{idx}",
            "title": f"标题 {idx}",
            "heading_path": f"总览 > 分节 {idx}",
            "start_line": idx * 10 + 1,
            "end_line": idx * 10 + 8,
            "snippet": f"步骤 {idx} " + ("内容" * 220),
            "score": round(0.9 - idx * 0.1, 3),
            "citation": f"doc-{idx}@ver-{idx}#L{idx * 10 + 1}-L{idx * 10 + 8}",
        }
        for idx in range(7)
    ]
    raw_result = {
        "status": "ok",
        "service": "order-service",
        "query": "HighHttp5xxRate",
        "method": "hybrid_rrf",
        "evidence_type": "reference_runbook",
        "authoritative_for_current_state": False,
        "candidate_count": 12,
        "hits": hits,
    }
    original = deepcopy(raw_result)
    summary = Worker._observation_summary(raw_result, tool_name="search_runbooks")

    assert raw_result == original
    assert summary["status"] == "ok"
    assert summary["evidence_type"] == "reference_runbook"
    assert summary["authoritative_for_current_state"] is False
    assert summary["summary_truncated"] is True
    # Keeps top-ranked hits first, never tail [-3:].
    assert [h["document_id"] for h in summary["hits"]][:3] == ["doc-0", "doc-1", "doc-2"]
    for hit in summary["hits"]:
        for key in (
            "document_id",
            "version_id",
            "chunk_id",
            "title",
            "heading_path",
            "start_line",
            "end_line",
            "citation",
        ):
            assert key in hit
    assert len(json.dumps(summary, ensure_ascii=False).encode("utf-8")) <= 5000

    # Cache TTL allows reuse within 300s, never live-health claims via effective_records.
    identity = {"commit_sha": SHA}
    args = {"service": "order-service", "query": "HighHttp5xxRate", "limit": 5}
    record = {
        "observation_id": "obs-rb-1",
        "name": "search_runbooks",
        "arguments": args,
        "call_id": "call-rb-1",
        "result": raw_result,
        "success": True,
        "collected_at": now.isoformat(),
        "identity": identity,
        "cache_key": cache_key("order-service", "search_runbooks", args, identity),
    }
    assert fresh(record, now + timedelta(seconds=299)) is True
    assert fresh(record, now + timedelta(seconds=301)) is False
    assert effective_records([record], identity, now, "order-service") == []


def test_end_to_end_investigation_with_real_mysql_corpus_and_cache(tmp_path, monkeypatch):
    kb_url = empty_database_url(tmp_path / "knowledge")
    create_runtime_schema(get_engine(kb_url))
    kb_store = KnowledgeStore(kb_url)
    provider = OfflineHashEmbeddingProvider(dimension=256)
    import_stats = import_corpus_into_store(
        kb_store,
        CORPUS_DIR,
        embedding_provider=provider,
    )
    assert import_stats["published_documents"] >= 10

    readonly_retriever = KnowledgeRetriever(
        KnowledgeStore(kb_url, read_only=True),
        embedding_provider=provider,
    )
    monkeypatch.setattr("tools.runbooks._build_retriever", lambda: readonly_retriever)

    clock = SimpleNamespace(now=datetime.now(timezone.utc))
    limits = RuntimeLimits(
        max_attempts=1,
        max_model_requests=6,
        max_tool_executions=10,
        max_resumes=0,
        max_duration_seconds=300,
        max_human_requests=0,
    )
    manager = create_runtime(tmp_path / "runtime", clock=lambda: clock.now, limits=limits)
    session, incident = _create_prometheus_order_session(manager)
    suggested_query = build_alert_runbook_query(incident)

    # Stub only live external HTTP/Prometheus/Loki tools; search_runbooks runs real MySQL RAG.
    monkeypatch.setitem(
        tool_registry["get_running_version"],
        "func",
        lambda service: {
            "status": "ok",
            "service": service,
            "commit_sha": SHA,
            "process_started_at": "2026-11-20T00:00:00Z",
            "runtime_flags": {"simulate_code_regression": True},
        },
    )
    monkeypatch.setitem(
        tool_registry["query_metrics"],
        "func",
        lambda service, metric: {
            "status": "ok",
            "service": service,
            "metric": metric,
            "value": 0.35,
            "evaluated_at": clock.now.isoformat(),
            "query_started_at": clock.now.isoformat(),
            "query_finished_at": clock.now.isoformat(),
        },
    )
    monkeypatch.setitem(
        tool_registry["query_logs"],
        "func",
        lambda service, start=None, end=None: {
            "status": "ok",
            "service": service,
            "logs": [
                {
                    "timestamp": "1763600000000000000",
                    "timestamp_utc": clock.now.isoformat(),
                    "line": "path=/order status=500 error_type=application_error exception_type=RuntimeError",
                    "labels": {"service": service},
                }
            ],
            "log_count": 1,
        },
    )

    sent_bodies = []
    observed_citation = {}

    def receive(request):
        body = json.loads(request.read())
        sent_bodies.append(body)
        round_no = len(sent_bodies)
        if round_no == 1:
            assert suggested_query in body["input"][0]["content"]
            assert any(t.get("name") == "search_runbooks" for t in body["tools"])
            return _http_model_response(
                calls=[
                    call(
                        "search_runbooks",
                        {
                            "service": "order-service",
                            "query": suggested_query,
                            "limit": 5,
                        },
                        "call-rb-1",
                    ),
                    # Cross-service call must be rejected by Worker._validate_tool.
                    call(
                        "search_runbooks",
                        {
                            "service": "inventory-service",
                            "query": "INV_503_BACKPRESSURE",
                            "limit": 3,
                        },
                        "call-rb-cross",
                    ),
                ]
            )
        if round_no == 2:
            rebuilt_text = body["input"][0]["content"]
            rebuilt_payload = json.loads(rebuilt_text.rsplit("\n", 1)[1])
            rb_pair = next(
                item
                for item in rebuilt_payload["recent_function_call_results"]
                if item["call_id"] == "call-rb-1"
            )
            assert rb_pair["use_for_current_state"] is False
            assert rb_pair["result"]["status"] == "ok"
            assert rb_pair["result"]["evidence_type"] == "reference_runbook"
            top_hit = rb_pair["result"]["hits"][0]
            observed_citation.update(top_hit)
            assert top_hit["document_id"] == "order-service-high-http-5xx"
            assert top_hit["citation"] == (
                f"{top_hit['document_id']}@{top_hit['version_id']}"
                f"#L{top_hit['start_line']}-L{top_hit['end_line']}"
            )
            cross_pair = next(
                item
                for item in rebuilt_payload["recent_function_call_results"]
                if item["call_id"] == "call-rb-cross"
            )
            assert cross_pair["result"]["status"] == "invalid_or_unallowed_tool_call"

            return _http_model_response(
                calls=[
                    # Duplicate query hits cache without consuming tool budget.
                    call(
                        "search_runbooks",
                        {
                            "service": "order-service",
                            "query": suggested_query,
                            "limit": 5,
                        },
                        "call-rb-dup",
                    ),
                    call(
                        "query_metrics",
                        {"service": "order-service", "metric": "http_5xx_rate"},
                        "call-metrics-1",
                    ),
                    call(
                        "query_logs",
                        {"service": "order-service"},
                        "call-logs-1",
                    ),
                ]
            )
        return _http_model_response(
            answer=(
                f"已参考手册 {observed_citation['citation']} 排查，"
                "并结合实时指标 http_5xx_rate=0.35 及日志 application_error 确认当前应用层回归。"
            )
        )

    with OpenAI(
        api_key="OFFLINE_NON_CREDENTIAL",
        base_url="https://provider.invalid",
        http_client=httpx2.Client(transport=httpx2.MockTransport(receive)),
    ) as sdk:
        agent = Agent(
            sdk,
            "只读证据调查。",
            input_token_counter=_fixture_token_bound,
        )
        completed = Worker(manager, agent, provider_verified=True).run_once()

    assert completed.status == SessionStatus.COMPLETED
    assert completed.total_model_requests == 3
    # Budget: 1 version probe + 1 search_runbooks + 1 query_metrics + 1 query_logs = 4
    # (cross-service rejected + duplicate search_runbooks served from cache).
    assert completed.total_tool_executions == 4
    assert completed.cache_hits == 1

    checkpoint = manager.store.load_checkpoint(completed)
    rb_records = [r for r in checkpoint["records"] if r["name"] == "search_runbooks"]
    assert len(rb_records) == 1
    rb_result = rb_records[0]["result"]
    assert rb_result["status"] == "ok"
    assert rb_result["evidence_type"] == "reference_runbook"
    assert rb_result["authoritative_for_current_state"] is False
    top = rb_result["hits"][0]
    assert top["document_id"] == "order-service-high-http-5xx"
    assert top["version_id"]
    assert top["chunk_id"]
    assert top["start_line"] >= 1
    assert top["end_line"] >= top["start_line"]
    assert top["citation"] == (
        f"{top['document_id']}@{top['version_id']}"
        f"#L{top['start_line']}-L{top['end_line']}"
    )

    # Verify the cited version and exact line span exist verbatim in MySQL KnowledgeStore.
    stored_version = kb_store.get_version(
        service="order-service",
        document_id=top["document_id"],
        version_id=top["version_id"],
    )
    stored_lines = stored_version.raw_markdown.split("\n")
    cited_span = "\n".join(stored_lines[top["start_line"] - 1 : top["end_line"]])
    assert top["snippet"].rstrip("...") in cited_span

    # Verify effective_records excludes search_runbooks while keeping live metrics/logs.
    live_records = effective_records(
        checkpoint["records"],
        checkpoint.get("identity"),
        clock.now,
        "order-service",
    )
    live_names = {r["name"] for r in live_records}
    assert "search_runbooks" not in live_names
    assert {"get_running_version", "query_metrics", "query_logs"}.issubset(live_names)

    # Verify projected incident.evidence preserves the runbook citation record.
    saved_incident = manager.incident_store.get_incident(session.incident_id)
    rb_evidence = [e for e in saved_incident.evidence if e.source == "search_runbooks"]
    assert len(rb_evidence) == 1
    projected_payload = json.loads(rb_evidence[0].detail)
    assert projected_payload["hits"][0]["document_id"] == top["document_id"]
    assert projected_payload["hits"][0]["version_id"] == top["version_id"]
    assert projected_payload["hits"][0]["chunk_id"] == top["chunk_id"]
    assert projected_payload["hits"][0]["start_line"] == top["start_line"]
    assert projected_payload["hits"][0]["end_line"] == top["end_line"]


@pytest.mark.parametrize(
    ("scenario", "expected_status", "expected_reason"),
    [
        ("no_data", "no_data", "no_matching_runbook"),
        ("knowledge_unavailable", "tool_error", "knowledge_unavailable"),
        ("knowledge_timeout", "tool_error", "knowledge_timeout"),
    ],
)
def test_worker_continues_investigation_on_runbook_no_data_and_errors(
    tmp_path,
    monkeypatch,
    scenario,
    expected_status,
    expected_reason,
):
    if scenario == "no_data":
        kb_url = empty_database_url(tmp_path / "kb-nomatch")
        create_runtime_schema(get_engine(kb_url))
        kb_store = KnowledgeStore(kb_url)
        provider = OfflineHashEmbeddingProvider(dimension=256)
        import_corpus_into_store(kb_store, CORPUS_DIR, embedding_provider=provider)
        retriever = KnowledgeRetriever(
            KnowledgeStore(kb_url, read_only=True),
            embedding_provider=provider,
        )
        monkeypatch.setattr("tools.runbooks._build_retriever", lambda: retriever)
        query_text = "Elasticsearch 索引分片 UNASSIGNED 如何调整堆内存？"
    elif scenario == "knowledge_unavailable":
        def raise_unavailable():
            raise KnowledgeStorageError("knowledge_database_unavailable")

        monkeypatch.setattr("tools.runbooks._build_retriever", raise_unavailable)
        query_text = "order-service HighHttp5xxRate"
    else:
        class _TimeoutRetriever:
            def search(self, **kwargs):
                raise TimeoutError("mysql_read_timeout")

        monkeypatch.setattr(
            "tools.runbooks._build_retriever",
            lambda: _TimeoutRetriever(),
        )
        query_text = "order-service HighHttp5xxRate"

    clock = SimpleNamespace(now=datetime.now(timezone.utc))
    manager = create_runtime(tmp_path / f"rt-{scenario}", clock=lambda: clock.now)
    session = create_session(manager)

    monkeypatch.setitem(
        tool_registry["get_running_version"],
        "func",
        lambda service: {
            "status": "ok",
            "service": service,
            "commit_sha": SHA,
            "process_started_at": "2026-11-20T00:00:00Z",
        },
    )
    monkeypatch.setitem(
        tool_registry["query_metrics"],
        "func",
        lambda service, metric: {
            "status": "ok",
            "service": service,
            "metric": metric,
            "value": 0.3,
            "evaluated_at": clock.now.isoformat(),
            "query_started_at": clock.now.isoformat(),
            "query_finished_at": clock.now.isoformat(),
        },
    )
    monkeypatch.setitem(
        tool_registry["query_logs"],
        "func",
        lambda service, start=None, end=None: {
            "status": "ok",
            "service": service,
            "logs": [
                {
                    "timestamp": "1763600000000000000",
                    "timestamp_utc": clock.now.isoformat(),
                    "line": "path=/order status=500 error_type=application_error",
                    "labels": {"service": service},
                }
            ],
            "log_count": 1,
        },
    )

    rounds = {"count": 0}

    def receive(request):
        body = json.loads(request.read())
        rounds["count"] += 1
        if rounds["count"] == 1:
            return _http_model_response(
                calls=[
                    call(
                        "search_runbooks",
                        {"service": "order-service", "query": query_text},
                        "call-rb-fail",
                    )
                ]
            )
        if rounds["count"] == 2:
            outputs = {
                item["call_id"]: json.loads(item["output"])
                for item in body["input"]
                if item.get("type") == "function_call_output"
            }
            if "call-rb-fail" in outputs:
                rb_out = outputs["call-rb-fail"]
            else:
                rebuilt = json.loads(body["input"][0]["content"].rsplit("\n", 1)[1])
                rb_out = next(
                    item["result"]
                    for item in rebuilt["recent_function_call_results"]
                    if item["call_id"] == "call-rb-fail"
                )
            assert rb_out["status"] == expected_status
            assert rb_out["reason"] == expected_reason
            if expected_status == "tool_error":
                assert rb_out["available"] is False
            return _http_model_response(
                calls=[
                    call(
                        "query_metrics",
                        {"service": "order-service", "metric": "http_5xx_rate"},
                        "call-m",
                    ),
                    call("query_logs", {"service": "order-service"}, "call-l"),
                ]
            )
        return _http_model_response(
            answer=(
                f"手册检索返回 {expected_status}({expected_reason})，"
                "已改用实时指标 http_5xx_rate=0.3 与日志完成排查，根因待进一步验证。"
            )
        )

    with OpenAI(
        api_key="OFFLINE_NON_CREDENTIAL",
        base_url="https://provider.invalid",
        http_client=httpx2.Client(transport=httpx2.MockTransport(receive)),
    ) as sdk:
        agent = Agent(
            sdk,
            "只读证据调查。",
            input_token_counter=_fixture_token_bound,
        )
        completed = Worker(manager, agent, provider_verified=True).run_once()

    assert completed.status == SessionStatus.COMPLETED
    assert completed.total_tool_executions == 4
    checkpoint = manager.store.load_checkpoint(completed)
    rb_record = next(
        r for r in checkpoint["records"] if r["name"] == "search_runbooks"
    )
    assert rb_record["result"]["status"] == expected_status
    assert rb_record["result"]["reason"] == expected_reason
    assert effective_records(
        [rb_record],
        checkpoint.get("identity"),
        clock.now,
        "order-service",
    ) == []
    saved_incident = manager.incident_store.get_incident(session.incident_id)
    assert not any(e.source == "search_runbooks" for e in saved_incident.evidence)
    assert {e.source for e in saved_incident.evidence} >= {
        "get_running_version",
        "query_metrics",
        "query_logs",
    }
