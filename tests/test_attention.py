"""T1 slice 3 — Needs-attention severity model v2.

``flag_attention`` is the pure evaluator: six ordered rules over an
already-classified partition, promoting every flagged row out of its workable
section (single-placement). ``now`` is injected so each age-based rule is
deterministic, and each rule is pinned both firing and NOT firing (its knob
widened) — a rule that can only ever fire is a rule that will cry wolf.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from lithos_lens.attention import AttentionPolicy, flag_attention
from lithos_lens.config import (
    DEFAULT_TASKS_CLAIM_EXPIRING_SOON_MINUTES,
    DEFAULT_TASKS_DISPATCH_TRIGGER_TAG_PREFIXES,
    DEFAULT_TASKS_GATE_WAITING_ATTENTION_HOURS,
    DEFAULT_TASKS_STALE_OPEN_AGE_DAYS,
    DEFAULT_TASKS_UNCLAIMED_READY_AGE_MINUTES,
)
from lithos_lens.frontier import classify_open_tasks
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord
from lithos_lens.tasks import ClaimRecord, SectionName, SectionRow, TaskRecord

_NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)

# The dispatch trigger tag loom actually dispatches on — rule 6's scope is
# "tasks a fleet was expected to pick up", and this is what that looks like.
_TRIGGER = "trigger:story-develop"


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
    assert policy.dispatch_trigger_tag_prefixes == (
        DEFAULT_TASKS_DISPATCH_TRIGGER_TAG_PREFIXES
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


# --- T2b: rule 3b, loom's PR reconciliation escalations -------------------

_PR_URL = "https://example.invalid/pull/84"


def _pr_gate(
    *,
    state: str,
    since: str,
    detail: str = "",
    state_pr_url: str = _PR_URL,
    created_at: str | None = None,
) -> TaskRecord:
    return _task(
        "gate-pr",
        task_type="gate",
        claims=(),
        created_at=created_at or _ago(hours=2),
        metadata={
            "gate_type": "pr",
            "pr_url": _PR_URL,
            "reconciliation_state": state,
            "reconciliation_detail": detail,
            "reconciliation_since": since,
            "reconciliation_pr_url": state_pr_url,
        },
    )


def test_a_pr_needing_a_human_escalates_immediately_with_looms_own_reason() -> None:
    """Rule 3b: `needs_human` is loom's OWN conclusion that it cannot proceed
    alone, reached on a ten-minute sweep — so unlike rule 3 there is no wait to
    serve, and the supporting fact is loom's line verbatim (it names the review
    or the conflict, which Lens cannot derive)."""
    gate = _pr_gate(
        state="needs_human",
        since=_ago(minutes=4),
        detail="Reviewer requested changes Lens cannot resolve.",
    )

    (row,) = _flag([gate])["attention"]

    assert row.task.id == "gate-pr"
    assert _rules(row) == ["pr-needs-decision"]
    assert row.attention[0].detail == "Reviewer requested changes Lens cannot resolve."


def test_a_needs_human_pr_says_so_even_when_loom_wrote_no_detail() -> None:
    """The fact is loom's when loom wrote one; a chip with no supporting fact at
    all would be the one thing §5.2.2 forbids."""
    (row,) = _flag([_pr_gate(state="needs_human", since=_ago(minutes=4))])["attention"]

    assert row.attention[0].detail


def test_a_failing_pr_gate_escalates_only_after_the_human_gate_threshold() -> None:
    """`gate_failed` is loom's business while it is transient. Past the SAME
    threshold rule 3 uses, "nobody is coming" is the same judgement — and the
    clock is the STATE's, so a month-old PR gate that failed an hour ago is an
    hour-old failure."""
    fresh = _pr_gate(state="gate_failed", since=_ago(hours=3), created_at=_ago(days=40))
    assert _section_ids(_flag([fresh]), "attention") == []

    stuck = _pr_gate(
        state="gate_failed",
        since=_ago(hours=30),
        detail="required check `e2e` has failed 6 times.",
        created_at=_ago(days=40),
    )
    (row,) = _flag([stuck])["attention"]
    assert _rules(row) == ["pr-needs-decision"]
    # Both halves: how long Lens measured, then loom's own line.
    assert "1d" in row.attention[0].detail
    assert "required check `e2e` has failed 6 times." in row.attention[0].detail

    # Knob respected, exactly like rule 3.
    relaxed = _flag([stuck], policy=AttentionPolicy(gate_waiting_attention_hours=48))
    assert _section_ids(relaxed, "attention") == []


def test_a_stale_gate_failed_escalates_on_a_long_but_valid_stamp() -> None:
    """Rule 3b reads the state's own clock, so anything that stops
    ``reconciliation_since`` parsing stops the escalation — silently, and only
    for the gates that have been failing longest.

    A sub-microsecond ISO stamp is longer than 40 characters and parses fine;
    a display bound applied before the parse turned it into an ellipsis, and
    this rule's never-fire-on-an-unreadable-timestamp policy then (correctly,
    from bad input) declined to promote a two-day-old failure.
    """
    stamp = _ago(days=2).replace("+00:00", ".000000000000000000001+00:00")
    assert len(stamp) > 40
    gate = _pr_gate(state="gate_failed", since=stamp, detail="ci is red.")

    (row,) = _flag([gate])["attention"]

    assert _rules(row) == ["pr-needs-decision"]
    assert "2d" in row.attention[0].detail


@pytest.mark.parametrize(
    ("hours", "promoted"),
    [(23.99, False), (24, False), (24.01, True)],
    ids=["just below", "exactly at", "just above"],
)
def test_the_failing_pr_threshold_is_strict_at_the_boundary(
    hours: float, promoted: bool
) -> None:
    """ "OLDER than 24h", like every other age rule here: a state sitting exactly
    ON its threshold has not crossed it yet and fires one tick later. Pinned at
    the boundary because `>=` and `>` are one character apart and every value
    either side of it agrees."""
    gate = _pr_gate(state="gate_failed", since=_ago(hours=hours), detail="ci is red.")

    assert bool(_section_ids(_flag([gate]), "attention")) is promoted


@pytest.mark.parametrize(
    "state",
    [
        "ready_to_merge",
        "awaiting_review",
        "behind",
        "reconciling",
        "resolving_conflict",
    ],
)
def test_a_pr_that_is_merely_moving_stays_in_the_gates_section(state: str) -> None:
    """Only the two ESCALATIONS promote. `behind` and `reconciling` are loom
    working; `ready_to_merge` is done. Promoting those would make Needs
    attention a list of every open PR."""
    gate = _pr_gate(state=state, since=_ago(days=9), detail="loom is on it.")

    assert _section_ids(_flag([gate]), "attention") == []


@pytest.mark.parametrize(
    "state",
    ["needs_human ", " needs_human", "gate_failed\n"],
    ids=["trailing space", "leading space", "failed with newline"],
)
def test_a_state_that_only_looks_escalating_does_not_promote(state: str) -> None:
    """Rule 3b fires on loom's vocabulary, matched exactly.

    A promotion is the most disruptive thing this rule can do — it pulls the
    gate out of the Gates section and puts it at the top of the board — so it
    must rest on a state loom actually wrote, not on one Lens tidied into
    shape. The gate still renders, with an unknown-state badge.
    """
    gate = _pr_gate(state=state, since=_ago(days=9), detail="cannot vouch for this.")

    assert _section_ids(_flag([gate]), "attention") == []


def test_a_needs_human_state_about_another_pr_does_not_escalate() -> None:
    """Same rule as the badge, from the same code: a state describing a PR this
    gate no longer points at is not evidence about this gate — and promoting on
    it would pull a healthy gate out of the Gates section on stale data."""
    gate = _pr_gate(
        state="needs_human",
        since=_ago(hours=1),
        state_pr_url="https://example.invalid/pull/12",
    )

    assert _section_ids(_flag([gate]), "attention") == []


def test_a_failed_state_with_an_unreadable_since_never_fires() -> None:
    """The module's never-fire policy: a timestamp Lens cannot parse must not
    trigger an age rule, whichever surface wrote it."""
    gate = _pr_gate(state="gate_failed", since="a while ago", created_at=_ago(days=40))

    assert _section_ids(_flag([gate]), "attention") == []


def test_a_promoted_pr_gate_outranks_the_claim_and_age_rules() -> None:
    """Severity order (§5.2.2): a PR waiting on a decision sits with the gate
    rules, above the rules about work already in flight."""
    gate = _pr_gate(state="needs_human", since=_ago(minutes=5), detail="decide.")
    stale = _task("stale", claims=(), created_at=_ago(days=30))

    sections = _flag([gate, stale], ready_ids={"stale"})

    assert _section_ids(sections, "attention") == ["gate-pr", "stale"]


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
    unpicked = _task("r", claims=(), tags=(_TRIGGER,), created_at=_ago(hours=3))
    waiting = _task("b", claims=(), tags=(_TRIGGER,), created_at=_ago(hours=3))
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


# Rule 6's scope matrix (2026-09). The rule assumes a fleet picks ready work up
# within the hour; on the live corpus only tasks carrying a dispatch trigger tag
# are ever picked up automatically, so judging the rest emptied Ready and turned
# Needs attention into the de-facto Ready list. Each row below is one way the
# scope decision can go: which tag the task carries, which prefixes are
# configured, whether it is old enough, and whether anyone claimed it.
_CLAIM = (ClaimRecord(agent="a", aspect="impl", expires_at=_ahead(hours=5)),)

_READY_UNCLAIMED_SCOPE_CASES = [
    pytest.param(
        ("project:influx", _TRIGGER),
        (),
        {"hours": 3},
        None,
        "attention",
        f'On the ready frontier with "{_TRIGGER}", unclaimed for 3h.',
        id="dispatch-tagged-and-old-is-promoted",
    ),
    pytest.param(
        ("project:influx",),
        (),
        {"hours": 3},
        None,
        "ready",
        None,
        id="untagged-work-stays-ready-at-any-age",
    ),
    pytest.param(
        ("project:influx",),
        (),
        {"hours": 3},
        (),
        "attention",
        "On the ready frontier, unclaimed for 3h.",
        id="empty-prefix-list-restores-the-old-behaviour",
    ),
    pytest.param(
        (_TRIGGER,),
        (),
        {"minutes": 30},
        None,
        "ready",
        None,
        id="tagged-but-younger-than-the-threshold-stays-ready",
    ),
    pytest.param(
        (_TRIGGER,),
        _CLAIM,
        {"hours": 3},
        None,
        "in_progress",
        None,
        id="tagged-but-claimed-renders-in-progress",
    ),
    pytest.param(
        ("dispatch:robot-day",),
        (),
        {"hours": 3},
        ("trigger:", "dispatch:"),
        "attention",
        'On the ready frontier with "dispatch:robot-day", unclaimed for 3h.',
        id="either-configured-prefix-matches",
    ),
    pytest.param(
        ("dispatch:robot-day",),
        (),
        {"hours": 3},
        None,
        "ready",
        None,
        id="a-prefix-that-is-not-configured-does-not-match",
    ),
    pytest.param(
        # Two tags match, and they are in the OPPOSITE order to the prefixes.
        # The fact must name the task's first matching TAG, not the first
        # configured PREFIX: the chip explains this task, so it should quote
        # what the task carries rather than what the config happens to list
        # first. Every other case here has exactly one matching tag, so
        # iterating prefixes outer instead would pass all of them and silently
        # change this.
        ("dispatch:robot-day", _TRIGGER),
        (),
        {"hours": 3},
        ("trigger:", "dispatch:"),
        "attention",
        'On the ready frontier with "dispatch:robot-day", unclaimed for 3h.',
        id="the-fact-names-the-first-matching-tag-not-the-first-prefix",
    ),
]


@pytest.mark.parametrize(
    ("tags", "claims", "age", "prefixes", "section", "detail"),
    _READY_UNCLAIMED_SCOPE_CASES,
)
def test_ready_unclaimed_fires_only_for_dispatch_triggered_work(
    tags: tuple[str, ...],
    claims: tuple[ClaimRecord, ...],
    age: dict[str, float],
    prefixes: tuple[str, ...] | None,
    section: SectionName,
    detail: str | None,
) -> None:
    """Rule 6 judges only work some fleet was expected to pick up.

    One ready row per case, and it must land in exactly one section: promoted
    with a ``ready-unclaimed`` chip whose supporting fact NAMES the trigger tag,
    or left in Ready (or In progress) with no chip at all.
    """
    row = _task("r", claims=claims, tags=tags, created_at=_ago(**age))
    policy = (
        AttentionPolicy()
        if prefixes is None
        else AttentionPolicy(dispatch_trigger_tag_prefixes=prefixes)
    )
    sections = _flag([row], ready_ids={"r"}, policy=policy)

    assert _section_ids(sections, section) == ["r"]
    # Single placement: the row is in that section and nowhere else.
    for other in ("attention", "ready", "in_progress", "blocked"):
        if other != section:
            assert _section_ids(sections, cast(SectionName, other)) == []
    (rendered,) = sections[section]
    if section == "attention":
        assert _rules(rendered) == ["ready-unclaimed"]
        assert rendered.attention[0].detail == detail
    else:
        assert rendered.attention == ()


def test_ready_unclaimed_respects_its_knob_and_ignores_claimed_rows() -> None:
    claimed = _task(
        "c",
        claims=(ClaimRecord(agent="a", aspect="impl", expires_at=_ahead(hours=5)),),
        created_at=_ago(hours=3),
    )
    unpicked = _task("r", claims=(), tags=(_TRIGGER,), created_at=_ago(hours=3))
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
        "r",
        claims=(),
        tags=(_TRIGGER,),
        created_at=_ago(minutes=policy.unclaimed_ready_age_minutes),
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
        "r",
        claims=(),
        tags=(_TRIGGER,),
        created_at=_ago(minutes=policy.unclaimed_ready_age_minutes + 1),
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
    old_ready = _task("r", claims=(), tags=(_TRIGGER,), created_at=_ago(days=20))
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
    # `fact` is the whole sentence as text; `detail` is only the half before
    # the blocker it names, because the short id of that blocker is rendered
    # between the two halves as markup (§5.3).
    assert "+1 more" in row.attention[0].fact


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


def test_promoted_gate_keeps_claims_unknown_when_claims_were_not_returned() -> None:
    """A gate promoted by rule 3 must not claim to be unclaimed.

    ``TaskRecord.claims=None`` means the read did not return claims — the
    degraded case a server that ignores ``with_claims`` produces. Collapsing it
    to ``()`` gave the promoted row ``claim_state == "known_unclaimed"``, i.e. a
    confident "unclaimed" chip over data Lens does not have.
    """
    gate = TaskRecord(
        id="gate-1",
        title="Sign-off",
        status="open",
        task_type="gate",
        created_by="planner",
        created_at=_ago(days=3),
        metadata={"gate_type": "human"},
        claims=None,
    )

    sections = flag_attention(
        {"in_progress": (), "ready": (), "blocked": ()},
        [gate],
        blocked=[],
        policy=AttentionPolicy(),
        now=_NOW,
        index={gate.id: gate},
    )

    (row,) = sections["attention"]
    assert row.claims_unknown is True
    assert row.claim_state == "unknown"


def test_claims_unknown_row_is_promoted_on_a_proven_dead_end() -> None:
    """Regression: a cancelled blocker is proven whatever the claims say.

    ``claims_unknown`` rows were excluded from promotion wholesale, so a task
    that can NEVER become ready sat in the degraded group with no reason chip —
    the frontier that proves it dead had answered fine; only the claim data was
    missing. The row is promoted on rules 1-2 alone and KEEPS its
    claims-unknown marker, so the board says both true things at once.
    """
    stuck = TaskRecord(
        id="stuck",
        title="Stuck task",
        status="open",
        created_by="planner",
        created_at=_ago(minutes=5),
        claims=None,
    )
    row = SectionRow(task=stuck, claims=(), claims_unknown=True)

    sections = flag_attention(
        {"in_progress": (), "ready": (), "blocked": (), "claims_unknown": (row,)},
        [stuck],
        blocked=[
            BlockedTaskRecord(
                task=stuck,
                blockers=(
                    BlockerRecord(
                        kind="blocker_unsatisfiable",
                        task_id="dead",
                        message="blocker cancelled",
                    ),
                ),
            )
        ],
        policy=AttentionPolicy(),
        now=_NOW,
        index={stuck.id: stuck},
    )

    # Single placement: it left the degraded group for the attention list…
    assert sections["claims_unknown"] == ()
    (promoted,) = sections["attention"]
    assert [reason.rule for reason in promoted.attention] == ["unsatisfiable"]
    # …without pretending the claims are known.
    assert promoted.claims_unknown is True
    assert promoted.claim_state == "unknown"


def test_claims_unknown_row_is_not_promoted_on_the_claim_or_age_rules() -> None:
    """The other half of the same rule: only the structural evidence carries.

    An OLD claims-unknown row stays put. Rule 5 is claim-independent, but a
    server that ignores ``with_claims`` puts every row in this group, and
    flagging each old one would bury the dead ends that rules 1-2 find — the
    list is a severity list, not an inventory.
    """
    ancient = TaskRecord(
        id="ancient",
        title="Ancient task",
        status="open",
        created_by="planner",
        created_at=_ago(days=90),
        claims=None,
    )
    row = SectionRow(task=ancient, claims=(), claims_unknown=True)

    sections = flag_attention(
        {"in_progress": (), "ready": (), "blocked": (), "claims_unknown": (row,)},
        [ancient],
        blocked=[],
        policy=AttentionPolicy(),
        now=_NOW,
        index={ancient.id: ancient},
    )

    assert sections["attention"] == ()
    assert [r.task.id for r in sections["claims_unknown"]] == ["ancient"]
