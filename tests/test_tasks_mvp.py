"""Milestone 1 Tasks MVP behavior tests."""

from __future__ import annotations

from dataclasses import replace
from html import unescape
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.knowledge import RelatedNeighborhood
from lithos_lens.lithos_client import LithosHealth, LithosToolError
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord, EdgeRecord
from lithos_lens.tasks import (
    AgentRecord,
    ClaimRecord,
    FindingRecord,
    NoteRecord,
    NoteSummary,
    TaskRecord,
    TaskStatusRecord,
)
from lithos_lens.web import create_app


class TaskFakeLithosClient:
    def __init__(
        self,
        *,
        health: LithosHealth = "ok",
        visible_failures: bool = False,
        ignore_tags: bool = False,
    ):
        self.health_value: LithosHealth = health
        self.visible_failures = visible_failures
        self.ignore_tags = ignore_tags
        self.closed = False
        self.register_calls = 0
        self.status_calls: list[str] = []
        self.list_calls: list[dict[str, Any]] = []
        # Task-graph oracle state (lithos 0.4). Lens never re-derives readiness,
        # so the fake is the source of truth: ready_ids / blocked drive the
        # frontier, edges/children drive the detail surfaces. By default the two
        # unclaimed workable open tasks sit on the ready frontier (open-claimed
        # is claimed, so it classifies as In progress regardless); tests that
        # exercise blocking override these.
        self.ready_ids: set[str] = {"open-unclaimed", "open-old"}
        self.blocked: dict[str, tuple[BlockerRecord, ...]] = {}
        self.edges: dict[str, list[EdgeRecord]] = {}
        self.children: dict[str, list[str]] = {}
        self.get_calls: list[str] = []
        self.edge_list_calls: list[dict[str, Any]] = []
        self.notes: dict[str, NoteRecord] = {
            "note-1": NoteRecord(
                id="note-1",
                title="Resolved Knowledge",
                content="# Resolved Knowledge\n\nBody.",
                tags=("project:influx",),
            )
        }
        self.tasks = [
            TaskRecord(
                id="open-claimed",
                title="Claimed open task",
                description="Work in progress",
                status="open",
                created_by="planner",
                created_at="2026-04-26T10:00:00+00:00",
                tags=("project:influx", "area:docs"),
            ),
            TaskRecord(
                id="open-unclaimed",
                title="Unclaimed open task",
                status="open",
                created_by="planner",
                created_at="2026-04-25T10:00:00+00:00",
                tags=("project:influx",),
            ),
            TaskRecord(
                id="open-old",
                title="Old open task",
                status="open",
                created_by="planner",
                created_at="2025-01-01T10:00:00+00:00",
            ),
            TaskRecord(
                id="done-recent",
                title="Recently completed task",
                status="completed",
                created_by="worker",
                created_at="2026-04-20T10:00:00+00:00",
            ),
            TaskRecord(
                id="done-old",
                title="Old completed task",
                status="completed",
                created_by="worker",
                created_at="2025-01-01T10:00:00+00:00",
            ),
            TaskRecord(
                id="cancelled-recent",
                title="Recently cancelled task",
                status="cancelled",
                created_by="worker",
                created_at="2026-04-21T10:00:00+00:00",
            ),
        ]

    async def startup(self) -> None:
        return None

    async def health(self) -> LithosHealth:
        return self.health_value

    async def register_agent(self) -> bool:
        self.register_calls += 1
        return True

    async def list_tasks(
        self,
        *,
        agent: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
        with_claims: bool = False,
    ) -> list[TaskRecord]:
        self.list_calls.append(
            {
                "agent": agent,
                "status": status,
                "tags": tags,
                "since": since,
                "with_claims": with_claims,
            }
        )
        rows = [task for task in self.tasks if status is None or task.status == status]
        if agent:
            rows = [task for task in rows if task.created_by == agent]
        if tags and not self.ignore_tags:
            rows = [task for task in rows if all(tag in task.tags for tag in tags)]
        if since:
            rows = [task for task in rows if task.created_at[:10] >= since[:10]]
        if with_claims:
            rows = [replace(task, claims=self._claims_for(task.id)) for task in rows]
        return rows

    def _by_id(self, task_id: str) -> TaskRecord | None:
        return next((task for task in self.tasks if task.id == task_id), None)

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
        self.get_calls.append(task_id)
        task = self._by_id(task_id)
        if task is None:
            # Mirror the concrete client: Lithos answers a missing task with an
            # error envelope (code=task_not_found), which LithosClient raises as
            # a coded LithosToolError. Callers must be able to rely on the same
            # contract against the fake.
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
                grandchildren = self.children.get(queue.pop(), [])
                for cid in grandchildren:
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
        self.edge_list_calls.append(
            {"task_id": task_id, "direction": direction, "types": types}
        )
        rows = list(self.edges.get(task_id, []))
        if direction != "both":
            rows = [edge for edge in rows if edge.direction == direction]
        if types:
            rows = [edge for edge in rows if edge.type in types]
        return rows

    def _claims_for(self, task_id: str) -> tuple[ClaimRecord, ...]:
        if task_id == "open-claimed":
            return (
                ClaimRecord(
                    agent="worker-a",
                    aspect="implementation",
                    expires_at="2026-04-26T11:00:00+00:00",
                ),
            )
        return ()

    async def task_status(self, task_id: str) -> TaskStatusRecord | None:
        self.status_calls.append(task_id)
        if self.visible_failures and task_id == "open-claimed":
            raise RuntimeError("status failed")
        task = next((item for item in self.tasks if item.id == task_id), None)
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
        if task_id == "open-claimed":
            return [
                FindingRecord(
                    id="finding-1",
                    task_id=task_id,
                    agent="worker-a",
                    summary="Important finding",
                    knowledge_id="note-1",
                    created_at="2026-04-26T10:30:00+00:00",
                ),
                FindingRecord(
                    id="finding-2",
                    task_id=task_id,
                    agent="worker-b",
                    summary="Fallback finding",
                    knowledge_id="missing-note",
                    created_at="2026-04-26T10:45:00+00:00",
                ),
            ]
        return []

    async def stats(self) -> dict[str, Any]:
        return {"open_claims": 1, "agents": 2}

    async def list_agents(self) -> list[AgentRecord]:
        return [
            AgentRecord(id="planner", name="Planner"),
            AgentRecord(id="worker", name="Worker"),
        ]

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None:
        if knowledge_id not in self.notes:
            raise RuntimeError("missing note")
        return self.notes[knowledge_id]

    async def read_note_by_path(self, path: str) -> NoteRecord | None:
        return None

    async def related(self, knowledge_id: str) -> RelatedNeighborhood:
        return RelatedNeighborhood()

    async def list_notes(
        self,
        *,
        title_contains: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[NoteSummary]:
        return []

    async def close(self) -> None:
        self.closed = True


def _client(config_path: Path, fake: TaskFakeLithosClient) -> TestClient:
    config = load_config(config_path)
    app = create_app(config, lithos_client_factory=lambda _: fake)
    return TestClient(app)


def test_dashboard_shows_current_situation_and_default_groups(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    assert "In progress" in response.text
    assert "Ready" in response.text
    assert "Blocked" in response.text
    assert "Claimed open task" in response.text
    assert "Unclaimed open task" in response.text
    # Open tasks are the live frontier and are NOT windowed by `since` (it scopes
    # only the resolved completed/cancelled sections), so an old still-open task
    # stays visible.
    assert "Old open task" in response.text
    assert "Recently completed task" in response.text
    assert "Old completed task" not in response.text
    assert "Recently cancelled task" in response.text
    assert "implementation - worker-a" in response.text


def test_dashboard_renders_filter_bar_before_task_groups(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    assert 'class="filter-bar"' in response.text
    assert response.text.index('class="filter-bar"') < response.text.index(
        'class="task-board"'
    )


def test_dashboard_applies_tag_filter_after_lithos_returns_rows(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient(ignore_tags=True)

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks?status=completed&tag=project:influx&agent=worker&since=2026-04-01"
        )

    assert response.status_code == 200
    assert "Recently completed task" not in response.text
    assert "Old completed task" not in response.text
    assert "No completed tasks match these filters" in response.text


def test_dashboard_accepts_uk_created_since_date(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?status=completed&since=01/04/2026")

    assert response.status_code == 200
    assert 'value="01/04/2026"' in response.text
    assert 'data-native-date value="2026-04-01"' in response.text
    assert 'data-open-date-picker aria-label="Open calendar"' in response.text
    completed_call = next(
        call for call in fake.list_calls if call["status"] == "completed"
    )
    # `since` scopes only the resolved (completed/cancelled) windows; the master
    # open call is unfiltered so it never carries a `since`.
    open_call = next(call for call in fake.list_calls if call["status"] == "open")
    assert open_call["since"] is None
    assert completed_call["since"] == "2026-04-01"


def test_task_list_tag_links_replace_tag_and_preserve_active_filters(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks?status=open&claimed_state=any&agent=planner&since=01/04/2026&tag=project:influx"
        )

    text = unescape(response.text)

    assert response.status_code == 200
    # claimed_state was retired, so it is no longer preserved in tag links.
    assert (
        'href="/tasks?status=open&agent=planner&since=01%2F04%2F2026&tag=area%3Adocs"'
    ) in text
    assert 'class="tag-chip tag-chip-project"' in text
    assert (
        'href="/tasks/open-claimed?status=open&claimed_state=any&'
        'agent=planner&since=01/04/2026&tag=project:influx"'
    ) in text


def test_task_detail_tag_links_replace_tag_and_preserve_active_filters(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks/open-claimed?status=open&agent=planner&since=01/04/2026&tag=old"
        )

    text = unescape(response.text)

    assert response.status_code == 200
    assert (
        'href="/tasks?status=open&agent=planner&since=01%2F04%2F2026&'
        'tag=project%3Ainflux"'
    ) in text
    assert 'class="tag-chip tag-chip-project"' in text


def test_legacy_claimed_state_url_is_ignored(
    lithos_lens_config_env: Path,
) -> None:
    """A stale ``?claimed_state=`` bookmark must degrade gracefully (story 24):
    it is parsed away, never rejected, and does not filter the sections."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks?status=open&claimed_state=known_unclaimed&since=2026-04-01"
        )

    assert response.status_code == 200
    # The claimed-state filter is gone, so the claimed row is not filtered out.
    assert "Claimed open task" in response.text
    assert "Unclaimed open task" in response.text


def test_blocker_chip_resolves_predecessor_title_under_tag_filter(
    lithos_lens_config_env: Path,
) -> None:
    """Regression (f-001): with a project/tag filter active, a blocked row's
    chip must still show the *title* of a predecessor that lives in a different
    project (and is therefore filtered out of the visible sections). The master
    open list is fetched unfiltered so the join can resolve it."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        TaskRecord(
            id="blk",
            title="Blocked in project A",
            status="open",
            created_by="planner",
            created_at="2026-04-26T10:00:00+00:00",
            tags=("project:a",),
        )
    )
    fake.tasks.append(
        TaskRecord(
            id="pred",
            title="Predecessor in project B",
            status="open",
            created_by="planner",
            created_at="2026-04-26T09:00:00+00:00",
            tags=("project:b",),
        )
    )
    fake.ready_ids = {"pred"}
    fake.blocked = {
        "blk": (
            BlockerRecord(
                kind="task",
                task_id="pred",
                type="blocks",
                status="open",
                message="Waiting on predecessor pred to complete.",
            ),
        )
    }

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?status=open&tag=project:a&since=2026-04-01")

    assert response.status_code == 200
    text = response.text
    blocked_group = text[text.index('data-task-group="blocked"') :]
    blocked_group = blocked_group[: blocked_group.index("</article>")]
    assert "Blocked in project A" in blocked_group
    # The chip carries the predecessor's title, resolved from the unfiltered
    # snapshot, even though project:b is filtered out of the visible sections.
    assert "Predecessor in project B" in blocked_group
    # The predecessor is not itself rendered as a visible row.
    ready_group = text[text.index('data-task-group="ready"') :]
    ready_group = ready_group[: ready_group.index("</article>")]
    assert "Predecessor in project B" not in ready_group


def test_blocker_chip_resolves_older_predecessor_title_under_since_filter(
    lithos_lens_config_env: Path,
) -> None:
    """Regression (f-001): a still-open predecessor created *before* the `since`
    date must still name the blocker chip. Open tasks are not windowed by
    `since`, so the master open snapshot stays whole and the title resolves."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        TaskRecord(
            id="blk",
            title="Recent blocked work",
            status="open",
            created_by="planner",
            created_at="2026-04-26T10:00:00+00:00",
        )
    )
    fake.tasks.append(
        TaskRecord(
            id="pred",
            title="Ancient predecessor",
            status="open",
            created_by="planner",
            created_at="2025-01-01T10:00:00+00:00",
        )
    )
    fake.ready_ids = {"pred"}
    fake.blocked = {
        "blk": (
            BlockerRecord(
                kind="task",
                task_id="pred",
                type="blocks",
                status="open",
                message="Waiting on predecessor pred to complete.",
            ),
        )
    }

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?status=open&since=2026-04-01")

    assert response.status_code == 200
    text = response.text
    blocked_group = text[text.index('data-task-group="blocked"') :]
    blocked_group = blocked_group[: blocked_group.index("</article>")]
    assert "Recent blocked work" in blocked_group
    # The chip shows the predecessor's title, resolved from the whole open
    # snapshot, despite the predecessor predating the `since` window.
    assert "Ancient predecessor" in blocked_group


def test_blocked_task_shows_predecessor_chip_then_moves_to_ready(
    lithos_lens_config_env: Path,
) -> None:
    """Slice-2 acceptance: an open-predecessor task renders in Blocked with the
    predecessor's title chip; completing the predecessor (in the fake oracle)
    moves it onto the ready frontier and into the Ready section."""
    fake = TaskFakeLithosClient()
    # open-unclaimed is blocked by open-claimed (an open predecessor). Only the
    # predecessor stays ready until it completes.
    fake.ready_ids = {"open-old"}
    fake.blocked = {
        "open-unclaimed": (
            BlockerRecord(
                kind="task",
                task_id="open-claimed",
                type="blocks",
                status="open",
                message="Waiting on predecessor open-claimed to complete.",
            ),
        )
    }

    with _client(lithos_lens_config_env, fake) as client:
        blocked_view = client.get("/tasks?status=open&since=2026-04-01")

    assert blocked_view.status_code == 200
    board = blocked_view.text
    blocked_group = board[board.index('data-task-group="blocked"') :]
    blocked_group = blocked_group[: blocked_group.index("</article>")]
    assert "Unclaimed open task" in blocked_group
    # The chip carries the blocking predecessor's *title*, not its id.
    assert 'class="blocker-chip blocker-chip-task"' in blocked_group
    assert "Claimed open task" in blocked_group

    # Complete the predecessor: the blocked task joins the ready frontier.
    fake.blocked = {}
    fake.ready_ids = {"open-old", "open-unclaimed"}

    with _client(lithos_lens_config_env, fake) as client:
        ready_view = client.get("/tasks?status=open&since=2026-04-01")

    assert ready_view.status_code == 200
    text = ready_view.text
    ready_group = text[text.index('data-task-group="ready"') :]
    ready_group = ready_group[: ready_group.index("</article>")]
    assert "Unclaimed open task" in ready_group
    # It left Blocked entirely: that section is now empty.
    blocked_after = text[text.index('data-task-group="blocked"') :]
    blocked_after = blocked_after[: blocked_after.index("</article>")]
    assert "Unclaimed open task" not in blocked_after


def test_claimed_but_blocked_row_is_decorated_in_progress(
    lithos_lens_config_env: Path,
) -> None:
    """A claimed task that Lithos also reports blocked stays In progress but
    carries a ``blocked`` decoration (story 13)."""
    fake = TaskFakeLithosClient()
    fake.blocked = {
        "open-claimed": (
            BlockerRecord(
                kind="task",
                task_id="open-unclaimed",
                type="blocks",
                status="open",
                message="Waiting on predecessor open-unclaimed to complete.",
            ),
        )
    }

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?status=open&since=2026-04-01")

    assert response.status_code == 200
    text = response.text
    in_progress = text[text.index('data-task-group="in_progress"') :]
    in_progress = in_progress[: in_progress.index("</article>")]
    assert "Claimed open task" in in_progress
    assert "data-claimed-but-blocked" in in_progress


def test_direct_task_detail_resolves_findings_and_note_links(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-claimed")

    assert response.status_code == 200
    assert "Claimed open task" in response.text
    assert "Active Claims" in response.text
    assert "Important finding" in response.text
    assert "Resolved Knowledge" in response.text
    assert "Fallback finding" in response.text
    assert "View document" in response.text
    assert "Could not resolve document title" in response.text


def test_unknown_task_renders_not_found_panel(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/no-such-task")

    assert response.status_code == 200
    assert "Task not found in current Lithos task lists" in response.text


def test_note_renderer_loads_linked_knowledge(lithos_lens_config_env: Path) -> None:
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/note-1?task=open-claimed")

    assert response.status_code == 200
    assert "Resolved Knowledge" in response.text
    assert "project: influx" in response.text
    assert "Back to Claimed open task" in response.text


def test_dashboard_uses_inline_claims_and_skips_task_status_fan_out(
    lithos_lens_config_env: Path,
) -> None:
    """When lithos_task_list returns claims inline, no per-task lithos_task_status
    calls are made for visible open tasks, and the dashboard still classifies
    rows correctly as claimed/unclaimed."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    assert "Claimed open task" in response.text
    assert "Unclaimed open task" in response.text
    # Dashboard renders the claim chip from inline claims, no fan-out.
    assert fake.status_calls == []
    open_list_calls = [c for c in fake.list_calls if c["status"] == "open"]
    assert open_list_calls
    assert all(c["with_claims"] is True for c in open_list_calls)
    # Other status groups don't carry the cost of the join.
    other_calls = [
        c for c in fake.list_calls if c["status"] in {"completed", "cancelled"}
    ]
    assert other_calls
    assert all(c["with_claims"] is False for c in other_calls)


def test_fake_task_get_raises_coded_not_found_like_the_real_client() -> None:
    """The shared fake and the concrete client agree on the not-found contract:
    a coded LithosToolError, never None (the PRD requires callers to be able to
    distinguish task_not_found)."""
    import asyncio

    fake = TaskFakeLithosClient()

    with pytest.raises(LithosToolError) as excinfo:
        asyncio.run(fake.task_get("no-such-task"))
    assert excinfo.value.code == "task_not_found"

    found = asyncio.run(fake.task_get("open-claimed"))
    assert found.id == "open-claimed"
