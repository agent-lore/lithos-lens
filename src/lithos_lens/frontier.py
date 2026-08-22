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
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any, Protocol, cast

from lithos_lens.frontier_fallback import (
    OPEN_SECTIONS,
    RETRY_FAILED_ERROR,
    flat_open_sections,
    frontier_reads,
    frontier_tools_absent,
    resolve_frontier,
)
from lithos_lens.task_filtering import (
    filters_narrow_the_board,
    invalid_project_metadata,
    matches_filters,
    project_convention_conflict,
    task_projects,
)
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord
from lithos_lens.tasks import (
    AgentRecord,
    BlockerChip,
    ClaimRecord,
    DashboardData,
    SectionName,
    SectionRow,
    TaskFilters,
    TaskRecord,
    TaskStatusName,
    TaskSummary,
    int_stat,
)

logger = logging.getLogger(__name__)

# Only ``task``-typed rows are workable. Epics and gates roll up / gate elsewhere
# and are excluded from both Lithos frontiers, so they never classify here.
WORKABLE_TASK_TYPE = "task"


class FrontierLithosClient(Protocol):
    """The client surface ``load_dashboard`` consumes (the five parallel calls)."""

    async def list_tasks(
        self,
        *,
        agent: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
        resolved_since: str | None = None,
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

    async def list_tool_names(self) -> set[str]: ...


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


async def load_dashboard(
    lithos: FrontierLithosClient,
    *,
    filters: TaskFilters,
    frontier_limit: int,
    graph_available: bool = True,
) -> DashboardData:
    """Assemble the dashboard from the parallel Lithos reads.

    All seven independent reads fan out in ONE gather: the master open list
    (claims inline), the ready and blocked frontiers, stats, the agent list,
    and the recently-resolved completed/cancelled windows. The only second
    round-trip is the read-skew retry below — and only when the first pair of
    frontier responses is inconsistent below the limit.

    ``graph_available=False`` says the caller already learned this server has
    no frontier tools (story 27): the two frontier calls are skipped and the
    open rows render flat. Detection is the same either way — a frontier call
    that fails because the TOOL is missing (never because the read failed)
    switches this load to the flat fallback and reports
    ``DashboardData.graph_available=False`` so the caller can remember it.

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

    async def load_closed(status: TaskStatusName) -> list[TaskRecord]:
        # Terminal rows are windowed by RESOLUTION time, never creation time:
        # ``resolved_since`` is the upstream filter for "recent work", so a
        # task created months ago and finished yesterday is in the window.
        #
        # That window is the ONLY filter pushed upstream (it bounds the fetch).
        # Project, tag and agent filtering is applied client-side over the
        # fetched rows (PRD "Filters and URL contract"): no upstream call can
        # express the metadata-OR-tag project match, and the upstream ``agent``
        # argument is creator-only, which would drop rows the agent merely
        # claims. Claims are omitted by default (lithos_task_list contract),
        # which leaves a resolved row ``claims=None`` — unknown, not "no
        # claimer" — and would silently hide resolved work the selected agent
        # claims. They are requested exactly when the agent filter needs them:
        # resolved rows render no claim chips (``claim_state`` is
        # not_applicable off the open list), so the unfiltered dashboard keeps
        # the cheaper read.
        return await lithos.list_tasks(
            status=status,
            resolved_since=filters.since,
            with_claims=bool(filters.agent),
        )

    ready_read, blocked_read = frontier_reads(
        lithos, frontier_limit=frontier_limit, graph_available=graph_available
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
        ready_read,
        blocked_read,
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

    # Suspicion comes from the reads failing TOGETHER; the verdict comes from
    # the server's tool list. Both frontier calls failing is already a degraded
    # render, so the extra round-trip is cheap and never on the happy path.
    tools_absent = False
    if (
        graph_available
        and isinstance(ready_result, BaseException)
        and isinstance(blocked_result, BaseException)
    ):
        tools_absent = await frontier_tools_absent(lithos)

    graph_available, frontier_ok, ready_list, blocked_records = resolve_frontier(
        ready_result,
        blocked_result,
        graph_available=graph_available,
        tools_absent=tools_absent,
        errors=errors,
    )

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
    ) -> _FrontierState:
        index = {task.id: task for task in snapshot}
        visible = [
            task
            for task in snapshot
            if matches_filters(task, filters=filters, status="open")
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

    # §14: a frontier READ failure renders the master open list flat too, not
    # just a missing-tools verdict. Half a frontier is not a classification —
    # rows would land in "Not classified", the tail whose banner explains it as
    # frontier-limit overflow, and an outage would read as truncation.
    if graph_available and frontier_ok:
        state = _partition_state(open_snapshot, ready_list, blocked_records)

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
            if (
                isinstance(retry_open, BaseException)
                or isinstance(retry_ready, BaseException)
                or isinstance(retry_blocked, BaseException)
            ):
                # Keep the first generation (a mixed one would be worse), but
                # SAY SO: without this the stripe can call a board healthy
                # whose skew was never resolved.
                errors.append(RETRY_FAILED_ERROR)
            else:
                open_snapshot = sorted(
                    cast(list[TaskRecord], retry_open),
                    key=lambda task: task.created_at,
                    reverse=True,
                )
                ready_list = cast(list[TaskRecord], retry_ready)
                blocked_records = cast(list[BlockedTaskRecord], retry_blocked)
                state = _partition_state(open_snapshot, ready_list, blocked_records)

        partition = state.partition
        at_limit = state.at_limit
        open_index = state.index
        reconciliation_pending = frontier_ok and state.skewed_frontier
        if reconciliation_pending:
            partition = _reclassify_conservative(partition, state.effective_overlap)
    else:
        # Flat fallback — no usable frontier, whether because the tools are
        # absent (story 27) or because a read of them failed. Either way there
        # is nothing to join, nothing to truncate, and no pair of reads that
        # could disagree, so the skew machinery above is skipped outright
        # rather than fed empty frontiers, which would read every open row as
        # "unclassified" and raise a false reconciliation warning.
        #
        # The two causes stay distinguishable to the operator through the
        # error lines (`FRONTIER_UNAVAILABLE_ERROR` vs "Could not load the
        # ready frontier.") and through `graph_available`, which stays True
        # for an outage: a transient failure must not be dressed up as "your
        # Lithos is too old", nor cost the caller its graph verdict.
        partition = flat_open_sections(
            [
                task
                for task in open_snapshot
                if matches_filters(task, filters=filters, status="open")
            ]
        )
        at_limit = False
        open_index = {task.id: task for task in open_snapshot}
        reconciliation_pending = False

    closed: dict[str, list[TaskRecord]] = {}
    for status, result in zip(("completed", "cancelled"), closed_results, strict=True):
        rows = _rows_for(status, result, filters, errors)
        # Exactly-one-row dedup: the (retried) master-open snapshot is the
        # authority on openness. A task still in the final open snapshot
        # renders in its open section only; a task absent from it renders in
        # its terminal section only (that direction is structural — the open
        # sections derive from the snapshot).
        closed[status] = [task for task in rows if task.id not in open_index]

    # Both project surfaces below read every row this load fetched — open AND
    # resolved — from the UNFILTERED reads, deduped by id: selecting one
    # project must not collapse the list of projects you can switch to, and a
    # convention conflict is a property of the task, not of its status.
    loaded_tasks = _loaded_task_rows(
        open_snapshot,
        [
            cast("list[TaskRecord]", result)
            for result in closed_results
            if not isinstance(result, BaseException)
        ],
    )
    _log_project_data_quality(loaded_tasks, filters)
    projects = _project_universe(loaded_tasks, filters)

    show_open = "open" in filters.statuses
    sections: dict[SectionName, tuple[SectionRow, ...]] = {}
    for section in OPEN_SECTIONS:
        sections[section] = partition.get(section, ()) if show_open else ()
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

    open_total = sum(len(partition.get(section, ())) for section in OPEN_SECTIONS)
    filters_narrowed = filters_narrow_the_board(filters)
    nothing_to_show = _is_nothing_to_show(
        open_snapshot,
        closed_results,
        errors=errors,
        filters_narrowed=filters_narrowed,
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
        projects=projects,
        # Truncation means a frontier response actually HIT frontier_limit,
        # leaving otherwise-classifiable rows in the tail. Unclassified rows
        # from a failed frontier read are surfaced by the error banner, and a
        # below-limit gap is read-skew (handled by the retry + conservative
        # Blocked above) — neither is mislabelled as truncation.
        truncated=frontier_ok and at_limit and bool(partition["unclassified"]),
        reconciliation_pending=reconciliation_pending,
        filters_narrowed=filters_narrowed,
        graph_available=graph_available,
        open_flat=not (graph_available and frontier_ok),
        nothing_to_show=nothing_to_show,
        errors=tuple(errors),
    )


def _is_nothing_to_show(
    open_snapshot: list[TaskRecord],
    closed_results: tuple[list[TaskRecord] | BaseException, ...],
    *,
    errors: list[str],
    filters_narrowed: bool,
) -> bool:
    """True when Lithos answered everything and returned nothing for this view.

    "Nothing here" as opposed to "your filters hid everything", which the
    per-section empty lines already say. Three things must hold: no recorded
    error (an outage empties the snapshot too), an empty master open list
    (which is read unfiltered), and no narrowing filter — every section is
    filtered down before it is counted (client-side since T1-S9), so under a
    filter an empty board says nothing about the corpus at all.

    ``since`` is the one filter that survives this test, because it always has
    a value and windowing the resolved sections is the dashboard's normal
    posture rather than a narrowing choice. That is precisely why this is not a
    statement about the corpus: work resolved before the window is invisible
    here, so the panel this drives must name the window rather than claim there
    are no tasks.
    """
    if errors or open_snapshot or filters_narrowed:
        return False
    return not any(
        not isinstance(result, BaseException) and bool(result)
        for result in closed_results
    )


def _loaded_task_rows(
    open_snapshot: Sequence[TaskRecord],
    closed_groups: Iterable[Sequence[TaskRecord]],
) -> tuple[TaskRecord, ...]:
    """Every task row this load fetched, deduped by id (open snapshot wins).

    Read skew can return the same id in both the open snapshot and a terminal
    window; the open snapshot is the authority on the row, and dedup keeps
    per-load reporting counted once.
    """
    rows: list[TaskRecord] = list(open_snapshot)
    seen = {task.id for task in rows}
    for group in closed_groups:
        for task in group:
            if task.id not in seen:
                seen.add(task.id)
                rows.append(task)
    return tuple(rows)


def _project_universe(
    tasks: Sequence[TaskRecord],
    filters: TaskFilters,
) -> tuple[str, ...]:
    """Every project slug present in the loaded rows, sorted (§5B.1).

    The universe is the union of BOTH conventions' slugs regardless of the
    active posture — §5B.1 is explicit that no project may be invisible to its
    own view — even though matching under a single-convention posture honours
    only that convention. Only the tag KEY follows configuration (§5B.9).
    """
    slugs: set[str] = set()
    for task in tasks:
        slugs.update(
            task_projects(task, convention="both", tag_key=filters.project_tag_key)
        )
    return tuple(sorted(slugs))


def _log_project_data_quality(
    tasks: Sequence[TaskRecord],
    filters: TaskFilters,
) -> None:
    """Report this load's project data-quality signals, once each (§5B.1).

    Two independent signals over every loaded row — resolved rows carry their
    conventions too:

    - the two conventions are present and DISAGREE. Reported in every posture:
      §5B.1 makes the warning a property of the data, not of the matching
      posture Lens happens to run (both values are read for the universe
      regardless). Neither value is dropped — the task matches under both slugs.
    - ``metadata.project`` is present but is not a string. Lens cannot read a
      project out of it, so the task is invisible to its project view; the
      value is ignored rather than coerced into a fabricated slug.
    """
    conflicts: list[str] = []
    malformed: list[str] = []
    for task in tasks:
        if project_convention_conflict(task, tag_key=filters.project_tag_key):
            conflicts.append(task.id)
        if invalid_project_metadata(task):
            malformed.append(task.id)
    if conflicts:
        logger.warning(
            "task project conventions disagree",
            extra={
                "lens_event": "lens.tasks.project_convention_conflict",
                "conflict_count": len(conflicts),
                "conflicting_task_ids": conflicts[:20],
            },
        )
    if malformed:
        logger.warning(
            "task metadata.project is not a string slug",
            extra={
                "lens_event": "lens.tasks.project_metadata_invalid",
                "invalid_count": len(malformed),
                "invalid_task_ids": malformed[:20],
            },
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
    """Filter + sort one status group's rows, recording a load error if any.

    Terminal groups sort newest-RESOLVED first (matching the ``resolved_since``
    window they are drawn from), falling back to ``created_at`` for a row whose
    ``resolved_at`` an older Lithos omitted.
    """
    if isinstance(result, BaseException):
        errors.append(f"Could not load {status} tasks.")
        return []
    rows = [
        task
        for task in cast(list[TaskRecord], result)
        if matches_filters(task, filters=filters, status=status)
    ]
    return sorted(
        rows, key=lambda task: task.resolved_at or task.created_at, reverse=True
    )
