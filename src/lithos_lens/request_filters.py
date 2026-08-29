"""Request-scoped filters and the URLs that carry them across navigation.

Split out of ``web.py`` when the T1-S13 MERGE pushed that module past the
800-line god-module ceiling — neither parent breached it alone (#50 took
web.py to 630, T1-S13 to 772; the merge was 857, because the two additions are
disjoint), which is why the remediation is its own change rather than an edit
to either.

The seam is a real one rather than a line-count convenience. Everything here
answers one question — *what did this request ask the board to show, and how
does a generated link carry that forward?* — and none of it touches the ASGI
app, the templates or the application state. ``web.py`` keeps the routes and
the rendering; the URL builders below are registered there as Jinja globals and
called once per generated link.

Distinct from ``task_filtering.py``, which is Foundation-tier: that module owns
the PREDICATES deciding whether a row survives a filter. This one is the HTTP
edge — reading filters off a ``Request``, measuring what they cost to re-emit,
and writing them back into hrefs. The dependency runs one way: this imports the
domain's filter vocabulary from ``tasks.py`` and neither imports back.

Two properties are load-bearing enough to state up front:

- **Parsed once per request.** The URL helpers run once per generated link —
  one per row, one per tag per row, plus the summary cards and the epic chips —
  and each parse is O(every param on the request), not O(the preserved ones).
  See :data:`_FILTER_STATE_ATTR`.
- **An oversized query preserves NOTHING.** The refusal is made at the parse,
  not per template, so a page that refuses to reflect a value cannot reflect it
  anyway once per link.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from fastapi import Request

from lithos_lens.tasks import (
    ADD_TAG_FILTER_KEY,
    MAX_FILTER_QUERY_BYTES,
    TAG_FILTER_KEY,
    TAG_FILTER_KEYS,
    honored_tags,
    split_filter_values,
)

_PRESERVED_FILTER_KEYS = (
    "status",
    "project",
    "agent",
    "since",
    TAG_FILTER_KEY,
    ADD_TAG_FILTER_KEY,
    "epic",
)


@dataclass(frozen=True)
class _RequestFilters:
    """This request's preserved filters, parsed once (see ``_request_filters``)."""

    oversized: bool
    params: tuple[tuple[str, str], ...]


# Where the parsed filters hang off the request. The URL helpers below run once
# per generated link — one per row, one per tag per row, plus the summary cards
# and the epic chips — and each scan is O(every param on the request), not
# O(the six preserved ones. Recomputing per link cost O(J x L) for J params and
# L links, and J is bounded only by the URL length limit: an unrecognised key
# scores nothing against the byte budget but still has to be walked. Parsed
# once, a request costs O(J + L).
_FILTER_STATE_ATTR = "lens_preserved_filters"


def _parse_preserved_filters(request: Request) -> _RequestFilters:
    """Scan the query string once: the preserved filters, and their emitted size.

    The size is the length of the exact string that will go into the links —
    ``urlencode`` run over the very pairs this returns — rather than any
    estimate of it. Both ways of estimating have now been wrong: counting code
    points ignored percent-encoding, where one character can cost 12 bytes (an
    astral character becomes ``%F0%9F%98%80``), and letting a value at the
    budget emit 12x it; and adding two separator bytes per pair over-counted by
    one, because ``urlencode`` writes "=" per pair but "&" only BETWEEN them,
    so a query of exactly ``MAX_FILTER_QUERY_BYTES`` was refused by a ceiling
    documented — and displayed to the operator — as rejecting only what is
    larger. Measuring the real thing cannot drift from it.

    Measured over the request AS IT ARRIVED, then emitted in canonical form.
    The two differ only for tags: ``add_tag`` (the filter bar's text box) is
    folded into the ``tag`` list once and never re-emitted, so a tag added
    through the form propagates through navigation as an ordinary ``tag`` pair
    instead of an "add" that would re-apply on every click. Canonicalising can
    only shrink the query — ``add_tag`` is four characters longer than ``tag``,
    and de-duplication only removes — so measuring the arrival form keeps the
    ceiling conservative.

    Empty values are dropped for every key EXCEPT ``tag``, where the empty
    string is a literal tag (``?tag=`` is the empty-tag scope) and dropping it
    would silently widen the board to everything.
    """
    raw = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key in _PRESERVED_FILTER_KEYS
    ]
    params = [
        (key, value) for key, value in raw if key not in TAG_FILTER_KEYS and value
    ]
    params.extend(
        (TAG_FILTER_KEY, tag)
        for tag in honored_tags(
            [value for key, value in raw if key == TAG_FILTER_KEY],
            added=[value for key, value in raw if key == ADD_TAG_FILTER_KEY],
        )
    )
    if len(urlencode(raw)) > MAX_FILTER_QUERY_BYTES:
        # An oversized request preserves NOTHING, so the guarantee holds at the
        # choke point rather than per template: the refusal page still renders
        # its own chrome (the "back to tasks" link), and every URL on it must
        # come out unfiltered — otherwise the page refusing to reflect the
        # value reflects it anyway, once per link.
        return _RequestFilters(oversized=True, params=())
    return _RequestFilters(oversized=False, params=tuple(params))


def _request_filters(request: Request) -> _RequestFilters:
    """This request's parsed filters, computed on first use and then reused."""
    cached = getattr(request.state, _FILTER_STATE_ATTR, None)
    if cached is None:
        cached = _parse_preserved_filters(request)
        setattr(request.state, _FILTER_STATE_ATTR, cached)
    return cached


def filter_query_oversized(request: Request) -> bool:
    """True when this request's filters exceed ``MAX_FILTER_QUERY_BYTES``.

    Measured over the preserved keys only — they are the ones re-emitted into
    every generated URL, and so the ones whose size the response multiplies.
    An oversized request is answered explicitly rather than served as though
    the offending filter had not been sent: see the constant for why trimming
    is not an option.
    """
    return _request_filters(request).oversized


def _preserved_filter_params(
    request: Request, *, exclude: str = ""
) -> list[tuple[str, str]]:
    """The live filters of this request, ready to re-emit into a generated URL.

    Echoed verbatim, every key alike — the routes refuse an over-budget query
    before rendering, so there is nothing left here to trim, and trimming per
    key was the wrong shape anyway: it dropped filter terms the board had been
    asked for.
    """
    return [
        (key, value)
        for key, value in _request_filters(request).params
        if key != exclude
    ]


def board_is_filtered(request: Request) -> bool:
    """True when this request narrows the board to a subset of tasks.

    Exists so the optimistic skeleton row can be suppressed on a narrowed
    board. The client cannot answer this itself: a ``task.created`` payload
    carries no tags, no project and no creator, so there is nothing to evaluate
    a new task against even if it wanted to.

    Deliberately answered HERE rather than by the browser reading its own query
    string, so the set of keys that count as narrowing has exactly one
    definition — :data:`_PRESERVED_FILTER_KEYS` — instead of a second copy in
    JavaScript that drifts the next time a filter is added.

    Every preserved key counts, ``since`` included. A just-created task is
    inside any past-anchored window, so ``since`` alone will rarely exclude it
    — but "rarely" is the wrong bar for a row that ASSERTS membership and
    persists if reconciliation fails, and a future ``since`` excludes it
    outright. The conservative reading costs an optimistic row on a narrowed
    board and buys a board that never claims something it has not checked.
    """
    return bool(_preserved_filter_params(request))


def board_admits_open(request: Request) -> bool:
    """True when a task that is now ``open`` still belongs on this board.

    The sibling question to :func:`board_is_filtered`, and deliberately a
    narrower one. That predicate counts EVERY preserved key, because a
    just-created task has no attributes the client can check against any of
    them. This one is asked about a row the server ALREADY rendered here — it
    passed every filter — so the only thing that can evict it is the one
    attribute reopening actually changes: its status. Project, agent, tag,
    epic and ``since`` are all unmoved by a reopen (``created_at`` does not
    change), and suppressing on those would hide a row from the board it still
    belongs to.

    Answered here rather than by the browser reading its own query string, for
    the same reason as :func:`board_is_filtered`: one definition of what the
    status filter's values mean. Which is why the values go through
    :func:`split_filter_values` and every ``status`` pair is read, not just the
    first — ``?status=completed,open`` and a two-box form submit are both boards
    that DO admit open rows, and reading either as "completed" would drop a
    reopened row from a board it still belongs to.
    """
    statuses = [
        status
        for key, value in _request_filters(request).params
        if key == "status"
        for status in split_filter_values(value)
    ]
    return not statuses or "open" in statuses


def task_tag_url(request: Request, tag: str) -> str:
    params = _preserved_filter_params(request, exclude="tag")
    params.append(("tag", tag))
    return f"/tasks?{urlencode(params)}"


def task_tag_clear_url(request: Request, tags: Sequence[str], tag: str) -> str:
    """Link an active-filter chip to the same board WITHOUT that one tag.

    The chip is the only place the ``?tag=`` scope is named: a cross-project
    tag (the monthly-roadmap convention) belongs to no project, so nothing else
    on the board says what the slice is — the row chips name each row's own
    tags, not the filter. Clearing one chip keeps the other tags rather than
    dropping the whole filter.

    ``tags`` is the HONOURED list (``TaskFilters.tags``) and NOT the raw query
    string: the two spellings (repeated and comma-joined) both fold into it, so
    rebuilding from the request would emit a tag twice or miss a duplicate it
    had collapsed. It also keeps the link truthful — clearing one chip cannot
    resurrect a tag the board is not filtering by.

    One repeated ``tag`` pair per remaining tag — the same and only spelling the
    request itself uses, now that tags are literal. That is what keeps the link
    inside ``MAX_FILTER_QUERY_BYTES``, which matters because a chip that renders
    but 400s when clicked is worse than no chip: encoding the output exactly as
    the input was encoded makes this link a strict subset of the request that
    was already accepted, so it cannot overflow a budget the request cleared.

    Round 6 comma-joined them to solve that budget problem, which worked only
    because tags were being split on commas — the very thing that made the real
    tag ``customer,2`` unnameable. With literal tags the two are the same
    representation and the budget property falls out for free.
    """
    params = _preserved_filter_params(request, exclude="tag")
    params.extend(("tag", other) for other in tags if other != tag)
    return f"/tasks?{urlencode(params)}" if params else "/tasks"


def task_detail_url(request: Request, task_id: str) -> str:
    """Link a task id as ONE path segment, with every reserved character encoded.

    ``normalize_task`` keeps ids as arbitrary non-empty strings — nothing
    upstream excludes URL-reserved characters — so ``quote``'s default
    ``safe="/"`` is the wrong default here: an id containing ``?`` or ``#``
    would truncate the path and route somewhere else, and one containing ``/``
    would invent a segment. ``safe=""`` matches the encoding
    ``knowledge_produced_by.ProducedByChip.url`` already uses for the same
    route, so the two agree on what a task link looks like.
    """
    params = _preserved_filter_params(request)
    suffix = f"?{urlencode(params)}" if params else ""
    return f"/tasks/{quote(task_id, safe='')}{suffix}"


def note_url(knowledge_id: str, task_id: str = "") -> str:
    """Link a finding's document, id-encoded, carrying the task back-link.

    Findings' ``knowledge_id`` and ``task_id`` are free strings off an
    agent-written payload (``tests/contracts/lithos_finding_list.json``), so
    neither may be interpolated into a URL raw: Jinja's autoescaping makes the
    attribute safe to EMBED but says nothing about what the URL then addresses.
    """
    task_suffix = f"?{urlencode({'task': task_id})}" if task_id else ""
    return f"/note/{quote(knowledge_id, safe='')}{task_suffix}"


def epic_scope_url(request: Request, epic_id: str) -> str:
    """Link an epic chip to the dashboard scoped to that epic — or unscoped.

    An empty ``epic_id`` clears the scope, which is what the SELECTED chip
    links to: clicking the active epic toggles its scope back off. Only one
    epic scopes the board at a time, so the incoming ``epic`` param is replaced
    rather than appended.
    """
    params = _preserved_filter_params(request, exclude="epic")
    if epic_id:
        params.append(("epic", epic_id))
    return f"/tasks?{urlencode(params)}" if params else "/tasks"


def task_card_url(request: Request, status: str, since: str, anchor: str = "") -> str:
    """Link a summary card to the board it actually counts.

    The card's number is computed over the ACTIVE filters, so the link has to
    carry them: project/tag/agent — and the epic scope — ride along (rebuilt
    from the request through the same allowlist as every other generated tasks
    URL), the card supplies
    the status it counts, and ``since`` is the resolved window this page is
    showing rather than whatever the request did or did not say. Dropping the
    filters made the card a lie by one click: the count described the filtered
    board, the destination showed the unfiltered one.
    """
    params = [
        (key, value)
        for key, value in _preserved_filter_params(request)
        if key not in {"status", "since"}
    ]
    params.append(("status", status))
    params.append(("since", since))
    return f"/tasks?{urlencode(params)}{anchor}"


def tasks_url(request: Request) -> str:
    params = _preserved_filter_params(request)
    return f"/tasks?{urlencode(params)}" if params else "/tasks"


def knowledge_tag_url(tag: str) -> str:
    """Link a note-page tag chip to the ``/knowledge`` list filtered by it (§6.4)."""
    return f"/knowledge?{urlencode({'tag': tag})}"


def tag_chip_class(tag: str) -> str:
    classes = ["tag-chip"]
    if tag.startswith("project:"):
        classes.append("tag-chip-project")
    return " ".join(classes)
