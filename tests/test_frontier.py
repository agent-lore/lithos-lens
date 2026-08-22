"""T1 slice 2 — frontier join (Ready / In progress / Blocked sections).

``classify_open_tasks`` is the pure join between the master open list and the
Lithos ready/blocked frontier. These tests pin every classification branch and
the blocker-chip resolution; readiness is supplied explicitly (the fake oracle
pattern) because Lens must never recompute it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from lithos_lens.frontier import (
    build_epic_rollup,
    classify_open_tasks,
    load_dashboard,
)
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord
from lithos_lens.tasks import (
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
    status: TaskStatusName = "open",
) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        title=f"Title {task_id}",
        status=status,
        task_type=task_type,
        tags=tags,
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
    ) -> None:
        self._open_seq = self._as_sequence(open_tasks)
        self._ready_seq = self._as_sequence(ready)
        self._blocked_seq = self._as_sequence(blocked)
        self._completed = completed or []
        self._cancelled = cancelled or []
        self._fail_ready = fail_ready
        self._children = children or {}
        self._fail_children = fail_children or set()
        self.open_calls = 0
        self.ready_calls = 0
        self.blocked_calls = 0
        self.children_calls: list[dict[str, Any]] = []

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
        with_claims: bool = False,
    ) -> list[TaskRecord]:
        # Mirror the real server: agent/tag are filtered upstream when passed.
        if (status or "open") == "open":
            index = min(self.open_calls, len(self._open_seq) - 1)
            self.open_calls += 1
            rows = self._open_seq[index]
        else:
            rows = {
                "completed": self._completed,
                "cancelled": self._cancelled,
            }[status or "open"]
        if agent:
            rows = [task for task in rows if task.created_by == agent]
        if tags:
            rows = [task for task in rows if all(tag in task.tags for tag in tags)]
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
        if task_id in self._fail_children:
            raise RuntimeError(f"children unavailable for {task_id}")
        rows = self._children.get(task_id, [])
        if not include_closed:
            rows = [task for task in rows if task.status == "open"]
        return rows

    async def stats(self) -> dict[str, Any]:
        return {"open_claims": 2, "agents": 3}

    async def list_agents(self) -> list[AgentRecord]:
        return [AgentRecord(id="a"), AgentRecord(id="b")]


# Comfortably above every fixture's epic count: the cap is exercised by its own
# test, not incidentally by the rest of the suite.
_EPIC_CAP = 50

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
    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )
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
    data = asyncio.run(
        load_dashboard(
            fake, filters=filters, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

    assert _section_ids(data.sections, "blocked") == ["blk"]
    (row,) = data.sections["blocked"]
    assert row.blockers[0].label == "Title pred"
    assert row.blockers[0].target_id == "pred"
    # The predecessor itself is filtered out of the visible sections.
    assert _section_ids(data.sections, "ready") == []


def test_load_dashboard_frontier_error_is_not_reported_as_truncation() -> None:
    """Regression (f-002): a failed frontier read leaves rows unclassified, but
    that is an error (surfaced by the banner), NOT frontier-limit truncation —
    ``truncated`` must stay False so the dashboard doesn't claim a false cap."""
    ready = _task("r", claims=())
    fake = _FrontierFake(open_tasks=[ready], ready=[ready], blocked=[], fail_ready=True)
    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )
    # The ready call failed, so the row can't be placed on the frontier.
    assert _section_ids(data.sections, "unclassified") == ["r"]
    assert data.truncated is False
    assert any("ready frontier" in message for message in data.errors)


def test_load_dashboard_flags_truncation_only_at_the_limit() -> None:
    """Truncation means a frontier response actually HIT frontier_limit: with
    limit=1 and two ready-able rows the ready read returns one row, the other
    lands in the Not-classified tail, and that tail IS truncation."""
    r1 = _task("r1", claims=())
    r2 = _task("r2", claims=())
    fake = _FrontierFake(open_tasks=[r1, r2], ready=[r1, r2], blocked=[])
    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=1, epic_strip_cap=_EPIC_CAP
        )
    )

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
    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

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
    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

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
    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

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
    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

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
    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

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
    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

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
    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=1, epic_strip_cap=_EPIC_CAP
        )
    )

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
    data = asyncio.run(
        load_dashboard(
            fake, filters=filters, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

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
    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

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
    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

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
    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

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
    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

    assert fake.ready_calls == 1
    assert _section_ids(data.sections, "claims_unknown") == ["x"]
    assert _section_ids(data.sections, "ready") == []
    assert _section_ids(data.sections, "blocked") == []
    assert data.reconciliation_pending is False


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
    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

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
    data = asyncio.run(
        load_dashboard(
            fake, filters=filters, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

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
    data = asyncio.run(
        load_dashboard(
            fake, filters=filters, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

    assert _section_ids(data.sections, "ready") == []
    assert [rollup.progress_label for rollup in data.epics] == ["5/8"]


def test_load_dashboard_ignores_a_scope_that_is_no_longer_an_open_epic() -> None:
    """A stale ``?epic=`` bookmark (the epic completed, or the id is junk)
    resolves to NO scope — the full board with ``epic_scope`` empty, so the
    template can explain it — rather than an unexplained empty page."""
    ready = _task("r", claims=())
    fake = _FrontierFake(open_tasks=[ready], ready=[ready], blocked=[])
    filters = replace(_FILTERS, epic="epic-gone")
    data = asyncio.run(
        load_dashboard(
            fake, filters=filters, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

    assert data.epics == ()
    assert data.epic_scope == ""
    assert _section_ids(data.sections, "ready") == ["r"]
    assert data.errors == ()


def test_an_empty_subtree_never_scopes_the_board() -> None:
    """Reviewer repro (c-001), lifecycle race: the epic is in the open snapshot
    but ``task_children`` — issued after it — answers empty, which is exactly
    what the contract returns for an epic that closed in between. Lens cannot
    tell that from a childless epic, so it must NOT resolve a scope: the board
    stays whole with ``epic_scope`` empty (the announced fallback) instead of
    silently rendering an empty scoped page."""
    epic = _epic("epic-1")
    ready = _task("r", claims=())
    fake = _FrontierFake(
        open_tasks=[epic, ready], ready=[ready], blocked=[], children={"epic-1": []}
    )
    filters = replace(_FILTERS, epic="epic-1")
    data = asyncio.run(
        load_dashboard(
            fake, filters=filters, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

    assert data.epic_scope == ""
    assert _section_ids(data.sections, "ready") == ["r"]
    assert data.summary.open_total == 1
    # The chip still renders (0/0 is honest about what the read returned), it
    # just is not the active scope.
    assert [rollup.progress_label for rollup in data.epics] == ["0/0"]
    assert not any(rollup.selected for rollup in data.epics)


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
    data = asyncio.run(
        load_dashboard(
            fake, filters=filters, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

    assert data.epic_scope == "epic-1"
    assert [rollup.progress_label for rollup in data.epics] == ["1/1"]
    # The row still renders from the open snapshot — the newer count did not
    # move it anywhere.
    assert _section_ids(data.sections, "ready") == ["in"]


def test_epic_fan_out_stops_at_the_cap_and_reports_the_remainder() -> None:
    """The strip is the one read whose fan-out follows the corpus, so it is
    capped — and the surplus is COUNTED, never silently dropped."""
    epics = [_epic(f"epic-{n}") for n in range(4)]
    ready = _task("r", claims=())
    fake = _FrontierFake(
        open_tasks=[*epics, ready],
        ready=[ready],
        blocked=[],
        children={epic.id: _subtree(done=1, open_=1) for epic in epics},
    )
    data = asyncio.run(
        load_dashboard(fake, filters=_FILTERS, frontier_limit=500, epic_strip_cap=2)
    )

    assert [rollup.task.id for rollup in data.epics] == ["epic-0", "epic-1"]
    assert data.epics_omitted == 2
    # The cap bounds the backend calls, not just what renders.
    assert len(fake.children_calls) == 2


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
    data = asyncio.run(
        load_dashboard(
            fake, filters=filters, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

    assert data.summary.in_progress == 1
    assert data.summary.active_claims == 1

    unscoped = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )
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
    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

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
    data = asyncio.run(
        load_dashboard(
            fake, filters=_FILTERS, frontier_limit=500, epic_strip_cap=_EPIC_CAP
        )
    )

    assert fake.open_calls == 2
    assert [rollup.task.id for rollup in data.epics] == ["epic-new"]
    assert [rollup.progress_label for rollup in data.epics] == ["5/8"]
    assert [call["task_id"] for call in fake.children_calls] == [
        "epic-old",
        "epic-new",
    ]
