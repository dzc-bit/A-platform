from __future__ import annotations

import json

import pytest

from app.database import SessionLocal
from app.models import KnowledgeChunk, KnowledgeDocument
from app.services import knowledge


@pytest.mark.skipif(not knowledge.LANGCHAIN_RAG_AVAILABLE, reason="LangChain RAG extras are not installed")
def test_retrieval_uses_persisted_langchain_embeddings_and_faiss(client) -> None:
    pytest.importorskip("faiss")
    document = KnowledgeDocument(
        title="FAISS 验收资料",
        source="测试",
        content="向量检索验收要求：合同审批材料齐全后两个工作日内完成初审，并需要保留可追溯引用。",
    )
    with SessionLocal() as db:
        db.add(document)
        db.flush()
        knowledge.index_document(db, document)
        db.commit()
        document_id = document.id
        chunk = db.query(KnowledgeChunk).filter(KnowledgeChunk.document_id == document_id).one()
        payload = json.loads(chunk.vector_json)

        assert payload["version"] == knowledge.EMBEDDING_VERSION
        assert len(payload["embedding"]) == knowledge.EMBEDDING_DIMENSIONS
        assert f":{knowledge.EMBEDDING_VERSION}:" in knowledge._cache_key(
            db, "合同审批材料齐全后多久完成初审", 2
        )

        hits = knowledge.retrieve(db, "合同审批材料齐全后多久完成初审", top_k=2, cache_ttl_seconds=0)

    with SessionLocal() as db:
        vector_index = knowledge._faiss_index(db)

    assert hits
    assert any(hit.document_id == document_id and hit.title == "FAISS 验收资料" for hit in hits)
    assert vector_index is not None


def test_hybrid_retrieval_fuses_vector_and_keyword_rankings(monkeypatch) -> None:
    vector_hits = [
        knowledge.RetrievalHit(1, "向量命中", "semantic evidence", 0.92),
        knowledge.RetrievalHit(2, "共同命中", "shared vector evidence", 0.81),
    ]
    keyword_hits = [
        knowledge.RetrievalHit(2, "共同命中", "shared keyword evidence", 0.98),
        knowledge.RetrievalHit(3, "关键词独有", "exact identifier ABC-2026", 0.88),
    ]
    monkeypatch.setattr(knowledge, "_faiss_retrieve", lambda *_: vector_hits)
    monkeypatch.setattr(knowledge, "_sparse_fallback", lambda *_: keyword_hits)

    hits = knowledge._hybrid_retrieve(object(), "ABC-2026", top_k=3)

    assert [hit.document_id for hit in hits] == [2, 1, 3]
    assert {hit.document_id for hit in hits} == {1, 2, 3}
    assert hits[0].score > hits[1].score > 0
