"""What the filter bar OFFERS: the Project, Tag and Agent option universes.

The three datalists on the dashboard's filter bar answer one question — "what
can I narrow to from here?" — and they all answer it the same way: over the
rows THIS load fetched (the open snapshot plus both resolved windows, deduped
by id) and BEFORE the filters narrow anything. That rule is what makes them
discovery surfaces rather than a restatement of the current view: selecting one
project must not collapse the list of projects you can switch to, and a tag
carried only by another project's rows — ``milestone:t2``, ``needs-human`` —
has to be offerable from the board that is hiding them.

Each universe's builder lives with the vocabulary it belongs to
(``task_filtering.project_universe`` / ``tag_universe``,
``agent_picker.agent_options``); what lives here is the one composition over
them, so the shared invariant is stated and applied in ONE place instead of
being restated by each caller. It also costs no read of its own: the option
universes are derived from responses ``load_dashboard`` already has, never from
a fan-out of their own.

``frontier.load_dashboard`` is the only caller.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from lithos_lens.agent_picker import AgentOption, agent_options
from lithos_lens.task_filtering import (
    loaded_task_rows,
    log_project_data_quality,
    project_universe,
    tag_universe,
)
from lithos_lens.tasks import AgentRecord, TaskFilters, TaskRecord

__all__ = ["FilterOptions", "build_filter_options"]


@dataclass(frozen=True)
class FilterOptions:
    """The filter bar's three option universes, built from one load's rows.

    Carried as one value because they share one derivation and one staleness —
    a board whose Project list came from the unfiltered reads and whose Tag
    list came from the filtered ones would be telling the operator two
    different stories about the same snapshot.
    """

    projects: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    # Every registration Lithos knows, ordered most-recently-active first, each
    # carrying its own window verdict; the template decides how much to render.
    agents: tuple[AgentOption, ...] = ()


def build_filter_options(
    open_snapshot: Sequence[TaskRecord],
    closed_results: Sequence[list[TaskRecord] | BaseException],
    agents_result: list[AgentRecord] | BaseException,
    filters: TaskFilters,
    errors: list[str],
    *,
    agent_inactive_days: int,
    now: datetime,
) -> FilterOptions:
    """Build the option universes, and log this load's project data quality.

    ``closed_results`` is taken in its raw settled form: a terminal window that
    FAILED contributes no rows rather than emptying the universes — the
    projects and tags the open snapshot carries are still perfectly good
    answers, and the failed read has already been reported by the caller.

    The data-quality pass rides along because it asks the same question of the
    same rows (every project convention the load saw, conflicting or
    unreadable), and running it anywhere else would mean deriving that row set
    a second time.
    """
    loaded_tasks = loaded_task_rows(
        open_snapshot,
        [
            cast("list[TaskRecord]", result)
            for result in closed_results
            if not isinstance(result, BaseException)
        ],
    )
    log_project_data_quality(loaded_tasks, filters)
    return FilterOptions(
        projects=project_universe(loaded_tasks, filters),
        tags=tag_universe(loaded_tasks),
        agents=agent_options(
            agents_result,
            loaded_tasks,
            errors,
            days=agent_inactive_days,
            now=now,
        ),
    )
