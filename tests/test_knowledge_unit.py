"""Unit tests for knowledge models, Markdown chunker, and embeddings."""
from datetime import datetime, timezone
import hashlib
import json
import math
import struct

import httpx2
import pytest

from knowledge.chunker import (
    DEFAULT_MAX_MARKDOWN_BYTES,
    chunk_markdown,
    compute_content_sha256,
    deterministic_document_id,
    deterministic_version_id,
    normalize_utf8_markdown,
)
from knowledge.embeddings import (
    EmbeddingConfig,
    OfflineHashEmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    build_embedding_provider,
    cosine_similarity,
    decode_float32,
    encode_float32,
    load_embedding_config_from_env,
    validate_vector,
)
from knowledge.models import (
    EmbeddingError,
    KnowledgeChunk,
    KnowledgeValidationError,
    SearchHit,
    SearchResult,
)


def test_chunk_markdown_preserves_headings_code_fences_and_exact_line_ranges():
    markdown = "\r\n".join(
        [
            "# 订单服务 HTTP 5xx 手册",
            "",
            "## 故障现象",
            "当错误码 `ERR_ORDER_TIMEOUT` 出现时，检查下游。",
            "",
            "```bash",
            "# 这是代码注释不是标题",
            "curl -s http://127.0.0.1:8001/health",
            "```",
            "",
            "## 处置步骤",
            "### 检查数据库连接池",
            "确认 PostgreSQL 连接数是否打满。",
        ]
    )
    chunked = chunk_markdown(
        service="order-service",
        source="runbooks/order_5xx.md",
        raw_markdown=markdown,
    )
    assert chunked.title == "订单服务 HTTP 5xx 手册"
    assert chunked.service == "order-service"
    assert chunked.content_sha256 == compute_content_sha256(
        normalize_utf8_markdown(markdown)
    )
    assert chunked.document_id == deterministic_document_id(
        "order-service",
        "runbooks/order_5xx.md",
    )
    assert chunked.version_id == deterministic_version_id(
        chunked.document_id,
        chunked.content_sha256,
    )

    lines = chunked.raw_markdown.split("\n")
    assert len(chunked.chunks) == 2
    first, second = chunked.chunks
    assert first.ordinal == 0
    assert first.heading_path == "订单服务 HTTP 5xx 手册 > 故障现象"
    assert first.start_line == 3
    assert first.end_line == 9
    assert first.chunk_text == "\n".join(lines[first.start_line - 1 : first.end_line])
    assert "# 这是代码注释不是标题" in first.chunk_text

    assert second.ordinal == 1
    assert second.heading_path == "订单服务 HTTP 5xx 手册 > 处置步骤 > 检查数据库连接池"
    assert second.start_line == 12
    assert second.end_line == 13
    assert second.chunk_text == "\n".join(lines[second.start_line - 1 : second.end_line])

    # Re-chunking identical content produces identical deterministic IDs.
    again = chunk_markdown(
        service="order-service",
        source="runbooks/order_5xx.md",
        raw_markdown=markdown,
    )
    assert [c.chunk_id for c in again.chunks] == [c.chunk_id for c in chunked.chunks]


def test_chunk_markdown_splits_long_sections_and_rejects_bad_inputs():
    long_lines = ["# 长文档", "", "## 大段落"]
    long_lines.extend([f"line-{idx:03d}: " + ("x" * 60) for idx in range(30)])
    chunked = chunk_markdown(
        service="order-service",
        source="long.md",
        raw_markdown="\n".join(long_lines),
        max_chunk_chars=250,
    )
    assert len(chunked.chunks) > 3
    lines = chunked.raw_markdown.split("\n")
    for idx, chunk in enumerate(chunked.chunks):
        assert chunk.ordinal == idx
        assert chunk.heading_path == "长文档 > 大段落"
        assert chunk.chunk_text == "\n".join(lines[chunk.start_line - 1 : chunk.end_line])

    with pytest.raises(KnowledgeValidationError, match="markdown_content_empty"):
        chunk_markdown(service="order-service", source="empty.md", raw_markdown="   \n\t ")

    with pytest.raises(KnowledgeValidationError, match="markdown_invalid_utf8"):
        chunk_markdown(service="order-service", source="bad.md", raw_markdown=b"\xff\xfe\xfd")

    with pytest.raises(KnowledgeValidationError, match="markdown_exceeds_max_bytes"):
        chunk_markdown(
            service="order-service",
            source="huge.md",
            raw_markdown="a" * (DEFAULT_MAX_MARKDOWN_BYTES + 1),
        )

    with pytest.raises(KnowledgeValidationError, match="markdown_contains_nul_byte"):
        chunk_markdown(service="order-service", source="nul.md", raw_markdown="# a\x00b")


def test_float32_vector_validation_and_roundtrip():
    vec = [0.6, 0.8, 0.0, -0.5]
    blob = encode_float32(vec, expected_dim=4)
    assert len(blob) == 16
    decoded = decode_float32(blob, expected_dim=4)
    assert math.isclose(decoded[0], 0.6, rel_tol=1e-6)
    assert math.isclose(cosine_similarity(decoded, decoded), 1.0, rel_tol=1e-6)

    with pytest.raises(EmbeddingError, match="embedding_dimension_mismatch"):
        encode_float32(vec, expected_dim=3)
    with pytest.raises(EmbeddingError, match="embedding_dimension_mismatch"):
        decode_float32(blob, expected_dim=5)
    with pytest.raises(EmbeddingError, match="zero_norm_embedding"):
        encode_float32([0.0, 0.0, 0.0, 0.0])
    with pytest.raises(EmbeddingError, match="zero_norm_embedding"):
        decode_float32(struct.pack("<4f", 0.0, 0.0, 0.0, 0.0))
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(EmbeddingError, match="non_finite_embedding_value"):
            validate_vector([1.0, bad])
        with pytest.raises(EmbeddingError, match="non_finite_embedding_value"):
            decode_float32(struct.pack("<2f", 1.0, bad))
    with pytest.raises(EmbeddingError, match="embedding_element_must_be_number"):
        validate_vector([True, 1.0])
    with pytest.raises(EmbeddingError, match="embedding_float32_overflow"):
        encode_float32([1e39, 1.0])


def test_offline_hash_embedding_provider_deterministic_and_semantic():
    provider = OfflineHashEmbeddingProvider(dimension=128)
    v1 = provider.embed_query("PG_CONN_REFUSED 数据库连接池耗尽")
    v2 = provider.embed_query("PG_CONN_REFUSED 数据库连接池耗尽")
    v_related = provider.embed_query("数据库连接池打满 PG_CONN_REFUSED")
    v_unrelated = provider.embed_query("inventory-service 缓存预热脚本")

    assert v1 == v2
    assert len(v1) == 128
    assert cosine_similarity(v1, v_related) > cosine_similarity(v1, v_unrelated)

    with pytest.raises(EmbeddingError, match="empty_embedding_input"):
        provider.embed_query("   ")


def test_openai_compatible_embedding_provider_validation_and_errors():
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/embeddings"
        assert request.headers["authorization"] == "Bearer test-secret-key"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["model"] == "text-embed-test"
        # Return out-of-order indices to verify index sorting.
        return httpx2.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                ]
            },
        )

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    cfg = EmbeddingConfig(
        provider="openai_compatible",
        model="text-embed-test",
        dimension=3,
        base_url="https://embed.example.test/v1",
        api_key="test-secret-key",
        timeout_seconds=5.0,
    )
    provider = OpenAICompatibleEmbeddingProvider(cfg, client=client)
    vectors = provider.embed_texts(["first", "second"])
    assert vectors == [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]

    # Dimension mismatch in response fails strictly.
    bad_client = httpx2.Client(
        transport=httpx2.MockTransport(
            lambda req: httpx2.Response(
                200,
                json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]},
            )
        )
    )
    bad_provider = OpenAICompatibleEmbeddingProvider(cfg, client=bad_client)
    with pytest.raises(EmbeddingError, match="embedding_dimension_mismatch"):
        bad_provider.embed_query("hello")

    # HTTP error does not leak API key.
    err_client = httpx2.Client(
        transport=httpx2.MockTransport(lambda req: httpx2.Response(502, text="bad gateway"))
    )
    err_provider = OpenAICompatibleEmbeddingProvider(cfg, client=err_client)
    with pytest.raises(EmbeddingError, match="embedding_http_status_502") as exc_info:
        err_provider.embed_query("hello")
    assert "test-secret-key" not in str(exc_info.value)


def test_embedding_config_from_env_and_models_validation(monkeypatch):
    monkeypatch.setenv("INCIDENT_AGENT_EMBEDDING_PROVIDER", "offline")
    monkeypatch.setenv("INCIDENT_AGENT_EMBEDDING_DIM", "64")
    cfg = load_embedding_config_from_env()
    provider = build_embedding_provider(cfg)
    assert provider.dimension == 64

    with pytest.raises(ValueError, match="timezone_required"):
        from knowledge.models import KnowledgeDocument

        KnowledgeDocument(
            document_id="doc-1",
            service="order-service",
            title="t",
            source="s.md",
            created_at=datetime(2026, 11, 18, 12, 0, 0),
            updated_at=datetime.now(timezone.utc),
        )

    with pytest.raises(ValueError, match="unavailable_status_requires_reason"):
        SearchResult(
            status="unavailable",
            service="order-service",
            query="5xx",
            method="hybrid_rrf",
        )

    with pytest.raises(ValueError, match="ok_status_requires_hits"):
        SearchResult(
            status="ok",
            service="order-service",
            query="5xx",
            method="hybrid_rrf",
            hits=(),
        )

    hit = SearchHit(
        document_id="doc-1",
        version_id="ver-1",
        chunk_id="chk-1",
        service="order-service",
        title="t",
        source="s.md",
        heading_path="H1",
        start_line=1,
        end_line=2,
        chunk_text="body",
        score=0.5,
    )
    chunk = KnowledgeChunk(
        chunk_id="chk-1",
        version_id="ver-1",
        ordinal=0,
        heading_path="H1",
        start_line=1,
        end_line=2,
        chunk_text="body",
        embedding_model="offline-ngram-v1",
        embedding_dim=2,
        embedding_vector=encode_float32([1.0, 0.0]),
    )
    assert hit.chunk_id == chunk.chunk_id
    assert hashlib.sha256(b"x").hexdigest()


def test_offline_bm25_and_evaluation_metrics(tmp_path):
    from knowledge.evaluator import OfflineBM25Retriever, evaluate_method, load_eval_queries

    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "order-5xx.md").write_text(
        "# order-service 5xx\n\n"
        "- **doc_id**: `order-5xx`\n"
        "- **service**: `order-service`\n"
        "- **status**: `active`\n\n"
        "## 检查回归开关\n"
        "遇到 ERR_ORDER_TIMEOUT 或 HighHttp5xxRate 时检查 simulate_code_regression。\n",
        encoding="utf-8",
    )
    (corpus_dir / "order-legacy.md").write_text(
        "# legacy\n\n"
        "- **doc_id**: `order-legacy`\n"
        "- **service**: `order-service`\n"
        "- **status**: `deprecated`\n\n"
        "## 旧手册\n"
        "禁止再使用旧版流程。\n",
        encoding="utf-8",
    )
    queries_path = tmp_path / "queries.json"
    queries_path.write_text(
        json.dumps(
            {
                "queries": [
                    {
                        "query_id": "q1",
                        "split": "dev",
                        "service": "order-service",
                        "category": "exact_code",
                        "query": "ERR_ORDER_TIMEOUT",
                        "expected_doc_ids": ["order-5xx"],
                        "forbidden_doc_ids": ["order-legacy"],
                    },
                    {
                        "query_id": "q2",
                        "split": "dev",
                        "service": "order-service",
                        "category": "no_match",
                        "query": "Kafka Consumer Lag 偏移量堆积怎么处理？",
                        "expected_doc_ids": [],
                        "forbidden_doc_ids": [],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    bm25 = OfflineBM25Retriever.from_corpus_directory(corpus_dir)
    queries = load_eval_queries(queries_path, split="dev")
    metrics = evaluate_method(
        method_name="offline_bm25",
        search_fn=bm25.search,
        queries=queries,
        split="dev",
    )
    assert metrics.recall_at_5 == 1.0
    assert metrics.mrr == 1.0
    assert metrics.no_match_accuracy == 1.0
    assert metrics.forbidden_hit_rate == 0.0
    assert metrics.mis_retrievals == ()
