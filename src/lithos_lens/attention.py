"""Needs-attention severity model v2: which open rows want an operator.

Six ordered rules over the joined dashboard snapshot (REQUIREMENTS §5.2.2):

1. ``unsatisfiable`` — a predecessor or gate was CANCELLED, so the task can
   never become ready without intervention;
2. ``cycle`` — the blocking chain forms a cycle;
3. ``gate-waiting`` — an open human gate has waited past its threshold;
4. ``claim-expiring`` — an active claim is about to lapse (expired claims are
   unobservable upstream, so this is the only observable signal);
5. ``stale-open`` — a workable open task nobody resolved;
6. ``ready-unclaimed`` — a ready-frontier row the fleet has not picked up.

A row that fires any rule is promoted into the ``attention`` section and
REMOVED from the section it would otherwise occupy (the single-placement
rule), carrying one reason chip per rule that fired. Rules 1-2 are intrinsic;
3-6 are threshold-driven by :class:`AttentionPolicy` (config-backed).

Two deliberate never-fire policies keep the list trustworthy: a timestamp Lens
cannot parse never triggers an age rule, and the degraded groups
(``claims_unknown`` / ``unclassified``) are never promoted — flagging a row
whose data Lens has already called incomplete would assert a problem it cannot
support. ``flag_attention`` is pure; ``frontier.load_dashboard`` applies it as
the last step of the join.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord
from lithos_lens.tasks import (
    ATTENTION_RULES,
    AttentionReason,
    ClaimRecord,
    SectionName,
    SectionRow,
    TaskRecord,
    parse_timestamp,
)

# Sections a row can be promoted OUT of into Needs attention (the workable
# three). The degraded groups are deliberately excluded — see the module
# docstring.
ATTENTION_SOURCE_SECTIONS: tuple[SectionName, ...] = (
    "in_progress",
    "ready",
    "blocked",
)

# The gate type whose waiting time escalates into Needs attention (rule 3):
# only a HUMAN gate is waiting on a person. Timer/ci/pr/external gates resolve
# on their own and belong in the Gates section.
HUMAN_GATE_TYPE = "human"
GATE_TASK_TYPE = "gate"


@dataclass(frozen=True)
class AttentionPolicy:
    """Thresholds for the age-based Needs-attention rules (3-6).

    The defaults mirror the ``[lithos-lens.tasks]`` config defaults —
    ``config.py`` is the operator-facing source of truth and
    ``tests/test_attention.py`` pins the two together — so a caller with no
    tuning to do (most tests) gets the shipped behavior. Rules 1 and 2 are
    intrinsic (a cancelled blocker or a cycle is a failure at any age) and have
    no knob.
    """

    gate_waiting_attention_hours: int = 24
    claim_expiring_soon_minutes: int = 10
    stale_open_age_days: int = 7
    unclaimed_ready_age_minutes: int = 60


def flag_attention(
    partition: dict[SectionName, tuple[SectionRow, ...]],
    visible_open: Sequence[TaskRecord],
    *,
    blocked: Sequence[BlockedTaskRecord],
    policy: AttentionPolicy,
    now: datetime,
    index: Mapping[str, TaskRecord] | None = None,
) -> dict[SectionName, tuple[SectionRow, ...]]:
    """Evaluate the six-rule severity model over a classified partition.

    Returns the partition with an ``attention`` section added and every flagged
    row removed from its workable section — the single-placement rule
    (REQUIREMENTS §5.2): a row that needs attention renders *only* here, so the
    operator cannot mistake an unsatisfiable task for one that is merely
    waiting. Rows keep their claims, blocker chips, and degraded-data flags.

    Rules fire in severity order and de-duplicate: a row appears exactly once,
    carrying one reason chip per rule that fired. The list sorts by severity,
    then oldest-first within a tier (the most persistent problem leads).

    ``visible_open`` supplies the human-gate rows (rule 3) — gates are not part
    of the workable partition — and must already be filter-scoped. ``index``
    (id -> task, defaulting to ``visible_open``) resolves blocker titles; pass
    the unfiltered snapshot so a filtered-out predecessor still names its
    reason.
    """
    resolve = index if index is not None else {task.id: task for task in visible_open}
    blocked_map = {record.task.id: record.blockers for record in blocked}
    sections = dict(partition)
    promoted: list[SectionRow] = []
    for section in ATTENTION_SOURCE_SECTIONS:
        kept: list[SectionRow] = []
        for row in partition.get(section, ()):
            reasons = _attention_reasons(
                row,
                section,
                blocked_map.get(row.task.id, ()),
                resolve,
                policy=policy,
                now=now,
            )
            if reasons:
                promoted.append(replace(row, attention=reasons))
            else:
                kept.append(row)
        sections[section] = tuple(kept)
    promoted.extend(_waiting_human_gates(visible_open, policy=policy, now=now))
    sections["attention"] = tuple(sorted(promoted, key=_attention_sort_key))
    return sections


def _attention_reasons(
    row: SectionRow,
    section: SectionName,
    blockers: Sequence[BlockerRecord],
    index: Mapping[str, TaskRecord],
    *,
    policy: AttentionPolicy,
    now: datetime,
) -> tuple[AttentionReason, ...]:
    """Rules 1, 2, 4, 5 and 6 for one workable row, in severity order."""
    reasons: list[AttentionReason] = []
    unsatisfiable = [b for b in blockers if b.kind == "blocker_unsatisfiable"]
    if unsatisfiable:
        reasons.append(
            AttentionReason(
                rule="unsatisfiable",
                detail=(
                    f"Blocker {_blocker_name(unsatisfiable[0], index)} was "
                    "cancelled — this task can never become ready without "
                    f"intervention.{_extra_blockers(len(unsatisfiable))}"
                ),
            )
        )
    cycles = [b for b in blockers if b.kind == "cycle"]
    if cycles:
        reasons.append(
            AttentionReason(
                rule="cycle",
                # The upstream message names the cycle members ("dependency
                # cycle: t-1 -> pred-2 -> pred-2"), which is the whole point of
                # the chip; fall back to the predecessor when it is missing.
                detail=cycles[0].message
                or f"Dependency cycle through {_blocker_name(cycles[0], index)}.",
            )
        )
    expiring = _expiring_claim(row.claims, policy=policy, now=now)
    if expiring is not None:
        claim, remaining = expiring
        reasons.append(
            AttentionReason(
                rule="claim-expiring",
                detail=(
                    f"Claim expiring — {claim.agent or 'unknown agent'} · "
                    f"{claim.aspect or 'unknown aspect'} · "
                    f"{_humanize(remaining)} remaining."
                ),
            )
        )
    stale_after = timedelta(days=policy.stale_open_age_days)
    age = _age(row.task.created_at, now=now)
    # Every threshold comparison is STRICT: the rules read "older than" /
    # "below", so a row sitting exactly ON its threshold has not crossed it
    # yet and must not be flagged (it will be one tick later).
    if age is not None and age > stale_after:
        reasons.append(
            AttentionReason(
                rule="stale-open",
                detail=f"Open {_humanize(age)} with no resolution.",
            )
        )
    if section == "ready" and not row.claims:
        # Rule 6 is ready-aware on purpose: an unclaimed BLOCKED task is
        # correct behavior, not a warning (the pre-graph rule flagged it and
        # was a structural false positive).
        unpicked_after = timedelta(minutes=policy.unclaimed_ready_age_minutes)
        if age is not None and age > unpicked_after:
            reasons.append(
                AttentionReason(
                    rule="ready-unclaimed",
                    detail=f"On the ready frontier, unclaimed for {_humanize(age)}.",
                )
            )
    return tuple(reasons)


def _waiting_human_gates(
    visible_open: Sequence[TaskRecord],
    *,
    policy: AttentionPolicy,
    now: datetime,
) -> list[SectionRow]:
    """Rule 3: open human gates that have waited LONGER than the threshold.

    Gates are not part of the workable partition, so a flagged gate is promoted
    from the (T1-S4) Gates section into this list; an unflagged one is
    untouched here.
    """
    waiting_after = timedelta(hours=policy.gate_waiting_attention_hours)
    rows: list[SectionRow] = []
    for task in visible_open:
        if task.task_type != GATE_TASK_TYPE:
            continue
        if str(task.metadata.get("gate_type") or "") != HUMAN_GATE_TYPE:
            continue
        age = _age(task.created_at, now=now)
        # Strict: "waited LONGER than the threshold" — exactly at it is not yet
        # late (same boundary policy as the other age rules).
        if age is None or age <= waiting_after:
            continue
        rows.append(
            SectionRow(
                task=task,
                claims=task.claims or (),
                attention=(
                    AttentionReason(
                        rule="gate-waiting",
                        detail=(
                            f"Human gate has waited {_humanize(age)} for a decision."
                        ),
                    ),
                ),
            )
        )
    return rows


def _expiring_claim(
    claims: Sequence[ClaimRecord],
    *,
    policy: AttentionPolicy,
    now: datetime,
) -> tuple[ClaimRecord, timedelta] | None:
    """Rule 4: the soonest-expiring claim inside the threshold, if any.

    Expired claims are unobservable (Lithos filters them out of every read at
    query time), so this is the observable replacement for the retired
    ``expired-claim`` rule: flag the claim *before* it silently vanishes.
    """
    threshold = timedelta(minutes=policy.claim_expiring_soon_minutes)
    soonest: tuple[ClaimRecord, timedelta] | None = None
    for claim in claims:
        expires_at = parse_timestamp(claim.expires_at)
        if expires_at is None:
            # A claim with no readable expiry can't be judged — never guess.
            continue
        remaining = expires_at - now
        # Strict: the rule fires when the remaining time is BELOW the
        # threshold, so a claim with exactly the threshold left is not flagged.
        if remaining >= threshold:
            continue
        if soonest is None or remaining < soonest[1]:
            soonest = (claim, remaining)
    return soonest


def _attention_sort_key(row: SectionRow) -> tuple[int, datetime, str]:
    """Severity first, then oldest-first within the tier (id breaks ties).

    Rows whose ``created_at`` is unreadable sort last in their tier rather than
    claiming to be the most persistent problem.
    """
    severity = row.attention[0].severity if row.attention else len(ATTENTION_RULES)
    created = parse_timestamp(row.task.created_at) or datetime.max.replace(tzinfo=UTC)
    return (severity, created, row.task.id)


def _blocker_name(blocker: BlockerRecord, index: Mapping[str, TaskRecord]) -> str:
    """Quoted title of a blocking task, falling back to its id."""
    predecessor = index.get(blocker.task_id)
    if predecessor is not None:
        return f'"{predecessor.title}"'
    return f'"{blocker.task_id}"' if blocker.task_id else "(unknown)"


def _extra_blockers(count: int) -> str:
    return f" (+{count - 1} more)" if count > 1 else ""


def _age(created_at: str, *, now: datetime) -> timedelta | None:
    """How long ago ``created_at`` was, or ``None`` when it is unreadable."""
    parsed = parse_timestamp(created_at)
    return None if parsed is None else now - parsed


def _humanize(delta: timedelta) -> str:
    """Coarse age text for a reason chip: ``12d`` / ``5h`` / ``9m``."""
    seconds = max(int(delta.total_seconds()), 0)
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"
