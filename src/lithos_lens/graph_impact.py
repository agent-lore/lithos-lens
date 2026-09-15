"""Downstream impact: "frees N in this graph, M immediately" (D10, T2-A7).

The panel's one forward-looking number, and the module exists because its two
figures come from two different authorities and degrade in two different ways:

- **N** is Lens's own arithmetic — the open transitive dependents of the focal
  task over the **active projection** (D6) of ``blocks`` + ``waits_on_gate``,
  within the graph this page actually fetched, downstream ghosts counted as the
  leaves they are (D5). It is a lower bound ("≥ N") in two states, and both are
  D10's: the **scope is incomplete** — any task's ``edge_list`` read failed,
  wherever it sits, because an unread edge list is precisely the evidence that
  the projection Lens can see may not be all of it — or an ``unknown`` edge (an
  endpoint whose status Lens could not read) is reachable downstream, which is
  counted in neither direction and whose far end is LISTED as unclassifiable
  rather than folded into the number.
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

import hashlib
import json
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
from lithos_lens.task_filtering import task_projects
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
#: The graph the page is DRAWING is not the graph this panel just assembled, so
#: there is no honest "in this graph" to count over — see
#: :func:`impact_fingerprint`. A state rather than a silent ``None``: the panel
#: was asked for the line and the reason it has no number is worth a sentence.
IMPACT_STALE = "stale"

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

    ``fingerprint`` is the rest of that requirement. A scope NAME only fixes
    which tasks are asked for, not which ones came back: the panel's reads run
    after the page's, and between the two a task can be created, completed or
    re-linked, and Lithos's blocked rows can move under an edge cache that did
    not — while the canvas, by design, is still drawing the graph it loaded
    with (D8 forbids an auto re-layout; the page raises "graph changed —
    refresh" instead). The fingerprint is that drawn ANSWER's identity, both
    figures' material included (:func:`impact_fingerprint`), so a count that
    would be over a DIFFERENT graph is withheld rather than printed beside a
    picture that disagrees with it.
    """

    kind: str = ""
    key: str = ""
    include_resolved: bool = False
    #: The page's :func:`impact_fingerprint`, empty when the caller named none.
    fingerprint: str = ""

    @property
    def scoped(self) -> bool:
        return bool(self.kind and self.key)


class ImpactClient(GraphScopeClient, CycleSignalClient, Protocol):
    """The scope reads plus the blocked reads — the same surface a page needs."""


def parse_impact_scope(
    raw: str | None,
    resolved: str | None = None,
    fingerprint: str | None = None,
) -> ImpactScope:
    """Parse ``project:<slug>`` / ``epic:<id>``; anything else is no scope.

    An id may itself contain ``:`` (task ids are arbitrary non-empty strings),
    so the split is on the FIRST separator only. An unknown kind is dropped
    rather than guessed at: the panel then renders with no impact line, which
    is what every host that passes no scope already gets.

    ``resolved`` is the request's ``include_resolved``, read through the same
    parser and the same by-kind defaults the page uses
    (:func:`~lithos_lens.graph_scope.parse_flag`) so the panel and its page
    never assemble different graphs from the same two values.

    ``fingerprint`` is taken as given — it is only ever compared against one
    Lens computes itself, so an unparseable or invented value can do nothing
    but withhold the line.
    """
    kind, _, key = (raw or "").strip().partition(SCOPE_SEPARATOR)
    if kind not in (SCOPE_PROJECT, SCOPE_EPIC) or not key.strip():
        return ImpactScope()
    return ImpactScope(
        kind=kind,
        key=key.strip(),
        include_resolved=parse_flag(resolved, kind == SCOPE_EPIC),
        fingerprint=(fingerprint or "").strip(),
    )


def impact_fingerprint(
    scope: TaskGraphScope,
    signal: CycleSignal,
    *,
    tag_key: str = DEFAULT_PROJECT_TAG_KEY,
) -> str:
    """The identity of one assembled ANSWER — everything D10's figures rest on.

    Both authorities, because both move independently and only one of them is
    cached. N is Lens's walk over the scope, so the node set (id, the status
    the count reads, the completeness that turns a status into ``unknown``, the
    ghost kind that decides whether it is drawn at all, and the project slugs
    coverage is decided by) and the edge set (endpoints, type and the state the
    active projection is read from) are in it. M is LITHOS's sole-blocker fact,
    read fresh on every panel with no cache under it, so the blocked rows for
    the nodes this graph holds — each row's blockers by kind, predecessor, type
    and status — are in it too, along with the coverage set and each read's
    outcome, which together decide whether M is withheld at all.

    The blocked half is what makes this a fingerprint of the ANSWER rather than
    of the picture. An eventless edge upsert elsewhere in the fleet
    (ROADMAP gap #1) can add a second blocker to a dependent while every edge
    entry this scope reads stays warm: the drawn graph is then byte-identical
    and M has still moved from 1 to 0 (round-1 correctness f-001). Nothing else
    downstream would notice, so it is caught here or not at all.

    What is deliberately NOT in it: titles, claims, blocker MESSAGES, error
    reasons — text that moves no figure and no class on the canvas. A
    fingerprint that changed on every heartbeat would withhold the line
    permanently rather than when it is actually wrong. Rows for tasks outside
    this graph are left out for the same reason: a scoped read legitimately
    names them, and no figure here is counted over them.

    Order is imposed on every part, because none of it arrives ordered: the
    blocked rows are folded out of two concurrent reads per project and their
    order follows whichever answered first (``graph_cycles._signal``).

    The material is serialised as canonical JSON rather than joined on a
    separator. Task ids are arbitrary non-empty strings (§5.1) and nothing
    normalises control characters out of them, so a chosen separator is one an
    id may legitimately CONTAIN — and a digest over joined fields then reads
    ``a -> b<sep>c`` and ``a<sep>b -> c`` as the same edge, letting a moved
    graph pass the equality check and print the wrong count (round-2
    correctness f-003). JSON's own escaping is what makes the encoding
    injective; every value stays a distinct element of a nested array.

    Truncated to 16 hex digits because it travels in a URL and is compared to
    a value Lens produced itself in the same process — this is a change
    detector, not a defence against a forged one, and a caller that invents a
    value only costs itself the line.
    """
    held = set(scope.node_ids)
    material = [
        sorted(
            [
                node.id,
                node.status,
                node.completeness,
                node.ghost_kind,
                sorted(task_projects(node.task, convention="both", tag_key=tag_key)),
            ]
            for node in scope.nodes
        ),
        sorted(
            [edge.from_task_id, edge.to_task_id, edge.type, edge.state]
            for edge in scope.edges
        ),
        sorted(signal.coverage),
        sorted(
            # The OUTCOME, not the reason: "this read cannot establish absence"
            # is the whole of what M asks of it (``graph_cycles.read_covers``).
            [read.project, read.by, read.truncated, bool(read.error), read.unmade]
            for read in signal.reads
        ),
        sorted(
            [
                record.task.id,
                sorted(
                    [blocker.kind, blocker.task_id, blocker.type, blocker.status]
                    for blocker in record.blockers
                ),
            ]
            for record in signal.blocked
            if record.task.id in held
        ),
        sorted(signal.projectless),
    ]
    encoded = json.dumps(material, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


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

    reached, unclassified = _downstream(scope, focus)
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
        # D10's rule, taken whole: N is a lower bound when the SCOPE is
        # incomplete — not merely when the unreadable task happens to sit on
        # the projection Lens can already see. An unread edge list is exactly
        # the evidence that the known projection may not be all of it, so
        # asking where the gap is would be reasoning from the gap's own
        # absence (round-1 correctness f-002).
        exact=not scope.incomplete and not unclassified,
        immediately=immediately if covered == len(dependents) else None,
        covered=covered,
        unclassified=tuple(_label(scope, task_id) for task_id in sorted(unclassified)),
        relations_exact=_relations_exact(scope, focus),
        chain_position=position,
        chain_length=length,
    )


def reconciled_impact(
    impact: DownstreamImpact | None, task: TaskRecord | None
) -> DownstreamImpact | None:
    """D10's line, kept only while the panel around it agrees about the focus.

    A focused panel is TWO reads, and this is the join between them. The impact
    is computed from the graph assembly's view of the focal task; the header
    beside it — title, type, and the STATUS BADGE — is a later, independent
    ``task_get`` (``graph_routes._focused_panel``, ``web._load_detail``). A task
    that completes between the two renders "Completing this frees 3 in this
    graph" under a ``completed`` badge, which is the one sentence D10 says a
    resolved task must never carry (round-2 correctness f-002).

    So the impact is kept only while the badge's own status is the one it was
    counted for. A disagreement is answered two ways, because the two
    directions are not the same question:

    - The badge says **completed or cancelled**. D10 has words for that state
      and they need no arithmetic — "completed; no pending impact", or the
      cancelled wording — so the panel STATES them, the way the same task's own
      render would a moment later (round-3 correctness f-002). Telling the
      operator to refresh a resolved task, in a future tense D10 forbids it,
      would be the defect this function exists to prevent wearing another
      sentence. The rest of the line is the GRAPH's, not the focal status's, so
      the chain position and the lower-bound note carry over unchanged —
      exactly what :func:`downstream_impact` builds for a resolved node.
    - The badge says **open** (or nothing readable) against figures counted for
      a resolved task, or against a state Lens cannot name. There is no
      resolved fact to state and the numbers cannot be recomputed from the read
      that disagreed with them, so the line degrades to :data:`IMPACT_STALE` —
      the same answer a moved graph gets from :func:`impact_fingerprint`, and a
      refresh is what resolves it. A panel whose task could not be read at all
      (``None``) reads the same way: it has no badge to agree with, and its
      markup carries the failure instead.
    """
    if impact is None:
        return None
    if task is None:
        return DownstreamImpact(focus=impact.focus, state=IMPACT_STALE)
    state = _focal_state(task.status)
    if state == impact.state:
        return impact
    if state in (IMPACT_COMPLETED, IMPACT_CANCELLED):
        return DownstreamImpact(
            focus=impact.focus,
            state=state,
            relations_exact=impact.relations_exact,
            chain_position=impact.chain_position,
            chain_length=impact.chain_length,
        )
    return DownstreamImpact(focus=impact.focus, state=IMPACT_STALE)


def _focal_state(status: str) -> str:
    """The impact state a focal task in ``status`` carries (D10)."""
    return IMPACT_OPEN if status == IMPACT_OPEN else _resolved_state(status)


def _resolved_state(status: str) -> str:
    if status == IMPACT_COMPLETED:
        return IMPACT_COMPLETED
    if status == IMPACT_CANCELLED:
        return IMPACT_CANCELLED
    return IMPACT_UNKNOWN


def _downstream(scope: TaskGraphScope, focus: str) -> tuple[tuple[str, ...], set[str]]:
    """Walk down from ``focus``: what it blocks, and what it might.

    The walk runs over ACTIVE dependency edges only (D6) — a satisfied edge
    frees nobody, and an edge into a resolved dependent constrains nothing — so
    anything reached only through a resolved dependent is correctly out of the
    count.

    ``unclassified`` is the far end of an ``unknown`` edge leaving anything the
    walk reached (the focus included): a relation Lens cannot classify, so it
    is named rather than counted in either direction. Whether the count is a
    lower bound for the OTHER reason — an unreadable edge list — is not asked
    here, because it is not a question about this walk: the scope's own
    ``incomplete`` set answers it (D10).
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
    return tuple(reached), unclassified


def _relations_exact(scope: TaskGraphScope, focus: str) -> bool:
    """Whether focus mode's LIT SET is the whole of what surrounds this task.

    Two ways it is not, and D8 states both: the SCOPE is incomplete — an
    unreadable edge list anywhere is evidence that the projection Lens can see
    is not all of it, wherever the gap turns out to be — or an ``unknown`` edge
    touches the focused node's own neighbourhood, which is a relation Lens
    cannot classify in either direction. The second is a question about THIS
    node, so it is walked; the first is not, so it is not.

    The walk is symmetric where :func:`_downstream` is not, because the canvas
    lights ancestors AND descendants (D8). Either way the picture is a lower
    bound of the neighbourhood and the panel says so, rather than letting a
    dimmed node read as "unrelated".
    """
    if scope.incomplete:
        return False
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
    return not (seen & unknown_at)


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
    into a number here. A scope the caller fingerprinted whose reads no longer
    reproduce the same ANSWER is the one degraded case that still renders — as
    :data:`IMPACT_STALE`, because the operator is looking at the older graph
    and "refresh" is the answer.
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
    if (
        scope.fingerprint
        and impact_fingerprint(assembled, signal, tag_key=tag_key) != scope.fingerprint
    ):
        # The page that opened this panel is not looking at what these reads
        # just answered — a task created, completed or re-linked since it
        # loaded, or a blocked row that moved under an edge cache that did not.
        # "Frees N in this graph, M immediately" has no honest answer then: the
        # canvas deliberately has not moved (D8), so figures counted here would
        # disagree with the lit set and the chain beside them. Checked AFTER
        # the blocked reads because M is one of the two figures and it comes
        # from them — a comparison made before would pin the picture and leave
        # M free to move behind it (round-1 correctness f-001).
        return DownstreamImpact(focus=focus, state=IMPACT_STALE)
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
