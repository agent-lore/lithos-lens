"""The demo fixture dataset behind fake-Lithos app mode.

:class:`FakeLithosDataset` is the pure data half of the fake-Lithos seam: an
immutable bundle of every record the in-memory
:class:`~lithos_lens.fake_lithos.FakeLithosClient` serves. Keeping the dataset
apart from the client behavior makes this file an editable demo artifact —
grow or reshape the demo here without touching protocol semantics — and lets
tests compose their own fixture sets (``FakeLithosClient(dataset=...)``)
instead of inheriting the demo's.

:func:`demo_dataset` builds the shipped demo set: enough tasks, claims,
findings, notes, and graph edges for every server-rendered surface to light up
in fake mode, which is exactly what the Playwright smoke suite under ``e2e/``
browses.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from lithos_lens.knowledge import RelatedNeighborhood, RelatedRef
from lithos_lens.task_graph import BlockerRecord, EdgeRecord
from lithos_lens.tasks import (
    AgentRecord,
    ClaimRecord,
    FindingRecord,
    NoteRecord,
    TaskRecord,
)

__all__ = ["FakeLithosDataset", "demo_dataset"]


@dataclass(frozen=True)
class FakeLithosDataset:
    """Immutable fixture bundle served by ``FakeLithosClient``.

    Every field defaults to empty, so a test can compose a minimal dataset
    from just the records its scenario needs. The graph fields are a fixture
    oracle, not derived state: Lens never re-derives readiness, so
    ``ready_ids`` / ``blocked`` are the source of truth for the frontier the
    client reports.
    """

    tasks: tuple[TaskRecord, ...] = ()
    notes: Mapping[str, NoteRecord] = field(default_factory=dict)
    related_neighborhoods: Mapping[str, RelatedNeighborhood] = field(
        default_factory=dict
    )
    ready_ids: frozenset[str] = frozenset()
    blocked: Mapping[str, tuple[BlockerRecord, ...]] = field(default_factory=dict)
    edges: Mapping[str, tuple[EdgeRecord, ...]] = field(default_factory=dict)
    children: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    findings: Mapping[str, tuple[FindingRecord, ...]] = field(default_factory=dict)
    claims: Mapping[str, tuple[ClaimRecord, ...]] = field(default_factory=dict)
    agents: tuple[AgentRecord, ...] = ()
    stats: Mapping[str, Any] = field(default_factory=dict)


def demo_dataset() -> FakeLithosDataset:
    """Build the shipped demo fixture set for fake-Lithos app mode."""

    # Findings on the flagship claimed task link to these notes so the
    # knowledge surface (/note/<id>) renders in fake mode too.
    notes: dict[str, NoteRecord] = {
        "note-influx-plan": NoteRecord(
            id="note-influx-plan",
            title="Influx migration plan",
            content=(
                "# Influx migration plan\n\n"
                "Cut over the ingest path first, then backfill. See "
                "[[note-influx-rollback]] for the abort route.\n\n"
                "- Stage 1: dual-write\n"
                "- Stage 2: read swap\n"
                "- Stage 3: decommission\n"
            ),
            tags=("project:influx", "kind:plan"),
        ),
        "note-influx-rollback": NoteRecord(
            id="note-influx-rollback",
            title="Influx rollback route",
            content=(
                "# Influx rollback route\n\n"
                "If read swap regresses, flip the feature gate and keep "
                "dual-writing.\n"
            ),
            tags=("project:influx", "kind:runbook"),
        ),
    }

    # Related-panel (K1-S4) neighborhood fixtures over the two notes: the plan
    # wiki-links the rollback route (outgoing link / incoming back-link) and
    # the rollback route carries an unresolved ``contradicts`` edge against the
    # plan, so the panel's direction badges and conflict label all render.
    related_neighborhoods: dict[str, RelatedNeighborhood] = {
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
            ),
        ),
    }

    tasks: tuple[TaskRecord, ...] = (
        TaskRecord(
            id="influx-ingest-cutover",
            title="Cut over Influx ingest path",
            description="Dual-write, then swap reads onto the new store.",
            status="open",
            created_by="planner",
            created_at="2026-08-06T09:00:00+00:00",
            tags=("project:influx", "area:data"),
        ),
        TaskRecord(
            id="influx-backfill",
            title="Backfill historical Influx series",
            description="Replay the archive once the cutover is stable.",
            status="open",
            created_by="planner",
            created_at="2026-08-05T09:00:00+00:00",
            tags=("project:influx", "area:data"),
        ),
        TaskRecord(
            id="influx-dashboards",
            title="Rebuild Influx operator dashboards",
            status="open",
            created_by="planner",
            created_at="2026-08-04T09:00:00+00:00",
            tags=("project:influx", "area:observability"),
        ),
        TaskRecord(
            id="lens-graph-view",
            title="Ship graph-native operator view",
            description="Rebuild the dashboard on the ready/blocked frontier.",
            status="open",
            created_by="planner",
            created_at="2026-08-03T09:00:00+00:00",
            tags=("project:lithos-lens", "milestone:t1"),
        ),
        TaskRecord(
            id="influx-ingest-old",
            title="Retire legacy Influx ingest shim",
            status="open",
            created_by="planner",
            created_at="2025-11-01T09:00:00+00:00",
            tags=("project:influx",),
        ),
        TaskRecord(
            id="lens-note-view",
            title="Land knowledge note view",
            status="completed",
            outcome="Shipped in 0.3.0.",
            created_by="worker-a",
            # Static and inside the suite's static since=2026-08-01 window,
            # so the completed group renders a real row (no Date.now()-style
            # nondeterminism — both sides of the comparison are fixed).
            created_at="2026-08-04T09:00:00+00:00",
            resolved_at="2026-08-05T17:00:00+00:00",
            tags=("project:lithos-lens", "milestone:k1"),
        ),
        TaskRecord(
            id="influx-spike",
            title="Spike Influx client options",
            status="cancelled",
            outcome="Superseded by the cutover plan.",
            created_by="worker-b",
            # Inside the since window for the same reason as lens-note-view.
            created_at="2026-08-03T09:00:00+00:00",
            resolved_at="2026-08-04T12:00:00+00:00",
            tags=("project:influx",),
        ),
    )

    return FakeLithosDataset(
        tasks=tasks,
        notes=notes,
        related_neighborhoods=related_neighborhoods,
        # Graph oracle: `influx-ingest-cutover` is the ready, claimed,
        # in-flight task (the only one carrying claims), so it must sit on the
        # ready frontier too; `influx-dashboards` / `lens-graph-view` are
        # ready and unclaimed; `influx-backfill` is blocked on the cutover.
        ready_ids=frozenset(
            {"influx-ingest-cutover", "influx-dashboards", "lens-graph-view"}
        ),
        blocked={
            "influx-backfill": (
                BlockerRecord(
                    kind="task",
                    task_id="influx-ingest-cutover",
                    type="blocks",
                    status="open",
                    message="Waiting on the ingest cutover to land.",
                ),
            ),
        },
        edges={
            "influx-ingest-cutover": (
                EdgeRecord(
                    from_task_id="influx-ingest-cutover",
                    to_task_id="influx-backfill",
                    type="blocks",
                    direction="outgoing",
                ),
            ),
            "influx-backfill": (
                EdgeRecord(
                    from_task_id="influx-ingest-cutover",
                    to_task_id="influx-backfill",
                    type="blocks",
                    direction="incoming",
                ),
            ),
        },
        findings={
            "influx-ingest-cutover": (
                FindingRecord(
                    id="finding-plan",
                    task_id="influx-ingest-cutover",
                    agent="worker-a",
                    summary="Dual-write path validated against the archive.",
                    knowledge_id="note-influx-plan",
                    created_at="2026-08-06T11:30:00+00:00",
                ),
                FindingRecord(
                    id="finding-orphan",
                    task_id="influx-ingest-cutover",
                    agent="worker-b",
                    summary="Read swap needs a rollback gate.",
                    knowledge_id="missing-note",
                    created_at="2026-08-06T12:15:00+00:00",
                ),
            ),
        },
        claims={
            "influx-ingest-cutover": (
                ClaimRecord(
                    agent="worker-a",
                    aspect="implementation",
                    expires_at="2026-08-08T18:00:00+00:00",
                ),
            ),
        },
        agents=(
            AgentRecord(id="planner", name="Planner"),
            AgentRecord(id="worker-a", name="Worker A"),
            AgentRecord(id="worker-b", name="Worker B"),
        ),
        stats={"open_claims": 1, "agents": 3},
    )
