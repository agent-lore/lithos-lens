"""The pure ready/blocked join: open rows in, dashboard sections out.

Split out of ``frontier.py`` when the T1 slices pushed it past the 800-line
god-module ceiling. This is the pass REQUIREMENTS names ``lens.tasks.
frontier_join``: no I/O, no policy about WHICH reads to make — just the
classification of an already-fetched open snapshot against already-fetched
frontier responses, plus the conservative re-reading applied when those
responses disagree.

``frontier.py`` keeps the assembly (what to read, when to retry, what to
report); everything here is a function of its arguments, which is what makes
the classification rules testable one row at a time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord
from lithos_lens.tasks import (
    BlockerChip,
    ClaimRecord,
    SectionName,
    SectionRow,
    TaskRecord,
)

# Only ``task``-typed rows are workable. Epics roll up into the strip and gates
# gate elsewhere; both are excluded from the Lithos frontiers, so they never
# classify here.
WORKABLE_TASK_TYPE = "task"

# Which summary counters each frontier read feeds. A capped read leaves in the
# Not-classified tail rows it would otherwise have placed, so its own section is
# UNDERSTATED — and Needs attention with it, because attention is promoted out
# of BOTH workable sections and a row stuck in the tail is never promoted.
# Nothing else moves: In progress and claims-unknown are decided by the claims
# on the master open list, which no cap can touch, and ``open_total`` counts the
# tail too, so it stays exact whatever the frontiers did.
COUNTERS_BY_FRONTIER: dict[str, tuple[str, ...]] = {
    "ready": ("ready", "attention"),
    "blocked": ("blocked", "attention"),
}


def approximate_counters(capped_frontiers: Sequence[str]) -> frozenset[str]:
    """Name the summary counters the given capped frontier reads understate.

    The two frontiers are capped independently, which is the whole point: with
    the blocked read complete, a workable open row missing from it is not
    blocked, so the tail is the ready read's overflow and only the ready-fed
    counters are approximate. Marking the board wholesale would understate what
    Lens knows exactly.

    The CALLER decides what counts as capped — an empty sequence (no cap, or no
    overflow to show for one, or a read that failed outright) marks nothing.
    """
    return frozenset(
        counter
        for side in capped_frontiers
        for counter in COUNTERS_BY_FRONTIER.get(side, ())
    )


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


def reclassify_conservative(
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
