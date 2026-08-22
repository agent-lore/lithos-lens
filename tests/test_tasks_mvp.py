"""Milestone 1 Tasks MVP behavior tests."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lithos_lens import state
from lithos_lens.config import load_config
from lithos_lens.fake_lithos import FAKE_TOOL_NAMES
from lithos_lens.knowledge import RelatedNeighborhood, SearchResult
from lithos_lens.lithos_client import LithosHealth, LithosToolError
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord, EdgeRecord
from lithos_lens.tasks import (
    MAX_SINCE_LOOKBACK_DAYS,
    AgentRecord,
    ClaimRecord,
    FindingRecord,
    NoteRecord,
    NoteSummary,
    SectionRow,
    TaskRecord,
    TaskStatusRecord,
    default_since,
    normalize_since_input,
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
        # What a tools/list probe sees. Feature detection reads THIS, never
        # error text, so a test models a pre-0.4 server by removing the two
        # frontier tools from the set.
        self.tool_names: set[str] = set(FAKE_TOOL_NAMES)
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
        # Inline claims per task id, returned only when a read asks for them
        # (with_claims / task_status) — the same contract as the server. Tests
        # extend this map (e.g. a resolved task an agent still claims).
        self.claims: dict[str, tuple[ClaimRecord, ...]] = {
            "open-claimed": (
                ClaimRecord(
                    agent="worker-a",
                    aspect="implementation",
                    expires_at="2026-04-26T11:00:00+00:00",
                ),
            )
        }
        self.notes: dict[str, NoteRecord] = {
            "note-1": NoteRecord(
                id="note-1",
                title="Resolved Knowledge",
                content="# Resolved Knowledge\n\nBody.",
                tags=("project:influx",),
            )
        }
        # Findings per task id, driven per-test (a staged ``[Reopened]``
        # finding is how a reopen is observable at all — see T1-S10).
        self.findings: dict[str, list[FindingRecord]] = {
            "open-claimed": [
                FindingRecord(
                    id="finding-1",
                    task_id="open-claimed",
                    agent="worker-a",
                    summary="Important finding",
                    knowledge_id="note-1",
                    created_at="2026-04-26T10:30:00+00:00",
                ),
                FindingRecord(
                    id="finding-2",
                    task_id="open-claimed",
                    agent="worker-b",
                    summary="Fallback finding",
                    knowledge_id="missing-note",
                    created_at="2026-04-26T10:45:00+00:00",
                ),
            ]
        }
        # /knowledge hybrid-search results, driven per-test (K1-S6).
        self.search_results: list[SearchResult] = []
        self.search_calls: list[dict[str, Any]] = []
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
                resolved_at="2026-04-22T10:00:00+00:00",
            ),
            TaskRecord(
                id="done-old",
                title="Old completed task",
                status="completed",
                created_by="worker",
                created_at="2025-01-01T10:00:00+00:00",
                resolved_at="2025-01-02T10:00:00+00:00",
            ),
            TaskRecord(
                id="cancelled-recent",
                title="Recently cancelled task",
                status="cancelled",
                created_by="worker",
                created_at="2026-04-21T10:00:00+00:00",
                resolved_at="2026-04-23T10:00:00+00:00",
            ),
        ]

    async def startup(self) -> None:
        return None

    async def health(self) -> LithosHealth:
        return self.health_value

    async def register_agent(self) -> bool:
        self.register_calls += 1
        return True

    async def list_tool_names(self) -> set[str]:
        """The graph-capable tool surface, unless a test narrows it."""
        return set(self.tool_names)

    async def list_tasks(
        self,
        *,
        agent: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
        resolved_since: str | None = None,
        with_claims: bool = False,
    ) -> list[TaskRecord]:
        self.list_calls.append(
            {
                "agent": agent,
                "status": status,
                "tags": tags,
                "since": since,
                "resolved_since": resolved_since,
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
        if resolved_since:
            # Upstream windows terminal rows on resolved_at and drops
            # NULL-resolved rows; the fake mirrors both halves.
            rows = [
                task
                for task in rows
                if task.resolved_at and task.resolved_at[:10] >= resolved_since[:10]
            ]
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
        return self.claims.get(task_id, ())

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
        return list(self.findings.get(task_id, []))

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

    async def recent_notes(
        self,
        *,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[NoteSummary]:
        return []

    async def search_notes(
        self,
        query: str,
        *,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[SearchResult]:
        self.search_calls.append({"query": query, "tags": tags, "limit": limit})
        rows = self.search_results
        return rows[:limit] if limit is not None else rows

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


def test_filter_bar_actions_stay_together_in_one_grid_cell(
    lithos_lens_config_env: Path,
) -> None:
    """Regression (round-3 visual review): "Apply filters" and "Reset" are wrapped
    in a single ``.filter-actions`` child of the filter bar. As two independent
    grid items they were flowed by the same auto-fit column count as the fields,
    so adding the Project filter (7 items, 3 columns at ~768px) orphaned Reset
    onto a row of its own."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")
        css = client.get("/static/lens.css")

    assert response.status_code == 200
    bar = response.text.split('class="filter-bar"')[1].split("</form>")[0]
    # Both actions live in the one wrapper, and the wrapper is in the filter bar.
    actions = bar.split('class="filter-actions"')[1].split("</div>")[0]
    assert "Apply filters" in actions
    assert 'href="/tasks">Reset</a>' in actions
    # …and the container is laid out as one cell rather than falling back to
    # two stacked full-width blocks.
    assert ".filter-actions {" in css.text


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


def test_dashboard_accepts_uk_resolved_since_date(
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
    # The date scopes only the resolved (completed/cancelled) windows, and it
    # goes out as `resolved_since`; the master open call is unfiltered.
    open_call = next(call for call in fake.list_calls if call["status"] == "open")
    assert open_call["since"] is None
    assert open_call["resolved_since"] is None
    assert completed_call["resolved_since"] == "2026-04-01"


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
    # Detail links preserve the active filters but strip the retired
    # claimed_state param, same as tag links — a legacy bookmark must not
    # keep propagating it through navigation.
    assert (
        'href="/tasks/open-claimed?status=open&agent=planner&'
        'since=01%2F04%2F2026&tag=project%3Ainflux"'
    ) in text
    assert "claimed_state" not in text.split("data-task-row")[1].split("</article>")[0]


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


def test_legacy_claimed_state_bookmark_does_not_propagate_through_navigation(
    lithos_lens_config_env: Path,
) -> None:
    """A stale ``?claimed_state=`` bookmark degrades on the list page AND stops
    propagating: detail links from the list, tag links, and the detail page's
    back-link all emit URLs without the retired param."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        listing = client.get(
            "/tasks?status=open&claimed_state=known_claimed&since=2026-04-01"
        )
        detail = client.get(
            "/tasks/open-claimed?status=open&claimed_state=known_claimed"
            "&since=2026-04-01"
        )

    listing_text = unescape(listing.text)
    detail_text = unescape(detail.text)

    assert listing.status_code == 200 and detail.status_code == 200
    # No generated link on either page carries the retired param.
    assert 'href="/tasks/open-claimed?status=open&since=2026-04-01"' in listing_text
    assert "claimed_state" not in listing_text.split("<main")[1]
    assert "claimed_state" not in detail_text.split("<main")[1]
    # The detail back-link keeps the real filters.
    assert 'href="/tasks?status=open&since=2026-04-01"' in detail_text


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


def test_agent_filter_matches_a_task_the_agent_only_claims(
    lithos_lens_config_env: Path,
) -> None:
    """Story 22 acceptance: ``?agent=X`` matches a task X merely claims, not
    only the tasks it created ("everything agent-zero is involved in")."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?agent=worker-a&since=2026-04-01")

    assert response.status_code == 200
    # open-claimed was created by "planner" and is claimed by "worker-a".
    assert "Claimed open task" in response.text
    # …while the tasks worker-a neither created nor claims drop out.
    assert "Unclaimed open task" not in response.text
    assert "Recently completed task" not in response.text
    # The agent filter is applied by Lens, never pushed upstream (the upstream
    # argument is creator-only and would drop the claimed row).
    assert all(call["agent"] is None for call in fake.list_calls)


def test_agent_filter_matches_a_claimer_on_a_resolved_task(
    lithos_lens_config_env: Path,
) -> None:
    """Story 22 holds for resolved rows too: the completed/cancelled windows are
    fetched WITH claims, so a completed task someone else created stays visible
    to the agent that claimed it (without claims it would read as unknown, and
    the row would silently vanish from the filter)."""
    fake = TaskFakeLithosClient()
    fake.claims["done-recent"] = (
        ClaimRecord(agent="worker-a", aspect="review", expires_at=""),
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?agent=worker-a&since=2026-04-01")

    assert response.status_code == 200
    assert "Recently completed task" in response.text
    # Created by "worker", claimed by nobody: out of scope for worker-a.
    assert "Recently cancelled task" not in response.text
    closed_calls = [call for call in fake.list_calls if call["status"] != "open"]
    assert closed_calls
    assert all(call["with_claims"] is True for call in closed_calls)


def test_project_filter_matches_both_conventions(
    lithos_lens_config_env: Path,
) -> None:
    """Story 23: a project view shows tasks stamped with ``metadata.project``
    AND tasks carrying the ``project:<slug>`` tag — neither convention hides a
    task from its own project (§5B.1)."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        TaskRecord(
            id="stamped",
            title="Stamped by metadata",
            status="open",
            created_by="planner",
            created_at="2026-04-24T10:00:00+00:00",
            metadata={"project": "influx"},
        )
    )
    fake.ready_ids.add("stamped")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?project=influx&since=2026-04-01")

    assert response.status_code == 200
    assert "Stamped by metadata" in response.text
    # Tagged with project:influx.
    assert "Claimed open task" in response.text
    assert "Unclaimed open task" in response.text
    # No project at all: out of scope of the project view.
    assert "Old open task" not in response.text
    # Both conventions' slugs reach the filter datalist.
    assert '<datalist id="projects">' in response.text
    assert '<option value="influx">' in response.text


def test_project_filter_is_preserved_across_navigation(
    lithos_lens_config_env: Path,
) -> None:
    """``?project=`` is part of the live filter vocabulary, so generated tag and
    detail links carry it (unlike the retired ``claimed_state``)."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks?status=open&project=influx&claimed_state=any&since=2026-04-01"
        )

    text = unescape(response.text)

    assert response.status_code == 200
    assert (
        'href="/tasks/open-claimed?status=open&project=influx&since=2026-04-01"'
    ) in text
    assert "claimed_state" not in text.split("<main")[1]


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
    # With no agent filter active, nothing reads a resolved row's claims (they
    # render no chips), so those windows don't carry the cost of the join —
    # they DO request claims once ?agent= is set, see the claimer test above.
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


def test_dashboard_renders_claims_unknown_chip_when_claims_not_returned(
    lithos_lens_config_env: Path,
) -> None:
    """When the master open list comes back without inline claims (older
    lithos / claims stripped), rows must read "claims unknown" — not the
    confident "unclaimed" — while still sectioning by frontier membership."""

    class NoClaimsClient(TaskFakeLithosClient):
        async def list_tasks(self, **kwargs: Any) -> list[TaskRecord]:
            rows = await super().list_tasks(**kwargs)
            # Simulate a server that never inlines claims: None, not ().
            return [replace(task, claims=None) for task in rows]

    fake = NoClaimsClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    assert "claims unknown" in response.text
    assert 'class="claim-chip claim-chip-open"' not in response.text
    # The rows render in the dedicated degraded-data group, and the workable
    # counts exclude them: unknown-claims tasks are not "Ready" or "Blocked".
    assert 'data-task-group="claims_unknown"' in response.text
    text = response.text
    ready_card = text.split("#task-group-ready")[1].split("</a>")[0]
    blocked_card = text.split("#task-group-blocked")[1].split("</a>")[0]
    assert "<strong>0</strong>" in ready_card
    assert "<strong>0</strong>" in blocked_card


def test_terminal_sections_window_by_resolution_time_not_creation_time(
    lithos_lens_config_env: Path,
) -> None:
    """T1-S10 acceptance: a task created 60+ days ago but resolved inside the
    window is recent work and must render — the window is pushed upstream as
    lithos_task_list's native resolved_since, and the card/filter labels say
    "Resolved since" so label and filter agree."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        TaskRecord(
            id="old-created-recent-resolved",
            title="Ancient task resolved yesterday",
            status="completed",
            created_by="worker",
            created_at="2020-01-01T00:00:00+00:00",
            resolved_at="2026-08-08T00:00:00+00:00",
        )
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    assert "Ancient task resolved yesterday" in response.text
    # Resolved before the window: still excluded (created_at is irrelevant).
    assert "Old completed task" not in response.text
    assert "Resolved since" in response.text
    assert "Created since" not in response.text

    # The window is a resolved_since push, never a created-at `since`.
    terminal_calls = [
        call for call in fake.list_calls if call["status"] in {"completed", "cancelled"}
    ]
    assert terminal_calls
    assert all(call["resolved_since"] == "2026-04-01" for call in terminal_calls)
    assert all(call["since"] is None for call in terminal_calls)


def test_terminal_rows_sort_newest_resolved_first(
    lithos_lens_config_env: Path,
) -> None:
    """The Completed group is drawn from a resolved-time window, so it orders
    by resolution: the ancient-but-just-resolved task leads the group."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        TaskRecord(
            id="old-created-recent-resolved",
            title="Ancient task resolved yesterday",
            status="completed",
            created_by="worker",
            created_at="2020-01-01T00:00:00+00:00",
            resolved_at="2026-08-08T00:00:00+00:00",
        )
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?status=completed&since=2026-04-01")

    assert response.status_code == 200
    assert response.text.index("Ancient task resolved yesterday") < response.text.index(
        "Recently completed task"
    )


def test_dashboard_clamps_an_unbounded_since_to_the_max_window(
    lithos_lens_config_env: Path,
) -> None:
    """``lithos_task_list`` takes no row limit, so ``since`` is the ONLY bound
    on the completed/cancelled reads and terminal history only grows: a
    lookback past ``MAX_SINCE_LOOKBACK_DAYS`` must be clamped before it
    reaches the wire, not honored (security/f-003)."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=01/01/0001")

    assert response.status_code == 200
    expected = (
        (datetime.now(UTC) - timedelta(days=MAX_SINCE_LOOKBACK_DAYS)).date().isoformat()
    )
    terminal_calls = [
        call for call in fake.list_calls if call["status"] in {"completed", "cancelled"}
    ]
    assert terminal_calls
    assert all(call["resolved_since"] == expected for call in terminal_calls)
    # The clamp is what the operator sees, so the filter never lies about its
    # own window.
    assert f'data-native-date value="{expected}"' in response.text


def test_dashboard_honors_a_since_inside_the_max_window(
    lithos_lens_config_env: Path,
) -> None:
    """The clamp is a ceiling, not a fixed window: a lookback within it is
    passed through untouched."""
    fake = TaskFakeLithosClient()
    inside = (datetime.now(UTC) - timedelta(days=90)).date().isoformat()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(f"/tasks?since={inside}")

    assert response.status_code == 200
    completed_call = next(
        call for call in fake.list_calls if call["status"] == "completed"
    )
    assert completed_call["resolved_since"] == inside


def test_since_ceiling_holds_against_a_wider_configured_window() -> None:
    """The ceiling is ABSOLUTE: no configured window can raise it (config
    rejects a wider value, and the day-count→date conversion clamps anyway),
    so neither the requested nor the default path can widen the two unlimited
    terminal reads (correctness/f-001)."""
    ceiling = (
        (datetime.now(UTC) - timedelta(days=MAX_SINCE_LOOKBACK_DAYS)).date().isoformat()
    )
    wide = MAX_SINCE_LOOKBACK_DAYS + 10_000

    # An explicit request older than the ceiling, under a wider default…
    assert normalize_since_input("01/01/0001", default_days=wide) == ceiling
    # …and the default window itself, which the blank/unparseable paths use.
    assert normalize_since_input("", default_days=wide) == ceiling
    assert normalize_since_input("not-a-date", default_days=wide) == ceiling
    assert default_since(wide) == ceiling
    # A day count no date arithmetic could take is bounded, not a 500.
    assert default_since(10**9) == ceiling


def test_terminal_rows_show_the_resolution_timestamp_they_are_windowed_on(
    lithos_lens_config_env: Path,
) -> None:
    """A Completed/Cancelled row must show its RESOLUTION date: the section is
    windowed and sorted on it, so showing the creation date instead makes a
    long-lived task read as a filter bug and the order look unsorted
    (security/f-004). Open rows keep their creation date; each is labelled so
    the two dates are distinguishable."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        TaskRecord(
            id="old-created-recent-resolved",
            title="Ancient task resolved yesterday",
            status="completed",
            created_by="worker",
            created_at="2020-01-01T00:00:00+00:00",
            resolved_at="2026-08-08T00:00:00+00:00",
        )
    )
    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    # The resolved date, labelled — not the 2020 creation date.
    assert (
        '<time datetime="2026-08-08T00:00:00+00:00" data-timestamp-kind="resolved">'
        "resolved 2026-08-08 00:00</time>" in response.text
    )
    assert "2020-01-01" not in response.text
    # Open rows are unaffected — they are not windowed on resolution.
    assert (
        '<time datetime="2026-04-26T10:00:00+00:00" data-timestamp-kind="created">'
        "created 2026-04-26 10:00</time>" in response.text
    )


def test_terminal_row_timestamp_falls_back_when_resolved_at_is_absent() -> None:
    """Defensive fallback, mirroring ``frontier._rows_for``'s sort key: a
    resolved_since window drops NULL-resolved rows upstream, so this is only
    reachable from a server that ignored the filter — the row must still show a
    date, labelled for the date it actually is."""
    row = SectionRow(
        task=TaskRecord(
            id="cancelled-unstamped",
            title="Unstamped cancelled task",
            status="cancelled",
            created_at="2026-04-24T10:00:00+00:00",
        )
    )

    assert row.timestamp == "2026-04-24T10:00:00+00:00"
    assert row.timestamp_label == "created"


def test_task_detail_marks_a_task_reopened_from_its_reopen_finding(
    lithos_lens_config_env: Path,
) -> None:
    """T1-S10 acceptance: ``lithos_task_reopen`` CLEARS resolved_at/outcome, so
    its durable ``[Reopened]`` finding is the only surviving evidence of the
    reopen — it drives both the header marker and the timeline marker.

    Findings are unauthenticated free text (any client can post the prefix
    under any agent name), so the header ATTRIBUTES the report to its posting
    agent rather than asserting the reversal as a system fact, and the
    timeline decoration is emitted as real markup (not through the escaping
    channel) so its class actually applies.
    """
    fake = TaskFakeLithosClient()
    fake.findings["open-unclaimed"] = [
        FindingRecord(
            id="finding-reopen",
            task_id="open-unclaimed",
            agent="operator",
            summary="[Reopened] from completed by operator",
            created_at="2026-04-27T09:00:00+00:00",
        )
    ]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    assert "data-reopened-marker" in response.text
    # Attributed, not asserted (security/f-001).
    assert "reopen reported by operator" in response.text
    # Real markup, not escaped text (security/f-002): the class must render as
    # an attribute or the timeline decoration silently never applies.
    assert '<li class="finding-reopened" data-reopen-finding>' in response.text


def test_task_detail_without_a_reopen_finding_carries_no_marker(
    lithos_lens_config_env: Path,
) -> None:
    """Ordinary findings must not be mistaken for reopens — the marker claims a
    lifecycle reversal, so it fires only on the ``[Reopened]`` prefix."""
    fake = TaskFakeLithosClient()
    fake.findings["open-unclaimed"] = [
        FindingRecord(
            id="finding-mention",
            task_id="open-unclaimed",
            agent="worker-a",
            summary="Discussed whether [Reopened] tasks need a follow-up",
            created_at="2026-04-27T09:00:00+00:00",
        )
    ]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    assert "data-reopened-marker" not in response.text
    assert "data-reopen-finding" not in response.text


# --- T1 slice 12: empty/degraded states -------------------------------------


class NoFrontierClient(TaskFakeLithosClient):
    """A pre-0.4 Lithos: the task-graph frontier tools do not exist.

    Both halves of that server are modelled: the calls fail (MCP answers an
    unknown tool with an error result), and — the part detection actually
    reads — ``tools/list`` does not name them.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.frontier_calls = 0
        self.tool_names -= {"lithos_task_ready", "lithos_task_blocked"}

    async def task_ready(self, **kwargs: Any) -> list[TaskRecord]:
        self.frontier_calls += 1
        raise LithosToolError("Unknown tool: lithos_task_ready", code="tool_error")

    async def task_blocked(self, **kwargs: Any) -> list[BlockedTaskRecord]:
        self.frontier_calls += 1
        raise LithosToolError("Unknown tool: lithos_task_blocked", code="tool_error")


class FrontierOutageClient(TaskFakeLithosClient):
    """A 0.4+ Lithos whose frontier reads fail transiently.

    The opposite of ``NoFrontierClient`` in the one way that matters: the tools
    ARE advertised, so detection must call this an outage rather than version
    skew — while the board still renders flat, since half a frontier classifies
    nothing.
    """

    async def task_ready(self, **kwargs: Any) -> list[TaskRecord]:
        raise LithosToolError("connection reset", code="tool_error")

    async def task_blocked(self, **kwargs: Any) -> list[BlockedTaskRecord]:
        raise LithosToolError("connection reset", code="tool_error")


def test_outage_cards_describe_the_flat_board_they_sit_above(
    lithos_lens_config_env: Path,
) -> None:
    """Regression: the summary cards follow the RENDER, not `graph_available`.

    Keyed on the latter they showed Ready/Blocked zeros — three counts
    presented as facts about a frontier that never answered — above a flat Open
    list that had no card of its own.
    """
    fake = FrontierOutageClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    # Rendered flat, but as an outage: no version notice, and the read error
    # is on screen.
    assert "Graph features need Lithos 0.4 or newer" not in response.text
    assert "Could not load the ready frontier." in response.text
    # The cards match the board: the flat Open count, no frontier zeros.
    assert "Open tasks" in response.text
    assert ">Ready<" not in response.text
    assert ">Blocked<" not in response.text
    assert "task-group-open" in response.text


def test_summary_cards_keep_the_filters_their_counts_were_computed_from(
    lithos_lens_config_env: Path,
) -> None:
    """Regression: a card counts the FILTERED board, so its link must carry the
    same filters — otherwise one click swaps the dataset out from under the
    number the operator just read."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks?since=2026-04-01&tag=area:docs&agent=planner&project=influx"
        )

    assert response.status_code == 200
    text = unescape(response.text)
    for card in ("status=open", "status=completed", "status=cancelled"):
        hrefs = [
            href for href in re.findall(r'href="(/tasks\?[^"]*)"', text) if card in href
        ]
        assert hrefs, f"no card link for {card}"
        for href in hrefs:
            assert "tag=area%3Adocs" in href or "tag=area:docs" in href
            assert "agent=planner" in href
            assert "project=influx" in href
            assert "since=2026-04-01" in href


def test_a_board_of_only_rolled_up_rows_says_so_instead_of_claiming_health(
    lithos_lens_config_env: Path,
) -> None:
    """Regression: epics are stripped from the graph sections (§5.3), so a
    tracker holding nothing but an open epic rendered an empty board under
    "All systems healthy" — an affirmative claim over work the operator could
    not see, and no explanation of where it went."""
    fake = TaskFakeLithosClient()
    fake.tasks = [
        TaskRecord(
            id="epic-1",
            title="Ship the thing",
            status="open",
            created_by="planner",
            created_at="2026-08-01T00:00:00+00:00",
            task_type="epic",
        )
    ]
    fake.ready_ids = set()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    assert 'data-empty-state="rolled-up"' in response.text
    assert "Nothing to work on here" in response.text
    assert "data-healthy-stripe" not in response.text
    # Not the empty-corpus panel: the open read DID return a row.
    assert 'data-empty-state="window"' not in response.text

    # …and it is scoped to a board that SHOWS the open side: asking for only
    # terminal rows empties those sections by choice, not by roll-up.
    with _client(lithos_lens_config_env, fake) as client:
        terminal_only = client.get("/tasks?status=completed&since=2026-04-01")

    assert 'data-empty-state="rolled-up"' not in terminal_only.text


def test_rolled_up_panel_replaces_the_open_side_not_the_whole_board(
    lithos_lens_config_env: Path,
) -> None:
    """Regression: the panel is an OPEN-side empty state.

    Rendered in place of the whole section loop it hid the terminal rows too —
    on the default board (all three statuses), a tracker whose only open row is
    an epic and which finished something last week showed the panel and NO
    completed section. Hiding real rows to explain an empty half is worse than
    the blank board this panel was added to fix.
    """
    fake = TaskFakeLithosClient()
    fake.tasks = [
        TaskRecord(
            id="epic-1",
            title="Ship the thing",
            status="open",
            created_by="planner",
            created_at="2026-08-01T00:00:00+00:00",
            task_type="epic",
        ),
        TaskRecord(
            id="done-1",
            title="Finished last week",
            status="completed",
            created_by="worker",
            created_at="2026-07-01T00:00:00+00:00",
            resolved_at="2026-08-08T00:00:00+00:00",
        ),
    ]
    fake.ready_ids = set()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    # The open side is explained…
    assert 'data-empty-state="rolled-up"' in response.text
    # …and the terminal side still renders, group and row.
    assert 'data-task-group="completed"' in response.text
    assert "Finished last week" in response.text
    # The empty workable groups stay out of the way: one explanation, not four.
    assert 'data-task-group="ready"' not in response.text


def test_dashboard_falls_back_to_flat_list_when_frontier_tools_are_missing(
    lithos_lens_config_env: Path,
) -> None:
    """Story 27: version skew degrades instead of breaking — the flat 0.1.0
    open list plus a notice naming the version that restores the graph."""
    fake = NoFrontierClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    assert "Graph features need Lithos 0.4 or newer" in response.text
    assert "data-graph-unavailable" in response.text
    # One flat Open section carrying every open row…
    assert 'data-task-group="open"' in response.text
    assert "Claimed open task" in response.text
    assert "Unclaimed open task" in response.text
    assert "Old open task" in response.text
    # …instead of the graph sections, which have nothing to say here.
    assert 'data-task-group="ready"' not in response.text
    assert 'data-task-group="blocked"' not in response.text
    assert 'data-task-group="in_progress"' not in response.text
    # The counters follow: no Ready/Blocked cards, one Open tasks count.
    assert "#task-group-ready" not in response.text
    assert "Open tasks" in response.text
    # The fallback is never silent: the same symptom is an outage or an
    # authorization filter, so the condition stays on the error channel for the
    # operator and for log-based monitoring (security f-001).
    assert "Some task data could not be loaded" in response.text
    assert "frontier tools are unavailable" in response.text
    assert "data-healthy-stripe" not in response.text
    # Terminal sections are unaffected (lithos_task_list predates the graph).
    assert "Recently completed task" in response.text


def test_flat_fallback_is_remembered_and_stops_probing_the_frontier(
    lithos_lens_config_env: Path,
) -> None:
    """Feature detection is a fact about the server build, so it is answered
    once per process: later renders skip both frontier calls rather than buying
    two guaranteed failures each."""
    fake = NoFrontierClient()

    with _client(lithos_lens_config_env, fake) as client:
        first = client.get("/tasks")
        calls_after_first = fake.frontier_calls
        second = client.get("/tasks")

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls_after_first > 0
    assert fake.frontier_calls == calls_after_first
    # The fallback still renders on the remembered answer…
    assert "Graph features need Lithos 0.4 or newer" in second.text
    assert 'data-task-group="open"' in second.text
    # …and still reports itself (security f-001): a degraded state that shows
    # its error only on the render that discovered it is invisible to most
    # refreshes inside the re-probe window.
    assert "frontier tools are unavailable" in first.text
    assert "frontier tools are unavailable" in second.text


def test_dashboard_renders_empty_state_when_lithos_has_no_tasks(
    lithos_lens_config_env: Path,
) -> None:
    """Empty corpus: every read succeeded and returned nothing. The board says
    so once, instead of repeating "nothing matched these filters" per section —
    a claim about filters that were never the reason."""
    fake = TaskFakeLithosClient()
    fake.tasks = []
    fake.ready_ids = set()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    assert 'data-empty-state="window"' in response.text
    assert "No tasks in this window" in response.text
    assert "match these filters" not in response.text
    # The live-update strip stays, so a task.created event still has somewhere
    # to land without a reload.
    assert 'data-task-list="pending"' in response.text


def test_empty_filter_result_keeps_the_per_section_message(
    lithos_lens_config_env: Path,
) -> None:
    """The mirror image: Lithos has tasks, the filter hides them all. That is a
    filter story, not an empty corpus, and must not read as "no tasks yet"."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?tag=project:nope&since=2026-04-01")

    assert response.status_code == 200
    assert 'data-empty-state="window"' not in response.text
    assert "No ready tasks match these filters" in response.text


def test_dashboard_renders_healthy_stripe_when_nothing_is_degraded(
    lithos_lens_config_env: Path,
) -> None:
    """The positive branch: every read landed, the frontier was complete and
    self-consistent, claims came back for every row."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    assert "data-healthy-stripe" in response.text
    assert "All systems healthy" in response.text
    assert "Some task data could not be loaded" not in response.text


def test_healthy_stripe_disappears_when_a_read_fails(
    lithos_lens_config_env: Path,
) -> None:
    """A degraded load must never also claim to be healthy."""

    class FailingStatsClient(TaskFakeLithosClient):
        async def stats(self) -> dict[str, Any]:
            raise RuntimeError("stats unavailable")

    fake = FailingStatsClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    assert "Some task data could not be loaded" in response.text
    assert "data-healthy-stripe" not in response.text


def test_dashboard_hides_the_board_when_lithos_is_unreachable(
    lithos_lens_config_env: Path,
) -> None:
    """Lithos unreachable: the banner and the service-status grid replace the
    board entirely — no sections, no empty state pretending the corpus is
    empty, and no counts Lens cannot stand behind."""
    fake = TaskFakeLithosClient(health="unreachable")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    assert "Lithos is offline or degraded" in response.text
    assert 'class="status-grid"' in response.text
    assert 'class="task-board"' not in response.text
    assert 'data-empty-state="window"' not in response.text
    assert "data-healthy-stripe" not in response.text


def test_missing_frontier_verdict_expires_and_is_re_probed(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (security f-001): the fallback verdict is remembered, never
    permanent. "The frontier tools are missing" is also what an outage or an
    authorization filter looks like, so once the re-probe window lapses Lens
    asks again and the graph sections come back with the frontier — no restart."""
    monkeypatch.setattr(state, "GRAPH_REPROBE_INTERVAL_S", 0.0)

    class RecoveringClient(NoFrontierClient):
        recovered = False

        async def task_ready(self, **kwargs: Any) -> list[TaskRecord]:
            if self.recovered:
                self.frontier_calls += 1
                return await TaskFakeLithosClient.task_ready(self, **kwargs)
            return await super().task_ready(**kwargs)

        async def task_blocked(self, **kwargs: Any) -> list[BlockedTaskRecord]:
            if self.recovered:
                self.frontier_calls += 1
                return await TaskFakeLithosClient.task_blocked(self, **kwargs)
            return await super().task_blocked(**kwargs)

        def upgrade(self) -> None:
            """The server gains the task graph (upgraded, or access restored)."""
            self.recovered = True
            self.tool_names = set(FAKE_TOOL_NAMES)

    fake = RecoveringClient()

    with _client(lithos_lens_config_env, fake) as client:
        degraded = client.get("/tasks?since=2026-04-01")
        calls_after_probe = fake.frontier_calls
        fake.upgrade()
        healed = client.get("/tasks?since=2026-04-01")

    assert "Graph features need Lithos 0.4 or newer" in degraded.text
    # The lapsed window let the next render probe again…
    assert fake.frontier_calls > calls_after_probe
    # …and the graph surface returns without a restart.
    assert "Graph features need Lithos 0.4 or newer" not in healed.text
    assert 'data-task-group="ready"' in healed.text
    assert 'data-task-group="open"' not in healed.text


@pytest.mark.parametrize(
    "query",
    ["tag=project:nope", "agent=nobody", "status=completed"],
)
def test_healthy_stripe_is_withheld_on_a_filtered_board(
    lithos_lens_config_env: Path, query: str
) -> None:
    """Regression (security f-002): the stripe makes a system-wide claim, but
    truncation, reconciliation and claims-unknown are measured over the rows
    the filters left. A shared link carrying ?tag=/?agent=/?status= must not be
    able to turn this degraded server into an affirmative "all healthy"."""

    class NoClaimsClient(TaskFakeLithosClient):
        async def list_tasks(self, **kwargs: Any) -> list[TaskRecord]:
            rows = await super().list_tasks(**kwargs)
            return [replace(task, claims=None) for task in rows]

    fake = NoClaimsClient()

    with _client(lithos_lens_config_env, fake) as client:
        unfiltered = client.get("/tasks?since=2026-04-01")
        filtered = client.get(f"/tasks?since=2026-04-01&{query}")

    # The degraded signal is real and visible on the whole board…
    assert "data-healthy-stripe" not in unfiltered.text
    assert 'data-task-group="claims_unknown"' in unfiltered.text
    # …and filtering it out of view does not make the system healthy.
    assert "data-healthy-stripe" not in filtered.text


def test_empty_state_is_withheld_when_a_filter_could_hide_terminal_rows(
    lithos_lens_config_env: Path,
) -> None:
    """Regression (correctness f-001): the completed/cancelled reads push
    agent/tags upstream, so with a terminal-only corpus a non-matching filter
    empties every response. That is a filter result — the board must say so
    instead of claiming Lithos has no tasks."""
    fake = TaskFakeLithosClient()
    fake.tasks = [task for task in fake.tasks if task.status == "completed"]
    fake.ready_ids = set()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?tag=project:nope&since=2026-04-01")

    assert response.status_code == 200
    assert 'data-empty-state="window"' not in response.text
    assert "No tasks in this window" not in response.text
    assert "No completed tasks match these filters" in response.text


def test_since_renders_in_one_format_across_the_page(
    lithos_lens_config_env: Path,
) -> None:
    """Regression: the terminal cards, the filter field and the empty-state
    panel all show the SAME `since` value, so they must all show it the same
    way — the cards used to print raw ISO a few hundred pixels above the
    DD/MM/YYYY input holding the identical date.

    The label is "Resolved since" from T1-S10 on (the window is a
    ``resolved_since`` push), which is a separate question from the format
    this pins.
    """
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    assert "Resolved since 01/04/2026" in response.text
    assert "Resolved since 2026-04-01" not in response.text
    # The links behind the cards keep the machine format the route parses
    # (unescaped like every other generated-URL assertion: the href carries
    # `&amp;` because it is now built in Python and autoescaped).
    assert "/tasks?status=completed&since=2026-04-01" in unescape(response.text)


def test_future_since_does_not_claim_the_tracker_is_empty(
    lithos_lens_config_env: Path,
) -> None:
    """Regression (security f-003): ``since`` is pushed into both terminal
    reads, so a date newer than every resolved task empties the board on a
    corpus that holds work. The panel may still render — nothing IS showing —
    but it must scope its claim to the window it was given rather than
    announcing an empty tracker, and it must say how to widen it."""
    fake = TaskFakeLithosClient()
    fake.tasks = [task for task in fake.tasks if task.status == "completed"]
    fake.ready_ids = set()

    with _client(lithos_lens_config_env, fake) as client:
        windowed = client.get("/tasks?since=2099-01-01")
        in_window = client.get("/tasks?since=2026-04-01")

    assert windowed.status_code == 200
    assert "No tasks yet" not in windowed.text
    assert "No tasks in this window" in windowed.text
    # The window that produced the emptiness is named, with the way out.
    assert "01/01/2099" in windowed.text
    assert 'Widen "Resolved since"' in windowed.text
    # Same server, a window that contains the work: the rows render.
    assert "Recently completed task" in in_window.text
    assert 'data-empty-state="window"' not in in_window.text
