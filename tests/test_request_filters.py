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
from urllib.parse import parse_qs

import pytest
from fastapi import Request

from lithos_lens.request_filters import (
    project_add_url,
    project_clear_url,
    project_remove_url,
)

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
