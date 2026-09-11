"""Frontier join: partition the open task set into workable sections.

The graph-native dashboard never re-derives readiness — Lithos owns the
ready/blocked frontier (``lithos_task_ready`` / ``lithos_task_blocked`` evaluate
gate and timer state at query time). Lens joins the id-sets those calls return
against the master open list: a workable open task is *In progress* when it
carries an inline claim, else *Ready* or *Blocked* by frontier membership, else
*Not classified* (only reachable under frontier-limit truncation). Epics and
gates are excluded from both frontiers upstream, so they never enter the
workable partition: open gates get their own section instead, assembled in
``gates.py`` (which also owns the waiter counts and the timer self-refresh).

On top of that join sits the Needs-attention severity model
(``attention.flag_attention``), applied last because it promotes rows OUT of
the sections computed here.

Open epics roll up instead: EVERY open epic gets a ``lithos_task_children``
read and a progress chip (``build_epic_rollup``), issued in bounded batches
(``EPIC_FANOUT_BATCH``) so the fan-out cannot flood the shared MCP session or
hold every subtree at once. The selected chip's descendant set scopes every
section (``?epic=``). Those children reads are independent of the open read
like every other call here, so a chip's counts may be one generation newer than
the sections — harmless, because counts are display-only and never decide a
row's placement. The SCOPE does decide placement, and there the generation gap
is ambiguous: an epic that closed between the two reads answers with an empty
subtree (per the ``lithos_task_children`` contract) and so does a genuinely
childless open epic, which must scope to an empty board. One authoritative
``lithos_task_get`` breaks that tie — see ``epic_strip._is_open_epic``.

``classify_open_tasks`` is the pure join; ``load_dashboard`` is the five-call
assembly that feeds it. Both live here (not in ``tasks.py``) because they
depend on the task-graph records in ``task_graph.py``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from lithos_lens.attention import AttentionPolicy, flag_attention
from lithos_lens.dashboard import DashboardData, TaskSummary
from lithos_lens.epic_strip import (
    epic_scope_ids,
    load_epic_rollups,
)
from lithos_lens.frontier_fallback import (
    RETRY_FAILED_ERROR,
    flat_open_sections,
    resolve_frontier,
)
from lithos_lens.frontier_join import (
    WORKABLE_TASK_TYPE,
    approximate_counters,
    classify_open_tasks,
    reclassify_conservative,
)
from lithos_lens.gates import GATE_TASK_TYPE, GateSection, load_gates
from lithos_lens.task_filtering import (
    filters_narrow_the_board,
    filters_narrow_the_open_side,
    loaded_task_rows,
    log_project_data_quality,
    matches_filters,
    project_universe,
)
from lithos_lens.task_graph import BlockedTaskRecord, EdgeRecord
from lithos_lens.tasks import (
    OPEN_SECTIONS,
    WORKABLE_SECTIONS,
    AgentRecord,
    SectionName,
    SectionRow,
    TaskFilters,
    TaskRecord,
    TaskStatusName,
    int_stat,
)

logger = logging.getLogger(__name__)


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

    async def task_children(
        self,
        task_id: str,
        *,
        recursive: bool = False,
        include_closed: bool = False,
    ) -> list[TaskRecord]: ...

    async def task_get(self, task_id: str) -> TaskRecord: ...

    # Read by the Gates section, and ONLY on its degraded waiter paths — the
    # healthy board derives every waiter count from the blocked frontier above
    # and issues no call here at all (``gates.attach_gate_waiters``).
    async def task_edge_list(
        self,
        task_id: str,
        *,
        direction: str = "both",
        types: list[str] | None = None,
    ) -> list[EdgeRecord]: ...

    async def stats(self) -> dict[str, Any]: ...

    async def list_agents(self) -> list[AgentRecord]: ...


async def load_dashboard(
    lithos: FrontierLithosClient,
    *,
    filters: TaskFilters,
    frontier_limit: int,
    attention: AttentionPolicy | None = None,
    now: datetime | None = None,
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

    Once the partition settles, ``flag_attention`` promotes the rows that need
    an operator into the Needs-attention section. ``attention`` carries the
    rule thresholds (config-backed; defaults when omitted) and ``now`` is
    injectable so the age-based rules are testable without freezing the clock.
    """
    errors: list[str] = []
    policy = attention or AttentionPolicy()
    evaluated_at = now or datetime.now(UTC)

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

    ready_read = lithos.task_ready(limit=frontier_limit, with_claims=False)
    blocked_read = lithos.task_blocked(limit=frontier_limit)
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

    frontier_ok, ready_list, blocked_records = resolve_frontier(
        ready_result,
        blocked_result,
        errors=errors,
    )
    # Tracked apart from ``frontier_ok`` (which is ready AND blocked): the gate
    # waiter counts depend on the blocked read alone, so a ready-only outage
    # must not degrade them — and "0 waiters" built on a blocked read that
    # never answered would be a fabricated fact rather than a degraded one.
    blocked_ok = not isinstance(blocked_result, BaseException)

    # Raw terminal rows (pre-filter). Two uses, one pass:
    #
    # - ``terminal_ids`` — a task appearing in BOTH the open snapshot and a
    #   terminal result is freshness skew worth a retry, whether or not the
    #   terminal row survives the section filters.
    # - ``terminal_index`` — blocker NAMES. A predecessor that was cancelled or
    #   completed is by definition absent from the open snapshot, and a
    #   cancelled one is exactly what the highest-severity rule fires on: the
    #   unsatisfiable chip and its reason sentence would print a raw task id on
    #   the loudest row on the board while the same page renders that task's
    #   title in Cancelled. These reads are already in hand, so naming it costs
    #   nothing. The residual is honest and bounded: a predecessor resolved
    #   outside the ``resolved_since`` window was never read, so it still falls
    #   back to its id — Lens names what it read.
    terminal_ids: set[str] = set()
    terminal_index: dict[str, TaskRecord] = {}
    for closed_result in closed_results:
        if not isinstance(closed_result, BaseException):
            for closed_task in closed_result:
                terminal_ids.add(closed_task.id)
                terminal_index[closed_task.id] = closed_task

    def _blocker_names(open_index: Mapping[str, TaskRecord]) -> dict[str, TaskRecord]:
        """The index used to NAME blockers — never to decide openness.

        Deliberately a second view rather than a wider ``index``: the open
        index is also the authority on which tasks are open (the terminal
        sections dedup against it), so a terminal row added there would delete
        itself from the section it belongs to. Naming has no such duty — it
        only needs a title for an id — so it takes the union, with the open
        rows winning a collision because they are the generation the sections
        render.
        """
        return {**terminal_index, **open_index}

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
        # Recorded PER SIDE — the two reads are capped independently, and only
        # the counters a capped side feeds are approximate (story 28). The skew
        # machinery below still asks the board-wide question.
        capped_frontiers = tuple(
            side
            for side, rows in (("ready", ready_rows), ("blocked", blocked_rows))
            if len(rows) >= frontier_limit
        )
        at_limit = bool(capped_frontiers)
        parts = classify_open_tasks(
            visible,
            ready_ids=ready_ids,
            blocked=blocked_rows,
            index=_blocker_names(index),
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
            visible,
            parts,
            effective_overlap,
            capped_frontiers,
            skewed_frontier,
            skewed_frontier or bool(terminal_overlap),
        )

    # The epic strip depends on the open snapshot (its epic ids), so it is
    # fetched here rather than in the main gather — and refetched below if the
    # skew retry adopts a newer snapshot, so the strip lists the epics of the
    # snapshot the sections were built from. The children reads themselves stay
    # independent reads (see the module docstring): counts can be a generation
    # newer, which is why only a non-empty subtree is allowed to scope.
    strip = await load_epic_rollups(lithos, open_snapshot, selected=filters.epic)
    scope_ids = epic_scope_ids(strip.rollups)

    # §14: a failed frontier read renders the master open list flat. Half a
    # frontier is not a classification — rows would land in "Not classified",
    # the tail whose banner explains it as frontier-limit overflow, so a failed
    # read would present itself as truncation.
    if frontier_ok:
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
                blocked_ok = True
                # Re-read the strip against the adopted snapshot, so the chips
                # list the epics of the generation the sections were built from.
                strip = await load_epic_rollups(
                    lithos, open_snapshot, selected=filters.epic
                )
                scope_ids = epic_scope_ids(strip.rollups)
                state = _partition_state(
                    open_snapshot, ready_list, blocked_records, scope_ids
                )

        partition = state.partition
        capped_frontiers = state.capped_frontiers
        open_index = state.index
        visible_open = state.visible
        reconciliation_pending = frontier_ok and state.skewed_frontier
        if reconciliation_pending:
            partition = reclassify_conservative(partition, state.effective_overlap)
        # Needs attention last: it promotes rows OUT of the sections above, so
        # it must see their final (post-reconciliation) membership.
        #
        # Graph branch only. Every source section it promotes from
        # (in_progress / ready / blocked) is empty in the flat fallback, so the
        # call would be a no-op there — and the rules it could still evaluate
        # are the ones whose inputs the fallback has already lost.
        partition = flag_attention(
            partition,
            state.visible,
            blocked=blocked_records,
            policy=policy,
            now=evaluated_at,
            index=_blocker_names(open_index),
        )
    else:
        # Flat fallback — no usable frontier, because a read of it failed
        # (a server that never had the tools fails the same way, and is
        # reported as the failed read it is). There is nothing to join,
        # nothing to truncate, and no pair of reads that could disagree, so
        # the skew machinery above is skipped outright rather than fed empty
        # frontiers, which would read every open row as "unclassified" and
        # raise a false reconciliation warning. The error lines name which
        # read did not answer.
        visible_open = [
            task
            for task in open_snapshot
            if matches_filters(
                task, filters=filters, status="open", scope_ids=scope_ids
            )
        ]
        # Gates are held back from the flat list because they still get their
        # own section below. Nothing about a gate's PLACEMENT came from the
        # frontier — only its waiter count did, and that count says so — so
        # dropping the operator's own queue here would lose a surface the
        # failed read never touched, and rendering it in both places would
        # break single placement.
        partition = flat_open_sections(
            [task for task in visible_open if task.task_type != GATE_TASK_TYPE]
        )
        capped_frontiers = ()
        open_index = {task.id: task for task in open_snapshot}
        reconciliation_pending = False

    if strip.failed:
        errors.append("Could not load epic progress.")

    # The Gates section is open work, so it follows the open sections' switch —
    # and the degraded per-gate waiter reads are skipped entirely when it is
    # hidden. It is assembled AFTER ``flag_attention`` so it can see which
    # gates that step already promoted (single placement), and it reads the
    # WHOLE open index rather than ``visible_open`` for waiter identities, so
    # scoping the board never shrinks a gate's "blocks N tasks".
    show_open = "open" in filters.statuses
    gate_section = (
        await load_gates(
            lithos,
            visible_open=visible_open,
            index=open_index,
            blocked=blocked_records,
            blocked_available=blocked_ok,
            # The gate counts care about the BLOCKED response specifically; a
            # truncated ready read says nothing about who waits on a gate.
            blocked_truncated=len(blocked_records) >= frontier_limit,
            placed_ids=frozenset(row.task.id for row in partition.get("attention", ())),
            now=evaluated_at,
        )
        if show_open
        else GateSection()
    )
    errors.extend(gate_section.errors)

    open_flat = not frontier_ok

    # Rows the graph rolls up rather than rendering. Epics only since T1-S4:
    # a gate renders in the Gates section (or, once it has waited too long, in
    # Needs attention), so counting it here would report visible work as
    # withheld. Counted over the SAME filtered set the sections are built from,
    # and after the skew retry may have replaced the snapshot, so the number
    # describes what this render actually withheld.
    # Zero unless the open side is actually on screen: with ``?status=completed``
    # the open sections are emptied by choice, and an epic in the snapshot must
    # not turn that into "nothing to work on here".
    rolled_up_open = (
        0
        if open_flat or "open" not in filters.statuses
        else sum(
            1
            for task in visible_open
            if task.task_type not in (WORKABLE_TASK_TYPE, GATE_TASK_TYPE)
        )
    )

    closed: dict[str, list[TaskRecord]] = {}
    for status, result in zip(("completed", "cancelled"), closed_results, strict=True):
        rows = _rows_for(status, result, filters, errors, scope_ids)
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
    loaded_tasks = loaded_task_rows(
        open_snapshot,
        [
            cast("list[TaskRecord]", result)
            for result in closed_results
            if not isinstance(result, BaseException)
        ],
    )
    log_project_data_quality(loaded_tasks, filters)
    projects = project_universe(loaded_tasks, filters)

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

    # ``open_total`` counts the open WORKABLE tasks Lens classified. Promoted
    # rows still count (they only changed section), but a promoted human gate
    # does not — gates were never part of the workable partition. The flat
    # fallback has no partition to read that from, so its one section counts
    # whole: without the frontier there is no workable/not distinction to make
    # among the rows it holds (gates are held out of it — they render in the
    # Gates section — so they are not counted there either).
    open_total = (
        sum(len(partition.get(section, ())) for section in WORKABLE_SECTIONS)
        + len(partition.get("open", ()))
        + len(partition.get("claims_unknown", ()))
        + len(partition.get("unclassified", ()))
        + sum(
            1
            for row in partition.get("attention", ())
            if row.task.task_type == WORKABLE_TASK_TYPE
        )
    )
    filters_narrowed = filters_narrow_the_board(
        filters, scope_applied=scope_ids is not None
    )
    open_side_narrowed = filters_narrow_the_open_side(
        filters, scope_applied=scope_ids is not None
    )
    nothing_to_show = _is_nothing_to_show(
        open_snapshot,
        closed_results,
        errors=errors,
        filters_narrowed=filters_narrowed,
    )
    # Truncation means a frontier response actually HIT frontier_limit AND
    # left otherwise-classifiable rows in the tail; one fact, computed once, so
    # the banner and the per-counter marking can never disagree about it.
    # Unclassified rows from a FAILED frontier read are surfaced by the error
    # banner, and a below-limit gap is read-skew (handled by the retry +
    # conservative Blocked above) — neither is mislabelled as truncation.
    truncated = (
        frontier_ok and bool(capped_frontiers) and bool(partition.get("unclassified"))
    )
    summary = TaskSummary(
        # ``.get`` throughout: the flat fallback's partition holds one section.
        attention=len(partition.get("attention", ())),
        in_progress=len(partition.get("in_progress", ())),
        ready=len(partition.get("ready", ())),
        blocked=len(partition.get("blocked", ())),
        gates=gate_section.count,
        claims_unknown=len(partition.get("claims_unknown", ())),
        unclassified=len(partition.get("unclassified", ())),
        open_total=open_total,
        # Claims on the rows actually rendered In progress — NOT the Lithos-wide
        # lithos_stats.open_claims. The card pairs this with the (filtered,
        # possibly epic-scoped) In-progress count, so a global stat there would
        # read "1 in progress / 10 active claims" on a scoped board.
        active_claims=sum(len(row.claims) for row in partition.get("in_progress", ())),
        recent_completed=len(closed["completed"]),
        recent_cancelled=len(closed["cancelled"]),
        agents=int_stat(stats, "agents", default=len(agents)),
        # Only the counters fed by a side that actually truncated: the board is
        # not approximate, the capped side's sections are.
        approximate=approximate_counters(capped_frontiers if truncated else ()),
    )
    return DashboardData(
        filters=filters,
        summary=summary,
        sections=sections,
        agents=agents,
        frontier_limit=frontier_limit,
        open_total=open_total,
        projects=projects,
        gate_groups=gate_section.groups,
        next_gate_ready_at=gate_section.next_ready_at,
        # Computed once above, per side, so the banner and the per-counter
        # marking cannot disagree about what truncated.
        truncated=truncated,
        reconciliation_pending=reconciliation_pending,
        filters_narrowed=filters_narrowed,
        open_side_narrowed=open_side_narrowed,
        open_flat=open_flat,
        rolled_up_open=rolled_up_open,
        nothing_to_show=nothing_to_show,
        errors=tuple(errors),
        epics=strip.rollups,
        # An ``?epic=`` that resolves to no scope — no longer an open epic, its
        # children read failed, or an empty subtree Lens could not confirm —
        # shows the whole board with the template's explanation. A CONFIRMED
        # childless epic is a real (empty) scope instead: an empty board, also
        # explained.
        epic_scope=filters.epic if scope_ids is not None else "",
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


class _FrontierState:
    """One generation of the joined reads: snapshot views + skew verdicts.

    ``capped_frontiers`` names which frontier reads came back full;
    ``skewed_frontier`` drives the conservative reclassification + banner;
    ``retry_worthy`` additionally includes the open∩terminal freshness
    overlap, which only warrants the retry (the later snapshot then
    arbitrates via the closed dedup).
    """

    __slots__ = (
        "snapshot",
        "index",
        "visible",
        "partition",
        "effective_overlap",
        "capped_frontiers",
        "skewed_frontier",
        "retry_worthy",
    )

    def __init__(
        self,
        snapshot: list[TaskRecord],
        index: Mapping[str, TaskRecord],
        visible: list[TaskRecord],
        partition: dict[SectionName, tuple[SectionRow, ...]],
        effective_overlap: set[str],
        capped_frontiers: tuple[str, ...],
        skewed_frontier: bool,
        retry_worthy: bool,
    ) -> None:
        self.snapshot = snapshot
        self.index = index
        # The filter-scoped subset the sections render (the gate rule reads it;
        # ``snapshot``/``index`` stay whole so blocker titles resolve).
        self.visible = visible
        self.partition = partition
        self.effective_overlap = effective_overlap
        # The frontier sides whose response hit ``frontier_limit``, in read
        # order — the per-counter approximate marking is derived from these.
        self.capped_frontiers = capped_frontiers
        self.skewed_frontier = skewed_frontier
        self.retry_worthy = retry_worthy


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
        if matches_filters(task, filters=filters, status=status, scope_ids=scope_ids)
    ]
    return sorted(
        rows, key=lambda task: task.resolved_at or task.created_at, reverse=True
    )
