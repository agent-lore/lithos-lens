"""A typed edge's "why?": evidence parsing and provenance wording (pure)."""

from __future__ import annotations

import json

import pytest

from lithos_lens.knowledge_edge_evidence import (
    EdgeEvidence,
    EdgeWhy,
    edge_why,
    parse_edge_evidence,
    provenance_label,
)

# The evidence edge inference stores (lithos lcma/edge_inference.py at 0.6.0).
INFERRED = json.dumps(
    {
        "rationale": "Both notes measure the same cluster's write rate.",
        "model": "claude-haiku-4-5",
        "confidence": 0.82,
    }
)


def test_valid_evidence_json_yields_its_three_fields() -> None:
    assert parse_edge_evidence(INFERRED) == EdgeEvidence(
        rationale="Both notes measure the same cluster's write rate.",
        model="claude-haiku-4-5",
        confidence=0.82,
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"rationale": "Only a sentence."}, EdgeEvidence(rationale="Only a sentence.")),
        ({"model": "m-1", "confidence": 1}, EdgeEvidence(model="m-1", confidence=1.0)),
        # A mistyped key is omitted like a missing one; the rest still show.
        (
            {"rationale": "Kept.", "model": 7, "confidence": True},
            EdgeEvidence(rationale="Kept."),
        ),
    ],
)
def test_partial_evidence_json_yields_only_the_present_fields(
    payload: dict[str, object], expected: EdgeEvidence
) -> None:
    assert parse_edge_evidence(json.dumps(payload)) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "inferred from a shared citation",  # not JSON
        "{rationale: unquoted}",  # broken JSON
        '["a", "list"]',  # JSON, not an object
        "0.9",  # JSON scalar
        '{"source": "manual"}',  # an object with none of the three keys
        "<script>alert(1)</script>",
    ],
)
def test_evidence_that_is_not_the_inference_json_is_kept_raw(raw: str) -> None:
    assert parse_edge_evidence(raw) == EdgeEvidence(raw=raw)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_null_or_blank_evidence_is_no_evidence(value: str | None) -> None:
    assert parse_edge_evidence(value) is None


@pytest.mark.parametrize(
    ("provenance_type", "actor", "label"),
    [
        ("inferred", "lithos-enrich", "inferred by lithos-enrich"),
        ("consolidation", "lithos-enrich", "reinforced by citation"),
        ("frontmatter", None, "declared in frontmatter"),
        ("asserted", "agent-x", "asserted by agent-x"),
        ("authored", None, "authored"),
        (None, "agent-x", "recorded by agent-x"),
        (None, None, ""),
    ],
)
def test_provenance_label_words_each_kind(
    provenance_type: str | None, actor: str | None, label: str
) -> None:
    assert provenance_label(provenance_type, actor) == label


def test_edge_why_reads_the_three_row_keys() -> None:
    row = {
        "provenance_type": "inferred",
        "provenance_actor": "lithos-enrich",
        "evidence": INFERRED,
    }
    assert edge_why(row) == EdgeWhy(
        provenance="inferred by lithos-enrich",
        evidence=parse_edge_evidence(INFERRED),
    )


def test_a_frontmatter_edge_has_provenance_and_no_evidence() -> None:
    row = {"provenance_type": "frontmatter", "provenance_actor": None, "evidence": None}
    assert edge_why(row) == EdgeWhy(provenance="declared in frontmatter")


def test_a_row_with_null_evidence_and_no_provenance_has_no_disclosure() -> None:
    row = {"provenance_type": None, "provenance_actor": None, "evidence": None}
    assert edge_why(row) is None
