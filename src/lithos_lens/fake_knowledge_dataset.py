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
``/knowledge`` landing shows both of its sections in fake mode, and the demo's
knowledge edge table (:func:`knowledge_edge_rows`) that ``lithos_edge_list``
serves.
"""

from __future__ import annotations

import json

from lithos_lens.knowledge import RelatedNeighborhood, RelatedRef
from lithos_lens.knowledge_edge_evidence import EdgeWhy, edge_why
from lithos_lens.tasks import NoteRecord

__all__ = ["intake_fixtures", "knowledge_edge_rows", "related_fixtures"]


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


# The demo note ids the edge table connects, plus one id that is NOT a note:
# edges outlive their notes upstream (nothing deletes them), so the demo
# carries a dangling endpoint for the graph's missing-note ghost.
_PLAN = "note-influx-plan"
_LEGACY = "note-influx-legacy-ingest"
_ROLLBACK = "note-influx-rollback"
_CAPACITY = "note-influx-capacity"
DANGLING_NOTE_ID = "note-influx-archived-sizing"


def _edge_row(
    edge_id: str,
    from_id: str,
    to_id: str,
    edge_type: str,
    weight: float,
    *,
    namespace: str = "influx",
    created_at: str = "2026-08-03T09:12:44.518230+00:00",
    updated_at: str | None = None,
    provenance_actor: str | None = None,
    provenance_type: str | None = None,
    evidence: str | None = None,
    conflict_state: str | None = None,
) -> dict[str, object]:
    """One ``edges`` row in the exact 12-key ``lithos_edge_list`` shape."""
    return {
        "edge_id": edge_id,
        "from_id": from_id,
        "to_id": to_id,
        "type": edge_type,
        "weight": weight,
        "namespace": namespace,
        "created_at": created_at,
        "updated_at": updated_at or created_at,
        "provenance_actor": provenance_actor,
        "provenance_type": provenance_type,
        "evidence": evidence,
        "conflict_state": conflict_state,
    }


def _inferred_row(
    edge_id: str,
    from_id: str,
    to_id: str,
    edge_type: str,
    confidence: float,
    rationale: str,
    *,
    provenance_actor: str = "lithos-enrich",
    updated_at: str | None = None,
    conflict_state: str | None = None,
) -> dict[str, object]:
    """An LCMA-inferred row: weight = confidence, the evidence JSON it writes."""
    evidence = json.dumps(
        {"rationale": rationale, "model": "claude-haiku-4-5", "confidence": confidence}
    )
    return _edge_row(
        edge_id,
        from_id,
        to_id,
        edge_type,
        confidence,
        updated_at=updated_at,
        provenance_actor=provenance_actor,
        provenance_type="inferred",
        evidence=evidence,
        conflict_state=conflict_state,
    )


def knowledge_edge_rows() -> tuple[dict[str, object], ...]:
    """The demo's knowledge edge table, as ``lithos_edge_list`` answers it.

    Raw rows in the vendored contract's shape
    (``tests/contracts/lithos_edge_list.json``), which the fake hands to the
    real normalizer. Symmetric types (``related_to``, ``analogy_to``,
    ``contradicts``) are stored ``from_id <= to_id``, as upstream stores
    them. Between them the rows carry every known type, one type Lens does
    not know (``assesses``), a dangling endpoint, two unresolved
    contradictions (``conflict_state`` NULL) and one resolved, and
    consolidation-weight ``related_to`` rows (0.03 and one repeat). The typed
    relations :func:`related_fixtures` shows are all here too, so the related
    panel and the graph agree in fake mode.
    """
    return (
        _inferred_row(
            "edge_4c1e9a7b20d3",
            _CAPACITY,
            _PLAN,
            "supports",
            0.82,
            "The capacity report's measured write rate is the headroom the "
            "migration plan's cutover window assumes.",
        ),
        _edge_row(
            "edge_9b2f61c0a4e8",
            _CAPACITY,
            _ROLLBACK,
            "related_to",
            0.5,
            provenance_type="reinforcement",
        ),
        _edge_row(
            "edge_15d0c3e8f972",
            _LEGACY,
            _PLAN,
            "related_to",
            0.03,
            provenance_actor="lithos-enrich",
            provenance_type="consolidation",
        ),
        _edge_row(
            "edge_d83a5b9e0c41",
            _LEGACY,
            _ROLLBACK,
            "related_to",
            0.06,
            updated_at="2026-08-05T14:02:10.004917+00:00",
            provenance_actor="lithos-enrich",
            provenance_type="consolidation",
        ),
        _inferred_row(
            "edge_6f47e2d91b35",
            _LEGACY,
            _ROLLBACK,
            "analogy_to",
            0.66,
            "Both describe draining writes to a second store before switching "
            "reads over.",
        ),
        _inferred_row(
            "edge_a07c5f3e18b2",
            _PLAN,
            _LEGACY,
            "refines",
            0.74,
            "The migration plan keeps the legacy ingest's stages and narrows "
            "the cutover to a dual-write window.",
        ),
        _inferred_row(
            "edge_2e98b4d6c01f",
            _ROLLBACK,
            _PLAN,
            "is_example_of",
            0.68,
            "The rollback route is one instance of the plan's abort stage.",
        ),
        _inferred_row(
            "edge_c5b1730f9ad6",
            _PLAN,
            _CAPACITY,
            "depends_on",
            0.71,
            "The plan's cutover date rests on the capacity report's headroom.",
        ),
        _edge_row(
            "edge_7d2c0e95b463",
            _ROLLBACK,
            _PLAN,
            "derived_from",
            1.0,
            namespace="runbooks",
            provenance_type="frontmatter",
        ),
        # Unresolved, asserted with neither provenance nor evidence: the
        # related panel's row with no "why?" disclosure.
        _edge_row(
            "edge_e1f4a8c27b90",
            _PLAN,
            _ROLLBACK,
            "contradicts",
            0.8,
        ),
        _inferred_row(
            "edge_38c9d1f5e6a7",
            _CAPACITY,
            _LEGACY,
            "contradicts",
            0.7,
            "The legacy ingest note sizes the cluster for half the write rate "
            "this report measured.",
        ),
        # Resolved: lithos_conflict_resolve wrote the resolution and the
        # resolver as provenance_actor.
        _inferred_row(
            "edge_b6e0f27d4c18",
            _LEGACY,
            _PLAN,
            "contradicts",
            0.9,
            "The legacy ingest keeps a single writer; the plan dual-writes.",
            provenance_actor="dave",
            updated_at="2026-08-06T10:30:00.120044+00:00",
            conflict_state="superseded",
        ),
        _edge_row(
            "edge_f29d84a6130c",
            _CAPACITY,
            _PLAN,
            "assesses",
            0.6,
            provenance_actor="worker-b",
            provenance_type="manual",
        ),
        _inferred_row(
            "edge_0a5e6c9b7d24",
            DANGLING_NOTE_ID,
            _CAPACITY,
            "supports",
            0.61,
            "The archived sizing estimate is the baseline this report revisits.",
        ),
    )
