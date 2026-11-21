"""HTTP view adapter for the versioned knowledge base; no raw SQL here."""
from contextlib import contextmanager
import math
from typing import Literal

from pydantic import ValidationError

from investigation.store import RuntimeStore
from knowledge.embeddings import (
    build_embedding_provider,
    load_embedding_config_from_env,
)
from knowledge.models import (
    EmbeddingError,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeError,
    KnowledgeNotFoundError,
    KnowledgeStorageError,
    KnowledgeValidationError,
    KnowledgeVersion,
)
from knowledge.retriever import KnowledgeRetriever
from knowledge.store import KnowledgeStore
from services.registry import registry
from web_api.queries import public_text
from web_api.schemas import (
    KnowledgeChunkItem,
    KnowledgeDocumentImportRequest,
    KnowledgeDocumentPage,
    KnowledgeDocumentSummary,
    KnowledgeImportResponse,
    KnowledgeSearchHit,
    KnowledgeSearchMethod,
    KnowledgeSearchResponse,
    KnowledgeVersionCreateRequest,
    KnowledgeVersionDetail,
    KnowledgeVersionSummary,
    RunbookCitationView,
)

MAX_SNIPPET_CHARS = 600
MAX_SESSION_RUNBOOK_CITATIONS = 20

_SAFE_VALIDATION_CODES = frozenset(
    {
        "unknown_service",
        "invalid_service_name",
        "invalid_source_name",
        "invalid_document_id",
        "invalid_version_id",
        "document_service_mismatch",
        "markdown_exceeds_max_bytes",
        "markdown_invalid_utf8",
        "markdown_contains_nul_byte",
        "markdown_content_empty",
        "markdown_produced_no_chunks",
        "query_cannot_be_empty",
        "query_exceeds_max_length",
        "invalid_top_k",
        "unsupported_retrieval_method",
        "embedding_chunk_count_mismatch",
    }
)

_SAFE_NOT_FOUND_CODES = frozenset(
    {
        "document_not_found",
        "version_not_found",
        "published_version_not_found",
    }
)


def knowledge_error_response(exc: KnowledgeError) -> tuple[int, str]:
    """Map domain knowledge exceptions to HTTP status and allowlisted error code."""
    raw = str(exc).strip()
    if isinstance(exc, KnowledgeValidationError):
        code = raw if raw in _SAFE_VALIDATION_CODES else "knowledge_validation_failed"
        return 422, code
    if isinstance(exc, KnowledgeNotFoundError):
        code = raw if raw in _SAFE_NOT_FOUND_CODES else "knowledge_not_found"
        return 404, code
    if isinstance(exc, EmbeddingError):
        return 503, "embedding_unavailable"
    if isinstance(exc, KnowledgeStorageError):
        if raw == "knowledge_schema_not_migrated":
            return 503, "knowledge_schema_not_migrated"
        return 503, "knowledge_unavailable"
    return 503, "knowledge_unavailable"


def _sanitize_unavailable_reason(
    reason: str | None,
    *,
    method: str,
    candidate_counts: dict[str, int],
) -> str:
    raw = (reason or "").strip()
    if raw == "knowledge_schema_not_migrated":
        return "knowledge_schema_not_migrated"
    if raw.startswith(("knowledge_", "database_url_", "mysql_")):
        return "knowledge_unavailable"
    if "embedding" in raw.lower() or "lexical" in candidate_counts:
        return "embedding_unavailable"
    if method in {"hybrid_rrf", "vector_cosine"} and raw:
        return "embedding_unavailable"
    return "knowledge_unavailable"


def require_registered_service(service: str) -> str:
    """Validate that a service name is non-empty and registered."""
    cleaned = service.strip() if isinstance(service, str) else ""
    if not cleaned or not registry.exists(cleaned):
        raise KnowledgeValidationError("unknown_service")
    return cleaned


def _validate_path_id(value: str, *, error_code: str) -> str:
    cleaned = value.strip() if isinstance(value, str) else ""
    if (
        not cleaned
        or len(cleaned) > 120
        or cleaned in {".", ".."}
        or ".." in cleaned
        or "/" in cleaned
        or "\\" in cleaned
    ):
        raise KnowledgeValidationError(error_code)
    return cleaned


def _format_citation(
    document_id: str,
    version_id: str,
    start_line: int,
    end_line: int,
) -> str:
    return f"{document_id}@{version_id}#L{start_line}-L{end_line}"


def _build_snippet(chunk_text: str, *, limit: int = MAX_SNIPPET_CHARS) -> str:
    text = chunk_text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _version_summary(version: KnowledgeVersion) -> KnowledgeVersionSummary:
    return KnowledgeVersionSummary(
        version_id=version.version_id,
        status=version.status,
        content_sha256=version.content_sha256,
        created_at=version.created_at,
    )


def _document_summary(
    document: KnowledgeDocument,
    versions: list[KnowledgeVersion],
) -> KnowledgeDocumentSummary:
    ordered_versions = [_version_summary(v) for v in reversed(versions)][:100]
    published_status: Literal["published", "unpublished"] = (
        "published" if document.published_version_id is not None else "unpublished"
    )
    return KnowledgeDocumentSummary(
        document_id=document.document_id,
        service=document.service,
        title=document.title,
        source=document.source,
        published_version_id=document.published_version_id,
        published_status=published_status,
        version_count=len(versions),
        versions=ordered_versions,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _chunk_item(chunk: KnowledgeChunk) -> KnowledgeChunkItem:
    return KnowledgeChunkItem(
        chunk_id=chunk.chunk_id,
        ordinal=chunk.ordinal,
        heading_path=chunk.heading_path,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        chunk_text=chunk.chunk_text,
        embedding_model=chunk.embedding_model,
        embedding_dim=chunk.embedding_dim,
    )


def extract_session_runbook_citations(
    *,
    runtime_url: str | None,
    session_id: str,
    default_service: str,
) -> list[RunbookCitationView]:
    """Read allowlisted search_runbooks citations from a session's checkpoint records."""
    store = RuntimeStore(runtime_url, read_only=True)
    with store.read() as db:
        session = store.get(session_id, db)
        checkpoint = store.load_checkpoint(session, db)

    records = checkpoint.get("records")
    if not isinstance(records, list):
        return []

    citations: list[RunbookCitationView] = []
    seen: set[tuple[str, str, str]] = set()

    for record in records:
        if not isinstance(record, dict) or record.get("name") != "search_runbooks":
            continue
        result = record.get("result")
        if not isinstance(result, dict) or result.get("status") != "ok":
            continue
        hits = result.get("hits")
        if not isinstance(hits, list):
            continue
        record_service = result.get("service")
        fallback_service = (
            record_service.strip()
            if isinstance(record_service, str) and record_service.strip()
            else default_service
        )

        for hit in hits:
            if len(citations) >= MAX_SESSION_RUNBOOK_CITATIONS:
                return citations
            if not isinstance(hit, dict):
                continue
            doc_id = hit.get("document_id")
            ver_id = hit.get("version_id")
            chunk_id = hit.get("chunk_id")
            title = hit.get("title")
            heading_path = hit.get("heading_path", "")
            start_line = hit.get("start_line")
            end_line = hit.get("end_line")
            if (
                not isinstance(doc_id, str)
                or not doc_id.strip()
                or not isinstance(ver_id, str)
                or not ver_id.strip()
                or not isinstance(chunk_id, str)
                or not chunk_id.strip()
                or not isinstance(title, str)
                or not title.strip()
                or not isinstance(heading_path, str)
                or type(start_line) is not int
                or type(end_line) is not int
                or start_line < 1
                or end_line < start_line
            ):
                continue

            key = (doc_id.strip(), ver_id.strip(), chunk_id.strip())
            if key in seen:
                continue

            raw_service = hit.get("service")
            service_name = (
                raw_service.strip()
                if isinstance(raw_service, str) and raw_service.strip()
                else fallback_service
            )
            raw_citation = hit.get("citation")
            citation_text = (
                raw_citation.strip()
                if isinstance(raw_citation, str) and raw_citation.strip()
                else _format_citation(key[0], key[1], start_line, end_line)
            )
            raw_snippet = hit.get("snippet")
            if not isinstance(raw_snippet, str) or not raw_snippet.strip():
                raw_snippet = hit.get("chunk_text")
            snippet_text = (
                public_text(_build_snippet(raw_snippet, limit=MAX_SNIPPET_CHARS), 600)
                if isinstance(raw_snippet, str) and raw_snippet.strip()
                else None
            )
            raw_score = hit.get("score")
            score_val: float | None = None
            if (
                isinstance(raw_score, (int, float))
                and not isinstance(raw_score, bool)
                and math.isfinite(float(raw_score))
            ):
                score_val = float(raw_score)

            try:
                view = RunbookCitationView(
                    document_id=public_text(key[0], 120),
                    version_id=public_text(key[1], 120),
                    chunk_id=public_text(key[2], 120),
                    service=public_text(service_name, 200),
                    title=public_text(title.strip(), 300),
                    heading_path=public_text(heading_path, 500),
                    start_line=start_line,
                    end_line=end_line,
                    citation=public_text(citation_text, 300),
                    snippet=snippet_text,
                    score=score_val,
                )
            except ValidationError:
                continue
            seen.add(key)
            citations.append(view)

    return citations


class KnowledgeViews:
    """Lazy HTTP adapter over KnowledgeStore and KnowledgeRetriever."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        embedding_provider=None,
    ):
        self.database_url = database_url
        self.embedding_provider = embedding_provider

    def _open_store(self, *, read_only: bool) -> KnowledgeStore:
        return KnowledgeStore(self.database_url, read_only=read_only)

    @contextmanager
    def _resolve_embedding_provider(self):
        if self.embedding_provider is not None:
            yield self.embedding_provider
            return
        try:
            config = load_embedding_config_from_env()
            provider = build_embedding_provider(config)
        except (EmbeddingError, ValueError, ValidationError) as exc:
            raise EmbeddingError("embedding_unavailable") from exc
        try:
            yield provider
        finally:
            close_fn = getattr(provider, "close", None)
            if callable(close_fn):
                close_fn()

    @staticmethod
    def _find_authorized_document(
        store: KnowledgeStore,
        document_id: str,
    ) -> KnowledgeDocument:
        for document in store.list_documents():
            if document.document_id == document_id:
                if not registry.exists(document.service):
                    raise KnowledgeNotFoundError("document_not_found")
                return document
        raise KnowledgeNotFoundError("document_not_found")

    def list_documents(
        self,
        *,
        limit: int,
        offset: int,
        service: str | None = None,
        status: Literal["published", "unpublished", "all"] | None = None,
    ) -> KnowledgeDocumentPage:
        resolved_service = (
            require_registered_service(service) if service is not None else None
        )
        store = self._open_store(read_only=True)
        documents = store.list_documents(
            service=resolved_service,
            published_only=(status == "published"),
        )
        authorized = [doc for doc in documents if registry.exists(doc.service)]
        if status == "unpublished":
            authorized = [
                doc for doc in authorized if doc.published_version_id is None
            ]
        authorized.sort(
            key=lambda doc: (-doc.updated_at.timestamp(), doc.document_id)
        )
        total = len(authorized)
        page_docs = authorized[offset : offset + limit]
        items = [
            _document_summary(
                doc,
                store.list_versions(
                    service=doc.service,
                    document_id=doc.document_id,
                ),
            )
            for doc in page_docs
        ]
        return KnowledgeDocumentPage(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            has_more=total > offset + limit,
        )

    def import_document(
        self,
        request: KnowledgeDocumentImportRequest,
    ) -> KnowledgeImportResponse:
        service = require_registered_service(request.service)
        source = request.source.strip()
        if not source:
            raise KnowledgeValidationError("invalid_source_name")
        title = (
            request.title.strip()
            if request.title is not None and request.title.strip()
            else None
        )
        document_id = (
            request.document_id.strip()
            if request.document_id is not None
            else None
        )

        store = self._open_store(read_only=False)
        with self._resolve_embedding_provider() as provider:
            result = store.import_document(
                service=service,
                source=source,
                title=title,
                document_id=document_id,
                raw_markdown=request.raw_markdown,
                embedding_provider=provider,
                publish=request.publish,
            )

        versions = store.list_versions(
            service=result.document.service,
            document_id=result.document.document_id,
        )
        return KnowledgeImportResponse(
            document=_document_summary(result.document, versions),
            version=_version_summary(result.version),
            chunk_count=result.chunk_count,
            idempotent=result.idempotent_hit,
        )

    def create_or_publish_version(
        self,
        document_id: str,
        request: KnowledgeVersionCreateRequest,
    ) -> KnowledgeImportResponse:
        clean_doc_id = _validate_path_id(
            document_id,
            error_code="invalid_document_id",
        )
        store = self._open_store(read_only=False)
        doc = self._find_authorized_document(store, clean_doc_id)

        if request.publish_version_id is not None:
            target_version_id = _validate_path_id(
                request.publish_version_id,
                error_code="invalid_version_id",
            )
            existing_version = store.get_version(
                service=doc.service,
                document_id=doc.document_id,
                version_id=target_version_id,
            )
            already_published = (
                doc.published_version_id == existing_version.version_id
                and existing_version.status == "published"
            )
            if already_published:
                published_version = existing_version
                updated_doc = doc
            else:
                published_version = store.publish_version(
                    service=doc.service,
                    document_id=doc.document_id,
                    version_id=target_version_id,
                )
                updated_doc = store.get_document(
                    service=doc.service,
                    document_id=doc.document_id,
                )
            versions = store.list_versions(
                service=updated_doc.service,
                document_id=updated_doc.document_id,
            )
            chunks = store.get_chunks_for_version(
                service=updated_doc.service,
                document_id=updated_doc.document_id,
                version_id=published_version.version_id,
            )
            return KnowledgeImportResponse(
                document=_document_summary(updated_doc, versions),
                version=_version_summary(published_version),
                chunk_count=max(1, len(chunks)),
                idempotent=already_published,
            )

        assert request.raw_markdown is not None
        source = (
            request.source.strip()
            if request.source is not None and request.source.strip()
            else doc.source
        )
        title = (
            request.title.strip()
            if request.title is not None and request.title.strip()
            else None
        )
        with self._resolve_embedding_provider() as provider:
            result = store.import_document(
                service=doc.service,
                source=source,
                title=title,
                document_id=doc.document_id,
                raw_markdown=request.raw_markdown,
                embedding_provider=provider,
                publish=request.publish,
            )
        versions = store.list_versions(
            service=result.document.service,
            document_id=result.document.document_id,
        )
        return KnowledgeImportResponse(
            document=_document_summary(result.document, versions),
            version=_version_summary(result.version),
            chunk_count=result.chunk_count,
            idempotent=result.idempotent_hit,
        )

    def get_version_detail(
        self,
        document_id: str,
        version_id: str,
    ) -> KnowledgeVersionDetail:
        clean_doc_id = _validate_path_id(
            document_id,
            error_code="invalid_document_id",
        )
        clean_version_id = _validate_path_id(
            version_id,
            error_code="invalid_version_id",
        )
        store = self._open_store(read_only=True)
        doc = self._find_authorized_document(store, clean_doc_id)
        version = store.get_version(
            service=doc.service,
            document_id=doc.document_id,
            version_id=clean_version_id,
        )
        chunks = store.get_chunks_for_version(
            service=doc.service,
            document_id=doc.document_id,
            version_id=version.version_id,
        )
        return KnowledgeVersionDetail(
            document_id=doc.document_id,
            service=doc.service,
            title=doc.title,
            source=doc.source,
            published_version_id=doc.published_version_id,
            version_id=version.version_id,
            status=version.status,
            content_sha256=version.content_sha256,
            created_at=version.created_at,
            raw_markdown=version.raw_markdown,
            chunks=[_chunk_item(chunk) for chunk in chunks],
        )

    def search(
        self,
        *,
        service: str,
        query: str,
        limit: int = 5,
        method: KnowledgeSearchMethod = "hybrid_rrf",
    ) -> KnowledgeSearchResponse:
        clean_service = require_registered_service(service)
        clean_query = query.strip() if isinstance(query, str) else ""
        if not clean_query:
            raise KnowledgeValidationError("query_cannot_be_empty")
        if len(clean_query) > 2000:
            raise KnowledgeValidationError("query_exceeds_max_length")

        try:
            store = self._open_store(read_only=True)
            if method == "mysql_ngram":
                retriever = KnowledgeRetriever(
                    store,
                    embedding_provider=None,
                )
                result = retriever.search(
                    service=clean_service,
                    query=clean_query,
                    top_k=limit,
                    method=method,
                )
            else:
                with self._resolve_embedding_provider() as provider:
                    retriever = KnowledgeRetriever(
                        store,
                        embedding_provider=provider,
                    )
                    result = retriever.search(
                        service=clean_service,
                        query=clean_query,
                        top_k=limit,
                        method=method,
                    )
        except (KnowledgeStorageError, EmbeddingError) as exc:
            _, safe_code = knowledge_error_response(exc)
            return KnowledgeSearchResponse(
                status="unavailable",
                service=clean_service,
                query=clean_query,
                method=method,
                hits=[],
                candidate_count=0,
                reason=safe_code,
            )

        candidate_count = sum(
            count
            for key, count in result.candidate_counts.items()
            if key != "vector_pool"
        )
        if result.status == "unavailable":
            return KnowledgeSearchResponse(
                status="unavailable",
                service=clean_service,
                query=clean_query,
                method=method,
                hits=[],
                candidate_count=candidate_count,
                reason=_sanitize_unavailable_reason(
                    result.reason,
                    method=method,
                    candidate_counts=result.candidate_counts,
                ),
            )
        if result.status == "no_match":
            return KnowledgeSearchResponse(
                status="no_match",
                service=clean_service,
                query=clean_query,
                method=method,
                hits=[],
                candidate_count=candidate_count,
                reason=None,
            )

        hits = [
            KnowledgeSearchHit(
                document_id=hit.document_id,
                version_id=hit.version_id,
                chunk_id=hit.chunk_id,
                service=hit.service,
                title=hit.title,
                source=hit.source,
                heading_path=hit.heading_path,
                start_line=hit.start_line,
                end_line=hit.end_line,
                snippet=_build_snippet(hit.chunk_text),
                score=hit.score,
                lexical_rank=hit.lexical_rank,
                vector_rank=hit.vector_rank,
                exact_match=hit.exact_match,
                citation=_format_citation(
                    hit.document_id,
                    hit.version_id,
                    hit.start_line,
                    hit.end_line,
                ),
            )
            for hit in result.hits
        ]
        return KnowledgeSearchResponse(
            status="ok",
            service=clean_service,
            query=clean_query,
            method=method,
            hits=hits,
            candidate_count=candidate_count,
            reason=None,
        )
