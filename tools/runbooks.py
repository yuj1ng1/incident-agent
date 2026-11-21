"""Read-only runbook search tool backed by the versioned MySQL knowledge base."""
import json
import re

import httpx2
from sqlalchemy.exc import SQLAlchemyError

from agent_logger.logger_config import logger
from knowledge.embeddings import (
    build_embedding_provider,
    load_embedding_config_from_env,
)
from knowledge.models import (
    EmbeddingError,
    KnowledgeStorageError,
    KnowledgeValidationError,
)
from knowledge.retriever import (
    KnowledgeRetriever,
    MAX_QUERY_CHARS,
)
from knowledge.store import KnowledgeStore
from services.registry import registry
from tools.tool_registry import tool

DEFAULT_RUNBOOK_LIMIT = 5
MIN_RUNBOOK_LIMIT = 1
MAX_RUNBOOK_LIMIT = 10
MAX_SNIPPET_CHARS = 600
MAX_SUGGESTED_QUERY_CHARS = 240
MAX_RUNBOOK_PAYLOAD_BYTES = 32768
DEFAULT_RETRIEVAL_METHOD = "hybrid_rrf"

_SECRET_VALUE_RE = re.compile(
    r"(?i)((?:password|passwd|api[_-]?key|token|authorization|secret)"
    r"\s*[=:]\s*)[^\s,;\"']+"
)
_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[^\s\"']+")
_WHITESPACE_RE = re.compile(r"\s+")


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    scrubbed = _BEARER_RE.sub(r"\1[redacted]", value)
    scrubbed = _SECRET_VALUE_RE.sub(r"\1[redacted]", scrubbed)
    return _WHITESPACE_RE.sub(" ", scrubbed).strip()


def _field(obj: object, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def build_alert_runbook_query(incident) -> str:
    """Build a concise search query from an incident's service, alert type, and summary."""
    if incident is None:
        return ""

    service = _clean_text(_field(incident, "service", ""))
    alerts = _field(incident, "alerts", None) or []
    latest_alert = alerts[-1] if alerts else None

    if not service and latest_alert is not None:
        service = _clean_text(_field(latest_alert, "service", ""))

    parts: list[str] = []
    seen_lower: list[str] = []

    def add_part(raw: object) -> None:
        text = _clean_text(raw)
        if not text:
            return
        lowered = text.lower()
        if any(lowered == existing or lowered in existing for existing in seen_lower):
            return
        parts.append(text)
        seen_lower.append(lowered)

    add_part(service)

    if latest_alert is not None:
        alertname = _clean_text(_field(latest_alert, "alertname", ""))
        if alertname and alertname.lower() != "legacy":
            add_part(alertname)

        add_part(_field(latest_alert, "metric", ""))

        annotations = _field(latest_alert, "annotations", None)
        labels = _field(latest_alert, "labels", None)
        if not isinstance(annotations, dict):
            annotations = {}
        if not isinstance(labels, dict):
            labels = {}

        add_part(annotations.get("error_code") or labels.get("error_code"))

        summary = ""
        for key in ("summary", "description", "message"):
            candidate = _clean_text(annotations.get(key))
            if candidate:
                summary = candidate
                break

        if summary:
            if service and summary.lower().startswith(service.lower() + " "):
                summary = summary[len(service) :].strip()
            add_part(summary)
        elif len(parts) <= 1:
            add_part(_field(latest_alert, "signal_type", ""))

    query = " ".join(parts).strip()
    if len(query) > MAX_SUGGESTED_QUERY_CHARS:
        query = query[:MAX_SUGGESTED_QUERY_CHARS].rstrip()
    return query


def _build_retriever() -> KnowledgeRetriever:
    """Instantiate a read-only KnowledgeStore and configured EmbeddingProvider."""
    store = KnowledgeStore(read_only=True)
    try:
        embedding_config = load_embedding_config_from_env()
        provider = build_embedding_provider(embedding_config)
    except ValueError as exc:
        raise EmbeddingError("invalid_embedding_config") from exc
    return KnowledgeRetriever(
        store,
        embedding_provider=provider,
    )


def _classify_error_reason(error: object) -> str:
    """Map storage, embedding, or timeout failures to safe bounded error codes."""
    if isinstance(error, (TimeoutError, httpx2.TimeoutException)):
        return "knowledge_timeout"
    if isinstance(error, KnowledgeStorageError):
        raw = str(error).lower()
        if "knowledge_schema_not_migrated" in raw:
            return "knowledge_schema_not_migrated"
        if "timeout" in raw:
            return "knowledge_timeout"
        return "knowledge_unavailable"
    if isinstance(error, EmbeddingError):
        raw = str(error).lower()
        if "timeout" in raw:
            return "knowledge_timeout"
        return "embedding_unavailable"
    if isinstance(error, str):
        raw = error.lower()
        if "timeout" in raw:
            return "knowledge_timeout"
        if "knowledge_schema_not_migrated" in raw:
            return "knowledge_schema_not_migrated"
        if "embedding" in raw:
            return "embedding_unavailable"
        return "knowledge_unavailable"
    name = type(error).__name__.lower()
    raw = str(error).lower()
    if "timeout" in name or "timeout" in raw:
        return "knowledge_timeout"
    return "knowledge_unavailable"


def _extract_candidate_count(result) -> int:
    counts = getattr(result, "candidate_counts", None)
    if isinstance(counts, dict):
        return sum(
            int(value)
            for key, value in counts.items()
            if key != "vector_pool"
            and isinstance(value, int)
            and not isinstance(value, bool)
        )
    single = getattr(result, "candidate_count", None)
    if isinstance(single, int) and not isinstance(single, bool):
        return single
    hits = getattr(result, "hits", ()) or ()
    return len(hits)


def _format_snippet(text: str, max_chars: int = MAX_SNIPPET_CHARS) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= 3:
        return normalized[:max_chars]
    return normalized[: max_chars - 3].rstrip() + "..."


def _format_hit(hit, *, snippet_limit: int = MAX_SNIPPET_CHARS) -> dict:
    citation = (
        f"{hit.document_id}@{hit.version_id}"
        f"#L{hit.start_line}-L{hit.end_line}"
    )
    return {
        "document_id": hit.document_id,
        "version_id": hit.version_id,
        "chunk_id": hit.chunk_id,
        "service": getattr(hit, "service", ""),
        "title": hit.title,
        "heading_path": hit.heading_path,
        "start_line": hit.start_line,
        "end_line": hit.end_line,
        "snippet": _format_snippet(hit.chunk_text, snippet_limit),
        "score": round(float(hit.score), 6),
        "citation": citation,
    }


def _encoded_size(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _tool_error_payload(
    *,
    reason: str,
    service: str,
    query: str,
) -> dict:
    return {
        "status": "tool_error",
        "reason": reason,
        "service": service,
        "query": query,
        "hits": [],
        "available": False,
    }


@tool
def search_runbooks(
    service: str,
    query: str,
    limit: int = DEFAULT_RUNBOOK_LIMIT,
) -> dict:
    """只读检索指定服务已发布的运维排查手册，返回带版本与行号引用的参考线索，不能替代实时状态观测。

    Args:
        service: 已登记的服务或依赖名称，仅检索该服务已发布版本的手册
        query: 检索关键词或告警现象、错误码、摘要，长度 1-2000 字符
        limit: 返回命中条数上限，范围 1-10，默认 5
    """
    if not isinstance(service, str) or not registry.exists(service):
        return {
            "status": "no_data",
            "reason": "unknown_service",
            "service": service if isinstance(service, str) else "",
            "hits": [],
        }

    if not isinstance(query, str) or not query.strip():
        return {
            "status": "invalid_arguments",
            "reason": "empty_query",
            "service": service,
            "hits": [],
        }

    clean_query = query.strip()
    if len(clean_query) > MAX_QUERY_CHARS:
        return {
            "status": "invalid_arguments",
            "reason": "query_too_long",
            "service": service,
            "hits": [],
        }

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < MIN_RUNBOOK_LIMIT:
        return {
            "status": "invalid_arguments",
            "reason": "invalid_limit",
            "service": service,
            "query": clean_query,
            "hits": [],
        }

    bounded_limit = min(limit, MAX_RUNBOOK_LIMIT)

    try:
        retriever = _build_retriever()
        result = retriever.search(
            service=service,
            query=clean_query,
            top_k=bounded_limit,
            method=DEFAULT_RETRIEVAL_METHOD,
        )
    except KnowledgeValidationError:
        return {
            "status": "invalid_arguments",
            "reason": "invalid_search_parameters",
            "service": service,
            "query": clean_query,
            "hits": [],
        }
    except (TimeoutError, httpx2.TimeoutException) as exc:
        logger.warning(
            "Runbook search timed out service=%s category=%s",
            service,
            type(exc).__name__,
        )
        return _tool_error_payload(
            reason="knowledge_timeout",
            service=service,
            query=clean_query,
        )
    except (KnowledgeStorageError, EmbeddingError, SQLAlchemyError, OSError) as exc:
        reason = _classify_error_reason(exc)
        logger.warning(
            "Runbook search unavailable service=%s reason=%s category=%s",
            service,
            reason,
            type(exc).__name__,
        )
        return _tool_error_payload(
            reason=reason,
            service=service,
            query=clean_query,
        )
    except Exception as exc:
        reason = _classify_error_reason(exc)
        logger.warning(
            "Runbook search failed service=%s reason=%s category=%s",
            service,
            reason,
            type(exc).__name__,
        )
        return _tool_error_payload(
            reason=reason,
            service=service,
            query=clean_query,
        )

    if result.status == "unavailable":
        reason = _classify_error_reason(result.reason or "knowledge_unavailable")
        return _tool_error_payload(
            reason=reason,
            service=service,
            query=clean_query,
        )

    scoped_hits = [
        hit
        for hit in (result.hits or ())
        if getattr(hit, "service", service) == service
    ]
    if result.status != "ok" or not scoped_hits:
        return {
            "status": "no_data",
            "reason": "no_matching_runbook",
            "service": service,
            "query": clean_query,
            "method": result.method,
            "hits": [],
        }

    payload = {
        "status": "ok",
        "service": service,
        "query": clean_query,
        "method": result.method,
        "evidence_type": "reference_runbook",
        "authoritative_for_current_state": False,
        "candidate_count": _extract_candidate_count(result),
        "hits": [],
    }
    for hit in scoped_hits[:bounded_limit]:
        formatted = _format_hit(hit, snippet_limit=MAX_SNIPPET_CHARS)
        if not formatted["service"]:
            formatted["service"] = service
        payload["hits"].append(formatted)
        if _encoded_size(payload) > MAX_RUNBOOK_PAYLOAD_BYTES:
            payload["hits"].pop()
            if not payload["hits"]:
                fallback = _format_hit(hit, snippet_limit=160)
                if not fallback["service"]:
                    fallback["service"] = service
                payload["hits"].append(fallback)
            break

    return payload
