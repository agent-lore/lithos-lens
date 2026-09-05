"""One graph page's node and edge set, assembled over the per-task edge cache.

A **scope** is what `/tasks/graph?project=…` or `?epic=…` renders: a set of
tasks, the edges between them, the one-hop ghosts those edges reach, and —
carried in the result rather than bolted on beside it — exactly how complete
that picture is. It is a function of the master task list plus
:mod:`lithos_lens.graph_cache`, fanning out only for cache misses, so a
project page warms the cache the detail mini-graph then reads for free (D2).
There is no per-scope memoisation: the cache is per TASK precisely so that
overlapping scopes share their reads.

What the assembly decides, and why each rule is where it is:

- **Dependency edges have three states, read off BOTH endpoints** (D6).
  ``active`` is the only one that means "this blocks now" — dependent
  ``open`` and predecessor ``open`` or ``cancelled`` (a cancelled
  predecessor blocks forever; a completed one blocks nothing). ``inactive``
  carries its reason: ``satisfied`` when the predecessor completed —
  which takes PRECEDENCE, because the dependency was met whatever the
  dependent later did — else ``dependent_resolved``. ``unknown`` is an edge
  Lens cannot classify because an endpoint's status could not be read; it is
  drawn, excluded from every "blocks now" computation, and degrades the
  claims it could affect to lower bounds (A2 onward).
- **Ghosts are one hop and leaf-only** (D5). Lens never reads a ghost's own
  edges, so fan-out is bounded by the scope rather than by the corpus.
  An out-of-scope endpoint of an INACTIVE dependency edge is dropped, not
  ghosted — a completed predecessor's edge is history, and history that
  ``include_resolved`` did not ask for. Cancelled and open far endpoints are
  ghosted, because a graph that hid them would contradict the dashboard.
- **Context is added upstream only** (D6). The immediate out-of-set
  ``parent_child`` PARENT and ``discovered_from`` SOURCE of an included node
  are resolved as context ghosts; an out-of-set CHILD or FOLLOW-ON never is,
  or an epic's edges would reintroduce exactly the completed children
  ``include_resolved=0`` removed. One hop, so no grandparent walk: the
  vendored ``task_get`` carries neither parent id nor edges, and reading a
  ghost's edge list is what D5 forbids.
- **Completeness is part of the result** (D2). ``incomplete`` names every
  node whose edge read failed, ``as_of`` is the OLDEST contributing
  ``fetched_at`` so a mostly-stale graph cannot present itself as current,
  and an incomplete node is never classified isolated — Lens does not know
  that it has no edges.

Everything here is topology and freshness. Lens still never re-implements the
readiness predicate: ``lithos_task_blocked`` stays the authority on which task
is blocked and on cycle membership (D4, slice A3).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from lithos_lens.graph_cache import EdgeCacheEntry, GraphCache
from lithos_lens.task_filtering import task_projects
from lithos_lens.task_graph import EdgeRecord
from lithos_lens.task_links import (
    BLOCKER_EDGE_TYPES,
    LINK_READ_TIMEOUT_S,
    PARENT_EDGE_TYPE,
    PROVENANCE_EDGE_TYPE,
)
from lithos_lens.tasks import (
    DEFAULT_PROJECT_CONVENTION,
    DEFAULT_PROJECT_TAG_KEY,
    ProjectConvention,
    TaskRecord,
)

# Mirror the ``[lithos-lens.graph]`` config defaults so a caller with no
# tuning to do gets the shipped behaviour; ``tests/test_graph_scope.py`` pins
# the two together (the same arrangement as ``attention.AttentionPolicy``).
DEFAULT_GRAPH_MAX_TASKS = 300
DEFAULT_GRAPH_FETCH_CONCURRENCY = 16

# Per-node completeness (D2/D3), carried in the payload so the client renders
# exactly what the server computed rather than re-deriving it.
COMPLETENESS_OK = "ok"
COMPLETENESS_EDGES_UNKNOWN = "edges_unknown"
COMPLETENESS_STATUS_UNKNOWN = "status_unknown"

# Dependency-edge states and the reason an inactive one carries.
EDGE_ACTIVE = "active"
EDGE_INACTIVE = "inactive"
EDGE_UNKNOWN = "unknown"
REASON_SATISFIED = "satisfied"
REASON_DEPENDENT_RESOLVED = "dependent_resolved"

# Why a scope was refused. Each carries a count with a DIFFERENT meaning, so
# the reason travels with it rather than being inferred from the number.
#: The task set alone exceeded the guard — counted before any edge was read.
REFUSAL_TASKS = "tasks"
#: The rendered node set (tasks plus the ghosts that survived classification)
#: exceeded the guard. This is §5.7's normative rule, and its count is EXACT.
REFUSAL_NODES = "nodes"

# What a ghost is there for. A dependency ghost is on the default canvas; a
# context ghost only appears with its overlay, but is resolved on EVERY
# request because the overlays are client-side state over a static payload.
GHOST_DEPENDENCY = "dependency"
GHOST_CONTEXT = "context"

#: The status of a ghost whose ``task_get`` failed. Not a
#: :data:`~lithos_lens.tasks.TaskStatusName`: it is the absence of one, and
#: the node is SHOWN carrying it because hiding a possibly-live blocker is
#: the wrong way to err.
UNKNOWN_STATUS = "unknown"

# Context edges: never satisfied, and ghosted only towards their source.
# ``parent_child`` points parent -> child and ``discovered_from`` points
# source -> discovered, so in both cases the ghostable side is ``from``.
CONTEXT_EDGE_TYPES: tuple[str, ...] = (PARENT_EDGE_TYPE, PROVENANCE_EDGE_TYPE)


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


@dataclass(frozen=True)
class GraphScopeLimits:
    """The two ``[graph]`` knobs that bound one scope's reads."""

    max_tasks: int = DEFAULT_GRAPH_MAX_TASKS
    fetch_concurrency: int = DEFAULT_GRAPH_FETCH_CONCURRENCY


@dataclass(frozen=True)
class GraphNode:
    """One node of the assembled graph: a task, or a one-hop ghost of one.

    Read :attr:`status`, never ``node.task.status``. A ghost whose
    ``task_get`` failed has no status to record, and its placeholder record
    cannot carry one — ``TaskRecord.status`` is a three-value literal, and
    inventing a fourth value there would put a lie in the domain model rather
    than in one derived property.
    """

    task: TaskRecord
    ghost: bool = False
    #: ``dependency`` / ``context`` on a ghost, ``""`` on an in-scope node. A
    #: node reached both ways is a DEPENDENCY ghost: it is on the default
    #: canvas, and the context overlay only adds edges to it.
    ghost_kind: str = ""
    completeness: str = COMPLETENESS_OK

    @property
    def id(self) -> str:
        return self.task.id

    @property
    def status(self) -> str:
        if self.completeness == COMPLETENESS_STATUS_UNKNOWN:
            return UNKNOWN_STATUS
        return self.task.status

    @property
    def label(self) -> str:
        return self.task.title or self.task.id


@dataclass(frozen=True)
class GraphEdge:
    """One deduped edge, with the state a dependency edge carries.

    ``state`` and ``reason`` are empty on ``parent_child`` /
    ``discovered_from``: those carry no readiness meaning, so classifying
    them would invent one.
    """

    edge: EdgeRecord
    state: str = ""
    reason: str = ""

    @property
    def from_task_id(self) -> str:
        return self.edge.from_task_id

    @property
    def to_task_id(self) -> str:
        return self.edge.to_task_id

    @property
    def type(self) -> str:
        return self.edge.type

    @property
    def dependency(self) -> bool:
        return self.edge.type in BLOCKER_EDGE_TYPES

    @property
    def active(self) -> bool:
        """In the active projection — the only edges "blocks now" is read from."""
        return self.state == EDGE_ACTIVE


@dataclass(frozen=True)
class ScopeRefusal:
    """A scope Lens would not render, why, and how large it was.

    Two reasons, and only the size of the RENDERED graph ever refuses a page:

    - :data:`REFUSAL_NODES` — §5.7's guard. ``count`` is the EXACT rendered
      node count, tasks plus the ghosts that survived classification, so a
      completed out-of-scope predecessor — whose edge and endpoint are both
      dropped — never inflates it. Deciding this exactly is what the ghost
      reads are for, and no resource bound is allowed to answer it instead:
      a page inside the guard renders, whatever it cost to find out.
    - :data:`REFUSAL_TASKS` — the task set alone was already over, so no
      edge was read at all. ``count`` is a lower bound: ghosts would only
      add to it, and resolving them to say so precisely would cost the very
      fan-out the guard exists to prevent.
    """

    count: int
    max_tasks: int
    reason: str = REFUSAL_NODES

    @property
    def ghosts_counted(self) -> bool:
        """Whether ``count`` includes ghosts — only the exact node count does."""
        return self.reason == REFUSAL_NODES


@dataclass(frozen=True)
class TaskGraphScope:
    """The assembled graph, or the refusal that replaced it."""

    kind: str = ""
    key: str = ""
    include_resolved: bool = False
    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    #: In-degree-zero nodes over the fetched dependency edges. Slice A2
    #: replaces this with the CONDENSED graph's roots, which also gives a
    #: cycle — where every member has an incoming edge — a representative.
    roots: tuple[str, ...] = ()
    #: In-scope tasks with no fetched dependency edge at all. Never a task
    #: whose edge read failed (D8), and never a ghost: a ghost exists only
    #: because some edge named it.
    isolated: tuple[str, ...] = ()
    #: task_id -> why its ``edge_list`` read failed.
    incomplete: Mapping[str, str] = field(default_factory=dict)
    #: Oldest contributing ``fetched_at``; ``None`` when nothing contributed.
    as_of: datetime | None = None
    refusal: ScopeRefusal | None = None

    @property
    def refused(self) -> bool:
        return self.refusal is not None

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self.nodes)

    def node(self, task_id: str) -> GraphNode | None:
        return next((node for node in self.nodes if node.id == task_id), None)

    @property
    def dependency_edges(self) -> tuple[GraphEdge, ...]:
        return tuple(edge for edge in self.edges if edge.dependency)

    @property
    def active_edges(self) -> tuple[GraphEdge, ...]:
        return tuple(edge for edge in self.edges if edge.active)


# ── Scope membership (§5.7 / D6) ───────────────────────────────────────


def project_scope_tasks(
    master: Sequence[TaskRecord],
    project: str,
    *,
    include_resolved: bool = False,
    convention: ProjectConvention = DEFAULT_PROJECT_CONVENTION,
    tag_key: str = DEFAULT_PROJECT_TAG_KEY,
) -> tuple[TaskRecord, ...]:
    """The project's tasks per §5B.1 — open only unless ``include_resolved``.

    Pure over the master list the dashboard already loaded, which is also
    where the ``resolved_since`` window is applied: ``include_resolved``
    admits the resolved rows that list carries, it does not widen the window.
    """
    rows = [
        task
        for task in master
        if project in task_projects(task, convention=convention, tag_key=tag_key)
    ]
    if not include_resolved:
        rows = [task for task in rows if task.status == "open"]
    return _ordered(rows)


async def epic_scope_tasks(
    lithos: GraphScopeClient,
    epic_id: str,
    *,
    include_resolved: bool = True,
) -> tuple[TaskRecord, ...]:
    """The epic's recursive subtree plus the epic — closed children by default.

    The anchor is read directly (``task_get``) rather than taken from the
    master list: an epic scope must work for a closed epic, which no open
    snapshot carries, and a deep link to a deleted one deserves the coded
    not-found envelope rather than an empty graph. The anchor stays in the
    scope whatever its own status — ``include_resolved=0`` hides an epic's
    finished CHILDREN, and hiding the epic itself would leave the page
    describing a subtree with no root.
    """
    epic, children = await asyncio.gather(
        lithos.task_get(epic_id),
        lithos.task_children(epic_id, recursive=True, include_closed=True),
    )
    rows = [child for child in children if include_resolved or child.status == "open"]
    return _ordered([epic, *rows])


# ── Assembly ───────────────────────────────────────────────────────────


async def load_project_scope(
    lithos: GraphScopeClient,
    *,
    project: str,
    master: Sequence[TaskRecord],
    cache: GraphCache,
    limits: GraphScopeLimits | None = None,
    include_resolved: bool = False,
    convention: ProjectConvention = DEFAULT_PROJECT_CONVENTION,
    tag_key: str = DEFAULT_PROJECT_TAG_KEY,
) -> TaskGraphScope:
    """Assemble `/tasks/graph?project=<slug>` (open-only by default)."""
    tasks = project_scope_tasks(
        master,
        project,
        include_resolved=include_resolved,
        convention=convention,
        tag_key=tag_key,
    )
    return await assemble_scope(
        lithos,
        tasks=tasks,
        master=master,
        cache=cache,
        limits=limits,
        kind="project",
        key=project,
        include_resolved=include_resolved,
    )


async def load_epic_scope(
    lithos: GraphScopeClient,
    *,
    epic_id: str,
    master: Sequence[TaskRecord],
    cache: GraphCache,
    limits: GraphScopeLimits | None = None,
    include_resolved: bool = True,
) -> TaskGraphScope:
    """Assemble `/tasks/graph?epic=<id>` (closed children included by default)."""
    tasks = await epic_scope_tasks(lithos, epic_id, include_resolved=include_resolved)
    return await assemble_scope(
        lithos,
        tasks=tasks,
        master=master,
        cache=cache,
        limits=limits,
        kind="epic",
        key=epic_id,
        include_resolved=include_resolved,
    )


async def assemble_scope(
    lithos: GraphScopeClient,
    *,
    tasks: Sequence[TaskRecord],
    master: Sequence[TaskRecord],
    cache: GraphCache,
    limits: GraphScopeLimits | None = None,
    kind: str = "",
    key: str = "",
    include_resolved: bool = False,
) -> TaskGraphScope:
    """Fan out for cache misses and build the scope from what came back.

    ``tasks`` is the node set the scope rule chose; ``master`` is the open
    snapshot, consulted so an OPEN far endpoint costs no read (D5). One
    semaphore covers this scope's whole fan-out — both the ``edge_list``
    misses and the ghost ``task_get``s — so the bound is per render rather
    than per phase.
    """
    limits = limits or GraphScopeLimits()
    scope_tasks = _ordered(tasks)

    def refused(count: int, reason: str) -> TaskGraphScope:
        return TaskGraphScope(
            kind=kind,
            key=key,
            include_resolved=include_resolved,
            refusal=ScopeRefusal(
                count=count, max_tasks=limits.max_tasks, reason=reason
            ),
        )

    if len(scope_tasks) > limits.max_tasks:
        # Refuse BEFORE spending one read per node: ghosts could only add to a
        # count that is already over, so no read can change the answer.
        return refused(len(scope_tasks), REFUSAL_TASKS)

    limiter = asyncio.Semaphore(limits.fetch_concurrency)
    in_scope = {task.id: task for task in scope_tasks}
    entries, incomplete = await _read_edges(lithos, scope_tasks, cache, limiter)
    edges = _dedupe_scope_edges(entries)

    far_ids = _ghostable_endpoints(edges, in_scope)
    far_tasks, unresolved = await _resolve_far_endpoints(
        lithos, far_ids, master, limiter
    )
    graph_edges, ghost_kinds = _classify_edges(edges, in_scope, far_tasks, unresolved)
    nodes = _build_nodes(scope_tasks, incomplete, far_tasks, unresolved, ghost_kinds)
    # §5.7's guard, on the EXACT rendered node set: tasks plus the ghosts that
    # actually survived classification, which is what "ghosts counted" means.
    if len(nodes) > limits.max_tasks:
        return refused(len(nodes), REFUSAL_NODES)

    return TaskGraphScope(
        kind=kind,
        key=key,
        include_resolved=include_resolved,
        nodes=nodes,
        edges=graph_edges,
        roots=_roots(nodes, graph_edges),
        isolated=_isolated(scope_tasks, graph_edges, incomplete),
        incomplete=incomplete,
        as_of=min((entry.fetched_at for entry in entries), default=None),
    )


async def _read_edges(
    lithos: GraphScopeClient,
    tasks: Sequence[TaskRecord],
    cache: GraphCache,
    limiter: asyncio.Semaphore,
) -> tuple[tuple[EdgeCacheEntry, ...], dict[str, str]]:
    """One cache read per node; failures become ``incomplete``, not silence."""

    async def fetch(task_id: str) -> list[EdgeRecord]:
        async with limiter:
            # Deadlined inside the gate, as the detail page's fan-out is: a
            # read that never answers would otherwise hold one of the few
            # slots for as long as the session stays half-open.
            return await asyncio.wait_for(
                lithos.task_edge_list(task_id, direction="both"), LINK_READ_TIMEOUT_S
            )

    results = await asyncio.gather(
        *(cache.edges_for(task.id, fetch) for task in tasks),
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


def _dedupe_scope_edges(entries: Sequence[EdgeCacheEntry]) -> tuple[EdgeRecord, ...]:
    """Merge every entry's edges, deduped and stripped of ``direction``.

    Each edge is returned by BOTH of its endpoints, with ``direction``
    relative to whichever task was asked — so the field is meaningless once
    the two lists are merged, and is cleared rather than left to be misread.
    ``from``/``to`` already carry the direction that matters.
    """
    seen: set[tuple[str, str, str]] = set()
    merged: list[EdgeRecord] = []
    for entry in entries:
        for edge in entry.edges:
            key = (edge.from_task_id, edge.to_task_id, edge.type)
            if key in seen or not edge.from_task_id or not edge.to_task_id:
                continue
            seen.add(key)
            merged.append(replace(edge, direction=""))
    return tuple(sorted(merged, key=lambda e: (e.from_task_id, e.to_task_id, e.type)))


def _ghostable_endpoints(
    edges: Sequence[EdgeRecord], in_scope: Mapping[str, TaskRecord]
) -> tuple[str, ...]:
    """Out-of-set endpoints that could become a ghost — in edge order.

    Dependency edges may ghost either side (the far endpoint's own status
    then decides whether the edge survives at all); context edges may ghost
    only their ``from`` side, which is the parent or the provenance source.
    Everything else — an out-of-set child, an out-of-set follow-on, an
    unknown future edge type — names no ghost, so its edge is dropped in
    :func:`_classify_edges` rather than pulling a node in.
    """
    # The list keeps the documented order; the set does the membership test.
    # An `in list` here would be O(edges x candidates) of uninterruptible CPU
    # on the event-loop thread, over an edge set whose size Lithos does not
    # cap — a stall that no deadline or render gate can shed, because there is
    # no await inside the loop.
    candidates: list[str] = []
    seen: set[str] = set()
    for edge in edges:
        far = _far_endpoint(edge, in_scope)
        if far and far not in seen:
            seen.add(far)
            candidates.append(far)
    return tuple(candidates)


def _far_endpoint(edge: EdgeRecord, in_scope: Mapping[str, TaskRecord]) -> str:
    """The ghostable out-of-set endpoint of ``edge``, or ``""``."""
    from_in = edge.from_task_id in in_scope
    to_in = edge.to_task_id in in_scope
    if from_in == to_in:
        # Both in scope (nothing to ghost) or neither (an edge no node in
        # this scope reported — nothing anchors it, so it is not drawn).
        return ""
    if edge.type in BLOCKER_EDGE_TYPES:
        return edge.to_task_id if from_in else edge.from_task_id
    if edge.type in CONTEXT_EDGE_TYPES and to_in:
        return edge.from_task_id
    return ""


async def _resolve_far_endpoints(
    lithos: GraphScopeClient,
    far_ids: Sequence[str],
    master: Sequence[TaskRecord],
    limiter: asyncio.Semaphore,
) -> tuple[dict[str, TaskRecord], set[str]]:
    """Resolve EVERY ghost candidate: the open snapshot first, ``task_get`` after.

    An OPEN far endpoint is already in the master list, so the common case —
    a live cross-project blocker — costs no read at all (D5). Only resolved
    (or absent) endpoints need one, and one that FAILS leaves the id in the
    returned ``unresolved`` set: the ghost is still shown, with
    ``status unknown``, and every dependency edge touching it is ``unknown``.

    Every candidate is read, without a count bound, and that is a decision
    rather than an omission. The classification each read decides is not
    cosmetic — it is drop-versus-ghost (D5/D6), so an endpoint left unread
    would render as an ``unknown`` ghost where the contract requires a
    completed predecessor's edge to be ABSENT, and could push a page that must
    render past the size guard. A cap here cannot be made semantics-preserving:
    the reads ARE the classification. So the honest statement is that this
    fan-out is bounded in CONCURRENCY (the scope's semaphore) and in the
    DURATION of each read (:data:`~lithos_lens.task_links.LINK_READ_TIMEOUT_S`,
    applied inside the gate), and not in total — the total is
    ``|out-of-set endpoints this scope's edges name that are not on the open
    list|``, which is chosen by whoever wrote those edges. The upstream answer
    is a bulk graph read (ROADMAP ledger gap #3), which removes the per-node
    fan-out entirely; the mitigation available here is the page size guard,
    which bounds the EDGE phase that produces these candidates.
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

    async def read(task_id: str) -> TaskRecord:
        async with limiter:
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
    return resolved, unresolved


def _classify_edges(
    edges: Sequence[EdgeRecord],
    in_scope: Mapping[str, TaskRecord],
    far_tasks: Mapping[str, TaskRecord],
    unresolved: set[str],
) -> tuple[tuple[GraphEdge, ...], dict[str, str]]:
    """State every dependency edge, drop what is out of scope AND inactive.

    Returns the kept edges plus the ghost kind each out-of-set endpoint
    earned, so a node that no surviving edge names is never materialised.
    """
    kept: list[GraphEdge] = []
    ghost_kinds: dict[str, str] = {}
    for edge in edges:
        far = _far_endpoint(edge, in_scope)
        if far == "" and not (
            edge.from_task_id in in_scope and edge.to_task_id in in_scope
        ):
            continue
        if edge.type in BLOCKER_EDGE_TYPES:
            state, reason = dependency_edge_state(
                _status_of(edge.from_task_id, in_scope, far_tasks, unresolved),
                _status_of(edge.to_task_id, in_scope, far_tasks, unresolved),
            )
            if far and state == EDGE_INACTIVE:
                # History pointing out of the scope: the completed
                # predecessor's edge is dropped rather than ghosted, and
                # returns — faded — under ``include_resolved`` once its
                # endpoint is IN the node set.
                continue
            kept.append(GraphEdge(edge=edge, state=state, reason=reason))
            if far:
                ghost_kinds[far] = GHOST_DEPENDENCY
        elif edge.type in CONTEXT_EDGE_TYPES:
            kept.append(GraphEdge(edge=edge))
            if far:
                ghost_kinds.setdefault(far, GHOST_CONTEXT)
        elif not far:
            # An unknown future edge type between two in-scope nodes still
            # renders (transport keeps what it does not understand); one
            # reaching outside has no ghosting rule and would invent one.
            kept.append(GraphEdge(edge=edge))
    return tuple(kept), ghost_kinds


def dependency_edge_state(
    predecessor_status: str, dependent_status: str
) -> tuple[str, str]:
    """Classify one ``blocks`` / ``waits_on_gate`` edge from both endpoints.

    ``satisfied`` deliberately precedes ``dependent_resolved``: when a
    completed predecessor meets a resolved dependent, the dependency WAS met,
    whatever the dependent went on to do.
    """
    if UNKNOWN_STATUS in (predecessor_status, dependent_status):
        return EDGE_UNKNOWN, ""
    if predecessor_status == "completed":
        return EDGE_INACTIVE, REASON_SATISFIED
    if dependent_status in ("completed", "cancelled"):
        return EDGE_INACTIVE, REASON_DEPENDENT_RESOLVED
    if dependent_status == "open" and predecessor_status in ("open", "cancelled"):
        return EDGE_ACTIVE, ""
    # Neither endpoint is unknown and no rule above fired: a status Lens does
    # not model (a future one, or a malformed record). Unknown is the honest
    # answer — it is excluded from "blocks now" and says so.
    return EDGE_UNKNOWN, ""


def _status_of(
    task_id: str,
    in_scope: Mapping[str, TaskRecord],
    far_tasks: Mapping[str, TaskRecord],
    unresolved: set[str],
) -> str:
    if task_id in in_scope:
        return in_scope[task_id].status
    if task_id in far_tasks:
        return far_tasks[task_id].status
    # Either a ghost whose ``task_get`` failed (it is in ``unresolved``) or an
    # endpoint nothing resolved at all. Both are the same claim: Lens does not
    # know this endpoint's status, so it classifies no edge through it.
    return UNKNOWN_STATUS


def _build_nodes(
    scope_tasks: Sequence[TaskRecord],
    incomplete: Mapping[str, str],
    far_tasks: Mapping[str, TaskRecord],
    unresolved: set[str],
    ghost_kinds: Mapping[str, str],
) -> tuple[GraphNode, ...]:
    nodes = [
        GraphNode(
            task=task,
            completeness=(
                COMPLETENESS_EDGES_UNKNOWN if task.id in incomplete else COMPLETENESS_OK
            ),
        )
        for task in scope_tasks
    ]
    ghosts = [
        GraphNode(
            task=far_tasks.get(ghost_id) or TaskRecord(id=ghost_id, title=""),
            ghost=True,
            ghost_kind=kind,
            completeness=(
                COMPLETENESS_STATUS_UNKNOWN
                if ghost_id in unresolved
                else COMPLETENESS_OK
            ),
        )
        for ghost_id, kind in ghost_kinds.items()
    ]
    return tuple(nodes) + tuple(
        sorted(ghosts, key=lambda node: (node.task.created_at, node.id))
    )


def _roots(nodes: Sequence[GraphNode], edges: Sequence[GraphEdge]) -> tuple[str, ...]:
    """Nodes no dependency edge points at — the layout's starting rank."""
    dependents = {edge.to_task_id for edge in edges if edge.dependency}
    return tuple(node.id for node in nodes if node.id not in dependents)


def _isolated(
    scope_tasks: Sequence[TaskRecord],
    edges: Sequence[GraphEdge],
    incomplete: Mapping[str, str],
) -> tuple[str, ...]:
    """In-scope tasks no dependency edge touches, in any state.

    A task whose edge read FAILED is never here (D8): "no dependency edges"
    is a claim, and Lens has no evidence for it. Hierarchy and provenance do
    not rescue a task from isolation — a task hanging off its epic and
    nothing else is exactly what the disclosure exists to fold away.
    """
    attached = {edge.from_task_id for edge in edges if edge.dependency} | {
        edge.to_task_id for edge in edges if edge.dependency
    }
    return tuple(
        task.id
        for task in scope_tasks
        if task.id not in attached and task.id not in incomplete
    )


def _ordered(tasks: Iterable[TaskRecord]) -> tuple[TaskRecord, ...]:
    """Deduped by id and ordered by (created_at, id) — the graph's tie-break.

    The same ordering D7 breaks longest-chain ties by, applied once here so
    every downstream list (layers, cycle members, the payload) inherits it.
    """
    by_id: dict[str, TaskRecord] = {}
    for task in tasks:
        by_id.setdefault(task.id, task)
    return tuple(sorted(by_id.values(), key=lambda task: (task.created_at, task.id)))


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
