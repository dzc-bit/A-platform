"""Standalone concurrency tests.

Covers three surfaces:

1. SQLite PRAGMA hardening from ``app.database`` (WAL / busy_timeout /
   foreign_keys / synchronous) plus real multi-threaded writes.
2. ``RetrievalCache`` concurrent get/set/eviction on its in-memory fallback.
3. ``TicketEventBroker`` fan-out to concurrently subscribing SSE consumers and
   the unsubscribe boundary.

The module deliberately does NOT use the shared ``tests/test_business_ai.db``
or the conftest ``client`` fixture: environment variables are pinned at import
time (mirroring conftest) and every test builds its own database/engine or
service instances, so this file also runs on its own.
"""

from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["DATABASE_URL"] = f"sqlite:///{(Path(__file__).parent / 'test_business_ai.db').as_posix()}"
os.environ["TOKEN_SECRET"] = "test-only-secret"
os.environ["DEMO_PASSWORD"] = "test-demo-password"
os.environ["LLM_API_KEY"] = ""
os.environ["DIFY_API_URL"] = ""
os.environ["DIFY_API_KEY"] = ""
os.environ["REDIS_URL"] = ""

from app import models  # noqa: E402,F401  (registers every mapper on Base)
from app.database import Base  # noqa: E402
from app.models import SupportTicket  # noqa: E402
from app.services.cache import RetrievalCache  # noqa: E402
from app.services.events import TicketEventBroker  # noqa: E402


def _make_sqlite_engine(db_path: Path):
    """Same engine recipe as ``app/database.py`` for a throwaway file.

    The ``Engine``-class connect listener registered in ``app.database`` also
    fires for this engine, so the PRAGMA assertions below verify that listener
    end to end rather than duplicating it.
    """
    return create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False},
        future=True,
    )


def test_sqlite_pragmas_applied_and_eight_thread_writes(tmp_path) -> None:
    db_path = tmp_path / "concurrency.db"
    engine = _make_sqlite_engine(db_path)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
    errors: list[Exception] = []
    thread_count = 8
    tickets_per_thread = 10

    try:
        Base.metadata.create_all(bind=engine)

        # The listener must have configured the very first pooled connection.
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
            assert connection.execute(text("PRAGMA busy_timeout")).scalar() == 5000
            assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
            # 1 == NORMAL (0=off, 2=full).
            assert connection.execute(text("PRAGMA synchronous")).scalar() == 1

        def insert_tickets(worker: int) -> None:
            try:
                # One session per thread; a commit per row maximizes the
                # write-lock contention the busy_timeout must absorb.
                with factory() as session:
                    for index in range(tickets_per_thread):
                        session.add(
                            SupportTicket(
                                customer_name=f"并发用户-{worker}",
                                question=f"线程 {worker} 的第 {index} 个并发写入问题",
                                category="一般咨询",
                                suggested_reply="待生成",
                            )
                        )
                        session.commit()
            except Exception as exc:  # pragma: no cover - only on failure
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=thread_count) as pool:
            list(pool.map(insert_tickets, range(thread_count)))

        assert errors == [], errors
        with factory() as session:
            total = session.scalar(select(func.count(SupportTicket.id)))
        assert total == thread_count * tickets_per_thread
    finally:
        engine.dispose()


def test_retrieval_cache_concurrent_set_get_eviction() -> None:
    max_entries = 64
    cache = RetrievalCache(max_entries=max_entries)
    errors: list[Exception] = []

    def churn(worker: int) -> None:
        try:
            for round_index in range(300):
                key = f"worker-{worker}-key-{round_index % 128}"
                cache.set(key, f"worker-{worker}-value-{round_index}", ttl_seconds=60)
                cache.get(key)
                if round_index % 64 == 0:
                    # Exercise clear() racing against get()/set(): its counter
                    # reset must not lose concurrent counter updates.
                    cache.clear()
        except Exception as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=churn, args=(worker,)) for worker in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [], errors
    with cache._lock:
        # Internal structure stays consistent and the bound holds no matter how
        # many threads raced set/get/evict.
        assert len(cache._memory) <= max_entries
        assert all(
            isinstance(expires_at, float) and isinstance(value, str)
            for expires_at, value in cache._memory.values()
        )
    status = cache.status()
    assert status.mode == "memory"  # REDIS_URL is pinned empty for this module
    assert status.hits > 0

    # Deterministic single-threaded cap check: inserting past the bound evicts
    # the oldest entry instead of growing without limit.
    tight = RetrievalCache(max_entries=2)
    tight.set("a", "1", ttl_seconds=60)
    tight.set("b", "2", ttl_seconds=60)
    tight.set("c", "3", ttl_seconds=60)
    with tight._lock:
        assert len(tight._memory) == 2
        assert set(tight._memory) == {"b", "c"}
    assert tight.get("a") is None
    assert tight.get("c") == "3"


def test_ticket_event_broker_concurrent_subscribers_and_unsubscribe() -> None:
    broker = TicketEventBroker()
    first_ready = asyncio.Event()
    second_ready = asyncio.Event()
    release = asyncio.Event()
    first_events: list[dict] = []
    second_events: list[dict] = []

    async def subscriber(
        ready: asyncio.Event,
        events: list[dict],
        hold_open: asyncio.Event | None,
        receives: int,
    ) -> None:
        async with broker.subscribe(predicate=lambda event: event.get("kind") == "ticket") as queue:
            # Entering the context registered this queue synchronously.
            ready.set()
            if hold_open is not None:
                await hold_open.wait()
            for _ in range(receives):
                events.append(await asyncio.wait_for(queue.get(), timeout=5))

    async def driver() -> None:
        first = asyncio.create_task(subscriber(first_ready, first_events, None, receives=1))
        second = asyncio.create_task(subscriber(second_ready, second_events, release, receives=2))
        await asyncio.gather(first_ready.wait(), second_ready.wait())
        assert broker.has_subscribers()

        # A non-ticket event must be filtered out for both subscribers.
        await broker.publish({"kind": "notification", "action": "ignored"})
        await asyncio.sleep(0)
        assert first_events == [] and second_events == []

        await broker.publish({"kind": "ticket", "action": "created"})
        await first  # received once, then its context exit unsubscribes it
        await broker.publish({"kind": "ticket", "action": "updated"})
        release.set()
        await second
        # The unsubscribed first subscriber must not have received the second
        # publish, and nothing stays attached afterwards.
        assert not broker.has_subscribers()

    asyncio.run(driver())

    assert [event["action"] for event in first_events] == ["created"]
    assert [event["action"] for event in second_events] == ["created", "updated"]
    assert second_events[1]["sequence"] > second_events[0]["sequence"]
