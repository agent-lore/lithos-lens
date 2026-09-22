"""T1 slice 9 — the rebased /tasks filter vocabulary.

Pure predicate level: what the query string parses to, which project slugs a
task claims under each convention (REQUIREMENTS §5B.1), and the
creator-OR-claimer agent match (§5.4.2). Route-level acceptance lives in
``test_tasks_mvp.py``; the loader-level wiring in ``test_frontier.py``.
"""

from __future__ import annotations

import pytest

from lithos_lens.task_filtering import (
    filters_narrow_the_open_side,
    invalid_project_metadata,
    matches_agent,
    matches_filters,
    project_convention_conflict,
    row_project_chips,
    row_tag_chips,
    task_projects,
)
from lithos_lens.tasks import (
    ClaimRecord,
    TaskFilters,
    TaskRecord,
    parse_filters,
)


def _task(
    *,
    created_by: str = "planner",
    tags: tuple[str, ...] = (),
    metadata: dict[str, object] | None = None,
    claims: tuple[ClaimRecord, ...] | None = (),
) -> TaskRecord:
    return TaskRecord(
        id="t1",
        title="Title",
        status="open",
        created_by=created_by,
        created_at="2026-04-26T10:00:00+00:00",
        tags=tags,
        metadata=dict(metadata or {}),
        claims=claims,
    )


def _filters(**overrides: object) -> TaskFilters:
    base: dict[str, object] = {
        "statuses": ("open",),
        "tags": (),
        "agent": "",
        "since": "",
    }
    base.update(overrides)
    return TaskFilters(**base)  # type: ignore[arg-type]


def test_parse_filters_collects_repeated_and_comma_separated_projects() -> None:
    filters = parse_filters(
        [("project", "lithos-loom"), ("project", "ganglion,influx")],
        default_days=30,
    )

    assert filters.projects == ("lithos-loom", "ganglion", "influx")


def test_parse_filters_honors_every_requested_tag() -> None:
    """``?tag=`` is an exact-match AND, and no term may be silently discarded:
    dropping one WIDENS the board, showing rows the operator excluded. The
    request size is bounded instead (``MAX_FILTER_QUERY_BYTES``), which cannot
    remove a predicate."""
    requested = tuple(f"t{i}" for i in range(40))

    filters = parse_filters([("tag", tag) for tag in requested], default_days=30)

    assert filters.tags == requested


def test_parse_filters_honors_a_tag_longer_than_any_lens_ceiling() -> None:
    """Lithos' tool schema puts no ``maxLength`` on a tag, so a task can validly
    carry a very long one. Filtering by that exact tag has to work — a length
    ceiling here would render the whole unfiltered board instead."""
    long_tag = "roadmap-" + "x" * 200

    filters = parse_filters([("tag", long_tag)], default_days=30)

    assert filters.tags == (long_tag,)


def test_parse_filters_keeps_tag_values_literal() -> None:
    """One ``tag`` parameter is one tag, verbatim. Unlike ``project`` and
    ``status``, a tag is not a constrained vocabulary — the vendored Lithos
    schema types it as a bare string — so a comma is tag content and
    surrounding whitespace is significant. Splitting made ``customer,2``
    unselectable and rewrote `` urgent `` into a filter that matched a
    different task."""
    filters = parse_filters(
        [("tag", "customer,2"), ("tag", " urgent "), ("tag", "needs review")],
        default_days=30,
    )

    assert filters.tags == ("customer,2", " urgent ", "needs review")


def test_parse_filters_still_splits_the_constrained_vocabularies() -> None:
    """``project`` and ``status`` keep the documented comma convenience: their
    values are slugs and an enum, where a comma cannot be part of a value."""
    filters = parse_filters(
        [("project", "lithos-loom,influx"), ("status", "open,completed")],
        default_days=30,
    )

    assert filters.projects == ("lithos-loom", "influx")
    assert filters.statuses == ("open", "completed")


def test_parse_filters_treats_the_empty_tag_as_a_literal_tag() -> None:
    """Regression (correctness/f-004): the vendored Lithos schema types a tag as
    a bare string with no ``minLength``, so ``""`` is a tag a task can validly
    carry and ``?tag=`` is the empty-tag scope.

    Reading it as "no filter" — which an earlier round did, and which its test
    codified — silently returned the whole unfiltered board for an exact-match
    request, the same class of failure as splitting ``customer,2``.
    """
    filters = parse_filters(
        [("tag", ""), ("tag", "roadmap-2026-08")],
        default_days=30,
    )

    assert filters.tags == ("", "roadmap-2026-08")


def test_parse_filters_ignores_a_blank_add_tag_box() -> None:
    """The filter bar's text input is its OWN parameter, so the blank it
    submits on every search cannot be confused with the literal empty tag.

    That separation is the whole reason ``tag`` can stay fully literal: one
    control means "nothing typed", the other means a value.
    """
    filters = parse_filters(
        [("tag", "roadmap-2026-08"), ("add_tag", "")],
        default_days=30,
    )

    assert filters.tags == ("roadmap-2026-08",)


def test_parse_filters_folds_the_add_tag_box_into_the_tag_set() -> None:
    filters = parse_filters(
        [("tag", "roadmap-2026-08"), ("add_tag", "loom-candidate")],
        default_days=30,
    )

    assert filters.tags == ("roadmap-2026-08", "loom-candidate")


def test_parse_filters_carries_the_configured_tag_key() -> None:
    """The tag KEY is the only project config a filter still carries: the
    posture knob is retired (§4.4), so matching needs nothing else."""
    filters = parse_filters([], default_days=30, project_tag_key="proj")

    assert filters.projects == ()
    assert filters.project_tag_key == "proj"
    assert not hasattr(filters, "project_convention")


def test_task_projects_unions_both_conventions() -> None:
    """§5B.1: under "both" a task belongs to every project either convention
    names — the metadata slug first, tags after, deduped."""
    task = _task(
        tags=("project:tagged", "project:lithos-loom", "area:docs"),
        metadata={"project": "lithos-loom"},
    )

    assert task_projects(task) == ("lithos-loom", "tagged")


@pytest.mark.parametrize(
    ("convention", "expected"),
    [
        ("metadata", ("meta-side",)),
        ("tag", ("tag-side",)),
        ("both", ("meta-side", "tag-side")),
    ],
)
def test_task_projects_honors_the_convention(
    convention: str, expected: tuple[str, ...]
) -> None:
    task = _task(tags=("project:tag-side",), metadata={"project": "meta-side"})

    assert task_projects(task, convention=convention) == expected  # type: ignore[arg-type]


def test_task_projects_uses_the_configured_tag_key() -> None:
    task = _task(tags=("proj:ganglion", "project:ignored"))

    assert task_projects(task, convention="tag", tag_key="proj") == ("ganglion",)


def test_task_without_a_project_claims_no_slug() -> None:
    assert task_projects(_task(tags=("area:docs",), metadata={"project": "  "})) == ()


@pytest.mark.parametrize(
    "value",
    [["influx"], {"slug": "influx"}, 42, 3.5, True, False, None],
)
def test_non_string_metadata_project_claims_no_slug(value: object) -> None:
    """§5B.1 defines metadata.project as a string slug. A non-string value is
    ignored, never coerced — ``str(["influx"])`` would fabricate the project
    ``['influx']``, put it in the dropdown, and match a URL nobody could
    otherwise produce."""
    task = _task(metadata={"project": value})

    assert task_projects(task) == ()
    assert task_projects(task, convention="metadata") == ()
    assert not matches_filters(
        task, filters=_filters(projects=(str(value),)), status="open"
    )


def test_malformed_metadata_project_does_not_fake_a_conflict() -> None:
    """An unreadable metadata value is not a competing convention: the tag is
    simply the task's only project."""
    task = _task(tags=("project:influx",), metadata={"project": ["influx"]})

    assert task_projects(task) == ("influx",)
    assert not project_convention_conflict(task)
    assert invalid_project_metadata(task)


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({}, False),
        ({"project": "influx"}, False),
        # Explicit null and blank strings mean "no project", not malformed.
        ({"project": None}, False),
        ({"project": "   "}, False),
        ({"project": ["influx"]}, True),
        ({"project": {"slug": "influx"}}, True),
        ({"project": 42}, True),
        ({"project": True}, True),
    ],
)
def test_invalid_project_metadata_flags_only_non_strings(
    metadata: dict[str, object], expected: bool
) -> None:
    assert invalid_project_metadata(_task(metadata=metadata)) is expected


def test_a_row_chips_the_project_only_its_metadata_carries() -> None:
    """§5B.1's row chip, and the case that had none: a task carrying
    ``metadata.project`` alone (loom's issue-mirrored work) has no tag for the
    row's tag strip to render, so the row must chip the project itself — the
    same slug ``?project=`` has matched since the membership knob was
    retired."""
    task = _task(metadata={"project": "lithos-loom"})

    assert row_project_chips(task) == ("lithos-loom",)
    assert row_tag_chips(task) == ()


def test_a_conflicting_rows_chip_is_the_metadata_value() -> None:
    """When the two conventions disagree, §5B.1 says the single displayed value
    is the metadata one. The tag keeps naming ``tagged`` beside it — neither
    value is dropped — but the row leads with the winner rather than stating
    only the loser."""
    task = _task(tags=("project:tagged", "area:docs"), metadata={"project": "stamped"})

    assert row_project_chips(task) == ("stamped",)
    assert row_tag_chips(task) == ("project:tagged", "area:docs")


def test_a_tag_only_row_renders_exactly_the_strip_it_always_had() -> None:
    """Nothing to lead with, nothing to drop: with no ``metadata.project`` the
    row's project is its tag, and that tag chip — project-styled, linking to
    its own board — is the chip it has always been."""
    task = _task(tags=("project:influx", "area:docs"))

    assert row_project_chips(task) == ()
    assert row_tag_chips(task) == ("project:influx", "area:docs")


def test_agreeing_conventions_are_chipped_once() -> None:
    """Tasks Lens creates write BOTH conventions (§5B.1), so the agreeing row
    is the common one. The metadata chip leads, and the tag that says the same
    project again is dropped rather than repeating it."""
    task = _task(tags=("project:influx", "area:docs"), metadata={"project": "influx"})

    assert row_project_chips(task) == ("influx",)
    assert row_tag_chips(task) == ("area:docs",)


def test_the_metadata_chip_leads_a_multi_project_row() -> None:
    """Regression (round-2 correctness/f-002): a task may belong to several
    projects at once (§5B.8), and upstream tag ORDER is not precedence.

    With ``primary`` stamped and tags naming ``secondary`` first, suppressing
    the metadata chip because *some* tag spelled ``primary`` left the row
    leading with ``secondary`` — the value §5B.1 does not resolve to. The chip
    never yields its place: the winner leads, the OTHER project keeps its tag
    chip, and the duplicate tag alone is dropped.
    """
    task = _task(
        tags=("project:secondary", "project:primary"),
        metadata={"project": "primary"},
    )

    assert row_project_chips(task) == ("primary",)
    assert row_tag_chips(task) == ("project:secondary",)


def test_a_duplicate_project_tag_is_matched_on_its_parsed_slug() -> None:
    """``task_projects`` reads a tag's slug stripped, so the dedup must too —
    ``project: influx `` is the same project, not a second one."""
    task = _task(tags=("project: influx ",), metadata={"project": "influx"})

    assert row_project_chips(task) == ("influx",)
    assert row_tag_chips(task) == ()


def test_a_rows_chips_read_the_configured_tag_key() -> None:
    """The tag half's key is config (§5B.9), and both halves of the strip read
    the same one: under ``proj`` the ``proj:influx`` tag is the duplicate the
    metadata chip has already said, while a literal ``project:influx`` tag is
    an ordinary tag there and is left exactly where it is."""
    agreeing = _task(tags=("proj:influx", "area:docs"), metadata={"project": "influx"})

    assert row_project_chips(agreeing, tag_key="proj") == ("influx",)
    assert row_tag_chips(agreeing, tag_key="proj") == ("area:docs",)

    other_key = _task(tags=("project:influx",), metadata={"project": "influx"})

    assert row_project_chips(other_key, tag_key="proj") == ("influx",)
    assert row_tag_chips(other_key, tag_key="proj") == ("project:influx",)


def test_a_projectless_row_chips_nothing() -> None:
    assert row_project_chips(_task(tags=("area:docs",))) == ()
    assert row_project_chips(_task(metadata={"project": ["influx"]})) == ()
    # …and a malformed value drops no tag: the tags are all the row has.
    assert row_tag_chips(_task(tags=("project:influx",), metadata={"project": 42})) == (
        "project:influx",
    )


def test_project_filter_matches_either_convention() -> None:
    metadata_only = _task(metadata={"project": "influx"})
    tag_only = _task(tags=("project:influx",))
    other = _task(tags=("project:ganglion",))
    filters = _filters(projects=("influx",))

    assert matches_filters(metadata_only, filters=filters, status="open")
    assert matches_filters(tag_only, filters=filters, status="open")
    assert not matches_filters(other, filters=filters, status="open")


def test_multiple_projects_select_their_union() -> None:
    filters = _filters(projects=("influx", "ganglion"))

    assert matches_filters(
        _task(tags=("project:ganglion",)), filters=filters, status="open"
    )
    assert not matches_filters(
        _task(tags=("project:cardinal",)), filters=filters, status="open"
    )


def test_project_filter_matches_a_row_under_either_convention() -> None:
    """`?project=` honours EITHER convention (§5B.1), so a control that offers
    a slug — the datalist, the scope picker, the quick-switch strip — can never
    hand the operator a value the filter refuses. `project_convention` used to
    select which one was honoured; it is parsed and ignored (§4.4), and a
    filter cannot even express a posture any more, which is why this test can
    state the rule without one."""
    filters = _filters(projects=("influx",))

    assert matches_filters(
        _task(tags=("project:influx",)), filters=filters, status="open"
    )
    assert matches_filters(
        _task(metadata={"project": "influx"}), filters=filters, status="open"
    )


def test_agent_filter_matches_creator_or_claimer() -> None:
    """Story 22: "everything agent-zero is involved in" is one filter."""
    creator = _task(created_by="agent-zero")
    claimer = _task(
        created_by="planner",
        claims=(ClaimRecord(agent="agent-zero", aspect="implementation"),),
    )
    unrelated = _task(
        created_by="planner", claims=(ClaimRecord(agent="worker-b", aspect="review"),)
    )
    filters = _filters(agent="agent-zero")

    assert matches_filters(creator, filters=filters, status="open")
    assert matches_filters(claimer, filters=filters, status="open")
    assert not matches_filters(unrelated, filters=filters, status="open")


def test_agent_match_on_unknown_claims_falls_back_to_creator() -> None:
    """``claims is None`` means claims were not returned; Lens does not guess a
    claimer match it cannot observe."""
    unknown = _task(created_by="planner", claims=None)

    assert not matches_agent(unknown, "agent-zero")
    assert matches_agent(unknown, "planner")


def test_conventions_conflict_only_when_both_present_and_disagreeing() -> None:
    assert project_convention_conflict(
        _task(tags=("project:tagged",), metadata={"project": "stamped"})
    )
    assert not project_convention_conflict(
        _task(tags=("project:same",), metadata={"project": "same"})
    )
    # A second, agreeing tag is the multi-project case (§5B.8), not a conflict.
    assert not project_convention_conflict(
        _task(tags=("project:same", "project:extra"), metadata={"project": "same"})
    )
    assert not project_convention_conflict(_task(metadata={"project": "stamped"}))
    assert not project_convention_conflict(_task(tags=("project:tagged",)))


# --- The two date windows (§5.4) --------------------------------------------
#
# ``since`` is the RESOLVED window (terminal rows, by resolved_at) and ``created
# _since`` is the CREATED window (every row, by created_at). The cases above
# pin the first; these pin the second, and the fact that neither reaches into
# the other.


def _terminal(*, created_at: str, resolved_at: str) -> TaskRecord:
    return TaskRecord(
        id="t2",
        title="Terminal",
        status="completed",
        created_by="worker",
        created_at=created_at,
        resolved_at=resolved_at,
    )


def test_parse_filters_reads_created_since_in_both_date_spellings() -> None:
    """Same syntax ``since`` accepts — ISO from a bookmark, DD/MM/YYYY from the
    filter bar's own text input — normalized to ISO either way."""
    assert (
        parse_filters([("created_since", "2026-09-14")], default_days=30).created_since
        == "2026-09-14"
    )
    assert (
        parse_filters([("created_since", "14/09/2026")], default_days=30).created_since
        == "2026-09-14"
    )


def test_created_since_defaults_to_no_window() -> None:
    """Unlike ``since``, which must bound the terminal FETCH and so defaults to
    the configured lookback, the created window is opt-in: absent means absent.
    A default here would narrow every open section of an unfiltered board."""
    filters = parse_filters([], default_days=30)

    assert filters.created_since == ""
    assert filters.since == parse_filters([], default_days=30).since != ""


def test_unparseable_created_since_falls_back_to_its_own_default() -> None:
    """Tolerated exactly as an unparseable ``since`` is — discarded rather than
    raising, so a mistyped bookmark still renders a board. The fallback is this
    field's default (no window), because falling back to a lookback would hide
    open rows under a date the operator never typed."""
    assert (
        parse_filters([("created_since", "nonsense")], default_days=30).created_since
        == ""
    )
    assert (
        parse_filters([("created_since", "32/13/2026")], default_days=30).created_since
        == ""
    )


def test_created_since_windows_open_rows_by_creation() -> None:
    """The whole point of the second field: ``since`` never narrows an open row,
    and ``created_since`` does."""
    filters = _filters(created_since="2026-04-20")
    # ``_task`` is created 2026-04-26.
    assert matches_filters(_task(), filters=filters, status="open")
    assert not matches_filters(
        _task(), filters=_filters(created_since="2026-05-01"), status="open"
    )


def test_created_since_is_inclusive_on_its_own_date() -> None:
    """``created_at >= date`` — a row created ON the date is in the window."""
    assert matches_filters(
        _task(), filters=_filters(created_since="2026-04-26"), status="open"
    )


def test_a_terminal_row_must_pass_both_windows() -> None:
    """They compose rather than override: the resolved window still windows by
    ``resolved_at`` and the created window still windows by ``created_at``, so a
    row has to satisfy each on its own date."""
    row = _terminal(created_at="2026-04-20T10:00:00+00:00", resolved_at="2026-05-10")
    both = {"statuses": ("completed",), "tags": (), "agent": ""}

    assert matches_filters(
        row,
        filters=_filters(**both, since="2026-05-01", created_since="2026-04-01"),
        status="completed",
    )
    # Created too early — passes the resolved window, fails the created one.
    assert not matches_filters(
        row,
        filters=_filters(**both, since="2026-05-01", created_since="2026-04-25"),
        status="completed",
    )
    # Resolved too early — passes the created window, fails the resolved one.
    assert not matches_filters(
        row,
        filters=_filters(**both, since="2026-06-01", created_since="2026-04-01"),
        status="completed",
    )


def _created(created_at: str) -> TaskRecord:
    """An open row carrying one upstream ``created_at`` verbatim.

    ``normalize_task`` does not validate the string — a missing value becomes
    ``""`` and anything else is preserved as written — so every value these
    tests pass is one the boundary really admits.
    """
    return TaskRecord(
        id="t3",
        title="Stamped",
        status="open",
        created_by="planner",
        created_at=created_at,
    )


@pytest.mark.parametrize(
    "created_at",
    [
        "",
        "not-a-date",
        "26/13/2026",
        # Junk wearing a valid ten-character prefix. These are the cases a
        # ``parse_date(task.created_at)`` guard misses (round-2
        # correctness/f-001): it reads ``value[:10]``, so both of these were
        # admitted as a valid 2026-05-02 — inside the window below — even
        # though nothing can say when the row was actually created.
        "2026-05-02junk",
        "2026-05-02T99:99:99",
        "2026-05-02T10:00:00+99:00",
    ],
)
def test_created_since_drops_a_row_whose_creation_date_is_unreadable(
    created_at: str,
) -> None:
    """Regression (correctness/f-001). The OPPOSITE of the resolved branch, and
    for the reason that branch gives: there the server already applied
    ``resolved_since`` and returned the row anyway, so keeping it honours a
    decision upstream made. Nothing applies this window but this predicate, so
    a row it cannot evaluate has not been shown to satisfy ``created_at >=
    date`` — keeping it would put an unvouched-for row on a narrowed board and
    into its section count.

    Without the window it is an ordinary row and still renders.
    """
    assert not matches_filters(
        _created(created_at),
        filters=_filters(created_since="2026-05-01"),
        status="open",
    )
    assert matches_filters(_created(created_at), filters=_filters(), status="open")


@pytest.mark.parametrize(
    ("created_at", "inside"),
    [
        # 2026-05-02T04:30 UTC — inside a 2026-05-02 window despite reading
        # 2026-05-01 in its own offset.
        ("2026-05-01T23:30:00-05:00", True),
        # 2026-05-01T19:30 UTC — outside it, despite reading 2026-05-02.
        ("2026-05-02T00:30:00+05:00", False),
        # The unambiguous pair, as controls.
        ("2026-05-02T00:00:00+00:00", True),
        ("2026-05-01T23:59:59+00:00", False),
        # A bare date is midnight UTC, the same instant the window names.
        ("2026-05-02", True),
    ],
)
def test_created_since_compares_the_row_instant_in_utc(
    created_at: str, inside: bool
) -> None:
    """Regression (round-2 correctness/f-001): the window is a date, the row is
    an instant, and the two are compared in UTC.

    Reading the stamp's first ten characters compares the date in whatever
    offset the row happens to carry, so the same moment fell on either side of
    the window depending on who wrote it. Every other instant in Lens is
    normalized to UTC first (``parse_timestamp``, which the age-based attention
    rules rest on), and this one is now too.
    """
    assert (
        matches_filters(
            _created(created_at),
            filters=_filters(created_since="2026-05-02"),
            status="open",
        )
        is inside
    )


def test_created_since_narrows_the_open_side() -> None:
    """It hides OPEN rows, so every whole-board claim (healthy stripe, empty
    corpus, the epic strip's scope) must treat the board as narrowed — which is
    the one thing ``since`` deliberately does not do."""
    assert filters_narrow_the_open_side(_filters(created_since="2026-05-01"))
    assert not filters_narrow_the_open_side(_filters(since="2026-05-01"))
