"""Every Lithos write refusal, turned into operator copy and an HTTP answer.

The one place §5C.4's table lives. A write to Lithos has exactly three endings
— it applied, Lithos refused it with a coded error envelope, or Lens never got
an answer — and this module owns the second and third: given the action, the
WHOLE envelope, and what a re-read of the task showed, it returns the copy the
operator reads and the status code the funnel answers with.

Pure, deliberately. It makes no Lithos call and holds no state, so every row of
the table is a value in, a value out — which is what lets the funnel (T3-W4)
have one classification path instead of per-route error handling, and lets the
rows be tested without a browser or a server.

Three rules hold for every row, and none of them is a per-route decision:

- **No write error is a 500.** A refusal is an answer, not a Lens failure. Each
  kind carries its own status (:data:`CONFLICT_STATUS`,
  :data:`REFUSED_STATUS`, :data:`UNKNOWN_OUTCOME_STATUS`) and an unmapped code
  is answered like any other refusal rather than raised.
- **A refused write says that nothing was changed** — first, before the reason,
  because that is the fact the operator needs before they decide what to do
  next. :attr:`WriteProblem.change_statement` is that sentence and every page
  renders it, so no template can quietly omit it.
- **The unknown-outcome case never claims the write did or did not apply.** Its
  claim is :data:`OUTCOME_UNKNOWN` and its copy states what a re-read shows
  NOW, which is a different claim from "the action failed". Lithos writes are
  not idempotent (§14), so a page that guessed either way would be telling the
  operator to retry something that may already have happened.

**The whole envelope, not a code and a message.** The mapper takes the error
envelope as a mapping because fields beyond those two carry what the copy
needs: ``ambiguous_id_prefix`` is useless without its ``candidates``, and a
field upstream adds later reaches the copy without a signature change. The
client's coded tool error carries the envelope whole (T3-W3); an error with an
EMPTY one is no answer at all, and the funnel passes ``None`` for it.

**One code, two facts.** ``task_not_found`` is what complete and cancel answer
for a task that is missing AND for one that is no longer open — Lithos spends
one code on both (Further Notes; a distinct code is a candidate upstream ask).
Lens cannot tell them apart from the envelope, so it does not try: the funnel
re-reads the task and passes the result in, and the row splits on that. A task
that exists gets the conflict page naming the status it has now; one that is
gone is said to be gone. A re-read that itself failed claims neither.

That split is the lifecycle actions' alone. Create and the edge actions spend
the same code on a task the request REFERS to — a parent, a predecessor, an
edge endpoint, or a prefix that matches nothing — so for them it is a mistyped
or stale reference, refused on the form with Lithos's message (which names the
parameter and the id) kept whole. Only a re-read showing the edge's own subject
gone turns it back into the conflict page.

**What is NOT decided here.** Which surface renders the copy. The two pages
this module feeds are standalone answers to a POST (``writes/conflict.html``,
``writes/unknown_outcome.html``); a :data:`REFUSED` problem has no page of its
own because its copy belongs on the form the operator just submitted, with
their input kept — so it renders through ``writes/notice.html``, the partial
both pages are built from, inside that form (T3-W7, T3-W8). :attr:`page` says
which of the three cases a problem is, and nothing here renders anything.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

#: The curated writes, as the span and audit-line suffix spell them (§15).
#: Carried on every problem so the funnel's one log line names the action
#: without the route restating it.
WriteAction = Literal[
    "complete", "reopen", "cancel", "create", "edge_upsert", "edge_remove"
]

#: Which of the three endings a problem is, which is also which surface answers
#: it: ``conflict`` and ``unknown_outcome`` are pages, ``refused`` is a notice
#: on the form that was submitted. See :attr:`WriteProblem.page`.
ProblemKind = Literal["conflict", "unknown_outcome", "refused"]

#: What the copy may claim about the store. Two values, not three: Lens never
#: says a write APPLIED from this module — that answer is a receipt (D5), not a
#: problem.
ChangeClaim = Literal["nothing", "unknown"]

#: How loudly the funnel logs this problem. ``info`` for a refusal Lens has
#: copy for (an ordinary race, or an operator asking for something Lithos
#: forbids), ``warning`` when Lens could not complete the exchange, ``error``
#: for a refusal that can only be a Lens defect — see ``invalid_edge_type``.
RefusalLogLevel = Literal["info", "warning", "error"]

#: The state the operator acted on is not the state that exists. 409 and not
#: 404 even for a task that is GONE: the URL was right when the page was
#: rendered, and a 404 reads — to the browser, the history entry and the
#: operator — as "no such page", which is the one wrong diagnosis here.
CONFLICT_STATUS = 409

#: Lithos refused the content of the request. 422 rather than 400: the form was
#: well-formed and parsed; what it asked for is what Lithos would not do.
REFUSED_STATUS = 422

#: The unknown-outcome page. 200, after the alternatives were weighed: a 4xx
#: blames a request that was fine, and a 5xx claims the action failed — which
#: is the one thing this page exists NOT to claim. The page itself is the
#: answer, HTTP has no code for "indeterminate", and §5C.4's "never a 500"
#: rules out saying it in the status line.
UNKNOWN_OUTCOME_STATUS = 200

#: What a refused write always says, before it says why.
NOTHING_CHANGED = "Nothing was changed."

#: What the unknown-outcome case says instead — and all it says about the
#: store.
OUTCOME_UNKNOWN = "The action may or may not have applied."

#: Appended to the unknown-code path. The code and message are rendered
#: verbatim beside it, so the report names the thing Lens could not map.
REPORT_THIS_HINT = (
    "Report this with the code and message above — Lens has no copy for it."
)

#: How a task gets a different parent. Lithos keeps one parent per task and
#: refuses a second, so the current parent relation has to go first (§5C.2).
PARENT_EXISTS_HINT = (
    "To give it a different parent, remove its current parent relation first, "
    "then add the new one."
)

#: The standalone page for a write refused because the world moved: the task
#: is not in the state the operator acted on, or is not there at all.
CONFLICT_PAGE = "writes/conflict.html"

#: The standalone page for a write Lens got no answer to.
UNKNOWN_OUTCOME_PAGE = "writes/unknown_outcome.html"

#: The partial that renders one problem's copy, whatever surface it lands on:
#: both pages above include it, and a :data:`REFUSED` problem — which has no
#: page of its own — is rendered through it inside the form that was submitted.
NOTICE_PARTIAL = "writes/notice.html"

#: The three endings, as the kinds are spelled above.
CONFLICT: ProblemKind = "conflict"
UNKNOWN_OUTCOME: ProblemKind = "unknown_outcome"
REFUSED: ProblemKind = "refused"


@dataclass(frozen=True)
class TaskRef:
    """A task named in copy: the id, and the title if one was read.

    Carried rather than formatted into the headline so the page names a task
    the way every other surface does — title, then the short-id element
    (§5.3) — instead of splicing an id into prose that would then have to be
    marked safe to link it.
    """

    task_id: str = ""
    title: str = ""

    @property
    def label(self) -> str:
        """Display text: the title, falling back to the bare id."""
        return self.title or self.task_id


#: The subject of an action that named no task, or named one the funnel had no
#: read for. The copy says "that task" rather than inventing a name for it.
NO_SUBJECT = TaskRef()

#: Where the re-read left things: the task exists (with ``status``), there is
#: no such task, the read failed, or no read was made.
RereadOutcome = Literal["exists", "absent", "unreadable", "not_read"]


@dataclass(frozen=True)
class TaskReread:
    """What a fresh ``lithos_task_get`` showed after the write was refused.

    The third input to the table, and the only one that is not a value the
    funnel already had: ``task_not_found`` and the no-envelope case are both
    answerable only by looking again. ``status`` is Lithos's, read after the
    refusal, so the copy states the status the task HAS rather than the one the
    form carried.
    """

    outcome: RereadOutcome = "not_read"
    status: str = ""

    @property
    def found(self) -> bool:
        return self.outcome == "exists" and bool(self.status)


#: What the funnel passes when the task was not re-read at all. ``create`` has
#: no task to re-read by id (D10), and a refusal the envelope answers on its
#: own needs no read.
NOT_READ = TaskReread()

#: The re-read found no such task.
TASK_ABSENT = TaskReread(outcome="absent")

#: The re-read itself failed. Distinct from "absent" and never collapsed into
#: it: a read that did not answer is not evidence of a deleted task, and
#: saying the task is gone on the strength of one would be the worst answer
#: this page can give.
REREAD_FAILED = TaskReread(outcome="unreadable")


@dataclass(frozen=True)
class MessageSegment:
    """One run of an upstream message: plain text, or a task id to link.

    The ``cycle`` row renders Lithos's message VERBATIM and gives the ids in it
    the short-id link treatment, which is presentation and nothing else. Making
    that a sequence of segments is what keeps the two apart: the template links
    what is marked, no sentence is marked safe HTML to get an anchor into it,
    and the message's text survives character for character —
    :attr:`WriteProblem.detail_text` reassembles it.
    """

    text: str
    #: Non-empty when this segment IS a task id, and the template should render
    #: it as the short-id element linked to that task.
    task_id: str = ""


@dataclass(frozen=True)
class WriteProblem:
    """One row of §5C.4's table, resolved against one attempt.

    Everything a page or a form needs to answer the operator, and nothing it
    has to decide for itself: the claim about the store, the fact, the next
    step, and the status code.
    """

    action: WriteAction
    kind: ProblemKind
    claim: ChangeClaim
    status_code: int
    #: The fact, as one sentence: what the task is now, what Lithos would not
    #: do. Empty only on the unknown-outcome case with nothing re-read.
    headline: str = ""
    #: Lens's own follow-on sentence — what to do, or what the copy does not
    #: know. Never upstream text.
    hint: str = ""
    #: The upstream message, VERBATIM, segmented for id links. Empty on rows
    #: whose copy replaces the message rather than quoting it.
    detail: tuple[MessageSegment, ...] = ()
    #: Lithos's error code. Present on every coded refusal, but rendered to the
    #: operator only when :attr:`show_code` says so.
    code: str = ""
    #: True on the unknown-code path alone. "Never surface a bare code" is a
    #: rule about codes Lens HAS copy for; where it has none, the code is the
    #: only handle the operator and the bug report have.
    show_code: bool = False
    #: The write parameter an ``invalid_input`` or missing-reference message
    #: names (:data:`INVALID_INPUT_FIELDS`), for a field-level re-render with
    #: the operator's input kept. The create and relation forms map it to their
    #: own inputs. Empty when the message names none of them — it then renders
    #: at form level.
    field: str = ""
    #: The id prefix that matched more than one task.
    prefix: str = ""
    #: The choices an ambiguous prefix leaves, from the envelope.
    candidates: tuple[TaskRef, ...] = ()
    #: The task the copy is about, when the action named one.
    subject: TaskRef = NO_SUBJECT
    log_level: RefusalLogLevel = "info"

    @property
    def change_statement(self) -> str:
        """What this problem claims about the store, as the page's first line."""
        return NOTHING_CHANGED if self.claim == "nothing" else OUTCOME_UNKNOWN

    @property
    def nothing_changed(self) -> bool:
        return self.claim == "nothing"

    @property
    def page(self) -> str:
        """The template that answers this problem on its own, or ``""``.

        Empty for a :data:`REFUSED` problem: its copy belongs inside the form
        the operator submitted, so the surface that owns the form renders
        ``writes/notice.html`` rather than redirecting away from the input it
        is keeping.
        """
        if self.kind == "conflict":
            return CONFLICT_PAGE
        if self.kind == "unknown_outcome":
            return UNKNOWN_OUTCOME_PAGE
        return ""

    @property
    def detail_text(self) -> str:
        """The upstream message as Lithos sent it, segmentation undone."""
        return "".join(segment.text for segment in self.detail)


@dataclass(frozen=True)
class _Attempt:
    """One refused attempt, as the row builders read it.

    Bundled so a row takes one argument and the table stays a mapping of code
    to builder — the alternative was five positional parameters repeated on
    nine functions.
    """

    action: WriteAction
    envelope: Mapping[str, Any]
    reread: TaskReread
    subject: TaskRef
    typed_id: str

    @property
    def code(self) -> str:
        return _text(self.envelope.get("code"))

    @property
    def message(self) -> str:
        return _text(self.envelope.get("message"))


def map_write_error(
    action: WriteAction,
    envelope: Mapping[str, Any] | None,
    *,
    reread: TaskReread = NOT_READ,
    subject: TaskRef = NO_SUBJECT,
    typed_id: str = "",
) -> WriteProblem:
    """Turn one refused or unanswered write into copy and an HTTP answer.

    ``envelope`` is what LITHOS ANSWERED, whole. ``None`` means it answered
    nothing — a transport failure or a timeout — which is the one case where
    the write may still have applied, and the only one that reaches the
    unknown-outcome page. An envelope with no readable ``code`` is still an
    answer, and an error answer from Lithos is a refusal, so it takes the
    unknown-code path rather than claiming the outcome is unknown.

    ``reread`` is what a fresh read of the task showed, for the two rows that
    cannot be answered from the envelope alone. ``subject`` is the task the
    copy names; ``typed_id`` is the id or prefix the operator's form carried,
    used for the ambiguous-prefix copy when the envelope does not spell the
    prefix itself.
    """
    if envelope is None:
        return _unknown_outcome(action, reread, subject)
    attempt = _Attempt(
        action=action,
        envelope=envelope,
        reread=reread,
        subject=subject,
        typed_id=typed_id,
    )
    row = _ROWS.get(attempt.code, _unmapped_code)
    return row(attempt)


def funnel_problem(
    action: WriteAction,
    kind: ProblemKind,
    headline: str,
    *,
    code: str,
    status_code: int,
    subject: TaskRef = NO_SUBJECT,
    hint: str = "",
) -> WriteProblem:
    """A refusal the write funnel decides itself, before Lithos sees a write.

    The funnel's own checks — a stale ``expected_status``, a form that says
    nothing usable, a task the action does not apply to, an identity that
    cannot write — refuse a write that was never sent, so nothing was changed
    by construction and the claim is fixed. They answer through the same pages
    and the same notice as an upstream refusal, so the operator reads one kind
    of copy whichever side caught it; ``code`` is the funnel's own name for
    the case (``not_a_gate``, ``bad_form`` …), carried for the audit line.
    """
    return WriteProblem(
        action=action,
        kind=kind,
        claim="nothing",
        status_code=status_code,
        headline=headline,
        hint=hint,
        code=code,
        subject=subject,
    )


def message_segments(message: str) -> tuple[MessageSegment, ...]:
    """Split an upstream message into text and the task ids inside it.

    Presentation only, and conservative on purpose. The ONE id form that can be
    recognised inside prose without risk is the UUID Lithos mints — in either
    spelling, since the corpus carries both the hyphenated form and the bare
    32 hex characters — so that is all this looks for, and ``uuid.UUID`` is
    what decides, not the shape of the token. An id in any other shape (the
    readable slugs the contracts use for fixtures) stays plain text:
    under-linking costs the operator a click, while a pattern loose enough to
    catch a slug would link words out of the sentence.

    Nothing downstream depends on what this finds. The segments reassemble into
    the message character for character either way, which is what "verbatim"
    means here, and no Lens decision reads them — the rule §5C.4 states as
    "Lens does not parse the message".
    """
    if not message:
        return ()
    segments: list[MessageSegment] = []
    cursor = 0
    for match in _ID_SHAPED.finditer(message):
        token = match.group(0)
        if not _is_task_id(token):
            continue
        if match.start() > cursor:
            segments.append(MessageSegment(text=message[cursor : match.start()]))
        segments.append(MessageSegment(text=token, task_id=token))
        cursor = match.end()
    if cursor < len(message):
        segments.append(MessageSegment(text=message[cursor:]))
    return tuple(segments)


# A run long enough to BE a uuid in either spelling (32 hex characters, or 36
# with hyphens), delimited so a longer token that merely contains one is left
# whole. Only a sieve: `_is_task_id` decides, so the hyphen placement is not
# something this pattern has to get right.
_ID_SHAPED = re.compile(
    r"(?<![0-9A-Za-z-])[0-9a-fA-F][0-9a-fA-F-]{30,34}[0-9a-fA-F](?![0-9A-Za-z-])"
)


def _verbatim(message: str) -> tuple[MessageSegment, ...]:
    """An upstream message as one plain segment: shown exactly as sent."""
    return (MessageSegment(text=message),) if message else ()


#: The write parameters Lithos 0.5.0 names in a refusal's message:
#: ``resolve_task_id(…, field=…)`` for the id references — create's two and the
#: edge's two endpoints — in both its ``invalid_input`` (too short) and its
#: ``task_not_found`` (no match), ``create_task``'s ``task_not_found`` for a
#: reference that does not exist, and ``_validate_gate_metadata`` for a gate's
#: metadata (lithos a4d2d62 ``tools/tasks.py`` ``lithos_task_create`` and
#: ``lithos_task_edge_upsert``, ``coordination.py`` ``resolve_task_id``,
#: ``create_task`` and ``_validate_gate_metadata``).
INVALID_INPUT_FIELDS: tuple[str, ...] = (
    "parent_task_id",
    "depends_on",
    "from_task_id",
    "to_task_id",
    "metadata.gate_type",
    "metadata.ready_at",
)

_FIELD = r"(" + "|".join(re.escape(name) for name in INVALID_INPUT_FIELDS) + r")"

# The only places Lithos 0.5.0 names a parameter, each anchored to Lithos's
# own text at an end of the message so the operator's input (which can be any
# string) cannot stand in for it: at the START, ``resolve_task_id``'s "<field>
# '<raw>' is too short", ``create_task``'s "<field> references nonexistent …"
# and ``_validate_gate_metadata``'s "a gate task requires <field> in …" / "a
# 'timer' gate requires a parseable <field> (…"; at the END,
# ``resolve_task_id``'s "No task matches id prefix '<raw>' (<field>)."
_FIELD_POSITIONS = (
    re.compile(
        r"^(?:a gate task requires |a 'timer' gate requires a parseable )?"
        + _FIELD
        + r" "
    ),
    re.compile(r"^No task matches id prefix .*\(" + _FIELD + r"\)\.?\s*$", re.S),
)


def _field_named_in(message: str) -> str:
    """The write parameter a refusal's message names, or ``""``.

    Read from the positions above alone, so a parameter's name inside the
    quoted input — another field typed into this one, or a repr with escaped
    quotes — is never taken for the field. A message with none is form level.
    """
    for position in _FIELD_POSITIONS:
        match = position.search(message)
        if match:
            return match.group(1)
    return ""


def _is_task_id(token: str) -> bool:
    """Whether a token is a Lithos id, by the one test that can say so.

    ``uuid.UUID`` rather than a pattern of this module's own, for the reason
    the wiki-link resolver uses it: it accepts every spelling of the same id
    and invents no second definition of what one looks like.
    """
    try:
        uuid.UUID(token)
    except ValueError:
        return False
    return True


def _unknown_outcome(
    action: WriteAction, reread: TaskReread, subject: TaskRef
) -> WriteProblem:
    """No answer from Lithos: say so, then say what a re-read shows now.

    The headline is the re-read and nothing more. It is deliberately NOT framed
    as evidence either way — a task that is now ``completed`` may have been
    completed by this write or by an agent a second earlier, and the page's
    whole job is to not pick one.
    """
    return WriteProblem(
        action=action,
        kind=UNKNOWN_OUTCOME,
        claim="unknown",
        status_code=UNKNOWN_OUTCOME_STATUS,
        headline=_reread_sentence(reread),
        hint=_unknown_outcome_hint(reread),
        subject=subject,
        log_level="warning",
    )


def _reread_sentence(reread: TaskReread) -> str:
    """What the re-read says about the task, as one sentence."""
    if reread.found:
        return f"This task is now {reread.status}."
    if reread.outcome == "absent":
        return "This task no longer exists."
    if reread.outcome == "unreadable":
        return "Lens could not read the task's current state either."
    return ""


def _unknown_outcome_hint(reread: TaskReread) -> str:
    if reread.outcome == "unreadable":
        return "Reload once Lithos is reachable to see what the task is now."
    return "Reload to see the task's current state before trying again."


#: The actions whose ``task_not_found`` is about the task they act on: complete
#: and cancel spend it on "gone or no longer open", reopen on "gone". Every
#: other action spends it on a task the request refers to.
_LIFECYCLE_ACTIONS: frozenset[WriteAction] = frozenset({"complete", "reopen", "cancel"})


def _task_not_found(attempt: _Attempt) -> WriteProblem:
    """One code, two facts — split by the re-read, never by the message.

    Complete and cancel answer ``task_not_found`` both for a task that is gone
    and for one that is no longer open. The re-read is the only thing that can
    tell those apart, and when it could not be made or failed, neither is
    claimed: the copy says what Lithos refused and that Lens cannot say which
    of the two it was. Create and edge refusals are a different fact, answered
    by :func:`_missing_reference`.
    """
    if attempt.action not in _LIFECYCLE_ACTIONS:
        return _missing_reference(attempt)
    if attempt.reread.found:
        return _conflict(attempt, f"This task is now {attempt.reread.status}.")
    if attempt.reread.outcome == "absent":
        return _conflict(
            attempt,
            "This task no longer exists.",
            hint="It may have been removed since you loaded the page.",
        )
    return _conflict(
        attempt,
        "Lithos refused this: the task is either gone or no longer open.",
        hint="Lens could not read the task to say which. Reload to see.",
    )


def _missing_reference(attempt: _Attempt) -> WriteProblem:
    """Create or an edge named a task Lithos has no match for.

    Lithos 0.5.0 raises ``task_not_found`` for a prefix that matches nothing,
    a parent or predecessor that does not exist, and an edge endpoint that does
    not (``resolve_task_id``, ``create_task``, ``upsert_task_edge``): a mistyped
    or stale reference, not a task that changed. So it is refused on the form,
    input kept, with the message whole — the only thing that says WHICH
    reference — and plain, since an id in it links nowhere. The exception is a
    re-read showing the edge's own subject gone: that is the conflict page's.
    """
    if attempt.reread.outcome == "absent":
        return _conflict(
            attempt,
            "This task no longer exists.",
            hint="It may have been removed since you loaded the page.",
        )
    return _refused(
        attempt,
        "Lithos couldn't find a task this refers to.",
        hint="Check the id and try again.",
        detail=_verbatim(attempt.message),
        field=_field_named_in(attempt.message),
    )


def _task_not_resolved(attempt: _Attempt) -> WriteProblem:
    """Reopen's refusal for a task that is already open."""
    return _conflict(attempt, "This task is already open.")


def _invalid_input(attempt: _Attempt) -> WriteProblem:
    """Lithos rejected a value: the upstream message, on the field it names.

    Lithos 0.5.0 sends no field key with ``invalid_input`` — its envelope is
    ``{status, code, message}`` and the parameter is named in the MESSAGE
    (``resolve_task_id``'s "parent_task_id 'abc' is too short…",
    ``_validate_gate_metadata``'s "…requires metadata.gate_type in…"). So the
    field is found there, by :func:`_field_named_in`. That is placement, which
    is presentation: the message is still shown whole, and a message naming no
    known field renders at form level — a worse-placed answer, not a wrong one.
    """
    return _refused(
        attempt,
        "Lithos would not accept this.",
        detail=_verbatim(attempt.message),
        field=_field_named_in(attempt.message),
    )


def _ambiguous_id_prefix(attempt: _Attempt) -> WriteProblem:
    """A prefix that matches more than one task, with the choices it leaves.

    The prefix is the envelope's when it carries one and the value the form
    sent otherwise — the one thing Lens knows for certain it asked about. The
    candidates are the reason this row reads the whole envelope: with only a
    code and a message the operator is told their prefix is ambiguous and left
    to go and find the tasks themselves.
    """
    prefix = _text(attempt.envelope.get("prefix")) or attempt.typed_id
    quoted = f"'{prefix}'" if prefix else "That id"
    return _refused(
        attempt,
        f"{quoted} matches more than one task.",
        hint="Pick the one you meant.",
        prefix=prefix,
        candidates=_candidates(attempt.envelope),
    )


def _cycle(attempt: _Attempt) -> WriteProblem:
    """The dependency Lithos refused because it would close a loop.

    Lens's own sentence first, then Lithos's message verbatim: the message
    names the members by full id, which is the only account of WHICH loop, and
    nothing Lens could reword without losing it.
    """
    return _refused(
        attempt,
        "This dependency would create a cycle.",
        detail=message_segments(attempt.message),
    )


def _parent_exists(attempt: _Attempt) -> WriteProblem:
    """A second parent for a task that has one. Lithos keeps one parent.

    §5C.4 has the copy name the existing parent and the way to replace it. The
    mapper makes no Lithos call to find the parent, and the envelope carries
    no field for it — but Lithos's message names it by full id ("task <child>
    already has a parent (<parent>); … Remove the existing parent_child edge
    before re-parenting."), so the message is quoted with the ``cycle`` row's
    short-id link treatment, which links the parent; the hint says how to
    replace it in the operator's terms.
    """
    return _refused(
        attempt,
        f"{_subject_label(attempt)} already has a parent.",
        hint=PARENT_EXISTS_HINT,
        detail=message_segments(attempt.message),
    )


def _self_edge(attempt: _Attempt) -> WriteProblem:
    return _refused(attempt, "A task can't depend on itself.")


def _not_a_gate(attempt: _Attempt) -> WriteProblem:
    return _refused(
        attempt,
        f"{_subject_label(attempt)} isn't a gate — only a gate can be waited on.",
    )


def _lens_defect(attempt: _Attempt) -> WriteProblem:
    """A refusal that can only be a Lens bug, answered as an unknown code.

    ``invalid_edge_type`` is the case: the relation form offers the valid types
    and nothing else, so Lithos rejecting the type means Lens sent one it
    should never have built. The operator gets the unknown-code copy, which is
    the honest answer to "something is wrong and Lens does not know what"; the
    ``error`` level is what makes it findable afterwards.
    """
    return _unmapped_code(attempt, log_level="error")


def _unmapped_code(
    attempt: _Attempt, *, log_level: RefusalLogLevel = "warning"
) -> WriteProblem:
    """A code §5C.4 has no row for: the code and message, verbatim, plus a hint.

    Forward compatibility, not a fallback to hide behind. A code Lens has never
    seen is still a refusal — Lithos answered — so the copy keeps the two rules
    every other row keeps (nothing was changed; no 500) and adds the only thing
    it can: exactly what Lithos said, and a request to report it.
    """
    return _refused(
        attempt,
        "Lithos refused this with a code Lens does not recognise.",
        hint=REPORT_THIS_HINT,
        # Plain, not segmented: the message is a diagnostic to be reported
        # character for character, and the short-id treatment would show a
        # shortened id where Lithos sent a full one.
        detail=_verbatim(attempt.message),
        show_code=True,
        log_level=log_level,
    )


#: §5C.4's table: the codes Lithos 0.5.0 raises on a write, each mapped to the
#: builder that turns one attempt into its copy. A code absent from here takes
#: :func:`_unmapped_code`, which is also where ``invalid_edge_type`` is sent
#: deliberately.
_ROWS: Mapping[str, Callable[[_Attempt], WriteProblem]] = {
    "task_not_found": _task_not_found,
    "task_not_resolved": _task_not_resolved,
    "invalid_input": _invalid_input,
    "ambiguous_id_prefix": _ambiguous_id_prefix,
    "cycle": _cycle,
    "parent_exists": _parent_exists,
    "self_edge": _self_edge,
    "not_a_gate": _not_a_gate,
    "invalid_edge_type": _lens_defect,
}

#: Every code §5C.4 spells a row for. Exposed so the contracts added in T3-W3
#: can be held to the table rather than to a copy of it.
MAPPED_CODES: frozenset[str] = frozenset(_ROWS)


def _conflict(attempt: _Attempt, headline: str, *, hint: str = "") -> WriteProblem:
    return WriteProblem(
        action=attempt.action,
        kind=CONFLICT,
        claim="nothing",
        status_code=CONFLICT_STATUS,
        headline=headline,
        hint=hint or "Reload to see the task as it is now.",
        code=attempt.code,
        subject=attempt.subject,
    )


def _refused(
    attempt: _Attempt,
    headline: str,
    *,
    hint: str = "",
    detail: tuple[MessageSegment, ...] = (),
    field: str = "",
    prefix: str = "",
    candidates: tuple[TaskRef, ...] = (),
    show_code: bool = False,
    log_level: RefusalLogLevel = "info",
) -> WriteProblem:
    return WriteProblem(
        action=attempt.action,
        kind=REFUSED,
        claim="nothing",
        status_code=REFUSED_STATUS,
        headline=headline,
        hint=hint,
        detail=detail,
        code=attempt.code,
        show_code=show_code,
        field=field,
        prefix=prefix,
        candidates=candidates,
        subject=attempt.subject,
        log_level=log_level,
    )


def _subject_label(attempt: _Attempt) -> str:
    """How copy names the task it is about when it has to name it in prose.

    Falls back to "That task" rather than to a bare id: the sentence has to
    read, and an unlabelled subject means the funnel had no read to name it
    with — which is a thing the copy should not pretend to know.
    """
    return attempt.subject.label or "That task"


def _candidates(envelope: Mapping[str, Any]) -> tuple[TaskRef, ...]:
    """The ``candidates`` an ambiguous-prefix envelope offers, as task refs.

    Reads ``id`` (with ``task_id`` as the alternative spelling) and ``title``,
    which is what a serialised Lithos task record carries, and tolerates a bare
    id string. Deliberately tolerant and deliberately NOT authoritative: the
    canonical shape is vendored by the contract T3-W3 adds, and this module must
    not become a second declaration of it. An entry that yields no id is
    dropped — the prefix copy stands on its own, and a choice with nothing to
    choose is worse than no list.
    """
    raw = envelope.get("candidates")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return ()
    refs = []
    for entry in raw:
        if isinstance(entry, Mapping):
            task_id = _text(entry.get("id")) or _text(entry.get("task_id"))
            refs.append(TaskRef(task_id=task_id, title=_text(entry.get("title"))))
        else:
            refs.append(TaskRef(task_id=_text(entry)))
    return tuple(ref for ref in refs if ref.task_id)


def _text(value: Any) -> str:
    """A string from an envelope field, with ``None`` read as absent."""
    return "" if value is None else str(value)
