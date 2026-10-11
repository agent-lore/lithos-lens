"""The knowledge graph's view model and the JSON payload drawn from it (K2 D12).

What :mod:`lithos_lens.knowledge_graph` assembles, as the page renders it: the
filters a view was drawn under (weight and the provenance groups, D6/D7), its
nodes with their facts and degree in view (D8/D9), its edges — typed rows and
the one-hop wiki-link and provenance pairs — the legend, the hidden-edge
counts, the refusal when there is one, and ``as_of``. :func:`graph_payload`
is that view as the plain JSON-ready dict the canvas reads, so the text
baseline and the canvas cannot disagree about what is drawn.

Split from the assembly the way ``graph_view`` sits beside ``graph_scope`` on
the task graph: records here, the work that fills them there.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Final, Literal

from lithos_lens.knowledge_edge_evidence import parse_edge_evidence
from lithos_lens.knowledge_edge_types import (
    EdgeDirection,
    LegendLine,
    edge_style,
    is_conflict_resolved,
)
from lithos_lens.knowledge_edges import KnowledgeEdge
from lithos_lens.knowledge_facts import FactsState, NoteFacts, NoteFactsTally
from lithos_lens.knowledge_graph_expansion import (
    ExpansionCollapse,
    ExpansionStep,
    NodeExpansion,
)

# Mirror the ``[lithos-lens.knowledge]`` config defaults, as knowledge_edges
# does; ``tests/test_knowledge_facts.py`` pins them to the ones Config
# carries. The page passes the configured ``graph_min_weight_default`` in.
DEFAULT_DEPTH = 1
MAX_DEPTH = 2
DEFAULT_MIN_WEIGHT = 0.1

#: The provenance filter's groups, in the order the page offers them (D7).
PROVENANCE_GROUPS: tuple[str, ...] = ("inferred", "reinforced", "declared", "other")

# ``reinforcement`` is the demo fixture's spelling of citation reinforcement;
# the live corpus writes ``consolidation`` (knowledge_edge_evidence words it
# "reinforced by citation"). Everything else — authored, manual*,
# conversation-derived, an unknown value, NULL — is ``other``.
_PROVENANCE_GROUP_OF = {
    "inferred": "inferred",
    "consolidation": "reinforced",
    "reinforcement": "reinforced",
    "frontmatter": "declared",
}

#: The pseudo-types the two one-hop layers draw as.
WIKI_LINK: Final = "wiki_link"
PROVENANCE: Final = "provenance"

#: The legend lines the two layers add when drawn, each saying it is one hop.
LAYER_LEGEND: Mapping[str, LegendLine] = {
    WIKI_LINK: LegendLine(
        WIKI_LINK,
        "kedge-wiki-link",
        "A links to B: a wiki-link written in A (shown one hop from the focus only)",
    ),
    PROVENANCE: LegendLine(
        PROVENANCE,
        "kedge-provenance",
        "A derived from B: B is a source A's frontmatter declares "
        "(shown one hop from the focus only)",
    ),
}

#: Why a scope was refused: over the node cap; the edge table is over its
#: bound (no endpoint-filtered read exists); the table could not be read.
RefusalReason = Literal["too_many_nodes", "table_refused", "unavailable"]

EdgeKind = Literal["typed", "wiki_link", "provenance"]

LayerRelation = Literal["links_to", "linked_from", "source", "derived"]


def provenance_group(provenance_type: str | None) -> str:
    """The filter group a row's ``provenance_type`` falls in."""
    return _PROVENANCE_GROUP_OF.get(provenance_type or "", "other")


@dataclass(frozen=True)
class KnowledgeGraphFilters:
    """The weight and provenance filters, applied before expansion and the cap.

    The edge ``edge=`` selects is exempt from both (D13): a link to its panel
    draws it whatever its weight is now and whatever the filters say, and it
    is not counted hidden. It still counts towards the cap like any drawn edge.
    """

    min_weight: float = DEFAULT_MIN_WEIGHT
    #: Groups shown; all of :data:`PROVENANCE_GROUPS` by default.
    provenance: frozenset[str] = frozenset(PROVENANCE_GROUPS)
    #: The ``edge=`` selection, never hidden; ``""`` for none.
    selected_edge: str = ""
    #: It is a ``pin=`` (beside ``selected=``), not an ``edge=``: left undrawn
    #: by the expansions, it exempts nothing, as the page's links drop it (D16).
    selected_is_pin: bool = False

    def hides_by_weight(self, edge: KnowledgeEdge) -> bool:
        return (
            edge.weight is not None
            and edge.weight < self.min_weight
            and edge.edge_id != self.selected_edge
        )

    def hides_by_provenance(self, edge: KnowledgeEdge) -> bool:
        return (
            provenance_group(edge.provenance_type) not in self.provenance
            and edge.edge_id != self.selected_edge
        )

    def shows(self, edge: KnowledgeEdge) -> bool:
        return not (self.hides_by_weight(edge) or self.hides_by_provenance(edge))


#: The filters a scope is drawn under when the request names none.
DEFAULT_FILTERS = KnowledgeGraphFilters()


@dataclass(frozen=True)
class HiddenEdgeCounts:
    """Typed edges the filters hide, counted over the unfiltered assembly.

    The two are counted independently (an edge both faint and filtered by
    provenance is in both); ``total`` is the distinct edges not drawn, which
    at depth 2 includes edges whose only way in was a hidden one.
    """

    by_weight: int = 0
    by_provenance: int = 0
    total: int = 0


@dataclass(frozen=True)
class ProvenanceFacet:
    """One provenance group the filter offers: its rows and the raw values."""

    group: str
    count: int
    #: The raw ``provenance_type`` values in the group; ``null`` for NULL.
    values: tuple[str, ...]
    shown: bool = True


@dataclass(frozen=True)
class KnowledgeGraphRefusal:
    """Why the graph was not drawn, and — when one fits — what would draw it.

    ``count`` is the node count against ``cap`` for ``too_many_nodes``, and
    the table's row count against its bound for ``table_refused``. At most
    one remedy is set, with the node count it would give.
    """

    reason: RefusalReason
    count: int = 0
    cap: int = 0
    remedy_depth: int | None = None
    remedy_min_weight: float | None = None
    remedy_count: int = 0

    @property
    def has_remedy(self) -> bool:
        return self.remedy_depth is not None or self.remedy_min_weight is not None

    @property
    def message(self) -> str:
        if self.reason == "unavailable":
            return "The knowledge edge table could not be read."
        if self.reason == "table_refused":
            return (
                f"Graph too large to index: {self.count} edges, over the "
                f"{self.cap} bound. Only type= or namespace= scopes can be drawn."
            )
        head = f"{self.count} notes in this view, over the {self.cap} cap."
        if self.remedy_depth is not None:
            return f"{head} depth={self.remedy_depth} shows {self.remedy_count}."
        if self.remedy_min_weight is not None:
            return (
                f"{head} min_weight={self.remedy_min_weight:.1f} "
                f"shows {self.remedy_count}."
            )
        return f"{head} No depth or weight filter brings it under; narrow the scope."


@dataclass(frozen=True)
class KnowledgeLayerRef:
    """One wiki-link or provenance neighbour of the focus, as K1 names it.

    ``drawn_as_typed`` marks a provenance pair the typed graph already draws
    as a ``derived_from`` edge: it is listed, not drawn twice.
    """

    id: str
    title: str
    relation: LayerRelation
    drawn_as_typed: bool = False


@dataclass(frozen=True)
class KnowledgeGraphNode:
    """One drawn note: its facts and their state, and its place in the view."""

    id: str
    label: str
    facts_state: FactsState
    facts: NoteFacts | None = None
    #: Drawn edges at this node: typed, wiki-link and provenance (D8 sizing).
    degree: int = 0
    hop: int = 0
    is_focus: bool = False
    #: Reached only by a wiki-link or provenance edge (no facts read spent).
    layer_only: bool = False
    #: D16: expanded by an applied ``expand=``; the request that first drew
    #: it (``None`` for the base view); its eligibility (focus mode only).
    expanded: bool = False
    via: str | None = None
    expansion: NodeExpansion | None = None

    @property
    def is_ghost(self) -> bool:
        """A missing note: drawn dashed with its short id, never dropped."""
        return self.facts_state == "missing"


@dataclass(frozen=True)
class KnowledgeGraphEdge:
    """One drawn edge: a typed row from the snapshot, or a one-hop layer pair."""

    id: str
    from_id: str
    to_id: str
    kind: EdgeKind
    type: str
    direction: EdgeDirection
    weight: float | None = None
    provenance: str | None = None
    conflict_state: str | None = None
    partial: bool = False
    #: The row's columns the text baseline and the edge panel print and the
    #: payload leaves out (the contradictions queue, D11; the panel, D10);
    #: ``None`` on a layer pair.
    namespace: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    provenance_actor: str | None = None
    evidence: str | None = None


@dataclass(frozen=True)
class KnowledgeGraphView:
    """Everything the page renders for one scope, refused or drawn."""

    mode: Literal["focus", "global"]
    filters: KnowledgeGraphFilters
    focus_id: str = ""
    depth: int = DEFAULT_DEPTH
    scope_type: str | None = None
    scope_namespace: str | None = None
    nodes: tuple[KnowledgeGraphNode, ...] = ()
    edges: tuple[KnowledgeGraphEdge, ...] = ()
    legend: tuple[LegendLine, ...] = ()
    wiki_links: tuple[KnowledgeLayerRef, ...] = ()
    backlinks: tuple[KnowledgeLayerRef, ...] = ()
    sources: tuple[KnowledgeLayerRef, ...] = ()
    derived: tuple[KnowledgeLayerRef, ...] = ()
    #: The ``lithos_related`` read failed: no wiki-link or provenance layer.
    layers_unavailable: bool = False
    hidden: HiddenEdgeCounts = HiddenEdgeCounts()
    would_be_nodes: Mapping[int, int] = field(
        default_factory=lambda: MappingProxyType({})
    )
    provenance_facets: tuple[ProvenanceFacet, ...] = ()
    refusal: KnowledgeGraphRefusal | None = None
    as_of: datetime | None = None
    #: The snapshot is the last good one; its refetch failed.
    stale: bool = False
    #: Global mode over the table's bound: the rows came from a direct
    #: filtered read stamped now, so no snapshot TTL applies to them.
    read_directly: bool = False
    #: ``edge=`` names an edge the snapshot does not hold (created after
    #: ``as_of``, or since deleted): the panel host says so.
    selected_edge_missing: bool = False
    #: The selected edge whose exemption changes this view (D13, D16): its
    #: drawing or its offers. A panel link keeps the view only while it
    #: selects that edge.
    pinned: str = ""
    #: Drawn edges whose own pin would redraw this view (D16): reached within
    #: ``depth`` only through a hidden edge and drawn by an expansion.
    pin_redraws: frozenset[str] = frozenset()

    def keeps_drawing_for(self, pin: str, *, is_pin: bool = False) -> bool:
        """A request selecting ``pin`` (its ``edge=``, or its ``pin=`` when
        ``is_pin``) draws this very view: the selection it was drawn under
        (the same filters, even once the edge has gone from the data — or of
        either kind while drawn: a drawn pin is kept); or, on a view no
        exemption changed (``pinned`` empty), none, a drawn edge whose own
        pin changes nothing, or an undrawn ``pin=`` of its own, which is
        discarded. Anything else may draw differently (an exemption lost or
        gained), so it is not answered from this view."""
        drawn = named_edge(self, pin) is not None
        selected = self.filters.selected_edge
        if (
            pin
            and pin == selected
            and (drawn or is_pin == self.filters.selected_is_pin)
        ):
            return True
        if self.pinned:
            return False
        if drawn:
            return pin not in self.pin_redraws
        return not pin or (is_pin and pin == selected)

    facts_tally: NoteFactsTally = NoteFactsTally()
    #: The facts cap, set when nodes went unread for it.
    facts_capped_at: int = 0
    #: D16: the ``expand=`` requests in URL order, and what removing each
    #: (with its ``via`` dependants) leaves drawn.
    expansions: tuple[ExpansionStep, ...] = ()
    collapses: Mapping[str, ExpansionCollapse] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def node(self, node_id: str) -> KnowledgeGraphNode | None:
        return next((node for node in self.nodes if node.id == node_id), None)

    @property
    def typed_legend(self) -> tuple[LegendLine, ...]:
        """The legend's typed-edge lines. A stored type may be spelled like a
        layer (``wiki_link``), so a line is told apart by what it is, not by
        its ``type`` string."""
        layers = tuple(LAYER_LEGEND.values())
        return tuple(line for line in self.legend if line not in layers)

    @property
    def layer_legend(self) -> tuple[LegendLine, ...]:
        """The legend's wiki-link and provenance layer lines, when drawn."""
        layers = tuple(LAYER_LEGEND.values())
        return tuple(line for line in self.legend if line in layers)

    @property
    def unresolved_contradictions(self) -> int:
        """Drawn typed ``contradicts`` edges not yet resolved (the toolbar's count)."""
        return sum(
            1
            for edge in self.edges
            if edge.kind == "typed"
            and edge.type == "contradicts"
            and not is_conflict_resolved(edge.conflict_state)
        )

    @property
    def ghosts(self) -> tuple[KnowledgeGraphNode, ...]:
        return tuple(node for node in self.nodes if node.is_ghost)

    @property
    def edges_to_missing(self) -> tuple[KnowledgeGraphEdge, ...]:
        """Typed edges with a missing endpoint (the text baseline's list)."""
        missing = {node.id for node in self.ghosts}
        return tuple(
            edge
            for edge in self.edges
            if edge.kind == "typed" and {edge.from_id, edge.to_id} & missing
        )


# ── the contradictions queue (D11) ─────────────────────────────────────

# Up to and including the first sentence mark followed by whitespace or the end.
_FIRST_SENTENCE = re.compile(r"^(.*?[.!?])(?=\s|$)", re.DOTALL)


def first_sentence(text: str) -> str:
    """``text`` up to and including its first ``.``, ``!`` or ``?`` that is
    followed by whitespace or the end; the whole (stripped) text without one.

    So "v1.2 is out. More" is "v1.2 is out." — a mark inside a token is not
    a sentence end.
    """
    text = text.strip()
    match = _FIRST_SENTENCE.match(text)
    return match.group(1) if match else text


def edge_rationale(evidence: str | None) -> str:
    """The first sentence of an edge's rationale, for the queue's line.

    The ``rationale`` of the parsed evidence JSON when it has one, else the
    raw text of evidence that is not a JSON object; ``""`` when there is
    neither (null evidence, or an object with no rationale).
    """
    parsed = parse_edge_evidence(evidence)
    if parsed is None:
        return ""
    return first_sentence(parsed.rationale or parsed.raw)


def _created_desc(created_at: str | None) -> tuple[int, float]:
    """A sort key putting the newest ``created_at`` first, missing last."""
    if not created_at:
        return (1, 0.0)
    try:
        stamp = datetime.fromisoformat(created_at)
    except ValueError:
        return (1, 0.0)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (0, -stamp.timestamp())


def contradictions_queue(
    edges: Iterable[KnowledgeGraphEdge],
) -> tuple[KnowledgeGraphEdge, ...]:
    """The ``contradicts`` edges as the queue lists them (PRD story 13).

    Unresolved first — anything ``lithos_conflict_resolve`` did not write,
    NULL and a caller's ``"pending"`` alike (``is_conflict_resolved``) — then
    newest ``created_at`` first within each state, rows with no (or an
    unreadable) timestamp last, then by ``edge_id`` so the order is total.
    """
    return tuple(
        sorted(
            (edge for edge in edges if edge.type == "contradicts"),
            key=lambda edge: (
                is_conflict_resolved(edge.conflict_state),
                _created_desc(edge.created_at),
                edge.id,
            ),
        )
    )


# ── the text baseline (D12) ────────────────────────────────────────────


@dataclass(frozen=True)
class KnowledgeEdgeEntry:
    """One typed edge as the text baseline lists it.

    An edge at the focus reads from the focus: ``→`` (outgoing), ``←``
    (incoming) or ``↔`` (symmetric) and the node at the other end, ``other``.
    Any other edge — depth 2's neighbour-to-neighbour edges, every edge in
    global mode — reads ``source → target`` or ``source ↔ target``. A type
    drawn as recorded reads as directed.
    """

    edge: KnowledgeGraphEdge
    arrow: str
    source: KnowledgeGraphNode
    target: KnowledgeGraphNode
    other: KnowledgeGraphNode | None = None

    @property
    def weight_label(self) -> str:
        weight = self.edge.weight
        return "weight unknown" if weight is None else f"{weight:.2f}"

    @property
    def rationale(self) -> str:
        """The first sentence of the edge's rationale (the queue's segment)."""
        return edge_rationale(self.edge.evidence)


@dataclass(frozen=True)
class KnowledgeEdgeSection:
    """One edge type's entries, under its legend line."""

    line: LegendLine
    entries: tuple[KnowledgeEdgeEntry, ...]


def _placeholder(node_id: str) -> KnowledgeGraphNode:
    return KnowledgeGraphNode(id=node_id, label=node_id, facts_state="unread")


def _entry(
    nodes: Mapping[str, KnowledgeGraphNode], focus: str, edge: KnowledgeGraphEdge
) -> KnowledgeEdgeEntry:
    source = nodes.get(edge.from_id) or _placeholder(edge.from_id)
    target = nodes.get(edge.to_id) or _placeholder(edge.to_id)
    symmetric = edge.direction is EdgeDirection.SYMMETRIC
    if focus and edge.from_id == focus:
        arrow = "↔" if symmetric else "→"
        return KnowledgeEdgeEntry(edge, arrow, source, target, target)
    if focus and edge.to_id == focus:
        arrow = "↔" if symmetric else "←"
        return KnowledgeEdgeEntry(edge, arrow, source, target, source)
    return KnowledgeEdgeEntry(edge, "↔" if symmetric else "→", source, target)


def _node_index(view: KnowledgeGraphView) -> dict[str, KnowledgeGraphNode]:
    return {node.id: node for node in view.nodes}


def _focus_of(view: KnowledgeGraphView) -> str:
    return view.focus_id if view.mode == "focus" else ""


def edge_entry(
    view: KnowledgeGraphView, edge: KnowledgeGraphEdge, at: str = ""
) -> KnowledgeEdgeEntry:
    """How ``edge`` reads in ``view``'s text: arrow, ends, and — at the
    focus, or at the node ``at`` names (a node panel) — the node at the
    other end."""
    return _entry(_node_index(view), at or _focus_of(view), edge)


def edge_sections(view: KnowledgeGraphView) -> tuple[KnowledgeEdgeSection, ...]:
    """The typed edges, one section per type in legend (D5) order.

    Within a type, the snapshot's order — except ``contradicts``, which is
    always the queue's: unresolved first, newest first
    (:func:`contradictions_queue`).
    """
    nodes, focus = _node_index(view), _focus_of(view)
    sections: list[KnowledgeEdgeSection] = []
    for line in view.typed_legend:
        edges = [e for e in view.edges if e.kind == "typed" and e.type == line.type]
        if not edges:
            continue
        if line.type == "contradicts":
            edges = list(contradictions_queue(edges))
        sections.append(
            KnowledgeEdgeSection(
                line, tuple(_entry(nodes, focus, edge) for edge in edges)
            )
        )
    return tuple(sections)


def named_edge(view: KnowledgeGraphView, edge_id: str) -> KnowledgeEdgeEntry | None:
    """The typed edge ``edge=`` names when the view draws it, else ``None``."""
    if not edge_id:
        return None
    edge = next((e for e in view.edges if e.kind == "typed" and e.id == edge_id), None)
    return None if edge is None else edge_entry(view, edge)


# ── the payload ────────────────────────────────────────────────────────


def _node_payload(node: KnowledgeGraphNode) -> dict[str, Any]:
    facts = node.facts or NoteFacts()
    expansion = node.expansion
    return {
        "id": node.id,
        "label": node.label,
        "facts_state": node.facts_state,
        "ghost": node.is_ghost,
        "focus": node.is_focus,
        "layer_only": node.layer_only,
        "hop": node.hop,
        "degree": node.degree,
        "title": facts.title,
        "note_type": facts.note_type,
        "status": facts.status,
        "namespace": facts.namespace,
        "confidence": facts.confidence,
        "lede": facts.lede,
        "expanded": node.expanded,
        "via": node.via,
        "undrawn_nodes": expansion.undrawn_nodes if expansion else 0,
        "undrawn_edges": expansion.undrawn_edges if expansion else 0,
        "expansion": None
        if expansion is None
        else {
            "state": expansion.state,
            "would_count": expansion.would_count,
            "cap": expansion.cap,
        },
    }


def _step_payload(step: ExpansionStep) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": step.id,
        "state": step.state,
        "added_nodes": step.added_nodes,
        "added_edges": step.added_edges,
    }
    if step.state == "refused":
        payload["would_count"] = step.would_count
    return payload


def _edge_style_payload(edge: KnowledgeGraphEdge) -> dict[str, Any]:
    """How the canvas draws ``edge`` (S4 D6): a typed row by ``edge_style``, a
    layer pair by its legend class — told apart by ``kind``, never by
    ``type``, since a stored type may be spelled like a layer."""
    if edge.kind == "typed":
        style = edge_style(edge)
        return {
            "class": style.css_class,
            "stroke": style.stroke,
            "arrowhead": style.arrowhead,
            "label": style.label,
        }
    return {
        "class": LAYER_LEGEND[edge.kind].css_class,
        "stroke": "solid" if edge.kind == WIKI_LINK else "dotted",
        "arrowhead": True,
        "label": "",
    }


def _edge_payload(edge: KnowledgeGraphEdge) -> dict[str, Any]:
    return {
        "id": edge.id,
        "from": edge.from_id,
        "to": edge.to_id,
        "kind": edge.kind,
        "type": edge.type,
        "weight": edge.weight,
        "provenance": edge.provenance,
        "provenance_group": (
            provenance_group(edge.provenance) if edge.kind == "typed" else None
        ),
        "conflict_state": edge.conflict_state,
        "direction": edge.direction.value,
        "partial": edge.partial,
        "style": _edge_style_payload(edge),
    }


def graph_payload(
    view: KnowledgeGraphView, *, colour: str = "namespace"
) -> dict[str, Any]:
    """The JSON-ready payload the canvas draws from: the view's nodes and
    edges with each edge's style, legend, hidden counts, provenance facets,
    unresolved-contradictions count, refusal and ``as_of`` — and the node
    ``colour`` mode the request asked for, nothing more."""
    refusal = view.refusal
    tally = view.facts_tally
    return {
        "mode": view.mode,
        "colour": colour,
        "focus": view.focus_id or None,
        "depth": view.depth,
        "scope": {"type": view.scope_type, "namespace": view.scope_namespace},
        "filters": {
            "min_weight": view.filters.min_weight,
            "provenance": [
                group for group in PROVENANCE_GROUPS if group in view.filters.provenance
            ],
        },
        "nodes": [_node_payload(node) for node in view.nodes],
        "edges": [_edge_payload(edge) for edge in view.edges],
        "expansions": [_step_payload(step) for step in view.expansions],
        "legend": [
            {
                "type": line.type,
                "class": line.css_class,
                "line": line.line,
                "known": line.known,
            }
            for line in view.legend
        ],
        "hidden": {
            "by_weight": view.hidden.by_weight,
            "by_provenance": view.hidden.by_provenance,
            "total": view.hidden.total,
        },
        "would_be_nodes": {
            str(level): count for level, count in view.would_be_nodes.items()
        },
        "provenance_facets": [
            {"group": facet.group, "count": facet.count, "shown": facet.shown}
            for facet in view.provenance_facets
        ],
        "unresolved_contradictions": view.unresolved_contradictions,
        "refusal": None
        if refusal is None
        else {
            "reason": refusal.reason,
            "count": refusal.count,
            "cap": refusal.cap,
            "remedy_depth": refusal.remedy_depth,
            "remedy_min_weight": refusal.remedy_min_weight,
            "remedy_count": refusal.remedy_count,
            "message": refusal.message,
        },
        "layers_unavailable": view.layers_unavailable,
        "facts": {
            "hits": tally.hits,
            "reads": tally.reads,
            "missing": tally.missing,
            "capped": tally.capped,
            "capped_unread": tally.capped_unread,
            "capped_pending": tally.capped_pending,
            "failed": tally.failed,
            "capped_at": view.facts_capped_at,
        },
        "as_of": view.as_of.isoformat() if view.as_of is not None else None,
        "stale": view.stale,
    }
