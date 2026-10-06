"""A typed edge's "why?": evidence parsing and provenance wording (pure)."""

from __future__ import annotations

import json

import pytest

from lithos_lens.knowledge import normalize_related
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
        "<script>alert(1)</script>",
        # Decoded, but a number no float holds: a normalization failure.
        json.dumps({"rationale": "Manual supporting evidence.", "confidence": 10**400}),
        '{"rationale": "Not a number.", "confidence": NaN}',
        # Nested past the decoder's recursion limit: json.loads raises
        # RecursionError, not ValueError.
        pytest.param("[" * 10000 + "0" + "]" * 10000, id="nested-past-recursion"),
    ],
)
def test_evidence_that_is_not_the_inference_json_is_kept_raw(raw: str) -> None:
    assert parse_edge_evidence(raw) == EdgeEvidence(raw=raw)


@pytest.mark.parametrize(
    "payload",
    [{}, {"source": "manual"}, {"rationale": "", "model": ""}],
)
def test_a_decoded_object_with_none_of_the_keys_is_empty_not_raw(
    payload: dict[str, object],
) -> None:
    """Zero present fields is the partial-JSON boundary: nothing to show, but
    it decoded, so the raw fallback (for decoding failures) does not apply."""
    assert parse_edge_evidence(json.dumps(payload)) == EdgeEvidence()


def test_an_overflowing_confidence_keeps_the_neighborhood_renderable() -> None:
    """One edge's unconvertible evidence must not fail the whole panel."""
    evidence = json.dumps({"rationale": "Manual.", "confidence": 10**400})
    row = {
        "from_id": "root",
        "to_id": "other",
        "type": "supports",
        "provenance_type": "asserted",
        "provenance_actor": "agent-x",
        "evidence": evidence,
    }
    (ref,) = normalize_related({"edges": {"outgoing": [row]}}).edges

    assert ref.why == EdgeWhy(
        provenance="asserted by agent-x", evidence=EdgeEvidence(raw=evidence)
    )


def test_too_deeply_nested_evidence_keeps_the_neighborhood_renderable() -> None:
    evidence = "[" * 10000 + "0" + "]" * 10000
    row = {
        "from_id": "root",
        "to_id": "other",
        "type": "supports",
        "evidence": evidence,
    }
    (ref,) = normalize_related({"edges": {"outgoing": [row]}}).edges

    assert ref.why == EdgeWhy(evidence=EdgeEvidence(raw=evidence))


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


@pytest.mark.parametrize("evidence", [None, "{}"])
def test_a_row_with_nothing_to_show_has_no_disclosure(evidence: str | None) -> None:
    row = {"provenance_type": None, "provenance_actor": None, "evidence": evidence}
    assert edge_why(row) is None
