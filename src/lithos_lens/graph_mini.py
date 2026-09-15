"""The task-detail mini-graph: two hops up, one down (T2-A5).

`/tasks/{task_id}/minigraph` is a SCOPE like the graph page's, over the same
per-task edge cache (D2) and serialised through the same payload builder (D3),
so the detail page's picture and the graph page's are drawn by one client
module from one vocabulary. What differs is the membership rule and what
bounds it, and both are D11's:

- **Two up, one down.** Incoming ``blocks``/``waits_on_gate`` to depth 2,
  outgoing to depth 1, the parent epic as a single labelled node, and no
  ``discovered_from`` at all. The mini-graph answers "why can't this run, and
  what does finishing it free" — provenance answers neither, and the detail
  page already renders it as text.
- **A cap that counts the focal task.** ``[graph].mini_graph_max_nodes`` (40)
  is the whole picture, filled in one deterministic priority — focal, parent,
  depth-1 blockers, depth-1 dependents, depth-2 blockers, each tier in
  (``created_at``, ``id``) order — so which 40 nodes an operator sees does not
  depend on which edge Lithos happened to return first. The remainder is
  reported through T1's shared tail rather than clipped silently, and the
  focus link opens the full project graph on this task.

Two rules here are narrower than the graph page's and are stated rather than
inherited:

- **There are no ghosts.** Every node is a task the neighbourhood named, drawn
  as itself; depth-1 dependents and depth-2 blockers are leaves because the
  scope STOPS there, not because Lens could not read them. Only a node whose
  own ``task_get`` failed carries ``status unknown``, and only one whose
  ``edge_list`` failed carries ``edges unknown`` — the same two markers, for
  the same two reasons, as everywhere else.
- **Depth-2 is enumerated from the depth-1 blockers that are DRAWN.** A
  blocker the cap cut is not on the picture, so its own blockers are not part
  of it either — and reading them would be fan-out spent on nodes no tier can
  reach. That keeps the tail's total exact for what this rule admits: it
  counts every candidate the rule names, drawn or not, rather than a number
  that would need an unbounded read to state.

No cycle signal is read here. Cycle membership is Lithos's verdict from a
SCOPED ``task_blocked`` read (D4), and a per-task fragment has no scope to
make it with; the mini-graph draws the shape its edges show and claims
nothing about membership, which is what leaving ``flagged`` unset means.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime

from lithos_lens.graph_cache import (
    CacheTally,
    EdgeCacheEntry,
    GraphCache,
    dedupe_edges,
)
from lithos_lens.graph_fanout import (
    GHOST_RESOLUTION_BUDGET_S,
    GraphScopeClient,
    partition_far_endpoints,
    read_edges,
    resolve_far_endpoints,
)
from lithos_lens.graph_layout import BlockingChain, Topology, build_topology
from lithos_lens.graph_page import graph_url
from lithos_lens.graph_scope import (
    COMPLETENESS_EDGES_UNKNOWN,
    COMPLETENESS_OK,
    COMPLETENESS_STATUS_UNKNOWN,
    DEFAULT_GRAPH_FETCH_CONCURRENCY,
    UNKNOWN_STATUS,
    GraphEdge,
    GraphNode,
    TaskGraphScope,
    dependency_edge_state,
)
from lithos_lens.graph_snapshot import lower_bound_nodes
from lithos_lens.graph_view import (
    OVERLAY_HIERARCHY,
    SCOPE_PROJECT,
    GraphPageParams,
    LayerGroup,
    LayerView,
    NodeView,
    payload_json,
)
from lithos_lens.task_filtering import task_projects
from lithos_lens.task_graph import EdgeRecord
from lithos_lens.task_links import (
    BLOCKER_EDGE_TYPES,
    PARENT_EDGE_TYPE,
    PageTail,
)
from lithos_lens.tasks import (
    DEFAULT_PROJECT_CONVENTION,
    DEFAULT_PROJECT_TAG_KEY,
    ProjectConvention,
    TaskRecord,
)

#: Mirrors the ``[lithos-lens.graph]`` default, like ``graph_scope``'s two do.
DEFAULT_GRAPH_MINI_GRAPH_MAX_NODES = 40

#: This scope's kind, in the payload the client reads. Neither ``project`` nor
#: ``epic``: a mini-graph is one TASK's neighbourhood, and a payload claiming
#: a project scope would invite a client to treat it as the project's graph.
SCOPE_TASK = "task"

#: The edge types a mini-graph draws. Dependencies, plus the one hierarchy
#: edge that carries the parent epic; ``discovered_from`` is excluded by D11
#: and its absence here is what excludes it.
MINI_EDGE_TYPES: tuple[str, ...] = (*BLOCKER_EDGE_TYPES, PARENT_EDGE_TYPE)


@dataclass(frozen=True)
class MiniGraphLimits:
    """What one mini-graph render may draw, and what it may spend doing it."""

    max_nodes: int = DEFAULT_GRAPH_MINI_GRAPH_MAX_NODES
    fetch_concurrency: int = DEFAULT_GRAPH_FETCH_CONCURRENCY


@dataclass(frozen=True)
class MiniGraphView:
    """One rendered mini-graph: its nodes, its payload, and its honesty."""

    task_id: str
    nodes: tuple[NodeView, ...] = ()
    edge_types: tuple[str, ...] = ()
    payload_json: str = "{}"
    #: The cap's remainder, through the detail page's one tail template.
    tail: PageTail = PageTail()
    #: `/tasks/graph?project=<slug>&focus=<id>`, or empty for a task that
    #: belongs to no project — a graph needs a scope, and inventing one for a
    #: projectless task would link to a page that cannot hold it.
    focus_url: str = ""
    #: The OLDEST contributing fetch, the same staleness bound the page states.
    as_of: datetime | None = None
    #: task_id -> why its ``edge_list`` read failed.
    incomplete: Mapping[str, str] = field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0
    ghost_reads: int = 0

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def capped(self) -> bool:
        return self.tail.truncated


async def load_mini_graph(
    lithos: GraphScopeClient,
    task_id: str,
    *,
    master: Sequence[TaskRecord] = (),
    cache: GraphCache,
    limits: MiniGraphLimits | None = None,
    convention: ProjectConvention = DEFAULT_PROJECT_CONVENTION,
    tag_key: str = DEFAULT_PROJECT_TAG_KEY,
) -> MiniGraphView:
    """Assemble the mini-graph for one task (D11).

    ``master`` is the open snapshot, consulted first for every neighbour for
    the reason D5 consults it: an OPEN neighbour is already on it, so the
    common case costs no ``task_get`` at all. One semaphore covers this
    render's whole fan-out — the cached ``edge_list`` misses and the neighbour
    reads together — so the bound is per render rather than per phase.

    Raises whatever the focal ``task_get`` raises: a mini-graph for a task
    Lens cannot read is not a smaller mini-graph, and the route answers with
    the fragment's own error markup rather than a picture of nothing.
    """
    limits = limits or MiniGraphLimits()
    limiter = asyncio.Semaphore(limits.fetch_concurrency)
    tally = CacheTally()
    known = {task.id: task for task in master if task.status == "open"}

    records: dict[str, TaskRecord] = {}
    unknown: set[str] = set()
    focal = known.get(task_id) or await lithos.task_get(task_id)
    records[focal.id] = focal

    entries, incomplete = await read_edges(lithos, (focal,), cache, limiter, tally)
    edges = _index(entry.edges for entry in entries)

    # First tier to name an id owns it: a task that both blocks this one and
    # waits on it (a two-cycle) is ONE node, drawn in the tier D11 fills first.
    claimed = {focal.id}
    parents = _fresh(
        _neighbours(edges, focal.id, (PARENT_EDGE_TYPE,), up=True), claimed
    )
    blockers = _fresh(
        _neighbours(edges, focal.id, BLOCKER_EDGE_TYPES, up=True), claimed
    )
    dependents = _fresh(
        _neighbours(edges, focal.id, BLOCKER_EDGE_TYPES, up=False), claimed
    )
    await _resolve(
        lithos,
        (*parents, *blockers, *dependents),
        known,
        records,
        unknown,
        limiter,
        tally,
    )

    selected = [focal.id]
    room = max(0, limits.max_nodes - 1)
    total = 1
    # "The parent epic as a single labelled node" — one, even in the forest
    # violation where an id has two, because the tier is the PARENT and not a
    # hierarchy walk.
    ordered_blockers = _ordered(blockers, records)
    tiers = (
        _ordered(parents, records)[:1],
        ordered_blockers,
        _ordered(dependents, records),
    )
    for tier in tiers:
        total += len(tier)
        taken = tier[:room]
        selected.extend(taken)
        room -= len(taken)

    drawn_blockers = [blocker for blocker in ordered_blockers if blocker in selected]
    deeper_entries, deeper_incomplete = await read_edges(
        lithos,
        [_record(records, blocker) for blocker in drawn_blockers],
        cache,
        limiter,
        tally,
    )
    incomplete = {**incomplete, **deeper_incomplete}
    edges = _index([edges.values(), *(entry.edges for entry in deeper_entries)])
    deeper = _fresh(
        [
            blocker_of_blocker
            for blocker in drawn_blockers
            for blocker_of_blocker in _neighbours(
                edges, blocker, BLOCKER_EDGE_TYPES, up=True
            )
        ],
        claimed,
    )
    await _resolve(lithos, deeper, known, records, unknown, limiter, tally)
    total += len(deeper)
    selected.extend(_ordered(deeper, records)[:room])

    scope = _scope(
        task_id=focal.id,
        selected=selected,
        records=records,
        unknown=unknown,
        edges=edges,
        incomplete=incomplete,
        entries=(*entries, *deeper_entries),
        tally=tally,
    )
    topology = build_topology(
        [node.task for node in scope.nodes],
        [edge.edge for edge in scope.edges],
        incomplete=incomplete,
        unknown_status=unknown,
    )
    views = _node_views(scope, topology, focus=focal.id, tag_key=tag_key)
    params = GraphPageParams(
        kind=SCOPE_TASK,
        key=focal.id,
        focus=focal.id,
        # The parent edge is a HIERARCHY edge, and the graph page hides that
        # overlay by default (D8). Here the parent epic is a member of the
        # scope by decision, so the overlay it hangs on is on by decision too
        # — otherwise the node D11 asks for would be drawn with no edge to it.
        overlays=(OVERLAY_HIERARCHY,),
        # Nothing is folded away in a picture this small: an isolate here is
        # the focal task with no neighbours, which is the answer, not clutter.
        show_isolated=True,
    )
    projects = task_projects(focal, convention=convention, tag_key=tag_key)
    return MiniGraphView(
        task_id=focal.id,
        nodes=tuple(views.values()),
        edge_types=tuple(
            edge_type
            for edge_type in MINI_EDGE_TYPES
            if any(edge.type == edge_type for edge in scope.edges)
        ),
        payload_json=payload_json(
            scope,
            topology,
            BlockingChain(),
            views,
            _layers(topology, views),
            params,
            (),
        ),
        # The tail counts NEIGHBOURS, not nodes: the cap is 40 including the
        # focal task (D11), but the sentence the operator reads is about the
        # tasks around this one — "this task has 60 related tasks in all; the
        # first 39 are listed above" is true of a 40-node picture, while
        # counting the focal task into either figure would make it off by one.
        # The remainder is the same number either way, which is the figure the
        # cap is actually accountable for.
        tail=PageTail(
            shown=len(selected) - 1, total=total - 1, size=limits.max_nodes - 1
        ),
        focus_url=(
            graph_url(
                GraphPageParams(kind=SCOPE_PROJECT, key=projects[0], focus=focal.id)
            )
            if projects
            else ""
        ),
        as_of=scope.as_of,
        incomplete=incomplete,
        cache_hits=tally.hits,
        cache_misses=tally.misses,
        ghost_reads=tally.ghost_reads,
    )


# ── Membership ─────────────────────────────────────────────────────────


def _index(
    edge_lists: Iterable[Iterable[EdgeRecord]],
) -> dict[tuple[str, str, str], EdgeRecord]:
    """Every edge read so far, deduped by ``(from, to, type)``.

    Keyed rather than listed because the merge happens twice (the focal read,
    then the depth-1 blockers') and an edge both endpoints reported must not
    become two. ``direction`` is dropped for the reason
    ``graph_cache.dedupe_edges`` gives: it is relative to whichever task was
    asked, so it means nothing once two tasks' lists are merged.
    """
    merged: dict[tuple[str, str, str], EdgeRecord] = {}
    for edges in edge_lists:
        for edge in dedupe_edges(tuple(edges)):
            if not edge.from_task_id or not edge.to_task_id:
                continue
            merged.setdefault(
                (edge.from_task_id, edge.to_task_id, edge.type),
                EdgeRecord(
                    from_task_id=edge.from_task_id,
                    to_task_id=edge.to_task_id,
                    type=edge.type,
                    metadata=dict(edge.metadata),
                    created_by=edge.created_by,
                    created_at=edge.created_at,
                ),
            )
    return merged


def _neighbours(
    edges: Mapping[tuple[str, str, str], EdgeRecord],
    task_id: str,
    types: Sequence[str],
    *,
    up: bool,
) -> tuple[str, ...]:
    """The far endpoints of ``task_id``'s edges of ``types``, in edge order.

    ``up`` reads INCOMING edges — blockers, and the parent, since both
    ``blocks`` and ``parent_child`` point predecessor/parent -> task — and
    ``up=False`` the outgoing ones, which are the dependents.
    """
    found: list[str] = []
    for edge in edges.values():
        if edge.type not in types:
            continue
        near, far = (
            (edge.to_task_id, edge.from_task_id)
            if up
            else (
                edge.from_task_id,
                edge.to_task_id,
            )
        )
        if near == task_id and far != task_id and far not in found:
            found.append(far)
    return tuple(found)


def _fresh(candidates: Sequence[str], claimed: set[str]) -> tuple[str, ...]:
    """Ids no earlier tier took, claiming them for this one as it goes."""
    taken: list[str] = []
    for candidate in candidates:
        if candidate in claimed:
            continue
        claimed.add(candidate)
        taken.append(candidate)
    return tuple(taken)


async def _resolve(
    lithos: GraphScopeClient,
    candidates: Sequence[str],
    known: Mapping[str, TaskRecord],
    records: dict[str, TaskRecord],
    unknown: set[str],
    limiter: asyncio.Semaphore,
    tally: CacheTally,
) -> None:
    """Fill ``records`` for every candidate, recording the reads that failed.

    Every candidate is read, because the record is what decides the node's
    status, its label and its place in its tier's (``created_at``, ``id``)
    order — a candidate left unread has no defensible position in either. The
    phase carries the same deadline the scope's ghost resolution does, and a
    read that misses it leaves its task ``status unknown`` rather than absent:
    dropping a possibly-live blocker is the wrong direction to err (D6), and
    the marker says exactly what happened.
    """
    resolved, pending = partition_far_endpoints(candidates, tuple(known.values()))
    # The gather is cancelled with the deadline, so `resolved` holds whatever
    # landed before it; the rest fall through as unknown below, which is the
    # same answer a failed read gets.
    with suppress(TimeoutError):
        await asyncio.wait_for(
            resolve_far_endpoints(lithos, pending, resolved, limiter, tally),
            GHOST_RESOLUTION_BUDGET_S,
        )
    records.update(resolved)
    unknown.update(candidate for candidate in candidates if candidate not in resolved)


def _record(records: Mapping[str, TaskRecord], task_id: str) -> TaskRecord:
    """The record for an id, or the placeholder a failed read leaves behind."""
    return records.get(task_id) or TaskRecord(id=task_id, title="")


def _ordered(candidates: Sequence[str], records: Mapping[str, TaskRecord]) -> list[str]:
    """One tier in (``created_at``, ``id``) order — D11's within-tier rule.

    A candidate whose record could not be read has no ``created_at`` to sort
    by and takes the empty string, which is where ``graph_layout``'s own
    ordering puts an unknown timestamp too.
    """
    return sorted(
        candidates,
        key=lambda task_id: (
            records[task_id].created_at if task_id in records else "",
            task_id,
        ),
    )


# ── The scope, and the views over it ───────────────────────────────────


def _scope(
    *,
    task_id: str,
    selected: Sequence[str],
    records: Mapping[str, TaskRecord],
    unknown: set[str],
    edges: Mapping[tuple[str, str, str], EdgeRecord],
    incomplete: Mapping[str, str],
    entries: Sequence[EdgeCacheEntry],
    tally: CacheTally,
) -> TaskGraphScope:
    """The selected ids as a scope: nodes, classified edges, completeness.

    Only edges BETWEEN selected nodes survive, so the cap never leaves an
    arrow pointing at a node the picture does not draw. A dependency edge is
    classified from both endpoints exactly as the page's is
    (:func:`~lithos_lens.graph_scope.dependency_edge_state`) — an inactive one
    is kept and drawn faded, because the detail page's own chain keeps a
    completed predecessor too and the two must not disagree.
    """
    inside = set(selected)
    nodes = tuple(
        GraphNode(
            task=_record(records, node_id),
            completeness=(
                COMPLETENESS_STATUS_UNKNOWN
                if node_id in unknown
                else COMPLETENESS_EDGES_UNKNOWN
                if node_id in incomplete
                else COMPLETENESS_OK
            ),
        )
        for node_id in _ordered(list(selected), records)
    )

    def status(node_id: str) -> str:
        if node_id in unknown or node_id not in records:
            return UNKNOWN_STATUS
        return records[node_id].status

    kept: list[GraphEdge] = []
    for edge in edges.values():
        if edge.type not in MINI_EDGE_TYPES:
            continue
        if edge.from_task_id not in inside or edge.to_task_id not in inside:
            continue
        if edge.type in BLOCKER_EDGE_TYPES:
            state, reason = dependency_edge_state(
                status(edge.from_task_id), status(edge.to_task_id)
            )
            kept.append(GraphEdge(edge=edge, state=state, reason=reason))
        else:
            kept.append(GraphEdge(edge=edge))
    return TaskGraphScope(
        kind=SCOPE_TASK,
        key=task_id,
        nodes=nodes,
        edges=tuple(
            sorted(
                kept,
                key=lambda edge: (edge.from_task_id, edge.to_task_id, edge.type),
            )
        ),
        incomplete=dict(incomplete),
        as_of=min((entry.fetched_at for entry in entries), default=None),
        cache_hits=tally.hits,
        cache_misses=tally.misses,
        ghost_reads=tally.ghost_reads,
    )


def _node_views(
    scope: TaskGraphScope,
    topology: Topology,
    *,
    focus: str,
    tag_key: str,
) -> dict[str, NodeView]:
    """One :class:`NodeView` per node — the same markers, minus the verdicts.

    ``flagged`` and ``cycle_unknown`` stay unset here and that is a decision,
    not an omission: both are Lithos's cycle verdict, which comes from a
    scoped ``task_blocked`` read this fragment does not make (D4). ``cycle_id``
    is Lens's own shape and is carried, so a loop these edges show is still
    bracketed by the client.
    """
    layer_of: dict[str, int] = {}
    cycle_of: dict[str, str] = {}
    via_cycle: set[str] = set()
    for condensation in topology.condensations:
        for member in condensation.members:
            layer_of[member] = condensation.layer
            if condensation.cycle is not None:
                cycle_of[member] = condensation.cycle.id
            if condensation.blocked_via_cycle:
                via_cycle.add(member)
    bounded = lower_bound_nodes(scope)
    return {
        node.id: NodeView(
            id=node.id,
            label=node.label,
            status=node.status,
            task_type=node.task.task_type,
            layer=layer_of.get(node.id, 0),
            projects=task_projects(node.task, convention="both", tag_key=tag_key),
            completeness=node.completeness,
            claims=tuple(claim.agent for claim in node.task.claims or ()),
            cycle_id=cycle_of.get(node.id, ""),
            blocked_via_cycle=node.id in via_cycle,
            focused=node.id == focus,
            bound=node.id in bounded,
        )
        for node in scope.nodes
    }


def _layers(topology: Topology, views: Mapping[str, NodeView]) -> tuple[LayerView, ...]:
    """The condensed layers, in the payload's shape.

    The mini-graph renders no layer TEXT of its own (D11: its baseline is the
    blocker chain plus the Blocks line, already on the page). These exist so
    the payload states the same ranks the graph page's does, which is what
    lets one client module place both pictures.
    """
    return tuple(
        LayerView(
            index=index,
            groups=tuple(
                LayerGroup(
                    id=condensation.id,
                    members=tuple(
                        views[member]
                        for member in condensation.members
                        if member in views
                    ),
                    cycle=condensation.cycle,
                    blocked_via_cycle=condensation.blocked_via_cycle,
                )
                for condensation in topology.condensations
                if condensation.layer == index
            ),
        )
        for index in range(len(topology.layers))
    )
