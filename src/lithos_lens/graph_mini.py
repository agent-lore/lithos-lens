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

Two rules here differ from the graph page's and are stated rather than
inherited:

- **There are no ghosts.** Every node is a task the neighbourhood named, drawn
  as itself; depth-1 dependents and depth-2 blockers are leaves because the
  scope STOPS there, not because Lens could not read them. Only a node whose
  own ``task_get`` failed carries ``status unknown``, and only one whose
  ``edge_list`` failed carries ``edges unknown`` — the same two markers, for
  the same two reasons, as everywhere else.
- **Depth 2 is enumerated from EVERY depth-1 blocker**, including the ones
  the cap cut and the one an earlier TIER drew (a parent epic may also block
  its own child — Lithos puts no type restriction on ``blocks``). The tail
  counts the whole neighbourhood D11 defines, so a blocker that is only
  counted, or drawn as the hierarchy node, still contributes its own blockers
  to the remainder; enumerating only the drawn blockers would quietly shrink
  the number an operator reads. What that costs is one cached ``edge_list``
  per depth-1 blocker — a frontier that already costs one record read apiece
  — and the RECORDS behind depth 2 are read only when a slot could still hold
  one.

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

from lithos_lens.epic_strip import EPIC_TASK_TYPE
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
    PARENT_BREADCRUMB_MAX_DEPTH,
    PARENT_EDGE_TYPE,
    PageTail,
)
from lithos_lens.tasks import (
    DEFAULT_PROJECT_CONVENTION,
    DEFAULT_PROJECT_TAG_KEY,
    ProjectConvention,
    TaskRecord,
    parse_timestamp,
)

#: Mirrors the ``[lithos-lens.graph]`` default, like ``graph_scope``'s two do.
DEFAULT_GRAPH_MINI_GRAPH_MAX_NODES = 40

#: This scope's kind, in the payload the client reads. Neither ``project`` nor
#: ``epic``: a mini-graph is one TASK's neighbourhood, and a payload claiming
#: a project scope would invite a client to treat it as the project's graph.
SCOPE_TASK = "task"

#: Why the parent-epic tier is absent when Lens could not DECIDE it — never
#: because the task has none. An absent hierarchy node looks the same either
#: way, so the fragment states which of the two happened (D11's tier is a
#: promise, and silence about a promise reads as "there is nothing there").
PARENT_EPIC_DEPTH = "depth"
PARENT_EPIC_CYCLE = "cycle"
PARENT_EPIC_UNREADABLE = "unreadable"

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
    #: Empty when the hierarchy tier is settled — the epic is drawn, or the
    #: chain provably holds none. Otherwise one of :data:`PARENT_EPIC_DEPTH`,
    #: :data:`PARENT_EPIC_CYCLE` or :data:`PARENT_EPIC_UNREADABLE`, and the
    #: fragment says the epic could not be determined rather than showing the
    #: same empty space a task with no epic gets.
    parent_epic_unknown: str = ""
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
    epic, epic_entries, parent_unknown = await _parent_epic(
        lithos,
        edges,
        focal.id,
        edges_unreadable=focal.id in incomplete,
        cache=cache,
        limiter=limiter,
        tally=tally,
        known=known,
        records=records,
        unknown=unknown,
    )
    # The DEPTH-1 BLOCKER FRONTIER, before any tier claims it. Tier membership
    # answers "which node does this id get drawn as", and depth 2 asks a
    # different question — "whose blockers are depth 2?" — whose answer is
    # every task with a blocker edge into the focal, however it is drawn. The
    # two were one list, and the case that separates them is real: Lithos puts
    # no type restriction on `blocks`, so a parent epic may also block its own
    # child. The parent tier claimed that epic, the blocker list lost it, and
    # its own blockers were never read or counted — silently absent from a
    # picture that reported no remainder (round-5 correctness f-007).
    frontier = _neighbours(edges, focal.id, BLOCKER_EDGE_TYPES, up=True)
    parents = _fresh((epic,) if epic else (), claimed)
    blockers = _fresh(frontier, claimed)
    dependents = _fresh(
        _neighbours(edges, focal.id, BLOCKER_EDGE_TYPES, up=False), claimed
    )
    await _resolve(
        lithos, (*frontier, *dependents), known, records, unknown, limiter, tally
    )

    # Depth 2 is read from EVERY depth-1 blocker, not only from the ones that
    # fit: the tail counts the whole neighbourhood D11 defines, so a blocker
    # the cap cut still contributes its own blockers to the remainder. The
    # reads are bounded by the depth-1 frontier, which already costs one record
    # read apiece.
    deeper_entries, deeper_incomplete = await read_edges(
        lithos,
        [_record(records, blocker) for blocker in frontier],
        cache,
        limiter,
        tally,
    )
    incomplete = {**incomplete, **deeper_incomplete}
    edges = _index([edges.values(), *(entry.edges for entry in deeper_entries)])
    deeper = _fresh(
        [
            blocker_of_blocker
            for blocker in frontier
            for blocker_of_blocker in _neighbours(
                edges, blocker, BLOCKER_EDGE_TYPES, up=True
            )
        ],
        claimed,
    )

    selected = [focal.id]
    room = max(0, limits.max_nodes - 1)
    # Every candidate the rule names, drawn or not — what the tail's total is
    # counted from, so the remainder is the whole of what is not shown.
    total = 1 + len(parents) + len(blockers) + len(dependents) + len(deeper)
    for tier in (
        _ordered(parents, records),
        _ordered(blockers, records),
        _ordered(dependents, records),
    ):
        taken = tier[:room]
        selected.extend(taken)
        room -= len(taken)
    if room and deeper:
        # The last tier, and the only one whose RECORDS wait on a slot being
        # left for it: with the cap already spent, depth 2 is counted rather
        # than drawn, and reading a record apiece would be a fan-out nothing
        # renders.
        await _resolve(lithos, deeper, known, records, unknown, limiter, tally)
        selected.extend(_ordered(deeper, records)[:room])

    scope = _scope(
        task_id=focal.id,
        selected=selected,
        records=records,
        unknown=unknown,
        edges=edges,
        incomplete=incomplete,
        entries=(*entries, *epic_entries, *deeper_entries),
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
            shown=len(selected) - 1,
            total=total - 1,
            # EXPLICIT, including zero: a cap of one leaves no room beside the
            # focal task, and a tail that fell back to the detail page's
            # 25-row default there would print "the first 25 are listed above"
            # over a picture showing none of them.
            size=max(0, limits.max_nodes - 1),
        ),
        focus_url=(
            graph_url(
                GraphPageParams(kind=SCOPE_PROJECT, key=projects[0], focus=focal.id)
            )
            if projects
            else ""
        ),
        parent_epic_unknown=parent_unknown,
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


async def _parent_epic(
    lithos: GraphScopeClient,
    edges: Mapping[tuple[str, str, str], EdgeRecord],
    focal_id: str,
    *,
    edges_unreadable: bool = False,
    cache: GraphCache,
    limiter: asyncio.Semaphore,
    tally: CacheTally,
    known: Mapping[str, TaskRecord],
    records: dict[str, TaskRecord],
    unknown: set[str],
) -> tuple[str, tuple[EdgeCacheEntry, ...], str]:
    """The nearest ANCESTOR EPIC, walking ``parent_child`` up from the focal.

    D11's hierarchy tier is "the parent epic", and an immediate parent is not
    one: Lithos's hierarchy is a forest of TASKS (``epic`` is a task type, not
    a level), so ``epic -> middle -> focal`` is a legal shape in which the
    immediate parent is a plain task. Taking it would both label a task as the
    epic and leave the real one off the picture, so the chain is walked — one
    hop at a time through the same cache, bounded by
    ``task_links.PARENT_BREADCRUMB_MAX_DEPTH`` and by a seen-set, which is the
    bound and the cycle guard the detail page's own breadcrumb walk uses.

    An epic further up than the immediate parent is drawn as a labelled node
    with no edge to it: the tasks BETWEEN them are not members of this scope
    (D11 asks for one node, not the chain), and an edge straight from the epic
    to the focal would be a relation Lithos never wrote.

    Exactly ONE ending means "this task has no parent epic": the chain runs
    out above the focal without passing one. The other three —
    :data:`PARENT_EPIC_DEPTH` (the walk's safety bound, which the detail
    page's breadcrumb shares), :data:`PARENT_EPIC_CYCLE` (a ``parent_child``
    loop in what the contract calls a forest) and
    :data:`PARENT_EPIC_UNREADABLE` (an ancestor's ``task_get`` or
    ``edge_list`` failed, or ``edges_unreadable`` — the FOCAL task's own edge
    list, which is where the first parent edge would have been) — mean Lens
    could not DECIDE, and they are returned as the third value rather than
    collapsed into the first. An absent node is the same picture either way,
    so the fragment says which of the two it is (round-2 correctness f-002); a
    walk that swallowed the difference would report a task with an epic as a
    task without one.

    Which of the four it is, is decided ON THE PENDING ANCESTOR and nowhere
    else — including after the last hop the bound allows. A walk that spent
    its final hop proving there is nothing above has ANSWERED, and reporting
    "deeper than this reads" there would invent an unknown; one that spent it
    arriving back at a task it already visited has found a cycle, not a depth
    (round-3 correctness f-002). Only an ancestor left genuinely unexplored is
    the bound's own outcome.
    """
    if edges_unreadable:
        # The focal task's edge list is where its parent edge lives, so a read
        # that failed leaves the whole tier unknowable — not absent.
        return "", (), PARENT_EPIC_UNREADABLE
    entries: list[EdgeCacheEntry] = []
    seen = {focal_id}
    # Lithos enforces a single parent, so the first is the chain; a second one
    # would be a forest violation and this tier is not a place to render it.
    above = _neighbours(edges, focal_id, (PARENT_EDGE_TYPE,), up=True)
    ancestor = above[0] if above else ""
    # One extra turn over the bound, which classifies and never walks: the
    # ancestor the last hop left pending gets the same three-way reading as
    # every other one.
    for hop in range(PARENT_BREADCRUMB_MAX_DEPTH + 1):
        if not ancestor:
            # Off the top of the forest: an answer, and the only one that
            # means this task genuinely has no epic above it.
            return "", tuple(entries), ""
        if ancestor in seen:
            return "", tuple(entries), PARENT_EPIC_CYCLE
        if hop == PARENT_BREADCRUMB_MAX_DEPTH:
            # An unexplored ancestor with no hops left: the bound, and the one
            # ending it may claim.
            return "", tuple(entries), PARENT_EPIC_DEPTH
        seen.add(ancestor)
        await _resolve(lithos, (ancestor,), known, records, unknown, limiter, tally)
        record = records.get(ancestor)
        if record is None:
            return "", tuple(entries), PARENT_EPIC_UNREADABLE
        if record.task_type == EPIC_TASK_TYPE:
            return ancestor, tuple(entries), ""
        step, failed = await read_edges(lithos, (record,), cache, limiter, tally)
        entries.extend(step)
        if failed:
            # The chain above this ancestor is unreadable, so whether an epic
            # sits on it is unknowable — not "no".
            return "", tuple(entries), PARENT_EPIC_UNREADABLE
        parents = _neighbours(
            _index(entry.edges for entry in step),
            ancestor,
            (PARENT_EDGE_TYPE,),
            up=True,
        )
        ancestor = parents[0] if parents else ""
    # Unreachable: the loop's own head answers every exit above.
    return "", tuple(entries), PARENT_EPIC_DEPTH


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
    # Anything an earlier phase already read — the ancestor walk and the
    # neighbour tiers overlap when one task both parents and blocks this one —
    # is answered from what is in hand rather than read a second time.
    warm = {task_id: records[task_id] for task_id in pending if task_id in records}
    resolved.update(warm)
    pending = tuple(task_id for task_id in pending if task_id not in warm)
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

    The timestamp is NORMALISED to UTC before it is compared, exactly as
    ``graph_layout``'s own ordering does it: two legal ISO stamps written at
    different offsets order lexically the wrong way round
    (``2026-09-01T00:30:00+01:00`` is BEFORE ``2026-09-01T00:00:00+00:00``),
    and at the cap that decides which task is drawn and which is only counted.

    A candidate whose record could not be read — or whose stamp cannot be
    parsed — has no time to sort by and takes the empty string, which is where
    that same ordering puts an unknown timestamp too.
    """

    def key(task_id: str) -> tuple[str, str]:
        record = records.get(task_id)
        parsed = parse_timestamp(record.created_at) if record is not None else None
        return (parsed.isoformat() if parsed is not None else "", task_id)

    return sorted(candidates, key=key)


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
