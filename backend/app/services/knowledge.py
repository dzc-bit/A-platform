from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import KnowledgeChunk, KnowledgeDocument
from .cache import retrieval_cache
from .runtime_settings import get_runtime_settings

try:
    from langchain_community.vectorstores import FAISS
    from langchain_core.documents import Document
    from langchain_core.embeddings import Embeddings
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    LANGCHAIN_RAG_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only before optional packages are installed
    FAISS = Document = Embeddings = RecursiveCharacterTextSplitter = None  # type: ignore[assignment,misc]
    LANGCHAIN_RAG_AVAILABLE = False


EMBEDDING_DIMENSIONS = 192
EMBEDDING_VERSION = "hash-faiss-v1"
RETRIEVAL_VERSION = "hybrid-rrf-v1"
_RRF_K = 60
_VECTOR_RRF_WEIGHT = 0.65
_KEYWORD_RRF_WEIGHT = 0.35


@dataclass(frozen=True)
class RetrievalHit:
    document_id: int
    title: str
    excerpt: str
    score: float


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def _tokens(text: str) -> Counter[str]:
    """Return deterministic mixed Chinese/Latin features for local embeddings."""
    normalized = _normalize(text)
    chinese = re.findall(r"[\u4e00-\u9fff]+", normalized)
    latin = re.findall(r"[a-z0-9_]+", normalized)
    grams = [f"zh:{piece[index:index + 2]}" for piece in chinese for index in range(max(len(piece) - 1, 0))]
    singles = [f"c:{char}" for piece in chinese for char in piece]
    return Counter([*grams, *singles, *(f"w:{token}" for token in latin)])


def _hash_embedding(text: str, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """Create a stable, normalized local embedding without sending text externally.

    The class intentionally implements LangChain's Embeddings contract. This makes the
    classroom FAISS pipeline reproducible on a disconnected machine while leaving a
    single, well-defined replacement point for a hosted embedding model later.
    """
    vector = [0.0] * dimensions
    for feature, count in _tokens(text).items():
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[bucket] += sign * math.sqrt(count)
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


if LANGCHAIN_RAG_AVAILABLE:

    class DeterministicHashEmbeddings(Embeddings):
        """Offline LangChain embedding adapter used by the local FAISS index."""

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [_hash_embedding(text) for text in texts]

        def embed_query(self, text: str) -> list[float]:
            return _hash_embedding(text)


    _embeddings: Any = DeterministicHashEmbeddings()
else:
    _embeddings = None


_index_lock = threading.Lock()
_index_cache: tuple[str, Any] | None = None


def _invalidate_vector_index() -> None:
    global _index_cache
    with _index_lock:
        _index_cache = None


def _splitter(size: int, overlap: int) -> Any | None:
    if not LANGCHAIN_RAG_AVAILABLE or RecursiveCharacterTextSplitter is None:
        return None
    return RecursiveCharacterTextSplitter(
        chunk_size=size,
        chunk_overlap=overlap,
        separators=["\n\n", "\n", "。", "！", "？", ".", " ", ""],
    )


def split_text(text: str, size: int = 220, overlap: int = 40) -> list[str]:
    """Split knowledge text through LangChain when installed, with a safe fallback."""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    if not 0 <= overlap < size:
        raise ValueError("chunk overlap must be non-negative and smaller than chunk size")
    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        return []
    splitter = _splitter(size, overlap)
    if splitter is not None:
        return [chunk.strip() for chunk in splitter.split_text(clean) if chunk.strip()]
    if len(clean) <= size:
        return [clean]
    chunks: list[str] = []
    start = 0
    while start < len(clean):
        end = min(len(clean), start + size)
        chunks.append(clean[start:end])
        if end == len(clean):
            break
        start = end - overlap
    return chunks


def _vector_payload(text: str) -> str:
    return json.dumps(
        {"version": EMBEDDING_VERSION, "embedding": _hash_embedding(text)},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _stored_embedding(payload: str, text: str) -> list[float]:
    try:
        parsed = json.loads(payload)
        embedding = parsed.get("embedding") if isinstance(parsed, dict) else None
        if isinstance(embedding, list) and len(embedding) == EMBEDDING_DIMENSIONS:
            return [float(value) for value in embedding]
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return _hash_embedding(text)


def _is_current_vector_payload(payload: str) -> bool:
    try:
        parsed = json.loads(payload)
        return (
            isinstance(parsed, dict)
            and parsed.get("version") == EMBEDDING_VERSION
            and isinstance(parsed.get("embedding"), list)
            and len(parsed["embedding"]) == EMBEDDING_DIMENSIONS
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _documents_for(document: KnowledgeDocument, size: int, overlap: int) -> list[Any]:
    chunks = split_text(document.content, size=size, overlap=overlap)
    if not LANGCHAIN_RAG_AVAILABLE or Document is None:
        return chunks
    return [
        Document(
            page_content=chunk,
            metadata={"document_id": document.id, "title": document.title, "position": position},
        )
        for position, chunk in enumerate(chunks)
    ]


def index_document(db: Session, document: KnowledgeDocument) -> int:
    """Persist LangChain-split chunks plus deterministic embeddings for FAISS retrieval."""
    runtime_settings = get_runtime_settings(db)
    db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
    documents = _documents_for(
        document,
        size=runtime_settings.knowledge_chunk_size,
        overlap=runtime_settings.knowledge_chunk_overlap,
    )
    db.add_all(
        [
            KnowledgeChunk(
                document_id=document.id,
                position=position,
                content=item.page_content if LANGCHAIN_RAG_AVAILABLE else item,
                vector_json=_vector_payload(item.page_content if LANGCHAIN_RAG_AVAILABLE else item),
            )
            for position, item in enumerate(documents)
        ]
    )
    # A re-index can change retrieval results without changing the parent row.
    _invalidate_vector_index()
    retrieval_cache.clear()
    return len(documents)


def remove_document(db: Session, document: KnowledgeDocument) -> None:
    """Delete a document and all of its persisted retrieval data."""
    db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
    db.delete(document)
    _invalidate_vector_index()
    retrieval_cache.clear()


def ensure_documents_indexed(db: Session) -> int:
    """Backfill sparse legacy chunks into the current LangChain/FAISS format."""
    indexed = 0
    documents = db.scalars(select(KnowledgeDocument).where(KnowledgeDocument.status == "ready")).all()
    for document in documents:
        chunks = db.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.document_id == document.id)
            .order_by(KnowledgeChunk.position)
        ).all()
        if not chunks or any(not _is_current_vector_payload(chunk.vector_json) for chunk in chunks):
            index_document(db, document)
            indexed += 1
    if indexed:
        db.commit()
    return indexed


def _knowledge_version(db: Session) -> str:
    rows = db.execute(
        select(KnowledgeChunk.id, KnowledgeDocument.id, KnowledgeDocument.updated_at, KnowledgeChunk.content)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(KnowledgeDocument.status == "ready")
        .order_by(KnowledgeChunk.id)
    ).all()
    digest = hashlib.sha256()
    for chunk_id, document_id, updated_at, content in rows:
        digest.update(f"{chunk_id}:{document_id}:{updated_at}:{content}".encode("utf-8"))
    return f"{len(rows)}:{digest.hexdigest()[:16]}"


def knowledge_version(db: Session) -> str:
    return f"{EMBEDDING_VERSION}:{RETRIEVAL_VERSION}:{_knowledge_version(db)}"


def _cache_key(db: Session, query: str, top_k: int) -> str:
    digest = hashlib.sha256(_normalize(query).encode("utf-8")).hexdigest()
    # Include the retrieval implementation version so a sparse-to-FAISS migration
    # cannot serve an old ranking from Redis after a rolling restart.
    return (
        f"{settings.redis_key_prefix}:rag:{EMBEDDING_VERSION}:{RETRIEVAL_VERSION}:"
        f"{_knowledge_version(db)}:{top_k}:{digest}"
    )


def _build_faiss_index(db: Session, version: str) -> Any | None:
    if not LANGCHAIN_RAG_AVAILABLE or FAISS is None or _embeddings is None:
        return None
    rows = db.execute(
        select(KnowledgeChunk, KnowledgeDocument)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(KnowledgeDocument.status == "ready")
        .order_by(KnowledgeChunk.id)
    ).all()
    if not rows:
        return None
    text_embeddings = [(chunk.content, _stored_embedding(chunk.vector_json, chunk.content)) for chunk, _ in rows]
    metadata = [
        {"document_id": document.id, "title": document.title, "position": chunk.position}
        for chunk, document in rows
    ]
    try:
        vector_store = FAISS.from_embeddings(text_embeddings, _embeddings, metadatas=metadata)
    except (ImportError, ModuleNotFoundError):  # pragma: no cover - only before faiss-cpu install
        return None
    with _index_lock:
        global _index_cache
        _index_cache = (version, vector_store)
    return vector_store


def _faiss_index(db: Session) -> Any | None:
    version = _knowledge_version(db)
    with _index_lock:
        cached = _index_cache
        if cached is not None and cached[0] == version:
            return cached[1]
    return _build_faiss_index(db, version)


def text_similarity(left: str, right: str) -> float:
    """Cosine similarity of deterministic local embeddings (normalized vectors)."""

    left_vector = _hash_embedding(left)
    right_vector = _hash_embedding(right)
    return sum(a * b for a, b in zip(left_vector, right_vector))


def _sparse_fallback(db: Session, query: str, top_k: int) -> list[RetrievalHit]:
    """Keep retrieval available if an optional vector dependency is temporarily absent."""
    query_vector = _tokens(query)
    candidates: list[RetrievalHit] = []
    normalized_query = _normalize(query)
    rows = db.execute(
        select(KnowledgeChunk, KnowledgeDocument)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(KnowledgeDocument.status == "ready")
    ).all()
    for chunk, document in rows:
        chunk_vector = _tokens(chunk.content)
        numerator = sum(value * chunk_vector.get(token, 0) for token, value in query_vector.items())
        denominator = math.sqrt(sum(value * value for value in query_vector.values())) * math.sqrt(
            sum(value * value for value in chunk_vector.values())
        )
        score = numerator / denominator if denominator else 0.0
        if normalized_query and normalized_query in _normalize(document.title):
            score += 0.15
        if score > 0:
            candidates.append(
                RetrievalHit(document.id, document.title, chunk.content[:260], round(min(score, 1.0), 3))
            )
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[:top_k]


def _faiss_retrieve(db: Session, query: str, top_k: int) -> list[RetrievalHit] | None:
    vector_store = _faiss_index(db)
    if vector_store is None:
        return None
    try:
        count = int(vector_store.index.ntotal)
        matches = vector_store.similarity_search_with_score(query, k=min(max(top_k * 3, top_k), count))
    except (AttributeError, RuntimeError, ValueError):
        return None
    selected: list[RetrievalHit] = []
    seen_document_ids: set[int] = set()
    normalized_query = _normalize(query)
    for document, distance in matches:
        document_id = int(document.metadata["document_id"])
        if document_id in seen_document_ids:
            continue
        score = 1.0 / (1.0 + max(float(distance), 0.0))
        if normalized_query and normalized_query in _normalize(str(document.metadata.get("title", ""))):
            score = min(score + 0.15, 1.0)
        selected.append(
            RetrievalHit(
                document_id=document_id,
                title=str(document.metadata["title"]),
                excerpt=document.page_content[:260],
                score=round(score, 3),
            )
        )
        seen_document_ids.add(document_id)
        if len(selected) == top_k:
            break
    return selected


def _hybrid_retrieve(db: Session, query: str, top_k: int) -> list[RetrievalHit]:
    """Fuse vector and exact-token rankings with deterministic weighted RRF.

    Vector search captures semantic similarity while the sparse side protects exact
    identifiers and domain terms. Reciprocal-rank fusion avoids comparing provider-
    specific raw score scales and keeps the offline ranking reproducible.
    """
    candidate_k = min(max(top_k * 3, top_k), 24)
    vector_hits = _faiss_retrieve(db, query, candidate_k)
    keyword_hits = _sparse_fallback(db, query, candidate_k)
    if vector_hits is None:
        return keyword_hits[:top_k]

    fused_scores: dict[int, float] = {}
    representatives: dict[int, tuple[float, RetrievalHit]] = {}
    for weight, hits in (
        (_VECTOR_RRF_WEIGHT, vector_hits),
        (_KEYWORD_RRF_WEIGHT, keyword_hits),
    ):
        for rank, hit in enumerate(hits, start=1):
            contribution = weight / (_RRF_K + rank)
            fused_scores[hit.document_id] = fused_scores.get(hit.document_id, 0.0) + contribution
            current = representatives.get(hit.document_id)
            if current is None or contribution > current[0]:
                representatives[hit.document_id] = (contribution, hit)

    maximum_score = (_VECTOR_RRF_WEIGHT + _KEYWORD_RRF_WEIGHT) / (_RRF_K + 1)
    selected = [
        RetrievalHit(
            document_id=document_id,
            title=representatives[document_id][1].title,
            excerpt=representatives[document_id][1].excerpt,
            score=round(min(score / maximum_score, 1.0), 3),
        )
        for document_id, score in fused_scores.items()
    ]
    selected.sort(key=lambda item: (-item.score, item.document_id))
    return selected[:top_k]


def retrieve(
    db: Session,
    query: str,
    top_k: int = 3,
    cache_ttl_seconds: int = 300,
) -> list[RetrievalHit]:
    """Retrieve citations from a local LangChain + FAISS RAG pipeline.

    The in-process sparse implementation is deliberately a runtime fallback, not the
    primary path. It keeps an offline classroom demo resilient if a vector extension
    is unavailable during maintenance.
    """
    if not 1 <= top_k <= 8:
        raise ValueError("top_k must be between 1 and 8")
    if cache_ttl_seconds < 0:
        raise ValueError("cache_ttl_seconds must not be negative")
    if not query.strip():
        return []
    cache_key = _cache_key(db, query, top_k)
    cached = retrieval_cache.get(cache_key) if cache_ttl_seconds else None
    if cached is not None:
        try:
            return [RetrievalHit(**item) for item in json.loads(cached)]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    selected = _hybrid_retrieve(db, query, top_k)
    if cache_ttl_seconds:
        retrieval_cache.set(
            cache_key,
            json.dumps([item.__dict__ for item in selected], ensure_ascii=False),
            cache_ttl_seconds,
        )
    return selected
