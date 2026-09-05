"""T2 slice A1 — the per-task edge cache (``graph_cache.py``).

The cache is the only thing standing between a hundred-node graph page and a
hundred `lithos_task_edge_list` round trips per render, and the only thing
bounding how stale those edges may be — edge upserts emit no event upstream,
so its TTL and the hub's eviction hook are the whole invalidation story. The
tests below pin exactly that: what it serves without asking upstream, what it
refuses to remember (a failed read), and what the event stream makes it
forget.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from lithos_lens.config import DEFAULT_GRAPH_CACHE_TTL_S as CONFIG_CACHE_TTL_S
from lithos_lens.config import EventsConfig, LithosConfig
from lithos_lens.events import LENS_REFRESH_EVENT, EventHub, LensEvent
from lithos_lens.graph_cache import (
    DEFAULT_GRAPH_CACHE_TTL_S,
    GraphCache,
    dedupe_edges,
)
from lithos_lens.task_graph import EdgeRecord

pytestmark = pytest.mark.anyio

_T0 = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


class StepClock:
    """A wall clock the test moves by hand."""

    def __init__(self, start: datetime = _T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def edge(from_id: str, to_id: str, *, direction: str = "outgoing") -> EdgeRecord:
    return EdgeRecord(
        from_task_id=from_id, to_task_id=to_id, type="blocks", direction=direction
    )


class Reader:
    """One recorded ``edge_list`` reader, optionally gated and/or failing."""

    def __init__(
        self,
        edges: dict[str, list[EdgeRecord]] | None = None,
        *,
        gate: asyncio.Event | None = None,
        fail: set[str] | None = None,
    ) -> None:
        self._edges = edges or {}
        self._gate = gate
        self._fail = fail or set()
        self.calls: list[str] = []

    async def __call__(self, task_id: str) -> list[EdgeRecord]:
        self.calls.append(task_id)
        if self._gate is not None:
            await self._gate.wait()
        if task_id in self._fail:
            raise RuntimeError(f"edge_list failed for {task_id}")
        return list(self._edges.get(task_id, ()))


def test_default_ttl_mirrors_the_config_default() -> None:
    """The module default and the operator-facing one are one decision.

    ``config.py`` is the source of truth an operator reads; the module default
    exists so a caller with no tuning to do gets the shipped behaviour. Pinned
    here so they cannot drift into two different 30-second answers.
    """
    assert DEFAULT_GRAPH_CACHE_TTL_S == CONFIG_CACHE_TTL_S


async def test_second_read_is_served_without_asking_upstream() -> None:
    cache = GraphCache(clock=StepClock())
    reader = Reader({"a": [edge("a", "b")]})

    first = await cache.edges_for("a", reader)
    second = await cache.edges_for("a", reader)

    assert reader.calls == ["a"]
    assert first == second
    assert (cache.hits, cache.misses, cache.size) == (1, 1, 1)


async def test_an_entry_past_its_ttl_is_re_read() -> None:
    clock = StepClock()
    cache = GraphCache(ttl_s=30, clock=clock)
    reader = Reader({"a": [edge("a", "b")]})

    first = await cache.edges_for("a", reader)
    clock.advance(seconds=30)
    second = await cache.edges_for("a", reader)

    assert reader.calls == ["a", "a"]
    assert second.fetched_at > first.fetched_at


async def test_two_concurrent_readers_share_one_in_flight_call() -> None:
    """Single-flight: the second reader joins, it does not race.

    Two tabs opening the same cold project page is the ordinary case, and
    without this each of them would issue the whole fan-out.
    """
    gate = asyncio.Event()
    cache = GraphCache(clock=StepClock())
    reader = Reader({"a": [edge("a", "b")]}, gate=gate)

    first = asyncio.create_task(cache.edges_for("a", reader))
    second = asyncio.create_task(cache.edges_for("a", reader))
    await asyncio.sleep(0)
    gate.set()
    entries = await asyncio.gather(first, second)

    assert reader.calls == ["a"]
    assert entries[0] == entries[1]


async def test_a_failed_read_is_not_cached_and_reaches_every_waiter() -> None:
    """An empty edge list means "no edges"; a failure means "unknown".

    Caching the failure as an empty entry would make the task render as
    isolated — a claim Lens has no evidence for — and hold that claim for a
    full TTL.
    """
    gate = asyncio.Event()
    cache = GraphCache(clock=StepClock())
    reader = Reader(gate=gate, fail={"a"})

    waiters = [asyncio.create_task(cache.edges_for("a", reader)) for _ in range(2)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*waiters, return_exceptions=True)

    assert all(isinstance(result, RuntimeError) for result in results)
    assert reader.calls == ["a"]
    assert cache.size == 0
    assert cache.get("a") is None


async def test_evict_drops_one_entry_and_flush_drops_all() -> None:
    cache = GraphCache(clock=StepClock())
    reader = Reader({"a": [edge("a", "b")], "b": []})
    await cache.edges_for("a", reader)
    await cache.edges_for("b", reader)

    cache.evict("a")
    assert cache.get("a") is None
    assert cache.get("b") is not None

    cache.flush()
    assert cache.size == 0
    assert cache.evictions == 2


async def test_an_entry_evicted_mid_flight_is_returned_but_not_stored() -> None:
    """The read describes the graph BEFORE the event that evicted it.

    Storing it would pin edges Lens already knows are stale for a full TTL,
    and nothing upstream would correct them: an edge upsert emits no event.
    """
    gate = asyncio.Event()
    cache = GraphCache(clock=StepClock())
    reader = Reader({"a": [edge("a", "b")]}, gate=gate)

    inflight = asyncio.create_task(cache.edges_for("a", reader))
    await asyncio.sleep(0)
    cache.evict("a")
    gate.set()
    entry = await inflight

    assert entry.edges == (edge("a", "b"),)
    assert cache.get("a") is None


async def test_the_hub_evicts_the_event_s_task_before_fanning_it_out() -> None:
    """A task event invalidates that task's entry, and only that one."""
    cache = GraphCache(clock=StepClock())
    reader = Reader({"a": [], "b": []})
    await cache.edges_for("a", reader)
    await cache.edges_for("b", reader)
    hub = EventHub(EventsConfig(enabled=False), LithosConfig(), graph_cache=cache)
    queue = hub.subscribe()

    await hub.publish(LensEvent(id="e1", type="task.updated", task_id="a"))

    assert cache.get("a") is None
    assert cache.get("b") is not None
    # Pre-fan-out: the browser that reacts to this event cannot be served the
    # entry the event invalidated.
    assert queue.get_nowait().task_id == "a"


async def test_a_lens_refresh_flushes_the_whole_cache() -> None:
    """`lens.refresh` is published when Lens knows it MISSED events.

    There are no task ids to evict then — they are in the events that never
    arrived — so the only honest scope is everything.
    """
    cache = GraphCache(clock=StepClock())
    reader = Reader({"a": [], "b": []})
    await cache.edges_for("a", reader)
    await cache.edges_for("b", reader)
    hub = EventHub(EventsConfig(enabled=False), LithosConfig(), graph_cache=cache)

    await hub.publish(LensEvent(id="e2", type=LENS_REFRESH_EVENT, task_id=""))

    assert cache.size == 0


async def test_a_system_event_evicts_nothing() -> None:
    """`agent.registered` carries no task scope; it invalidates no edges."""
    cache = GraphCache(clock=StepClock())
    reader = Reader({"a": []})
    await cache.edges_for("a", reader)
    hub = EventHub(EventsConfig(enabled=False), LithosConfig(), graph_cache=cache)

    await hub.publish(
        LensEvent(id="e3", type="agent.registered", task_id="", requires_refresh=False)
    )

    assert cache.get("a") is not None


def test_dedupe_collapses_an_edge_reported_from_both_directions() -> None:
    """`direction` is relative to whoever was asked, so it is not identity."""
    both_ways = [
        edge("a", "a", direction="outgoing"),
        edge("a", "a", direction="incoming"),
        edge("a", "b"),
    ]

    assert dedupe_edges(both_ways) == (both_ways[0], both_ways[2])
