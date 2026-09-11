"""Bounded reads against Lithos for one graph render.

Split from :mod:`lithos_lens.graph_scope` so the fan-out and the bounds on it
sit together: assembly decides WHAT a scope contains, this decides what asking
is allowed to cost. Every read here goes through two gates — the process-wide
`graph_fanout_gate()` reservation that keeps graph pages from taking the whole
MCP session, and the caller's per-render limiter — and carries its own
deadline inside them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Protocol

from lithos_lens.graph_cache import (
    CacheTally,
    EdgeCacheEntry,
    GraphCache,
    graph_fanout_gate,
)
from lithos_lens.task_graph import EdgeRecord
from lithos_lens.task_links import LINK_READ_TIMEOUT_S
from lithos_lens.tasks import TaskRecord

# How many out-of-set endpoints Lens will resolve for one render, and how long
# it will spend doing it.
#
# These bound a fan-out whose size is chosen by whoever WROTE the edges, not by
# whoever configured the page. `task_edge_list` caps no edge count, so a scope
# whose node set is comfortably inside `max_tasks` can still name unboundedly
# many far endpoints — D5's "fan-out is bounded by the scope" holds for a
# ghost's own edges, which are never fetched, but not for how many ghosts one
# scope can NAME. Semaphores bound how many of those reads run at once; they do
# not bound how many are queued, how much memory the queue costs, or how long
# the queue takes to drain. Worse, each read's own deadline does not start until
# it acquires both gates, so a deep queue defers the very timeout meant to
# contain it.
#
# Past these bounds Lens REFUSES the scope rather than rendering it. Leaving
# candidates unread and drawing them as `unknown` ghosts would be the cheaper
# answer and the wrong one: D5/D6 require a completed predecessor's edge to be
# ABSENT, so an unread candidate is a fabricated node, and one that counts
# toward the very guard it slipped past. A refusal says Lens does not know; a
# fabricated ghost says something false about the graph.
#
# Internal safety nets rather than dials operators tune — the same call as
# `graph_cache.MAX_GRAPH_CACHE_ENTRIES` and `task_links.LINK_READ_TIMEOUT_S`,
# and the reason the PRD's four `[graph]` knobs stay four.
MAX_GHOST_RESOLUTION_READS = 1000
GHOST_RESOLUTION_BUDGET_S = 10.0


class GraphScopeClient(Protocol):
    """The narrow client surface scope assembly needs."""

    async def task_get(self, task_id: str) -> TaskRecord: ...

    async def task_children(
        self,
        task_id: str,
        *,
        recursive: bool = False,
        include_closed: bool = False,
    ) -> list[TaskRecord]: ...

    async def task_edge_list(
        self,
        task_id: str,
        *,
        direction: str = "both",
        types: list[str] | None = None,
    ) -> list[EdgeRecord]: ...


async def read_edges(
    lithos: GraphScopeClient,
    tasks: Sequence[TaskRecord],
    cache: GraphCache,
    limiter: asyncio.Semaphore,
    tally: CacheTally | None = None,
) -> tuple[tuple[EdgeCacheEntry, ...], dict[str, str]]:
    """One cache read per node; failures become ``incomplete``, not silence.

    ``tally`` counts THIS render's hits and misses, so the page can report its
    own fan-out rather than a slice of a process-wide counter (see
    :class:`~lithos_lens.graph_cache.CacheTally`).
    """

    async def fetch(task_id: str) -> list[EdgeRecord]:
        async with graph_fanout_gate(), limiter:
            # Deadlined inside the gate, as the detail page's fan-out is: a
            # read that never answers would otherwise hold one of the few
            # slots for as long as the session stays half-open.
            return await asyncio.wait_for(
                lithos.task_edge_list(task_id, direction="both"), LINK_READ_TIMEOUT_S
            )

    results = await asyncio.gather(
        *(cache.edges_for(task.id, fetch, tally) for task in tasks),
        return_exceptions=True,
    )
    entries: list[EdgeCacheEntry] = []
    incomplete: dict[str, str] = {}
    for task, result in zip(tasks, results, strict=True):
        if isinstance(result, BaseException):
            incomplete[task.id] = _failure_reason(result)
        else:
            entries.append(result)
    return tuple(entries), incomplete


def partition_far_endpoints(
    far_ids: Sequence[str],
    master: Sequence[TaskRecord],
) -> tuple[dict[str, TaskRecord], tuple[str, ...]]:
    """Split ghost candidates into "already known" and "needs a read".

    Pure, and separated from the reads so the work budget can be applied to
    the read list BEFORE any of it is enqueued. An OPEN far endpoint is
    already on the master list, so the common case — a live cross-project
    blocker — costs no read at all (D5); only resolved or absent endpoints
    need one.
    """
    open_index = {task.id: task for task in master if task.status == "open"}
    resolved: dict[str, TaskRecord] = {}
    pending: list[str] = []
    for far_id in far_ids:
        known = open_index.get(far_id)
        if known is not None:
            resolved[far_id] = known
        else:
            pending.append(far_id)
    return resolved, tuple(pending)


async def resolve_far_endpoints(
    lithos: GraphScopeClient,
    pending: Sequence[str],
    resolved: dict[str, TaskRecord],
    limiter: asyncio.Semaphore,
) -> set[str]:
    """Read every pending candidate, filling ``resolved`` and returning failures.

    Each read decides drop-versus-ghost (D5/D6), so a candidate left unread
    would render as an ``unknown`` ghost where the contract requires a
    completed predecessor's edge to be ABSENT. That is why no candidate is
    skipped and no sampling rule applies WITHIN a render: the reads are the
    classification. What bounds the cost is the caller — a queue-length
    budget and a deadline on this whole phase
    (:data:`MAX_GHOST_RESOLUTION_READS`, :data:`GHOST_RESOLUTION_BUDGET_S`),
    past which the scope is refused rather than answered approximately.

    A read that FAILS is different from one never issued: its id joins the
    returned ``unresolved`` set, the ghost is shown with ``status unknown``,
    and every dependency edge touching it is ``unknown`` — Lens asked and did
    not get an answer, which D6 has a semantic for.
    """

    async def read(task_id: str) -> TaskRecord:
        async with graph_fanout_gate(), limiter:
            return await asyncio.wait_for(lithos.task_get(task_id), LINK_READ_TIMEOUT_S)

    results = await asyncio.gather(
        *(read(task_id) for task_id in pending), return_exceptions=True
    )
    unresolved: set[str] = set()
    for task_id, result in zip(pending, results, strict=True):
        if isinstance(result, BaseException):
            unresolved.add(task_id)
        else:
            resolved[task_id] = result
    return unresolved


def _failure_reason(exc: BaseException) -> str:
    """A short, stable reason for the ``incomplete`` map.

    The Lithos error code when there is one (``LithosToolError`` carries it),
    else the exception type. Matched duck-typed because the layering contract
    forbids Foundation importing the client.
    """
    code: Any = getattr(exc, "code", "")
    if isinstance(code, str) and code:
        return code
    return type(exc).__name__
