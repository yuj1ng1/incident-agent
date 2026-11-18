"""Real MySQL 8.4 tests for knowledge base migration 0006 and ngram FULLTEXT."""
from datetime import datetime, timezone
from pathlib import Path
import struct

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from db import tables
from db.config import get_engine
from db.tables import (
    create_runtime_schema,
    knowledge_chunks,
    knowledge_documents,
    knowledge_versions,
)
from mysql_support import empty_database_url

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_TABLES = (
    "knowledge_documents",
    "knowledge_versions",
    "knowledge_chunks",
)


def _alembic_config():
    return Config(str(ROOT / "alembic.ini"))


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _schema_signature(engine):
    inspector = inspect(engine)
    signature = {}
    with engine.connect() as connection:
        for table_name in KNOWLEDGE_TABLES:
            columns = {
                col["name"]: {
                    "type": str(col["type"]),
                    "nullable": col["nullable"],
                }
                for col in inspector.get_columns(table_name)
            }
            pk = sorted(inspector.get_pk_constraint(table_name)["constrained_columns"])
            uniques = sorted(
                tuple(sorted(uc["column_names"]))
                for uc in inspector.get_unique_constraints(table_name)
            )
            fks = sorted(
                (
                    tuple(fk["constrained_columns"]),
                    fk["referred_table"],
                    tuple(fk["referred_columns"]),
                )
                for fk in inspector.get_foreign_keys(table_name)
            )
            indexes = sorted(
                (
                    idx["name"],
                    tuple(idx["column_names"]),
                    bool(idx["unique"]),
                )
                for idx in inspector.get_indexes(table_name)
            )
            create_sql = connection.exec_driver_sql(
                f"SHOW CREATE TABLE `{table_name}`"
            ).one()[1]
            signature[table_name] = {
                "columns": columns,
                "pk": pk,
                "uniques": uniques,
                "fks": fks,
                "indexes": indexes,
                "has_ngram_fulltext": "FULLTEXT KEY" in create_sql
                and "WITH PARSER `ngram`" in create_sql,
            }
    return signature


def test_upgrade_0005_to_0006_downgrade_and_reupgrade(tmp_path, monkeypatch):
    url = empty_database_url(tmp_path / "upgrade-cycle")
    monkeypatch.setenv("INCIDENT_AGENT_DATABASE_URL", url)
    config = _alembic_config()

    command.upgrade(config, "0005_remediation_store")
    engine = get_engine(url)
    before_tables = set(inspect(engine).get_table_names())
    assert "remediation_state" in before_tables
    for name in KNOWLEDGE_TABLES:
        assert name not in before_tables

    command.upgrade(config, "head")
    with engine.connect() as connection:
        version = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    assert version == "0006_knowledge_base"

    after_upgrade = set(inspect(engine).get_table_names())
    for name in KNOWLEDGE_TABLES:
        assert name in after_upgrade

    sig_first = _schema_signature(engine)
    assert sig_first["knowledge_chunks"]["has_ngram_fulltext"] is True

    command.downgrade(config, "0005_remediation_store")
    after_downgrade = set(inspect(engine).get_table_names())
    assert "remediation_state" in after_downgrade
    for name in KNOWLEDGE_TABLES:
        assert name not in after_downgrade

    command.upgrade(config, "head")
    sig_second = _schema_signature(engine)
    assert sig_second == sig_first


def test_create_runtime_schema_matches_alembic_0006(tmp_path, monkeypatch):
    alembic_url = empty_database_url(tmp_path / "alembic-parity")
    monkeypatch.setenv("INCIDENT_AGENT_DATABASE_URL", alembic_url)
    command.upgrade(_alembic_config(), "head")

    runtime_url = empty_database_url(tmp_path / "runtime-parity")
    runtime_engine = get_engine(runtime_url)
    create_runtime_schema(runtime_engine)

    alembic_sig = _schema_signature(get_engine(alembic_url))
    runtime_sig = _schema_signature(runtime_engine)
    assert runtime_sig == alembic_sig
    assert set(tables.metadata.tables).issuperset(KNOWLEDGE_TABLES)


def test_knowledge_constraints_binary_ids_and_embedding_blob(tmp_path, monkeypatch):
    url = empty_database_url(tmp_path / "constraints")
    monkeypatch.setenv("INCIDENT_AGENT_DATABASE_URL", url)
    command.upgrade(_alembic_config(), "head")
    engine = get_engine(url)
    now = _utc_now()

    with engine.begin() as connection:
        for doc_id in ("doc-1", "DOC-1", "doc-1 "):
            connection.execute(
                knowledge_documents.insert().values(
                    document_id=doc_id,
                    service="order-service",
                    title=f"Title {doc_id!r}",
                    source="knowledge/corpus/order.md",
                    published_version_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )

        connection.execute(
            knowledge_versions.insert().values(
                version_id="ver-1",
                document_id="doc-1",
                content_sha256="a" * 64,
                raw_markdown="# Order Runbook\n",
                status="published",
                created_at=now,
            )
        )
        connection.execute(
            knowledge_documents.update()
            .where(knowledge_documents.c.document_id == "doc-1")
            .values(published_version_id="ver-1")
        )

        vector_bytes = struct.pack("<4f", 0.25, -0.5, 0.75, 0.125)
        connection.execute(
            knowledge_chunks.insert().values(
                chunk_id="chk-1",
                version_id="ver-1",
                ordinal=0,
                heading_path="Order Runbook",
                start_line=1,
                end_line=5,
                chunk_text="Check connection pool.",
                embedding_model="text-embedding-test",
                embedding_dim=4,
                embedding_vector=vector_bytes,
            )
        )
        connection.execute(
            knowledge_chunks.insert().values(
                chunk_id="chk-no-vec",
                version_id="ver-1",
                ordinal=1,
                heading_path="Order Runbook > Fallback",
                start_line=6,
                end_line=10,
                chunk_text="Lexical only chunk.",
                embedding_model=None,
                embedding_dim=None,
                embedding_vector=None,
            )
        )

    with engine.connect() as connection:
        docs = connection.execute(
            select(knowledge_documents.c.document_id)
        ).scalars().all()
        assert set(docs) == {"doc-1", "DOC-1", "doc-1 "}
        stored_vec = connection.execute(
            select(knowledge_chunks.c.embedding_vector).where(
                knowledge_chunks.c.chunk_id == "chk-1"
            )
        ).scalar_one()
        assert struct.unpack("<4f", stored_vec) == pytest.approx(
            (0.25, -0.5, 0.75, 0.125)
        )

    # Duplicate (document_id, content_sha256) is rejected.
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                knowledge_versions.insert().values(
                    version_id="ver-dup-sha",
                    document_id="doc-1",
                    content_sha256="a" * 64,
                    raw_markdown="# Order Runbook\n",
                    status="draft",
                    created_at=now,
                )
            )

    # Foreign keys reject orphan version and orphan chunk.
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                knowledge_versions.insert().values(
                    version_id="ver-orphan",
                    document_id="doc-missing",
                    content_sha256="b" * 64,
                    raw_markdown="# Missing\n",
                    status="draft",
                    created_at=now,
                )
            )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                knowledge_chunks.insert().values(
                    chunk_id="chk-orphan",
                    version_id="ver-missing",
                    ordinal=0,
                    heading_path="Missing",
                    start_line=1,
                    end_line=2,
                    chunk_text="Orphan",
                )
            )


def test_mysql84_ngram_fulltext_chinese_and_exact_error_codes(tmp_path, monkeypatch):
    url = empty_database_url(tmp_path / "ngram-fts")
    monkeypatch.setenv("INCIDENT_AGENT_DATABASE_URL", url)
    command.upgrade(_alembic_config(), "head")
    engine = get_engine(url)
    now = _utc_now()

    docs = [
        ("doc-order-5xx", "order-service", "订单服务 5xx 排查", "ver-order-5xx"),
        ("doc-order-db", "order-db", "订单库连接与锁排查", "ver-order-db"),
        ("doc-inv-503", "inventory-service", "库存服务背压手册", "ver-inv-503"),
        ("doc-order-stale", "order-service", "旧版网关超时手册", "ver-order-stale"),
    ]
    chunks = [
        (
            "chk-order-5xx-1",
            "ver-order-5xx",
            0,
            "订单服务 > HighHttp5xxRate 告警排查",
            "当订单服务出现 HighHttp5xxRate 且日志报错 ERR_ORDER_TIMEOUT 时，优先检查下游依赖超时与线程阻塞。",
        ),
        (
            "chk-order-db-1",
            "ver-order-db",
            0,
            "订单库 > PostgreSQL 连接池耗尽",
            "若日志出现 PG_CONN_REFUSED，需要排查最大连接数、连接泄漏及长事务锁等待。",
        ),
        (
            "chk-order-db-2",
            "ver-order-db",
            1,
            "订单库 > 慢查询与索引分析",
            "检查 pg_stat_activity 与慢日志，定位未走索引的全表扫描。",
        ),
        (
            "chk-inv-503-1",
            "ver-inv-503",
            0,
            "库存服务 > 背压限流",
            "返回 INV_503_BACKPRESSURE 说明库存扣减队列积压并触发背压保护。",
        ),
        (
            "chk-order-stale-1",
            "ver-order-stale",
            0,
            "旧版文档 > 网关健康检查",
            "检查历史网关节点摘除记录，确认上游实例重启后的就绪探针。",
        ),
    ]

    with engine.begin() as connection:
        for doc_id, service, title, ver_id in docs:
            connection.execute(
                knowledge_documents.insert().values(
                    document_id=doc_id,
                    service=service,
                    title=title,
                    source=f"knowledge/corpus/{doc_id}.md",
                    published_version_id=ver_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            connection.execute(
                knowledge_versions.insert().values(
                    version_id=ver_id,
                    document_id=doc_id,
                    content_sha256=doc_id.encode("utf-8").hex().ljust(64, "0")[:64],
                    raw_markdown=f"# {title}\n",
                    status="published",
                    created_at=now,
                )
            )
        for chunk_id, ver_id, ordinal, heading_path, chunk_text in chunks:
            connection.execute(
                knowledge_chunks.insert().values(
                    chunk_id=chunk_id,
                    version_id=ver_id,
                    ordinal=ordinal,
                    heading_path=heading_path,
                    start_line=1,
                    end_line=10,
                    chunk_text=chunk_text,
                )
            )

    def _search(query, mode):
        clause = (
            "MATCH(heading_path, chunk_text) AGAINST (:q IN BOOLEAN MODE)"
            if mode == "boolean"
            else "MATCH(heading_path, chunk_text) AGAINST (:q IN NATURAL LANGUAGE MODE)"
        )
        sql = text(
            f"SELECT chunk_id, {clause} AS score "
            "FROM knowledge_chunks "
            f"WHERE {clause} "
            "ORDER BY score DESC, chunk_pk ASC"
        )
        with engine.connect() as connection:
            return connection.execute(sql, {"q": query}).fetchall()

    # Heading-only Chinese phrase ("连接池耗尽" is in heading_path, not chunk_text).
    nl_pool = _search("连接池耗尽", "natural")
    assert nl_pool and nl_pool[0][0] == "chk-order-db-1"

    nl_lock = _search("锁等待", "natural")
    assert [row[0] for row in nl_lock] == ["chk-order-db-1"]

    nl_backpressure = _search("背压限流", "natural")
    assert [row[0] for row in nl_backpressure] == ["chk-inv-503-1"]

    # Exact phrase boolean queries work for uppercase error codes and Chinese phrases.
    assert [
        row[0] for row in _search('"ERR_ORDER_TIMEOUT"', "boolean")
    ] == ["chk-order-5xx-1"]
    assert [
        row[0] for row in _search('"HighHttp5xxRate"', "boolean")
    ] == ["chk-order-5xx-1"]
    assert [
        row[0] for row in _search('"PG_CONN_REFUSED"', "boolean")
    ] == ["chk-order-db-1"]
    assert [
        row[0] for row in _search('"INV_503_BACKPRESSURE"', "boolean")
    ] == ["chk-inv-503-1"]
    assert [
        row[0] for row in _search('"连接池耗尽"', "boolean")
    ] == ["chk-order-db-1"]

    # Unmatched error code returns empty result.
    assert _search('"NON_EXISTENT_ERROR_999"', "boolean") == []
