"""What cancelling a task does to everything else, stated before it is done.

A cancelled predecessor blocks its dependents forever: Lithos treats a
cancelled ``blocks`` / ``waits_on_gate`` predecessor as never satisfiable, so
the dependents are permanently blocked until someone re-routes them. Nothing
upstream says so before the cancel. This module computes the consequence the
cancel confirm page (and, with ``[writes].confirm_cancel = false``, the
receipt) states (T3 D9, REQUIREMENTS §5C.2 "Cancel"):

- **stranded directly** — the open dependents one hop out, over **active**
  dependency edges (T2's edge states, :func:`graph_scope.dependency_edge_state`);
- **behind them** — the further open transitive dependents;
- the **active claims** the cancel releases, read BEFORE it (they are gone once
  it lands);
- the task's **open children**, which are NOT cancelled with it.

The walk goes downstream through the per-task edge cache, the focal task's own
entry evicted and re-read first so the first hop is never served from before
the operator opened the page. It **crosses project boundaries** — a cancel
strands whoever depends on the task, not only tasks in the same scope — so
which dependents are open comes from ONE cross-project
``list_tasks(status="open")`` read (the read the Proceed anyway page makes for
the same reason), not from a ``task_get`` per node. A dependent missing from
that whole list is resolved, and its edge is ``dependent_resolved``.

**Bounded, and honest about it.** The walk visits at most ``max_nodes``
dependents (the graph page's own ``[graph].max_tasks`` — the PRD adds no knob)
and runs for at most :data:`WALK_DEADLINE_S`. Where the graph page REFUSES a
scope past its bound, this walk DEGRADES: past the budget, past the deadline,
or after any failed edge read, both counts are lower bounds ("≥ N") and carry
the reason — the T2 rule. A failed open-list read leaves nothing to classify
edges against, so the consequence is unavailable rather than a confident zero.

TaskGraph rather than Web: it is a downstream walk over the edge cache and the
edge-state rule, the same layer as ``graph_impact``, and it is typed against
its own narrow client Protocol (as ``graph_fanout.GraphScopeClient`` is), so it
adds no edge to the client. Every read catches its own failure: the write path
calls this inside ``perform``, where an exception would be classified as the
cancel's own failure.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from lithos_lens.graph_cache import EdgeCacheEntry, GraphCache
from lithos_lens.graph_fanout import GraphScopeClient, read_edges
from lithos_lens.graph_scope import EDGE_ACTIVE, dependency_edge_state
from lithos_lens.task_links import BLOCKER_EDGE_TYPES, GATE_TASK_TYPE
from lithos_lens.tasks import TaskRecord, TaskStatusRecord

logger = logging.getLogger(__name__)

__all__ = [
    "WALK_DEADLINE_S",
    "CancelClient",
    "CancelConsequences",
    "ClaimHolder",
    "DownstreamWalk",
    "load_cancel_consequences",
    "walk_downstream",
]

#: How long the downstream walk may take, end to end. An internal safety net,
#: like ``graph_fanout.GHOST_RESOLUTION_BUDGET_S``, not an operator dial: past
#: it the counts so far are stated as lower bounds ("took too long").
WALK_DEADLINE_S = 10.0

#: The status a dependent absent from the whole open list stands in with. It
#: is completed or cancelled — Lens does not need to know which, because
#: either makes the edge ``dependent_resolved`` and the dependent unstranded.
_RESOLVED = "completed"

#: Why the counts are lower bounds, worded for "≥ N — <reason>".
REASON_DEADLINE = "the walk took too long"


class CancelClient(GraphScopeClient, Protocol):
    """The narrow client surface the consequence read needs."""

    async def list_tasks(self, *, status: str | None = None) -> list[TaskRecord]: ...

    async def task_status(self, task_id: str) -> TaskStatusRecord | None: ...


@dataclass(frozen=True)
class DownstreamWalk:
    """The open dependents a cancel strands, as one bounded walk saw them.

    ``direct`` and ``behind`` are ordered by ``created_at`` then id, so "the
    first few" are the oldest. ``exact`` is false when either count is a lower
    bound, and ``bound_reasons`` says why, one phrase per cause.
    """

    direct: tuple[TaskRecord, ...] = ()
    behind: tuple[TaskRecord, ...] = ()
    exact: bool = True
    bound_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClaimHolder:
    """One agent's active claims on the task: who loses what if it is cancelled."""

    agent: str
    aspects: tuple[str, ...] = ()


@dataclass(frozen=True)
class CancelConsequences:
    """Everything the cancel confirm page states about one open task.

    ``walk`` is None when the open list could not be read — there is nothing
    to classify edges against — and ``unavailable`` then says why. A failed
    claims or children read leaves its own flag set rather than an empty
    tuple, so "releases no claims" is never said when Lens could not look.
    """

    task: TaskRecord
    walk: DownstreamWalk | None = None
    unavailable: str = ""
    claims: tuple[ClaimHolder, ...] = ()
    claims_unread: bool = False
    open_children: tuple[TaskRecord, ...] = ()
    children_unread: bool = False

    @property
    def is_gate(self) -> bool:
        return self.task.task_type == GATE_TASK_TYPE


def _order(tasks: Sequence[TaskRecord]) -> tuple[TaskRecord, ...]:
    return tuple(sorted(tasks, key=lambda task: (task.created_at, task.id)))


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _active_dependents(
    entry: EdgeCacheEntry,
    predecessor_status: str,
    open_index: Mapping[str, TaskRecord],
) -> list[TaskRecord]:
    """The open tasks ``entry``'s task holds back over an ACTIVE dependency edge.

    Only its OUTGOING ``blocks`` / ``waits_on_gate`` edges: those point at its
    dependents. Classified by T2's rule, so a resolved dependent (absent from
    the open list) is ``dependent_resolved`` and never stranded.
    """
    found: list[TaskRecord] = []
    for edge in entry.edges:
        if edge.type not in BLOCKER_EDGE_TYPES or edge.from_task_id != entry.task_id:
            continue
        dependent = open_index.get(edge.to_task_id)
        state, _ = dependency_edge_state(
            predecessor_status, dependent.status if dependent else _RESOLVED
        )
        if dependent is not None and state == EDGE_ACTIVE:
            found.append(dependent)
    return found


async def walk_downstream(
    lithos: GraphScopeClient,
    task: TaskRecord,
    *,
    open_index: Mapping[str, TaskRecord],
    cache: GraphCache,
    fetch_concurrency: int,
    max_nodes: int,
    deadline_s: float = WALK_DEADLINE_S,
) -> DownstreamWalk:
    """Walk ``task``'s active dependents, hop by hop, within the bounds.

    Breadth-first so the first hop is complete before anything behind it is
    read: "stranded directly" is the number most worth having exactly. A
    visited set keeps a cycle from counting a task twice, and the focal task
    is never counted — a cycle back to it strands nothing new.
    """
    # Re-read the focal task's own entry first (F3): the operator is about to
    # decide on it, and the TTL alone would let a 30s-old edge list stand.
    cache.evict(task.id)
    limiter = asyncio.Semaphore(max(fetch_concurrency, 1))
    hops: dict[str, int] = {}
    visited = {task.id}
    reasons: list[str] = []
    unread: dict[str, str] = {}
    over_budget = False
    frontier: list[TaskRecord] = [task]
    hop = 0
    try:
        async with asyncio.timeout(deadline_s):
            while frontier and not over_budget:
                hop += 1
                entries, incomplete = await read_edges(lithos, frontier, cache, limiter)
                if incomplete:
                    unread.update(incomplete)
                    logger.warning(
                        "cancel consequence edge read failed",
                        extra={"task_id": task.id, "incomplete": dict(incomplete)},
                    )
                candidates = [
                    dependent
                    for entry in entries
                    for dependent in _active_dependents(
                        entry,
                        task.status if entry.task_id == task.id else "open",
                        open_index,
                    )
                ]
                found: list[TaskRecord] = []
                for dependent in candidates:
                    if dependent.id in visited:
                        continue
                    if len(hops) >= max_nodes:
                        over_budget = True
                        break
                    visited.add(dependent.id)
                    hops[dependent.id] = hop
                    found.append(dependent)
                # Ordered, so which nodes a budget stop leaves unread does not
                # depend on the order Lithos listed the edges in.
                frontier = list(_order(found))
    except TimeoutError:
        reasons.append(REASON_DEADLINE)
    if unread:
        codes = ", ".join(sorted(set(unread.values())))
        reasons.append(
            f"Lens couldn't read the dependencies of "
            f"{_plural(len(unread), 'task')} ({codes})"
        )
    if over_budget:
        reasons.append(
            f"the walk stopped at its budget of {_plural(max_nodes, 'task')}"
        )
    stranded = [open_index[task_id] for task_id in hops]
    return DownstreamWalk(
        direct=_order([row for row in stranded if hops[row.id] == 1]),
        behind=_order([row for row in stranded if hops[row.id] > 1]),
        exact=not reasons,
        bound_reasons=tuple(reasons),
    )


def _holders(status: TaskStatusRecord | None) -> tuple[ClaimHolder, ...]:
    """The task's claims grouped by agent, agents and aspects in order."""
    by_agent: dict[str, list[str]] = defaultdict(list)
    for claim in status.claims if status is not None else ():
        by_agent[claim.agent].append(claim.aspect)
    return tuple(
        ClaimHolder(agent=agent, aspects=tuple(sorted(aspects)))
        for agent, aspects in sorted(by_agent.items())
    )


async def load_cancel_consequences(
    lithos: CancelClient,
    task: TaskRecord,
    *,
    cache: GraphCache,
    fetch_concurrency: int,
    max_nodes: int,
    deadline_s: float = WALK_DEADLINE_S,
) -> CancelConsequences:
    """Read what cancelling ``task`` (an open task) strands, releases and keeps.

    Three independent reads first — the whole open list, the task's claims and
    its open children — then the walk over the open list. Never raises: each
    read that fails degrades its own part of the answer and says so.
    """
    open_read, status_read, children_read = await asyncio.gather(
        lithos.list_tasks(status="open"),
        lithos.task_status(task.id),
        lithos.task_children(task.id),
        return_exceptions=True,
    )
    claims: tuple[ClaimHolder, ...] = ()
    if isinstance(status_read, BaseException):
        logger.warning("cancel claims read failed", extra={"task_id": task.id})
    else:
        claims = _holders(status_read)
    children: tuple[TaskRecord, ...] = ()
    if isinstance(children_read, BaseException):
        logger.warning("cancel children read failed", extra={"task_id": task.id})
    else:
        children = _order([row for row in children_read if row.status == "open"])
    unavailable = ""
    walk: DownstreamWalk | None = None
    if isinstance(open_read, BaseException):
        logger.warning("cancel open-list read failed", extra={"task_id": task.id})
        unavailable = "Lens couldn't read the list of open tasks"
    else:
        try:
            walk = await walk_downstream(
                lithos,
                task,
                open_index={row.id: row for row in open_read},
                cache=cache,
                fetch_concurrency=fetch_concurrency,
                max_nodes=max_nodes,
                deadline_s=deadline_s,
            )
        except Exception:
            # read_edges already turns a failed read into `incomplete`; this
            # is the backstop that keeps a defect here from failing a cancel.
            logger.warning("cancel consequence walk failed", exc_info=True)
            unavailable = "Lens couldn't walk the task's dependents"
    return CancelConsequences(
        task=task,
        walk=walk,
        unavailable=unavailable,
        claims=claims,
        claims_unread=isinstance(status_read, BaseException),
        open_children=children,
        children_unread=isinstance(children_read, BaseException),
    )
