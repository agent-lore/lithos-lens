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
- **Single-flight** — two concurrent scopes over the same task share one
  in-flight call rather than racing to make the same read twice. A cold
  project page opened in two tabs is the ordinary case, not an exotic one.
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
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from lithos_lens.task_graph import EdgeRecord

# Mirrors the ``[lithos-lens.graph].cache_ttl_s`` config default so a caller
# with no tuning to do (most tests) gets the shipped behaviour;
# ``tests/test_graph_cache.py`` pins the two together.
DEFAULT_GRAPH_CACHE_TTL_S = 30

#: How the cache reads a task's edges. The caller supplies it — bound to a
#: Lithos client and, on a scope fan-out, wrapped in that scope's semaphore.
EdgeFetch = Callable[[str], Awaitable[Sequence[EdgeRecord]]]

#: Injectable clock. The same reading serves both the TTL and the operator's
#: ``as_of`` line, so it is deliberately a WALL clock rather than a monotonic
#: one: a graph page has to name the instant its data came from, and two
#: clocks would let the timestamp shown disagree with the entry's freshness.
#: The residual — a system clock step could expire an entry early or hold it
#: a little long — costs at most one extra read against a 30-second TTL.
Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class EdgeCacheEntry:
    """One task's edges, and when they were read.

    ``fetched_at`` is what makes a scope's ``as_of`` honest: a scope reports
    the OLDEST ``fetched_at`` among the entries that contributed to it, so a
    graph assembled mostly from warm entries never presents itself as current.
    """

    task_id: str
    edges: tuple[EdgeRecord, ...] = ()
    fetched_at: datetime = datetime.min.replace(tzinfo=UTC)


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
    ) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._entries: dict[str, EdgeCacheEntry] = {}
        self._inflight: dict[str, asyncio.Task[EdgeCacheEntry]] = {}
        # Task ids evicted while their read was in flight. That result
        # describes the graph BEFORE the event that evicted it, so it is
        # returned to the waiters who asked for it and then dropped rather
        # than stored — caching it would pin known-stale edges for a full TTL.
        self._invalidated_inflight: set[str] = set()
        #: Served from a live entry, or joined to an in-flight read. Both mean
        #: "no new upstream call", which is what the graph page reports.
        self.hits = 0
        #: Upstream ``edge_list`` calls this cache issued.
        self.misses = 0
        self.evictions = 0

    @property
    def size(self) -> int:
        return len(self._entries)

    def get(self, task_id: str) -> EdgeCacheEntry | None:
        """The live entry for ``task_id``, or ``None`` when absent or expired.

        A pure read: it neither fetches nor counts, so a caller can ask what
        is warm without changing the hit/miss figures the page reports.
        """
        entry = self._entries.get(task_id)
        if entry is None:
            return None
        if (self._clock() - entry.fetched_at).total_seconds() >= self._ttl_s:
            del self._entries[task_id]
            return None
        return entry

    async def edges_for(self, task_id: str, fetch: EdgeFetch) -> EdgeCacheEntry:
        """The task's edges, from the cache or from one shared upstream read.

        Raises whatever ``fetch`` raises, to every waiter, and stores nothing:
        the caller decides what a failed read means (for a scope, an
        ``incomplete`` node — never an edge-less one).
        """
        entry = self.get(task_id)
        if entry is not None:
            self.hits += 1
            return entry

        inflight = self._inflight.get(task_id)
        if inflight is None:
            self.misses += 1
            inflight = asyncio.create_task(
                self._load(task_id, fetch), name=f"graph-cache-edges-{task_id}"
            )
            self._inflight[task_id] = inflight
        else:
            self.hits += 1
        # Shielded: one waiter being cancelled (a browser that went away
        # mid-render) must not cancel the read the OTHER waiters are on.
        return await asyncio.shield(inflight)

    async def _load(self, task_id: str, fetch: EdgeFetch) -> EdgeCacheEntry:
        try:
            edges = await fetch(task_id)
            entry = EdgeCacheEntry(
                task_id=task_id,
                edges=dedupe_edges(edges),
                fetched_at=self._clock(),
            )
            if task_id not in self._invalidated_inflight:
                self._entries[task_id] = entry
            return entry
        finally:
            self._inflight.pop(task_id, None)
            self._invalidated_inflight.discard(task_id)

    def evict(self, task_id: str) -> None:
        """Drop one task's entry — what a consumed task event does."""
        if not task_id:
            return
        if self._entries.pop(task_id, None) is not None:
            self.evictions += 1
        if task_id in self._inflight:
            self._invalidated_inflight.add(task_id)

    def flush(self) -> None:
        """Drop every entry — what ``lens.refresh`` does.

        A refresh is published exactly when Lens knows it may have MISSED
        events (a stream gap wider than Lithos's replay buffer), so per-task
        eviction has nothing to work from: the ids it would evict are the
        ones in the events that never arrived.
        """
        self.evictions += len(self._entries)
        self._entries.clear()
        self._invalidated_inflight.update(self._inflight)
