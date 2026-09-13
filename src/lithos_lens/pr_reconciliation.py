"""loom's PR reconciliation state, as the board renders it (PRD S7).

``lithos-loom`` keeps a PR gate's *current* state on the gate itself: on every
sweep it writes four flat scalar metadata keys onto every still-open ``pr``
gate — ``reconciliation_state`` (the closed vocabulary below),
``reconciliation_detail`` (one line, why), ``reconciliation_since`` (when the
state last *changed*) and ``reconciliation_pr_url`` (the PR the state
describes). The state is DERIVED, not tracked: it answers "what is this PR
doing right now?", which is why findings stay the history and this is a
snapshot that can be rewritten wholesale on the next sweep. Contract source:
lithos-loom ``docs/SPECIFICATION.md`` §2.2 "Reconciliation state (PRD S7)".

Lens treats the vocabulary as OPAQUE STRINGS with a known list. ``_STYLES``
below is the one mapping from a state to how it renders — its text and its
colour tone — and every surface (the gate row, the side panel, the detail page)
reads it rather than string-matching a state in a template. A value outside the
list is not an error: loom may add one, so it renders as its own raw text in the
unknown tone, and a value that is not a string at all falls back to the generic
advisory metadata chips the Gates section already draws.

Two honesty rules the rest of the module exists to keep:

- **the state must be about THIS PR.** A gate whose PR was replaced keeps the
  previous PR's state until the next sweep rewrites it, so the badge renders
  only when ``reconciliation_pr_url`` and the gate's own ``pr_url`` are both
  present and equal — positive evidence, not the absence of a contradiction;
- **nothing here is Lens's own judgement.** The detail line is loom's, shown
  verbatim; the age is measured from loom's own ``reconciliation_since`` and is
  empty when that stamp cannot be read, never guessed.

This module knows nothing about gates (``gates.py`` imports IT, not the other
way round), so the caller passes the gate type it already resolved. The view
model it fills in — :class:`~lithos_lens.tasks.Reconciliation` — lives beside
``SectionRow`` for the reason given in its own docstring: the rows that carry a
badge are defined there, and defining it here would close an import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lithos_lens.tasks import (
    Reconciliation,
    TaskRecord,
    humanize_age,
    parse_timestamp,
)

# The gate type loom's reconciliation sweep writes on. ``gates.py`` folds this
# into KNOWN_GATE_TYPES, so the two surfaces cannot drift on what a PR gate is.
PR_GATE_TYPE = "pr"

# The four keys loom writes, plus the gate's own PR url the fourth is matched
# against. Named here because both the badge (which reads them) and the Gates
# section's advisory chips (which stop repeating them once the badge renders)
# need the same list.
RECONCILIATION_STATE_KEY = "reconciliation_state"
RECONCILIATION_DETAIL_KEY = "reconciliation_detail"
RECONCILIATION_SINCE_KEY = "reconciliation_since"
RECONCILIATION_PR_URL_KEY = "reconciliation_pr_url"
PR_URL_KEY = "pr_url"

RECONCILIATION_KEYS = frozenset(
    {
        RECONCILIATION_STATE_KEY,
        RECONCILIATION_DETAIL_KEY,
        RECONCILIATION_SINCE_KEY,
        RECONCILIATION_PR_URL_KEY,
    }
)

# The two states that are an ESCALATION rather than progress: loom has stopped
# making progress on its own and wants a person (``attention.py`` rule 3b).
NEEDS_HUMAN_STATE = "needs_human"
GATE_FAILED_STATE = "gate_failed"

# Bounds, and the deliberate absence of one.
#
# The STATE is NOT capped. Its whole contract is that loom owns the vocabulary
# and may extend it, so a value Lens does not recognise is rendered as its own
# text — and a cap would quietly render a DIFFERENT value ("a_very_long_futur…")
# for any future state that outgrew it, which is the one failure mode the
# opaque-string contract exists to prevent. Peer-written text of unknown length
# is not a new exposure on these rows either: a task's title and description
# reach every row of the board verbatim already. The markup TOKENS built from
# the state stay closed whatever its length (``slug``/``tone`` collapse to
# ``unknown``), so an unbounded value buys no class and no selector hook.
#
# ``reconciliation_detail`` is capped at its own stated domain rather than at a
# policy of ours: loom documents it as one line of at most 200 characters.
#
# ``reconciliation_since`` is capped only AFTER it has failed to parse — the
# cap is a bound on how much unreadable text a badge will show, never a filter
# in front of the parser. An ISO instant has no length limit worth asserting
# (the fractional second is arbitrary-precision, and ``fromisoformat`` accepts
# more digits than 40 characters leaves room for), so bounding first would have
# turned a perfectly good stamp into an ellipsized one, dropped the age off the
# badge, and stopped an old ``gate_failed`` state from ever escalating.
_DETAIL_CAP = 200
_UNREADABLE_SINCE_CAP = 40


@dataclass(frozen=True)
class ReconciliationStyle:
    """How ONE reconciliation state renders: its text, its colour, its rank.

    ``tone`` is a markup-safe token, never a colour: the stylesheet owns what
    "danger" looks like, and a template that string-matched the state would be
    a second copy of this vocabulary waiting to drift from it.

    ``severity`` is the ordering rank (§5.2.3), stated rather than derived from
    position: the two IN-FLIGHT states are one tier, so they must share a rank
    — deriving it from the list index would make ``reconciling`` outrank
    ``resolving_conflict`` forever and silently swallow the age tie-break the
    section promises.
    """

    state: str
    label: str
    tone: str
    severity: int


# THE mapping: loom's closed vocabulary, MOST SEVERE FIRST. It is load-bearing
# twice over — the badge colour and the ordering of PR gates inside the Gates
# section (§5.2.3) — so this list is the single place either can be changed.
# Ranks are not dense: ``reconciling`` and ``resolving_conflict`` are the same
# tier ("loom is working on it"), ordered against each other by age alone.
_STYLES: tuple[ReconciliationStyle, ...] = (
    ReconciliationStyle(NEEDS_HUMAN_STATE, "needs human", "danger", 0),
    ReconciliationStyle(GATE_FAILED_STATE, "gate failed", "warn", 1),
    ReconciliationStyle("behind", "behind", "warn", 2),
    ReconciliationStyle("reconciling", "reconciling", "info", 3),
    ReconciliationStyle("resolving_conflict", "resolving conflict", "info", 3),
    ReconciliationStyle("awaiting_review", "awaiting review", "neutral", 4),
    ReconciliationStyle("ready_to_merge", "ready to merge", "ok", 5),
)

_BY_STATE: dict[str, ReconciliationStyle] = {style.state: style for style in _STYLES}

# The markup token and the ordering rank for a state loom has that Lens does
# not. It sorts after every state Lens knows (Lens cannot rank a word it has
# never seen, and guessing would put an unknown word above a real escalation);
# a PR gate with NO state at all sorts after that again.
UNKNOWN_STATE_TONE = "unknown"
UNKNOWN_STATE_SEVERITY = max(style.severity for style in _STYLES) + 1
NO_STATE_SEVERITY = UNKNOWN_STATE_SEVERITY + 1


def known_states() -> tuple[ReconciliationStyle, ...]:
    """The rendered vocabulary, most severe first — the mapping, read-only."""
    return _STYLES


def reconciliation_of(
    task: TaskRecord,
    *,
    gate_type: str,
    now: datetime | None = None,
) -> Reconciliation | None:
    """The PR reconciliation state to RENDER for a gate, or None for none.

    None — no badge at all — in three cases, which is the whole judgement this
    module makes:

    - the gate is not a ``pr`` gate. Nothing else carries these keys, and a
      badge built from stray metadata on a timer gate would be a fiction;
    - ``reconciliation_state`` is absent, empty, or not a string. loom writes
      flat scalars, so a container here is malformed rather than meaningful —
      and it stays visible either way, as the generic advisory chips that render
      whenever this returns None;
    - the state cannot be CONFIRMED to be about the PR this gate points at (see
      ``_describes_this_pr``).

    ``now`` supplies the badge's age and is optional: without it the badge
    renders without one.
    """
    if gate_type != PR_GATE_TYPE:
        return None
    metadata = task.metadata
    state = _scalar_text(metadata.get(RECONCILIATION_STATE_KEY))
    if not state:
        return None
    if not _describes_this_pr(metadata):
        return None
    since = _scalar_text(metadata.get(RECONCILIATION_SINCE_KEY))
    # Parsed from the WHOLE value, before any display bound touches it.
    parsed_since = parse_timestamp(since)
    style = _BY_STATE.get(state)
    return Reconciliation(
        state=state,
        # THE mapping, applied — the only place a state becomes a wording, a
        # colour or a rank. An unknown state keeps its own text and takes the
        # unknown token for every hook built from it.
        label=style.label if style is not None else state,
        slug=style.state if style is not None else UNKNOWN_STATE_TONE,
        tone=style.tone if style is not None else UNKNOWN_STATE_TONE,
        severity=style.severity if style is not None else UNKNOWN_STATE_SEVERITY,
        detail=_bounded(
            _scalar_text(metadata.get(RECONCILIATION_DETAIL_KEY)), _DETAIL_CAP
        ),
        # Normalized when it parses; bounded raw text only when it does NOT —
        # an unreadable stamp stays visible rather than vanishing, and drives no
        # age rather than a guessed one.
        since=(
            parsed_since.isoformat()
            if parsed_since is not None
            else _bounded(since, _UNREADABLE_SINCE_CAP)
        ),
        age=(
            humanize_age(now - parsed_since)
            if now is not None and parsed_since is not None
            else ""
        ),
    )


def _describes_this_pr(metadata: Mapping[str, Any]) -> bool:
    """Whether the state is CONFIRMED to be about the PR this gate points at.

    loom writes ``reconciliation_pr_url`` with every state precisely so this
    question has an answer, so the test is positive evidence rather than the
    absence of contradiction: both urls must be present, non-empty strings and
    exactly equal. A gate whose PR was replaced keeps the previous PR's state
    until the next sweep, and one whose url is missing on either side offers
    nothing to check it against — in both cases Lens cannot say which PR the
    state describes, and a badge would say it anyway. Nothing is hidden by
    withholding it: the four raw keys stay on the row as advisory chips and in
    the detail page's metadata table.

    Compared EXACTLY, on the values as loom wrote them: no bounding (truncating
    first would make two long urls sharing a prefix compare equal) and no
    normalisation either. ``strip()`` appears once, to decide whether there is a
    url here at all — a whitespace-only value is an absent one. It is not
    applied to the comparison, because "these two metadata values are the same
    url" is a question about the values, and a Lens-side cleanup that made
    ``" …/pull/12 "`` equal to ``"…/pull/12"`` would be Lens deciding two
    different strings name the same PR — the judgement this check exists to
    refuse.
    """
    state_url = metadata.get(RECONCILIATION_PR_URL_KEY)
    gate_url = metadata.get(PR_URL_KEY)
    if not isinstance(state_url, str) or not isinstance(gate_url, str):
        return False
    return bool(state_url.strip()) and state_url == gate_url


def _scalar_text(value: Any) -> str:
    """One loom-written key as stripped text; "" for anything that is not one.

    No cap: the two values that have one take it from :func:`_bounded` at the
    point they are RENDERED, so nothing here can shorten a value before it is
    interpreted. Non-strings are refused outright rather than stringified — all
    four keys are documented as flat scalars, so a dict or a list here is
    malformed, and ``str()``-ing a peer-sized container on every render is the
    allocation the Gates section refuses everywhere else.
    """
    return value.strip() if isinstance(value, str) else ""


def _bounded(text: str, cap: int) -> str:
    """``text`` capped for DISPLAY, with the ellipsis that says it was cut."""
    return text[: cap - 1] + "…" if len(text) > cap else text
