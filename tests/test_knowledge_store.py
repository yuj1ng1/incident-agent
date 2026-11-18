"""MySQL 8.4 integration tests for KnowledgeStore and KnowledgeRetriever."""
from pathlib import Path
import time

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from db import tables
from db.config import get_engine
from db.tables import create_runtime_schema
from knowledge.embeddings import EmbeddingProvider, OfflineHashEmbeddingProvider
from knowledge.models import (
    EmbeddingError,
    KnowledgeStorageError,
    KnowledgeValidationError,
)
from knowledge.retriever import KnowledgeRetriever, reciprocal_rank_fusion
from knowledge.store import KnowledgeStore
from mysql_support import empty_database_url


@pytest.fixture
def kb_env(tmp_path):
    url = empty_database_url(tmp_path)
    engine = get_engine(url)
    create_runtime_schema(engine)
    store = KnowledgeStore(url)
    provider = OfflineHashEmbeddingProvider(dimension=128)
    retriever = KnowledgeRetriever(store, embedding_provider=provider)
    return url, store, provider, retriever


def test_store_requires_migrated_schema(tmp_path):
    unmigrated_url = empty_database_url(tmp_path / "empty")
    with pytest.raises(KnowledgeStorageError, match="knowledge_schema_not_migrated"):
        KnowledgeStore(unmigrated_url)


def test_import_idempotency_and_version_switching(kb_env):
    _, store, provider, retriever = kb_env
    v1_md = "\n".join(
        [
            "# order-service 5xx 排查手册",
            "",
            "## 定位步骤",
            "发生 ERR_ORDER_TIMEOUT 时检查下游连接池。",
        ]
    )
    first = store.import_document(
        service="order-service",
        source="knowledge/corpus/order-5xx.md",
        document_id="order-5xx",
        raw_markdown=v1_md,
        embedding_provider=provider,
        publish=True,
    )
    assert first.idempotent_hit is False
    assert first.published is True
    assert first.version.status == "published"

    # Re-importing exact same content is idempotent.
    second = store.import_document(
        service="order-service",
        source="knowledge/corpus/order-5xx.md",
        document_id="order-5xx",
        raw_markdown=v1_md,
        embedding_provider=provider,
        publish=True,
    )
    assert second.idempotent_hit is True
    assert second.version.version_id == first.version.version_id
    assert len(store.list_versions(service="order-service", document_id="order-5xx")) == 1

    # Import v2 as draft; published reads must still only see v1.
    v2_md = "\n".join(
        [
            "# order-service 5xx 排查手册 v2",
            "",
            "## 新版处置",
            "新增 INV_503_BACKPRESSURE 限流保护说明。",
        ]
    )
    draft = store.import_document(
        service="order-service",
        source="knowledge/corpus/order-5xx.md",
        document_id="order-5xx",
        raw_markdown=v2_md,
        embedding_provider=provider,
        publish=False,
    )
    assert draft.published is False
    assert draft.version.status == "draft"
    assert retriever.search(
        service="order-service",
        query="INV_503_BACKPRESSURE",
        method="mysql_ngram",
    ).status == "no_match"
    assert retriever.search(
        service="order-service",
        query="ERR_ORDER_TIMEOUT",
        method="hybrid_rrf",
    ).hits[0].version_id == first.version.version_id

    # Publish v2 -> v1 is archived, v2 becomes searchable, v1 no longer returned.
    switched = store.publish_version(
        service="order-service",
        document_id="order-5xx",
        version_id=draft.version.version_id,
    )
    assert switched.status == "published"
    assert store.get_version(
        service="order-service",
        document_id="order-5xx",
        version_id=first.version.version_id,
    ).status == "archived"

    after_switch = retriever.search(
        service="order-service",
        query="INV_503_BACKPRESSURE",
        method="hybrid_rrf",
    )
    assert after_switch.status == "ok"
    assert after_switch.hits[0].version_id == draft.version.version_id
    assert retriever.search(
        service="order-service",
        query="ERR_ORDER_TIMEOUT",
        method="mysql_ngram",
    ).status == "no_match"


def test_failed_import_preserves_published_version(kb_env):
    _, store, provider, retriever = kb_env
    v1_md = "# order-db 手册\n\n## 故障\n遇到 PG_CONN_REFUSED 检查端口 5432。\n"
    v1 = store.import_document(
        service="order-db",
        source="order-db.md",
        document_id="order-db-main",
        raw_markdown=v1_md,
        embedding_provider=provider,
        publish=True,
    )

    class _BrokenProvider:
        model_name = "broken-model"
        dimension = 128

        def embed_texts(self, texts):
            raise EmbeddingError("upstream_embedding_503")

        def embed_query(self, text):
            raise EmbeddingError("upstream_embedding_503")

    v2_md = "# order-db 手册 v2\n\n## 故障\n新版 PG_LOCK_WAIT_TIMEOUT 排查。\n"
    with pytest.raises(EmbeddingError, match="upstream_embedding_503"):
        store.import_document(
            service="order-db",
            source="order-db.md",
            document_id="order-db-main",
            raw_markdown=v2_md,
            embedding_provider=_BrokenProvider(),
            publish=True,
        )

    # DB transaction failure during chunk insert must roll back cleanly.
    def fail_chunk_insert(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO knowledge_chunks" in statement:
            raise OperationalError(statement, parameters, RuntimeError("injected_db_error"))

    event.listen(store.engine, "before_cursor_execute", fail_chunk_insert)
    try:
        with pytest.raises(KnowledgeStorageError, match="knowledge_database_unavailable"):
            store.import_document(
                service="order-db",
                source="order-db.md",
                document_id="order-db-main",
                raw_markdown=v2_md,
                embedding_provider=provider,
                publish=True,
            )
    finally:
        event.remove(store.engine, "before_cursor_execute", fail_chunk_insert)

    # Non-Operational SQLAlchemyError inside transaction raises knowledge_transaction_failed.
    from sqlalchemy.exc import DataError

    def fail_data_error(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO knowledge_chunks" in statement:
            raise DataError(statement, parameters, RuntimeError("bad_data"))

    event.listen(store.engine, "before_cursor_execute", fail_data_error)
    try:
        with pytest.raises(
            KnowledgeStorageError,
            match="knowledge_transaction_failed:DataError",
        ):
            store.import_document(
                service="order-db",
                source="order-db.md",
                document_id="order-db-main",
                raw_markdown=v2_md,
                embedding_provider=provider,
                publish=True,
            )
    finally:
        event.remove(store.engine, "before_cursor_execute", fail_data_error)

    # Verify v1 is intact, still published, no v2 version leaked.
    versions = store.list_versions(service="order-db", document_id="order-db-main")
    assert [v.version_id for v in versions] == [v1.version.version_id]
    res = retriever.search(
        service="order-db",
        query="PG_CONN_REFUSED",
        method="hybrid_rrf",
    )
    assert res.status == "ok"
    assert res.hits[0].version_id == v1.version.version_id


def test_cross_service_isolation_and_method_statuses(kb_env):
    _, store, provider, retriever = kb_env
    store.import_document(
        service="order-service",
        source="order.md",
        document_id="doc-order",
        raw_markdown="# 订单服务\n\n## 故障码\n出现 HighHttp5xxRate 时核对 disable_demo_regression。\n",
        embedding_provider=provider,
        publish=True,
    )
    store.import_document(
        service="inventory-service",
        source="inv.md",
        document_id="doc-inv",
        raw_markdown="# 库存服务\n\n## 背压\n出现 INV_503_BACKPRESSURE 时限流保护。\n",
        embedding_provider=provider,
        publish=True,
    )

    with pytest.raises(KnowledgeValidationError, match="document_service_mismatch"):
        store.import_document(
            service="inventory-service",
            source="order.md",
            document_id="doc-order",
            raw_markdown="# 冲突文档\n",
            embedding_provider=provider,
            publish=True,
        )

    for method in ("mysql_ngram", "vector_cosine", "hybrid_rrf"):
        inv_on_order = retriever.search(
            service="order-service",
            query="INV_503_BACKPRESSURE 库存限流",
            method=method,
        )
        assert inv_on_order.status == "no_match"
        assert inv_on_order.hits == ()

        hit_order = retriever.search(
            service="order-service",
            query="HighHttp5xxRate disable_demo_regression",
            method=method,
        )
        assert hit_order.status == "ok"
        assert all(hit.service == "order-service" for hit in hit_order.hits)
        assert hit_order.hits[0].document_id == "doc-order"

    # Embedding model mismatch isolates vector retrieval cleanly.
    other_provider = OfflineHashEmbeddingProvider(
        model_name="other-embed-model",
        dimension=128,
    )
    other_retriever = KnowledgeRetriever(store, embedding_provider=other_provider)
    vec_mismatch = other_retriever.search(
        service="order-service",
        query="HighHttp5xxRate",
        method="vector_cosine",
    )
    assert vec_mismatch.status == "no_match"

    # Unavailable embedding provider produces explicit unavailable status.
    class _FailingEmbed:
        model_name = provider.model_name
        dimension = provider.dimension

        def embed_texts(self, texts):
            raise EmbeddingError("embedding_timeout")

        def embed_query(self, text):
            raise EmbeddingError("embedding_timeout")

    broken_retriever = KnowledgeRetriever(store, embedding_provider=_FailingEmbed())
    unavail = broken_retriever.search(
        service="order-service",
        query="HighHttp5xxRate",
        method="hybrid_rrf",
    )
    assert unavail.status == "unavailable"
    assert "embedding_timeout" in (unavail.reason or "")
    assert unavail.hits == ()

    # Database failure produces explicit unavailable status.
    def fail_select(conn, cursor, statement, parameters, context, executemany):
        if "knowledge_chunks" in statement:
            raise OperationalError(statement, parameters, RuntimeError("db_lost"))

    event.listen(store.engine, "before_cursor_execute", fail_select)
    try:
        db_unavail = retriever.search(
            service="order-service",
            query="HighHttp5xxRate",
            method="mysql_ngram",
        )
        assert db_unavail.status == "unavailable"
        assert db_unavail.reason == "knowledge_database_unavailable"
        assert db_unavail.hits == ()
    finally:
        event.remove(store.engine, "before_cursor_execute", fail_select)


def test_rrf_fusion_ranking():
    fused = reciprocal_rank_fusion(
        lexical_ranks={"chk-a": 1, "chk-b": 2},
        vector_ranks={"chk-b": 1, "chk-c": 2},
        exact_matches={"chk-b"},
        rrf_k=60,
        exact_bonus=0.02,
    )
    assert fused[0][0] == "chk-b"
    assert {cid for cid, _ in fused} == {"chk-a", "chk-b", "chk-c"}


def test_run_retrieval_evaluation_end_to_end(kb_env, tmp_path):
    import json
    from knowledge.evaluator import import_corpus_into_store, run_retrieval_evaluation

    _, store, provider, _ = kb_env
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / "order-db-unavailable.md").write_text(
        "# order-db 不可用\n\n"
        "- **doc_id**: `order-db-unavailable`\n"
        "- **service**: `order-db`\n"
        "- **status**: `active`\n\n"
        "## TCP 5432 探针\n"
        "报错 PG_CONN_REFUSED 时先检查 postgres 实例与端口 5432。\n",
        encoding="utf-8",
    )
    (corpus_dir / "order-db-legacy.md").write_text(
        "# order-db 旧手册\n\n"
        "- **doc_id**: `order-db-legacy`\n"
        "- **service**: `order-db`\n"
        "- **status**: `deprecated`\n\n"
        "## 旧版重建\n"
        "PG_CONN_REFUSED 旧流程。\n",
        encoding="utf-8",
    )
    queries_path = tmp_path / "queries.json"
    queries_path.write_text(
        json.dumps(
            {
                "queries": [
                    {
                        "query_id": "q-pos",
                        "split": "dev",
                        "service": "order-db",
                        "category": "exact_code",
                        "query": "PG_CONN_REFUSED",
                        "expected_doc_ids": ["order-db-unavailable"],
                        "forbidden_doc_ids": ["order-db-legacy"],
                    },
                    {
                        "query_id": "q-neg",
                        "split": "dev",
                        "service": "order-db",
                        "category": "no_match",
                        "query": "Elasticsearch 索引分片 UNASSIGNED 如何调整堆内存？",
                        "expected_doc_ids": [],
                        "forbidden_doc_ids": [],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    stats = import_corpus_into_store(store, corpus_dir, embedding_provider=provider)
    assert stats["published_documents"] == 1
    assert stats["draft_documents"] == 1

    report = run_retrieval_evaluation(
        store=store,
        queries_path=queries_path,
        embedding_provider=provider,
        split="dev",
    )
    assert report.corpus_document_count == 1
    by_method = {m.method: m for m in report.methods}
    assert set(by_method) == {
        "mysql_ngram",
        "vector_cosine",
        "hybrid_rrf",
        "offline_bm25",
    }
    for name in ("mysql_ngram", "hybrid_rrf", "offline_bm25"):
        assert by_method[name].recall_at_5 == 1.0
        assert by_method[name].no_match_accuracy == 1.0
        assert by_method[name].forbidden_hit_rate == 0.0
