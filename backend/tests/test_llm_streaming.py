from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import llm as llm_service
from app.services.llm import (
    LLMStreamCompleted,
    LLMStreamTextDelta,
    LLMStreamToolCallDelta,
    OpenAICompatibleClient,
)


def _choice(delta: dict[str, object], finish_reason: str | None = None) -> str:
    return "data: " + json.dumps(
        {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]},
        ensure_ascii=False,
    )


class FakeStreamResponse:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.closed = False

    async def __aenter__(self) -> "FakeStreamResponse":
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closed = True

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self.lines:
            yield line


def _install_provider(
    monkeypatch: pytest.MonkeyPatch,
    response: FakeStreamResponse,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def stream(self, method: str, url: str, **kwargs: object) -> FakeStreamResponse:
            captured.update({"method": method, "url": url, **kwargs})
            return response

    monkeypatch.setattr(llm_service.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(
        llm_service,
        "settings",
        SimpleNamespace(
            llm_api_key="test-only-key",
            llm_base_url="https://provider.invalid/v1",
            llm_model="test-model",
        ),
    )
    return captured


def test_stream_complete_sets_stream_true_and_forwards_exact_content_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeStreamResponse(
        [
            ": keep-alive",
            _choice({"content": "首个"}),
            "",
            _choice({"content": " token"}),
            "",
            _choice({}, "stop"),
            "",
            "data: [DONE]",
            "",
        ]
    )
    captured = _install_provider(monkeypatch, response)

    async def collect() -> list[object]:
        return [
            event
            async for event in OpenAICompatibleClient().stream_complete(
                "system",
                "question",
            )
        ]

    events = asyncio.run(collect())
    deltas = [event.text for event in events if isinstance(event, LLMStreamTextDelta)]
    terminal = next(event for event in events if isinstance(event, LLMStreamCompleted))

    assert captured["method"] == "POST"
    assert captured["json"]["stream"] is True
    assert deltas == ["首个", " token"]
    assert terminal.completion.text == "首个 token"
    assert terminal.completion.used_fallback is False
    assert response.closed is True


def test_stream_complete_joins_multiline_data_and_streamed_tool_arguments_by_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_payload = json.dumps(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_order_1",
                                "type": "function",
                                "function": {
                                    "name": "order_query_privacy_notice",
                                    "arguments": '{"order',
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        }
    )
    split_at = first_payload.index('"delta"')
    response = FakeStreamResponse(
        [
            f"data: {first_payload[:split_at]}",
            f"data: {first_payload[split_at:]}",
            "",
            _choice(
                {
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": '_id":"A-42"}'}}
                    ]
                }
            ),
            "",
            _choice({}, "tool_calls"),
            "",
            "data: [DONE]",
            "",
        ]
    )
    _install_provider(monkeypatch, response)

    async def collect() -> list[object]:
        return [event async for event in OpenAICompatibleClient().stream_complete("system", "question")]

    events = asyncio.run(collect())
    markers = [event for event in events if isinstance(event, LLMStreamToolCallDelta)]
    terminal = next(event for event in events if isinstance(event, LLMStreamCompleted))

    assert [event.index for event in markers] == [0, 0]
    assert len(terminal.completion.tool_calls) == 1
    assert terminal.completion.tool_calls[0].id == "call_order_1"
    assert terminal.completion.tool_calls[0].name == "order_query_privacy_notice"
    assert terminal.completion.tool_calls[0].arguments == {"order_id": "A-42"}
    assert terminal.completion.tool_call_parse_failed is False


def test_stream_complete_rejects_tool_arguments_over_incremental_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeStreamResponse(
        [
            _choice(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_order_1",
                            "type": "function",
                            "function": {
                                "name": "order_query_privacy_notice",
                                "arguments": "x" * 4_097,
                            },
                        }
                    ]
                }
            ),
            "",
            _choice({}, "tool_calls"),
            "",
            "data: [DONE]",
            "",
        ]
    )
    _install_provider(monkeypatch, response)

    async def collect() -> list[object]:
        return [event async for event in OpenAICompatibleClient().stream_complete("system", "question")]

    events = asyncio.run(collect())
    terminal = next(event for event in events if isinstance(event, LLMStreamCompleted))
    assert terminal.completion.tool_calls == ()
    assert terminal.completion.tool_call_parse_failed is True


def test_closing_stream_consumer_closes_provider_response(monkeypatch: pytest.MonkeyPatch) -> None:
    release = asyncio.Event()
    provider_closed = asyncio.Event()

    class BlockingResponse(FakeStreamResponse):
        async def aiter_lines(self) -> AsyncIterator[str]:
            try:
                yield _choice({"content": "首"})
                yield ""
                await release.wait()
            finally:
                provider_closed.set()

    response = BlockingResponse([])
    _install_provider(monkeypatch, response)

    async def scenario() -> None:
        stream = OpenAICompatibleClient().stream_complete("system", "question")
        first = await anext(stream)
        assert isinstance(first, LLMStreamTextDelta)
        assert first.text == "首"
        await stream.aclose()
        await asyncio.wait_for(provider_closed.wait(), timeout=1)

    asyncio.run(scenario())
    assert response.closed is True


def test_cancelled_error_from_provider_is_not_converted_to_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CancellingResponse(FakeStreamResponse):
        async def aiter_lines(self) -> AsyncIterator[str]:
            raise asyncio.CancelledError
            yield ""  # pragma: no cover

    _install_provider(monkeypatch, CancellingResponse([]))

    async def consume() -> None:
        async for _ in OpenAICompatibleClient().stream_complete("system", "question"):
            pass

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(consume())
