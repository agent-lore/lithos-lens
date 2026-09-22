"""Client-side task filtering: the project conventions and match predicates.

Split out of ``tasks.py`` when the combined T1 filter slices pushed that module
past the 800-line god-module ceiling (``docs/architecture.toml`` budgets). The
seam is the natural one: ``tasks.py`` owns the domain records and normalizers,
while every predicate that decides whether a row survives the dashboard's
filters lives here. The dependency runs one way — this module imports the
records, ``tasks.py`` never imports back — so the pair stays acyclic.

Project resolution (§5B.1) sits here too: a task's project is a *derived*
property read from ``metadata.project`` and/or a ``project:<slug>`` tag rather
than a stored field — as do the display reading a row's chip is drawn from
(``row_project_chips``) and the two per-load reporting passes built on it
(``project_universe`` for the filter dropdown, ``log_project_data_quality``
for the convention-conflict warnings). ``tag_universe`` sits beside the first
of them: a different vocabulary, built over the same loaded rows.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Sequence

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
    parse_timestamp,
)

logger = logging.getLogger(__name__)

# The terminal windows the dashboard reads, in the order it gathers them — the
# pairing :func:`board_visible_ids` reads its ``closed_results`` with.
TERMINAL_STATUS_READS: tuple[TaskStatusName, ...] = ("completed", "cancelled")


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


def row_project_chips(
    task: TaskRecord,
    *,
    tag_key: str = DEFAULT_PROJECT_TAG_KEY,
) -> tuple[str, ...]:
    """The projects a board ROW must chip for itself, in §5B.1 order (§5.4.1).

    A row states its project, and §5B.1 says which value that is where a single
    one is needed: ``metadata.project`` when present, else the
    ``<tag_key>:<slug>`` tag. A row's tag strip already names the tag half — a
    ``project:<slug>`` tag renders as the project-styled chip it is, linking to
    that tag's board — so what it cannot name, and what this returns, is every
    slug the row claims that no tag of its own spells out.

    In practice that is the metadata convention, and it is the whole of the
    defect this closes: a row carrying ``metadata.project`` alone (loom's
    issue-mirrored work, which ``?project=`` has matched since the membership
    knob was retired) rendered NO project chip, and a row whose conventions
    disagree rendered only the losing tag value. The slugs come back in
    :func:`task_projects`' ``"both"`` order — metadata first — so the chip a
    conflicting row leads with is the metadata one the rest of Lens resolves
    to, with the tag chip still beside it: §5B.1 drops neither value, it
    orders them.

    A row whose conventions AGREE is chipped once, by its tag, rather than
    twice with the same slug; a tag-only row is untouched. Read through
    :func:`task_projects` rather than off ``metadata`` directly, so the row,
    the side panel's chip, the quick-switch strip and what ``?project=``
    matches are one reading of a task — a row cannot name a project the filter
    would refuse, nor stay silent about one it would match.
    """
    tagged = task_projects(task, convention="tag", tag_key=tag_key)
    return tuple(
        slug
        for slug in task_projects(task, convention="both", tag_key=tag_key)
        if slug not in tagged
    )


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

    An empty selection matches everything; otherwise the task's slugs under
    BOTH conventions are intersected with the selection (§5.4.2). Membership
    reads the same universe every control that OFFERS a project enumerates —
    the Project datalist, the graph scope picker, the quick-switch strip — so
    no control can hand the operator a slug this filter then refuses.
    ``[tasks].project_convention`` used to select which convention was
    honoured here; that made every offering control a source of dead-end
    values under a single-convention posture, so the knob is parsed and
    ignored (§4.4) and matching is unconditionally ``"both"``.
    """
    if not filters.projects:
        return True
    slugs = task_projects(task, convention="both", tag_key=filters.project_tag_key)
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
    if filters.created_since:
        # The CREATED window (§5.4), and the one date filter that narrows
        # EVERY section: "what came in since Monday" is a question about
        # intake, so an open row is as much of an answer as a resolved one.
        # Applied here over the loaded snapshot rather than pushed upstream —
        # the rows are already in hand, and pushing it would change what the
        # terminal reads fetch, which is ``since``'s job alone.
        #
        # A row whose ``created_at`` cannot be read is DROPPED, which is the
        # opposite of the resolved branch below and deliberately so. That
        # branch keeps an unreadable row because the SERVER already applied the
        # window (``resolved_since``) and deliberately returned the row, so
        # re-deriving the exclusion here could only hide rows upstream had
        # already admitted. This window is pushed nowhere: no read has filtered
        # on ``created_at``, so this predicate is the only thing standing
        # between the operator and a row that has not been shown to satisfy
        # ``created_at >= date``. Keeping it would put a row on a narrowed
        # board without evidence of membership, and the section counts would
        # count it (round-1 correctness/f-001). ``normalize_task`` admits the
        # state — a missing ``created_at`` normalizes to ``""`` — so it is
        # reachable from real data rather than hypothetical.
        #
        # The ROW's stamp is read with ``parse_timestamp``, which parses the
        # WHOLE value and normalizes it to UTC, rather than with ``parse_date``
        # — that helper reads ``value[:10]`` and is right for the query-string
        # dates it was written for, but wrong for an upstream timestamp on both
        # counts (round-2 correctness/f-001):
        #
        # - a ten-character prefix cannot tell a timestamp from junk wearing
        #   one, so ``2026-09-17junk`` and ``2026-09-17T99:99:99`` both read as
        #   a valid 17 September and slipped past the drop above;
        # - a prefix is the date in the stamp's OWN offset, not in UTC, so
        #   ``2026-09-16T23:30:00-05:00`` (17 September in UTC) fell out of a
        #   17 September window and ``2026-09-17T00:30:00+05:00`` (16
        #   September in UTC) fell into it. Everything else in Lens compares
        #   instants in UTC (``parse_timestamp``'s own contract, which the
        #   age-based attention rules rest on), so this window has to as well.
        #
        # ``filters.created_since`` keeps ``parse_date``: it is a bare ISO date
        # by construction (``normalize_created_since_input``), which is exactly
        # what that helper reads.
        created_at = parse_timestamp(task.created_at)
        created_since_date = parse_date(filters.created_since)
        if created_since_date is not None and (
            created_at is None or created_at.date() < created_since_date
        ):
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

    The one list every open-side filter must join — tag, agent, project, the
    ``created_since`` window (§5.4: it windows open rows too, unlike ``since``)
    and the applied ``?epic=`` scope — plus the one status case that matters:
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
        or bool(filters.created_since)
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
    ``created_since`` is the opposite case and DOES narrow (via
    :func:`filters_narrow_the_open_side`) — it hides open rows, so a board
    carrying one cannot make the whole-system claim.
    """
    return filters_narrow_the_open_side(filters, scope_applied=scope_applied) or set(
        filters.statuses
    ) != set(TASK_STATUSES)


def unread_displayed_statuses(
    closed_results: Sequence[Sequence[TaskRecord] | BaseException],
    *,
    filters: TaskFilters,
) -> frozenset[str]:
    """The statuses the board DISPLAYS whose read did not answer.

    The one uncertainty that makes row membership unknowable: a window this
    board is showing did not answer, so a row that belongs on it may exist and
    Lens never saw it. Every claim of the form "nothing here matches your
    filters" has to stand down on that — and on that alone. A failed stats or
    agent-list read, or another epic's children read, says nothing about which
    rows this board holds, which is why those are NOT read from the aggregate
    error list.

    Returned per STATUS rather than as one flag because the two consumers ask
    at different grains: the epic-scope explanations are about the board as a
    whole (any entry withholds them), while the section that renders empty must
    say "could not be loaded" for the window that failed and keep saying "no
    match" for the windows that answered — suppressing both would hide rows
    Lens does have.

    Takes the gather results verbatim (``list | BaseException``, the shape
    ``frontier_fallback.resolve_frontier`` also accepts), paired with
    :data:`TERMINAL_STATUS_READS`. The open read is deliberately not part of
    it: without the open snapshot there are no epics to explain, so nothing
    downstream can make the claim in the first place.
    """
    return frozenset(
        status
        for status, result in zip(TERMINAL_STATUS_READS, closed_results, strict=True)
        if status in filters.statuses and isinstance(result, BaseException)
    )


def board_visible_ids(
    open_snapshot: Sequence[TaskRecord],
    closed_results: Sequence[Sequence[TaskRecord] | BaseException],
    *,
    filters: TaskFilters,
    open_row_types: Collection[str] | None,
) -> frozenset[str] | None:
    """The ids a board RENDERS under its filters — ``None`` when unscoped.

    The epic strip's scope (§5.2.1). Every chip links to the CURRENT filters
    plus ``?epic=<id>``, so a chip is only worth drawing when the epic has a
    descendant among these ids — which is precisely the set of rows that
    survives the filters the rest of the page applies. Built from the same
    reads and the same predicate the sections use, one status at a time, so
    the strip and the board cannot disagree about what is on screen:

    - only rows that can be PLACED count. ``open_row_types`` is the open task
      types that render as rows (``None`` on the flat fallback, where every
      open row does): an epic or any other rolled-up type is NOT a row this
      board shows, so a chip resting on one would be the dead end the rule
      exists to remove. Terminal rows have no such test — every resolved row
      renders in its section, epics included;
    - only the statuses actually shown contribute (``?status=completed`` hides
      the open sections, so an open row cannot make a chip non-empty there);
    - terminal rows carry the same open-snapshot dedup the sections apply, so
      a row read skew returned twice is counted where it renders;
    - the ``?epic=`` scope itself is deliberately NOT applied (``scope_ids`` is
      ``None``): the strip must stay the same whichever chip is selected, or
      selecting one epic would erase the others and strand the operator inside
      it.

    ``None`` is returned in the two cases where there is nothing to say:

    - an UNNARROWED board — nothing is filtered out of view, so there is no
      scope to apply and the caller keeps its whole set. Narrowing is
      :func:`filters_narrow_the_board`'s definition, shared with the empty-
      state and healthy-stripe claims: ``since`` windows the resolved reads
      (the dashboard's normal posture) and does not narrow;
    - a displayed status whose read FAILED. Its rows are unknown, not absent,
      and every claim built on this set — "this epic has no tasks on this
      board", "nothing under this epic matches these filters" — would state a
      filter result Lens cannot know. The unfiltered strip plus the load-error
      banner is the honest degraded answer.
    """
    if not filters_narrow_the_board(filters, scope_applied=False):
        return None
    if unread_displayed_statuses(closed_results, filters=filters):
        return None
    visible: set[str] = set()
    if "open" in filters.statuses:
        visible.update(
            task.id
            for task in open_snapshot
            if (open_row_types is None or task.task_type in open_row_types)
            and matches_filters(task, filters=filters, status="open", scope_ids=None)
        )
    open_ids = {task.id for task in open_snapshot}
    for status, result in zip(TERMINAL_STATUS_READS, closed_results, strict=True):
        if status not in filters.statuses or isinstance(result, BaseException):
            continue
        visible.update(
            task.id
            for task in result
            if task.id not in open_ids
            and matches_filters(task, filters=filters, status=status, scope_ids=None)
        )
    return frozenset(visible)


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

    The universe is the union of BOTH conventions' slugs — §5B.1 is explicit
    that no project may be invisible to its own view — and so, since
    ``project_convention`` was retired as a membership knob, is
    :func:`matches_projects`: what this offers is exactly what the filter
    matches. Only the tag KEY follows configuration (§5B.9).
    """
    slugs: set[str] = set()
    for task in tasks:
        slugs.update(
            task_projects(task, convention="both", tag_key=filters.project_tag_key)
        )
    return tuple(sorted(slugs))


def tag_universe(tasks: Sequence[TaskRecord]) -> tuple[str, ...]:
    """Every tag present in the loaded rows, sorted (§5.4).

    Built exactly like :func:`project_universe`, and for the same reason: the
    scope of a view is often a tag that spans projects (``milestone:t2``,
    ``needs-human``), and a Tag box that offers nothing can only be typed into
    by someone who already knows the vocabulary. Over the LOADED rows and
    before the filters narrow, so a tag carried only by another project's rows
    — or only by a resolved-window row — is still discoverable from the board
    that is hiding them.

    Raw tag strings, deduped and sorted, with no normalisation: one ``tag``
    parameter is one literal tag, so the box must offer exactly what it would
    submit.
    """
    tags: set[str] = set()
    for task in tasks:
        tags.update(task.tags)
    return tuple(sorted(tags))


def log_project_data_quality(
    tasks: Sequence[TaskRecord],
    filters: TaskFilters,
) -> None:
    """Report this load's project data-quality signals, once each (§5B.1).

    Two independent signals over every loaded row — resolved rows carry their
    conventions too:

    - the two conventions are present and DISAGREE. §5B.1 makes the warning a
      property of the DATA — the disagreement is real however Lens reads it —
      and neither value is dropped: the task matches under both slugs.
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
