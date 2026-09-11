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
from lithos_lens.config import MAX_GRAPH_INT_KNOBS, EventsConfig, LithosConfig
from lithos_lens.events import LENS_REFRESH_EVENT, EventHub, LensEvent
from lithos_lens.graph_cache import (
    DEFAULT_GRAPH_CACHE_TTL_S,
    GRAPH_FANOUT_SESSION_SHARE,
    MAX_GRAPH_CACHE_ENTRIES,
    GraphCache,
    dedupe_edges,
)
from lithos_lens.mcp_transport import MAX_CONCURRENT_TOOL_CALLS
from lithos_lens.task_graph import EdgeRecord

pytestmark = pytest.mark.anyio

_T0 = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


class StepClock:
    """The cache's two clocks, moved by hand and independently.

    ``advance`` moves both, as real time does. ``skew`` moves ONLY the wall
    clock, which is what an NTP correction or a VM clock adjustment does — and
    what must not touch expiry.
    """

    def __init__(self, start: datetime = _T0) -> None:
        self.now = start
        self.ticks_now = 1000.0

    def __call__(self) -> datetime:
        return self.now

    def ticks(self) -> float:
        return self.ticks_now

    def advance(self, **delta: float) -> None:
        step = timedelta(**delta)
        self.now += step
        self.ticks_now += step.total_seconds()

    def skew(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def edge(from_id: str, to_id: str, *, direction: str = "outgoing") -> EdgeRecord:
    return EdgeRecord(
        from_task_id=from_id, to_task_id=to_id, type="blocks", direction=direction
    )


class Reader:
    """One recorded ``edge_list`` reader, optionally gated and/or failing.

    ``peak`` is the most reads it ever had in flight at once — the figure the
    per-task flight cap bounds. ``versioned`` makes each call return a
    distinguishable edge set, so a test can say WHICH read a result came from
    rather than only how many were made.
    """

    def __init__(
        self,
        edges: dict[str, list[EdgeRecord]] | None = None,
        *,
        gate: asyncio.Event | None = None,
        fail: set[str] | None = None,
        versioned: bool = False,
    ) -> None:
        self._edges = edges or {}
        self._gate = gate
        self._fail = fail or set()
        self._versioned = versioned
        self.calls: list[str] = []
        self.inflight = 0
        self.peak = 0

    async def __call__(self, task_id: str) -> list[EdgeRecord]:
        self.calls.append(task_id)
        version = len(self.calls)
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            if self._gate is not None:
                await self._gate.wait()
            if task_id in self._fail:
                raise RuntimeError(f"edge_list failed for {task_id}")
            if self._versioned:
                return [edge(task_id, f"v{version}")]
            return list(self._edges.get(task_id, ()))
        finally:
            self.inflight -= 1


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
    cache = GraphCache(ttl_s=30, clock=clock, ticks=clock.ticks)
    reader = Reader({"a": [edge("a", "b")]})

    first = await cache.edges_for("a", reader)
    clock.advance(seconds=30)
    second = await cache.edges_for("a", reader)

    assert reader.calls == ["a", "a"]
    assert second.fetched_at > first.fetched_at


async def test_a_backwards_wall_clock_does_not_extend_an_entry_s_life() -> None:
    """Expiry runs on the monotonic clock, `as_of` on the wall clock.

    A wall clock can step BACKWARDS (an NTP correction, a VM clock
    adjustment), which makes an entry's wall-clock age negative and would keep
    serving it until real time caught back up — hours, for a large enough
    correction. The TTL is the only invalidation path for an edge another
    agent added, so it may not be the thing that moves.
    """
    clock = StepClock()
    cache = GraphCache(ttl_s=30, clock=clock, ticks=clock.ticks)
    reader = Reader({"a": [edge("a", "b")]})
    await cache.edges_for("a", reader)

    clock.skew(hours=-1)
    clock.advance(seconds=31)

    assert cache.get("a") is None
    await cache.edges_for("a", reader)
    assert reader.calls == ["a", "a"]


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


async def test_a_reader_arriving_after_an_eviction_starts_a_new_read() -> None:
    """Eviction RETIRES the flight; it does not merely mark its result.

    The interleaving this exists for: a scope starts reading task `a`, the
    `task.updated` for `a` is consumed and evicts before the hub notifies
    browsers, and a browser reacts and asks again while the first read is
    still out. Joining the in-flight read would hand that refresh exactly the
    pre-event edges the event invalidated — which is what the hub's
    pre-fan-out ordering is supposed to make impossible.
    """
    gate = asyncio.Event()
    cache = GraphCache(clock=StepClock())
    reader = Reader({"a": [edge("a", "b")]}, gate=gate)

    early = asyncio.create_task(cache.edges_for("a", reader))
    await asyncio.sleep(0)
    cache.evict("a")
    late = asyncio.create_task(cache.edges_for("a", reader))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(early, late)

    assert reader.calls == ["a", "a"]
    # The post-event read is the one that gets cached; the retired flight
    # neither stores its result nor evicts the newer flight from the map.
    assert cache.get("a") is not None


async def test_concurrent_reads_of_one_task_are_bounded() -> None:
    """Retirement makes the collapse per-generation, so it needs its own bound.

    Both terms are chosen outside Lens: how many renders arrive, and how often
    the entry is evicted (one per consumed task event, at the fleet's rate).
    Each retired read keeps running and keeps its slot in the process-wide MCP
    gate, whose deadline includes queue time — so unbounded duplicates would
    time out unrelated renders, not merely slow the graph page. Past the cap a
    reader waits for a slot instead of adding one.
    """
    gate = asyncio.Event()
    cache = GraphCache(clock=StepClock(), max_flights_per_task=2)
    reader = Reader({"a": [edge("a", "b")]}, gate=gate, versioned=True)

    waiters = []
    for _ in range(4):
        # Every reader arrives just after an eviction, so none of them may
        # join the flight already out — the worst case for amplification.
        waiters.append(asyncio.create_task(cache.edges_for("a", reader)))
        await asyncio.sleep(0)
        cache.evict("a")
    gate.set()
    await asyncio.gather(*waiters)

    assert reader.peak <= 2
    assert cache.waits >= 1


async def test_at_the_cap_a_post_eviction_reader_still_gets_a_post_event_read() -> None:
    """The cap is backpressure, never a shared stale result.

    Every live read here was started before the last eviction, so none of them
    may answer a request made after it — that is the guarantee the hub's
    pre-fan-out eviction exists to give, and it has to hold under pressure or
    it holds only when it is not needed. The third reader therefore waits for
    a slot and reads for itself, and gets the read that started AFTER the
    event rather than the freshest one that predates it.
    """
    gate = asyncio.Event()
    cache = GraphCache(clock=StepClock(), max_flights_per_task=2)
    reader = Reader(gate=gate, versioned=True)

    first = asyncio.create_task(cache.edges_for("a", reader))
    await asyncio.sleep(0)
    cache.evict("a")
    second = asyncio.create_task(cache.edges_for("a", reader))
    await asyncio.sleep(0)
    cache.evict("a")

    late = asyncio.create_task(cache.edges_for("a", reader))
    await asyncio.sleep(0)
    # Held at the cap: it has NOT joined either retired read.
    assert reader.calls == ["a", "a"]

    gate.set()
    await asyncio.gather(first, second)
    entry = await late

    assert reader.calls == ["a", "a", "a"]
    assert entry.edges == (edge("a", "v3"),)
    # Its read was never overtaken, so it is the one that gets cached.
    assert cache.get("a") == entry


async def test_entries_are_bounded_and_the_least_recently_used_goes_first() -> None:
    """WHICH ids are cached is chosen from outside, so the count needs a bound.

    The TTL is enforced lazily on read, so an entry nobody asks for again is
    never swept — a one-off `?epic=<id>` scope would otherwise pin its whole
    subtree's edge lists for the life of the process.
    """
    cache = GraphCache(clock=StepClock(), max_entries=2)
    reader = Reader({"a": [], "b": [], "c": []})
    await cache.edges_for("a", reader)
    await cache.edges_for("b", reader)

    assert cache.get("a") is not None  # `a` becomes the most recently used
    await cache.edges_for("c", reader)

    assert cache.size == 2
    assert cache.get("b") is None
    assert cache.get("a") is not None and cache.get("c") is not None


def test_the_graph_share_leaves_the_session_room_for_everything_else() -> None:
    """A reservation only reserves if it is smaller than what it reserves from.

    The graph layer names the session's gate rather than importing it — the
    layering contract forbids Foundation reaching Core — so the two numbers
    can drift apart in the source. Pinned here, where a test may import both:
    at the share's ceiling the dashboard, the detail page and the fleet's own
    traffic would be queueing behind graph renders on a deadline that counts
    queue time.
    """
    assert GRAPH_FANOUT_SESSION_SHARE < MAX_CONCURRENT_TOOL_CALLS


def test_the_shipped_entry_bound_clears_the_largest_configurable_scope() -> None:
    """A page must never evict its own working set while assembling it."""
    assert MAX_GRAPH_INT_KNOBS["max_tasks"] <= MAX_GRAPH_CACHE_ENTRIES


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
