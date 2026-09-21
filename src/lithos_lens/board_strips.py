"""The board's chrome strips, both derived from ONE generation of the reads.

Above the sections the dashboard draws two strips of chips — the epic rollups
(``?epic=``, §5.2.1) and the project quick-switch strip (``?project=``, §5.3) —
and they answer the same shape of question about the board below them: *which
of these is there work in, and where does clicking take me?* That shared
subject is this module.

Both obey one rule, which is why they are assembled together rather than in
``frontier``: **a chip must lead to a non-empty board**. A chip links to the
ACTIVE filters plus its own term, so it can only be drawn when the rows it
would leave are rows this board actually renders — the same filter predicate
(``matches_filters``) and the same read generation the sections are built
from. The skew retry adopts a NEW generation, and both strips have to move
with it; ``load_board_strips`` is the single call the assembly makes per
generation, so they cannot drift apart or be half-updated.

They differ in what they cost and in what the term means:

- the epic strip needs a read per open epic (``epic_strip``, which owns that
  fan-out and its resource bound). Its chips scope the board to a subtree, so
  the selected epic's descendant ids come back as ``scope_ids`` — the epic
  scope every section, and the project strip, is then evaluated under;
- the project strip needs no read at all. The projects in a scope are already
  in the snapshot, under §5B.1's two conventions, which is the whole reason
  this strip exists: an operator moving between the projects of a roadmap tag
  was retyping a filter whose values Lens could have offered.

The seam is the one ``docs/architecture.toml`` asked the next change to
``frontier.py`` to find: the assembly keeps the join, the sections, the
counters and the degraded-mode policy; the chrome that describes them lives
here.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import replace
from typing import NamedTuple

from lithos_lens.epic_strip import (
    EpicChildrenClient,
    EpicStrip,
    epic_scope_ids,
    load_epic_rollups,
)
from lithos_lens.task_filtering import (
    board_visible_ids,
    matches_filters,
    task_projects,
)
from lithos_lens.tasks import TaskFilters, TaskRecord


class ProjectChip(NamedTuple):
    """One project in the quick-switch strip (§5.3).

    ``open_count`` is how many OPEN rows of this project the strip's scope
    holds — a count, not a cap: it says how much work moving there would show,
    and it is what the chips are ordered by. ``selected`` marks a slug the
    ``?project=`` filter currently holds, so the chip renders as the live
    filter and links to removing just that one slug.
    """

    slug: str
    open_count: int
    selected: bool = False


class BoardStrips(NamedTuple):
    """Both strips of one generation, plus the epic scope the first resolved.

    ``scope_ids`` is the selected epic's descendant ids — ``None`` when nothing
    is scoped, an EMPTY set when a confirmed-childless epic really does scope
    the board to nothing (see ``epic_strip._resolve_empty_selection``). It is
    returned rather than re-derived by the caller because the project strip is
    counted UNDER it: the two strips compose, so the projects on offer are the
    projects of the epic the operator is inside.
    """

    epics: EpicStrip
    scope_ids: frozenset[str] | None
    projects: tuple[ProjectChip, ...]


async def load_board_strips(
    lithos: EpicChildrenClient,
    snapshot: Sequence[TaskRecord],
    closed_results: Sequence[Sequence[TaskRecord] | BaseException],
    *,
    filters: TaskFilters,
    open_row_types: Collection[str] | None,
) -> BoardStrips:
    """Assemble both strips from the reads currently in hand.

    Called once per generation: the skew retry rebinds the snapshot and the
    terminal reads, and a strip built against the generation before it would
    describe a board that is no longer on screen — chips for an epic whose only
    row has gone, a project count nothing adds up to.

    ``open_row_types`` is the set of open task types that render as ROWS
    (``None`` on the flat fallback, where every open row does). A type that
    rolls up is not a row either strip may rest a chip on.
    """
    epics = await load_epic_rollups(
        lithos,
        snapshot,
        selected=filters.epic,
        visible_ids=board_visible_ids(
            snapshot,
            closed_results,
            filters=filters,
            open_row_types=open_row_types,
        ),
    )
    scope_ids = epic_scope_ids(epics.rollups)
    return BoardStrips(
        epics=epics,
        scope_ids=scope_ids,
        projects=build_project_strip(
            snapshot,
            filters=filters,
            scope_ids=scope_ids,
            open_row_types=open_row_types,
        ),
    )


def build_project_strip(
    snapshot: Sequence[TaskRecord],
    *,
    filters: TaskFilters,
    scope_ids: frozenset[str] | None,
    open_row_types: Collection[str] | None,
) -> tuple[ProjectChip, ...]:
    """The projects inside the current scope, with their open-row counts (§5.3).

    The scope is **every active filter except ``project``**. That is the whole
    point of the strip: it answers "which projects are in what I am looking
    at", so clicking between them must not shrink it — the other projects have
    to stay one click away — and it still recomputes as the OTHER filters
    change. Selecting a project marks its chip rather than removing the rest.

    Counted over the OPEN rows this board renders — the open sections and the
    Gates section, under the same predicate and generation as the sections
    themselves. Terminal rows contribute nothing: a project whose only rows are
    resolved holds no open work to switch to, and a count mixing the two would
    not match any section on the page. With the open side switched off
    (``?status=completed``) there are no such rows at all and the strip is
    empty; the template still shows the clear affordance while a filter is
    active.

    Slugs come from :func:`~lithos_lens.task_filtering.task_projects` under
    ``convention="both"`` — the one enumeration, and the same call
    ``project_universe`` (the Project datalist) and ``graph_page.
    observed_projects`` (the scope picker) make. That is §5B.1's universe rule,
    the union of both conventions "so no project is invisible to its own view":
    a task carrying ``metadata.project`` alone (loom's issue-mirrored work)
    counts exactly like a tagged one.

    The universe and what ``?project=<slug>`` MATCHES are the same reading of a
    row under the default ``project_convention = "both"``, which is where the
    no-dead-end rule at the top of this module is proved. They come apart under
    a single-convention posture — ``matches_projects`` honours only the
    configured one — and that gap is §5B.1's own, not this strip's: the Project
    datalist beside it has offered unreachable values in exactly that case
    since the universe rule was written (pinned by
    ``test_project_universe_unions_both_conventions_under_a_single_posture``).
    Closing it means making the FILTER read the universe too, which is a change
    to §5B.1's normative matching rule and to every surface that filters — not
    something a strip may decide on its own — it is Lithos task ``f990395d``,
    which widens the filter to both conventions and retires the knob. Until
    then this strip states the same universe the datalist does, so the two
    controls never disagree about which projects exist, and
    ``test_a_single_convention_posture_inherits_the_5b1_universe_gap`` keeps
    the residual visible instead of silent.

    Ordered by count descending, then slug — the strip reads as a summary of
    where the work is, and its order is stable while the operator clicks
    between projects (the counts do not move, because the project filter is not
    part of the scope).
    """
    if "open" not in filters.statuses:
        return ()
    scope = replace(filters, projects=())
    counts: Counter[str] = Counter()
    for task in snapshot:
        if open_row_types is not None and task.task_type not in open_row_types:
            continue
        if not matches_filters(task, filters=scope, status="open", scope_ids=scope_ids):
            continue
        counts.update(
            task_projects(task, convention="both", tag_key=scope.project_tag_key)
        )
    return tuple(
        ProjectChip(slug=slug, open_count=count, selected=slug in filters.projects)
        for slug, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )
