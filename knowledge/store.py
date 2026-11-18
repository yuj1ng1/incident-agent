"""MySQL-backed versioned knowledge store with short write transactions."""
from contextlib import contextmanager
from datetime import timezone
from typing import Callable

from sqlalchemy import (
    and_,
    desc,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.mysql import match as mysql_match
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from db import tables
from db.config import database_url as configured_database_url, get_engine
from knowledge.chunker import (
    DEFAULT_MAX_CHUNK_CHARS,
    DEFAULT_MAX_MARKDOWN_BYTES,
    chunk_markdown,
)
from knowledge.embeddings import EmbeddingProvider, encode_float32
from knowledge.models import (
    ImportResult,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeNotFoundError,
    KnowledgeStorageError,
    KnowledgeValidationError,
    KnowledgeVersion,
    utc_now,
)

_REQUIRED_TABLES = (
    "knowledge_documents",
    "knowledge_versions",
    "knowledge_chunks",
)


def _utc_naive(value):
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_aware(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _escape_like(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


class KnowledgeStore:
    """Authoritative MySQL store for versioned Markdown runbooks and embeddings."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        clock: Callable = utc_now,
        read_only: bool = False,
    ):
        try:
            self.database_url = database_url or configured_database_url()
        except RuntimeError as exc:
            raise KnowledgeStorageError(str(exc)) from exc
        self.engine = get_engine(self.database_url)
        self.clock = clock
        self.read_only = read_only
        try:
            inspector = inspect(self.engine)
            missing = [
                name for name in _REQUIRED_TABLES if not inspector.has_table(name)
            ]
            if missing:
                raise KnowledgeStorageError("knowledge_schema_not_migrated")
        except OperationalError:
            raise KnowledgeStorageError("knowledge_database_unavailable") from None

    @contextmanager
    def transaction(self):
        if self.read_only:
            raise KnowledgeStorageError("knowledge_store_read_only")
        try:
            with self.engine.begin() as db:
                yield db
        except KnowledgeStorageError:
            raise
        except KnowledgeValidationError:
            raise
        except KnowledgeNotFoundError:
            raise
        except OperationalError as exc:
            raise KnowledgeStorageError("knowledge_database_unavailable") from exc
        except SQLAlchemyError as exc:
            raise KnowledgeStorageError(
                f"knowledge_read_failed:{type(exc).__name__}"
            ) from exc
        except SQLAlchemyError as exc:
            raise KnowledgeStorageError(
                f"knowledge_transaction_failed:{type(exc).__name__}"
            ) from exc

    @contextmanager
    def read(self):
        """Repeatable-read snapshot; session default forbids writes."""
        try:
            connection = self.engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            )
        except OperationalError as exc:
            raise KnowledgeStorageError("knowledge_database_unavailable") from exc
        except SQLAlchemyError as exc:
            raise KnowledgeStorageError(
                f"knowledge_read_failed:{type(exc).__name__}"
            ) from exc
        try:
            connection.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
            connection.commit()
            with connection.begin():
                yield connection
        except OperationalError as exc:
            raise KnowledgeStorageError("knowledge_database_unavailable") from exc
        except SQLAlchemyError as exc:
            raise KnowledgeStorageError(
                f"knowledge_read_failed:{type(exc).__name__}"
            ) from exc
        finally:
            if connection.in_transaction():
                connection.rollback()
            try:
                connection.exec_driver_sql("SET SESSION TRANSACTION READ WRITE")
                connection.commit()
            finally:
                connection.close()

    @staticmethod
    def _row_to_document(row) -> KnowledgeDocument:
        return KnowledgeDocument(
            document_id=row["document_id"],
            service=row["service"],
            title=row["title"],
            source=row["source"],
            published_version_id=row["published_version_id"],
            created_at=_utc_aware(row["created_at"]),
            updated_at=_utc_aware(row["updated_at"]),
        )

    @staticmethod
    def _row_to_version(row) -> KnowledgeVersion:
        return KnowledgeVersion(
            version_id=row["version_id"],
            document_id=row["document_id"],
            content_sha256=row["content_sha256"],
            raw_markdown=row["raw_markdown"],
            status=row["status"],
            created_at=_utc_aware(row["created_at"]),
        )

    @staticmethod
    def _row_to_chunk(row) -> KnowledgeChunk:
        return KnowledgeChunk(
            chunk_id=row["chunk_id"],
            version_id=row["version_id"],
            ordinal=row["ordinal"],
            heading_path=row["heading_path"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            chunk_text=row["chunk_text"],
            embedding_model=row["embedding_model"],
            embedding_dim=row["embedding_dim"],
            embedding_vector=bytes(row["embedding_vector"])
            if row["embedding_vector"] is not None
            else None,
        )

    def _find_existing_version(
        self,
        db,
        *,
        document_id: str,
        content_sha256: str,
        for_update: bool = False,
    ):
        docs = tables.knowledge_documents
        vers = tables.knowledge_versions
        doc_stmt = select(docs).where(docs.c.document_id == document_id)
        ver_stmt = select(vers).where(
            and_(
                vers.c.document_id == document_id,
                vers.c.content_sha256 == content_sha256,
            )
        )
        if for_update:
            doc_stmt = doc_stmt.with_for_update()
            ver_stmt = ver_stmt.with_for_update()
        doc_row = db.execute(doc_stmt).mappings().first()
        ver_row = db.execute(ver_stmt).mappings().first()
        return doc_row, ver_row

    def _publish_locked_version(
        self,
        db,
        *,
        document_id: str,
        version_id: str,
        title: str | None = None,
        source: str | None = None,
        now_naive,
    ) -> None:
        docs = tables.knowledge_documents
        vers = tables.knowledge_versions
        db.execute(
            update(vers)
            .where(
                and_(
                    vers.c.document_id == document_id,
                    vers.c.status == "published",
                    vers.c.version_id != version_id,
                )
            )
            .values(status="archived")
        )
        db.execute(
            update(vers)
            .where(vers.c.version_id == version_id)
            .values(status="published")
        )
        doc_updates = {
            "published_version_id": version_id,
            "updated_at": now_naive,
        }
        if title is not None:
            doc_updates["title"] = title
        if source is not None:
            doc_updates["source"] = source
        db.execute(
            update(docs)
            .where(docs.c.document_id == document_id)
            .values(**doc_updates)
        )

    def _count_chunks(self, db, version_id: str) -> int:
        chunks = tables.knowledge_chunks
        rows = db.execute(
            select(chunks.c.chunk_id).where(chunks.c.version_id == version_id)
        ).all()
        return len(rows)

    def import_document(
        self,
        *,
        service: str,
        source: str,
        raw_markdown: str | bytes,
        title: str | None = None,
        document_id: str | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        publish: bool = True,
        max_bytes: int = DEFAULT_MAX_MARKDOWN_BYTES,
        max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    ) -> ImportResult:
        """Chunk & embed outside DB, then persist & publish in a short transaction."""
        chunked = chunk_markdown(
            service=service,
            source=source,
            raw_markdown=raw_markdown,
            title=title,
            document_id=document_id,
            max_bytes=max_bytes,
            max_chunk_chars=max_chunk_chars,
        )

        # Fast idempotency check before calling external embedding API.
        with self.read() as db:
            doc_row, ver_row = self._find_existing_version(
                db,
                document_id=chunked.document_id,
                content_sha256=chunked.content_sha256,
            )
            if doc_row is not None and doc_row["service"] != chunked.service:
                raise KnowledgeValidationError("document_service_mismatch")
            if ver_row is not None:
                chunk_count = self._count_chunks(db, ver_row["version_id"])
                already_published = (
                    ver_row["status"] == "published"
                    and doc_row["published_version_id"] == ver_row["version_id"]
                )
                if not publish or already_published:
                    return ImportResult(
                        document=self._row_to_document(doc_row),
                        version=self._row_to_version(ver_row),
                        chunk_count=chunk_count,
                        idempotent_hit=True,
                        published=already_published,
                    )

        # Compute and validate ALL embeddings before entering write transaction.
        prepared_chunks: list[KnowledgeChunk] = []
        if embedding_provider is not None:
            texts = [
                f"{chunk.heading_path}\n{chunk.chunk_text}"
                for chunk in chunked.chunks
            ]
            vectors = embedding_provider.embed_texts(texts)
            if len(vectors) != len(chunked.chunks):
                raise KnowledgeValidationError("embedding_chunk_count_mismatch")
            for chunk, vec in zip(chunked.chunks, vectors):
                blob = encode_float32(
                    vec,
                    expected_dim=embedding_provider.dimension,
                )
                prepared_chunks.append(
                    chunk.model_copy(
                        update={
                            "embedding_model": embedding_provider.model_name,
                            "embedding_dim": embedding_provider.dimension,
                            "embedding_vector": blob,
                        }
                    )
                )
        else:
            prepared_chunks = list(chunked.chunks)

        docs = tables.knowledge_documents
        vers = tables.knowledge_versions
        chunks_tbl = tables.knowledge_chunks
        now_naive = _utc_naive(self.clock())

        with self.transaction() as db:
            doc_row, ver_row = self._find_existing_version(
                db,
                document_id=chunked.document_id,
                content_sha256=chunked.content_sha256,
                for_update=True,
            )
            if doc_row is not None and doc_row["service"] != chunked.service:
                raise KnowledgeValidationError("document_service_mismatch")

            if doc_row is None:
                db.execute(
                    docs.insert().values(
                        document_id=chunked.document_id,
                        service=chunked.service,
                        title=chunked.title,
                        source=chunked.source,
                        published_version_id=None,
                        created_at=now_naive,
                        updated_at=now_naive,
                    )
                )

            if ver_row is not None:
                if publish and (
                    ver_row["status"] != "published"
                    or (doc_row and doc_row["published_version_id"] != ver_row["version_id"])
                ):
                    self._publish_locked_version(
                        db,
                        document_id=chunked.document_id,
                        version_id=ver_row["version_id"],
                        title=chunked.title,
                        source=chunked.source,
                        now_naive=now_naive,
                    )
                final_doc, final_ver = self._find_existing_version(
                    db,
                    document_id=chunked.document_id,
                    content_sha256=chunked.content_sha256,
                )
                return ImportResult(
                    document=self._row_to_document(final_doc),
                    version=self._row_to_version(final_ver),
                    chunk_count=self._count_chunks(db, final_ver["version_id"]),
                    idempotent_hit=True,
                    published=(final_ver["status"] == "published"),
                )

            status = "published" if publish else "draft"
            try:
                db.execute(
                    vers.insert().values(
                        version_id=chunked.version_id,
                        document_id=chunked.document_id,
                        content_sha256=chunked.content_sha256,
                        raw_markdown=chunked.raw_markdown,
                        status=status,
                        created_at=now_naive,
                    )
                )
                db.execute(
                    chunks_tbl.insert(),
                    [
                        {
                            "chunk_id": chunk.chunk_id,
                            "version_id": chunk.version_id,
                            "ordinal": chunk.ordinal,
                            "heading_path": chunk.heading_path,
                            "start_line": chunk.start_line,
                            "end_line": chunk.end_line,
                            "chunk_text": chunk.chunk_text,
                            "embedding_model": chunk.embedding_model,
                            "embedding_dim": chunk.embedding_dim,
                            "embedding_vector": chunk.embedding_vector,
                        }
                        for chunk in prepared_chunks
                    ],
                )
            except IntegrityError as exc:
                raise KnowledgeStorageError("knowledge_integrity_conflict") from exc

            if publish:
                self._publish_locked_version(
                    db,
                    document_id=chunked.document_id,
                    version_id=chunked.version_id,
                    title=chunked.title,
                    source=chunked.source,
                    now_naive=now_naive,
                )

            final_doc, final_ver = self._find_existing_version(
                db,
                document_id=chunked.document_id,
                content_sha256=chunked.content_sha256,
            )
            return ImportResult(
                document=self._row_to_document(final_doc),
                version=self._row_to_version(final_ver),
                chunk_count=len(prepared_chunks),
                idempotent_hit=False,
                published=publish,
            )

    def publish_version(
        self,
        *,
        service: str,
        document_id: str,
        version_id: str,
    ) -> KnowledgeVersion:
        """Atomically switch a document's published version within its service."""
        docs = tables.knowledge_documents
        vers = tables.knowledge_versions
        now_naive = _utc_naive(self.clock())
        with self.transaction() as db:
            doc_row = db.execute(
                select(docs)
                .where(
                    and_(
                        docs.c.document_id == document_id,
                        docs.c.service == service,
                    )
                )
                .with_for_update()
            ).mappings().first()
            if doc_row is None:
                raise KnowledgeNotFoundError("document_not_found")

            ver_row = db.execute(
                select(vers)
                .where(
                    and_(
                        vers.c.document_id == document_id,
                        vers.c.version_id == version_id,
                    )
                )
                .with_for_update()
            ).mappings().first()
            if ver_row is None:
                raise KnowledgeNotFoundError("version_not_found")

            self._publish_locked_version(
                db,
                document_id=document_id,
                version_id=version_id,
                now_naive=now_naive,
            )
            updated = db.execute(
                select(vers).where(vers.c.version_id == version_id)
            ).mappings().one()
            return self._row_to_version(updated)

    def list_documents(
        self,
        *,
        service: str | None = None,
        published_only: bool = False,
    ) -> list[KnowledgeDocument]:
        docs = tables.knowledge_documents
        stmt = select(docs).order_by(docs.c.service.asc(), docs.c.document_id.asc())
        if service is not None:
            stmt = stmt.where(docs.c.service == service)
        if published_only:
            stmt = stmt.where(docs.c.published_version_id.is_not(None))
        with self.read() as db:
            rows = db.execute(stmt).mappings().all()
            return [self._row_to_document(row) for row in rows]

    def get_document(self, *, service: str, document_id: str) -> KnowledgeDocument:
        docs = tables.knowledge_documents
        with self.read() as db:
            row = db.execute(
                select(docs).where(
                    and_(
                        docs.c.document_id == document_id,
                        docs.c.service == service,
                    )
                )
            ).mappings().first()
            if row is None:
                raise KnowledgeNotFoundError("document_not_found")
            return self._row_to_document(row)

    def get_version(
        self,
        *,
        service: str,
        document_id: str,
        version_id: str,
    ) -> KnowledgeVersion:
        docs = tables.knowledge_documents
        vers = tables.knowledge_versions
        with self.read() as db:
            row = db.execute(
                select(vers)
                .select_from(
                    vers.join(
                        docs,
                        vers.c.document_id == docs.c.document_id,
                    )
                )
                .where(
                    and_(
                        docs.c.service == service,
                        vers.c.document_id == document_id,
                        vers.c.version_id == version_id,
                    )
                )
            ).mappings().first()
            if row is None:
                raise KnowledgeNotFoundError("version_not_found")
            return self._row_to_version(row)

    def get_published_version(
        self,
        *,
        service: str,
        document_id: str,
    ) -> KnowledgeVersion:
        docs = tables.knowledge_documents
        vers = tables.knowledge_versions
        with self.read() as db:
            row = db.execute(
                select(vers)
                .select_from(
                    docs.join(
                        vers,
                        and_(
                            docs.c.published_version_id == vers.c.version_id,
                            docs.c.document_id == vers.c.document_id,
                        ),
                    )
                )
                .where(
                    and_(
                        docs.c.service == service,
                        docs.c.document_id == document_id,
                        vers.c.status == "published",
                    )
                )
            ).mappings().first()
            if row is None:
                raise KnowledgeNotFoundError("published_version_not_found")
            return self._row_to_version(row)

    def list_versions(
        self,
        *,
        service: str,
        document_id: str,
    ) -> list[KnowledgeVersion]:
        docs = tables.knowledge_documents
        vers = tables.knowledge_versions
        with self.read() as db:
            rows = db.execute(
                select(vers)
                .select_from(
                    vers.join(docs, vers.c.document_id == docs.c.document_id)
                )
                .where(
                    and_(
                        docs.c.service == service,
                        vers.c.document_id == document_id,
                    )
                )
                .order_by(vers.c.version_pk.asc())
            ).mappings().all()
            return [self._row_to_version(row) for row in rows]

    def get_chunks_for_version(
        self,
        *,
        service: str,
        document_id: str,
        version_id: str,
    ) -> list[KnowledgeChunk]:
        docs = tables.knowledge_documents
        vers = tables.knowledge_versions
        chunks = tables.knowledge_chunks
        with self.read() as db:
            rows = db.execute(
                select(chunks)
                .select_from(
                    chunks.join(
                        vers,
                        chunks.c.version_id == vers.c.version_id,
                    ).join(
                        docs,
                        vers.c.document_id == docs.c.document_id,
                    )
                )
                .where(
                    and_(
                        docs.c.service == service,
                        docs.c.document_id == document_id,
                        vers.c.version_id == version_id,
                    )
                )
                .order_by(chunks.c.ordinal.asc())
            ).mappings().all()
            return [self._row_to_chunk(row) for row in rows]

    def list_published_chunks(
        self,
        *,
        service: str,
        embedding_model: str | None = None,
        embedding_dim: int | None = None,
    ) -> list[dict]:
        """Return all published chunks for a single service."""
        docs = tables.knowledge_documents
        vers = tables.knowledge_versions
        chunks = tables.knowledge_chunks
        conditions = [
            docs.c.service == service,
            docs.c.published_version_id == vers.c.version_id,
            vers.c.status == "published",
        ]
        if embedding_model is not None:
            conditions.append(chunks.c.embedding_model == embedding_model)
        if embedding_dim is not None:
            conditions.append(chunks.c.embedding_dim == embedding_dim)

        stmt = (
            select(
                docs.c.document_id,
                docs.c.service,
                docs.c.title,
                docs.c.source,
                vers.c.version_id,
                chunks.c.chunk_id,
                chunks.c.ordinal,
                chunks.c.heading_path,
                chunks.c.start_line,
                chunks.c.end_line,
                chunks.c.chunk_text,
                chunks.c.embedding_model,
                chunks.c.embedding_dim,
                chunks.c.embedding_vector,
            )
            .select_from(
                chunks.join(vers, chunks.c.version_id == vers.c.version_id).join(
                    docs,
                    vers.c.document_id == docs.c.document_id,
                )
            )
            .where(and_(*conditions))
            .order_by(docs.c.document_id.asc(), chunks.c.ordinal.asc())
        )
        with self.read() as db:
            return [dict(row) for row in db.execute(stmt).mappings().all()]

    def list_published_chunk_vectors(
        self,
        *,
        service: str,
        embedding_model: str,
        embedding_dim: int,
    ) -> list[dict]:
        """Fetch only chunk IDs and float32 blobs for exact Python cosine ranking."""
        docs = tables.knowledge_documents
        vers = tables.knowledge_versions
        chunks = tables.knowledge_chunks
        stmt = (
            select(
                chunks.c.chunk_id,
                chunks.c.embedding_vector,
            )
            .select_from(
                chunks.join(vers, chunks.c.version_id == vers.c.version_id).join(
                    docs,
                    vers.c.document_id == docs.c.document_id,
                )
            )
            .where(
                and_(
                    docs.c.service == service,
                    docs.c.published_version_id == vers.c.version_id,
                    vers.c.status == "published",
                    chunks.c.embedding_model == embedding_model,
                    chunks.c.embedding_dim == embedding_dim,
                    chunks.c.embedding_vector.is_not(None),
                )
            )
            .order_by(chunks.c.chunk_id.asc())
        )
        with self.read() as db:
            return [dict(row) for row in db.execute(stmt).mappings().all()]

    def fetch_published_chunks_by_ids(
        self,
        *,
        service: str,
        chunk_ids: list[str] | tuple[str, ...],
    ) -> dict[str, dict]:
        """Load full published chunk payloads for selected Top-K chunk IDs."""
        if not chunk_ids:
            return {}
        docs = tables.knowledge_documents
        vers = tables.knowledge_versions
        chunks = tables.knowledge_chunks
        stmt = (
            select(
                docs.c.document_id,
                docs.c.service,
                docs.c.title,
                docs.c.source,
                vers.c.version_id,
                chunks.c.chunk_id,
                chunks.c.ordinal,
                chunks.c.heading_path,
                chunks.c.start_line,
                chunks.c.end_line,
                chunks.c.chunk_text,
            )
            .select_from(
                chunks.join(vers, chunks.c.version_id == vers.c.version_id).join(
                    docs,
                    vers.c.document_id == docs.c.document_id,
                )
            )
            .where(
                and_(
                    docs.c.service == service,
                    docs.c.published_version_id == vers.c.version_id,
                    vers.c.status == "published",
                    chunks.c.chunk_id.in_(list(chunk_ids)),
                )
            )
        )
        with self.read() as db:
            rows = db.execute(stmt).mappings().all()
            return {row["chunk_id"]: dict(row) for row in rows}

    def search_published_fulltext(
        self,
        *,
        service: str,
        query: str,
        identifiers: tuple[str, ...] = (),
        limit: int = 20,
    ) -> list[dict]:
        """Run MySQL ngram FULLTEXT + escaped LIKE exact-code search on published chunks."""
        docs = tables.knowledge_documents
        vers = tables.knowledge_versions
        chunks = tables.knowledge_chunks

        ft_expr = mysql_match(
            chunks.c.heading_path,
            chunks.c.chunk_text,
            against=query,
        ).in_natural_language_mode()
        ft_score = ft_expr.label("ft_score")

        exact_clauses = []
        for ident in identifiers:
            pattern = f"%{_escape_like(ident)}%"
            exact_clauses.append(chunks.c.chunk_text.like(pattern, escape="\\"))
            exact_clauses.append(chunks.c.heading_path.like(pattern, escape="\\"))

        match_filter = (
            or_(ft_expr > 0, *exact_clauses)
            if exact_clauses
            else (ft_expr > 0)
        )

        stmt = (
            select(
                docs.c.document_id,
                docs.c.service,
                docs.c.title,
                docs.c.source,
                vers.c.version_id,
                chunks.c.chunk_id,
                chunks.c.ordinal,
                chunks.c.heading_path,
                chunks.c.start_line,
                chunks.c.end_line,
                chunks.c.chunk_text,
                ft_score,
            )
            .select_from(
                chunks.join(vers, chunks.c.version_id == vers.c.version_id).join(
                    docs,
                    vers.c.document_id == docs.c.document_id,
                )
            )
            .where(
                and_(
                    docs.c.service == service,
                    docs.c.published_version_id == vers.c.version_id,
                    vers.c.status == "published",
                    match_filter,
                )
            )
            .order_by(desc(ft_expr), chunks.c.chunk_id.asc())
            .limit(limit)
        )
        with self.read() as db:
            return [dict(row) for row in db.execute(stmt).mappings().all()]
