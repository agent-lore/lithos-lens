"""T1 slice 9 — the rebased /tasks filter vocabulary.

Pure predicate level: what the query string parses to, which project slugs a
task claims under each convention (REQUIREMENTS §5B.1), and the
creator-OR-claimer agent match (§5.4.2). Route-level acceptance lives in
``test_tasks_mvp.py``; the loader-level wiring in ``test_frontier.py``.
"""

from __future__ import annotations

import pytest

from lithos_lens.task_filtering import (
    invalid_project_metadata,
    matches_agent,
    matches_filters,
    project_convention_conflict,
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

    filters = parse_filters([("tag", ",".join(requested))], default_days=30)

    assert filters.tags == requested


def test_parse_filters_honors_a_tag_longer_than_any_lens_ceiling() -> None:
    """Lithos' tool schema puts no ``maxLength`` on a tag, so a task can validly
    carry a very long one. Filtering by that exact tag has to work — a length
    ceiling here would render the whole unfiltered board instead."""
    long_tag = "roadmap-" + "x" * 200

    filters = parse_filters([("tag", long_tag)], default_days=30)

    assert filters.tags == (long_tag,)


def test_parse_filters_folds_both_tag_spellings_into_one_list() -> None:
    filters = parse_filters(
        [("tag", "a,b"), ("tag", "c")],
        default_days=30,
    )

    assert filters.tags == ("a", "b", "c")


def test_parse_filters_collapses_repeated_tags() -> None:
    """``all(tag in task.tags …)`` is idempotent, so a repeat is a redundant
    chip and a wasted comparison per row, never a different result."""
    filters = parse_filters(
        [("tag", "roadmap-2026-08,roadmap-2026-08"), ("tag", "roadmap-2026-08")],
        default_days=30,
    )

    assert filters.tags == ("roadmap-2026-08",)


def test_parse_filters_carries_the_configured_convention() -> None:
    filters = parse_filters(
        [],
        default_days=30,
        project_convention="tag",
        project_tag_key="proj",
    )

    assert filters.projects == ()
    assert filters.project_convention == "tag"
    assert filters.project_tag_key == "proj"


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


def test_project_filter_under_metadata_convention_ignores_the_tag() -> None:
    filters = _filters(projects=("influx",), project_convention="metadata")

    assert not matches_filters(
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
