"""The per-task edge cache every graph scope reads through.

Lithos has **no bulk graph fetch** (ROADMAP ledger gap #3), so a dependency
graph is assembled one ``lithos_task_edge_list(task_id, direction="both")``
call per node. A project graph is ~100 of those, and the detail mini-graph,
the side panel and a second tab all ask for overlapping subsets of the same
corpus. Caching the *scope* would answer none of that overlap — two scopes
that share ninety nodes would share nothing — so what is cached is the unit
the reads are actually made in: ONE ENTRY PER TASK, holding that task's
deduped edge list and the instant it was read (D2).

Three properties, each earning its keep:

- **TTL** (``[graph].cache_ttl_s``, 30s) — the staleness bound. It is the
  ONLY bound available: edge upserts emit no event upstream (ledger gap #1),
  so an edge another agent adds is invisible until this expires or a task
  event lands on one of its endpoints. The page states its ``as_of`` rather
  than implying freshness, which is why :attr:`EdgeCacheEntry.fetched_at`
  is part of the entry and not an implementation detail.
- **Single-flight, per generation** — concurrent scopes over the same task
  share one in-flight call rather than racing to make the same read twice; a
  cold project page opened in two tabs is the ordinary case, not an exotic
  one. The collapse is per GENERATION, not absolute: an eviction retires the
  flight (see below), so a reader arriving after an event starts one more
  read rather than joining a result that predates it. Under a churning fleet
  — evictions are upstream-driven and a graph page is opened at exactly the
  tasks that churn — that is one extra concurrent read per eviction landing
  inside one read's window, so the CONCURRENCY is bounded at
  :data:`MAX_FLIGHTS_PER_TASK` live reads per id: past it a reader WAITS for
  one of them to finish and then reads for itself. Backpressure, not sharing
  — a reader that arrived after an eviction may never be answered from a read
  that started before it, whatever the pressure, or the hub's pre-fan-out
  ordering would stop meaning anything at exactly the load it matters at.
- **Event eviction** — :meth:`evict` on any consumed task event,
  :meth:`flush` on ``lens.refresh``. The :class:`~lithos_lens.events.EventHub`
  calls both BEFORE it fans an event out to browsers, so the refresh a
  browser makes in response to an event cannot be served the entry that
  event invalidated.

A failed read is **never** cached — not as an empty list, not as a sentinel.
An empty edge list means "this task has no edges" and would make the task
render as isolated; a failure means Lens does not know. The exception
propagates to every waiter, and the scope records it in its ``incomplete``
set (see :mod:`lithos_lens.graph_scope`).

Foundation module: it holds no client. Callers pass the read as a callable,
so the layering contract (Foundation must not import Core) holds and the
fan-out semaphore stays the caller's to place.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from lithos_lens.task_graph import EdgeRecord

# Mirrors the ``[lithos-lens.graph].cache_ttl_s`` config default so a caller
# with no tuning to do (most tests) gets the shipped behaviour;
# ``tests/test_graph_cache.py`` pins the two together.
DEFAULT_GRAPH_CACHE_TTL_S = 30

# Ceiling on retained entries, evicting least-recently-used first. WHICH task
# ids get cached is chosen from outside (``?epic=<id>`` admits any subtree), the
# TTL is enforced lazily on read — an entry nobody asks for again is never
# swept — and each entry holds a task's full edge list, whose size Lithos does
# not cap. So without a bound, resident memory of a long-lived process grows to
# every edge of every task ever scoped. The same call MAX_EVENT_SUBSCRIBERS and
# MAX_CONCURRENT_RENDERS make for the other structures whose size the caller
# chooses.
#
# Set at the ``[graph].max_tasks`` CEILING rather than its default, so no single
# page scope — however it is configured — can evict its own working set while
# assembling it, and so the mini-graph still reuses what a project page warmed.
MAX_GRAPH_CACHE_ENTRIES = 2000

# How many of the shared MCP session's slots ALL graph reads together may
# hold — every ``edge_list`` this cache issues and every ghost ``task_get``
# the scope assembly makes, across every concurrent render. The session's own
# gate is 16 (``mcp_transport.MAX_CONCURRENT_TOOL_CALLS`` — named rather than
# imported: Foundation may not reach Core) and its deadline includes QUEUE
# time, so a fan-out that fills it does not slow the dashboard and the detail
# page, it times them out. A per-render semaphore bounds one page and reserves
# nothing; this reserves half the session for everything else. The same call
# MAX_CONCURRENT_RENDERS and MAX_EVENT_SUBSCRIBERS make: bound the share of a
# shared resource one caller may hold.
GRAPH_FANOUT_SESSION_SHARE = 8

# Ceiling on concurrent upstream reads of ONE task id. Both terms that drive
# duplicate flights are chosen outside Lens: how many renders arrive (browser
# tabs, up to MAX_CONCURRENT_RENDERS) and how often the entry is evicted (one
# per consumed task event, at the fleet's rate). Each retired flight keeps
# running — it holds a slot in the 16-wide process-wide MCP gate whose
# CALL_TIMEOUT_S includes QUEUE time, so the overflow does not just slow the
# graph page, it times out unrelated renders.
#
# Above this many live reads of one id, a reader WAITS for one to finish and
# then makes its own read. Latency, not staleness: joining one of those reads
# would answer a post-eviction request with pre-eviction edges, which is the
# invalidation guarantee this cache exists to keep. The wait is bounded by the
# deadline the caller puts on each read (the scope's LINK_READ_TIMEOUT_S), so
# a stuck read delays a reader rather than pinning it.
MAX_FLIGHTS_PER_TASK = 4

#: How the cache reads a task's edges. The caller supplies it — bound to a
#: Lithos client and, on a scope fan-out, wrapped in that scope's semaphore.
EdgeFetch = Callable[[str], Awaitable[Sequence[EdgeRecord]]]

#: Injectable WALL clock: it stamps ``fetched_at``, which is what the page
#: shows as its ``as_of`` line. It does NOT decide expiry — see :data:`Ticks`.
Clock = Callable[[], datetime]

#: Injectable MONOTONIC clock, in seconds; it alone decides expiry.
#:
#: Two clocks rather than one, deliberately. ``fetched_at`` has to be wall time
#: because an operator reads it, and wall time can move BACKWARDS (an NTP
#: correction, a VM clock adjustment) — under which a wall-clock TTL computes a
#: negative age and keeps serving the entry until real time catches up, which a
#: large enough correction stretches into hours. That would break the one claim
#: the TTL makes: it is the ONLY invalidation path for an edge another agent
#: added, because edge upserts emit no event.
Ticks = Callable[[], float]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class EdgeCacheEntry:
    """One task's edges, and when they were read.

    ``fetched_at`` is what makes a scope's ``as_of`` honest: a scope reports
    the OLDEST ``fetched_at`` among the entries that contributed to it, so a
    graph assembled mostly from warm entries never presents itself as current.

    ``expires_at`` is on a different clock and is not comparable with it: it is
    a MONOTONIC instant (:data:`Ticks`), so a system clock step cannot extend
    an entry's life past the TTL it was cached under.
    """

    task_id: str
    edges: tuple[EdgeRecord, ...] = ()
    fetched_at: datetime = datetime.min.replace(tzinfo=UTC)
    expires_at: float = 0.0


# The live fan-out gate and the loop it belongs to; see graph_fanout_gate().
_fanout_gate: asyncio.Semaphore | None = None
_fanout_gate_loop: asyncio.AbstractEventLoop | None = None


def graph_fanout_gate() -> asyncio.Semaphore:
    """The process-wide gate every graph read passes through.

    Built lazily and rebuilt when the running loop changes, because an
    ``asyncio.Semaphore`` binds to the loop that first waits on it: one built
    at import would be pinned to whichever loop touched it first and raise for
    every other. A process serves one loop, so in production this is built
    once and shared by every render — which is the point. A per-render gate
    bounds one page and reserves nothing.
    """
    global _fanout_gate, _fanout_gate_loop
    loop = asyncio.get_running_loop()
    if _fanout_gate is None or _fanout_gate_loop is not loop:
        _fanout_gate = asyncio.Semaphore(GRAPH_FANOUT_SESSION_SHARE)
        _fanout_gate_loop = loop
    return _fanout_gate


def dedupe_edges(edges: Sequence[EdgeRecord]) -> tuple[EdgeRecord, ...]:
    """Collapse edges that name the same (from, to, type), keeping order.

    ``direction="both"`` returns a self-loop twice — once as incoming, once as
    outgoing — and the scope merges the lists of both endpoints of every edge,
    so the same relationship arrives from two sides. Identity is the triple:
    ``direction`` is relative to whichever task was asked, so it is exactly
    the field that must NOT take part.
    """
    seen: set[tuple[str, str, str]] = set()
    unique: list[EdgeRecord] = []
    for edge in edges:
        key = (edge.from_task_id, edge.to_task_id, edge.type)
        if key in seen:
            continue
        seen.add(key)
        unique.append(edge)
    return tuple(unique)


@dataclass
class _Flight:
    """One upstream read of one task, and whether it still counts.

    ``retired`` is set by :meth:`GraphCache.evict` / :meth:`GraphCache.flush`:
    the read is still running and its waiters still get its result, but it
    may no longer be joined by a new reader nor stored as an entry, because
    it describes the graph before the event that retired it.
    """

    generation: int
    task: asyncio.Task[EdgeCacheEntry]
    retired: bool = False


class GraphCache:
    """Per-task edge entries with a TTL, single-flight, and event eviction.

    Lives on :class:`~lithos_lens.state.AppState` beside the ``EventHub`` —
    one per process, shared by every graph surface.
    """

    def __init__(
        self,
        *,
        ttl_s: float = DEFAULT_GRAPH_CACHE_TTL_S,
        clock: Clock = _utcnow,
        ticks: Ticks = time.monotonic,
        max_entries: int = MAX_GRAPH_CACHE_ENTRIES,
        max_flights_per_task: int = MAX_FLIGHTS_PER_TASK,
    ) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._ticks = ticks
        self._max_entries = max_entries
        self._max_flights = max_flights_per_task
        # Insertion order IS recency order: every hit re-inserts its key (see
        # `_touch`), so the oldest key is the least recently USED one and the
        # bound evicts LRU rather than FIFO.
        self._entries: dict[str, EdgeCacheEntry] = {}
        # Every LIVE read of a task id, oldest first. At most one of them is
        # joinable (not retired); the rest are reads an eviction overtook and
        # that are still running. Both facts are needed at once — the joinable
        # one to collapse concurrent readers, the whole list to bound how many
        # duplicate reads one churning task id may have out (`_max_flights`).
        self._flights: dict[str, list[_Flight]] = {}
        self._generation = 0
        #: Served from a live entry, or joined to an in-flight read. Both mean
        #: "no new upstream call", which is what the graph page reports.
        self.hits = 0
        #: Upstream ``edge_list`` calls this cache issued.
        self.misses = 0
        self.evictions = 0
        #: Readers held back by the per-task flight cap. A rising figure is
        #: the signal that eviction pressure is outrunning the reads.
        self.waits = 0

    @property
    def size(self) -> int:
        return len(self._entries)

    def get(self, task_id: str) -> EdgeCacheEntry | None:
        """The live entry for ``task_id``, or ``None`` when absent or expired.

        A pure read as far as the counters go: it neither fetches nor counts,
        so a caller can ask what is warm without changing the hit/miss figures
        the page reports. It does refresh the entry's RECENCY, which is what
        makes the size bound an LRU rather than a FIFO.
        """
        entry = self._entries.get(task_id)
        if entry is None:
            return None
        if self._ticks() >= entry.expires_at:
            del self._entries[task_id]
            return None
        self._touch(task_id)
        return entry

    def _touch(self, task_id: str) -> None:
        self._entries[task_id] = self._entries.pop(task_id)

    async def edges_for(self, task_id: str, fetch: EdgeFetch) -> EdgeCacheEntry:
        """The task's edges, from the cache or from one shared upstream read.

        Raises whatever ``fetch`` raises, to every waiter, and stores nothing:
        the caller decides what a failed read means (for a scope, an
        ``incomplete`` node — never an edge-less one).
        """
        while True:
            entry = self.get(task_id)
            if entry is not None:
                self.hits += 1
                return entry

            flights = self._flights.setdefault(task_id, [])
            current = next((flight for flight in flights if not flight.retired), None)
            if current is not None:
                self.hits += 1
                # Shielded: one waiter being cancelled (a browser that went
                # away mid-render) must not cancel the read the OTHER waiters
                # are on.
                return await asyncio.shield(current.task)

            if len(flights) >= self._max_flights:
                # Every live read of this id was overtaken by an eviction, so
                # none of them may answer this request, and the cap says not
                # to add another. Wait for a slot and re-decide: by then the
                # answer may be a fresh cached entry, a flight started by
                # another post-eviction reader, or this reader's own read.
                self.waits += 1
                await asyncio.wait(
                    [flight.task for flight in flights],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                continue

            self.misses += 1
            self._generation += 1
            generation = self._generation
            inflight = asyncio.create_task(
                self._load(task_id, fetch, generation),
                name=f"graph-cache-edges-{task_id}",
            )
            flights.append(_Flight(generation=generation, task=inflight))
            return await asyncio.shield(inflight)

    async def _load(
        self, task_id: str, fetch: EdgeFetch, generation: int
    ) -> EdgeCacheEntry:
        try:
            edges = await fetch(task_id)
            entry = EdgeCacheEntry(
                task_id=task_id,
                edges=dedupe_edges(edges),
                fetched_at=self._clock(),
                expires_at=self._ticks() + self._ttl_s,
            )
            flight = self._flight(task_id, generation)
            if flight is not None and not flight.retired:
                self._store(entry)
            return entry
        finally:
            self._retire_finished(task_id, generation)

    def _flight(self, task_id: str, generation: int) -> _Flight | None:
        return next(
            (
                flight
                for flight in self._flights.get(task_id, ())
                if flight.generation == generation
            ),
            None,
        )

    def _retire_finished(self, task_id: str, generation: int) -> None:
        """Drop one finished flight, leaving every other flight for that id.

        Matched by generation rather than by task id, so a read that an
        eviction overtook cannot remove the newer read that replaced it.
        """
        flights = [
            flight
            for flight in self._flights.get(task_id, ())
            if flight.generation != generation
        ]
        if flights:
            self._flights[task_id] = flights
        else:
            self._flights.pop(task_id, None)

    def _store(self, entry: EdgeCacheEntry) -> None:
        self._entries[entry.task_id] = entry
        while len(self._entries) > self._max_entries:
            # Least recently used first. Counted as an eviction: it is one,
            # and a page whose cache is thrashing should be able to see it.
            self._entries.pop(next(iter(self._entries)))
            self.evictions += 1

    def evict(self, task_id: str) -> None:
        """Drop one task's entry — what a consumed task event does.

        Also RETIRES any read of that task already in flight. Retiring is what
        makes the hub's pre-fan-out ordering mean anything: the browser that
        reacts to the event asks again, and an eviction that only marked the
        flight would hand that new request the very result the event
        invalidated — for as long as the upstream read took. Callers already
        waiting on the retired flight still receive its result: it is the
        answer to the question they asked, and it is simply not cached.
        """
        if not task_id:
            return
        if self._entries.pop(task_id, None) is not None:
            self.evictions += 1
        for flight in self._flights.get(task_id, ()):
            flight.retired = True

    def flush(self) -> None:
        """Drop every entry — what ``lens.refresh`` does.

        A refresh is published exactly when Lens knows it may have MISSED
        events (a stream gap wider than Lithos's replay buffer), so per-task
        eviction has nothing to work from: the ids it would evict are the
        ones in the events that never arrived.
        """
        self.evictions += len(self._entries)
        self._entries.clear()
        # Every flight in progress is retired too, for the same reason
        # :meth:`evict` retires one: a refresh says Lens missed events, so a
        # read started before it cannot be trusted to answer a request made
        # after it.
        for flights in self._flights.values():
            for flight in flights:
                flight.retired = True
