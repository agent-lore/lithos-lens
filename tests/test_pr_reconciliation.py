"""T2b — loom's PR reconciliation state (``pr_reconciliation.py``).

The vocabulary is loom's, not ours: lens treats it as opaque strings with a
known list (lithos-loom ``docs/SPECIFICATION.md`` §2.2, PRD S7). So the tests
below pin the two halves of that contract — that every state loom documents has
a rendering, in the severity order the Gates section orders by, and that
everything Lens does NOT recognise still reaches the operator as its own text
rather than as a crash, a blank, or a borrowed colour.

The rest is the module's one judgement call: a state describes ONE PR, so a
state about a replaced PR is withheld rather than shown under the new one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from lithos_lens.pr_reconciliation import (
    GATE_FAILED_STATE,
    NEEDS_HUMAN_STATE,
    NO_STATE_SEVERITY,
    PR_GATE_TYPE,
    UNKNOWN_STATE_SEVERITY,
    known_states,
    reconciliation_of,
)
from lithos_lens.tasks import Reconciliation, TaskRecord

_NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


def _pr_gate(
    *,
    state: Any = "needs_human",
    detail: str = "Review requested changes on the migration plan.",
    since: Any = "2026-09-13T10:00:00+00:00",
    pr_url: Any = "https://github.com/agent-lore/lithos-lens/pull/84",
    state_pr_url: Any | None = None,
    extra: dict[str, Any] | None = None,
    status: str = "open",
) -> TaskRecord:
    """A ``pr`` gate carrying loom's four keys, all four independently overridable.

    ``state_pr_url`` defaults to ``pr_url`` — the healthy case, where the state
    describes the PR the gate points at.
    """
    metadata: dict[str, Any] = {
        "gate_type": PR_GATE_TYPE,
        "pr_url": pr_url,
        "reconciliation_state": state,
        "reconciliation_detail": detail,
        "reconciliation_since": since,
        "reconciliation_pr_url": pr_url if state_pr_url is None else state_pr_url,
    }
    metadata.update(extra or {})
    return TaskRecord(
        id="gate-pr",
        title="Land the migration PR",
        status=status,  # type: ignore[arg-type]
        task_type="gate",
        created_at="2026-09-10T09:00:00+00:00",
        metadata=metadata,
    )


def test_every_state_loom_documents_has_one_rendering_in_severity_order() -> None:
    """The PRD S7 vocabulary, in the order §5.2.3 orders PR gates by.

    This is the whole mapping asserted as data: loom's closed set, the wording
    the badge shows, and the colour tone — most severe first, which is also the
    Gates section's ordering key. A state added upstream without a rendering
    here shows as `unknown` (below), so this list failing is the signal that the
    two sides have drifted.
    """
    assert [(style.state, style.label, style.tone) for style in known_states()] == [
        ("needs_human", "needs human", "danger"),
        ("gate_failed", "gate failed", "warn"),
        ("behind", "behind", "warn"),
        ("reconciling", "reconciling", "info"),
        ("resolving_conflict", "resolving conflict", "info"),
        ("awaiting_review", "awaiting review", "neutral"),
        ("ready_to_merge", "ready to merge", "ok"),
    ]


def test_severity_ranks_follow_the_mapping_and_leave_room_below_it() -> None:
    """Ordering comes from the mapping's stated ranks, so the two cannot
    disagree — and both "a word we do not know" and "no state at all" rank
    below every real one."""
    ranks = [
        reconciliation_of(_pr_gate(state=style.state), gate_type=PR_GATE_TYPE)
        for style in known_states()
    ]
    severities = [state.severity for state in ranks if state is not None]
    assert len(severities) == len(known_states())
    assert severities == sorted(severities)
    assert max(severities) < UNKNOWN_STATE_SEVERITY < NO_STATE_SEVERITY


def test_the_two_in_flight_states_are_one_tier_and_nothing_else_ties() -> None:
    """§5.2.3 ranks "the in-flight pair" as ONE position: `reconciling` and
    `resolving_conflict` are both "loom is working on it", so neither outranks
    the other and age decides between them (``test_gates`` pins the tie-break).

    Deriving the rank from the list index made them differ by one forever,
    which reads as an ordering rule nobody wrote — so the ranks are asserted as
    the tiering they are, and every OTHER state is asserted distinct so a
    future typo cannot silently merge two real tiers.
    """
    rank = {style.state: style.severity for style in known_states()}

    assert rank["reconciling"] == rank["resolving_conflict"]
    assert rank["behind"] < rank["reconciling"] < rank["awaiting_review"]
    others = [state for state in rank if state not in {"resolving_conflict"}]
    assert len(set(rank[state] for state in others)) == len(others)


def test_the_badge_carries_the_state_and_the_age_of_the_change() -> None:
    """ "needs human · 2h": the age is measured from ``reconciliation_since`` —
    when the STATE last changed — not from the gate's own creation."""
    state = reconciliation_of(_pr_gate(), gate_type=PR_GATE_TYPE, now=_NOW)

    assert state is not None
    assert state.badge_text == "needs human · 2h"
    assert state.tone == "danger"
    assert state.slug == "needs_human"
    assert state.detail == "Review requested changes on the migration plan."
    assert state.since == "2026-09-13T10:00:00+00:00"


def test_a_ready_to_merge_pr_renders_green_and_is_not_an_escalation() -> None:
    state = reconciliation_of(
        _pr_gate(state="ready_to_merge", detail="All gates green."),
        gate_type=PR_GATE_TYPE,
        now=_NOW,
    )

    assert state is not None
    assert (state.label, state.tone) == ("ready to merge", "ok")
    # Not an escalation: neither state ``attention.py`` rule 3b promotes on.
    assert state.state not in {NEEDS_HUMAN_STATE, GATE_FAILED_STATE}


def test_an_unknown_state_renders_as_its_own_text_in_the_unknown_tone() -> None:
    """loom owns the vocabulary and may extend it. An unrecognised value is not
    an error: it renders as the raw text it is, greyed, and its MARKUP hook
    collapses to `unknown` so a peer-written value cannot borrow another
    state's colour — or any other class in the sheet."""
    state = reconciliation_of(
        _pr_gate(state="awaiting_second_review"), gate_type=PR_GATE_TYPE, now=_NOW
    )

    assert state is not None
    assert state.label == "awaiting_second_review"
    assert state.badge_text == "awaiting_second_review · 2h"
    assert state.tone == "unknown"
    assert state.slug == "unknown"
    assert state.severity == UNKNOWN_STATE_SEVERITY


def test_a_state_about_another_pr_is_withheld_entirely() -> None:
    """A replacement PR on the same gate starts fresh, and until loom's next
    sweep rewrites the keys the gate still carries the OLD PR's state. Rendering
    it would state the previous PR's condition under the current one."""
    gate = _pr_gate(
        state="ready_to_merge",
        state_pr_url="https://github.com/agent-lore/lithos-lens/pull/12",
    )

    assert reconciliation_of(gate, gate_type=PR_GATE_TYPE, now=_NOW) is None


def test_the_pr_match_is_not_weakened_by_the_length_bound() -> None:
    """Two long urls sharing a prefix are DIFFERENT PRs. The comparison runs on
    the raw values for exactly this reason: bounding them first would truncate
    both to the same text and let a stale state through the one check that
    exists to catch it."""
    base = "https://github.example.invalid/" + "o" * 300 + "/pull/"
    gate = _pr_gate(pr_url=base + "84", state_pr_url=base + "12")

    assert reconciliation_of(gate, gate_type=PR_GATE_TYPE, now=_NOW) is None


@pytest.mark.parametrize(
    "state_pr_url",
    [
        " https://github.com/agent-lore/lithos-lens/pull/84",
        "https://github.com/agent-lore/lithos-lens/pull/84 ",
        "https://github.com/agent-lore/lithos-lens/pull/84\n",
    ],
    ids=["leading space", "trailing space", "trailing newline"],
)
def test_two_urls_that_are_not_the_same_string_are_not_a_match(
    state_pr_url: str,
) -> None:
    """The check is EXACT equality on the values loom wrote, not a comparison of
    Lens-cleaned versions of them.

    Trimming both sides first reads as harmless tidying, and is not: it makes
    Lens rule that two different metadata values name the same PR, which is the
    one judgement this check exists to refuse. Whitespace in a url loom writes
    means loom wrote something Lens cannot vouch for, and the honest answer to
    "is this state about this PR?" is then "cannot tell" — the raw keys stay on
    the row either way.
    """
    gate = _pr_gate(state_pr_url=state_pr_url)

    assert reconciliation_of(gate, gate_type=PR_GATE_TYPE, now=_NOW) is None


@pytest.mark.parametrize(
    "dropped",
    [
        ("pr_url", "reconciliation_pr_url"),
        ("pr_url",),
        ("reconciliation_pr_url",),
    ],
    ids=["neither url", "no gate url", "no state url"],
)
@pytest.mark.parametrize("blanked", [False, True], ids=["absent", "empty"])
def test_an_unconfirmable_pr_renders_no_badge(
    dropped: tuple[str, ...], blanked: bool
) -> None:
    """The match is POSITIVE evidence, not the absence of a contradiction.

    loom writes ``reconciliation_pr_url`` with every state exactly so "is this
    state about this PR?" has an answer; with either url missing (or blank)
    there is nothing to answer it with, and a badge would answer it anyway —
    the failure mode being a `needs_human` escalation promoted on a state that
    might belong to a PR this gate no longer points at. Nothing is hidden: the
    raw keys stay among the row's advisory chips.
    """
    gate = _pr_gate()
    metadata = {
        key: ("   " if blanked and key in dropped else value)
        for key, value in gate.metadata.items()
        if blanked or key not in dropped
    }

    assert (
        reconciliation_of(
            TaskRecord(
                id=gate.id,
                title=gate.title,
                status="open",
                task_type="gate",
                metadata=metadata,
            ),
            gate_type=PR_GATE_TYPE,
            now=_NOW,
        )
        is None
    )


@pytest.mark.parametrize(
    "state",
    ["needs_human ", " needs_human", "needs_human\n", " ready_to_merge", "\tbehind"],
    ids=["trailing space", "leading space", "trailing newline", "green", "tab"],
)
def test_a_state_that_only_looks_like_a_known_one_stays_unknown(state: str) -> None:
    """The vocabulary is matched EXACTLY, on the value as loom wrote it.

    Trimming first reads as harmless tidying and is the same mistake as
    trimming the PR url: it makes Lens rule that `"needs_human "` is loom's
    `needs_human`, and that verdict is not cosmetic — it takes the red badge,
    the top of the Gates ordering and an IMMEDIATE promotion into Needs
    attention on a value loom's own closed set does not contain. The honest
    answer to a string Lens cannot find in the mapping is the same whatever it
    resembles: grey, unknown, and its own text (whitespace included, since the
    contract is that the operator sees what loom wrote).
    """
    rendered = reconciliation_of(
        _pr_gate(state=state), gate_type=PR_GATE_TYPE, now=_NOW
    )

    assert rendered is not None
    assert rendered.state == state
    assert rendered.label == state
    assert (rendered.slug, rendered.tone) == ("unknown", "unknown")
    assert rendered.severity == UNKNOWN_STATE_SEVERITY
    # …and it is neither of the states rule 3b escalates on.
    assert rendered.state not in {NEEDS_HUMAN_STATE, GATE_FAILED_STATE}


@pytest.mark.parametrize("status", ["completed", "cancelled"])
@pytest.mark.parametrize("state", ["needs_human", "ready_to_merge"])
def test_a_resolved_gate_carries_no_live_state(status: str, state: str) -> None:
    """loom sweeps still-OPEN PR gates, so a resolved gate's four keys are the
    last snapshot taken before it closed — frozen, and never refreshed again.

    The badge is present tense by construction ("what is this PR doing right
    now?"), so on a completed or cancelled gate it would assert a live
    condition about a PR nothing is watching: a gate someone completed to
    unblock the work would fly a red `needs human` for ever, and a merged one
    would go on claiming `ready to merge`. The keys are not lost — the detail
    page's metadata table renders them, which is where history belongs.
    """
    resolved = _pr_gate(state=state, status=status)
    still_open = _pr_gate(state=state)

    assert reconciliation_of(resolved, gate_type=PR_GATE_TYPE, now=_NOW) is None
    # …and the only thing that changed is the status.
    assert reconciliation_of(still_open, gate_type=PR_GATE_TYPE, now=_NOW) is not None


@pytest.mark.parametrize("gate_type", ["human", "timer", "ci", "external_task", ""])
def test_only_pr_gates_carry_a_reconciliation_state(gate_type: str) -> None:
    """These keys are loom's PR sweep. On any other gate they are stray
    metadata, and a badge built from them would be a fiction."""
    assert reconciliation_of(_pr_gate(), gate_type=gate_type) is None


@pytest.mark.parametrize("state", ["", "   ", None, 7, {"state": "needs_human"}, []])
def test_a_missing_or_non_scalar_state_renders_no_badge(state: Any) -> None:
    """loom writes flat scalars. Anything else is malformed rather than
    meaningful — and it stays visible either way, as the Gates section's
    advisory chips, which render exactly when this returns None."""
    assert reconciliation_of(_pr_gate(state=state), gate_type=PR_GATE_TYPE) is None


def test_an_unreadable_since_keeps_the_state_and_drops_only_the_age() -> None:
    """The never-guess rule, applied to the badge: a stamp Lens cannot parse
    stays visible as the text it is and produces NO age, rather than a duration
    invented from the gate's creation time."""
    state = reconciliation_of(
        _pr_gate(since="last tuesday"), gate_type=PR_GATE_TYPE, now=_NOW
    )

    assert state is not None
    assert state.badge_text == "needs human"
    assert state.since == "last tuesday"
    assert state.age == ""


def test_a_since_in_the_future_reads_as_zero_rather_than_negative() -> None:
    """Clock skew between loom and Lens is not a story the badge should tell."""
    state = reconciliation_of(
        _pr_gate(since=(_NOW + timedelta(hours=3)).isoformat()),
        gate_type=PR_GATE_TYPE,
        now=_NOW,
    )

    assert state is not None and state.badge_text == "needs human · 0m"


def test_the_detail_is_bounded_to_looms_own_stated_line_length() -> None:
    """``metadata`` is peer-written whatever loom's own contract says, and this
    one reaches the page as the badge's tooltip. Capped at loom's OWN stated
    domain — one line of ≤200 characters — not at a number Lens invented."""
    state = reconciliation_of(
        _pr_gate(detail="d" * 5_000), gate_type=PR_GATE_TYPE, now=_NOW
    )

    assert state is not None
    assert len(state.detail) == 200 and state.detail.endswith("…")


def test_an_unreadable_stamp_is_bounded_only_after_it_fails_to_parse() -> None:
    """The display bound on ``reconciliation_since`` applies to text that is
    already known NOT to be an instant — it is never a filter in front of the
    parser."""
    state = reconciliation_of(
        _pr_gate(since="s" * 500), gate_type=PR_GATE_TYPE, now=_NOW
    )

    assert state is not None
    assert len(state.since) == 40 and state.since.endswith("…")
    assert state.age == ""


def test_a_long_but_valid_stamp_still_dates_the_badge() -> None:
    """An ISO instant has no length worth asserting: the fractional second is
    arbitrary-precision and ``datetime.fromisoformat`` accepts more digits than
    a 40-character bound leaves room for.

    Bounding before parsing turned exactly this value into an ellipsized string
    the parser then rejected — so the badge silently lost its age, and (see
    ``test_attention``) an old ``gate_failed`` state stopped escalating. The
    stamp is parsed from the whole value; only what fails to parse is bounded.
    """
    long_stamp = "2026-09-13T10:00:00.000000000000000000001+00:00"
    short_stamp = "2026-09-13T10:00:00+00:00"
    assert len(long_stamp) > 40

    long_state = reconciliation_of(
        _pr_gate(since=long_stamp), gate_type=PR_GATE_TYPE, now=_NOW
    )
    short_state = reconciliation_of(
        _pr_gate(since=short_stamp), gate_type=PR_GATE_TYPE, now=_NOW
    )

    assert long_state is not None and short_state is not None
    # The same instant written two ways renders the same badge: precision the
    # datetime domain cannot hold changes nothing, and length changes nothing.
    assert long_state.badge_text == short_state.badge_text == "needs human · 2h"
    assert long_state.since == short_state.since == "2026-09-13T10:00:00+00:00"
    assert "…" not in long_state.since


def test_an_unknown_state_is_never_shortened_however_long_it_is() -> None:
    """The opaque-string contract is the whole point of the unknown tone: loom
    owns the vocabulary and may extend it, and a capped badge would render a
    DIFFERENT value ("a_very_long_futur…") for any state that outgrew the cap —
    exactly the upstream truth the operator came to the badge for.

    The markup tokens stay closed at any length, so an unbounded value buys no
    class and no selector hook; that is what makes the text safe to render."""
    long_state = "awaiting_" + "second_" * 20 + "review"
    state = reconciliation_of(
        _pr_gate(state=long_state), gate_type=PR_GATE_TYPE, now=_NOW
    )

    assert state is not None
    assert len(long_state) > 100
    assert state.state == long_state
    assert state.label == long_state
    assert state.badge_text == f"{long_state} · 2h"
    assert (state.slug, state.tone) == ("unknown", "unknown")


def test_the_badge_text_drops_the_age_separator_when_there_is_no_age() -> None:
    """The one thing the view model decides for itself. A dangling "· " would
    read as a value that failed to render rather than as one Lens declined to
    invent."""
    assert Reconciliation(state="x", label="needs human").badge_text == "needs human"
    assert (
        Reconciliation(state="x", label="needs human", age="2h").badge_text
        == "needs human · 2h"
    )
