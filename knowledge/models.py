"""Validated domain models for the versioned operations knowledge base."""
from datetime import datetime, timezone
import math
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


VersionStatus = Literal["draft", "published", "archived"]
SearchStatus = Literal["ok", "no_match", "unavailable"]
RetrievalMethod = Literal[
    "mysql_ngram",
    "vector_cosine",
    "hybrid_rrf",
    "offline_bm25",
]


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class KnowledgeError(RuntimeError):
    """Base domain error for knowledge base operations."""


class KnowledgeValidationError(KnowledgeError):
    """Raised when document, chunk, query, or configuration input is invalid."""


class KnowledgeStorageError(KnowledgeError):
    """Raised when the MySQL knowledge store fails or is unmigrated/unavailable."""


class KnowledgeNotFoundError(KnowledgeError):
    """Raised when a requested document or version does not exist."""


class EmbeddingError(KnowledgeError):
    """Raised when embedding generation, decoding, or validation fails."""


class KnowledgeRecord(BaseModel):
    """Base Pydantic model enforcing strict fields and UTC timestamps."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        hide_input_in_errors=True,
    )

    @field_validator("*", mode="after")
    @classmethod
    def _ensure_utc_datetime(cls, value):
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timezone_required")
            return value.astimezone(timezone.utc)
        return value


class KnowledgeDocument(KnowledgeRecord):
    """Logical runbook document scoped to a single service."""

    document_id: str = Field(min_length=1, max_length=120)
    service: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    source: str = Field(min_length=1, max_length=300)
    published_version_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
    )
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeVersion(KnowledgeRecord):
    """Immutable snapshot of Markdown content belonging to a document."""

    version_id: str = Field(min_length=1, max_length=120)
    document_id: str = Field(min_length=1, max_length=120)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_markdown: str = Field(min_length=1)
    status: VersionStatus = "draft"
    created_at: datetime = Field(default_factory=utc_now)


class KnowledgeChunk(KnowledgeRecord):
    """Heading-aware Markdown chunk with line coordinates and optional vector."""

    chunk_id: str = Field(min_length=1, max_length=120)
    version_id: str = Field(min_length=1, max_length=120)
    ordinal: int = Field(ge=0)
    heading_path: str = Field(max_length=500)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    chunk_text: str = Field(min_length=1)
    embedding_model: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
    )
    embedding_dim: int | None = Field(default=None, ge=1)
    embedding_vector: bytes | None = None

    @model_validator(mode="after")
    def _validate_coordinates_and_embedding(self):
        if self.end_line < self.start_line:
            raise ValueError("end_line_before_start_line")
        emb_fields = (
            self.embedding_model,
            self.embedding_dim,
            self.embedding_vector,
        )
        present = sum(item is not None for item in emb_fields)
        if present not in (0, 3):
            raise ValueError("incomplete_embedding_metadata")
        if present == 3:
            expected_bytes = self.embedding_dim * 4
            if len(self.embedding_vector) != expected_bytes:
                raise ValueError("embedding_vector_byte_length_mismatch")
        return self


class ChunkedDocument(KnowledgeRecord):
    """Output of deterministic Markdown parsing and chunking before persistence."""

    document_id: str = Field(min_length=1, max_length=120)
    version_id: str = Field(min_length=1, max_length=120)
    service: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    source: str = Field(min_length=1, max_length=300)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_markdown: str = Field(min_length=1)
    chunks: tuple[KnowledgeChunk, ...] = Field(min_length=1)


class ImportResult(KnowledgeRecord):
    """Outcome of importing (and optionally publishing) a document version."""

    document: KnowledgeDocument
    version: KnowledgeVersion
    chunk_count: int = Field(ge=1)
    idempotent_hit: bool = False
    published: bool = False


class SearchHit(KnowledgeRecord):
    """Single retrieved chunk citing exact document, version, and line span."""

    document_id: str = Field(min_length=1, max_length=120)
    version_id: str = Field(min_length=1, max_length=120)
    chunk_id: str = Field(min_length=1, max_length=120)
    service: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    source: str = Field(min_length=1, max_length=300)
    heading_path: str = Field(max_length=500)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    chunk_text: str = Field(min_length=1)
    score: float
    lexical_rank: int | None = Field(default=None, ge=1)
    vector_rank: int | None = Field(default=None, ge=1)
    exact_match: bool = False

    @field_validator("score", mode="before")
    @classmethod
    def _finite_score(cls, value):
        if isinstance(value, bool):
            raise ValueError("boolean_score_invalid")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non_finite_score")
        return number


class SearchResult(KnowledgeRecord):
    """Explicit retrieval result distinguishing ok, no_match, and unavailable."""

    status: SearchStatus
    service: str = Field(min_length=1, max_length=200)
    query: str
    method: str = Field(min_length=1, max_length=64)
    hits: tuple[SearchHit, ...] = ()
    reason: str | None = None
    candidate_counts: dict[str, int] = Field(default_factory=dict)
    elapsed_ms: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _validate_status_semantics(self):
        if self.status == "ok" and not self.hits:
            raise ValueError("ok_status_requires_hits")
        if self.status != "ok" and self.hits:
            raise ValueError("non_ok_status_must_not_contain_hits")
        if self.status == "unavailable" and not self.reason:
            raise ValueError("unavailable_status_requires_reason")
        return self


class MisRetrievalExample(KnowledgeRecord):
    """Captured evaluation failure or forbidden-document hit for error analysis."""

    query_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    service: str = Field(min_length=1)
    query: str
    expected_doc_ids: tuple[str, ...] = ()
    forbidden_doc_ids: tuple[str, ...] = ()
    retrieved_doc_ids: tuple[str, ...] = ()
    status: SearchStatus
    reason: str


class EvalMethodMetrics(KnowledgeRecord):
    """Aggregated retrieval quality and latency metrics for one method."""

    method: str = Field(min_length=1)
    split: str = Field(min_length=1)
    total_queries: int = Field(ge=0)
    positive_queries: int = Field(ge=0)
    no_match_queries: int = Field(ge=0)
    recall_at_5: float = Field(ge=0.0, le=1.0)
    mrr: float = Field(ge=0.0, le=1.0)
    no_match_accuracy: float = Field(ge=0.0, le=1.0)
    forbidden_hit_rate: float = Field(ge=0.0, le=1.0)
    p95_latency_ms: float = Field(ge=0.0)
    mean_candidate_count: float = Field(ge=0.0)
    mis_retrievals: tuple[MisRetrievalExample, ...] = ()


class EvalMetrics(KnowledgeRecord):
    """Comparison report across multiple retrieval strategies on a query set."""

    split: str = Field(min_length=1)
    corpus_document_count: int = Field(ge=0)
    corpus_chunk_count: int = Field(ge=0)
    methods: tuple[EvalMethodMetrics, ...] = ()


Document = KnowledgeDocument
Version = KnowledgeVersion
Chunk = KnowledgeChunk
