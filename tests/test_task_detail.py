"""T1 slice 7 — the graph-native task detail page (text-first).

The page is now assembled from ``lithos_task_get`` + ``lithos_task_status`` +
``lithos_task_edge_list(direction="both")`` + ``lithos_finding_list`` +
``lithos_task_children``, so these tests pin what the EDGES turn into: the
level-1 blocker chain with live status, the parent breadcrumb, the children
table, spawn provenance in both directions, and the type badges.

They also pin the bound on the fan-out those edges imply. A task's edge count
is agent-controlled and Lithos enforces no maximum, so "resolve every
blocker's status" would be an unbounded ``lithos_task_get`` fan-out on the
shared MCP session. ``task_links`` renders a first page plus a counted tail
instead; the assertions below are on the CALL COUNT and the tail copy, not on
reading the code.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.task_detail import load_task_detail
from lithos_lens.task_graph import EdgeRecord
from lithos_lens.task_links import (
    LINK_FANOUT_CONCURRENCY,
    LINK_PAGE_SIZE,
    PARENT_BREADCRUMB_MAX_DEPTH,
    LinkTarget,
    load_link_page,
)
from lithos_lens.tasks import TaskRecord
from lithos_lens.web import create_app
from tests.test_tasks_mvp import TaskFakeLithosClient


def _client(config_path: Path, fake: TaskFakeLithosClient) -> TestClient:
    config = load_config(config_path)
    app = create_app(config, lithos_client_factory=lambda _: fake)
    return TestClient(app)


def _task(
    task_id: str,
    *,
    title: str = "",
    status: str = "open",
    task_type: str = "task",
    metadata: dict[str, object] | None = None,
    outcome: str = "",
    resolved_at: str = "",
) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        title=title or f"Title {task_id}",
        status=status,  # type: ignore[arg-type]
        created_by="planner",
        created_at="2026-08-01T10:00:00+00:00",
        task_type=task_type,
        metadata=dict(metadata or {}),
        outcome=outcome,
        resolved_at=resolved_at,
    )


def _edge(from_id: str, to_id: str, edge_type: str) -> tuple[EdgeRecord, EdgeRecord]:
    """The same edge as both endpoints see it (``direction`` is relative)."""
    return (
        EdgeRecord(
            from_task_id=from_id,
            to_task_id=to_id,
            type=edge_type,
            direction="outgoing",
        ),
        EdgeRecord(
            from_task_id=from_id,
            to_task_id=to_id,
            type=edge_type,
            direction="incoming",
        ),
    )


def _link(fake: TaskFakeLithosClient, from_id: str, to_id: str, edge_type: str) -> None:
    outgoing, incoming = _edge(from_id, to_id, edge_type)
    fake.edges.setdefault(from_id, []).append(outgoing)
    fake.edges.setdefault(to_id, []).append(incoming)


# --- Acceptance: blockers with live status ---------------------------------


def test_blocked_task_detail_lists_every_blocker_with_its_live_status(
    lithos_lens_config_env: Path,
) -> None:
    """Headline acceptance: the detail page answers "why can't this run?" with
    one line per immediate blocker, each carrying the blocker's LIVE status
    read from Lithos — not the status the edge was written with."""
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [
            _task("pred-open", title="Design schema"),
            _task("pred-cancelled", title="Old spike", status="cancelled"),
            _task(
                "gate-review",
                title="Human review",
                task_type="gate",
                metadata={"gate_type": "human"},
            ),
        ]
    )
    _link(fake, "pred-open", "open-unclaimed", "blocks")
    _link(fake, "pred-cancelled", "open-unclaimed", "blocks")
    _link(fake, "gate-review", "open-unclaimed", "waits_on_gate")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    text = response.text
    assert "Blocked by" in text
    # Every blocker is named, with the status Lithos reports for it now.
    assert 'data-link-target="pred-open"' in text
    assert 'data-link-target="pred-cancelled"' in text
    assert 'data-link-target="gate-review"' in text
    assert "Design schema" in text and "Old spike" in text and "Human review" in text
    assert 'class="badge badge-cancelled">cancelled</span>' in text
    # A cancelled predecessor can never complete: called out, not just coloured.
    assert "data-link-unsatisfiable" in text
    # The gate carries its type, so "what would resolving this unblock" is legible.
    assert 'data-link-type="gate"' in text
    assert "gate: human" in text
    # No tail: three blockers is well inside one page.
    assert "data-link-tail" not in text


def test_task_with_no_blockers_says_so(lithos_lens_config_env: Path) -> None:
    """The affirmative answer matters as much as the list: an open task with no
    incoming blocking edges must say nothing is blocking it, not render blank."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    assert "Nothing is blocking this task." in response.text


def test_a_blocker_whose_status_read_fails_still_renders_without_a_claim(
    lithos_lens_config_env: Path,
) -> None:
    """The edge says the blocker exists, so dropping the line would understate
    the answer. It renders — and makes no status claim."""
    fake = TaskFakeLithosClient()
    # No task record for "ghost-pred", so task_get answers task_not_found.
    _link(fake, "ghost-pred", "open-unclaimed", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    assert 'data-link-target="ghost-pred"' in response.text
    assert "data-link-unresolved" in response.text


# --- Acceptance (a)+(b): the bounded first page and its visible tail --------


def _fan_out_fixture(fake: TaskFakeLithosClient, count: int) -> None:
    """Give ``open-unclaimed`` ``count`` blockers — the runaway-agent shape."""
    for index in range(count):
        blocker_id = f"pred-{index:03d}"
        fake.tasks.append(_task(blocker_id, title=f"Predecessor {index:03d}"))
        _link(fake, blocker_id, "open-unclaimed", "blocks")


def test_blocker_fan_out_is_bounded_to_one_page_of_task_get_calls(
    lithos_lens_config_env: Path,
) -> None:
    """Acceptance (a): a task with more blockers than the page size renders the
    first page with live statuses and issues only page-size ``task_get``
    lookups for them.

    Asserted on the recorded calls, because the defect this bounds is the
    number of round trips on the shared MCP session — not the size of the HTML.
    """
    fake = TaskFakeLithosClient()
    _fan_out_fixture(fake, LINK_PAGE_SIZE + 17)

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    # One task_get for the page's own task, then exactly one page of blockers.
    assert fake.get_calls[0] == "open-unclaimed"
    assert len(fake.get_calls) == 1 + LINK_PAGE_SIZE
    assert fake.get_calls[1:] == [
        f"pred-{index:03d}" for index in range(LINK_PAGE_SIZE)
    ]
    # The first page really is rendered with live status, not just counted.
    assert 'data-link-target="pred-000"' in response.text
    assert f'data-link-target="pred-{LINK_PAGE_SIZE - 1:03d}"' in response.text
    assert f'data-link-target="pred-{LINK_PAGE_SIZE:03d}"' not in response.text


def test_the_blocker_tail_says_how_many_more_blockers_exist(
    lithos_lens_config_env: Path,
) -> None:
    """Acceptance (b): the overflow is graceful and VISIBLE. A silently
    truncated blocker list on a "why can't this task run?" page is worse than a
    slow one, so the tail names the remainder and the total — the same contract
    as the dashboard's frontier-limit accuracy banner."""
    extra = 17
    fake = TaskFakeLithosClient()
    _fan_out_fixture(fake, LINK_PAGE_SIZE + extra)

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    text = response.text
    assert 'data-link-tail="blockers"' in text
    assert f'data-link-remaining="{extra}"' in text
    assert f"{extra} more blockers not shown." in text
    assert f"This task has {LINK_PAGE_SIZE + extra} blockers in all" in text


# --- Acceptance (c): ONE reusable helper, callable at any level -------------


class _CountingLinkClient:
    """A client that records its ``task_get`` fan-out and its concurrency."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.get_calls: list[str] = []
        self.in_flight = 0
        self.peak_in_flight = 0

    async def task_get(self, task_id: str) -> TaskRecord:
        self.get_calls.append(task_id)
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        return _task(task_id)

    async def task_edge_list(
        self,
        task_id: str,
        *,
        direction: str = "both",
        types: list[str] | None = None,
    ) -> list[EdgeRecord]:
        return []


def test_load_link_page_bounds_a_blocker_set_at_any_level() -> None:
    """Acceptance (c): the pagination is ONE helper with ONE page-size
    constant, callable for a blocker set at any level.

    T1-S8 expands a blocker's own blockers; this is that call — a bare blocker
    set with no detail page around it — and it gets the same page, the same
    call bound and the same remainder count.
    """
    lithos = _CountingLinkClient()
    targets = [LinkTarget(f"deep-{index:03d}", "blocks") for index in range(60)]

    page = asyncio.run(load_link_page(lithos, targets))

    assert len(page.links) == LINK_PAGE_SIZE
    assert len(lithos.get_calls) == LINK_PAGE_SIZE
    assert page.total == 60
    assert page.remaining == 60 - LINK_PAGE_SIZE
    assert page.truncated
    # Live status, resolved through the same path the level-1 chain uses.
    assert page.links[0].task_id == "deep-000"
    assert page.links[0].status == "open"


def test_load_link_page_leaves_a_short_set_whole() -> None:
    """No tail below the page size: the bound must not cost the ordinary case."""
    lithos = _CountingLinkClient()
    targets = [LinkTarget(f"deep-{index}", "blocks") for index in range(3)]

    page = asyncio.run(load_link_page(lithos, targets))

    assert len(page.links) == 3
    assert page.remaining == 0
    assert not page.truncated


# --- Acceptance (d): the concurrency bound is retained alongside the count --


def test_link_page_fan_out_never_exceeds_the_concurrency_bound() -> None:
    """Acceptance (d): belt and braces. The count bound caps the render's TOTAL
    work; this caps how much of it hits the shared MCP session at once, which
    the count bound alone does not (a single gather would dump the whole page).
    """
    lithos = _CountingLinkClient(delay=0.01)
    targets = [LinkTarget(f"deep-{index:03d}", "blocks") for index in range(60)]

    page = asyncio.run(load_link_page(lithos, targets))

    assert len(page.links) == LINK_PAGE_SIZE
    assert lithos.peak_in_flight == LINK_FANOUT_CONCURRENCY


def test_one_render_shares_a_single_limiter_across_its_link_pages(
    lithos_lens_config_env: Path,
) -> None:
    """A per-PAGE limiter would let one render run N pages x N slots. The
    detail load shares one limiter, so the bound is on the whole render."""

    class SlowFake(TaskFakeLithosClient):
        def __init__(self) -> None:
            super().__init__()
            self.in_flight = 0
            self.peak_in_flight = 0

        async def task_get(self, task_id: str) -> TaskRecord:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            try:
                await asyncio.sleep(0.01)
                return await super().task_get(task_id)
            finally:
                self.in_flight -= 1

    fake = SlowFake()
    # Blockers AND both provenance directions, all over the page size, so three
    # pages are in flight together.
    for index in range(LINK_PAGE_SIZE + 5):
        for prefix, edge_type, forward in (
            ("pred", "blocks", False),
            ("src", "discovered_from", False),
            ("spawn", "discovered_from", True),
        ):
            other = f"{prefix}-{index:03d}"
            fake.tasks.append(_task(other))
            if forward:
                _link(fake, "open-unclaimed", other, edge_type)
            else:
                _link(fake, other, "open-unclaimed", edge_type)

    asyncio.run(load_task_detail(fake, "open-unclaimed"))

    assert fake.peak_in_flight <= LINK_FANOUT_CONCURRENCY
    # And the count bound still holds per page: one task_get for the page's own
    # task plus one page each for blockers, sources and follow-ons.
    assert len(fake.get_calls) == 1 + 3 * LINK_PAGE_SIZE


# --- Provenance, hierarchy, badges, outcome --------------------------------


def test_a_spawned_task_shows_the_task_it_was_discovered_from(
    lithos_lens_config_env: Path,
) -> None:
    """Headline acceptance: emergent work is traceable to its origin."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task("source-task", title="Cut over ingest path"))
    _link(fake, "source-task", "open-unclaimed", "discovered_from")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    assert "Discovered while working on" in response.text
    assert 'data-link-list="discovered-from"' in response.text
    assert "Cut over ingest path" in response.text


def test_a_source_task_lists_the_follow_ons_it_spawned(
    lithos_lens_config_env: Path,
) -> None:
    """Provenance runs BOTH directions off the one edge list."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task("follow-on", title="Backfill historical series"))
    _link(fake, "open-unclaimed", "follow-on", "discovered_from")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    assert "Spawned follow-ons" in response.text
    assert 'data-link-list="spawned"' in response.text
    assert "Backfill historical series" in response.text


def test_detail_renders_the_parent_breadcrumb_up_to_the_root(
    lithos_lens_config_env: Path,
) -> None:
    """Single-parent forest, so the trail up is a chain, root first."""
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [
            _task("epic-root", title="Auth rework", task_type="epic"),
            _task("mid-parent", title="Session store"),
        ]
    )
    _link(fake, "epic-root", "mid-parent", "parent_child")
    _link(fake, "mid-parent", "open-unclaimed", "parent_child")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    trail = response.text
    assert "data-parent-breadcrumb" in trail
    assert trail.index("Auth rework") < trail.index("Session store")
    assert "data-breadcrumb-incomplete" not in trail


def test_the_parent_walk_stops_on_a_cycle_and_says_the_trail_is_incomplete(
    lithos_lens_config_env: Path,
) -> None:
    """``parent_child`` is meant to be an acyclic forest, but the edges are
    agent-written: a cycle must stop the walk rather than loop it forever, and
    the trail must not then imply its first entry is the root."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task("loop-parent", title="Loop parent"))
    _link(fake, "loop-parent", "open-unclaimed", "parent_child")
    _link(fake, "open-unclaimed", "loop-parent", "parent_child")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    assert "data-breadcrumb-incomplete" in response.text
    # The walk read the one real parent and stopped; it did not spin.
    assert fake.get_calls.count("loop-parent") == 1


def test_the_parent_walk_stops_at_the_depth_bound() -> None:
    """A pathologically deep chain bounds the SEQUENTIAL read chain too."""
    fake = TaskFakeLithosClient()
    depth = PARENT_BREADCRUMB_MAX_DEPTH + 4
    fake.tasks.append(_task("chain-000"))
    for index in range(1, depth):
        fake.tasks.append(_task(f"chain-{index:03d}"))
        _link(fake, f"chain-{index:03d}", f"chain-{index - 1:03d}", "parent_child")
    _link(fake, "chain-000", "open-unclaimed", "parent_child")

    detail = asyncio.run(load_task_detail(fake, "open-unclaimed"))

    assert len(detail.breadcrumb.ancestors) == PARENT_BREADCRUMB_MAX_DEPTH
    assert detail.breadcrumb.incomplete


def test_detail_renders_the_children_table_with_per_child_status(
    lithos_lens_config_env: Path,
) -> None:
    """Hierarchy is navigable downward too — closed children included, so a
    child's status is legible rather than the row silently vanishing."""
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [
            _task("child-open", title="Write the migration"),
            _task("child-done", title="Agree the schema", status="completed"),
        ]
    )
    fake.children["open-unclaimed"] = ["child-open", "child-done"]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    text = response.text
    assert 'data-child-id="child-open"' in text
    assert 'data-child-id="child-done"' in text
    assert "Agree the schema" in text


@pytest.mark.parametrize(
    ("task_type", "metadata", "expected"),
    [
        ("task", {}, 'data-task-type="task"'),
        ("epic", {}, 'data-task-type="epic"'),
        ("gate", {"gate_type": "human"}, 'data-gate-type="human"'),
    ],
)
def test_detail_carries_the_task_type_badge(
    lithos_lens_config_env: Path,
    task_type: str,
    metadata: dict[str, object],
    expected: str,
) -> None:
    fake = TaskFakeLithosClient()
    fake.tasks = [
        replace(
            task,
            task_type=task_type,
            metadata={**task.metadata, **metadata},
        )
        if task.id == "open-unclaimed"
        else task
        for task in fake.tasks
    ]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    assert expected in response.text


def test_a_resolved_task_shows_its_outcome_and_resolution_time(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        _task(
            "done-outcome",
            title="Shipped work",
            status="completed",
            outcome="Cut over cleanly; no rollback needed.",
            resolved_at="2026-08-20T12:00:00+00:00",
        )
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/done-outcome")

    assert response.status_code == 200
    text = response.text
    assert "data-resolved-at" in text
    assert "2026-08-20T12:00:00+00:00" in text
    assert "Cut over cleanly; no rollback needed." in text


def test_a_failed_task_read_is_not_reported_as_a_missing_task(
    lithos_lens_config_env: Path,
) -> None:
    """``task_not_found`` is Lithos's ANSWER; any other failure is a failed
    read, and telling the operator the task does not exist would be a lie."""

    class BrokenGetFake(TaskFakeLithosClient):
        async def task_get(self, task_id: str) -> TaskRecord:
            raise RuntimeError("transport blew up")

    fake = BrokenGetFake()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    assert "Could not load this task from Lithos." in response.text
    assert "Lithos has no task with this id" not in response.text


def test_a_failed_edge_read_degrades_only_the_blocker_section(
    lithos_lens_config_env: Path,
) -> None:
    """One failed read must not take the page down, and must not read as
    "nothing is blocking this task"."""

    class BrokenEdgeFake(TaskFakeLithosClient):
        async def task_edge_list(
            self,
            task_id: str,
            *,
            direction: str = "both",
            types: list[str] | None = None,
        ) -> list[EdgeRecord]:
            raise RuntimeError("edge read failed")

    fake = BrokenEdgeFake()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    text = response.text
    assert "Blockers unavailable. Refresh this task to retry." in text
    assert "Nothing is blocking this task." not in text
    # The rest of the page still renders.
    assert "Unclaimed open task" in text
