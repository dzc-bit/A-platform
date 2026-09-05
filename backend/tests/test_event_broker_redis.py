"""RedisEventBroker behaviour with fakeredis as the injected Redis client."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis as fakeredis_aioredis

from app.services import events as events_module
from app.services.events import RedisEventBroker, TicketEventBroker, create_event_broker

_CHANNEL = "business-ai:events"


def _foreign_envelope(source: str, payload: dict[str, Any]) -> str:
    return json.dumps({"source": source, "payload": payload}, ensure_ascii=False)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Interface parity with the in-memory broker
# ---------------------------------------------------------------------------


def test_redis_broker_publish_fans_out_locally_with_monotonic_sequence() -> None:
    async def scenario() -> None:
        broker = RedisEventBroker("redis://unused", client=fakeredis_aioredis.FakeRedis())
        async with broker.subscribe() as queue:
            await broker.publish({"kind": "ticket", "action": "created"})
            await broker.publish({"kind": "ticket", "action": "updated"})
            first = await asyncio.wait_for(queue.get(), 2)
            second = await asyncio.wait_for(queue.get(), 2)
        assert first["sequence"] == 1 and first["action"] == "created"
        assert second["sequence"] == 2 and second["action"] == "updated"

    _run(scenario())


def test_redis_broker_predicate_fail_closed() -> None:
    async def scenario() -> None:
        broker = RedisEventBroker("redis://unused", client=fakeredis_aioredis.FakeRedis())

        def broken_predicate(_event: dict[str, Any]) -> bool:
            raise RuntimeError("authorization check exploded")

        async with broker.subscribe(broken_predicate) as queue:
            await broker.publish({"kind": "ticket"})
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(queue.get(), 0.1)

    _run(scenario())


def test_redis_broker_full_queue_drops_oldest() -> None:
    async def scenario() -> None:
        broker = RedisEventBroker("redis://unused", queue_size=2, client=fakeredis_aioredis.FakeRedis())
        async with broker.subscribe() as queue:
            for index in range(3):
                await broker.publish({"action": index})
            first = await asyncio.wait_for(queue.get(), 0.1)
            second = await asyncio.wait_for(queue.get(), 0.1)
        assert first["action"] == 1  # action 0 was dropped as the oldest
        assert second["action"] == 2

    _run(scenario())


def test_redis_broker_has_subscribers() -> None:
    async def scenario() -> None:
        broker = RedisEventBroker("redis://unused", client=fakeredis_aioredis.FakeRedis())
        assert broker.has_subscribers() is False
        async with broker.subscribe():
            assert broker.has_subscribers() is True
        assert broker.has_subscribers() is False

    _run(scenario())


# ---------------------------------------------------------------------------
# Pump behaviour (the Redis listener task)
# ---------------------------------------------------------------------------


def test_pump_relays_foreign_message_to_local_queue() -> None:
    async def scenario() -> None:
        client = fakeredis_aioredis.FakeRedis()
        broker = RedisEventBroker("redis://unused", client=client)
        async with broker.subscribe() as queue:
            await client.publish(
                _CHANNEL,
                _foreign_envelope("other-process", {"kind": "ticket", "sequence": 7, "action": "updated"}),
            )
            received = await asyncio.wait_for(queue.get(), 2)
        assert received == {"kind": "ticket", "sequence": 7, "action": "updated"}

    _run(scenario())


def test_pump_skips_own_publish_echo_so_event_arrives_once() -> None:
    async def scenario() -> None:
        client = fakeredis_aioredis.FakeRedis()
        broker = RedisEventBroker("redis://unused", client=client)
        async with broker.subscribe() as queue:
            await broker.publish({"kind": "ticket", "action": "created"})
            received = await asyncio.wait_for(queue.get(), 2)
            # Give the pump a chance to relay the publisher's own echo.
            await asyncio.sleep(0.1)
            assert queue.qsize() == 0
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(queue.get(), 0.1)
        assert received["action"] == "created"

    _run(scenario())


def test_pump_skips_subscribe_confirmations_and_malformed_frames() -> None:
    async def scenario() -> None:
        broker = RedisEventBroker("redis://unused")
        valid = {"type": "message", "data": _foreign_envelope("other", {"kind": "ticket", "sequence": 1})}

        class StubPubSub:
            def __init__(self, own_source: str) -> None:
                self.frames: list[dict[str, Any]] = [
                    {"type": "subscribe", "pattern": None, "channel": _CHANNEL, "data": 1},
                    {"type": "pong"},
                    {"type": "message", "data": b"not-json"},
                    {"type": "message", "data": _foreign_envelope(own_source, {"kind": "own"})},
                    valid,
                ]

            def listen(self) -> Any:
                async def generator() -> Any:
                    for frame in self.frames:
                        yield frame

                return generator()

            async def aclose(self) -> None:
                self.closed = True

        stub = StubPubSub(own_source=broker._source)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        broker._subscribers.add(queue)
        pump = asyncio.create_task(broker._pump(stub))
        received = await asyncio.wait_for(queue.get(), 2)
        await asyncio.wait_for(asyncio.shield(pump), 2)
        assert received == {"kind": "ticket", "sequence": 1}
        assert queue.qsize() == 0  # own-source frame was skipped

    _run(scenario())


def test_concurrent_subscribe_creates_exactly_one_pump() -> None:
    """Two subscribers racing the first pump start must not spawn two pumps."""

    async def scenario() -> None:
        class SlowPubSub:
            def __init__(self) -> None:
                self.subscribe_calls = 0

            async def subscribe(self, channel: str) -> None:
                self.subscribe_calls += 1
                await asyncio.sleep(0.05)

            def listen(self) -> Any:
                async def generator() -> Any:
                    # Park the pump so it is still "running" for the second subscriber.
                    await asyncio.sleep(10)
                    yield {}

                return generator()

            async def aclose(self) -> None:
                pass

        class StubClient:
            def __init__(self) -> None:
                self.pubsubs: list[SlowPubSub] = []

            def pubsub(self) -> SlowPubSub:
                pubsub = SlowPubSub()
                self.pubsubs.append(pubsub)
                return pubsub

        client = StubClient()
        broker = RedisEventBroker("redis://unused", client=client)

        async def hold_subscription() -> None:
            async with broker.subscribe():
                await asyncio.sleep(0.2)

        await asyncio.gather(hold_subscription(), hold_subscription())
        assert len(client.pubsubs) == 1
        assert client.pubsubs[0].subscribe_calls == 1

    _run(scenario())


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_create_event_broker_returns_in_memory_broker_without_redis_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(events_module, "settings", SimpleNamespace(redis_url=None, redis_key_prefix="business-ai"))
    assert isinstance(create_event_broker(), TicketEventBroker)


def test_create_event_broker_returns_redis_broker_with_prefixed_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        events_module,
        "settings",
        SimpleNamespace(redis_url="redis://localhost:6379/0", redis_key_prefix="biz"),
    )
    broker = create_event_broker()
    assert isinstance(broker, RedisEventBroker)
    assert broker._channel == "biz:events"


def test_module_level_symbol_keeps_ticket_event_broker_name() -> None:
    # routers/shared.py and the SSE endpoints bind to this exact symbol.
    assert hasattr(events_module, "ticket_event_broker")
