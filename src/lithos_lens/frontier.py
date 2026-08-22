"""Frontier join: partition the open task set into workable sections.

The graph-native dashboard never re-derives readiness — Lithos owns the
ready/blocked frontier (``lithos_task_ready`` / ``lithos_task_blocked`` evaluate
gate and timer state at query time). Lens joins the id-sets those calls return
against the master open list: a workable open task is *In progress* when it
carries an inline claim, else *Ready* or *Blocked* by frontier membership, else
*Not classified* (only reachable under frontier-limit truncation). Epics and
gates are excluded from both frontiers upstream, so they never enter the
workable partition; their dedicated sections arrive in later T1 slices.

Open epics roll up instead: one ``lithos_task_children`` fan-out per open epic
builds the progress chips of the epic strip (``build_epic_rollup``), and the
selected chip's descendant set scopes every section (``?epic=``).

``classify_open_tasks`` is the pure join; ``load_dashboard`` is the five-call
assembly that feeds it. Both live here (not in ``tasks.py``) because they depend
on the task-graph records in ``task_graph.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, Protocol, cast

from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord
from lithos_lens.tasks import (
    AgentRecord,
    BlockerChip,
    ClaimRecord,
    DashboardData,
    EpicRollup,
    SectionName,
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

# Epics roll up into the strip (one progress chip each) rather than appearing
# in any section; the strip is built from their recursive children.
EPIC_TASK_TYPE = "epic"

# The three workable sections plus the truncation tail, in render order.
WORKABLE_SECTIONS: tuple[SectionName, ...] = ("in_progress", "ready", "blocked")


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

    async def task_children(
        self,
        task_id: str,
        *,
        recursive: bool = False,
        include_closed: bool = False,
    ) -> list[TaskRecord]: ...

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
) -> dict[SectionName, tuple[SectionRow, ...]]:
    """Join the master open list against the ready/blocked frontier.

    Returns the three workable sections plus ``unclassified``. ``index`` (id →
    task) resolves blocker-chip titles; it defaults to ``open_tasks`` but a
    caller can pass the unfiltered snapshot so a filtered-out predecessor still
    names its chip. Readiness is taken verbatim from ``ready_ids`` / ``blocked``
    — never recomputed.
    """
    resolve = index if index is not None else {task.id: task for task in open_tasks}
    blocked_map = {record.task.id: record.blockers for record in blocked}
    buckets: dict[SectionName, list[SectionRow]] = {
        "in_progress": [],
        "ready": [],
        "blocked": [],
        "claims_unknown": [],
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
        elif task.claims is None:
            # TaskRecord.claims: None means claims were NOT returned even
            # though requested — the task might belong in In progress, so it
            # must not sit in the Ready ("unclaimed and workable now") or
            # Blocked counts. It renders in the dedicated claims-unknown group
            # (visible, flagged, blocker chips kept for context) — the same
            # degraded-data treatment as the read-skew rows. The bucket does
            # not depend on frontier membership, so these rows never feed the
            # truncation or skew signals either.
            buckets["claims_unknown"].append(
                SectionRow(task=task, blockers=chips, claims_unknown=True)
            )
        elif task.id in ready_ids:
            buckets["ready"].append(SectionRow(task=task))
        elif task.id in blocked_map:
            buckets["blocked"].append(SectionRow(task=task, blockers=chips))
        else:
            # A workable open task in neither frontier set. With healthy reads
            # this only happens under frontier-limit truncation; ``load_dashboard``
            # decides whether to label it truncation (vs. a failed frontier read).
            buckets["unclassified"].append(SectionRow(task=task))
    return {section: tuple(rows) for section, rows in buckets.items()}


def build_epic_rollup(
    epic: TaskRecord,
    children: Sequence[TaskRecord],
) -> EpicRollup:
    """Roll one epic's recursive children up into its progress chip.

    ``children`` is the ``lithos_task_children(recursive=True,
    include_closed=True)`` answer, so it is the whole subtree in every status.
    Progress counts only WORKABLE descendants and drops cancelled ones from the
    denominator — see :class:`~lithos_lens.tasks.EpicRollup` for why — while the
    scope set keeps EVERY descendant id, whatever its type or status.
    """
    workable = [task for task in children if task.task_type == WORKABLE_TASK_TYPE]
    done = sum(1 for task in workable if task.status == "completed")
    cancelled = sum(1 for task in workable if task.status == "cancelled")
    return EpicRollup(
        task=epic,
        done=done,
        total=len(workable) - cancelled,
        cancelled=cancelled,
        descendant_ids=frozenset(task.id for task in children if task.id),
    )


async def _load_epic_rollups(
    lithos: FrontierLithosClient,
    snapshot: Sequence[TaskRecord],
    *,
    selected: str,
) -> tuple[tuple[EpicRollup, ...], bool]:
    """Fan ``lithos_task_children`` out over every open epic in the snapshot.

    One recursive, closed-inclusive call per epic, all gathered — the epic
    count is small (~20 in production) and the calls are independent. This is
    the one read that cannot join the main gather: the epic ids come from the
    open list it fetches.

    Returns the rollups in snapshot order (marking the ``?epic=`` selection)
    plus a flag set when ANY call failed. A failed epic is dropped from the
    strip rather than shown with a wrong count; the caller turns the flag into
    the usual load-error banner.
    """
    epics = [task for task in snapshot if task.task_type == EPIC_TASK_TYPE]
    if not epics:
        return (), False
    results = await asyncio.gather(
        *(
            lithos.task_children(epic.id, recursive=True, include_closed=True)
            for epic in epics
        ),
        return_exceptions=True,
    )
    rollups: list[EpicRollup] = []
    failed = False
    for epic, result in zip(epics, results, strict=True):
        if isinstance(result, BaseException):
            failed = True
            continue
        rollup = build_epic_rollup(epic, cast(list[TaskRecord], result))
        rollups.append(replace(rollup, selected=bool(selected) and epic.id == selected))
    return tuple(rollups), failed


async def load_dashboard(
    lithos: FrontierLithosClient,
    *,
    filters: TaskFilters,
    frontier_limit: int,
) -> DashboardData:
    """Assemble the dashboard from the parallel Lithos reads.

    All seven independent reads fan out in ONE gather: the master open list
    (claims inline), the ready and blocked frontiers, stats, the agent list,
    and the recently-resolved completed/cancelled windows. The epic-strip
    children fan-out follows as a second (internally parallel) round-trip
    because its epic ids come from the open list; the only other extra
    round-trip is the read-skew retry below — and only when the first pair of
    frontier responses is inconsistent below the limit.

    The open/ready/blocked calls are independent reads, not a snapshot, so a
    workable open task can be missing from both frontier lists (or present in
    both) through pure read-skew. Policy: a frontier response that actually
    hit ``frontier_limit`` is truncation; an inconsistency BELOW the limit
    retries the ready+blocked pair once, and if it persists the affected rows
    classify conservatively as Blocked with the reconciliation-warning surface
    (wrongly-Ready invites wasted operator attention; wrongly-Blocked is safe)
    — the same explicit degraded-data pattern as ``claims_unknown``.
    """
    errors: list[str] = []
    query_tags = list(filters.tags) or None
    query_agent = filters.agent or None

    async def load_closed(status: TaskStatusName) -> list[TaskRecord]:
        return await lithos.list_tasks(
            agent=query_agent,
            status=status,
            tags=query_tags,
            since=filters.since,
        )

    (
        open_result,
        ready_result,
        blocked_result,
        stats_result,
        agents_result,
        completed_result,
        cancelled_result,
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
        load_closed("completed"),
        load_closed("cancelled"),
        return_exceptions=True,
    )
    # gather() overloads stop narrowing positionally beyond five results, so
    # the per-slot unions are reasserted here.
    closed_results = (
        cast("list[TaskRecord] | BaseException", completed_result),
        cast("list[TaskRecord] | BaseException", cancelled_result),
    )

    # The full open snapshot (unfiltered by agent/tag) resolves blocker-chip
    # predecessor titles even when a predecessor is filtered out of the visible
    # sections; ``visible_open`` is the agent/tag-filtered subset that the
    # sections render.
    open_snapshot = _sorted_or_error(
        "open", cast("list[TaskRecord] | BaseException", open_result), errors
    )

    ready_list: list[TaskRecord] = []
    frontier_ok = True
    if isinstance(ready_result, BaseException):
        errors.append("Could not load the ready frontier.")
        frontier_ok = False
    else:
        ready_list = cast(list[TaskRecord], ready_result)

    blocked_records: list[BlockedTaskRecord] = []
    if isinstance(blocked_result, BaseException):
        errors.append("Could not load the blocked frontier.")
        frontier_ok = False
    else:
        blocked_records = cast(list[BlockedTaskRecord], blocked_result)

    # Raw terminal ids (pre-filter): a task appearing in BOTH the open
    # snapshot and a terminal result is freshness skew worth a retry, whether
    # or not the terminal row survives the section filters.
    terminal_ids: set[str] = set()
    for closed_result in closed_results:
        if not isinstance(closed_result, BaseException):
            terminal_ids.update(task.id for task in closed_result)

    def _partition_state(
        snapshot: list[TaskRecord],
        ready_rows: list[TaskRecord],
        blocked_rows: list[BlockedTaskRecord],
        scope_ids: frozenset[str] | None,
    ) -> _FrontierState:
        index = {task.id: task for task in snapshot}
        visible = [
            task
            for task in snapshot
            if matches_filters(
                task, filters=filters, status="open", scope_ids=scope_ids
            )
        ]
        ready_ids = {task.id for task in ready_rows}
        overlap = ready_ids & {record.task.id for record in blocked_rows}
        # Truncation is a fact about the response size, not an inference from
        # gaps: a frontier read that returned frontier_limit rows hit its cap.
        at_limit = (
            len(ready_rows) >= frontier_limit or len(blocked_rows) >= frontier_limit
        )
        parts = classify_open_tasks(
            visible,
            ready_ids=ready_ids,
            blocked=blocked_rows,
            index=index,
        )
        # Only an overlap that changes what RENDERS is a contradiction worth
        # the warning: a filtered-out task is in no section, and a
        # claims-unknown row stays in its degraded group whatever the
        # frontiers say. A would-be-Ready overlap AND a claimed overlap (the
        # blocked membership decorates the In-progress row with
        # claimed_but_blocked + chips) both count — and they are skew
        # REGARDLESS of the limit: the same id in both responses is
        # contradictory even at the cap.
        rendered_overlap_ids = {row.task.id for row in parts["ready"]} | {
            row.task.id for row in parts["in_progress"]
        }
        effective_overlap = overlap & rendered_overlap_ids
        skewed_frontier = bool(effective_overlap) or (
            not at_limit and bool(parts["unclassified"])
        )
        # An id in both the open snapshot and a terminal result is freshness
        # skew: it drives the retry (the later snapshot then arbitrates via
        # the closed dedup) but not the moved-to-Blocked surface.
        terminal_overlap = terminal_ids & set(index)
        return _FrontierState(
            snapshot,
            index,
            parts,
            effective_overlap,
            at_limit,
            skewed_frontier,
            skewed_frontier or bool(terminal_overlap),
        )

    # The epic strip depends on the open snapshot (its epic ids), so it is
    # fetched here rather than in the main gather — and refetched below if the
    # skew retry adopts a newer snapshot, so strip and sections never mix
    # generations.
    epics, epic_error = await _load_epic_rollups(
        lithos, open_snapshot, selected=filters.epic
    )
    scope_ids = _epic_scope_ids(epics)
    state = _partition_state(open_snapshot, ready_list, blocked_records, scope_ids)

    if frontier_ok and state.retry_worthy:
        # Read-skew between independent reads (a would-be-Ready task also in
        # the blocked response, or a below-limit frontier gap). Retry ALL
        # THREE reads together — the master open list too, or a task that
        # closed after the stale open read would keep rendering in an open
        # section alongside its terminal row. Adopt the retried generation
        # only when every read succeeds (no mixed generations); a persisting
        # disagreement is handled conservatively below rather than trusted.
        retry_open, retry_ready, retry_blocked = await asyncio.gather(
            lithos.list_tasks(status="open", with_claims=True),
            lithos.task_ready(limit=frontier_limit, with_claims=False),
            lithos.task_blocked(limit=frontier_limit),
            return_exceptions=True,
        )
        if not (
            isinstance(retry_open, BaseException)
            or isinstance(retry_ready, BaseException)
            or isinstance(retry_blocked, BaseException)
        ):
            open_snapshot = sorted(
                cast(list[TaskRecord], retry_open),
                key=lambda task: task.created_at,
                reverse=True,
            )
            ready_list = cast(list[TaskRecord], retry_ready)
            blocked_records = cast(list[BlockedTaskRecord], retry_blocked)
            epics, epic_error = await _load_epic_rollups(
                lithos, open_snapshot, selected=filters.epic
            )
            scope_ids = _epic_scope_ids(epics)
            state = _partition_state(
                open_snapshot, ready_list, blocked_records, scope_ids
            )

    if epic_error:
        errors.append("Could not load epic progress.")

    partition = state.partition
    at_limit = state.at_limit
    open_index = state.index
    reconciliation_pending = frontier_ok and state.skewed_frontier
    if reconciliation_pending:
        partition = _reclassify_conservative(partition, state.effective_overlap)

    closed: dict[str, list[TaskRecord]] = {}
    for status, result in zip(("completed", "cancelled"), closed_results, strict=True):
        rows = _rows_for(status, result, filters, errors, scope_ids)
        # Exactly-one-row dedup: the (retried) master-open snapshot is the
        # authority on openness. A task still in the final open snapshot
        # renders in its open section only; a task absent from it renders in
        # its terminal section only (that direction is structural — the open
        # sections derive from the snapshot).
        closed[status] = [task for task in rows if task.id not in open_index]

    show_open = "open" in filters.statuses
    sections: dict[SectionName, tuple[SectionRow, ...]] = {}
    for section in WORKABLE_SECTIONS + ("claims_unknown", "unclassified"):
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

    open_total = (
        sum(len(partition[section]) for section in WORKABLE_SECTIONS)
        + len(partition["claims_unknown"])
        + len(partition["unclassified"])
    )
    summary = TaskSummary(
        in_progress=len(partition["in_progress"]),
        ready=len(partition["ready"]),
        blocked=len(partition["blocked"]),
        claims_unknown=len(partition["claims_unknown"]),
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
        # Truncation means a frontier response actually HIT frontier_limit,
        # leaving otherwise-classifiable rows in the tail. Unclassified rows
        # from a failed frontier read are surfaced by the error banner, and a
        # below-limit gap is read-skew (handled by the retry + conservative
        # Blocked above) — neither is mislabelled as truncation.
        truncated=frontier_ok and at_limit and bool(partition["unclassified"]),
        reconciliation_pending=reconciliation_pending,
        errors=tuple(errors),
        epics=epics,
        # An ``?epic=`` naming something that is no longer an open epic (a
        # stale bookmark, or an epic that just completed) resolves to NO scope:
        # the board shows everything and the template says why, rather than
        # rendering an unexplained empty page.
        epic_scope=filters.epic if scope_ids is not None else "",
    )


class _FrontierState:
    """One generation of the joined reads: snapshot views + skew verdicts.

    ``skewed_frontier`` drives the conservative reclassification + banner;
    ``retry_worthy`` additionally includes the open∩terminal freshness
    overlap, which only warrants the retry (the later snapshot then
    arbitrates via the closed dedup).
    """

    __slots__ = (
        "snapshot",
        "index",
        "partition",
        "effective_overlap",
        "at_limit",
        "skewed_frontier",
        "retry_worthy",
    )

    def __init__(
        self,
        snapshot: list[TaskRecord],
        index: Mapping[str, TaskRecord],
        partition: dict[SectionName, tuple[SectionRow, ...]],
        effective_overlap: set[str],
        at_limit: bool,
        skewed_frontier: bool,
        retry_worthy: bool,
    ) -> None:
        self.snapshot = snapshot
        self.index = index
        self.partition = partition
        self.effective_overlap = effective_overlap
        self.at_limit = at_limit
        self.skewed_frontier = skewed_frontier
        self.retry_worthy = retry_worthy


def _reclassify_conservative(
    partition: dict[SectionName, tuple[SectionRow, ...]],
    overlap: set[str],
) -> dict[SectionName, tuple[SectionRow, ...]]:
    """Apply the conservative read-skew interpretation, flagged for the banner.

    Unclaimed overlap rows (in BOTH frontier responses) leave Ready — the
    ready-first classify branch had placed them there — and every unclassified
    row joins them in Blocked: wrongly-Ready invites an operator to start work
    that may be blocked; wrongly-Blocked merely defers attention. CLAIMED
    overlap rows stay In progress with their blocked decoration kept, marked
    awaiting reconciliation.
    """
    moved = [
        replace(row, reconciliation_pending=True)
        for row in partition["ready"]
        if row.task.id in overlap
    ] + [replace(row, reconciliation_pending=True) for row in partition["unclassified"]]
    # A claimed overlap row stays In progress (the claim wins the section) and
    # KEEPS its blocked decoration — the conservative interpretation — but is
    # marked awaiting reconciliation so the badge/banner explain that the
    # decoration may be read-skew rather than real blockage.
    in_progress = tuple(
        replace(row, reconciliation_pending=True) if row.task.id in overlap else row
        for row in partition["in_progress"]
    )
    return {
        **partition,
        "in_progress": in_progress,
        "ready": tuple(row for row in partition["ready"] if row.task.id not in overlap),
        "blocked": partition["blocked"] + tuple(moved),
        "unclassified": (),
    }


def _epic_scope_ids(epics: Sequence[EpicRollup]) -> frozenset[str] | None:
    """The selected epic's descendant ids, or ``None`` when nothing is scoped.

    ``None`` (no scope) and an empty frozenset (an epic with no descendants)
    are deliberately different answers — see ``matches_filters``.
    """
    return next((epic.descendant_ids for epic in epics if epic.selected), None)


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
    scope_ids: frozenset[str] | None = None,
) -> list[TaskRecord]:
    """Filter + sort one status group's rows, recording a load error if any."""
    if isinstance(result, BaseException):
        errors.append(f"Could not load {status} tasks.")
        return []
    rows = [
        task
        for task in cast(list[TaskRecord], result)
        if matches_filters(task, filters=filters, status=status, scope_ids=scope_ids)
    ]
    return sorted(rows, key=lambda task: task.created_at, reverse=True)
