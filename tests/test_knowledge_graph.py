"""K2 slice 2 — ego and scoped-global assembly (``knowledge_graph.py``).

The PRD's "Ego assembly (pure)" cases run over a small fixture graph built
here, where every depth and filter boundary is one edge: depth 1 against
depth 2, the filters applied before expansion and the cap, the refusal's count
and remedy, the would-be node count per depth, ghosts retained, the
wiki-link and provenance layers one hop at either depth, degree in view, and
the payload naming exactly what the view model holds. The orchestrators run
against the fake's demo knowledge dataset, where the dangling endpoint is the
ghost and the cap check is proven to spend no Lithos read.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from lithos_lens.fake_knowledge_dataset import DANGLING_NOTE_ID
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.knowledge import RelatedNeighborhood, RelatedRef
from lithos_lens.knowledge_edge_types import EdgeDirection
from lithos_lens.knowledge_edges import EdgeTable, EdgeTableSnapshot, KnowledgeEdge
from lithos_lens.knowledge_facts import (
    NoteFacts,
    NoteFactsAnswer,
    NoteFactsBatch,
    NoteFactsCache,
)
from lithos_lens.knowledge_graph import (
    assemble_focus_graph,
    assemble_focus_view,
    assemble_global_graph,
    ego_typed_graph,
    global_typed_graph,
    read_order,
)
from lithos_lens.knowledge_graph_view import (
    DEFAULT_MIN_WEIGHT,
    KnowledgeGraphFilters,
    KnowledgeGraphView,
    graph_payload,
    provenance_group,
)
from lithos_lens.tasks import NoteRecord

pytestmark = pytest.mark.anyio

_T0 = datetime(2026, 10, 7, 9, 0, 0, tzinfo=UTC)

PLAN = "note-influx-plan"
CAPACITY = "note-influx-capacity"
ROLLBACK = "note-influx-rollback"
LEGACY = "note-influx-legacy-ingest"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def edge(
    edge_id: str,
    from_id: str,
    to_id: str,
    edge_type: str = "supports",
    weight: float | None = 0.8,
    *,
    provenance_type: str | None = "inferred",
    namespace: str = "ns",
    conflict_state: str | None = None,
    partial: bool = False,
) -> KnowledgeEdge:
    return KnowledgeEdge(
        edge_id=edge_id,
        from_id=from_id,
        to_id=to_id,
        type=edge_type,
        weight=weight,
        namespace=namespace,
        provenance_type=provenance_type,
        conflict_state=conflict_state,
        partial=partial,
    )


# The fixture graph around focus F. Depth 1 (default filters): F, A, B, X, P.
# Depth 2 adds D (via A) and E (via B). C is reached only by a 0.03
# consolidation edge, so it and G behind it are hidden; W hangs off A by a
# faint edge, so depth 2 must filter too; H is three hops out.
# X is a dangling endpoint (no note). L1 is a wiki-link target with typed
# edges of its own, which no depth may expand.
FIXTURE_ROWS = (
    edge("e-fa", "F", "A", "supports", 0.8),
    edge("e-fb", "F", "B", "related_to", 0.5, provenance_type="consolidation"),
    edge("e-fc", "C", "F", "related_to", 0.03, provenance_type="consolidation"),
    edge("e-fx", "X", "F", "supports", 0.7),
    edge("e-fp", "F", "P", "assesses", 0.6, provenance_type="manual"),
    edge("e-ad", "A", "D", "refines", 0.7),
    edge("e-aw", "A", "W", "supports", 0.05),
    edge("e-be", "E", "B", "supports", 0.9),
    edge("e-cg", "C", "G", "supports", 0.9),
    edge("e-dh", "D", "H", "supports", 0.9),
    edge("e-l1z", "L1", "Z", "supports", 0.9),
)


def snapshot(rows: tuple[KnowledgeEdge, ...] = FIXTURE_ROWS) -> EdgeTableSnapshot:
    return EdgeTableSnapshot(rows=rows, as_of=_T0)


def facts_for(ids: dict[str, str], missing: tuple[str, ...] = ()) -> NoteFactsBatch:
    answers = {
        node_id: NoteFactsAnswer(node_id, "ok", NoteFacts(title=title))
        for node_id, title in ids.items()
    }
    answers.update(
        {node_id: NoteFactsAnswer(node_id, "missing") for node_id in missing}
    )
    return NoteFactsBatch(answers=answers)


LAYERS = RelatedNeighborhood(
    links=(RelatedRef(id="L1", title="Linked note"),),
    backlinks=(
        RelatedRef(id="L2", title="Linking note"),
        RelatedRef(id="A", title="A"),
    ),
    sources=(RelatedRef(id="S", title="Source note"),),
    derived=(RelatedRef(id="DV", title="Derived note"),),
    # The typed-edge section lithos_related also answers is ignored: the
    # snapshot is the typed-edge source.
    edges=(RelatedRef(id="Q", edge_type="supports", direction="outgoing"),),
)


def ids(view: KnowledgeGraphView) -> set[str]:
    return {node.id for node in view.nodes}


# ── depth, filters, cap ────────────────────────────────────────────────


def test_depth_one_and_two_node_sets() -> None:
    one = ego_typed_graph(snapshot(), "F", depth=1)
    two = ego_typed_graph(snapshot(), "F", depth=2)

    assert set(one.hops) == {"F", "A", "B", "X", "P"}
    assert {e.edge_id for e in one.edges} == {"e-fa", "e-fb", "e-fx", "e-fp"}
    assert set(two.hops) == {"F", "A", "B", "X", "P", "D", "E"}
    assert {e.edge_id for e in two.edges} == {
        "e-fa", "e-fb", "e-fx", "e-fp", "e-ad", "e-be",
    }  # fmt: skip
    assert (two.hops["F"], two.hops["A"], two.hops["D"]) == (0, 1, 2)


def test_the_would_be_node_count_per_depth_is_a_lookup_under_the_filters() -> None:
    assert dict(ego_typed_graph(snapshot(), "F", depth=1).would_be_nodes) == {
        1: 5,
        2: 7,
    }
    assert dict(ego_typed_graph(snapshot(), "F", depth=2).would_be_nodes) == {
        1: 5,
        2: 7,
    }

    unfiltered = KnowledgeGraphFilters(min_weight=0.0)
    counts = ego_typed_graph(snapshot(), "F", filters=unfiltered).would_be_nodes
    assert dict(counts) == {1: 6, 2: 10}  # + C, then + G and W


def test_a_hidden_edge_pulls_in_nothing_at_depth_two() -> None:
    typed = ego_typed_graph(snapshot(), "F", depth=2)
    assert "C" not in typed.hops and "G" not in typed.hops
    assert "W" not in typed.hops  # filtered at the second hop as at the first
    assert typed.hidden.by_weight == 2  # e-fc, e-aw
    assert typed.hidden.by_provenance == 0
    assert typed.hidden.total == 3  # those two and e-cg behind e-fc


def test_weight_filter_never_hides_an_unknown_weight() -> None:
    rows = (
        edge("e1", "F", "N", weight=None, partial=True),
        edge("e2", "F", "M", weight=0.05),
    )
    typed = ego_typed_graph(snapshot(rows), "F")
    assert set(typed.hops) == {"F", "N"}
    assert typed.hidden.by_weight == 1


def test_provenance_groups_and_their_filter() -> None:
    assert [
        provenance_group(value)
        for value in ("inferred", "consolidation", "reinforcement", "frontmatter",
                      "authored", "manual", "conversation-derived", "odd", None)
    ] == ["inferred", "reinforced", "reinforced", "declared",
          "other", "other", "other", "other", "other"]  # fmt: skip

    no_other = KnowledgeGraphFilters(provenance=frozenset({"inferred", "reinforced"}))
    typed = ego_typed_graph(snapshot(), "F", filters=no_other)
    assert "P" not in typed.hops
    assert typed.hidden.by_provenance == 1
    assert typed.hidden.by_weight == 1
    assert typed.hidden.total == 2
    facets = {facet.group: facet for facet in typed.provenance_facets}
    assert (facets["other"].count, facets["other"].values, facets["other"].shown) == (
        1,
        ("manual",),
        False,
    )
    assert facets["reinforced"].values == ("consolidation",)
    assert facets["reinforced"].count == 2


def test_filters_apply_before_the_cap() -> None:
    # Six nodes unfiltered, five once the 0.03 edge is hidden: the default
    # filter is a way under a cap of five.
    refused = ego_typed_graph(
        snapshot(), "F", filters=KnowledgeGraphFilters(min_weight=0.0), max_nodes=5
    )
    assert refused.refusal is not None and refused.refusal.count == 6

    drawn = ego_typed_graph(snapshot(), "F", max_nodes=5)
    assert drawn.refusal is None
    assert len(drawn.hops) == 5


def test_refusal_names_the_count_and_depth_one_as_the_remedy() -> None:
    typed = ego_typed_graph(snapshot(), "F", depth=2, max_nodes=5)
    refusal = typed.refusal

    assert refusal is not None
    assert (refusal.reason, refusal.count, refusal.cap) == ("too_many_nodes", 7, 5)
    assert (refusal.remedy_depth, refusal.remedy_count) == (1, 5)
    assert refusal.remedy_min_weight is None
    assert refusal.message == "7 notes in this view, over the 5 cap. depth=1 shows 5."
    assert typed.edges and dict(typed.would_be_nodes) == {1: 5, 2: 7}


def test_refusal_names_the_lowest_min_weight_in_tenths_that_fits() -> None:
    # Depth 1 is five nodes; at 0.6 the 0.5 related_to edge goes, leaving four.
    refusal = ego_typed_graph(snapshot(), "F", depth=1, max_nodes=4).refusal

    assert refusal is not None
    assert refusal.remedy_depth is None
    assert (refusal.remedy_min_weight, refusal.remedy_count) == (0.6, 4)
    assert "min_weight=0.6 shows 4" in refusal.message


def test_refusal_says_when_no_depth_or_weight_brings_it_under() -> None:
    rows = (
        edge("e1", "F", "N1", weight=None),
        edge("e2", "F", "N2", weight=None),
        edge("e3", "F", "N3", weight=1.0),
    )
    refusal = ego_typed_graph(snapshot(rows), "F", max_nodes=2).refusal

    assert refusal is not None
    assert not refusal.has_remedy
    assert "No depth or weight filter brings it under" in refusal.message


def test_read_order_is_focus_then_hop_then_degree_then_id() -> None:
    typed = ego_typed_graph(snapshot(), "F", depth=2)
    view = assemble_focus_view(snapshot(), "F", depth=2)
    # A and B carry two drawn edges each, X and P one; D and E are hop 2.
    assert read_order(typed, view.edges) == ["F", "A", "B", "P", "X", "D", "E"]


# ── the view: ghosts, layers, degree ───────────────────────────────────


def test_ghosts_are_retained_with_their_short_id_and_listed() -> None:
    view = assemble_focus_view(
        snapshot(),
        "F",
        facts=facts_for({"F": "Focus", "A": "A", "B": "B", "P": "P"}, missing=("X",)),
    )

    ghost = view.node("X")
    assert ghost is not None and ghost.is_ghost
    assert ghost.label == "X"
    assert [e.id for e in view.edges_to_missing] == ["e-fx"]
    assert [node.id for node in view.ghosts] == ["X"]


def test_an_unread_node_is_labelled_by_its_full_id_not_drawn_as_a_ghost() -> None:
    view = assemble_focus_view(
        snapshot(), "F", facts=NoteFactsBatch(capped_at=2), max_nodes=10
    )
    node = view.node("A")
    assert node is not None
    assert (node.facts_state, node.label, node.is_ghost) == ("unread", "A", False)
    assert view.facts_capped_at == 2


@pytest.mark.parametrize("depth", [1, 2])
def test_wiki_link_and_provenance_layers_are_one_hop_at_any_depth(depth: int) -> None:
    view = assemble_focus_view(snapshot(), "F", depth=depth, neighborhood=LAYERS)

    layer_edges = {
        (e.kind, e.from_id, e.to_id) for e in view.edges if e.kind != "typed"
    }
    assert layer_edges == {
        ("wiki_link", "F", "L1"),
        ("wiki_link", "L2", "F"),
        ("wiki_link", "A", "F"),
        ("provenance", "F", "S"),
        ("provenance", "DV", "F"),
    }
    # L1's own typed edge to Z is never expanded, at either depth.
    assert "Z" not in ids(view)
    assert "Q" not in ids(view)  # related()'s typed section is ignored
    for node_id in ("L1", "L2", "S", "DV"):
        node = view.node(node_id)
        assert node is not None
        assert (node.layer_only, node.hop, node.facts_state) == (True, 1, "unread")
    linked = view.node("L1")
    assert linked is not None and linked.label == "Linked note"
    # A is a typed endpoint too: one node, not a layer-only duplicate.
    assert [n.id for n in view.nodes].count("A") == 1
    assert not view.node("A").layer_only  # type: ignore[union-attr]
    assert [line.type for line in view.legend][-2:] == ["wiki_link", "provenance"]
    assert all("one hop" in line.line for line in view.legend[-2:])


def test_a_provenance_pair_drawn_as_derived_from_is_listed_not_drawn_twice() -> None:
    rows = (edge("e-d", "F", "S", "derived_from", 1.0, provenance_type="frontmatter"),)
    neighborhood = RelatedNeighborhood(sources=(RelatedRef(id="S", title="Source"),))
    view = assemble_focus_view(snapshot(rows), "F", neighborhood=neighborhood)

    assert [(e.kind, e.type) for e in view.edges] == [("typed", "derived_from")]
    assert [(ref.id, ref.drawn_as_typed) for ref in view.sources] == [("S", True)]
    assert [line.type for line in view.legend] == ["derived_from"]


def test_degree_in_view_counts_every_drawn_edge_at_a_node() -> None:
    view = assemble_focus_view(snapshot(), "F", depth=2, neighborhood=LAYERS)

    for node in view.nodes:
        incident = [e for e in view.edges if node.id in (e.from_id, e.to_id)]
        assert node.degree == len(incident), node.id
    focus = view.node("F")
    assert focus is not None and focus.is_focus
    assert focus.degree == 4 + 5  # four typed edges, five layer pairs
    a = view.node("A")
    assert a is not None and a.degree == 3  # supports, refines, wiki-link


def test_edges_carry_type_weight_provenance_conflict_state_and_direction() -> None:
    rows = (
        edge("e1", "F", "A", "related_to", 0.5, provenance_type="consolidation"),
        edge("e2", "B", "F", "contradicts", 0.7, conflict_state="superseded"),
        edge("e3", "F", "C", "assesses", 0.6, provenance_type=None),
    )
    view = assemble_focus_view(snapshot(rows), "F")
    by_id = {e.id: e for e in view.edges}

    assert by_id["e1"].direction is EdgeDirection.SYMMETRIC
    assert (by_id["e2"].conflict_state, by_id["e2"].type) == (
        "superseded",
        "contradicts",
    )
    assert by_id["e3"].direction is EdgeDirection.AS_RECORDED
    assert (by_id["e1"].weight, by_id["e1"].provenance) == (0.5, "consolidation")
    assert [line.type for line in view.legend] == [
        "related_to",
        "contradicts",
        "assesses",
    ]


def test_the_payload_names_every_node_and_edge_the_view_model_holds() -> None:
    view = assemble_focus_view(
        snapshot(),
        "F",
        depth=2,
        neighborhood=LAYERS,
        facts=facts_for({"F": "Focus", "A": "Alpha"}, missing=("X",)),
    )
    payload = json.loads(json.dumps(graph_payload(view)))

    assert [n["id"] for n in payload["nodes"]] == [n.id for n in view.nodes]
    assert [e["id"] for e in payload["edges"]] == [e.id for e in view.edges]
    node_ids = {n["id"] for n in payload["nodes"]}
    assert all({e["from"], e["to"]} <= node_ids for e in payload["edges"])
    fa = next(e for e in payload["edges"] if e["id"] == "e-fa")
    assert fa == {
        "id": "e-fa",
        "from": "F",
        "to": "A",
        "kind": "typed",
        "type": "supports",
        "weight": 0.8,
        "provenance": "inferred",
        "provenance_group": "inferred",
        "conflict_state": None,
        "direction": "directed",
        "partial": False,
    }
    x = next(n for n in payload["nodes"] if n["id"] == "X")
    assert (x["ghost"], x["facts_state"], x["label"]) == (True, "missing", "X")
    alpha = next(n for n in payload["nodes"] if n["id"] == "A")
    assert (alpha["title"], alpha["degree"]) == ("Alpha", 3)
    assert [line["type"] for line in payload["legend"]] == [
        line.type for line in view.legend
    ]
    assert payload["hidden"] == {"by_weight": 2, "by_provenance": 0, "total": 3}
    assert payload["would_be_nodes"] == {"1": 5, "2": 7}
    assert payload["as_of"] == _T0.isoformat()
    assert payload["refusal"] is None
    assert payload["filters"] == {
        "min_weight": DEFAULT_MIN_WEIGHT,
        "provenance": ["inferred", "reinforced", "declared", "other"],
    }


def test_the_payload_serialises_every_fact_direction_and_conflict_state() -> None:
    rows = (
        edge("e1", "F", "A", "related_to", 0.5, provenance_type="consolidation"),
        edge("e2", "B", "F", "contradicts", 0.7, conflict_state="superseded"),
        edge("e3", "F", "C", "assesses", None, provenance_type=None, partial=True),
    )
    full = NoteFacts(
        title="Focus note",
        note_type="observation",
        status="quarantined",
        namespace="reports",
        confidence="85%",
        lede="What the focus says, in a line.",
    )
    facts = NoteFactsBatch(
        answers={
            "F": NoteFactsAnswer("F", "ok", full),
            "A": NoteFactsAnswer("A", "pending", NoteFacts(title="Alpha")),
            "B": NoteFactsAnswer("B", "missing"),
        }
    )
    view = assemble_focus_view(snapshot(rows), "F", facts=facts)
    payload = json.loads(json.dumps(graph_payload(view)))
    nodes = {n["id"]: n for n in payload["nodes"]}
    edges = {e["id"]: e for e in payload["edges"]}

    assert nodes["F"] == {
        "id": "F",
        "label": "Focus note",
        "facts_state": "ok",
        "ghost": False,
        "focus": True,
        "layer_only": False,
        "hop": 0,
        "degree": 3,
        "title": "Focus note",
        "note_type": "observation",
        "status": "quarantined",
        "namespace": "reports",
        "confidence": "85%",
        "lede": "What the focus says, in a line.",
    }
    assert (nodes["A"]["facts_state"], nodes["A"]["title"]) == ("pending", "Alpha")
    assert (nodes["B"]["ghost"], nodes["B"]["label"]) == (True, "B")
    assert (nodes["C"]["facts_state"], nodes["C"]["label"]) == ("unread", "C")
    assert edges["e1"] == {
        "id": "e1",
        "from": "F",
        "to": "A",
        "kind": "typed",
        "type": "related_to",
        "weight": 0.5,
        "provenance": "consolidation",
        "provenance_group": "reinforced",
        "conflict_state": None,
        "direction": "symmetric",
        "partial": False,
    }
    assert edges["e2"] == {
        "id": "e2",
        "from": "B",
        "to": "F",
        "kind": "typed",
        "type": "contradicts",
        "weight": 0.7,
        "provenance": "inferred",
        "provenance_group": "inferred",
        "conflict_state": "superseded",
        "direction": "symmetric",
        "partial": False,
    }
    assert edges["e3"] == {
        "id": "e3",
        "from": "F",
        "to": "C",
        "kind": "typed",
        "type": "assesses",
        "weight": None,
        "provenance": None,
        "provenance_group": "other",
        "conflict_state": None,
        "direction": "as_recorded",
        "partial": True,
    }
    assert payload["legend"] == [
        {
            "type": line.type,
            "class": line.css_class,
            "line": line.line,
            "known": line.known,
        }
        for line in view.legend
    ]
    assert [line["type"] for line in payload["legend"]] == [
        "related_to",
        "contradicts",
        "assesses",
    ]
    assert payload["legend"][-1]["known"] is False


def test_a_refused_view_has_no_nodes_and_its_payload_carries_the_refusal() -> None:
    view = assemble_focus_view(snapshot(), "F", depth=2, max_nodes=5)
    payload = graph_payload(view)

    assert view.nodes == () and view.edges == ()
    assert payload["refusal"]["count"] == 7
    assert payload["refusal"]["remedy_depth"] == 1
    assert payload["would_be_nodes"] == {"1": 5, "2": 7}


def test_global_typed_graph_takes_endpoints_and_refuses_with_a_weight_remedy() -> None:
    rows = tuple(
        edge(f"e{i}", f"N{i}", f"M{i}", weight=0.25 if i % 2 else 0.85)
        for i in range(4)
    )
    typed = global_typed_graph(rows, max_nodes=4)
    assert typed.refusal is not None and typed.refusal.count == 8
    assert (typed.refusal.remedy_min_weight, typed.refusal.remedy_count) == (0.3, 4)
    assert typed.refusal.remedy_depth is None

    drawn = global_typed_graph(rows, filters=KnowledgeGraphFilters(min_weight=0.3))
    assert set(drawn.hops) == {"N0", "M0", "N2", "M2"}
    assert drawn.hidden.by_weight == 2


# ── orchestrators over the fake ────────────────────────────────────────


class Wiring:
    """The S3 wiring in miniature: an edge table, a related read and a facts
    cache over one fake, with every Lithos read counted."""

    def __init__(self, fake: FakeLithosClient, **table: Any) -> None:
        self.fake = fake
        self.reads: list[str] = []
        self.related_calls: list[str] = []
        self.related_error: Exception | None = None
        self.table = EdgeTable(self._edge_list, **table)
        self.facts = NoteFactsCache(self._read, lambda: asyncio.Semaphore(8))

    async def _edge_list(
        self, edge_type: str | None, namespace: str | None
    ) -> tuple[KnowledgeEdge, ...]:
        return await self.fake.edge_list(type=edge_type, namespace=namespace)

    async def _read(self, note_id: str) -> NoteRecord | None:
        self.reads.append(note_id)
        return await self.fake.read_note(note_id, max_length=1)

    async def related(self, note_id: str) -> RelatedNeighborhood:
        self.related_calls.append(note_id)
        if self.related_error is not None:
            raise self.related_error
        return await self.fake.related(note_id)

    async def focus(self, focus: str, **kwargs: Any) -> KnowledgeGraphView:
        return await assemble_focus_graph(
            self.table, self.related, self.facts, focus, **kwargs
        )


@pytest.fixture
def wiring() -> Wiring:
    return Wiring(FakeLithosClient())


async def test_focus_graph_over_the_demo_dataset(wiring: Wiring) -> None:
    view = await wiring.focus(CAPACITY)

    assert view.refusal is None and view.as_of is not None and not view.stale
    assert ids(view) == {CAPACITY, PLAN, ROLLBACK, LEGACY, DANGLING_NOTE_ID}
    ghost = view.node(DANGLING_NOTE_ID)
    assert ghost is not None and ghost.is_ghost and ghost.label == DANGLING_NOTE_ID[:8]
    assert [e.id for e in view.edges_to_missing] == ["edge_0a5e6c9b7d24"]
    plan = view.node(PLAN)
    assert plan is not None and plan.label == "Influx migration plan"
    assert plan.facts is not None and plan.facts.status == "active"
    assert wiring.reads[0] == CAPACITY  # the focus is read first
    assert wiring.related_calls == [CAPACITY]
    assert view.facts_tally.reads == 5 and view.facts_tally.missing == 1
    # Capacity's fixture neighbourhood wiki-links the plan and the rollback
    # route (both typed neighbours too) and declares the plan as a source.
    assert {(e.kind, e.to_id) for e in view.edges if e.kind != "typed"} == {
        ("wiki_link", PLAN),
        ("wiki_link", ROLLBACK),
        ("provenance", PLAN),
    }


async def test_the_cap_is_checked_before_any_related_call_or_facts_read(
    wiring: Wiring,
) -> None:
    view = await wiring.focus(CAPACITY, max_nodes=2)

    assert view.refusal is not None and view.refusal.reason == "too_many_nodes"
    assert wiring.related_calls == [] and wiring.reads == []


async def test_a_focus_lithos_cannot_find_is_a_ghost_with_its_edges_drawn(
    wiring: Wiring,
) -> None:
    view = await wiring.focus(DANGLING_NOTE_ID)

    focus = view.node(DANGLING_NOTE_ID)
    assert focus is not None and focus.is_focus and focus.is_ghost
    assert [e.id for e in view.edges] == ["edge_0a5e6c9b7d24"]
    assert not view.layers_unavailable
    assert wiring.reads == [CAPACITY]  # the focus itself spent no read


async def test_a_failed_related_read_drops_the_layers_and_keeps_the_typed_graph(
    wiring: Wiring,
) -> None:
    wiring.related_error = RuntimeError("lithos_related timed out")
    view = await wiring.focus(CAPACITY)

    assert view.layers_unavailable
    assert all(e.kind == "typed" for e in view.edges)
    assert len(view.edges) == 6  # every capacity row in the demo table
    assert view.wiki_links == () and view.sources == ()


async def test_quarantine_behind_an_unchanged_title_renders_on_the_next_draw(
    wiring: Wiring,
) -> None:
    await wiring.focus(CAPACITY)
    note = wiring.fake.dataset.notes[PLAN]
    metadata = {
        **note.metadata,
        "status": "quarantined",
        "summaries": {"short": "Quarantined after misleading feedback."},
    }
    wiring.fake.dataset = replace(
        wiring.fake.dataset,
        notes={**wiring.fake.dataset.notes, PLAN: replace(note, metadata=metadata)},
    )
    wiring.facts.apply_note_event("note.updated", {"id": PLAN, "title": note.title})
    wiring.reads.clear()

    view = await wiring.focus(CAPACITY)

    assert wiring.reads == [PLAN]
    plan = view.node(PLAN)
    assert plan is not None and plan.facts is not None
    assert (plan.facts.status, plan.facts.lede) == (
        "quarantined",
        "Quarantined after misleading feedback.",
    )
    payload_plan = next(n for n in graph_payload(view)["nodes"] if n["id"] == PLAN)
    assert (
        payload_plan["status"],
        payload_plan["lede"],
        payload_plan["facts_state"],
    ) == (
        "quarantined",
        "Quarantined after misleading feedback.",
        "ok",
    )


async def test_a_table_over_its_bound_refuses_focus_and_serves_filtered_global() -> (
    None
):
    wiring = Wiring(FakeLithosClient(), max_edges=3)

    focus = await wiring.focus(CAPACITY)
    assert focus.refusal is not None
    assert (focus.refusal.reason, focus.refusal.cap) == ("table_refused", 3)
    assert focus.refusal.count == 14
    assert wiring.related_calls == [] and wiring.reads == []

    scoped = await assemble_global_graph(
        wiring.table, wiring.facts, type="contradicts", clock=lambda: _T0
    )
    assert scoped.refusal is None
    assert {e.id for e in scoped.edges} == {
        "edge_e1f4a8c27b90", "edge_38c9d1f5e6a7", "edge_b6e0f27d4c18",
    }  # fmt: skip
    assert scoped.as_of == _T0
    assert ("lithos_edge_list", {"type": "contradicts"}) in wiring.fake.tool_calls


async def test_an_unreadable_table_is_refused_as_unavailable() -> None:
    async def failing(
        edge_type: str | None, namespace: str | None
    ) -> list[KnowledgeEdge]:
        raise RuntimeError("lithos down")

    facts = NoteFactsCache(_never_read, lambda: asyncio.Semaphore(1))
    table = EdgeTable(failing)
    focus = await assemble_focus_graph(table, _never_related, facts, PLAN)
    scoped = await assemble_global_graph(table, facts, namespace="influx")

    for view in (focus, scoped):
        assert view.refusal is not None and view.refusal.reason == "unavailable"


async def _never_read(note_id: str) -> NoteRecord | None:
    raise AssertionError("no facts read on a refusal")


async def _never_related(note_id: str) -> RelatedNeighborhood:
    raise AssertionError("no related read on a refusal")


async def test_global_graph_reads_facts_by_degree_and_has_no_layers(
    wiring: Wiring,
) -> None:
    view = await assemble_global_graph(wiring.table, wiring.facts, namespace="influx")

    assert view.mode == "global" and view.refusal is None
    assert wiring.related_calls == []
    assert all(e.kind == "typed" for e in view.edges)
    degrees = {node.id: node.degree for node in view.nodes}
    expected = sorted(degrees, key=lambda node_id: (-degrees[node_id], node_id))
    assert wiring.reads == expected
    assert view.hidden.by_weight == 2  # the two consolidation-weight related_to rows


async def test_a_stale_snapshot_is_served_and_says_so() -> None:
    fake = FakeLithosClient()
    clock = [0.0]
    fail = [False]

    async def fetch(edge_type: str | None, namespace: str | None) -> Any:
        if fail[0]:
            raise RuntimeError("refetch failed")
        return await fake.edge_list(type=edge_type, namespace=namespace)

    table = EdgeTable(fetch, ttl_s=10, ticks=lambda: clock[0])
    facts = NoteFactsCache(fake_read(fake), lambda: asyncio.Semaphore(8))
    await table.read()
    clock[0] = 20.0
    fail[0] = True

    view = await assemble_focus_graph(table, fake.related, facts, CAPACITY)

    assert view.stale and view.refusal is None and view.nodes
    assert graph_payload(view)["stale"] is True


def fake_read(fake: FakeLithosClient) -> Any:
    async def read(note_id: str) -> NoteRecord | None:
        return await fake.read_note(note_id, max_length=1)

    return read


# ── orchestrators over hand-built rows ─────────────────────────────────


class RowWiring:
    """An edge table over fixed rows, a facts read answering any id with a
    note titled after it (``None`` for the ids in ``missing``), and a
    ``related`` read answering one neighbourhood — every call counted."""

    def __init__(
        self,
        rows: tuple[KnowledgeEdge, ...],
        neighborhood: RelatedNeighborhood | None = None,
        missing: tuple[str, ...] = (),
    ) -> None:
        self.rows = rows
        self.neighborhood = neighborhood or RelatedNeighborhood()
        self.missing = set(missing)
        self.reads: list[str] = []
        self.related_calls: list[str] = []
        self.table = EdgeTable(self._edge_list)
        self.facts = NoteFactsCache(self._read, lambda: asyncio.Semaphore(8))

    async def _edge_list(
        self, edge_type: str | None, namespace: str | None
    ) -> tuple[KnowledgeEdge, ...]:
        return self.rows

    async def _read(self, note_id: str) -> NoteRecord | None:
        self.reads.append(note_id)
        if note_id in self.missing:
            return None
        return NoteRecord(id=note_id, title=f"Title {note_id}", content="")

    async def related(self, note_id: str) -> RelatedNeighborhood:
        self.related_calls.append(note_id)
        return self.neighborhood

    async def scoped(self, **kwargs: Any) -> KnowledgeGraphView:
        return await assemble_global_graph(self.table, self.facts, **kwargs)


# Two types across two namespaces, every row with its own endpoints, so each
# selector picks out exactly the nodes its rows name.
MIXED_ROWS = (
    edge("m1", "S1", "T1", "supports", namespace="influx"),
    edge("m2", "S2", "T2", "supports", namespace="influx"),
    edge("m3", "S3", "T3", "supports", namespace="research"),
    edge("m4", "R1", "U1", "refines", namespace="influx"),
    edge("m5", "R2", "U2", "refines", namespace="research"),
    edge("m6", "R3", "U3", "contradicts", namespace="research"),
)


@pytest.mark.parametrize(
    ("scope", "edge_ids"),
    [
        ({"type": "supports"}, {"m1", "m2", "m3"}),
        ({"namespace": "influx"}, {"m1", "m2", "m4"}),
        ({"type": "supports", "namespace": "research"}, {"m3"}),
        ({"type": "refines", "namespace": "influx"}, {"m4"}),
        ({"type": "contradicts", "namespace": "influx"}, set()),
    ],
)
async def test_a_scope_draws_and_reads_only_its_own_rows(
    scope: dict[str, str], edge_ids: set[str]
) -> None:
    wiring = RowWiring(MIXED_ROWS)
    expected_nodes = {
        end for row in MIXED_ROWS if row.edge_id in edge_ids for end in row.endpoints
    }

    # The cap is exactly the scope's own node count: the other rows' nodes
    # must not count against it.
    view = await wiring.scoped(max_nodes=max(len(expected_nodes), 1), **scope)

    assert view.refusal is None
    assert {e.id for e in view.edges} == edge_ids
    assert ids(view) == expected_nodes
    assert set(wiring.reads) == expected_nodes  # excluded rows spend no read
    assert (view.scope_type, view.scope_namespace) == (
        scope.get("type"),
        scope.get("namespace"),
    )


async def test_a_scope_over_its_cap_is_refused_on_its_own_rows_only() -> None:
    wiring = RowWiring(MIXED_ROWS)

    view = await wiring.scoped(type="supports", max_nodes=5)

    assert view.refusal is not None
    assert (view.refusal.count, view.refusal.cap) == (6, 5)
    assert wiring.reads == []


@pytest.mark.parametrize("depth", [1, 2])
async def test_layer_only_neighbours_spend_no_read_and_do_not_count_to_the_cap(
    depth: int,
) -> None:
    wiring = RowWiring(FIXTURE_ROWS, neighborhood=LAYERS, missing=("X",))
    typed_nodes = set(ego_typed_graph(snapshot(), "F", depth=depth).hops)

    # The cap is exactly the typed node count; four layer-only neighbours
    # (L1, L2, S, DV) take the drawn view over it, and must not refuse it.
    view = await assemble_focus_graph(
        wiring.table,
        wiring.related,
        wiring.facts,
        "F",
        depth=depth,
        max_nodes=len(typed_nodes),
    )

    assert view.refusal is None
    assert len(view.nodes) == len(typed_nodes) + 4
    assert wiring.related_calls == ["F"]
    assert set(wiring.reads) == typed_nodes
    assert len(wiring.reads) == len(typed_nodes)
    layer_only = {n.id: n for n in view.nodes if n.layer_only}
    assert {node_id: n.label for node_id, n in layer_only.items()} == {
        "L1": "Linked note",
        "L2": "Linking note",
        "S": "Source note",
        "DV": "Derived note",
    }


async def test_layer_titles_survive_an_exhausted_facts_cap() -> None:
    wiring = RowWiring(FIXTURE_ROWS, neighborhood=LAYERS)

    view = await assemble_focus_graph(
        wiring.table, wiring.related, wiring.facts, "F", fanout_cap=1
    )

    assert wiring.reads == ["F"]
    assert view.facts_capped_at == 1
    labels = {n.id: (n.label, n.facts_state) for n in view.nodes}
    assert labels["F"] == ("Title F", "ok")
    assert labels["A"] == ("A", "unread")  # a typed node past the cap
    assert labels["L1"] == ("Linked note", "unread")
    assert labels["S"] == ("Source note", "unread")
