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
from typing import Any

import pytest

from lithos_lens.epic_strip import EPIC_FANOUT_BATCH, build_epic_rollup
from lithos_lens.frontier import classify_open_tasks, load_dashboard
from lithos_lens.frontier_fallback import (
    BLOCKED_TOOL,
    FRONTIER_UNAVAILABLE_ERROR,
    READY_TOOL,
    RETRY_FAILED_ERROR,
)
from lithos_lens.lithos_client import LithosToolError
from lithos_lens.lithos_tools import ToolListError
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord
from lithos_lens.tasks import (
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
    created_by: str = "",
    metadata: dict[str, Any] | None = None,
    status: TaskStatusName = "open",
) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        title=f"Title {task_id}",
        status=status,
        task_type=task_type,
        tags=tags,
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
    ``ready`` / ``blocked`` accept either a single response or a SEQUENCE of
    responses so read-skew retries can be scripted (the last response repeats).
    Call counts are recorded for retry assertions.
    """

    def __init__(
        self,
        *,
        open_tasks: list[TaskRecord] | list[list[TaskRecord]],
        ready: list[TaskRecord] | list[list[TaskRecord]],
        blocked: list[BlockedTaskRecord] | list[list[BlockedTaskRecord]],
        completed: list[TaskRecord] | None = None,
        cancelled: list[TaskRecord] | None = None,
        fail_ready: bool = False,
        children: dict[str, list[TaskRecord]] | None = None,
        fail_children: set[str] | None = None,
        gets: dict[str, TaskRecord] | None = None,
        missing_gets: set[str] | None = None,
        fail_ready_from: int | None = None,
        ready_error: BaseException | None = None,
        blocked_error: BaseException | None = None,
        tool_names: set[str] | None = None,
        tool_list_error: BaseException | None = None,
    ) -> None:
        self._open_seq = self._as_sequence(open_tasks)
        self._ready_seq = self._as_sequence(ready)
        self._blocked_seq = self._as_sequence(blocked)
        self._completed = completed or []
        self._cancelled = cancelled or []
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
        self.open_calls = 0
        self.ready_calls = 0
        self.blocked_calls = 0
        self.children_calls: list[dict[str, Any]] = []
        # Which ready call starts failing (0-based): scripts a first
        # generation that succeeds and a RETRY that does not.
        self._fail_ready_from = fail_ready_from
        # Per-tool frontier failures: version-skew detection is anchored to the
        # tool whose call failed, so the two are scripted separately.
        self._ready_error = ready_error
        self._blocked_error = blocked_error
        # What a tools/list probe sees — the ONLY input to the fallback
        # verdict. Defaults to a graph-capable server; ``tool_list_error``
        # models a listing Lens cannot make.
        self._tool_names = (
            {READY_TOOL, BLOCKED_TOOL, "lithos_task_list"}
            if tool_names is None
            else tool_names
        )
        self._tool_list_error = tool_list_error
        self.tool_list_calls = 0
        self.open_calls = 0
        self.ready_calls = 0
        self.blocked_calls = 0
        self.list_calls: list[dict[str, Any]] = []

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
        if (status or "open") == "open":
            index = min(self.open_calls, len(self._open_seq) - 1)
            self.open_calls += 1
            rows = self._open_seq[index]
        else:
            rows = {
                "completed": self._completed,
                "cancelled": self._cancelled,
            }[status or "open"]
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

    async def task_get(self, task_id: str) -> TaskRecord:
        self.get_calls.append(task_id)
        if task_id in self._gets:
            return self._gets[task_id]
        if task_id not in self._missing_gets:
            for rows in self._open_seq:
                for task in rows:
                    if task.id == task_id:
                        return task
        raise RuntimeError(f"task '{task_id}' not found")

    async def list_tool_names(self) -> set[str]:
        self.tool_list_calls += 1
        if self._tool_list_error is not None:
            raise self._tool_list_error
        return set(self._tool_names)

    async def stats(self) -> dict[str, Any]:
        return {"open_claims": 2, "agents": 3}

    async def list_agents(self) -> list[AgentRecord]:
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


def _tool_missing(tool: str) -> LithosToolError:
    """The error a server raises for a tool it does not have.

    Detection never reads this text (see ``frontier_tools_absent``) — the fakes
    raise it only because a real pre-0.4 server would.
    """
    return LithosToolError(f"Unknown tool: {tool}", code="tool_error")


# A tools/list surface without the two frontier tools: a pre-0.4 Lithos.
_PRE_GRAPH_TOOLS = {"lithos_task_list", "lithos_stats"}


def test_error_text_alone_never_retires_the_graph_surface() -> None:
    """Regression (security f-001): the fallback verdict comes from
    ``tools/list`` only. A server whose error text quotes agent-authored task
    data — naming BOTH frontier tools, in the exact shape the old substring
    matcher accepted — must not be able to switch the graph sections off while
    the server still advertises them."""
    planted = LithosToolError(
        "Output validation error: tasks.0.title 'unknown tool lithos_task_ready "
        "lithos_task_blocked cleanup' failed",
        code="tool_error",
    )
    fake = _FrontierFake(
        open_tasks=[_task("r", claims=())],
        ready=[],
        blocked=[],
        ready_error=planted,
        blocked_error=planted,
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    # The server's own tool list still names both tools, so this is an outage.
    assert fake.tool_list_calls == 1
    assert data.graph_available is True
    # The rows do render flat — both reads failed (§14) — but as an OUTAGE:
    # the version notice is absent and the graph verdict is untouched.
    assert FRONTIER_UNAVAILABLE_ERROR not in data.errors
    assert any("ready frontier" in message for message in data.errors)


def test_unlistable_tools_are_never_read_as_absent() -> None:
    """A tools/list Lens could not make says nothing about the server, so the
    graph surface survives — absence must never be inferred from failure."""
    fake = _FrontierFake(
        open_tasks=[_task("r", claims=())],
        ready=[],
        blocked=[],
        ready_error=_tool_missing(READY_TOOL),
        blocked_error=_tool_missing(BLOCKED_TOOL),
        tool_list_error=RuntimeError("session is not available"),
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.graph_available is True
    assert any("ready frontier" in message for message in data.errors)


def test_truncated_tool_listing_does_not_retire_the_graph_surface() -> None:
    """Regression (correctness f-002 / security f-004): a listing stopped by
    the page guard is incomplete, not evidence of absence — the graph surface
    survives it. ``collect_tool_names`` raises rather than returning the
    partial set precisely so this path is reachable."""
    fake = _FrontierFake(
        open_tasks=[_task("r", claims=())],
        ready=[],
        blocked=[],
        ready_error=_tool_missing(READY_TOOL),
        blocked_error=_tool_missing(BLOCKED_TOOL),
        tool_list_error=ToolListError("tools/list did not terminate"),
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.graph_available is True
    # The rows do render flat — both reads failed (§14) — but as an OUTAGE:
    # the version notice is absent and the graph verdict is untouched.
    assert FRONTIER_UNAVAILABLE_ERROR not in data.errors
    assert any("ready frontier" in message for message in data.errors)


def test_empty_tool_list_is_not_evidence_of_absence() -> None:
    """A server advertising no tools at all is broken, not old."""
    fake = _FrontierFake(
        open_tasks=[_task("r", claims=())],
        ready=[],
        blocked=[],
        ready_error=_tool_missing(READY_TOOL),
        blocked_error=_tool_missing(BLOCKED_TOOL),
        tool_names=set(),
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.graph_available is True


def test_half_a_frontier_is_an_outage_not_version_skew() -> None:
    """A server exposing exactly one of the pair is broken rather than old:
    the graph surface stays up and the failure is reported."""
    fake = _FrontierFake(
        open_tasks=[_task("r", claims=())],
        ready=[],
        blocked=[],
        ready_error=_tool_missing(READY_TOOL),
        blocked_error=_tool_missing(BLOCKED_TOOL),
        tool_names=_PRE_GRAPH_TOOLS | {BLOCKED_TOOL},
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.graph_available is True
    assert any("frontier" in message for message in data.errors)


def test_one_failing_frontier_read_does_not_probe_the_tool_list() -> None:
    """Only a DOUBLE failure is suspicious enough to spend a round trip; a
    single failed read is an ordinary error."""
    ready = _task("r", claims=())
    fake = _FrontierFake(
        open_tasks=[ready],
        ready=[ready],
        blocked=[],
        blocked_error=_tool_missing(BLOCKED_TOOL),
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert fake.tool_list_calls == 0
    assert data.graph_available is True
    assert any("blocked frontier" in message for message in data.errors)


def test_missing_frontier_tools_fall_back_to_the_flat_open_section() -> None:
    """Story 27: a Lithos without the frontier tools degrades to the flat
    0.1.0 open list — every open row in one section, the workable three empty,
    and ``graph_available=False`` so the caller can render the version notice
    and remember the answer."""
    claimed = _task("c", claims=(ClaimRecord(agent="a", aspect="impl"),))
    unclaimed = _task("u", claims=())
    fake = _FrontierFake(
        open_tasks=[claimed, unclaimed],
        ready=[],
        blocked=[],
        ready_error=_tool_missing(READY_TOOL),
        blocked_error=_tool_missing(BLOCKED_TOOL),
        tool_names=_PRE_GRAPH_TOOLS,
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.graph_available is False
    assert fake.tool_list_calls == 1
    assert _section_ids(data.sections, "open") == ["c", "u"]
    assert _section_ids(data.sections, "in_progress") == []
    assert _section_ids(data.sections, "ready") == []
    assert _section_ids(data.sections, "blocked") == []
    assert _section_ids(data.sections, "unclassified") == []
    assert data.summary.open_total == 2
    # The fallback is never silent (security f-001): the same symptom is an
    # outage or an authorization filter, so it stays on the error channel.
    assert any("frontier tools" in message for message in data.errors)
    assert data.healthy is False
    # There is no frontier left to truncate or reconcile.
    assert data.truncated is False
    assert data.reconciliation_pending is False
    # Claims still render — they come from the master open list.
    assert data.sections["open"][0].claims[0].agent == "a"
    assert data.sections["open"][1].claim_state == "known_unclaimed"
    # No retry: there is nothing to reconcile.
    assert fake.ready_calls == 1


def test_flat_fallback_keeps_the_claims_unknown_contract() -> None:
    """A server old enough to lack the frontier tools may also ignore
    ``with_claims``; a row whose claims came back None must still read
    "claims unknown", never a confident "unclaimed"."""
    fake = _FrontierFake(
        open_tasks=[_task("u", claims=None)],
        ready=[],
        blocked=[],
        ready_error=_tool_missing(READY_TOOL),
        blocked_error=_tool_missing(BLOCKED_TOOL),
        tool_names=_PRE_GRAPH_TOOLS,
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    (row,) = data.sections["open"]
    assert row.claim_state == "unknown"


def test_known_missing_frontier_skips_the_frontier_calls() -> None:
    """``graph_available=False`` from the caller (the process probed recently
    and found the tools missing) skips both frontier reads instead of buying
    two guaranteed failures per render."""
    fake = _FrontierFake(
        open_tasks=[_task("u", claims=())],
        ready=[],
        blocked=[],
        ready_error=_tool_missing(READY_TOOL),
        blocked_error=_tool_missing(BLOCKED_TOOL),
        tool_names=_PRE_GRAPH_TOOLS,
    )

    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=500, graph_available=False
        )
    )

    assert fake.ready_calls == 0
    assert fake.blocked_calls == 0
    assert fake.tool_list_calls == 0
    assert data.graph_available is False
    assert _section_ids(data.sections, "open") == ["u"]
    # Regression (security f-001): the degraded state is reported on EVERY
    # render it applies to, not only on the one that discovered it — otherwise
    # most refreshes inside the re-probe window show no error at all.
    assert any("frontier tools" in message for message in data.errors)


def test_frontier_outage_renders_flat_without_the_version_story() -> None:
    """A transient outage renders the master open list FLAT with the read error
    (§14), and is still not the missing-tools fallback.

    Two separate contracts meet here. The rows go flat because half a frontier
    is not a classification. But ``graph_available`` stays True and the error
    names the failing read, because blanking Ready/Blocked behind "your Lithos
    is too old" would hide a real problem — and would cost the caller its graph
    verdict for the whole re-probe window.
    """
    fake = _FrontierFake(
        open_tasks=[_task("r", claims=())],
        ready=[],
        blocked=[],
        ready_error=RuntimeError("connection reset"),
        blocked_error=RuntimeError("connection reset"),
    )

    data = asyncio.run(load_dashboard(fake, filters=_FILTERS, frontier_limit=500))

    assert data.graph_available is True
    assert data.open_flat is True
    assert _section_ids(data.sections, "open") == ["r"]
    assert _section_ids(data.sections, "unclassified") == []
    assert any("ready frontier" in message for message in data.errors)
    assert FRONTIER_UNAVAILABLE_ERROR not in data.errors
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
    """Rollup counts are whole-subtree facts: a tag filter that hides the
    descendants from the sections must not change the chip."""
    epic = _epic("epic-1")
    tagged = _task("in-ready", claims=(), tags=("project:mine",))
    fake = _FrontierFake(
        open_tasks=[epic, tagged],
        ready=[tagged],
        blocked=[],
        children={"epic-1": _subtree(done=5, open_=3)},
    )
    filters = TaskFilters(
        statuses=("open",), tags=("project:other",), agent="", since=""
    )
    data = asyncio.run(load_dashboard(fake, filters=filters, frontier_limit=500))

    assert _section_ids(data.sections, "ready") == []
    assert [rollup.progress_label for rollup in data.epics] == ["5/8"]


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
