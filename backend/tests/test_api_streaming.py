from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Sequence

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import api as api_module
from app.database import Base
from app.models import Message, User
from app.schemas import ChatRequest
from app.services.agent import BusinessAgentOrchestrator
from app.services.llm import (
    Completion,
    LLMHistoryMessage,
    LLMStreamCompleted,
    LLMStreamTextDelta,
    LLMToolDefinition,
    LLMToolResult,
)
from app.services.seed import seed_demo_data


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
    seed_demo_data(session)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _event(chunk: str | bytes) -> tuple[str, dict[str, object]]:
    text = chunk.decode() if isinstance(chunk, bytes) else chunk
    event_name = text.splitlines()[0].removeprefix("event: ")
    raw_data = next(line.removeprefix("data: ") for line in text.splitlines() if line.startswith("data: "))
    return event_name, json.loads(raw_data)


async def _next_named(iterator: AsyncIterator[str | bytes], expected: str) -> dict[str, object]:
    while True:
        name, payload = _event(await anext(iterator))
        if name == expected:
            return payload


class GateStreamingLLM:
    supports_tool_calls = False

    def __init__(self) -> None:
        self.allow_finish = asyncio.Event()
        self.model_finished = asyncio.Event()
        self.provider_closed = asyncio.Event()

    async def stream_complete(
        self,
        _system_prompt: str,
        _user_prompt: str,
        *,
        history: Sequence[LLMHistoryMessage] = (),
        tools: Sequence[LLMToolDefinition] = (),
        tool_results: Sequence[LLMToolResult] = (),
    ) -> AsyncIterator[object]:
        del history, tools, tool_results
        try:
            yield LLMStreamTextDelta("首个模型 Token")
            await self.allow_finish.wait()
            yield LLMStreamTextDelta(" 已到达客户端，后续内容仍在模型服务中继续生成，完整回答现在结束。")
            self.model_finished.set()
            yield LLMStreamCompleted(
                Completion(
                    text="首个模型 Token 已到达客户端，后续内容仍在模型服务中继续生成，完整回答现在结束。",
                    used_fallback=False,
                )
            )
        finally:
            self.provider_closed.set()


class FailingStreamingLLM:
    supports_tool_calls = False

    def __init__(self, *, after_token: bool) -> None:
        self.after_token = after_token

    async def stream_complete(self, *_: object, **__: object) -> AsyncIterator[object]:
        if self.after_token:
            yield LLMStreamTextDelta("不完整的模型内容")
        yield LLMStreamCompleted(
            Completion(text=None, used_fallback=True, reason="模型流式调用失败：ReadError")
        )


def _user(db: Session) -> User:
    user = db.scalar(select(User).where(User.email == "enterprise@neusoft.local"))
    assert user is not None
    return user


def _assistant_count(db: Session) -> int:
    return int(db.scalar(select(func.count(Message.id)).where(Message.role == "assistant")) or 0)


def test_first_model_token_arrives_before_model_completion_and_database_commit(
    seeded_db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GateStreamingLLM()
    monkeypatch.setattr(api_module, "orchestrator", BusinessAgentOrchestrator(llm_client=fake))

    async def scenario() -> None:
        response = await api_module.stream_chat(
            ChatRequest(message="发票申请需要什么材料？"),
            _user(seeded_db),
            seeded_db,
        )
        iterator = response.body_iterator
        first_token = await asyncio.wait_for(_next_named(iterator, "token"), timeout=2)
        assert first_token == {"text": "首个模型 Token", "origin": "model"}
        assert fake.model_finished.is_set() is False
        assert _assistant_count(seeded_db) == 0

        fake.allow_finish.set()
        done = await asyncio.wait_for(_next_named(iterator, "done"), timeout=2)
        assert done["answer"] == "首个模型 Token 已到达客户端，后续内容仍在模型服务中继续生成，完整回答现在结束。"
        assert fake.model_finished.is_set() is True
        assert _assistant_count(seeded_db) == 1
        await iterator.aclose()

    asyncio.run(scenario())


def test_client_close_cancels_provider_and_rolls_back_unfinished_chat(
    seeded_db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GateStreamingLLM()
    monkeypatch.setattr(api_module, "orchestrator", BusinessAgentOrchestrator(llm_client=fake))

    async def scenario() -> None:
        response = await api_module.stream_chat(
            ChatRequest(message="发票申请需要什么材料？"),
            _user(seeded_db),
            seeded_db,
        )
        iterator = response.body_iterator
        await asyncio.wait_for(_next_named(iterator, "token"), timeout=2)
        await iterator.aclose()
        await asyncio.wait_for(fake.provider_closed.wait(), timeout=1)
        assert _assistant_count(seeded_db) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("after_token", [False, True])
def test_stream_failure_uses_single_fallback_before_token_or_reset_after_partial_token(
    seeded_db: Session,
    monkeypatch: pytest.MonkeyPatch,
    after_token: bool,
) -> None:
    monkeypatch.setattr(
        api_module,
        "orchestrator",
        BusinessAgentOrchestrator(llm_client=FailingStreamingLLM(after_token=after_token)),
    )

    async def scenario() -> list[tuple[str, dict[str, object]]]:
        response = await api_module.stream_chat(
            ChatRequest(message="发票申请需要什么材料？"),
            _user(seeded_db),
            seeded_db,
        )
        return [_event(chunk) async for chunk in response.body_iterator]

    events = asyncio.run(scenario())
    tokens = [payload for name, payload in events if name == "token"]
    resets = [payload for name, payload in events if name == "reset"]
    done = next(payload for name, payload in events if name == "done")

    assert done["used_fallback"] is True
    assert "当前知识库" not in str(done["answer"])
    if after_token:
        assert tokens == [{"text": "不完整的模型内容", "origin": "model"}]
        assert resets == [{"text": done["answer"]}]
    else:
        assert len(tokens) == 1
        assert tokens[0] == {"text": done["answer"], "origin": "fallback"}
        assert resets == []

    saved = seeded_db.scalar(select(Message).where(Message.role == "assistant"))
    assert saved is not None
    assert saved.content == done["answer"]
