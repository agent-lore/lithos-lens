"""Milestone 1 Tasks MVP behavior tests."""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlencode, urlsplit

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import QueryParams

from lithos_lens.config import load_config
from lithos_lens.epic_strip import EPIC_FANOUT_BATCH
from lithos_lens.knowledge import RelatedNeighborhood, SearchResult
from lithos_lens.lithos_client import LithosHealth, LithosToolError
from lithos_lens.logging import JsonFormatter
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord, EdgeRecord
from lithos_lens.tasks import (
    MAX_FILTER_QUERY_BYTES,
    MAX_FILTER_TAG_CHIPS,
    MAX_SINCE_LOOKBACK_DAYS,
    OPEN_SECTIONS,
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

# The Needs-attention rules (T1-S3) compare fixture timestamps against the real
# clock, so the OPEN fixtures below are anchored to "now" — a static open row
# would silently age into the stale-open rule and drift out of the section a
# test is asserting on. Terminal rows keep their fixed dates: the `since`
# window compares them against the suite's fixed 2026-04-01 filter, so both
# sides of THAT comparison must stay static.
# Whole seconds: these stamps reach rendered HTML (the detail page prints
# ``created_at`` verbatim) and the vendored contracts and demo fixtures all use
# second precision, so microseconds would make the fixtures the odd ones out.
_NOW = datetime.now(UTC).replace(microsecond=0)


def _ago(**delta: float) -> str:
    return (_NOW - timedelta(**delta)).isoformat()


def _ahead(**delta: float) -> str:
    return (_NOW + timedelta(**delta)).isoformat()


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
        # Inline claims per task id, returned only when a read asks for them
        # (with_claims / task_status) — the same contract as the server. Tests
        # extend this map (e.g. a resolved task an agent still claims).
        self.claims: dict[str, tuple[ClaimRecord, ...]] = {
            "open-claimed": (
                ClaimRecord(
                    agent="worker-a",
                    aspect="implementation",
                    # Relative, and comfortably outside
                    # claim_expiring_soon_minutes: a fixed stamp would drift
                    # into the past and silently promote this row into Needs
                    # attention (T1-S3 rule 4) in every test that uses it.
                    expires_at=_ahead(hours=6),
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
                created_at=_ago(hours=2),
                tags=("project:influx", "area:docs"),
            ),
            TaskRecord(
                id="open-unclaimed",
                title="Unclaimed open task",
                status="open",
                created_by="planner",
                # Younger than unclaimed_ready_age_minutes: on the ready
                # frontier and NOT flagged, so Ready-section assertions hold.
                created_at=_ago(minutes=20),
                tags=("project:influx",),
            ),
            TaskRecord(
                id="open-old",
                title="Old open task",
                status="open",
                created_by="planner",
                # Genuinely old on purpose: it is the fixture that fires the
                # stale-open rule and lands in Needs attention.
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


def test_task_detail_names_the_empty_tag_rather_than_drawing_a_blank_pill(
    lithos_lens_config_env: Path,
) -> None:
    """The detail page must honour the convention row.html and the active-filter
    chip already state: a task really can carry the empty tag, so a blank pill
    reads as a rendering bug rather than as a real scope.

    Pinned because the rule was written down in two of the three places that
    render a tag and omitted in the third, which is invisible on any task whose
    tags happen to be non-empty.
    """
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        TaskRecord(
            id="empty-tagged-detail",
            title="Empty tagged detail task",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=("", "project:influx"),
        )
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/empty-tagged-detail")

    text = unescape(response.text)

    assert response.status_code == 200
    assert "(empty tag)" in text
    # The non-empty tag alongside it still renders normally — the guard must not
    # relabel every chip.
    assert "project: influx" in text
    # And no chip is drawn with nothing in it.
    assert not re.search(r'class="tag-chip[^"]*" href="[^"]*"></a>', text)


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


def _datalist_options(html: str, list_id: str) -> list[str]:
    """The option VALUES of one datalist, HTML-DECODED, in rendered order.

    Decoded because a tag is an arbitrary string: ``R&D`` is markup-escaped on
    the way out, and comparing the raw attribute text would let an assertion
    pass on a value no browser would ever hand back.
    """
    block = re.search(rf'<datalist id="{list_id}">(.*?)</datalist>', html, re.S)
    assert block is not None, f"the {list_id} datalist is missing"
    return [
        unescape(value)
        for value in re.findall(r'<option value="([^"]*)">', block.group(1))
    ]


def test_tag_box_offers_the_whole_snapshot_vocabulary(
    lithos_lens_config_env: Path,
) -> None:
    """The Tag box is a discovery surface, like Project and Agent beside it: it
    offers every tag the load fetched — a cross-project tag, and one carried
    only by a row inside the resolved window — even while the active project
    filter hides the rows that carry them."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        TaskRecord(
            id="open-elsewhere",
            title="Another project's open task",
            status="open",
            created_by="planner",
            created_at="2026-04-24T10:00:00+00:00",
            tags=("project:ganglion", "needs-human"),
        )
    )
    fake.tasks.append(
        TaskRecord(
            id="done-elsewhere",
            title="Another project's completed task",
            status="completed",
            created_by="worker",
            created_at="2026-04-20T10:00:00+00:00",
            resolved_at="2026-04-22T10:00:00+00:00",
            tags=("project:ganglion", "milestone:t2"),
        )
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?project=influx&since=2026-04-01")

    assert response.status_code == 200
    # The box SUBMITS as before and now offers a vocabulary to submit.
    assert '<input type="text" name="add_tag" list="tags"' in response.text
    # Neither ganglion row is on this board…
    assert "Another project's open task" not in response.text
    assert "Another project's completed task" not in response.text
    # …and both their tags are still offerable, sorted and deduped across the
    # rows that share ``project:influx`` / ``area:docs``.
    assert _datalist_options(response.text, "tags") == [
        "area:docs",
        "milestone:t2",
        "needs-human",
        "project:ganglion",
        "project:influx",
    ]


def test_tag_datalist_offers_the_literal_tag_the_box_would_submit(
    lithos_lens_config_env: Path,
) -> None:
    """A tag is a bare string upstream — an ampersand, a quote and significant
    whitespace are all ordinary content — and the box's job is to offer exactly
    what it would submit. So the offered value is asserted DECODED (the markup
    really does carry entities), and then actually submitted: the round trip is
    the claim, not the attribute text."""
    awkward = 'R&D "spike"'
    padded = " needs review "
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        TaskRecord(
            id="awkward",
            title="Awkwardly tagged task",
            status="open",
            created_by="planner",
            created_at="2026-04-24T10:00:00+00:00",
            tags=(awkward, padded),
        )
    )
    fake.ready_ids.add("awkward")

    with _client(lithos_lens_config_env, fake) as client:
        offered = client.get("/tasks?since=2026-04-01")
        submitted = client.get(
            "/tasks", params=[("since", "2026-04-01"), ("add_tag", awkward)]
        )

    assert offered.status_code == 200
    assert _datalist_options(offered.text, "tags") == [
        padded,
        awkward,
        "area:docs",
        "project:influx",
    ]
    # The decode above is doing work: the attribute itself is escaped.
    assert '<option value="R&amp;D &#34;spike&#34;">' in offered.text
    # And the offered string, submitted verbatim, is a filter that matches the
    # row it came from and nothing else.
    assert submitted.status_code == 200
    assert "Awkwardly tagged task" in submitted.text
    assert "Claimed open task" not in submitted.text


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


# ---------------------------------------------------------------------------
# T1-S13 — the ``?tag=`` cross-project scope.
#
# The monthly-roadmap convention tags one committed set across every project it
# touches (``roadmap-2026-08`` spans lithos-loom, lithos-lens, influx, …), and
# the loom customer queue is the tag ``loom-candidate``. Neither is a project,
# so ``?project=`` cannot render either as one screen — that is what the tag
# scope is for.
# ---------------------------------------------------------------------------


def _roadmap_fake() -> TaskFakeLithosClient:
    """A corpus where one tag spans two projects, with an untagged row in each.

    Every pairing the sections need is present: ready/stale/completed rows both
    inside and outside the tag, so a section that ignored the filter would show
    a row this fixture can name.
    """
    fake = TaskFakeLithosClient()
    fake.tasks = [
        TaskRecord(
            id="loom-ready",
            title="Loom roadmap item",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=("project:lithos-loom", "roadmap-2026-08"),
        ),
        TaskRecord(
            id="lens-ready",
            title="Lens roadmap item",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=("project:lithos-lens", "roadmap-2026-08"),
        ),
        TaskRecord(
            id="loom-offscope",
            title="Loom side quest",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=("project:lithos-loom",),
        ),
        TaskRecord(
            id="lens-stale",
            title="Stale roadmap item",
            status="open",
            created_by="planner",
            # Old enough to fire the stale-open rule: this row is how the
            # Needs-attention section is observable under the tag filter.
            created_at="2025-01-01T10:00:00+00:00",
            tags=("project:lithos-lens", "roadmap-2026-08"),
        ),
        TaskRecord(
            id="lens-stale-offscope",
            title="Stale side quest",
            status="open",
            created_by="planner",
            created_at="2025-01-01T10:00:00+00:00",
            tags=("project:lithos-lens",),
        ),
        TaskRecord(
            id="loom-done",
            title="Done roadmap item",
            status="completed",
            created_by="worker",
            created_at="2026-04-20T10:00:00+00:00",
            resolved_at="2026-04-22T10:00:00+00:00",
            tags=("project:lithos-loom", "roadmap-2026-08"),
        ),
        TaskRecord(
            id="loom-done-offscope",
            title="Done side quest",
            status="completed",
            created_by="worker",
            created_at="2026-04-20T10:00:00+00:00",
            resolved_at="2026-04-22T10:00:00+00:00",
            tags=("project:lithos-loom",),
        ),
    ]
    fake.ready_ids = {
        "loom-ready",
        "lens-ready",
        "loom-offscope",
        "lens-stale",
        "lens-stale-offscope",
    }
    return fake


def test_tag_filter_scopes_every_section_across_projects(
    lithos_lens_config_env: Path,
) -> None:
    """Acceptance: ``?tag=roadmap-2026-08`` shows tasks from at least two
    different projects in ONE view, and every section respects it."""
    fake = _roadmap_fake()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?tag=roadmap-2026-08&since=2026-04-01")

    text = unescape(response.text)

    assert response.status_code == 200
    # Two different projects, one screen — the whole point of the tag scope.
    assert "Loom roadmap item" in text
    assert "Lens roadmap item" in text
    assert 'href="/tasks?since=2026-04-01&tag=project%3Alithos-loom"' in text
    assert 'href="/tasks?since=2026-04-01&tag=project%3Alithos-lens"' in text
    # Needs attention and Completed are scoped by the same predicate…
    assert "Stale roadmap item" in text
    assert "Done roadmap item" in text
    # …and every untagged row is out of scope, in every section.
    assert "Loom side quest" not in text
    assert "Stale side quest" not in text
    assert "Done side quest" not in text
    # The board says it is a slice rather than claiming system-wide health.
    assert "data-attention-scoped" not in text  # the attention list is non-empty
    assert "All systems healthy" not in text


def test_tag_filter_matches_tags_exactly(lithos_lens_config_env: Path) -> None:
    """Exact match, not prefix: ``roadmap-2026-08`` must not drag in
    ``roadmap-2026-08-stretch`` (a neighbouring month's convention would
    otherwise leak into the committed set)."""
    fake = _roadmap_fake()
    fake.tasks.append(
        TaskRecord(
            id="stretch",
            title="Stretch roadmap item",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=("project:influx", "roadmap-2026-08-stretch"),
        )
    )
    fake.ready_ids.add("stretch")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?tag=roadmap-2026-08&since=2026-04-01")

    assert response.status_code == 200
    assert "Loom roadmap item" in response.text
    assert "Stretch roadmap item" not in response.text


def test_unknown_tag_renders_the_all_clear_empty_state_not_an_error(
    lithos_lens_config_env: Path,
) -> None:
    """Acceptance: a tag nothing carries is an empty view, not a failure. The
    all-clear is the SCOPED one — every read landed, but the board is a slice,
    so it must not make the system-wide "All systems healthy" claim."""
    fake = _roadmap_fake()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?tag=loom-candidate&since=2026-04-01")

    text = unescape(response.text)

    assert response.status_code == 200
    assert "banner-warning" not in text
    assert "data-attention-scoped" in text
    assert "Nothing needs attention in this view." in text
    assert "No ready tasks match these filters." in text
    # The empty-corpus panel would misdescribe the cause: Lithos answered with
    # rows, the filter hid them.
    assert 'data-empty-state="window"' not in text
    assert "Loom roadmap item" not in text


def test_tag_filter_composes_with_project_agent_and_epic_scope(
    lithos_lens_config_env: Path,
) -> None:
    """The tag scope narrows an already-scoped board rather than replacing it:
    tag AND project AND agent AND the ``?epic=`` descendants."""
    fake = _roadmap_fake()
    fake.tasks.append(
        TaskRecord(
            id="epic-1",
            title="Roadmap epic",
            status="open",
            created_by="planner",
            created_at=_ago(hours=2),
            task_type="epic",
        )
    )
    fake.children["epic-1"] = ["loom-ready", "loom-offscope", "lens-ready"]

    with _client(lithos_lens_config_env, fake) as client:
        scoped = client.get("/tasks?epic=epic-1&tag=roadmap-2026-08&since=2026-04-01")
        narrowed = client.get(
            "/tasks?epic=epic-1&tag=roadmap-2026-08&project=lithos-loom"
            "&agent=planner&since=2026-04-01"
        )

    assert scoped.status_code == 200
    # Inside the epic AND carrying the tag.
    assert "Loom roadmap item" in scoped.text
    assert "Lens roadmap item" in scoped.text
    # In the epic but untagged; tagged but outside the epic.
    assert "Loom side quest" not in scoped.text
    assert "Stale roadmap item" not in scoped.text

    assert narrowed.status_code == 200
    assert "Loom roadmap item" in narrowed.text
    # Same epic and tag, wrong project.
    assert "Lens roadmap item" not in narrowed.text


def _epic_row(task_id: str, title: str) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        title=title,
        status="open",
        created_by="planner",
        created_at=_ago(hours=2),
        task_type="epic",
    )


def test_epic_strip_drops_the_chips_the_filters_would_empty(
    lithos_lens_config_env: Path,
) -> None:
    """Repro (prod 2026-09-11): the strip drew a chip for every open epic in
    the corpus on a tag-filtered board, and each chip's link carries that
    filter — so the off-tag ones led to boards with no rows and four "no match"
    lines. The filtered strip draws only the epics with work on this board, and
    says how many it left out."""
    fake = _roadmap_fake()
    fake.tasks.append(_epic_row("epic-roadmap", "Roadmap epic"))
    fake.tasks.append(_epic_row("epic-side", "Side quest epic"))
    fake.children["epic-roadmap"] = ["loom-ready", "lens-ready"]
    fake.children["epic-side"] = ["loom-offscope"]

    with _client(lithos_lens_config_env, fake) as client:
        filtered = client.get("/tasks?tag=roadmap-2026-08&since=2026-04-01")
        whole = client.get("/tasks?since=2026-04-01")

    text = unescape(filtered.text)

    assert filtered.status_code == 200
    assert 'data-epic-chip="epic-roadmap"' in text
    assert 'data-epic-chip="epic-side"' not in text
    # Not a silent strip: the chips it did not draw are counted.
    assert "1 more epic has no tasks on this board" in text
    # The unfiltered board still summarises the whole corpus.
    assert 'data-epic-chip="epic-side"' in whole.text
    assert "data-epic-strip-hidden" not in whole.text

    # …and when the filters empty EVERY chip the note stands alone, rather
    # than the strip vanishing as if the corpus had no epics.
    with _client(lithos_lens_config_env, fake) as client:
        nothing = client.get("/tasks?tag=loom-candidate&since=2026-04-01")

    assert "data-epic-chip" not in nothing.text
    assert "2 epics have no tasks on this board" in unescape(nothing.text)


def _epic_chip_links(html: str) -> dict[str, str]:
    """Every chip the strip rendered: epic id -> the href a click follows."""
    return {
        chip_id: unescape(href)
        for href, chip_id in re.findall(
            r'<a class="epic-chip[^"]*"\s+href="([^"]+)"\s+data-epic-chip="([^"]+)"',
            html,
        )
    }


def test_every_chip_on_a_fully_filtered_board_leads_to_a_board_with_rows(
    lithos_lens_config_env: Path,
) -> None:
    """The acceptance criterion, followed the way an operator follows it: on a
    board narrowed by tag AND project AND agent AND status, take the href of
    every chip the strip drew, request it, and require rows.

    The chip link is the active filters plus ``?epic=`` (§5.2.1), so this also
    pins that the link keeps carrying them — a chip that quietly dropped the
    project filter would "work" by widening the board it promised to scope."""
    fake = _roadmap_fake()
    fake.tasks.extend(
        [
            _epic_row("epic-loom", "Loom epic"),
            _epic_row("epic-lens", "Lens epic"),
            _epic_row("epic-untagged", "Side quest epic"),
            _epic_row("epic-done", "Finished epic"),
            _epic_row("epic-other-agent", "Other agent's epic"),
            _epic_row("epic-claimed", "Claimed-by-planner epic"),
        ]
    )
    # Two rows that differ from ``loom-ready`` in the AGENT dimension alone:
    # same project, same tag, same open status. One is another agent's work
    # outright; the other is another agent's row that ``planner`` claims, which
    # the creator-OR-claimer match (§5.4.2) must keep.
    fake.tasks.extend(
        [
            TaskRecord(
                id="loom-other",
                title="Loom roadmap item, other agent",
                status="open",
                created_by="worker",
                created_at=_ago(minutes=20),
                tags=("project:lithos-loom", "roadmap-2026-08"),
            ),
            TaskRecord(
                id="loom-claimed",
                title="Loom roadmap item, claimed by planner",
                status="open",
                created_by="worker",
                created_at=_ago(minutes=20),
                tags=("project:lithos-loom", "roadmap-2026-08"),
            ),
        ]
    )
    fake.ready_ids.add("loom-other")
    fake.claims["loom-claimed"] = (
        ClaimRecord(
            agent="planner", aspect="implementation", expires_at=_ahead(hours=6)
        ),
    )
    fake.children["epic-loom"] = ["loom-ready"]
    fake.children["epic-lens"] = ["lens-ready", "lens-stale"]
    fake.children["epic-untagged"] = ["loom-offscope"]
    fake.children["epic-done"] = ["loom-done"]
    fake.children["epic-other-agent"] = ["loom-other"]
    fake.children["epic-claimed"] = ["loom-claimed"]

    query = (
        "tag=roadmap-2026-08&project=lithos-loom&agent=planner"
        "&status=open&since=2026-04-01"
    )
    with _client(lithos_lens_config_env, fake) as client:
        board = client.get(f"/tasks?{query}")
        chips = _epic_chip_links(board.text)
        followed = {epic_id: client.get(href) for epic_id, href in chips.items()}
        # The same board with the agent term dropped, as the control.
        any_agent = client.get(f"/tasks?{query.replace('&agent=planner', '')}")

    assert board.status_code == 200
    # Wrong project, wrong tag, resolved work on an open-only board, and
    # another agent's work: four chips that could only have led to an empty
    # board. The claimed-by-planner epic survives on the claimer half of the
    # agent match.
    assert list(chips) == ["epic-loom", "epic-claimed"]
    assert "4 more epics have no tasks on this board" in unescape(board.text)
    # The agent term does that work on its own: without it, and with every
    # other filter unchanged, the other agent's epic is back.
    assert 'data-epic-chip="epic-other-agent"' in any_agent.text
    assert 'data-epic-chip="epic-lens"' not in any_agent.text

    for epic_id, href in chips.items():
        # The link is the board's own filters plus the epic — all of them.
        for term in (
            "tag=roadmap-2026-08",
            "project=lithos-loom",
            "agent=planner",
            "status=open",
            "since=2026-04-01",
            f"epic={epic_id}",
        ):
            assert term in href, (epic_id, href)
        response = followed[epic_id]
        assert response.status_code == 200
        # …and following it lands on rows, not on four "no match" lines.
        assert "data-task-row" in response.text, epic_id
        assert "data-epic-scope-unmatched" not in response.text, epic_id
    assert "Loom roadmap item" in followed["epic-loom"].text
    assert "claimed by planner" in unescape(followed["epic-claimed"].text)


def test_a_chip_whose_only_work_is_a_gate_leads_to_its_gate(
    lithos_lens_config_env: Path,
) -> None:
    """Gates are rows this board places (they have their own section), so an
    epic whose only matching descendant is an open gate is a live chip — and
    following it must render that gate rather than the "nothing matches"
    explanation, which reads ``gate_groups`` separately from the sections."""
    fake = _roadmap_fake()
    _add_gate(
        fake,
        "loom-gate",
        title="Loom roadmap approval",
        tags=("project:lithos-loom", "roadmap-2026-08"),
    )
    fake.tasks.append(_epic_row("epic-gated", "Gated epic"))
    fake.children["epic-gated"] = ["loom-gate"]

    query = "tag=roadmap-2026-08&project=lithos-loom&since=2026-04-01"
    with _client(lithos_lens_config_env, fake) as client:
        board = client.get(f"/tasks?{query}")
        chips = _epic_chip_links(board.text)
        scoped = client.get(chips["epic-gated"])

    text = unescape(scoped.text)

    assert board.status_code == 200
    assert scoped.status_code == 200
    # The gate is the whole content of that board, and it renders.
    assert 'data-gate-row data-task-id="loom-gate"' in scoped.text
    assert 'data-task-group="gates"' in scoped.text
    # …so neither epic explanation applies — the board is not blank.
    assert "data-epic-scope-unmatched" not in text
    assert "data-epic-scope-rolled-up" not in text


def test_a_scope_holding_only_rolled_up_rows_says_which_gap_it_is(
    lithos_lens_config_env: Path,
) -> None:
    """Reviewer repro (c-001) as the operator sees it: the sub-epic under this
    scope DID survive the filters, so "none of it survives the other filters"
    would be false. The board says what is actually true — the matching rows
    roll up rather than rendering — and says it once."""
    fake = _roadmap_fake()
    fake.tasks.extend(
        [
            _epic_row("epic-outer", "Outer epic"),
            _epic_row("epic-nested", "Nested epic"),
        ]
    )
    # The nested epic carries the filtered tag, so it is not filtered OUT; it
    # is simply not a row. Its own subtree is empty, so it has no chip either.
    fake.tasks[-1] = replace(fake.tasks[-1], tags=("roadmap-2026-08",))
    fake.children["epic-outer"] = ["epic-nested"]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks?epic=epic-outer&tag=roadmap-2026-08&since=2026-04-01"
        )

    text = unescape(response.text)

    assert response.status_code == 200
    assert "data-epic-scope-rolled-up" in text
    assert "Nothing under this epic renders as a row." in text
    assert "roll up rather than rendering here" in text
    # The WRONG explanation, and the generic ones, stay away.
    assert "data-epic-scope-unmatched" not in text
    assert "No tasks under this epic match these filters." not in text
    assert 'data-empty-state="rolled-up"' not in text
    assert "data-task-group=" not in text


class _CompletedWindowDown(TaskFakeLithosClient):
    """Every read answers except the completed window (§14 degraded path)."""

    async def list_tasks(self, **kwargs: Any) -> list[TaskRecord]:
        if kwargs.get("status") == "completed":
            raise RuntimeError("completed window unavailable")
        return await super().list_tasks(**kwargs)


def test_an_unread_window_is_not_reported_as_a_filter_result(
    lithos_lens_config_env: Path,
) -> None:
    """Reviewer repro (c-002): on a ``?status=completed`` board scoped to an
    epic whose subtree HAS a matching completed child, the completed read
    fails. The epic explanations stand down — Lens cannot claim a filter result
    about rows it never saw — and the section that came back empty must not
    make that claim either, which is what it used to do one line below the
    banner saying the read failed."""
    template = _roadmap_fake()
    fake = _CompletedWindowDown()
    fake.tasks = template.tasks
    fake.ready_ids = template.ready_ids
    fake.tasks.append(_epic_row("epic-loom", "Loom epic"))
    fake.children["epic-loom"] = ["loom-done"]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks?status=completed&tag=roadmap-2026-08&epic=epic-loom"
            "&since=2026-04-01"
        )

    text = unescape(response.text)

    assert response.status_code == 200
    # The read that failed is named…
    assert "Could not load completed tasks." in text
    # …and the empty section says the same thing rather than the opposite.
    assert "data-section-unavailable" in text
    assert "could not be loaded" in text
    assert "No completed tasks match these filters." not in text
    # Neither epic explanation is supportable here, so neither renders.
    assert "data-epic-scope-unmatched" not in text
    assert "data-epic-scope-rolled-up" not in text


def test_a_window_the_board_hides_cannot_silence_the_epic_explanation(
    lithos_lens_config_env: Path,
) -> None:
    """The complement, as rendered: on an open-only board the completed window
    is off screen, so its outage says nothing about which rows belong here. The
    epic explanation — supported entirely by reads that answered — must still
    render, the strip must still be scoped, and no section may claim to be
    unavailable."""
    template = _roadmap_fake()
    fake = _CompletedWindowDown()
    fake.tasks = template.tasks
    fake.ready_ids = template.ready_ids
    fake.tasks.extend(
        [
            _epic_row("epic-side", "Side quest epic"),
            _epic_row("epic-roadmap", "Roadmap epic"),
        ]
    )
    fake.children["epic-side"] = ["loom-offscope"]
    fake.children["epic-roadmap"] = ["loom-ready"]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks?status=open&tag=roadmap-2026-08&epic=epic-side&since=2026-04-01"
        )

    text = unescape(response.text)

    assert response.status_code == 200
    # The failed read is reported, and nothing on this board is unavailable
    # because of it — the completed window is not on screen at all.
    assert "Could not load completed tasks." in text
    assert "data-section-unavailable" not in text
    # The explanation this board CAN support still renders, and replaces the
    # generic groups as before.
    assert "data-epic-scope-unmatched" in text
    assert "No tasks under this epic match these filters." in text
    assert "data-task-group=" not in text
    # …and the strip is still scoped: the roadmap epic keeps its chip beside
    # the selected one, and nothing else does.
    assert 'data-epic-chip="epic-roadmap"' in text
    assert 'data-epic-chip="epic-side"' in text


def test_a_window_that_answered_still_says_no_match(
    lithos_lens_config_env: Path,
) -> None:
    """The other half: only the window that failed is unknown. The cancelled
    read answered, so its empty section keeps the honest filter wording — a
    blanket suppression would hide that distinction (and, where the read did
    return rows, the rows themselves)."""
    template = _roadmap_fake()
    fake = _CompletedWindowDown()
    fake.tasks = template.tasks
    fake.ready_ids = template.ready_ids

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks?status=completed&status=cancelled&tag=roadmap-2026-08"
            "&since=2026-04-01"
        )

    text = unescape(response.text)

    assert response.status_code == 200
    assert "Completed tasks could not be loaded" in text
    assert "No cancelled tasks match these filters." in text


def test_an_epic_scope_the_filters_empty_says_so(
    lithos_lens_config_env: Path,
) -> None:
    """The selected chip stays whatever the filters leave of it, so the board
    explains the empty sections instead of leaving the operator to work out
    that the epic was never in this filter — and offers the way out."""
    fake = _roadmap_fake()
    fake.tasks.append(_epic_row("epic-side", "Side quest epic"))
    fake.children["epic-side"] = ["loom-offscope"]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks?epic=epic-side&tag=roadmap-2026-08&since=2026-04-01"
        )

    text = unescape(response.text)

    assert response.status_code == 200
    assert "data-epic-scope-unmatched" in text
    assert "No tasks under this epic match these filters." in text
    assert 'href="/tasks?epic=epic-side"' in text
    # The chip is still there to click back out of.
    assert 'data-epic-chip="epic-side"' in text
    # …and the wrong explanations are not: the scope WAS applied, and the epic
    # is not childless.
    assert "data-epic-scope-missing" not in text
    assert "data-epic-scope-empty" not in text
    # The failure this task is about: four (here five) "No … match these
    # filters" lines that never mention the epic. The banner replaces them —
    # every empty section group is gone, not just quieter.
    for line in (
        "Nothing needs attention in this view.",
        "No open gates match these filters.",
        "No ready tasks match these filters.",
        "No blocked tasks match these filters.",
        "No completed tasks match these filters.",
        "No cancelled tasks match these filters.",
    ):
        assert line not in text, line
    assert "data-task-group=" not in text


def test_active_tag_filter_renders_a_chip_that_clears_only_that_tag(
    lithos_lens_config_env: Path,
) -> None:
    """The tag scope is otherwise invisible: a cross-project tag names no
    project, and the row chips only name each row's own tags. The chip says
    what the board is scoped to and links to the same board without it,
    keeping every other filter."""
    fake = _roadmap_fake()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks?project=lithos-loom&tag=roadmap-2026-08&tag=loom-candidate"
            "&agent=planner&since=01/04/2026"
        )
        css = client.get("/static/lens.css")

    text = unescape(response.text)

    assert response.status_code == 200
    chips = text.split("data-active-filters")[1].split("</section>")[0]
    assert 'data-active-filter-tag="roadmap-2026-08"' in chips
    assert 'data-active-filter-tag="loom-candidate"' in chips
    # Clearing one chip keeps the other tag and every other live filter.
    assert (
        'href="/tasks?project=lithos-loom&agent=planner'
        '&since=01%2F04%2F2026&tag=loom-candidate"'
    ) in chips
    assert (
        'href="/tasks?project=lithos-loom&agent=planner'
        '&since=01%2F04%2F2026&tag=roadmap-2026-08"'
    ) in chips
    # The chip sits with the filter surfaces, above the board it describes…
    assert text.index("data-active-filters") < text.index('class="task-board"')
    # …and reads as the interactive chip it is, rather than unstyled text.
    assert ".active-filter-chip {" in css.text


def test_long_tag_filters_the_board_instead_of_being_dropped(
    lithos_lens_config_env: Path,
) -> None:
    """Regression (correctness/f-001): a tag longer than any ceiling Lens might
    invent is still a VALID Lithos tag — the tool schema sets no ``maxLength``
    — so ``?tag=<it>`` must filter to it.

    A round-2 length ceiling dropped the term and rendered the whole unfiltered
    board with no chip: strictly worse than an error, because the operator saw
    unrelated rows under chrome that claimed a scope was applied.
    """
    long_tag = "roadmap-" + "x" * 200
    fake = _roadmap_fake()
    fake.tasks.append(
        TaskRecord(
            id="long-tagged",
            title="Long tagged item",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=("project:influx", long_tag),
        )
    )
    fake.ready_ids.add("long-tagged")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(f"/tasks?tag={long_tag}&since=2026-04-01")

    assert response.status_code == 200
    # The predicate is applied, not discarded…
    assert "Long tagged item" in response.text
    assert "Loom roadmap item" not in response.text
    # …and the board says it is scoped.
    assert "data-active-filters" in response.text
    assert "All systems healthy" not in response.text


def test_every_requested_tag_term_narrows_the_board(
    lithos_lens_config_env: Path,
) -> None:
    """Regression (correctness/f-001): a count ceiling weakened an N-term AND to
    its first N-1 terms, so rows missing the dropped term showed as matches."""
    terms = [f"term-{i}" for i in range(MAX_FILTER_TAG_CHIPS + 8)]
    fake = _roadmap_fake()
    fake.tasks.append(
        TaskRecord(
            id="all-terms",
            title="Carries every term",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=tuple(terms),
        )
    )
    fake.tasks.append(
        TaskRecord(
            id="missing-last",
            title="Missing the last term",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            # Every term but the final one: a board that honoured only the
            # first N would show this row as a match.
            tags=tuple(terms[:-1]),
        )
    )
    fake.ready_ids.update({"all-terms", "missing-last"})

    with _client(lithos_lens_config_env, fake) as client:
        query = urlencode([("tag", term) for term in terms] + [("since", "2026-04-01")])
        response = client.get(f"/tasks?{query}")

    assert response.status_code == 200
    assert "Carries every term" in response.text
    assert "Missing the last term" not in response.text


def test_chip_strip_caps_what_it_draws_but_says_the_rest_still_apply(
    lithos_lens_config_env: Path,
) -> None:
    """The strip is quadratic in tag count, so it stops drawing chips — but the
    tags it does not draw are still filtering, and the strip has to say so or
    the board under-reports its own scope."""
    terms = [f"term-{i}" for i in range(MAX_FILTER_TAG_CHIPS + 5)]
    fake = _roadmap_fake()

    with _client(lithos_lens_config_env, fake) as client:
        query = urlencode([("tag", term) for term in terms] + [("since", "2026-04-01")])
        response = client.get(f"/tasks?{query}")

    assert response.status_code == 200
    assert response.text.count("data-active-filter-tag=") == MAX_FILTER_TAG_CHIPS
    assert "and 5 more tags, also applied" in response.text
    # Undrawn terms still filter: nothing carries them, so the board is empty.
    assert "Loom roadmap item" not in response.text


@pytest.mark.parametrize(
    "key",
    ["tag", "status", "epic", "since", "created_since", "project", "agent"],
)
def test_oversized_filter_query_is_refused_not_silently_widened(
    lithos_lens_config_env: Path, key: str
) -> None:
    """Regression (security/f-001, security/f-003): every filter is re-emitted
    into each generated URL — the summary cards, a detail link per row, a tag
    link per tag per row — so the response echoes the query string
    O(rows x tags) times. On a 400-row board a 58 KB ``?status=`` rendered a
    116 MB body (~2000x) and 34 KB of ``?tag=`` rendered 499 MB, against a
    single-worker event loop.

    Bounding the REQUEST bounds every echo at once. It is refused rather than
    trimmed: dropping filter terms would widen the board (correctness/f-001).
    """
    fake = _roadmap_fake()
    oversized = "x" * (MAX_FILTER_QUERY_BYTES + 1)

    with _client(lithos_lens_config_env, fake) as client:
        baseline = client.get("/tasks?since=2026-04-01")
        response = client.get("/tasks", params={key: oversized})

    assert response.status_code == 400
    assert "data-filter-rejected" in response.text
    assert "Filter too large to apply" in response.text
    # No board is rendered, so no row can leak past the filter that was refused.
    assert "data-task-group" not in response.text
    assert "Loom roadmap item" not in response.text
    # The offending value is not echoed back — reflecting it is the whole bug.
    assert oversized not in response.text
    assert len(response.text) < len(baseline.text)


@pytest.mark.parametrize(
    ("label", "char", "encoded_per_char"),
    [
        # One character, several encoded sizes. urlencode emits quote_plus
        # output, so the guard has to count THAT, not code points.
        ("reserved-ascii", "%", 3),
        ("cjk", "\u6f22", 9),
        ("astral", "\U0001f600", 12),
    ],
)
def test_oversized_filter_is_measured_in_the_bytes_it_emits(
    lithos_lens_config_env: Path, label: str, char: str, encoded_per_char: int
) -> None:
    """Regression (security/f-004): the ceiling used to count CODE POINTS while
    ``urlencode`` emits percent-encoded BYTES, so a value at the character
    budget could emit up to 12x it and sail through.

    Measured on a 400-row board: 1,018 astral characters scored 1,018 against a
    1,024 budget, emitted 12,216 bytes into every generated link, and rendered
    25 MB — 9.8x the ASCII worst case the budget was set for.
    """
    fake = _roadmap_fake()
    # Comfortably inside the budget by character count, over it once encoded —
    # exactly the shape that slipped through.
    value = char * (MAX_FILTER_QUERY_BYTES // encoded_per_char + 10)
    assert len(value) < MAX_FILTER_QUERY_BYTES, "must pass a code-point count"
    assert len(quote_plus(value)) > MAX_FILTER_QUERY_BYTES

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks", params={"epic": value})

    assert response.status_code == 400
    assert "data-filter-rejected" in response.text
    assert "data-task-group" not in response.text
    assert value not in response.text


def test_non_ascii_filter_within_the_budget_is_still_served(
    lithos_lens_config_env: Path,
) -> None:
    """The ceiling counts encoded bytes, which is stricter — so it has to be
    checked from the other side too: a real non-ASCII tag is not collateral."""
    fake = _roadmap_fake()
    fake.tasks.append(
        TaskRecord(
            id="accented",
            title="Accented tag item",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=("project:influx", "área:donn\u00e9es"),
        )
    )
    fake.ready_ids.add("accented")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks", params={"tag": "área:donn\u00e9es", "since": "2026-04-01"}
        )

    assert response.status_code == 200
    assert "data-active-filters" in response.text
    assert "Accented tag item" in response.text


def test_filters_are_parsed_once_per_request_not_once_per_link(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (security/f-005): the preserved-filter scan runs per generated
    URL — one per row, one per tag per row, plus the cards and epic chips — and
    each scan walks EVERY query param, including unrecognised ones that score
    nothing against the byte budget. That made a request cost O(params x links):
    5,000 junk params took 0.69s on a 400-row board against 0.04s clean.

    Counted rather than timed, so it cannot go flaky: the query string is
    scanned a fixed number of times per request, not once per link.
    """
    scans = 0
    original = QueryParams.multi_items

    def counting(self: QueryParams) -> Any:
        nonlocal scans
        scans += 1
        return original(self)

    monkeypatch.setattr(QueryParams, "multi_items", counting)

    fake = _roadmap_fake()
    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?tag=roadmap-2026-08&since=2026-04-01")

    assert response.status_code == 200
    links = response.text.count('href="/tasks')
    # The board really does render many filter-carrying links off this request…
    assert links > 10
    # …and they share one scan (plus the route's own parse_filters read).
    assert scans <= 3, f"{scans} query-string scans for {links} generated links"


@pytest.mark.parametrize(
    "path", ["/tasks", "/tasks/open-claimed", "/tasks/open-claimed/findings"]
)
def test_oversized_filters_reflect_nowhere_on_any_tasks_route(
    lithos_lens_config_env: Path, path: str
) -> None:
    """The amplification lives in ``_preserved_filter_params``, which every
    tasks route shares — so the guard belongs there, not on one route. The
    refusal page still renders its own links, and none of them may carry the
    value the page is refusing to reflect."""
    fake = TaskFakeLithosClient()
    oversized = "x" * (MAX_FILTER_QUERY_BYTES + 1)

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(path, params={"status": oversized})

    assert response.status_code == 400
    assert "data-filter-rejected" in response.text
    assert oversized not in response.text


def test_filter_query_at_the_exact_size_ceiling_is_served(
    lithos_lens_config_env: Path,
) -> None:
    """Regression (correctness/f-002): the ceiling rejects only what is LARGER
    than it — the banner tells the operator so — and a query of exactly
    ``MAX_FILTER_QUERY_BYTES`` was being refused anyway.

    The cause was estimating the separators: two bytes per pair, when
    ``urlencode`` writes "=" per pair but "&" only BETWEEN pairs, so every
    non-empty filter scored one byte high. The old boundary test hid it by
    asserting on a 1,019-byte value — a 1,023-byte query it called "exactly at
    the ceiling".

    The arithmetic is pinned here as well as the behaviour, so this test cannot
    quietly drift off the boundary again.
    """
    fake = _roadmap_fake()
    at_ceiling = "x" * (MAX_FILTER_QUERY_BYTES - len("tag="))
    over_ceiling = at_ceiling + "x"
    assert len(urlencode([("tag", at_ceiling)])) == MAX_FILTER_QUERY_BYTES
    assert len(urlencode([("tag", over_ceiling)])) == MAX_FILTER_QUERY_BYTES + 1

    with _client(lithos_lens_config_env, fake) as client:
        served = client.get("/tasks", params={"tag": at_ceiling})
        refused = client.get("/tasks", params={"tag": over_ceiling})

    assert served.status_code == 200
    assert "data-filter-rejected" not in served.text
    assert "data-active-filters" in served.text
    assert refused.status_code == 400
    assert "data-filter-rejected" in refused.text


def test_multi_key_filter_query_at_the_exact_size_ceiling_is_served(
    lithos_lens_config_env: Path,
) -> None:
    """The same boundary with two pairs, which is where the "&"-only-between
    rule actually bites: an estimate that charged every pair for both
    separators was over by one per additional pair."""
    fake = _roadmap_fake()
    # "agent=" + "&" + "tag=" + the two values == the ceiling exactly.
    agent = "a" * 20
    tag = "x" * (MAX_FILTER_QUERY_BYTES - len("agent=") - 1 - len("tag=") - len(agent))
    pairs = [("agent", agent), ("tag", tag)]
    assert len(urlencode(pairs)) == MAX_FILTER_QUERY_BYTES

    with _client(lithos_lens_config_env, fake) as client:
        over = [("agent", agent), ("tag", tag + "x")]
        served = client.get(f"/tasks?{urlencode(pairs)}")
        refused = client.get(f"/tasks?{urlencode(over)}")

    assert served.status_code == 200
    assert "data-filter-rejected" not in served.text
    assert refused.status_code == 400


def test_rejection_logs_the_route_template_not_the_raw_path(
    lithos_lens_config_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (security/f-006): ``task_id`` is a free path segment bounded
    only by the URL length limit, and NOT by ``MAX_FILTER_QUERY_BYTES``, which
    measures the query string. Logging the concrete path let a 50 KB request
    write a 50,213-byte log line while the response stayed correctly bounded at
    1,507 B — a persistent sink for a transient input, on a container whose
    json-file driver has no size cap.
    """
    fake = TaskFakeLithosClient()
    long_id = "A" * 20000

    with (
        _client(lithos_lens_config_env, fake) as client,
        caplog.at_level(logging.WARNING, logger="lithos_lens.web"),
    ):
        response = client.get(
            f"/tasks/{long_id}",
            params={"epic": "y" * (MAX_FILTER_QUERY_BYTES + 1)},
        )

    assert response.status_code == 400
    record = next(
        item
        for item in caplog.records
        if item.getMessage() == "filter query rejected as oversized"
    )
    # The template, which is what the field name has always implied.
    assert getattr(record, "lens_route", None) == "/tasks/{task_id}"
    # And the emitted line is bounded, whatever the request length.
    emitted = JsonFormatter().format(record)
    assert long_id[:200] not in emitted
    assert len(emitted) < 1000


@pytest.mark.parametrize(
    ("label", "literal"),
    [
        # The vendored Lithos schema types a tag as a bare "string" — no
        # pattern, no length, no excluded characters — so all of these are
        # ordinary tag content, and an exact-match filter has to name them.
        ("comma", "customer,2"),
        ("leading-and-trailing-space", " urgent "),
        ("inner-space", "needs review"),
        ("plus", "a+b"),
        ("percent", "50%-done"),
    ],
)
def test_literal_tags_are_selectable_end_to_end(
    lithos_lens_config_env: Path, label: str, literal: str
) -> None:
    """Regression (correctness/f-004): ``?tag=`` claimed exact match but its URL
    vocabulary could not represent the tag domain.

    Every value was comma-split and stripped, so the real tag ``customer,2``
    became ``customer`` AND ``2`` and matched nothing, and `` urgent `` was
    rewritten to ``urgent`` — which matched a DIFFERENT task. A filter that
    quietly answers a question nobody asked is worse than one that finds
    nothing.

    Follows the row's own tag link too, so the round trip is pinned end to end
    rather than just the parse.
    """
    fake = _roadmap_fake()
    fake.tasks.append(
        TaskRecord(
            id="literal",
            title="Literally tagged task",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=(literal,),
        )
    )
    # The value the OLD splitting/stripping would have collapsed this to. When
    # that differs from the literal, a task carrying it is the decoy: the old
    # behaviour matched this one instead of the one that was asked for.
    collapsed = tuple(part.strip() for part in literal.split(",") if part.strip())
    fake.ready_ids.add("literal")
    if collapsed != (literal,):
        fake.tasks.append(
            TaskRecord(
                id="near-miss",
                title="Near miss task",
                status="open",
                created_by="planner",
                created_at=_ago(minutes=20),
                tags=collapsed,
            )
        )
        fake.ready_ids.add("near-miss")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks", params={"tag": literal})

        assert response.status_code == 200
        assert "Literally tagged task" in response.text
        # The collapsed value must NOT drag its own task in.
        if collapsed != (literal,):
            assert "Near miss task" not in response.text

        # The row's tag chip links back to this same filtered board.
        text = unescape(response.text)
        row = text.split("data-task-row")[1]
        href = re.findall(r'class="tag-chip[^"]*" href="([^"]+)"', row)[0]
        followed = client.get(href)

    assert followed.status_code == 200
    assert "Literally tagged task" in followed.text


def test_empty_tag_is_a_literal_scope_end_to_end(
    lithos_lens_config_env: Path,
) -> None:
    """Regression (correctness/f-004): ``""`` is a tag a task can validly carry —
    the vendored schema sets no ``minLength`` — so ``?tag=`` is the empty-tag
    scope, not the absence of a filter.

    Reading blank as absence returned the whole unfiltered board for an
    exact-match request. The decoys below are what makes that visible: an
    ordinarily-tagged task and an untagged one, neither of which may appear.
    """
    fake = _roadmap_fake()
    fake.tasks.append(
        TaskRecord(
            id="empty-tagged",
            title="Empty tagged task",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=("",),
        )
    )
    fake.tasks.append(
        TaskRecord(
            id="untagged",
            title="Untagged decoy task",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=(),
        )
    )
    fake.ready_ids.update({"empty-tagged", "untagged"})

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?tag=")

        assert response.status_code == 200
        # Only the task carrying the empty tag — an unfiltered board would show
        # every one of these.
        assert "Empty tagged task" in response.text
        assert "Untagged decoy task" not in response.text
        assert "Loom roadmap item" not in response.text

        text = unescape(response.text)
        # The scope is named, not silently applied — and not drawn as a blank
        # pill, which would read as a rendering bug.
        assert 'data-active-filter-tag=""' in text
        assert "(empty tag)" in text

        # Round-trips through the row's own tag link…
        row = text.split("data-task-row")[1]
        row_href = re.findall(r'class="tag-chip[^"]*" href="([^"]+)"', row)[0]
        followed = client.get(row_href)

        # …and through the chip's clear link.
        clear_href = re.findall(r'href="([^"]+)"[^>]*data-active-filter-tag=""', text)[
            0
        ]
        cleared = client.get(clear_href)

    assert followed.status_code == 200
    assert "Empty tagged task" in followed.text
    assert "Untagged decoy task" not in followed.text

    assert cleared.status_code == 200
    assert "data-active-filters" not in cleared.text
    # Clearing the only tag really does widen back to the whole board.
    assert "Loom roadmap item" in cleared.text


def test_blank_add_tag_box_does_not_become_an_empty_tag_filter(
    lithos_lens_config_env: Path,
) -> None:
    """The filter bar's box is a separate parameter, so the blank an HTML form
    submits on every search cannot be read as the empty-tag scope — which is
    what lets ``tag`` stay fully literal.

    This is the ordinary UI path: submit the bar with nothing typed.
    """
    fake = _roadmap_fake()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks", params=[("status", "open"), ("add_tag", ""), ("agent", "")]
        )

    assert response.status_code == 200
    # No tag filter was applied…
    assert "data-active-filters" not in response.text
    # …so the board is the whole open board, not an empty-tag scope.
    assert "Loom roadmap item" in response.text


def test_add_tag_box_folds_into_the_tag_set_and_stops_propagating(
    lithos_lens_config_env: Path,
) -> None:
    """A tag added through the box becomes an ordinary ``tag`` on the way out.

    Generated links carry the canonical list, so the "add" does not re-apply on
    every subsequent click — and the empty tag survives navigation with it.
    """
    fake = _roadmap_fake()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks", params=[("tag", ""), ("add_tag", "roadmap-2026-08")]
        )

    assert response.status_code == 200
    text = unescape(response.text)
    detail_href = re.findall(r'class="task-title" href="([^"]+)"', text)
    chip_hrefs = re.findall(r'href="([^"]+)"[^>]*data-active-filter-tag=', text)
    # Both tags are active…
    assert text.count("data-active-filter-tag=") == 2
    # …and every generated link re-emits them as canonical `tag` pairs only.
    for href in [*detail_href, *chip_hrefs]:
        assert "add_tag" not in href


def test_repeated_tag_params_and_together_and_commas_stay_literal(
    lithos_lens_config_env: Path,
) -> None:
    """The AND spelling is the repeated parameter. ``?tag=a,b`` is ONE tag —
    the two must not be confusable, which is the whole point of dropping the
    comma convenience for tags."""
    fake = _roadmap_fake()
    fake.tasks.append(
        TaskRecord(
            id="two-tags",
            title="Has a and b",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=("a", "b"),
        )
    )
    fake.tasks.append(
        TaskRecord(
            id="one-tag",
            title="Has the literal a,b",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=("a,b",),
        )
    )
    fake.ready_ids.update({"two-tags", "one-tag"})

    with _client(lithos_lens_config_env, fake) as client:
        both = client.get("/tasks", params=[("tag", "a"), ("tag", "b")])
        literal = client.get("/tasks", params={"tag": "a,b"})

    assert "Has a and b" in both.text
    assert "Has the literal a,b" not in both.text

    assert "Has the literal a,b" in literal.text
    assert "Has a and b" not in literal.text


def test_filter_bar_round_trips_multiple_tags_without_joining_them(
    lithos_lens_config_env: Path,
) -> None:
    """Submitting the filter bar must not collapse an active multi-tag scope
    into one comma-joined literal. The active tags ride as hidden ``tag``
    inputs — the same convention the epic scope already uses — so a plain
    form submit re-sends exactly what was active."""
    fake = _roadmap_fake()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks", params=[("tag", "roadmap-2026-08"), ("tag", "loom-candidate")]
        )

    assert response.status_code == 200
    bar = response.text.split('class="filter-bar"')[1].split("</form>")[0]
    assert '<input type="hidden" name="tag" value="roadmap-2026-08">' in bar
    assert '<input type="hidden" name="tag" value="loom-candidate">' in bar
    # …and the visible box adds one more rather than re-submitting a join.
    assert 'value="roadmap-2026-08,loom-candidate"' not in bar


def test_chip_clear_link_from_a_near_budget_tag_query_still_works(
    lithos_lens_config_env: Path,
) -> None:
    """Regression (correctness/f-003): every chip advertises itself as a clear
    control, so following one has to work for any request the board accepted.

    It did not: the clear link re-emitted the tags in a spelling that could be
    larger than the accepted request, so a near-budget board rendered chips
    that 400 when clicked. Tags now have ONE spelling, so a clear link is a
    strict subset of the request that was already accepted.

    Follows the link rather than inspecting it, which is the only way this
    shows up: a small two-tag href cannot cross the ceiling.
    """
    tags = [f"roadmap-2026-{i:02d}" for i in range(51)]
    query = urlencode([("tag", tag) for tag in tags])
    # Near the budget and inside it: the request the board must accept, and
    # whose clear links must therefore also be accepted.
    assert 900 < len(query) <= MAX_FILTER_QUERY_BYTES

    fake = _roadmap_fake()
    fake.tasks.append(
        TaskRecord(
            id="carries-all",
            title="Carries every filter tag",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=(*tags, "project:influx"),
        )
    )
    fake.ready_ids.add("carries-all")

    with _client(lithos_lens_config_env, fake) as client:
        board = client.get(f"/tasks?{query}")
        assert board.status_code == 200
        assert "Carries every filter tag" in board.text

        text = unescape(board.text)
        href = re.findall(r'href="([^"]+)"[^>]*data-active-filter-tag=', text)[0]
        # The link the chip advertises is itself within the budget…
        assert len(href.split("?", 1)[1]) <= MAX_FILTER_QUERY_BYTES
        cleared = client.get(href)

    # …and following it shows the same board minus that one tag.
    assert cleared.status_code == 200
    assert "data-filter-rejected" not in cleared.text
    assert "Carries every filter tag" in cleared.text
    remaining = re.findall(r'data-active-filter-tag="([^"]+)"', unescape(cleared.text))
    assert tags[0] not in remaining
    assert tags[1] in remaining


def test_dashboard_debug_log_does_not_carry_the_raw_query_pairs(
    lithos_lens_config_env: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (security/f-007): params outside ``_PRESERVED_FILTER_KEYS``
    score zero against ``MAX_FILTER_QUERY_BYTES``, so the raw pair list is
    unbounded attacker-controlled data — a 47 KB junk query wrote 72 KB of log.
    Under the container's size-capped rotation that is cheap eviction of the
    log history, which on a service with no authentication is the only forensic
    record there is.
    """
    fake = _roadmap_fake()
    junk = [(f"j{i}", "x" * 5) for i in range(4000)]

    with (
        _client(lithos_lens_config_env, fake) as client,
        caplog.at_level(logging.DEBUG, logger="lithos_lens.web"),
    ):
        response = client.get("/tasks", params=[*junk, ("tag", "roadmap-2026-08")])

    assert response.status_code == 200
    record = next(
        item
        for item in caplog.records
        if item.getMessage() == "tasks dashboard filters parsed"
    )
    # The bounded count replaces the unbounded list…
    assert getattr(record, "query_param_count", None) == len(junk) + 1
    assert not hasattr(record, "query_items")
    # …so the emitted line stays small whatever the request carried, and the
    # junk never reaches it.
    emitted = JsonFormatter().format(record)
    assert "j3999" not in emitted
    assert len(emitted) < 1000
    # The diagnostic value — the PARSED filters — is still there.
    assert getattr(record, "tags", None) == ["roadmap-2026-08"]


def test_no_active_filter_chip_without_a_tag_filter(
    lithos_lens_config_env: Path,
) -> None:
    fake = _roadmap_fake()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?project=lithos-loom&since=2026-04-01")

    assert response.status_code == 200
    assert "data-active-filters" not in response.text


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
            created_at=_ago(hours=2),
            tags=("project:a",),
        )
    )
    fake.tasks.append(
        TaskRecord(
            id="pred",
            title="Predecessor in project B",
            status="open",
            created_by="planner",
            created_at=_ago(hours=3),
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
            created_at=_ago(hours=2),
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


def test_cancelled_blocker_row_renders_only_in_needs_attention(
    lithos_lens_config_env: Path,
) -> None:
    """Slice-3 acceptance: a task whose blocker was cancelled renders ONLY in
    Needs attention, carrying an ``unsatisfiable`` reason chip that names the
    dead predecessor. Leaving it in Blocked would read as ordinary waiting."""
    fake = TaskFakeLithosClient()
    fake.ready_ids = {"open-unclaimed"}
    fake.blocked = {
        "open-old": (
            BlockerRecord(
                kind="blocker_unsatisfiable",
                task_id="open-claimed",
                type="blocks",
                status="cancelled",
                message="Blocking predecessor open-claimed was cancelled;",
            ),
        )
    }

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?status=open&since=2026-04-01")

    assert response.status_code == 200
    text = response.text
    attention = text[text.index('data-task-group="attention"') :]
    attention = attention[: attention.index("</article>")]
    assert 'data-task-id="open-old"' in attention
    assert 'data-attention-rule="unsatisfiable"' in attention
    # The chip's supporting fact names the cancelled predecessor by title.
    assert "Claimed open task" in unescape(attention)
    # Single placement: the row is gone from Blocked, and that section is empty.
    blocked_group = text[text.index('data-task-group="blocked"') :]
    blocked_group = blocked_group[: blocked_group.index("</article>")]
    assert 'data-task-id="open-old"' not in blocked_group
    assert "No blocked tasks match these filters" in blocked_group


def test_fresh_blocked_unclaimed_row_stays_out_of_needs_attention(
    lithos_lens_config_env: Path,
) -> None:
    """Slice-3 acceptance (the false-positive half): an ordinary open
    predecessor is correct waiting, so a fresh blocked unclaimed row stays in
    Blocked — and an empty attention list shows the healthy stripe rather than
    being hidden."""
    fake = TaskFakeLithosClient()
    # Drop the deliberately-ancient fixture so nothing else is flagged.
    fake.tasks = [task for task in fake.tasks if task.id != "open-old"]
    fake.ready_ids = set()
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
        response = client.get("/tasks?status=open&since=2026-04-01")

    assert response.status_code == 200
    text = response.text
    blocked_group = text[text.index('data-task-group="blocked"') :]
    blocked_group = blocked_group[: blocked_group.index("</article>")]
    assert 'data-task-id="open-unclaimed"' in blocked_group
    assert "data-attention-rule" not in text
    attention = text[text.index('data-task-group="attention"') :]
    attention = attention[: attention.index("</article>")]
    assert "data-attention-healthy" in attention
    assert "All systems healthy" in unescape(attention)


def test_healthy_stripe_is_withheld_when_a_frontier_read_failed(
    lithos_lens_config_env: Path,
) -> None:
    """Alerting integrity: rules 1 and 2 fire ONLY from the blocked frontier,
    so when that read fails the empty attention list means "nothing was
    examined", not "nothing is wrong". The stripe must withhold the claim."""
    fake = TaskFakeLithosClient()
    # Drop the ancient fixture so the list is genuinely empty.
    fake.tasks = [task for task in fake.tasks if task.id != "open-old"]

    async def failing_task_blocked(**_: Any) -> list[BlockedTaskRecord]:
        raise RuntimeError("blocked frontier unavailable")

    fake.task_blocked = failing_task_blocked  # type: ignore[method-assign]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?status=open&since=2026-04-01")

    assert response.status_code == 200
    text = response.text
    assert "All systems healthy" not in text
    assert "data-attention-healthy" not in text
    assert "data-attention-unknown" in text
    assert "Cannot assess" in unescape(text)
    # The existing error banner still explains what went wrong.
    assert "Some task data could not be loaded." in text


def test_a_frontier_row_no_read_placed_withholds_the_system_wide_stripe(
    lithos_lens_config_env: Path,
) -> None:
    """The ready frontier returned a task the master open list did not, the one
    retry saw the same thing, and no resolved window explains it either.

    The page still makes no claim that a task is MISSING (it cannot know that —
    see the test below, where the resolved window returns the row and it
    renders). What it may not do is assert "All systems healthy — 0 issues":
    the ghost reached no section, so the attention rules never evaluated it and
    it may be a newly-ready open task the open read missed. The system-wide
    claim gives way to the view-scoped one; no banner is raised, because
    nothing else about the load is degraded.
    """
    # The board that shows the stripe today (see the test above), plus one
    # ghost row on the ready frontier.
    fake = TaskFakeLithosClient()
    fake.tasks = [task for task in fake.tasks if task.id != "open-old"]
    fake.ready_ids = set()
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

    async def ghost_task_ready(**_: Any) -> list[TaskRecord]:
        # Returned by the frontier, absent from the open list — every time, so
        # the single retry cannot settle it.
        return [TaskRecord(id="just-closed", title="Just closed", status="open")]

    fake.task_ready = ghost_task_ready  # type: ignore[method-assign]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?status=open&since=2026-04-01")

    assert response.status_code == 200
    text = response.text
    # No notice asserting an absence the page cannot know (the row can be
    # rendered under Completed — see the test below).
    assert "not shown in any section below" not in unescape(text)
    assert "data-frontier-skew-banner" not in text
    # The reconciliation surface stays away: no row moved, so none is marked.
    assert "data-reconciliation-banner" not in text
    # The affirmative system-wide claim is withheld…
    assert "data-attention-healthy" not in text
    assert "All systems healthy" not in unescape(text)
    # …and, with no failed/truncated read to point at, the honest fallback is
    # the view-scoped line rather than "Cannot assess … see the notice above",
    # which would name a notice this page deliberately does not raise.
    assert "data-attention-unknown" not in text
    assert "data-attention-scoped" in text
    assert "Nothing needs attention in this view." in unescape(text)


def test_a_frontier_only_row_can_render_in_the_resolved_window(
    lithos_lens_config_env: Path,
) -> None:
    """The counterexample to "frontier-only means it renders nowhere": the
    ready frontier returns a task the open list does not BECAUSE it just
    completed — and the completed window of the same load returns it.

    The row is on the page, under Completed. Nothing on that page may say it is
    missing.
    """
    fake = TaskFakeLithosClient()
    fake.ready_ids = set()

    async def ghost_task_ready(**_: Any) -> list[TaskRecord]:
        # An id the open read never returns (it is completed) and the
        # completed window does — every time, so the retry cannot settle it.
        return [TaskRecord(id="done-recent", title="Recently completed task")]

    fake.task_ready = ghost_task_ready  # type: ignore[method-assign]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    text = response.text
    completed = text[text.index('data-task-group="completed"') :]
    completed = completed[: completed.index("</article>")]
    assert 'data-task-id="done-recent"' in completed
    assert "not shown in any section below" not in unescape(text)
    assert "data-frontier-skew-banner" not in text
    # The board is not empty and never says it is.
    assert "No tasks in this window" not in unescape(text)
    # And because a read of this generation PLACED the row, the health claim
    # is not withheld on its account (contrast the test above).
    assert "data-attention-unknown" not in text


def test_healthy_stripe_is_withheld_when_the_frontier_truncated(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same claim, other cause: a truncated frontier leaves rows unexamined in
    the Not-classified tail, which is never promoted — so "0 issues" would be
    asserting health over rows nobody looked at."""
    fake = TaskFakeLithosClient()
    fake.tasks = [task for task in fake.tasks if task.id != "open-old"]
    fake.tasks.append(
        TaskRecord(
            id="open-fresh",
            title="Fresh ready task",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=5),
        )
    )
    fake.ready_ids = {"open-unclaimed", "open-fresh"}
    monkeypatch.setenv("LITHOS_LENS_TASKS_FRONTIER_LIMIT", "1")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?status=open&since=2026-04-01")

    assert response.status_code == 200
    text = response.text
    assert "Section counts are approximate." in text  # truncation banner
    assert "All systems healthy" not in text
    assert "data-attention-unknown" in text


def test_only_the_truncated_side_marks_its_counter_on_the_page(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Story 28 on the rendered board: the ready read caps at the limit while
    the blocked read answers in full, so the Ready and Needs-attention cards
    carry the "at least this many" marking and the Blocked card — an exact
    count Lithos answered completely — does not."""
    fake = TaskFakeLithosClient()
    # Drop the stale fixture (it fires the age rule) and add a second and third
    # unclaimed ready row, so the ready frontier has 3 rows against a limit of 2.
    fake.tasks = [task for task in fake.tasks if task.id != "open-old"]
    fake.tasks.extend(
        TaskRecord(
            id=task_id,
            title=title,
            status="open",
            created_by="planner",
            created_at=_ago(minutes=5),
        )
        for task_id, title in (
            ("open-fresh", "Fresh ready task"),
            ("open-spare", "Spare ready task"),
            ("open-blocked", "Blocked open task"),
        )
    )
    fake.ready_ids = {"open-unclaimed", "open-fresh", "open-spare"}
    fake.blocked = {
        "open-blocked": (
            BlockerRecord(
                kind="task",
                task_id="open-unclaimed",
                type="blocks",
                status="open",
                message="Waiting on the unclaimed task.",
            ),
        )
    }
    monkeypatch.setenv("LITHOS_LENS_TASKS_FRONTIER_LIMIT", "2")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?status=open&since=2026-04-01")

    assert response.status_code == 200
    text = unescape(response.text)
    # The tail and its banner are there — and the banner names the ONE side.
    assert "Not classified" in text
    assert "Section counts are approximate." in text
    assert "The Lithos ready frontier truncated at 2" in text
    assert "blocked frontier" not in text
    # The ready-fed counters are marked; the blocked count stands as exact.
    assert 'data-approximate-count="ready"' in text
    assert 'data-approximate-count="attention"' in text
    assert 'data-approximate-count="blocked"' not in text


def test_each_counters_note_names_only_the_sides_that_feed_it(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With BOTH frontiers capped, the marking is right and the EXPLANATION has
    to be too.

    ``attention`` is fed by both reads — a promoted row can come from either
    frontier — but ``ready`` is fed only by the ready read and ``blocked`` only
    by the blocked one. Naming the board-wide list on every marked card told
    the operator that a capped BLOCKED read is why the READY count is
    approximate, which is the exact conflation this slice exists to undo,
    restated in prose after the number had been got right.

    The one-sided case above cannot catch this: with a single capped side the
    board-wide list and the per-counter list are the same tuple.
    """
    fake = TaskFakeLithosClient()
    fake.tasks = [task for task in fake.tasks if task.id != "open-old"]
    fake.tasks.extend(
        TaskRecord(
            id=task_id,
            title=title,
            status="open",
            created_by="planner",
            created_at=_ago(minutes=5),
        )
        for task_id, title in (
            ("ready-a", "Ready A"),
            ("ready-b", "Ready B"),
            ("ready-c", "Ready C"),
            ("blocked-a", "Blocked A"),
            ("blocked-b", "Blocked B"),
            ("blocked-c", "Blocked C"),
        )
    )
    fake.ready_ids = {"ready-a", "ready-b", "ready-c"}
    fake.blocked = {
        task_id: (
            BlockerRecord(
                kind="task",
                task_id="ready-a",
                type="blocks",
                status="open",
                message="Waiting.",
            ),
        )
        for task_id in ("blocked-a", "blocked-b", "blocked-c")
    }
    monkeypatch.setenv("LITHOS_LENS_TASKS_FRONTIER_LIMIT", "2")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?status=open&since=2026-04-01")

    assert response.status_code == 200
    text = unescape(response.text)
    notes = dict(re.findall(r'data-approximate-count="(\w+)">([^<]*)', text))
    # Both sides capped, so all three counters are marked...
    assert set(notes) == {"ready", "blocked", "attention"}
    # ...but each names only what feeds it.
    assert "the ready frontier truncated" in notes["ready"]
    assert "blocked" not in notes["ready"]
    assert "the blocked frontier truncated" in notes["blocked"]
    assert "ready" not in notes["blocked"]
    # Needs attention is the one genuinely fed by both.
    assert "the ready and blocked frontiers truncated" in notes["attention"]


def test_stale_open_row_is_flagged_with_its_reason_chip(
    lithos_lens_config_env: Path,
) -> None:
    """The age rules reach the page too: the ancient fixture is promoted out of
    Ready with a ``stale-open`` chip and a supporting fact, and the header
    counter agrees with the rendered list."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?status=open&since=2026-04-01")

    assert response.status_code == 200
    text = response.text
    attention = text[text.index('data-task-group="attention"') :]
    attention = attention[: attention.index("</article>")]
    assert 'data-task-id="open-old"' in attention
    assert 'data-attention-rule="stale-open"' in attention
    assert "with no resolution" in unescape(attention)
    assert "<strong data-attention-count>1</strong>" in text
    # It left Ready: only the fresh unclaimed fixture remains there.
    ready_group = text[text.index('data-task-group="ready"') :]
    ready_group = ready_group[: ready_group.index("</article>")]
    assert 'data-task-id="open-old"' not in ready_group
    assert 'data-task-id="open-unclaimed"' in ready_group


def test_only_dispatch_triggered_ready_work_reaches_needs_attention(
    lithos_lens_config_env: Path,
) -> None:
    """Rule 6 acceptance on the page: four ready rows of the same age, and only
    the one a fleet was expected to pick up is promoted.

    The live corpus is mostly untagged ready work (loom dispatches on
    ``trigger:``, robot-companion tasks wait for a robot day), so judging every
    ready row emptied Ready and made Needs attention the de-facto Ready list.
    """
    fake = TaskFakeLithosClient()
    # Drop the deliberately-ancient fixture: it fires stale-open, and this test
    # is counting rule-6 rows.
    fake.tasks = [task for task in fake.tasks if task.id != "open-old"]
    fake.tasks.extend(
        TaskRecord(
            id=task_id,
            title=title,
            status="open",
            created_by="planner",
            created_at=created_at,
            tags=tags,
        )
        for task_id, title, tags, created_at in (
            (
                "dispatch-old",
                "Dispatched and unpicked",
                ("trigger:story-develop",),
                _ago(hours=3),
            ),
            (
                "untagged-old",
                "Nobody promised to pick this up",
                ("area:docs",),
                _ago(hours=3),
            ),
            (
                "dispatch-fresh",
                "Dispatched a moment ago",
                ("trigger:story-develop",),
                _ago(minutes=20),
            ),
            (
                "dispatch-claimed",
                "Dispatched and picked up",
                ("trigger:story-develop",),
                _ago(hours=3),
            ),
        )
    )
    fake.claims["dispatch-claimed"] = (
        ClaimRecord(
            agent="worker-b", aspect="implementation", expires_at=_ahead(hours=6)
        ),
    )
    fake.ready_ids = {
        "open-unclaimed",
        "dispatch-old",
        "untagged-old",
        "dispatch-fresh",
        "dispatch-claimed",
    }

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?status=open&since=2026-04-01")

    assert response.status_code == 200
    text = response.text
    assert text.count('data-attention-rule="ready-unclaimed"') == 1
    attention = _group(text, "attention")
    assert 'data-task-id="dispatch-old"' in attention
    # The supporting fact names the trigger tag, so the operator can see WHICH
    # fleet was expected to pick the task up.
    assert "trigger:story-develop" in unescape(attention)
    assert "<strong data-attention-count>1</strong>" in text
    # The other three stay where they belong: untagged and fresh work is still
    # Ready, and the claimed row is In progress.
    ready_group = _group(text, "ready")
    for task_id in ("untagged-old", "dispatch-fresh", "open-unclaimed"):
        assert f'data-task-id="{task_id}"' in ready_group
    assert 'data-task-id="dispatch-claimed"' in _group(text, "in_progress")


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
    """T1-S7: a deep link to a deleted task is answered by Lithos's own
    ``task_not_found`` envelope, not by failing to find it in three scanned
    lists — so the panel says so, and no ``lithos_task_list`` call is made."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/no-such-task")

    assert response.status_code == 200
    assert "Lithos has no task with this id" in response.text
    # `find_task` is deleted: the page addresses the one task directly.
    assert fake.get_calls == ["no-such-task"]
    assert fake.list_calls == []


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


def test_dead_end_surfaces_even_when_claims_are_unknown(
    lithos_lens_config_env: Path,
) -> None:
    """Regression, page level: a proven dead end is not hidden by missing claims.

    The degraded groups were excluded from promotion wholesale, so a task whose
    blocker was CANCELLED sat quietly in Claims unknown with no reason chip —
    even though the frontier proving it dead had answered; only the claim data
    was missing. It now renders in Needs attention with its ``unsatisfiable``
    chip AND its claims-unknown chip: both statements are true at once.
    """

    class NoClaimsClient(TaskFakeLithosClient):
        async def list_tasks(self, **kwargs: Any) -> list[TaskRecord]:
            rows = await super().list_tasks(**kwargs)
            return [replace(task, claims=None) for task in rows]

    fake = NoClaimsClient()
    fake.blocked = {
        "open-old": (
            BlockerRecord(
                kind="blocker_unsatisfiable",
                task_id="open-claimed",
                type="blocks",
                status="cancelled",
                message="Blocking predecessor open-claimed was cancelled;",
            ),
        )
    }

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?status=open&since=2026-04-01")

    assert response.status_code == 200
    text = response.text
    attention = _group(text, "attention")
    assert 'data-task-id="open-old"' in attention
    assert 'data-attention-rule="unsatisfiable"' in attention
    # Promoted, but still honest about what Lens does not know.
    assert "claims unknown" in attention
    # Single placement holds across the group boundary.
    assert 'data-task-id="open-old"' not in _group(text, "claims_unknown")


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
    # Both windows are on the bar; this one is the RESOLVED window, and the
    # labels are what keep them apart (see the label test below).
    assert "Resolved since (terminal only, by resolution)" in response.text
    # The created window is off — nothing was asked of it, so no chip claims it.
    assert "data-active-filter-created-since" not in response.text

    # The window is a resolved_since push, never a created-at `since`.
    terminal_calls = [
        call for call in fake.list_calls if call["status"] in {"completed", "cancelled"}
    ]
    assert terminal_calls
    assert all(call["resolved_since"] == "2026-04-01" for call in terminal_calls)
    assert all(call["since"] is None for call in terminal_calls)


# --- Created since (T2 UX pass) ---------------------------------------------
#
# The second date window: "what came in since Monday", which the bar could not
# ask before. It windows EVERY section by created_at, client-side over rows
# already loaded, and leaves ``since`` — the resolved window, and the only
# filter pushed upstream — doing exactly what it did.


def _section_count(text: str, section: str) -> int:
    """The number in one section's header (``<h2>Label</h2><span>N</span>``)."""
    match = re.search(r"<span>(\d+)</span>", _group(text, section))
    assert match is not None, f"no header count for {section}"
    return int(match.group(1))


def _card_count(text: str, label: str) -> int:
    """The number on the situation card titled ``label``."""
    card = text.split(f"<span>{label}</span>", 1)[1].split("</a>", 1)[0]
    match = re.search(r"<strong[^>]*>(\d+)</strong>", card)
    assert match is not None, f"no card count for {label}"
    return int(match.group(1))


def test_created_since_windows_every_section_and_its_counts(
    lithos_lens_config_env: Path,
) -> None:
    """The gap ``since`` never closed, and the acceptance's "every section".

    ``Old open task`` was created in 2025 and ``Unclaimed open task`` minutes
    ago, and both are open — so the only thing that can tell them apart is the
    created window. The gates go through their OWN assembly (``load_gates``
    off ``visible_open``) and the terminal sections through the resolved reads,
    so each is placed old-and-new and checked by title AND by count: a
    regression that narrowed the rows but left a summary stale, or that built
    Gates from the unfiltered snapshot, would otherwise pass.
    """
    fake = TaskFakeLithosClient()
    # Deliberately ``ci`` gates: a human gate that has waited too long is
    # promoted into Needs attention (§5.2.2) and would leave this section for
    # a reason that has nothing to do with the created window.
    _add_gate(fake, "gate-old", gate_type="ci", created_at="2025-01-01T10:00:00+00:00")
    _add_gate(fake, "gate-new", gate_type="ci")
    yesterday = (_NOW - timedelta(days=1)).date().isoformat()

    with _client(lithos_lens_config_env, fake) as client:
        before = client.get("/tasks?since=2026-04-01")
        after = client.get(f"/tasks?since=2026-04-01&created_since={yesterday}")

    assert before.status_code == 200
    assert after.status_code == 200
    unfiltered, narrowed = before.text, after.text

    # The open row created in 2025 is on the board, in Needs attention (it is
    # stale), and the one created minutes ago is beside it in Ready.
    assert "Old open task" in _group(unfiltered, "attention")
    assert _section_count(unfiltered, "attention") == 1
    assert _section_count(unfiltered, "ready") == 1
    # Both gates render, and both terminal windows hold their row.
    assert "Gate gate-old" in _group(unfiltered, "gates")
    assert _section_count(unfiltered, "gates") == 2
    assert _section_count(unfiltered, "completed") == 1
    assert _section_count(unfiltered, "cancelled") == 1

    # Narrowed: every section drops the rows created before the date, and the
    # rows created after it stay exactly where they were.
    assert "Old open task" not in narrowed
    assert _section_count(narrowed, "attention") == 0
    assert "Unclaimed open task" in _group(narrowed, "ready")
    assert _section_count(narrowed, "ready") == 1
    assert "Gate gate-old" not in narrowed
    assert "Gate gate-new" in _group(narrowed, "gates")
    assert _section_count(narrowed, "gates") == 1
    # Terminal rows too: both were created in April, so both windows empty even
    # though the RESOLVED window still admits them (they are in ``since``).
    assert "Recently completed task" not in narrowed
    assert "Recently cancelled task" not in narrowed
    assert _section_count(narrowed, "completed") == 0
    assert _section_count(narrowed, "cancelled") == 0

    # The situation cards count the same narrowed board the sections render —
    # a card is a link to its section, so the two disagreeing is a lie by one
    # click.
    for label, section in (
        ("Needs attention", "attention"),
        ("Ready", "ready"),
        ("Gates", "gates"),
        ("Recently completed", "completed"),
        ("Recently cancelled", "cancelled"),
    ):
        assert _card_count(unfiltered, label) == _section_count(unfiltered, section)
        assert _card_count(narrowed, label) == _section_count(narrowed, section)


def test_created_since_changes_no_lithos_read(
    lithos_lens_config_env: Path,
) -> None:
    """It is applied over the loaded snapshot, so the call log is byte-identical
    with and without it — the terminal reads keep pushing ``since`` as
    ``resolved_since`` and nothing pushes a created window upstream."""
    without = TaskFakeLithosClient()
    with_window = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, without) as client:
        client.get("/tasks?since=2026-04-01")
    with _client(lithos_lens_config_env, with_window) as client:
        client.get("/tasks?since=2026-04-01&created_since=2026-04-21")

    assert with_window.list_calls == without.list_calls
    terminal_calls = [
        call
        for call in with_window.list_calls
        if call["status"] in {"completed", "cancelled"}
    ]
    assert terminal_calls
    assert all(call["resolved_since"] == "2026-04-01" for call in terminal_calls)
    assert all(call["since"] is None for call in terminal_calls)


def test_a_terminal_row_must_pass_both_windows(
    lithos_lens_config_env: Path,
) -> None:
    """Both active at once: the resolved window admits two terminal rows and the
    created window keeps only the one created late enough."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01&created_since=2026-04-21")

    assert response.status_code == 200
    # Created 2026-04-21, resolved 2026-04-23 — inside both windows.
    assert "Recently cancelled task" in response.text
    # Created 2026-04-20, resolved 2026-04-22 — inside the resolved window only.
    assert "Recently completed task" not in response.text
    # Outside both, as it was before this filter existed.
    assert "Old completed task" not in response.text


# The whole preserved-filter vocabulary, active at once, for the two tests that
# have to prove a link carries it: one window must not ride at another's
# expense, and "preserves every other filter" is only shown by exercising them
# all. ``epic-1`` below is a real open epic scoping both fixture rows, so the
# scope is APPLIED rather than merely echoed.
_FULL_FILTER_QUERY = (
    "status=open&project=influx&agent=planner&tag=area:docs&epic=epic-1"
    "&since=2026-04-01&created_since=2026-04-21"
)


def _epic_scoped_fake() -> TaskFakeLithosClient:
    """The default board plus an epic over one recent and one ancient row.

    Both rows carry the project and tag the full query filters on and were
    created by ``planner``, so nothing but the created window separates them.
    """
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        TaskRecord(
            id="epic-1",
            title="The initiative",
            status="open",
            created_by="planner",
            created_at=_ago(hours=3),
            task_type="epic",
        )
    )
    fake.tasks.append(
        TaskRecord(
            id="influx-old",
            title="Old influx task",
            status="open",
            created_by="planner",
            created_at="2025-01-01T10:00:00+00:00",
            tags=("project:influx", "area:docs"),
        )
    )
    fake.ready_ids.add("influx-old")
    fake.children["epic-1"] = ["influx-old", "open-claimed"]
    return fake


def test_both_date_windows_ride_every_generated_link(
    lithos_lens_config_env: Path,
) -> None:
    """The URL-carries-both criterion, checked on the links the board WRITES.

    Every generated tasks URL rebuilds its query from one allowlist
    (``_PRESERVED_FILTER_KEYS``), so a key missing from it is dropped by the
    cards, the rows, the tag chips and the panel alike — silently widening the
    board one click later. The two windows carry different dates so neither
    assertion can be satisfied by the other's value.
    """
    fake = _epic_scoped_fake()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(f"/tasks?{_FULL_FILTER_QUERY}")

    assert response.status_code == 200
    text = unescape(response.text)
    # Matched on the MARKUP each builder emits, not on the query strings, so a
    # link that legitimately drops a key (a chip's own clear link) cannot be
    # mistaken for one of these families.
    families = {
        # Situation cards: each links to the section whose count it shows.
        "card": re.findall(r'<a class="metric-card" href="([^"]*)"', text),
        # Row tag chips: the clicked tag replaces the filtered one.
        "tag": re.findall(r'<a class="tag-chip[^"]*" href="([^"]*)"', text),
        # Epic chips: the strip's scope links.
        "epic": re.findall(r'href="([^"]*)"[^>]*data-epic-chip=', text),
        # One detail link per row…
        "detail": re.findall(r'href="(/tasks/open-claimed[^"]*)"', text),
        # …and the side-panel fragment the row's click handler fetches.
        "panel": re.findall(r'data-panel-url="([^"]*)"', text),
    }
    for family, links in families.items():
        assert links, f"no {family} links on the board"
        for href in links:
            assert "created_since=2026-04-21" in href, (family, href)
            assert "since=2026-04-01" in href, (family, href)


def test_created_since_renders_a_chip_that_clears_only_itself(
    lithos_lens_config_env: Path,
) -> None:
    """An active created window hides open rows, so the strip must name it — and
    removing it must leave EVERY other filter standing (the resolved window
    included), which is only shown with the whole vocabulary active."""
    fake = _epic_scoped_fake()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(f"/tasks?{_FULL_FILTER_QUERY}")
        text = response.text
        assert 'data-active-filter-created-since="2026-04-21"' in text
        assert "Created since 21/04/2026" in text
        assert "Old influx task" not in text
        href = unescape(
            re.findall(r'href="([^"]+)"[^>]*data-active-filter-created-since=', text)[0]
        )
        cleared = client.get(href)

    assert "created_since" not in href
    params = QueryParams(href.split("?", 1)[1])
    # Everything else the board was scoped by rides through the removal.
    assert params.getlist("status") == ["open"]
    assert params.getlist("project") == ["influx"]
    assert params.get("agent") == "planner"
    assert params.getlist("tag") == ["area:docs"]
    assert params.get("epic") == "epic-1"
    assert params.get("since") == "2026-04-01"

    assert cleared.status_code == 200
    assert "data-active-filter-created-since" not in cleared.text
    # Only the created window came off: the row it hid is back, every other
    # filter is still drawn as applied, and the rows they exclude stay out.
    assert "Old influx task" in cleared.text
    assert 'data-active-filter-tag="area:docs"' in cleared.text
    assert "Recently completed task" not in cleared.text
    assert "Unclaimed open task" not in cleared.text


def _date_filter_labels(text: str) -> dict[str, str]:
    """The filter bar's two date ``<label>`` blocks, keyed by their input name.

    Sliced out of ``.filter-dates`` — the one cell that holds the pair — so the
    assertions below are about CONTROLS in a shared layout rather than about
    strings occurring somewhere on a large page.
    """
    block = text.split('<div class="filter-dates">', 1)[1].split("</div>", 1)[0]
    labels = re.findall(r"<label>(.*?)</label>", block, re.S)
    named = {}
    for label in labels:
        match = re.search(r'name="([^"]+)"', label)
        assert match is not None, f"date label with no named control: {label}"
        named[match.group(1)] = label
    return named


def test_the_two_date_labels_say_which_rows_they_window(
    lithos_lens_config_env: Path,
) -> None:
    """The reason the two were confused in the first place: a label naming only
    its date says nothing about which rows it narrows.

    Asserted structurally — each phrase must be in the label that WRAPS the
    control it describes, and both controls must be the pair inside
    ``.filter-dates`` — so deleting an input while leaving its text on the page
    fails here. (That the pair then renders side by side is browser truth; the
    e2e suite measures it.)
    """
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        text = client.get("/tasks?since=2026-04-01&created_since=2026-04-21").text
        blank = client.get("/tasks?since=2026-04-01").text

    labels = _date_filter_labels(text)
    assert set(labels) == {"created_since", "since"}

    created = labels["created_since"]
    assert "Created since (open + terminal, by creation)" in created
    assert 'type="text" name="created_since"' in created
    # The ACTIVE window is what the field shows, in the bar's display format,
    # with the native picker carrying the same date in ISO.
    assert 'value="21/04/2026"' in created
    assert 'type="date" data-native-date value="2026-04-21"' in created

    resolved = labels["since"]
    assert "Resolved since (terminal only, by resolution)" in resolved
    assert 'type="text" name="since"' in resolved
    assert 'value="01/04/2026"' in resolved
    assert 'type="date" data-native-date value="2026-04-01"' in resolved

    # No created window: the field renders EMPTY rather than pre-filled with a
    # default the board is not applying (the resolved field, which always has a
    # value, still shows its own).
    blank_labels = _date_filter_labels(blank)
    assert (
        'name="created_since" data-display-date value=""'
        in (blank_labels["created_since"])
    )
    assert 'value="01/04/2026"' in blank_labels["since"]


def test_unparseable_created_since_renders_the_board_unwindowed(
    lithos_lens_config_env: Path,
) -> None:
    """Handled as an unparseable ``since`` is: discarded, never a 500. The
    fallback is this field's own default — no window — so a typo cannot narrow
    the open sections under a date the operator never typed."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01&created_since=not-a-date")

    assert response.status_code == 200
    assert "Old open task" in response.text
    assert "data-active-filter-created-since" not in response.text


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
    # Open rows are unaffected — they are not windowed on resolution. Asserted
    # by KIND rather than by a literal stamp: T1-S3 moved the open fixtures to
    # now-relative timestamps so its age rules read them the way it intends.
    assert (
        '<time datetime="2025-01-01T10:00:00+00:00" data-timestamp-kind="created">'
        "created 2025-01-01 10:00</time>" in response.text
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
    """A server whose frontier calls fail the way a tool-less one does.

    MCP answers an unknown tool with an error result, which is all Lens ever
    sees of a pre-0.4 server — and all it needs to render the flat board.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.frontier_calls = 0

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
    """Regression: the summary cards follow the RENDER, not the read verdict.

    Keyed on anything else they showed Ready/Blocked zeros — three counts
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


def test_dashboard_renders_flat_when_the_frontier_reads_fail(
    lithos_lens_config_env: Path,
) -> None:
    """Page level: no usable frontier degrades instead of breaking.

    Covers the withdrawn pre-0.4 case too — this fake IS a server without the
    tools, and Lens treats it as what it observes: two reads that did not
    answer. The board falls back to the flat open list with the read errors on
    screen; nothing diagnoses a version.
    """
    fake = NoFrontierClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
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
    # Never silent, and never dressed up as a version story.
    assert "Some task data could not be loaded" in response.text
    assert "Could not load the ready frontier." in response.text
    assert "Could not load the blocked frontier." in response.text
    assert "Lithos 0.4" not in response.text
    assert "data-graph-unavailable" not in response.text
    assert "data-healthy-stripe" not in response.text
    # Terminal sections are unaffected — a different read entirely.
    assert "Recently completed task" in response.text


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
    self-consistent, claims came back for every row — and, since T1-S3 moved
    the stripe into the Needs-attention section, nothing needs attention."""
    fake = TaskFakeLithosClient()
    # ``open-old`` exists precisely to fire the stale-open rule; drop it so the
    # attention list is genuinely empty and the stripe can make its claim.
    fake.tasks = [task for task in fake.tasks if task.id != "open-old"]

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


def test_the_graph_surface_returns_as_soon_as_the_frontier_answers(
    lithos_lens_config_env: Path,
) -> None:
    """A degraded render never sticks: every load attempts the frontier.

    This used to be a property of the re-probe window that cached the
    "tools are missing" verdict. With that detection withdrawn there is no
    verdict to expire — the reads are simply made again — and the property it
    protected still has to hold: the operator gets the graph back on the next
    render, with no restart and nothing to reset.
    """

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

        def recover(self) -> None:
            """The frontier starts answering (restored, or the server upgraded)."""
            self.recovered = True

    fake = RecoveringClient()

    with _client(lithos_lens_config_env, fake) as client:
        degraded = client.get("/tasks?since=2026-04-01")
        calls_while_degraded = fake.frontier_calls
        fake.recover()
        healed = client.get("/tasks?since=2026-04-01")

    assert 'data-task-group="open"' in degraded.text
    assert "Could not load the ready frontier." in degraded.text
    # The next render asked again, unconditionally…
    assert fake.frontier_calls > calls_while_degraded
    # …and the graph sections are back.
    assert "Could not load the ready frontier." not in healed.text
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


def _with_epic(fake: TaskFakeLithosClient) -> TaskFakeLithosClient:
    """Give the fake an epic over eight subtree tasks, five of them completed.

    The three open children are placed on the ready frontier (the fake is the
    readiness oracle) so they classify normally instead of tripping the
    read-skew surface, and they are FRESH — younger than both T1-S3 age
    thresholds (stale-open at 7 days, ready-unclaimed at 60 minutes). An older
    child would be promoted into Needs attention, which is correct behaviour
    but moves the rows these scoping tests are looking for.
    """
    fake.tasks.append(
        TaskRecord(
            id="epic-1",
            title="Storage migration",
            status="open",
            task_type="epic",
            created_by="planner",
            created_at="2026-04-27T10:00:00+00:00",
        )
    )
    fake.tasks.extend(
        TaskRecord(
            id=f"epic-child-{n}",
            title=f"Epic child {n}",
            status="completed" if n <= 5 else "open",
            created_by="worker",
            created_at="2026-04-24T10:00:00+00:00" if n <= 5 else _ago(minutes=5),
            resolved_at="2026-04-25T10:00:00+00:00" if n <= 5 else "",
        )
        for n in range(1, 9)
    )
    fake.children["epic-1"] = [f"epic-child-{n}" for n in range(1, 9)]
    fake.ready_ids |= {"epic-child-6", "epic-child-7", "epic-child-8"}
    return fake


def _group(text: str, section: str) -> str:
    """The rendered markup of one dashboard section.

    Sliced to the NEXT section rather than to the first ``</article>``: rows
    are articles themselves (row.html), so the old cut stopped after the first
    row and silently hid the rest from every assertion built on this helper.
    """
    start = text.index(f'data-task-group="{section}"')
    rest = text[start + 1 :]
    end = rest.find('data-task-group="')
    return text[start:] if end == -1 else text[start : start + 1 + end]


def test_epic_strip_chip_shows_recursive_subtree_progress(
    lithos_lens_config_env: Path,
) -> None:
    """Slice-5 acceptance: an epic with 5 of 8 subtree tasks done renders a
    ``5/8`` chip — and the epic itself never becomes a task row."""
    fake = _with_epic(TaskFakeLithosClient())

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?status=open&since=2026-04-01")

    assert response.status_code == 200
    text = response.text
    strip = text[text.index("data-epic-strip") :]
    strip = strip[: strip.index("</section>")]
    assert 'data-epic-chip="epic-1"' in strip
    assert "Storage migration" in strip
    assert ">5/8<" in strip
    assert 'max="8" value="5"' in strip
    # The chip links the board at that epic; the epic is not a section row.
    assert "epic=epic-1" in strip
    assert 'data-task-id="epic-1"' not in text


def test_clicking_an_epic_chip_scopes_the_sections_to_its_descendants(
    lithos_lens_config_env: Path,
) -> None:
    """Slice-5 acceptance: following the chip's ``?epic=`` link leaves only the
    epic's descendants on the board, and the active chip links back out so the
    scope can be cleared."""
    fake = _with_epic(TaskFakeLithosClient())

    with _client(lithos_lens_config_env, fake) as client:
        unscoped = client.get("/tasks?since=2026-04-01")
        scoped = client.get("/tasks?since=2026-04-01&epic=epic-1")

    assert unscoped.status_code == 200
    assert "Unclaimed open task" in _group(unscoped.text, "ready")

    assert scoped.status_code == 200
    text = scoped.text
    ready = _group(text, "ready")
    assert "Epic child 6" in ready
    # Everything outside the epic subtree is gone from every section.
    assert "Unclaimed open task" not in text
    assert "Claimed open task" not in text
    assert "Recently completed task" not in text
    assert "Epic child 1" in _group(text, "completed")
    # The active chip is marked and toggles the scope off.
    strip = text[text.index("data-epic-strip") :]
    strip = strip[: strip.index("</section>")]
    assert "epic-chip-selected" in strip
    assert "epic=epic-1" not in strip
    # A filter submit keeps the scope instead of silently dropping it.
    assert '<input type="hidden" name="epic" value="epic-1">' in text


def test_stale_epic_scope_shows_the_whole_board_with_a_notice(
    lithos_lens_config_env: Path,
) -> None:
    """A bookmark naming an epic that is no longer open must not render an
    unexplained empty board: everything shows, with the reason stated."""
    fake = _with_epic(TaskFakeLithosClient())

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01&epic=epic-gone")

    assert response.status_code == 200
    text = response.text
    assert "data-epic-scope-missing" in text
    assert "Unclaimed open task" in _group(text, "ready")


def test_epic_scope_survives_tag_and_detail_navigation(
    lithos_lens_config_env: Path,
) -> None:
    """The scope is a live filter param, so the links generated on a scoped
    board carry it (an epic-scoped tag click stays inside the epic)."""
    fake = _with_epic(TaskFakeLithosClient())
    fake.children["epic-1"].append("open-unclaimed")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01&epic=epic-1")

    assert response.status_code == 200
    row = response.text[response.text.index('data-task-id="open-unclaimed"') :]
    row = row[: row.index("</article>")]
    assert "epic=epic-1" in row


def _card(text: str, anchor: str) -> str:
    """The situation card whose link carries ``anchor`` (an href or fragment)."""
    return text.split(anchor)[1].split("</a>")[0]


def test_situation_cards_keep_the_active_epic_scope_and_filters(
    lithos_lens_config_env: Path,
) -> None:
    """Reviewer repro (c-002): the five situation cards are the dashboard's
    primary navigation. On a scoped board they must stay inside the scope —
    clicking a count used to drop ``epic=`` and show every epic again."""
    fake = _with_epic(TaskFakeLithosClient())

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01&epic=epic-1&tag=project:influx")

    assert response.status_code == 200
    grid = response.text.split('aria-label="Current situation"')[1]
    grid = grid[: grid.index("</section>")]
    hrefs = [
        chunk.split('"')[0] for chunk in grid.split('class="metric-card" href="')[1:]
    ]
    # Seven cards since T1-S4 added Gates (T1-S3 added Needs attention); every
    # one keeps the scope.
    assert len(hrefs) == 7
    for href in hrefs:
        assert "epic=epic-1" in href
        assert "tag=project%3Ainflux" in href
        assert "since=2026-04-01" in href
    # Each card still narrows to its own status (five open-side cards now:
    # Needs attention, In progress, Ready, Blocked, Gates).
    assert sum("status=open" in href for href in hrefs) == 5
    assert sum("status=completed" in href for href in hrefs) == 1
    assert sum("status=cancelled" in href for href in hrefs) == 1


def test_in_progress_card_claims_count_follows_the_scope(
    lithos_lens_config_env: Path,
) -> None:
    """Reviewer repro (c-003): the claims line under the In-progress count must
    describe the same rows as the count above it. The fake's Lithos-wide stat
    reports one open claim on ``open-claimed``, which is outside the epic."""
    fake = _with_epic(TaskFakeLithosClient())

    with _client(lithos_lens_config_env, fake) as client:
        unscoped = client.get("/tasks?since=2026-04-01")
        scoped = client.get("/tasks?since=2026-04-01&epic=epic-1")

    unscoped_card = _card(unscoped.text, "#task-group-in_progress")
    assert "<strong>1</strong>" in unscoped_card
    assert "1 active claim<" in unscoped_card

    scoped_card = _card(scoped.text, "#task-group-in_progress")
    # No claimed task inside the epic: both numbers must read zero.
    assert "<strong>0</strong>" in scoped_card
    assert "0 active claims" in scoped_card


def test_in_progress_card_pluralizes_its_claim_count_like_the_row_chip(
    lithos_lens_config_env: Path,
) -> None:
    """One quantity, one page, one spelling: the card used to hardcode the
    plural, so the single claim in the fixture read "1 active claims" while the
    row chip a few hundred pixels below said "1 claim". Now that the card
    counts only the rendered rows, 0 and 1 are the ordinary case."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    card = _card(response.text, "#task-group-in_progress")
    assert "1 active claim<" in card
    assert "1 active claims" not in card
    # The row chip's wording is the convention being matched.
    assert "1 claim<" in response.text


def test_epic_that_lost_its_subtree_between_reads_falls_back_unscoped(
    lithos_lens_config_env: Path,
) -> None:
    """Reviewer repro (c-001) end to end: the epic is still in the open list but
    has closed by the time its children are read, so that read comes back empty
    and the confirming ``task_get`` answers the coded not-found. The board must
    render whole with the explanation, not as an empty scoped page."""

    class VanishedEpicClient(TaskFakeLithosClient):
        async def task_get(self, task_id: str) -> TaskRecord:
            # The epic was deleted after the open list was read.
            if task_id == "epic-1":
                raise LithosToolError("Task 'epic-1' not found.", code="task_not_found")
            return await super().task_get(task_id)

    fake = _with_epic(VanishedEpicClient())
    fake.children["epic-1"] = []

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01&epic=epic-1")

    assert response.status_code == 200
    text = response.text
    assert "data-epic-scope-missing" in text
    assert "data-epic-scope-empty" not in text
    assert "Unclaimed open task" in _group(text, "ready")
    # The stale chip is gone with the scope it claimed.
    assert 'data-epic-chip="epic-1"' not in text


def test_every_open_epic_renders_a_chip(lithos_lens_config_env: Path) -> None:
    """Story 8 at the rendering level: past one fan-out batch the strip keeps
    going — every open epic still gets its chip, with no "partial strip"
    caveat."""
    fake = _with_epic(TaskFakeLithosClient())
    extra = EPIC_FANOUT_BATCH * 2
    for n in range(extra):
        fake.tasks.append(
            TaskRecord(
                id=f"epic-extra-{n}",
                title=f"Extra epic {n}",
                status="open",
                task_type="epic",
                created_by="planner",
                created_at="2026-04-27T10:00:00+00:00",
            )
        )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    text = response.text
    assert text.count("data-epic-chip=") == extra + 1
    assert 'data-epic-chip="epic-extra-0"' in text
    assert f'data-epic-chip="epic-extra-{extra - 1}"' in text


def test_childless_epic_scope_renders_an_explained_empty_board(
    lithos_lens_config_env: Path,
) -> None:
    """Reviewer repro (c-001), the valid boundary: an open epic with no tasks
    scopes to an EMPTY board — its descendant set really is empty — and the
    page says so instead of looking broken or leaking other epics' work."""
    fake = _with_epic(TaskFakeLithosClient())
    fake.children["epic-1"] = []

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01&epic=epic-1")

    assert response.status_code == 200
    text = response.text
    assert "data-epic-scope-empty" in text
    assert "data-epic-scope-missing" not in text
    assert "epic-chip-selected" in text
    # No other epic's work leaked in.
    assert "Unclaimed open task" not in text
    assert "Claimed open task" not in text
    # Regression: the board is SCOPED, so it cannot also make the system-wide
    # claim — "Nothing under this epic yet" beside "All systems healthy" told
    # the operator both that a slice was empty and that everything was fine.
    assert "data-healthy-stripe" not in text
    assert "All systems healthy" not in text


# --- The optimistic skeleton row and the board's scope ---------------------


def test_the_board_tells_the_client_whether_it_is_narrowed(
    lithos_lens_config_env: Path,
) -> None:
    """a3fd5f01: the optimistic skeleton row asserted membership it had never
    checked, because it cannot check it — a ``task.created`` payload carries no
    tags, no project and no creator, so the client has nothing to evaluate the
    new task against.

    The scope decision is therefore made HERE and shipped to the client as a
    flag. Deciding it in JavaScript from ``window.location.search`` would put a
    second copy of the preserved-key list in the browser, to drift the next
    time a filter is added.
    """
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        unfiltered = client.get("/tasks")
        by_tag = client.get("/tasks?tag=area%3Adata")
        by_project = client.get("/tasks?project=influx")
        # `since` counts too. A just-created task is inside any past-anchored
        # window, so this one will rarely exclude it — but "rarely" is the
        # wrong bar for a row that asserts membership and persists when
        # reconciliation fails, and a FUTURE `since` excludes it outright.
        by_since = client.get("/tasks?since=2026-08-01")

    assert "boardFiltered: false" in unfiltered.text
    assert "boardFiltered: true" in by_tag.text
    assert "boardFiltered: true" in by_project.text
    assert "boardFiltered: true" in by_since.text


def test_an_unrecognised_query_param_does_not_narrow_the_board(
    lithos_lens_config_env: Path,
) -> None:
    """The flag tracks the PRESERVED keys, not "the query string is non-empty".

    A param outside the allowlist scores nothing against the filter budget and
    scopes nothing, so suppressing the optimistic row for it would cost
    responsiveness for no correctness gain — and would make an arbitrary
    tracking param silently change the board's behaviour.
    """
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?utm_source=slack")

    assert "boardFiltered: false" in response.text


@pytest.mark.parametrize(
    ("query", "admits_open"),
    [
        # No status filter: the board shows every status, open included.
        ("", True),
        ("status=open", True),
        ("status=completed", False),
        ("status=cancelled", False),
        # The comma form `parse_filters` documents as a convenience.
        ("status=open,completed", True),
        ("status=completed,open", True),
        ("status=completed,cancelled", False),
        # Repeated parameters, which parse_filters accumulates. The order must
        # not matter — and a single-value read cannot manage that: Starlette's
        # QueryParams.get returns the LAST value, so it happens to be right on
        # `completed&open` and wrong on `open&completed`. Both orderings are
        # here so a fix that is accidentally right on one is still caught.
        ("status=open&status=completed", True),
        ("status=completed&status=open", True),
        ("status=completed&status=cancelled", False),
        # An unrecognized status leaves no valid selection, and parse_filters
        # falls back to ALL statuses rather than an empty board — so this
        # board does show open rows, and must say so.
        ("status=nonsense", True),
        ("status=nonsense,completed", False),
    ],
)
def test_board_admits_open_matches_the_statuses_the_board_actually_renders(
    lithos_lens_config_env: Path, query: str, admits_open: bool
) -> None:
    """``boardAdmitsOpen`` tells the client whether a row that has just become
    ``open`` still belongs on this board — it is what stops a reopened row
    being parked in the pending strip under a filter that excludes it.

    It is read off the statuses THIS RENDER parsed, so it cannot disagree with
    the board around it. Re-deriving it from the raw query string did: a
    string compare against "open" reads `status=open,completed` as excluding
    open, and on the repeated form is right or wrong purely by ordering, since
    QueryParams.get collapses it to a single value (the LAST one).

    So the assertion is deliberately doubled: the flag must match what the
    board RENDERS, not merely what a second parser thinks the query said.
    """
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(f"/tasks?since=2026-04-01{'&' + query if query else ''}")

    assert response.status_code == 200
    text = response.text
    assert f"boardAdmitsOpen: {'true' if admits_open else 'false'}" in text
    # The board's own rows are the oracle: an open fixture row renders exactly
    # when the flag says open is admitted.
    assert ("Unclaimed open task" in text) is admits_open


# --- T1-S4: the Gates section, as rendered -------------------------------


def _add_gate(
    fake: TaskFakeLithosClient,
    task_id: str,
    *,
    gate_type: str = "human",
    created_at: str | None = None,
    metadata: dict[str, Any] | None = None,
    title: str | None = None,
    tags: tuple[str, ...] = (),
) -> None:
    meta: dict[str, Any] = {"gate_type": gate_type}
    meta.update(metadata or {})
    fake.tasks.append(
        TaskRecord(
            id=task_id,
            title=title or f"Gate {task_id}",
            status="open",
            task_type="gate",
            created_by="planner",
            # Young by default: a human gate past gate_waiting_attention_hours
            # is promoted into Needs attention and leaves this section.
            created_at=created_at or _ago(hours=1),
            tags=tags,
            metadata=meta,
        )
    )


def _waits_on(fake: TaskFakeLithosClient, waiter_id: str, *gate_ids: str) -> None:
    fake.blocked[waiter_id] = fake.blocked.get(waiter_id, ()) + tuple(
        BlockerRecord(kind="gate", task_id=gate_id, type="waits_on_gate")
        for gate_id in gate_ids
    )
    fake.ready_ids.discard(waiter_id)


def test_gates_section_renders_type_badges_with_human_gates_first(
    lithos_lens_config_env: Path,
) -> None:
    """§5.2.3: one group per gate type, human first — the operator's own queue
    leads — and the badge carries the type. The markup hook is the closed
    vocabulary slug, never the raw metadata string."""
    fake = TaskFakeLithosClient()
    _add_gate(fake, "gate-ci", gate_type="ci", created_at=_ago(hours=5))
    _add_gate(fake, "gate-human", gate_type="human", created_at=_ago(hours=2))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    body = response.text
    assert 'id="task-group-gates"' in body
    human = body.index('data-gate-group="human"')
    ci = body.index('data-gate-group="ci"')
    assert human < ci
    assert (
        '<span class="badge badge-gate badge-gate-human" data-gate-type-badge>human'
        in body
    )
    assert "<strong data-gate-count>2</strong>" in body


def test_human_gate_row_says_how_many_tasks_it_blocks(
    lithos_lens_config_env: Path,
) -> None:
    """PRD acceptance: a human gate lists "blocks N tasks", counted from the
    tasks Lithos reports as waiting on it, and the count expands into the list
    of those tasks."""
    fake = TaskFakeLithosClient()
    _add_gate(fake, "gate-human")
    _waits_on(fake, "open-unclaimed", "gate-human")
    _waits_on(fake, "open-old", "gate-human")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    body = response.text
    assert 'data-gate-waiters-state="known"' in body
    assert '<summary data-gate-waiting="2">blocks 2 tasks</summary>' in body
    assert "Unclaimed open task" in body


def test_single_waiter_reads_in_the_singular(lithos_lens_config_env: Path) -> None:
    fake = TaskFakeLithosClient()
    _add_gate(fake, "gate-human")
    _waits_on(fake, "open-unclaimed", "gate-human")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert '<summary data-gate-waiting="1">blocks 1 task</summary>' in response.text


def test_gate_waiter_count_survives_a_narrowed_board(
    lithos_lens_config_env: Path,
) -> None:
    """The waiter source is the whole frontier whatever the board is scoped to:
    filtering to one project must not make a gate that blocks two tasks read
    "blocks 1 task"."""
    fake = TaskFakeLithosClient()
    # The gate is inside the narrowed scope; one of its two waiters is not.
    _add_gate(fake, "gate-human", tags=("project:influx",))
    _waits_on(fake, "open-unclaimed", "gate-human")  # tagged project:influx
    _waits_on(fake, "open-old", "gate-human")  # tagged project:lithos-lens

    with _client(lithos_lens_config_env, fake) as client:
        wide = client.get("/tasks?since=2026-04-01")
        narrow = client.get("/tasks?since=2026-04-01&tag=project%3Ainflux")

    assert '<summary data-gate-waiting="2">blocks 2 tasks</summary>' in wide.text
    assert '<summary data-gate-waiting="2">blocks 2 tasks</summary>' in narrow.text
    # The board itself IS narrowed — the out-of-scope waiter has no ROW…
    assert 'id="task-row-open-old"' in wide.text
    assert 'id="task-row-open-old"' not in narrow.text
    # …while the gate's waiter list still names it, because both the count and
    # the list come off the unfiltered blocked frontier.
    assert "Old open task" in narrow.text


def test_timer_gate_renders_a_countdown_and_one_self_refresh_instant(
    lithos_lens_config_env: Path,
) -> None:
    """Lithos emits no event when a timer lapses, so the board publishes the
    earliest still-future ready_at once and tasks.js schedules a single refresh
    against it. The row itself carries the absolute stamp as the no-JS
    baseline, which the countdown replaces."""
    fake = TaskFakeLithosClient()
    soon = _ahead(hours=3)
    _add_gate(fake, "gate-soon", gate_type="timer", metadata={"ready_at": soon})
    _add_gate(
        fake, "gate-later", gate_type="timer", metadata={"ready_at": _ahead(days=4)}
    )
    _add_gate(
        fake, "gate-lapsed", gate_type="timer", metadata={"ready_at": _ago(hours=1)}
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    body = response.text
    assert f'data-gates-next-ready-at="{soon}"' in body
    assert body.count("data-gates-next-ready-at") == 1
    assert f'data-gate-ready-at="{soon}"' in body
    # A lapsed timer gate still renders — it stays open until someone completes
    # it — it just does not schedule anything.
    assert body.count("data-gate-ready-at") == 3


def test_malformed_timer_ready_at_renders_the_row_instead_of_a_500(
    lithos_lens_config_env: Path,
) -> None:
    """``metadata`` is peer-written: an unparseable stamp, and one whose UTC
    shift overflows the datetime domain, must both degrade to "no countdown"
    rather than taking the dashboard down for every operator."""
    fake = TaskFakeLithosClient()
    _add_gate(
        fake,
        "gate-overflow",
        gate_type="timer",
        metadata={"ready_at": "9999-12-31T23:59:59-01:00"},
    )
    _add_gate(fake, "gate-garbage", gate_type="timer", metadata={"ready_at": "soon"})

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert response.status_code == 200
    body = response.text
    assert "data-gates-next-ready-at" not in body
    assert "Gate gate-overflow" in body
    assert "Gate gate-garbage" in body


def test_hostile_gate_metadata_renders_as_text_not_markup(
    lithos_lens_config_env: Path,
) -> None:
    """An unknown gate_type must not become an injected class token, and the
    advisory chips are escaped like any other peer-written string."""
    fake = TaskFakeLithosClient()
    _add_gate(
        fake,
        "gate-hostile",
        gate_type='human" data-gate-type-badge="',
        metadata={"note": "<script>alert(1)</script>"},
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    body = response.text
    assert response.status_code == 200
    assert 'data-gate-group="unknown"' in body
    assert 'class="badge badge-gate badge-gate-unknown"' in body
    assert "badge-gate-human" not in body
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_open_gate_renders_exactly_once_on_the_board(
    lithos_lens_config_env: Path,
) -> None:
    """Single placement: gates never enter the workable sections (Lithos
    excludes them from both frontiers), and a gate promoted into Needs
    attention leaves the Gates section rather than appearing twice."""
    fake = TaskFakeLithosClient()
    _add_gate(fake, "gate-fresh", title="Fresh gate")
    _add_gate(fake, "gate-stale", title="Stale gate", created_at=_ago(days=4))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    body = response.text
    assert body.count('id="task-row-gate-fresh"') == 1
    assert body.count('id="task-row-gate-stale"') == 1
    # The stale one was promoted, so it renders as an attention TASK row and
    # not as a gate row; the fresh one is the other way round.
    assert 'data-gate-row data-task-id="gate-fresh"' in body
    assert 'data-gate-row data-task-id="gate-stale"' not in body


def test_gates_section_says_so_when_no_gate_matches(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    assert "No open gates match these filters." in response.text
    assert "<strong data-gate-count>0</strong>" in response.text


def test_a_browser_parseable_only_ready_at_renders_without_a_countdown_hook(
    lithos_lens_config_env: Path,
) -> None:
    """The rendered half of the same contract as
    ``test_a_stamp_only_the_browser_can_parse_drives_no_countdown``: the row
    still SHOWS the stamp Lens could not parse, but publishes no
    ``data-gate-ready-at``, which is the only thing tasks.js ticks from.

    Asserted here rather than only on the model because the defect was in the
    template — the attribute was emitted from the display field, so every value
    that survived as text became a countdown the browser drove on its own.
    """
    fake = TaskFakeLithosClient()
    _add_gate(
        fake,
        "timer-browser-only",
        gate_type="timer",
        metadata={"ready_at": "2026/09/01"},
    )
    _add_gate(
        fake,
        "timer-real",
        gate_type="timer",
        metadata={"ready_at": _ahead(hours=3)},
    )

    with _client(lithos_lens_config_env, fake) as client:
        text = client.get("/tasks?since=2026-04-01").text

    hooks = re.findall(r'data-gate-ready-at="([^"]*)"', text)
    # Exactly one countdown hook: the gate whose stamp both sides parse.
    assert len(hooks) == 1
    assert "2026/09/01" not in hooks[0]
    # The unparseable one is still on the page, as static text.
    assert "2026/09/01" in text
    assert "data-gate-ready-unparsed" in text


# --- T2b: loom's PR reconciliation state, as the board renders it ---------

_PR_URL = "https://example.invalid/lithos-lens/pull/84"


def _add_pr_gate(
    fake: TaskFakeLithosClient,
    task_id: str,
    *,
    state: str,
    detail: str = "",
    since: str | None = None,
    pr_url: str = _PR_URL,
    state_pr_url: str | None = None,
    created_at: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """A `pr` gate carrying the four keys loom writes on every sweep."""
    metadata: dict[str, Any] = {
        "pr_url": pr_url,
        "reconciliation_state": state,
        "reconciliation_detail": detail,
        "reconciliation_since": since or _ago(hours=2),
        "reconciliation_pr_url": pr_url if state_pr_url is None else state_pr_url,
    }
    metadata.update(extra or {})
    _add_gate(
        fake,
        task_id,
        gate_type="pr",
        created_at=created_at,
        metadata=metadata,
    )


def test_a_needs_human_pr_gate_badges_red_and_enters_needs_attention(
    lithos_lens_config_env: Path,
) -> None:
    """T2b acceptance, both halves at once: the state loom computes every ten
    minutes is a first-class badge with loom's line as its tooltip, and
    `needs_human` also promotes the gate into Needs attention with the reason
    and the same fact — the escalation loom `human` gates already get, without
    the 24h wait."""
    fake = TaskFakeLithosClient()
    _add_pr_gate(
        fake,
        "gate-pr-stuck",
        state="needs_human",
        detail="Reviewer requested changes Lens cannot resolve.",
    )

    with _client(lithos_lens_config_env, fake) as client:
        body = client.get("/tasks?since=2026-04-01").text

    assert 'data-reconciliation-state="needs_human"' in body
    assert 'class="badge badge-reconciliation badge-reconciliation-danger"' in body
    assert 'title="Reviewer requested changes Lens cannot resolve."' in body
    # The badge says the state AND how long it has held it.
    assert ">needs human · 2h<" in body
    # Promoted: the reason chip and its supporting fact, on the attention row.
    # The chip's TEXT is §5.2.2's wording, not the slug — the slug stays the
    # markup hook, and a chip reading "pr-needs-decision" is an internal token
    # leaking onto the operator's board.
    assert (
        '<span class="attention-chip attention-chip-pr-needs-decision"'
        ' data-attention-rule="pr-needs-decision">PR needs a decision</span>'
    ) in body
    assert ">pr-needs-decision<" not in body
    assert "Reviewer requested changes Lens cannot resolve." in body
    # Single placement — promoted out of the Gates section, not rendered twice —
    # and the badge travelled with the row, so it is the ATTENTION row that
    # carries it.
    assert body.count('id="task-row-gate-pr-stuck"') == 1
    assert body.count("badge-reconciliation-danger") == 1
    assert 'data-gate-row data-task-id="gate-pr-stuck"' not in body
    assert '<article class="task-row" id="task-row-gate-pr-stuck"' in body


def test_a_ready_to_merge_pr_gate_badges_green_and_stays_in_the_gates_section(
    lithos_lens_config_env: Path,
) -> None:
    """The other end of the vocabulary: green, and NOT an escalation — a PR
    that is ready to merge needs no one's attention."""
    fake = TaskFakeLithosClient()
    _add_pr_gate(
        fake, "gate-pr-done", state="ready_to_merge", detail="All gates green."
    )

    with _client(lithos_lens_config_env, fake) as client:
        body = client.get("/tasks?since=2026-04-01").text

    assert 'class="badge badge-reconciliation badge-reconciliation-ok"' in body
    assert ">ready to merge · 2h<" in body
    assert 'data-gate-row data-task-id="gate-pr-done"' in body
    assert 'data-attention-rule="pr-needs-decision"' not in body


def test_a_state_about_a_replaced_pr_renders_no_badge(
    lithos_lens_config_env: Path,
) -> None:
    """A replacement PR on the same gate starts fresh, and the old state sits
    there until loom's next sweep. The row withholds it — and keeps the raw
    keys as advisory chips, so it stays inspectable without being asserted."""
    fake = TaskFakeLithosClient()
    _add_pr_gate(
        fake,
        "gate-pr-replaced",
        state="needs_human",
        detail="About the PR this gate no longer points at.",
        state_pr_url="https://example.invalid/lithos-lens/pull/12",
    )

    with _client(lithos_lens_config_env, fake) as client:
        body = client.get("/tasks?since=2026-04-01").text

    assert "data-reconciliation-state" not in body
    assert "badge-reconciliation" not in body
    assert 'data-attention-rule="pr-needs-decision"' not in body
    # …and the row is still a gate row carrying the keys as ordinary metadata.
    assert 'data-gate-row data-task-id="gate-pr-replaced"' in body
    assert "reconciliation_detail" in body


def test_an_unknown_state_renders_as_grey_text_rather_than_a_crash(
    lithos_lens_config_env: Path,
) -> None:
    """The vocabulary is loom's and may grow. An unrecognised value renders as
    the text it is, in the unknown tone — and its markup hook collapses to
    `unknown`, so a peer-written state cannot borrow another state's colour."""
    fake = TaskFakeLithosClient()
    _add_pr_gate(fake, "gate-pr-new", state="awaiting_second_review")
    _add_pr_gate(fake, "gate-pr-hostile", state='needs_human" data-evil="')

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    body = response.text
    assert response.status_code == 200
    assert 'class="badge badge-reconciliation badge-reconciliation-unknown"' in body
    assert ">awaiting_second_review · 2h<" in body
    # The hostile value is escaped TEXT and buys no markup hook at all.
    assert 'data-reconciliation-state="unknown"' in body
    assert 'data-evil="' not in body
    assert "badge-reconciliation-danger" not in body


def test_pr_gates_render_in_state_severity_order(
    lithos_lens_config_env: Path,
) -> None:
    """§5.2.3 ordering, server-rendered and JS-free: the PR that needs a person
    leads, then the failures, then the ones loom is still working, then the
    ones that are done."""
    fake = TaskFakeLithosClient()
    # Added youngest-severe first, so age alone would produce a different order.
    _add_pr_gate(fake, "pr-merge", state="ready_to_merge", created_at=_ago(days=3))
    _add_pr_gate(fake, "pr-review", state="awaiting_review", created_at=_ago(days=2))
    _add_pr_gate(fake, "pr-flight", state="reconciling", created_at=_ago(days=2))
    _add_pr_gate(fake, "pr-behind", state="behind", created_at=_ago(hours=20))
    _add_pr_gate(fake, "pr-failed", state="gate_failed", created_at=_ago(hours=2))

    with _client(lithos_lens_config_env, fake) as client:
        body = client.get("/tasks?since=2026-04-01").text

    rendered = re.findall(r'id="task-row-(pr-[a-z]+)"', body)
    assert rendered == ["pr-failed", "pr-behind", "pr-flight", "pr-review", "pr-merge"]


def test_pr_and_human_gate_escalations_survive_a_frontier_outage(
    lithos_lens_config_env: Path,
) -> None:
    """The gate rules read the master open list and the clock — neither of which
    a failed frontier read touched — so they must still fire on the flat board.

    This is the shape the promise breaks in: "a `needs_human` PR promotes
    immediately" quietly becomes "…unless a frontier read failed", and the one
    line the operator most needs goes missing in exactly the situation where
    something is already wrong. The rules whose evidence the outage DID destroy
    (the blocker-derived and claim-derived ones) stay silent, which is the
    other half of the contract.
    """
    fake = NoFrontierClient()
    _add_pr_gate(
        fake,
        "gate-pr-stuck",
        state="needs_human",
        detail="Reviewer requested changes Lens cannot resolve.",
    )
    _add_gate(fake, "gate-waited", gate_type="human", created_at=_ago(days=3))
    _add_pr_gate(fake, "gate-pr-done", state="ready_to_merge")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    body = response.text
    assert response.status_code == 200
    assert 'data-task-group="open"' in body  # the flat board, as before
    # Both gate rules fired, with their wording and their facts…
    assert ">PR needs a decision</span>" in body
    assert "Reviewer requested changes Lens cannot resolve." in body
    assert 'data-attention-rule="gate-waiting"' in body
    # …and single placement still holds: promoted rows left the Gates section.
    assert 'data-gate-row data-task-id="gate-pr-stuck"' not in body
    assert 'data-gate-row data-task-id="gate-waited"' not in body
    # The healthy PR is untouched — an outage is not a reason to escalate it.
    assert 'data-gate-row data-task-id="gate-pr-done"' in body
    assert "badge-reconciliation-ok" in body
    # Rules whose evidence the outage destroyed stay silent: no blocker records
    # exist, so nothing claims a cancelled blocker or a cycle.
    assert 'data-attention-rule="unsatisfiable"' not in body
    assert 'data-attention-rule="cycle"' not in body


def test_a_long_failing_pr_gate_escalates_like_a_waiting_human_gate(
    lithos_lens_config_env: Path,
) -> None:
    """`gate_failed` past the human-gate threshold is the same "nobody is
    coming" judgement, so it takes the same promotion — dated from the state's
    own `reconciliation_since`, not the gate's age."""
    fake = TaskFakeLithosClient()
    _add_pr_gate(
        fake,
        "gate-pr-failing",
        state="gate_failed",
        since=_ago(days=2),
        detail="required check `e2e` has failed 6 times.",
    )

    with _client(lithos_lens_config_env, fake) as client:
        body = client.get("/tasks?since=2026-04-01").text

    assert 'data-attention-rule="pr-needs-decision"' in body
    assert ">PR needs a decision</span>" in body
    assert "required check `e2e` has failed 6 times." in body
    assert 'data-gate-row data-task-id="gate-pr-failing"' not in body


# --- project quick-switch strip (§5.3) -------------------------------------


def _project_chip_links(html: str) -> dict[str, str]:
    """Every chip the project strip rendered: slug -> the href a click follows."""
    return {
        slug: unescape(href)
        for href, slug in re.findall(
            r'<a class="project-chip[^"]*"\s+href="([^"]+)"'
            r'\s+data-project-chip="([^"]+)"',
            html,
        )
    }


def _project_chip_counts(html: str) -> dict[str, int]:
    """slug -> the open-row count the chip states."""
    return {
        slug: int(count)
        for slug, count in re.findall(
            r'data-project-chip="([^"]+)".*?data-project-open-count>(\d+)<',
            html,
            re.S,
        )
    }


def _three_project_fake() -> TaskFakeLithosClient:
    """``_roadmap_fake`` plus a third project inside the tag, and one outside.

    The mirrored row carries ``metadata.project`` and no project TAG — loom's
    issue-mirrored convention (§5B.1), which the strip must count like any
    other.
    """
    fake = _roadmap_fake()
    fake.tasks.extend(
        [
            TaskRecord(
                id="core-ready",
                title="Core roadmap item",
                status="open",
                created_by="planner",
                created_at=_ago(minutes=20),
                tags=("roadmap-2026-08",),
                metadata={"project": "lithos"},
            ),
            TaskRecord(
                id="atlas-ready",
                title="Atlas item, other scope",
                status="open",
                created_by="planner",
                created_at=_ago(minutes=20),
                tags=("project:lithos-atlas",),
            ),
        ]
    )
    fake.ready_ids.update({"core-ready", "atlas-ready"})
    return fake


def test_the_project_strip_enumerates_the_scope_instead_of_retyping_it(
    lithos_lens_config_env: Path,
) -> None:
    """The ask (Dave, 2026-09-11): a roadmap tag is a scope spanning a handful
    of projects, and switching between them meant editing the Project box with
    no indication of what was even in there. The strip names them, with how
    much open work each holds — and a project whose rows are all outside the
    tag is not among them."""
    fake = _three_project_fake()

    with _client(lithos_lens_config_env, fake) as client:
        board = client.get("/tasks?tag=roadmap-2026-08&since=2026-04-01")

    text = unescape(board.text)
    chips = _project_chip_links(text)

    assert board.status_code == 200
    # Ordered by open count, then slug. lithos-lens holds the Ready row and the
    # stale one Needs attention promoted; the metadata-only project counts like
    # any other; the off-tag project is absent.
    assert list(chips) == ["lithos-lens", "lithos", "lithos-loom"]
    assert _project_chip_counts(text) == {
        "lithos-lens": 2,
        "lithos": 1,
        "lithos-loom": 1,
    }
    assert 'data-project-chip="lithos-atlas"' not in text
    # Each chip ADDS its project to the live filters rather than replacing them.
    for slug, href in chips.items():
        assert "tag=roadmap-2026-08" in href
        assert "since=2026-04-01" in href
        assert f"project={slug}" in href
    # Nothing is selected yet, so there is nothing to clear.
    assert "data-project-clear" not in text


def test_clicking_a_project_chip_narrows_the_board_without_shrinking_the_strip(
    lithos_lens_config_env: Path,
) -> None:
    """One click to narrow, one more to add a second project, and the strip
    itself never moves: it is scoped by every filter EXCEPT ``project``, so the
    other projects stay one click away with their counts intact."""
    fake = _three_project_fake()

    with _client(lithos_lens_config_env, fake) as client:
        board = client.get("/tasks?tag=roadmap-2026-08&since=2026-04-01")
        one = client.get(_project_chip_links(board.text)["lithos-loom"])
        two = client.get(_project_chip_links(unescape(one.text))["lithos-lens"])

    one_text = unescape(one.text)
    two_text = unescape(two.text)

    assert one.status_code == 200
    # The board narrowed…
    assert "Loom roadmap item" in one_text
    assert "Lens roadmap item" not in one_text
    # …the strip did not, and the chip the operator clicked says it is live.
    assert _project_chip_counts(one_text) == {
        "lithos-lens": 2,
        "lithos": 1,
        "lithos-loom": 1,
    }
    assert re.search(
        r'class="project-chip project-chip-selected"[^>]*'
        r'data-project-chip="lithos-loom"[^>]*aria-current="true"',
        one_text,
        re.S,
    )
    # A second project ORs onto the first — the comma form the filter already
    # means — and both chips are marked.
    assert "project=lithos-loom,lithos-lens" in two_text
    assert "Loom roadmap item" in two_text
    assert "Lens roadmap item" in two_text
    assert two_text.count('aria-current="true"') == 2
    # An unselected chip announces itself ADDITIVELY: with lithos-loom live,
    # the lithos-lens control goes to BOTH, so "show only" would be false.
    assert re.search(
        r'data-project-chip="lithos-lens"[^>]*aria-label="Add lithos-lens"',
        one_text,
        re.S,
    )
    assert re.search(
        r'data-project-chip="lithos-loom"[^>]*aria-label="Remove lithos-loom"',
        two_text,
        re.S,
    )


def test_removing_one_project_leaves_the_other_and_everything_else(
    lithos_lens_config_env: Path,
) -> None:
    """The way back out of a two-project selection, followed: clicking the
    selected chip removes ITS slug only — the other project stays selected, the
    board keeps its rows, and no unrelated parameter moves."""
    fake = _three_project_fake()
    query = (
        "tag=roadmap-2026-08&project=lithos-loom,lithos-lens"
        "&since=2026-04-01&all_agents=1"
    )

    with _client(lithos_lens_config_env, fake) as client:
        two = client.get(f"/tasks?{query}")
        remove_href = _project_chip_links(unescape(two.text))["lithos-loom"]
        one = client.get(remove_href)

    one_text = unescape(one.text)

    assert one.status_code == 200
    assert parse_qs(urlsplit(remove_href).query) == {
        "tag": ["roadmap-2026-08"],
        "since": ["2026-04-01"],
        "all_agents": ["1"],
        "project": ["lithos-lens"],
    }
    # One project left selected, the other back to an add link…
    assert re.search(
        r'data-project-chip="lithos-lens"[^>]*aria-current="true"', one_text, re.S
    )
    assert one_text.count('aria-current="true"') == 1
    # …and the board followed it.
    assert "Lens roadmap item" in one_text
    assert "Loom roadmap item" not in one_text
    # The strip is whole either way — removing a project is not narrowing it.
    assert _project_chip_counts(one_text) == {
        "lithos-lens": 2,
        "lithos": 1,
        "lithos-loom": 1,
    }


def test_the_project_strip_is_a_labelled_section_below_the_epic_strip(
    lithos_lens_config_env: Path,
) -> None:
    """The rendering contract (§5.3): a third strip, BELOW the active-filters
    row and the epic strip, carrying its own accessible name. Asserted on the
    markup rather than left to the screenshots, which are written but never
    compared — moving the strip above the epic chips, or dropping the label a
    screen reader announces it by, would otherwise change nothing observable.
    """
    fake = _three_project_fake()
    fake.tasks.append(_epic_row("epic-roadmap", "Roadmap epic"))
    fake.children["epic-roadmap"] = ["loom-ready"]

    with _client(lithos_lens_config_env, fake) as client:
        board = client.get("/tasks?tag=roadmap-2026-08&since=2026-04-01")

    text = board.text

    assert (
        '<section class="project-strip" aria-label="Projects" data-project-strip>'
        in text
    )
    # Below the two strips it composes with, in that order.
    assert (
        text.index("data-active-filters")
        < text.index("data-epic-strip")
        < text.index("data-project-strip")
    )


def test_clearing_the_project_filter_keeps_every_other_parameter(
    lithos_lens_config_env: Path,
) -> None:
    """ "Back to all projects" is one click, and it removes ``project`` ALONE:
    the tag scope, the agent, and both date windows are what the operator is
    browsing under and must survive it."""
    fake = _three_project_fake()
    # Two more projects inside the tag, each held OUT of the scope by one of
    # the other filters alone — so this board proves the agent match and the
    # created window really are part of what the strip enumerates, rather than
    # riding along as parameters nothing tests.
    fake.tasks.extend(
        [
            TaskRecord(
                id="cardinal-other-agent",
                title="Cardinal roadmap item",
                status="open",
                created_by="worker",
                created_at=_ago(minutes=20),
                tags=("project:lithos-cardinal", "roadmap-2026-08"),
            ),
            TaskRecord(
                id="ganglion-pre-window",
                title="Ganglion roadmap item",
                status="open",
                created_by="planner",
                created_at="2023-06-01T10:00:00+00:00",
                tags=("project:lithos-ganglion", "roadmap-2026-08"),
            ),
        ]
    )
    fake.ready_ids.update({"cardinal-other-agent", "ganglion-pre-window"})
    query = (
        "status=open&tag=roadmap-2026-08&project=lithos-loom,lithos-lens"
        "&agent=planner&since=2026-04-01&created_since=2024-01-01&all_agents=1"
    )

    with _client(lithos_lens_config_env, fake) as client:
        board = client.get(f"/tasks?{query}")
        clear_href = unescape(
            re.findall(r'href="([^"]+)"[^>]*data-project-clear', board.text)[0]
        )
        cleared = client.get(clear_href)
        # Controls: drop ONE filter at a time from the cleared board and the
        # project it was hiding appears. Without these the two assertions above
        # would hold for a strip that ignored both predicates.
        any_agent = client.get(clear_href.replace("&agent=planner", ""))
        any_age = client.get(clear_href.replace("&created_since=2024-01-01", ""))

    cleared_text = unescape(cleared.text)

    assert board.status_code == 200
    assert "project=" not in clear_href
    assert parse_qs(urlsplit(clear_href).query) == {
        "status": ["open"],
        "tag": ["roadmap-2026-08"],
        "agent": ["planner"],
        "since": ["2026-04-01"],
        "created_since": ["2024-01-01"],
        "all_agents": ["1"],
    }
    assert cleared.status_code == 200
    # Every project in the scope is back, none of them selected, and the clear
    # is gone with the filter it cleared. The two projects the agent match and
    # the created window exclude are NOT back — clearing removes `project`
    # alone.
    assert list(_project_chip_links(cleared_text)) == [
        "lithos-lens",
        "lithos",
        "lithos-loom",
    ]
    assert 'aria-current="true"' not in cleared_text
    assert "data-project-clear" not in cleared_text

    assert "lithos-cardinal" in _project_chip_links(unescape(any_agent.text))
    assert "lithos-ganglion" in _project_chip_links(unescape(any_age.text))


def test_the_project_strip_is_drawn_only_where_it_has_switching_to_offer(
    lithos_lens_config_env: Path,
) -> None:
    """A one-project scope has nothing to switch between, so the strip stays
    out of the way — unless a project filter is active, where the clear has to
    remain reachable however little is left."""
    fake = _three_project_fake()

    with _client(lithos_lens_config_env, fake) as client:
        single = client.get("/tasks?tag=project:lithos-atlas&since=2026-04-01")
        filtered = client.get(
            "/tasks?tag=project:lithos-atlas&project=lithos-atlas&since=2026-04-01"
        )

    assert single.status_code == 200
    assert "data-project-strip" not in single.text
    # With the filter live the strip returns, and so does the way out of it.
    assert "data-project-strip" in filtered.text
    assert "data-project-clear" in filtered.text


def _big_tag_fake(tag: str) -> TaskFakeLithosClient:
    """One tag spanning two projects, and nothing else — so the query the
    board carries is exactly ``tag=<tag>`` and the byte arithmetic below is
    the ceiling's own."""
    fake = TaskFakeLithosClient()
    fake.tasks = [
        TaskRecord(
            id="lens-ready",
            title="Lens roadmap item",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=("project:lithos-lens", tag),
        ),
        TaskRecord(
            id="loom-ready",
            title="Loom roadmap item",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=("project:lithos-loom", tag),
        ),
    ]
    fake.ready_ids = {"lens-ready", "loom-ready"}
    return fake


def _tag_at(emitted_bytes: int) -> str:
    """A literal tag whose ``tag=`` pair measures ``emitted_bytes``."""
    return "t" * (emitted_bytes - len("tag="))


def test_an_add_chip_at_the_filter_budget_is_followed_and_accepted(
    lithos_lens_config_env: Path,
) -> None:
    """Regression (round-1 correctness f-001), the reachable side: the largest
    board from which adding a project still fits must actually add it.

    Every chip on the strip advertises one-click narrowing, so following one
    has to work for any request the board itself accepted — the promise
    ``test_chip_clear_link_from_a_near_budget_tag_query_still_works`` makes for
    the tag chips. Asserting the href alone would not show it: only following
    the link runs it back through the ceiling that would refuse it.
    """
    added = len("&") + len(urlencode([("project", "lithos-lens")]))
    tag = _tag_at(MAX_FILTER_QUERY_BYTES - added)
    fake = _big_tag_fake(tag)

    with _client(lithos_lens_config_env, fake) as client:
        board = client.get(f"/tasks?tag={tag}")
        href = _project_chip_links(unescape(board.text))["lithos-lens"]
        narrowed = client.get(href)

    assert board.status_code == 200
    # The link is exactly ON the ceiling — one byte of tag more and it would
    # be over, which is what makes this the boundary rather than a headroom
    # case that proves nothing.
    assert len(urlsplit(href).query) == MAX_FILTER_QUERY_BYTES
    assert narrowed.status_code == 200
    assert "data-filter-rejected" not in narrowed.text
    assert "Lens roadmap item" in narrowed.text
    assert "Loom roadmap item" not in narrowed.text
    # …and the tag scope came along, so the strip still offers the way back.
    assert "data-project-clear" in narrowed.text


def test_a_project_chip_is_never_offered_past_the_filter_budget(
    lithos_lens_config_env: Path,
) -> None:
    """The other side of the same boundary (round-1 correctness f-001).

    One byte of tag further and there is no room for ``&project=`` at all.
    The bytes have to come from somewhere and every pair in the query is a
    filter the board was asked for, so the chip is drawn WITHOUT a link
    instead of with one the router refuses at 400: the strip still says which
    projects are in the scope and how much open work each holds — its subject
    — and it does not advertise a click that fails. The 400 is asserted on the
    URL the chip WOULD have carried, so this pins a real dead end rather than
    a hypothetical one.
    """
    added = len("&") + len(urlencode([("project", "lithos-lens")]))
    tag = _tag_at(MAX_FILTER_QUERY_BYTES - added + 1)
    fake = _big_tag_fake(tag)

    with _client(lithos_lens_config_env, fake) as client:
        board = client.get(f"/tasks?tag={tag}")
        refused = client.get(f"/tasks?tag={tag}&project=lithos-lens")

    assert board.status_code == 200
    text = unescape(board.text)
    # The link that is not offered is the link that would not work.
    assert refused.status_code == 400
    assert _project_chip_links(text) == {}
    # The strip is still a strip: both projects, both counts, said as they are.
    assert 'data-project-chip="lithos-lens" data-project-chip-unavailable' in " ".join(
        text.split()
    )
    assert _project_chip_counts(text) == {"lithos-lens": 1, "lithos-loom": 1}
    assert "cannot be added" in text


def test_a_project_chip_the_filter_cannot_carry_is_drawn_without_a_link(
    lithos_lens_config_env: Path,
) -> None:
    """The other dead end: ``?project=`` is comma-joined and split on the comma,
    so a slug that contains one (``project:lithos,atlas`` is a legal tag) has no
    filter spelling at all — its link would filter for ``lithos`` OR ``atlas``
    and land on a board holding none of the rows the chip counted. The chip is
    still drawn with its slug and count (the strip's subject), but as text
    wearing the reason, never as that link. The dead end is asserted on the
    URL the chip WOULD have carried, so this pins a real one."""
    tag = "roadmap-2026-09"
    fake = _big_tag_fake(tag)
    fake.tasks.append(
        TaskRecord(
            id="odd-ready",
            title="Oddly tagged item",
            status="open",
            created_by="planner",
            created_at=_ago(minutes=20),
            tags=("project:lithos,atlas", tag),
        )
    )
    fake.ready_ids.add("odd-ready")

    with _client(lithos_lens_config_env, fake) as client:
        board = client.get(f"/tasks?tag={tag}")
        followed = client.get(f"/tasks?tag={tag}&project=lithos,atlas")

    assert board.status_code == 200
    text = unescape(board.text)
    # The two carriable projects link; the comma one is drawn, counted, inert.
    assert set(_project_chip_links(text)) == {"lithos-lens", "lithos-loom"}
    assert _project_chip_counts(text) == {
        "lithos-lens": 1,
        "lithos-loom": 1,
        "lithos,atlas": 1,
    }
    assert 'data-project-chip="lithos,atlas" data-project-chip-unavailable' in " ".join(
        text.split()
    )
    assert "lithos,atlas cannot be added: its slug contains a comma" in text
    # …because the link it would have carried is a board with none of its rows.
    assert followed.status_code == 200
    assert "Oddly tagged item" not in followed.text


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        (
            "tag=nothing-carries-this&project=lithos-loom&since=2026-04-01",
            "the other filters leave no open row at all",
        ),
        (
            "status=completed&project=lithos-loom&since=2026-04-01",
            "the open side is switched off, so no open row is on the board",
        ),
    ],
)
def test_the_clear_survives_a_scope_with_no_projects_left_in_it(
    lithos_lens_config_env: Path, query: str, reason: str
) -> None:
    """The state the "shown whenever ``project`` is set" rule exists for.

    With a project filter live and NOTHING in the scope to draw a chip from,
    the strip is the only control that can widen the board again — the chips
    that would have removed the projects one at a time are exactly what is
    missing. A strip keyed on "are there any chips" would vanish here and
    strand the operator on an empty board with the filter that emptied it.
    """
    fake = _three_project_fake()

    with _client(lithos_lens_config_env, fake) as client:
        stuck = client.get(f"/tasks?{query}")
        clear_href = unescape(
            re.findall(r'href="([^"]+)"[^>]*data-project-clear', stuck.text)[0]
        )
        cleared = client.get(clear_href)

    assert stuck.status_code == 200, reason
    # The labelled strip is there, holding the clear and no chips.
    assert (
        '<section class="project-strip" aria-label="Projects" data-project-strip>'
        in stuck.text
    )
    assert _project_chip_links(unescape(stuck.text)) == {}
    # …and the clear drops ``project`` alone, leaving what emptied the board
    # visible rather than silently widening it too.
    assert parse_qs(urlsplit(clear_href).query) == {
        key: value for key, value in parse_qs(query).items() if key != "project"
    }
    assert cleared.status_code == 200
    assert "data-project-clear" not in cleared.text


# --- the retired project_convention posture (§4.4) --------------------------


def _open_row_ids(html: str) -> set[str]:
    """Every OPEN-side task row the board rendered — the rows a chip counts.

    The resolved sections are excluded the same way ``project_chips`` excludes
    them: a completed row is not work to switch to, so it is not in a count.
    """
    return {
        task_id
        for section in OPEN_SECTIONS
        if f'data-task-group="{section}"' in html
        for task_id in re.findall(r'data-task-id="([^"]+)"', _group(html, section))
    }


#: Every project in ``_three_project_fake``'s snapshot, and the OPEN rows a
#: click on it must land on. ``lithos`` is carried by ``metadata.project``
#: alone — the case a single-convention posture used to strand.
_PROJECT_DESTINATIONS = {
    "lithos": {"core-ready"},
    "lithos-atlas": {"atlas-ready"},
    "lithos-lens": {"lens-ready", "lens-stale", "lens-stale-offscope"},
    "lithos-loom": {"loom-ready", "loom-offscope"},
}


@pytest.mark.parametrize("posture", ["both", "tag", "metadata"])
def test_every_offered_project_leads_to_its_rows_whatever_the_posture(
    lithos_lens_config_env: Path, posture: str
) -> None:
    """`?project=` matches under BOTH conventions whatever the config says.

    ``[tasks].project_convention`` used to select which §5B.1 convention
    matching honoured, while every control that OFFERS a project — the datalist
    (``project_universe``), the graph scope picker, and the quick-switch strip —
    unioned both. Under ``"tag"`` or ``"metadata"`` those controls could
    therefore hand the operator a slug the filter refused: a dead-end datalist
    value, a dead-end chip with a positive count. The knob is parsed and
    ignored (§4.4), so this follows EVERY offered value — datalist options as
    well as strip chips — and asserts the exact rows behind each, not merely
    how many.
    """
    lithos_lens_config_env.write_text(
        lithos_lens_config_env.read_text()
        + f'\n[lithos-lens.tasks]\nproject_convention = "{posture}"\n'
    )
    fake = _three_project_fake()

    with _client(lithos_lens_config_env, fake) as client:
        board = client.get("/tasks?tag=roadmap-2026-08&since=2026-04-01")
        chips = _project_chip_links(unescape(board.text))
        followed = {slug: client.get(href).text for slug, href in chips.items()}
        # The datalist is the universe over the LOADED rows, before the filters
        # narrow (§5.4), so its values are followed on an unscoped board —
        # which is what typing one into the Project box does.
        offered = {
            slug: client.get(f"/tasks?project={slug}&since=2026-04-01").text
            for slug in _datalist_options(board.text, "projects")
        }

    text = unescape(board.text)
    assert board.status_code == 200
    # Every value the datalist offers lands on exactly that project's rows —
    # including `lithos`, which only `metadata.project` names.
    assert set(offered) == set(_PROJECT_DESTINATIONS), posture
    for slug, body in offered.items():
        assert _open_row_ids(unescape(body)) == _PROJECT_DESTINATIONS[slug], (
            posture,
            slug,
        )
    # The strip states the same universe within the tag scope, and each chip's
    # count is the rows the click actually lands on — by IDENTITY, so a link
    # showing the wrong rows in the right quantity fails here.
    assert _project_chip_counts(text) == {
        "lithos-lens": 2,
        "lithos": 1,
        "lithos-loom": 1,
    }, posture
    scoped = {
        "lithos-lens": {"lens-ready", "lens-stale"},
        "lithos": {"core-ready"},
        "lithos-loom": {"loom-ready"},
    }
    for slug, body in followed.items():
        assert _open_row_ids(unescape(body)) == scoped[slug], (posture, slug)
