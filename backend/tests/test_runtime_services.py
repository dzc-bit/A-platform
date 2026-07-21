from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base
from app.models import AISetting, Conversation, Message, User
from app.services import cache as cache_module
from app.services.agent import BusinessAgentOrchestrator
from app.services.cache import RetrievalCache, retrieval_cache
from app.services.events import TicketEventBroker
from app.services.llm import Completion, LLMHistoryMessage
from app.services.runtime_settings import get_runtime_settings, validate_setting
from app.services.seed import seed_demo_data


class CapturingLLM:
    def __init__(self) -> None:
        self.system_prompt = ""
        self.user_prompt = ""
        self.history: list[LLMHistoryMessage] = []

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        history: tuple[LLMHistoryMessage, ...] | list[LLMHistoryMessage] = (),
    ) -> Completion:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        self.history = list(history)
        return Completion(text="这是由测试模型生成的完整商务答复，包含可执行的下一步处理建议。", used_fallback=False)


def _set_setting(db: Session, key: str, value: str) -> None:
    setting = db.scalar(select(AISetting).where(AISetting.key == key))
    assert setting is not None
    setting.value = value


@pytest.fixture()
def seeded_db() -> Iterator[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        seed_demo_data(session)
        yield session
    finally:
        session.close()
        engine.dispose()


def test_runtime_setting_validation_and_invalid_value_fallback(seeded_db: Session) -> None:
    assert validate_setting("retrieval_top_k", " 4 ") == "4"
    assert validate_setting("knowledge_chunk_size", "100") == "100"
    assert validate_setting("knowledge_chunk_overlap", "99") == "99"
    assert validate_setting("conversation_memory_messages", "0") == "0"
    assert validate_setting("llm_model", "qwen3.7-plus") == "qwen3.7-plus"
    assert validate_setting("vision_model", "") == ""
    assert validate_setting("vision_model", "qwen3-vl-plus") == "qwen3-vl-plus"
    assert validate_setting("default_language", "zh-CN") == "zh-CN"
    assert validate_setting("reply_strategy", "detailed") == "detailed"
    with pytest.raises(ValueError):
        validate_setting("retrieval_top_k", "9")
    with pytest.raises(ValueError):
        validate_setting("knowledge_chunk_size", "99")
    with pytest.raises(ValueError):
        validate_setting("knowledge_chunk_overlap", "100")
    with pytest.raises(ValueError):
        validate_setting("llm_model", "")
    with pytest.raises(ValueError):
        validate_setting("default_language", "fr-FR")
    with pytest.raises(ValueError):
        validate_setting("reply_strategy", "unbounded")
    with pytest.raises(ValueError):
        validate_setting("unknown_setting", "value")

    _set_setting(seeded_db, "retrieval_top_k", "not-a-number")
    seeded_db.commit()
    assert get_runtime_settings(seeded_db).retrieval_top_k == 3


def test_chat_uses_configured_prompt_top_k_and_bounded_conversation_history(seeded_db: Session) -> None:
    question = "合同审批和开票分别需要准备哪些材料？"
    configured_prompt = "你是受控企业助手。请以清晰、审慎的中文依据知识和工具结果回答，不确定时明确建议转人工。"
    user_id = seeded_db.scalar(select(User.id).where(User.email == "enterprise@neusoft.local"))
    assert user_id is not None
    _set_setting(seeded_db, "assistant_prompt", configured_prompt)
    _set_setting(seeded_db, "retrieval_top_k", "2")
    _set_setting(seeded_db, "conversation_memory_messages", "2")
    _set_setting(seeded_db, "retrieval_cache_ttl_seconds", "30")

    conversation = Conversation(user_id=user_id, title="记忆边界测试")
    seeded_db.add(conversation)
    seeded_db.flush()
    seeded_db.add_all(
        [
            Message(conversation_id=conversation.id, role="user", content="最早的用户问题"),
            Message(conversation_id=conversation.id, role="assistant", content="最早的助手回复"),
            Message(conversation_id=conversation.id, role="user", content="需要保留的用户问题"),
            Message(conversation_id=conversation.id, role="assistant", content="需要保留的助手回复"),
            Message(conversation_id=conversation.id, role="user", content=question),
        ]
    )
    seeded_db.commit()

    retrieval_cache.clear()
    llm = CapturingLLM()
    result = asyncio.run(
        BusinessAgentOrchestrator(llm_client=llm).run(
            seeded_db,
            question,
            conversation_id=conversation.id,
        )
    )

    assert llm.system_prompt == configured_prompt
    assert [(message.role, message.content) for message in llm.history[1:]] == [
        ("user", "需要保留的用户问题"),
        ("assistant", "需要保留的助手回复"),
    ]
    assert llm.history[0].role == "system"
    assert "较早会话摘要" in llm.history[0].content
    assert "最早的用户问题" in llm.history[0].content
    assert question not in llm.history[0].content
    assert result.citations
    assert len(result.citations) <= 2
    assert any("top_k=2" in item.detail for item in result.trace)


def test_chat_includes_user_preference_instruction_in_model_prompt(seeded_db: Session) -> None:
    llm = CapturingLLM()
    asyncio.run(
        BusinessAgentOrchestrator(llm_client=llm).run(
            seeded_db,
            "客服首次响应时限是多久？",
            preference_instruction="使用英文详细回答，并保留依据与下一步。",
        )
    )

    assert "用户偏好" in llm.system_prompt
    assert "使用英文详细回答" in llm.system_prompt


def test_chat_reads_admin_language_and_reply_strategy(seeded_db: Session) -> None:
    _set_setting(seeded_db, "default_language", "en-US")
    _set_setting(seeded_db, "reply_strategy", "detailed")
    seeded_db.commit()

    llm = CapturingLLM()
    asyncio.run(
        BusinessAgentOrchestrator(llm_client=llm).run(
            seeded_db,
            "客服首次响应时限是多久？",
        )
    )

    assert "系统默认回答语言：使用英文" in llm.system_prompt
    assert "系统默认回复策略：详细回答" in llm.system_prompt


def test_retrieval_cache_uses_bounded_memory_fallback(monkeypatch) -> None:
    monkeypatch.setattr(cache_module, "settings", replace(settings, redis_url=None))
    cache = RetrievalCache(max_entries=2)

    cache.set("first", "1", 60)
    cache.set("second", "2", 60)
    cache.set("third", "3", 60)

    assert cache.get("first") is None
    assert cache.get("second") == "2"
    assert cache.get("third") == "3"
    assert cache.status().mode == "memory"
    assert cache.status().hits == 2
    assert cache.status().misses == 1

    cache.clear()
    assert cache.get("second") is None
    assert cache.status().hits == 0
    assert cache.status().misses == 1


def test_ticket_event_broker_fans_out_and_unsubscribes() -> None:
    broker = TicketEventBroker(queue_size=2)

    async def exercise() -> tuple[dict[str, object], int]:
        async with broker.subscribe() as queue:
            await broker.publish({"action": "updated", "ticket": {"id": 7}})
            event = await queue.get()
        return event, len(broker._subscribers)

    event, remaining_subscribers = asyncio.run(exercise())
    assert event["sequence"] == 1
    assert event["action"] == "updated"
    assert event["ticket"] == {"id": 7}
    assert remaining_subscribers == 0
