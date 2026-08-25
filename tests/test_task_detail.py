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
import inspect
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lithos_lens import task_detail
from lithos_lens.config import load_config
from lithos_lens.task_detail import load_findings_timeline, load_task_detail
from lithos_lens.task_graph import EdgeRecord
from lithos_lens.task_links import (
    DETAIL_FANOUT_CONCURRENCY,
    DETAIL_PAGE_SIZE,
    Breadcrumb,
    first_page,
    last_page,
    link_page_from_tasks,
    load_blocker_page,
    load_link_page,
    task_type_badge,
)
from lithos_lens.tasks import FindingRecord, NoteRecord, TaskRecord
from lithos_lens.web import create_app, note_url
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
    assert 'data-overflow-tail="blockers"' in text
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


# --- the findings timeline is bounded the same way (security/f-001) --------


class NoteProbeClient(TaskFakeLithosClient):
    """Records every ``read_note`` — the timeline's title fan-out."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.note_calls: list[dict[str, Any]] = []
        self.notes_in_flight = 0
        self.peak_notes_in_flight = 0

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None:
        self.note_calls.append({"id": knowledge_id, "max_length": max_length})
        self.notes_in_flight += 1
        self.peak_notes_in_flight = max(self.peak_notes_in_flight, self.notes_in_flight)
        try:
            await asyncio.sleep(0)
            return NoteRecord(id=knowledge_id, title=f"Note {knowledge_id}", content="")
        finally:
            self.notes_in_flight -= 1


def _stage_findings(
    fake: TaskFakeLithosClient, task_id: str, count: int, *, first_index: int = 0
) -> None:
    """``count`` findings on ``task_id``, each citing a DISTINCT document."""
    fake.findings[task_id] = [
        FindingRecord(
            id=f"finding-{index:03d}",
            task_id=task_id,
            agent="worker-a",
            summary=f"Finding {index}",
            knowledge_id=f"note-{index:03d}",
            # Ascending, so "the newest page" is unambiguous.
            created_at=f"2026-04-{(index % 28) + 1:02d}T{index % 24:02d}:00:00+00:00",
        )
        for index in range(first_index, first_index + count)
    ]


def test_findings_timeline_pages_the_newest_and_states_the_rest(
    lithos_lens_config_env: Path,
) -> None:
    """The findings count is agent-controlled exactly like the edge count —
    ``lithos_finding_post`` is uncredentialed and ``lithos_finding_list`` takes
    no limit — and each distinct ``knowledge_id`` costs a ``lithos_read`` round
    trip. So the timeline is paged by the SAME constant, its title fan-out is
    bounded by the page, and the collapsed remainder is stated rather than
    silently dropped."""
    fake = NoteProbeClient()
    older = 7
    _stage_findings(fake, "open-unclaimed", DETAIL_PAGE_SIZE + older)

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    text = response.text
    # One rendered row per page slot, and one lookup per rendered row.
    assert text.count("<p>Finding ") == DETAIL_PAGE_SIZE
    assert len(fake.note_calls) == DETAIL_PAGE_SIZE
    # The page kept the NEWEST findings: the oldest ones are what collapsed.
    assert f"Finding {DETAIL_PAGE_SIZE + older - 1}" in text
    assert "Finding 0<" not in text
    # ... and the tail says how many, in the shared overflow wording.
    assert 'data-overflow-tail="findings"' in text
    assert f"{older} more are not shown" in text
    assert f"most recent {DETAIL_PAGE_SIZE}" in text


def test_finding_title_lookups_are_gated_and_body_free(
    lithos_lens_config_env: Path,
) -> None:
    """The title fan-out shares the render's gate (it used to run ungated AND
    sequentially), and reads only the frontmatter — a title never pulls a whole
    note body, the same economy the related panel already makes."""
    fake = NoteProbeClient()
    _stage_findings(fake, "open-unclaimed", DETAIL_PAGE_SIZE)

    _run(load_task_detail(fake, "open-unclaimed"))

    assert [call["max_length"] for call in fake.note_calls] == [1] * DETAIL_PAGE_SIZE
    assert fake.peak_notes_in_flight > 1  # concurrent, not the old serial loop
    assert fake.peak_notes_in_flight <= DETAIL_FANOUT_CONCURRENCY
    assert fake.peak_notes_in_flight < DETAIL_PAGE_SIZE


def test_one_lookup_serves_every_finding_citing_the_same_document() -> None:
    fake = NoteProbeClient()
    fake.findings["open-unclaimed"] = [
        FindingRecord(
            id=f"finding-{index}",
            task_id="open-unclaimed",
            agent="worker-a",
            summary=f"Finding {index}",
            knowledge_id="note-shared",
            created_at=f"2026-04-2{index}T10:00:00+00:00",
        )
        for index in range(3)
    ]

    detail = _run(load_task_detail(fake, "open-unclaimed"))

    assert len(detail.findings) == 3
    assert [call["id"] for call in fake.note_calls] == ["note-shared"]


def test_reopen_marker_survives_a_paged_timeline(
    lithos_lens_config_env: Path,
) -> None:
    """The marker is derived from the whole findings list, not from the page:
    it costs no round trip, so paging the timeline must not turn a reopened
    task into an un-reopened-looking one."""
    fake = NoteProbeClient()
    _stage_findings(fake, "open-unclaimed", DETAIL_PAGE_SIZE, first_index=10)
    fake.findings["open-unclaimed"].insert(
        0,
        FindingRecord(
            id="finding-reopen",
            task_id="open-unclaimed",
            agent="operator",
            summary="[Reopened] from completed by operator",
            # Older than every finding on the page, so it is collapsed away.
            created_at="2026-01-01T09:00:00+00:00",
        ),
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    assert "data-reopened-marker" in response.text
    assert "reopen reported by operator" in response.text
    # The reopen finding itself is off the page — only the marker survives.
    assert "data-reopen-finding" not in response.text


# --- the findings fragment pays only for findings (security/f-002) ---------


def test_findings_fragment_does_not_pay_for_the_graph_fan_out(
    lithos_lens_config_env: Path,
) -> None:
    """``/tasks/{id}/findings`` renders the timeline and nothing else, so it
    must not issue the detail page's edge read, children read, link lookups or
    parent walk — a fragment is the cheapest thing to request in a loop and
    must not be the most expensive thing to serve."""
    fake = NoteProbeClient()
    fake.edges["open-claimed"] = [_blocks("open-unclaimed", "open-claimed")]
    fake.children["open-claimed"] = ["open-unclaimed"]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-claimed/findings")

    assert response.status_code == 200
    # It still renders what it is for.
    assert "Important finding" in response.text
    # ... and nothing else was read: no task_get, no edges, no children.
    assert fake.get_calls == []
    assert fake.edge_list_calls == []


# --- the page size is not caller-supplied (security/f-003) -----------------


def test_the_page_size_cannot_be_widened_by_a_caller() -> None:
    """The bound is the constant, not a default argument. A ``page_size``
    keyword would let T1-S8 page a deeper level at any size — reintroducing the
    exact defect one level down — without touching ``DETAIL_PAGE_SIZE`` or
    failing any test of it."""
    for helper in (
        first_page,
        last_page,
        link_page_from_tasks,
        load_link_page,
        load_blocker_page,
    ):
        params = set(inspect.signature(helper).parameters)
        assert "page_size" not in params, helper.__name__

    # And the primitive itself never yields more than the constant.
    assert len(first_page(range(DETAIL_PAGE_SIZE * 4))[0]) == DETAIL_PAGE_SIZE
    assert len(last_page(range(DETAIL_PAGE_SIZE * 4))[0]) == DETAIL_PAGE_SIZE


def test_last_page_keeps_the_newest_and_counts_what_precedes_it() -> None:
    items = list(range(DETAIL_PAGE_SIZE + 3))

    page, older = last_page(items)

    assert page == tuple(items[3:])
    assert older == 3
    assert last_page([1, 2]) == ((1, 2), 0)


def test_findings_timeline_loader_reports_a_failed_read() -> None:
    class BrokenFindingsClient(TaskFakeLithosClient):
        async def list_findings(self, task_id: str, **kwargs: Any) -> Any:
            raise RuntimeError("findings read failed")

    detail = _run(load_findings_timeline(BrokenFindingsClient(), "open-claimed"))

    assert detail.findings_state == "error"
    assert detail.findings == ()


# --- agent-controlled ids are encoded, not interpolated (security/f-005) ---


def test_a_finding_cannot_aim_its_document_link_at_another_page(
    lithos_lens_config_env: Path,
) -> None:
    """``knowledge_id`` is a free-form agent string that Lithos never validates
    and Lens never rewrites, so interpolating it raw lets whoever posted the
    finding choose where "View document" actually goes: a browser normalizes
    ``/note/../tasks/x`` to ``/tasks/x`` before it ever issues the request.
    Encoding the id keeps the link pointing at the document it claims to."""
    fake = TaskFakeLithosClient()
    fake.findings["open-unclaimed"] = [
        FindingRecord(
            id="finding-traversal",
            task_id="open-unclaimed",
            agent="worker-a",
            summary="Cites a document that is really a path",
            knowledge_id="../tasks/open-claimed",
            created_at="2026-04-27T09:00:00+00:00",
        )
    ]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    assert 'href="/note/../tasks/' not in response.text
    assert "/note/..%2Ftasks%2Fopen-claimed" in response.text


def test_note_url_encodes_the_whole_id_and_the_task_back_link() -> None:
    """``safe=""`` is the point: the default ``quote`` leaves ``/`` alone, and
    ``/`` is the character a traversal needs. The back-link is urlencoded so a
    value carrying ``&`` cannot graft on a parameter."""
    assert note_url("plain-id") == "/note/plain-id"
    assert note_url("../../etc") == "/note/..%2F..%2Fetc"
    assert note_url("a b&c") == "/note/a%20b%26c"
    assert note_url("note-1", "task&x=1") == "/note/note-1?task=task%26x%3D1"


# --- a stalled Lithos costs a partial page, not a held request (f-006) -----


class _StallingLinkClient(TaskFakeLithosClient):
    """Answers the identifying read, then stalls every per-link lookup."""

    async def task_get(self, task_id: str) -> TaskRecord:
        if task_id.startswith("blocker-"):
            await asyncio.Event().wait()
        return await super().task_get(task_id)


def test_a_stalled_lithos_renders_a_partial_page_instead_of_holding_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The count bound caps how MUCH work a render does; it does not cap how
    LONG the render takes when every call stalls to the per-call ceiling, and
    ``/health`` is a separate endpoint so nothing upstream notices. Past the
    budget the page renders what it has: the stalled section degrades to the
    state it already knows how to show, and the sections that answered survive.
    """
    monkeypatch.setattr(task_detail, "DETAIL_RENDER_BUDGET_S", 0.05)
    fake = _StallingLinkClient()
    _stage_blockers(fake, "open-claimed", 3)

    detail = _run(load_task_detail(fake, "open-claimed"))

    # Wave 1 answered, so the page still identifies its task ...
    assert detail.task is not None
    assert detail.task.title == "Claimed open task"
    # ... the stalled list says so rather than reading as "nothing blocks this"
    assert detail.blockers.state == "error"
    assert detail.blockers.links == ()
    # ... and the sections that DID answer are not thrown away with it.
    assert [view.finding.id for view in detail.findings] == ["finding-1", "finding-2"]


def test_a_stalled_title_lookup_costs_only_the_titles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The timeline is already in hand when the titles are fetched, so running
    out of budget there must cost the labels, not the findings."""

    class StallingNoteClient(TaskFakeLithosClient):
        async def read_note(
            self, knowledge_id: str, *, max_length: int | None = None
        ) -> NoteRecord | None:
            await asyncio.Event().wait()

    monkeypatch.setattr(task_detail, "DETAIL_RENDER_BUDGET_S", 0.05)

    detail = _run(load_findings_timeline(StallingNoteClient(), "open-claimed"))

    assert detail.findings_state == "ok"
    assert [view.finding.id for view in detail.findings] == ["finding-1", "finding-2"]
    # Every row falls back to the label it already shows for an unreadable doc.
    assert {view.link_label for view in detail.findings} == {"View document"}
