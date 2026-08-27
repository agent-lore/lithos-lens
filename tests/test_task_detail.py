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
from starlette.requests import Request

from lithos_lens import task_links
from lithos_lens.config import load_config
from lithos_lens.task_detail import load_findings_timeline, load_task_detail
from lithos_lens.task_graph import EdgeRecord
from lithos_lens.task_links import (
    DETAIL_FANOUT_CONCURRENCY,
    DETAIL_PAGE_SIZE,
    GATE_TYPES,
    UNKNOWN_TASK_TYPE_BADGE,
    Breadcrumb,
    first_page,
    last_page,
    link_page_from_tasks,
    load_blocker_page,
    load_link_page,
    task_type_badge,
)
from lithos_lens.tasks import FindingRecord, NoteRecord, TaskRecord
from lithos_lens.web import create_app, note_url, task_detail_url
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


def test_a_runaway_edge_count_still_renders_a_page_and_the_true_remainder(
    lithos_lens_config_env: Path,
) -> None:
    """The bound is on the WORK, never on the input.

    A buggy agent minting edges in a loop is the case this story exists to
    handle, so the page must behave identically at any edge count: the same
    first page with live statuses, the same page-size fan-out, and a tail
    stating the REAL number left off — not a round number, and not an
    "unavailable" section. Nothing on the path (transport, loader or template)
    may impose a ceiling that turns this input into a failed read.
    """
    fake = TaskFakeLithosClient()
    runaway = 5_000
    _stage_blockers(fake, "open-unclaimed", runaway)

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    text = response.text
    assert text.count('data-link-id="blocker-') == DETAIL_PAGE_SIZE
    assert len([call for call in fake.get_calls if call.startswith("blocker-")]) == (
        DETAIL_PAGE_SIZE
    )
    assert 'class="badge badge-open"' in text
    # The remainder is counted, not estimated.
    assert f"{runaway - DETAIL_PAGE_SIZE} more are not shown" in text
    assert f"of {runaway} blockers" in text
    assert "Blockers unavailable" not in text


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


def test_a_type_badge_cannot_be_forged_from_either_half(
    lithos_lens_config_env: Path,
) -> None:
    """Both halves of this badge are agent-controlled with no credential:
    ``lithos_task_create`` takes ``task_type`` as a bare string, and
    ``lithos_task_update`` MERGES ``metadata`` per key — so ``gate_type`` can
    be set on any existing task with one call that touches nothing else
    (``tests/contracts/_tools_snapshot.json``). Autoescaped, neither is XSS; it
    is a SPOOFABLE badge sitting beside the live status on the surface an
    operator reads to decide whether a human is holding a task. Clamping one
    half only moves the forgery to the other, so both are clamped to their
    closed server vocabularies, exactly as ``status`` already is."""
    gate_spoof = "human — approved, safe to merge"
    assert (
        task_type_badge(
            TaskRecord(
                id="g", title="g", task_type="gate", metadata={"gate_type": gate_spoof}
            )
        )
        == "gate"
    )
    for gate_type in GATE_TYPES:
        assert (
            task_type_badge(
                TaskRecord(
                    id="g",
                    title="g",
                    task_type="gate",
                    metadata={"gate_type": gate_type},
                )
            )
            == f"gate: {gate_type}"
        )

    # The mirror of the above, and the one the length bound never touched:
    # "gate: human" is 11 characters, so a SHORT task_type impersonates a real
    # human gate byte for byte unless the type itself is clamped.
    real_gate = TaskRecord(
        id="g", title="g", task_type="gate", metadata={"gate_type": "human"}
    )
    type_spoof = TaskRecord(id="t", title="t", task_type="gate: human")
    assert task_type_badge(type_spoof) != task_type_badge(real_gate)
    assert task_type_badge(type_spoof) == UNKNOWN_TASK_TYPE_BADGE
    assert task_type_badge(TaskRecord(id="t", title="t", task_type="x" * 80)) == (
        UNKNOWN_TASK_TYPE_BADGE
    )

    # And the rendered page agrees, for both halves.
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        TaskRecord(
            id="gate-spoof",
            title="Waiting on something",
            status="open",
            task_type="gate",
            metadata={"gate_type": gate_spoof},
            created_at="2026-04-20T10:00:00+00:00",
        )
    )
    fake.tasks.append(
        TaskRecord(
            id="type-spoof",
            title="Not a gate at all",
            status="open",
            task_type="gate: human",
            created_at="2026-04-20T10:00:00+00:00",
        )
    )
    with _client(lithos_lens_config_env, fake) as client:
        gate_page = client.get("/tasks/gate-spoof")
        type_page = client.get("/tasks/type-spoof")

    assert gate_page.status_code == 200
    assert "safe to merge" not in gate_page.text.split("<h2>Metadata</h2>")[0]
    assert type_page.status_code == 200
    assert "gate: human" not in type_page.text
    assert UNKNOWN_TASK_TYPE_BADGE in type_page.text


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


def test_a_finding_event_refreshes_the_fragment_not_the_whole_page(
    lithos_lens_config_env: Path,
) -> None:
    """The cheap loader above is only a mitigation if it is what the CLIENT
    requests. It was not: ``refreshFragments`` refetched ``location.href`` —
    the whole detail page, graph fan-out included — and nothing requested
    ``/tasks/{id}/findings`` at all. Since ``lithos_finding_post`` needs no
    credential and no path to Lens, that let whoever posts findings set how
    often every open detail tab performed the most expensive render in the app.

    Asserted at the level the EVENT reaches, not at the handler: routing the
    finding to the fragment means nothing if the same event goes on to schedule
    the whole-page reconcile one line later in the caller (it did —
    ``finding.posted`` is in ``SPARSE_EVENT_TYPES``, so ``requires_refresh`` is
    always true for it). Both refresh paths are also floored, and neither can
    be starved past a ceiling: see the bounds test below.
    """
    fake = NoteProbeClient()
    with _client(lithos_lens_config_env, fake) as client:
        fragment = client.get("/tasks/open-claimed/findings")
        page = client.get("/tasks/open-claimed")
        script = client.get("/static/tasks.js")

    # The swap target the reconcile replaces, on both sides of the swap.
    assert 'data-refresh-fragment="findings"' in fragment.text
    assert 'data-refresh-fragment="findings"' in page.text

    source = script.text
    assert "/findings" in source
    # A finding.posted event on the open task refreshes the timeline...
    handle_finding = _js_function(source, "handleFinding")
    assert "scheduleFindingsRefresh" in handle_finding
    # ...and does NOT also fall through to the whole-page reconcile in the
    # dispatcher: the handler reports the event handled, and the dispatcher
    # skips the expensive path for handled events.
    dispatch = _js_function(source, "handleEvent")
    assert "handled = handleFinding(message)" in dispatch
    assert "if (message.requires_refresh && !handled) scheduleReconcile();" in dispatch


def test_both_refresh_paths_are_floored_and_neither_can_be_starved() -> None:
    """The floor and the ceiling are one mechanism, and both paths use it: an
    event stream no client needs Lens access to drive must not set the render
    rate (the floor), and must not defer a pending refresh forever by re-arming
    its debounce either (the ceiling — otherwise the board holds stale
    blockers, claims and statuses for the whole burst while still reading
    "Live updates connected")."""
    source = (Path("src/lithos_lens/static/tasks.js")).read_text()
    schedule = _js_function(source, "scheduleRefresh")

    # One floor per path, applied by the shared scheduler...
    assert "path.lastRunAt + path.minIntervalMs" in schedule
    assert "minIntervalMs: DETAIL_RECONCILE_MIN_INTERVAL_MS" in source.replace(
        "detailTaskId ? ", ""
    )
    assert "minIntervalMs: FINDINGS_MIN_INTERVAL_MS" in source
    # ...and one ceiling on how long re-arming may defer a pending refresh,
    # measured from the FIRST deferred event rather than the latest.
    assert "path.deferredSince + MAX_DEFER_MS" in schedule
    assert "if (!path.deferredSince) path.deferredSince = now;" in schedule


def _js_function(source: str, name: str) -> str:
    """The body of one top-level function in ``tasks.js``.

    The refresh bounds are browser behaviour with no Python seam, and the JS is
    exercised end to end by the Playwright suite rather than here; these
    assertions pin the SHAPE that the security review turned on — which
    function schedules what — so a later edit that quietly restores the
    unbounded path fails in the fast suite instead of in review.
    """
    return source.split(f"function {name}")[1].split("\n  function ")[0]


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
    value carrying ``&`` cannot graft on a parameter.

    ``task_detail_url`` is held to the SAME rule, on the same line: T1-S7 gave
    it three new sinks whose contents an agent controls (every blocker,
    provenance and children row, and every breadcrumb ancestor). Ids are
    server-minted today, so this is the hardening that keeps the two helpers
    from disagreeing about a traversal the moment an imported id — or an
    upstream that accepts a caller-chosen one — arrives.
    """
    assert note_url("plain-id") == "/note/plain-id"
    assert note_url("../../etc") == "/note/..%2F..%2Fetc"
    assert note_url("a b&c") == "/note/a%20b%26c"
    assert note_url("note-1", "task&x=1") == "/note/note-1?task=task%26x%3D1"

    request = Request(
        {"type": "http", "method": "GET", "query_string": b"", "headers": []}
    )
    assert task_detail_url(request, "plain-id") == "/tasks/plain-id"
    assert task_detail_url(request, "../note/pwned") == "/tasks/..%2Fnote%2Fpwned"
    assert task_detail_url(request, "a b&c") == "/tasks/a%20b%26c"


def test_a_blocker_row_cannot_aim_its_link_at_another_page(
    lithos_lens_config_env: Path,
) -> None:
    """The rendered sink, not just the helper: a related-task row links to
    whatever id the edge names, so an id carrying ``/`` would be an href the
    browser normalizes to a different Lens page before requesting it."""
    fake = TaskFakeLithosClient()
    traversal_id = "../note/note-1"
    fake.tasks.append(
        TaskRecord(
            id=traversal_id,
            title="Blocker with a path for an id",
            status="open",
            created_at="2026-04-20T10:00:00+00:00",
        )
    )
    fake.edges["open-unclaimed"] = [_blocks(traversal_id, "open-unclaimed")]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    assert 'href="/tasks/../' not in response.text
    assert "/tasks/..%2Fnote%2Fnote-1" in response.text


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
    monkeypatch.setattr(task_links, "DETAIL_RENDER_BUDGET_S", 0.05)
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

    monkeypatch.setattr(task_links, "DETAIL_RENDER_BUDGET_S", 0.05)

    detail = _run(load_findings_timeline(StallingNoteClient(), "open-claimed"))

    assert detail.findings_state == "ok"
    assert [view.finding.id for view in detail.findings] == ["finding-1", "finding-2"]
    # Every row falls back to the label it already shows for an unreadable doc.
    assert {view.link_label for view in detail.findings} == {"View document"}


def test_the_render_budget_covers_the_identifying_read_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget is the REQUEST's, not each wave's. Taken after wave 1 it
    would only bound waves 2-3, so a wave-1 read that answers just inside the
    per-call ceiling would still be followed by a full fresh budget — the page
    would take the ceiling PLUS the budget, which is not the bound the module
    advertises.

    Structural, not timed: wave 1 answers slowly but successfully, then a
    wave-2 read that is faster than a whole budget — and would therefore
    succeed if the budget restarted — must NOT get one.
    """
    monkeypatch.setattr(task_links, "DETAIL_RENDER_BUDGET_S", 0.5)

    class SlowStartClient(TaskFakeLithosClient):
        async def task_get(self, task_id: str) -> TaskRecord:
            await asyncio.sleep(0.4)  # nearly the whole budget, but it ANSWERS
            return await super().task_get(task_id)

        async def list_findings(self, task_id: str, **kwargs: Any) -> Any:
            await asyncio.sleep(0.3)  # < one budget, > what wave 1 left
            return await super().list_findings(task_id, **kwargs)

    detail = _run(load_task_detail(SlowStartClient(), "open-claimed"))

    # Wave 1 answered inside the budget, so the page still knows its task ...
    assert detail.task is not None
    # ... and wave 2 inherited what was LEFT of it, not a fresh one.
    assert detail.findings_state == "error"


def test_load_blocker_page_carries_a_budget_at_any_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1-S8 expands deeper levels through this entry point, standalone — no
    render around it to impose a deadline. Both of its awaits (the edge read
    and the lookups behind it) must therefore be bounded by its own budget, or
    one expansion could spend a per-call timeout on each in turn."""
    monkeypatch.setattr(task_links, "DETAIL_RENDER_BUDGET_S", 0.05)

    # (a) the per-blocker lookups stall
    stalled_links = _StallingLinkClient()
    _stage_blockers(stalled_links, "open-old", 3)
    assert _run(load_blocker_page(stalled_links, "open-old")).state == "error"

    # (b) the edge read itself stalls
    class StalledEdgeClient(TaskFakeLithosClient):
        async def task_edge_list(self, task_id: str, **kwargs: Any) -> Any:
            await asyncio.Event().wait()

    assert _run(load_blocker_page(StalledEdgeClient(), "open-old")).state == "error"


def test_a_standalone_link_page_is_bounded_without_a_caller_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bounds live in the loader, not in the caller: a page requested with
    no deadline still gets one rather than none."""
    monkeypatch.setattr(task_links, "DETAIL_RENDER_BUDGET_S", 0.05)
    fake = _StallingLinkClient()
    _stage_blockers(fake, "open-old", 2)

    page = _run(load_link_page(fake, fake.edges["open-old"]))

    assert page.state == "error"
    assert page.links == ()
