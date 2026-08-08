"""In-memory fake Lithos client for the e2e / demo "fake-Lithos app mode".

The real :class:`~lithos_lens.lithos_client.LithosClient` needs a live Lithos
MCP-over-SSE server to answer any tool call. That makes the shipped UI awkward
to drive from an end-to-end browser suite (or to demo offline): there is no
Lithos to point it at.

:class:`FakeLithosClient` satisfies the same
:class:`~lithos_lens.lithos_client.LithosClientProtocol` with a fixed,
deterministic set of in-memory fixtures — enough tasks, claims, findings,
notes, and graph edges for every server-rendered surface to light up. When the
``LITHOS_LENS_FAKE_LITHOS`` environment variable is truthy the application
factory (:func:`lithos_lens.web.create_app`) wires this client in instead of the
real one, so ``uv run lithos-lens`` boots a fully browsable app with no Lithos
behind it. The Playwright smoke suite under ``e2e/`` drives exactly that mode.

The fixtures deliberately live next to the shipped code (not under ``tests/``)
because this is a real, launchable application mode, not a test double that only
the unit suite can see.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

from lithos_lens.config import LithosConfig
from lithos_lens.lithos_client import LithosHealth, LithosToolError
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord, EdgeRecord
from lithos_lens.tasks import (
    AgentRecord,
    ClaimRecord,
    FindingRecord,
    NoteRecord,
    TaskRecord,
    TaskStatusRecord,
)

__all__ = ["FakeLithosClient", "fake_lithos_enabled"]

_TRUTHY = {"1", "true", "yes", "on"}


def fake_lithos_enabled() -> bool:
    """Return whether the fake-Lithos app mode is switched on via the environment.

    Reads ``LITHOS_LENS_FAKE_LITHOS``. Any of ``1/true/yes/on`` (case- and
    whitespace-insensitive) enables it; anything else — including unset — leaves
    the app on the real :class:`~lithos_lens.lithos_client.LithosClient`.
    """
    return os.environ.get("LITHOS_LENS_FAKE_LITHOS", "").strip().lower() in _TRUTHY


class FakeLithosClient:
    """A protocol-complete Lithos client backed by static in-memory fixtures.

    Every method is a pure lookup over the fixture set below; nothing touches
    the network. ``health()`` always reports ``"ok"`` so the whole dashboard
    renders. The constructor accepts a :class:`LithosConfig` purely so it is
    drop-in swappable for :class:`LithosClient` in the app factory — the config
    is otherwise unused.
    """

    def __init__(self, config: LithosConfig | None = None) -> None:
        self._config = config
        self.closed = False

        # Findings on the flagship claimed task link to these notes so the
        # knowledge surface (/note/<id>) renders in fake mode too.
        self.notes: dict[str, NoteRecord] = {
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

        self.tasks: list[TaskRecord] = [
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
                created_at="2026-07-28T09:00:00+00:00",
                resolved_at="2026-08-02T17:00:00+00:00",
                tags=("project:lithos-lens", "milestone:k1"),
            ),
            TaskRecord(
                id="influx-spike",
                title="Spike Influx client options",
                status="cancelled",
                outcome="Superseded by the cutover plan.",
                created_by="worker-b",
                created_at="2026-07-20T09:00:00+00:00",
                resolved_at="2026-07-30T12:00:00+00:00",
                tags=("project:influx",),
            ),
        ]

        # Graph oracle: Lens never re-derives readiness, so the fixtures are the
        # source of truth. `influx-ingest-cutover` is the ready, claimed, in-flight
        # task; `influx-backfill` is blocked on it.
        self.ready_ids: set[str] = {"influx-dashboards", "lens-graph-view"}
        self.blocked: dict[str, tuple[BlockerRecord, ...]] = {
            "influx-backfill": (
                BlockerRecord(
                    kind="task",
                    task_id="influx-ingest-cutover",
                    type="blocks",
                    status="open",
                    message="Waiting on the ingest cutover to land.",
                ),
            ),
        }
        self.edges: dict[str, list[EdgeRecord]] = {
            "influx-ingest-cutover": [
                EdgeRecord(
                    from_task_id="influx-ingest-cutover",
                    to_task_id="influx-backfill",
                    type="blocks",
                    direction="outgoing",
                ),
            ],
            "influx-backfill": [
                EdgeRecord(
                    from_task_id="influx-ingest-cutover",
                    to_task_id="influx-backfill",
                    type="blocks",
                    direction="incoming",
                ),
            ],
        }
        self.children: dict[str, list[str]] = {}

    # ── lifecycle ──────────────────────────────────────────────────────

    async def startup(self) -> None:
        return None

    async def health(self) -> LithosHealth:
        return "ok"

    async def register_agent(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True

    # ── reads ──────────────────────────────────────────────────────────

    async def list_tasks(
        self,
        *,
        agent: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
        with_claims: bool = False,
    ) -> list[TaskRecord]:
        rows = [task for task in self.tasks if status is None or task.status == status]
        if agent:
            rows = [task for task in rows if task.created_by == agent]
        if tags:
            rows = [task for task in rows if all(tag in task.tags for tag in tags)]
        if since:
            rows = [task for task in rows if task.created_at[:10] >= since[:10]]
        if with_claims:
            rows = [replace(task, claims=self._claims_for(task.id)) for task in rows]
        return rows

    async def task_ready(
        self,
        *,
        limit: int | None = None,
        with_claims: bool = False,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[TaskRecord]:
        rows = [
            task
            for task in self.tasks
            if task.id in self.ready_ids and task.status == "open"
        ]
        if with_claims:
            rows = [replace(task, claims=self._claims_for(task.id)) for task in rows]
        return rows[:limit] if limit is not None else rows

    async def task_blocked(
        self,
        *,
        limit: int | None = None,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[BlockedTaskRecord]:
        rows = [
            BlockedTaskRecord(task=task, blockers=self.blocked[task.id])
            for task in self.tasks
            if task.id in self.blocked and task.status == "open"
        ]
        return rows[:limit] if limit is not None else rows

    async def task_get(self, task_id: str) -> TaskRecord:
        task = self._by_id(task_id)
        if task is None:
            # Mirror the concrete client: a missing task is an error envelope
            # (code=task_not_found), surfaced as a coded LithosToolError.
            raise LithosToolError(f"Task '{task_id}' not found.", code="task_not_found")
        return task

    async def task_children(
        self,
        task_id: str,
        *,
        recursive: bool = False,
        include_closed: bool = False,
    ) -> list[TaskRecord]:
        child_ids = list(self.children.get(task_id, []))
        if recursive:
            queue = list(child_ids)
            while queue:
                for cid in self.children.get(queue.pop(), []):
                    if cid not in child_ids:
                        child_ids.append(cid)
                        queue.append(cid)
        rows = [task for cid in child_ids if (task := self._by_id(cid)) is not None]
        if not include_closed:
            rows = [task for task in rows if task.status == "open"]
        return rows

    async def task_edge_list(
        self,
        task_id: str,
        *,
        direction: str = "both",
        types: list[str] | None = None,
    ) -> list[EdgeRecord]:
        rows = list(self.edges.get(task_id, []))
        if direction != "both":
            rows = [edge for edge in rows if edge.direction == direction]
        if types:
            rows = [edge for edge in rows if edge.type in types]
        return rows

    async def task_status(self, task_id: str) -> TaskStatusRecord | None:
        task = self._by_id(task_id)
        if task is None:
            return None
        return TaskStatusRecord(
            id=task.id,
            title=task.title,
            status=task.status,
            claims=self._claims_for(task_id),
        )

    async def list_findings(
        self, task_id: str, *, since: str | None = None
    ) -> list[FindingRecord]:
        if task_id != "influx-ingest-cutover":
            return []
        return [
            FindingRecord(
                id="finding-plan",
                task_id=task_id,
                agent="worker-a",
                summary="Dual-write path validated against the archive.",
                knowledge_id="note-influx-plan",
                created_at="2026-08-06T11:30:00+00:00",
            ),
            FindingRecord(
                id="finding-orphan",
                task_id=task_id,
                agent="worker-b",
                summary="Read swap needs a rollback gate.",
                knowledge_id="missing-note",
                created_at="2026-08-06T12:15:00+00:00",
            ),
        ]

    async def stats(self) -> dict[str, Any]:
        return {"open_claims": 1, "agents": 3}

    async def list_agents(self) -> list[AgentRecord]:
        return [
            AgentRecord(id="planner", name="Planner"),
            AgentRecord(id="worker-a", name="Worker A"),
            AgentRecord(id="worker-b", name="Worker B"),
        ]

    async def read_note(self, knowledge_id: str) -> NoteRecord | None:
        if knowledge_id not in self.notes:
            raise LithosToolError(
                f"Knowledge '{knowledge_id}' not found.", code="knowledge_not_found"
            )
        return self.notes[knowledge_id]

    # ── helpers ────────────────────────────────────────────────────────

    def _by_id(self, task_id: str) -> TaskRecord | None:
        return next((task for task in self.tasks if task.id == task_id), None)

    def _claims_for(self, task_id: str) -> tuple[ClaimRecord, ...]:
        if task_id == "influx-ingest-cutover":
            return (
                ClaimRecord(
                    agent="worker-a",
                    aspect="implementation",
                    expires_at="2026-08-08T18:00:00+00:00",
                ),
            )
        return ()
