"""The demo dataset's related-panel half: the influx notes' neighborhoods.

:func:`~lithos_lens.fake_dataset.demo_dataset` carries these as
``related_neighborhoods``; they live here because that module sits at the
800-line ceiling ``docs/architecture.toml`` enforces, and because they are a
concern of their own: what ``lithos_related`` answers for each demo note.

A typed edge's ``why`` is built with
:func:`~lithos_lens.knowledge_edge_evidence.edge_why` from the three edge-row
keys exactly as ``lithos_related`` sends them
(``tests/contracts/lithos_related.json``): ``evidence`` a JSON string or null,
``provenance_type`` and ``provenance_actor`` as stored. Between them the rows
show each provenance the related panel words differently — an inference with
its rationale, a citation reinforcement, a frontmatter declaration — and one
edge (the ``contradicts`` pair) with neither, which renders no disclosure.
"""

from __future__ import annotations

import json

from lithos_lens.knowledge import RelatedNeighborhood, RelatedRef
from lithos_lens.knowledge_edge_evidence import EdgeWhy, edge_why

__all__ = ["related_fixtures"]


def _inferred(rationale: str, confidence: float) -> EdgeWhy | None:
    """An LCMA-inferred edge row's why: the evidence JSON edge inference writes."""
    return edge_why(
        {
            "provenance_type": "inferred",
            "provenance_actor": "lithos-enrich",
            "evidence": json.dumps(
                {
                    "rationale": rationale,
                    "model": "claude-haiku-4-5",
                    "confidence": confidence,
                }
            ),
        }
    )


def _declared(provenance_type: str) -> EdgeWhy | None:
    """A row with no evidence (null), only its provenance — no rationale."""
    return edge_why(
        {"provenance_type": provenance_type, "provenance_actor": None, "evidence": None}
    )


def related_fixtures() -> dict[str, RelatedNeighborhood]:
    """Related-panel (K1-S4) neighborhoods over the influx notes.

    The plan wiki-links the rollback route (outgoing link / incoming
    back-link) and the rollback route carries an unresolved ``contradicts``
    edge against the plan, so the panel's direction badges and conflict label
    all render. The capacity report has every group but back-links, so the
    summary line counts and omits.
    """
    return {
        "note-influx-plan": RelatedNeighborhood(
            links=(
                RelatedRef(id="note-influx-rollback", title="Influx rollback route"),
            ),
            unresolved=("drafts/influx-capacity.md",),
            edges=(
                RelatedRef(
                    id="note-influx-rollback",
                    edge_type="contradicts",
                    weight=0.8,
                    direction="incoming",
                    conflict_state="unresolved",
                ),
            ),
        ),
        "note-influx-capacity": RelatedNeighborhood(
            links=(
                RelatedRef(id="note-influx-plan", title="Influx migration plan"),
                RelatedRef(id="note-influx-rollback", title="Influx rollback route"),
            ),
            sources=(RelatedRef(id="note-influx-plan", title="Influx migration plan"),),
            edges=(
                RelatedRef(
                    id="note-influx-plan",
                    edge_type="supports",
                    weight=0.82,
                    direction="outgoing",
                    why=_inferred(
                        "The capacity report's measured write rate is the headroom "
                        "the migration plan's cutover window assumes.",
                        0.82,
                    ),
                ),
                RelatedRef(
                    id="note-influx-rollback",
                    edge_type="related_to",
                    weight=0.5,
                    direction="outgoing",
                    why=_declared("consolidation"),
                ),
                RelatedRef(
                    id="note-influx-legacy-ingest",
                    edge_type="contradicts",
                    weight=0.7,
                    direction="incoming",
                    why=_inferred(
                        "The legacy ingest note sizes the cluster for half the "
                        "write rate this report measured.",
                        0.7,
                    ),
                ),
            ),
        ),
        "note-influx-rollback": RelatedNeighborhood(
            backlinks=(
                RelatedRef(id="note-influx-plan", title="Influx migration plan"),
            ),
            edges=(
                RelatedRef(
                    id="note-influx-plan",
                    edge_type="contradicts",
                    weight=0.8,
                    direction="outgoing",
                    conflict_state="unresolved",
                ),
                RelatedRef(
                    id="note-influx-plan",
                    edge_type="derived_from",
                    weight=1.0,
                    direction="outgoing",
                    why=_declared("frontmatter"),
                ),
            ),
        ),
    }
