"""Every Lithos write refusal becomes operator copy — never a bare code, never a 500.

§5C.4's table, one case per row, plus the three rules that hold across all of
them: a refused write says nothing was changed, no write error is answered with
a 500, and the unknown-outcome case claims neither that the write applied nor
that it did not.

The rows are the codes Lithos 0.5.0 actually raises on a write (T3 PRD D6 and
its Further Notes), so the cases below are the vocabulary Lens is held to — not
a sample of it. ``test_every_mapped_code_has_a_case`` keeps the two in step: a
code added to the mapper without a case here fails.

The two pages are rendered through the real template environment. No route
reaches them until the write funnel lands (T3-W4), so the helper below builds
the environment the app builds rather than going through the app — but the
templates, the filters and the partials they include are the shipped ones, and
the assertions are about what an operator would read on the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import partial
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from lithos_lens.request_filters import task_detail_url
from lithos_lens.template_vocabulary import short_id
from lithos_lens.web import TEMPLATE_DIR
from lithos_lens.write_errors import (
    CONFLICT_PAGE,
    MAPPED_CODES,
    NO_SUBJECT,
    NOT_READ,
    NOTICE_PARTIAL,
    REREAD_FAILED,
    TASK_ABSENT,
    UNKNOWN_OUTCOME_PAGE,
    MessageSegment,
    TaskRef,
    TaskReread,
    WriteAction,
    WriteProblem,
    map_write_error,
    message_segments,
)
from lithos_lens.write_routes import operator_page_url, request_identity

#: A real Lithos id, in both spellings the corpus carries: the ``cycle``
#: message names members by full id, and the short-id treatment the page gives
#: them is only meaningful if the id is longer than its prefix.
HYPHENATED_ID = "44a943fc-6055-4603-b3d7-9aabdecd73e9"
BARE_ID = "28105098aa4c4d0fbb2f6b06d0e0b0aa"
THIRD_ID = "9f1c2b7e-3d4a-4e5f-8a6b-7c8d9e0f1a2b"

#: What the operator is told about the store, spelled out HERE rather than
#: imported: the sentences are the requirement (§5C.4), so a test that took
#: them from the module under test would pass whatever the module said.
REFUSED_CLAIM = "Nothing was changed."
UNKNOWN_CLAIM = "The action may or may not have applied."
CLAIM_SENTENCE = {"nothing": REFUSED_CLAIM, "unknown": UNKNOWN_CLAIM}

SUBJECT = TaskRef(task_id=BARE_ID, title="Cut over Influx ingest path")


def envelope(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """A Lithos write error envelope: ``{status, code, message}`` plus fields.

    ``extra`` is how the rows that need more than a code and a message get it —
    ``candidates`` on an ambiguous prefix, ``field`` on an ``invalid_input``.
    That the mapper reads the WHOLE envelope is the point of the signature
    (§5C.4), so the cases pass whole envelopes.
    """
    return {"status": "error", "code": code, "message": message, **extra}


# ── §5C.4's table, one case per row ────────────────────────────────────


@dataclass(frozen=True)
class Case:
    """One row of the table: what the mapper is given, and what it must answer."""

    name: str
    action: WriteAction
    envelope: dict[str, Any] | None
    kind: str
    status_code: int
    headline: str
    #: The page that answers this on its own, or ``""`` when the copy belongs
    #: on the form the operator submitted.
    page: str = ""
    reread: TaskReread = NOT_READ
    subject: TaskRef = NO_SUBJECT
    typed_id: str = ""
    #: The upstream message, which rows that quote Lithos must carry verbatim.
    detail_text: str = ""
    show_code: bool = False
    log_level: str = "info"
    hint_contains: str = ""
    field: str = ""
    candidate_ids: tuple[str, ...] = ()
    claim: str = "nothing"


#: Lithos 0.5.0's own shape (``coordination.py`` ``upsert_task_edge``): the
#: edge, then the loop it closes, every member by full id — three distinct
#: members and prose between them, so a dropped, reordered or reworded run of
#: the message cannot pass for the original.
CYCLE_MESSAGE = (
    f"blocks edge {HYPHENATED_ID} -> {BARE_ID} would create a dependency cycle: "
    f"{BARE_ID} -> {THIRD_ID} -> {HYPHENATED_ID}"
)
CYCLE_MEMBERS = [HYPHENATED_ID, BARE_ID, BARE_ID, THIRD_ID, HYPHENATED_ID]

#: Lithos 0.5.0's ``parent_exists`` message (``upsert_task_edge``): it names the
#: existing parent by full id and says how to re-parent.
PARENT_EXISTS_MESSAGE = (
    f"task {BARE_ID} already has a parent ({THIRD_ID}); a task may have at most "
    "one parent. Remove the existing parent_child edge before re-parenting."
)

#: Lithos 0.5.0's ``resolve_task_id`` message for a too-short id, which names
#: the create parameter in the message and sends no other key.
SHORT_PARENT_MESSAGE = (
    "parent_task_id 'abc' is too short: pass the full task id or a prefix of at "
    "least 6 characters."
)

CASES: tuple[Case, ...] = (
    Case(
        name="task_not_found, task exists: the conflict page names the status now",
        action="complete",
        envelope=envelope("task_not_found", "Task not found or not open."),
        reread=TaskReread(outcome="exists", status="completed"),
        subject=SUBJECT,
        kind="conflict",
        status_code=409,
        headline="This task is now completed.",
        page=CONFLICT_PAGE,
    ),
    Case(
        name="task_not_found, task absent: it no longer exists",
        action="cancel",
        envelope=envelope("task_not_found", "Task not found or not open."),
        reread=TASK_ABSENT,
        subject=SUBJECT,
        kind="conflict",
        status_code=409,
        headline="This task no longer exists.",
        page=CONFLICT_PAGE,
        hint_contains="removed since you loaded the page",
    ),
    Case(
        name="task_not_found, re-read failed: neither fact is claimed",
        action="complete",
        envelope=envelope("task_not_found", "Task not found or not open."),
        reread=REREAD_FAILED,
        subject=SUBJECT,
        kind="conflict",
        status_code=409,
        headline="Lithos refused this: the task is either gone or no longer open.",
        page=CONFLICT_PAGE,
        hint_contains="could not read the task",
    ),
    Case(
        name="task_not_resolved: reopen on an open task",
        action="reopen",
        envelope=envelope("task_not_resolved", "Task is not resolved."),
        subject=SUBJECT,
        kind="conflict",
        status_code=409,
        headline="This task is already open.",
        page=CONFLICT_PAGE,
    ),
    Case(
        name="invalid_input: the upstream message, on the field it names",
        action="create",
        envelope=envelope("invalid_input", SHORT_PARENT_MESSAGE),
        kind="refused",
        status_code=422,
        headline="Lithos would not accept this.",
        detail_text=SHORT_PARENT_MESSAGE,
        field="parent_task_id",
    ),
    Case(
        name="ambiguous_id_prefix: the prefix, with the candidates as choices",
        action="create",
        envelope=envelope(
            "ambiguous_id_prefix",
            "Prefix 'influx' matches 2 tasks.",
            prefix="influx",
            candidates=[
                {"id": BARE_ID, "title": "Cut over Influx ingest path"},
                {"id": HYPHENATED_ID, "title": "Backfill Influx history"},
            ],
        ),
        kind="refused",
        status_code=422,
        headline="'influx' matches more than one task.",
        candidate_ids=(BARE_ID, HYPHENATED_ID),
        hint_contains="Pick the one you meant",
    ),
    Case(
        name="cycle: Lens's sentence, then the message verbatim",
        action="edge_upsert",
        envelope=envelope("cycle", CYCLE_MESSAGE),
        kind="refused",
        status_code=422,
        headline="This dependency would create a cycle.",
        detail_text=CYCLE_MESSAGE,
    ),
    Case(
        name="parent_exists: the task is named, then its parent and how to replace it",
        action="edge_upsert",
        envelope=envelope("parent_exists", PARENT_EXISTS_MESSAGE),
        subject=SUBJECT,
        kind="refused",
        status_code=422,
        headline="Cut over Influx ingest path already has a parent.",
        detail_text=PARENT_EXISTS_MESSAGE,
        hint_contains="remove its current parent relation first",
    ),
    Case(
        name="self_edge",
        action="edge_upsert",
        envelope=envelope("self_edge", "from_task_id == to_task_id."),
        subject=SUBJECT,
        kind="refused",
        status_code=422,
        headline="A task can't depend on itself.",
    ),
    Case(
        name="not_a_gate",
        action="edge_upsert",
        envelope=envelope("not_a_gate", "waits_on_gate requires a gate task."),
        subject=SUBJECT,
        kind="refused",
        status_code=422,
        headline="Cut over Influx ingest path isn't a gate — only a gate can be "
        "waited on.",
    ),
    Case(
        name="invalid_edge_type: a Lens defect, on the unknown-code path at error",
        action="edge_upsert",
        envelope=envelope("invalid_edge_type", "Unknown edge type 'blocks_maybe'."),
        kind="refused",
        status_code=422,
        headline="Lithos refused this with a code Lens does not recognise.",
        detail_text="Unknown edge type 'blocks_maybe'.",
        show_code=True,
        log_level="error",
        hint_contains="Report this",
    ),
    Case(
        name="an unknown code: the code and message verbatim, with the hint",
        action="complete",
        envelope=envelope("task_frozen", "Task is frozen by policy 'audit'."),
        kind="refused",
        status_code=422,
        headline="Lithos refused this with a code Lens does not recognise.",
        detail_text="Task is frozen by policy 'audit'.",
        show_code=True,
        log_level="warning",
        hint_contains="Report this",
    ),
    Case(
        name="no envelope: the outcome is unknown, plus what the re-read shows",
        action="complete",
        envelope=None,
        reread=TaskReread(outcome="exists", status="completed"),
        subject=SUBJECT,
        kind="unknown_outcome",
        status_code=200,
        headline="This task is now completed.",
        page=UNKNOWN_OUTCOME_PAGE,
        claim="unknown",
        log_level="warning",
    ),
)


def mapped(case: Case) -> WriteProblem:
    return map_write_error(
        case.action,
        case.envelope,
        reread=case.reread,
        subject=case.subject,
        typed_id=case.typed_id,
    )


CASE_PARAMS = [pytest.param(case, id=case.name) for case in CASES]


@pytest.mark.parametrize("case", CASE_PARAMS)
def test_every_row_produces_its_copy_and_its_answer(case: Case) -> None:
    problem = mapped(case)

    assert problem.kind == case.kind
    assert problem.status_code == case.status_code
    assert problem.headline == case.headline
    assert problem.page == case.page
    assert problem.claim == case.claim
    assert problem.detail_text == case.detail_text
    assert problem.show_code is case.show_code
    assert problem.log_level == case.log_level
    assert problem.field == case.field
    assert tuple(c.task_id for c in problem.candidates) == case.candidate_ids
    assert case.hint_contains in problem.hint
    # The action the funnel attempted rides along, for its one audit line.
    assert problem.action == case.action


@pytest.mark.parametrize("case", CASE_PARAMS)
def test_no_row_is_answered_with_a_server_error(case: Case) -> None:
    """§5C.4: never a 500. A refusal is an answer, not a Lens failure."""
    assert mapped(case).status_code < 500


@pytest.mark.parametrize("case", CASE_PARAMS)
def test_a_refused_write_says_nothing_was_changed(case: Case) -> None:
    """The fact the operator needs before the reason — on every refused row.

    The one row this does not hold for is the one where it would be a lie: with
    no answer from Lithos, Lens does not know, and says so instead.
    """
    problem = mapped(case)

    assert case.claim == ("unknown" if case.kind == "unknown_outcome" else "nothing")
    assert problem.change_statement == CLAIM_SENTENCE[case.claim]
    assert problem.nothing_changed is (case.claim == "nothing")


def test_copy_replaces_the_code_on_every_row_that_has_copy() -> None:
    """§5C.4: never surface a bare code — where Lens has a row, it has a sentence.

    The unknown-code path is the deliberate exception, and ``invalid_edge_type``
    is deliberately sent down it: there the code is the only handle the operator
    and the bug report have, so it is rendered on purpose rather than leaked.
    """
    for case in CASES:
        code = "" if case.envelope is None else str(case.envelope["code"])
        problem = mapped(case)
        if code in MAPPED_CODES and code != "invalid_edge_type":
            assert problem.code == code, case.name
            assert not problem.show_code, case.name
            assert problem.headline, case.name


def test_every_mapped_code_has_a_case() -> None:
    """The cases above are the mapper's vocabulary, not a sample of it."""
    covered = {case.envelope["code"] for case in CASES if case.envelope is not None}

    assert covered >= MAPPED_CODES


# ── task_not_found: one code, two facts, split by the re-read ──────────


def test_task_not_found_splits_on_the_re_read() -> None:
    """The same envelope, three re-reads, three different things said.

    Complete and cancel answer ``task_not_found`` for "missing" AND for "not
    open", so the envelope alone cannot say which happened. Nothing here reads
    the message to guess.
    """
    refusal = envelope("task_not_found", "Task not found or not open.")

    exists = map_write_error(
        "complete", refusal, reread=TaskReread(outcome="exists", status="cancelled")
    )
    absent = map_write_error("complete", refusal, reread=TASK_ABSENT)
    unreadable = map_write_error("complete", refusal, reread=REREAD_FAILED)

    assert exists.headline == "This task is now cancelled."
    assert absent.headline == "This task no longer exists."
    # Neither fact claimed: not "gone", not a status.
    assert "no longer exists" not in unreadable.headline
    assert "is now" not in unreadable.headline
    # All three are the same page, and all three say nothing changed.
    assert {exists.page, absent.page, unreadable.page} == {CONFLICT_PAGE}
    assert all(p.nothing_changed for p in (exists, absent, unreadable))


def test_a_status_the_re_read_did_not_return_is_not_named() -> None:
    """An ``exists`` re-read with no status cannot name one.

    The sentence is "this task is now *\\<status\\>*"; with the status missing
    it would read "this task is now ." — so the row falls back to claiming
    neither fact rather than printing a sentence with a hole in it.
    """
    problem = map_write_error(
        "cancel",
        envelope("task_not_found", "Task not found or not open."),
        reread=TaskReread(outcome="exists"),
    )

    assert problem.headline == (
        "Lithos refused this: the task is either gone or no longer open."
    )


# ── ambiguous_id_prefix: the envelope's candidates ─────────────────────


def test_ambiguous_prefix_carries_every_candidate_with_its_title() -> None:
    problem = map_write_error(
        "create",
        envelope(
            "ambiguous_id_prefix",
            "Prefix 'inf' matches 2 tasks.",
            prefix="inf",
            candidates=[
                {"id": BARE_ID, "title": "Cut over Influx ingest path"},
                {"id": HYPHENATED_ID, "title": "Backfill Influx history"},
            ],
        ),
    )

    assert problem.prefix == "inf"
    assert problem.headline == "'inf' matches more than one task."
    assert [(c.task_id, c.title) for c in problem.candidates] == [
        (BARE_ID, "Cut over Influx ingest path"),
        (HYPHENATED_ID, "Backfill Influx history"),
    ]


def test_ambiguous_prefix_falls_back_to_the_prefix_the_form_sent() -> None:
    """Until T3-W3 the envelope reaching the mapper may be code and message only.

    The copy still names a prefix, because the value the operator typed is a
    thing Lens knows without being told — and "that id is ambiguous" without
    saying which id is barely copy at all.
    """
    problem = map_write_error(
        "edge_upsert",
        envelope("ambiguous_id_prefix", "Prefix matches 2 tasks."),
        typed_id="influx",
    )

    assert problem.headline == "'influx' matches more than one task."
    assert problem.candidates == ()


def test_a_candidate_with_no_id_is_dropped_rather_than_offered() -> None:
    """A choice that cannot be chosen is worse than a shorter list."""
    problem = map_write_error(
        "create",
        envelope(
            "ambiguous_id_prefix",
            "Prefix 'inf' matches 2 tasks.",
            candidates=[{"title": "No id here"}, {"id": BARE_ID, "title": "Real"}],
        ),
    )

    assert [c.task_id for c in problem.candidates] == [BARE_ID]


def test_candidates_in_an_unexpected_shape_do_not_break_the_copy() -> None:
    """The authoritative shape is the contract T3-W3 vendors, not this module.

    A bare id string is read as an id; anything the mapper cannot read leaves
    the prefix copy standing on its own, because a refusal page that raises is
    the one outcome §5C.4 rules out.
    """
    strings = map_write_error(
        "create",
        envelope("ambiguous_id_prefix", "m", prefix="inf", candidates=[BARE_ID]),
    )
    junk = map_write_error(
        "create",
        envelope("ambiguous_id_prefix", "m", prefix="inf", candidates="not-a-list"),
    )

    assert [(c.task_id, c.title) for c in strings.candidates] == [(BARE_ID, "")]
    assert junk.candidates == ()
    assert junk.headline == "'inf' matches more than one task."


# ── The upstream message, verbatim ─────────────────────────────────────


def test_the_cycle_message_survives_segmentation_character_for_character() -> None:
    """ "Verbatim" is the claim, so reassembly is the test."""
    problem = map_write_error("edge_upsert", envelope("cycle", CYCLE_MESSAGE))

    assert problem.detail_text == CYCLE_MESSAGE
    assert "".join(segment.text for segment in problem.detail) == CYCLE_MESSAGE


def test_task_ids_in_a_message_are_marked_for_the_short_id_treatment() -> None:
    """Both spellings of a Lithos id, and nothing else in the sentence."""
    segments = message_segments(CYCLE_MESSAGE)

    assert [s.task_id for s in segments if s.task_id] == CYCLE_MEMBERS
    assert all(s.text == s.task_id for s in segments if s.task_id)


def test_ordinary_words_are_never_mistaken_for_task_ids() -> None:
    """The scan is presentation, so a false positive is the failure that matters.

    A word linked to a task that does not exist is a worse page than an id left
    as plain text — and the readable slugs the fixtures use are deliberately
    left alone rather than matched by a looser pattern.
    """
    message = "Edge from influx-ingest-cutover to influx-backfill closes a cycle."

    assert [s.task_id for s in message_segments(message) if s.task_id] == []
    assert message_segments(message)[0].text == message


def test_an_id_shaped_token_that_is_not_an_id_is_left_in_the_text() -> None:
    """The sieve is wide and ``uuid.UUID`` decides, so the reject path has to
    hold the message together: a run it declines is ordinary text, and the
    segments still reassemble into exactly what Lithos sent."""
    almost = "0123456789abcdef-0123456789abcd"
    message = f"Edge {almost} and {BARE_ID} close a cycle."

    segments = message_segments(message)

    assert [s.task_id for s in segments if s.task_id] == [BARE_ID]
    assert "".join(s.text for s in segments) == message


def test_a_message_with_no_ids_is_one_plain_segment() -> None:
    assert message_segments("Task is frozen.") == (
        MessageSegment(text="Task is frozen."),
    )
    assert message_segments("") == ()


# ── invalid_input: the field Lithos's message names ───────────────────


@pytest.mark.parametrize(
    ("message", "field"),
    [
        pytest.param(SHORT_PARENT_MESSAGE, "parent_task_id", id="parent_task_id"),
        pytest.param(
            "depends_on 'abc12' is too short: pass the full task id or a prefix "
            "of at least 6 characters.",
            "depends_on",
            id="depends_on",
        ),
        pytest.param(
            "a gate task requires metadata.gate_type in ['ci', 'external_task', "
            "'human', 'pr', 'timer'], got 'maybe'.",
            "metadata.gate_type",
            id="metadata.gate_type",
        ),
        pytest.param(
            "a 'timer' gate requires a parseable metadata.ready_at (ISO datetime), "
            "got 'tomorrow'.",
            "metadata.ready_at",
            id="metadata.ready_at",
        ),
        pytest.param(
            "Something no create field is named in.", "", id="no field: form level"
        ),
    ],
)
def test_invalid_input_lands_on_the_field_its_message_names(
    message: str, field: str
) -> None:
    """The messages Lithos 0.5.0 sends from ``lithos_task_create``, as sent:
    ``{status, code, message}`` and no other key. The parameter is named in
    the message, so that is where the field is found — and the message is
    still shown whole, on that field."""
    problem = map_write_error("create", envelope("invalid_input", message))

    assert problem.field == field
    assert problem.detail_text == message
    assert visible_text(detail_html(render(NOTICE_PARTIAL, problem))) == message


def test_a_field_name_inside_a_longer_name_is_not_a_field() -> None:
    problem = map_write_error(
        "create",
        envelope("invalid_input", "old_parent_task_id and depends_on_all are unknown."),
    )

    assert problem.field == ""


# ── The envelope Lens could not read ──────────────────────────────────


def test_an_envelope_with_no_code_is_a_refusal_not_an_unknown_outcome() -> None:
    """Lithos ANSWERED, and an error answer is a refusal: nothing changed.

    Only the absence of an answer leaves the outcome unknown, which is why
    ``None`` and ``{}`` are not the same input.
    """
    problem = map_write_error("reopen", {"status": "error", "message": "Nope."})

    assert problem.kind == "refused"
    assert problem.nothing_changed
    assert problem.detail_text == "Nope."
    assert problem.code == ""


def test_an_unnamed_subject_is_not_given_an_id_for_a_name() -> None:
    """The copy has to read, and a missing read is not a thing to pretend about."""
    problem = map_write_error("edge_upsert", envelope("parent_exists", "x"))

    assert problem.headline == "That task already has a parent."


# ── The unknown outcome ───────────────────────────────────────────────


@dataclass(frozen=True)
class LostAnswer:
    """One thing a re-read can show after Lithos never answered, and the copy
    the operator must be given for it."""

    name: str
    action: WriteAction
    reread: TaskReread
    headline: str
    hint: str
    subject: TaskRef = SUBJECT


RELOAD_HINT = "Reload to see the task's current state before trying again."

#: The no-envelope row, once per thing the re-read can find. The statuses are
#: distinct on purpose: an attempted ``complete`` that did NOT apply leaves the
#: task open, one an agent cancelled meanwhile reads cancelled, and one that
#: may have applied reads completed — the page must report each as it is, so a
#: mapper that said the same thing for all three cannot pass.
LOST_ANSWERS: tuple[LostAnswer, ...] = (
    LostAnswer(
        name="re-read finds the task still open",
        action="complete",
        reread=TaskReread(outcome="exists", status="open"),
        headline="This task is now open.",
        hint=RELOAD_HINT,
    ),
    LostAnswer(
        name="re-read finds the task cancelled",
        action="complete",
        reread=TaskReread(outcome="exists", status="cancelled"),
        headline="This task is now cancelled.",
        hint=RELOAD_HINT,
    ),
    LostAnswer(
        name="re-read finds the task completed",
        action="complete",
        reread=TaskReread(outcome="exists", status="completed"),
        headline="This task is now completed.",
        hint=RELOAD_HINT,
    ),
    LostAnswer(
        name="re-read finds no such task",
        action="cancel",
        reread=TASK_ABSENT,
        headline="This task no longer exists.",
        hint=RELOAD_HINT,
    ),
    LostAnswer(
        name="re-read failed too",
        action="cancel",
        reread=REREAD_FAILED,
        headline="Lens could not read the task's current state either.",
        hint="Reload once Lithos is reachable to see what the task is now.",
    ),
    LostAnswer(
        name="nothing to re-read (create)",
        action="create",
        reread=NOT_READ,
        headline="",
        hint=RELOAD_HINT,
        subject=NO_SUBJECT,
    ),
)
LOST_ANSWER_PARAMS = [pytest.param(case, id=case.name) for case in LOST_ANSWERS]

#: Words that would settle what the page must leave open — that the write
#: applied, or that it did not.
VERDICT_WORDS = (
    "failed",
    "succeeded",
    "success",
    "did not apply",
    "didn't apply",
    "was applied",
    "has been applied",
    "was not applied",
    "nothing was changed",
    "no changes",
)


@pytest.mark.parametrize("case", LOST_ANSWER_PARAMS)
def test_the_unknown_outcome_states_what_the_re_read_found_without_a_verdict(
    case: LostAnswer,
) -> None:
    problem = map_write_error(
        case.action, None, reread=case.reread, subject=case.subject
    )

    assert problem.page == UNKNOWN_OUTCOME_PAGE
    assert problem.claim == "unknown"
    assert problem.change_statement == UNKNOWN_CLAIM
    assert problem.headline == case.headline
    assert problem.hint == case.hint
    copy = " ".join([problem.change_statement, problem.headline, problem.hint])
    assert not [word for word in VERDICT_WORDS if word in copy.lower()]


def test_the_unknown_outcome_with_no_re_read_says_only_what_it_knows() -> None:
    """``create`` has nothing to re-read by id (D10), so there is no second line."""
    problem = map_write_error("create", None)

    assert problem.page == UNKNOWN_OUTCOME_PAGE
    assert problem.headline == ""
    assert problem.change_statement == UNKNOWN_CLAIM


# ── The two pages ─────────────────────────────────────────────────────


def render(template: str, problem: WriteProblem) -> str:
    """Render one write-outcome template through the shipped environment.

    Not through the app: no route answers with these pages until the write
    funnel lands (T3-W4), and a page nothing can reach is still a page whose
    copy has to be right. So this builds the same ``Jinja2Templates`` over the
    same template directory, with the ``short_id`` filter and the
    ``task_detail_url`` global ``web.create_app`` registers, and the two
    globals the chrome's identity chip reads, as ``register_write_routes``
    binds them (no configured default, no cookie) — the real ones, so the
    partials these pages include behave as they will in the app.

    ``url_for`` is the one stub: it is supplied by Starlette's own template
    response (the chrome's stylesheet and script URLs) rather than by Lens.
    """
    templates = Jinja2Templates(directory=TEMPLATE_DIR)
    templates.env.filters["short_id"] = short_id
    templates.env.globals["task_detail_url"] = task_detail_url
    templates.env.globals["operator_identity"] = partial(
        request_identity, default_operator=""
    )
    templates.env.globals["operator_page_url"] = operator_page_url
    templates.env.globals["url_for"] = lambda name, path="": f"/{name}/{path}"
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/tasks/x/complete",
            "query_string": b"",
            "headers": [],
        }
    )
    return templates.get_template(template).render(
        request=request,
        config=SimpleNamespace(environment="test"),
        active_view="tasks",
        problem=problem,
    )


def text_of(html: str, css_class: str) -> list[str]:
    """The visible text of every element carrying ``css_class``."""
    found = re.findall(rf'<[a-z]+ class="{css_class}"[^>]*>(.*?)</[a-z]+>', html, re.S)
    return [unescape(re.sub(r"<[^>]+>", " ", block)).strip() for block in found]


def test_the_conflict_page_states_that_nothing_changed_and_what_the_task_is_now() -> (
    None
):
    problem = map_write_error(
        "complete",
        envelope("task_not_found", "Task not found or not open."),
        reread=TaskReread(outcome="exists", status="completed"),
        subject=SUBJECT,
    )

    html = render(problem.page, problem)

    assert text_of(html, "write-outcome-claim") == [REFUSED_CLAIM]
    assert text_of(html, "write-outcome-headline") == ["This task is now completed."]
    # The task is named the way every other surface names one (§5.3).
    assert SUBJECT.title in html
    assert f'title="{BARE_ID}">{short_id(BARE_ID)}</code>' in html
    assert f'href="/tasks/{BARE_ID}"' in html


def test_the_conflict_page_for_a_task_that_is_gone_says_so() -> None:
    problem = map_write_error(
        "cancel",
        envelope("task_not_found", "Task not found or not open."),
        reread=TASK_ABSENT,
        subject=SUBJECT,
    )

    html = render(problem.page, problem)

    assert text_of(html, "write-outcome-claim") == [REFUSED_CLAIM]
    assert text_of(html, "write-outcome-headline") == ["This task no longer exists."]
    assert "removed since you loaded the page" in html


def outcome_section_text(html: str) -> str:
    """Everything the operator reads in a page's outcome section — its heading,
    the subject, the copy and the way on — as one whitespace-normalized line."""
    found = re.findall(
        r'<section class="write-outcome[^"]*">(.*?)</section>', html, re.S
    )
    assert len(found) == 1
    return " ".join(visible_text(found[0]).split())


@pytest.mark.parametrize("case", LOST_ANSWER_PARAMS)
def test_the_unknown_outcome_page_says_only_that_it_may_or_may_not_have_applied(
    case: LostAnswer,
) -> None:
    """The WHOLE page, not the mapper's lines: the template's own heading and
    links are operator copy too, and a heading that said "The action failed."
    over a correct claim would still be the verdict this page must not give.
    So the section's visible text is pinned word for word — the indeterminacy,
    then what the re-read found, and nothing else."""
    problem = map_write_error(
        case.action, None, reread=case.reread, subject=case.subject
    )

    html = render(problem.page, problem)

    subject = (
        [case.subject.title, short_id(case.subject.task_id)]
        if case.subject.task_id
        else []
    )
    way_on = "Open the task" if case.subject.task_id else "Back to the board"
    expected = [
        "Outcome unknown",
        "Lens did not hear back",
        *subject,
        UNKNOWN_CLAIM,
        *([case.headline] if case.headline else []),
        case.hint,
        way_on,
    ]
    assert outcome_section_text(html) == " ".join(expected)
    assert "<title>Outcome unknown | Lithos Lens</title>" in html
    assert text_of(html, "write-outcome-claim") == [UNKNOWN_CLAIM]
    page_copy = outcome_section_text(html).lower()
    assert not [word for word in VERDICT_WORDS if word in page_copy]


def test_both_pages_state_whether_anything_was_changed() -> None:
    """The one line neither page may be rendered without."""
    for problem, claim in (
        (
            map_write_error(
                "reopen", envelope("task_not_resolved", "Task is not resolved.")
            ),
            REFUSED_CLAIM,
        ),
        (map_write_error("complete", None), UNKNOWN_CLAIM),
    ):
        html = render(problem.page, problem)

        assert text_of(html, "write-outcome-claim") == [claim]


@pytest.mark.parametrize("case", CASE_PARAMS)
def test_every_row_renders_its_change_claim_and_its_copy(case: Case) -> None:
    """Every row of the table, rendered where it lands: its page, or — for a
    refused row, which has none — the notice inside the submitted form.

    The rendered surface states whether anything was changed, carries the row's
    sentence, and shows a code only on the unknown-code path.
    """
    problem = mapped(case)

    html = render(problem.page or NOTICE_PARTIAL, problem)

    assert text_of(html, "write-outcome-claim") == [CLAIM_SENTENCE[case.claim]]
    if case.headline:
        assert text_of(html, "write-outcome-headline") == [case.headline]
    # Upstream text is quoted where the row quotes it (its id links are
    # checked by the cycle test below) and nowhere else.
    assert bool(text_of(html, "write-outcome-detail")) is bool(case.detail_text)
    assert bool(text_of(html, "write-outcome-code")) is case.show_code


def test_an_unknown_code_renders_its_code_and_message_with_the_report_hint() -> None:
    """Rendered through the notice partial: a refused write has no page of its
    own, because its copy belongs on the form the operator submitted with their
    input kept."""
    problem = map_write_error(
        "complete", envelope("task_frozen", "Task is frozen by policy 'audit'.")
    )

    html = render(NOTICE_PARTIAL, problem)

    assert "task_frozen" in text_of(html, "write-outcome-code")[0]
    assert text_of(html, "write-outcome-detail") == [
        "Task is frozen by policy 'audit'."
    ]
    assert "Report this" in text_of(html, "write-outcome-hint")[0]
    assert text_of(html, "write-outcome-claim") == [REFUSED_CLAIM]


def test_each_candidate_renders_as_a_choice_naming_its_id_and_title() -> None:
    problem = map_write_error(
        "create",
        envelope(
            "ambiguous_id_prefix",
            "Prefix 'inf' matches 2 tasks.",
            prefix="inf",
            candidates=[
                {"id": BARE_ID, "title": "Cut over Influx ingest path"},
                {"id": HYPHENATED_ID, "title": "Backfill Influx history"},
            ],
        ),
    )

    html = render(NOTICE_PARTIAL, problem)

    choices = re.findall(r"<li>(.*?)</li>", html, re.S)
    assert len(choices) == 2
    for choice, candidate in zip(choices, problem.candidates, strict=True):
        assert candidate.title in choice
        assert f'title="{candidate.task_id}">{short_id(candidate.task_id)}' in choice
        assert f'href="/tasks/{candidate.task_id}"' in choice
    assert text_of(html, "write-outcome-claim") == [REFUSED_CLAIM]


def test_a_cycle_message_links_its_ids_and_shows_the_rest_as_written() -> None:
    """Presentation only: the ids become the short-id element, the prose does not
    change, and nothing in the sentence is marked safe HTML to get there."""
    problem = map_write_error("edge_upsert", envelope("cycle", CYCLE_MESSAGE))

    html = render(NOTICE_PARTIAL, problem)
    detail = detail_html(html)

    # The whole message, in order: every id shown as its short id and every
    # run of prose exactly as Lithos wrote it.
    assert visible_text(detail) == (
        "blocks edge 44a943fc -> 28105098 would create a dependency cycle: "
        "28105098 -> 9f1c2b7e -> 44a943fc"
    )
    # Each member linked to itself, in the order the message names them.
    assert link_targets(detail) == [f"/tasks/{member}" for member in CYCLE_MEMBERS]
    assert text_of(html, "write-outcome-headline") == [
        "This dependency would create a cycle."
    ]


def test_an_unknown_code_message_is_shown_whole_even_where_it_holds_an_id() -> None:
    """The short-id treatment is the ``cycle`` row's presentation, not the
    unknown-code path's: a message Lens has no copy for is a diagnostic to be
    reported as sent, so a full id in it stays full and unlinked."""
    message = f"Task {HYPHENATED_ID} cannot be changed."
    problem = map_write_error("complete", envelope("task_frozen", message))

    detail = detail_html(render(NOTICE_PARTIAL, problem))

    assert visible_text(detail) == message
    assert link_targets(detail) == []


def test_an_unknown_code_message_keeps_its_line_breaks_and_spacing() -> None:
    """Verbatim includes whitespace: a diagnostic laid out over lines, or with
    a run of spaces, reaches the operator laid out the same way.

    Two halves, because the browser decides the second. The text node carries
    the message exactly, with nothing the template added inside the element,
    and the element's rule keeps whitespace as written (``pre-wrap``) instead
    of collapsing it, while still wrapping a long line. A paragraph's default,
    ``white-space: normal``, would show the message on one line with the runs
    of spaces folded into one.
    """
    message = "Policy refused the task:\n  reason: queue  is paused.\n\tsee: ops"
    problem = map_write_error("complete", envelope("new_upstream_code", message))

    detail = detail_html(render(NOTICE_PARTIAL, problem))

    assert unescape(detail) == message
    assert css_declaration(".write-outcome-detail", "white-space") == "pre-wrap"


def css_declaration(selector: str, prop: str) -> str:
    """One declaration's value from the lens.css rule for exactly ``selector``,
    or "" if that rule does not declare it. Same shape as the stylesheet
    assertions in ``tests/test_blocker_chain.py``."""
    css = (
        Path(__file__).parent.parent / "src" / "lithos_lens" / "static" / "lens.css"
    ).read_text()
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules = re.findall(r"([^{}]+)\{([^}]*)\}", css)
    for selectors, body in rules:
        if selector in (part.strip() for part in selectors.split(",")):
            found = re.search(rf"(?:^|;)\s*{prop}\s*:\s*([^;]+)", body)
            if found:
                return found.group(1).strip()
    return ""


def test_parent_exists_names_the_parent_and_how_to_replace_it() -> None:
    """§5C.4 and §5C.2: the refusal names the existing parent, and the way to
    give the task a different one. The parent comes from Lithos's message —
    the mapper makes no call to look it up — linked like any id Lens shows."""
    problem = map_write_error(
        "edge_upsert",
        envelope("parent_exists", PARENT_EXISTS_MESSAGE),
        subject=SUBJECT,
    )

    html = render(NOTICE_PARTIAL, problem)
    detail = detail_html(html)

    assert text_of(html, "write-outcome-headline") == [
        "Cut over Influx ingest path already has a parent."
    ]
    assert visible_text(detail) == (
        "task 28105098 already has a parent (9f1c2b7e); a task may have at most "
        "one parent. Remove the existing parent_child edge before re-parenting."
    )
    assert link_targets(detail) == [f"/tasks/{BARE_ID}", f"/tasks/{THIRD_ID}"]
    assert text_of(html, "write-outcome-hint") == [
        "To give it a different parent, remove its current parent relation first, "
        "then add the new one."
    ]


def detail_html(html: str) -> str:
    """The inner HTML of the one upstream-message element on a page."""
    found = re.findall(r'<p class="write-outcome-detail">(.*?)</p>', html, re.S)
    assert len(found) == 1
    return found[0]


def visible_text(fragment: str) -> str:
    """What the operator reads in a fragment: its text, tags removed, nothing
    added between them."""
    return unescape(re.sub(r"<[^>]+>", "", fragment))


def link_targets(fragment: str) -> list[str]:
    """Every link's target in a fragment, in document order."""
    return re.findall(r'<a href="([^"]*)"', fragment)


def test_a_message_is_escaped_rather_than_rendered() -> None:
    """Lithos's message is upstream text on a Lens page, so it is escaped.

    Verbatim means the operator SEES what Lithos said — including markup, as
    text — not that the browser runs it.
    """
    problem = map_write_error(
        "edge_upsert", envelope("cycle", "<script>alert(1)</script> & more")
    )

    html = render(NOTICE_PARTIAL, problem)

    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; more" in html
    assert "<script" not in html
