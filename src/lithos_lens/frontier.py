"""Frontier join: partition the open task set into workable sections.

The graph-native dashboard never re-derives readiness — Lithos owns the
ready/blocked frontier (``lithos_task_ready`` / ``lithos_task_blocked`` evaluate
gate and timer state at query time). Lens joins the id-sets those calls return
against the master open list: a workable open task is *In progress* when it
carries an inline claim, else *Ready* or *Blocked* by frontier membership, else
*Not classified* (only reachable under frontier-limit truncation). Epics and
gates are excluded from both frontiers upstream, so they never enter the
workable partition; their dedicated sections arrive in later T1 slices.

``classify_open_tasks`` is the pure join; ``load_dashboard`` is the five-call
assembly that feeds it. Both live here (not in ``tasks.py``) because they depend
on the task-graph records in ``task_graph.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, cast

from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord
from lithos_lens.tasks import (
    AgentRecord,
    BlockerChip,
    ClaimRecord,
    DashboardData,
    SectionRow,
    TaskFilters,
    TaskRecord,
    TaskStatusName,
    TaskSummary,
    int_stat,
    matches_filters,
)

# Only ``task``-typed rows are workable. Epics and gates roll up / gate elsewhere
# and are excluded from both Lithos frontiers, so they never classify here.
WORKABLE_TASK_TYPE = "task"

# The three workable sections plus the truncation tail, in render order.
WORKABLE_SECTIONS: tuple[str, ...] = ("in_progress", "ready", "blocked")


class FrontierLithosClient(Protocol):
    """The client surface ``load_dashboard`` consumes (the five parallel calls)."""

    async def list_tasks(
        self,
        *,
        agent: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
        with_claims: bool = False,
    ) -> list[TaskRecord]: ...

    async def task_ready(
        self,
        *,
        limit: int | None = None,
        with_claims: bool = False,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[TaskRecord]: ...

    async def task_blocked(
        self,
        *,
        limit: int | None = None,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[BlockedTaskRecord]: ...

    async def stats(self) -> dict[str, Any]: ...

    async def list_agents(self) -> list[AgentRecord]: ...


def _blocker_chips(
    blockers: Sequence[BlockerRecord],
    index: Mapping[str, TaskRecord],
) -> tuple[BlockerChip, ...]:
    """Render a blocked row's structured blockers as display chips.

    Each chip's label is the blocking task's title when it resolves against the
    open snapshot; otherwise a legible fallback (the predecessor/gate id, or the
    raw blocker message) so a chip is never blank. Gate-name/type polish and the
    unsatisfiable/cycle promotion out of Blocked are later T1 slices.
    """
    chips: list[BlockerChip] = []
    for blocker in blockers:
        predecessor = index.get(blocker.task_id)
        if predecessor is not None:
            label = predecessor.title
        else:
            label = blocker.task_id or blocker.message or blocker.kind
        chips.append(
            BlockerChip(label=label, kind=blocker.kind, target_id=blocker.task_id)
        )
    return tuple(chips)


def classify_open_tasks(
    open_tasks: Sequence[TaskRecord],
    *,
    ready_ids: set[str],
    blocked: Sequence[BlockedTaskRecord],
    index: Mapping[str, TaskRecord] | None = None,
) -> dict[str, tuple[SectionRow, ...]]:
    """Join the master open list against the ready/blocked frontier.

    Returns the three workable sections plus ``unclassified``. ``index`` (id →
    task) resolves blocker-chip titles; it defaults to ``open_tasks`` but a
    caller can pass the unfiltered snapshot so a filtered-out predecessor still
    names its chip. Readiness is taken verbatim from ``ready_ids`` / ``blocked``
    — never recomputed.
    """
    resolve = index if index is not None else {task.id: task for task in open_tasks}
    blocked_map = {record.task.id: record.blockers for record in blocked}
    buckets: dict[str, list[SectionRow]] = {
        "in_progress": [],
        "ready": [],
        "blocked": [],
        "unclassified": [],
    }
    for task in open_tasks:
        if task.task_type != WORKABLE_TASK_TYPE:
            continue
        claims: tuple[ClaimRecord, ...] = task.claims or ()
        chips = _blocker_chips(blocked_map.get(task.id, ()), resolve)
        if claims:
            # In progress wins over ready/blocked: a claim means an agent is on
            # it. A claim on a blocked row is the anomaly flagged inline.
            buckets["in_progress"].append(
                SectionRow(
                    task=task,
                    claims=claims,
                    blockers=chips,
                    claimed_but_blocked=task.id in blocked_map,
                )
            )
        elif task.id in ready_ids:
            buckets["ready"].append(SectionRow(task=task))
        elif task.id in blocked_map:
            buckets["blocked"].append(SectionRow(task=task, blockers=chips))
        else:
            # Only reachable when the frontier calls truncated at their limit.
            buckets["unclassified"].append(SectionRow(task=task))
    return {section: tuple(rows) for section, rows in buckets.items()}


async def load_dashboard(
    lithos: FrontierLithosClient,
    *,
    filters: TaskFilters,
    frontier_limit: int,
) -> DashboardData:
    """Assemble the dashboard from the parallel Lithos reads.

    Five calls fan out concurrently: the master open list (claims inline), the
    ready and blocked frontiers, and the recently-resolved completed/cancelled
    windows. Stats and the agent list ride along for the header and filter
    dropdown.
    """
    errors: list[str] = []
    query_tags = list(filters.tags) or None
    query_agent = filters.agent or None

    (
        open_result,
        ready_result,
        blocked_result,
        stats_result,
        agents_result,
    ) = await asyncio.gather(
        # The master open set is the whole open list (PRD data contract):
        # NO agent/tag/since filter is pushed. Open tasks are the live frontier,
        # not a time window (``since`` scopes only the resolved completed/
        # cancelled sections), and filtering client-side keeps one fetch serving
        # every projection while leaving the snapshot whole — so a blocker chip
        # can resolve a predecessor's title even when that predecessor is itself
        # filtered out of, or older than, the visible sections.
        lithos.list_tasks(status="open", with_claims=True),
        lithos.task_ready(limit=frontier_limit, with_claims=False),
        lithos.task_blocked(limit=frontier_limit),
        lithos.stats(),
        lithos.list_agents(),
        return_exceptions=True,
    )

    async def load_closed(status: TaskStatusName) -> list[TaskRecord]:
        return await lithos.list_tasks(
            agent=query_agent,
            status=status,
            tags=query_tags,
            since=filters.since,
        )

    closed_results = await asyncio.gather(
        load_closed("completed"),
        load_closed("cancelled"),
        return_exceptions=True,
    )

    # The full open snapshot (unfiltered by agent/tag) resolves blocker-chip
    # predecessor titles even when a predecessor is filtered out of the visible
    # sections; ``visible_open`` is the agent/tag-filtered subset that the
    # sections render.
    open_snapshot = _sorted_or_error("open", open_result, errors)
    open_index = {task.id: task for task in open_snapshot}
    visible_open = [
        task
        for task in open_snapshot
        if matches_filters(task, filters=filters, status="open")
    ]

    ready_ids: set[str] = set()
    if isinstance(ready_result, BaseException):
        errors.append("Could not load the ready frontier.")
    else:
        ready_ids = {task.id for task in cast(list[TaskRecord], ready_result)}

    blocked_records: list[BlockedTaskRecord] = []
    if isinstance(blocked_result, BaseException):
        errors.append("Could not load the blocked frontier.")
    else:
        blocked_records = cast(list[BlockedTaskRecord], blocked_result)

    closed: dict[str, list[TaskRecord]] = {}
    for status, result in zip(("completed", "cancelled"), closed_results, strict=True):
        closed[status] = _rows_for(status, result, filters, errors)

    partition = classify_open_tasks(
        visible_open,
        ready_ids=ready_ids,
        blocked=blocked_records,
        index=open_index,
    )

    show_open = "open" in filters.statuses
    sections: dict[str, tuple[SectionRow, ...]] = {}
    for section in WORKABLE_SECTIONS + ("unclassified",):
        sections[section] = partition[section] if show_open else ()
    for status in ("completed", "cancelled"):
        sections[status] = (
            tuple(SectionRow(task=task) for task in closed[status])
            if status in filters.statuses
            else ()
        )

    stats: dict[str, Any] = {}
    if isinstance(stats_result, BaseException):
        errors.append("Could not load Lithos stats.")
    else:
        stats = cast(dict[str, Any], stats_result)

    agents: tuple[AgentRecord, ...] = ()
    if isinstance(agents_result, BaseException):
        errors.append("Could not load agent list.")
    else:
        agents = tuple(cast(list[AgentRecord], agents_result))

    open_total = sum(len(partition[section]) for section in WORKABLE_SECTIONS) + len(
        partition["unclassified"]
    )
    summary = TaskSummary(
        in_progress=len(partition["in_progress"]),
        ready=len(partition["ready"]),
        blocked=len(partition["blocked"]),
        unclassified=len(partition["unclassified"]),
        open_total=open_total,
        open_claims=int_stat(stats, "open_claims"),
        recent_completed=len(closed["completed"]),
        recent_cancelled=len(closed["cancelled"]),
        agents=int_stat(stats, "agents", default=len(agents)),
    )
    return DashboardData(
        filters=filters,
        summary=summary,
        sections=sections,
        agents=agents,
        frontier_limit=frontier_limit,
        open_total=open_total,
        truncated=bool(partition["unclassified"]),
        errors=tuple(errors),
    )


def _sorted_or_error(
    status: TaskStatusName,
    result: list[TaskRecord] | BaseException,
    errors: list[str],
) -> list[TaskRecord]:
    """Sort one status group newest-first, recording a load error if any.

    No filtering — the open snapshot must stay whole so it can resolve blocker
    chips; the caller applies ``matches_filters`` to pick the visible subset.
    """
    if isinstance(result, BaseException):
        errors.append(f"Could not load {status} tasks.")
        return []
    return sorted(
        cast(list[TaskRecord], result),
        key=lambda task: task.created_at,
        reverse=True,
    )


def _rows_for(
    status: TaskStatusName,
    result: list[TaskRecord] | BaseException,
    filters: TaskFilters,
    errors: list[str],
) -> list[TaskRecord]:
    """Filter + sort one status group's rows, recording a load error if any."""
    if isinstance(result, BaseException):
        errors.append(f"Could not load {status} tasks.")
        return []
    rows = [
        task
        for task in cast(list[TaskRecord], result)
        if matches_filters(task, filters=filters, status=status)
    ]
    return sorted(rows, key=lambda task: task.created_at, reverse=True)
