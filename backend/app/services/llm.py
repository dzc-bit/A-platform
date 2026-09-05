from __future__ import annotations

import asyncio
import json
import re
import weakref
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

import httpx

from ..config import settings


@dataclass(frozen=True)
class Completion:
    text: str | None
    used_fallback: bool
    reason: str | None = None
    tool_calls: tuple["LLMToolCall", ...] = ()
    tool_call_parse_failed: bool = False


@dataclass(frozen=True)
class LLMStreamTextDelta:
    text: str


@dataclass(frozen=True)
class LLMStreamToolCallDelta:
    index: int


@dataclass(frozen=True)
class LLMStreamCompleted:
    completion: Completion


@dataclass(frozen=True)
class LLMHistoryMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class LLMToolDefinition:
    """A minimal OpenAI-compatible function declaration.

    The orchestrator exposes only typed, read-only tools. Keeping the declaration
    explicit makes provider payloads and fake-client tests stable.
    """

    name: str
    description: str
    parameters: Mapping[str, object]

    def as_openai_tool(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


@dataclass(frozen=True)
class LLMToolCall:
    id: str
    name: str
    arguments: Mapping[str, object]

    def as_openai_tool_call(self) -> dict[str, object]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(dict(self.arguments))},
        }


@dataclass(frozen=True)
class LLMToolResult:
    call: LLMToolCall
    content: str


_TOOL_CALL_ID_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_TOOL_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
_MAX_TOOL_ARGUMENTS_LENGTH = 4_096
_MAX_STREAM_TOOL_CALLS = 16

# Shared provider connection pool ---------------------------------------------
# One httpx.AsyncClient per asyncio event loop, created lazily and reused for
# every provider call on that loop so TCP/TLS handshakes are amortized and the
# number of concurrent sockets stays bounded. Weak keys let a cached entry be
# dropped once its loop is garbage collected (tests create many short-lived
# loops via asyncio.run).
_SHARED_CLIENT_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)
_shared_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
    weakref.WeakKeyDictionary()
)


def _get_shared_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient for the running loop, creating it lazily.

    Creation is fully synchronous (no await between lookup and store), so two
    coroutines on the same loop can never race into building two clients. The
    ``is_closed`` re-check keeps the pool usable after an external shutdown.
    """
    loop = asyncio.get_running_loop()
    client = _shared_clients.get(loop)
    if client is None or getattr(client, "is_closed", False):
        client = httpx.AsyncClient(limits=_SHARED_CLIENT_LIMITS)
        _shared_clients[loop] = client
    return client


async def aclose_shared_clients() -> None:
    """Close every pooled AsyncClient and empty the pool; safe to repeat.

    Intended for application shutdown (FastAPI lifespan) and tests. Closing a
    stale client must never break the caller, so failures are swallowed.
    """
    for loop, client in list(_shared_clients.items()):
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 - shutdown must never raise from a stale client
            pass
        _shared_clients.pop(loop, None)


# Bounded retry policy for transient provider failures (timeouts, transport
# errors, HTTP 429/5xx). Permanent failures (other 4xx, malformed payloads)
# surface immediately through the existing fallback path. Streaming calls only
# retry before the first provider byte; afterwards recovery is owned by the
# downstream reset mechanism instead of a replay here.
_LLM_MAX_RETRIES = 2
_LLM_RETRY_BACKOFFS = (0.5, 1.0)


def _is_transient_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _retry_backoff(retries: int) -> float:
    """Delay before the next attempt; ``retries`` is the 1-based retry number."""
    return _LLM_RETRY_BACKOFFS[min(retries, len(_LLM_RETRY_BACKOFFS)) - 1]


async def _post_with_retries(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout: float,
) -> tuple[httpx.Response, int]:
    """POST with bounded retries for transient failures (timeouts, 429/5xx).

    Returns the response and the number of retries consumed so callers can
    report them. Exhausted transient failures re-raise the original error
    (status failures via ``raise_for_status``) with a ``retries`` attribute so
    the caller can still report the count; other 4xx raise immediately.
    """
    retries = 0
    while True:
        try:
            response = await client.post(endpoint, headers=headers, json=payload, timeout=timeout)
        except (httpx.TimeoutException, httpx.TransportError) as error:
            if retries >= _LLM_MAX_RETRIES:
                error.retries = retries  # type: ignore[attr-defined]
                raise
        else:
            if not _is_transient_status(response.status_code):
                return response, retries
            if retries >= _LLM_MAX_RETRIES:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as error:
                    error.retries = retries  # type: ignore[attr-defined]
                    raise
        retries += 1
        await asyncio.sleep(_retry_backoff(retries))


@dataclass
class _StreamToolCallAccumulator:
    id_parts: list[str] = field(default_factory=list)
    type_name: str | None = None
    name_parts: list[str] = field(default_factory=list)
    argument_parts: list[str] = field(default_factory=list)
    arguments_length: int = 0
    invalid: bool = False

    def append(self, raw_call: Mapping[str, object]) -> None:
        call_id = raw_call.get("id")
        if call_id is not None:
            if isinstance(call_id, str):
                self.id_parts.append(call_id)
            else:
                self.invalid = True

        call_type = raw_call.get("type")
        if call_type is not None:
            if isinstance(call_type, str) and (self.type_name is None or self.type_name == call_type):
                self.type_name = call_type
            else:
                self.invalid = True

        function = raw_call.get("function")
        if function is None:
            return
        if not isinstance(function, Mapping):
            self.invalid = True
            return

        name = function.get("name")
        if name is not None:
            if isinstance(name, str):
                self.name_parts.append(name)
            else:
                self.invalid = True

        arguments = function.get("arguments")
        if arguments is not None:
            if not isinstance(arguments, str):
                self.invalid = True
                return
            self.arguments_length += len(arguments)
            if self.arguments_length > _MAX_TOOL_ARGUMENTS_LENGTH:
                self.invalid = True
                return
            self.argument_parts.append(arguments)

    def as_provider_call(self) -> dict[str, object] | None:
        if self.invalid:
            return None
        return {
            "id": "".join(self.id_parts),
            "type": self.type_name or "function",
            "function": {
                "name": "".join(self.name_parts),
                "arguments": "".join(self.argument_parts) or "{}",
            },
        }


def parse_openai_tool_calls(raw_tool_calls: object) -> tuple[tuple[LLMToolCall, ...], bool]:
    """Parse provider tool calls without allowing malformed payloads to escape.

    A malformed item is ignored and reported to the caller. The orchestrator then uses
    its deterministic local route instead of executing data supplied by the provider.
    """
    if raw_tool_calls is None:
        return (), False
    if not isinstance(raw_tool_calls, list):
        return (), True

    parsed_calls: list[LLMToolCall] = []
    parse_failed = False
    for raw_call in raw_tool_calls:
        if not isinstance(raw_call, Mapping):
            parse_failed = True
            continue
        call_id = raw_call.get("id")
        function = raw_call.get("function")
        call_type = raw_call.get("type")
        if (
            call_type not in {None, "function"}
            or not isinstance(call_id, str)
            or not _TOOL_CALL_ID_PATTERN.fullmatch(call_id)
            or not isinstance(function, Mapping)
        ):
            parse_failed = True
            continue

        name = function.get("name")
        raw_arguments = function.get("arguments", "{}")
        if not isinstance(name, str) or not _TOOL_NAME_PATTERN.fullmatch(name):
            parse_failed = True
            continue
        try:
            if isinstance(raw_arguments, str):
                if len(raw_arguments) > _MAX_TOOL_ARGUMENTS_LENGTH:
                    raise ValueError("tool arguments exceed the supported size")
                arguments = json.loads(raw_arguments)
            elif isinstance(raw_arguments, Mapping):
                arguments = dict(raw_arguments)
                if len(json.dumps(arguments)) > _MAX_TOOL_ARGUMENTS_LENGTH:
                    raise ValueError("tool arguments exceed the supported size")
            else:
                raise ValueError("tool arguments must be an object")
        except (TypeError, ValueError, json.JSONDecodeError):
            parse_failed = True
            continue
        if not isinstance(arguments, dict) or not all(isinstance(key, str) for key in arguments):
            parse_failed = True
            continue
        parsed_calls.append(LLMToolCall(id=call_id, name=name, arguments=arguments))
    return tuple(parsed_calls), parse_failed


class OpenAICompatibleClient:
    """Best-effort provider adapter; the platform remains usable without credentials."""

    supports_tool_calls = True

    @staticmethod
    def _messages(
        system_prompt: str,
        user_prompt: str,
        history: Sequence[LLMHistoryMessage],
        tool_results: Sequence[LLMToolResult],
    ) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = [
            {"role": "system", "content": system_prompt},
            *[
                {"role": message.role, "content": message.content}
                for message in history
                if message.content.strip()
            ],
            {"role": "user", "content": user_prompt},
        ]
        if tool_results:
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [result.call.as_openai_tool_call() for result in tool_results],
                }
            )
            messages.extend(
                {
                    "role": "tool",
                    "tool_call_id": result.call.id,
                    "content": result.content,
                }
                for result in tool_results
            )
        return messages

    @classmethod
    def _request_payload(
        cls,
        system_prompt: str,
        user_prompt: str,
        *,
        history: Sequence[LLMHistoryMessage],
        tools: Sequence[LLMToolDefinition],
        tool_results: Sequence[LLMToolResult],
        model: str | None,
        stream: bool,
        temperature: float | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": model or settings.llm_model,
            "temperature": temperature if temperature is not None else 0.2,
            "messages": cls._messages(system_prompt, user_prompt, history, tool_results),
        }
        if stream:
            payload["stream"] = True
        if tools:
            payload["tools"] = [tool.as_openai_tool() for tool in tools]
            payload["tool_choice"] = "auto"
        return payload

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        history: Sequence[LLMHistoryMessage] = (),
        tools: Sequence[LLMToolDefinition] = (),
        tool_results: Sequence[LLMToolResult] = (),
        model: str | None = None,
        timeout: float | None = None,
        temperature: float | None = None,
    ) -> Completion:
        if not settings.llm_api_key:
            return Completion(text=None, used_fallback=True, reason="未配置 LLM_API_KEY")
        endpoint = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        retries = 0
        try:
            payload = self._request_payload(
                system_prompt,
                user_prompt,
                history=history,
                tools=tools,
                tool_results=tool_results,
                model=model,
                stream=False,
                temperature=temperature,
            )
            client = _get_shared_client()
            response, retries = await _post_with_retries(
                client,
                endpoint,
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                payload=payload,
                timeout=timeout or 20,
            )
            response.raise_for_status()
            response_payload = response.json()
            if not isinstance(response_payload, Mapping):
                raise ValueError("provider response must be an object")
            choices = response_payload.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
                raise ValueError("provider response did not include a choice")
            message = choices[0].get("message")
            if not isinstance(message, Mapping):
                raise ValueError("provider response did not include a message")
            raw_content = message.get("content")
            if raw_content is None:
                content = None
            elif isinstance(raw_content, str):
                content = raw_content.strip() or None
            else:
                raise ValueError("provider content must be text or null")
            tool_calls, parse_failed = parse_openai_tool_calls(message.get("tool_calls"))
            return Completion(
                text=content,
                used_fallback=False,
                tool_calls=tool_calls,
                tool_call_parse_failed=parse_failed,
            )
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
            retries = getattr(error, "retries", retries)
            suffix = f"（已重试 {retries} 次）" if retries else ""
            return Completion(text=None, used_fallback=True, reason=f"模型调用失败：{type(error).__name__}{suffix}")

    async def stream_complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        history: Sequence[LLMHistoryMessage] = (),
        tools: Sequence[LLMToolDefinition] = (),
        tool_results: Sequence[LLMToolResult] = (),
        model: str | None = None,
    ) -> AsyncIterator[LLMStreamTextDelta | LLMStreamToolCallDelta | LLMStreamCompleted]:
        if not settings.llm_api_key:
            yield LLMStreamCompleted(
                Completion(text=None, used_fallback=True, reason="未配置 LLM_API_KEY")
            )
            return

        endpoint = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        text_parts: list[str] = []
        tool_calls: dict[int, _StreamToolCallAccumulator] = {}
        stream_parse_failed = False

        def parse_data(data: str) -> tuple[list[LLMStreamTextDelta | LLMStreamToolCallDelta], bool]:
            nonlocal stream_parse_failed
            if data == "[DONE]":
                return [], True
            payload = json.loads(data)
            if not isinstance(payload, Mapping):
                raise ValueError("provider stream event must be an object")
            if payload.get("error") is not None:
                raise ValueError("provider stream returned an error event")
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
                raise ValueError("provider stream event did not include a choice")
            delta = choices[0].get("delta")
            if delta is None:
                delta = {}
            if not isinstance(delta, Mapping):
                raise ValueError("provider stream delta must be an object")

            events: list[LLMStreamTextDelta | LLMStreamToolCallDelta] = []
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ValueError("provider stream content must be text or null")
                if content:
                    text_parts.append(content)
                    events.append(LLMStreamTextDelta(content))

            raw_tool_calls = delta.get("tool_calls")
            if raw_tool_calls is not None:
                if not isinstance(raw_tool_calls, list):
                    raise ValueError("provider stream tool calls must be a list")
                for raw_call in raw_tool_calls:
                    if not isinstance(raw_call, Mapping):
                        stream_parse_failed = True
                        continue
                    index = raw_call.get("index")
                    if (
                        not isinstance(index, int)
                        or isinstance(index, bool)
                        or index < 0
                        or index >= _MAX_STREAM_TOOL_CALLS
                    ):
                        stream_parse_failed = True
                        continue
                    accumulator = tool_calls.setdefault(index, _StreamToolCallAccumulator())
                    accumulator.append(raw_call)
                    if accumulator.invalid:
                        stream_parse_failed = True
                    events.append(LLMStreamToolCallDelta(index))
            return events, False

        retries = 0
        try:
            payload = self._request_payload(
                system_prompt,
                user_prompt,
                history=history,
                tools=tools,
                tool_results=tool_results,
                model=model,
                stream=True,
            )
            client = _get_shared_client()
            while True:
                body_received = False
                try:
                    async with client.stream(
                        "POST",
                        endpoint,
                        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                        json=payload,
                        timeout=20,
                    ) as response:
                        response.raise_for_status()
                        data_lines: list[str] = []
                        finished = False
                        async for raw_line in response.aiter_lines():
                            body_received = True
                            line = raw_line.removesuffix("\r")
                            if not line:
                                if not data_lines:
                                    continue
                                events, finished = parse_data("\n".join(data_lines))
                                data_lines.clear()
                                for event in events:
                                    yield event
                                if finished:
                                    break
                                continue
                            if line.startswith(":"):
                                continue
                            if line.startswith("data:"):
                                value = line[5:]
                                data_lines.append(value[1:] if value.startswith(" ") else value)
                        if data_lines and not finished:
                            events, _ = parse_data("\n".join(data_lines))
                            for event in events:
                                yield event
                except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as error:
                    # Only pristine attempts may retry: once provider bytes arrived
                    # the consumer may already hold deltas, so recovery belongs to
                    # the downstream reset mechanism instead of a replay here.
                    if isinstance(error, httpx.HTTPStatusError):
                        transient = _is_transient_status(error.response.status_code)
                    else:
                        transient = True
                    if body_received or not transient or retries >= _LLM_MAX_RETRIES:
                        raise
                    retries += 1
                    await asyncio.sleep(_retry_backoff(retries))
                    continue
                break

            raw_calls: list[dict[str, object]] = []
            for index in sorted(tool_calls):
                raw_call = tool_calls[index].as_provider_call()
                if raw_call is None:
                    stream_parse_failed = True
                else:
                    raw_calls.append(raw_call)
            parsed_calls, final_parse_failed = parse_openai_tool_calls(raw_calls or None)
            yield LLMStreamCompleted(
                Completion(
                    text="".join(text_parts) or None,
                    used_fallback=False,
                    tool_calls=parsed_calls,
                    tool_call_parse_failed=stream_parse_failed or final_parse_failed,
                )
            )
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
            suffix = f"（已重试 {retries} 次）" if retries else ""
            yield LLMStreamCompleted(
                Completion(
                    text=None,
                    used_fallback=True,
                    reason=f"模型流式调用失败：{type(error).__name__}{suffix}",
                )
            )

    async def complete_vision(
        self,
        system_prompt: str,
        user_prompt: str,
        image_data_url: str,
        *,
        model: str | None = None,
    ) -> Completion:
        """Analyze one user-provided image through an OpenAI-compatible vision model."""
        vision_model = (model or settings.llm_vision_model or "").strip()
        if not vision_model:
            return Completion(text=None, used_fallback=True, reason="未配置图片理解模型")
        if not settings.llm_api_key:
            return Completion(text=None, used_fallback=True, reason="未配置 LLM_API_KEY")

        endpoint = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        retries = 0
        try:
            payload: dict[str, object] = {
                "model": vision_model,
                "temperature": 0.1,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": user_prompt},
                            {"type": "image_url", "image_url": {"url": image_data_url}},
                        ],
                    },
                ],
            }
            client = _get_shared_client()
            response, retries = await _post_with_retries(
                client,
                endpoint,
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                payload=payload,
                timeout=30,
            )
            response.raise_for_status()
            response_payload = response.json()
            if not isinstance(response_payload, Mapping):
                raise ValueError("provider response must be an object")
            choices = response_payload.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
                raise ValueError("provider response did not include a choice")
            message = choices[0].get("message")
            if not isinstance(message, Mapping):
                raise ValueError("provider response did not include a message")
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("provider response did not include image analysis text")
            return Completion(text=content.strip(), used_fallback=False)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
            retries = getattr(error, "retries", retries)
            suffix = f"（已重试 {retries} 次）" if retries else ""
            return Completion(text=None, used_fallback=True, reason=f"图片模型调用失败：{type(error).__name__}{suffix}")
