"""Deterministic Markdown validation, heading-aware chunking, and IDs."""
import hashlib
from pathlib import PurePosixPath
import re

from knowledge.models import (
    ChunkedDocument,
    KnowledgeChunk,
    KnowledgeValidationError,
)

DEFAULT_MAX_MARKDOWN_BYTES = 512 * 1024
DEFAULT_MAX_CHUNK_CHARS = 1000
MAX_HEADING_PATH_CHARS = 500
MAX_TITLE_CHARS = 300

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+[ \t]*)?$")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def normalize_utf8_markdown(
    content: str | bytes,
    *,
    max_bytes: int = DEFAULT_MAX_MARKDOWN_BYTES,
) -> str:
    """Validate bounded UTF-8 Markdown input and normalize line endings."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise KnowledgeValidationError("positive_max_bytes_required")

    if isinstance(content, (bytes, bytearray)):
        raw_bytes = bytes(content)
        if len(raw_bytes) > max_bytes:
            raise KnowledgeValidationError("markdown_exceeds_max_bytes")
        try:
            text = raw_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise KnowledgeValidationError("markdown_invalid_utf8") from exc
    elif isinstance(content, str):
        try:
            encoded = content.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise KnowledgeValidationError("markdown_invalid_utf8") from exc
        if len(encoded) > max_bytes:
            raise KnowledgeValidationError("markdown_exceeds_max_bytes")
        text = content
    else:
        raise KnowledgeValidationError("markdown_must_be_str_or_utf8_bytes")

    if "\x00" in text:
        raise KnowledgeValidationError("markdown_contains_nul_byte")

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise KnowledgeValidationError("markdown_content_empty")
    return normalized


def compute_content_sha256(normalized_markdown: str) -> str:
    """Return lowercase hex SHA-256 of normalized UTF-8 Markdown content."""
    return hashlib.sha256(normalized_markdown.encode("utf-8")).hexdigest()


def _safe_slug(value: str, *, limit: int = 40) -> str:
    stem = PurePosixPath(value.replace("\\", "/")).stem or value
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._").lower()
    if not slug:
        return "doc"
    return slug[:limit].rstrip("-._") or "doc"


def deterministic_document_id(service: str, source: str) -> str:
    """Derive a stable document ID from (service, source)."""
    clean_service = service.strip()
    clean_source = source.strip()
    if not clean_service or not clean_source:
        raise KnowledgeValidationError("service_and_source_required")
    digest = hashlib.sha256(
        f"{clean_service}\0{clean_source}".encode("utf-8")
    ).hexdigest()[:16]
    slug = _safe_slug(clean_source, limit=48)
    return f"doc-{slug}-{digest}"


def deterministic_version_id(document_id: str, content_sha256: str) -> str:
    """Derive a stable version ID from (document_id, content_sha256)."""
    digest = hashlib.sha256(
        f"{document_id}\0{content_sha256}".encode("utf-8")
    ).hexdigest()[:24]
    return f"ver-{digest}"


def deterministic_chunk_id(
    version_id: str,
    ordinal: int,
    heading_path: str,
    start_line: int,
    end_line: int,
) -> str:
    """Derive a stable chunk ID from version identity and exact chunk coordinates."""
    payload = f"{version_id}\0{ordinal}\0{heading_path}\0{start_line}\0{end_line}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"chk-{digest}"


def _format_heading_path(stack: list[tuple[int, str]], fallback: str) -> str:
    if not stack:
        path = fallback
    else:
        path = " > ".join(title for _, title in stack)
    if len(path) <= MAX_HEADING_PATH_CHARS:
        return path
    return path[: MAX_HEADING_PATH_CHARS - 3].rstrip() + "..."


def _split_oversized_block(
    lines: list[str],
    start_line: int,
    end_line: int,
    max_chunk_chars: int,
) -> list[tuple[int, int]]:
    """Split a multi-line block into line ranges bounded by max_chunk_chars."""
    ranges: list[tuple[int, int]] = []
    cur_start = start_line
    cur_chars = 0
    for line_no in range(start_line, end_line + 1):
        line_len = len(lines[line_no - 1])
        added = line_len if cur_chars == 0 else line_len + 1
        if cur_chars > 0 and cur_chars + added > max_chunk_chars:
            ranges.append((cur_start, line_no - 1))
            cur_start = line_no
            cur_chars = line_len
        else:
            cur_chars += added
    if cur_start <= end_line:
        ranges.append((cur_start, end_line))
    return ranges


def _trim_blank_edges(lines: list[str], start_line: int, end_line: int) -> tuple[int, int] | None:
    while start_line <= end_line and not lines[start_line - 1].strip():
        start_line += 1
    while end_line >= start_line and not lines[end_line - 1].strip():
        end_line -= 1
    if start_line > end_line:
        return None
    return start_line, end_line


def _parse_sections(
    lines: list[str],
    fallback_heading: str,
) -> tuple[str | None, list[tuple[str, int | None, list[tuple[int, int]]]]]:
    """Parse Markdown into sections of (heading_path, heading_line, block_ranges)."""
    heading_stack: list[tuple[int, str]] = []
    sections: list[tuple[str, int | None, list[tuple[int, int]]]] = []
    first_heading: str | None = None

    current_heading_path = _format_heading_path(heading_stack, fallback_heading)
    current_heading_line: int | None = None
    current_blocks: list[tuple[int, int]] = []

    block_start: int | None = None
    fence_char: str | None = None
    fence_len = 0

    def flush_block(end_line: int) -> None:
        nonlocal block_start
        if block_start is not None:
            trimmed = _trim_blank_edges(lines, block_start, end_line)
            if trimmed is not None:
                current_blocks.append(trimmed)
            block_start = None

    def flush_section() -> None:
        if current_blocks:
            sections.append(
                (current_heading_path, current_heading_line, list(current_blocks))
            )

    for idx, line in enumerate(lines, start=1):
        fence_match = _FENCE_RE.match(line)
        if fence_char is None:
            if fence_match:
                marker = fence_match.group(1)
                fence_char = marker[0]
                fence_len = len(marker)
                if block_start is None:
                    block_start = idx
                continue
        else:
            if (
                fence_match
                and fence_match.group(1)[0] == fence_char
                and len(fence_match.group(1)) >= fence_len
                and line.strip() == fence_match.group(1)
            ):
                fence_char = None
                fence_len = 0
                if block_start is None:
                    block_start = idx
                continue
            if block_start is None:
                block_start = idx
            continue

        heading_match = _HEADING_RE.match(line)
        if heading_match:
            flush_block(idx - 1)
            flush_section()
            current_blocks = []

            level = len(heading_match.group(1))
            heading_text = heading_match.group(2).strip()
            if first_heading is None:
                first_heading = heading_text
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, heading_text))
            current_heading_path = _format_heading_path(heading_stack, fallback_heading)
            current_heading_line = idx
            continue

        if not line.strip():
            flush_block(idx - 1)
        elif block_start is None:
            block_start = idx

    flush_block(len(lines))
    if current_blocks:
        flush_section()
    elif not sections and current_heading_line is not None:
        sections.append(
            (current_heading_path, current_heading_line, [(current_heading_line, current_heading_line)])
        )

    return first_heading, sections


def chunk_markdown(
    *,
    service: str,
    source: str,
    raw_markdown: str | bytes,
    title: str | None = None,
    document_id: str | None = None,
    max_bytes: int = DEFAULT_MAX_MARKDOWN_BYTES,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
) -> ChunkedDocument:
    """Validate and chunk a Markdown runbook into deterministic KnowledgeChunks."""
    clean_service = service.strip() if isinstance(service, str) else ""
    clean_source = source.strip() if isinstance(source, str) else ""
    if not clean_service or len(clean_service) > 200:
        raise KnowledgeValidationError("invalid_service_name")
    if not clean_source or len(clean_source) > 300:
        raise KnowledgeValidationError("invalid_source_name")
    if type(max_chunk_chars) is not int or max_chunk_chars < 64:
        raise KnowledgeValidationError("invalid_max_chunk_chars")

    normalized = normalize_utf8_markdown(raw_markdown, max_bytes=max_bytes)
    sha256_hex = compute_content_sha256(normalized)
    resolved_doc_id = (
        document_id.strip()
        if document_id is not None
        else deterministic_document_id(clean_service, clean_source)
    )
    if not resolved_doc_id or len(resolved_doc_id) > 120:
        raise KnowledgeValidationError("invalid_document_id")
    version_id = deterministic_version_id(resolved_doc_id, sha256_hex)

    lines = normalized.split("\n")
    fallback_heading = (title.strip() if isinstance(title, str) and title.strip() else None) or clean_source
    first_heading, sections = _parse_sections(lines, fallback_heading)

    resolved_title = (
        title.strip()
        if isinstance(title, str) and title.strip()
        else (first_heading or PurePosixPath(clean_source.replace("\\", "/")).stem or clean_source)
    )
    if len(resolved_title) > MAX_TITLE_CHARS:
        resolved_title = resolved_title[:MAX_TITLE_CHARS]

    raw_ranges: list[tuple[str, int, int]] = []
    for heading_path, heading_line, blocks in sections:
        expanded_blocks: list[tuple[int, int]] = []
        for blk_start, blk_end in blocks:
            blk_text = "\n".join(lines[blk_start - 1 : blk_end])
            if len(blk_text) <= max_chunk_chars:
                expanded_blocks.append((blk_start, blk_end))
            else:
                expanded_blocks.extend(
                    _split_oversized_block(lines, blk_start, blk_end, max_chunk_chars)
                )

        chunk_start = (
            heading_line
            if heading_line is not None and heading_line < expanded_blocks[0][0]
            else expanded_blocks[0][0]
        )
        chunk_end = expanded_blocks[0][1]
        for blk_start, blk_end in expanded_blocks[1:]:
            candidate_text = "\n".join(lines[chunk_start - 1 : blk_end])
            if len(candidate_text) <= max_chunk_chars:
                chunk_end = blk_end
            else:
                raw_ranges.append((heading_path, chunk_start, chunk_end))
                chunk_start = blk_start
                chunk_end = blk_end
        raw_ranges.append((heading_path, chunk_start, chunk_end))

    if not raw_ranges:
        raise KnowledgeValidationError("markdown_produced_no_chunks")

    chunks: list[KnowledgeChunk] = []
    for ordinal, (heading_path, start_line, end_line) in enumerate(raw_ranges):
        trimmed = _trim_blank_edges(lines, start_line, end_line)
        if trimmed is None:
            continue
        s_line, e_line = trimmed
        chunk_text = "\n".join(lines[s_line - 1 : e_line])
        chunk_id = deterministic_chunk_id(
            version_id,
            len(chunks),
            heading_path,
            s_line,
            e_line,
        )
        chunks.append(
            KnowledgeChunk(
                chunk_id=chunk_id,
                version_id=version_id,
                ordinal=len(chunks),
                heading_path=heading_path,
                start_line=s_line,
                end_line=e_line,
                chunk_text=chunk_text,
            )
        )

    if not chunks:
        raise KnowledgeValidationError("markdown_produced_no_chunks")

    return ChunkedDocument(
        document_id=resolved_doc_id,
        version_id=version_id,
        service=clean_service,
        title=resolved_title,
        source=clean_source,
        content_sha256=sha256_hex,
        raw_markdown=normalized,
        chunks=tuple(chunks),
    )
