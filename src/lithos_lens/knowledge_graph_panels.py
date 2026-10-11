"""The knowledge graph's node and edge panels (K2 D10), and what they and the
text offer for an expansion (D16).

One drawn note's panel — its chips, its relations in view — or one drawn typed
edge's, built from the view model alone, so the page's panel and the
fragment a click fetches (``/knowledge/graph/panel``) are the same panel. The
expansion controls read the server's own eligibility and collapse sets off the
view (:mod:`lithos_lens.knowledge_graph_expansion`), never the drawn counts.

Split from :mod:`lithos_lens.knowledge_graph_view` as the panels' own reading
of the view: records there, what one selection shows of them here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from lithos_lens.knowledge_edge_evidence import (
    EdgeEvidence,
    parse_edge_evidence,
    provenance_label,
)
from lithos_lens.knowledge_edge_types import (
    RelationPhrase,
    conflict_state_label,
    is_conflict_resolved,
    relation_phrase,
)
from lithos_lens.knowledge_graph_view import (
    LAYER_LEGEND,
    KnowledgeEdgeEntry,
    KnowledgeEdgeSection,
    KnowledgeGraphEdge,
    KnowledgeGraphNode,
    KnowledgeGraphView,
    contradictions_queue,
    edge_entry,
    named_edge,
)
from lithos_lens.knowledge_metadata import NoteMetadata

#: Which panel is open: a note's, or a typed edge's.
PanelKind = Literal["node", "edge"]


def node_metadata(node: KnowledgeGraphNode | None) -> NoteMetadata | None:
    """A node's chips and lede through K1's ``NoteMetadata`` (S3 D7), so the
    status slug the chip's class is built from has one definition. ``None``
    for a node with no facts: a ghost, or a node not read for this view."""
    if node is None or node.facts is None:
        return None
    facts = node.facts
    return NoteMetadata(
        note_type=facts.note_type,
        status=facts.status,
        namespace=facts.namespace,
        confidence=facts.confidence,
        lede=facts.lede,
    )


@dataclass(frozen=True)
class KnowledgeNodePanel:
    """The node panel: one drawn note, its chips, and its relations in view.

    ``relations`` holds every drawn edge at the note — typed rows and the
    wiki-link and provenance pairs alike, so they add up to its degree —
    grouped by legend line in legend order, each read from the note.
    """

    node: KnowledgeGraphNode
    meta: NoteMetadata | None
    relations: tuple[KnowledgeEdgeSection, ...] = ()

    @property
    def kind(self) -> PanelKind:
        return "node"


@dataclass(frozen=True)
class KnowledgeEdgePanel:
    """The edge panel: one drawn typed edge, its row, and both endpoints."""

    entry: KnowledgeEdgeEntry
    source_meta: NoteMetadata | None = None
    target_meta: NoteMetadata | None = None

    @property
    def kind(self) -> PanelKind:
        return "edge"

    @property
    def edge(self) -> KnowledgeGraphEdge:
        return self.entry.edge

    @property
    def phrase(self) -> RelationPhrase:
        return relation_phrase(self.edge.type)

    @property
    def provenance(self) -> str:
        """The plain-language line K1's "why?" uses: "inferred by …"."""
        return provenance_label(self.edge.provenance, self.edge.provenance_actor)

    @property
    def evidence(self) -> EdgeEvidence | None:
        return parse_edge_evidence(self.edge.evidence)

    @property
    def is_contradiction(self) -> bool:
        return self.edge.type == "contradicts"

    @property
    def conflict_resolved(self) -> bool:
        return is_conflict_resolved(self.edge.conflict_state)

    @property
    def conflict_label(self) -> str:
        return conflict_state_label(self.edge.conflict_state)


def node_panel(view: KnowledgeGraphView, node_id: str) -> KnowledgeNodePanel | None:
    """The panel for ``node_id`` when the view draws it, else ``None``."""
    node = view.node(node_id) if node_id else None
    if node is None:
        return None
    at_node = [edge for edge in view.edges if node_id in (edge.from_id, edge.to_id)]
    layers = tuple(LAYER_LEGEND.values())
    groups: list[KnowledgeEdgeSection] = []
    for line in view.legend:
        if line in layers:
            edges = [edge for edge in at_node if edge.kind == line.type]
        else:
            edges = [e for e in at_node if e.kind == "typed" and e.type == line.type]
            if line.type == "contradicts":
                edges = list(contradictions_queue(edges))
        if edges:
            entries = tuple(edge_entry(view, edge, at=node_id) for edge in edges)
            groups.append(KnowledgeEdgeSection(line, entries))
    return KnowledgeNodePanel(node, node_metadata(node), tuple(groups))


def edge_panel(view: KnowledgeGraphView, edge_id: str) -> KnowledgeEdgePanel | None:
    """The panel for the typed edge ``edge_id`` names when drawn, else ``None``."""
    entry = named_edge(view, edge_id)
    if entry is None:
        return None
    return KnowledgeEdgePanel(
        entry, node_metadata(entry.source), node_metadata(entry.target)
    )


def graph_panel(
    view: KnowledgeGraphView, *, selected: str, edge: str
) -> KnowledgeNodePanel | KnowledgeEdgePanel | None:
    """The one panel a request selects: ``edge`` when given (it wins over
    ``selected``, S5 S1), else ``selected``; ``None`` when not drawn."""
    if edge:
        return edge_panel(view, edge)
    return node_panel(view, selected)


def expansion_dependants(view: KnowledgeGraphView, root: str) -> tuple[str, ...]:
    """The expansions removing ``root``'s request also removes — those whose
    note it first drew, transitively (D16 Collapse) — by their notes' labels
    in this view, in URL order. The displayed view's answer: a later request
    may differ once the data has changed."""
    collapse = view.collapses.get(root)
    if collapse is None:
        return ()
    labels = []
    for request in collapse.removed:
        if request != root:
            node = view.node(request)
            labels.append(node.label if node is not None else request)
    return tuple(labels)
