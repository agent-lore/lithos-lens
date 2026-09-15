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

from collections.abc import Sequence
from dataclasses import dataclass, replace
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
from lithos_lens.graph_snapshot import (
    CanvasNotes,
    canvas_holds,
    impact_fingerprint,
    lower_bound_nodes,
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
#: A focal node D10 gives no figures at all — an epic, which carries no
#: ``blocks`` edges. Not "zero freed"; there is no N to state. The view model
#: still exists because D8's lower bound and D7's chain position are claims
#: about the CANVAS, owed for a focused epic like any other node (round-1
#: correctness f-001, round-2 f-005).
IMPACT_NONE = "none"
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


def with_canvas_notes(
    impact: DownstreamImpact | None, notes: CanvasNotes, *, focus: str
) -> DownstreamImpact | None:
    """Put the CLIENT's account of its canvas onto D10's line (D7/D8).

    The FIGURES are untouched — they are the server's arithmetic over its own
    reads, and nothing the browser says can make a withheld N honest. The notes
    are replaced wholesale, because they describe the picture and the client is
    the one looking at it.

    A line that does not exist at all is still owed them: an assembly that was
    refused, failed, or no longer holds the focus produces no impact, while the
    canvas goes on drawing the focused node with its neighbourhood lit. That
    answers :data:`IMPACT_NONE` — no figures, the notes alone — rather than
    silence (round-4 correctness f-006).
    """
    if not notes.stated:
        return impact
    if impact is None:
        return DownstreamImpact(
            focus=focus,
            state=IMPACT_NONE,
            relations_exact=notes.relations_exact,
            chain_position=notes.chain_position,
            chain_length=notes.chain_length,
        )
    return replace(
        impact,
        relations_exact=notes.relations_exact,
        chain_position=notes.chain_position,
        chain_length=notes.chain_length,
    )


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


def downstream_impact(
    scope: TaskGraphScope,
    signal: CycleSignal,
    *,
    focus: str,
    chain: BlockingChain | None = None,
    tag_key: str = DEFAULT_PROJECT_TAG_KEY,
) -> DownstreamImpact | None:
    """D10's two figures for ``focus`` over an assembled scope, pure.

    ``None`` when there is nothing to state at all: a focus this graph does not
    hold, so no projection exists to count over. An EPIC answers
    :data:`IMPACT_NONE` rather than ``None`` — D10 gives it no figures (a zero
    would read as "finishing this frees nobody"), while D8's lower-bound
    statement and D7's chain position are claims about the CANVAS rather than
    about N, and the panel owes a focused epic both.
    """
    node = scope.node(focus)
    if node is None:
        return None
    position, length = _chain_position(focus, chain)
    if node.task.task_type == "epic":
        # No FIGURES, and a zero would read as "finishing this frees nobody"
        # rather than "this is not that kind of task". Everything else the
        # panel says under a focus is a claim about the CANVAS rather than
        # about N, and is owed for an epic like any other focused node: D8's
        # lower-bound statement about the lit set, and D7's position on the
        # scope's longest chain when the epic is on it — which a scope whose
        # blocking projection is one node wide is exactly the boundary for
        # (round-2 correctness f-001, f-005).
        return DownstreamImpact(
            focus=focus,
            state=IMPACT_NONE,
            relations_exact=_relations_exact(scope, focus),
            chain_position=position,
            chain_length=length,
        )
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
    impact: DownstreamImpact | None,
    task: TaskRecord | None,
    *,
    scoped: bool = False,
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
      that disagreed with them, so the FIGURES degrade to :data:`IMPACT_STALE`
      and a refresh is what resolves it, while the notes about the canvas carry
      over (:func:`_stale_line`). A panel whose task could not be read at all
      (``None``) has no badge to agree with and no refresh that would resolve
      one, so it keeps the notes with no figures and no sentence at all — its
      markup carries the failure itself (round-4 correctness f-007).

    A panel with NO impact at all is the third case, and ``scoped`` is what
    makes it answerable. D10's resolved wording is a statement about the TASK,
    not a count over a graph, so a panel that ASKED for an impact still states
    it when the assembly could produce none — the rebuilt scope no longer holds
    the focus (a task that resolves leaves an ``include_resolved=0`` project
    graph the moment it does), the scope was refused, or the read failed
    (round-4 correctness f-002). ``scoped`` is false for every panel that
    asked for no impact line — the dashboard's — and an epic is excluded here
    exactly as it is in :func:`downstream_impact`, so neither gains a line it
    never had.
    """
    if impact is not None and impact.state == IMPACT_NONE:
        # An epic's line makes no claim about the focal STATUS — it carries no
        # figures for a badge to disagree with, only D8's statement about the
        # canvas — so there is nothing here to reconcile and nothing a later
        # read could turn stale.
        return impact
    resolved = _resolved_focus(task)
    if impact is None:
        if scoped and resolved and task is not None and task.task_type != "epic":
            return DownstreamImpact(focus=task.id, state=resolved)
        return None
    if task is None:
        # The panel could not read the focal task at all, so there is no status
        # for D10 to count against and no badge for a figure to disagree with
        # — and also nothing to REFRESH away, which "this graph has changed"
        # would tell the operator to do. The line degrades to its notes, which
        # are about the canvas and have nothing to do with that read, and the
        # panel's own markup carries the failure (round-4 correctness f-007).
        return _figureless(impact)
    if _focal_state(task.status) == impact.state:
        return impact
    if resolved:
        return DownstreamImpact(
            focus=impact.focus,
            state=resolved,
            relations_exact=impact.relations_exact,
            chain_position=impact.chain_position,
            chain_length=impact.chain_length,
        )
    return _stale_line(impact)


def _figureless(impact: DownstreamImpact) -> DownstreamImpact:
    """:data:`IMPACT_NONE` — no figures, no sentence, the canvas notes alone."""
    return DownstreamImpact(
        focus=impact.focus,
        state=IMPACT_NONE,
        relations_exact=impact.relations_exact,
        chain_position=impact.chain_position,
        chain_length=impact.chain_length,
    )


def _stale_line(impact: DownstreamImpact) -> DownstreamImpact:
    """:data:`IMPACT_STALE`, keeping what the disagreement did not touch.

    The FIGURES go: they were counted for a focal status this panel's own read
    does not agree with. The notes are the GRAPH's — the canvas this render
    drew is what the operator is looking at either way — so they carry over as
    they do into the resolved wording above, and for the same reason (round-3
    correctness f-006).
    """
    return DownstreamImpact(
        focus=impact.focus,
        state=IMPACT_STALE,
        relations_exact=impact.relations_exact,
        chain_position=impact.chain_position,
        chain_length=impact.chain_length,
    )


def _focal_state(status: str) -> str:
    """The impact state a focal task in ``status`` carries (D10)."""
    return IMPACT_OPEN if status == IMPACT_OPEN else _resolved_state(status)


def _resolved_focus(task: TaskRecord | None) -> str:
    """``completed``/``cancelled`` when the panel's own read says so, else "".

    The one question both branches above ask of the panel's record: D10 gives
    those two states words that need no graph behind them.
    """
    if task is None or task.status == IMPACT_OPEN:
        return ""
    state = _resolved_state(task.status)
    return state if state in (IMPACT_COMPLETED, IMPACT_CANCELLED) else ""


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

    A downstream GHOST is counted and then stopped at (D5/D10). Lens never read
    its edge list, so anything beyond it is not in this graph: the edges that
    appear to leave one were reported by somebody ELSE's list — two in-scope
    tasks can name the same out-of-scope task, one as a dependent and one as a
    blocker, and the assembly materialises it once between them. Walking
    through it would count a task the fetched topology does not reach from here
    and print it inside "frees N in this graph" (round-6 correctness f-013).
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
            node = scope.node(dependent)
            if node is not None and not node.ghost:
                queue.append(dependent)
    unclassified = {
        dependent
        for task_id in seen
        for dependent in unknown_out.get(task_id, ())
        if dependent not in seen
    }
    return tuple(reached), unclassified


def _relations_exact(scope: TaskGraphScope, focus: str) -> bool:
    """Whether focus mode's lit set is the whole of what surrounds ``focus``."""
    return focus not in lower_bound_nodes(scope)


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
    impact = downstream_impact(
        assembled,
        signal,
        focus=focus,
        chain=longest_blocking_chain(topology),
        tag_key=tag_key,
    )
    drawn = impact_fingerprint(assembled, signal, tag_key=tag_key)
    if not scope.fingerprint or drawn == scope.fingerprint:
        return impact
    # The page that opened this panel is not looking at what these reads just
    # answered — a task created, completed or re-linked since it loaded, or a
    # blocked row that moved under an edge cache that did not. Compared AFTER
    # the blocked reads because M is one of the two figures and comes from
    # them: comparing before would pin the picture and leave M free to move
    # behind it (round-1 correctness f-001). The whole answer is computed first
    # and then degraded, because which of its parts survive is the question
    # `_stale_impact` answers.
    return _stale_impact(
        impact, focus=focus, canvas_holds=canvas_holds(drawn, scope.fingerprint)
    )


def _stale_impact(
    impact: DownstreamImpact | None, *, focus: str, canvas_holds: bool
) -> DownstreamImpact:
    """What survives a snapshot this assembly no longer reproduces.

    The FIGURES never do: "frees N in this graph, M immediately" names the
    graph beside it, the canvas deliberately has not moved (D8), and a count
    over reads that disagree with the drawn answer has no honest place to be
    printed — whichever half moved, since N rests on the canvas and M on the
    answer.

    The NOTES are a different question, and the reason the fingerprint has two
    halves (round-3 correctness f-006). D8's lower bound and D7's "on the
    longest chain (k of n)" are claims about the PICTURE, which a blocked row
    moving under an unchanged graph falsifies neither of — so they are carried
    while the canvas half holds, and dropped with the figures when it does not,
    where they would describe an assembly nobody is looking at. An EPIC is
    where that decides the whole line: D10 states no figures for it, so its
    line is nothing BUT those notes (round-2 correctness f-004). With the
    canvas unchanged nothing about it is stale; with it moved the epic says so,
    never in the future tense the general sentence uses — D10 forbids it that
    one wording.
    """
    if impact is None:  # the focus is in scope by here; belt and braces
        return DownstreamImpact(focus=focus, state=IMPACT_STALE)
    figureless = impact.state == IMPACT_NONE  # an epic: notes and nothing else
    if canvas_holds:
        return impact if figureless else _stale_line(impact)
    return DownstreamImpact(
        focus=focus,
        state=IMPACT_NONE if figureless else IMPACT_STALE,
        stale=figureless,
    )
