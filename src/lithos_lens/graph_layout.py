"""Graph topology over a fetched scope: cycles, layers, chain, hierarchy (T2-A2).

Pure functions over the transport records this package already has —
``EdgeRecord`` for the fetched edges, ``TaskRecord`` for the node set,
``BlockedTaskRecord``/``BlockerRecord`` for Lithos's own cycle verdict. Nothing
here reads Lithos: the scope assembly (``graph_scope``) fetches, this module
says what the fetched shape IS, which is what makes every rule below testable
one fixture at a time.

The division of labour with Lithos is T2's D4 and it is not symmetric.
**Membership of a cycle is Lithos's verdict** — a ``kind="cycle"`` blocker on a
``lithos_task_blocked`` row — and this module never overrides it: a task Lithos
flags is a cycle member here even when Tarjan finds no strongly connected
component, because a cycle running through two out-of-scope tasks
(``A(in) -> B(ghost) -> C(ghost) -> A``) is invisible to a scope that never
fetches a ghost's edges (D5). **The SHAPE is Lens's** — the members, their
order, the representative path — because that is topology over an edge set, not
readiness, and Lens still never re-implements the readiness predicate.

Three answers, three deliberately different edge sets, each because of what it
claims. Cycles and layers use EVERY fetched dependency edge, active or not,
because layers are the planned sequence and a satisfied edge is still a fact
about the plan. The longest chain uses the ACTIVE projection only (D6/D7),
because "blocking" is a claim about now. The hierarchy tree uses
``parent_child``, which carries no readiness meaning and is never satisfied.

Everything this module emits is ordered by ``(created_at, id)`` and every
adjacency it walks is sorted the same way, so the render is a function of the
graph and not of the order the edges happened to arrive in.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from lithos_lens.task_graph import BlockedTaskRecord, EdgeRecord
from lithos_lens.task_links import BLOCKER_EDGE_TYPES, PARENT_EDGE_TYPE
from lithos_lens.tasks import TaskRecord, parse_timestamp

# D6's dependency edges: the only two types that carry readiness meaning, so
# the only two that make a cycle, a layer or a chain. Same tuple the detail
# page's blocker walk uses — "what stops a task running" has one definition in
# this codebase, not one per surface.
DEPENDENCY_EDGE_TYPES: tuple[str, ...] = BLOCKER_EDGE_TYPES

# The ``lithos_task_blocked`` blocker kind that IS Lithos's cycle verdict (see
# task_graph.KNOWN_BLOCKER_KINDS).
CYCLE_BLOCKER_KIND = "cycle"

# A node whose own status could not be read — a ghost whose ``task_get``
# failed (D2). Not a Lithos status: ``TaskRecord.status`` cannot hold it, which
# is why the callers pass those ids separately.
STATUS_UNKNOWN = "unknown"

EdgeState = Literal["active", "inactive", "unknown"]
InactiveReason = Literal["satisfied", "dependent_resolved"]
ChainBound = Literal["exact", "lower_bound"]

# Statuses of a predecessor that still constrain a dependent. A cancelled
# predecessor blocks forever (T1's unsatisfiable case); a completed one blocks
# nothing.
_BLOCKING_PREDECESSOR_STATUSES = frozenset({"open", "cancelled"})


@dataclass(frozen=True)
class DependencyEdge:
    """One fetched ``blocks``/``waits_on_gate`` edge, classified per D6.

    ``from_task_id`` is the predecessor and ``to_task_id`` the dependent: both
    types point blocker -> blocked. ``reason`` is set only when ``state`` is
    ``inactive``, and ``satisfied`` takes precedence over
    ``dependent_resolved`` — a completed predecessor MET the dependency,
    whatever the dependent then did, and reporting the dependent's own
    resolution instead would hide that.
    """

    from_task_id: str
    to_task_id: str
    type: str
    state: EdgeState
    reason: str = ""


@dataclass(frozen=True)
class Cycle:
    """A cycle in the scope: Lithos's verdict, or Lens's own SCC, or both.

    ``members`` are ordered by ``(created_at, id)``. ``path`` is ONE
    representative walk of the cycle — a DFS from the smallest member over
    adjacency sorted the same way, closing back on that member — not an
    enumeration of every cycle through the component. It is empty when
    ``scc`` is false: a Lithos-flagged member with no component in the fetched
    topology closes its loop through tasks this scope never fetched, so Lens
    has no path to draw and ``message`` (Lithos's own text) is all there is.
    """

    id: str
    members: tuple[str, ...]
    path: tuple[str, ...] = ()
    # Tarjan found this component in the fetched topology, so Lens can draw it.
    scc: bool = False
    # Lithos reported a ``kind="cycle"`` blocker on at least one member.
    flagged: bool = False
    message: str = ""


@dataclass(frozen=True)
class Condensation:
    """One node of the condensed graph: a cycle's members, or a lone task.

    ``id`` is the representative task id — the smallest member by
    ``(created_at, id)`` — so a condensation is addressable by an id the
    payload and the canvas already carry, and ``roots`` can name a cyclic
    condensation without inventing a synthetic node id.

    ``blocked_via_cycle`` marks a condensation that is not itself a cycle but
    sits downstream of one. That marker is the whole reason cycle members are
    condensed rather than dropped: a cycle with no layer takes its dependents
    off the page with it, and work that vanishes is worse than work that is
    labelled unreachable.
    """

    id: str
    members: tuple[str, ...]
    layer: int = 0
    cycle: Cycle | None = None
    blocked_via_cycle: bool = False
    # The representative's own ``created_at``, carried so the chain's
    # tie-breaks order condensations by the same ``(created_at, id)`` as
    # everything else without re-reading the task records.
    created_at: str = ""


@dataclass(frozen=True)
class BlockingChain:
    """The longest blocking chain, by node count, over the active projection.

    ``nodes`` are condensation ids in order, so a cyclic condensation counts
    once however many members it holds. ``bound`` is ``lower_bound`` whenever
    the answer could only be longer than it says — see
    :func:`longest_blocking_chain` for the two ways that happens — and the
    caller renders ">= N" rather than "N" for it.
    """

    nodes: tuple[str, ...] = ()
    bound: ChainBound = "exact"

    @property
    def length(self) -> int:
        return len(self.nodes)


@dataclass(frozen=True)
class HierarchyRow:
    """One line of the indented ``parent_child`` tree."""

    task_id: str
    depth: int = 0
    has_children: bool = False


@dataclass(frozen=True)
class Topology:
    """The condensed shape of one fetched scope.

    ``layers`` holds condensation ids, layer by layer; ``condensations`` is the
    same set flattened in render order (layer, then ``(created_at, id)``).
    """

    nodes: tuple[str, ...] = ()
    condensations: tuple[Condensation, ...] = ()
    layers: tuple[tuple[str, ...], ...] = ()
    cycles: tuple[Cycle, ...] = ()
    roots: tuple[str, ...] = ()
    edges: tuple[DependencyEdge, ...] = ()
    incomplete: frozenset[str] = frozenset()

    def condensation_of(self, task_id: str) -> Condensation | None:
        """The condensation ``task_id`` belongs to, by id OR by membership."""
        for condensation in self.condensations:
            if task_id == condensation.id or task_id in condensation.members:
                return condensation
        return None


def _sort_key(task_id: str, created_at: str) -> tuple[str, str]:
    """The one ordering in this module: ``(created_at, id)``.

    Timestamps are normalized to UTC before comparison so two differently
    offset ISO strings order chronologically rather than lexically; an absent
    or unreadable one sorts first and falls back to the id, which keeps a ghost
    with no known creation time in a stable place instead of a random one.
    """
    parsed = parse_timestamp(created_at)
    return (parsed.isoformat() if parsed is not None else "", task_id)


def _dedupe_edges(
    edges: Sequence[EdgeRecord], types: Collection[str]
) -> list[EdgeRecord]:
    """Edges of the given types, one per ``(from, to, type)``.

    The cache fetches ``direction="both"`` per task, so an edge between two
    in-scope tasks arrives twice — harmless to the shape, but it would draw
    twice in the payload.
    """
    seen: set[tuple[str, str, str]] = set()
    kept: list[EdgeRecord] = []
    for edge in edges:
        if edge.type not in types or not edge.from_task_id or not edge.to_task_id:
            continue
        key = (edge.from_task_id, edge.to_task_id, edge.type)
        if key in seen:
            continue
        seen.add(key)
        kept.append(edge)
    return kept


def _status_map(
    tasks: Sequence[TaskRecord],
    edges: Sequence[EdgeRecord],
    unknown_status: Collection[str],
) -> dict[str, str]:
    """id -> status for every node, including endpoints not in ``tasks``.

    An endpoint the scope supplied no record for gets ``unknown`` rather than
    an assumed status: an assumed ``open`` would make its edges active and
    inflate the chain, an assumed ``completed`` would hide a live blocker.
    """
    statuses = {task.id: task.status for task in tasks}
    for edge in edges:
        for endpoint in (edge.from_task_id, edge.to_task_id):
            if endpoint and endpoint not in statuses:
                statuses[endpoint] = STATUS_UNKNOWN
    for task_id in unknown_status:
        statuses[task_id] = STATUS_UNKNOWN
    return statuses


def classify_dependency_edges(
    edges: Sequence[EdgeRecord],
    tasks: Sequence[TaskRecord],
    *,
    unknown_status: Collection[str] = (),
) -> tuple[DependencyEdge, ...]:
    """Classify the fetched dependency edges from BOTH endpoints (D6).

    ``active`` — the dependent is open and the predecessor is open or
    cancelled. ``inactive`` — the predecessor is completed (``satisfied``), or
    the dependent is completed/cancelled (``dependent_resolved``); satisfied
    wins when both hold. ``unknown`` — either endpoint's status could not be
    read, in which case Lens cannot classify the edge in EITHER direction and
    says so, rather than guessing an answer every downstream claim would
    inherit.
    """
    statuses = _status_map(tasks, edges, unknown_status)
    classified: list[DependencyEdge] = []
    for edge in _dedupe_edges(edges, DEPENDENCY_EDGE_TYPES):
        predecessor = statuses.get(edge.from_task_id, STATUS_UNKNOWN)
        dependent = statuses.get(edge.to_task_id, STATUS_UNKNOWN)
        state: EdgeState
        reason = ""
        if STATUS_UNKNOWN in (predecessor, dependent):
            state = "unknown"
        elif predecessor == "completed":
            state, reason = "inactive", "satisfied"
        elif dependent != "open":
            state, reason = "inactive", "dependent_resolved"
        elif predecessor in _BLOCKING_PREDECESSOR_STATUSES:
            state = "active"
        else:  # pragma: no cover - every status is covered by the branches above
            state = "unknown"
        classified.append(
            DependencyEdge(
                from_task_id=edge.from_task_id,
                to_task_id=edge.to_task_id,
                type=edge.type,
                state=state,
                reason=reason,
            )
        )
    return tuple(classified)


def _adjacency(
    nodes: Sequence[str],
    edges: Sequence[DependencyEdge],
    key: Mapping[str, tuple[str, str]],
) -> dict[str, tuple[str, ...]]:
    """predecessor -> dependents, each list sorted by ``(created_at, id)``.

    Sorting here is what makes Tarjan's component order and the representative
    path independent of the order the edges arrived in.
    """
    out: dict[str, list[str]] = {node: [] for node in nodes}
    for edge in edges:
        out[edge.from_task_id].append(edge.to_task_id)
    return {
        node: tuple(sorted(set(targets), key=lambda t: key[t]))
        for node, targets in out.items()
    }


def _strongly_connected(
    nodes: Sequence[str], adjacency: Mapping[str, tuple[str, ...]]
) -> list[list[str]]:
    """Tarjan's SCC, iterative so a deep chain cannot exhaust the stack."""
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0
    for root in nodes:
        if root in index_of:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, cursor = work[-1]
            if cursor == 0:
                index_of[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            descended = False
            neighbours = adjacency.get(node, ())
            while cursor < len(neighbours):
                child = neighbours[cursor]
                cursor += 1
                if child not in index_of:
                    work[-1] = (node, cursor)
                    work.append((child, 0))
                    descended = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index_of[child])
            if descended:
                continue
            work.pop()
            if low[node] == index_of[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return components


def _representative_path(
    start: str, members: Collection[str], adjacency: Mapping[str, tuple[str, ...]]
) -> tuple[str, ...]:
    """One walk from ``start`` back to ``start`` inside the component.

    DFS over the same sorted adjacency, so the path is the graph's, not the
    input order's. A self-loop yields ``(A, A)``; a component always has such a
    walk, so the bare ``(start,)`` fallback is unreachable for a real SCC and
    is there to keep the function total.
    """
    member_set = set(members)
    inside = {
        node: tuple(n for n in adjacency.get(node, ()) if n in member_set)
        for node in members
    }
    path = [start]
    visited = {start}
    cursors = [0]
    while path:
        neighbours = inside.get(path[-1], ())
        cursor = cursors[-1]
        if cursor >= len(neighbours):
            visited.discard(path.pop())
            cursors.pop()
            continue
        cursors[-1] = cursor + 1
        nxt = neighbours[cursor]
        if nxt == start:
            return (*path, start)
        if nxt in visited:
            continue
        visited.add(nxt)
        path.append(nxt)
        cursors.append(0)
    return (start,)


def _flagged_cycle_members(blocked: Sequence[BlockedTaskRecord]) -> dict[str, str]:
    """Lithos's cycle verdict: task id -> the blocker's own message."""
    flagged: dict[str, str] = {}
    for record in blocked:
        for blocker in record.blockers:
            if blocker.kind == CYCLE_BLOCKER_KIND and record.task.id not in flagged:
                flagged[record.task.id] = blocker.message
    return flagged


def _condense(
    nodes: Sequence[str],
    adjacency: Mapping[str, tuple[str, ...]],
    key: Mapping[str, tuple[str, str]],
    flagged: Mapping[str, str],
) -> tuple[list[tuple[str, tuple[str, ...], Cycle | None]], dict[str, str]]:
    """Group nodes into condensations and say which of them are cycles.

    Every SCC of size > 1 — and every self-loop — is one condensation. Every
    other node is its own, and a Lithos-flagged one among them is condensed
    alone AS A CYCLE, so it still receives a layer and its dependents are still
    layered below it and marked (D4).
    """
    groups: list[tuple[str, tuple[str, ...], Cycle | None]] = []
    member_of: dict[str, str] = {}
    for component in _strongly_connected(nodes, adjacency):
        members = tuple(sorted(component, key=lambda node: key[node]))
        representative = members[0]
        self_loop = len(members) == 1 and representative in adjacency.get(
            representative, ()
        )
        cycle: Cycle | None = None
        if len(members) > 1 or self_loop:
            flagged_members = [m for m in members if m in flagged]
            cycle = Cycle(
                id=representative,
                members=members,
                path=_representative_path(representative, members, adjacency),
                scc=True,
                flagged=bool(flagged_members),
                message=flagged.get(flagged_members[0], "") if flagged_members else "",
            )
        elif representative in flagged:
            cycle = Cycle(
                id=representative,
                members=members,
                scc=False,
                flagged=True,
                message=flagged[representative],
            )
        groups.append((representative, members, cycle))
        for member in members:
            member_of[member] = representative
    groups.sort(key=lambda group: key[group[0]])
    return groups, member_of


def _condensed_edges(
    edges: Sequence[DependencyEdge], member_of: Mapping[str, str]
) -> list[tuple[str, str]]:
    """Deduped condensation-level edges, dropping the ones inside a group."""
    return sorted(
        {
            (member_of[edge.from_task_id], member_of[edge.to_task_id])
            for edge in edges
            if member_of[edge.from_task_id] != member_of[edge.to_task_id]
        }
    )


def _layer(
    group_ids: Sequence[str],
    condensed: Sequence[tuple[str, str]],
    key: Mapping[str, tuple[str, str]],
) -> dict[str, int]:
    """Kahn over the condensed graph: layer = longest path from any root.

    The condensation is by SCC of this very edge set, so the condensed graph is
    a DAG and every node dequeues exactly once.
    """
    successors: dict[str, list[str]] = {group: [] for group in group_ids}
    indegree = dict.fromkeys(group_ids, 0)
    for predecessor, dependent in condensed:
        successors[predecessor].append(dependent)
        indegree[dependent] += 1
    layers = dict.fromkeys(group_ids, 0)
    roots = sorted((g for g in group_ids if not indegree[g]), key=lambda g: key[g])
    queue = deque(roots)
    while queue:
        group = queue.popleft()
        for dependent in successors[group]:
            layers[dependent] = max(layers[dependent], layers[group] + 1)
            indegree[dependent] -= 1
            if not indegree[dependent]:
                queue.append(dependent)
    return layers


def _blocked_via_cycle(
    cyclic: Collection[str], condensed: Sequence[tuple[str, str]]
) -> set[str]:
    """Every condensation strictly downstream of a cycle."""
    successors: dict[str, list[str]] = {}
    for predecessor, dependent in condensed:
        successors.setdefault(predecessor, []).append(dependent)
    reached: set[str] = set()
    queue = deque(cyclic)
    while queue:
        for dependent in successors.get(queue.popleft(), ()):
            if dependent not in reached and dependent not in cyclic:
                reached.add(dependent)
                queue.append(dependent)
    return reached


def build_topology(
    tasks: Sequence[TaskRecord],
    edges: Sequence[EdgeRecord],
    *,
    blocked: Sequence[BlockedTaskRecord] = (),
    incomplete: Collection[str] = (),
    unknown_status: Collection[str] = (),
) -> Topology:
    """Condense the fetched scope into cycles, layers and roots.

    ``tasks`` is the node set (in-scope tasks and their ghosts); ``edges`` is
    every fetched edge — the dependency ones are selected here, so callers pass
    the scope's edge list whole. ``blocked`` carries Lithos's cycle verdict,
    ``incomplete`` the nodes whose edge read failed and ``unknown_status`` the
    ghosts whose ``task_get`` failed.

    Layering uses every dependency edge whatever its state, because layers are
    the planned sequence rather than a claim about what blocks now — that claim
    is :func:`longest_blocking_chain`, and it uses the active projection.
    """
    dependency = classify_dependency_edges(edges, tasks, unknown_status=unknown_status)
    node_ids = set(task.id for task in tasks)
    for edge in dependency:
        node_ids.update((edge.from_task_id, edge.to_task_id))
    created_at = {task.id: task.created_at for task in tasks}
    key = {node: _sort_key(node, created_at.get(node, "")) for node in node_ids}
    nodes = sorted(node_ids, key=lambda node: key[node])

    adjacency = _adjacency(nodes, dependency, key)
    groups, member_of = _condense(
        nodes, adjacency, key, _flagged_cycle_members(blocked)
    )
    condensed = _condensed_edges(dependency, member_of)
    group_ids = [group[0] for group in groups]
    layers = _layer(group_ids, condensed, key)
    cyclic = {group_id for group_id, _, cycle in groups if cycle is not None}
    downstream = _blocked_via_cycle(cyclic, condensed)

    condensations = tuple(
        sorted(
            (
                Condensation(
                    id=group_id,
                    members=members,
                    layer=layers[group_id],
                    cycle=cycle,
                    blocked_via_cycle=group_id in downstream,
                    created_at=created_at.get(group_id, ""),
                )
                for group_id, members, cycle in groups
            ),
            key=lambda condensation: (condensation.layer, key[condensation.id]),
        )
    )
    depth = max(layers.values(), default=-1) + 1
    with_indegree = {dependent for _, dependent in condensed}
    return Topology(
        nodes=tuple(nodes),
        condensations=condensations,
        layers=tuple(
            tuple(c.id for c in condensations if c.layer == index)
            for index in range(depth)
        ),
        cycles=tuple(c.cycle for c in condensations if c.cycle is not None),
        # D4: in-degree-zero condensations, plus one representative per cyclic
        # condensation — a cycle has no root of its own, and a layout given no
        # root inside it draws its members from wherever it happens to start.
        roots=tuple(
            c.id for c in condensations if c.id not in with_indegree or c.id in cyclic
        ),
        edges=dependency,
        incomplete=frozenset(incomplete),
    )


def _chain_bound(topology: Topology) -> ChainBound:
    """Exact only when nothing could make the true chain longer (D7).

    Two independent degradations, and the second is the subtle one: ANY
    unknown dependency edge anywhere in the fetched graph lowers the bound,
    not just one on or near the chain. An unknown edge sits in neither
    projection, so a disconnected ``X -> Y`` pair could itself be the longest
    active component — "it is off the current chain" proves nothing about
    whether it would have been the chain.
    """
    if topology.incomplete or any(edge.state == "unknown" for edge in topology.edges):
        return "lower_bound"
    return "exact"


def _active_condensed(
    topology: Topology,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Successor/predecessor maps of the ACTIVE projection, condensed."""
    member_of = {
        member: condensation.id
        for condensation in topology.condensations
        for member in condensation.members
    }
    successors: dict[str, list[str]] = {c.id: [] for c in topology.condensations}
    predecessors: dict[str, list[str]] = {c.id: [] for c in topology.condensations}
    for predecessor, dependent in sorted(
        {
            (member_of[edge.from_task_id], member_of[edge.to_task_id])
            for edge in topology.edges
            if edge.state == "active"
            and member_of[edge.from_task_id] != member_of[edge.to_task_id]
        }
    ):
        successors[predecessor].append(dependent)
        predecessors[dependent].append(predecessor)
    return successors, predecessors


def _longest_from(
    order: Sequence[str],
    neighbours: Mapping[str, Sequence[str]],
    key: Mapping[str, tuple[str, str]],
) -> tuple[dict[str, int], dict[str, str]]:
    """Longest path length per node over ``neighbours``, plus the step taken.

    ``order`` must be reverse-topological for the direction being walked. Ties
    break on the smallest ``(created_at, id)`` at each step, which is what
    makes the traced chain the same one on every render.
    """
    length = dict.fromkeys(neighbours, 1)
    step: dict[str, str] = {}
    for node in order:
        best = 0
        chosen = ""
        for neighbour in neighbours[node]:
            candidate = length[neighbour]
            # ``not chosen`` first: the tie-break below reads ``key[chosen]``,
            # which only exists once a first neighbour has been taken.
            if (
                not chosen
                or candidate > best
                or (candidate == best and key[neighbour] < key[chosen])
            ):
                best, chosen = candidate, neighbour
        if chosen:
            length[node] = best + 1
            step[node] = chosen
    return length, step


def _walk(start: str, step: Mapping[str, str]) -> list[str]:
    walked = [start]
    while walked[-1] in step:
        walked.append(step[walked[-1]])
    return walked


def longest_blocking_chain(topology: Topology, *, through: str = "") -> BlockingChain:
    """The longest chain of blocking work in this graph, by node count (D7).

    Over the condensed DAG of the ACTIVE projection: a completed predecessor's
    edge is not in it, so a finished three-chain never outranks the open
    two-chain that is the real critical sequence. A cyclic condensation counts
    once and ghosts count. ``through`` (a task id or a condensation id) asks
    for the longest chain THROUGH that node instead — the longest walk into it
    joined to the longest walk out of it — which is what focus mode traces.

    This is the longest chain WITHIN THIS GRAPH, never a corpus-wide critical
    path: the scope is a page's worth of tasks and their one-hop ghosts.
    """
    bound = _chain_bound(topology)
    if not topology.condensations:
        return BlockingChain(bound=bound)
    # The layer order is topological for every dependency edge, so it is
    # topological for the active subset too.
    order = [c.id for c in topology.condensations]
    key = {c.id: _sort_key(c.id, c.created_at) for c in topology.condensations}
    successors, predecessors = _active_condensed(topology)
    down, next_step = _longest_from(list(reversed(order)), successors, key)
    if not through:
        start = min(order, key=lambda node: (-down[node], key[node]))
        return BlockingChain(nodes=tuple(_walk(start, next_step)), bound=bound)
    focus = topology.condensation_of(through)
    if focus is None:
        return BlockingChain(bound=bound)
    _, previous_step = _longest_from(order, predecessors, key)
    upward = _walk(focus.id, previous_step)
    return BlockingChain(
        nodes=tuple(reversed(upward)) + tuple(_walk(focus.id, next_step))[1:],
        bound=bound,
    )


def hierarchy_rows(
    tasks: Sequence[TaskRecord], edges: Sequence[EdgeRecord]
) -> tuple[HierarchyRow, ...]:
    """The scope's ``parent_child`` forest, flattened into indented rows.

    ``parent_child`` points parent -> child and carries no readiness meaning,
    so completion never drops one of these edges and the tree shows the same
    shape whatever the statuses are. A node whose parent is outside the scope
    is a root here; siblings are ordered by ``(created_at, id)``. Lithos
    guarantees a single-parent forest, but the walk still refuses to revisit a
    node, because these edges are agent-written and a malformed loop must
    degrade to a shorter tree rather than hang the render.
    """
    created_at = {task.id: task.created_at for task in tasks}
    nodes = {task.id for task in tasks}
    key = {node: _sort_key(node, created_at.get(node, "")) for node in nodes}
    children: dict[str, list[str]] = {node: [] for node in nodes}
    parented: set[str] = set()
    for edge in _dedupe_edges(edges, (PARENT_EDGE_TYPE,)):
        if edge.from_task_id in nodes and edge.to_task_id in nodes:
            children[edge.from_task_id].append(edge.to_task_id)
            parented.add(edge.to_task_id)
    ordered = {
        node: sorted(set(kids), key=lambda kid: key[kid])
        for node, kids in children.items()
    }
    by_key = sorted(nodes, key=lambda node: key[node])
    # Unparented nodes are the real roots; every node is then offered as a seed
    # so a malformed loop (every member parented, none reachable from a root)
    # renders a shorter tree instead of dropping its tasks off the page.
    return _walk_forest([n for n in by_key if n not in parented] + by_key, ordered)


def _walk_forest(
    seeds: Sequence[str], children: Mapping[str, Sequence[str]]
) -> tuple[HierarchyRow, ...]:
    """Pre-order DFS from each unvisited seed, emitting one row per node."""
    rows: list[HierarchyRow] = []
    visited: set[str] = set()
    for seed in seeds:
        stack = [(seed, 0)]
        while stack:
            node, depth = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            kids = children[node]
            rows.append(
                HierarchyRow(task_id=node, depth=depth, has_children=bool(kids))
            )
            stack.extend((kid, depth + 1) for kid in reversed(kids))
    return tuple(rows)
