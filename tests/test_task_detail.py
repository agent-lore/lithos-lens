"""T1 slice 7 — the graph-native task detail page.

Covers the rebase itself (``lithos_task_get`` + status + edges + children +
findings, with ``find_task``'s three-list scan deleted) and the bound on the
per-blocker fan-out: every related-task list on the page renders a BOUNDED
FIRST PAGE plus a tail that states the remainder, through one shared helper
(``first_page`` / ``load_link_page``) that T1-S8's deeper blocker levels reach
via ``load_blocker_page``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.task_detail import (
    DETAIL_FANOUT_CONCURRENCY,
    DETAIL_PAGE_SIZE,
    Breadcrumb,
    first_page,
    load_blocker_page,
    load_task_detail,
    task_type_badge,
)
from lithos_lens.task_graph import EdgeRecord
from lithos_lens.tasks import TaskRecord
from lithos_lens.web import create_app
from tests.test_tasks_mvp import TaskFakeLithosClient


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _client(config_path: Path, fake: TaskFakeLithosClient) -> TestClient:
    config = load_config(config_path)
    app = create_app(config, lithos_client_factory=lambda _: fake)
    return TestClient(app)


def _blocks(blocker_id: str, task_id: str) -> EdgeRecord:
    """An incoming ``blocks`` edge on ``task_id`` (relative to ``task_id``)."""
    return EdgeRecord(
        from_task_id=blocker_id,
        to_task_id=task_id,
        type="blocks",
        direction="incoming",
    )


def _stage_blockers(
    fake: TaskFakeLithosClient, task_id: str, count: int, *, prefix: str = "blocker"
) -> None:
    """Give ``task_id`` ``count`` open blockers, each a real task in the fake."""
    edges: list[EdgeRecord] = []
    for index in range(count):
        blocker_id = f"{prefix}-{index:03d}"
        fake.tasks.append(
            TaskRecord(
                id=blocker_id,
                title=f"Blocker {index}",
                status="open",
                created_at="2026-04-20T10:00:00+00:00",
            )
        )
        edges.append(_blocks(blocker_id, task_id))
    fake.edges[task_id] = edges


# --- acceptance: a blocked task lists each blocker with live status ---------


def test_blocked_task_detail_lists_every_blocker_with_live_status(
    lithos_lens_config_env: Path,
) -> None:
    """Slice acceptance: the level-1 chain names each blocker and says where it
    is RIGHT NOW — a completed predecessor and an open one must not read the
    same, and a gate says it is a gate and which kind."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        TaskRecord(
            id="gate-review",
            title="Human review gate",
            status="open",
            task_type="gate",
            metadata={"gate_type": "human"},
            created_at="2026-04-20T10:00:00+00:00",
        )
    )
    fake.edges["open-unclaimed"] = [
        _blocks("open-claimed", "open-unclaimed"),
        _blocks("done-recent", "open-unclaimed"),
        EdgeRecord(
            from_task_id="gate-review",
            to_task_id="open-unclaimed",
            type="waits_on_gate",
            direction="incoming",
        ),
    ]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    text = response.text
    blockers = text[text.index('data-detail-section="blockers"') :]
    blockers = blockers[: blockers.index("</section>")]
    # Each blocker: its title, its relation, and its LIVE status.
    assert "blocked by" in blockers
    assert "Claimed open task" in blockers
    assert "Recently completed task" in blockers
    assert "waiting on gate" in blockers
    assert "Human review gate" in blockers
    assert "gate: human" in blockers
    assert 'class="badge badge-open"' in blockers
    assert 'class="badge badge-completed"' in blockers


def test_detail_loads_the_task_by_id_instead_of_scanning_the_status_lists(
    lithos_lens_config_env: Path,
) -> None:
    """``find_task`` is gone: the page addresses the task with one
    ``lithos_task_get`` and never fans out over the three status lists."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-claimed")

    assert response.status_code == 200
    assert "Claimed open task" in response.text
    assert fake.list_calls == []
    assert fake.get_calls == ["open-claimed"]


def test_unreadable_task_does_not_claim_the_task_is_missing(
    lithos_lens_config_env: Path,
) -> None:
    """A failed read is not a deleted task: only the ``task_not_found``
    envelope may render the not-found panel."""

    class BrokenGetClient(TaskFakeLithosClient):
        async def task_get(self, task_id: str) -> TaskRecord:
            raise RuntimeError("session dropped")

    fake = BrokenGetClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-claimed")

    assert response.status_code == 200
    assert "Task not found in current Lithos task lists" not in response.text
    assert "Task unavailable" in response.text


# --- acceptance: the level-1 blocker fan-out is bounded --------------------


def test_blocker_page_bounds_the_fan_out_and_states_the_remainder(
    lithos_lens_config_env: Path,
) -> None:
    """A task with more blockers than the page size renders the FIRST PAGE with
    live statuses plus a tail, and issues exactly page-size ``task_get``
    lookups — the edge count is agent-controlled, so the render cost must not
    follow it.

    The tail is the other half: an operator has to see that more blockers exist
    and how many, or a silently trimmed list reads as a complete answer to "why
    can't this run?".
    """
    fake = TaskFakeLithosClient()
    overflow = 15
    _stage_blockers(fake, "open-unclaimed", DETAIL_PAGE_SIZE + overflow)

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    text = response.text
    # Exactly one rendered row per lookup, and no lookup past the page.
    blocker_lookups = [call for call in fake.get_calls if call.startswith("blocker-")]
    assert len(blocker_lookups) == DETAIL_PAGE_SIZE
    assert text.count('data-link-id="blocker-') == DETAIL_PAGE_SIZE
    assert 'data-link-id="blocker-000"' in text
    assert 'data-link-id="blocker-039"' not in text
    # The rendered page still carries live status, not just titles.
    assert 'class="badge badge-open"' in text
    # The tail names the remainder rather than truncating silently.
    assert 'data-link-tail="blockers"' in text
    assert f"{overflow} more are not shown" in text
    assert f"of {DETAIL_PAGE_SIZE + overflow} blockers" in text


def test_blocker_fan_out_is_also_concurrency_bounded(
    lithos_lens_config_env: Path,
) -> None:
    """Belt and braces: the page size bounds the TOTAL lookups, the semaphore
    bounds how many are in flight on the shared MCP session at once."""

    class ProbeClient(TaskFakeLithosClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.in_flight = 0
            self.peak_in_flight = 0

        async def task_get(self, task_id: str) -> TaskRecord:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            try:
                # Yield, so calls that are allowed to overlap actually do.
                await asyncio.sleep(0)
                return await super().task_get(task_id)
            finally:
                self.in_flight -= 1

    fake = ProbeClient()
    _stage_blockers(fake, "open-unclaimed", DETAIL_PAGE_SIZE)

    _run(load_task_detail(fake, "open-unclaimed"))

    assert fake.peak_in_flight > 1  # still concurrent
    assert fake.peak_in_flight <= DETAIL_FANOUT_CONCURRENCY
    # Not just "within the constant": strictly fewer than the lookups the page
    # issued, so removing the gate (and letting the whole page fan out at once)
    # fails here even if the constant itself is raised.
    assert fake.peak_in_flight < DETAIL_PAGE_SIZE
    assert len([call for call in fake.get_calls if call.startswith("blocker-")]) == (
        DETAIL_PAGE_SIZE
    )


def test_load_blocker_page_applies_the_same_bound_at_any_level() -> None:
    """The reuse seam T1-S8 expands each deeper level through: given only a
    task id, it reads that task's blocker edges and returns the same bounded,
    tailed page — no second pagination path to keep in step."""
    fake = TaskFakeLithosClient()
    _stage_blockers(fake, "open-old", DETAIL_PAGE_SIZE + 4, prefix="deep")

    page = _run(load_blocker_page(fake, "open-old"))

    assert len(page.links) == DETAIL_PAGE_SIZE
    assert page.remaining == 4
    assert page.total == DETAIL_PAGE_SIZE + 4
    assert len(fake.get_calls) == DETAIL_PAGE_SIZE
    assert fake.edge_list_calls == [
        {
            "task_id": "open-old",
            "direction": "incoming",
            "types": ["blocks", "waits_on_gate"],
        }
    ]


def test_first_page_is_the_single_pagination_decision() -> None:
    items = list(range(DETAIL_PAGE_SIZE + 3))

    page, remaining = first_page(items)

    assert page == tuple(items[:DETAIL_PAGE_SIZE])
    assert remaining == 3
    assert first_page(items[:2]) == ((0, 1), 0)


def test_blocker_read_failure_never_reads_as_nothing_blocking() -> None:
    """An empty blocker list is a claim ("nothing is blocking this"), so a
    failed read must degrade to the error state instead."""

    class BrokenEdgeClient(TaskFakeLithosClient):
        async def task_edge_list(self, task_id: str, **kwargs: Any) -> Any:
            raise RuntimeError("edge read failed")

    page = _run(load_blocker_page(BrokenEdgeClient(), "open-unclaimed"))

    assert page.state == "error"
    assert page.links == ()


def test_unresolvable_blocker_is_still_listed() -> None:
    """A blocker whose ``task_get`` fails keeps its row — an unreadable blocker
    is still a reason the task cannot run."""
    fake = TaskFakeLithosClient()
    fake.edges["open-unclaimed"] = [_blocks("ghost-task", "open-unclaimed")]

    detail = _run(load_task_detail(fake, "open-unclaimed"))

    assert [link.task_id for link in detail.blockers.links] == ["ghost-task"]
    assert detail.blockers.links[0].resolved is False
    assert detail.blockers.links[0].status_label == "status unknown"


# --- acceptance: a spawned task shows its source ---------------------------


def test_spawned_task_detail_shows_its_source_and_its_own_follow_ons(
    lithos_lens_config_env: Path,
) -> None:
    """Slice acceptance: ``discovered_from`` renders in BOTH directions — the
    work this task fell out of, and the follow-ons it spawned."""
    fake = TaskFakeLithosClient()
    fake.edges["open-unclaimed"] = [
        EdgeRecord(
            from_task_id="open-claimed",
            to_task_id="open-unclaimed",
            type="discovered_from",
            direction="incoming",
        ),
        EdgeRecord(
            from_task_id="open-unclaimed",
            to_task_id="open-old",
            type="discovered_from",
            direction="outgoing",
        ),
    ]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    text = response.text
    provenance = text[text.index('data-detail-section="provenance"') :]
    provenance = provenance[: provenance.index("</section>")]
    assert "Discovered while working on" in provenance
    assert "Claimed open task" in provenance
    assert "Spawned follow-ons" in provenance
    assert "Old open task" in provenance
    # Provenance links are not blocker lines.
    assert "blocked by" not in provenance


# --- hierarchy -------------------------------------------------------------


def test_parent_breadcrumb_walks_the_single_parent_chain_to_the_root(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [
            TaskRecord(id="root-epic", title="Root epic", task_type="epic"),
            TaskRecord(id="sub-epic", title="Sub epic", task_type="epic"),
        ]
    )
    fake.edges["open-unclaimed"] = [
        EdgeRecord(
            from_task_id="sub-epic",
            to_task_id="open-unclaimed",
            type="parent_child",
            direction="incoming",
        )
    ]
    fake.edges["sub-epic"] = [
        EdgeRecord(
            from_task_id="root-epic",
            to_task_id="sub-epic",
            type="parent_child",
            direction="incoming",
        )
    ]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")
    detail = _run(load_task_detail(fake, "open-unclaimed"))

    assert response.status_code == 200
    assert "data-parent-breadcrumb" in response.text
    # Root first, then down to the immediate parent.
    assert [task.id for task in detail.breadcrumb.ancestors] == [
        "root-epic",
        "sub-epic",
    ]
    assert detail.breadcrumb.truncated is False


def test_parent_chain_cycle_stops_and_says_so() -> None:
    """``parent_child`` cycles are not forbidden upstream, so the walk must
    terminate on one — and mark the chain truncated rather than implying the
    last entry it reached is the root."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(TaskRecord(id="loop-parent", title="Loop parent"))
    fake.edges["open-unclaimed"] = [
        EdgeRecord(
            from_task_id="loop-parent",
            to_task_id="open-unclaimed",
            type="parent_child",
            direction="incoming",
        )
    ]
    fake.edges["loop-parent"] = [
        EdgeRecord(
            from_task_id="open-unclaimed",
            to_task_id="loop-parent",
            type="parent_child",
            direction="incoming",
        )
    ]

    detail = _run(load_task_detail(fake, "open-unclaimed"))

    assert [task.id for task in detail.breadcrumb.ancestors] == ["loop-parent"]
    assert detail.breadcrumb.truncated is True


def test_children_table_lists_children_with_their_status(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()
    fake.children["open-claimed"] = ["open-unclaimed", "done-recent"]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-claimed")

    assert response.status_code == 200
    text = response.text
    children = text[text.index('data-detail-section="children"') :]
    children = children[: children.index("</section>")]
    # include_closed: a completed child is part of the hierarchy too.
    assert "Unclaimed open task" in children
    assert "Recently completed task" in children


def test_children_table_is_paged_by_the_same_helper() -> None:
    """The children count is as agent-controlled as the blocker count, and it
    goes through the same page-size constant and tail."""
    fake = TaskFakeLithosClient()
    child_ids: list[str] = []
    for index in range(DETAIL_PAGE_SIZE + 2):
        child_id = f"child-{index:03d}"
        child_ids.append(child_id)
        fake.tasks.append(TaskRecord(id=child_id, title=f"Child {index}"))
    fake.children["open-claimed"] = child_ids

    detail = _run(load_task_detail(fake, "open-claimed"))

    assert len(detail.children.links) == DETAIL_PAGE_SIZE
    assert detail.children.remaining == 2


# --- header badges, resolution --------------------------------------------


def test_type_badge_names_the_task_type_and_a_gate_s_kind() -> None:
    assert task_type_badge(TaskRecord(id="t", title="t")) == "task"
    assert task_type_badge(TaskRecord(id="e", title="e", task_type="epic")) == "epic"
    assert (
        task_type_badge(
            TaskRecord(
                id="g", title="g", task_type="gate", metadata={"gate_type": "timer"}
            )
        )
        == "gate: timer"
    )
    # A gate whose metadata lost its kind is still a gate, not a plain task.
    assert task_type_badge(TaskRecord(id="g", title="g", task_type="gate")) == "gate"


def test_detail_reports_the_outcome_and_resolution_time(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()
    for index, task in enumerate(fake.tasks):
        if task.id == "done-recent":
            fake.tasks[index] = TaskRecord(
                id=task.id,
                title=task.title,
                status="completed",
                created_at=task.created_at,
                resolved_at=task.resolved_at,
                outcome="Shipped behind the ingest flag.",
            )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/done-recent")

    assert response.status_code == 200
    assert 'data-detail-section="resolution"' in response.text
    assert "Shipped behind the ingest flag." in response.text
    assert "2026-04-22T10:00:00+00:00" in response.text


def test_open_task_renders_no_resolution_section(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-claimed")

    assert 'data-detail-section="resolution"' not in response.text


def test_empty_breadcrumb_is_not_truncated() -> None:
    assert Breadcrumb().truncated is False
