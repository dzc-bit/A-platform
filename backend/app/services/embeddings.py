from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from collections import Counter

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

# Local hash embedding: stable, normalized, and fully offline. These constants
# double as the version/dimension contract for stored chunk payloads when the
# deployment runs without a cloud embedding provider.
EMBEDDING_DIMENSIONS = 192
HASH_EMBEDDING_VERSION = "hash-faiss-v1"

_CLOUD_TIMEOUT_SECONDS = 30


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def token_features(text: str) -> Counter[str]:
    """Return deterministic mixed Chinese/Latin features for local embeddings."""
    normalized = _normalize_text(text)
    chinese = re.findall(r"[\u4e00-\u9fff]+", normalized)
    latin = re.findall(r"[a-z0-9_]+", normalized)
    grams = [f"zh:{piece[index:index + 2]}" for piece in chinese for index in range(max(len(piece) - 1, 0))]
    singles = [f"c:{char}" for piece in chinese for char in piece]
    return Counter([*grams, *singles, *(f"w:{token}" for token in latin)])


def hash_embedding(text: str, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """Create a stable, normalized local embedding without sending text externally.

    Implements LangChain's expected vector semantics so the classroom FAISS
    pipeline stays reproducible on a disconnected machine. The cloud client
    below is the single, well-defined replacement point for hosted models.
    """
    vector = [0.0] * dimensions
    for feature, count in token_features(text).items():
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[bucket] += sign * math.sqrt(count)
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def is_cloud_configured() -> bool:
    """Whether an OpenAI-compatible embedding provider is fully configured."""
    return bool(settings.embedding_api_key and settings.embedding_model)


def embedding_version() -> str:
    """Version label of the ACTIVE embedding implementation.

    It participates in stored chunk payloads, the FAISS index cache and the
    retrieval cache keys, so switching providers migrates caches and stored
    vectors instead of mixing two vector spaces.
    """
    if is_cloud_configured():
        return f"cloud:{settings.embedding_model}"
    return HASH_EMBEDDING_VERSION


class EmbeddingAPIError(RuntimeError):
    """A configured cloud embedding call failed; callers degrade cleanly."""


# Known limitation: the retrieval pipeline (FAISS indexing and query embedding)
# is synchronous, so a configured cloud provider is called synchronously too and
# can block the event loop for up to _CLOUD_TIMEOUT_SECONDS during an outage.
# Query-time failures degrade to the sparse retriever; indexing failures fall
# back to hash vectors. Moving retrieval behind asyncio.to_thread end-to-end is
# the follow-up if this becomes a production concern.


# One process-wide synchronous client: the retrieval pipeline (FAISS indexing
# and query embedding) is synchronous, so the asyncio pool in services/llm.py
# does not apply here. httpx.Client is thread-safe for concurrent requests.
_http_lock = threading.Lock()
_http_client: httpx.Client | None = None


def _get_http_client() -> httpx.Client:
    global _http_client
    with _http_lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = httpx.Client(
                timeout=_CLOUD_TIMEOUT_SECONDS,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return _http_client


def close_http_client() -> None:
    """Close the pooled sync client on application shutdown; safe to repeat."""
    global _http_client
    with _http_lock:
        if _http_client is None:
            return
        try:
            _http_client.close()
        except Exception:  # noqa: BLE001 - shutdown must never raise here
            pass
        _http_client = None


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts through the configured OpenAI-compatible /embeddings API.

    Raises EmbeddingAPIError on connectivity, HTTP, or payload failures so the
    caller can degrade cleanly; empty input short-circuits to [].
    """
    if not texts:
        return []
    if not is_cloud_configured():
        raise EmbeddingAPIError("未配置 EMBEDDING_API_KEY/EMBEDDING_MODEL，云端向量不可用")
    endpoint = f"{settings.embedding_base_url.rstrip('/')}/embeddings"
    try:
        response = _get_http_client().post(
            endpoint,
            headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
            json={"model": settings.embedding_model, "input": texts},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as error:
        raise EmbeddingAPIError(f"云端向量调用失败：{type(error).__name__}") from error
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or len(data) != len(texts):
        raise EmbeddingAPIError("云端向量响应缺少与输入等长的 data 数组")
    vectors: list[list[float]] = []
    for item in data:
        embedding = item.get("embedding") if isinstance(item, dict) else None
        if not isinstance(embedding, list) or not embedding:
            raise EmbeddingAPIError("云端向量响应格式不正确")
        try:
            vectors.append([float(value) for value in embedding])
        except (TypeError, ValueError) as error:
            raise EmbeddingAPIError("云端向量包含非数值项") from error
    return vectors


def embed_texts_with_fallback(texts: list[str]) -> tuple[list[list[float]], str]:
    """Embed texts for storage, returning ``(vectors, version_label)``.

    Cloud embeddings are used when a provider is configured and reachable;
    otherwise the local hash implementation answers with the hash version
    label. The label always matches the returned vector space, so a degraded
    indexing run can never store hash vectors under the cloud version —
    ensure_documents_indexed re-indexes such chunks on the next startup.
    """
    if is_cloud_configured():
        try:
            return embed_texts(texts), embedding_version()
        except EmbeddingAPIError:
            logger.warning("云端向量调用失败，本次索引退化为本地哈希向量", exc_info=True)
    return [hash_embedding(text) for text in texts], HASH_EMBEDDING_VERSION
