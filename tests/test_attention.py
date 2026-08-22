"""T1 slice 3 — Needs-attention severity model v2.

``flag_attention`` is the pure evaluator: six ordered rules over an
already-classified partition, promoting every flagged row out of its workable
section (single-placement). ``now`` is injected so each age-based rule is
deterministic, and each rule is pinned both firing and NOT firing (its knob
widened) — a rule that can only ever fire is a rule that will cry wolf.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from lithos_lens.attention import AttentionPolicy, flag_attention
from lithos_lens.config import (
    DEFAULT_TASKS_CLAIM_EXPIRING_SOON_MINUTES,
    DEFAULT_TASKS_GATE_WAITING_ATTENTION_HOURS,
    DEFAULT_TASKS_STALE_OPEN_AGE_DAYS,
    DEFAULT_TASKS_UNCLAIMED_READY_AGE_MINUTES,
)
from lithos_lens.frontier import classify_open_tasks
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord
from lithos_lens.tasks import ClaimRecord, SectionName, TaskRecord

_NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _task(
    task_id: str,
    *,
    task_type: str = "task",
    claims: Any = None,
    tags: tuple[str, ...] = (),
    created_at: str = "",
    metadata: dict[str, Any] | None = None,
) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        title=f"Title {task_id}",
        status="open",
        task_type=task_type,
        tags=tags,
        created_at=created_at,
        metadata=metadata or {},
        claims=claims,
    )


def _blocked(task: TaskRecord, *blockers: BlockerRecord) -> BlockedTaskRecord:
    return BlockedTaskRecord(task=task, blockers=tuple(blockers))


def _section_ids(
    sections: dict[SectionName, tuple[Any, ...]], key: SectionName
) -> list[str]:
    return [row.task.id for row in sections[key]]


def _ago(**delta: float) -> str:
    """ISO ``created_at`` / ``expires_at`` that far before :data:`_NOW`."""
    return (_NOW - timedelta(**delta)).isoformat()


def _ahead(**delta: float) -> str:
    return (_NOW + timedelta(**delta)).isoformat()


def _flag(
    open_tasks: list[TaskRecord],
    *,
    ready_ids: set[str] | None = None,
    blocked: list[BlockedTaskRecord] | None = None,
    policy: AttentionPolicy | None = None,
    now: datetime = _NOW,
) -> dict[SectionName, tuple[Any, ...]]:
    """Classify then flag — the pipeline order ``load_dashboard`` uses."""
    blocked_rows = blocked or []
    partition = classify_open_tasks(
        open_tasks, ready_ids=ready_ids or set(), blocked=blocked_rows
    )
    return flag_attention(
        partition,
        open_tasks,
        blocked=blocked_rows,
        policy=policy or AttentionPolicy(),
        now=now,
    )


def _rules(row: Any) -> list[str]:
    return [reason.rule for reason in row.attention]


def test_attention_policy_defaults_match_the_config_defaults() -> None:
    """The policy defaults exist so tests need no wiring; config.py is the
    operator-facing source of truth. Drift between them would make the shipped
    dashboard behave differently from every unit test."""
    policy = AttentionPolicy()
    assert policy.gate_waiting_attention_hours == (
        DEFAULT_TASKS_GATE_WAITING_ATTENTION_HOURS
    )
    assert policy.claim_expiring_soon_minutes == (
        DEFAULT_TASKS_CLAIM_EXPIRING_SOON_MINUTES
    )
    assert policy.stale_open_age_days == DEFAULT_TASKS_STALE_OPEN_AGE_DAYS
    assert policy.unclaimed_ready_age_minutes == (
        DEFAULT_TASKS_UNCLAIMED_READY_AGE_MINUTES
    )


def test_unsatisfiable_blocker_is_promoted_out_of_blocked() -> None:
    """Slice-3 acceptance (rule 1): a task whose blocker was CANCELLED renders
    only in Needs attention, with an ``unsatisfiable`` chip naming the dead
    predecessor. Leaving it in Blocked would read as "merely waiting"."""
    cancelled_pred = _task("old-spike", created_at=_ago(days=2))
    stuck = _task("stuck", claims=(), created_at=_ago(hours=1))
    sections = _flag(
        [stuck, cancelled_pred],
        blocked=[
            _blocked(
                stuck,
                BlockerRecord(
                    kind="blocker_unsatisfiable",
                    task_id="old-spike",
                    type="blocks",
                    status="cancelled",
                    message="Blocking predecessor old-spike was cancelled;",
                ),
            )
        ],
    )

    assert _section_ids(sections, "attention") == ["stuck"]
    assert _section_ids(sections, "blocked") == []
    (row,) = sections["attention"]
    assert _rules(row) == ["unsatisfiable"]
    assert "Title old-spike" in row.attention[0].detail
    # The blocker chips survive the promotion — the row still explains itself.
    assert [chip.kind for chip in row.blockers] == ["blocker_unsatisfiable"]


def test_dependency_cycle_is_promoted_with_the_member_message() -> None:
    """Rule 2: the upstream message names the cycle members, which is the whole
    value of the chip, so it is carried verbatim."""
    member = _task("c1", claims=(), created_at=_ago(hours=2))
    sections = _flag(
        [member],
        blocked=[
            _blocked(
                member,
                BlockerRecord(
                    kind="cycle",
                    task_id="c2",
                    type="blocks",
                    status="open",
                    message="dependency cycle: c1 -> c2 -> c1",
                ),
            )
        ],
    )

    (row,) = sections["attention"]
    assert _rules(row) == ["cycle"]
    assert row.attention[0].detail == "dependency cycle: c1 -> c2 -> c1"
    assert _section_ids(sections, "blocked") == []


def test_fresh_blocked_unclaimed_task_is_not_flagged() -> None:
    """Slice-3 acceptance (the false-positive half): an ordinary open
    predecessor is *correct* waiting. A fresh blocked, unclaimed task must stay
    in Blocked — rule 6 is ready-aware precisely so this never fires."""
    waiting = _task("waiting", claims=(), created_at=_ago(hours=6))
    sections = _flag(
        [waiting],
        blocked=[
            _blocked(waiting, BlockerRecord(kind="task", task_id="pred", type="blocks"))
        ],
    )

    assert _section_ids(sections, "attention") == []
    assert _section_ids(sections, "blocked") == ["waiting"]


def test_waiting_human_gate_is_flagged_and_respects_its_knob() -> None:
    """Rule 3: an open human gate past the threshold escalates. Gates are not
    in the workable partition, so this is the join's only source of gate rows."""
    gate = _task(
        "gate-1",
        task_type="gate",
        claims=(),
        created_at=_ago(hours=30),
        metadata={"gate_type": "human"},
    )
    sections = _flag([gate])
    (row,) = sections["attention"]
    assert row.task.id == "gate-1"
    assert _rules(row) == ["gate-waiting"]
    # Coarse-by-design age text: 30 hours reads as "1d" on the chip.
    assert "1d" in row.attention[0].detail

    # Knob respected: raise the threshold past the wait and it stops firing.
    relaxed = _flag([gate], policy=AttentionPolicy(gate_waiting_attention_hours=48))
    assert _section_ids(relaxed, "attention") == []


@pytest.mark.parametrize("gate_type", ["timer", "ci", "pr", "external_task", ""])
def test_non_human_gates_never_escalate(gate_type: str) -> None:
    """Only a HUMAN gate is waiting on a person; the rest resolve themselves
    (a timer lapses, CI reports) and belong in the Gates section."""
    gate = _task(
        "gate-x",
        task_type="gate",
        claims=(),
        created_at=_ago(days=30),
        metadata={"gate_type": gate_type} if gate_type else {},
    )
    assert _section_ids(_flag([gate]), "attention") == []


def test_claim_expiring_soon_is_promoted_out_of_in_progress() -> None:
    """Rule 4: the observable replacement for the retired expired-claim rule —
    flag the claim BEFORE it silently vanishes from every Lithos read."""
    claim = ClaimRecord(agent="agent-zero", aspect="impl", expires_at=_ahead(minutes=6))
    working = _task("w", claims=(claim,), created_at=_ago(hours=1))
    sections = _flag([working], ready_ids={"w"})

    assert _section_ids(sections, "attention") == ["w"]
    assert _section_ids(sections, "in_progress") == []
    (row,) = sections["attention"]
    assert _rules(row) == ["claim-expiring"]
    assert "agent-zero" in row.attention[0].detail
    assert "6m remaining" in row.attention[0].detail
    # The claim itself still rides along for the row's claim chip.
    assert row.claims == (claim,)


def test_claim_expiring_respects_its_knob_and_ignores_far_off_claims() -> None:
    claim = ClaimRecord(agent="a", aspect="impl", expires_at=_ahead(minutes=45))
    working = _task("w", claims=(claim,), created_at=_ago(hours=1))

    assert _section_ids(_flag([working]), "attention") == []
    widened = _flag([working], policy=AttentionPolicy(claim_expiring_soon_minutes=60))
    assert _section_ids(widened, "attention") == ["w"]


def test_claim_without_a_readable_expiry_never_fires() -> None:
    """A claim Lens cannot judge must not be guessed at: no expires_at, no
    flag (the same never-fire-on-unreadable-time policy as the age rules)."""
    working = _task(
        "w",
        claims=(ClaimRecord(agent="a", aspect="impl", expires_at=""),),
        created_at=_ago(hours=1),
    )
    assert _section_ids(_flag([working]), "attention") == []


def test_stale_open_flags_workable_rows_and_respects_its_knob() -> None:
    """Rule 5: an open workable task nobody resolved, whatever section it sits
    in (here: blocked, so rule 6 cannot be the one firing)."""
    stale = _task("s", claims=(), created_at=_ago(days=9))
    blocked = [
        _blocked(stale, BlockerRecord(kind="task", task_id="pred", type="blocks"))
    ]
    sections = _flag([stale], blocked=blocked)

    assert _section_ids(sections, "attention") == ["s"]
    assert _section_ids(sections, "blocked") == []
    (row,) = sections["attention"]
    assert _rules(row) == ["stale-open"]
    assert "9d" in row.attention[0].detail

    relaxed = _flag(
        [stale], blocked=blocked, policy=AttentionPolicy(stale_open_age_days=30)
    )
    assert _section_ids(relaxed, "attention") == []
    assert _section_ids(relaxed, "blocked") == ["s"]


def test_ready_unclaimed_flags_only_ready_rows() -> None:
    """Rule 6 is ready-aware: at the same age, the READY row is "the fleet is
    not picking up work" and the BLOCKED row is correct behavior."""
    unpicked = _task("r", claims=(), created_at=_ago(hours=3))
    waiting = _task("b", claims=(), created_at=_ago(hours=3))
    sections = _flag(
        [unpicked, waiting],
        ready_ids={"r"},
        blocked=[
            _blocked(waiting, BlockerRecord(kind="task", task_id="pred", type="blocks"))
        ],
    )

    assert _section_ids(sections, "attention") == ["r"]
    assert _section_ids(sections, "blocked") == ["b"]
    assert _section_ids(sections, "ready") == []
    (row,) = sections["attention"]
    assert _rules(row) == ["ready-unclaimed"]
    assert "3h" in row.attention[0].detail


def test_ready_unclaimed_respects_its_knob_and_ignores_claimed_rows() -> None:
    claimed = _task(
        "c",
        claims=(ClaimRecord(agent="a", aspect="impl", expires_at=_ahead(hours=5)),),
        created_at=_ago(hours=3),
    )
    unpicked = _task("r", claims=(), created_at=_ago(hours=3))
    sections = _flag(
        [claimed, unpicked],
        ready_ids={"c", "r"},
        policy=AttentionPolicy(unclaimed_ready_age_minutes=600),
    )
    # Neither fires: the claimed row is being worked, the unclaimed row is
    # younger than the widened threshold.
    assert _section_ids(sections, "attention") == []
    assert _section_ids(sections, "in_progress") == ["c"]
    assert _section_ids(sections, "ready") == ["r"]


def test_rules_3_to_6_do_not_fire_exactly_at_their_threshold() -> None:
    """Boundary contract: the rules read "older than" / "below", so a row
    sitting EXACTLY on its threshold has not crossed it yet. Inclusive
    comparisons would flag a gate at 24h00m, a task on its 7th day, a ready row
    at 60m, and a claim with exactly 10m left — none of which is late."""
    policy = AttentionPolicy()
    gate = _task(
        "gate-1",
        task_type="gate",
        claims=(),
        created_at=_ago(hours=policy.gate_waiting_attention_hours),
        metadata={"gate_type": "human"},
    )
    stale = _task("s", claims=(), created_at=_ago(days=policy.stale_open_age_days))
    unpicked = _task(
        "r", claims=(), created_at=_ago(minutes=policy.unclaimed_ready_age_minutes)
    )
    claimed = _task(
        "c",
        claims=(
            ClaimRecord(
                agent="a",
                aspect="impl",
                expires_at=_ahead(minutes=policy.claim_expiring_soon_minutes),
            ),
        ),
        created_at=_ago(hours=1),
    )
    sections = _flag(
        [gate, stale, unpicked, claimed],
        ready_ids={"r"},
        blocked=[_blocked(stale, BlockerRecord(kind="task", task_id="pred"))],
    )

    assert _section_ids(sections, "attention") == []
    assert _section_ids(sections, "blocked") == ["s"]
    assert _section_ids(sections, "ready") == ["r"]
    assert _section_ids(sections, "in_progress") == ["c"]


def test_rules_3_to_6_fire_one_tick_past_their_threshold() -> None:
    """The other side of the boundary: a minute later every rule fires, so the
    strict comparison delays the flag rather than suppressing it."""
    policy = AttentionPolicy()
    gate = _task(
        "gate-1",
        task_type="gate",
        claims=(),
        created_at=_ago(hours=policy.gate_waiting_attention_hours, minutes=1),
        metadata={"gate_type": "human"},
    )
    stale = _task(
        "s", claims=(), created_at=_ago(days=policy.stale_open_age_days, minutes=1)
    )
    unpicked = _task(
        "r", claims=(), created_at=_ago(minutes=policy.unclaimed_ready_age_minutes + 1)
    )
    claimed = _task(
        "c",
        claims=(
            ClaimRecord(
                agent="a",
                aspect="impl",
                expires_at=_ahead(minutes=policy.claim_expiring_soon_minutes - 1),
            ),
        ),
        created_at=_ago(hours=1),
    )
    sections = _flag(
        [gate, stale, unpicked, claimed],
        ready_ids={"r"},
        blocked=[_blocked(stale, BlockerRecord(kind="task", task_id="pred"))],
    )

    assert sorted(_section_ids(sections, "attention")) == ["c", "gate-1", "r", "s"]
    assert _section_ids(sections, "blocked") == []
    assert _section_ids(sections, "ready") == []
    assert _section_ids(sections, "in_progress") == []


def test_row_firing_several_rules_appears_once_with_a_chip_per_rule() -> None:
    """De-dup + severity order: an old unclaimed ready row fires rules 5 and 6,
    and renders as ONE row carrying both chips, most severe first."""
    old_ready = _task("r", claims=(), created_at=_ago(days=20))
    sections = _flag([old_ready], ready_ids={"r"})

    assert _section_ids(sections, "attention") == ["r"]
    (row,) = sections["attention"]
    assert _rules(row) == ["stale-open", "ready-unclaimed"]


def test_repeated_unsatisfiable_blockers_collapse_to_one_chip() -> None:
    stuck = _task("s", claims=(), created_at=_ago(hours=1))
    sections = _flag(
        [stuck],
        blocked=[
            _blocked(
                stuck,
                BlockerRecord(
                    kind="blocker_unsatisfiable", task_id="p1", status="cancelled"
                ),
                BlockerRecord(
                    kind="blocker_unsatisfiable", task_id="p2", status="cancelled"
                ),
            )
        ],
    )
    (row,) = sections["attention"]
    assert _rules(row) == ["unsatisfiable"]
    assert "+1 more" in row.attention[0].detail


def test_attention_sorts_by_severity_then_oldest_first() -> None:
    """Severity tiers first; within a tier the most persistent problem leads."""
    young_cycle = _task("cycle-young", claims=(), created_at=_ago(hours=1))
    old_stale = _task("stale-old", claims=(), created_at=_ago(days=40))
    younger_stale = _task("stale-young", claims=(), created_at=_ago(days=8))
    sections = _flag(
        [young_cycle, younger_stale, old_stale],
        ready_ids={"stale-old", "stale-young"},
        blocked=[
            _blocked(young_cycle, BlockerRecord(kind="cycle", task_id="x", message="c"))
        ],
    )

    assert _section_ids(sections, "attention") == [
        "cycle-young",
        "stale-old",
        "stale-young",
    ]


@pytest.mark.parametrize(
    "created_at",
    [
        "",
        "not-a-date",
        # Parses fine, but converting to UTC leaves the datetime domain
        # (OverflowError, not ValueError). An upstream record carrying one must
        # degrade to "unreadable" like any other bad value — raising here would
        # 500 the whole dashboard for every operator until the row was fixed.
        "9999-12-31T23:59:59-05:00",
        "0001-01-01T00:00:00+12:00",
    ],
)
def test_unreadable_created_at_never_fires_an_age_rule(created_at: str) -> None:
    """A timestamp Lens cannot read must not manufacture a "stale" flag — and
    must not take the render down either."""
    row = _task("row", claims=(), created_at=created_at)
    sections = _flag([row], ready_ids={"row"})
    assert _section_ids(sections, "attention") == []
    assert _section_ids(sections, "ready") == ["row"]


def test_unreadable_claim_expiry_never_fires_or_raises() -> None:
    """Same guarantee on the claim side: an out-of-domain ``expires_at`` is
    unjudgeable, not a reason to flag (or to crash)."""
    working = _task(
        "w",
        claims=(
            ClaimRecord(
                agent="a", aspect="impl", expires_at="9999-12-31T23:59:59-05:00"
            ),
        ),
        created_at=_ago(hours=1),
    )
    sections = _flag([working])
    assert _section_ids(sections, "attention") == []
    assert _section_ids(sections, "in_progress") == ["w"]


def test_degraded_rows_are_never_promoted() -> None:
    """claims-unknown and not-classified rows are ones Lens could NOT place;
    flagging them would assert a problem from data it already called
    incomplete."""
    unknown = _task("u", claims=None, created_at=_ago(days=40))
    unclassified = _task("n", claims=(), created_at=_ago(days=40))
    partition = classify_open_tasks(
        [unknown, unclassified], ready_ids=set(), blocked=[]
    )
    sections = flag_attention(
        partition,
        [unknown, unclassified],
        blocked=[],
        policy=AttentionPolicy(),
        now=_NOW,
    )
    assert _section_ids(sections, "attention") == []
    assert _section_ids(sections, "claims_unknown") == ["u"]
    assert _section_ids(sections, "unclassified") == ["n"]
