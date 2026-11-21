"""Offline BM25 baseline and retrieval evaluation runner."""
import argparse
from collections import Counter
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import time

from knowledge.chunker import chunk_markdown
from knowledge.embeddings import EmbeddingProvider, OfflineHashEmbeddingProvider
from knowledge.models import (
    EvalMethodMetrics,
    EvalMetrics,
    KnowledgeValidationError,
    MisRetrievalExample,
    SearchHit,
    SearchResult,
)
from knowledge.retriever import (
    DEFAULT_TOP_K,
    KnowledgeRetriever,
    _GENERIC_CJK_TERMS,
    _count_exact_matches,
    _count_meaningful_overlap,
    _extract_meaningful_terms,
    extract_exact_identifiers,
)
from knowledge.store import KnowledgeStore

_META_LINE_RE = re.compile(
    r"^-\s+\*\*([a-zA-Z0-9_]+)\*\*:\s+`([^`]+)`\s*$"
)
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:-]+")
_CJK_SPAN_RE = re.compile(r"[\u4e00-\u9fff]+")


def parse_runbook_metadata(raw_markdown: str, *, fallback_stem: str) -> dict[str, str]:
    """Extract optional frontmatter bullet metadata from corpus Markdown."""
    meta: dict[str, str] = {}
    for line in raw_markdown.splitlines()[:25]:
        match = _META_LINE_RE.match(line.strip())
        if match:
            meta[match.group(1)] = match.group(2).strip()
    meta.setdefault("doc_id", fallback_stem)
    meta.setdefault("status", "active")
    return meta


def load_corpus_documents(corpus_dir: Path) -> list[dict]:
    """Read Markdown corpus files from disk without writing to DB."""
    if not corpus_dir.is_dir():
        raise KnowledgeValidationError("corpus_directory_not_found")
    documents: list[dict] = []
    for path in sorted(corpus_dir.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        meta = parse_runbook_metadata(raw, fallback_stem=path.stem)
        service = meta.get("service", "").strip()
        if not service:
            raise KnowledgeValidationError(f"missing_service_metadata:{path.name}")
        documents.append(
            {
                "document_id": meta["doc_id"],
                "service": service,
                "status": meta["status"],
                "source": f"knowledge/corpus/{path.name}",
                "raw_markdown": raw,
            }
        )
    if not documents:
        raise KnowledgeValidationError("corpus_directory_empty")
    return documents


def import_corpus_into_store(
    store: KnowledgeStore,
    corpus_dir: Path,
    *,
    embedding_provider: EmbeddingProvider | None = None,
    publish_deprecated: bool = False,
) -> dict[str, int]:
    """Import corpus Markdown files into KnowledgeStore (deprecated as draft)."""
    docs = load_corpus_documents(corpus_dir)
    published_count = 0
    draft_count = 0
    total_chunks = 0
    for item in docs:
        should_publish = publish_deprecated or item["status"] != "deprecated"
        result = store.import_document(
            service=item["service"],
            source=item["source"],
            document_id=item["document_id"],
            raw_markdown=item["raw_markdown"],
            embedding_provider=embedding_provider,
            publish=should_publish,
        )
        total_chunks += result.chunk_count
        if should_publish:
            published_count += 1
        else:
            draft_count += 1
    return {
        "documents": len(docs),
        "published_documents": published_count,
        "draft_documents": draft_count,
        "chunks": total_chunks,
    }


def _tokenize_bm25(text: str) -> list[str]:
    tokens: list[str] = []
    for tok in _ASCII_TOKEN_RE.findall(text):
        lowered = tok.lower()
        tokens.append(lowered)
        if "_" in lowered or "-" in lowered or "." in lowered:
            for part in re.split(r"[_.-]+", lowered):
                if len(part) >= 2:
                    tokens.append(part)
    for span in _CJK_SPAN_RE.findall(text):
        for idx in range(len(span) - 1):
            bg = span[idx : idx + 2]
            if bg not in _GENERIC_CJK_TERMS:
                tokens.append(f"c2:{bg}")
    return tokens


@dataclass(frozen=True)
class _BM25Chunk:
    document_id: str
    version_id: str
    chunk_id: str
    service: str
    title: str
    source: str
    heading_path: str
    start_line: int
    end_line: int
    chunk_text: str
    tokens: tuple[str, ...]
    tf: Counter[str]


class OfflineBM25Retriever:
    """In-memory Okapi BM25 baseline scoped strictly to (service, published)."""

    def __init__(
        self,
        chunks: list[_BM25Chunk],
        *,
        k1: float = 1.5,
        b: float = 0.75,
        min_score: float = 2.5,
    ):
        self.k1 = k1
        self.b = b
        self.min_score = min_score
        self._by_service: dict[str, list[_BM25Chunk]] = {}
        for chunk in chunks:
            self._by_service.setdefault(chunk.service, []).append(chunk)

        self._df: dict[str, Counter[str]] = {}
        self._avgdl: dict[str, float] = {}
        for service, items in self._by_service.items():
            df: Counter[str] = Counter()
            total_len = 0
            for item in items:
                total_len += len(item.tokens)
                for term in set(item.tokens):
                    df[term] += 1
            self._df[service] = df
            self._avgdl[service] = total_len / len(items) if items else 1.0

    @classmethod
    def from_corpus_directory(
        cls,
        corpus_dir: Path,
        *,
        publish_deprecated: bool = False,
        min_score: float = 2.5,
    ) -> "OfflineBM25Retriever":
        docs = load_corpus_documents(corpus_dir)
        bm25_chunks: list[_BM25Chunk] = []
        for item in docs:
            if not publish_deprecated and item["status"] == "deprecated":
                continue
            chunked = chunk_markdown(
                service=item["service"],
                source=item["source"],
                document_id=item["document_id"],
                raw_markdown=item["raw_markdown"],
            )
            for chunk in chunked.chunks:
                tokens = tuple(
                    _tokenize_bm25(f"{chunk.heading_path}\n{chunk.chunk_text}")
                )
                bm25_chunks.append(
                    _BM25Chunk(
                        document_id=chunked.document_id,
                        version_id=chunked.version_id,
                        chunk_id=chunk.chunk_id,
                        service=chunked.service,
                        title=chunked.title,
                        source=chunked.source,
                        heading_path=chunk.heading_path,
                        start_line=chunk.start_line,
                        end_line=chunk.end_line,
                        chunk_text=chunk.chunk_text,
                        tokens=tokens,
                        tf=Counter(tokens),
                    )
                )
        return cls(bm25_chunks, min_score=min_score)

    @classmethod
    def from_store(
        cls,
        store: KnowledgeStore,
        services: set[str] | tuple[str, ...] | list[str],
        *,
        min_score: float = 2.5,
    ) -> "OfflineBM25Retriever":
        bm25_chunks: list[_BM25Chunk] = []
        for service in sorted(set(services)):
            for row in store.list_published_chunks(service=service):
                tokens = tuple(
                    _tokenize_bm25(f"{row['heading_path']}\n{row['chunk_text']}")
                )
                bm25_chunks.append(
                    _BM25Chunk(
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
                        tokens=tokens,
                        tf=Counter(tokens),
                    )
                )
        return cls(bm25_chunks, min_score=min_score)

    def search(
        self,
        *,
        service: str,
        query: str,
        top_k: int = DEFAULT_TOP_K,
    ) -> SearchResult:
        started = time.perf_counter()
        docs = self._by_service.get(service, [])
        if not docs:
            return SearchResult(
                status="no_match",
                service=service,
                query=query,
                method="offline_bm25",
                candidate_counts={"corpus_chunks": 0},
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )

        query_tokens = _tokenize_bm25(query)
        identifiers = extract_exact_identifiers(query)
        ascii_terms, cjk_bigrams = _extract_meaningful_terms(query)
        df = self._df[service]
        avgdl = self._avgdl[service]
        n_docs = len(docs)

        scored: list[tuple[float, bool, _BM25Chunk]] = []
        for chunk in docs:
            dl = len(chunk.tokens) or 1
            score = 0.0
            for term in set(query_tokens):
                freq = chunk.tf.get(term, 0)
                if freq == 0:
                    continue
                doc_freq = df.get(term, 0)
                idf = math.log((n_docs - doc_freq + 0.5) / (doc_freq + 0.5) + 1.0)
                denom = freq + self.k1 * (1.0 - self.b + self.b * dl / avgdl)
                score += idf * (freq * (self.k1 + 1.0)) / denom

            exact_hits = _count_exact_matches(
                identifiers,
                chunk.heading_path,
                chunk.chunk_text,
            )
            if exact_hits > 0:
                score += exact_hits * 10.0
            else:
                ascii_hits, cjk_hits = _count_meaningful_overlap(
                    ascii_terms,
                    cjk_bigrams,
                    chunk.heading_path,
                    chunk.chunk_text,
                )
                if ascii_hits == 0 and cjk_hits < 2:
                    continue
                if identifiers and ascii_hits == 0 and cjk_hits < 4:
                    continue

            if exact_hits > 0 or score >= self.min_score:
                scored.append((score, exact_hits > 0, chunk))

        scored.sort(key=lambda item: (-item[0], item[2].chunk_id))
        top_items = scored[:top_k]
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if not top_items:
            return SearchResult(
                status="no_match",
                service=service,
                query=query,
                method="offline_bm25",
                candidate_counts={"corpus_chunks": len(docs), "matched": 0},
                elapsed_ms=elapsed_ms,
            )

        hits = tuple(
            SearchHit(
                document_id=chunk.document_id,
                version_id=chunk.version_id,
                chunk_id=chunk.chunk_id,
                service=chunk.service,
                title=chunk.title,
                source=chunk.source,
                heading_path=chunk.heading_path,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                chunk_text=chunk.chunk_text,
                score=score,
                lexical_rank=rank,
                exact_match=is_exact,
            )
            for rank, (score, is_exact, chunk) in enumerate(top_items, start=1)
        )
        return SearchResult(
            status="ok",
            service=service,
            query=query,
            method="offline_bm25",
            hits=hits,
            candidate_counts={"corpus_chunks": len(docs), "matched": len(scored)},
            elapsed_ms=elapsed_ms,
        )


def load_eval_queries(queries_path: Path, *, split: str = "all") -> list[dict]:
    """Load and filter labeled evaluation queries from JSON."""
    data = json.loads(queries_path.read_text(encoding="utf-8"))
    queries = data.get("queries")
    if not isinstance(queries, list):
        raise KnowledgeValidationError("eval_queries_missing_list")
    if split == "all":
        return list(queries)
    return [q for q in queries if q.get("split") == split]


def _percentile_95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1))
    return ordered[idx]


def evaluate_method(
    *,
    method_name: str,
    search_fn,
    queries: list[dict],
    split: str,
    top_k: int = DEFAULT_TOP_K,
) -> EvalMethodMetrics:
    """Evaluate one retrieval strategy on labeled queries."""
    positive_count = 0
    no_match_count = 0
    recall_hits = 0
    mrr_sum = 0.0
    no_match_correct = 0
    forbidden_hits = 0
    latencies: list[float] = []
    candidate_totals: list[int] = []
    mis_retrievals: list[MisRetrievalExample] = []

    for item in queries:
        expected = tuple(item.get("expected_doc_ids") or ())
        forbidden = tuple(item.get("forbidden_doc_ids") or ())
        result: SearchResult = search_fn(
            service=item["service"],
            query=item["query"],
            top_k=top_k,
        )
        latencies.append(float(result.elapsed_ms or 0.0))
        cand_total = sum(
            v for k, v in result.candidate_counts.items() if k != "vector_pool"
        )
        candidate_totals.append(cand_total)

        retrieved_docs = tuple(hit.document_id for hit in result.hits)
        forbidden_hit = any(doc_id in forbidden for doc_id in retrieved_docs)
        if forbidden_hit:
            forbidden_hits += 1

        if expected:
            positive_count += 1
            first_rank = next(
                (
                    idx
                    for idx, doc_id in enumerate(retrieved_docs, start=1)
                    if doc_id in expected
                ),
                None,
            )
            if first_rank is not None:
                recall_hits += 1
                mrr_sum += 1.0 / first_rank
            else:
                mis_retrievals.append(
                    MisRetrievalExample(
                        query_id=item["query_id"],
                        category=item["category"],
                        service=item["service"],
                        query=item["query"],
                        expected_doc_ids=expected,
                        forbidden_doc_ids=forbidden,
                        retrieved_doc_ids=retrieved_docs,
                        status=result.status,
                        reason=result.reason or "expected_doc_not_in_top_k",
                    )
                )
            if forbidden_hit and first_rank is not None:
                mis_retrievals.append(
                    MisRetrievalExample(
                        query_id=item["query_id"],
                        category=item["category"],
                        service=item["service"],
                        query=item["query"],
                        expected_doc_ids=expected,
                        forbidden_doc_ids=forbidden,
                        retrieved_doc_ids=retrieved_docs,
                        status=result.status,
                        reason="forbidden_doc_in_top_k",
                    )
                )
        else:
            no_match_count += 1
            if result.status == "no_match":
                no_match_correct += 1
            else:
                mis_retrievals.append(
                    MisRetrievalExample(
                        query_id=item["query_id"],
                        category=item["category"],
                        service=item["service"],
                        query=item["query"],
                        expected_doc_ids=expected,
                        forbidden_doc_ids=forbidden,
                        retrieved_doc_ids=retrieved_docs,
                        status=result.status,
                        reason="false_positive_on_no_match_query",
                    )
                )

    total = len(queries)
    return EvalMethodMetrics(
        method=method_name,
        split=split,
        total_queries=total,
        positive_queries=positive_count,
        no_match_queries=no_match_count,
        recall_at_5=(recall_hits / positive_count) if positive_count else 0.0,
        mrr=(mrr_sum / positive_count) if positive_count else 0.0,
        no_match_accuracy=(no_match_correct / no_match_count) if no_match_count else 1.0,
        forbidden_hit_rate=(forbidden_hits / total) if total else 0.0,
        p95_latency_ms=_percentile_95(latencies),
        mean_candidate_count=(
            sum(candidate_totals) / len(candidate_totals) if candidate_totals else 0.0
        ),
        mis_retrievals=tuple(mis_retrievals),
    )


def run_retrieval_evaluation(
    *,
    store: KnowledgeStore,
    queries_path: Path,
    embedding_provider: EmbeddingProvider | None = None,
    split: str = "all",
    top_k: int = DEFAULT_TOP_K,
) -> EvalMetrics:
    """Compare mysql_ngram, vector_cosine, hybrid_rrf, and offline_bm25."""
    provider = embedding_provider or OfflineHashEmbeddingProvider()
    queries = load_eval_queries(queries_path, split=split)
    services = sorted({item["service"] for item in queries})
    retriever = KnowledgeRetriever(store, embedding_provider=provider)
    bm25 = OfflineBM25Retriever.from_store(store, services)

    published_docs = 0
    published_chunks = 0
    for svc in services:
        published_docs += len(store.list_documents(service=svc, published_only=True))
        published_chunks += len(store.list_published_chunks(service=svc))

    method_metrics = (
        evaluate_method(
            method_name="mysql_ngram",
            search_fn=lambda *, service, query, top_k=top_k: retriever.search(
                service=service,
                query=query,
                top_k=top_k,
                method="mysql_ngram",
            ),
            queries=queries,
            split=split,
            top_k=top_k,
        ),
        evaluate_method(
            method_name="vector_cosine",
            search_fn=lambda *, service, query, top_k=top_k: retriever.search(
                service=service,
                query=query,
                top_k=top_k,
                method="vector_cosine",
            ),
            queries=queries,
            split=split,
            top_k=top_k,
        ),
        evaluate_method(
            method_name="hybrid_rrf",
            search_fn=lambda *, service, query, top_k=top_k: retriever.search(
                service=service,
                query=query,
                top_k=top_k,
                method="hybrid_rrf",
            ),
            queries=queries,
            split=split,
            top_k=top_k,
        ),
        evaluate_method(
            method_name="offline_bm25",
            search_fn=bm25.search,
            queries=queries,
            split=split,
            top_k=top_k,
        ),
    )

    return EvalMetrics(
        split=split,
        corpus_document_count=published_docs,
        corpus_chunk_count=published_chunks,
        methods=method_metrics,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Stage 2 knowledge retrieval evaluation."
    )
    parser.add_argument("--corpus-dir", type=Path, default=Path("knowledge/corpus"))
    parser.add_argument(
        "--queries-path",
        type=Path,
        default=Path("knowledge/eval/queries.json"),
    )
    parser.add_argument("--split", choices=("dev", "holdout", "all"), default="all")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--database-url", type=str, default=None)
    args = parser.parse_args(argv)

    provider = OfflineHashEmbeddingProvider()
    store = KnowledgeStore(args.database_url)
    import_corpus_into_store(store, args.corpus_dir, embedding_provider=provider)
    report = run_retrieval_evaluation(
        store=store,
        queries_path=args.queries_path,
        embedding_provider=provider,
        split=args.split,
        top_k=args.top_k,
    )
    print(report.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
