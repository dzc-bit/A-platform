from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol
from uuid import uuid4

from ..config import settings

logger = logging.getLogger(__name__)

# Redis Pub/Sub channel payload envelope: {"source": <broker instance id>,
# "payload": <event dict>}.  The source marker lets a broker skip the echo of
# its own publish (Redis delivers a published message back to the publisher's
# subscription too) so every event lands in each process exactly once.
_ENVELOPE_SOURCE_KEY = "source"
_ENVELOPE_PAYLOAD_KEY = "payload"


class EventBroker(Protocol):
    """Structural interface shared by the in-memory and Redis brokers."""

    def has_subscribers(self) -> bool: ...

    async def publish(self, event: dict[str, Any]) -> None: ...

    def subscribe(
        self, predicate: Callable[[dict[str, Any]], bool] | None = None
    ) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]: ...


def _fanout_to_queues(
    payload: dict[str, Any],
    subscribers: set[asyncio.Queue[dict[str, Any]]],
    predicates: dict[asyncio.Queue[dict[str, Any]], Callable[[dict[str, Any]], bool] | None],
) -> None:
    """Deliver ``payload`` to matching local queues; predicates fail closed.

    Shared by both brokers so the authorization and back-pressure semantics can
    never drift between the in-memory and Redis implementations.
    """
    for queue in tuple(subscribers):
        predicate = predicates.get(queue)
        if predicate is not None:
            try:
                if not predicate(payload):
                    continue
            except Exception:
                # A failed authorization predicate must fail closed for
                # that subscriber, never broadcast the event accidentally.
                continue
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(payload)


class TicketEventBroker:
    """Single-process fan-out for support updates; Redis Pub/Sub is the scale-out seam.

    Threading model: every mutation below happens on the asyncio event loop
    thread with no ``await`` between state changes, so subscribe/unsubscribe/
    publish cannot interleave mid-mutation.  ``publish`` iterates a snapshot
    copy of the subscriber set, so a concurrent (un)subscribe never mutates the
    collection being iterated.  The ``asyncio.Queue`` objects themselves are
    therefore never touched from a foreign thread; cross-process fan-out is
    delegated to Redis instead of sharing these queues.
    """

    def __init__(self, queue_size: int = 64) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        # Keep predicates alongside the existing queue set so the original
        # teaching-test introspection remains compatible.  Filtering before a
        # queue receives an event prevents cross-user data leakage.
        self._predicates: dict[asyncio.Queue[dict[str, Any]], Callable[[dict[str, Any]], bool] | None] = {}
        self._sequence = 0

    @asynccontextmanager
    async def subscribe(
        self,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        # Register the authorization predicate BEFORE the queue becomes visible
        # to publish(): a subscriber present in ``_subscribers`` without its
        # predicate would fail open and receive events meant to be filtered.
        self._predicates[queue] = predicate
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
            self._predicates.pop(queue, None)

    async def publish(self, event: dict[str, Any]) -> None:
        # ``tuple(...)`` takes an atomic snapshot, so subscribers added or
        # removed while this loop runs are simply not part of this broadcast.
        self._sequence += 1
        payload = {"sequence": self._sequence, **event}
        _fanout_to_queues(payload, self._subscribers, self._predicates)

    def has_subscribers(self) -> bool:
        """Whether any live SSE consumer is currently attached."""
        return bool(self._subscribers)


class RedisEventBroker:
    """Redis Pub/Sub broker with the exact :class:`TicketEventBroker` interface.

    Connections are lazy: nothing touches Redis at import or construction
    time; the first ``subscribe`` opens the listener pump and the first
    ``publish`` opens the publishing client.  When Redis is unreachable the
    broker degrades to in-process fan-out (local queues still receive the
    event) instead of failing the request that triggered the event.

    Delivery model per process: ``publish`` increments the process-local
    monotonic ``sequence``, fans out to the local queues directly, and also
    PUBLISHes the envelope so other processes can fan out.  The pump skips the
    publisher's own echo via the ``source`` marker, so each consumer sees the
    event exactly once.  Foreign events keep their producing process's
    sequence number; only locally published events are renumbered here.
    """

    def __init__(
        self,
        redis_url: str,
        *,
        queue_size: int = 64,
        channel: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._redis_url = redis_url
        self._queue_size = queue_size
        self._channel = channel or "business-ai:events"
        # ``client`` exists for tests (fakeredis injection); production code
        # always goes through the lazily created real client.
        self._injected_client = client
        self._client: Any | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._predicates: dict[asyncio.Queue[dict[str, Any]], Callable[[dict[str, Any]], bool] | None] = {}
        self._sequence = 0
        self._source = uuid4().hex
        self._pump_task: asyncio.Task[None] | None = None
        # Serializes pump creation: without it, two coroutines passing the
        # "no pump yet" check while the first is still awaiting its Pub/Sub
        # subscribe would both spawn a pump, double-deliver every foreign
        # event, and leak the overwritten connection.
        self._pump_lock = asyncio.Lock()

    # -- connection management -------------------------------------------------

    def _get_client(self) -> Any:
        if self._injected_client is not None:
            return self._injected_client
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self._redis_url)
        return self._client

    async def _ensure_pump(self) -> None:
        async with self._pump_lock:
            if self._pump_task is not None and not self._pump_task.done():
                return
            pubsub: Any | None = None
            try:
                pubsub = self._get_client().pubsub()
                await pubsub.subscribe(self._channel)
            except Exception:
                logger.warning("Redis 事件订阅失败，退化为进程内广播", exc_info=True)
                # Never leak the pubsub when the connection attempt failed.
                if pubsub is not None:
                    await self._aclose(pubsub)
                return
            self._pump_task = asyncio.create_task(self._pump(pubsub))

    async def _pump(self, pubsub: Any) -> None:
        """Relay foreign Redis messages into local queues until cancelled."""
        try:
            async for message in pubsub.listen():
                # Subscription confirmations ({"type": "subscribe", ...}) and
                # pings are not events; only real messages carry envelopes.
                if not isinstance(message, dict) or message.get("type") != "message":
                    continue
                raw = message.get("data")
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                if not isinstance(raw, str):
                    continue
                try:
                    envelope = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(envelope, dict) or envelope.get(_ENVELOPE_SOURCE_KEY) == self._source:
                    continue
                payload = envelope.get(_ENVELOPE_PAYLOAD_KEY)
                if isinstance(payload, dict):
                    _fanout_to_queues(payload, self._subscribers, self._predicates)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Redis 事件泵已退出，退化为进程内广播", exc_info=True)
        finally:
            await self._aclose(pubsub)

    @staticmethod
    async def _aclose(pubsub: Any) -> None:
        closer = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
        if closer is None:
            return
        try:
            result = closer()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass

    # -- TicketEventBroker-compatible interface --------------------------------

    @asynccontextmanager
    async def subscribe(
        self,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        # Same ordering invariant as the in-memory broker: predicate first,
        # then visibility, so the queue can never receive a filtered event.
        self._predicates[queue] = predicate
        self._subscribers.add(queue)
        try:
            await self._ensure_pump()
            yield queue
        finally:
            self._subscribers.discard(queue)
            self._predicates.pop(queue, None)
            if not self._subscribers and self._pump_task is not None and not self._pump_task.done():
                self._pump_task.cancel()
                self._pump_task = None

    async def publish(self, event: dict[str, Any]) -> None:
        self._sequence += 1
        payload = {"sequence": self._sequence, **event}
        _fanout_to_queues(payload, self._subscribers, self._predicates)
        envelope = json.dumps(
            {_ENVELOPE_SOURCE_KEY: self._source, _ENVELOPE_PAYLOAD_KEY: payload},
            ensure_ascii=False,
            default=str,
        )
        try:
            await self._get_client().publish(self._channel, envelope)
        except Exception:
            logger.warning("Redis 事件发布失败，本进程订阅者仍可收到该事件", exc_info=True)

    def has_subscribers(self) -> bool:
        return bool(self._subscribers)


def create_event_broker() -> EventBroker:
    """Pick the broker implementation from configuration.

    ``REDIS_URL`` unset or empty keeps the deterministic in-memory broker;
    a configured URL switches to the Redis-backed implementation with an
    identical interface, so callers (routers and tests) need no changes.
    """
    if settings.redis_url:
        return RedisEventBroker(settings.redis_url, channel=f"{settings.redis_key_prefix}:events")
    return TicketEventBroker()


ticket_event_broker: EventBroker = create_event_broker()
