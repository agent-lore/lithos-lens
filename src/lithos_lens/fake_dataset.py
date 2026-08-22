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
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
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


# One clock read per process: every relative fixture timestamp is an offset
# from this anchor, so ``demo_dataset()`` stays deterministic (repeated builds
# are identical, and a test can compare two of them) while still being
# "recent" relative to whenever the demo is actually run.
_ANCHOR = datetime.now(UTC)


def _ago(**delta: float) -> str:
    """An ISO timestamp that far before the process anchor.

    The Needs-attention rules (T1-S3) compare a row's ``created_at`` against
    the real clock, so the demo's *open* rows are relative: a fixed date would
    age past ``stale_open_age_days`` and drain every workable section into
    Needs attention, leaving fake mode showing a permanently unhealthy board.
    Terminal rows stay fixed — they are windowed by a ``since`` filter the
    browsing suites also state as a fixed date, so both sides of THAT
    comparison must be static.
    """
    return (_ANCHOR - timedelta(**delta)).isoformat()


def _ahead(**delta: float) -> str:
    return (_ANCHOR + timedelta(**delta)).isoformat()


@dataclass(frozen=True)
class FakeLithosDataset:
    """Immutable fixture bundle served by ``FakeLithosClient``.

    Every field defaults to empty, so a test can compose a minimal dataset
    from just the records its scenario needs. The graph fields are a fixture
    oracle, not derived state: Lens never re-derives readiness, so
    ``ready_ids`` / ``blocked`` are the source of truth for the frontier the
    client reports.

    Immutability is enforced at the container level: the dataclass is frozen
    and every mapping field is defensively copied into a read-only view at
    construction, so neither a caller-held source dict nor a lookup result can
    mutate the dataset afterwards. The one deliberate gap is the ``metadata``
    dict *inside* individual records (``TaskRecord`` et al.), which stays as
    those records define it.
    """

    tasks: tuple[TaskRecord, ...] = ()
    notes: Mapping[str, NoteRecord] = field(default_factory=dict)
    # Explicit path -> note-id mapping for path-addressed reads (the wiki
    # resolver's [[folder/note]] probe). Paths are DISTINCT from note ids on
    # purpose: a fake that equates path stem with id would make every
    # path->UUID workflow test tautological.
    note_paths: Mapping[str, str] = field(default_factory=dict)
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

    def __post_init__(self) -> None:
        # Freeze the mapping fields: dict(...) breaks aliasing with whatever
        # the caller passed in, MappingProxyType rejects later item writes.
        # object.__setattr__ is the sanctioned frozen-dataclass escape hatch
        # for __post_init__ normalization.
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, Mapping) and not isinstance(value, MappingProxyType):
                object.__setattr__(self, f.name, MappingProxyType(dict(value)))


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
                "[[runbooks/influx-rollback|Influx rollback route]] for "
                "the abort route.\n\n"
                "- Stage 1: dual-write\n"
                "- Stage 2: read swap\n"
                "- Stage 3: decommission\n"
            ),
            tags=("project:influx", "kind:plan"),
            # Representative §6.4 frontmatter so chips / lede / supersedes /
            # authorship all render in fake mode and the browser suites
            # actually exercise the K1-S3 surface. Scope is deliberately
            # non-shared: a "shared" scope renders no chip.
            metadata={
                "note_type": "summary",
                "status": "active",
                "confidence": 0.9,
                "access_scope": "task",
                "namespace": "plans",
                "supersedes": "note-influx-legacy-ingest",
                "summaries": {
                    "short": (
                        "Cut ingest over first, backfill after; "
                        "abort via the feature gate."
                    )
                },
                "author": "worker-a",
                "contributors": ["planner", "worker-b"],
                "created_at": "2026-08-01T09:00:00+00:00",
                "updated_at": "2026-08-06T10:00:00+00:00",
            },
        ),
        "note-influx-legacy-ingest": NoteRecord(
            id="note-influx-legacy-ingest",
            title="Legacy ingest approach",
            content=(
                "# Legacy ingest approach\n\n"
                "Kept for history; superseded by the migration plan.\n"
            ),
            tags=("project:influx", "kind:plan"),
            # The quarantined fixture: exercises the colour-coded status chip
            # (and its computed-style browser assertion + screenshot capture).
            metadata={
                "note_type": "hypothesis",
                "status": "quarantined",
                "confidence": 0.2,
                # Distinct updated_at stamps (oldest of the three) keep the
                # /knowledge recently-updated ordering non-trivial in fake
                # mode: newest-first differs from dict insertion order.
                "updated_at": "2026-07-20T09:00:00+00:00",
            },
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
            metadata={"updated_at": "2026-08-05T12:00:00+00:00"},
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
            created_at=_ago(hours=3),
            tags=("project:influx", "area:data"),
        ),
        TaskRecord(
            id="influx-backfill",
            title="Backfill historical Influx series",
            description="Replay the archive once the cutover is stable.",
            status="open",
            created_by="planner",
            created_at=_ago(hours=2),
            tags=("project:influx", "area:data"),
        ),
        TaskRecord(
            id="influx-dashboards",
            title="Rebuild Influx operator dashboards",
            status="open",
            created_by="planner",
            # Younger than unclaimed_ready_age_minutes, so the demo keeps a
            # populated Ready section rather than flagging its whole frontier.
            created_at=_ago(minutes=40),
            tags=("project:influx", "area:observability"),
        ),
        TaskRecord(
            id="lens-graph-view",
            title="Ship graph-native operator view",
            description="Rebuild the dashboard on the ready/blocked frontier.",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=("project:lithos-lens", "milestone:t1"),
        ),
        TaskRecord(
            id="influx-ingest-old",
            title="Retire legacy Influx ingest shim",
            status="open",
            created_by="planner",
            # Deliberately ancient: the demo's Needs-attention row (stale open
            # + ready-but-unclaimed), so that section has something to show.
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
        note_paths={
            "plans/influx-migration.md": "note-influx-plan",
            "plans/legacy-ingest.md": "note-influx-legacy-ingest",
            "runbooks/influx-rollback.md": "note-influx-rollback",
        },
        related_neighborhoods=related_neighborhoods,
        # Graph oracle: every open workable task is placed on exactly one
        # frontier so none falls into the Not-classified tail (a healthy corpus
        # this small can never be limit-truncated). `influx-ingest-cutover` is
        # the ready, claimed, in-flight task (the only one carrying claims), so
        # it must sit on the ready frontier too; `influx-dashboards` /
        # `lens-graph-view` / `influx-ingest-old` are ready and unclaimed;
        # `influx-backfill` is blocked on the cutover.
        ready_ids=frozenset(
            {
                "influx-ingest-cutover",
                "influx-dashboards",
                "lens-graph-view",
                "influx-ingest-old",
            }
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
                    # Findings track their task's (relative) creation so the
                    # detail timeline never predates the task it belongs to.
                    created_at=_ago(hours=2, minutes=30),
                ),
                FindingRecord(
                    id="finding-orphan",
                    task_id="influx-ingest-cutover",
                    agent="worker-b",
                    summary="Read swap needs a rollback gate.",
                    knowledge_id="missing-note",
                    created_at=_ago(hours=2),
                ),
            ),
        },
        claims={
            "influx-ingest-cutover": (
                ClaimRecord(
                    agent="worker-a",
                    aspect="implementation",
                    # Well outside claim_expiring_soon_minutes: the demo's
                    # In-progress row stays In progress.
                    expires_at=_ahead(hours=6),
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
