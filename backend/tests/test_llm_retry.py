"""Retry and shared-connection-pool behaviour of the OpenAI-compatible client.

Every test injects one shared AsyncClient backed by httpx.MockTransport via the
``_get_shared_client`` seam, so provider failure sequences stay programmable.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace

import httpx
import pytest

from app.services import llm as llm_service
from app.services.llm import (
    LLMStreamCompleted,
    LLMStreamTextDelta,
    OpenAICompatibleClient,
)


@pytest.fixture(autouse=True)
def _fast_retries_and_fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove real backoff sleeping and point the client at a fake provider."""
    monkeypatch.setattr(llm_service, "_LLM_RETRY_BACKOFFS", (0.0, 0.0))
    monkeypatch.setattr(
        llm_service,
        "settings",
        SimpleNamespace(
            llm_api_key="test-only-key",
            llm_base_url="https://provider.invalid/v1",
            llm_model="test-model",
            llm_vision_model="test-vision-model",
        ),
    )


def _install_scripted_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Inject one shared AsyncClient with MockTransport; return the seen requests."""
    seen: list[httpx.Request] = []

    def counting_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(counting_handler))
    monkeypatch.setattr(llm_service, "_get_shared_client", lambda: client)
    return seen


def _scripted_handler(outcomes: list[object]) -> Callable[[httpx.Request], httpx.Response]:
    """Handler replaying a fixed sequence of responses and exceptions."""

    def handler(_request: httpx.Request) -> httpx.Response:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, httpx.Response)
        return outcome

    return handler


def _completion_payload(text: str = "provider answer") -> dict[str, object]:
    return {"choices": [{"message": {"content": text, "tool_calls": None}}]}


def _sse(delta: dict[str, object], finish_reason: str | None = None) -> bytes:
    event = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
    return ("data: " + json.dumps(event) + "\n\n").encode("utf-8")


_STREAM_BODY = _sse({"content": "首个"}) + _sse({}, "stop") + b"data: [DONE]\n\n"


async def _stream_then_reset(chunks: list[bytes]) -> AsyncIterator[bytes]:
    """A provider stream that delivers some bytes and then drops the connection."""
    for chunk in chunks:
        yield chunk
    raise httpx.ReadError("connection reset by peer")


def _run_stream(client: OpenAICompatibleClient) -> list[object]:
    async def collect() -> list[object]:
        return [event async for event in client.stream_complete("system", "question")]

    return asyncio.run(collect())


# ---------------------------------------------------------------------------
# Non-streaming complete()
# ---------------------------------------------------------------------------


def test_complete_retries_transient_500_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_scripted_client(
        monkeypatch,
        _scripted_handler([httpx.Response(500), httpx.Response(200, json=_completion_payload())]),
    )

    result = asyncio.run(OpenAICompatibleClient().complete("system", "question"))

    assert result.used_fallback is False
    assert result.text == "provider answer"
    assert len(seen) == 2  # exactly one retry after the transient 500


def test_complete_falls_back_after_retry_exhaustion_and_reports_retry_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_scripted_client(monkeypatch, lambda _request: httpx.Response(500))

    result = asyncio.run(OpenAICompatibleClient().complete("system", "question"))

    assert result.used_fallback is True
    assert result.text is None
    assert result.reason is not None
    assert "HTTPStatusError" in result.reason
    assert "已重试 2 次" in result.reason
    assert len(seen) == 3  # initial attempt + 2 retries


def test_complete_retries_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_scripted_client(
        monkeypatch,
        _scripted_handler([httpx.Response(429), httpx.Response(200, json=_completion_payload())]),
    )

    result = asyncio.run(OpenAICompatibleClient().complete("system", "question"))

    assert result.used_fallback is False
    assert result.text == "provider answer"
    assert len(seen) == 2


def test_complete_retries_transport_error_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_scripted_client(
        monkeypatch,
        _scripted_handler(
            [httpx.ConnectError("boom"), httpx.Response(200, json=_completion_payload())]
        ),
    )

    result = asyncio.run(OpenAICompatibleClient().complete("system", "question"))

    assert result.used_fallback is False
    assert result.text == "provider answer"
    assert len(seen) == 2


def test_complete_does_not_retry_permanent_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_scripted_client(monkeypatch, lambda _request: httpx.Response(400))

    result = asyncio.run(OpenAICompatibleClient().complete("system", "question"))

    assert result.used_fallback is True
    assert result.reason is not None
    assert "HTTPStatusError" in result.reason
    assert "已重试" not in result.reason
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# Vision
# ---------------------------------------------------------------------------


def test_complete_vision_retries_transient_500_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_scripted_client(
        monkeypatch,
        _scripted_handler(
            [httpx.Response(500), httpx.Response(200, json=_completion_payload("图中为红色状态灯"))]
        ),
    )

    result = asyncio.run(
        OpenAICompatibleClient().complete_vision("system", "识别", "data:image/png;base64,AAAA")
    )

    assert result.used_fallback is False
    assert result.text == "图中为红色状态灯"
    assert len(seen) == 2


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def test_stream_complete_retries_connect_error_before_first_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_scripted_client(
        monkeypatch,
        _scripted_handler([httpx.ConnectError("boom"), httpx.Response(200, content=_STREAM_BODY)]),
    )

    events = _run_stream(OpenAICompatibleClient())

    deltas = [event.text for event in events if isinstance(event, LLMStreamTextDelta)]
    terminal = next(event for event in events if isinstance(event, LLMStreamCompleted))
    assert deltas == ["首个"]
    assert terminal.completion.used_fallback is False
    assert terminal.completion.text == "首个"
    assert len(seen) == 2


def test_stream_complete_retries_500_status_before_body(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_scripted_client(
        monkeypatch,
        _scripted_handler([httpx.Response(500), httpx.Response(200, content=_STREAM_BODY)]),
    )

    events = _run_stream(OpenAICompatibleClient())

    deltas = [event.text for event in events if isinstance(event, LLMStreamTextDelta)]
    terminal = next(event for event in events if isinstance(event, LLMStreamCompleted))
    assert deltas == ["首个"]
    assert terminal.completion.used_fallback is False
    assert len(seen) == 2


def test_stream_complete_never_retries_after_bytes_were_received(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_scripted_client(
        monkeypatch,
        lambda _request: httpx.Response(
            200, content=_stream_then_reset([_sse({"content": "部分"})])
        ),
    )

    events = _run_stream(OpenAICompatibleClient())

    deltas = [event.text for event in events if isinstance(event, LLMStreamTextDelta)]
    terminal = next(event for event in events if isinstance(event, LLMStreamCompleted))
    assert deltas == ["部分"]  # the partial delta reached the consumer
    assert terminal.completion.used_fallback is True
    assert terminal.completion.reason is not None
    assert "ReadError" in terminal.completion.reason
    assert len(seen) == 1  # no second attempt once provider bytes arrived


# ---------------------------------------------------------------------------
# Shared client pool
# ---------------------------------------------------------------------------


def test_injected_shared_client_is_reused_across_loops_and_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_scripted_client(
        monkeypatch, lambda _request: httpx.Response(200, json=_completion_payload())
    )
    client = OpenAICompatibleClient()

    first = asyncio.run(client.complete("system", "q1"))
    second = asyncio.run(client.complete("system", "q2"))

    assert first.used_fallback is False
    assert second.used_fallback is False
    assert len(seen) == 2  # the same injected client instance served both loops


def test_aclose_shared_clients_is_idempotent_and_pool_recreates() -> None:
    async def acquire_and_close() -> httpx.AsyncClient:
        client = llm_service._get_shared_client()
        await llm_service.aclose_shared_clients()
        return client

    first = asyncio.run(acquire_and_close())
    assert first.is_closed is True

    # Repeated shutdown calls (even from fresh loops) must stay safe.
    asyncio.run(llm_service.aclose_shared_clients())
    asyncio.run(llm_service.aclose_shared_clients())

    async def acquire() -> httpx.AsyncClient:
        return llm_service._get_shared_client()

    second = asyncio.run(acquire())
    assert second is not first
    assert second.is_closed is False

    asyncio.run(llm_service.aclose_shared_clients())
