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

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, Literal

from lithos_lens.knowledge_edge_types import EdgeDirection, LegendLine
from lithos_lens.knowledge_edges import KnowledgeEdge
from lithos_lens.knowledge_facts import FactsState, NoteFacts, NoteFactsTally

# Mirror the ``[lithos-lens.knowledge]`` config defaults, as knowledge_edges
# does; ``tests/test_knowledge_facts.py`` pins the one Config carries.
# ``graph_min_weight_default`` becomes a config knob with the page (S3).
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
    """The weight and provenance filters, applied before expansion and the cap."""

    min_weight: float = DEFAULT_MIN_WEIGHT
    #: Groups shown; all of :data:`PROVENANCE_GROUPS` by default.
    provenance: frozenset[str] = frozenset(PROVENANCE_GROUPS)

    def hides_by_weight(self, edge: KnowledgeEdge) -> bool:
        return edge.weight is not None and edge.weight < self.min_weight

    def hides_by_provenance(self, edge: KnowledgeEdge) -> bool:
        return provenance_group(edge.provenance_type) not in self.provenance

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
    facts_tally: NoteFactsTally = NoteFactsTally()
    #: The facts cap, set when nodes went unread for it.
    facts_capped_at: int = 0

    def node(self, node_id: str) -> KnowledgeGraphNode | None:
        return next((node for node in self.nodes if node.id == node_id), None)

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


# ── the payload ────────────────────────────────────────────────────────


def _node_payload(node: KnowledgeGraphNode) -> dict[str, Any]:
    facts = node.facts or NoteFacts()
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
    }


def graph_payload(view: KnowledgeGraphView) -> dict[str, Any]:
    """The JSON-ready payload the canvas draws from: the view's nodes and
    edges, legend, hidden counts, refusal and ``as_of``, nothing more."""
    refusal = view.refusal
    tally = view.facts_tally
    return {
        "mode": view.mode,
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
            "failed": tally.failed,
            "capped_at": view.facts_capped_at,
        },
        "as_of": view.as_of.isoformat() if view.as_of is not None else None,
        "stale": view.stale,
    }
