"""Relevance feedback on an Influx note: 👍 / 👎 for a profile (K4, §10).

- ``POST /note/{note_id}/feedback`` — the form posts ``profile`` (one of the
  note's ``profile:<name>`` tags), ``verdict`` (``accepted`` for 👍,
  ``rejected`` for 👎) and ``next``. One tag patch through the write funnel,
  attributed to the operator: the action reads the note
  (``lithos_read(max_length=1)``: its tags and ``version``), records the
  verdict for that profile and drops the opposite one (``note_feedback``), and
  sends the WHOLE new list with ``lithos_note_update(…, expected_version=)`` —
  the list replaces the note's, so it may only land on the note it was edited
  from. On ``version_conflict`` it reads again and retries once; a second
  conflict is the conflict page, with nothing written. A note that already
  carries exactly the verdict asked for is written nothing, and says so.

The buttons are ``writes/feedback_action.html``, one pair per profile the note
carries — a note in two profiles asks for each verdict separately, since
Influx reads them per profile — on the knowledge landing's Recent intake rows
(👍 and 👎), and 👎 alone on the note page and the knowledge graph's node panel
(§10.3). A verdict the note already carries renders pressed.

§10.3 sketches the endpoint as ``POST /api/feedback`` with a JSON body; it is
form-encoded at a note path instead because §5C.3 governs every write — no
JSON write API, a 303 to a page rendered from fresh reads, and nothing that
needs JavaScript.

Registered by the write route group, which owns the funnel, like cancel,
create and edge.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from lithos_lens.knowledge_facts import NOTE_UPDATED, NoteFactsCache
from lithos_lens.lithos_client import LithosClientProtocol, LithosToolError
from lithos_lens.note_feedback import (
    ACCEPTED,
    REJECTED,
    Verdict,
    apply_verdict,
    is_verdict,
    note_feedback,
    note_profiles,
    verdict_tag,
)
from lithos_lens.receipts import FeedbackFacts, ReceiptTask
from lithos_lens.state import AppState
from lithos_lens.tasks import NoteRecord
from lithos_lens.write_audit import WriteLedger
from lithos_lens.write_errors import (
    CONFLICT,
    CONFLICT_STATUS,
    REFUSED,
    UNKNOWN_OUTCOME,
    UNKNOWN_OUTCOME_STATUS,
    TaskRef,
    WriteProblem,
    funnel_problem,
    map_write_error,
)
from lithos_lens.write_funnel import NoteWrite, WriteDone, WriteForm, WriteFunnel

logger = logging.getLogger(__name__)

__all__ = [
    "VERSION_CONFLICT_RETRIES",
    "feedback_write",
    "note_feedback_path",
    "register_feedback_routes",
]

#: How many times a write that lost to a concurrent change is re-read and
#: retried before the operator is told (§10.2: "re-read and retry once").
VERSION_CONFLICT_RETRIES = 1

#: The verdicts each surface offers (§10.3): the feed both, the note page and
#: the graph's node panel 👎 alone.
FEED_VERDICTS: tuple[Verdict, ...] = (ACCEPTED, REJECTED)
NOTE_VERDICTS: tuple[Verdict, ...] = (REJECTED,)

BAD_FORM_COPY = (
    "The form didn't say which verdict, or for which profile — reload and try again."
)
NOTE_GONE_COPY = "This note no longer exists."
NOTE_GONE_HINT = "It may have been removed since you loaded the page."
READ_FAILED_COPY = "Lens couldn't read the note, so it didn't try the write."
NO_VERSION_COPY = "Lens couldn't read the note's version, so it didn't try the write."
KEPT_CHANGING_COPY = "The note kept changing while Lens was writing to it."
KEPT_CHANGING_HINT = (
    "Lens read it again and retried once, and it changed again. Reload to see "
    "it as it is now, then choose again."
)

_BAD_REQUEST = 400
_UNAVAILABLE = 503


def note_feedback_path(note_id: str) -> str:
    """The feedback POST target, the id as ONE encoded segment."""
    return f"/note/{quote(note_id, safe='')}/feedback"


def _note_path(note_id: str) -> str:
    return f"/note/{quote(note_id, safe='')}"


def _subject(note_id: str, note: NoteRecord | None = None) -> TaskRef:
    """The note a problem names — ``TaskRef`` is the copy's subject shape."""
    if note is None:
        return TaskRef(task_id=note_id)
    return TaskRef(task_id=note.id or note_id, title=note.title)


def _version_of(note: NoteRecord) -> int | None:
    version = note.metadata.get("version")
    return (
        version if isinstance(version, int) and not isinstance(version, bool) else None
    )


async def _read(
    client: LithosClientProtocol, note_id: str
) -> NoteRecord | WriteProblem:
    """The pre-check read: the note's tags and version, or why there are none."""
    try:
        note = await client.read_note(note_id, max_length=1)
    except Exception as exc:
        if isinstance(exc, LithosToolError) and exc.code == "doc_not_found":
            note = None
        else:
            logger.warning("feedback pre-check read failed", extra={"note_id": note_id})
            return funnel_problem(
                "feedback",
                REFUSED,
                READ_FAILED_COPY,
                code="precheck_failed",
                status_code=_UNAVAILABLE,
                subject=_subject(note_id),
                hint="Reload once Lithos is reachable.",
            )
    if note is None:
        return _note_gone(note_id, "note_not_found")
    return note


def _note_gone(note_id: str, code: str) -> WriteProblem:
    return funnel_problem(
        "feedback",
        CONFLICT,
        NOTE_GONE_COPY,
        code=code,
        status_code=CONFLICT_STATUS,
        subject=_subject(note_id),
        hint=NOTE_GONE_HINT,
    )


@dataclass(frozen=True)
class _Lost:
    """A patch that lost to a concurrent change (``version_conflict``)."""

    subject: TaskRef


async def _patch_once(
    client: LithosClientProtocol,
    operator: str,
    ledger: WriteLedger,
    note_id: str,
    profile: str,
    verdict: Verdict,
    *,
    retried: bool,
) -> WriteDone | WriteProblem | _Lost:
    """One read-then-patch (§10.2 steps 1-3): what it did, why not, or lost."""
    note = await _read(client, note_id)
    if isinstance(note, WriteProblem):
        return note
    subject = _subject(note_id, note)
    version = _version_of(note)
    ledger.observed_status = "" if version is None else f"version {version}"
    if profile not in note_profiles(note.tags):
        return funnel_problem(
            "feedback",
            CONFLICT,
            f"This note is no longer in profile {profile}.",
            code="profile_absent",
            status_code=CONFLICT_STATUS,
            subject=subject,
            hint="Reload to see the profiles it is in now.",
        )
    tags = apply_verdict(note.tags, profile, verdict)
    named = ReceiptTask(task_id=note.id or note_id, title=note.title)
    if tags == note.tags:
        ledger.attributes["unchanged"] = True
        return WriteDone(
            task=named, feedback=FeedbackFacts(profile, verdict, unchanged=True)
        )
    if version is None:
        return funnel_problem(
            "feedback",
            REFUSED,
            NO_VERSION_COPY,
            code="version_unreadable",
            status_code=_UNAVAILABLE,
            subject=subject,
            hint="Reload and try again.",
        )
    try:
        result = await client.note_update(
            note_id, agent=operator, tags=tags, expected_version=version
        )
    except Exception as exc:
        return await _failed(client, exc, ledger, subject, profile, verdict)
    ledger.envelope = asdict(result)
    if result.conflicted:
        return _Lost(subject)
    if not result.updated:
        # An answer a tag patch should never get (`slug_collision` and
        # `duplicate` need a title or source change): said verbatim, as any
        # code Lens has no copy for.
        return map_write_error(
            "feedback",
            {"code": result.status, "message": result.message},
            subject=subject,
        )
    ledger.attributes["retried"] = retried
    return WriteDone(
        task=ReceiptTask(
            task_id=result.note_id or named.task_id, title=result.title or named.title
        ),
        answer=asdict(result),
        feedback=FeedbackFacts(profile, verdict, retried=retried),
    )


def feedback_write(
    note_id: str, profile: str, verdict: str, *, note_facts: NoteFactsCache
) -> NoteWrite:
    """The feedback action, as the funnel drives it (§10.2).

    ``note_facts`` is the knowledge graph's facts cache: after a patch that
    applied — or a read that found the verdict already there — the note's
    entry is marked stale, so the graph the redirect may land on reads its
    tags again rather than waiting for a ``note.updated`` event.
    """

    async def perform(
        client: LithosClientProtocol, operator: str, ledger: WriteLedger
    ) -> WriteDone | WriteProblem:
        if not profile or not is_verdict(verdict):
            return funnel_problem(
                "feedback",
                REFUSED,
                BAD_FORM_COPY,
                code="bad_form",
                status_code=_BAD_REQUEST,
                subject=_subject(note_id),
            )
        chosen: Verdict = ACCEPTED if verdict == ACCEPTED else REJECTED
        subject = _subject(note_id)
        for attempt in range(1 + VERSION_CONFLICT_RETRIES):
            outcome = await _patch_once(
                client, operator, ledger, note_id, profile, chosen, retried=attempt > 0
            )
            if not isinstance(outcome, _Lost):
                if isinstance(outcome, WriteDone):
                    # "Already marked" too: the read just now may be the
                    # first to see an agent's verdict whose note.updated has
                    # not arrived, and the page this redirects to must not
                    # draw from facts older than that read.
                    note_facts.apply_note_event(
                        NOTE_UPDATED, {"id": outcome.task.task_id}
                    )
                return outcome
            subject = outcome.subject
            logger.info(
                "feedback write lost to a concurrent change",
                extra={"note_id": note_id, "attempt": attempt + 1},
            )
        return funnel_problem(
            "feedback",
            CONFLICT,
            KEPT_CHANGING_COPY,
            code="version_conflict",
            status_code=CONFLICT_STATUS,
            subject=subject,
            hint=KEPT_CHANGING_HINT,
        )

    return NoteWrite(action="feedback", perform=perform)


async def _failed(
    client: LithosClientProtocol,
    exc: Exception,
    ledger: WriteLedger,
    subject: TaskRef,
    profile: str,
    verdict: Verdict,
) -> WriteProblem:
    """A patch that raised: Lithos's refusal, or no answer at all.

    Only a NON-EMPTY envelope is Lithos answering (the funnel's rule): a
    deleted note is the conflict page, any other code its §5C.4 row. With no
    envelope the patch may have applied, so the page says so and states what a
    fresh read shows now — whether the note carries the verdict — and nothing
    more.
    """
    envelope = dict(exc.envelope) if isinstance(exc, LithosToolError) else {}
    ledger.envelope = envelope
    if envelope:
        if envelope.get("code") == "note_not_found":
            return _note_gone(subject.task_id, "note_not_found")
        return map_write_error("feedback", envelope, subject=subject)
    logger.warning(
        "feedback outcome unknown", extra={"note_id": subject.task_id}, exc_info=True
    )
    tag = verdict_tag(verdict, profile)
    now = await _read(client, subject.task_id)
    if isinstance(now, WriteProblem):
        headline = "Lens could not read the note's current state either."
    elif tag in now.tags:
        headline = f"The note now carries {tag}."
    else:
        headline = f"The note does not carry {tag} now."
    return WriteProblem(
        action="feedback",
        kind=UNKNOWN_OUTCOME,
        claim="unknown",
        status_code=UNKNOWN_OUTCOME_STATUS,
        headline=headline,
        hint="Reload the note to see its verdicts before choosing again.",
        subject=subject,
        log_level="warning",
    )


def register_feedback_routes(
    app: FastAPI,
    state: AppState,
    templates: Jinja2Templates,
    funnel: WriteFunnel,
) -> None:
    """Attach the feedback POST and the template globals its buttons use."""

    templates.env.globals["note_feedback"] = note_feedback
    templates.env.globals["note_feedback_path"] = note_feedback_path
    templates.env.globals["feed_verdicts"] = FEED_VERDICTS
    templates.env.globals["note_verdicts"] = NOTE_VERDICTS

    @app.post("/note/{note_id}/feedback")
    async def post_feedback(request: Request, note_id: str) -> Response:
        """Record 👍 / 👎 for one of the note's profiles (§10.2)."""
        form = await request.form()
        profile = str(form.get("profile") or "")
        verdict = str(form.get("verdict") or "")
        arguments: dict[str, Any] = {
            "note_id": note_id,
            "profile": profile,
            "verdict": verdict,
        }
        return await funnel.submit(
            request,
            WriteForm(
                task_id=note_id,
                expected_status="",
                next_url=str(form.get("next") or ""),
                arguments=arguments,
                home=_note_path(note_id),
            ),
            feedback_write(note_id, profile, verdict, note_facts=state.note_facts),
        )
