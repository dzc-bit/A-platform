"""Cloud embedding client and version-migration behaviour of the RAG pipeline."""

from __future__ import annotations

import json

import httpx
import pytest

from app.database import SessionLocal
from app.models import KnowledgeChunk, KnowledgeDocument
from app.services import embeddings as embeddings_service
from app.services import knowledge


def _cloud_settings(**overrides: object) -> object:
    base: dict[str, object] = {
        "embedding_api_key": "test-embedding-key",
        "embedding_base_url": "https://embeddings.invalid/v1",
        "embedding_model": "test-embedding-model",
    }
    base.update(overrides)
    from types import SimpleNamespace

    return SimpleNamespace(**base)


def _install_mock_provider(
    monkeypatch: pytest.MonkeyPatch, handler: object
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def counting_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)  # type: ignore[arg-type,return-value]

    client = httpx.Client(transport=httpx.MockTransport(counting_handler))
    monkeypatch.setattr(embeddings_service, "_get_http_client", lambda: client)
    return seen


def _provider_response(vectors: list[list[float]]) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"embedding": vector} for vector in vectors]})


# ---------------------------------------------------------------------------
# Cloud client behaviour
# ---------------------------------------------------------------------------


def test_embed_texts_parses_provider_response_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings_service, "settings", _cloud_settings())
    seen = _install_mock_provider(
        monkeypatch,
        lambda _request: _provider_response([[0.1, 0.2], ["0.3", 0.4]]),
    )

    vectors = embeddings_service.embed_texts(["第一段", "second"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert len(seen) == 1
    body = seen[0].read().decode("utf-8")
    assert '"input"' in body and "第一段" in body
    assert seen[0].url.path == "/v1/embeddings"


def test_embed_texts_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings_service, "settings", _cloud_settings())
    _install_mock_provider(monkeypatch, lambda _request: httpx.Response(500))

    with pytest.raises(embeddings_service.EmbeddingAPIError):
        embeddings_service.embed_texts(["问题"])


def test_embed_texts_raises_on_malformed_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings_service, "settings", _cloud_settings())
    _install_mock_provider(
        monkeypatch,
        lambda _request: httpx.Response(200, json={"data": [{"embedding": []}]}),
    )

    with pytest.raises(embeddings_service.EmbeddingAPIError):
        embeddings_service.embed_texts(["问题", "另一段"])


def test_embed_texts_empty_input_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings_service, "settings", _cloud_settings())

    assert embeddings_service.embed_texts([]) == []


# ---------------------------------------------------------------------------
# Version labels and fallback semantics
# ---------------------------------------------------------------------------


def test_embedding_version_tracks_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings_service, "settings", _cloud_settings())
    assert embeddings_service.embedding_version() == "cloud:test-embedding-model"
    assert knowledge.embedding_version() == "cloud:test-embedding-model"

    monkeypatch.setattr(
        embeddings_service,
        "settings",
        _cloud_settings(embedding_api_key=None),
    )
    assert embeddings_service.embedding_version() == "hash-faiss-v1"


def test_fallback_returns_hash_vectors_without_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        embeddings_service,
        "settings",
        _cloud_settings(embedding_api_key=None, embedding_model=None),
    )

    vectors, version = embeddings_service.embed_texts_with_fallback(["合同审批"])

    assert version == "hash-faiss-v1"
    assert len(vectors) == 1
    assert len(vectors[0]) == embeddings_service.EMBEDDING_DIMENSIONS


def test_fallback_returns_hash_vectors_when_cloud_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings_service, "settings", _cloud_settings())
    _install_mock_provider(monkeypatch, lambda _request: httpx.Response(503))

    vectors, version = embeddings_service.embed_texts_with_fallback(["合同审批"])

    assert version == "hash-faiss-v1"
    assert len(vectors[0]) == embeddings_service.EMBEDDING_DIMENSIONS


# ---------------------------------------------------------------------------
# Storage payload migration through ensure_documents_indexed
# ---------------------------------------------------------------------------


def test_version_switch_marks_chunks_stale_and_reindex_migrates_them(
    monkeypatch: pytest.MonkeyPatch, client
) -> None:
    monkeypatch.setattr(embeddings_service, "settings", _cloud_settings())
    _install_mock_provider(monkeypatch, lambda _request: _provider_response([[0.5, 0.5]]))
    document = KnowledgeDocument(
        title="云端向量迁移资料",
        source="测试",
        content="云端向量迁移验收：切换配置后旧向量应被重新索引为当前版本。",
    )
    with SessionLocal() as db:
        db.add(document)
        db.flush()
        knowledge.index_document(db, document)
        db.commit()
        document_id = document.id
        chunk = db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == document_id).one()
        assert chunk.vector_json and '"version":"cloud:test-embedding-model"' in chunk.vector_json
        assert knowledge._is_current_vector_payload(chunk.vector_json) is True

    # The deployment drops the cloud provider: stored cloud payloads turn stale.
    monkeypatch.setattr(embeddings_service, "settings", _cloud_settings(embedding_api_key=None))
    with SessionLocal() as db:
        chunk = db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == document_id).one()
        assert knowledge._is_current_vector_payload(chunk.vector_json) is False

        indexed = knowledge.ensure_documents_indexed(db)

        assert indexed == 1
        # index_document deletes and recreates the chunk row, so re-query it.
        migrated = (
            db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == document_id).one()
        )
        assert migrated.vector_json and '"version":"hash-faiss-v1"' in migrated.vector_json
        payload = json.loads(migrated.vector_json)
        assert len(payload["embedding"]) == embeddings_service.EMBEDDING_DIMENSIONS


def test_faiss_index_skips_foreign_version_chunks(monkeypatch: pytest.MonkeyPatch, client) -> None:
    if not knowledge.LANGCHAIN_RAG_AVAILABLE:
        pytest.skip("LangChain RAG extras are not installed")
    monkeypatch.setattr(embeddings_service, "settings", _cloud_settings(embedding_api_key=None))
    document = KnowledgeDocument(
        title="混合空间防护资料",
        source="测试",
        content="混合空间防护：只允许当前版本的向量进入索引。",
    )
    with SessionLocal() as db:
        db.add(document)
        db.flush()
        knowledge.index_document(db, document)
        db.commit()
        document_id = document.id

    # Simulate a stale chunk written by another embedding implementation.
    with SessionLocal() as db:
        chunk = db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == document_id).one()
        chunk.vector_json = chunk.vector_json.replace("hash-faiss-v1", "legacy-vector-v0")
        db.commit()

        index = knowledge._faiss_index(db)
        # With LangChain available and the seeded corpus present, an index must
        # exist; only the legacy chunk is excluded from it.
        assert index is not None
        indexed_ids = {doc.metadata["document_id"] for doc in index.docstore._dict.values()}
        assert document_id not in indexed_ids
