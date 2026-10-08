"""K2 slice 3 — the contradictions queue's pure helpers (``knowledge_graph_view``).

``?type=contradicts`` lists every edge unresolved-first, newest-first within
state, each with the first sentence of its rationale (PRD D11, story 13). The
order and the sentence are pure functions so the page only prints them; the
page's own test (``test_knowledge_graph_page.py``) proves it prints them in
this order.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from lithos_lens.knowledge_edge_types import EdgeDirection
from lithos_lens.knowledge_edges import EdgeTableSnapshot, KnowledgeEdge
from lithos_lens.knowledge_graph import assemble_focus_view
from lithos_lens.knowledge_graph_view import (
    KnowledgeGraphEdge,
    contradictions_queue,
    edge_rationale,
    first_sentence,
)


def contradiction(
    edge_id: str,
    *,
    conflict_state: str | None = None,
    created_at: str | None = "2026-08-01T00:00:00+00:00",
    edge_type: str = "contradicts",
) -> KnowledgeGraphEdge:
    return KnowledgeGraphEdge(
        id=edge_id,
        from_id="A",
        to_id="B",
        kind="typed",
        type=edge_type,
        direction=EdgeDirection.SYMMETRIC,
        conflict_state=conflict_state,
        created_at=created_at,
    )


def test_unresolved_first_by_the_resolution_rule_not_by_null() -> None:
    """``"pending"`` and ``""`` are unresolved (F6): only the four
    ``lithos_conflict_resolve`` values sort after."""
    queue = contradictions_queue(
        [
            contradiction("e1", conflict_state="superseded"),
            contradiction("e2", conflict_state="pending"),
            contradiction("e3", conflict_state=None),
            contradiction("e4", conflict_state="merged"),
            contradiction("e5", conflict_state=""),
        ]
    )
    assert [edge.id for edge in queue] == ["e2", "e3", "e5", "e1", "e4"]


def test_newest_first_within_state_missing_timestamps_last_then_edge_id() -> None:
    queue = contradictions_queue(
        [
            contradiction("old", created_at="2026-08-01T00:00:00+00:00"),
            contradiction("none", created_at=None),
            contradiction("new", created_at="2026-09-01T00:00:00+00:00"),
            contradiction("bad", created_at="not a time"),
            contradiction("tie-b", created_at="2026-08-15T00:00:00+00:00"),
            contradiction("tie-a", created_at="2026-08-15T00:00:00+00:00"),
            contradiction("resolved-new", conflict_state="refuted",
                          created_at="2026-10-01T00:00:00+00:00"),
        ]
    )  # fmt: skip
    assert [edge.id for edge in queue] == [
        "new", "tie-a", "tie-b", "old", "bad", "none", "resolved-new",
    ]  # fmt: skip


def test_the_queue_lists_contradicts_rows_only() -> None:
    queue = contradictions_queue(
        [contradiction("c"), contradiction("s", edge_type="supports")]
    )
    assert [edge.id for edge in queue] == ["c"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("One. Two.", "One."),
        ("Is it? Yes.", "Is it?"),
        ("Stop!\nGo.", "Stop!"),
        ("Version 1.2 ships. Then 1.3.", "Version 1.2 ships."),
        ("No mark at all", "No mark at all"),
        ("  Trailing mark.  ", "Trailing mark."),
        ("e.g.this. Then", "e.g.this."),
        ("", ""),
    ],
)
def test_first_sentence(text: str, expected: str) -> None:
    assert first_sentence(text) == expected


def test_the_rationale_comes_from_evidence_json_or_its_raw_text() -> None:
    inferred = json.dumps(
        {"rationale": "Half the write rate. Measured in May.", "model": "m"}
    )
    assert edge_rationale(inferred) == "Half the write rate."
    assert edge_rationale("Free text, not JSON. More.") == "Free text, not JSON."
    assert edge_rationale(json.dumps({"model": "m"})) == ""
    assert edge_rationale(None) == ""


def test_the_view_edge_carries_the_queue_columns_from_the_row() -> None:
    row = KnowledgeEdge(
        edge_id="e",
        from_id="A",
        to_id="B",
        type="contradicts",
        weight=0.7,
        namespace="influx",
        created_at="2026-08-03T09:12:44+00:00",
        evidence='{"rationale": "R."}',
    )
    view = assemble_focus_view(
        EdgeTableSnapshot(rows=(row,), as_of=datetime(2026, 10, 8, tzinfo=UTC)), "A"
    )
    (drawn,) = view.edges
    assert (drawn.namespace, drawn.created_at, drawn.evidence) == (
        "influx",
        "2026-08-03T09:12:44+00:00",
        '{"rationale": "R."}',
    )
