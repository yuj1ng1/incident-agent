"""Pluggable embedding providers and strict float32 vector serialization."""
from collections.abc import Sequence
import hashlib
import math
import os
import re
import struct
from typing import Literal, Protocol, runtime_checkable

import httpx2
from pydantic import BaseModel, ConfigDict, Field, model_validator

from knowledge.models import EmbeddingError

DEFAULT_OFFLINE_MODEL = "offline-ngram-v1"
DEFAULT_OFFLINE_DIM = 256
MAX_EMBEDDING_DIM = 8192

_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:-]+")
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|[_.:-]+")


def validate_vector(
    values: Sequence[float],
    *,
    expected_dim: int | None = None,
) -> tuple[float, ...]:
    """Validate dimension, finiteness, and non-zero float32 norm."""
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise EmbeddingError("embedding_must_be_numeric_sequence")
    dim = len(values)
    if dim == 0 or dim > MAX_EMBEDDING_DIM:
        raise EmbeddingError("invalid_embedding_dimension")
    if expected_dim is not None and dim != expected_dim:
        raise EmbeddingError("embedding_dimension_mismatch")

    parsed: list[float] = []
    for item in values:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise EmbeddingError("embedding_element_must_be_number")
        number = float(item)
        if not math.isfinite(number):
            raise EmbeddingError("non_finite_embedding_value")
        parsed.append(number)

    try:
        packed = struct.pack(f"<{dim}f", *parsed)
    except OverflowError as exc:
        raise EmbeddingError("embedding_float32_overflow") from exc

    unpacked = struct.unpack(f"<{dim}f", packed)
    squared_norm = 0.0
    for val in unpacked:
        if not math.isfinite(val):
            raise EmbeddingError("non_finite_float32_embedding_value")
        squared_norm += val * val
    if squared_norm <= 0.0 or not math.isfinite(squared_norm):
        raise EmbeddingError("zero_norm_embedding")
    return unpacked


def encode_float32(
    values: Sequence[float],
    *,
    expected_dim: int | None = None,
) -> bytes:
    """Validate and encode a numeric vector as little-endian float32 bytes."""
    validated = validate_vector(values, expected_dim=expected_dim)
    return struct.pack(f"<{len(validated)}f", *validated)


def decode_float32(
    blob: bytes | bytearray | memoryview,
    *,
    expected_dim: int | None = None,
) -> tuple[float, ...]:
    """Decode and strictly validate a little-endian float32 vector blob."""
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise EmbeddingError("embedding_blob_must_be_bytes")
    raw = bytes(blob)
    if not raw or len(raw) % 4 != 0:
        raise EmbeddingError("invalid_embedding_blob_length")
    dim = len(raw) // 4
    if dim > MAX_EMBEDDING_DIM:
        raise EmbeddingError("invalid_embedding_dimension")
    if expected_dim is not None and dim != expected_dim:
        raise EmbeddingError("embedding_dimension_mismatch")

    unpacked = struct.unpack(f"<{dim}f", raw)
    squared_norm = 0.0
    for val in unpacked:
        if not math.isfinite(val):
            raise EmbeddingError("non_finite_embedding_value")
        squared_norm += val * val
    if squared_norm <= 0.0 or not math.isfinite(squared_norm):
        raise EmbeddingError("zero_norm_embedding")
    return unpacked


def cosine_similarity(
    vec_a: Sequence[float],
    vec_b: Sequence[float],
) -> float:
    """Compute exact cosine similarity between two equal-dimension vectors."""
    if len(vec_a) != len(vec_b) or not vec_a:
        raise EmbeddingError("embedding_dimension_mismatch")
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for a, b in zip(vec_a, vec_b):
        if not (math.isfinite(a) and math.isfinite(b)):
            raise EmbeddingError("non_finite_embedding_value")
        dot += a * b
        norm_a += a * a
        norm_b += b * b
    denom = math.sqrt(norm_a) * math.sqrt(norm_b)
    if denom <= 0.0 or not math.isfinite(denom):
        raise EmbeddingError("zero_norm_embedding")
    score = dot / denom
    return max(-1.0, min(1.0, score))


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol for pluggable text embedding backends."""

    @property
    def model_name(self) -> str:
        ...

    @property
    def dimension(self) -> int:
        ...

    def embed_texts(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        ...

    def embed_query(self, text: str) -> tuple[float, ...]:
        ...


class EmbeddingConfig(BaseModel):
    """Explicit configuration for selecting and validating an embedding backend."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    provider: Literal["offline", "openai_compatible"] = "offline"
    model: str = Field(default=DEFAULT_OFFLINE_MODEL, min_length=1, max_length=120)
    dimension: int | None = Field(default=DEFAULT_OFFLINE_DIM, ge=1, le=MAX_EMBEDDING_DIM)
    base_url: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    timeout_seconds: float = Field(default=15.0, gt=0.0, le=60.0)
    max_batch_size: int = Field(default=32, ge=1, le=256)
    send_dimensions: bool = False

    @model_validator(mode="after")
    def _validate_provider_fields(self):
        if self.provider == "offline":
            if self.dimension is None:
                self.dimension = DEFAULT_OFFLINE_DIM
        elif self.provider == "openai_compatible":
            if not self.base_url or not self.base_url.strip():
                raise EmbeddingError("embedding_base_url_required")
            cleaned = self.base_url.strip()
            if not cleaned.startswith(("http://", "https://")):
                raise EmbeddingError("embedding_base_url_must_be_http")
            self.base_url = cleaned.rstrip("/")
            if not self.api_key or not self.api_key.strip():
                raise EmbeddingError("embedding_api_key_required")
            if self.dimension is None:
                raise EmbeddingError("embedding_dimension_required")
        return self


def load_embedding_config_from_env() -> EmbeddingConfig:
    """Build EmbeddingConfig from INCIDENT_AGENT_EMBEDDING_* environment vars."""
    provider = (
        os.environ.get("INCIDENT_AGENT_EMBEDDING_PROVIDER", "offline")
        .strip()
        .lower()
    )
    if provider not in {"offline", "openai_compatible"}:
        raise EmbeddingError("unsupported_embedding_provider")

    dim_raw = os.environ.get("INCIDENT_AGENT_EMBEDDING_DIM", "").strip()
    dimension = int(dim_raw) if dim_raw else (DEFAULT_OFFLINE_DIM if provider == "offline" else None)

    timeout_raw = os.environ.get("INCIDENT_AGENT_EMBEDDING_TIMEOUT_SECONDS", "").strip()
    timeout_seconds = float(timeout_raw) if timeout_raw else 15.0

    default_model = DEFAULT_OFFLINE_MODEL if provider == "offline" else ""
    model = os.environ.get("INCIDENT_AGENT_EMBEDDING_MODEL", default_model).strip()
    if not model:
        raise EmbeddingError("embedding_model_required")

    return EmbeddingConfig(
        provider=provider,
        model=model,
        dimension=dimension,
        base_url=os.environ.get("INCIDENT_AGENT_EMBEDDING_BASE_URL", "").strip() or None,
        api_key=os.environ.get("INCIDENT_AGENT_EMBEDDING_API_KEY", "").strip() or None,
        timeout_seconds=timeout_seconds,
        send_dimensions=(
            os.environ.get("INCIDENT_AGENT_EMBEDDING_SEND_DIMENSIONS", "").strip().lower()
            in {"1", "true", "yes"}
        ),
    )


def _extract_weighted_features(text: str) -> list[tuple[str, float]]:
    """Extract CJK char n-grams and code/word tokens for deterministic hashing."""
    features: list[tuple[str, float]] = []
    compact = re.sub(r"\s+", " ", text.strip())
    if not compact:
        return features

    for token in _TOKEN_RE.findall(compact):
        features.append((f"tok:{token.lower()}", 2.5))
        features.append((f"raw:{token}", 1.5))
        for part in _CAMEL_SPLIT_RE.split(token):
            if len(part) >= 2:
                features.append((f"sub:{part.lower()}", 1.2))

    # Keep CJK and non-whitespace character n-grams for Chinese operational prose.
    cjk_or_text = "".join(ch for ch in compact.lower() if not ch.isspace())
    for n, weight in ((1, 0.8), (2, 1.6), (3, 1.8)):
        if len(cjk_or_text) >= n:
            for idx in range(len(cjk_or_text) - n + 1):
                gram = cjk_or_text[idx : idx + n]
                features.append((f"c{n}:{gram}", weight))
    return features


class OfflineHashEmbeddingProvider:
    """Deterministic local n-gram hashing embeddings for tests and offline mode."""

    def __init__(
        self,
        *,
        model_name: str = DEFAULT_OFFLINE_MODEL,
        dimension: int = DEFAULT_OFFLINE_DIM,
    ):
        if not model_name or not model_name.strip():
            raise EmbeddingError("embedding_model_required")
        if type(dimension) is not int or not (1 <= dimension <= MAX_EMBEDDING_DIM):
            raise EmbeddingError("invalid_embedding_dimension")
        self._model_name = model_name.strip()
        self._dimension = dimension

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    def _embed_one(self, text: str) -> tuple[float, ...]:
        if not isinstance(text, str) or not text.strip():
            raise EmbeddingError("empty_embedding_input")
        features = _extract_weighted_features(text)
        if not features:
            raise EmbeddingError("empty_embedding_input")

        vec = [0.0] * self._dimension
        for feature, weight in features:
            digest = hashlib.blake2b(
                feature.encode("utf-8"),
                digest_size=8,
            ).digest()
            raw_int = int.from_bytes(digest, "little")
            bucket = raw_int % self._dimension
            sign = 1.0 if ((raw_int >> 63) & 1) == 0 else -1.0
            vec[bucket] += sign * weight

        norm = math.sqrt(sum(v * v for v in vec))
        if norm <= 0.0:
            # Deterministic fallback if signed hashes exactly cancelled out.
            fallback_digest = hashlib.sha256(text.encode("utf-8")).digest()
            bucket = int.from_bytes(fallback_digest[:4], "little") % self._dimension
            vec[bucket] = 1.0
            norm = 1.0

        normalized = [v / norm for v in vec]
        return validate_vector(normalized, expected_dim=self._dimension)

    def embed_texts(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence):
            raise EmbeddingError("texts_must_be_sequence")
        return [self._embed_one(text) for text in texts]

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._embed_one(text)


class OpenAICompatibleEmbeddingProvider:
    """Config-driven HTTP client for OpenAI-compatible /embeddings endpoints."""

    def __init__(
        self,
        config: EmbeddingConfig,
        *,
        client: httpx2.Client | None = None,
    ):
        if config.provider != "openai_compatible":
            raise EmbeddingError("expected_openai_compatible_config")
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx2.Client(timeout=config.timeout_seconds)

    @property
    def model_name(self) -> str:
        return self._config.model

    @property
    def dimension(self) -> int:
        assert self._config.dimension is not None
        return self._config.dimension

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request_batch(self, batch: Sequence[str]) -> list[tuple[float, ...]]:
        payload: dict[str, object] = {
            "model": self._config.model,
            "input": list(batch),
        }
        if self._config.send_dimensions:
            payload["dimensions"] = self.dimension

        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self._config.base_url}/embeddings"
        try:
            response = self._client.post(
                url,
                json=payload,
                headers=headers,
                timeout=self._config.timeout_seconds,
            )
        except httpx2.TimeoutException as exc:
            raise EmbeddingError("embedding_request_timeout") from exc
        except httpx2.HTTPError as exc:
            raise EmbeddingError(
                f"embedding_http_error:{type(exc).__name__}"
            ) from exc

        if response.status_code != 200:
            raise EmbeddingError(f"embedding_http_status_{response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise EmbeddingError("embedding_invalid_json") from exc

        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise EmbeddingError("embedding_response_missing_data")
        items = body["data"]
        if len(items) != len(batch):
            raise EmbeddingError("embedding_response_count_mismatch")

        ordered: list[tuple[float, ...] | None] = [None] * len(batch)
        for pos, item in enumerate(items):
            if not isinstance(item, dict):
                raise EmbeddingError("embedding_item_invalid")
            idx = item.get("index", pos)
            if type(idx) is not int or not (0 <= idx < len(batch)):
                raise EmbeddingError("embedding_item_index_invalid")
            if ordered[idx] is not None:
                raise EmbeddingError("embedding_duplicate_index")
            ordered[idx] = validate_vector(
                item.get("embedding"),
                expected_dim=self.dimension,
            )

        return [vec for vec in ordered if vec is not None]

    def embed_texts(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence):
            raise EmbeddingError("texts_must_be_sequence")
        if not texts:
            return []
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                raise EmbeddingError("empty_embedding_input")

        results: list[tuple[float, ...]] = []
        step = self._config.max_batch_size
        for start in range(0, len(texts), step):
            results.extend(self._request_batch(texts[start : start + step]))
        return results

    def embed_query(self, text: str) -> tuple[float, ...]:
        if not isinstance(text, str) or not text.strip():
            raise EmbeddingError("empty_embedding_input")
        return self._request_batch([text])[0]


def build_embedding_provider(config: EmbeddingConfig | None = None) -> EmbeddingProvider:
    """Instantiate an EmbeddingProvider from explicit or environment config."""
    resolved = config or load_embedding_config_from_env()
    if resolved.provider == "offline":
        return OfflineHashEmbeddingProvider(
            model_name=resolved.model,
            dimension=resolved.dimension or DEFAULT_OFFLINE_DIM,
        )
    return OpenAICompatibleEmbeddingProvider(resolved)
