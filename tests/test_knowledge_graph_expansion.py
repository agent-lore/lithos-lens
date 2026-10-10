"""K2 slice 8 — expansion assembly (D16), pure, over a fixture graph.

The PRD's "Expansion assembly (pure)" cases: an expanded note's filtered
edges and far endpoints are added and nothing else; URL order; duplicates and
the focus dropped; unreached and refused requests (a later one that fits still
applies); ``undrawn_nodes`` / ``undrawn_edges``, ``via`` and ``hop``; the
collapse set with transitive dependants and overlapping branches; edge-only
additions; layer-only roots and their promotion to typed endpoints, the visual
additions asserted apart from the cap count at the cap boundary; the ``edge=``
exemption; and an over-cap base that still spends no related or facts read.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from lithos_lens.knowledge import RelatedNeighborhood, RelatedRef
from lithos_lens.knowledge_edges import EdgeTable, EdgeTableSnapshot, KnowledgeEdge
from lithos_lens.knowledge_facts import NoteFactsCache
from lithos_lens.knowledge_graph import (
    assemble_focus_graph,
    assemble_focus_view,
    build_view,
)
from lithos_lens.knowledge_graph_expansion import dependants, walk_expansions
from lithos_lens.knowledge_graph_typed import global_typed_graph
from lithos_lens.knowledge_graph_view import (
    KnowledgeGraphFilters,
    KnowledgeGraphView,
    graph_payload,
)
from lithos_lens.tasks import NoteRecord

pytestmark = pytest.mark.anyio

_T0 = datetime(2026, 10, 10, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def edge(edge_id: str, from_id: str, to_id: str, weight: float = 0.8) -> KnowledgeEdge:
    return KnowledgeEdge(
        edge_id=edge_id,
        from_id=from_id,
        to_id=to_id,
        type="supports",
        weight=weight,
        namespace="ns",
        provenance_type="inferred",
    )


# Around focus F at depth 1 the base draws F, A and B, plus two layer-only
# notes: L (a wiki-link) and L2 (a back-link). Neither counts towards the cap.
# A reaches C and D, and L by a typed edge (expanding A promotes L); W hangs
# off A by a faint edge the default filters hide. C reaches E. B reaches D
# (shared with A), G, and L2. L reaches M; L2 has only its edge to B.
ROWS = (
    edge("e-fa", "F", "A"),
    edge("e-fb", "F", "B"),
    edge("e-ac", "A", "C"),
    edge("e-ad", "A", "D"),
    edge("e-aw", "A", "W", 0.05),
    edge("e-la", "L", "A"),
    edge("e-ce", "C", "E"),
    edge("e-bd", "B", "D"),
    edge("e-bg", "B", "G"),
    edge("e-l2b", "L2", "B"),
    edge("e-lm", "L", "M"),
)

LAYERS = RelatedNeighborhood(
    links=(RelatedRef(id="L", title="Linked note"),),
    backlinks=(RelatedRef(id="L2", title="Linking note"),),
)


def view_of(*expand: str, **kwargs: Any) -> KnowledgeGraphView:
    snapshot = EdgeTableSnapshot(rows=ROWS, as_of=_T0)
    kwargs.setdefault("neighborhood", LAYERS)
    return assemble_focus_view(snapshot, "F", expand=expand, **kwargs)


def nodes(view: KnowledgeGraphView) -> dict[str, Any]:
    return {node.id: node for node in view.nodes}


def typed_ids(view: KnowledgeGraphView) -> set[str]:
    return {edge.id for edge in view.edges if edge.kind == "typed"}


def cap_count(view: KnowledgeGraphView) -> int:
    """The cap's own count: the focus and the typed endpoints drawn."""
    return sum(1 for node in view.nodes if not node.layer_only)


def steps(view: KnowledgeGraphView) -> list[tuple[str, str, int, int]]:
    return [
        (step.id, step.state, step.added_nodes, step.added_edges)
        for step in view.expansions
    ]


# ── what an expansion adds ─────────────────────────────────────────────


def test_an_expanded_notes_filtered_edges_and_far_endpoints_are_added_and_no_more() -> (
    None
):
    base, view = view_of(), view_of("A")

    assert typed_ids(view) - typed_ids(base) == {"e-ac", "e-ad", "e-la"}
    assert set(nodes(view)) - set(nodes(base)) == {"C", "D"}
    # W's edge is under the weight filter; C's and B's edges are between drawn
    # notes that are neither in depth nor expanded, so they stay undrawn.
    assert not {"e-aw", "e-ce", "e-bd"} & typed_ids(view)
    assert steps(view) == [("A", "applied", 2, 3)]
    by_id = nodes(view)
    assert (by_id["A"].expanded, by_id["A"].via, by_id["A"].hop) == (True, None, 1)
    assert [(by_id[n].via, by_id[n].hop) for n in ("C", "D")] == [("A", 2)] * 2
    # The faint edge expanding A met is now counted hidden.
    assert (base.hidden.by_weight, view.hidden.by_weight) == (0, 1)
    assert view.hidden.total == 1


def test_requests_apply_in_url_order_and_a_note_not_yet_drawn_is_unreached() -> None:
    in_order = view_of("A", "C")
    reversed_ = view_of("C", "A")

    assert steps(in_order) == [("A", "applied", 2, 3), ("C", "applied", 1, 1)]
    assert (nodes(in_order)["E"].via, nodes(in_order)["E"].hop) == ("C", 3)
    # C is drawn only once A's step has run: at its own turn it is not there.
    assert steps(reversed_) == [("C", "unreached", 0, 0), ("A", "applied", 2, 3)]
    assert "E" not in nodes(reversed_)
    assert nodes(reversed_)["C"].expanded is False


def test_duplicates_and_the_focus_are_dropped() -> None:
    view = view_of("A", "F", "A")

    assert steps(view) == [("A", "applied", 2, 3)]
    assert graph_payload(view)["expansions"] == [
        {"id": "A", "state": "applied", "added_nodes": 2, "added_edges": 3}
    ]


def test_a_request_for_a_note_not_in_the_view_adds_nothing() -> None:
    base, view = view_of(), view_of("NOWHERE")

    assert steps(view) == [("NOWHERE", "unreached", 0, 0)]
    assert (set(nodes(view)), typed_ids(view)) == (set(nodes(base)), typed_ids(base))


# ── the cap: refused on its own, two counts ────────────────────────────


def test_a_refused_request_adds_nothing_and_a_later_one_that_fits_still_applies() -> (
    None
):
    # After A the cap count is 6 (F A B C D, and L promoted). B would add G and
    # promote L2: 8, over 7. C adds E: 7, which fits.
    view = view_of("A", "B", "C", max_nodes=7)
    without_b = view_of("A", "C", max_nodes=7)

    assert view.refusal is None
    assert steps(view) == [
        ("A", "applied", 2, 3),
        ("B", "refused", 0, 0),
        ("C", "applied", 1, 1),
    ]
    refused = view.expansions[1]
    assert (refused.would_count, refused.would_add_nodes, refused.cap) == (8, 1, 7)
    assert graph_payload(view)["expansions"][1] == {
        "id": "B",
        "state": "refused",
        "added_nodes": 0,
        "added_edges": 0,
        "would_count": 8,
    }
    # The refused step changed neither the visible set nor the cap count.
    assert set(nodes(view)) == set(nodes(without_b))
    assert typed_ids(view) == typed_ids(without_b)
    assert cap_count(view) == cap_count(without_b) == 7
    assert nodes(view)["B"].expanded is False


def test_an_over_cap_base_is_refused_whatever_the_expansions() -> None:
    view = view_of("A", max_nodes=2)

    assert view.refusal is not None
    assert (view.refusal.count, view.refusal.cap) == (3, 2)
    assert view.expansions == () and view.nodes == ()


def test_a_layer_only_root_expands_its_typed_edges_and_is_promoted() -> None:
    base, view = view_of(), view_of("L")
    by_id = nodes(view)

    # L keeps its base place: hop 1, no via, but is now a typed endpoint.
    assert (by_id["L"].layer_only, by_id["L"].hop, by_id["L"].via) == (False, 1, None)
    assert by_id["L"].expanded
    assert (by_id["M"].via, by_id["M"].hop) == ("L", 2)
    assert typed_ids(view) - typed_ids(base) == {"e-lm", "e-la"}
    # One visible note added (M); the cap count grows by two (L and M).
    assert steps(view) == [("L", "applied", 1, 2)]
    assert len(view.nodes) - len(base.nodes) == 1
    assert cap_count(view) - cap_count(base) == 2
    assert graph_payload(view)["nodes"][-1]["via"] is None  # L2: still layer-only


@pytest.mark.parametrize(
    ("cap", "state", "counted"),
    [(4, "applied", 4), (3, "refused", 3)],
)
def test_an_edge_only_promotion_at_the_cap_boundary(
    cap: int, state: str, counted: int
) -> None:
    """Expanding L2 adds no visible note — its one edge runs to B — but
    promotes L2 itself: the cap count is 4 against a base of 3."""
    base = view_of(max_nodes=cap)
    view = view_of("L2", max_nodes=cap)

    eligibility = nodes(base)["L2"].expansion
    assert (eligibility.undrawn_nodes, eligibility.undrawn_edges) == (0, 1)
    assert eligibility.would_count == 4
    assert eligibility.state == ("available" if state == "applied" else "over_cap")
    assert view.expansions[0].state == state
    assert set(nodes(view)) == set(nodes(base))  # no visible addition either way
    assert cap_count(view) == counted
    if state == "applied":
        assert "e-l2b" in typed_ids(view)
        assert steps(view) == [("L2", "applied", 0, 1)]
    else:
        assert view.expansions[0].would_count == 4
        assert typed_ids(view) == typed_ids(base)


@pytest.mark.parametrize(("cap", "state"), [(6, "available"), (5, "over_cap")])
def test_eligibility_at_the_cap_boundary_counts_promotions_not_visible_nodes(
    cap: int, state: str
) -> None:
    """B would add D and G (visible) and promote L2 (already drawn): 3 + 3 = 6
    on the cap. The view already draws 5 notes with its two layer-only ones,
    so ``nodes + undrawn_nodes`` (7) would wrongly refuse it at a cap of 6."""
    view = view_of(max_nodes=cap)
    b = nodes(view)["B"].expansion

    assert (b.undrawn_nodes, b.undrawn_edges, b.would_count, b.cap) == (2, 3, 6, cap)
    assert len(view.nodes) + b.undrawn_nodes == 7
    assert b.state == state
    assert nodes(view)["F"].expansion.state == "focus"
    applied = view_of("B", max_nodes=cap).expansions[0]
    assert applied.state == ("applied" if state == "available" else "refused")


def test_undrawn_counts_for_the_focus_a_depth_neighbour_and_an_expanded_note() -> None:
    payload = {node["id"]: node for node in graph_payload(view_of("A"))["nodes"]}

    assert (payload["F"]["undrawn_nodes"], payload["F"]["undrawn_edges"]) == (0, 0)
    assert payload["F"]["expansion"]["state"] == "focus"
    assert (payload["A"]["undrawn_nodes"], payload["A"]["undrawn_edges"]) == (0, 0)
    assert payload["A"]["expansion"]["state"] == "expanded"
    # B, a depth neighbour: G is new; D (now drawn by A) and L2 are not; three
    # edges — so the counts are against the final view, not the base.
    assert (payload["B"]["undrawn_nodes"], payload["B"]["undrawn_edges"]) == (1, 3)
    assert payload["B"]["expansion"] == {
        "state": "available",
        "would_count": 8,
        "cap": 250,
    }
    assert payload["D"]["undrawn_nodes"] == 0  # edge-only: D–B
    assert payload["D"]["undrawn_edges"] == 1


def test_a_note_whose_edges_are_all_drawn_is_complete_and_its_request_applies() -> None:
    base = view_of(depth=2)
    view = view_of("A", depth=2)

    assert nodes(base)["A"].expansion.state == "complete"
    assert steps(view) == [("A", "applied", 0, 0)]
    assert nodes(view)["A"].expanded
    assert nodes(view)["A"].expansion.state == "expanded"


# ── collapse: transitive dependants, overlapping branches ──────────────


def test_collapse_removes_a_request_and_its_transitive_via_dependants() -> None:
    view = view_of("A", "C", "B")

    assert view.collapses["A"].removed == ("A", "C")  # C was first drawn by A
    assert view.collapses["C"].removed == ("C",)
    assert view.collapses["B"].removed == ("B",)


def test_a_shared_note_survives_while_its_dependent_expansion_is_removed() -> None:
    view = view_of("A", "C", "B")
    after_a = view.collapses["A"]

    # D was first drawn by A, but B still reaches it; C and E were A's alone.
    assert "D" in after_a.nodes and "e-bd" in after_a.edges
    assert not {"C", "E"} & after_a.nodes
    assert "e-ce" not in after_a.edges
    # Exactly what the remaining requests draw when assembled afresh.
    rebuilt = view_of("B")
    assert after_a.nodes == set(nodes(rebuilt))
    assert after_a.edges == typed_ids(rebuilt)


def test_an_unapplied_request_removes_only_itself() -> None:
    view = view_of("C", "A")

    assert view.collapses["C"].removed == ("C",)
    # C is unreached at its turn but first drawn by A: removing A takes it too.
    assert view.collapses["A"].removed == ("C", "A")  # in URL order


def test_dependants_follow_via_chains_whatever_their_order() -> None:
    walk = walk_expansions(
        lambda node_id: tuple(row for row in ROWS if node_id in row.endpoints),
        lambda row: (row.weight or 0) >= 0.1,
        focus="F",
        hops={"F": 0, "A": 1, "B": 1},
        edges=ROWS[:2],
        layer_ids=(),
        requests=("A", "C", "B"),
        cap=250,
    )

    assert dependants(walk, "A") == ("A", "C")
    assert dependants(walk, "B") == ("B",)


# ── the edge= / pin= exemption ─────────────────────────────────────────


def test_an_expansion_draws_the_exempt_selection_and_pins_it() -> None:
    selected = KnowledgeGraphFilters(selected_edge="e-aw")
    base = view_of(filters=selected)
    view = view_of("A", filters=selected)

    # Two hops out, the selection is not in depth 1's view; A's expansion
    # draws it whatever its weight, and only the exemption does.
    assert "e-aw" not in typed_ids(base) and base.pinned == ""
    assert "e-aw" in typed_ids(view) and "W" in nodes(view)
    assert view.pinned == "e-aw"
    assert view.hidden.by_weight == 0  # an exempt edge is never hidden
    assert view.keeps_drawing_for("e-aw") and not view.keeps_drawing_for("")
    assert nodes(view)["W"].via == "A"


# ── the orchestrator: reads ─────────────────────────────────────────────


class Wiring:
    """Fixed rows, a counted ``related`` and a counted facts read."""

    def __init__(self) -> None:
        self.reads: list[str] = []
        self.related_calls: list[str] = []
        self.table = EdgeTable(self._edge_list)
        self.facts = NoteFactsCache(self._read, lambda: asyncio.Semaphore(8))

    async def _edge_list(
        self, edge_type: str | None, namespace: str | None
    ) -> tuple[KnowledgeEdge, ...]:
        return ROWS

    async def _read(self, note_id: str) -> NoteRecord | None:
        self.reads.append(note_id)
        return NoteRecord(id=note_id, title=f"Title {note_id}", content="")

    async def related(self, note_id: str) -> RelatedNeighborhood:
        self.related_calls.append(note_id)
        return LAYERS

    async def view(self, *expand: str, **kwargs: Any) -> KnowledgeGraphView:
        return await assemble_focus_graph(
            self.table, self.related, self.facts, "F", expand=expand, **kwargs
        )


async def test_an_over_cap_base_spends_no_related_or_facts_read_on_expansions() -> None:
    wiring = Wiring()

    view = await wiring.view("A", "L", max_nodes=2)

    assert view.refusal is not None
    assert (wiring.related_calls, wiring.reads) == ([], [])


async def test_a_layer_only_root_reads_its_facts_once_promoted_and_no_layers() -> None:
    wiring = Wiring()

    view = await wiring.view("L")

    # Only the focus's neighbourhood is read: L's own is not fetched.
    assert wiring.related_calls == ["F"]
    # Promoted L and its new neighbour M are typed nodes and read in hop
    # order after the base; L2, still layer-only, is not read.
    assert wiring.reads[0] == "F"
    assert set(wiring.reads) == {"F", "A", "B", "L", "M"}
    assert wiring.reads.index("M") > wiring.reads.index("L")
    by_id = nodes(view)
    assert (by_id["L"].label, by_id["L"].facts_state) == ("Title L", "ok")
    assert (by_id["L2"].label, by_id["L2"].facts_state) == ("Linking note", "unread")


async def test_expanded_nodes_spend_the_fanout_cap_after_the_base() -> None:
    wiring = Wiring()

    view = await wiring.view("A", "C", fanout_cap=4)

    # The focus, then the hop-1 notes (L promoted by A's edge to it); the
    # expansions' notes at hops 2 and 3 come after, and the cap is spent.
    assert wiring.reads[0] == "F"
    assert set(wiring.reads) == {"F", "A", "B", "L"}
    assert {nodes(view)[n].facts_state for n in ("C", "D", "E")} == {"unread"}


def test_global_mode_carries_the_same_payload_shape_with_no_expansion() -> None:
    view = build_view(
        global_typed_graph(ROWS),
        mode="global",
        filters=KnowledgeGraphFilters(),
        scope_type="supports",
    )
    payload = json.loads(json.dumps(graph_payload(view)))

    assert payload["expansions"] == []
    for node in payload["nodes"]:
        assert (node["expanded"], node["via"], node["expansion"]) == (
            False,
            None,
            None,
        )
        assert (node["undrawn_nodes"], node["undrawn_edges"]) == (0, 0)
