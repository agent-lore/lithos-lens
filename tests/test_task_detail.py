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
from lithos_lens.task_detail import TaskDetailData, load_task_detail
from lithos_lens.task_graph import EdgeRecord
from lithos_lens.task_links import (
    LINK_FANOUT_CONCURRENCY,
    LINK_PAGE_SIZE,
    PARENT_BREADCRUMB_MAX_DEPTH,
    LinkedTask,
    LinkPage,
    LinkTarget,
    load_link_page,
)
from lithos_lens.tasks import FindingRecord, NoteRecord, TaskRecord
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
    assert page.tail.remaining == 60 - LINK_PAGE_SIZE
    assert page.tail.truncated
    # Live status, resolved through the same path the level-1 chain uses.
    assert page.links[0].task_id == "deep-000"
    assert page.links[0].status == "open"


def test_load_link_page_leaves_a_short_set_whole() -> None:
    """No tail below the page size: the bound must not cost the ordinary case."""
    lithos = _CountingLinkClient()
    targets = [LinkTarget(f"deep-{index}", "blocks") for index in range(3)]

    page = asyncio.run(load_link_page(lithos, targets))

    assert len(page.links) == 3
    assert page.tail.remaining == 0
    assert not page.tail.truncated


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


# --- Round 2: the bound holds for every agent-sized set on the page --------


def test_load_link_page_takes_no_page_size_override() -> None:
    """correctness/f-001: an overridable page size is a hole in the bound, not
    a convenience — ``targets[:-1]`` is not a smaller page but almost the whole
    set, and the tail copy would then claim a size nobody used.

    ``LINK_PAGE_SIZE`` is authoritative for every caller at every level, so the
    parameter does not exist and a caller that reaches for it fails loudly
    without issuing a single lookup.
    """
    lithos = _CountingLinkClient()
    targets = [LinkTarget(f"deep-{index:04d}", "blocks") for index in range(200)]

    with pytest.raises(TypeError):
        load_link_page(lithos, targets, page_size=-1)  # type: ignore[call-arg]

    assert lithos.get_calls == []
    # And the bound the constant states really is what a normal call applies.
    page = asyncio.run(load_link_page(lithos, targets))
    assert len(lithos.get_calls) == LINK_PAGE_SIZE
    assert page.tail.page_size == LINK_PAGE_SIZE


def test_a_runaway_child_set_renders_one_page_plus_a_counted_tail(
    lithos_lens_config_env: Path,
) -> None:
    """security/f-001: ``lithos_task_children`` costs one round trip, but M is
    agent-chosen and ``include_closed=True`` maximises it, so what needs
    bounding is the RENDER — every row is otherwise concatenated into every
    response, on every auto-refresh tick.

    Bounded through the same page and the same tail path as the blockers, at no
    extra lookup: the records are already in hand.
    """
    extra = 9
    fake = TaskFakeLithosClient()
    child_ids = []
    for index in range(LINK_PAGE_SIZE + extra):
        child_id = f"child-{index:03d}"
        child_ids.append(child_id)
        fake.tasks.append(_task(child_id, title=f"Child {index:03d}"))
    fake.children["open-unclaimed"] = child_ids

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    text = response.text
    assert text.count("data-child-id=") == LINK_PAGE_SIZE
    assert 'data-child-id="child-000"' in text
    assert f'data-child-id="child-{LINK_PAGE_SIZE:03d}"' not in text
    # Visible, not silent — and through the one shared tail path.
    assert 'data-link-tail="children"' in text
    assert f"{extra} more children not shown." in text
    assert f"This task has {LINK_PAGE_SIZE + extra} children in all" in text
    # The bound is a RENDER bound: it costs no extra Lithos reads.
    assert fake.get_calls == ["open-unclaimed"]


def test_a_short_child_set_renders_whole_with_no_tail(
    lithos_lens_config_env: Path,
) -> None:
    """The ordinary case must not pay for the bound."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task("child-only", title="Only child"))
    fake.children["open-unclaimed"] = ["child-only"]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert "Only child" in response.text
    assert 'data-link-tail="children"' not in response.text


def _finding(index: int, *, knowledge_id: str | None = None) -> FindingRecord:
    return FindingRecord(
        id=f"finding-{index:03d}",
        task_id="open-unclaimed",
        agent="worker-a",
        summary=f"Finding {index:03d}",
        knowledge_id=f"note-{index:03d}" if knowledge_id is None else knowledge_id,
        # Timeline order is a plain string sort on created_at, so the stamps
        # have to stay lexicographically ordered for any index the tests use.
        created_at=(
            f"2026-08-01T{10 + index // 3600:02d}"
            f":{(index // 60) % 60:02d}:{index % 60:02d}+00:00"
        ),
    )


class _NoteCountingFake(TaskFakeLithosClient):
    """Counts and times the finding-note lookups a render makes."""

    def __init__(self, *, delay: float = 0.0) -> None:
        super().__init__()
        self.delay = delay
        self.note_reads: list[str] = []
        self.note_read_lengths: list[int | None] = []
        self.in_flight = 0
        self.peak_in_flight = 0

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None:
        self.note_reads.append(knowledge_id)
        self.note_read_lengths.append(max_length)
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        return NoteRecord(id=knowledge_id, title=f"Doc {knowledge_id}", content="")


def test_finding_note_lookups_are_bounded_and_concurrent() -> None:
    """security/f-002: one ``lithos_read`` per distinct knowledge id, with the
    id count agent-chosen and ``lithos_finding_list`` accepting no limit — the
    same unbounded per-render fan-out as the blocker chain, and it used to run
    SEQUENTIALLY, so N reads cost N latencies end to end.

    Now: one page of lookups, under the render's shared limiter and deadline.
    """
    extra = 11
    fake = _NoteCountingFake(delay=0.01)
    fake.findings["open-unclaimed"] = [
        _finding(index) for index in range(LINK_PAGE_SIZE + extra)
    ]

    detail = asyncio.run(load_task_detail(fake, "open-unclaimed"))

    assert len(fake.note_reads) == LINK_PAGE_SIZE
    # Concurrent (not the old sequential walk), and still inside the bound.
    assert 1 < fake.peak_in_flight <= LINK_FANOUT_CONCURRENCY
    # EVERY finding still renders — the bound costs titles, never rows. The
    # timeline's own row count is the one agent-sized set on this page left
    # unbounded ON PURPOSE (task_detail's module docstring names it and says
    # why), so this assertion pins that choice rather than overlooking it.
    assert len(detail.findings) == LINK_PAGE_SIZE + extra
    resolved = [view for view in detail.findings if view.note_title]
    assert len(resolved) == LINK_PAGE_SIZE
    # A finding outside the page shows the generic label and claims NO failure:
    # "could not resolve" is reserved for reads that actually failed.
    unresolved = [view for view in detail.findings if not view.note_title]
    assert all(view.note_error == "" for view in unresolved)
    assert all(view.link_label == "View document" for view in unresolved)


def test_a_stalled_neighbour_read_cannot_pin_a_fan_out_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """security/f-004: count and concurrency were bounded; DURATION was not.

    Nothing below this helper imposes a deadline — ``session.call_tool`` takes
    no timeout and uvicorn sets no request deadline — so eight answerless reads
    would pin every slot and the request task for as long as the session stays
    half-open, and the rest of the page would never run.
    """
    monkeypatch.setattr("lithos_lens.task_links.LINK_READ_TIMEOUT_S", 0.05)

    class StalledClient(_CountingLinkClient):
        async def task_get(self, task_id: str) -> TaskRecord:
            self.get_calls.append(task_id)
            await asyncio.sleep(30)
            raise AssertionError("unreachable")

    lithos = StalledClient()
    targets = [LinkTarget(f"deep-{index:03d}", "blocks") for index in range(10)]

    page = asyncio.run(load_link_page(lithos, targets))

    # The page still answers, every line rendered, none of them claiming a
    # status it never read.
    assert len(page.links) == 10
    assert all(link.unresolved for link in page.links)


def test_a_stalled_parent_read_does_not_stall_the_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The breadcrumb walk is sequential, so one answerless read there stalls
    the whole render rather than one slot of it."""
    monkeypatch.setattr("lithos_lens.task_links.LINK_READ_TIMEOUT_S", 0.05)

    class StalledParentFake(TaskFakeLithosClient):
        async def task_get(self, task_id: str) -> TaskRecord:
            if task_id == "slow-parent":
                await asyncio.sleep(30)
            return await super().task_get(task_id)

    fake = StalledParentFake()
    fake.tasks.append(_task("slow-parent", title="Slow parent"))
    _link(fake, "slow-parent", "open-unclaimed", "parent_child")

    detail = asyncio.run(load_task_detail(fake, "open-unclaimed"))

    assert detail.task is not None
    assert detail.breadcrumb.ancestors == ()
    assert detail.breadcrumb.incomplete


class _CountingId(str):
    """A knowledge id that counts how often it is compared for equality.

    Turns the dedup's COMPLEXITY into something a test can assert without a
    stopwatch — timing thresholds are flaky on a shared CI box, and the
    difference here is algorithmic, not constant-factor. A ``not in <list>``
    scan compares each id against every id kept so far (Θ(N²)); a hash-based
    dedup compares only when two ids collide in the table (≈0).
    """

    comparisons = 0

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return str.__eq__(self, other)

    def __hash__(self) -> int:
        return str.__hash__(self)


def test_finding_note_dedup_is_not_quadratic() -> None:
    """security/f-005: the round-2 note bound deduped ids with a linear list
    scan, making the loop Θ(N²) over an agent-chosen N.

    The loop holds no ``await``, so that is not a slow render — it blocks the
    single event loop for the whole worker, stalling every other in-flight
    request (the dashboard, the SSE stream, /health) with it, on every
    auto-refresh tick of one open tab. ``bounded_page`` cannot rescue it: it
    slices the RESULT, so the scan runs at full N first.
    """
    count = 1200
    fake = _NoteCountingFake()
    fake.findings["open-unclaimed"] = [
        _finding(index, knowledge_id=_CountingId(f"note-{index:05d}"))
        for index in range(count)
    ]

    _CountingId.comparisons = 0
    detail = asyncio.run(load_task_detail(fake, "open-unclaimed"))

    # Linear in N with generous headroom: the list scan costs ~N²/2 (~719k
    # here), a hash-based dedup costs collisions only.
    assert _CountingId.comparisons < 10 * count
    # And the fix must not have cost the behaviour the dedup is there for:
    # one read per distinct id, spent on the NEWEST page of them.
    assert len(fake.note_reads) == LINK_PAGE_SIZE
    assert fake.note_reads == [
        f"note-{index:05d}"
        for index in range(count - 1, count - 1 - LINK_PAGE_SIZE, -1)
    ]
    assert len(detail.findings) == count


def test_finding_note_dedup_reads_each_id_once_however_often_it_recurs() -> None:
    """The dedup exists to spend one ``lithos_read`` per DISTINCT id, and every
    finding carrying that id gets the title the single read returned — however
    far apart in the timeline they sit."""
    fake = _NoteCountingFake()
    fake.findings["open-unclaimed"] = [
        _finding(0, knowledge_id="note-b"),
        _finding(1, knowledge_id="note-a"),
        _finding(2, knowledge_id="note-b"),
        _finding(3, knowledge_id=""),
    ]

    detail = asyncio.run(load_task_detail(fake, "open-unclaimed"))

    assert fake.note_reads == ["note-b", "note-a"]
    assert [view.note_title for view in detail.findings] == [
        "Doc note-b",
        "Doc note-a",
        "Doc note-b",
        "",
    ]


def test_a_cancelled_provenance_link_is_not_called_unsatisfiable(
    lithos_lens_config_env: Path,
) -> None:
    """correctness/f-002, security/f-007: "unsatisfiable" is a BLOCKER verdict
    — this dependency can never be met, so the task can never run.

    One partial renders all three neighbour lists, so a status-only rule leaked
    the verdict into both provenance directions: a cancelled follow-on came out
    carrying the page's loudest treatment on a section that is purely
    historical, on a page that otherwise said nothing was blocking the task.
    A cancelled source or follow-on is merely cancelled — nothing waits on it.
    """
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [
            _task("dead-source", title="Cancelled source", status="cancelled"),
            _task("dead-follow-on", title="Cancelled follow-on", status="cancelled"),
        ]
    )
    _link(fake, "dead-source", "open-unclaimed", "discovered_from")
    _link(fake, "open-unclaimed", "dead-follow-on", "discovered_from")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    text = response.text
    # Both still render, and still say they were cancelled.
    assert "Cancelled source" in text and "Cancelled follow-on" in text
    assert text.count('class="badge badge-cancelled">cancelled</span>') == 2
    # But neither claims to make this task impossible to run — the page says
    # nothing is blocking it, and the chips must not contradict that.
    assert "data-link-unsatisfiable" not in text
    assert "Nothing is blocking this task." in text


def test_the_unsatisfiable_verdict_still_fires_on_a_cancelled_blocker(
    lithos_lens_config_env: Path,
) -> None:
    """The other half of the same rule: gating it on the edge type must not
    cost the call-out on the list where it is the whole point."""
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [
            _task("dead-pred", title="Cancelled predecessor", status="cancelled"),
            _task(
                "dead-gate",
                title="Cancelled gate",
                status="cancelled",
                task_type="gate",
                metadata={"gate_type": "human"},
            ),
        ]
    )
    _link(fake, "dead-pred", "open-unclaimed", "blocks")
    _link(fake, "dead-gate", "open-unclaimed", "waits_on_gate")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    # Both blocking edge types carry the verdict: a cancelled gate strands its
    # waiter exactly as a cancelled predecessor does.
    assert response.text.count("data-link-unsatisfiable") == 2


# --- Review round 5 (lithos-loom develop review 53) ------------------------


def test_finding_note_titles_use_the_cheap_truncated_read() -> None:
    """security/f-002: the title lookup asked for whole documents.

    §5.6 names the call: ``lithos_read(id=knowledge_id, max_length=1)``, "the
    cheap title fetch" — a truncated read still returns complete frontmatter,
    so the title arrives either way. Without ``max_length`` Lithos serialises
    each document's entire body, up to a page of whole documents per render,
    over the shared MCP session, to populate one string per row.
    """
    fake = _NoteCountingFake()
    fake.findings["open-unclaimed"] = [_finding(index) for index in range(3)]

    detail = asyncio.run(load_task_detail(fake, "open-unclaimed"))

    assert len(fake.note_reads) == 3
    # Every one of them truncated — not "most", and not defaulted.
    assert fake.note_read_lengths == [1, 1, 1]
    # And the cheap read still yields the titles the page renders.
    assert all(view.note_title for view in detail.findings)


def test_the_note_lookup_page_is_spent_on_the_newest_findings() -> None:
    """correctness/f-006: the bounded lookup resolved the OLDEST page of ids.

    Some end has to go without: "View document" is §5.6's read-FAILURE
    fallback, so whichever findings fall outside the page wear a label the
    spec reserves for something else. §5.6 also says a long timeline MAY
    collapse OLDER findings behind a disclosure — so the old end is the one
    the spec is already willing to lose, and spending the page there puts the
    borrowed label where it does least damage. Resolving the oldest instead
    degraded exactly the rows an operator opening a task is reading.
    """
    extra = 7
    total = LINK_PAGE_SIZE + extra
    fake = _NoteCountingFake()
    fake.findings["open-unclaimed"] = [_finding(index) for index in range(total)]

    detail = asyncio.run(load_task_detail(fake, "open-unclaimed"))

    # Still exactly one page of reads: the bound is unchanged, only its aim.
    assert len(fake.note_reads) == LINK_PAGE_SIZE
    # The timeline still renders oldest-first and in full.
    assert len(detail.findings) == total
    titled = [bool(view.note_title) for view in detail.findings]
    # The generic label lands on the oldest rows, the titles on the newest.
    assert titled == [False] * extra + [True] * LINK_PAGE_SIZE


def test_a_completed_blocker_is_marked_satisfied_rather_than_left_looking_current(
    lithos_lens_config_env: Path,
) -> None:
    """Completing a predecessor does not delete its ``blocks`` edge — the edge
    is the durable record that the dependency existed — so it stays in the set
    "Why can't this run" is reconstructed from. Rendered like any other
    blocker, it answers that question with something that finished.

    Marked, not filtered: the tail counts against the edge set, so dropping
    rows here would make the remainder arithmetic describe a set the page
    never showed, and would delete what this task waited on — most of what the
    section is read for once the task is unblocked.
    """
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [
            _task("pred-done", title="Finished predecessor", status="completed"),
            _task("pred-live", title="Live predecessor"),
        ]
    )
    _link(fake, "pred-done", "open-unclaimed", "blocks")
    _link(fake, "pred-live", "open-unclaimed", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    text = response.text
    # Both rows survive — the completed one is evidence, not noise.
    assert "Finished predecessor" in text and "Live predecessor" in text
    # Exactly one of them is called out as no longer holding the task back.
    assert text.count("data-link-satisfied") == 1
    # And "satisfied" is not the same claim as "unsatisfiable": a completed
    # blocker is resolved, a cancelled one is a dead end.
    assert "data-link-unsatisfiable" not in text


def test_a_satisfied_verdict_is_never_claimed_on_a_provenance_link(
    lithos_lens_config_env: Path,
) -> None:
    """The mirror of the unsatisfiable rule. One partial renders all three
    neighbour lists, so a status-only rule would tag every completed source
    task and follow-on "satisfied" — a dependency verdict on a section that
    records no dependency."""
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [
            _task("done-source", title="Finished source", status="completed"),
            _task("done-follow-on", title="Finished follow-on", status="completed"),
        ]
    )
    _link(fake, "done-source", "open-unclaimed", "discovered_from")
    _link(fake, "open-unclaimed", "done-follow-on", "discovered_from")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    text = response.text
    assert "Finished source" in text and "Finished follow-on" in text
    assert "data-link-satisfied" not in text


class _GatingReadRecorder(TaskFakeLithosClient):
    """Records how many of the five gating reads overlap in flight."""

    def __init__(self, *, delay: float = 0.01) -> None:
        super().__init__()
        self.delay = delay
        self.in_flight = 0
        self.peak_in_flight = 0

    async def _tick(self):
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1

    async def task_get(self, task_id: str) -> TaskRecord:
        await self._tick()
        return await super().task_get(task_id)

    async def task_status(self, task_id: str):
        await self._tick()
        return await super().task_status(task_id)

    async def task_edge_list(self, task_id: str, **kwargs):
        await self._tick()
        return await super().task_edge_list(task_id, **kwargs)

    async def list_findings(self, task_id: str, **kwargs):
        await self._tick()
        return await super().list_findings(task_id, **kwargs)

    async def task_children(self, task_id: str, **kwargs):
        await self._tick()
        return await super().task_children(task_id, **kwargs)


def test_the_gating_reads_are_issued_concurrently() -> None:
    """correctness/f-007: §5.5's data contract says ``task_get`` +
    ``task_status`` + ``task_edge_list`` + ``finding_list`` are "gathered
    concurrently"; ``task_get`` was awaited to completion first.

    That bought a short-circuit on the one path nobody is waiting for — a
    deleted task's 404 — and charged every real page view an extra serial
    round trip for it. Asserted on overlap, not on elapsed time: the defect is
    structural, and a clock threshold would be flaky on a shared box.
    """
    fake = _GatingReadRecorder()

    asyncio.run(load_task_detail(fake, "open-unclaimed"))

    # All five in flight together, not one-then-four.
    assert fake.peak_in_flight == 5


def test_a_stalled_gating_read_cannot_hang_the_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """security/f-001: the same change that deadlined every neighbour read left
    the five GATING reads bare.

    Nothing under them imposes one — ``session.call_tool`` takes no timeout,
    ``SESSION_WAIT_TIMEOUT_S`` covers only session establishment, uvicorn
    sets no request deadline — so a half-open session pinned the request task
    on the reads the render cannot even start without, which is a strictly
    worse place to be unbounded than the fan-out behind them.
    """
    monkeypatch.setattr("lithos_lens.task_detail.LINK_READ_TIMEOUT_S", 0.05)

    class _StalledChildren(TaskFakeLithosClient):
        async def task_children(self, task_id: str, **kwargs):
            await asyncio.Event().wait()  # never answers
            raise AssertionError("unreachable")

    fake = _StalledChildren()

    async def run() -> TaskDetailData:
        async with asyncio.timeout(5):
            return await load_task_detail(fake, "open-unclaimed")

    detail = asyncio.run(run())

    # The render completes, and degrades only the section that stalled.
    assert detail.task is not None
    assert detail.children_state.value == "error"
    assert "Could not load child tasks." in detail.errors
    # The rest of the page is intact — one stalled read is not a failed page.
    assert detail.status_state.value == "ok"
    assert detail.relations_state.value == "ok"


def test_reserved_characters_in_ids_are_encoded_into_generated_links(
    lithos_lens_config_env: Path,
) -> None:
    """security/f-005: task ids and knowledge ids are arbitrary non-empty
    strings off agent-written payloads, and both were interpolated into hrefs
    without full encoding — ``quote``'s default ``safe="/"`` for the task link,
    nothing at all for the finding's document link.

    Autoescaping makes the attribute safe to EMBED; it says nothing about what
    the URL then addresses. A ``?`` or ``#`` truncates the path and routes
    somewhere else; a ``/`` invents a segment.
    """
    fake = TaskFakeLithosClient()
    # A slash is the character the two encodings actually disagree about:
    # quote's default safe="/" passes it through, turning one id into two path
    # segments and a link to somewhere else entirely.
    fake.tasks.append(_task("team/pred", title="Awkward predecessor"))
    _link(fake, "team/pred", "open-unclaimed", "blocks")
    fake.findings["open-unclaimed"] = [
        _finding(0, knowledge_id="doc?x#y"),
    ]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    text = response.text
    # The blocker link is ONE path segment naming the whole id.
    assert "/tasks/team%2Fpred" in text
    assert 'href="/tasks/team/pred' not in text
    # The finding's document link addresses the document, not a prefix of it:
    # unescaped, the "?" ends the path and "#" starts a fragment, so the href
    # resolved to /note/doc with the rest of the id read as a query string.
    assert "/note/doc%3Fx%23y?task=open-unclaimed" in text
    assert 'href="/note/doc?x' not in text


# --- The heading is a claim about the present ------------------------------


def _blocker_heading(text: str) -> str:
    """The <h2> the blocker section actually rendered."""
    section = text.split("data-blocker-chain", 1)[1]
    return section.split("<h2>", 1)[1].split("</h2>", 1)[0].strip()


def test_a_task_whose_blockers_have_all_completed_is_not_headed_blocked_by(
    lithos_lens_config_env: Path,
) -> None:
    """ "Blocked by" is a claim about the PRESENT, and a completed predecessor
    keeps its ``blocks`` edge — so the section's set outlives the blockage it
    records. Heading a list of finished dependencies "Blocked by" states the
    opposite of what its own rows say."""
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [
            _task("pred-done-a", title="First predecessor", status="completed"),
            _task("pred-done-b", title="Second predecessor", status="completed"),
        ]
    )
    _link(fake, "pred-done-a", "open-unclaimed", "blocks")
    _link(fake, "pred-done-b", "open-unclaimed", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    text = response.text
    assert _blocker_heading(text) == "Dependencies"
    # The rows are still there, and still say what they are.
    assert "First predecessor" in text and "Second predecessor" in text
    assert text.count("data-link-satisfied") == 2


def test_one_live_blocker_among_completed_ones_still_heads_the_section_blocked_by(
    lithos_lens_config_env: Path,
) -> None:
    """The other half of the rule: the heading must not soften just because
    MOST of the set has finished."""
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [
            _task("pred-done", title="Finished predecessor", status="completed"),
            _task("pred-live", title="Live predecessor"),
        ]
    )
    _link(fake, "pred-done", "open-unclaimed", "blocks")
    _link(fake, "pred-live", "open-unclaimed", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert _blocker_heading(response.text) == "Blocked by"


def test_a_cancelled_blocker_still_heads_the_section_blocked_by(
    lithos_lens_config_env: Path,
) -> None:
    """A dead end is not a satisfied dependency. "unsatisfiable" is the
    strongest form of "this cannot run", so it must not read as cleared."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task("pred-dead", title="Cancelled spike", status="cancelled"))
    _link(fake, "pred-dead", "open-unclaimed", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    text = response.text
    assert _blocker_heading(text) == "Blocked by"
    assert "data-link-unsatisfiable" in text


def test_a_blocker_whose_status_never_arrived_is_not_read_as_cleared(
    lithos_lens_config_env: Path,
) -> None:
    """An unresolved row means the page does not KNOW the blocker is done.

    Silence is the one answer that must not be optimistic here: "Dependencies"
    over a row whose status could not be read would tell the operator the task
    is free to run on the strength of a failed request.
    """
    fake = TaskFakeLithosClient()
    # The edge names a predecessor the task list has no record of, so its
    # task_get raises and the row renders with no status claim.
    _link(fake, "pred-missing", "open-unclaimed", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    text = response.text
    assert "data-link-unresolved" in text
    assert _blocker_heading(text) == "Blocked by"


def test_a_truncated_blocker_page_is_never_read_as_cleared(
    lithos_lens_config_env: Path,
) -> None:
    """Every RENDERED blocker completed, but the page truncated — so the
    statuses behind the tail were never read.

    Under-claiming blockage on a "why can't this run?" page is the failure the
    section exists to prevent, so "may be blocked" resolves to "Blocked by".
    The tail's own copy ("N more blockers not shown") then stays consistent
    with the heading for free.
    """
    overflow = 4
    fake = TaskFakeLithosClient()
    for index in range(LINK_PAGE_SIZE + overflow):
        blocker_id = f"pred-{index:03d}"
        fake.tasks.append(_task(blocker_id, status="completed"))
        _link(fake, blocker_id, "open-unclaimed", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    text = response.text
    assert text.count("data-link-satisfied") == LINK_PAGE_SIZE
    assert f'data-link-remaining="{overflow}"' in text
    assert _blocker_heading(text) == "Blocked by"


def test_a_task_with_no_blocker_edges_is_not_headed_blocked_by(
    lithos_lens_config_env: Path,
) -> None:
    """The empty case obeys the same rule — the heading claimed blockage the
    body immediately denied."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    text = response.text
    assert _blocker_heading(text) == "Dependencies"
    assert "Nothing is blocking this task." in text


def test_a_truncated_provenance_page_says_nothing_about_being_blocked() -> None:
    """``still_blocking``'s tail clause is qualified on the page holding
    BLOCKING links. One helper builds all three neighbour lists, so an
    unqualified "a tail means maybe" would let a task with a runaway set of
    spawned follow-ons — and no blockers at all — report itself blocked.
    """
    spawned = LinkPage(
        links=tuple(
            LinkedTask(task_id=f"follow-{index}", edge_type="discovered_from")
            for index in range(LINK_PAGE_SIZE)
        ),
        total=LINK_PAGE_SIZE + 10,
    )

    assert spawned.tail.truncated
    assert spawned.unsatisfied == ()
    assert spawned.still_blocking is False
