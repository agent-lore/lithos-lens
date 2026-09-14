"""The graph page's view model: one frozen dataclass per thing the page states.

Split from :mod:`lithos_lens.graph_page` (which folds a scope, a cycle verdict
and a topology into these) so the SHAPE of the page is readable in one file and
the assembly in another. Every marker a row can carry is a field here rather
than a condition in Jinja, because each is a claim with a rule behind it —
"in a cycle" is Lithos's verdict, "cycle status unknown" is the absence of one,
and a template deriving either would be a second implementation of a rule the
PRD states once.

:func:`payload_json` lives here for the same reason: the embedded payload is
this view model addressed to the CLIENT rather than to Jinja — same nodes, same
layers, same chain — and A4's canvas draws from it, so the two renderings of one
page's shape are written side by side where a divergence is visible.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from lithos_lens.graph_layout import (
    BlockingChain,
    Cycle,
    Topology,
    active_condensed,
    longest_paths,
)
from lithos_lens.graph_scope import (
    COMPLETENESS_EDGES_UNKNOWN,
    COMPLETENESS_STATUS_UNKNOWN,
    EDGE_ACTIVE,
    EDGE_INACTIVE,
    EDGE_UNKNOWN,
    ScopeRefusal,
    TaskGraphScope,
)
from lithos_lens.tasks import task_detail_path

SCOPE_PROJECT = "project"
SCOPE_EPIC = "epic"

#: The spellings a documented ``1|0`` toggle accepts. Both sets are explicit so
#: a value in NEITHER can be told apart from a valid false — which is what lets
#: a scope's own default survive a malformed URL (see :func:`parse_flag`).
TRUE_FLAGS = frozenset({"1", "true", "yes", "on"})
FALSE_FLAGS = frozenset({"0", "false", "no", "off"})

#: The overlays D8 puts behind a toggle. Parsed once (in ``graph_page``) so an
#: unknown value in a bookmarked URL is dropped in one place.
OVERLAY_HIERARCHY = "hierarchy"
OVERLAY_PROVENANCE = "provenance"
KNOWN_OVERLAYS: tuple[str, ...] = (OVERLAY_HIERARCHY, OVERLAY_PROVENANCE)


@dataclass(frozen=True)
class GraphPageParams:
    """One graph page's URL state (D8), parsed once.

    ``show_isolated`` and ``include_resolved`` both default by SCOPE KIND —
    opposite ways round — so they are resolved here rather than carried as
    tri-state values into the template.
    """

    kind: str = ""
    key: str = ""
    include_resolved: bool = False
    focus: str = ""
    overlays: tuple[str, ...] = ()
    show_isolated: bool = False

    @property
    def scoped(self) -> bool:
        return bool(self.kind and self.key)


def parse_flag(raw: str | None, default: bool) -> bool:
    """Parse a documented ``1|0`` toggle, keeping the DEFAULT when it is neither.

    The scope toggles default by scope KIND and in opposite directions (D6/D8),
    so "anything I do not recognise is false" is the one reading that must not
    be used: ``include_resolved=2`` on an epic would silently hide its closed
    children, and ``isolated=garbage`` would silently collapse a disclosure
    that is open by default. A malformed value is not a request for the
    opposite behaviour — it carries no request at all — so the default stands.

    Beside :class:`GraphPageParams` because both readers of these toggles are
    reading the SAME page's state: the graph route, and the side panel's
    ``scope=`` (:mod:`lithos_lens.graph_impact`), which has to assemble the
    graph its page did or answer a click differently from the deep link to it.
    """
    value = (raw or "").strip().lower()
    if value in TRUE_FLAGS:
        return True
    if value in FALSE_FLAGS:
        return False
    return default


@dataclass(frozen=True)
class DownstreamImpact:
    """What completing the focused task would free, and how sure Lens is (D10).

    Two figures from two authorities — N is Lens's walk over the active
    projection, M is Lithos's sole-blocker fact — computed by
    :mod:`lithos_lens.graph_impact` and rendered by the side panel.

    ``immediately`` is ``None`` when M is WITHHELD, never zero: "nothing is
    freed immediately" and "Lens could not read the fact" are different
    answers, and a zero would present the second as the first.
    """

    focus: str = ""
    #: The focal task's own state, which decides whether there is a
    #: future-tense claim to make at all: only ``open`` carries numbers.
    state: str = "open"
    #: N, over the active projection within the fetched graph.
    frees: int = 0
    #: False when N could only be larger: something downstream is unreadable.
    exact: bool = True
    #: M, or ``None`` when a dependent's project read did not cover it.
    immediately: int | None = None
    #: How many of the counted dependents a complete read did cover.
    covered: int = 0
    #: Dependents reached only through an ``unknown`` edge — listed, never
    #: counted, because Lens cannot classify the relation in either direction.
    unclassified: tuple[str, ...] = ()
    #: Whether the LIT SET focus mode draws (D8) is the whole of it. False
    #: when an unreadable edge list or an ``unknown`` edge sits anywhere in the
    #: focused task's neighbourhood: the canvas then shows a lower bound of
    #: what surrounds it, and the panel says so — in both directions, which is
    #: why this is not the same question as ``exact`` (N is downstream only).
    relations_exact: bool = True
    #: Where the focal task sits on the SCOPE's longest chain (D7), 1-based.
    #: Zero when it is not on it — the line is not rendered then.
    chain_position: int = 0
    chain_length: int = 0

    @property
    def open(self) -> bool:
        return self.state == "open"

    @property
    def withheld(self) -> bool:
        """Whether M is missing because the coverage was incomplete."""
        return self.open and self.immediately is None

    @property
    def on_chain(self) -> bool:
        return bool(self.chain_position)


@dataclass(frozen=True)
class NodeView:
    """One rendered node, with every marker the text layer states."""

    id: str
    label: str
    status: str
    task_type: str
    layer: int = 0
    ghost: bool = False
    ghost_kind: str = ""
    projects: tuple[str, ...] = ()
    completeness: str = ""
    claims: tuple[str, ...] = ()
    isolated: bool = False
    #: The condensation this node is drawn in — Lens's SHAPE, used for the
    #: bracketed group and the payload. NOT the verdict: see ``flagged``.
    cycle_id: str = ""
    #: Lithos said this task is in a cycle (a ``kind="cycle"`` blocker). D4
    #: makes membership Lithos's and the shape Lens's, so the row's marker
    #: reads THIS and never the condensation: an SCC Lens can see while every
    #: blocked read failed is a shape with no verdict behind it, and a row
    #: claiming "in a cycle" beside "cycle status unknown" contradicts itself.
    flagged: bool = False
    cycle_message: str = ""
    cycle_unknown: bool = False
    blocked_via_cycle: bool = False
    unresolvable: bool = False
    focused: bool = False

    @property
    def edges_unknown(self) -> bool:
        return self.completeness == COMPLETENESS_EDGES_UNKNOWN

    @property
    def status_unknown(self) -> bool:
        return self.completeness == COMPLETENESS_STATUS_UNKNOWN

    @property
    def in_cycle(self) -> bool:
        """Lithos's verdict — the only thing that puts the marker on a row."""
        return self.flagged


@dataclass(frozen=True)
class EdgeView:
    """One dependency edge as the text renders it, under its dependent.

    The text baseline has no arrows, so an edge is rendered where it can only
    be read one way: on the node it blocks, naming the predecessor. An
    ``inactive`` one keeps its reason — ``satisfied`` is a different fact from
    ``dependent_resolved``, and a faded line with no label would say neither.
    """

    from_id: str
    from_label: str
    to_id: str
    type: str
    state: str = ""
    reason: str = ""
    #: Which endpoint's status could not be read. An ``unknown`` edge has two
    #: causes (D6) and the line states the one it has, rather than always
    #: blaming the predecessor.
    from_unknown: bool = False
    to_unknown: bool = False

    @property
    def active(self) -> bool:
        return self.state == EDGE_ACTIVE

    @property
    def inactive(self) -> bool:
        return self.state == EDGE_INACTIVE

    @property
    def unknown(self) -> bool:
        return self.state == EDGE_UNKNOWN


@dataclass(frozen=True)
class LayerGroup:
    """One condensation inside a layer: a lone task, or a cycle's members."""

    id: str
    members: tuple[NodeView, ...]
    cycle: Cycle | None = None
    blocked_via_cycle: bool = False

    @property
    def bracketed(self) -> bool:
        """Whether this group is DRAWN as a cycle.

        A Lithos-flagged member with no component in the fetched topology is
        condensed alone so it still gets a layer (D4) — but there is nothing to
        bracket: these edges show no loop, whatever the reason, and a group
        here would draw a cycle of one. Such a cycle is named in the callout
        instead, under whichever heading its blocker's endpoint supports
        (``graph_page._callout``).
        """
        return self.cycle is not None and self.cycle.scc


@dataclass(frozen=True)
class LayerView:
    index: int
    groups: tuple[LayerGroup, ...]

    @property
    def nodes(self) -> tuple[NodeView, ...]:
        return tuple(node for group in self.groups for node in group.members)


@dataclass(frozen=True)
class CycleView:
    """A cycle for the callout: Lens's own, or one only Lithos can see."""

    id: str
    members: tuple[NodeView, ...]
    path: tuple[NodeView, ...] = ()
    scc: bool = False
    message: str = ""


@dataclass(frozen=True)
class ChainView:
    """The longest blocking chain line (D7), with its honesty attached."""

    nodes: tuple[NodeView, ...] = ()
    exact: bool = True
    unreadable_nodes: int = 0
    unresolvable_edges: int = 0
    #: In focus mode the chain THROUGH the focused task replaces the scope's
    #: (D7/D8), and the line says whose chain it is — otherwise a shorter
    #: number would read as the graph's longest, which it is not.
    through: NodeView | None = None

    @property
    def length(self) -> int:
        return len(self.nodes)

    @property
    def incomplete_note(self) -> str:
        """Why the chain is a lower bound, in the page's own words."""
        parts: list[str] = []
        if self.unreadable_nodes:
            parts.append(f"{self.unreadable_nodes} tasks' edges unreadable")
        if self.unresolvable_edges:
            parts.append(f"{self.unresolvable_edges} edges unresolvable")
        return ", ".join(parts)


@dataclass(frozen=True)
class HierarchyRowView:
    node: NodeView
    depth: int = 0
    has_children: bool = False


@dataclass(frozen=True)
class Banner:
    """One honesty banner. ``id`` is the test/CSS hook, ``text`` the sentence."""

    id: str
    text: str


@dataclass(frozen=True)
class GraphPageView:
    """Everything `/tasks/graph` renders for one scope."""

    params: GraphPageParams = field(default_factory=GraphPageParams)
    nodes: tuple[NodeView, ...] = ()
    layers: tuple[LayerView, ...] = ()
    #: dependent id -> its incoming dependency edges, in render order.
    incoming: Mapping[str, tuple[EdgeView, ...]] = field(default_factory=dict)
    isolated: tuple[NodeView, ...] = ()
    hierarchy: tuple[HierarchyRowView, ...] = ()
    cycles: tuple[CycleView, ...] = ()
    #: Flagged cycles whose loop demonstrably leaves the scope (every partner
    #: Lithos names is outside the in-scope task set).
    external_cycles: tuple[CycleView, ...] = ()
    #: Flagged cycles the fetched edges do not show and that nothing proves to
    #: leave the scope: the blocker names an in-scope predecessor, or names
    #: none. Neither drawable nor provably external — and the category asserts
    #: nothing further, because one named predecessor says nothing about the
    #: rest of the path and a stale edge-empty cache entry is indistinguishable
    #: here from an unread one.
    unshaped_cycles: tuple[CycleView, ...] = ()
    chain: ChainView = field(default_factory=ChainView)
    #: D10's line for the focused task, when this render carries a ``focus=``
    #: the scope holds. Computed here rather than by the panel route because
    #: this render already has the scope and the cycle signal in hand.
    impact: DownstreamImpact | None = None
    banners: tuple[Banner, ...] = ()
    edge_types: tuple[str, ...] = ()
    edge_count: int = 0
    #: D4's coverage set — every project this render planned to read — and how
    #: those reads ended, carried so the route can count them without
    #: re-deriving the plan. ``reads_ok + reads_truncated + reads_failed`` is
    #: the number of calls actually ISSUED; ``reads_unmade`` is the rest of the
    #: plan, which the phase deadline caught still queued.
    coverage: tuple[str, ...] = ()
    #: What this render cost, counted per request rather than sampled off the
    #: process-wide cache counters (which a concurrent page also moves).
    cache_hits: int = 0
    cache_misses: int = 0
    ghost_reads: int = 0
    reads_ok: int = 0
    reads_truncated: int = 0
    reads_failed: int = 0
    reads_unmade: int = 0
    as_of: datetime | None = None
    refusal: ScopeRefusal | None = None
    payload_json: str = "{}"

    @property
    def refused(self) -> bool:
        return self.refusal is not None

    @property
    def ghosts(self) -> tuple[NodeView, ...]:
        return tuple(node for node in self.nodes if node.ghost)

    @property
    def cycle_count(self) -> int:
        return len(self.cycles) + len(self.external_cycles) + len(self.unshaped_cycles)

    @property
    def fanout(self) -> int:
        """Upstream reads this render issued: edge misses plus ghost reads."""
        return self.cache_misses + self.ghost_reads


def active_chain_payload(topology: Topology) -> dict[str, object]:
    """The active projection's longest-path DP, addressed to the CLIENT (D8).

    Focus mode traces the chain THROUGH the focused node, and its transitions
    are client-side (``pushState``, no reload, no fetch) — so every chain the
    page can be asked to show has to be reachable from the STATIC payload. One
    chain is not enough for that, and a client re-deriving the DP would be a
    second implementation of D7's answer; shipping the DP's own pointers is
    neither. ``of`` maps every task to its condensation representative, ``up``
    and ``down`` give the next step of the longest walk into and out of each
    condensation, and ``chain`` is the scope's own longest — what an unfocused
    page traces.

    The chain through X is then the walk up from X's condensation, reversed,
    joined to the walk down: exactly what
    :func:`~lithos_lens.graph_layout.longest_blocking_chain` computes with
    ``through=X``, tie-breaks included, because it is the same DP.

    Computed here rather than in ``graph_layout`` because it exists only to be
    serialised: this module is the view model addressed to the client, and
    :func:`payload_json` below is its one consumer.
    """
    if not topology.nodes:
        return {"of": {}, "up": {}, "down": {}, "chain": []}
    order_of = {node: index for index, node in enumerate(topology.nodes)}
    groups, member_of, successors, predecessors = active_condensed(topology, order_of)
    down = longest_paths(list(reversed(groups)), successors, order_of)
    up = longest_paths(groups, predecessors, order_of, against_the_render=True)
    start = min(groups, key=lambda node: (-len(down[node]), order_of[node]))
    return {
        "of": dict(member_of),
        "up": _next_steps(up),
        "down": _next_steps(down),
        "chain": list(down[start]),
    }


def _next_steps(paths: Mapping[str, Sequence[str]]) -> dict[str, str]:
    """Each walk's SECOND node — the one step a client follows from here."""
    return {node: path[1] for node, path in paths.items() if len(path) > 1}


def payload_json(
    scope: TaskGraphScope,
    topology: Topology,
    chain: BlockingChain,
    views: Mapping[str, NodeView],
    layers: Sequence[LayerView],
    params: GraphPageParams,
    folded: Sequence[str],
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
                # What the canvas needs and cannot derive (A4): the claims that
                # make a node "in progress", and the detail URL a double-click
                # navigates to — `tasks.task_detail_path` owns the rule that an
                # id colliding with a page under `/tasks/` is addressed through
                # the query alias, and the browser does not restate it.
                "claims": list(node.claims),
                "detail_url": task_detail_path(node.id),
                "cycle": node.cycle_id,
                # Shape and verdict are separate fields because they are
                # separate facts (D4): the canvas groups on ``cycle`` and marks
                # on ``flagged``.
                "flagged": node.flagged,
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
            # Parallel to ``nodes``: what each of those condensations HOLDS.
            # The chain condenses the active projection, the picture is drawn
            # from the all-edge one, and the two partitions legitimately
            # differ — an `A -> B` that is live inside a loop closed by a
            # completed `C` is one drawn cycle and a live two-chain at once.
            # So the canvas is told the chain's own membership rather than
            # left to map an id through a node's ``cycle``, which would trace
            # a different chain from the one the text states.
            "members": [list(members) for members in chain.members],
        },
        # And the DP behind it, so a client-side focus transition can trace the
        # chain through the newly focused node — which is a different chain
        # from the one above, on a page that is not being re-rendered (D8).
        "active_chain": active_chain_payload(topology),
        "roots": list(topology.roots),
        # The list the PAGE folds, not the raw D8 set: a flagged cycle member
        # is layered rather than folded, and the payload has to agree with the
        # text about where every node is rendered.
        "isolated": list(folded),
        "incomplete": dict(scope.incomplete),
        "as_of": scope.as_of.isoformat() if scope.as_of else None,
    }
    return json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
