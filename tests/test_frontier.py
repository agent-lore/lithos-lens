"""T1 slice 2 — frontier join (Ready / In progress / Blocked sections).

``classify_open_tasks`` is the pure join between the master open list and the
Lithos ready/blocked frontier. These tests pin every classification branch and
the blocker-chip resolution; readiness is supplied explicitly (the fake oracle
pattern) because Lens must never recompute it.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from lithos_lens.epic_strip import EPIC_FANOUT_BATCH, build_epic_rollup
from lithos_lens.frontier import classify_open_tasks, load_dashboard
from lithos_lens.frontier_fallback import RETRY_FAILED_ERROR
from lithos_lens.gates import GATE_WAITER_FANOUT_CAP
from lithos_lens.lithos_client import LithosToolError
from lithos_lens.normalizers import normalize_task
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord, EdgeRecord
from lithos_lens.tasks import (
    OPEN_SECTIONS,
    TASK_STATUSES,
    AgentRecord,
    ClaimRecord,
    SectionName,
    TaskFilters,
    TaskRecord,
    TaskStatusName,
)


def _task(
    task_id: str,
    *,
    task_type: str = "task",
    claims: Any = None,
    tags: tuple[str, ...] = (),
    created_at: str = "",
    created_by: str = "",
    metadata: dict[str, Any] | None = None,
    status: TaskStatusName = "open",
) -> TaskRecord:
    # ``created_at`` defaults to blank on purpose: the age-based attention
    # rules never fire on a timestamp they cannot read, so join-only tests stay
    # unaffected by them.
    return TaskRecord(
        id=task_id,
        title=f"Title {task_id}",
        status=status,
        task_type=task_type,
        tags=tags,
        created_at=created_at,
        created_by=created_by,
        metadata=dict(metadata or {}),
        claims=claims,
    )


def _blocked(task: TaskRecord, *blockers: BlockerRecord) -> BlockedTaskRecord:
    return BlockedTaskRecord(task=task, blockers=tuple(blockers))


def _section_ids(
    sections: Mapping[SectionName, tuple[Any, ...]], key: SectionName
) -> list[str]:
    return [row.task.id for row in sections[key]]


def test_claims_none_rows_leave_ready_and_blocked_counts() -> None:
    """TaskRecord.claims contract: ``None`` means claims were NOT returned even
    though requested — the row might belong in In progress, so it must not sit
    in the Ready ("unclaimed and workable now") or Blocked counts either. It
    stays VISIBLE in the dedicated claims-unknown group with the degraded-data
    treatment, mirroring the read-skew surface."""
    unknown_ready = _task("r", claims=None)
    unknown_blocked = _task("b", claims=None)
    known_ready = _task("k", claims=())
    sections = classify_open_tasks(
        [unknown_ready, unknown_blocked, known_ready],
        ready_ids={"r", "k"},
        blocked=[_blocked(unknown_blocked, BlockerRecord(kind="task", task_id="x"))],
    )

    # Only the KNOWN-unclaimed row counts as Ready…
    assert _section_ids(sections, "ready") == ["k"]
    assert _section_ids(sections, "blocked") == []
    assert _section_ids(sections, "in_progress") == []
    # …and the unknown rows are visible, flagged, never silently dropped.
    assert _section_ids(sections, "claims_unknown") == ["r", "b"]
    assert all(row.claims_unknown for row in sections["claims_unknown"])
    assert sections["claims_unknown"][0].claim_state == "unknown"
    # A blocked-listed unknown row keeps its blocker chips for context.
    assert sections["claims_unknown"][1].blockers


def test_claims_none_in_neither_frontier_is_still_claims_unknown() -> None:
    """A claims-unknown row's bucket does not depend on frontier membership,
    so it never pollutes the truncation/skew tail either."""
    sections = classify_open_tasks(
        [_task("u", claims=None)], ready_ids=set(), blocked=[]
    )
    assert _section_ids(sections, "claims_unknown") == ["u"]
    assert _section_ids(sections, "unclassified") == []


def test_claim_makes_in_progress_beating_ready_and_blocked() -> None:
    claimed = _task("c", claims=(ClaimRecord(agent="a", aspect="impl"),))
    sections = classify_open_tasks(
        [claimed],
        ready_ids={"c"},
        blocked=[_blocked(claimed, BlockerRecord(kind="task", task_id="x"))],
    )
    assert _section_ids(sections, "in_progress") == ["c"]
    assert _section_ids(sections, "ready") == []
    assert _section_ids(sections, "blocked") == []


def test_ready_membership_classifies_ready() -> None:
    task = _task("r", claims=())
    sections = classify_open_tasks([task], ready_ids={"r"}, blocked=[])
    assert _section_ids(sections, "ready") == ["r"]


def test_blocked_membership_classifies_blocked_with_predecessor_title_chip() -> None:
    predecessor = _task("pred", claims=())
    blocked = _task("b", claims=())
    sections = classify_open_tasks(
        [blocked, predecessor],
        ready_ids={"pred"},
        blocked=[
            _blocked(blocked, BlockerRecord(kind="task", task_id="pred", type="blocks"))
        ],
    )
    assert _section_ids(sections, "blocked") == ["b"]
    (row,) = sections["blocked"]
    assert [chip.label for chip in row.blockers] == ["Title pred"]
    assert row.blockers[0].kind == "task"
    assert row.blockers[0].target_id == "pred"


def test_blocker_chip_falls_back_when_predecessor_absent() -> None:
    blocked = _task("b", claims=())
    sections = classify_open_tasks(
        [blocked],
        ready_ids=set(),
        blocked=[
            _blocked(
                blocked,
                BlockerRecord(kind="gate", task_id="gate-9", message="Waiting on gate"),
            )
        ],
    )
    (row,) = sections["blocked"]
    # No predecessor in the snapshot -> fall back to the id rather than a blank.
    assert row.blockers[0].label == "gate-9"


def test_unclassified_only_when_absent_from_both_frontiers() -> None:
    task = _task("u", claims=())
    sections = classify_open_tasks([task], ready_ids=set(), blocked=[])
    assert _section_ids(sections, "unclassified") == ["u"]
    assert _section_ids(sections, "ready") == []
    assert _section_ids(sections, "blocked") == []


@pytest.mark.parametrize("task_type", ["epic", "gate"])
def test_epics_and_gates_never_enter_workable_sections(task_type: str) -> None:
    non_workable = _task("e", task_type=task_type, claims=())
    sections = classify_open_tasks([non_workable], ready_ids={"e"}, blocked=[])
    assert all(sections[key] == () for key in sections)


def test_task_type_less_payload_still_reaches_a_workable_section() -> None:
    """The missing-``task_type`` default is what keeps a malformed row visible.

    ``task_type`` is required by the 0.4 contract, so a payload without it is
    malformed rather than old (the pre-0.4 fallback was withdrawn). The
    normalizer still defaults it to "task", and this is why: the workable
    filter above drops every non-"task" type, so normalizing the absence to
    an empty string would silently DELETE the task from the board instead of
    degrading it.
    """
    task = normalize_task({"id": "m", "title": "No task_type", "claims": []})
    sections = classify_open_tasks([task], ready_ids={"m"}, blocked=[])
    assert _section_ids(sections, "ready") == ["m"]


def test_claimed_but_blocked_is_flagged_in_progress() -> None:
    claimed = _task("c", claims=(ClaimRecord(agent="a", aspect="impl"),))
    sections = classify_open_tasks(
        [claimed],
        ready_ids=set(),
        blocked=[_blocked(claimed, BlockerRecord(kind="task", task_id="x"))],
    )
    (row,) = sections["in_progress"]
    assert row.claimed_but_blocked is True
    assert row.blockers  # chips still attached for the anomaly


# --- load_dashboard assembly ----------------------------------------------


class _Response(list[TaskRecord]):
    """A weak-referenceable ``task_children`` response.

    A plain ``list`` cannot be weakly referenced, and the residency probe in
    ``_FrontierFake.task_children`` needs to watch a finished batch's responses
    actually die (CPython frees them the moment the last reference drops).
    """


class _FrontierFake:
    """Minimal client covering the parallel calls load_dashboard makes.

    The frontier reads HONOR their ``limit`` (mirroring the real server), and
    every read of a generation — ``open_tasks`` / ``ready`` / ``blocked`` and
    the ``completed`` / ``cancelled`` windows — accepts either a single
    response or a SEQUENCE of responses so read-skew retries can be scripted
    (the last response repeats). Call counts are recorded for retry assertions.
    """

    def __init__(
        self,
        *,
        open_tasks: list[TaskRecord] | list[list[TaskRecord]],
        ready: list[TaskRecord] | list[list[TaskRecord]],
        blocked: list[BlockedTaskRecord] | list[list[BlockedTaskRecord]],
        completed: list[TaskRecord] | list[list[TaskRecord]] | None = None,
        cancelled: list[TaskRecord] | list[list[TaskRecord]] | None = None,
        fail_ready: bool = False,
        children: dict[str, list[TaskRecord]] | None = None,
        fail_children: set[str] | None = None,
        gets: dict[str, TaskRecord] | None = None,
        missing_gets: set[str] | None = None,
        fail_ready_from: int | None = None,
        fail_completed_from: int | None = None,
        ready_error: BaseException | None = None,
        blocked_error: BaseException | None = None,
        edges: dict[str, list[EdgeRecord]] | None = None,
        late_edges: dict[str, list[EdgeRecord]] | None = None,
        fail_edges: bool = False,
        fail_stats: bool = False,
    ) -> None:
        self._open_seq = self._as_sequence(open_tasks)
        self._ready_seq = self._as_sequence(ready)
        self._blocked_seq = self._as_sequence(blocked)
        self._completed_seq = self._as_sequence(completed or [])
        self._cancelled_seq = self._as_sequence(cancelled or [])
        self._fail_ready = fail_ready
        self._children = children or {}
        self._fail_children = fail_children or set()
        # task_get answers from ``gets`` first, else from any open generation
        # (an epic Lens saw open confirms as open); ``missing_gets`` makes it
        # raise, mirroring the coded not-found the real client raises.
        self._gets = gets or {}
        self._missing_gets = missing_gets or set()
        self._inflight = 0
        self.max_children_inflight = 0
        # Weakrefs to every response handed out, plus the high-water mark of
        # how many were alive at once — the memory bound the batching claims.
        self._responses: list[weakref.ref[_Response]] = []
        self.max_live_responses = 0
        self.get_calls: list[str] = []
        # Frontier call ARGUMENTS, not just counts: the retry must re-ask each
        # read exactly as the first generation did.
        self.ready_args: list[dict[str, Any]] = []
        self.blocked_args: list[dict[str, Any]] = []
        self._edges = edges or {}
        # Edges that come into existence only AFTER the ready frontier was
        # read. ``task_ready`` is awaited in the opening gather and
        # ``task_edge_list`` strictly later, so this scripts the real ordering:
        # an edge created in between is the NEWER fact, and no frontier
        # response from before it can speak to it.
        self._late_edges = late_edges or {}
        self._fail_edges = fail_edges
        # A read that has nothing to do with which ROWS the board holds: the
        # error banner reports it, and the row-level claims stand.
        self._fail_stats = fail_stats
        self.edge_list_calls: list[dict[str, Any]] = []
        self.open_calls = 0
        self.ready_calls = 0
        self.blocked_calls = 0
        self.children_calls: list[dict[str, Any]] = []
        # Which ready call starts failing (0-based): scripts a first
        # generation that succeeds and a RETRY that does not. The same for the
        # completed window — the retry re-reads it too, so it has the same
        # power to leave the generation mixed.
        self._fail_ready_from = fail_ready_from
        self._fail_completed_from = fail_completed_from
        # Per-tool frontier failures: each read reports its own error line
        # ("ready" vs "blocked"), so the two are scripted separately.
        self._ready_error = ready_error
        self._blocked_error = blocked_error
        self.open_calls = 0
        self.ready_calls = 0
        self.blocked_calls = 0
        self.completed_calls = 0
        self.cancelled_calls = 0
        self.list_calls: list[dict[str, Any]] = []
        # The Lithos TOOL name of every call this fake answers, in the order it
        # answered them — the whole read budget of a load, not the six
        # per-tool logs above. A cost assertion that reads only the logs that
        # happen to exist cannot see a read nobody thought to instrument.
        self.calls: list[str] = []

    @staticmethod
    def _as_sequence(value: list[Any]) -> list[list[Any]]:
        if value and isinstance(value[0], list):
            return value
        return [value]

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
        # Mirror the real server: agent/tag are filtered upstream when passed.
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
        self.calls.append("lithos_task_list")
        if (status or "open") == "open":
            index = min(self.open_calls, len(self._open_seq) - 1)
            self.open_calls += 1
            rows = self._open_seq[index]
        elif status == "completed":
            if self._fail_completed_from is not None and (
                self.completed_calls >= self._fail_completed_from
            ):
                self.completed_calls += 1
                raise RuntimeError("completed window unavailable")
            index = min(self.completed_calls, len(self._completed_seq) - 1)
            self.completed_calls += 1
            rows = self._completed_seq[index]
        else:
            index = min(self.cancelled_calls, len(self._cancelled_seq) - 1)
            self.cancelled_calls += 1
            rows = self._cancelled_seq[index]
        if resolved_since:
            # Upstream windows on resolved_at and drops NULL-resolved rows.
            rows = [
                task
                for task in rows
                if task.resolved_at and task.resolved_at >= resolved_since
            ]
        if agent:
            rows = [task for task in rows if task.created_by == agent]
        if tags:
            rows = [task for task in rows if all(tag in task.tags for tag in tags)]
        if not with_claims:
            # Contract: claims are OMITTED unless requested, and the normalizer
            # renders that absence as ``None`` (unknown), not an empty tuple.
            rows = [replace(task, claims=None) for task in rows]
        return rows

    async def task_ready(
        self,
        *,
        limit: int | None = None,
        with_claims: bool = False,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[TaskRecord]:
        self.ready_args.append({"limit": limit, "with_claims": with_claims})
        self.calls.append("lithos_task_ready")
        if self._fail_ready:
            raise RuntimeError("ready frontier unavailable")
        if self._fail_ready_from is not None and self.ready_calls >= (
            self._fail_ready_from
        ):
            self.ready_calls += 1
            raise RuntimeError("ready frontier unavailable")
        if self._ready_error is not None:
            self.ready_calls += 1
            raise self._ready_error
        index = min(self.ready_calls, len(self._ready_seq) - 1)
        self.ready_calls += 1
        rows = self._ready_seq[index]
        return rows[:limit] if limit is not None else rows

    async def task_blocked(
        self,
        *,
        limit: int | None = None,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[BlockedTaskRecord]:
        self.blocked_args.append({"limit": limit})
        self.calls.append("lithos_task_blocked")
        if self._blocked_error is not None:
            self.blocked_calls += 1
            raise self._blocked_error
        index = min(self.blocked_calls, len(self._blocked_seq) - 1)
        self.blocked_calls += 1
        rows = self._blocked_seq[index]
        return rows[:limit] if limit is not None else rows

    async def task_children(
        self,
        task_id: str,
        *,
        recursive: bool = False,
        include_closed: bool = False,
    ) -> list[TaskRecord]:
        self.children_calls.append(
            {
                "task_id": task_id,
                "recursive": recursive,
                "include_closed": include_closed,
            }
        )
        self.calls.append("lithos_task_children")
        self._inflight += 1
        self.max_children_inflight = max(self.max_children_inflight, self._inflight)
        try:
            # Yield so concurrent calls actually overlap: without an await the
            # coroutine would run start-to-finish and never observe a peak.
            await asyncio.sleep(0)
            if task_id in self._fail_children:
                raise RuntimeError(f"children unavailable for {task_id}")
            # A fresh list per call, like the real client's normalization —
            # returning the fixture's own list would make it immortal and the
            # residency probe meaningless.
            rows = _Response(
                task
                for task in self._children.get(task_id, [])
                if include_closed or task.status == "open"
            )
            self._responses.append(weakref.ref(rows))
            self.max_live_responses = max(
                self.max_live_responses,
                sum(1 for ref in self._responses if ref() is not None),
            )
            return rows
        finally:
            self._inflight -= 1

    async def task_edge_list(
        self,
        task_id: str,
        *,
        direction: str = "both",
        types: list[str] | None = None,
    ) -> list[EdgeRecord]:
        # Recorded rather than answered: every assertion below is about
        # WHETHER the Gates section reaches for edges at all.
        self.edge_list_calls.append(
            {"task_id": task_id, "direction": direction, "types": types}
        )
        self.calls.append("lithos_task_edge_list")
        if self._fail_edges:
            raise RuntimeError(f"edges unavailable for {task_id}")
        edges = list(self._edges.get(task_id, []))
        if self.ready_calls:
            edges.extend(self._late_edges.get(task_id, []))
        return edges

    async def task_get(self, task_id: str) -> TaskRecord:
        self.get_calls.append(task_id)
        self.calls.append("lithos_task_get")
        if task_id in self._gets:
            return self._gets[task_id]
        if task_id not in self._missing_gets:
            for rows in self._open_seq:
                for task in rows:
                    if task.id == task_id:
                        return task
        raise RuntimeError(f"task '{task_id}' not found")

    async def stats(self) -> dict[str, Any]:
        self.calls.append("lithos_stats")
        if self._fail_stats:
            raise RuntimeError("stats unavailable")
        return {"open_claims": 2, "agents": 3}

    async def list_agents(self) -> list[AgentRecord]:
        self.calls.append("lithos_agent_list")
        return [AgentRecord(id="a"), AgentRecord(id="b")]


_FILTERS = TaskFilters(
    statuses=("open", "completed", "cancelled"), tags=(), agent="", since=""
)


def test_load_dashboard_partitions_and_counts() -> None:
    in_prog = _task("c", claims=(ClaimRecord(agent="a", aspect="impl"),))
    ready = _task("r", claims=())
    blocked = _task("b", claims=())
    fake = _FrontierFake(
        open_tasks=[in_prog, ready, blocked],
        ready=[ready],
        blocked=[_blocked(blocked, BlockerRecord(kind="task", task_id="c"))],
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))
    assert _section_ids(data.sections, "in_progress") == ["c"]
    assert _section_ids(data.sections, "ready") == ["r"]
    assert _section_ids(data.sections, "blocked") == ["b"]
    assert data.summary.in_progress == 1
    assert data.summary.ready == 1
    assert data.summary.blocked == 1
    assert data.summary.open_total == 3
    assert data.truncated is False
    assert data.errors == ()


def test_load_dashboard_names_a_cancelled_predecessor_from_the_terminal_read() -> None:
    """The unsatisfiable row is the one that most needs a readable blocker.

    A cancelled predecessor is by definition NOT in the open snapshot, so the
    chip and the reason sentence both fell back to the raw task id — on the
    board's highest-severity row, while the same page rendered that task's
    title in the Cancelled group. The terminal reads are already made, so the
    join resolves the name from them.
    """
    stranded = _task("stranded", claims=())
    dead = _task("dead", claims=(), status="cancelled")
    fake = _FrontierFake(
        open_tasks=[stranded],
        ready=[],
        blocked=[
            _blocked(
                stranded,
                BlockerRecord(
                    kind="blocker_unsatisfiable",
                    task_id="dead",
                    type="blocks",
                    status="cancelled",
                ),
            )
        ],
        cancelled=[dead],
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    (row,) = data.sections["attention"]
    assert row.task.id == "stranded"
    assert [chip.label for chip in row.blockers] == ["Title dead"]
    (reason,) = row.attention
    assert reason.rule == "unsatisfiable"
    assert '"Title dead"' in reason.detail


def test_load_dashboard_resolves_blocker_title_when_predecessor_filtered_out() -> None:
    """Regression (f-001): the blocker chip must render the predecessor's TITLE
    even when a tag filter hides the predecessor from the visible sections. The
    master open list is fetched unfiltered so the join can still resolve it."""
    blocked = _task("blk", claims=(), tags=("project:a",))
    pred = _task("pred", claims=(), tags=("project:b",))
    fake = _FrontierFake(
        open_tasks=[blocked, pred],
        ready=[pred],
        blocked=[
            _blocked(blocked, BlockerRecord(kind="task", task_id="pred", type="blocks"))
        ],
    )
    filters = TaskFilters(statuses=("open",), tags=("project:a",), agent="", since="")
    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert _section_ids(data.sections, "blocked") == ["blk"]
    (row,) = data.sections["blocked"]
    assert row.blockers[0].label == "Title pred"
    assert row.blockers[0].target_id == "pred"
    # The predecessor itself is filtered out of the visible sections.
    assert _section_ids(data.sections, "ready") == []


def test_load_dashboard_windows_terminal_sections_by_resolution_time() -> None:
    """T1-S10: the Completed/Cancelled window is pushed as ``resolved_since``
    (never the created-at ``since``), so a task created long before the window
    but resolved inside it renders — and one resolved before it does not."""
    ancient = replace(
        _task("ancient", claims=()),
        status="completed",
        created_at="2020-01-01T00:00:00+00:00",
        resolved_at="2026-08-08T00:00:00+00:00",
    )
    stale = replace(
        _task("stale", claims=()),
        status="completed",
        created_at="2026-07-30T00:00:00+00:00",
        resolved_at="2026-07-31T00:00:00+00:00",
    )
    fake = _FrontierFake(
        open_tasks=[], ready=[], blocked=[], completed=[ancient, stale]
    )
    filters = TaskFilters(
        statuses=("open", "completed", "cancelled"),
        tags=(),
        agent="",
        since="2026-08-01",
    )
    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert _section_ids(data.sections, "completed") == ["ancient"]
    assert data.summary.recent_completed == 1
    terminal_calls = [
        {key: call[key] for key in ("status", "since", "resolved_since")}
        for call in fake.list_calls
        if call["status"] != "open"
    ]
    assert terminal_calls == [
        {"status": "completed", "since": None, "resolved_since": "2026-08-01"},
        {"status": "cancelled", "since": None, "resolved_since": "2026-08-01"},
    ]


def test_load_dashboard_orders_terminal_rows_newest_resolved_first() -> None:
    """Terminal rows come from a resolved-time window, so they sort by
    resolution — creation order would bury just-finished old work."""
    old_created = replace(
        _task("old-created", claims=()),
        status="completed",
        created_at="2020-01-01T00:00:00+00:00",
        resolved_at="2026-08-08T00:00:00+00:00",
    )
    new_created = replace(
        _task("new-created", claims=()),
        status="completed",
        created_at="2026-08-02T00:00:00+00:00",
        resolved_at="2026-08-03T00:00:00+00:00",
    )
    fake = _FrontierFake(
        open_tasks=[], ready=[], blocked=[], completed=[new_created, old_created]
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert _section_ids(data.sections, "completed") == ["old-created", "new-created"]


def test_load_dashboard_frontier_error_is_not_reported_as_truncation() -> None:
    """Regression (f-002): a failed frontier read is an error (surfaced by the
    banner), NOT frontier-limit truncation — ``truncated`` must stay False so
    the dashboard doesn't claim a false cap.

    §14 also settles where the row goes: the master open list renders FLAT.
    Leaving it in "Not classified" would file an outage under the tail whose
    banner explains it as frontier-limit overflow.
    """
    ready = _task("r", claims=())
    fake = _FrontierFake(open_tasks=[ready], ready=[ready], blocked=[], fail_ready=True)
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))
    assert data.open_flat is True
    assert _section_ids(data.sections, "open") == ["r"]
    assert _section_ids(data.sections, "unclassified") == []
    assert data.truncated is False
    assert any("ready frontier" in message for message in data.errors)


def test_load_dashboard_flags_truncation_only_at_the_limit() -> None:
    """Truncation means a frontier response actually HIT frontier_limit: with
    limit=1 and two ready-able rows the ready read returns one row, the other
    lands in the Not-classified tail, and that tail IS truncation."""
    r1 = _task("r1", claims=())
    r2 = _task("r2", claims=())
    fake = _FrontierFake(open_tasks=[r1, r2], ready=[r1, r2], blocked=[])
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=1))

    assert _section_ids(data.sections, "ready") == ["r1"]
    assert _section_ids(data.sections, "unclassified") == ["r2"]
    assert data.truncated is True
    assert data.reconciliation_pending is False
    assert data.errors == ()
    # At the limit there is nothing to reconcile — no retry is spent.
    assert fake.ready_calls == 1


def test_only_the_capped_frontier_side_marks_its_counter_approximate() -> None:
    """Story 28, the sharp half: ``lithos_task_ready`` and ``lithos_task_blocked``
    are capped INDEPENDENTLY, so a board where only Ready hit the limit must not
    call the exact Blocked count approximate. Only the counters the capped read
    feeds are marked — Ready itself, and Needs attention, which is promoted out
    of both workable sections."""
    r1, r2, r3 = (_task(f"r{n}", claims=()) for n in (1, 2, 3))
    blocked = _task("b", claims=())
    fake = _FrontierFake(
        open_tasks=[r1, r2, r3, blocked],
        ready=[r1, r2, r3],  # honors limit=2 -> capped, r3 falls into the tail
        blocked=[_blocked(blocked, BlockerRecord(kind="task", task_id="p"))],
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=2))

    assert _section_ids(data.sections, "unclassified") == ["r3"]
    assert data.truncated is True
    # The Ready read was cut short; the Blocked read answered in full.
    assert data.summary.approximate == frozenset({"ready", "attention"})
    assert data.summary.approximate_frontiers == ("ready",)
    # The counts claims alone decide are untouched by any cap.
    assert "in_progress" not in data.summary.approximate
    assert "claims_unknown" not in data.summary.approximate


def test_only_the_capped_frontier_side_marks_its_counter_approximate_blocked() -> None:
    """The mirror: Blocked at the limit while Ready answered in full marks the
    Blocked counter and leaves the exact Ready count alone."""
    ready = _task("r", claims=())
    b1, b2, b3 = (_task(f"b{n}", claims=()) for n in (1, 2, 3))
    fake = _FrontierFake(
        open_tasks=[ready, b1, b2, b3],
        ready=[ready],
        blocked=[  # honors limit=2 -> capped, b3 falls into the tail
            _blocked(row, BlockerRecord(kind="task", task_id="p"))
            for row in (b1, b2, b3)
        ],
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=2))

    assert _section_ids(data.sections, "unclassified") == ["b3"]
    assert data.truncated is True
    assert data.summary.approximate == frozenset({"blocked", "attention"})
    assert data.summary.approximate_frontiers == ("blocked",)


def test_both_capped_frontier_sides_mark_both_counters() -> None:
    """Both reads at the limit is the board-wide case the old single banner
    described — still reported, now as the union of the two sides."""
    r1, r2 = (_task(f"r{n}", claims=()) for n in (1, 2))
    b1, b2 = (_task(f"b{n}", claims=()) for n in (1, 2))
    fake = _FrontierFake(
        open_tasks=[r1, r2, b1, b2],
        ready=[r1, r2],
        blocked=[
            _blocked(row, BlockerRecord(kind="task", task_id="p")) for row in (b1, b2)
        ],
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=1))

    assert _section_ids(data.sections, "unclassified") == ["r2", "b2"]
    assert data.summary.approximate == frozenset({"ready", "blocked", "attention"})
    assert data.summary.approximate_frontiers == ("ready", "blocked")


def test_an_untruncated_frontier_marks_no_counter_approximate() -> None:
    """A response that merely FITS the limit exactly left nothing in the tail,
    so every counter is exact — the same evidence ``truncated`` is gated on."""
    ready = _task("r", claims=())
    fake = _FrontierFake(open_tasks=[ready], ready=[ready], blocked=[])
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=1))

    assert data.truncated is False
    assert data.summary.approximate == frozenset()
    assert data.summary.approximate_frontiers == ()


def test_frontier_read_error_never_marks_a_counter_approximate() -> None:
    """A frontier OUTAGE must never read as "we have too many tasks". The
    surviving read can sit on its cap — that is not evidence of truncation when
    its partner never answered, and §14 renders the board flat anyway."""
    ready = _task("r", claims=())
    blocked = _task("b", claims=())
    fake = _FrontierFake(
        open_tasks=[ready, blocked],
        ready=[ready],
        blocked=[_blocked(blocked, BlockerRecord(kind="task", task_id="p"))],
        fail_ready=True,
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=1))

    assert data.open_flat is True
    assert data.truncated is False
    assert data.summary.approximate == frozenset()
    assert any("ready frontier" in message for message in data.errors)


def test_load_dashboard_below_limit_gap_retries_then_classifies_blocked() -> None:
    """A workable open task absent from BOTH frontier responses while BELOW the
    limit is read-skew, not truncation (the reads are independent, not a
    snapshot). Policy: retry the ready+blocked pair once; if the gap persists,
    classify the row conservatively as Blocked with the reconciliation-warning
    surface — wrongly-Ready invites wasted operator attention, wrongly-Blocked
    is safe."""
    ready = _task("r", claims=())
    gap = _task("g", claims=())
    fake = _FrontierFake(open_tasks=[ready, gap], ready=[ready], blocked=[])
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    # Retried once, still inconsistent -> conservative Blocked, flagged.
    assert fake.ready_calls == 2
    assert fake.blocked_calls == 2
    assert _section_ids(data.sections, "unclassified") == []
    assert _section_ids(data.sections, "blocked") == ["g"]
    (row,) = data.sections["blocked"]
    assert row.reconciliation_pending is True
    assert data.truncated is False
    assert data.reconciliation_pending is True
    assert data.errors == ()


def test_load_dashboard_retry_heals_read_skew() -> None:
    """When the single retry returns a consistent pair, the row classifies
    normally — no warning, no conservative bucket."""
    ready = _task("r", claims=())
    gap = _task("g", claims=())
    fake = _FrontierFake(
        open_tasks=[ready, gap],
        ready=[[ready], [ready, gap]],  # first read misses g, retry sees it
        blocked=[],
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert fake.ready_calls == 2
    assert _section_ids(data.sections, "ready") == ["r", "g"]
    assert data.reconciliation_pending is False
    assert data.truncated is False


def test_load_dashboard_ready_and_blocked_overlap_goes_conservative_blocked() -> None:
    """The same id in BOTH frontier responses is read-skew too: after the
    failed retry the row must land in Blocked (never Ready) with the
    reconciliation warning."""
    both = _task("x", claims=())
    blocked_record = _blocked(both, BlockerRecord(kind="task", task_id="p"))
    fake = _FrontierFake(open_tasks=[both], ready=[both], blocked=[blocked_record])
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert fake.ready_calls == 2
    assert _section_ids(data.sections, "ready") == []
    assert _section_ids(data.sections, "blocked") == ["x"]
    (row,) = data.sections["blocked"]
    assert row.reconciliation_pending is True
    assert data.reconciliation_pending is True
    assert data.truncated is False


def test_retry_refreshes_master_open_and_completed_task_renders_once() -> None:
    """Reviewer repro (finding 1): a task that completed between the stale
    open read and the closed read must not render TWICE (Blocked from the
    stale open + Completed). On skew the master-open read retries with the
    frontier pair, the later snapshot takes precedence, and the row renders
    exactly once, in its terminal section."""
    done = _task("x", claims=())
    ready = _task("r", claims=())
    done_completed = TaskRecord(
        id="x", title="Title x", status="completed", task_type="task"
    )
    fake = _FrontierFake(
        # Stale first open read still contains x; the retried snapshot doesn't.
        open_tasks=[[done, ready], [ready]],
        ready=[ready],
        blocked=[],
        completed=[done_completed],
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert fake.open_calls == 2
    assert fake.ready_calls == 2
    # x appears in NO open section…
    for section in (
        "in_progress",
        "ready",
        "blocked",
        "claims_unknown",
        "unclassified",
    ):
        assert "x" not in _section_ids(data.sections, section)
    # …and exactly once, in Completed.
    assert _section_ids(data.sections, "completed") == ["x"]
    # The skew healed with the refreshed snapshot: no warning, no truncation.
    assert data.reconciliation_pending is False
    assert data.truncated is False


def test_open_terminal_overlap_retries_then_open_wins_when_still_open() -> None:
    """Round 4: an id in both the initial open snapshot and a terminal list is
    freshness skew — it retries ALL THREE reads once rather than silently
    preferring Open. When the retried snapshot still contains the task, the
    open section wins for real (evidence-based) and the terminal record drops."""
    both = _task("x", claims=())
    stale_completed = TaskRecord(
        id="x", title="Title x", status="completed", task_type="task"
    )
    fake = _FrontierFake(
        open_tasks=[both], ready=[both], blocked=[], completed=[stale_completed]
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert fake.open_calls == 2
    assert fake.ready_calls == 2
    assert _section_ids(data.sections, "ready") == ["x"]
    assert _section_ids(data.sections, "completed") == []
    assert data.summary.recent_completed == 0
    # Terminal overlap drives the retry, not the moved-to-Blocked banner.
    assert data.reconciliation_pending is False


def test_open_terminal_overlap_retry_lets_terminal_win_when_open_drops_it() -> None:
    """The other outcome: the retried open snapshot no longer contains the
    task, so the later-snapshot precedence lets the terminal record render —
    exactly one row, in Completed."""
    both = _task("x", claims=())
    done = TaskRecord(id="x", title="Title x", status="completed", task_type="task")
    fake = _FrontierFake(
        open_tasks=[[both], []],
        ready=[[both], []],
        blocked=[],
        completed=[done],
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert fake.open_calls == 2
    for section in (
        "in_progress",
        "ready",
        "blocked",
        "claims_unknown",
        "unclassified",
    ):
        assert "x" not in _section_ids(data.sections, section)
    assert _section_ids(data.sections, "completed") == ["x"]
    assert data.reconciliation_pending is False


def test_overlap_is_skew_even_at_the_frontier_limit() -> None:
    """Reviewer repro (finding 2): frontier_limit=1 with the SAME task in both
    responses. at_limit must not mask the contradiction — retry, then
    conservative Blocked with the warning."""
    both = _task("x", claims=())
    blocked_record = _blocked(both, BlockerRecord(kind="task", task_id="p"))
    fake = _FrontierFake(open_tasks=[both], ready=[both], blocked=[blocked_record])
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=1))

    assert fake.ready_calls == 2
    assert _section_ids(data.sections, "ready") == []
    assert _section_ids(data.sections, "blocked") == ["x"]
    assert data.reconciliation_pending is True


def test_overlap_on_a_filtered_out_task_is_a_no_op() -> None:
    """An overlap whose task the agent/tag filter hides changes nothing that
    renders: no retry, no banner."""
    hidden = _task("h", claims=(), tags=("project:other",))
    visible = _task("v", claims=(), tags=("project:mine",))
    fake = _FrontierFake(
        open_tasks=[hidden, visible],
        ready=[hidden, visible],
        blocked=[_blocked(hidden, BlockerRecord(kind="task", task_id="p"))],
    )
    filters = TaskFilters(
        statuses=("open",), tags=("project:mine",), agent="", since=""
    )
    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert fake.ready_calls == 1
    assert _section_ids(data.sections, "ready") == ["v"]
    assert data.reconciliation_pending is False


def test_claimed_overlap_retries_then_stays_in_progress_flagged() -> None:
    """Reviewer repro (round 4): blocked membership also drives the
    claimed_but_blocked decoration and blocker chips on an In-progress row, so
    a claimed task returned by BOTH frontiers is render-effective skew — it
    must retry, and on persistence keep the task In progress WITH the blocked
    decoration (conservative interpretation) but marked awaiting
    reconciliation so the banner/badge explain it."""
    claimed = _task("c", claims=(ClaimRecord(agent="a", aspect="impl"),))
    fake = _FrontierFake(
        open_tasks=[claimed],
        ready=[claimed],
        blocked=[_blocked(claimed, BlockerRecord(kind="task", task_id="p"))],
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert fake.ready_calls == 2
    assert _section_ids(data.sections, "in_progress") == ["c"]
    (row,) = data.sections["in_progress"]
    assert row.claimed_but_blocked is True
    assert row.blockers
    assert row.reconciliation_pending is True
    assert data.reconciliation_pending is True
    # Nothing moved to Blocked — the claim still wins the section.
    assert _section_ids(data.sections, "blocked") == []


def test_claimed_overlap_healed_on_retry_leaves_no_residual_flags() -> None:
    """When the retry drops the claimed task from the blocked response, the
    false anomaly disappears entirely: no decoration, no pending flag, no
    banner."""
    claimed = _task("c", claims=(ClaimRecord(agent="a", aspect="impl"),))
    fake = _FrontierFake(
        open_tasks=[claimed],
        ready=[claimed],
        blocked=[[_blocked(claimed, BlockerRecord(kind="task", task_id="p"))], []],
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert fake.ready_calls == 2
    (row,) = data.sections["in_progress"]
    assert row.claimed_but_blocked is False
    assert row.blockers == ()
    assert row.reconciliation_pending is False
    assert data.reconciliation_pending is False


def _terminal_reads(fake: _FrontierFake) -> list[str | None]:
    """The status of every terminal-window read, in call order."""
    return [
        call["status"]
        for call in fake.list_calls
        if call["status"] in ("completed", "cancelled")
    ]


def test_a_ready_only_row_retries_every_read_of_the_generation() -> None:
    """Regression (loom ``lens43-composed-projects``): a row the FRONTIER
    returned and the open read did not is skew too.

    Ordering: both terminal windows answer empty, ``task_ready`` returns T, T
    completes, the master open read answers empty. T was then in no section,
    in no overlap, and nothing made the state retry-worthy — so the board
    rendered "No tasks in this window" over a read of its own generation that
    had returned a task.

    It must retry, and retry ALL FIVE reads: adopting a fresh open/ready/
    blocked triple beside the first generation's stale (empty) completed
    window is the mixed generation §14 forbids, and would lose T entirely.
    """
    t = _task("t", claims=())
    done = replace(t, status="completed")
    fake = _FrontierFake(
        open_tasks=[[], []],
        ready=[[t], []],
        blocked=[],
        completed=[[], [done]],
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert (fake.open_calls, fake.ready_calls, fake.blocked_calls) == (2, 2, 2)
    assert _terminal_reads(fake) == [
        "completed",
        "cancelled",
        "completed",
        "cancelled",
    ]
    # The whole retried generation is adopted, so T renders where it now is.
    assert _section_ids(data.sections, "completed") == ["t"]
    assert data.nothing_to_show is False
    assert data.errors == ()


def test_a_blocked_only_row_retries_the_same_way_a_ready_only_row_does() -> None:
    """The blocked frontier is the other half of the same contradiction: a
    task it returned that the open read never saw retries the generation and
    lands wherever the retried reads find it."""
    t = _task("t", claims=())
    done = replace(t, status="completed")
    fake = _FrontierFake(
        open_tasks=[[], []],
        ready=[],
        blocked=[[_blocked(t, BlockerRecord(kind="task", task_id="p"))], []],
        completed=[[], [done]],
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert (fake.open_calls, fake.ready_calls, fake.blocked_calls) == (2, 2, 2)
    assert _section_ids(data.sections, "completed") == ["t"]
    assert data.nothing_to_show is False


def test_a_coherent_empty_generation_may_still_say_nothing_to_show() -> None:
    """The retry ARBITRATES rather than vetoing the panel: when the re-read of
    every one of the five finds nothing anywhere, the load holds a coherent
    empty generation and "No tasks in this window" is a claim it supports."""
    t = _task("t", claims=())
    fake = _FrontierFake(open_tasks=[[], []], ready=[[t], []], blocked=[])

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert fake.ready_calls == 2
    assert data.nothing_to_show is True
    assert data.errors == ()


def test_the_adopted_generation_drops_the_first_terminal_rows() -> None:
    """Whole-or-none, from the direction the other tests cannot see.

    They all script terminal windows that GAIN a row on the retry, which a
    buggy implementation retaining (or unioning) the first generation's
    terminal results would still pass. Here the first generation is the one
    holding rows: T on the ready frontier the open read never saw (the retry
    trigger), a stale completed row and a stale cancelled row. The retry finds
    nothing anywhere — including both windows — so the adopted generation is
    empty, the two stale rows are GONE from the page, and the load may say so.
    Keeping them would be the mixed generation §14 forbids, and would render
    resolved rows this generation never read.
    """
    t = _task("t", claims=())
    stale_done = replace(t, id="stale-done", status="completed")
    stale_gone = replace(t, id="stale-gone", status="cancelled")
    fake = _FrontierFake(
        open_tasks=[[], []],
        ready=[[t], []],
        blocked=[],
        completed=[[stale_done], []],
        cancelled=[[stale_gone], []],
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert (fake.open_calls, fake.ready_calls, fake.blocked_calls) == (2, 2, 2)
    assert _terminal_reads(fake) == [
        "completed",
        "cancelled",
        "completed",
        "cancelled",
    ]
    # The first generation's terminal rows are not carried over…
    assert _section_ids(data.sections, "completed") == []
    assert _section_ids(data.sections, "cancelled") == []
    # …so the empty panel describes the adopted generation, not a mix of two.
    assert data.nothing_to_show is True
    assert data.errors == ()


@pytest.mark.parametrize("side", ["ready", "blocked"])
def test_a_frontier_only_row_is_skew_even_at_the_frontier_limit(side: str) -> None:
    """The at-limit twin of the overlap regression above.

    Truncation explains a frontier response that is SHORT of rows the open list
    holds; it explains nothing about a row the frontier RETURNED and the open
    read never saw. That contradiction stands at the cap exactly as below it,
    on either frontier — so the generation retries whole, and the retried one
    (which finds T resolved) is what renders.
    """
    t = _task("t", claims=())
    done = replace(t, status="completed")
    ghost_blocked = _blocked(t, BlockerRecord(kind="task", task_id="p"))
    fake = _FrontierFake(
        open_tasks=[[], []],
        # Exactly ``frontier_limit`` rows on the side under test: the response
        # hit its cap AND contradicts the open read.
        ready=[[t], []] if side == "ready" else [],
        blocked=[[ghost_blocked], []] if side == "blocked" else [],
        completed=[[], [done]],
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=1))

    assert (fake.open_calls, fake.ready_calls, fake.blocked_calls) == (2, 2, 2)
    assert _terminal_reads(fake) == [
        "completed",
        "cancelled",
        "completed",
        "cancelled",
    ]
    assert _section_ids(data.sections, "completed") == ["t"]
    assert data.nothing_to_show is False
    # A cap that returned a contradiction is not truncation to report: the
    # retried generation left no classifiable row in the tail.
    assert data.truncated is False
    assert data.errors == ()


def test_a_below_limit_gap_adopts_the_retried_terminal_windows_too() -> None:
    """Variant 2 of the same defect, reached through the OTHER skew signal.

    X is in the initial open read but in neither frontier (a below-limit gap),
    so the generation retries; the retry finds X gone from open/ready/blocked
    and resolved in the completed window. While the retry re-read only three
    of the five, the load kept the first generation's empty completed result
    and X — a task this one load saw twice — rendered in no section at all.
    """
    x = _task("x", claims=())
    done = replace(x, status="completed")
    fake = _FrontierFake(
        open_tasks=[[x], []],
        ready=[],
        blocked=[],
        completed=[[], [done]],
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert fake.open_calls == 2
    assert _terminal_reads(fake) == [
        "completed",
        "cancelled",
        "completed",
        "cancelled",
    ]
    for section in (
        "in_progress",
        "ready",
        "blocked",
        "claims_unknown",
        "unclassified",
    ):
        assert "x" not in _section_ids(data.sections, section)
    assert _section_ids(data.sections, "completed") == ["x"]
    assert data.nothing_to_show is False
    assert data.reconciliation_pending is False


def test_a_frontier_only_row_surviving_the_retry_withholds_the_empty_panel() -> None:
    """The contradiction can PERSIST: both generations answer open ``[]`` and
    ready ``[T]`` with empty terminal windows.

    T is a task this load read twice, so the empty-state panel may not claim
    the corpus is empty — and no read of either generation PLACED it, so the
    system-wide "All systems healthy — 0 issues" claim is withheld too: the
    attention rules only ever see rows that reached a section. The
    reconciliation banner still stays away as designed (it annotates a
    rendered row, and there is none). The retry stays single-shot.
    """
    t = _task("t", claims=())
    fake = _FrontierFake(open_tasks=[], ready=[t], blocked=[])

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    # Exactly two generations — one retry, no loop.
    assert (fake.open_calls, fake.ready_calls, fake.blocked_calls) == (2, 2, 2)
    assert (fake.completed_calls, fake.cancelled_calls) == (2, 2)
    assert data.nothing_to_show is False
    # Not the reconciliation surface: no row moved, so nothing is annotated.
    assert data.reconciliation_pending is False
    assert data.errors == ()
    # T is in no section — the whole reason the affirmative claim is withheld.
    assert all(not rows for rows in data.sections.values())
    assert data.frontier_unplaced is True
    assert data.healthy is False


def test_a_frontier_only_row_the_terminal_window_explains_still_renders() -> None:
    """Frontier-only does NOT mean "renders nowhere".

    Both generations agree: open ``[]``, ready ``[T]`` — and the completed
    window of the same generation returns T, which is exactly how a task that
    resolved between the two open-side reads is supposed to surface. So T IS
    on the page, under Completed; the load may not call the corpus empty, and
    nothing about this state may claim the row is missing from the board.
    """
    t = _task("t", claims=())
    done = replace(t, status="completed", resolved_at="2026-09-12T00:00:00Z", claims=())
    fake = _FrontierFake(open_tasks=[], ready=[t], blocked=[], completed=[done])

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert _section_ids(data.sections, "completed") == ["t"]
    assert data.nothing_to_show is False
    assert data.reconciliation_pending is False
    assert data.errors == ()
    # Still the settled gate: the row this load read is PLACED (the resolved
    # window explains the frontier-only id), so there is no degraded signal to
    # report and the stripe is not withheld.
    assert data.frontier_unplaced is False
    assert data.healthy is True


def test_an_unplaced_frontier_only_row_withholds_the_healthy_stripe() -> None:
    """Reviewer repro (#82, PR re-review): the stripe is withheld by the
    UNPLACED row itself, not by an empty board.

    Here the board is otherwise the healthy one: every read answered, nothing
    truncated, no filter, one Ready row rendered. The blocked frontier also
    returns G, which the open read never saw and neither resolved window
    explains — so G reached no section and the attention rules never evaluated
    it. G may be a newly-ready open task the open read missed, so "0 issues"
    cannot be asserted; nothing else about the load is degraded, so no banner
    and no row decoration appear.
    """
    rendered = _task("r", claims=())
    ghost = _task("g", claims=())
    fake = _FrontierFake(
        open_tasks=[rendered],
        ready=[rendered],
        blocked=[_blocked(ghost, BlockerRecord(kind="task", task_id="x"))],
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    # A blocked-only row drives the retry exactly as a ready-only one does.
    assert (fake.open_calls, fake.ready_calls, fake.blocked_calls) == (2, 2, 2)
    assert _section_ids(data.sections, "ready") == ["r"]
    assert "g" not in _section_ids(data.sections, "blocked")
    assert data.frontier_unplaced is True
    assert data.healthy is False
    # The stripe is the ONLY thing withheld: no error, no truncation, no
    # reconciliation surface, and the board is not called empty.
    assert data.errors == ()
    assert data.truncated is False
    assert data.reconciliation_pending is False
    assert data.nothing_to_show is False


def test_the_retry_adopts_the_cancelled_window_and_re_asks_the_same_reads() -> None:
    """The adopted generation is whole on the CANCELLED side too, and the
    re-read must ask each question exactly as the first gather did: same
    ``resolved_since`` window, same claim request, same frontier limit.
    A retry that re-read a different query would adopt a generation the first
    one cannot be compared with."""
    t = _task("t", claims=(), created_by="ada")
    dropped = replace(t, status="cancelled", resolved_at="2026-09-12T00:00:00Z")
    fake = _FrontierFake(
        open_tasks=[[], []],
        ready=[[t], []],
        blocked=[],
        cancelled=[[], [dropped]],
    )
    filters = TaskFilters(
        statuses=TASK_STATUSES, tags=(), agent="ada", since="2026-09-01"
    )

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    # The retried cancelled result is what renders — not the first (empty) one.
    assert _section_ids(data.sections, "cancelled") == ["t"]
    # Both generations asked the same five questions.
    open_reads = [call for call in fake.list_calls if call["status"] == "open"]
    terminal = [
        call for call in fake.list_calls if call["status"] in ("completed", "cancelled")
    ]
    assert len(open_reads) == 2 and open_reads[0] == open_reads[1]
    assert [call["status"] for call in terminal] == [
        "completed",
        "cancelled",
        "completed",
        "cancelled",
    ]
    assert all(
        call["resolved_since"] == "2026-09-01" and call["with_claims"] is True
        for call in terminal
    )
    assert fake.ready_args == [{"limit": 500, "with_claims": False}] * 2
    assert fake.blocked_args == [{"limit": 500}] * 2


def test_a_terminal_read_failing_the_retry_keeps_the_first_generation() -> None:
    """All five or none: a terminal window that does not answer the retry is a
    failed re-read like any other, and the retried triple beside it is thrown
    away with it.

    The first generation is materially different from the (successful) retried
    one — X is Ready and D is in the completed window, where the retry would
    have emptied the board — so adopting the wrong one is visible in the
    sections rather than only in a flag.
    """
    x = _task("x", claims=())
    gap = _task("g", claims=())
    done = TaskRecord(id="d", title="Title d", status="completed", task_type="task")
    fake = _FrontierFake(
        # The below-limit gap (g in neither frontier) is what asks for a retry.
        open_tasks=[[x, gap], []],
        ready=[[x], []],
        blocked=[],
        completed=[done],
        fail_completed_from=1,
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    # The completed window WAS part of the retry, and it is what failed.
    assert (fake.open_calls, fake.ready_calls, fake.completed_calls) == (2, 2, 2)
    assert RETRY_FAILED_ERROR in data.errors
    assert data.healthy is False
    # …so the whole first generation still renders: the retried open/ready pair
    # would have emptied both of these.
    assert _section_ids(data.sections, "ready") == ["x"]
    assert _section_ids(data.sections, "completed") == ["d"]
    # …including the unhealed gap's conservative Blocked placement.
    assert _section_ids(data.sections, "blocked") == ["g"]
    assert data.reconciliation_pending is True


def test_summary_counts_exclude_claims_unknown_rows() -> None:
    unknown = _task("u", claims=None)
    ready = _task("r", claims=())
    fake = _FrontierFake(
        open_tasks=[unknown, ready], ready=[unknown, ready], blocked=[]
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.summary.ready == 1
    assert data.summary.blocked == 0
    assert data.summary.claims_unknown == 1
    # Visible, never hidden: the row is in the board and the open total.
    assert _section_ids(data.sections, "claims_unknown") == ["u"]
    assert data.summary.open_total == 2
    # An unknown row on the ready frontier is not skew — its bucket does not
    # depend on the frontier answer.
    assert fake.ready_calls == 1
    assert data.reconciliation_pending is False


def test_overlap_row_with_unknown_claims_stays_in_claims_unknown() -> None:
    """Combined case: same task in both frontier responses AND claims=None.
    The claims-unknown bucket wins (its rendering could not change), so no
    retry and no reconciliation banner — just the degraded-data group."""
    both = _task("x", claims=None)
    fake = _FrontierFake(
        open_tasks=[both],
        ready=[both],
        blocked=[_blocked(both, BlockerRecord(kind="task", task_id="p"))],
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert fake.ready_calls == 1
    assert _section_ids(data.sections, "claims_unknown") == ["x"]
    assert _section_ids(data.sections, "ready") == []
    assert _section_ids(data.sections, "blocked") == []
    assert data.reconciliation_pending is False


# ── T1 slice 9: filters rebase ─────────────────────────────────────────


def test_project_and_agent_filters_are_never_pushed_upstream() -> None:
    """Only the resolved-time window is pushed: the upstream agent argument is
    creator-only and no upstream call can express the metadata-OR-tag project
    match, so both filters are applied client-side over the fetched rows.

    The window rides on ``resolved_since`` (T1-S10), never ``since``: terminal
    sections are scoped by when work FINISHED, so pushing the same value as a
    created-at bound would silently drop long-running work that resolved inside
    the window.
    """
    mine = _task("m", claims=(), created_by="agent-zero", tags=("project:influx",))
    fake = _FrontierFake(open_tasks=[mine], ready=[mine], blocked=[])
    filters = TaskFilters(
        statuses=("open", "completed", "cancelled"),
        tags=("area:docs",),
        agent="agent-zero",
        since="2026-04-01",
        projects=("influx",),
    )

    asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    closed_calls = [call for call in fake.list_calls if call["status"] != "open"]
    assert closed_calls, "the completed/cancelled windows were never fetched"
    for call in closed_calls:
        assert call["agent"] is None
        assert call["tags"] is None
        assert call["since"] is None
        assert call["resolved_since"] == "2026-04-01"


def test_agent_filter_keeps_a_task_the_agent_only_claims() -> None:
    """Story 22 acceptance: ``?agent=X`` matches a task X merely claims."""
    claimed = _task(
        "claimed",
        created_by="planner",
        claims=(ClaimRecord(agent="agent-zero", aspect="implementation"),),
    )
    created = _task("created", created_by="agent-zero", claims=())
    other = _task("other", created_by="planner", claims=())
    fake = _FrontierFake(
        open_tasks=[claimed, created, other],
        ready=[created, other],
        blocked=[],
    )
    filters = TaskFilters(statuses=("open",), tags=(), agent="agent-zero", since="")

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert _section_ids(data.sections, "in_progress") == ["claimed"]
    assert _section_ids(data.sections, "ready") == ["created"]
    assert data.summary.open_total == 2


def test_project_filter_matches_either_convention_over_the_snapshot() -> None:
    """Story 23: neither convention makes a task invisible to its project view."""
    stamped = _task("stamped", claims=(), metadata={"project": "influx"})
    tagged = _task("tagged", claims=(), tags=("project:influx",))
    elsewhere = _task("elsewhere", claims=(), tags=("project:ganglion",))
    fake = _FrontierFake(
        open_tasks=[stamped, tagged, elsewhere],
        ready=[stamped, tagged, elsewhere],
        blocked=[],
    )
    filters = TaskFilters(
        statuses=("open",), tags=(), agent="", since="", projects=("influx",)
    )

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert _section_ids(data.sections, "ready") == ["stamped", "tagged"]


def test_project_universe_unions_open_and_resolved_rows() -> None:
    """The filter dropdown's universe is the union of both conventions' slugs
    across the loaded snapshot (§5B.1), resolved rows included."""
    stamped = _task("stamped", claims=(), metadata={"project": "influx"})
    tagged = _task("tagged", claims=(), tags=("project:ganglion",))
    done = _task("done", status="completed", tags=("project:cardinal",))
    fake = _FrontierFake(
        open_tasks=[stamped, tagged],
        ready=[stamped, tagged],
        blocked=[],
        completed=[done],
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.projects == ("cardinal", "ganglion", "influx")


def test_project_universe_survives_an_active_project_filter() -> None:
    """Scoping to one project must not collapse the dropdown to that project —
    the universe comes from the unfiltered reads."""
    mine = _task("mine", claims=(), tags=("project:influx",))
    other = _task("other", claims=(), tags=("project:ganglion",))
    done = _task("done", status="completed", tags=("project:cardinal",))
    fake = _FrontierFake(
        open_tasks=[mine, other], ready=[mine, other], blocked=[], completed=[done]
    )
    filters = TaskFilters(
        statuses=("open", "completed"),
        tags=(),
        agent="",
        since="",
        projects=("influx",),
    )

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert _section_ids(data.sections, "ready") == ["mine"]
    assert data.projects == ("cardinal", "ganglion", "influx")


def test_tag_universe_offers_a_tag_only_another_project_carries() -> None:
    """The Tag box's whole point is discovery: the scope of a view is often a
    tag that spans projects, and the universe is built over the loaded rows
    BEFORE the filters narrow — so narrowing to a project that does not carry
    ``milestone:t2`` must not hide it, exactly as with ``projects``."""
    mine = _task("mine", claims=(), tags=("project:influx", "area:docs"))
    other = _task("other", claims=(), tags=("project:ganglion", "milestone:t2"))
    fake = _FrontierFake(open_tasks=[mine, other], ready=[mine, other], blocked=[])

    wide = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))
    assert wide.tags == (
        "area:docs",
        "milestone:t2",
        "project:ganglion",
        "project:influx",
    )

    scoped = asyncio.run(
        load_dashboard(
            fake,
            filters=replace(_FILTERS, projects=("influx",)),
            frontier_limit=500,
        )
    )
    # The board is narrowed to the row that has none of them…
    assert _section_ids(scoped.sections, "ready") == ["mine"]
    # …and the vocabulary you can switch to is not.
    assert scoped.tags == wide.tags


def test_tag_universe_spans_the_resolved_window_and_stops_at_its_edge() -> None:
    """The universe is what this load FETCHED: the open snapshot plus the rows
    the terminal windows returned. A tag carried only by a resolved row inside
    the window is offered; one carried only by a row the window never returned
    was never loaded, so it is not."""
    open_row = _task("open-row", claims=(), tags=("area:docs",))
    inside = replace(
        _task("inside", status="completed", tags=("milestone:t2",)),
        resolved_at="2026-05-10T10:00:00+00:00",
    )
    outside = replace(
        _task("outside", status="completed", tags=("milestone:t1",)),
        resolved_at="2026-04-01T10:00:00+00:00",
    )
    fake = _FrontierFake(
        open_tasks=[open_row],
        ready=[open_row],
        blocked=[],
        completed=[inside, outside],
    )

    data = asyncio.run(
        load_dashboard(
            fake,
            filters=replace(_FILTERS, since="2026-05-01"),
            frontier_limit=500,
        )
    )

    assert data.tags == ("area:docs", "milestone:t2")


def test_tag_universe_is_sorted_and_deduped_across_rows() -> None:
    """One option per tag, in one order — rows sharing a tag must not offer it
    twice, and the datalist renders this tuple verbatim."""
    first = _task("first", claims=(), tags=("needs-human", "area:docs"))
    second = _task("second", claims=(), tags=("area:docs", "needs-human"))
    third = _task("third", claims=(), tags=("area:docs",))
    fake = _FrontierFake(
        open_tasks=[first, second, third],
        ready=[first, second, third],
        blocked=[],
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.tags == ("area:docs", "needs-human")


def test_tag_universe_offers_raw_tag_strings_and_folds_nothing() -> None:
    """Upstream types a tag as a bare string with no pattern and no length, so
    ``honored_tags`` treats whitespace, case and the empty string as ordinary
    content. The box has to offer exactly what it would submit, which means the
    universe folds NOTHING: these are four distinct tags, not one."""
    row = _task("row", claims=(), tags=(" Tag ", "Tag", "tag", ""))
    other = _task("other", claims=(), tags=("tag",))
    fake = _FrontierFake(open_tasks=[row, other], ready=[row, other], blocked=[])

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    # Codepoint order: the empty tag first, then the leading space, then T < t.
    # The empty tag IS a tag (``?tag=`` names it) and is carried like any
    # other; what the BOX does with a blank value is the unchanged
    # ``tag``/``add_tag`` split, not the universe's business.
    assert data.tags == ("", " Tag ", "Tag", "tag")


def test_tag_universe_reads_the_open_row_when_a_terminal_read_echoes_it() -> None:
    """Read skew can return one id in BOTH the open snapshot and a terminal
    window. ``loaded_task_rows`` makes the open snapshot the authority on such
    a row, and the universe sees exactly the rows it returns — so a tag only
    the stale terminal copy carries is not offered as though it were current.
    A universe unioned straight off each response would offer it."""
    fresh = _task("drifting", claims=(), tags=("area:docs",))
    stale = replace(
        _task("drifting", status="completed", tags=("area:retired",)),
        resolved_at="2026-05-10T10:00:00+00:00",
    )
    other = _task("other", claims=(), tags=("needs-human",))
    fake = _FrontierFake(
        open_tasks=[fresh, other],
        ready=[fresh, other],
        blocked=[],
        completed=[stale],
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.tags == ("area:docs", "needs-human")


def test_tag_universe_survives_a_terminal_read_that_failed() -> None:
    """A window that did not answer contributes no rows — it must not empty the
    universe. The tags on the rows that DID load are still perfectly good
    answers, and the failure is reported by the error banner instead; a filter
    bar that goes blank because one read fell over is the worse board."""
    open_row = _task("open-row", claims=(), tags=("area:docs",))
    cancelled = replace(
        _task("gone", status="cancelled", tags=("needs-human",)),
        resolved_at="2026-05-10T10:00:00+00:00",
    )
    fake = _FrontierFake(
        open_tasks=[open_row],
        ready=[open_row],
        blocked=[],
        completed=[],
        cancelled=[cancelled],
        fail_completed_from=0,
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.errors
    assert data.tags == ("area:docs", "needs-human")


# The WHOLE read budget of an unfiltered board with no epics and no gates:
# three ``lithos_task_list`` windows, both frontiers, the stats card and the
# agent picker's one registration read. Asserted as a multiset over the fake's
# own call log rather than per-tool, so a read nobody thought to instrument
# still fails the test.
_DASHBOARD_READ_BUDGET = sorted(
    [
        "lithos_task_list",
        "lithos_task_list",
        "lithos_task_list",
        "lithos_task_ready",
        "lithos_task_blocked",
        "lithos_stats",
        "lithos_agent_list",
    ]
)


def test_tag_universe_costs_no_extra_lithos_read() -> None:
    """It is folded out of rows the board already holds. The same snapshot with
    and without tags therefore makes an identical call log — AND that log is
    the fixed budget above, so a corpus-wide ``lithos_tags`` read or a per-tag
    fan-out fails here even though it would leave the two logs equal."""

    def _fake(tags: tuple[str, ...]) -> _FrontierFake:
        rows = [_task("a", claims=(), tags=tags), _task("b", claims=(), tags=tags)]
        done = _task("done", status="completed", tags=tags)
        return _FrontierFake(open_tasks=rows, ready=rows, blocked=[], completed=[done])

    tagged, bare = _fake(("milestone:t2", "needs-human")), _fake(())
    with_tags = asyncio.run(
        load_dashboard(tagged, filters=_FILTERS, frontier_limit=500)
    )
    without_tags = asyncio.run(
        load_dashboard(bare, filters=_FILTERS, frontier_limit=500)
    )

    assert with_tags.tags == ("milestone:t2", "needs-human")
    assert without_tags.tags == ()
    assert sorted(tagged.calls) == sorted(bare.calls) == _DASHBOARD_READ_BUDGET
    # The per-tool ARGUMENTS are identical too: nothing about a tag changed
    # what any of those reads asked for either.
    assert tagged.list_calls == bare.list_calls
    assert tagged.ready_args == bare.ready_args
    assert tagged.blocked_args == bare.blocked_args


def test_disagreeing_project_conventions_warn_to_telemetry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§5B.1: when both conventions are present and disagree, neither value is
    dropped (the task matches under both slugs) but the conflict is reported."""
    conflicted = _task(
        "conflicted",
        claims=(),
        tags=("project:tagged",),
        metadata={"project": "stamped"},
    )
    agreeing = _task(
        "agreeing", claims=(), tags=("project:same",), metadata={"project": "same"}
    )
    fake = _FrontierFake(
        open_tasks=[conflicted, agreeing], ready=[conflicted, agreeing], blocked=[]
    )

    with caplog.at_level("WARNING", logger="lithos_lens.frontier"):
        data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "lens_event", "") == "lens.tasks.project_convention_conflict"
    ]
    # The structured extras the JSON formatter emits (see JsonFormatter).
    assert record.__dict__["conflicting_task_ids"] == ["conflicted"]
    assert record.__dict__["conflict_count"] == 1
    # Both slugs still select the task, and both reach the filter dropdown.
    assert data.projects == ("same", "stamped", "tagged")
    for slug in ("stamped", "tagged"):
        scoped = TaskFilters(
            statuses=("open",), tags=(), agent="", since="", projects=(slug,)
        )
        scoped_data = asyncio.run(
            load_dashboard(fake, filters=scoped, frontier_limit=500)
        )
        assert _section_ids(scoped_data.sections, "ready") == ["conflicted"]


def test_single_convention_posture_still_warns_about_a_conflict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§5B.1 makes the conflict warning a property of the DATA: a task carrying
    two disagreeing conventions is reported whatever posture Lens matches
    under. The posture narrows matching only — both values are read either
    way."""
    conflicted = _task(
        "conflicted",
        claims=(),
        tags=("project:tagged",),
        metadata={"project": "stamped"},
    )
    fake = _FrontierFake(open_tasks=[conflicted], ready=[conflicted], blocked=[])
    filters = TaskFilters(
        statuses=("open",),
        tags=(),
        agent="",
        since="",
        project_convention="metadata",
    )

    with caplog.at_level("WARNING", logger="lithos_lens.frontier"):
        data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "lens_event", "") == "lens.tasks.project_convention_conflict"
    ]
    assert record.__dict__["conflicting_task_ids"] == ["conflicted"]
    # The posture narrows MATCHING, not the universe: §5B.1 keeps the dropdown
    # the union of both conventions' slugs so no project is invisible.
    assert data.projects == ("stamped", "tagged")


def test_malformed_metadata_project_is_reported_and_never_fabricates_a_slug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-string metadata.project cannot be read as a project: it must not
    reach the dropdown as a coerced ``['influx']``, must not fake a convention
    conflict, and must not vanish silently either."""
    malformed = _task(
        "malformed",
        claims=(),
        tags=("project:real",),
        metadata={"project": ["real"]},
    )
    fake = _FrontierFake(open_tasks=[malformed], ready=[malformed], blocked=[])

    with caplog.at_level("WARNING", logger="lithos_lens.frontier"):
        data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.projects == ("real",)
    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "lens_event", "") == "lens.tasks.project_metadata_invalid"
    ]
    assert record.__dict__["invalid_task_ids"] == ["malformed"]
    assert record.__dict__["invalid_count"] == 1
    # The tag is the only readable convention, so there is nothing to reconcile.
    assert not [
        r
        for r in caplog.records
        if getattr(r, "lens_event", "") == "lens.tasks.project_convention_conflict"
    ]


def test_project_universe_unions_both_conventions_under_a_single_posture() -> None:
    """§5B.1: the universe is the union of both conventions' slugs whatever the
    posture — a tag-only project must not vanish from the dropdown just because
    matching honours ``metadata``."""
    stamped = _task("stamped", claims=(), metadata={"project": "influx"})
    tagged = _task("tagged", claims=(), tags=("project:ganglion",))
    fake = _FrontierFake(
        open_tasks=[stamped, tagged], ready=[stamped, tagged], blocked=[]
    )
    filters = TaskFilters(
        statuses=("open",),
        tags=(),
        agent="",
        since="",
        project_convention="metadata",
    )

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert data.projects == ("ganglion", "influx")
    # Matching still honours the posture: the tag-only row is out of scope.
    filters = replace(filters, projects=("ganglion",))
    scoped = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))
    assert _section_ids(scoped.sections, "ready") == []


def test_resolved_rows_are_fetched_with_claims_only_for_the_agent_match() -> None:
    """Claims are omitted unless requested, so the completed/cancelled windows
    must ask for them whenever the agent filter is active — otherwise every
    resolved row is claims-unknown and the creator-OR-claimer match silently
    degrades to creator-only. Nothing else reads them (resolved rows render no
    claim chips), so the unfiltered dashboard keeps the cheaper read."""

    def closed_calls(fake: _FrontierFake) -> list[dict[str, Any]]:
        return [call for call in fake.list_calls if call["status"] != "open"]

    scoped = _FrontierFake(open_tasks=[], ready=[], blocked=[])
    asyncio.run(
        load_dashboard(
            scoped,
            filters=TaskFilters(
                statuses=("open", "completed", "cancelled"),
                tags=(),
                agent="agent-zero",
                since="",
            ),
            frontier_limit=500,
        )
    )
    unscoped = _FrontierFake(open_tasks=[], ready=[], blocked=[])
    asyncio.run(load_dashboard(unscoped, filters=_FILTERS, frontier_limit=500))

    assert closed_calls(scoped)
    assert all(call["with_claims"] is True for call in closed_calls(scoped))
    assert closed_calls(unscoped)
    assert all(call["with_claims"] is False for call in closed_calls(unscoped))


def test_agent_filter_keeps_a_resolved_task_the_agent_only_claimed() -> None:
    """Story 22 across the whole dashboard: a completed task someone else
    created, still claimed by the selected agent, stays visible."""
    done = _task(
        "done",
        status="completed",
        created_by="planner",
        claims=(ClaimRecord(agent="agent-zero", aspect="review"),),
    )
    other = _task("other-done", status="completed", created_by="planner", claims=())
    fake = _FrontierFake(open_tasks=[], ready=[], blocked=[], completed=[done, other])
    filters = TaskFilters(
        statuses=("open", "completed"), tags=(), agent="agent-zero", since=""
    )

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert _section_ids(data.sections, "completed") == ["done"]
    assert data.summary.recent_completed == 1


def test_conflict_on_a_resolved_row_warns_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A convention conflict is a property of the task, not of its status: a
    recently resolved row with disagreeing conventions is reported like any
    other, and each id is counted once per load."""
    done = _task(
        "done",
        status="completed",
        tags=("project:tagged",),
        metadata={"project": "stamped"},
    )
    fake = _FrontierFake(open_tasks=[], ready=[], blocked=[], completed=[done])

    with caplog.at_level("WARNING", logger="lithos_lens.frontier"):
        data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    (record,) = [
        r
        for r in caplog.records
        if getattr(r, "lens_event", "") == "lens.tasks.project_convention_conflict"
    ]
    assert record.__dict__["conflicting_task_ids"] == ["done"]
    assert record.__dict__["conflict_count"] == 1
    assert data.projects == ("stamped", "tagged")


def test_a_task_in_both_the_open_and_terminal_reads_is_reported_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Read skew can return the same id twice; the conflict report dedups it."""
    conflicted = _task(
        "x", claims=(), tags=("project:tagged",), metadata={"project": "stamped"}
    )
    terminal = replace(conflicted, status="completed")
    fake = _FrontierFake(
        open_tasks=[conflicted],
        ready=[conflicted],
        blocked=[],
        completed=[terminal],
    )

    with caplog.at_level("WARNING", logger="lithos_lens.frontier"):
        asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    records = [
        r
        for r in caplog.records
        if getattr(r, "lens_event", "") == "lens.tasks.project_convention_conflict"
    ]
    # One warning per load, and the duplicated id counted once.
    assert [r.__dict__["conflict_count"] for r in records] == [1]
    assert records[0].__dict__["conflicting_task_ids"] == ["x"]


# --- T1 slice 12: empty/degraded states -------------------------------------


def test_a_server_without_the_frontier_tools_renders_flat_as_a_failed_read() -> None:
    """Acceptance for the withdrawn version fallback (2026-08-24).

    A Lithos that does not serve ``lithos_task_ready`` / ``lithos_task_blocked``
    fails those calls, and that is all Lens concludes: the board renders the
    flat open list with the read-error banner, exactly as for an outage. No
    ``tools/list`` probe, no version diagnosis, no cached verdict — the notice
    was the only thing detection ever bought, for a server that has never been
    on the other end of this client.
    """
    claimed = _task("c", claims=(ClaimRecord(agent="a", aspect="impl"),))
    unclaimed = _task("u", claims=())
    fake = _FrontierFake(
        open_tasks=[claimed, unclaimed],
        ready=[],
        blocked=[],
        ready_error=LithosToolError(
            "Unknown tool: lithos_task_ready", code="tool_error"
        ),
        blocked_error=LithosToolError(
            "Unknown tool: lithos_task_blocked", code="tool_error"
        ),
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.open_flat is True
    assert _section_ids(data.sections, "open") == ["c", "u"]
    assert _section_ids(data.sections, "in_progress") == []
    assert _section_ids(data.sections, "ready") == []
    assert _section_ids(data.sections, "blocked") == []
    assert _section_ids(data.sections, "unclassified") == []
    assert data.summary.open_total == 2
    # Reported as the failed reads they are, on every render.
    assert any("ready frontier" in message for message in data.errors)
    assert any("blocked frontier" in message for message in data.errors)
    assert data.healthy is False
    # There is no frontier left to truncate or reconcile, and nothing to retry.
    assert data.truncated is False
    assert data.reconciliation_pending is False
    assert fake.ready_calls == 1
    # Claims still render — they come from the master open list.
    assert data.sections["open"][0].claims[0].agent == "a"
    assert data.sections["open"][1].claim_state == "known_unclaimed"


def test_flat_fallback_keeps_the_claims_unknown_contract() -> None:
    """A server that cannot answer the frontier may also ignore
    ``with_claims``; a row whose claims came back None must still read
    "claims unknown", never a confident "unclaimed"."""
    fake = _FrontierFake(
        open_tasks=[_task("u", claims=None)],
        ready=[],
        blocked=[],
        ready_error=RuntimeError("frontier unavailable"),
        blocked_error=RuntimeError("frontier unavailable"),
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    (row,) = data.sections["open"]
    assert row.claim_state == "unknown"


def test_frontier_outage_renders_flat_with_the_read_error() -> None:
    """A transient outage renders the master open list FLAT (§14) and names the
    read that failed: half a frontier is not a classification, and the operator
    needs to know which call did not answer."""
    fake = _FrontierFake(
        open_tasks=[_task("r", claims=())],
        ready=[],
        blocked=[],
        ready_error=RuntimeError("connection reset"),
        blocked_error=RuntimeError("connection reset"),
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.open_flat is True
    assert _section_ids(data.sections, "open") == ["r"]
    assert _section_ids(data.sections, "unclassified") == []
    assert any("ready frontier" in message for message in data.errors)
    assert data.healthy is False


def test_a_failed_skew_retry_is_reported_not_swallowed() -> None:
    """Regression: a retry that fails must reach the error channel.

    The retry keeps the first generation when it cannot re-read (a mixed
    generation would be worse), and that part is deliberate. What was missing
    is the report: a retry triggered by TERMINAL overlap alone leaves
    ``reconciliation_pending`` False, so with no error line the board rendered
    the affirmative "All systems healthy" stripe over a task showing in both an
    open section and a terminal one.
    """
    dupe = _task("t", claims=())
    completed = replace(dupe, status="completed")
    fake = _FrontierFake(
        open_tasks=[dupe],
        ready=[dupe],
        blocked=[],
        completed=[completed],
        fail_ready_from=1,
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    # The retry was attempted and failed; the first generation still renders.
    assert fake.ready_calls == 2
    assert _section_ids(data.sections, "ready") == ["t"]
    # No frontier disagreement, so this error line is the ONLY signal there is.
    assert data.reconciliation_pending is False
    assert RETRY_FAILED_ERROR in data.errors
    assert data.healthy is False


def test_empty_corpus_is_flagged_when_lithos_returns_nothing() -> None:
    """All reads succeeded and Lithos has nothing at all: the board says "no
    tasks yet" rather than the per-section "nothing matched these filters"."""
    fake = _FrontierFake(open_tasks=[], ready=[], blocked=[])

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.nothing_to_show is True
    assert data.open_total == 0
    assert data.healthy is True


def test_filters_hiding_every_row_is_not_an_empty_corpus() -> None:
    """nothing_to_show is measured on the RAW responses: a filter that hides
    every row leaves the corpus non-empty, so the operator is told their
    filters matched nothing instead of that Lithos is empty."""
    fake = _FrontierFake(
        open_tasks=[_task("r", claims=(), tags=("project:a",))],
        ready=[_task("r", claims=(), tags=("project:a",))],
        blocked=[],
    )
    filters = TaskFilters(statuses=("open",), tags=("project:b",), agent="", since="")

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert data.nothing_to_show is False
    assert data.open_total == 0


@pytest.mark.parametrize(
    "filters",
    [
        TaskFilters(statuses=TASK_STATUSES, tags=("project:b",), agent="", since=""),
        TaskFilters(statuses=TASK_STATUSES, tags=(), agent="someone-else", since=""),
    ],
)
def test_terminal_only_corpus_hidden_by_a_filter_is_not_empty(
    filters: TaskFilters,
) -> None:
    """Regression (correctness f-001): the terminal reads push agent/tags
    UPSTREAM, so when every existing task is completed/cancelled a filter that
    excludes them empties every response. That is a filter result, not an empty
    corpus, and must not render as "no tasks yet"."""
    done = TaskRecord(
        id="d",
        title="Done",
        status="completed",
        task_type="task",
        created_by="someone",
        tags=("project:a",),
    )
    fake = _FrontierFake(open_tasks=[], ready=[], blocked=[], completed=[done])

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    # The filtered reads came back empty…
    assert _section_ids(data.sections, "completed") == []
    # …but the corpus is not known to be empty, so the panel stays away.
    assert data.nothing_to_show is False


def test_terminal_rows_alone_are_not_an_empty_corpus() -> None:
    """Open is empty but something resolved in the window — there IS work to
    show, so the empty-corpus panel must not claim otherwise."""
    done = TaskRecord(id="d", title="Done", status="completed", task_type="task")
    fake = _FrontierFake(open_tasks=[], ready=[], blocked=[], completed=[done])

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.nothing_to_show is False
    assert _section_ids(data.sections, "completed") == ["d"]


def test_failed_read_is_never_reported_as_an_empty_corpus() -> None:
    """An outage empties the open snapshot too; "no tasks yet" would be a lie,
    so any recorded error rules the empty-corpus panel out."""
    fake = _FrontierFake(open_tasks=[], ready=[], blocked=[], fail_ready=True)

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.errors
    assert data.nothing_to_show is False


def test_healthy_is_false_while_a_degraded_signal_is_live() -> None:
    """The healthy stripe is the claim "nothing is wrong": truncation,
    reconciliation, failed reads, and unknown claims each falsify it."""
    r1 = _task("r1", claims=())
    r2 = _task("r2", claims=())
    truncated = asyncio.run(
        load_dashboard(
            _FrontierFake(open_tasks=[r1, r2], ready=[r1, r2], blocked=[]),
            filters=_FILTERS,
            frontier_limit=1,
        )
    )
    assert truncated.truncated is True
    assert truncated.healthy is False

    unknown_claims = asyncio.run(
        load_dashboard(
            _FrontierFake(open_tasks=[_task("u", claims=None)], ready=[], blocked=[]),
            filters=_FILTERS,
            frontier_limit=500,
        )
    )
    assert unknown_claims.sections["claims_unknown"]
    assert unknown_claims.healthy is False

    healthy = asyncio.run(
        load_dashboard(
            _FrontierFake(open_tasks=[r1], ready=[r1], blocked=[]),
            filters=_FILTERS,
            frontier_limit=500,
        )
    )
    assert healthy.healthy is True


@pytest.mark.parametrize(
    "filters",
    [
        TaskFilters(statuses=TASK_STATUSES, tags=("project:nope",), agent="", since=""),
        TaskFilters(statuses=TASK_STATUSES, tags=(), agent="nobody", since=""),
        TaskFilters(statuses=("completed",), tags=(), agent="", since=""),
    ],
)
def test_healthy_is_withheld_on_a_narrowed_board(filters: TaskFilters) -> None:
    """Regression (security f-002): truncation, reconciliation and
    claims-unknown are all measured over the rows the filters left, so on a
    narrowed board they cannot support the stripe's system-wide claim. The
    degraded signal here (claims never returned) is real but filtered out of
    view — the stripe must not turn that into "all systems healthy"."""
    fake = _FrontierFake(
        open_tasks=[_task("u", claims=None, tags=("project:a",))],
        ready=[],
        blocked=[],
    )

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert data.filters_narrowed is True
    assert data.healthy is False


# --- epic rollup strip (T1-S5) --------------------------------------------


def _epic(task_id: str = "epic-1") -> TaskRecord:
    return _task(task_id, task_type="epic")


def _subtree(done: int, open_: int) -> list[TaskRecord]:
    return [_task(f"d{n}", status="completed") for n in range(done)] + [
        _task(f"o{n}") for n in range(open_)
    ]


def test_build_epic_rollup_reports_completed_over_subtree_size() -> None:
    """Slice-5 acceptance (data half): an epic with 5 of 8 subtree tasks
    completed rolls up to 5/8."""
    rollup = build_epic_rollup(_epic(), _subtree(done=5, open_=3))

    assert (rollup.done, rollup.total) == (5, 8)
    assert rollup.progress_label == "5/8"
    assert rollup.percent == 62
    assert rollup.selected is False


def test_build_epic_rollup_counts_only_workable_descendants() -> None:
    """Nested epics and gates are structure, not units of work: they never
    enter the counts (a sub-epic would double-count its own children) but they
    DO stay in the scope set — they are part of the initiative."""
    children = [
        _task("t1", status="completed"),
        _task("t2"),
        _task("sub-epic", task_type="epic"),
        _task("gate-1", task_type="gate"),
    ]
    rollup = build_epic_rollup(_epic(), children)

    assert rollup.progress_label == "1/2"
    assert rollup.descendant_ids == {"t1", "t2", "sub-epic", "gate-1"}


def test_build_epic_rollup_drops_cancelled_work_from_the_denominator() -> None:
    """Cancelled descendants can never complete, so keeping them in the
    denominator would pin the chip below 100% forever. They are counted
    separately instead of vanishing."""
    children = [
        _task("t1", status="completed"),
        _task("t2", status="completed"),
        _task("t3", status="cancelled"),
    ]
    rollup = build_epic_rollup(_epic(), children)

    assert rollup.progress_label == "2/2"
    assert rollup.percent == 100
    assert rollup.cancelled == 1


def test_build_epic_rollup_handles_a_childless_epic() -> None:
    rollup = build_epic_rollup(_epic(), [])
    assert rollup.progress_label == "0/0"
    assert rollup.percent == 0
    assert rollup.descendant_ids == frozenset()


def test_load_dashboard_builds_one_chip_per_open_epic() -> None:
    epic = _epic("epic-1")
    other = _epic("epic-2")
    ready = _task("r", claims=())
    fake = _FrontierFake(
        open_tasks=[epic, other, ready],
        ready=[ready],
        blocked=[],
        children={
            "epic-1": _subtree(done=5, open_=3),
            "epic-2": [_task("x", status="completed")],
        },
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert [rollup.progress_label for rollup in data.epics] == ["5/8", "1/1"]
    # One recursive, closed-inclusive call per epic — and none for plain tasks.
    assert fake.children_calls == [
        {"task_id": "epic-1", "recursive": True, "include_closed": True},
        {"task_id": "epic-2", "recursive": True, "include_closed": True},
    ]
    # The epics themselves never enter a section; the workable task still does.
    assert _section_ids(data.sections, "ready") == ["r"]
    assert data.epic_scope == ""
    assert not any(rollup.selected for rollup in data.epics)


def test_load_dashboard_scopes_every_section_to_the_selected_epic() -> None:
    """Slice-5 acceptance (view half): ``?epic=`` scopes the sections to that
    epic's descendants — in-scope rows keep their classification, everything
    else disappears from the board and from the counts."""
    epic = _epic("epic-1")
    inside_ready = _task("in-ready", claims=())
    inside_done = _task("in-done", status="completed")
    outside = _task("outside", claims=())
    fake = _FrontierFake(
        open_tasks=[epic, inside_ready, outside],
        ready=[inside_ready, outside],
        blocked=[],
        completed=[inside_done, _task("outside-done", status="completed")],
        children={"epic-1": [inside_ready, inside_done]},
    )
    filters = replace(_FILTERS, epic="epic-1")
    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert _section_ids(data.sections, "ready") == ["in-ready"]
    assert _section_ids(data.sections, "completed") == ["in-done"]
    assert data.summary.ready == 1
    assert data.summary.open_total == 1
    assert data.summary.recent_completed == 1
    # The strip still lists every epic (so the operator can switch scope) and
    # marks the active one.
    assert data.epic_scope == "epic-1"
    assert [rollup.selected for rollup in data.epics] == [True]


def test_epic_rollup_counts_ignore_the_section_filters() -> None:
    """Rollup counts are whole-subtree facts: a tag filter that hides most of
    the descendants from the sections must not change the chip. (The chip
    itself is kept by the one descendant that IS on the board — see the
    scoping tests below.)"""
    epic = _epic("epic-1")
    tagged = _task("in-ready", claims=(), tags=("project:mine",))
    fake = _FrontierFake(
        open_tasks=[epic, tagged],
        ready=[tagged],
        blocked=[],
        children={"epic-1": [tagged, *_subtree(done=5, open_=3)]},
    )
    filters = TaskFilters(
        statuses=("open",), tags=("project:mine",), agent="", since=""
    )
    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert _section_ids(data.sections, "ready") == ["in-ready"]
    # The whole subtree (one open row on the board plus the eight it hides),
    # NOT the 1/1 the filtered sections show.
    assert [rollup.progress_label for rollup in data.epics] == ["5/9"]


# --- the strip follows the board's filters (§5.2.1) ------------------------


def _tagged(task_id: str, *tags: str) -> TaskRecord:
    return _task(task_id, claims=(), tags=tags)


def test_the_strip_lists_only_epics_with_work_on_the_filtered_board() -> None:
    """Regression: the strip was built from the unfiltered open snapshot, so a
    filtered board drew a chip for every open epic in the corpus — and each
    chip links to the ACTIVE filters plus ``?epic=``, which for an off-filter
    epic can only produce an empty board. Chips now describe the board the
    operator is looking at, and the ones dropped are counted out loud."""
    on_board = _epic("epic-on")
    off_board = _epic("epic-off")
    inside = _tagged("inside", "roadmap")
    elsewhere = _tagged("elsewhere", "other")
    fake = _FrontierFake(
        open_tasks=[on_board, off_board, inside, elsewhere],
        ready=[inside, elsewhere],
        blocked=[],
        children={"epic-on": [inside], "epic-off": [elsewhere]},
    )
    filters = replace(_FILTERS, tags=("roadmap",))

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert [rollup.task.id for rollup in data.epics] == ["epic-on"]
    assert data.epics_hidden == 1
    # …and the unfiltered board is unchanged: every open epic still gets a chip.
    whole = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))
    assert [rollup.task.id for rollup in whole.epics] == ["epic-on", "epic-off"]
    assert whole.epics_hidden == 0


def test_the_strip_matches_on_descendants_not_the_epics_own_tags() -> None:
    """The scoping rule §5.2.1 states, in the one case where the two readings
    differ. An epic carrying the filtered tag whose children do not is exactly
    the dead-end chip (its scope lands on nothing); a cross-project epic with a
    child in the filter is exactly the chip an operator wants."""
    labelled = _task("epic-labelled", task_type="epic", tags=("roadmap",))
    cross = _task("epic-cross", task_type="epic", tags=("other",))
    off_tag_child = _tagged("child-off", "other")
    on_tag_child = _tagged("child-on", "roadmap")
    fake = _FrontierFake(
        open_tasks=[labelled, cross, off_tag_child, on_tag_child],
        ready=[off_tag_child, on_tag_child],
        blocked=[],
        children={"epic-labelled": [off_tag_child], "epic-cross": [on_tag_child]},
    )
    filters = replace(_FILTERS, tags=("roadmap",))

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert [rollup.task.id for rollup in data.epics] == ["epic-cross"]
    assert data.epics_hidden == 1


def test_a_chip_the_filters_left_on_the_strip_leads_to_a_non_empty_board() -> None:
    """The acceptance criterion end to end: follow every chip the filtered
    board draws and none of them lands on an empty board."""
    epics = [_epic(f"epic-{n}") for n in range(4)]
    rows = [_tagged(f"row-{n}", "roadmap" if n < 2 else "other") for n in range(4)]
    fake = _FrontierFake(
        open_tasks=[*epics, *rows],
        ready=rows,
        blocked=[],
        children={f"epic-{n}": [rows[n]] for n in range(4)},
    )
    filters = replace(_FILTERS, tags=("roadmap",))

    strip = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert [rollup.task.id for rollup in strip.epics] == ["epic-0", "epic-1"]
    for rollup in strip.epics:
        followed = asyncio.run(
            load_dashboard(
                fake,
                filters=replace(filters, epic=rollup.task.id),
                frontier_limit=500,
            )
        )
        assert followed.epic_scope == rollup.task.id
        assert any(followed.sections.values()), rollup.task.id


def test_the_strip_does_not_shrink_as_the_operator_moves_between_epics() -> None:
    """The strip is scoped by the OTHER filters, never by ``?epic=`` itself —
    selecting one epic must not erase the chips that lead out of it."""
    epics = [_epic("epic-a"), _epic("epic-b")]
    a_row = _tagged("a-row", "roadmap")
    b_row = _tagged("b-row", "roadmap")
    fake = _FrontierFake(
        open_tasks=[*epics, a_row, b_row],
        ready=[a_row, b_row],
        blocked=[],
        children={"epic-a": [a_row], "epic-b": [b_row]},
    )
    filters = replace(_FILTERS, tags=("roadmap",), epic="epic-a")

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert [rollup.task.id for rollup in data.epics] == ["epic-a", "epic-b"]
    assert [rollup.selected for rollup in data.epics] == [True, False]
    assert _section_ids(data.sections, "ready") == ["a-row"]


def test_a_terminal_only_board_keeps_the_epics_whose_resolved_work_it_shows() -> None:
    """ "On this board" is the statuses actually rendered, not "open": under
    ``?status=completed`` a chip earns its place from the resolved rows, and an
    epic whose only matching work is open leads nowhere there."""
    resolved_epic = _epic("epic-resolved")
    open_epic = _epic("epic-open")
    done = _task("done", status="completed", tags=("roadmap",))
    still_open = _tagged("still-open", "roadmap")
    fake = _FrontierFake(
        open_tasks=[resolved_epic, open_epic, still_open],
        ready=[still_open],
        blocked=[],
        completed=[done],
        children={"epic-resolved": [done], "epic-open": [still_open]},
    )
    filters = TaskFilters(
        statuses=("completed",), tags=("roadmap",), agent="", since=""
    )

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert [rollup.task.id for rollup in data.epics] == ["epic-resolved"]
    assert data.epics_hidden == 1


def test_a_chip_resting_only_on_a_rolled_up_descendant_is_not_drawn() -> None:
    """Reviewer repro (c-001): "a descendant among the rows this board renders"
    means a row that can be PLACED. A nested epic matches the filters but rolls
    up into the strip rather than rendering, so an outer epic whose only match
    is that nested epic is the same dead-end chip — and the nested epic, being
    childless, has no work on this board either."""
    outer = _epic("epic-outer")
    nested = _task("epic-nested", task_type="epic", tags=("roadmap",))
    working = _epic("epic-working")
    row = _tagged("row", "roadmap")
    fake = _FrontierFake(
        open_tasks=[outer, nested, working, row],
        ready=[row],
        blocked=[],
        children={"epic-outer": [nested], "epic-working": [row]},
    )
    filters = replace(_FILTERS, tags=("roadmap",))

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert [rollup.task.id for rollup in data.epics] == ["epic-working"]
    assert data.epics_hidden == 2


def test_a_scope_holding_only_a_rolled_up_row_says_which_gap_it_is() -> None:
    """The other half of c-001: a bookmark can still select that outer epic.
    Its chip is kept and no section renders — but the nested epic DID survive
    the filters, so "none of it survives the other filters" would be false.
    The two gaps are told apart: this one is the placement rule, not the
    filters, and widening them would find nothing."""
    outer = _epic("epic-outer")
    nested = _task("epic-nested", task_type="epic", tags=("roadmap",))
    fake = _FrontierFake(
        open_tasks=[outer, nested],
        ready=[],
        blocked=[],
        children={"epic-outer": [nested]},
    )
    filters = replace(_FILTERS, tags=("roadmap",), epic="epic-outer")

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert data.epic_scope == "epic-outer"
    assert not any(data.sections.values())
    assert data.epic_scope_blank is True
    assert data.epic_scope_rolled_up is True
    assert data.epic_scope_unmatched is False
    # The nested epic is still counted as a row this board withheld — it is
    # the subject of the explanation.
    assert data.rolled_up_open == 1


def test_an_unplaceable_row_is_never_dropped_from_the_rolled_up_count() -> None:
    """Reviewer repro (c-003): ``TaskRecord.task_type`` keeps unknown future
    strings, and the classifier cannot place one. Such a row has no chip and no
    section, so if it also left the rolled-up count the board would render
    empty under "All systems healthy" while holding it."""
    future = _task("future-1", task_type="future_type")
    fake = _FrontierFake(open_tasks=[future], ready=[], blocked=[])

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.epics == ()
    assert data.rolled_up_open == 1
    assert data.rolled_up_only is True
    assert data.healthy is False


def test_an_unrelated_read_failure_does_not_silence_the_epic_explanation() -> None:
    """Reviewer repro (c-002, narrowed): the reads that decide row membership
    all answered — this epic demonstrably has nothing under it on this board —
    so a failed stats read must not take the explanation away. Only a window
    the board DISPLAYS can make membership unknown."""
    epic = _epic("epic-1")
    inside = _tagged("inside", "other")
    fake = _FrontierFake(
        open_tasks=[epic, inside],
        ready=[inside],
        blocked=[],
        children={"epic-1": [inside]},
        fail_stats=True,
    )
    filters = replace(_FILTERS, tags=("roadmap",), epic="epic-1")

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert any("stats" in message for message in data.errors)
    assert data.rows_incomplete is False
    assert data.epic_scope_unmatched is True


def test_a_failed_terminal_read_leaves_the_strip_unscoped() -> None:
    """Reviewer repro (c-002): "no tasks on this board" is a claim about the
    FILTERS. With a displayed window that never answered, the rows are unknown
    rather than absent — the matching one may be in the read that failed — so
    the strip scopes nothing, hides nothing, and the load-error banner explains
    the board."""
    epic = _epic("epic-1")
    done = _task("done", status="completed", tags=("roadmap",))
    fake = _FrontierFake(
        open_tasks=[epic],
        ready=[],
        blocked=[],
        completed=[done],
        children={"epic-1": [done]},
        fail_completed_from=0,
    )
    filters = TaskFilters(
        statuses=("completed",), tags=("roadmap",), agent="", since=""
    )

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert any("completed" in message for message in data.errors)
    assert [rollup.task.id for rollup in data.epics] == ["epic-1"]
    assert data.epics_hidden == 0
    # Recorded per status, not as one flag: the window that did not answer is
    # the one whose empty section must say so (the others answered).
    assert data.unread_statuses == frozenset({"completed"})
    assert data.rows_incomplete is True

    # …and the same board with that epic selected does not turn the outage
    # into "nothing here matches your filters" either.
    scoped = asyncio.run(
        load_dashboard(
            fake, filters=replace(filters, epic="epic-1"), frontier_limit=500
        )
    )
    assert scoped.epic_scope == "epic-1"
    assert not any(scoped.sections.values())
    assert scoped.epic_scope_unmatched is False


def test_a_failed_read_the_board_does_not_display_changes_nothing() -> None:
    """The complement of that contract: only a window this board SHOWS can make
    its rows unknown. On an open-only board the completed window is not on
    screen, so its outage must not scope the strip back to the whole corpus,
    must not swallow the count of chips the filters dropped, and must not take
    away an explanation the reads that DID answer fully support."""
    selected = _epic("epic-1")
    partner = _epic("epic-2")
    stranger = _epic("epic-3")
    inside = _tagged("inside", "other")
    on_tag = _tagged("on-tag", "roadmap")
    off_tag = _tagged("off-tag", "other")
    fake = _FrontierFake(
        open_tasks=[selected, partner, stranger, inside, on_tag, off_tag],
        ready=[inside, on_tag, off_tag],
        blocked=[],
        children={"epic-1": [inside], "epic-2": [on_tag], "epic-3": [off_tag]},
        fail_completed_from=0,
    )
    filters = TaskFilters(
        statuses=("open",), tags=("roadmap",), agent="", since="", epic="epic-1"
    )

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    # The outage is still reported — it is simply not this board's rows.
    assert any("completed" in message for message in data.errors)
    assert data.unread_statuses == frozenset()
    assert data.rows_incomplete is False
    # So every filter-derived statement stands: the strip is still scoped (the
    # off-tag epic dropped, and counted)…
    assert [rollup.task.id for rollup in data.epics] == ["epic-1", "epic-2"]
    assert data.epics_hidden == 1
    # …and the selected epic still gets the explanation its own (successful)
    # reads support.
    assert data.epic_scope == "epic-1"
    assert not any(data.sections.values())
    assert data.epic_scope_unmatched is True


def test_the_strip_follows_the_generation_the_skew_retry_adopted() -> None:
    """The retry path carries the scope, not just the fan-out: the adopted
    generation changes WHICH epic has work on the board, so chips computed
    against the first generation would leave an off-filter chip behind."""
    stale_epic = _epic("epic-stale")
    fresh_epic = _epic("epic-fresh")
    stale_row = _tagged("stale-row", "roadmap")
    fresh_row = _tagged("fresh-row", "roadmap")
    gap = _tagged("gap", "roadmap")
    fake = _FrontierFake(
        # First generation: the stale epic's row is on the board, and ``gap``
        # is in neither frontier (the skew trigger). The retry replaces both
        # the epics and the rows.
        open_tasks=[
            [stale_epic, fresh_epic, stale_row, gap],
            [stale_epic, fresh_epic, fresh_row],
        ],
        ready=[[stale_row], [fresh_row]],
        blocked=[],
        children={"epic-stale": [stale_row], "epic-fresh": [fresh_row]},
    )
    filters = replace(_FILTERS, tags=("roadmap",))

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert fake.open_calls == 2
    # The stale epic's only row is gone from the adopted snapshot, so its chip
    # goes with it — and the fresh epic's row arrived, so its chip is drawn.
    assert [rollup.task.id for rollup in data.epics] == ["epic-fresh"]
    assert data.epics_hidden == 1
    assert _section_ids(data.sections, "ready") == ["fresh-row"]


def test_hidden_chips_accumulate_across_every_fan_out_batch() -> None:
    """The corpus that motivated the rule is many batches deep, and the hidden
    count is summed one ``EPIC_FANOUT_BATCH`` at a time — so it has to survive
    the boundary. Kept and hidden epics sit on both sides of both boundaries,
    and the note beside the strip reports the whole corpus, not the last
    batch."""
    count = EPIC_FANOUT_BATCH * 2 + 4
    kept = (0, 7, 8, 15, 16, 19)
    epics = [_epic(f"epic-{index:02d}") for index in range(count)]
    rows = {index: _tagged(f"row-{index:02d}", "roadmap") for index in kept}
    off_tag = _tagged("off-tag", "other")
    fake = _FrontierFake(
        open_tasks=[*epics, *rows.values(), off_tag],
        ready=[*rows.values(), off_tag],
        blocked=[],
        children={
            f"epic-{index:02d}": [rows[index]] if index in kept else [off_tag]
            for index in range(count)
        },
    )
    filters = replace(_FILTERS, tags=("roadmap",))

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert [rollup.task.id for rollup in data.epics] == [
        f"epic-{index:02d}" for index in kept
    ]
    assert data.epics_hidden == count - len(kept)
    # Every epic was still READ — the scoping narrows the strip, not the
    # fan-out — so the count above is the whole corpus.
    assert len(fake.children_calls) == count


def test_the_selected_epic_keeps_its_chip_and_the_board_explains_the_gap() -> None:
    """The residual dead end: the scoped chip is kept whatever the filters
    leave of it (it is the live scope and the way back out), so the board — not
    four "no match" section lines — has to say why it is empty."""
    epic = _epic("epic-1")
    inside = _tagged("inside", "other")
    fake = _FrontierFake(
        open_tasks=[epic, inside],
        ready=[inside],
        blocked=[],
        children={"epic-1": [inside]},
    )
    filters = replace(_FILTERS, tags=("roadmap",), epic="epic-1")

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert [rollup.task.id for rollup in data.epics] == ["epic-1"]
    assert data.epic_scope == "epic-1"
    assert not any(data.sections.values())
    assert data.epic_scope_unmatched is True
    # No confirming read was needed: the subtree is not empty, the filters
    # emptied it — a different thing, said differently.
    assert fake.get_calls == []


def test_a_scoped_board_with_rows_explains_nothing() -> None:
    """The mirror: the explanation is for an EMPTIED scope only. A scope that
    renders rows (or a confirmed-childless epic, which has its own banner)
    must not carry it."""
    epic = _epic("epic-1")
    inside = _tagged("inside", "roadmap")
    fake = _FrontierFake(
        open_tasks=[epic, inside],
        ready=[inside],
        blocked=[],
        children={"epic-1": [inside]},
    )
    filters = replace(_FILTERS, tags=("roadmap",), epic="epic-1")

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))
    assert data.epic_scope_unmatched is False

    childless = _FrontierFake(
        open_tasks=[epic, inside], ready=[inside], blocked=[], children={"epic-1": []}
    )
    empty = asyncio.run(load_dashboard(childless, filters=filters, frontier_limit=500))
    assert empty.epic_scope == "epic-1"
    assert empty.epic_scope_unmatched is False


def test_load_dashboard_ignores_a_scope_that_is_no_longer_an_open_epic() -> None:
    """A stale ``?epic=`` bookmark (the epic completed, or the id is junk)
    resolves to NO scope — the full board with ``epic_scope`` empty, so the
    template can explain it — rather than an unexplained empty page."""
    ready = _task("r", claims=())
    fake = _FrontierFake(open_tasks=[ready], ready=[ready], blocked=[])
    filters = replace(_FILTERS, epic="epic-gone")
    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert data.epics == ()
    assert data.epic_scope == ""
    assert _section_ids(data.sections, "ready") == ["r"]
    assert data.errors == ()


def test_confirmed_childless_epic_keeps_a_real_empty_scope() -> None:
    """An open epic whose recursive children really are ``[]`` scopes to an
    EMPTY set — the chip's contract is "scope to my descendants", and it has
    none, so the board is empty rather than showing every other task. The
    ambiguity with a just-closed epic is resolved by re-reading the epic (here
    it confirms open), not by refusing to scope."""
    epic = _epic("epic-1")
    ready = _task("r", claims=())
    fake = _FrontierFake(
        open_tasks=[epic, ready], ready=[ready], blocked=[], children={"epic-1": []}
    )
    filters = replace(_FILTERS, epic="epic-1")
    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert fake.get_calls == ["epic-1"]
    assert data.epic_scope == "epic-1"
    assert _section_ids(data.sections, "ready") == []
    assert data.summary.open_total == 0
    assert data.scoped_epic is not None
    assert data.scoped_epic.progress_label == "0/0"


def test_epic_that_cannot_be_confirmed_open_falls_back_unscoped() -> None:
    """Reviewer repro (c-001), lifecycle race: the epic was in the open
    snapshot but has closed by the time ``task_children`` runs, so it answers
    empty — same as a childless epic. The confirming ``task_get`` fails (a
    deleted task raises the coded not-found error), so Lens does NOT scope:
    the board stays whole with the announced fallback, and the stale chip goes
    rather than claiming an epic that is gone."""
    epic = _epic("epic-1")
    ready = _task("r", claims=())
    fake = _FrontierFake(
        open_tasks=[epic, ready],
        ready=[ready],
        blocked=[],
        children={"epic-1": []},
        missing_gets={"epic-1"},
    )
    filters = replace(_FILTERS, epic="epic-1")
    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert fake.get_calls == ["epic-1"]
    assert data.epic_scope == ""
    assert data.epics == ()
    assert _section_ids(data.sections, "ready") == ["r"]
    assert data.summary.open_total == 1


def test_epic_confirmed_resolved_falls_back_unscoped() -> None:
    """The other confirmation outcome: the epic still exists but has since
    completed, so it is no longer an open epic — same fallback, no scope."""
    epic = _epic("epic-1")
    ready = _task("r", claims=())
    fake = _FrontierFake(
        open_tasks=[epic, ready],
        ready=[ready],
        blocked=[],
        children={"epic-1": []},
        gets={"epic-1": _task("epic-1", task_type="epic", status="completed")},
    )
    filters = replace(_FILTERS, epic="epic-1")
    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert data.epic_scope == ""
    assert data.epics == ()
    assert _section_ids(data.sections, "ready") == ["r"]


def test_an_applied_epic_scope_narrows_the_board() -> None:
    """Regression: a scope hides part of the corpus, so the whole-system claims
    must stand down.

    ``?epic=`` filters every section to one subtree — exactly what
    ``filters_narrowed`` exists to detect — but the predicate only knew about
    tag/agent/project/status, so a scoped board still rendered the system-wide
    "All systems healthy" stripe over the tasks it was hiding.
    """
    epic = _epic("epic-1")
    child = _task("child", claims=())
    outsider = _task("outsider", claims=())
    fake = _FrontierFake(
        open_tasks=[epic, child, outsider],
        ready=[child, outsider],
        blocked=[],
        children={"epic-1": [child]},
    )

    scoped = asyncio.run(
        load_dashboard(
            fake, filters=replace(_FILTERS, epic="epic-1"), frontier_limit=500
        )
    )

    assert scoped.epic_scope == "epic-1"
    assert _section_ids(scoped.sections, "ready") == ["child"]
    assert scoped.filters_narrowed is True
    assert scoped.healthy is False

    # …and the same board unscoped is not narrowed, so the stripe returns.
    unscoped = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))
    assert unscoped.filters_narrowed is False
    assert unscoped.healthy is True


def test_an_unresolved_epic_scope_does_not_narrow_the_board() -> None:
    """The mirror case: a requested epic that could NOT be resolved leaves the
    board showing everything under the "scope not applied" banner. Nothing is
    hidden, so nothing about the whole-system claims changes — narrowing tracks
    the scope that was applied, not the one that was asked for."""
    epic = _epic("epic-1")
    ready = _task("r", claims=())
    fake = _FrontierFake(
        open_tasks=[epic, ready],
        ready=[ready],
        blocked=[],
        children={"epic-1": []},
        missing_gets={"epic-1"},
    )

    data = asyncio.run(
        load_dashboard(
            fake, filters=replace(_FILTERS, epic="epic-1"), frontier_limit=500
        )
    )

    assert data.epic_scope == ""
    assert _section_ids(data.sections, "ready") == ["r"]
    assert data.filters_narrowed is False


def test_an_unselected_childless_epic_is_never_re_read() -> None:
    """The confirming read is paid only for the ambiguity that matters: a
    childless epic nobody scoped to just renders 0/0."""
    fake = _FrontierFake(
        open_tasks=[_epic("epic-1"), _task("r", claims=())],
        ready=[_task("r", claims=())],
        blocked=[],
        children={"epic-1": []},
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert fake.get_calls == []
    assert [rollup.progress_label for rollup in data.epics] == ["0/0"]


def test_a_selected_epic_with_descendants_needs_no_confirming_read() -> None:
    """No ambiguity, no extra round-trip: a non-empty subtree proves nothing
    about the epic's status is worth re-checking (its rows are what render)."""
    epic = _epic("epic-1")
    inside = _task("in", claims=())
    fake = _FrontierFake(
        open_tasks=[epic, inside],
        ready=[inside],
        blocked=[],
        children={"epic-1": [inside]},
    )
    filters = replace(_FILTERS, epic="epic-1")
    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert fake.get_calls == []
    assert data.epic_scope == "epic-1"


def test_scope_survives_a_child_closing_between_the_two_reads() -> None:
    """The other half of the generation gap: a DESCENDANT that completes after
    the open read still scopes correctly, because the scope is an id set (the
    child renders from whichever snapshot the sections came from) and the chip
    counts are display-only."""
    epic = _epic("epic-1")
    inside = _task("in", claims=())
    fake = _FrontierFake(
        open_tasks=[epic, inside],
        ready=[inside],
        blocked=[],
        # The children read is a generation newer: it already reports the child
        # completed, while the open snapshot still has it open.
        children={"epic-1": [_task("in", status="completed")]},
    )
    filters = replace(_FILTERS, epic="epic-1")
    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert data.epic_scope == "epic-1"
    assert [rollup.progress_label for rollup in data.epics] == ["1/1"]
    # The row still renders from the open snapshot — the newer count did not
    # move it anywhere.
    assert _section_ids(data.sections, "ready") == ["in"]


def test_every_open_epic_gets_a_chip_with_the_fan_out_kept_in_batches() -> None:
    """Story 8 wants a chip for EACH open epic, so nothing is capped away —
    what is bounded is concurrency: the children reads go out in batches of
    EPIC_FANOUT_BATCH, so neither the shared MCP session nor memory sees the
    whole corpus at once."""
    count = EPIC_FANOUT_BATCH * 3 + 1
    epics = [_epic(f"epic-{n}") for n in range(count)]
    ready = _task("r", claims=())
    fake = _FrontierFake(
        open_tasks=[*epics, ready],
        ready=[ready],
        blocked=[],
        children={epic.id: _subtree(done=1, open_=1) for epic in epics},
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    # Every epic rolled up — none dropped, none skipped.
    assert [rollup.task.id for rollup in data.epics] == [epic.id for epic in epics]
    assert len(fake.children_calls) == count
    # …but never more than one batch in flight at a time.
    assert fake.max_children_inflight == EPIC_FANOUT_BATCH


def test_a_finished_batch_is_released_before_the_next_is_issued() -> None:
    """The residency half of the fan-out bound: the previous batch's subtrees
    (and the loop variables pointing into them) must be gone before the next
    batch goes out, or two batches' responses coexist. Reducing each batch in
    its own frame is what guarantees it."""
    epics = [_epic(f"epic-{n}") for n in range(EPIC_FANOUT_BATCH * 3)]
    ready = _task("r", claims=())
    fake = _FrontierFake(
        open_tasks=[*epics, ready],
        ready=[ready],
        blocked=[],
        children={epic.id: _subtree(done=1, open_=1) for epic in epics},
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert len(data.epics) == len(epics)
    # Never more than ONE batch of responses alive at any moment.
    assert fake.max_live_responses == EPIC_FANOUT_BATCH


def test_only_the_selected_epic_keeps_its_descendant_ids() -> None:
    """The set is read only as the ``?epic=`` scope, and the subtree reads are
    include_closed=True — so keeping one per epic would retain an id for every
    task ever closed under every epic, for the whole render. The strip keeps at
    most the selected epic's."""
    epics = [_epic(f"epic-{n}") for n in range(3)]
    ready = _task("r", claims=())
    fake = _FrontierFake(
        open_tasks=[*epics, ready],
        ready=[ready],
        blocked=[],
        children={epic.id: _subtree(done=1, open_=1) for epic in epics},
    )

    unscoped = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))
    # Nothing is scoped, so no set is retained at all.
    assert all(rollup.descendant_ids == frozenset() for rollup in unscoped.epics)
    # …and the counts, which is what the chips actually render, are unharmed.
    assert [rollup.progress_label for rollup in unscoped.epics] == ["1/2"] * 3

    scoped = asyncio.run(
        load_dashboard(
            fake, filters=replace(_FILTERS, epic="epic-1"), frontier_limit=500
        )
    )
    kept = {
        rollup.task.id: rollup.descendant_ids
        for rollup in scoped.epics
        if rollup.descendant_ids
    }
    assert list(kept) == ["epic-1"]
    assert kept["epic-1"] == {"d0", "o0"}
    # The retained set is the real scope: the sections still filter by it.
    assert scoped.epic_scope == "epic-1"


def test_a_scope_on_a_late_epic_still_resolves() -> None:
    """Regression for the capped-strip behaviour: a bookmarked scope naming an
    epic far down the list must still work, not fall back to the whole board
    because the strip stopped short."""
    epics = [_epic(f"epic-{n}") for n in range(EPIC_FANOUT_BATCH * 3 + 1)]
    last = epics[-1]
    inside = _task("in", claims=())
    outside = _task("out", claims=())
    fake = _FrontierFake(
        open_tasks=[*epics, inside, outside],
        ready=[inside, outside],
        blocked=[],
        children={last.id: [inside]},
    )
    filters = replace(_FILTERS, epic=last.id)
    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert data.epic_scope == last.id
    assert _section_ids(data.sections, "ready") == ["in"]


def test_active_claims_counts_only_the_rendered_in_progress_rows() -> None:
    """Reviewer repro (c-003): the situation card pairs the In-progress count
    with its claims, so both must describe the same set. A claim outside the
    epic scope must not inflate the scoped card."""
    epic = _epic("epic-1")
    inside = _task("in", claims=(ClaimRecord(agent="a", aspect="impl"),))
    outside = _task("out", claims=(ClaimRecord(agent="b", aspect="impl"),))
    fake = _FrontierFake(
        open_tasks=[epic, inside, outside],
        ready=[],
        blocked=[],
        children={"epic-1": [inside]},
    )
    filters = replace(_FILTERS, epic="epic-1")
    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert data.summary.in_progress == 1
    assert data.summary.active_claims == 1

    unscoped = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))
    assert unscoped.summary.in_progress == 2
    # Unscoped, the same derivation still describes the rendered rows — the
    # fake's Lithos-wide stat (open_claims=2 here) is not what drives it.
    assert unscoped.summary.active_claims == 2


def test_failed_epic_children_read_drops_the_chip_and_reports_the_error() -> None:
    """A children read that fails must not produce a chip with a wrong count:
    the epic drops out of the strip and the load-error banner says so. The rest
    of the dashboard still renders."""
    epic = _epic("epic-1")
    healthy = _epic("epic-2")
    ready = _task("r", claims=())
    fake = _FrontierFake(
        open_tasks=[epic, healthy, ready],
        ready=[ready],
        blocked=[],
        children={"epic-2": [_task("x", status="completed")]},
        fail_children={"epic-1"},
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert [rollup.task.id for rollup in data.epics] == ["epic-2"]
    assert any("epic progress" in message for message in data.errors)
    assert _section_ids(data.sections, "ready") == ["r"]


def test_epic_strip_is_refetched_when_the_skew_retry_adopts_a_new_snapshot() -> None:
    """The strip must not mix generations with the sections: when read-skew
    forces the master-open retry, the epic fan-out runs again over the retried
    snapshot (the first snapshot's epic had already closed)."""
    stale_epic = _epic("epic-old")
    fresh_epic = _epic("epic-new")
    ready = _task("r", claims=())
    gap = _task("g", claims=())
    fake = _FrontierFake(
        # First open read carries the stale epic and a task in neither
        # frontier (the skew trigger); the retried snapshot replaces it.
        open_tasks=[[stale_epic, ready, gap], [fresh_epic, ready]],
        ready=[ready],
        blocked=[],
        children={
            "epic-old": _subtree(done=1, open_=1),
            "epic-new": _subtree(done=5, open_=3),
        },
    )
    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert fake.open_calls == 2
    assert [rollup.task.id for rollup in data.epics] == ["epic-new"]
    assert [rollup.progress_label for rollup in data.epics] == ["5/8"]
    assert [call["task_id"] for call in fake.children_calls] == [
        "epic-old",
        "epic-new",
    ]


# --- Needs attention on the assembly path (T1-S3) --------------------------
#
# The rule model itself is covered by tests/test_attention.py; these pin that
# load_dashboard applies it, scopes it by the filters, and counts it honestly.

_NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _ago(**delta: float) -> str:
    return (_NOW - timedelta(**delta)).isoformat()


def _ahead(**delta: float) -> str:
    return (_NOW + timedelta(**delta)).isoformat()


def test_load_dashboard_promotes_and_counts_attention() -> None:
    """Assembly level: the promotion happens on the real dashboard path, the
    header counter reports it, and open_total still counts every workable open
    task exactly once (a promoted row only changed section)."""
    stuck = _task("stuck", claims=(), created_at=_ago(hours=1))
    ready = _task("r", claims=(), created_at=_ago(minutes=5))
    gate = _task(
        "gate-1",
        task_type="gate",
        claims=(),
        created_at=_ago(days=3),
        metadata={"gate_type": "human"},
    )
    fake = _FrontierFake(
        open_tasks=[stuck, ready, gate],
        ready=[ready],
        blocked=[
            _blocked(
                stuck,
                BlockerRecord(
                    kind="blocker_unsatisfiable", task_id="dead", status="cancelled"
                ),
            )
        ],
    )
    data = asyncio.run(
        load_dashboard(fake, filters=_FILTERS, frontier_limit=500, now=_NOW)
    )

    assert _section_ids(data.sections, "attention") == ["stuck", "gate-1"]
    assert _section_ids(data.sections, "blocked") == []
    assert _section_ids(data.sections, "ready") == ["r"]
    assert data.summary.attention == 2
    assert data.summary.blocked == 0
    # Two workable open tasks (the promoted one still counts); the promoted
    # GATE never belonged to the workable partition, so it does not.
    assert data.summary.open_total == 2


def test_load_dashboard_attention_is_empty_when_only_terminal_statuses_show() -> None:
    """The attention list is an OPEN-section surface: with `open` deselected it
    renders empty rather than leaking rows into a terminal-only view."""
    stuck = _task("stuck", claims=(), created_at=_ago(days=40))
    fake = _FrontierFake(open_tasks=[stuck], ready=[], blocked=[])
    filters = TaskFilters(statuses=("completed",), tags=(), agent="", since="")
    data = asyncio.run(
        load_dashboard(fake, filters=filters, frontier_limit=500, now=_NOW)
    )
    assert data.sections["attention"] == ()


def test_load_dashboard_filters_scope_the_attention_list() -> None:
    """Gate rows come from the filtered snapshot, so a gate outside the tag
    filter must not appear in the attention list."""
    mine = _task(
        "gate-mine",
        task_type="gate",
        claims=(),
        created_at=_ago(days=3),
        tags=("project:mine",),
        metadata={"gate_type": "human"},
    )
    theirs = _task(
        "gate-theirs",
        task_type="gate",
        claims=(),
        created_at=_ago(days=3),
        tags=("project:other",),
        metadata={"gate_type": "human"},
    )
    fake = _FrontierFake(open_tasks=[mine, theirs], ready=[], blocked=[])
    filters = TaskFilters(
        statuses=("open",), tags=("project:mine",), agent="", since=""
    )
    data = asyncio.run(
        load_dashboard(fake, filters=filters, frontier_limit=500, now=_NOW)
    )
    assert _section_ids(data.sections, "attention") == ["gate-mine"]


# --- T1-S4: the Gates section on the assembly path -----------------------


def _gate_task(
    task_id: str,
    *,
    gate_type: str = "human",
    created_at: str = "",
    tags: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> TaskRecord:
    # Young by default: a human gate older than gate_waiting_attention_hours is
    # promoted into Needs attention (rule 3) and leaves the Gates section, so a
    # stale default would silently empty the surface under test.
    created_at = created_at or _ago(hours=1)
    meta: dict[str, Any] = {"gate_type": gate_type}
    meta.update(metadata or {})
    return _task(
        task_id,
        task_type="gate",
        claims=(),
        created_at=created_at,
        tags=tags,
        metadata=meta,
    )


def _gate_row(data: Any, gate_id: str) -> Any:
    return next(row for row in data.gates if row.task.id == gate_id)


def test_load_dashboard_renders_gates_in_their_own_section() -> None:
    """Gates are excluded from both frontiers upstream, so they reach the board
    only through this section — and the header counter reports them apart from
    the workable three."""
    gate = _gate_task("gate-1")
    work = _task("w", claims=())
    fake = _FrontierFake(open_tasks=[gate, work], ready=[work], blocked=[])
    data = asyncio.run(
        load_dashboard(fake, filters=_FILTERS, frontier_limit=500, now=_NOW)
    )

    assert [row.task.id for row in data.gates] == ["gate-1"]
    assert data.summary.gates == 1
    assert data.summary.ready == 1
    # …and a gate is not counted as a rolled-up row any more: it renders.
    assert data.rolled_up_open == 0


def test_open_gate_appears_in_exactly_one_place_on_the_board() -> None:
    """Single placement: a gate the severity rules promoted into Needs
    attention is NOT also listed under Gates, and an unpromoted one is in the
    Gates section and no partition section."""
    fresh = _gate_task("gate-fresh", created_at=_ago(hours=1))
    stale = _gate_task("gate-stale", created_at=_ago(days=9))
    fake = _FrontierFake(open_tasks=[fresh, stale], ready=[], blocked=[])
    data = asyncio.run(
        load_dashboard(fake, filters=_FILTERS, frontier_limit=500, now=_NOW)
    )

    placements = {
        gate_id: sum(
            1
            for rows in data.sections.values()
            for row in rows
            if row.task.id == gate_id
        )
        + sum(1 for row in data.gates if row.task.id == gate_id)
        for gate_id in ("gate-fresh", "gate-stale")
    }
    assert placements == {"gate-fresh": 1, "gate-stale": 1}
    assert _section_ids(data.sections, "attention") == ["gate-stale"]
    assert [row.task.id for row in data.gates] == ["gate-fresh"]


def test_gate_waiter_count_is_not_narrowed_by_the_boards_filters() -> None:
    """Both ``task_blocked`` call sites pass only ``limit`` — no project, tag or
    agent filter — so a gate that blocks three tasks still reads "blocks 3
    tasks" on a board scoped to one of them."""
    gate = _gate_task("gate-1", tags=("project:mine",))
    waiters = [
        _task("w1", claims=(), tags=("project:mine",)),
        _task("w2", claims=(), tags=("project:other",)),
        _task("w3", claims=(), tags=("project:other",)),
    ]
    blocked = [
        _blocked(
            waiter,
            BlockerRecord(kind="gate", task_id="gate-1", type="waits_on_gate"),
        )
        for waiter in waiters
    ]
    filtered = TaskFilters(
        statuses=("open",), tags=("project:mine",), agent="", since=""
    )

    def _load(filters: TaskFilters) -> Any:
        fake = _FrontierFake(
            open_tasks=[gate, *waiters], ready=[], blocked=list(blocked)
        )
        data = asyncio.run(
            load_dashboard(fake, filters=filters, frontier_limit=500, now=_NOW)
        )
        # No filter is pushed to the blocked read on either call site.
        return data, fake

    wide, _ = _load(_FILTERS)
    narrow, narrow_fake = _load(filtered)

    assert _gate_row(wide, "gate-1").waiters_label == "blocks 3 tasks"
    assert _gate_row(narrow, "gate-1").waiters_label == "blocks 3 tasks"
    # The board itself IS scoped — only one waiter renders as a row…
    assert _section_ids(narrow.sections, "blocked") == ["w1"]
    # …and the waiter list still names every one of them.
    assert [w.id for w in _gate_row(narrow, "gate-1").waiters] == ["w1", "w2", "w3"]
    assert narrow_fake.edge_list_calls == []


def test_healthy_board_derives_gate_waiters_without_a_single_edge_read() -> None:
    """The blocked frontier already names every waiter, so the normal render
    costs zero extra calls however many gates the corpus holds."""
    gates = [_gate_task(f"gate-{index:03d}") for index in range(60)]
    fake = _FrontierFake(open_tasks=list(gates), ready=[], blocked=[])
    data = asyncio.run(
        load_dashboard(fake, filters=_FILTERS, frontier_limit=500, now=_NOW)
    )

    assert data.summary.gates == 60
    assert fake.edge_list_calls == []


def test_truncated_blocked_read_caps_the_gate_edge_fanout() -> None:
    """Degraded path: the blocked response hit the limit, so waiters fall back
    to the gates' own edges — bounded, because the gate count is
    peer-controlled and this runs on every render."""
    gates = [_gate_task(f"gate-{index:03d}") for index in range(60)]
    waiter = _task("w1", claims=())
    fake = _FrontierFake(
        open_tasks=[*gates, waiter],
        ready=[],
        blocked=[
            _blocked(waiter, BlockerRecord(kind="gate", task_id="gate-000")),
        ],
        edges={
            "gate-000": [
                EdgeRecord(
                    from_task_id="gate-000", to_task_id="w1", type="waits_on_gate"
                )
            ]
        },
    )
    data = asyncio.run(
        load_dashboard(fake, filters=_FILTERS, frontier_limit=1, now=_NOW)
    )

    assert len(fake.edge_list_calls) == GATE_WAITER_FANOUT_CAP
    assert _gate_row(data, "gate-000").waiters_label == "blocks 1 task (unverified)"
    # Past the cap the row degrades rather than reporting an unread zero.
    assert _gate_row(data, "gate-059").waiters_label == "blocks at least 0 tasks"


def test_gates_render_when_the_frontier_read_fails_and_stay_out_of_the_flat_list() -> (
    None
):
    """A gate's PLACEMENT never came from the frontier — only its waiter count
    did. So a frontier outage keeps the operator's own queue on screen, with
    the count honestly reported as unavailable, and the gate does not also
    appear in the flat open list."""
    gate = _gate_task("gate-1")
    work = _task("w", claims=())

    def _load(**kwargs: Any) -> tuple[Any, Any]:
        fake = _FrontierFake(
            open_tasks=[gate, work],
            ready=[],
            blocked=[],
            blocked_error=RuntimeError("blocked frontier unavailable"),
            **kwargs,
        )
        return (
            asyncio.run(
                load_dashboard(fake, filters=_FILTERS, frontier_limit=500, now=_NOW)
            ),
            fake,
        )

    # The gate's own edges still answer: an unverified count, not a silent one.
    data, fake = _load(
        edges={
            "gate-1": [
                EdgeRecord(from_task_id="gate-1", to_task_id="w", type="waits_on_gate")
            ]
        }
    )
    assert data.open_flat is True
    # The flat list holds the workable row and NOT the gate — which renders in
    # the Gates section, so single placement survives the outage.
    assert _section_ids(data.sections, "open") == ["w"]
    assert [row.task.id for row in data.gates] == ["gate-1"]
    assert _gate_row(data, "gate-1").waiters_label == "blocks 1 task (unverified)"
    assert len(fake.edge_list_calls) == 1

    # Neither source answered: the row says the count is unavailable rather
    # than presenting an unread zero, and the page says so too.
    data, _ = _load(fail_edges=True)
    assert _gate_row(data, "gate-1").waiters_label == "waiter count unavailable"
    assert (
        "Could not load waiter counts for some gates; "
        "their counts are shown as unavailable." in data.errors
    )


def test_gates_and_their_edge_reads_are_skipped_when_the_open_side_is_hidden() -> None:
    """``?status=completed`` hides the open sections by choice; the Gates
    section follows them, and the degraded fan-out never runs."""
    gate = _gate_task("gate-1")
    fake = _FrontierFake(open_tasks=[gate], ready=[], blocked=[])
    filters = TaskFilters(statuses=("completed",), tags=(), agent="", since="")
    data = asyncio.run(
        load_dashboard(fake, filters=filters, frontier_limit=500, now=_NOW)
    )

    assert data.gates == ()
    assert data.summary.gates == 0
    assert fake.edge_list_calls == []


def test_next_gate_ready_at_is_the_earliest_visible_future_timer() -> None:
    """The board publishes ONE instant for the browser's one-shot refresh."""
    soon = _gate_task(
        "timer-soon", gate_type="timer", metadata={"ready_at": _ahead(hours=2)}
    )
    later = _gate_task(
        "timer-later", gate_type="timer", metadata={"ready_at": _ahead(days=2)}
    )
    lapsed = _gate_task(
        "timer-lapsed", gate_type="timer", metadata={"ready_at": _ago(hours=2)}
    )
    fake = _FrontierFake(open_tasks=[later, lapsed, soon], ready=[], blocked=[])
    data = asyncio.run(
        load_dashboard(fake, filters=_FILTERS, frontier_limit=500, now=_NOW)
    )

    assert data.next_gate_ready_at == (
        datetime.fromisoformat(_ahead(hours=2)).astimezone(UTC).isoformat()
    )


def test_an_edge_created_after_the_ready_read_still_counts_as_a_waiter() -> None:
    """Reviewer repro (correctness f-002): the reads are independent
    generations, and the edge list is the LATER one.

    Sequence: ``task_ready`` answers first and calls `victim` ready; another
    actor then creates `gate-1 -waits_on_gate-> victim`, which per the
    ``lithos_task_edge_upsert`` contract makes `victim` wait from that moment;
    the degraded edge read then returns it. The earlier ready response cannot
    refute the newer edge, so `victim` must be counted — a round-2 cross-check
    against `ready_ids` dropped it and rendered "blocks 0 tasks (unverified)".

    The other two targets are still dropped, and neither drop is a refutation
    of an edge: an epic is not a unit of work the count is over, and `ghost`
    names nothing the section could draw a row for."""
    gate = _gate_task("gate-1")
    victim = _task("victim", claims=())
    epic = _task("an-epic", task_type="epic", claims=())
    # One unrelated blocked row, so the blocked response HITS frontier_limit
    # below — truncation is what routes the waiter counts to the edge read.
    filler = _task("filler", claims=())
    fake = _FrontierFake(
        open_tasks=[gate, victim, epic, filler],
        # `victim` was ready when the frontier answered…
        ready=[victim],
        blocked=[_blocked(filler, BlockerRecord(kind="task", task_id="elsewhere"))],
        late_edges={
            # …and only afterwards did the gate come to hold it.
            "gate-1": [
                EdgeRecord(
                    from_task_id="gate-1", to_task_id=target, type="waits_on_gate"
                )
                for target in ("victim", "an-epic", "ghost")
            ]
        },
    )
    # frontier_limit=1 truncates the blocked read, which is what sends the
    # waiter counts down the edge fallback in the first place.
    data = asyncio.run(
        load_dashboard(fake, filters=_FILTERS, frontier_limit=1, now=_NOW)
    )

    row = _gate_row(data, "gate-1")
    assert [waiter.id for waiter in row.waiters] == ["victim"]
    assert row.waiters_label == "blocks 1 task (unverified)"


# --- project quick-switch strip (§5.3) -------------------------------------


def _in_project(task_id: str, project: str, *tags: str) -> TaskRecord:
    """An open, unclaimed row carrying the tag convention's project slug."""
    return _task(task_id, claims=(), tags=(f"project:{project}", *tags))


def test_the_project_strip_enumerates_the_projects_of_the_scoped_board() -> None:
    """The ask (Dave, 2026-09-11): a roadmap tag spans a handful of projects,
    and the set is already in the snapshot — so the strip offers it with an
    open-row count each, instead of the operator retyping the Project box.
    Ordered by count then slug, so it reads as a summary of where the work is.
    """
    rows = [
        _in_project("lens-1", "lithos-lens", "roadmap"),
        _in_project("lens-2", "lithos-lens", "roadmap"),
        _in_project("loom-1", "lithos-loom", "roadmap"),
        _in_project("core-1", "lithos", "roadmap"),
        # Same project as a scoped row, but outside the tag: it is not on this
        # board, so it must neither add a chip nor inflate a count.
        _in_project("core-elsewhere", "lithos"),
        # A project with NO row in the scope at all.
        _in_project("other-1", "lithos-other"),
    ]
    fake = _FrontierFake(open_tasks=rows, ready=rows, blocked=[])
    filters = replace(_FILTERS, tags=("roadmap",))

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert [(chip.slug, chip.open_count) for chip in data.project_chips] == [
        ("lithos-lens", 2),
        ("lithos", 1),
        ("lithos-loom", 1),
    ]
    assert not any(chip.selected for chip in data.project_chips)


def test_the_strip_does_not_shrink_as_the_operator_moves_between_projects() -> None:
    """The strip is scoped by every active filter EXCEPT ``project``, so
    narrowing to one project marks its chip and leaves the others one click
    away — with their counts unmoved, because the scope did not change."""
    rows = [
        _in_project("lens-1", "lithos-lens", "roadmap"),
        _in_project("lens-2", "lithos-lens", "roadmap"),
        _in_project("loom-1", "lithos-loom", "roadmap"),
        _in_project("core-1", "lithos", "roadmap"),
    ]
    fake = _FrontierFake(open_tasks=rows, ready=rows, blocked=[])
    filters = replace(_FILTERS, tags=("roadmap",), projects=("lithos-loom",))

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert [(chip.slug, chip.open_count) for chip in data.project_chips] == [
        ("lithos-lens", 2),
        ("lithos", 1),
        ("lithos-loom", 1),
    ]
    assert [chip.slug for chip in data.project_chips if chip.selected] == [
        "lithos-loom"
    ]
    # …and the board itself IS narrowed — the strip's scope is not the board's.
    assert _section_ids(data.sections, "ready") == ["loom-1"]


def test_a_second_project_ors_onto_the_first() -> None:
    """Projects OR among themselves (``?project=a,b``, what the comma form
    already means), so adding one widens the board and marks both chips."""
    rows = [
        _in_project("lens-1", "lithos-lens", "roadmap"),
        _in_project("loom-1", "lithos-loom", "roadmap"),
        _in_project("core-1", "lithos", "roadmap"),
    ]
    fake = _FrontierFake(open_tasks=rows, ready=rows, blocked=[])
    filters = replace(
        _FILTERS, tags=("roadmap",), projects=("lithos-lens", "lithos-loom")
    )

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert sorted(chip.slug for chip in data.project_chips if chip.selected) == [
        "lithos-lens",
        "lithos-loom",
    ]
    assert sorted(_section_ids(data.sections, "ready")) == ["lens-1", "loom-1"]
    # The unselected project is still offered, still counted.
    assert ("lithos", 1) in [
        (chip.slug, chip.open_count) for chip in data.project_chips
    ]


def test_a_metadata_only_project_is_counted_like_a_tagged_one() -> None:
    """§5B.1's both-conventions rule, which is the default posture: loom's
    issue-mirrored tasks carry ``metadata.project`` and no project TAG, and a
    strip that read only the tag convention would leave those projects — this
    UX pass's own tasks among them — out of the scope they are inside."""
    mirrored = _task(
        "mirrored",
        claims=(),
        tags=("roadmap",),
        metadata={"project": "lithos-loom"},
    )
    tagged = _in_project("tagged", "lithos-lens", "roadmap")
    fake = _FrontierFake(
        open_tasks=[mirrored, tagged], ready=[mirrored, tagged], blocked=[]
    )

    data = asyncio.run(
        load_dashboard(
            fake, filters=replace(_FILTERS, tags=("roadmap",)), frontier_limit=500
        )
    )

    assert [(chip.slug, chip.open_count) for chip in data.project_chips] == [
        ("lithos-lens", 1),
        ("lithos-loom", 1),
    ]


def test_every_chip_leads_to_a_board_with_its_own_rows_on_it_whatever_the_posture() -> (
    None
):
    """The no-dead-end rule, under each supported ``project_convention``.

    The enumeration is §5B.1's universe (both conventions, the call the Project
    datalist and the graph scope picker share), but a chip is an OFFER to add
    ``?project=<slug>`` — so what the strip draws is the universe intersected
    with what that link would match. Under ``"both"`` those coincide and a
    metadata-only project counts exactly like a tagged one; under a
    single-convention posture ``matches_projects`` honours only that
    convention, so the slug the other one carries names an empty board and is
    not offered. Every chip is FOLLOWED here, and its count checked against the
    rows the board then holds: a strip that advertised a slug its own filter
    cannot reach would fail on the empty board, and one that mis-stated the
    count would fail on the number.
    """
    mirrored = _task(
        "mirrored",
        claims=(),
        tags=("roadmap",),
        metadata={"project": "lithos-loom"},
    )
    tagged = _in_project("tagged", "lithos-lens", "roadmap")
    fake = _FrontierFake(
        open_tasks=[mirrored, tagged], ready=[mirrored, tagged], blocked=[]
    )
    expected = {
        "both": [("lithos-lens", 1), ("lithos-loom", 1)],
        "tag": [("lithos-lens", 1)],
        "metadata": [("lithos-loom", 1)],
    }

    for convention, chips in expected.items():
        filters = replace(_FILTERS, tags=("roadmap",), project_convention=convention)
        data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

        assert [(chip.slug, chip.open_count) for chip in data.project_chips] == chips, (
            convention
        )

        for chip in data.project_chips:
            followed = asyncio.run(
                load_dashboard(
                    fake,
                    filters=replace(filters, projects=(chip.slug,)),
                    frontier_limit=500,
                )
            )
            shown = [
                row.task.id
                for section in OPEN_SECTIONS
                for row in followed.sections[section]
            ]
            assert shown, (convention, chip.slug)
            assert len(shown) == chip.open_count, (convention, chip.slug)


def test_the_strip_scope_honours_the_agent_and_created_windows() -> None:
    """The scope is every active filter except ``project`` — ALL of them. A
    strip that skipped the agent match or the created window would enumerate a
    project whose only row this board does not show, which is exactly the
    dead-end chip §5.2.1's rule exists to remove."""
    mine = _task(
        "lens-mine",
        claims=(),
        tags=("project:lithos-lens", "roadmap"),
        created_by="planner",
        created_at="2026-09-10T10:00:00+00:00",
    )
    theirs = _task(
        "loom-theirs",
        claims=(),
        tags=("project:lithos-loom", "roadmap"),
        created_by="worker",
        created_at="2026-09-10T10:00:00+00:00",
    )
    older = _task(
        "core-older",
        claims=(),
        tags=("project:lithos", "roadmap"),
        created_by="planner",
        created_at="2026-01-02T10:00:00+00:00",
    )
    rows = [mine, theirs, older]
    fake = _FrontierFake(open_tasks=rows, ready=rows, blocked=[])
    filters = replace(
        _FILTERS,
        tags=("roadmap",),
        agent="planner",
        created_since="2026-09-01",
    )

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    # One row survives both windows, so one project is on the strip: the other
    # agent's project and the pre-window project are not offered.
    assert [(chip.slug, chip.open_count) for chip in data.project_chips] == [
        ("lithos-lens", 1)
    ]
    # …and the chip that IS offered still leads somewhere.
    for chip in data.project_chips:
        followed = asyncio.run(
            load_dashboard(
                fake,
                filters=replace(filters, projects=(chip.slug,)),
                frontier_limit=500,
            )
        )
        assert any(followed.sections.values()), chip.slug


def test_the_project_strip_counts_open_rows_only() -> None:
    """The count is "how much open work is there to switch to". A project
    whose only rows are in the resolved windows holds none, so it is not on the
    strip — and a resolved row of a project that IS there does not inflate it.
    """
    open_row = _in_project("lens-open", "lithos-lens", "roadmap")
    done = _task(
        "lens-done",
        status="completed",
        tags=("project:lithos-lens", "roadmap"),
    )
    archived = _task(
        "loom-done",
        status="completed",
        tags=("project:lithos-loom", "roadmap"),
    )
    fake = _FrontierFake(
        open_tasks=[open_row],
        ready=[open_row],
        blocked=[],
        completed=[done, archived],
    )
    filters = replace(_FILTERS, tags=("roadmap",))

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert _section_ids(data.sections, "completed") == ["lens-done", "loom-done"]
    assert [(chip.slug, chip.open_count) for chip in data.project_chips] == [
        ("lithos-lens", 1)
    ]


def test_the_project_strip_counts_gates_and_not_rolled_up_rows() -> None:
    """ "Open rows the board would show" is the open sections plus Gates. An
    epic rolls up into its own strip rather than rendering, so a project whose
    only match is an epic would be a chip leading to an empty board."""
    gate = _gate_task("gate-1", tags=("project:lithos-loom", "roadmap"))
    work = _in_project("lens-1", "lithos-lens", "roadmap")
    epic = _task("epic-1", task_type="epic", tags=("project:lithos-epics", "roadmap"))
    fake = _FrontierFake(
        open_tasks=[gate, work, epic],
        ready=[work],
        blocked=[],
        children={"epic-1": [work]},
    )
    filters = replace(_FILTERS, tags=("roadmap",))

    data = asyncio.run(
        load_dashboard(fake, filters=filters, frontier_limit=500, now=_NOW)
    )

    assert [(chip.slug, chip.open_count) for chip in data.project_chips] == [
        ("lithos-lens", 1),
        ("lithos-loom", 1),
    ]


def test_a_project_chip_never_leads_to_an_empty_board() -> None:
    """The acceptance criterion followed the way an operator follows it: take
    every chip the strip drew and select it — each must leave rows behind."""
    rows = [
        _in_project("lens-1", "lithos-lens", "roadmap"),
        _in_project("loom-1", "lithos-loom", "roadmap"),
        _in_project("core-1", "lithos", "roadmap"),
        _in_project("off-1", "lithos-other"),
    ]
    fake = _FrontierFake(open_tasks=rows, ready=rows, blocked=[])
    filters = replace(_FILTERS, tags=("roadmap",))

    strip = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert [chip.slug for chip in strip.project_chips] == [
        "lithos",
        "lithos-lens",
        "lithos-loom",
    ]
    for chip in strip.project_chips:
        followed = asyncio.run(
            load_dashboard(
                fake,
                filters=replace(filters, projects=(chip.slug,)),
                frontier_limit=500,
            )
        )
        assert any(followed.sections.values()), chip.slug


def test_the_project_strip_follows_the_generation_the_skew_retry_adopted() -> None:
    """Mirrors the epic strip's own rule: the adopted generation decides which
    rows the board holds, so counts computed against the first one would name a
    project the board no longer shows."""
    stale_row = _in_project("stale-row", "lithos-stale", "roadmap")
    fresh_row = _in_project("fresh-row", "lithos-fresh", "roadmap")
    gap = _tagged("gap", "roadmap")
    fake = _FrontierFake(
        # First generation holds the stale project's row and ``gap`` (in
        # neither frontier — the skew trigger); the retry replaces both.
        open_tasks=[[stale_row, gap], [fresh_row]],
        ready=[[stale_row], [fresh_row]],
        blocked=[],
    )
    filters = replace(_FILTERS, tags=("roadmap",))

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert fake.open_calls == 2
    assert [(chip.slug, chip.open_count) for chip in data.project_chips] == [
        ("lithos-fresh", 1)
    ]


def test_the_project_strip_composes_with_an_epic_scope() -> None:
    """The strips compose: inside ``?epic=`` the projects on offer are the
    projects of THAT epic's rows, so the two chrome rows describe one board."""
    epic = _epic("epic-1")
    inside = _in_project("inside", "lithos-lens", "roadmap")
    outside = _in_project("outside", "lithos-loom", "roadmap")
    fake = _FrontierFake(
        open_tasks=[epic, inside, outside],
        ready=[inside, outside],
        blocked=[],
        children={"epic-1": [inside]},
    )
    filters = replace(_FILTERS, tags=("roadmap",), epic="epic-1")

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert data.epic_scope == "epic-1"
    assert [chip.slug for chip in data.project_chips] == ["lithos-lens"]


def test_a_terminal_only_board_draws_no_project_strip() -> None:
    """With the open side switched off there are no open rows to switch
    between, so the strip has nothing to say (the template keeps the clear
    reachable on its own, from the live filter)."""
    done = _task("done", status="completed", tags=("project:lithos-lens",))
    fake = _FrontierFake(open_tasks=[], ready=[], blocked=[], completed=[done])
    filters = TaskFilters(statuses=("completed",), tags=(), agent="", since="")

    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert _section_ids(data.sections, "completed") == ["done"]
    assert data.project_chips == ()
