"""Relevance feedback on an Influx note: profiles, verdicts and the tag patch.

REQUIREMENTS §10. Feedback is profile-scoped and lives in the note's own tags:
a note Influx ingested for a profile carries ``profile:<name>``, and the
operator's verdict for that profile is ``influx:accepted:<name>`` (👍) or
``influx:rejected:<name>`` (👎). Influx reads the rejected tag today (negative
examples in its filter prompt, and the profile is not re-added on dedup); the
accepted one is recorded for when it does.

Pure, deliberately — no Lithos call, no state. The write path
(``feedback_routes``) reads the note, asks :func:`apply_verdict` for the tag
list to send, and sends it whole, because ``lithos_note_update``'s ``tags``
REPLACES the list. So the one rule everything here keeps is that every tag the
verdict is not about is carried through exactly as read: same string, same
order, duplicates and all.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "ACCEPTED",
    "PROFILE_TAG_PREFIX",
    "REJECTED",
    "VERDICTS",
    "ProfileFeedback",
    "Verdict",
    "apply_verdict",
    "is_verdict",
    "note_feedback",
    "note_profiles",
    "verdict_tag",
]

#: The tag Influx stamps on a note it ingested for a profile.
PROFILE_TAG_PREFIX = "profile:"

#: The two verdicts, as the form posts them and the tag spells them.
Verdict = Literal["accepted", "rejected"]
ACCEPTED: Verdict = "accepted"
REJECTED: Verdict = "rejected"
VERDICTS: tuple[Verdict, ...] = (ACCEPTED, REJECTED)

_OPPOSITE: dict[Verdict, Verdict] = {ACCEPTED: REJECTED, REJECTED: ACCEPTED}


def is_verdict(value: str) -> bool:
    """Whether a posted value is one of the two verdicts, exactly."""
    return value in VERDICTS


def verdict_tag(verdict: Verdict, profile: str) -> str:
    """The tag that records ``verdict`` for ``profile``."""
    return f"influx:{verdict}:{profile}"


def note_profiles(tags: Iterable[str]) -> tuple[str, ...]:
    """The profiles a note was ingested for, in tag order, each once.

    A bare ``profile:`` names no profile and is skipped: a verdict for it
    would be a tag no Influx profile reads.
    """
    profiles: dict[str, None] = {}
    for tag in tags:
        if tag.startswith(PROFILE_TAG_PREFIX):
            name = tag[len(PROFILE_TAG_PREFIX) :]
            if name:
                profiles.setdefault(name, None)
    return tuple(profiles)


@dataclass(frozen=True)
class ProfileFeedback:
    """One profile's verdict buttons on a note: which of the two is pressed.

    Both can be pressed at once — a note some other writer tagged both ways —
    and then both render pressed, so the operator sees it and clicking either
    leaves exactly one.
    """

    profile: str
    accepted: bool = False
    rejected: bool = False

    def pressed(self, verdict: str) -> bool:
        return self.accepted if verdict == ACCEPTED else self.rejected


def note_feedback(tags: Iterable[str]) -> tuple[ProfileFeedback, ...]:
    """One :class:`ProfileFeedback` per profile the note carries; none without.

    A note with no ``profile:*`` tag is not Influx's to judge, so it offers
    no buttons at all.
    """
    present = set(tags := tuple(tags))
    return tuple(
        ProfileFeedback(
            profile=profile,
            accepted=verdict_tag(ACCEPTED, profile) in present,
            rejected=verdict_tag(REJECTED, profile) in present,
        )
        for profile in note_profiles(tags)
    )


def apply_verdict(
    tags: Iterable[str], profile: str, verdict: Verdict
) -> tuple[str, ...]:
    """The note's full tag list with ``verdict`` recorded for ``profile``.

    Drops every copy of the opposite verdict for that profile and appends the
    verdict's tag unless the note already carries it. Every other tag is
    returned as read, in its place. Equal to the input when there is nothing
    to change — which is how the caller knows not to write.
    """
    opposite = verdict_tag(_OPPOSITE[verdict], profile)
    kept = tuple(tag for tag in tags if tag != opposite)
    wanted = verdict_tag(verdict, profile)
    return kept if wanted in kept else (*kept, wanted)
