"""The typed-edge half of a knowledge graph view (K2 D3, D6, D7, D16).

Pure, from the edge-table snapshot alone and before any Lithos read, so a
refused scope spends nothing (:mod:`lithos_lens.knowledge_graph` runs it first
and assembles the rest of the view around it):

- **Focus mode** walks the focus's typed edges, and at depth 2 each
  neighbour's, under the weight and provenance filters — applied before
  depth 2 expands and before the cap. The edge ``edge=`` / ``pin=`` selects is
  exempt from both, within ``depth`` hops even when a hidden edge is the only
  way to it.
- **Scoped-global mode** takes the rows of a ``type`` and/or ``namespace``.
- **The cap is a refusal** counting the focus and the typed endpoints (ghosts
  included), naming the first remedy that fits: depth 1, else the lowest
  ``min_weight`` in tenths, else none. Beside it: the would-be node count per
  depth, the hidden-edge counts and the provenance facets.
- **Expansion** (D16) runs on a base view that fit, against it and the focus's
  layer-only notes (:mod:`lithos_lens.knowledge_graph_expansion`); what the
  drawing shows — hidden counts, facets, the pinned selection — is recounted
  over the final typed set, while the would-be counts and the base refusal
  stay the base view's.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from lithos_lens.knowledge_edges import EdgeTableSnapshot, KnowledgeEdge
from lithos_lens.knowledge_graph_expansion import (
    KnowledgeExpansion,
    expand_typed,
    walk_expansions,
)
from lithos_lens.knowledge_graph_view import (
    DEFAULT_DEPTH,
    DEFAULT_FILTERS,
    MAX_DEPTH,
    PROVENANCE_GROUPS,
    HiddenEdgeCounts,
    KnowledgeGraphFilters,
    KnowledgeGraphRefusal,
    ProvenanceFacet,
    provenance_group,
)

# Mirror the ``[lithos-lens.knowledge]`` config defaults, as knowledge_edges
# does; ``tests/test_knowledge_facts.py`` pins both to the ones Config carries.
# The page passes the configured caps in.
DEFAULT_FOCUS_MAX_NODES = 250
DEFAULT_GLOBAL_MAX_NODES = 500

# Counts the unfiltered assembly the hidden counts are taken over.
_SHOW_ALL = KnowledgeGraphFilters(
    min_weight=float("-inf"), provenance=frozenset(PROVENANCE_GROUPS)
)


@dataclass(frozen=True)
class KnowledgeTypedGraph:
    """The typed-edge half of a view, from the snapshot alone, before any read.

    ``hops`` maps each node to its distance from the focus (0 for the focus;
    every node is 0 in global mode), in the order nodes were reached.
    """

    hops: Mapping[str, int]
    edges: tuple[KnowledgeEdge, ...]
    hidden: HiddenEdgeCounts = HiddenEdgeCounts()
    #: Node count each depth would draw under the current filters (focus mode).
    would_be_nodes: Mapping[int, int] = field(
        default_factory=lambda: MappingProxyType({})
    )
    provenance_facets: tuple[ProvenanceFacet, ...] = ()
    refusal: KnowledgeGraphRefusal | None = None
    #: The ``edge=`` / ``pin=`` selection when only its exemption draws it:
    #: the drawing differs from the plain filters'. ``""`` otherwise.
    pinned: str = ""
    #: Focus mode once the expansion pass has run (D16): its steps, ``via``,
    #: and every visible note's eligibility.
    expansion: KnowledgeExpansion | None = None


# ── typed assembly (pure, snapshot only) ───────────────────────────────


def _bfs(
    edges_of: Callable[[str], Sequence[KnowledgeEdge]],
    focus: str,
    depth: int,
    filters: KnowledgeGraphFilters,
) -> tuple[dict[str, int], dict[str, KnowledgeEdge]]:
    """BFS from the focus over the edges ``filters`` shows, ``depth`` hops."""
    hops = {focus: 0}
    edges: dict[str, KnowledgeEdge] = {}
    frontier = [focus]
    for hop in range(1, depth + 1):
        reached: list[str] = []
        for node_id in frontier:
            for edge in edges_of(node_id):
                if not filters.shows(edge):
                    continue
                edges.setdefault(edge.edge_id, edge)
                for end in edge.endpoints:
                    if end not in hops:
                        hops[end] = hop
                        reached.append(end)
        frontier = reached
    return hops, edges


def _ego(
    edges_of: Callable[[str], Sequence[KnowledgeEdge]],
    focus: str,
    depth: int,
    filters: KnowledgeGraphFilters,
) -> tuple[dict[str, int], dict[str, KnowledgeEdge]]:
    """:func:`_bfs`, plus the ``edge=`` selection when the filters hid the
    path to it: a selected edge within ``depth`` hops of the focus is drawn
    with its endpoints (at their unfiltered hop) whatever hides the way in."""
    hops, edges = _bfs(edges_of, focus, depth, filters)
    selected = filters.selected_edge
    if selected and selected not in edges:
        reach, unfiltered = _bfs(edges_of, focus, depth, _SHOW_ALL)
        edge = unfiltered.get(selected)
        if edge is not None:
            edges[selected] = edge
            for end in edge.endpoints:
                hops.setdefault(end, reach[end])
    return hops, edges


def _scoped_nodes(edges: Iterable[KnowledgeEdge]) -> dict[str, int]:
    return {end: 0 for edge in edges for end in edge.endpoints}


def _hidden(
    drawn: Mapping[str, KnowledgeEdge],
    unfiltered: Mapping[str, KnowledgeEdge],
    filters: KnowledgeGraphFilters,
) -> HiddenEdgeCounts:
    rows = unfiltered.values()
    return HiddenEdgeCounts(
        by_weight=sum(1 for edge in rows if filters.hides_by_weight(edge)),
        by_provenance=sum(1 for edge in rows if filters.hides_by_provenance(edge)),
        total=len(unfiltered.keys() - drawn.keys()),
    )


def _provenance_facets(
    rows: Iterable[KnowledgeEdge], filters: KnowledgeGraphFilters
) -> tuple[ProvenanceFacet, ...]:
    counts: Counter[str] = Counter()
    values: dict[str, set[str]] = {}
    for edge in rows:
        group = provenance_group(edge.provenance_type)
        counts[group] += 1
        # Only NULL is "null": an empty string is a value as stored.
        raw = "null" if edge.provenance_type is None else edge.provenance_type
        values.setdefault(group, set()).add(raw)
    return tuple(
        ProvenanceFacet(
            group,
            counts[group],
            tuple(sorted(values[group])),
            group in filters.provenance,
        )
        for group in PROVENANCE_GROUPS
        if counts[group]
    )


def _weight_remedy(
    count_at: Callable[[KnowledgeGraphFilters], int],
    filters: KnowledgeGraphFilters,
    cap: int,
) -> tuple[float, int] | None:
    """The lowest ``min_weight`` in tenths above the current one that fits."""
    for tenth in range(1, 11):
        weight = tenth / 10
        if weight <= filters.min_weight + 1e-9:
            continue
        count = count_at(replace(filters, min_weight=weight))
        if count <= cap:
            return weight, count
    return None


def ego_typed_graph(
    snapshot: EdgeTableSnapshot,
    focus: str,
    *,
    depth: int = DEFAULT_DEPTH,
    filters: KnowledgeGraphFilters = DEFAULT_FILTERS,
    max_nodes: int = DEFAULT_FOCUS_MAX_NODES,
) -> KnowledgeTypedGraph:
    """The focus's typed graph at ``depth``, filtered, with its cap verdict.

    A lookup over the snapshot's endpoint index: no Lithos call. The node
    count per depth is the same lookup at depth 1 and 2.
    """
    depth = min(max(depth, 1), MAX_DEPTH)
    hops, edges = _ego(snapshot.edges_of, focus, depth, filters)
    _, unfiltered = _bfs(snapshot.edges_of, focus, depth, _SHOW_ALL)
    selected = filters.selected_edge
    plain = replace(filters, selected_edge="")
    pinned = (
        selected
        if selected in edges
        and selected not in _bfs(snapshot.edges_of, focus, depth, plain)[1]
        else ""
    )

    def count_at(candidate: KnowledgeGraphFilters, at_depth: int = depth) -> int:
        return len(_ego(snapshot.edges_of, focus, at_depth, candidate)[0])

    would_be = {
        level: (len(hops) if level == depth else count_at(filters, level))
        for level in range(1, MAX_DEPTH + 1)
    }
    refusal = None
    if len(hops) > max_nodes:
        refusal = KnowledgeGraphRefusal("too_many_nodes", len(hops), max_nodes)
        if depth > 1 and would_be[1] <= max_nodes:
            refusal = replace(refusal, remedy_depth=1, remedy_count=would_be[1])
        elif (remedy := _weight_remedy(count_at, filters, max_nodes)) is not None:
            refusal = replace(
                refusal, remedy_min_weight=remedy[0], remedy_count=remedy[1]
            )
    return KnowledgeTypedGraph(
        hops=MappingProxyType(hops),
        edges=tuple(edges.values()),
        hidden=_hidden(edges, unfiltered, filters),
        would_be_nodes=MappingProxyType(would_be),
        provenance_facets=_provenance_facets(unfiltered.values(), filters),
        refusal=refusal,
        pinned=pinned,
    )


def expanded_typed_graph(
    snapshot: EdgeTableSnapshot,
    typed: KnowledgeTypedGraph,
    focus: str,
    *,
    depth: int = DEFAULT_DEPTH,
    filters: KnowledgeGraphFilters = DEFAULT_FILTERS,
    max_nodes: int = DEFAULT_FOCUS_MAX_NODES,
    layer_ids: Iterable[str] = (),
    expand: Sequence[str] = (),
) -> KnowledgeTypedGraph:
    """The capped base typed graph with the ``expand=`` requests applied (D16)
    against it and the focus's layer-only notes (``layer_ids``).

    What the drawing shows is recounted over the final typed set: the hidden
    counts (each applied note's unfiltered edges too), the provenance facets
    and the pinned selection. The would-be counts, and the base refusal they
    decide, stay the base view's. Pure: no read.
    """
    depth = min(max(depth, 1), MAX_DEPTH)
    layer_ids = tuple(layer_ids)
    expansion = expand_typed(
        snapshot.edges_of,
        filters.shows,
        focus=focus,
        hops=typed.hops,
        edges=typed.edges,
        layer_ids=layer_ids,
        requests=expand,
        cap=max_nodes,
    )
    _, unfiltered = _bfs(snapshot.edges_of, focus, depth, _SHOW_ALL)
    for root in expansion.expanded:
        for row in snapshot.edges_of(root):
            unfiltered.setdefault(row.edge_id, row)
    drawn = {edge.edge_id: edge for edge in expansion.edges}
    selected, pinned = filters.selected_edge, ""
    if selected in drawn:
        plain = replace(filters, selected_edge="")
        hops, edges = _bfs(snapshot.edges_of, focus, depth, plain)
        walk = walk_expansions(
            snapshot.edges_of,
            plain.shows,
            focus=focus,
            hops=hops,
            edges=edges.values(),
            layer_ids=layer_ids,
            requests=expand,
            cap=max_nodes,
        )
        pinned = "" if any(e.edge_id == selected for e in walk.edges) else selected
    return replace(
        typed,
        hops=expansion.hops,
        edges=expansion.edges,
        hidden=_hidden(drawn, unfiltered, filters),
        provenance_facets=_provenance_facets(unfiltered.values(), filters),
        pinned=pinned,
        expansion=expansion,
    )


def scoped_rows(
    snapshot: EdgeTableSnapshot, *, type: str | None, namespace: str | None
) -> tuple[KnowledgeEdge, ...]:
    """The snapshot rows of a ``type`` and/or ``namespace`` (both: their overlap)."""
    if type is None and namespace is None:
        raise ValueError("a scoped graph needs type= and/or namespace=")
    rows = (
        snapshot.of_type(type)
        if type is not None
        else snapshot.in_namespace(namespace or "")
    )
    if type is not None and namespace is not None:
        rows = tuple(row for row in rows if row.namespace == namespace)
    return rows


def global_typed_graph(
    rows: Sequence[KnowledgeEdge],
    *,
    filters: KnowledgeGraphFilters = DEFAULT_FILTERS,
    max_nodes: int = DEFAULT_GLOBAL_MAX_NODES,
) -> KnowledgeTypedGraph:
    """A scoped-global typed graph: the rows' endpoints, filtered, capped."""
    unfiltered = {row.edge_id: row for row in rows}
    edges = {key: row for key, row in unfiltered.items() if filters.shows(row)}
    hops = _scoped_nodes(edges.values())
    selected = edges.get(filters.selected_edge)
    plain = replace(filters, selected_edge="")
    pinned = "" if selected is None or plain.shows(selected) else selected.edge_id

    def count_at(candidate: KnowledgeGraphFilters) -> int:
        return len(_scoped_nodes(row for row in rows if candidate.shows(row)))

    refusal = None
    if len(hops) > max_nodes:
        refusal = KnowledgeGraphRefusal("too_many_nodes", len(hops), max_nodes)
        if (remedy := _weight_remedy(count_at, filters, max_nodes)) is not None:
            refusal = replace(
                refusal, remedy_min_weight=remedy[0], remedy_count=remedy[1]
            )
    return KnowledgeTypedGraph(
        hops=MappingProxyType(hops),
        edges=tuple(edges.values()),
        hidden=_hidden(edges, unfiltered, filters),
        provenance_facets=_provenance_facets(unfiltered.values(), filters),
        refusal=refusal,
        pinned=pinned,
    )
