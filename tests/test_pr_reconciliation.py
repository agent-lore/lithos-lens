"""T2b — loom's PR reconciliation state (``reconciliation.py``).

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
        status="open",
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
    """Ordering is the mapping's index, so the two cannot disagree — and both
    "a word we do not know" and "no state at all" rank below every real one."""
    ranks = [
        reconciliation_of(_pr_gate(state=style.state), gate_type=PR_GATE_TYPE)
        for style in known_states()
    ]
    severities = [state.severity for state in ranks if state is not None]
    assert severities == sorted(severities) == list(range(len(known_states())))
    assert max(severities) < UNKNOWN_STATE_SEVERITY < NO_STATE_SEVERITY


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


def test_a_gate_carrying_no_pr_url_on_either_side_still_shows_its_state() -> None:
    """Both absent compares equal, which is the honest reading: there is no
    second PR for the state to be about."""
    gate = _pr_gate()
    metadata = {
        key: value
        for key, value in gate.metadata.items()
        if key not in {"pr_url", "reconciliation_pr_url"}
    }
    state = reconciliation_of(
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

    assert state is not None and state.slug == "needs_human"


def test_a_gate_url_with_no_state_url_beside_it_is_not_a_match() -> None:
    """The other half of the same rule: loom writes ``reconciliation_pr_url``
    with every state, so a state missing it cannot be confirmed to describe the
    PR this gate points at — and an unconfirmable state is not shown."""
    gate = _pr_gate()
    metadata = {
        key: value
        for key, value in gate.metadata.items()
        if key != "reconciliation_pr_url"
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


def test_peer_written_values_are_bounded_before_they_reach_the_markup() -> None:
    """``metadata`` is peer-written whatever loom's own contract says, and all
    three of these reach the page — the state as a label, the detail as a
    tooltip, the stamp as an attribute."""
    state = reconciliation_of(
        _pr_gate(state="x" * 500, detail="d" * 5_000, since="s" * 500),
        gate_type=PR_GATE_TYPE,
        now=_NOW,
    )

    assert state is not None
    assert len(state.state) == 40 and state.state.endswith("…")
    assert len(state.detail) == 200 and state.detail.endswith("…")
    assert len(state.since) == 40 and state.since.endswith("…")
    # …and an unrecognisable state is still rendered, as its own bounded text.
    assert state.tone == "unknown"


def test_the_badge_text_drops_the_age_separator_when_there_is_no_age() -> None:
    """The one thing the view model decides for itself. A dangling "· " would
    read as a value that failed to render rather than as one Lens declined to
    invent."""
    assert Reconciliation(state="x", label="needs human").badge_text == "needs human"
    assert (
        Reconciliation(state="x", label="needs human", age="2h").badge_text
        == "needs human · 2h"
    )
