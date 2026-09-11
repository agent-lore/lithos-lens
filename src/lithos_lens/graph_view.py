"""The graph page's view model: one frozen dataclass per thing the page states.

Split from :mod:`lithos_lens.graph_page` (which folds a scope, a cycle verdict
and a topology into these) so the SHAPE of the page is readable in one file and
the assembly in another. Every marker a row can carry is a field here rather
than a condition in Jinja, because each is a claim with a rule behind it —
"in a cycle" is Lithos's verdict, "cycle status unknown" is the absence of one,
and a template deriving either would be a second implementation of a rule the
PRD states once.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from lithos_lens.graph_layout import Cycle
from lithos_lens.graph_scope import (
    COMPLETENESS_EDGES_UNKNOWN,
    COMPLETENESS_STATUS_UNKNOWN,
    EDGE_ACTIVE,
    EDGE_INACTIVE,
    EDGE_UNKNOWN,
    ScopeRefusal,
)

SCOPE_PROJECT = "project"
SCOPE_EPIC = "epic"

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
    cycle_id: str = ""
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
        return bool(self.cycle_id)


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
        bracket: its loop closes through ghosts this scope never fetched, so
        the callout's "through tasks outside this scope" is where it belongs
        and a group here would draw a cycle of one.
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
    external_cycles: tuple[CycleView, ...] = ()
    chain: ChainView = field(default_factory=ChainView)
    banners: tuple[Banner, ...] = ()
    edge_types: tuple[str, ...] = ()
    edge_count: int = 0
    #: The coverage set actually read (D4) and how those reads ended, carried
    #: so the route can count them without re-deriving the plan.
    coverage: tuple[str, ...] = ()
    reads_ok: int = 0
    reads_truncated: int = 0
    reads_failed: int = 0
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
        return len(self.cycles) + len(self.external_cycles)
