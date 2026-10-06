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

It also carries the demo's intake notes (:func:`intake_fixtures`), so the
``/knowledge`` landing shows both of its sections in fake mode.
"""

from __future__ import annotations

import json

from lithos_lens.knowledge import RelatedNeighborhood, RelatedRef
from lithos_lens.knowledge_edge_evidence import EdgeWhy, edge_why
from lithos_lens.tasks import NoteRecord

__all__ = ["intake_fixtures", "related_fixtures"]


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


def intake_fixtures() -> tuple[dict[str, NoteRecord], dict[str, str]]:
    """Intake notes (§7.1) and their paths, one per way a note becomes intake.

    Tagged only (``ingested-by:influx`` under ``influx/``), tagged AND under an
    intake prefix (``papers/``), and prefixed only (``digests/``, untagged).
    All three are newer than every one of the demo's own notes, which is the
    live corpus's shape: a single recency list would be intake end to end.
    Their text names neither "influx" nor "ingest", so the demo's searches for
    those still answer the notes they always did.
    """
    notes = {
        "intake-rss-tail-latency": NoteRecord(
            id="intake-rss-tail-latency",
            title="Tracing tail latency in streaming pipelines",
            content="Feed item: where p99 hides in a streaming pipeline.\n",
            tags=("ingested-by:influx", "source:rss"),
            metadata={"updated_at": "2026-08-09T07:00:00+00:00"},
        ),
        "intake-paper-compaction": NoteRecord(
            id="intake-paper-compaction",
            title="Log-structured compaction under write bursts",
            content="Paper: compaction scheduling when writes arrive in bursts.\n",
            tags=("ingested-by:influx", "source:arxiv"),
            metadata={"updated_at": "2026-08-08T07:00:00+00:00"},
        ),
        "intake-digest-storage": NoteRecord(
            id="intake-digest-storage",
            title="Weekly digest: storage engines",
            content="Digest: this week's storage-engine reading.\n",
            metadata={"updated_at": "2026-08-07T07:00:00+00:00"},
        ),
    }
    paths = {
        "influx/rss/tracing-tail-latency.md": "intake-rss-tail-latency",
        "papers/log-structured-compaction.md": "intake-paper-compaction",
        "digests/2026-w32-storage-engines.md": "intake-digest-storage",
    }
    return notes, paths
