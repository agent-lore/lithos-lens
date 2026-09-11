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
  dependency edge is ``unknown``, so it is excluded from the active projection
  in both directions rather than guessed at (D6);
- the chain is a LOWER BOUND whenever any node's edges are unreadable or any
  ``unknown`` edge exists anywhere in the fetched graph (D7).

The one number this page never states is a corpus-wide one: the chain is
"within this graph" by construction, and says so.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Protocol
from urllib.parse import urlencode

from lithos_lens.graph_cache import GraphCache
from lithos_lens.graph_cycles import (
    CycleSignal,
    CycleSignalClient,
    load_cycle_signal,
)
from lithos_lens.graph_fanout import GraphScopeClient
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
    GraphNode,
    GraphScopeLimits,
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
        return GraphPageView(params=params, refusal=scope.refusal)
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
        blocked=signal.blocked,
        incomplete=scope.incomplete,
        unknown_status=[
            node.id
            for node in scope.nodes
            if node.completeness == COMPLETENESS_STATUS_UNKNOWN
        ],
    )
    chain = longest_blocking_chain(topology)
    views = _node_views(scope, topology, signal, params=params, tag_key=tag_key)
    isolated_ids = set(scope.isolated)
    layers = _layers(topology, views, isolated_ids)
    cycles, external = _callout(topology, views)
    return GraphPageView(
        params=params,
        nodes=tuple(views.values()),
        layers=layers,
        incoming=_incoming(scope, views),
        isolated=tuple(
            views[node_id] for node_id in scope.isolated if node_id in views
        ),
        hierarchy=_hierarchy(scope, views),
        cycles=cycles,
        external_cycles=external,
        chain=_chain_view(chain, views, scope),
        banners=_banners(scope, signal),
        edge_types=_edge_types(scope),
        as_of=scope.as_of,
        coverage=signal.coverage,
        reads_ok=sum(1 for read in signal.reads if read.ok),
        reads_truncated=sum(1 for read in signal.reads if read.truncated),
        reads_failed=sum(1 for read in signal.reads if read.error),
        payload_json=_payload_json(scope, topology, chain, views, layers, params),
        edge_count=len(scope.edges),
    )


def _node_views(
    scope: TaskGraphScope,
    topology: Topology,
    signal: CycleSignal,
    *,
    params: GraphPageParams,
    tag_key: str,
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
    unresolvable = {
        edge.to_task_id
        for edge in scope.edges
        if edge.dependency and edge.state == EDGE_UNKNOWN
    }
    isolated = set(scope.isolated)
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
            cycle_message=signal.flagged.get(node.id, ""),
            # Ghosts carry no cycle marker: no scoped read claims to cover the
            # blockers of a task this page only sees the edge of.
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
    isolated: set[str],
) -> tuple[LayerView, ...]:
    """Topology layers as render groups, with the isolates folded out.

    An isolated task is in the disclosure instead, so it is dropped from its
    layer here — but it keeps the layer the payload reports, so the two
    renderings agree on where every node sits.
    """
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
    """Every node's incoming dependency edges, keyed by dependent."""
    incoming: dict[str, list[EdgeView]] = {node_id: [] for node_id in views}
    for edge in scope.edges:
        if not edge.dependency or edge.to_task_id not in incoming:
            continue
        predecessor = views.get(edge.from_task_id)
        incoming[edge.to_task_id].append(
            EdgeView(
                from_id=edge.from_task_id,
                from_label=predecessor.label if predecessor else edge.from_task_id,
                to_id=edge.to_task_id,
                type=edge.type,
                state=edge.state,
                reason=edge.reason,
            )
        )
    return {node_id: tuple(edges) for node_id, edges in incoming.items()}


def _callout(
    topology: Topology, views: Mapping[str, NodeView]
) -> tuple[tuple[CycleView, ...], tuple[CycleView, ...]]:
    """The callout, split by what Lens can actually draw (D4).

    A cycle with a fetched component gets its members and one representative
    path. One Lithos flagged but Tarjan cannot see closes through two or more
    ghosts, so there is no path to draw and Lithos's own message is all there
    is — it goes under "through tasks outside this scope" rather than being
    dropped or drawn as a group it is not.
    """
    drawn: list[CycleView] = []
    external: list[CycleView] = []
    for cycle in topology.cycles:
        view = CycleView(
            id=cycle.id,
            members=tuple(views[member] for member in cycle.members if member in views),
            path=tuple(views[node] for node in cycle.path if node in views),
            scc=cycle.scc,
            message=cycle.message,
        )
        (drawn if cycle.scc else external).append(view)
    return tuple(drawn), tuple(external)


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
) -> ChainView:
    """The chain line's nodes and the two counts that explain a lower bound."""
    return ChainView(
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
    if signal.truncated_projects:
        banners.append(
            Banner(
                id="cycle-truncated",
                text=(
                    "Cycle signal incomplete: the blocked read truncated for "
                    f"{_join(signal.truncated_projects)}. Tasks absent from that "
                    "response are marked cycle status unknown, not cycle-free."
                ),
            )
        )
    if signal.failed_projects:
        banners.append(
            Banner(
                id="cycle-unavailable",
                text=(
                    "Cycle signal unavailable: the blocked read failed for "
                    f"{_join(signal.failed_projects)}. Their tasks are marked "
                    "cycle status unknown."
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


def _payload_json(
    scope: TaskGraphScope,
    topology: Topology,
    chain: BlockingChain,
    views: Mapping[str, NodeView],
    layers: Sequence[LayerView],
    params: GraphPageParams,
) -> str:
    """D3's embedded payload — the same node set, layers and chain as the text.

    Serialised here rather than in the template so the escaping is applied
    once: ``<`` is escaped so a task title containing ``</script>`` cannot end
    the element early.
    """
    payload = {
        "scope": {
            "kind": params.kind,
            "key": params.key,
            "include_resolved": params.include_resolved,
            "focus": params.focus,
            "overlays": list(params.overlays),
            "isolated": params.show_isolated,
        },
        "nodes": [
            {
                "id": node.id,
                "label": node.label,
                "status": node.status,
                "type": node.task_type,
                "layer": node.layer,
                "ghost": node.ghost,
                "ghost_kind": node.ghost_kind,
                "projects": list(node.projects),
                "completeness": node.completeness,
                "cycle": node.cycle_id,
                "cycle_unknown": node.cycle_unknown,
                "blocked_via_cycle": node.blocked_via_cycle,
                "isolated": node.isolated,
            }
            for node in views.values()
        ],
        "edges": [
            {
                "from": edge.from_task_id,
                "to": edge.to_task_id,
                "type": edge.type,
                "state": edge.state,
                "reason": edge.reason,
            }
            for edge in scope.edges
        ],
        "layers": [[node.id for node in layer.nodes] for layer in layers],
        "cycles": [
            {
                "id": cycle.id,
                "members": list(cycle.members),
                "path": list(cycle.path),
                "scc": cycle.scc,
                "flagged": cycle.flagged,
                "message": cycle.message,
            }
            for cycle in topology.cycles
        ],
        "ghosts": [node.id for node in scope.nodes if node.ghost],
        "longest_chain": {
            "nodes": list(chain.nodes),
            "length": chain.length,
            "bound": chain.bound,
        },
        "roots": list(topology.roots),
        "isolated": list(scope.isolated),
        "incomplete": dict(scope.incomplete),
        "as_of": scope.as_of.isoformat() if scope.as_of else None,
    }
    return json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")


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
    include_resolved = _flag(query.get("include_resolved"), kind == SCOPE_EPIC)
    return GraphPageParams(
        kind=kind,
        key=key,
        include_resolved=include_resolved,
        focus=(query.get("focus") or query.get("selected") or "").strip(),
        overlays=tuple(
            overlay
            for overlay in KNOWN_OVERLAYS
            if overlay in _split(query.get("overlays"))
        ),
        show_isolated=_flag(query.get("isolated"), kind == SCOPE_EPIC),
    )


def _flag(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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
) -> str:
    """Build a `/tasks/graph` URL — a fresh scope, or this one with one toggle.

    Naming ``project=`` or ``epic=`` starts a NEW scope and deliberately drops
    the current page's state: a ghost's "open its project's graph" link that
    carried this page's ``focus`` would point at a node that scope may not
    contain. Everything else edits the current URL in place, which is what the
    toggles need.
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
        query.append(("focus", target))
    if params.overlays:
        query.append(("overlays", ",".join(params.overlays)))
    return "/tasks/graph?" + urlencode(query)


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
