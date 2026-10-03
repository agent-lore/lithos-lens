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
from html import unescape
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
    NOTHING_CHANGED,
    NOTICE_PARTIAL,
    OUTCOME_UNKNOWN,
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

#: A real Lithos id, in both spellings the corpus carries: the ``cycle``
#: message names members by full id, and the short-id treatment the page gives
#: them is only meaningful if the id is longer than its prefix.
HYPHENATED_ID = "44a943fc-6055-4603-b3d7-9aabdecd73e9"
BARE_ID = "28105098aa4c4d0fbb2f6b06d0e0b0aa"

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


CYCLE_MESSAGE = (
    f"Edge would create a cycle: {HYPHENATED_ID} -> {BARE_ID} -> {HYPHENATED_ID}"
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
        envelope=envelope(
            "invalid_input",
            "gate_type 'maybe' is not a known gate type.",
            field="gate_type",
        ),
        kind="refused",
        status_code=422,
        headline="Lithos would not accept this.",
        detail_text="gate_type 'maybe' is not a known gate type.",
        field="gate_type",
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
        name="parent_exists: the task is named",
        action="edge_upsert",
        envelope=envelope("parent_exists", "Task already has a parent."),
        subject=SUBJECT,
        kind="refused",
        status_code=422,
        headline="Cut over Influx ingest path already has a parent.",
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

    if problem.kind == "unknown_outcome":
        assert problem.change_statement == OUTCOME_UNKNOWN
        assert not problem.nothing_changed
    else:
        assert problem.change_statement == NOTHING_CHANGED
        assert problem.nothing_changed


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

    assert [s.task_id for s in segments if s.task_id] == [
        HYPHENATED_ID,
        BARE_ID,
        HYPHENATED_ID,
    ]
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


def test_the_unknown_outcome_states_the_re_read_without_claiming_either_way() -> None:
    reread = TaskReread(outcome="exists", status="completed")

    problem = map_write_error("complete", None, reread=reread, subject=SUBJECT)

    assert problem.change_statement == OUTCOME_UNKNOWN
    assert problem.headline == "This task is now completed."
    # Neither verdict appears anywhere in the copy.
    copy = " ".join([problem.change_statement, problem.headline, problem.hint])
    assert "was completed" not in copy
    assert "failed" not in copy
    assert "did not" not in copy


def test_the_unknown_outcome_with_no_re_read_says_only_what_it_knows() -> None:
    """``create`` has nothing to re-read by id (D10), so there is no second line."""
    problem = map_write_error("create", None)

    assert problem.page == UNKNOWN_OUTCOME_PAGE
    assert problem.headline == ""
    assert problem.change_statement == OUTCOME_UNKNOWN


def test_a_lost_answer_and_an_unreadable_re_read_claim_nothing() -> None:
    problem = map_write_error("cancel", None, reread=REREAD_FAILED, subject=SUBJECT)

    assert problem.change_statement == OUTCOME_UNKNOWN
    assert problem.headline == "Lens could not read the task's current state either."


# ── The two pages ─────────────────────────────────────────────────────


def render(template: str, problem: WriteProblem) -> str:
    """Render one write-outcome template through the shipped environment.

    Not through the app: no route answers with these pages until the write
    funnel lands (T3-W4), and a page nothing can reach is still a page whose
    copy has to be right. So this builds the same ``Jinja2Templates`` over the
    same template directory, with the ``short_id`` filter and the
    ``task_detail_url`` global ``web.create_app`` registers — the real ones, so
    the partials these pages include behave as they will in the app.

    ``url_for`` is the one stub: it is supplied by Starlette's own template
    response (the chrome's stylesheet and script URLs) rather than by Lens.
    """
    templates = Jinja2Templates(directory=TEMPLATE_DIR)
    templates.env.filters["short_id"] = short_id
    templates.env.globals["task_detail_url"] = task_detail_url
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

    assert text_of(html, "write-outcome-claim") == [NOTHING_CHANGED]
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

    assert text_of(html, "write-outcome-claim") == [NOTHING_CHANGED]
    assert text_of(html, "write-outcome-headline") == ["This task no longer exists."]
    assert "removed since you loaded the page" in html


def test_the_unknown_outcome_page_says_it_may_or_may_not_have_applied() -> None:
    problem = map_write_error(
        "complete",
        None,
        reread=TaskReread(outcome="exists", status="completed"),
        subject=SUBJECT,
    )

    html = render(problem.page, problem)

    assert text_of(html, "write-outcome-claim") == [OUTCOME_UNKNOWN]
    assert text_of(html, "write-outcome-headline") == ["This task is now completed."]
    assert NOTHING_CHANGED not in html


def test_both_pages_state_whether_anything_was_changed() -> None:
    """The one line neither page may be rendered without."""
    for problem in (
        map_write_error(
            "reopen", envelope("task_not_resolved", "Task is not resolved.")
        ),
        map_write_error("complete", None),
    ):
        html = render(problem.page, problem)

        assert text_of(html, "write-outcome-claim") == [problem.change_statement]


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
    assert text_of(html, "write-outcome-claim") == [NOTHING_CHANGED]


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
    assert text_of(html, "write-outcome-claim") == [NOTHING_CHANGED]


def test_a_cycle_message_links_its_ids_and_shows_the_rest_as_written() -> None:
    """Presentation only: the ids become the short-id element, the prose does not
    change, and nothing in the sentence is marked safe HTML to get there."""
    problem = map_write_error("edge_upsert", envelope("cycle", CYCLE_MESSAGE))

    html = render(NOTICE_PARTIAL, problem)
    detail = re.search(r'<p class="write-outcome-detail">(.*?)</p>', html, re.S)
    assert detail is not None

    assert html.count(f'href="/tasks/{HYPHENATED_ID}"') == 2
    assert f'href="/tasks/{BARE_ID}"' in html
    assert "Edge would create a cycle:" in detail.group(1)
    assert text_of(html, "write-outcome-headline") == [
        "This dependency would create a cycle."
    ]


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
