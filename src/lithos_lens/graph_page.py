"""Assembling `/tasks/graph`: layers, callout, chain, payload (T2-A3).

This is the page D3 calls the first-class baseline. Everything the operator can
read without JavaScript is decided here — the cycle callout and its banners, the
legend, the longest blocking chain line, the topological layers, the isolated
disclosure, the hierarchy tree — and the Cytoscape layer (A4) draws from the
same embedded payload, so the picture and the text cannot disagree. The view
model those results are poured into lives in :mod:`lithos_lens.graph_view`.

The module is deliberately a FOLD, not a fetcher: :func:`load_graph_page` makes
exactly three moves — assemble the scope (``graph_scope``), read Lithos's cycle
verdict (``graph_cycles``), condense the shape (``graph_layout``) — and
:func:`build_graph_page` is pure over those three results. Every marker the
page renders is a field computed here rather than a condition expressed in
Jinja, because each one is a CLAIM with a rule behind it:

- ``in a cycle`` is Lithos's verdict, carried with Lithos's own message,
  whatever Tarjan found (D4);
- ``cycle status unknown`` is the absence of a claim — a truncated, failed or
  never-made read — and never renders as "no cycle" (D4);
- ``edges unknown`` marks a node whose edge read failed, which is also why it
  is not in the isolated disclosure: Lens has no evidence it is edge-less (D8);
- ``blocked by unresolvable predecessor`` marks a node whose incoming
  dependency edge is ``unknown`` BECAUSE ITS PREDECESSOR could not be read —
  not merely because the edge is unknown, which is equally true of an edge
  into an unreadable ghost and would be a false cause there (D6);
- the chain is a LOWER BOUND whenever any node's edges are unreadable or any
  ``unknown`` edge exists anywhere in the fetched graph (D7).

The one number this page never states is a corpus-wide one: the chain is
"within this graph" by construction, and says so.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol
from urllib.parse import urlencode

from lithos_lens.graph_cache import GraphCache
from lithos_lens.graph_cycles import (
    CycleSignal,
    CycleSignalClient,
    coverage_projects,
    load_cycle_signal,
)
from lithos_lens.graph_fanout import GraphScopeClient
from lithos_lens.graph_impact import downstream_impact, impact_fingerprint
from lithos_lens.graph_layout import (
    BlockingChain,
    Topology,
    build_topology,
    hierarchy_rows,
    longest_blocking_chain,
)
from lithos_lens.graph_scope import (
    COMPLETENESS_STATUS_UNKNOWN,
    EDGE_UNKNOWN,
    REFUSAL_COVERAGE,
    GraphNode,
    GraphScopeLimits,
    ScopeRefusal,
    TaskGraphScope,
    load_epic_scope,
    load_project_scope,
)
from lithos_lens.graph_view import (
    KNOWN_OVERLAYS,
    SCOPE_EPIC,
    SCOPE_PROJECT,
    Banner,
    ChainView,
    CycleView,
    EdgeView,
    GraphPageParams,
    GraphPageView,
    HierarchyRowView,
    LayerGroup,
    LayerView,
    NodeView,
    parse_flag,
    payload_json,
)
from lithos_lens.task_filtering import task_projects
from lithos_lens.task_links import (
    BLOCKER_EDGE_TYPES,
    PARENT_EDGE_TYPE,
    PROVENANCE_EDGE_TYPE,
)
from lithos_lens.tasks import (
    DEFAULT_PROJECT_CONVENTION,
    DEFAULT_PROJECT_TAG_KEY,
    GRAPH_SELECTION_KEY,
    PANEL_SELECTION_KEY,
    ProjectConvention,
    TaskRecord,
)

#: Legend order: dependency edges first, because they are the default view.
LEGEND_EDGE_ORDER: tuple[str, ...] = (
    *BLOCKER_EDGE_TYPES,
    PARENT_EDGE_TYPE,
    PROVENANCE_EDGE_TYPE,
)

#: One plain-language line per edge type — "A ──▶ B means A blocks B" is the
#: whole point of the legend: an arrow's direction is easy to misread, and the
#: text baseline has no arrow at all.
EDGE_LEGEND: Mapping[str, str] = {
    "blocks": "A → B means A blocks B: B cannot start until A is done.",
    "waits_on_gate": "A ⇢ B means B waits on gate A.",
    PARENT_EDGE_TYPE: "A ▸ B means A is B's parent (hierarchy, never satisfied).",
    PROVENANCE_EDGE_TYPE: "A ⋯ B means B was discovered from A.",
}


# ── Assembly ───────────────────────────────────────────────────────────


class GraphPageClient(GraphScopeClient, CycleSignalClient, Protocol):
    """The client surface one graph page needs: the scope reads plus blocked."""


async def load_graph_page(
    lithos: GraphPageClient,
    *,
    params: GraphPageParams,
    master: Sequence[TaskRecord],
    cache: GraphCache,
    limits: GraphScopeLimits | None = None,
    frontier_limit: int,
    convention: ProjectConvention = DEFAULT_PROJECT_CONVENTION,
    tag_key: str = DEFAULT_PROJECT_TAG_KEY,
) -> GraphPageView:
    """Assemble one scope, read its cycle signal, and fold both into the page."""
    limits = limits or GraphScopeLimits()
    if params.kind == SCOPE_EPIC:
        scope = await load_epic_scope(
            lithos,
            epic_id=params.key,
            master=master,
            cache=cache,
            limits=limits,
            include_resolved=params.include_resolved,
        )
    else:
        scope = await load_project_scope(
            lithos,
            project=params.key,
            master=master,
            cache=cache,
            limits=limits,
            include_resolved=params.include_resolved,
            convention=convention,
            tag_key=tag_key,
        )
    if scope.refused:
        return GraphPageView(
            params=params,
            refusal=scope.refusal,
            cache_hits=scope.cache_hits,
            cache_misses=scope.cache_misses,
            ghost_reads=scope.ghost_reads,
        )
    # D4 admits no sampling: every project in the coverage set gets its read
    # pair, or this page does not answer "is this in a cycle?" at all. The set
    # comes from task TAGS, so it is bounded here — BEFORE one call is queued —
    # and answered with a refusal rather than by quietly reading some of it.
    # The guard is ``max_tasks``: one project per task is §5B.1's shape.
    coverage = coverage_projects(scope, convention=convention, tag_key=tag_key)
    if len(coverage) > limits.max_tasks:
        return GraphPageView(
            params=params,
            refusal=ScopeRefusal(
                count=len(coverage),
                max_tasks=limits.max_tasks,
                reason=REFUSAL_COVERAGE,
            ),
            cache_hits=scope.cache_hits,
            cache_misses=scope.cache_misses,
            ghost_reads=scope.ghost_reads,
        )
    signal = await load_cycle_signal(
        lithos,
        scope,
        frontier_limit=frontier_limit,
        convention=convention,
        tag_key=tag_key,
        fetch_concurrency=limits.fetch_concurrency,
    )
    return build_graph_page(scope, signal, params=params, tag_key=tag_key)


def build_graph_page(
    scope: TaskGraphScope,
    signal: CycleSignal,
    *,
    params: GraphPageParams,
    tag_key: str = DEFAULT_PROJECT_TAG_KEY,
) -> GraphPageView:
    """Pure fold of scope + cycle verdict into everything the page renders."""
    topology = build_topology(
        [node.task for node in scope.nodes],
        [edge.edge for edge in scope.edges],
        blocked=signal.verdicts,  # authority only; see ``CycleSignal.verdicts``
        incomplete=scope.incomplete,
        unknown_status=[
            node.id
            for node in scope.nodes
            if node.completeness == COMPLETENESS_STATUS_UNKNOWN
        ],
    )
    # The scope's own chain, always: it is what the panel's "on the longest
    # chain (k of n)" is a position ON, and in focus mode the line below
    # renders a DIFFERENT chain (D7) — the one through the focused task.
    chain = longest_blocking_chain(topology)
    focus = params.focus if scope.node(params.focus) is not None else ""
    shown = chain
    impact = None
    if focus:
        shown = longest_blocking_chain(topology, through=focus)
        impact = downstream_impact(
            scope, signal, focus=focus, chain=chain, tag_key=tag_key
        )
    folded = _folded_isolates(scope, topology)
    views = _node_views(
        scope, topology, signal, params=params, tag_key=tag_key, folded=folded
    )
    layers = _layers(topology, views, folded)
    cycles, external, unshaped = _callout(topology, views, signal, scope)
    return GraphPageView(
        params=params,
        nodes=tuple(views.values()),
        layers=layers,
        incoming=_incoming(scope, views),
        isolated=tuple(views[node_id] for node_id in folded if node_id in views),
        hierarchy=_hierarchy(scope, views),
        cycles=cycles,
        external_cycles=external,
        unshaped_cycles=unshaped,
        chain=_chain_view(shown, views, scope, through=views.get(focus)),
        impact=impact,
        fingerprint=impact_fingerprint(scope, signal, tag_key=tag_key),
        banners=_banners(scope, signal),
        edge_types=_edge_types(scope),
        as_of=scope.as_of,
        cache_hits=scope.cache_hits,
        cache_misses=scope.cache_misses,
        ghost_reads=scope.ghost_reads,
        coverage=signal.coverage,
        reads_ok=sum(1 for read in signal.reads if read.ok),
        reads_truncated=sum(1 for read in signal.reads if read.truncated),
        reads_failed=sum(1 for read in signal.reads if read.error),
        reads_unmade=sum(1 for read in signal.reads if read.unmade),
        payload_json=payload_json(
            scope, topology, shown, views, layers, params, folded
        ),
        edge_count=len(scope.edges),
    )


def _folded_isolates(scope: TaskGraphScope, topology: Topology) -> tuple[str, ...]:
    """The in-scope tasks this page folds into the disclosure.

    ``scope.isolated`` — no fetched dependency edge in any state (D8) — MINUS
    every task Lithos flagged as a cycle member. D4 requires a flagged member
    with no component to be condensed alone and LAYERED, and the two rules
    collide in a state the TTL makes reachable: an edge upsert emits no event
    (ledger gap #1), so a warm, successful, edge-empty cache entry can be
    served for a task the uncached blocked read is calling cyclic. The
    authority wins over the absence of evidence — folding it away would put a
    task Lithos says cannot run behind a collapsed disclosure.
    """
    cyclic = {
        member
        for condensation in topology.condensations
        if condensation.cycle is not None
        for member in condensation.members
    }
    return tuple(node_id for node_id in scope.isolated if node_id not in cyclic)


def _node_views(
    scope: TaskGraphScope,
    topology: Topology,
    signal: CycleSignal,
    *,
    params: GraphPageParams,
    tag_key: str,
    folded: Sequence[str],
) -> dict[str, NodeView]:
    """One :class:`NodeView` per scope node, markers resolved."""
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
    # "Blocked by an unresolvable PREDECESSOR" is a claim about the other end
    # of the edge, so it is read off the predecessor's completeness rather than
    # off the edge's state: an edge is equally ``unknown`` when it is the
    # DEPENDENT whose status could not be read (an in-scope task pointing at a
    # downstream ghost), and marking that ghost as blocked by an unreadable
    # predecessor would invent a cause for a node whose predecessor is known.
    unresolved = {
        node.id
        for node in scope.nodes
        if node.completeness == COMPLETENESS_STATUS_UNKNOWN
    }
    unresolvable = {
        edge.to_task_id
        for edge in scope.edges
        if edge.dependency
        and edge.state == EDGE_UNKNOWN
        and edge.from_task_id in unresolved
    }
    isolated = set(folded)
    views: dict[str, NodeView] = {}
    for node in scope.nodes:
        views[node.id] = NodeView(
            id=node.id,
            label=node.label,
            status=node.status,
            task_type=node.task.task_type,
            layer=layer_of.get(node.id, 0),
            ghost=node.ghost,
            ghost_kind=node.ghost_kind,
            projects=task_projects(node.task, convention="both", tag_key=tag_key),
            completeness=node.completeness,
            claims=_claims(node),
            isolated=node.id in isolated,
            cycle_id=cycle_of.get(node.id, ""),
            # Ghosts carry NEITHER cycle marker: no scoped read covers the
            # blockers of a task this page sees one edge of, so its absence
            # is not "cycle-free" and a row naming it is not this graph's
            # verdict either.
            flagged=not node.ghost and node.id in signal.flagged,
            cycle_message="" if node.ghost else signal.flagged.get(node.id, ""),
            cycle_unknown=not node.ghost and node.id in signal.unknown,
            blocked_via_cycle=node.id in via_cycle,
            unresolvable=node.id in unresolvable,
            focused=bool(params.focus) and node.id == params.focus,
        )
    return views


def _claims(node: GraphNode) -> tuple[str, ...]:
    """Claiming agents, when the master read asked for them (else empty)."""
    return tuple(claim.agent for claim in node.task.claims or ())


def _layers(
    topology: Topology,
    views: Mapping[str, NodeView],
    folded: Sequence[str],
) -> tuple[LayerView, ...]:
    """Topology layers as render groups, with the folded isolates left out.

    A folded task is in the disclosure instead, so it is dropped from its layer
    here — but it keeps the layer the payload reports, so the two renderings
    agree on where every node sits. What is NOT folded (see
    :func:`_folded_isolates`) stays in its layer, which is how a Lithos-flagged
    cycle member with no fetched edge still gets one.
    """
    isolated = set(folded)
    by_id = {condensation.id: condensation for condensation in topology.condensations}
    layers: list[LayerView] = []
    for index, group_ids in enumerate(topology.layers):
        groups: list[LayerGroup] = []
        for group_id in group_ids:
            condensation = by_id[group_id]
            members = tuple(
                views[member]
                for member in condensation.members
                if member in views and member not in isolated
            )
            if not members:
                continue
            groups.append(
                LayerGroup(
                    id=group_id,
                    members=members,
                    cycle=condensation.cycle,
                    blocked_via_cycle=condensation.blocked_via_cycle,
                )
            )
        if groups:
            layers.append(LayerView(index=index, groups=tuple(groups)))
    return tuple(layers)


def _incoming(
    scope: TaskGraphScope, views: Mapping[str, NodeView]
) -> dict[str, tuple[EdgeView, ...]]:
    """Every node's incoming dependency edges, keyed by dependent.

    Each edge carries WHICH endpoint Lens could not resolve, because an
    ``unknown`` edge has two causes (D6) and only one of them is about the
    predecessor — the line renders the cause it actually has.
    """
    incoming: dict[str, list[EdgeView]] = {node_id: [] for node_id in views}
    for edge in scope.edges:
        if not edge.dependency or edge.to_task_id not in incoming:
            continue
        predecessor = views.get(edge.from_task_id)
        dependent = views[edge.to_task_id]
        incoming[edge.to_task_id].append(
            EdgeView(
                from_id=edge.from_task_id,
                from_label=predecessor.label if predecessor else edge.from_task_id,
                to_id=edge.to_task_id,
                type=edge.type,
                state=edge.state,
                reason=edge.reason,
                from_unknown=bool(predecessor and predecessor.status_unknown),
                to_unknown=dependent.status_unknown,
            )
        )
    return {node_id: tuple(edges) for node_id, edges in incoming.items()}


def _callout(
    topology: Topology,
    views: Mapping[str, NodeView],
    signal: CycleSignal,
    scope: TaskGraphScope,
) -> tuple[tuple[CycleView, ...], tuple[CycleView, ...], tuple[CycleView, ...]]:
    """The callout, split three ways by what Lens can actually SHOW (D4).

    A cycle with a fetched component gets its members and one representative
    path. For one Lithos flagged that Tarjan cannot see, the missing component
    is not itself evidence of anything — so the split is made on the blocker's
    own endpoint rather than on the absence of shape, and only ONE of the two
    answers is a claim:

    - every task Lithos names as the cycle partner is outside the in-scope task
      set (a ghost, or nothing this page fetched) — the loop demonstrably
      leaves the scope, which is D4's bounded promise and the honest
      "through tasks outside this scope";
    - otherwise — a named partner is in scope, or none is named — Lens says
      only that it has no shape for this cycle. It does NOT say the loop is
      inside the scope: the blocker names one immediate predecessor, and
      ``A(in) -> X(ghost) -> Y(ghost) -> B(in) -> A`` has an in-scope
      predecessor while most of its path lies outside. Nor does it say why the
      shape is missing — a stale edge-empty cache entry an unnotified upsert
      overtook and a failed edge read look identical from here.
    """
    in_scope = {node.id for node in scope.nodes if not node.ghost}
    drawn: list[CycleView] = []
    external: list[CycleView] = []
    unshaped: list[CycleView] = []
    for cycle in topology.cycles:
        view = CycleView(
            id=cycle.id,
            members=tuple(views[member] for member in cycle.members if member in views),
            path=tuple(views[node] for node in cycle.path if node in views),
            scc=cycle.scc,
            message=cycle.message,
        )
        if cycle.scc:
            drawn.append(view)
            continue
        partners = tuple(
            partner
            for member in cycle.members
            for partner in signal.cycle_partners.get(member, ())
        )
        outside = bool(partners) and not any(
            partner in in_scope for partner in partners
        )
        (external if outside else unshaped).append(view)
    return tuple(drawn), tuple(external), tuple(unshaped)


def _hierarchy(
    scope: TaskGraphScope, views: Mapping[str, NodeView]
) -> tuple[HierarchyRowView, ...]:
    """The `parent_child` tree — always rendered, whatever the overlays say."""
    rows = hierarchy_rows(
        [node.task for node in scope.nodes], [edge.edge for edge in scope.edges]
    )
    return tuple(
        HierarchyRowView(
            node=views[row.task_id], depth=row.depth, has_children=row.has_children
        )
        for row in rows
        if row.task_id in views
    )


def _chain_view(
    chain: BlockingChain,
    views: Mapping[str, NodeView],
    scope: TaskGraphScope,
    *,
    through: NodeView | None = None,
) -> ChainView:
    """The chain line's nodes and the two counts that explain a lower bound.

    ``through`` is the focused task when this render is in focus mode, and the
    chain handed in is then the one THROUGH it (D7/D8) rather than the scope's
    longest. The line names it, because a chain through a mid-graph task is
    routinely shorter than the graph's longest and an unlabelled number there
    would understate it.
    """
    return ChainView(
        through=through,
        nodes=tuple(views[node] for node in chain.nodes if node in views),
        exact=chain.bound == "exact",
        unreadable_nodes=len(scope.incomplete),
        unresolvable_edges=sum(
            1 for edge in scope.edges if edge.dependency and edge.state == EDGE_UNKNOWN
        ),
    )


def _banners(scope: TaskGraphScope, signal: CycleSignal) -> tuple[Banner, ...]:
    """Every "this picture is partial" statement the page owes the operator."""
    banners: list[Banner] = []
    if scope.incomplete:
        banners.append(
            Banner(
                id="edges-incomplete",
                text=(
                    f"{len(scope.incomplete)} tasks' edges could not be fetched. "
                    "They are shown with their edges unknown, are never counted "
                    "as isolated, and the longest chain below is a lower bound."
                ),
            )
        )
    # Each partial read states what IT is, and nothing about which rows ended
    # up marked: coverage is per task and per read (a task another read
    # returned, or that a complete read which could match it did not return,
    # is known), so any per-task rule in these two sentences would be false for
    # some combination of outcomes. The rule is stated once, with its real
    # count, in the banner below them.
    partial = sorted(set(signal.unknown) - set(signal.projectless))
    if signal.truncated_projects:
        banners.append(
            Banner(
                id="cycle-truncated",
                text=(
                    "Cycle signal incomplete: the blocked read truncated for "
                    f"{_join(signal.truncated_projects)}. A truncated response "
                    "is not evidence that a task is cycle-free."
                ),
            )
        )
    if signal.failed_projects:
        banners.append(
            Banner(
                id="cycle-unavailable",
                text=(
                    "Cycle signal unavailable: the blocked read failed for "
                    f"{_join(signal.failed_projects)}. A failed read is not "
                    "evidence that a task is cycle-free."
                ),
            )
        )
    if signal.unmade_projects:
        banners.append(
            Banner(
                id="cycle-unmade",
                text=(
                    "Cycle signal incomplete: this render ran out of time "
                    "before the blocked read for "
                    f"{_join(signal.unmade_projects)} could be made. Those "
                    "reads were never sent, and a read never made is not "
                    "evidence that a task is cycle-free."
                ),
            )
        )
    if partial:
        banners.append(
            Banner(
                id="cycle-unknown-count",
                text=(
                    f"{len(partial)} tasks on this page are marked cycle status "
                    "unknown: no response returned them, and no complete read "
                    "that could have matched them was made."
                ),
            )
        )
    if signal.projectless:
        banners.append(
            Banner(
                id="cycle-projectless",
                text=(
                    f"{len(signal.projectless)} tasks carry no project, so no "
                    "scoped blocked read can cover them; their cycle status is "
                    "unknown."
                ),
            )
        )
    return tuple(banners)


def _edge_types(scope: TaskGraphScope) -> tuple[str, ...]:
    """The edge types actually present, in legend order (D8's "exactly")."""
    present = {edge.type for edge in scope.edges}
    known = [edge_type for edge_type in LEGEND_EDGE_ORDER if edge_type in present]
    other = sorted(present - set(LEGEND_EDGE_ORDER))
    return tuple(known + other)


def _join(values: Sequence[str]) -> str:
    return ", ".join(values)


# ── Query-string state (D8) ────────────────────────────────────────────


def parse_graph_params(query: Mapping[str, str]) -> GraphPageParams:
    """Parse `/tasks/graph`'s URL state, defaults resolved by scope kind.

    ``project`` wins over ``epic`` when a hand-edited URL carries both: one
    scope is rendered, and picking the first-listed one is more predictable
    than refusing. ``selected=`` is accepted as an alias of ``focus=`` — the
    graph page has ONE selection parameter (D8), and a link arriving from the
    dashboard's vocabulary is canonicalised rather than ignored.
    """
    project = (query.get("project") or "").strip()
    epic = (query.get("epic") or "").strip()
    kind, key = (
        (SCOPE_PROJECT, project)
        if project
        else (SCOPE_EPIC, epic)
        if epic
        else ("", "")
    )
    # Opposite defaults, both by scope kind: a project graph is about what can
    # still run; an epic graph is about an initiative's progress, which its
    # finished children are half of.
    include_resolved = parse_flag(query.get("include_resolved"), kind == SCOPE_EPIC)
    return GraphPageParams(
        kind=kind,
        key=key,
        include_resolved=include_resolved,
        # Byte-for-byte, deliberately NOT stripped. A task id is an arbitrary
        # non-empty string (§5.1), so ``" task "`` is an id Lens can really be
        # handed — and trimming one here would look a different node up, trace
        # a different chain, fetch a different panel, and (through the
        # ``selected=`` redirect) canonicalise the wrong id into the URL bar
        # permanently. The client reads the same parameter raw
        # (``graph.js``: ``params.get(SELECTION_PARAM)``), so trimming on this
        # side is also a server/client split. Only ABSENT and empty are "no
        # focus", which is the one distinction this parse has to make.
        focus=query.get(GRAPH_SELECTION_KEY) or query.get(PANEL_SELECTION_KEY) or "",
        overlays=tuple(
            overlay
            for overlay in KNOWN_OVERLAYS
            if overlay in _split(query.get("overlays"))
        ),
        show_isolated=parse_flag(query.get("isolated"), kind == SCOPE_EPIC),
    )


def _split(raw: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (raw or "").split(",") if part.strip())


def graph_url(
    params: GraphPageParams | None = None,
    *,
    project: str = "",
    epic: str = "",
    include_resolved: bool | None = None,
    isolated: bool | None = None,
    focus: str | None = None,
    toggle_overlay: str = "",
) -> str:
    """Build a `/tasks/graph` URL — a fresh scope, or this one with one toggle.

    Naming ``project=`` or ``epic=`` starts a NEW scope and deliberately drops
    the current page's state: a ghost's "open its project's graph" link that
    carried this page's ``focus`` would point at a node that scope may not
    contain. Everything else edits the current URL in place, which is what the
    toggles need.

    ``toggle_overlay`` FLIPS one overlay's membership and leaves the other
    alone, because that is the only edit the toolbar makes — the two overlays
    are independent switches (D8) and a link that set the whole list would turn
    the other one off as a side effect. An empty result drops the parameter
    rather than emitting ``overlays=``: "absent" and "none" are the same state,
    and the client reads a missing parameter as no overlays for that reason.
    """
    if project or epic:
        return "/tasks/graph?" + urlencode(
            [("project", project)] if project else [("epic", epic)]
        )
    params = params or GraphPageParams()
    query: list[tuple[str, str]] = []
    if params.scoped:
        query.append((params.kind, params.key))
    resolved = params.include_resolved if include_resolved is None else include_resolved
    query.append(("include_resolved", "1" if resolved else "0"))
    show = params.show_isolated if isolated is None else isolated
    query.append(("isolated", "1" if show else "0"))
    target = params.focus if focus is None else focus
    if target:
        query.append((GRAPH_SELECTION_KEY, target))
    overlays = params.overlays
    if toggle_overlay:
        overlays = tuple(
            overlay
            for overlay in KNOWN_OVERLAYS
            if (overlay in params.overlays) != (overlay == toggle_overlay)
        )
    if overlays:
        query.append(("overlays", ",".join(overlays)))
    return "/tasks/graph?" + urlencode(query)


def scope_param(params: GraphPageParams) -> str:
    """This page's scope as the panel's ``scope=`` spells it (D10, T2-A7).

    ``project:<slug>`` / ``epic:<id>``, empty when the page has no scope. The
    panel needs it to count a downstream impact at all — N is a count within
    ONE fetched graph — and it is built here, beside ``graph_url``, so the
    page's URL vocabulary has one home.
    """
    return f"{params.kind}:{params.key}" if params.scoped else ""


def observed_projects(
    rows: Sequence[TaskRecord], *, tag_key: str = DEFAULT_PROJECT_TAG_KEY
) -> tuple[str, ...]:
    """Every project slug the snapshot observes, under BOTH conventions (§5B.1).

    The scope picker's left column. Both conventions regardless of the active
    posture, for §5B.1's reason: no project may be invisible to its own view.
    """
    slugs: set[str] = set()
    for task in rows:
        slugs.update(task_projects(task, convention="both", tag_key=tag_key))
    return tuple(sorted(slugs))


def open_epics(rows: Sequence[TaskRecord]) -> tuple[TaskRecord, ...]:
    """The picker's right column: open epics, newest first."""
    epics = [
        task for task in rows if task.task_type == "epic" and task.status == "open"
    ]
    return tuple(
        sorted(epics, key=lambda task: (task.created_at, task.id), reverse=True)
    )
