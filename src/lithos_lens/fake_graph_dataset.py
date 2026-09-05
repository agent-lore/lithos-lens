"""The demo dataset's dependency-graph half: one board with every branch on it.

:func:`~lithos_lens.fake_dataset.demo_dataset` grew a second cluster when the
task graph pages (T2) arrived, and it lives here for two reasons. The
mechanical one is the 800-line module ceiling ``docs/architecture.toml``
enforces. The real one is that this cluster exists for a different reason
than the influx one: the influx tasks demonstrate the DASHBOARD, and these
demonstrate the GRAPH — a dependency cycle, a cross-project ``blocks`` edge,
a cancelled predecessor, a resolved predecessor inside and outside the
resolved window, an epic with a child in another project, isolated tasks,
and a chain of depth 5, so the e2e visual pipeline sees every branch of the
graph assembly on one board.

Every edge is stated ONCE, as a ``(from, to, type)`` triple, and
:func:`edge_index` mirrors it onto both endpoints the way
``lithos_task_edge_list`` reports it (``direction`` relative to whichever
task was asked). Nineteen edges hand-written from both ends is thirty-eight
literals with one truth between them; the fixture that disagrees with itself
would be a fake that no client could ever encounter.

The timestamps take the demo's process anchor as an argument rather than
reading a clock of their own: ``demo_dataset()`` must stay deterministic
within a process (a test compares two builds), and the two halves must age
together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from lithos_lens.task_graph import BlockerRecord, EdgeRecord
from lithos_lens.tasks import TaskRecord

__all__ = ["GraphFixtures", "edge_index", "graph_fixtures"]

LOOM_TAGS = ("project:lithos-loom",)


@dataclass(frozen=True)
class GraphFixtures:
    """The graph cluster, in the shape ``FakeLithosDataset`` merges."""

    tasks: tuple[TaskRecord, ...] = ()
    edges: dict[str, tuple[EdgeRecord, ...]] = field(default_factory=dict)
    children: dict[str, tuple[str, ...]] = field(default_factory=dict)
    ready_ids: frozenset[str] = frozenset()
    blocked: dict[str, tuple[BlockerRecord, ...]] = field(default_factory=dict)


def graph_fixtures(anchor: datetime) -> GraphFixtures:
    """Build the graph cluster relative to the demo's process anchor."""

    def ago(**delta: float) -> str:
        return (anchor - timedelta(**delta)).replace(microsecond=0).isoformat()

    tasks: tuple[TaskRecord, ...] = (
        # The scope anchor: an OPEN epic, so it also joins the dashboard's
        # rollup strip, and its subtree deliberately crosses a project
        # boundary (lens-graph-page) — the multi-project epic case §5B.8
        # requires and the coverage-set cycle read (D4) has to span.
        TaskRecord(
            id="loom-epic",
            title="Loom run harness",
            description="Umbrella epic for the loom develop-cycle harness.",
            status="open",
            task_type="epic",
            created_by="planner",
            created_at=ago(days=6),
            tags=LOOM_TAGS,
        ),
        # The depth-5 blocking chain: schema -> transport -> worker -> ship ->
        # announce. The longest-chain line (D7) is arithmetic over exactly
        # this, so the demo has a chain with a KNOWN answer to render.
        TaskRecord(
            id="loom-schema",
            title="Design the run-record schema",
            status="open",
            created_by="planner",
            # Younger than unclaimed_ready_age_minutes: the head of the chain
            # stays in Ready instead of being promoted into Needs attention.
            created_at=ago(minutes=25),
            tags=LOOM_TAGS,
        ),
        TaskRecord(
            id="loom-transport",
            title="Implement the run transport",
            status="open",
            created_by="planner",
            created_at=ago(days=2),
            tags=LOOM_TAGS,
        ),
        TaskRecord(
            id="loom-worker",
            title="Wire the worker loop",
            status="open",
            created_by="planner",
            created_at=ago(days=2, hours=1),
            tags=LOOM_TAGS,
        ),
        TaskRecord(
            id="loom-ship",
            title="Ship the harness",
            status="open",
            created_by="planner",
            created_at=ago(days=1),
            tags=LOOM_TAGS,
        ),
        TaskRecord(
            id="loom-announce",
            title="Announce the harness",
            status="open",
            created_by="planner",
            created_at=ago(hours=20),
            tags=LOOM_TAGS,
        ),
        # The cross-project dependency, from both sides: a loom task blocks a
        # lens task, so each project's graph renders the other endpoint as a
        # one-hop ghost carrying its own project chip (D5).
        TaskRecord(
            id="lens-graph-page",
            title="Render the task graph page",
            status="open",
            created_by="planner",
            created_at=ago(hours=18),
            tags=("project:lithos-lens", "milestone:t2"),
        ),
        # The two-node cycle. Lithos is the authority on cycle MEMBERSHIP
        # (D4), so both rows also carry a kind="cycle" blocker below.
        TaskRecord(
            id="loom-cycle-a",
            title="Reconcile the run ledger",
            status="open",
            created_by="worker-b",
            created_at=ago(days=3),
            tags=LOOM_TAGS,
        ),
        TaskRecord(
            id="loom-cycle-b",
            title="Rebuild the ledger index",
            status="open",
            created_by="worker-b",
            created_at=ago(days=3, hours=1),
            tags=LOOM_TAGS,
        ),
        # The cancelled predecessor and what it strands. A cancelled blocker
        # can never complete, so this edge stays ACTIVE forever — the case a
        # graph that hid cancelled endpoints would contradict the dashboard on.
        TaskRecord(
            id="loom-cancelled-pred",
            title="Port the legacy run bridge",
            status="cancelled",
            outcome="Superseded by the new transport.",
            created_by="worker-b",
            created_at=ago(days=5),
            # Static and inside the browsing suites' since=2026-08-01 window,
            # for the same reason the influx terminal rows are.
            resolved_at="2026-08-25T12:00:00+00:00",
            tags=LOOM_TAGS,
        ),
        TaskRecord(
            id="loom-blocked-forever",
            title="Migrate the legacy run archive",
            status="open",
            created_by="worker-b",
            created_at=ago(days=1, hours=2),
            tags=LOOM_TAGS,
        ),
        # Two completed predecessors, one INSIDE the resolved window and one
        # outside it. Both their edges are `satisfied`; the difference is
        # whether `include_resolved=1` can bring the endpoint back at all.
        TaskRecord(
            id="loom-design-done",
            title="Agree the run-record format",
            status="completed",
            outcome="Format agreed and documented.",
            created_by="planner",
            created_at="2026-08-14T09:00:00+00:00",
            resolved_at="2026-08-20T10:00:00+00:00",
            tags=LOOM_TAGS,
        ),
        TaskRecord(
            id="loom-research-old",
            title="Survey existing run harnesses",
            status="completed",
            outcome="Survey written up.",
            created_by="planner",
            created_at="2026-06-02T09:00:00+00:00",
            resolved_at="2026-06-10T09:00:00+00:00",
            tags=LOOM_TAGS,
        ),
        # The isolated pair: real open work with no dependency edge at all.
        # `loom-docs-tidy` hangs off the epic and `loom-metrics-note` off its
        # provenance source — neither is a dependency, so both fold into the
        # "N isolated tasks" disclosure (D8) rather than crowding the graph.
        TaskRecord(
            id="loom-docs-tidy",
            title="Tidy the harness docs",
            status="open",
            created_by="worker-a",
            created_at=ago(minutes=20),
            tags=LOOM_TAGS,
        ),
        TaskRecord(
            id="loom-metrics-note",
            title="Note the harness metrics gaps",
            status="open",
            created_by="worker-a",
            created_at=ago(minutes=12),
            tags=LOOM_TAGS,
        ),
    )

    edges = edge_index(
        (
            # The depth-5 chain.
            ("loom-schema", "loom-transport", "blocks"),
            ("loom-transport", "loom-worker", "blocks"),
            ("loom-worker", "loom-ship", "blocks"),
            ("loom-ship", "loom-announce", "blocks"),
            # Cross-project: loom -> lens.
            ("loom-ship", "lens-graph-page", "blocks"),
            # The cycle.
            ("loom-cycle-a", "loom-cycle-b", "blocks"),
            ("loom-cycle-b", "loom-cycle-a", "blocks"),
            # The cancelled predecessor.
            ("loom-cancelled-pred", "loom-blocked-forever", "blocks"),
            # The two satisfied edges (completed predecessors).
            ("loom-design-done", "loom-transport", "blocks"),
            ("loom-research-old", "loom-schema", "blocks"),
            # Hierarchy, including the out-of-project child.
            ("loom-epic", "loom-schema", "parent_child"),
            ("loom-epic", "loom-transport", "parent_child"),
            ("loom-epic", "loom-worker", "parent_child"),
            ("loom-epic", "loom-ship", "parent_child"),
            ("loom-epic", "loom-announce", "parent_child"),
            ("loom-epic", "loom-docs-tidy", "parent_child"),
            ("loom-epic", "lens-graph-page", "parent_child"),
            # Provenance from a source resolved OUTSIDE the window: on an
            # open-only scope it is the context-ghost fixture (D6).
            ("loom-research-old", "loom-metrics-note", "discovered_from"),
        )
    )

    return GraphFixtures(
        tasks=tasks,
        edges=edges,
        children={
            "loom-epic": (
                "loom-schema",
                "loom-transport",
                "loom-worker",
                "loom-ship",
                "loom-announce",
                "loom-docs-tidy",
                "lens-graph-page",
            )
        },
        # The frontier oracle for this cluster: the chain head and the two
        # isolates are ready, everything the edges above stop is blocked.
        # Every open task-typed row appears in exactly one of the two.
        ready_ids=frozenset({"loom-schema", "loom-docs-tidy", "loom-metrics-note"}),
        blocked={
            "loom-transport": (
                BlockerRecord(
                    kind="task",
                    task_id="loom-schema",
                    type="blocks",
                    status="open",
                    message="Waiting on the run-record schema.",
                ),
            ),
            "loom-worker": (
                BlockerRecord(
                    kind="task",
                    task_id="loom-transport",
                    type="blocks",
                    status="open",
                    message="Waiting on the run transport.",
                ),
            ),
            "loom-ship": (
                BlockerRecord(
                    kind="task",
                    task_id="loom-worker",
                    type="blocks",
                    status="open",
                    message="Waiting on the worker loop.",
                ),
            ),
            "loom-announce": (
                BlockerRecord(
                    kind="task",
                    task_id="loom-ship",
                    type="blocks",
                    status="open",
                    message="Waiting on the harness to ship.",
                ),
            ),
            "lens-graph-page": (
                BlockerRecord(
                    kind="task",
                    task_id="loom-ship",
                    type="blocks",
                    status="open",
                    message="Waiting on the harness to ship.",
                ),
            ),
            "loom-cycle-a": (
                BlockerRecord(
                    kind="cycle",
                    task_id="loom-cycle-b",
                    type="blocks",
                    status="open",
                    message="Dependency cycle: loom-cycle-a -> loom-cycle-b -> "
                    "loom-cycle-a.",
                ),
            ),
            "loom-cycle-b": (
                BlockerRecord(
                    kind="cycle",
                    task_id="loom-cycle-a",
                    type="blocks",
                    status="open",
                    message="Dependency cycle: loom-cycle-b -> loom-cycle-a -> "
                    "loom-cycle-b.",
                ),
            ),
            "loom-blocked-forever": (
                BlockerRecord(
                    kind="blocker_unsatisfiable",
                    task_id="loom-cancelled-pred",
                    type="blocks",
                    status="cancelled",
                    message="Blocker 'Port the legacy run bridge' was cancelled; "
                    "this task can never become ready.",
                ),
            ),
        },
    )


def edge_index(
    triples: tuple[tuple[str, str, str], ...],
) -> dict[str, tuple[EdgeRecord, ...]]:
    """Mirror ``(from, to, type)`` onto both endpoints, as Lithos reports them.

    ``lithos_task_edge_list`` answers for ONE task and stamps ``direction``
    relative to it, so the same edge appears in two lists with two different
    directions — which is exactly what the scope assembly dedupes. Deriving
    both sides from one triple is what keeps the two copies agreeing.
    """
    index: dict[str, list[EdgeRecord]] = {}
    for from_id, to_id, edge_type in triples:
        index.setdefault(from_id, []).append(
            EdgeRecord(
                from_task_id=from_id,
                to_task_id=to_id,
                type=edge_type,
                direction="outgoing",
            )
        )
        index.setdefault(to_id, []).append(
            EdgeRecord(
                from_task_id=from_id,
                to_task_id=to_id,
                type=edge_type,
                direction="incoming",
            )
        )
    return {task_id: tuple(edges) for task_id, edges in index.items()}
