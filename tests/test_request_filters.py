"""The generated-URL builders, exercised directly on a request.

The board's links are built once per render from one allowlist
(``request_filters._PRESERVED_FILTER_KEYS``), and most of them are covered
where they are USED — through the rendered page, in ``test_tasks_mvp``. The
project strip's three builders (§5.3) get a unit test of their own as well,
because "preserves every other parameter" is a claim about a SET of keys, and
a page test can only assert the handful its fixture happens to carry: here the
request carries every parameter a live board can hold at once and the parsed
output is compared whole, so a key silently dropped from the allowlist fails.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qs, urlencode

import pytest
from fastapi import Request

from lithos_lens.request_filters import (
    project_add_problem,
    project_add_url,
    project_clear_url,
    project_remove_url,
    tag_chip_class,
)
from lithos_lens.tasks import MAX_FILTER_QUERY_BYTES

# Every FILTER a live board can carry at once, plus the four kinds of
# parameter that reach ``/tasks`` and must NOT ride a generated link. The
# board's links are an allowlist by design (``_PRESERVED_FILTER_KEYS``), so
# "anything else present" is anything the board itself would send back — not
# any key an arbitrary URL happens to carry. Each exclusion has its own
# reason, and :func:`test_project_links_carry_the_board_state_and_nothing_else`
# states them.
_FULL_QUERY = (
    "status=open&status=completed"
    "&tag=roadmap-2026-09&tag=needs-human"
    "&epic=epic-1"
    "&agent=planner"
    "&since=2026-04-01"
    "&created_since=2026-01-01"
    "&all_agents=1"
    "&project=lithos-lens,lithos-loom"
    "&selected=loom-worker"
    "&chain=some-task"
    "&claimed_state=any"
    "&unrecognised=x"
)

# What every project link must carry forward, whichever way it moves the
# project filter.
_PRESERVED = {
    "status": ["open", "completed"],
    "tag": ["roadmap-2026-09", "needs-human"],
    "epic": ["epic-1"],
    "agent": ["planner"],
    "since": ["2026-04-01"],
    "created_since": ["2026-01-01"],
    # Not a row filter and not in the allowlist — re-emitted by these builders
    # on purpose (the strip is clicked while the picker is open) as the
    # canonical literal the toggle itself writes.
    "all_agents": ["1"],
}

_SELECTED = ("lithos-lens", "lithos-loom")


def _request(query: str = _FULL_QUERY) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/tasks",
            "query_string": query.encode(),
            "headers": [],
        }
    )


def _query_of(url: str) -> dict[str, list[str]]:
    path, _, query = url.partition("?")
    assert path == "/tasks", url
    return parse_qs(query, keep_blank_values=True)


@pytest.mark.parametrize(
    ("name", "build", "expected_project"),
    [
        (
            "add",
            lambda request: project_add_url(request, _SELECTED, "lithos-core"),
            ["lithos-lens,lithos-loom,lithos-core"],
        ),
        (
            "remove",
            lambda request: project_remove_url(request, _SELECTED, "lithos-lens"),
            ["lithos-loom"],
        ),
        ("clear", project_clear_url, None),
    ],
)
def test_project_links_preserve_every_other_parameter(
    name: str,
    build: Callable[[Request], str],
    expected_project: list[str] | None,
) -> None:
    """Each builder moves ``project`` and nothing else.

    Compared as the WHOLE parsed query rather than key by key, so a parameter
    that stopped being carried — or one that started being invented — fails
    here rather than in whichever surface first misses it.
    """
    query = _query_of(build(_request()))

    expected = dict(_PRESERVED)
    if expected_project is not None:
        expected["project"] = expected_project
    assert query == expected, name


@pytest.mark.parametrize(
    ("key", "why"),
    [
        (
            "selected",
            # web.py `_selected_panel`: "no generated link carries a selection "
            # "into navigation it has nothing to do with" — it names one open
            # panel, not a slice of the board, and the panel it names may not
            # even be a row of the board the chip leads to.
            "one open panel is not board state",
        ),
        (
            "chain",
            # One blocker-expansion walk. It is MEASURED against
            # MAX_FILTER_QUERY_BYTES precisely because a fragment copies it per
            # line; copying it per chip as well would carry a walk into
            # navigation that has nothing to do with it.
            "one expansion walk is not board state",
        ),
        (
            "claimed_state",
            # Retired with the graph-native dashboard. The allowlist exists so
            # a legacy bookmark degrades on arrival instead of propagating.
            "a retired filter must not be revived by a link",
        ),
        (
            "unrecognised",
            # The one with teeth: an unknown key is not in _MEASURED_QUERY_KEYS,
            # so nothing bounds its size. Echoing it once per chip would let a
            # crafted URL multiply its own bytes across the strip — the exact
            # multiplication the byte budget was added to close.
            "an unmeasured value must not be multiplied per chip",
        ),
    ],
)
def test_project_links_carry_the_board_state_and_nothing_else(
    key: str, why: str
) -> None:
    """The allowlist's own rule, one case per reason it exists.

    Every one of these arrives on a real request — the panel selection on every
    click-opened board, ``chain`` from an expanded blocker, ``claimed_state``
    from a pre-T1 bookmark — and none of them describes the slice of tasks a
    project chip navigates to. A builder that preserved "everything present"
    would carry all four.
    """
    for url in (
        project_add_url(_request(), _SELECTED, "lithos-core"),
        project_remove_url(_request(), _SELECTED, "lithos-lens"),
        project_clear_url(_request()),
    ):
        assert key not in _query_of(url), why


def test_adding_a_project_already_selected_does_not_duplicate_it() -> None:
    """A chip is only rendered unselected when its slug is absent, but the
    builder is the thing that guarantees it: a doubled slug would widen the
    URL on every click and read as two projects in the filter box."""
    url = project_add_url(_request(), _SELECTED, "lithos-loom")

    assert _query_of(url)["project"] == ["lithos-lens,lithos-loom"]


def test_removing_the_last_project_leaves_the_parameter_off() -> None:
    """Removing the only selected project is clearing it: an empty
    ``?project=`` would filter to the projectless rows rather than to none."""
    url = project_remove_url(_request(), ("lithos-lens",), "lithos-lens")

    assert "project" not in _query_of(url)


def test_a_project_link_on_a_bare_board_carries_nothing_extra() -> None:
    """The other end of the range: no filters at all, so the link is the
    project alone — and clearing from there is the bare board."""
    assert (
        project_add_url(_request(""), (), "lithos-lens") == "/tasks?project=lithos-lens"
    )
    assert project_clear_url(_request("")) == "/tasks"


def test_all_agents_is_re_emitted_as_the_canonical_literal() -> None:
    """Whatever spelling the toggle arrived in, the link writes ``1``: the
    value is never copied out of the request, so a huge ``?all_agents=`` cannot
    be multiplied once per chip in the strip."""
    url = project_add_url(_request("all_agents=yes"), (), "lithos-lens")

    assert _query_of(url)["all_agents"] == ["1"]


def test_a_board_without_the_toggle_does_not_grow_one() -> None:
    """The complement: preserved when present, never invented."""
    url = project_add_url(_request("tag=roadmap"), (), "lithos-lens")

    assert "all_agents" not in _query_of(url)


def _tag_query_of(emitted_bytes: int) -> str:
    """A one-tag query whose arrival measures exactly ``emitted_bytes``.

    ``t`` so the value costs one byte per character, which makes the arithmetic
    below the ceiling's own rather than an encoding's.
    """
    return "tag=" + "t" * (emitted_bytes - len("tag="))


def test_an_add_link_the_board_would_refuse_is_not_offered() -> None:
    """Regression (round-1 correctness f-001): an ADD link is the one link on
    the strip that makes the query LONGER, so it is the one that can point
    outside ``MAX_FILTER_QUERY_BYTES``.

    A request whose filters sit exactly on the ceiling is accepted and draws
    the strip; ``&project=lithos-lens`` is twenty bytes it does not have, and
    following that chip reached ``filter_query_oversized`` and 400 instead of
    the project it named. There is no shorter link to build — every other pair
    is a filter the board was asked for — so the builder returns nothing and
    the chip is drawn without one.
    """
    added = len("&") + len("project=lithos-lens")
    # The largest board from which adding this slug still fits, and one byte
    # of tag more: the two sides of the ceiling, computed from it.
    fits = _tag_query_of(MAX_FILTER_QUERY_BYTES - added)
    over = fits + "t"

    assert project_add_url(_request(fits), (), "lithos-lens") == (
        f"/tasks?{fits}&project=lithos-lens"
    )
    assert project_add_url(_request(over), (), "lithos-lens") == ""


def test_the_budget_is_measured_over_what_the_link_will_re_emit() -> None:
    """The ceiling is charged against the ALLOWLISTED keys only, so an
    unrecognised key — which the link drops — must not spend budget the
    operator could have used on a project.

    The complement of ``test_project_links_carry_the_board_state_and_nothing_
    else``: a key that rides nowhere costs nothing here either, which is what
    keeps the two readings of the query one reading.
    """
    added = len("&") + len("project=lithos-lens")
    fits = _tag_query_of(MAX_FILTER_QUERY_BYTES - added)
    junk = "&unrecognised=" + "x" * 4000

    assert project_add_url(_request(fits + junk), (), "lithos-lens") == (
        f"/tasks?{fits}&project=lithos-lens"
    )


def test_removing_and_clearing_are_offered_at_the_very_ceiling() -> None:
    """The other half of f-001: a link that only SHRINKS the query cannot
    cross a ceiling the request it was built from already cleared, so the way
    back out of a project filter stays reachable on any board that renders.
    """
    # A two-project board sitting exactly ON the ceiling. The selection is
    # measured as the parse measures it — through ``urlencode``, which charges
    # the separating comma three bytes — so this is the real edge, not one
    # counted off the literal href.
    selection = urlencode([("project", ",".join(_SELECTED))])
    tag = _tag_query_of(MAX_FILTER_QUERY_BYTES - len("&") - len(selection))
    request_query = f"{tag}&project={','.join(_SELECTED)}"

    assert project_remove_url(_request(request_query), _SELECTED, "lithos-lens") == (
        f"/tasks?{tag}&project=lithos-loom"
    )
    assert project_clear_url(_request(request_query)) == f"/tasks?{tag}"
    # …and the chip that WOULD grow it is the one withheld.
    assert project_add_url(_request(request_query), _SELECTED, "lithos-core") == ""


def test_a_slug_the_filter_cannot_carry_is_not_offered() -> None:
    """The other way a chip can lead nowhere: ``?project=`` is comma-joined and
    the parse splits every value on the comma, so a slug that CONTAINS one
    (``project:a,b`` is a legal tag) can never be expressed as a filter value —
    following its chip would filter for ``a`` OR ``b`` and show a board with no
    rows of the project the chip named. Nothing about the budget saves it, so
    the builder withholds the link and names the reason, which the template
    shows in place of the budget wording.
    """
    assert project_add_url(_request("tag=roadmap"), (), "lithos,lens") == ""
    problem = project_add_problem(_request("tag=roadmap"), (), "lithos,lens")
    assert "comma" in problem
    # A slug the filter CAN carry has no problem to report…
    assert project_add_problem(_request("tag=roadmap"), (), "lithos-lens") == ""
    # …and the budget refusal reports its own reason, distinct from the comma.
    added = len("&") + len("project=lithos-lens")
    over = _tag_query_of(MAX_FILTER_QUERY_BYTES - added) + "t"
    assert "query-size limit" in project_add_problem(_request(over), (), "lithos-lens")


def test_a_tag_is_classed_a_project_by_the_configured_key() -> None:
    """Regression (round-2 correctness/f-001): project STYLING reads the same
    §5B.9 key as project resolution, because they answer the same question.

    Hard-coding ``project:`` left a deployment keyed on ``proj`` with a row
    whose only project chip was an unstyled tag — for a project ``?project=``
    matches — while dressing an ordinary ``project:`` tag as a project it does
    not belong to there.
    """
    assert tag_chip_class("project:influx") == "tag-chip tag-chip-project"
    assert tag_chip_class("area:docs") == "tag-chip"

    assert tag_chip_class("proj:influx", tag_key="proj") == "tag-chip tag-chip-project"
    assert tag_chip_class("project:influx", tag_key="proj") == "tag-chip"
