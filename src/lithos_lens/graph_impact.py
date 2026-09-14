"""Downstream impact: "frees N in this graph, M immediately" (D10, T2-A7).

The panel's one forward-looking number, and the module exists because its two
figures come from two different authorities and degrade in two different ways:

- **N** is Lens's own arithmetic — the open transitive dependents of the focal
  task over the **active projection** (D6) of ``blocks`` + ``waits_on_gate``,
  within the graph this page actually fetched, downstream ghosts counted as the
  leaves they are (D5). It is a lower bound ("≥ N") whenever something
  downstream is unreadable: a dependent whose own ``edge_list`` failed hides
  whatever it blocks, and an ``unknown`` edge (an endpoint whose status Lens
  could not read) is counted in neither direction — the node it names is
  LISTED as unclassifiable rather than folded into the count.
- **M** is Lithos's — a dependent whose scoped ``task_blocked`` row names this
  task as its SOLE unsatisfied blocker is one that completing this task frees
  right now. That fact only exists for dependents D4's coverage set actually
  covered, which is why the downstream ghosts' projects are in it. Where a
  dependent's project read truncated, failed, was never made, or could not be
  issued at all (a projectless task), M is **withheld** and the panel says how
  many of the dependents were covered. Stating "M of the K covered" instead
  was the other option D10 allows; withholding is the branch taken, because a
  partial M reads exactly like a whole one at a glance and the operator is
  choosing what to work on next from it.

A **completed or cancelled** focal task has no future-tense impact at all — its
edges are satisfied or unsatisfiable, not pending — and an epic carries no
``blocks`` edges, so neither states a number (D10).

Everything here is pure over an assembled scope and its cycle signal;
:func:`load_impact` is the one impure entry, for the panel fragment route,
which has no graph page around it to borrow an assembly from.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from lithos_lens.graph_cache import GraphCache
from lithos_lens.graph_cycles import (
    CycleSignal,
    CycleSignalClient,
    blocked_coverage,
    coverage_projects,
    load_cycle_signal,
)
from lithos_lens.graph_layout import (
    BlockingChain,
    build_topology,
    longest_blocking_chain,
)
from lithos_lens.graph_scope import (
    COMPLETENESS_STATUS_UNKNOWN,
    EDGE_UNKNOWN,
    GraphScopeClient,
    GraphScopeLimits,
    TaskGraphScope,
    load_epic_scope,
    load_project_scope,
)
from lithos_lens.graph_view import (
    SCOPE_EPIC,
    SCOPE_PROJECT,
    DownstreamImpact,
    parse_flag,
)
from lithos_lens.tasks import (
    DEFAULT_PROJECT_CONVENTION,
    DEFAULT_PROJECT_TAG_KEY,
    ProjectConvention,
    TaskRecord,
)

#: The focal states the panel has words for. ``open`` is the only one that
#: carries numbers; the other three are ANSWERS, not missing data — the view
#: model they are written into is :class:`~lithos_lens.graph_view.DownstreamImpact`.
IMPACT_OPEN = "open"
IMPACT_COMPLETED = "completed"
IMPACT_CANCELLED = "cancelled"
IMPACT_UNKNOWN = "unknown"

#: The ``scope=`` the panel fragment route accepts, ``<kind>:<key>``.
SCOPE_SEPARATOR = ":"


@dataclass(frozen=True)
class ImpactScope:
    """The scope a panel fragment was asked to count its impact over.

    ``include_resolved`` travels with it because the panel must assemble the
    SAME graph the page it was opened from did: the two disagree about whether
    a resolved task is a node at all, and a focal task one of them holds and
    the other does not would answer a click differently from the deep link to
    the same address.
    """

    kind: str = ""
    key: str = ""
    include_resolved: bool = False

    @property
    def scoped(self) -> bool:
        return bool(self.kind and self.key)


class ImpactClient(GraphScopeClient, CycleSignalClient, Protocol):
    """The scope reads plus the blocked reads — the same surface a page needs."""


def parse_impact_scope(raw: str | None, resolved: str | None = None) -> ImpactScope:
    """Parse ``project:<slug>`` / ``epic:<id>``; anything else is no scope.

    An id may itself contain ``:`` (task ids are arbitrary non-empty strings),
    so the split is on the FIRST separator only. An unknown kind is dropped
    rather than guessed at: the panel then renders with no impact line, which
    is what every host that passes no scope already gets.

    ``resolved`` is the request's ``include_resolved``, read through the same
    parser and the same by-kind defaults the page uses
    (:func:`~lithos_lens.graph_scope.parse_flag`) so the panel and its page
    never assemble different graphs from the same two values.
    """
    kind, _, key = (raw or "").strip().partition(SCOPE_SEPARATOR)
    if kind not in (SCOPE_PROJECT, SCOPE_EPIC) or not key.strip():
        return ImpactScope()
    return ImpactScope(
        kind=kind,
        key=key.strip(),
        include_resolved=parse_flag(resolved, kind == SCOPE_EPIC),
    )


def downstream_impact(
    scope: TaskGraphScope,
    signal: CycleSignal,
    *,
    focus: str,
    chain: BlockingChain | None = None,
    tag_key: str = DEFAULT_PROJECT_TAG_KEY,
) -> DownstreamImpact | None:
    """D10's two figures for ``focus`` over an assembled scope, pure.

    ``None`` when there is nothing to state: a focus this graph does not hold
    (so no projection exists to count over), or an epic — an epic carries no
    ``blocks`` edges and D10 gives it no impact line rather than a zero, which
    would read as "finishing this frees nobody".
    """
    node = scope.node(focus)
    if node is None or node.task.task_type == "epic":
        return None
    position, length = _chain_position(focus, chain)
    if node.status != "open":
        # Completed, cancelled, or a ghost Lens could not read. None of the
        # three supports a future-tense number: the first two have no pending
        # edges, and for the third Lens does not know what it has.
        return DownstreamImpact(
            focus=focus,
            state=_resolved_state(node.status),
            relations_exact=_relations_exact(scope, focus),
            chain_position=position,
            chain_length=length,
        )

    reached, unclassified, unreadable = _downstream(scope, focus)
    dependents = tuple(
        task_id
        for task_id in reached
        if (reached_node := scope.node(task_id)) is not None
        and reached_node.status == "open"
    )
    covered, immediately = _sole_blocker_count(
        scope, signal, focus=focus, dependents=dependents, tag_key=tag_key
    )
    return DownstreamImpact(
        focus=focus,
        state=IMPACT_OPEN,
        frees=len(dependents),
        exact=not unreadable and not unclassified,
        immediately=immediately if covered == len(dependents) else None,
        covered=covered,
        unclassified=tuple(_label(scope, task_id) for task_id in sorted(unclassified)),
        relations_exact=_relations_exact(scope, focus),
        chain_position=position,
        chain_length=length,
    )


def _resolved_state(status: str) -> str:
    if status == IMPACT_COMPLETED:
        return IMPACT_COMPLETED
    if status == IMPACT_CANCELLED:
        return IMPACT_CANCELLED
    return IMPACT_UNKNOWN


def _downstream(
    scope: TaskGraphScope, focus: str
) -> tuple[tuple[str, ...], set[str], bool]:
    """Walk down from ``focus``: what it blocks, what it might, what is unread.

    Three results, because D10 needs all three separately. The walk itself runs
    over ACTIVE dependency edges only (D6) — a satisfied edge frees nobody, and
    an edge into a resolved dependent constrains nothing — so anything reached
    only through a resolved dependent is correctly out of the count.

    ``unclassified`` is the far end of an ``unknown`` edge leaving anything the
    walk reached (the focus included): a relation Lens cannot classify, so it
    is named rather than counted in either direction. ``unreadable`` is whether
    any node the walk reached — again including the focus — had its own
    ``edge_list`` read fail, which hides whatever IT blocks and is the other
    way N becomes a lower bound.
    """
    successors: dict[str, list[str]] = {}
    unknown_out: dict[str, list[str]] = {}
    for edge in scope.edges:
        if not edge.dependency:
            continue
        if edge.active:
            successors.setdefault(edge.from_task_id, []).append(edge.to_task_id)
        elif edge.state == EDGE_UNKNOWN:
            unknown_out.setdefault(edge.from_task_id, []).append(edge.to_task_id)
    reached: list[str] = []
    seen = {focus}
    queue = [focus]
    while queue:
        current = queue.pop(0)
        for dependent in successors.get(current, ()):
            if dependent in seen:
                continue
            seen.add(dependent)
            reached.append(dependent)
            queue.append(dependent)
    unclassified = {
        dependent
        for task_id in seen
        for dependent in unknown_out.get(task_id, ())
        if dependent not in seen
    }
    unreadable = any(task_id in scope.incomplete for task_id in seen)
    return tuple(reached), unclassified, unreadable


def _relations_exact(scope: TaskGraphScope, focus: str) -> bool:
    """Whether focus mode's LIT SET is the whole of what surrounds this task.

    The canvas lights the focused node's ancestors AND descendants over the
    active projection (D8), so this walk is symmetric where :func:`_downstream`
    is not — and it fails for the same two reasons the count does: a node in
    the neighbourhood whose own ``edge_list`` read failed hides whatever else
    it relates to, and an ``unknown`` edge touching it is a relation Lens
    cannot classify in either direction. Either way the picture is a lower
    bound of the neighbourhood, and the panel says so rather than letting a
    dimmed node read as "unrelated".
    """
    neighbours: dict[str, list[str]] = {}
    unknown_at: set[str] = set()
    for edge in scope.edges:
        if not edge.dependency:
            continue
        if edge.active:
            neighbours.setdefault(edge.from_task_id, []).append(edge.to_task_id)
            neighbours.setdefault(edge.to_task_id, []).append(edge.from_task_id)
        elif edge.state == EDGE_UNKNOWN:
            unknown_at.update((edge.from_task_id, edge.to_task_id))
    seen = {focus}
    queue = [focus]
    while queue:
        current = queue.pop(0)
        for other in neighbours.get(current, ()):
            if other in seen:
                continue
            seen.add(other)
            queue.append(other)
    return not (seen & unknown_at) and not (seen & set(scope.incomplete))


def _sole_blocker_count(
    scope: TaskGraphScope,
    signal: CycleSignal,
    *,
    focus: str,
    dependents: Sequence[str],
    tag_key: str,
) -> tuple[int, int]:
    """(dependents a read covered, of those the focal task solely blocks).

    A dependent PRESENT in some blocked response is answered for, whatever
    else that response truncated; one absent from every response is answered
    only when a complete read that could have matched it was made (D4's rule,
    the same one the cycle markers use). An uncovered dependent is what
    withholds M: Lens cannot say whether this task is its sole blocker, and
    guessing either way would move the number the operator is choosing by.
    """
    rows = {record.task.id: record for record in signal.blocked}
    covered = 0
    immediately = 0
    for dependent in dependents:
        record = rows.get(dependent)
        if record is not None:
            covered += 1
            blockers = record.blockers
            if len(blockers) == 1 and blockers[0].task_id == focus:
                immediately += 1
            continue
        node = scope.node(dependent)
        if node is not None and blocked_coverage(signal, node.task, tag_key=tag_key):
            # Answered, and the answer is "not blocked at all" — so completing
            # this task is not what frees it.
            covered += 1
    return covered, immediately


def _label(scope: TaskGraphScope, task_id: str) -> str:
    node = scope.node(task_id)
    return node.label if node is not None else task_id


def _chain_position(focus: str, chain: BlockingChain | None) -> tuple[int, int]:
    """Where ``focus`` sits on the scope's longest chain, 1-based (D7).

    Matched through the chain's OWN condensation membership, not by id: a
    cycle occupies one step of the chain under its representative, and every
    member of it is on the chain at that step.
    """
    if chain is None or not chain.nodes:
        return 0, 0
    for index, condensation in enumerate(chain.nodes):
        members = (
            chain.members[index] if index < len(chain.members) else (condensation,)
        )
        if focus == condensation or focus in members:
            return index + 1, chain.length
    return 0, chain.length


async def load_impact(
    lithos: ImpactClient,
    *,
    scope: ImpactScope,
    focus: str,
    master: Sequence[TaskRecord],
    cache: GraphCache,
    limits: GraphScopeLimits | None = None,
    frontier_limit: int,
    convention: ProjectConvention = DEFAULT_PROJECT_CONVENTION,
    tag_key: str = DEFAULT_PROJECT_TAG_KEY,
) -> DownstreamImpact | None:
    """Assemble ``scope`` and answer D10 for ``focus`` — the fragment route's path.

    The graph PAGE never calls this: it has the scope and the cycle signal in
    hand already (``graph_page.build_graph_page``) and computes the impact from
    those, so a server-rendered ``focus=`` costs no second assembly. A panel
    fetched on its own has no page around it, and the per-task edge cache is
    what makes that affordable — the scope the operator is looking at is warm.

    ``None`` whenever the answer would not be honest: a scope too large to
    render is also too large to count over, and a refusal there must not turn
    into a number here.
    """
    limits = limits or GraphScopeLimits()
    if not scope.scoped or not focus:
        return None
    if scope.kind == SCOPE_EPIC:
        assembled = await load_epic_scope(
            lithos,
            epic_id=scope.key,
            master=master,
            cache=cache,
            limits=limits,
            include_resolved=scope.include_resolved,
        )
    else:
        assembled = await load_project_scope(
            lithos,
            project=scope.key,
            master=master,
            cache=cache,
            limits=limits,
            include_resolved=scope.include_resolved,
            convention=convention,
            tag_key=tag_key,
        )
    if assembled.refused or assembled.node(focus) is None:
        return None
    if len(coverage_projects(assembled, convention=convention, tag_key=tag_key)) > (
        limits.max_tasks
    ):
        return None
    signal = await load_cycle_signal(
        lithos,
        assembled,
        frontier_limit=frontier_limit,
        convention=convention,
        tag_key=tag_key,
        fetch_concurrency=limits.fetch_concurrency,
    )
    topology = build_topology(
        [node.task for node in assembled.nodes],
        [edge.edge for edge in assembled.edges],
        blocked=signal.verdicts,
        incomplete=assembled.incomplete,
        unknown_status=[
            node.id
            for node in assembled.nodes
            if node.completeness == COMPLETENESS_STATUS_UNKNOWN
        ],
    )
    return downstream_impact(
        assembled,
        signal,
        focus=focus,
        chain=longest_blocking_chain(topology),
        tag_key=tag_key,
    )
