"""Bounded reads over one task's neighbours: a first page, then a visible tail.

Everything the task-detail page says about a task's NEIGHBOURS — the level-1
blocker chain, spawn provenance in both directions, the parent breadcrumb —
is resolved here, because they share one hazard. A task's edge set is written
by agents and Lithos enforces no maximum edge count, so "show every neighbour
with its live status" is a per-render ``lithos_task_get`` fan-out whose SIZE
is chosen by whatever wrote the graph. The realistic source of a runaway edge
set is a buggy agent, not a hostile one, which is exactly why the bound below
has to stay legible rather than silently clip.

That fan-out is not the same order of work as the one ``lithos_task_edge_list``
call that produced the ids. That call is ONE round trip returning N rows,
parsed locally in O(N). Resolving the ids is N round trips, each a
request/response exchange multiplexed onto the single process-wide MCP session
every other page render shares — N times the latency and N times the session
contention. Nothing upstream caps it either: ``session.call_tool`` takes no
per-call timeout, ``_SESSION_WAIT_TIMEOUT_S`` covers only session
establishment, and uvicorn sets no request deadline. So the fan-out is bounded
here, twice over:

- **Count** — only the first :data:`LINK_PAGE_SIZE` neighbours are resolved,
  and the remainder renders as a tail (``templates/tasks/link_page.html``)
  that SAYS how many more there are. A silently clipped "why can't this run?"
  list is worse than a slow one, so the tail follows the dashboard's
  frontier-truncation precedent: an accuracy banner, not a disappearance.
- **Concurrency** — at most :data:`LINK_FANOUT_CONCURRENCY` of those reads are
  in flight at once. The two cover different dimensions and neither replaces
  the other: a count bound alone still lets one render dump a whole page onto
  the session in a single gather, and a concurrency bound alone leaves the
  TOTAL work — and so the render's duration — unbounded.

Together they make one detail render's neighbour fan-out statically bounded:
at most ``LINK_PAGE_SIZE`` reads per page loaded, plus at most
``2 * PARENT_BREADCRUMB_MAX_DEPTH`` for the breadcrumb walk.

T1-S8 expands an unfinished blocker one level deeper on demand. It resolves
each new level through :func:`load_link_page` too, so the page size, the
remainder count and the tail markup have exactly ONE definition however deep
the chain goes — reimplementing them per level would reintroduce this defect
one level down.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple, Protocol, cast

from lithos_lens.task_graph import EdgeRecord
from lithos_lens.tasks import TaskRecord

# Edge types that stop a task running. Both point blocker -> blocked
# (``blocks``: the predecessor must complete; ``waits_on_gate``: the gate must
# resolve), so a task's blockers are its INCOMING edges of these types.
BLOCKER_EDGE_TYPES: tuple[str, ...] = ("blocks", "waits_on_gate")

# ``discovered_from`` points source -> discovered, so a task's INCOMING edge of
# this type is where it came from and its OUTGOING ones are the follow-ons it
# spawned. Non-blocking either way: this is provenance, not a dependency.
PROVENANCE_EDGE_TYPE = "discovered_from"

# ``parent_child`` points parent -> child, and Lithos guarantees a single-parent
# forest, so walking INCOMING edges of this type is a simple chain to the root.
PARENT_EDGE_TYPE = "parent_child"

GATE_TASK_TYPE = "gate"

# How many neighbours one page resolves with live status. Not a ceiling on how
# many a task may HAVE — the remainder is counted and rendered as a tail — so
# it restricts the render, never the input domain. Set far above any
# hand-authored blocker set (production's deepest is a handful) so the tail is
# a runaway signal rather than routine chrome.
LINK_PAGE_SIZE = 25

# How many of a page's reads may be in flight at once. The same figure as
# ``epic_strip.EPIC_FANOUT_BATCH`` and for the same reason: it is this repo's
# answer to "how many reads may contend for the shared MCP session at once".
# Deliberately BELOW the page size, so it actually binds instead of being a
# formality the page bound already covers.
LINK_FANOUT_CONCURRENCY = 8

# How far the parent walk climbs before it stops and says so. The forest is
# shallow in practice (epic -> task); this bounds the SEQUENTIAL read chain the
# walk would otherwise inherit from the graph's depth.
PARENT_BREADCRUMB_MAX_DEPTH = 10

# Both constants above are internal, not config: they are safety nets rather
# than dials operators tune, the same call as ``EPIC_FANOUT_BATCH`` and
# ``knowledge.RELATED_RENDER_CAP``.


class TaskLinkClient(Protocol):
    """The narrow client surface the neighbour reads need."""

    async def task_get(self, task_id: str) -> TaskRecord: ...

    async def task_edge_list(
        self,
        task_id: str,
        *,
        direction: str = "both",
        types: list[str] | None = None,
    ) -> list[EdgeRecord]: ...


class LinkTarget(NamedTuple):
    """One neighbour id to resolve, with the edge type that named it."""

    task_id: str
    edge_type: str


@dataclass(frozen=True)
class LinkedTask:
    """One neighbour of a task, carrying the live status actually read for it.

    ``unresolved`` marks a neighbour whose ``lithos_task_get`` failed. The line
    still renders — the edge says the blocker exists, and dropping it would
    understate the set the operator is asking about — but it makes no status
    claim.
    """

    task_id: str
    edge_type: str = ""
    title: str = ""
    status: str = ""
    task_type: str = ""
    gate_type: str = ""
    unresolved: bool = False

    @property
    def label(self) -> str:
        """Display text: the title, falling back to the bare id."""
        return self.title or self.task_id

    @property
    def satisfied(self) -> bool:
        """True when this neighbour no longer holds the task back."""
        return self.status == "completed"

    @property
    def unsatisfiable(self) -> bool:
        """A cancelled predecessor can never complete — a dead end."""
        return self.status == "cancelled"


@dataclass(frozen=True)
class LinkPage:
    """One bounded page of a task's neighbours, plus the size of its tail.

    ``links`` are the neighbours whose live status was read; ``total`` is how
    many the edge list reported. The two differ exactly when the edge set is
    larger than ``page_size``, which is what :attr:`remaining` reports and the
    tail renders. Keeping ``total`` (rather than a bare truncation flag) is the
    point: the operator gets to see how many more blockers exist.
    """

    links: tuple[LinkedTask, ...] = ()
    total: int = 0
    page_size: int = LINK_PAGE_SIZE

    @property
    def remaining(self) -> int:
        return max(0, self.total - len(self.links))

    @property
    def truncated(self) -> bool:
        return self.remaining > 0


@dataclass(frozen=True)
class Breadcrumb:
    """The parent chain above a task, root FIRST.

    ``incomplete`` means the walk stopped before reaching a root — the depth
    bound was hit, a read failed, or the "forest" turned out to contain a cycle
    — so the trail renders a leading ellipsis instead of implying the first
    entry is the root.
    """

    ancestors: tuple[TaskRecord, ...] = ()
    incomplete: bool = False


def gate_type_of(task: TaskRecord) -> str:
    """The ``metadata.gate_type`` of a gate (human/timer/ci/pr/external_task).

    Empty for anything that is not a gate: ``gate_type`` is required only of
    gates, so reading it off a plain task would render a badge from stray
    metadata.
    """
    if task.task_type != GATE_TASK_TYPE:
        return ""
    return str(task.metadata.get("gate_type") or "")


def incoming_targets(
    task_id: str,
    edges: Sequence[EdgeRecord],
    types: Sequence[str],
) -> tuple[LinkTarget, ...]:
    """Neighbours on edges pointing AT ``task_id`` (``other -> task_id``).

    Selected on the endpoint ids rather than the payload's ``direction`` label.
    ``direction`` is a server-computed convenience field that
    :func:`~lithos_lens.task_graph.normalize_edge` defaults to empty when
    absent, and a detail page that answers "no blockers" because a field went
    missing is the failure this page exists to prevent.
    """
    return tuple(
        LinkTarget(edge.from_task_id, edge.type)
        for edge in edges
        if edge.type in types and edge.to_task_id == task_id and edge.from_task_id
    )


def outgoing_targets(
    task_id: str,
    edges: Sequence[EdgeRecord],
    types: Sequence[str],
) -> tuple[LinkTarget, ...]:
    """Neighbours on edges pointing AWAY from ``task_id`` (``task_id -> other``)."""
    return tuple(
        LinkTarget(edge.to_task_id, edge.type)
        for edge in edges
        if edge.type in types and edge.from_task_id == task_id and edge.to_task_id
    )


def new_link_limiter() -> asyncio.Semaphore:
    """A fan-out limiter for ONE render.

    Share a single limiter across every :func:`load_link_page` call in a render
    and the bound applies to that render's whole neighbour fan-out rather than
    to each page separately.
    """
    return asyncio.Semaphore(LINK_FANOUT_CONCURRENCY)


async def load_link_page(
    lithos: TaskLinkClient,
    targets: Sequence[LinkTarget],
    *,
    page_size: int = LINK_PAGE_SIZE,
    limiter: asyncio.Semaphore | None = None,
) -> LinkPage:
    """Read the live status of the first ``page_size`` targets; count the rest.

    THE bounded fan-out, and the only place a neighbour list decides how much
    of itself to resolve. It issues at most ``page_size`` ``lithos_task_get``
    calls whatever ``targets`` holds, at most :data:`LINK_FANOUT_CONCURRENCY`
    of them at a time, and reports ``total`` so the caller's tail can name the
    remainder instead of hiding it.

    Callable for a blocker set at any level: T1-S8 passes the blockers of an
    expanded blocker and gets the same page, the same bound and the same tail.
    """
    if not targets:
        return LinkPage(total=0, page_size=page_size)
    gate = limiter or new_link_limiter()
    page = tuple(targets[:page_size])

    async def resolve(target: LinkTarget) -> Any:
        async with gate:
            return await lithos.task_get(target.task_id)

    results = await asyncio.gather(
        *(resolve(target) for target in page), return_exceptions=True
    )
    return LinkPage(
        links=tuple(
            _linked_task(target, result)
            for target, result in zip(page, results, strict=True)
        ),
        total=len(targets),
        page_size=page_size,
    )


async def load_parent_breadcrumb(
    lithos: TaskLinkClient,
    task_id: str,
    edges: Sequence[EdgeRecord],
    *,
    max_depth: int = PARENT_BREADCRUMB_MAX_DEPTH,
) -> Breadcrumb:
    """Walk ``parent_child`` upward from a task towards its root.

    Sequential by nature — a level's parent is only known once that level has
    been read — so the read chain inherits the graph's depth unless something
    stops it. ``max_depth`` does, and a seen-set catches a cycle: the
    single-parent, acyclic forest makes both belt-and-braces rather than
    load-bearing, but this walk is driven by agent-written edges and a cycle
    here would be an unbounded request loop rather than a wrong picture.

    Any of those stops (and a failed read) sets ``incomplete``, so the trail
    never implies its first entry is the root when it is not.
    """
    ancestors: list[TaskRecord] = []
    seen = {task_id}
    parents = incoming_targets(task_id, edges, (PARENT_EDGE_TYPE,))
    incomplete = False
    while parents:
        # A single-parent forest, so the first incoming parent_child edge IS
        # the parent; a second would be an upstream invariant violation and is
        # ignored rather than branching the trail.
        parent_id = parents[0].task_id
        if parent_id in seen or len(ancestors) >= max_depth:
            incomplete = True
            break
        seen.add(parent_id)
        try:
            parent = await lithos.task_get(parent_id)
            parent_edges = await lithos.task_edge_list(
                parent_id, direction="incoming", types=[PARENT_EDGE_TYPE]
            )
        except Exception:
            incomplete = True
            break
        ancestors.append(parent)
        parents = incoming_targets(parent_id, parent_edges, (PARENT_EDGE_TYPE,))
    ancestors.reverse()
    return Breadcrumb(ancestors=tuple(ancestors), incomplete=incomplete)


def _linked_task(target: LinkTarget, result: Any) -> LinkedTask:
    if isinstance(result, BaseException):
        return LinkedTask(
            task_id=target.task_id, edge_type=target.edge_type, unresolved=True
        )
    task = cast(TaskRecord, result)
    return LinkedTask(
        task_id=task.id or target.task_id,
        edge_type=target.edge_type,
        title=task.title,
        status=task.status,
        task_type=task.task_type,
        gate_type=gate_type_of(task),
    )
