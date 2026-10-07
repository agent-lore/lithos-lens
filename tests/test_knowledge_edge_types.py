"""K2 slice 1 — the knowledge-edge vocabulary (``knowledge_edge_types.py``).

Table-driven against K2 PRD D5: each known type's direction and style, no
arrowhead on a symmetric type, an unknown type drawn as recorded with its raw
label, and a legend listing exactly the types present, in table order.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from lithos_lens.knowledge_edge_types import (
    KNOWN_KNOWLEDGE_EDGE_TYPES,
    UNKNOWN_EDGE_CSS_CLASS,
    EdgeDirection,
    EdgeStyle,
    direction_of,
    edge_style,
    known_edge_type,
    legend,
)


@dataclass(frozen=True)
class Row:
    """The two fields the vocabulary reads off an edge row."""

    type: str
    conflict_state: str | None = None


# (type, direction, stroke, arrowhead, css class) — the PRD's D5 table.
KNOWN = [
    ("supports", EdgeDirection.DIRECTED, "solid", True, "kedge-supports"),
    ("related_to", EdgeDirection.SYMMETRIC, "solid", False, "kedge-related-to"),
    ("analogy_to", EdgeDirection.SYMMETRIC, "solid", False, "kedge-analogy-to"),
    ("refines", EdgeDirection.DIRECTED, "solid", True, "kedge-refines"),
    ("is_example_of", EdgeDirection.DIRECTED, "solid", True, "kedge-is-example-of"),
    ("depends_on", EdgeDirection.DIRECTED, "solid", True, "kedge-depends-on"),
    ("derived_from", EdgeDirection.DIRECTED, "dotted", True, "kedge-derived-from"),
]


def test_the_table_is_the_prd_order() -> None:
    assert [known.name for known in KNOWN_KNOWLEDGE_EDGE_TYPES] == [
        "supports",
        "related_to",
        "analogy_to",
        "refines",
        "is_example_of",
        "depends_on",
        "derived_from",
        "contradicts",
    ]


@pytest.mark.parametrize(
    ("edge_type", "direction", "stroke", "arrowhead", "css_class"),
    KNOWN,
    ids=[row[0] for row in KNOWN],
)
def test_each_known_type_s_direction_and_style(
    edge_type: str,
    direction: EdgeDirection,
    stroke: str,
    arrowhead: bool,
    css_class: str,
) -> None:
    row = Row(edge_type)
    assert direction_of(row) is direction
    assert edge_style(row) == EdgeStyle(css_class, stroke, arrowhead)  # type: ignore[arg-type]
    known = known_edge_type(edge_type)
    assert known is not None and known.legend.startswith("A ")


def test_an_unresolved_contradiction_is_dashed_and_unlabelled() -> None:
    row = Row("contradicts")
    assert direction_of(row) is EdgeDirection.SYMMETRIC
    assert edge_style(row) == EdgeStyle(
        "kedge-contradicts kedge-unresolved", "dashed", False
    )


@pytest.mark.parametrize("state", ["accepted_dual", "superseded", "refuted", "merged"])
def test_a_resolved_contradiction_is_muted_and_labelled_with_its_resolution(
    state: str,
) -> None:
    assert edge_style(Row("contradicts", state)) == EdgeStyle(
        "kedge-contradicts kedge-resolved", "dashed", False, label=state
    )


@pytest.mark.parametrize("marker", ["unresolved", "pending", "Superseded", " "])
def test_an_authored_marker_is_not_taken_for_a_resolution(marker: str) -> None:
    """``lithos_edge_upsert`` stores a caller's ``conflict_state`` unvalidated,
    so only the four ``lithos_conflict_resolve`` values mean resolved. Any
    other marker stays unresolved (dashed red) and is shown as written."""
    assert edge_style(Row("contradicts", marker)) == EdgeStyle(
        "kedge-contradicts kedge-unresolved", "dashed", False, label=marker
    )


def test_an_empty_marker_is_unresolved_and_unlabelled() -> None:
    assert edge_style(Row("contradicts", "")) == EdgeStyle(
        "kedge-contradicts kedge-unresolved", "dashed", False
    )


@pytest.mark.parametrize(
    "edge_type",
    [known.name for known in KNOWN_KNOWLEDGE_EDGE_TYPES],
)
def test_symmetric_types_have_no_arrowhead_and_directed_ones_do(
    edge_type: str,
) -> None:
    row = Row(edge_type)
    symmetric = edge_type in {"related_to", "analogy_to", "contradicts"}
    assert (direction_of(row) is EdgeDirection.SYMMETRIC) is symmetric
    assert edge_style(row).arrowhead is not symmetric


@pytest.mark.parametrize("edge_type", ["assesses", "summarizes", "Supports"])
def test_an_unknown_type_is_drawn_as_recorded_with_its_raw_label(
    edge_type: str,
) -> None:
    row = Row(edge_type, conflict_state="superseded")
    assert known_edge_type(edge_type) is None
    assert direction_of(row) is EdgeDirection.AS_RECORDED
    assert direction_of(row).has_arrowhead
    assert edge_style(row) == EdgeStyle(
        UNKNOWN_EDGE_CSS_CLASS, "solid", True, label=edge_type
    )


def test_the_legend_lists_exactly_the_types_present_in_order() -> None:
    present = ["contradicts", "assesses", "supports", "derived_from", "supports"]
    lines = legend(present)

    assert [line.type for line in lines] == [
        "supports",
        "derived_from",
        "contradicts",
        "assesses",
    ]
    assert [line.known for line in lines] == [True, True, True, False]
    assert lines[0].line == "A supports B: A is evidence for B"
    assert lines[-1].css_class == UNKNOWN_EDGE_CSS_CLASS
    assert "assesses" in lines[-1].line and "as recorded" in lines[-1].line


def test_unknown_types_follow_the_known_ones_by_name_and_empty_is_empty() -> None:
    assert [line.type for line in legend(["zeta", "alpha", "related_to"])] == [
        "related_to",
        "alpha",
        "zeta",
    ]
    assert legend([]) == ()
