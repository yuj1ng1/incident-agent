"""Hybrid service-scoped knowledge retriever (MySQL ngram + exact + cosine + RRF)."""
from collections.abc import Sequence
import re
import time

from knowledge.embeddings import (
    EmbeddingProvider,
    cosine_similarity,
    decode_float32,
)
from knowledge.models import (
    EmbeddingError,
    KnowledgeStorageError,
    KnowledgeValidationError,
    RetrievalMethod,
    SearchHit,
    SearchResult,
)
from knowledge.store import KnowledgeStore

DEFAULT_TOP_K = 5
MAX_TOP_K = 20
MAX_QUERY_CHARS = 2000
DEFAULT_RRF_K = 60
DEFAULT_EXACT_BONUS = 0.02
DEFAULT_MIN_LEXICAL_SCORE = 0.5
DEFAULT_MIN_VECTOR_SCORE = 0.28

_UPPER_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_CAMEL_IDENT_RE = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z0-9][A-Za-z0-9]*)+\b")
_SNAKE_IDENT_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_ASCII_DOMAIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{2,}")
_CJK_SPAN_RE = re.compile(r"[\u4e00-\u9fff]+")

_GENERIC_CJK_TERMS = frozenset(
    {
        "如何",
        "怎么",
        "怎样",
        "什么",
        "哪些",
        "哪个",
        "为什么",
        "是否",
        "能否",
        "可以",
        "应该",
        "需要",
        "还是",
        "或者",
        "以及",
        "并且",
        "如果",
        "时候",
        "当前",
        "现在",
        "已经",
        "直接",
        "进行",
        "完成",
        "出现",
        "发生",
        "导致",
        "引起",
        "触发",
        "排查",
        "处理",
        "解决",
        "检查",
        "确认",
        "核对",
        "验证",
        "查看",
        "观察",
        "恢复",
        "服务",
        "系统",
        "故障",
        "异常",
        "报错",
        "错误",
        "问题",
        "告警",
        "日志",
        "指标",
        "接口",
        "请求",
        "状态",
        "步骤",
        "流程",
        "方式",
        "原因",
        "作用",
        "影响",
        "配置",
        "版本",
    }
)


def extract_exact_identifiers(query: str) -> tuple[str, ...]:
    """Extract deterministic error codes, alert names, and snake_case identifiers."""
    seen: dict[str, None] = {}
    for pattern in (_UPPER_CODE_RE, _CAMEL_IDENT_RE, _SNAKE_IDENT_RE):
        for match in pattern.findall(query):
            seen.setdefault(match, None)
    return tuple(seen.keys())


def _extract_meaningful_terms(query: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extract ASCII technical terms and CJK 2-grams excluding generic filler."""
    ascii_terms: dict[str, None] = {}
    for tok in _ASCII_DOMAIN_RE.findall(query):
        ascii_terms.setdefault(tok.lower(), None)

    cjk_bigrams: dict[str, None] = {}
    for span in _CJK_SPAN_RE.findall(query):
        if len(span) < 2:
            continue
        for idx in range(len(span) - 1):
            bg = span[idx : idx + 2]
            if bg not in _GENERIC_CJK_TERMS:
                cjk_bigrams.setdefault(bg, None)
    return tuple(ascii_terms.keys()), tuple(cjk_bigrams.keys())


def _count_exact_matches(
    identifiers: tuple[str, ...],
    heading_path: str,
    chunk_text: str,
) -> int:
    if not identifiers:
        return 0
    haystack = f"{heading_path}\n{chunk_text}"
    haystack_lower = haystack.lower()
    count = 0
    for ident in identifiers:
        if ident in haystack or ident.lower() in haystack_lower:
            count += 1
    return count


def _count_meaningful_overlap(
    ascii_terms: tuple[str, ...],
    cjk_bigrams: tuple[str, ...],
    heading_path: str,
    chunk_text: str,
) -> tuple[int, int]:
    haystack_lower = f"{heading_path}\n{chunk_text}".lower()
    ascii_hits = sum(1 for term in ascii_terms if term in haystack_lower)
    cjk_hits = sum(1 for bg in cjk_bigrams if bg in haystack_lower)
    return ascii_hits, cjk_hits


def reciprocal_rank_fusion(
    lexical_ranks: dict[str, int],
    vector_ranks: dict[str, int],
    *,
    exact_matches: set[str] | frozenset[str] = frozenset(),
    rrf_k: int = DEFAULT_RRF_K,
    exact_bonus: float = DEFAULT_EXACT_BONUS,
) -> list[tuple[str, float]]:
    """Fuse ranked chunk IDs via Reciprocal Rank Fusion with deterministic tie-breaks."""
    if rrf_k <= 0:
        raise KnowledgeValidationError("rrf_k_must_be_positive")
    all_ids = set(lexical_ranks) | set(vector_ranks)
    scored: list[tuple[str, float, int, int]] = []
    for chunk_id in all_ids:
        lex_rank = lexical_ranks.get(chunk_id)
        vec_rank = vector_ranks.get(chunk_id)
        score = 0.0
        if lex_rank is not None:
            score += 1.0 / (rrf_k + lex_rank)
        if vec_rank is not None:
            score += 1.0 / (rrf_k + vec_rank)
        if chunk_id in exact_matches:
            score += exact_bonus
        scored.append(
            (
                chunk_id,
                score,
                lex_rank if lex_rank is not None else 10**9,
                vec_rank if vec_rank is not None else 10**9,
            )
        )
    scored.sort(key=lambda item: (-item[1], item[2], item[3], item[0]))
    return [(chunk_id, score) for chunk_id, score, _, _ in scored]


class KnowledgeRetriever:
    """Service-scoped published-only retriever supporting ngram, cosine, and RRF."""

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        rrf_k: int = DEFAULT_RRF_K,
        exact_bonus: float = DEFAULT_EXACT_BONUS,
        min_lexical_score: float = DEFAULT_MIN_LEXICAL_SCORE,
        min_vector_score: float = DEFAULT_MIN_VECTOR_SCORE,
        allow_lexical_fallback: bool = False,
    ):
        self.store = store
        self.embedding_provider = embedding_provider
        self.rrf_k = rrf_k
        self.exact_bonus = exact_bonus
        self.min_lexical_score = min_lexical_score
        self.min_vector_score = min_vector_score
        self.allow_lexical_fallback = allow_lexical_fallback

    @staticmethod
    def _validate_search_args(
        service: str,
        query: str,
        top_k: int,
        method: str,
    ) -> tuple[str, str]:
        if not isinstance(service, str) or not service.strip():
            raise KnowledgeValidationError("invalid_service_name")
        if not isinstance(query, str) or not query.strip():
            raise KnowledgeValidationError("query_cannot_be_empty")
        if len(query) > MAX_QUERY_CHARS:
            raise KnowledgeValidationError("query_exceeds_max_length")
        if type(top_k) is not int or not (1 <= top_k <= MAX_TOP_K):
            raise KnowledgeValidationError("invalid_top_k")
        if method not in {"mysql_ngram", "vector_cosine", "hybrid_rrf"}:
            raise KnowledgeValidationError("unsupported_retrieval_method")
        return service.strip(), query.strip()

    def _run_lexical_branch(
        self,
        *,
        service: str,
        query: str,
        candidate_limit: int,
    ) -> tuple[list[dict], dict[str, int]]:
        identifiers = extract_exact_identifiers(query)
        ascii_terms, cjk_bigrams = _extract_meaningful_terms(query)
        raw_rows = self.store.search_published_fulltext(
            service=service,
            query=query,
            identifiers=identifiers,
            limit=candidate_limit,
        )

        # Require either an exact identifier match or meaningful lexical overlap.
        ranked: list[tuple[tuple, dict]] = []
        for row in raw_rows:
            ft_score = float(row.get("ft_score") or 0.0)
            exact_hits = _count_exact_matches(
                identifiers,
                row["heading_path"],
                row["chunk_text"],
            )
            ascii_hits, cjk_hits = _count_meaningful_overlap(
                ascii_terms,
                cjk_bigrams,
                row["heading_path"],
                row["chunk_text"],
            )
            if exact_hits == 0:
                if ft_score < self.min_lexical_score:
                    continue
                if ascii_hits == 0 and cjk_hits < 2:
                    continue
                # If query contains explicit unknown error codes (e.g. WECHAT_PAY_SIGN_INVALID),
                # avoid false positives driven solely by generic Chinese filler words.
                if identifiers and ascii_hits == 0 and cjk_hits < 4:
                    continue

            combined_score = ft_score + exact_hits * 100.0 + ascii_hits * 2.0 + cjk_hits * 0.2
            enriched = dict(row)
            enriched["lexical_score"] = combined_score
            enriched["exact_match"] = exact_hits > 0
            ranked.append(((-combined_score, row["chunk_id"]), enriched))

        ranked.sort(key=lambda item: item[0])
        ordered_rows = [item[1] for item in ranked]
        ranks = {row["chunk_id"]: idx for idx, row in enumerate(ordered_rows, start=1)}
        return ordered_rows, ranks

    def _run_vector_branch(
        self,
        *,
        service: str,
        query: str,
        candidate_limit: int,
    ) -> tuple[list[tuple[str, float]], dict[str, int], int]:
        if self.embedding_provider is None:
            raise EmbeddingError("embedding_provider_not_configured")
        query_vec = self.embedding_provider.embed_query(query)
        vector_rows = self.store.list_published_chunk_vectors(
            service=service,
            embedding_model=self.embedding_provider.model_name,
            embedding_dim=self.embedding_provider.dimension,
        )
        scored: list[tuple[str, float]] = []
        for row in vector_rows:
            chunk_vec = decode_float32(
                row["embedding_vector"],
                expected_dim=self.embedding_provider.dimension,
            )
            sim = cosine_similarity(query_vec, chunk_vec)
            if sim >= self.min_vector_score:
                scored.append((row["chunk_id"], sim))
        scored.sort(key=lambda item: (-item[1], item[0]))
        top_candidates = scored[:candidate_limit]
        ranks = {
            chunk_id: idx for idx, (chunk_id, _) in enumerate(top_candidates, start=1)
        }
        return top_candidates, ranks, len(vector_rows)

    @staticmethod
    def _build_hit(
        row: dict,
        *,
        score: float,
        lexical_rank: int | None = None,
        vector_rank: int | None = None,
        exact_match: bool = False,
    ) -> SearchHit:
        return SearchHit(
            document_id=row["document_id"],
            version_id=row["version_id"],
            chunk_id=row["chunk_id"],
            service=row["service"],
            title=row["title"],
            source=row["source"],
            heading_path=row["heading_path"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            chunk_text=row["chunk_text"],
            score=score,
            lexical_rank=lexical_rank,
            vector_rank=vector_rank,
            exact_match=exact_match,
        )

    def search(
        self,
        *,
        service: str,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        method: RetrievalMethod | str = "hybrid_rrf",
    ) -> SearchResult:
        """Retrieve published runbook chunks scoped strictly to service."""
        clean_service, clean_query = self._validate_search_args(
            service,
            query,
            top_k,
            method,
        )
        started = time.perf_counter()
        candidate_limit = max(top_k * 6, 30)

        try:
            if method == "mysql_ngram":
                lex_rows, lex_ranks = self._run_lexical_branch(
                    service=clean_service,
                    query=clean_query,
                    candidate_limit=candidate_limit,
                )
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                if not lex_rows:
                    return SearchResult(
                        status="no_match",
                        service=clean_service,
                        query=clean_query,
                        method=method,
                        candidate_counts={"lexical": 0},
                        elapsed_ms=elapsed_ms,
                    )
                hits = tuple(
                    self._build_hit(
                        row,
                        score=row["lexical_score"],
                        lexical_rank=lex_ranks[row["chunk_id"]],
                        exact_match=row["exact_match"],
                    )
                    for row in lex_rows[:top_k]
                )
                return SearchResult(
                    status="ok",
                    service=clean_service,
                    query=clean_query,
                    method=method,
                    hits=hits,
                    candidate_counts={"lexical": len(lex_rows)},
                    elapsed_ms=elapsed_ms,
                )

            if method == "vector_cosine":
                vec_candidates, vec_ranks, total_vec = self._run_vector_branch(
                    service=clean_service,
                    query=clean_query,
                    candidate_limit=candidate_limit,
                )
                top_vec = vec_candidates[:top_k]
                payloads = self.store.fetch_published_chunks_by_ids(
                    service=clean_service,
                    chunk_ids=[cid for cid, _ in top_vec],
                )
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                hits_list = [
                    self._build_hit(
                        payloads[cid],
                        score=score,
                        vector_rank=vec_ranks[cid],
                    )
                    for cid, score in top_vec
                    if cid in payloads
                ]
                if not hits_list:
                    return SearchResult(
                        status="no_match",
                        service=clean_service,
                        query=clean_query,
                        method=method,
                        candidate_counts={"vector_pool": total_vec, "vector": len(vec_candidates)},
                        elapsed_ms=elapsed_ms,
                    )
                return SearchResult(
                    status="ok",
                    service=clean_service,
                    query=clean_query,
                    method=method,
                    hits=tuple(hits_list),
                    candidate_counts={"vector_pool": total_vec, "vector": len(vec_candidates)},
                    elapsed_ms=elapsed_ms,
                )

            # hybrid_rrf
            lex_rows, lex_ranks = self._run_lexical_branch(
                service=clean_service,
                query=clean_query,
                candidate_limit=candidate_limit,
            )
            lex_by_id = {row["chunk_id"]: row for row in lex_rows}
            exact_ids = {
                row["chunk_id"] for row in lex_rows if row.get("exact_match")
            }

            vec_ranks: dict[str, int] = {}
            total_vec = 0
            try:
                _, vec_ranks, total_vec = self._run_vector_branch(
                    service=clean_service,
                    query=clean_query,
                    candidate_limit=candidate_limit,
                )
            except EmbeddingError as exc:
                if not self.allow_lexical_fallback:
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    return SearchResult(
                        status="unavailable",
                        service=clean_service,
                        query=clean_query,
                        method=method,
                        reason=str(exc) or "embedding_unavailable",
                        candidate_counts={"lexical": len(lex_rows)},
                        elapsed_ms=elapsed_ms,
                    )

            fused = reciprocal_rank_fusion(
                lex_ranks,
                vec_ranks,
                exact_matches=exact_ids,
                rrf_k=self.rrf_k,
                exact_bonus=self.exact_bonus,
            )
            top_fused = fused[:top_k]
            missing_ids = [cid for cid, _ in top_fused if cid not in lex_by_id]
            fetched = self.store.fetch_published_chunks_by_ids(
                service=clean_service,
                chunk_ids=missing_ids,
            )
            identifiers = extract_exact_identifiers(clean_query)
            hits_list: list[SearchHit] = []
            for cid, score in top_fused:
                row = lex_by_id.get(cid) or fetched.get(cid)
                if row is None:
                    continue
                is_exact = cid in exact_ids or (
                    _count_exact_matches(
                        identifiers,
                        row["heading_path"],
                        row["chunk_text"],
                    )
                    > 0
                )
                hits_list.append(
                    self._build_hit(
                        row,
                        score=score,
                        lexical_rank=lex_ranks.get(cid),
                        vector_rank=vec_ranks.get(cid),
                        exact_match=is_exact,
                    )
                )

            elapsed_ms = (time.perf_counter() - started) * 1000.0
            counts = {
                "lexical": len(lex_rows),
                "vector_pool": total_vec,
                "vector": len(vec_ranks),
            }
            if not hits_list:
                return SearchResult(
                    status="no_match",
                    service=clean_service,
                    query=clean_query,
                    method=method,
                    candidate_counts=counts,
                    elapsed_ms=elapsed_ms,
                )
            return SearchResult(
                status="ok",
                service=clean_service,
                query=clean_query,
                method=method,
                hits=tuple(hits_list),
                candidate_counts=counts,
                elapsed_ms=elapsed_ms,
            )
        except (KnowledgeStorageError, EmbeddingError) as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            return SearchResult(
                status="unavailable",
                service=clean_service,
                query=clean_query,
                method=method,
                reason=str(exc) or type(exc).__name__,
                elapsed_ms=elapsed_ms,
            )
