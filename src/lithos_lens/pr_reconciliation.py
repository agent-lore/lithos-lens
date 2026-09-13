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
  only when ``reconciliation_pr_url`` matches the gate's own ``pr_url``;
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

# Bounds. ``metadata`` is peer-written whatever loom's own contract says, and
# every value below reaches the markup: the state as a badge label, the detail
# as its tooltip, the stamp as a ``datetime`` attribute. The detail cap is
# loom's own stated limit; the other two are "longer than this is not one of
# these" rather than a policy of ours.
_STATE_CAP = 40
_DETAIL_CAP = 200
_SINCE_CAP = 40


@dataclass(frozen=True)
class ReconciliationStyle:
    """How ONE reconciliation state renders: its text and its colour tone.

    ``tone`` is a markup-safe token, never a colour: the stylesheet owns what
    "danger" looks like, and a template that string-matched the state would be
    a second copy of this vocabulary waiting to drift from it.
    """

    state: str
    label: str
    tone: str


# THE mapping: loom's closed vocabulary, ordered MOST SEVERE FIRST. The order
# is load-bearing twice over — it is the badge colour and it is the ordering of
# PR gates inside the Gates section (§5.2.3) — so the list is the single place
# either can be changed.
_STYLES: tuple[ReconciliationStyle, ...] = (
    ReconciliationStyle(NEEDS_HUMAN_STATE, "needs human", "danger"),
    ReconciliationStyle(GATE_FAILED_STATE, "gate failed", "warn"),
    ReconciliationStyle("behind", "behind", "warn"),
    ReconciliationStyle("reconciling", "reconciling", "info"),
    ReconciliationStyle("resolving_conflict", "resolving conflict", "info"),
    ReconciliationStyle("awaiting_review", "awaiting review", "neutral"),
    ReconciliationStyle("ready_to_merge", "ready to merge", "ok"),
)

_BY_STATE: dict[str, tuple[int, ReconciliationStyle]] = {
    style.state: (severity, style) for severity, style in enumerate(_STYLES)
}

# The markup token and the ordering rank for a state loom has that Lens does
# not. It sorts after every state Lens knows (Lens cannot rank a word it has
# never seen, and guessing would put an unknown word above a real escalation);
# a PR gate with NO state at all sorts after that again.
UNKNOWN_STATE_TONE = "unknown"
UNKNOWN_STATE_SEVERITY = len(_STYLES)
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

    None — no badge at all — in four cases, which is the whole judgement this
    module makes:

    - the gate is not a ``pr`` gate. Nothing else carries these keys, and a
      badge built from stray metadata on a timer gate would be a fiction;
    - ``reconciliation_state`` is absent, empty, or not a string. loom writes
      flat scalars, so a container here is malformed rather than meaningful —
      and it stays visible either way, as the generic advisory chips that render
      whenever this returns None;
    - ``reconciliation_pr_url`` does not match the gate's ``pr_url``. The state
      then describes a PR this gate no longer points at (a replacement PR on the
      same gate starts fresh, and loom rewrites the keys on its next sweep only)
      — showing it would state the old PR's condition under the new one's row.
      The raw keys stay on the row as advisory chips, so the stale state is
      inspectable without being asserted;
    - the two url values are compared RAW rather than as bounded text, because
      bounding first would let two long urls sharing a prefix compare equal —
      i.e. would make a stale state pass the one check that exists to catch it.
      Both absent compares equal, which is the honest reading: there is no
      second PR for the state to be about.

    ``now`` supplies the badge's age and is optional: without it the badge
    renders without one.
    """
    if gate_type != PR_GATE_TYPE:
        return None
    metadata = task.metadata
    state = _scalar_text(metadata.get(RECONCILIATION_STATE_KEY), cap=_STATE_CAP)
    if not state:
        return None
    if metadata.get(RECONCILIATION_PR_URL_KEY) != metadata.get(PR_URL_KEY):
        return None
    since = _scalar_text(metadata.get(RECONCILIATION_SINCE_KEY), cap=_SINCE_CAP)
    parsed_since = parse_timestamp(since)
    entry = _BY_STATE.get(state)
    severity, style = entry if entry is not None else (UNKNOWN_STATE_SEVERITY, None)
    return Reconciliation(
        state=state,
        # THE mapping, applied — the only place a state becomes a wording, a
        # colour or a rank. An unknown state keeps its own text and takes the
        # unknown token for every hook built from it.
        label=style.label if style is not None else state,
        slug=style.state if style is not None else UNKNOWN_STATE_TONE,
        tone=style.tone if style is not None else UNKNOWN_STATE_TONE,
        severity=severity,
        detail=_scalar_text(metadata.get(RECONCILIATION_DETAIL_KEY), cap=_DETAIL_CAP),
        # Normalized when it parses, bounded raw text when it does not: an
        # unreadable stamp stays visible rather than vanishing, and drives no
        # age rather than a guessed one.
        since=parsed_since.isoformat() if parsed_since is not None else since,
        age=(
            humanize_age(now - parsed_since)
            if now is not None and parsed_since is not None
            else ""
        ),
    )


def _scalar_text(value: Any, *, cap: int) -> str:
    """One loom-written key as bounded, stripped text; "" for anything else.

    Non-strings are refused outright rather than stringified. All four keys are
    documented as flat scalars, so a dict or a list here is malformed — and
    ``str()``-ing a peer-sized container on every render to keep 40 bytes of it
    is the allocation the Gates section refuses everywhere else.
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text[: cap - 1] + "…" if len(text) > cap else text
