"""Knowledge graph assembly: focus (ego) and scoped-global views (K2 D3, D6–D9, D11).

Everything here is assembled from the edge-table snapshot
(:mod:`lithos_lens.knowledge_edges`), one ``lithos_related`` neighbourhood and
the note facts cache (:mod:`lithos_lens.knowledge_facts`), so the page (S3)
only renders:

- **Typed edges** from the snapshot. Focus mode takes the focus's edges and, at
  depth 2, each neighbour's; scoped-global mode takes the rows of a ``type``
  and/or ``namespace``. Nodes are the endpoints.
- **Filters before everything else.** ``min_weight`` (default
  ``[knowledge].graph_min_weight_default``, 0.1) and the provenance groups
  (inferred / reinforced / declared / other) hide edges BEFORE depth 2 expands
  and before the cap, so a hidden 0.03 consolidation edge pulls in nothing and
  "hide faint edges" is a way under the cap. An edge with no known weight
  (NULL upstream, or a ``partial`` row) is never hidden by weight: unknown is
  not faint.
- **The cap is a refusal**, checked before any ``lithos_related`` call or facts
  read so a refused scope spends nothing. It counts the focus and the typed
  endpoints (ghosts included) and names the first remedy that fits: depth 1,
  else the lowest ``min_weight`` in tenths, else none.
- **Wiki-links and provenance** come from one ``related(focus)`` and are one
  hop whatever the depth (``lithos_related`` at depth 2 answers a flat set with
  no pairs to draw). Their nodes take the inline title and spend no facts read.
  A failed call drops both layers and says so; a ``doc_not_found`` makes the
  focus a ghost whose typed edges are still drawn.
- **Facts** for the typed nodes from the cache, read in priority order: the
  focus, then by hop, then by degree in view, then id (global: degree, id).
  A missing note is a ghost — short-id label, dashed, never dropped, and listed
  under "edges to missing notes".

The records it fills, and the payload, are in
:mod:`lithos_lens.knowledge_graph_view`.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Literal

from lithos_lens.knowledge import RelatedNeighborhood, RelatedRef
from lithos_lens.knowledge_edge_types import EdgeDirection, direction_of, legend
from lithos_lens.knowledge_edges import (
    EdgeTable,
    EdgeTableRefusal,
    EdgeTableSnapshot,
    KnowledgeEdge,
)
from lithos_lens.knowledge_facts import NoteFactsBatch, NoteFactsCache, is_doc_not_found
from lithos_lens.knowledge_graph_view import (
    DEFAULT_DEPTH,
    DEFAULT_FILTERS,
    LAYER_LEGEND,
    MAX_DEPTH,
    PROVENANCE,
    PROVENANCE_GROUPS,
    WIKI_LINK,
    EdgeKind,
    HiddenEdgeCounts,
    KnowledgeGraphEdge,
    KnowledgeGraphFilters,
    KnowledgeGraphNode,
    KnowledgeGraphRefusal,
    KnowledgeGraphView,
    KnowledgeLayerRef,
    LayerRelation,
    ProvenanceFacet,
    RefusalReason,
    provenance_group,
)

logger = logging.getLogger(__name__)

# Mirror the ``[lithos-lens.knowledge]`` config defaults, as knowledge_edges
# does; ``tests/test_knowledge_facts.py`` pins the one Config carries.
# ``graph_global_max_nodes`` becomes a config knob with the page (S3), which
# passes it in.
DEFAULT_FOCUS_MAX_NODES = 250
DEFAULT_GLOBAL_MAX_NODES = 500

# Counts the unfiltered assembly the hidden counts are taken over.
_SHOW_ALL = KnowledgeGraphFilters(
    min_weight=float("-inf"), provenance=frozenset(PROVENANCE_GROUPS)
)

#: The injected ``lithos_related`` read: ``client.related``.
RelatedRead = Callable[[str], Awaitable[RelatedNeighborhood]]

#: Injectable wall clock, for a filtered read's ``as_of``.
Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


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


# ── typed assembly (pure, snapshot only) ───────────────────────────────


def _expand(
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
        values.setdefault(group, set()).add(edge.provenance_type or "null")
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
    hops, edges = _expand(snapshot.edges_of, focus, depth, filters)
    _, unfiltered = _expand(snapshot.edges_of, focus, depth, _SHOW_ALL)

    def count_at(candidate: KnowledgeGraphFilters, at_depth: int = depth) -> int:
        return len(_expand(snapshot.edges_of, focus, at_depth, candidate)[0])

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
    )


# ── layers, degree and read order ──────────────────────────────────────


def _layer_refs(
    refs: Iterable[RelatedRef], relation: LayerRelation, focus: str
) -> tuple[KnowledgeLayerRef, ...]:
    seen: dict[str, KnowledgeLayerRef] = {}
    for ref in refs:
        if ref.id and ref.id != focus and ref.id not in seen:
            seen[ref.id] = KnowledgeLayerRef(ref.id, ref.title, relation)
    return tuple(seen.values())


@dataclass(frozen=True)
class _Layers:
    wiki_links: tuple[KnowledgeLayerRef, ...] = ()
    backlinks: tuple[KnowledgeLayerRef, ...] = ()
    sources: tuple[KnowledgeLayerRef, ...] = ()
    derived: tuple[KnowledgeLayerRef, ...] = ()
    edges: tuple[KnowledgeGraphEdge, ...] = ()


def _layers(
    focus: str, neighborhood: RelatedNeighborhood, typed: Sequence[KnowledgeEdge]
) -> _Layers:
    """The one-hop wiki-link and provenance pairs, minus those typed already."""
    typed_derivations = {
        (edge.from_id, edge.to_id) for edge in typed if edge.type == "derived_from"
    }
    edges: list[KnowledgeGraphEdge] = []

    def pair(kind: EdgeKind, from_id: str, to_id: str) -> None:
        edges.append(
            KnowledgeGraphEdge(
                id=f"{kind}:{from_id}->{to_id}",
                from_id=from_id,
                to_id=to_id,
                kind=kind,
                type=kind,
                direction=EdgeDirection.DIRECTED,
            )
        )

    links = _layer_refs(neighborhood.links, "links_to", focus)
    backlinks = _layer_refs(neighborhood.backlinks, "linked_from", focus)
    for ref in links:
        pair(WIKI_LINK, focus, ref.id)
    for ref in backlinks:
        pair(WIKI_LINK, ref.id, focus)

    def provenance(
        refs: tuple[KnowledgeLayerRef, ...],
    ) -> tuple[KnowledgeLayerRef, ...]:
        listed: list[KnowledgeLayerRef] = []
        for ref in refs:
            # derived -> source, the way the typed derived_from row runs.
            ends = (focus, ref.id) if ref.relation == "source" else (ref.id, focus)
            drawn = ends in typed_derivations
            listed.append(replace(ref, drawn_as_typed=drawn))
            if not drawn:
                pair(PROVENANCE, *ends)
        return tuple(listed)

    sources = provenance(_layer_refs(neighborhood.sources, "source", focus))
    derived = provenance(_layer_refs(neighborhood.derived, "derived", focus))
    return _Layers(links, backlinks, sources, derived, tuple(edges))


def _degrees(edges: Iterable[KnowledgeGraphEdge]) -> Counter[str]:
    degree: Counter[str] = Counter()
    for edge in edges:
        for end in {edge.from_id, edge.to_id}:
            degree[end] += 1
    return degree


def _typed_edge(edge: KnowledgeEdge) -> KnowledgeGraphEdge:
    return KnowledgeGraphEdge(
        id=edge.edge_id,
        from_id=edge.from_id,
        to_id=edge.to_id,
        kind="typed",
        type=edge.type,
        direction=direction_of(edge),
        weight=edge.weight,
        provenance=edge.provenance_type,
        conflict_state=edge.conflict_state,
        partial=edge.partial,
    )


def read_order(
    typed: KnowledgeTypedGraph, drawn: Iterable[KnowledgeGraphEdge] = ()
) -> list[str]:
    """The typed nodes in facts-read priority: hop, then degree in view desc,
    then id. The focus is hop 0, so it is first; in global mode every node is
    hop 0 and the order is degree, then id."""
    degree = _degrees(drawn)
    return sorted(typed.hops, key=lambda node: (typed.hops[node], -degree[node], node))


# ── the view model ─────────────────────────────────────────────────────


def build_view(
    typed: KnowledgeTypedGraph,
    *,
    mode: Literal["focus", "global"],
    filters: KnowledgeGraphFilters,
    focus_id: str = "",
    depth: int = DEFAULT_DEPTH,
    scope_type: str | None = None,
    scope_namespace: str | None = None,
    neighborhood: RelatedNeighborhood | None = None,
    facts: NoteFactsBatch | None = None,
    layers_unavailable: bool = False,
    focus_missing: bool = False,
    as_of: datetime | None = None,
    stale: bool = False,
) -> KnowledgeGraphView:
    """The view model from a typed graph, the focus's layers and the facts.

    A refused typed graph yields a view with the refusal and the counts and
    no nodes. ``facts`` absent leaves every node ``unread`` (labelled by id);
    ``focus_missing`` (``lithos_related`` said ``doc_not_found``) makes the
    focus a ghost whatever its facts say.
    """
    base = KnowledgeGraphView(
        mode=mode,
        filters=filters,
        focus_id=focus_id,
        depth=depth,
        scope_type=scope_type,
        scope_namespace=scope_namespace,
        hidden=typed.hidden,
        would_be_nodes=typed.would_be_nodes,
        provenance_facets=typed.provenance_facets,
        refusal=typed.refusal,
        as_of=as_of,
        stale=stale,
        layers_unavailable=layers_unavailable,
    )
    if typed.refusal is not None:
        return base
    layers = (
        _layers(focus_id, neighborhood, typed.edges)
        if mode == "focus" and neighborhood is not None
        else _Layers()
    )
    edges = tuple(_typed_edge(edge) for edge in typed.edges) + layers.edges
    degree = _degrees(edges)
    batch = facts or NoteFactsBatch()
    nodes: list[KnowledgeGraphNode] = []
    for node_id, hop in typed.hops.items():
        answer = batch.for_id(node_id)
        if focus_missing and node_id == focus_id:
            answer = replace(answer, state="missing", facts=None)
        nodes.append(
            KnowledgeGraphNode(
                id=node_id,
                label=answer.label,
                facts_state=answer.state,
                facts=answer.facts,
                degree=degree[node_id],
                hop=hop,
                is_focus=mode == "focus" and node_id == focus_id,
            )
        )
    placed = set(typed.hops)
    for ref in (
        *layers.wiki_links,
        *layers.backlinks,
        *layers.sources,
        *layers.derived,
    ):
        if ref.id in placed:
            continue
        placed.add(ref.id)
        nodes.append(
            KnowledgeGraphNode(
                id=ref.id,
                label=ref.title or ref.id,
                facts_state="unread",
                degree=degree[ref.id],
                hop=1,
                layer_only=True,
            )
        )
    present = {edge.type for edge in edges if edge.kind == "typed"}
    layer_lines = tuple(
        line
        for kind, line in LAYER_LEGEND.items()
        if any(e.kind == kind for e in edges)
    )
    return replace(
        base,
        nodes=tuple(nodes),
        edges=edges,
        legend=legend(present) + layer_lines,
        wiki_links=layers.wiki_links,
        backlinks=layers.backlinks,
        sources=layers.sources,
        derived=layers.derived,
        facts_tally=batch.tally,
        facts_capped_at=batch.capped_at,
    )


def assemble_focus_view(
    snapshot: EdgeTableSnapshot,
    focus: str,
    *,
    depth: int = DEFAULT_DEPTH,
    filters: KnowledgeGraphFilters = DEFAULT_FILTERS,
    max_nodes: int = DEFAULT_FOCUS_MAX_NODES,
    neighborhood: RelatedNeighborhood | None = None,
    facts: NoteFactsBatch | None = None,
    layers_unavailable: bool = False,
    focus_missing: bool = False,
) -> KnowledgeGraphView:
    """The pure focus view: typed graph, cap, layers and facts in one call."""
    typed = ego_typed_graph(
        snapshot, focus, depth=depth, filters=filters, max_nodes=max_nodes
    )
    return build_view(
        typed,
        mode="focus",
        filters=filters,
        focus_id=focus,
        depth=min(max(depth, 1), MAX_DEPTH),
        neighborhood=neighborhood,
        facts=facts,
        layers_unavailable=layers_unavailable,
        focus_missing=focus_missing,
        as_of=snapshot.as_of,
        stale=snapshot.stale,
    )


# ── orchestrators: one per mode ────────────────────────────────────────


def _refused(
    reason: RefusalReason,
    *,
    mode: Literal["focus", "global"],
    filters: KnowledgeGraphFilters,
    count: int = 0,
    cap: int = 0,
    **scope: Any,
) -> KnowledgeGraphView:
    return KnowledgeGraphView(
        mode=mode,
        filters=filters,
        refusal=KnowledgeGraphRefusal(reason, count, cap),
        **scope,
    )


async def _read_table(table: EdgeTable) -> EdgeTableSnapshot | EdgeTableRefusal | None:
    try:
        return await table.read()
    except Exception:
        logger.warning("knowledge graph: edge table unavailable", exc_info=True)
        return None


async def assemble_focus_graph(
    table: EdgeTable,
    related: RelatedRead,
    facts: NoteFactsCache,
    focus: str,
    *,
    depth: int = DEFAULT_DEPTH,
    filters: KnowledgeGraphFilters = DEFAULT_FILTERS,
    max_nodes: int = DEFAULT_FOCUS_MAX_NODES,
    fanout_cap: int | None = None,
) -> KnowledgeGraphView:
    """The focus view: the snapshot, then ``related(focus)``, then the facts.

    A refusal — the table unreadable, the table over its bound, or the scope
    over the cap — returns before ``related`` or any facts read.
    """
    depth = min(max(depth, 1), MAX_DEPTH)
    scope: dict[str, Any] = {"focus_id": focus, "depth": depth}
    state = await _read_table(table)
    if state is None:
        return _refused("unavailable", mode="focus", filters=filters, **scope)
    if isinstance(state, EdgeTableRefusal):
        return _refused(
            "table_refused",
            mode="focus",
            filters=filters,
            count=state.row_count,
            cap=state.max_edges,
            **scope,
        )
    typed = ego_typed_graph(
        state, focus, depth=depth, filters=filters, max_nodes=max_nodes
    )
    view_args: dict[str, Any] = {
        "mode": "focus",
        "filters": filters,
        "as_of": state.as_of,
        "stale": state.stale,
        **scope,
    }
    if typed.refusal is not None:
        return build_view(typed, **view_args)
    neighborhood = RelatedNeighborhood()
    layers_unavailable = focus_missing = False
    try:
        neighborhood = await related(focus)
    except Exception as exc:
        if is_doc_not_found(exc):
            focus_missing = True
            facts.mark_missing(focus)
        else:
            layers_unavailable = True
            logger.warning(
                "knowledge graph: related read failed for %s", focus, exc_info=True
            )
    layers = _layers(focus, neighborhood, typed.edges)
    drawn = tuple(_typed_edge(edge) for edge in typed.edges) + layers.edges
    batch = await facts.lookup(read_order(typed, drawn), cap=fanout_cap)
    return build_view(
        typed,
        neighborhood=neighborhood,
        facts=batch,
        layers_unavailable=layers_unavailable,
        focus_missing=focus_missing,
        **view_args,
    )


async def assemble_global_graph(
    table: EdgeTable,
    facts: NoteFactsCache,
    *,
    type: str | None = None,
    namespace: str | None = None,
    filters: KnowledgeGraphFilters = DEFAULT_FILTERS,
    max_nodes: int = DEFAULT_GLOBAL_MAX_NODES,
    fanout_cap: int | None = None,
    clock: Clock = _utcnow,
) -> KnowledgeGraphView:
    """The scoped-global view: rows by ``type`` and/or ``namespace``, then facts.

    Over the table's bound the rows come from a direct filtered read instead
    (stamped now, never stale). No ``lithos_related`` call: global mode has no
    focus, so no wiki-link or provenance layer.
    """
    if type is None and namespace is None:
        raise ValueError("a scoped graph needs type= and/or namespace=")
    scope: dict[str, Any] = {"scope_type": type, "scope_namespace": namespace}
    state = await _read_table(table)
    if state is None:
        return _refused("unavailable", mode="global", filters=filters, **scope)
    as_of: datetime | None
    if isinstance(state, EdgeTableSnapshot):
        rows = scoped_rows(state, type=type, namespace=namespace)
        as_of, stale = state.as_of, state.stale
    else:
        try:
            rows = await table.filtered(type=type, namespace=namespace)
        except Exception:
            logger.warning("knowledge graph: filtered edge read failed", exc_info=True)
            return _refused("unavailable", mode="global", filters=filters, **scope)
        as_of, stale = clock(), False
    typed = global_typed_graph(rows, filters=filters, max_nodes=max_nodes)
    view_args: dict[str, Any] = {
        "mode": "global",
        "filters": filters,
        "as_of": as_of,
        "stale": stale,
        **scope,
    }
    if typed.refusal is not None:
        return build_view(typed, **view_args)
    drawn = tuple(_typed_edge(edge) for edge in typed.edges)
    batch = await facts.lookup(read_order(typed, drawn), cap=fanout_cap)
    return build_view(typed, facts=batch, **view_args)
