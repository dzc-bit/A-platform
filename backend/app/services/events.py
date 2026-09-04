from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any


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
        for queue in tuple(self._subscribers):
            predicate = self._predicates.get(queue)
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

    def has_subscribers(self) -> bool:
        """Whether any live SSE consumer is currently attached."""
        return bool(self._subscribers)


ticket_event_broker = TicketEventBroker()
