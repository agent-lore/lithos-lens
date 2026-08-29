"""Client-side task filtering: the project conventions and match predicates.

Split out of ``tasks.py`` when the combined T1 filter slices pushed that module
past the 800-line god-module ceiling (``docs/architecture.toml`` budgets). The
seam is the natural one: ``tasks.py`` owns the domain records and normalizers,
while every predicate that decides whether a row survives the dashboard's
filters lives here. The dependency runs one way — this module imports the
records, ``tasks.py`` never imports back — so the pair stays acyclic.

Project resolution (§5B.1) sits here too: a task's project is a *derived*
property read from either ``metadata.project`` or a ``project:<slug>`` tag
depending on the configured convention, which is filtering input rather than
a stored field — as do the two per-load reporting passes built on it
(``project_universe`` for the filter dropdown, ``log_project_data_quality``
for the convention-conflict warnings).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from lithos_lens.tasks import (
    DEFAULT_PROJECT_CONVENTION,
    DEFAULT_PROJECT_TAG_KEY,
    TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    ProjectConvention,
    TaskFilters,
    TaskRecord,
    TaskStatusName,
    parse_date,
)

logger = logging.getLogger(__name__)


def _metadata_project_slug(task: TaskRecord) -> str:
    """The task's ``metadata.project`` slug — ``""`` when absent or unusable.

    ``metadata`` is free-form upstream JSON, but §5B.1 defines this key as a
    string slug. A non-string value is NOT coerced: ``str(["influx"])`` would
    fabricate a project named ``['influx']`` that reaches the filter dropdown,
    matches a URL nobody could otherwise produce, and can fake a convention
    conflict. Such a value is ignored here and reported by the loader
    (``lens.tasks.project_metadata_invalid``) instead.
    """
    value = task.metadata.get("project")
    return value.strip() if isinstance(value, str) else ""


def invalid_project_metadata(task: TaskRecord) -> bool:
    """True when ``metadata.project`` is present but is not a string (§5B.1).

    Explicit ``None`` and blank strings mean "no project" and are not flagged;
    a list/mapping/number/bool is malformed data whose project is unreadable,
    so the task is invisible to its project view and someone should know.
    """
    value = task.metadata.get("project")
    return value is not None and not isinstance(value, str)


def task_projects(
    task: TaskRecord,
    *,
    convention: ProjectConvention = DEFAULT_PROJECT_CONVENTION,
    tag_key: str = DEFAULT_PROJECT_TAG_KEY,
) -> tuple[str, ...]:
    """Every project slug a task claims under ``convention`` (§5B.1).

    Both live conventions are read under ``"both"`` — ``metadata.project``
    first, then each ``<tag_key>:<slug>`` tag — and the result is the UNION, so
    a task tagged for one project and stamped with another is visible in both
    project views rather than invisible to one. (Display precedence, where the
    metadata value wins the single row chip, is a row-anatomy concern; here
    membership is what matters.) Order is stable — metadata slug first — and
    duplicates collapse.
    """
    slugs: list[str] = []
    if convention in ("metadata", "both"):
        metadata_slug = _metadata_project_slug(task)
        if metadata_slug:
            slugs.append(metadata_slug)
    if convention in ("tag", "both"):
        prefix = f"{tag_key}:"
        for tag in task.tags:
            if not tag.startswith(prefix):
                continue
            tag_slug = tag[len(prefix) :].strip()
            if tag_slug and tag_slug not in slugs:
                slugs.append(tag_slug)
    return tuple(slugs)


def project_convention_conflict(
    task: TaskRecord,
    *,
    tag_key: str = DEFAULT_PROJECT_TAG_KEY,
) -> bool:
    """True when a task carries BOTH conventions and they disagree (§5B.1).

    Neither value is dropped — the task stays visible under both slugs — but
    the disagreement is a data-quality signal the loader reports to telemetry
    (``lens.tasks.project_convention_conflict``).
    """
    metadata_slugs = task_projects(task, convention="metadata", tag_key=tag_key)
    tag_slugs = task_projects(task, convention="tag", tag_key=tag_key)
    if not metadata_slugs or not tag_slugs:
        return False
    return metadata_slugs[0] not in tag_slugs


def matches_agent(task: TaskRecord, agent: str) -> bool:
    """Creator-OR-claimer agent match (§5.4.2).

    "Everything agent-zero is involved in" is one filter: the row matches when
    the agent created the task or holds one of its inline claims. Claims are
    only inline on the master open list; a row whose claims were not returned
    (``claims is None``) can only match on creator — Lens does not guess.
    """
    if task.created_by == agent:
        return True
    return any(claim.agent == agent for claim in task.claims or ())


def matches_projects(task: TaskRecord, filters: TaskFilters) -> bool:
    """Multi-select project match: does the task belong to ANY selected project?

    An empty selection matches everything; otherwise the task's slugs under the
    active convention are intersected with the selection (§5.4.2).
    """
    if not filters.projects:
        return True
    slugs = task_projects(
        task,
        convention=filters.project_convention,
        tag_key=filters.project_tag_key,
    )
    return any(slug in filters.projects for slug in slugs)


def matches_filters(
    task: TaskRecord,
    *,
    filters: TaskFilters,
    status: TaskStatusName,
    scope_ids: frozenset[str] | None = None,
) -> bool:
    """Client-side filter predicate shared by the dashboard sections.

    Public because the frontier join (``frontier.py``) re-applies it over the
    joined snapshot; the guardrail forbids reaching for another module's
    privates.

    ``scope_ids`` is the resolved ``?epic=`` scope — the selected epic's
    descendant ids. ``None`` means "no epic scope"; an EMPTY set is a real
    scope (a confirmed childless epic) and correctly hides everything. The
    unconfirmable case — an epic that may have closed since the open read —
    resolves to ``None``, not to an empty set (see ``frontier``).
    """
    if scope_ids is not None and task.id not in scope_ids:
        return False
    if task.status != status:
        return False
    if filters.agent and not matches_agent(task, filters.agent):
        return False
    if not matches_projects(task, filters):
        return False
    if filters.tags and not all(tag in task.tags for tag in filters.tags):
        return False
    if status in TERMINAL_TASK_STATUSES and filters.since:
        # Terminal rows are windowed by RESOLUTION time (``resolved_since``
        # upstream), not creation time — a task created months ago and finished
        # yesterday is recent work. A row whose ``resolved_at`` is missing or
        # unparseable is kept: upstream already excluded NULL-resolved rows
        # from the window, so re-deriving the exclusion here would only hide
        # rows the server deliberately returned.
        resolved_date = parse_date(task.resolved_at)
        since_date = parse_date(filters.since)
        if (
            resolved_date is not None
            and since_date is not None
            and resolved_date < since_date
        ):
            return False
    return True


def filters_narrow_the_open_side(
    filters: TaskFilters, *, scope_applied: bool = False
) -> bool:
    """True when these filters hide OPEN rows from the sections.

    The one list every open-side filter must join — tag, agent, project, and
    the applied ``?epic=`` scope — plus the one status case that matters:
    dropping ``open`` from the status set takes the whole open side off screen,
    so a degraded row there (claims unknown, say) is hidden rather than absent.

    Narrowing to ``?status=open`` is deliberately NOT narrowing here: it hides
    only terminal sections, and no claim about the open board rests on those.
    That asymmetry is the whole reason this is separate from
    :func:`filters_narrow_the_board`, which adds any status subset for the
    claims that cover the whole page (the empty-corpus panel).
    """
    return (
        scope_applied
        or bool(filters.tags)
        or bool(filters.agent)
        or bool(filters.projects)
        or "open" not in filters.statuses
    )


def filters_narrow_the_board(
    filters: TaskFilters, *, scope_applied: bool = False
) -> bool:
    """True when these filters hide part of the corpus from the sections.

    The whole-system claims on the dashboard (the healthy stripe, the empty
    corpus panel) are only sound on an unnarrowed board, because both the
    section partition and the terminal reads are filtered by agent/tag/status.

    ``project`` narrows like tag/agent: T1-S9 filters the sections down to one
    project's rows, and a shared ``?project=`` link must not let a slice of the
    board make the stripe's system-wide claim.

    ``scope_applied`` is the ``?epic=`` scope, and it is passed rather than
    read off ``filters.epic`` because the two differ: a REQUESTED epic that
    could not be resolved (closed since the open read, or a failed children
    read) leaves the board showing everything under a "scope not applied"
    banner, which is not narrowed. Only a scope that actually filtered the
    sections — ``frontier``'s ``scope_ids is not None``, including the empty
    set of a confirmed childless epic — hides part of the corpus.

    ``since`` is deliberately not narrowing here: it windows only the resolved
    completed/cancelled reads, which the empty-corpus copy names explicitly,
    and the open reads every degraded signal derives from ignore it.
    """
    return filters_narrow_the_open_side(filters, scope_applied=scope_applied) or set(
        filters.statuses
    ) != set(TASK_STATUSES)


def loaded_task_rows(
    open_snapshot: Sequence[TaskRecord],
    closed_groups: Iterable[Sequence[TaskRecord]],
) -> tuple[TaskRecord, ...]:
    """Every task row this load fetched, deduped by id (open snapshot wins).

    Read skew can return the same id in both the open snapshot and a terminal
    window; the open snapshot is the authority on the row, and dedup keeps
    per-load reporting counted once.
    """
    rows: list[TaskRecord] = list(open_snapshot)
    seen = {task.id for task in rows}
    for group in closed_groups:
        for task in group:
            if task.id not in seen:
                seen.add(task.id)
                rows.append(task)
    return tuple(rows)


def project_universe(
    tasks: Sequence[TaskRecord],
    filters: TaskFilters,
) -> tuple[str, ...]:
    """Every project slug present in the loaded rows, sorted (§5B.1).

    The universe is the union of BOTH conventions' slugs regardless of the
    active posture — §5B.1 is explicit that no project may be invisible to its
    own view — even though matching under a single-convention posture honours
    only that convention. Only the tag KEY follows configuration (§5B.9).
    """
    slugs: set[str] = set()
    for task in tasks:
        slugs.update(
            task_projects(task, convention="both", tag_key=filters.project_tag_key)
        )
    return tuple(sorted(slugs))


def log_project_data_quality(
    tasks: Sequence[TaskRecord],
    filters: TaskFilters,
) -> None:
    """Report this load's project data-quality signals, once each (§5B.1).

    Two independent signals over every loaded row — resolved rows carry their
    conventions too:

    - the two conventions are present and DISAGREE. Reported in every posture:
      §5B.1 makes the warning a property of the data, not of the matching
      posture Lens happens to run (both values are read for the universe
      regardless). Neither value is dropped — the task matches under both slugs.
    - ``metadata.project`` is present but is not a string. Lens cannot read a
      project out of it, so the task is invisible to its project view; the
      value is ignored rather than coerced into a fabricated slug.
    """
    conflicts: list[str] = []
    malformed: list[str] = []
    for task in tasks:
        if project_convention_conflict(task, tag_key=filters.project_tag_key):
            conflicts.append(task.id)
        if invalid_project_metadata(task):
            malformed.append(task.id)
    if conflicts:
        logger.warning(
            "task project conventions disagree",
            extra={
                "lens_event": "lens.tasks.project_convention_conflict",
                "conflict_count": len(conflicts),
                "conflicting_task_ids": conflicts[:20],
            },
        )
    if malformed:
        logger.warning(
            "task metadata.project is not a string slug",
            extra={
                "lens_event": "lens.tasks.project_metadata_invalid",
                "invalid_count": len(malformed),
                "invalid_task_ids": malformed[:20],
            },
        )
