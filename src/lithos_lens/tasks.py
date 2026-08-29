"""Tasks dashboard domain records, view models, and their normalizers.

The two surfaces built ON these live next door, each split out at the 800-line
ceiling: ``task_filtering`` holds the filter predicates and the project
conventions, and ``dashboard`` holds the assembled ``DashboardData`` the board
renders. Both import this module and it imports neither, so the dependency
runs one way.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

TaskStatusName = Literal["open", "completed", "cancelled"]
# Sections of the graph-native dashboard. The three workable sections are
# computed by joining the master open list against the Lithos ready/blocked
# frontier (see ``frontier.py``); ``unclassified`` only fills under frontier
# truncation. Completed/cancelled window recently-resolved work.
# ``attention`` is the severity-ordered Needs-attention list: rows promoted OUT
# of the section they would otherwise occupy (single-placement rule).
# ``open`` is the flat-fallback section: it holds every open row when there is
# no usable ready/blocked frontier, in which case the three workable sections
# stay empty. It is never populated alongside them.
SectionName = Literal[
    "attention",
    "open",
    "in_progress",
    "ready",
    "blocked",
    "claims_unknown",
    "unclassified",
    "completed",
    "cancelled",
]
# Known task types from the Lithos 0.4 task graph: "task" (workable), "epic",
# "gate". Transport records carry the raw server string so an unknown future
# type survives round-trip; a MISSING task_type is malformed under the 0.4
# contract and defaults to "task" so the row still renders (see
# ``normalizers.normalize_task``).
KNOWN_TASK_TYPES = frozenset({"task", "epic", "gate"})

TASK_STATUSES: tuple[TaskStatusName, ...] = ("open", "completed", "cancelled")

# Every open-side section, in render order. Only one mode's are ever filled:
# the flat ``open`` list, or the workable three plus their degraded tails.
# Canonical here so the render order, the open row count and the
# "did anything render?" checks below cannot drift apart.
# The three sections a workable open task can classify into. Needs attention
# promotes rows OUT of these, so a row counted here is one no rule fired on.
WORKABLE_SECTIONS: tuple[SectionName, ...] = ("in_progress", "ready", "blocked")

OPEN_SECTIONS: tuple[SectionName, ...] = (
    # Needs attention leads: its rows are promoted OUT of the three below.
    "attention",
    "open",
    "in_progress",
    "ready",
    "blocked",
    "claims_unknown",
    "unclassified",
)
# The statuses whose sections are windowed and sorted on ``resolved_at``.
TERMINAL_TASK_STATUSES: frozenset[str] = frozenset({"completed", "cancelled"})

# ``lithos_task_reopen`` records every reopen as a durable finding whose
# summary starts with this literal marker; it clears ``resolved_at`` and
# ``outcome``, so the finding is the only surviving evidence of the reopen.
#
# TRUST BOUNDARY: findings are free text. ``lithos_finding_post`` takes
# ``{task_id, agent, summary}`` with no credential, so ANY Lithos client can
# mint this prefix on ANY task under any ``agent`` string — the marker is a
# REPORT, not a verified fact, and the UI must attribute it rather than assert
# it (hence the "reopen reported by <agent>" copy). Corroborating against task
# state is not available either: a reopened-then-recompleted task legitimately
# carries ``resolved_at`` again. The authoritative signal is the upstream
# ``task.reopened`` event, which the Lens pipeline does not consume yet (T1
# slice 6); cross-checking against it is the follow-up.
REOPENED_FINDING_PREFIX = "[Reopened]"

# Absolute ceiling on the completed/cancelled lookback, in days.
# ``lithos_task_list`` takes no row limit, so this window is the ONLY bound on
# those two reads — and terminal history, unlike the open frontier, only ever
# grows: an unbounded ``?since=01/01/0001`` would pull the whole archive into
# one render. It bounds BOTH inputs that can widen the window — the ``?since=``
# request AND the configured ``default_time_range_days`` (rejected above this
# value at config load, and clamped here regardless) — so no configuration can
# raise it. A safety bound rather than an operator dial, the same shape as the
# client's ``_RECENT_NOTES_MAX_PAGES`` runaway guard.
MAX_SINCE_LOOKBACK_DAYS = 365

# Ceiling on the SIZE of the filter query string, in bytes — the ONE bound on
# ``?tag=`` / ``?status=`` / ``?epic=`` / ``?since=`` / ``?project=`` /
# ``?agent=``, applied to their total rather than per key.
#
# The bound is on the request, not on the filter's MEANING, because every
# filter is carried forward into each generated URL — the summary cards, one
# detail link per row, one tag link per tag per row. The response therefore
# echoes the query string O(rows x tags) times: on a 400-row board a 58 KB
# ``?status=`` rendered a 116 MB body (~2000x), and 34 KB of ``?tag=`` rendered
# 499 MB. Bounding the input bounds every one of those echoes at once, which
# per-key value ceilings did not.
#
# 1 KB is ~9x a rich real filter (status + project + agent + since + epic +
# tag is ~110 bytes; a 129-character tag still fits with room to spare), so no
# filter an operator or a bookmark can produce is affected.
#
# Deliberately REJECTED rather than trimmed (``web._filter_query_oversized``).
# Silently dropping filter terms is the one response that is never safe: it
# WIDENS the board, showing rows the operator's filter excluded, under chrome
# claiming a scope that is not applied. The ``since`` clamp below is the
# opposite shape and stays as it is — clamping a window narrows it, so it can
# only ever show less.
MAX_FILTER_QUERY_BYTES = 1024

# How many active-filter chips the board draws before summarising the rest.
# A RENDERING bound only: every tag still filters, and the strip says how many
# it is not naming. The strip is quadratic in tag count (a chip per tag, each
# re-emitting the others), so it needs a bound of its own even under the byte
# ceiling above.
MAX_FILTER_TAG_CHIPS = 12

# The two query keys that carry tags. ``tag`` is the filter itself and is fully
# literal; ``add_tag`` is the filter bar's text box, folded into the tag set at
# parse time and never re-emitted. They are separate so that a blank text box —
# which an HTML form submits on every search — cannot be confused with the
# literal empty tag that ``?tag=`` names.
TAG_FILTER_KEY = "tag"
ADD_TAG_FILTER_KEY = "add_tag"
TAG_FILTER_KEYS = (TAG_FILTER_KEY, ADD_TAG_FILTER_KEY)

# Project tracking conventions (REQUIREMENTS §5B.1). Two are live in the
# production corpus and their counts disagree: ``metadata.project = "<slug>"``
# (what Lithos itself understands) and a ``project:<slug>`` tag (the original
# Lens convention). ``project_convention`` selects which are honoured.
ProjectConvention = Literal["metadata", "tag", "both"]
PROJECT_CONVENTIONS: tuple[ProjectConvention, ...] = ("metadata", "tag", "both")
DEFAULT_PROJECT_CONVENTION: ProjectConvention = "both"
DEFAULT_PROJECT_TAG_KEY = "project"


class SectionState(StrEnum):
    OK = "ok"
    ERROR = "error"


@dataclass(frozen=True)
class TaskRecord:
    id: str
    title: str
    description: str = ""
    status: TaskStatusName = "open"
    created_by: str = ""
    created_at: str = ""
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    outcome: str = ""
    completed_at: str = ""
    # Task-graph type (lithos 0.4); see KNOWN_TASK_TYPES. Raw server value —
    # an unknown type survives round-trip. Older payloads omit it; default
    # "task".
    task_type: str = "task"
    # Timestamp a task reached a terminal state. Completed/cancelled rows are
    # windowed by this (not created_at). Empty for open tasks or older payloads.
    resolved_at: str = ""
    # Inline claims when the upstream lithos_task_list call was made with
    # with_claims=True (added in lithos #221). ``None`` means "claims were
    # not requested or not returned"; an empty tuple means "no active claims".
    claims: tuple[ClaimRecord, ...] | None = None


@dataclass(frozen=True)
class ClaimRecord:
    agent: str
    aspect: str
    expires_at: str = ""


@dataclass(frozen=True)
class TaskStatusRecord:
    id: str
    title: str
    status: str
    claims: tuple[ClaimRecord, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FindingRecord:
    id: str
    task_id: str
    agent: str
    summary: str
    knowledge_id: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class AgentRecord:
    id: str
    name: str = ""
    type: str = ""
    last_seen_at: str = ""


@dataclass(frozen=True)
class NoteRecord:
    id: str
    title: str
    content: str
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NoteSummary:
    """A lightweight note row from ``lithos_list`` (no body).

    Used by the wiki-link resolver's title-disambiguation step; the ``path`` /
    ``updated`` / ``tags`` fields carry the browsing metadata later slices (the
    ``/knowledge`` recent list) render.
    """

    id: str
    title: str = ""
    path: str = ""
    updated: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class BlockerChip:
    """One "waiting for" chip on a Blocked (or claimed-but-blocked) row.

    ``label`` is the display text — the blocking task's title when it resolves
    against the open snapshot, else a fallback (the gate/predecessor id or the
    raw blocker message). ``kind`` mirrors the source
    :class:`~lithos_lens.task_graph.BlockerRecord` kind
    (``task``/``gate``/``blocker_unsatisfiable``/``cycle``); ``target_id`` is
    the blocking task/gate id, kept for the deep-links a later slice adds.
    """

    label: str
    kind: str = "task"
    target_id: str = ""


# Needs-attention rules in severity order (§5.2.2 rule 1 -> 6). The slug IS the
# reason chip's text, so the vocabulary is fixed here and rendered verbatim.
ATTENTION_RULES: tuple[str, ...] = (
    "unsatisfiable",
    "cycle",
    "gate-waiting",
    "claim-expiring",
    "stale-open",
    "ready-unclaimed",
)


@dataclass(frozen=True)
class AttentionReason:
    """One fired Needs-attention rule on a promoted row.

    ``rule`` is a slug from :data:`ATTENTION_RULES` (also the chip text);
    ``detail`` is the one-line supporting fact the chip carries (e.g. ``Blocker
    "Design schema" was cancelled``), which the detail page's "Why this task is
    here" block reuses.
    """

    rule: str
    detail: str = ""

    @property
    def severity(self) -> int:
        """Rule order — lower is more severe. Unknown slugs sort last."""
        if self.rule in ATTENTION_RULES:
            return ATTENTION_RULES.index(self.rule)
        return len(ATTENTION_RULES)


@dataclass(frozen=True)
class SectionRow:
    """A task rendered in one dashboard section, with its display extras.

    ``claims`` are the inline claims from the master open list (the frontier
    calls don't carry them); ``blockers`` are the resolved blocker chips on a
    blocked row. ``claimed_but_blocked`` flags a claimed row (In progress) that
    Lithos also reports as blocked — an agent holding a claim on infeasible
    work. ``claims_unknown`` flags a row whose ``TaskRecord.claims`` was
    ``None`` — claims were not returned even though requested — which is NOT
    the same as an empty tuple (no active claims); the chip reads
    "claims unknown" instead of a confident "unclaimed".

    ``attention`` holds the Needs-attention reasons that fired for the row
    (empty for every row outside that section): a flagged row is promoted OUT
    of the section it would otherwise occupy, so the reasons travel with it.
    """

    task: TaskRecord
    claims: tuple[ClaimRecord, ...] = ()
    blockers: tuple[BlockerChip, ...] = ()
    claimed_but_blocked: bool = False
    claims_unknown: bool = False
    attention: tuple[AttentionReason, ...] = ()
    # The frontier reads are independent (no cross-call snapshot); when they
    # disagree even after the single retry, the row is classified
    # conservatively as Blocked and flagged so the template can render the
    # reconciliation warning instead of asserting real blockage.
    reconciliation_pending: bool = False

    @property
    def claim_state(self) -> str:
        if self.task.status != "open":
            return "not_applicable"
        if self.claims:
            return "known_claimed"
        return "unknown" if self.claims_unknown else "known_unclaimed"

    @property
    def timestamp(self) -> str:
        """The timestamp this row shows: RESOLUTION time on a terminal row.

        Completed/Cancelled are windowed and sorted on ``resolved_at``, so a
        row there must show that date — otherwise a task created long ago and
        finished yesterday reads as a filter bug ("2020-01-01" under "Resolved
        since 2026-04-01") and the newest-resolved-first order looks unsorted.
        Falls back to ``created_at`` when an older Lithos omitted
        ``resolved_at``, mirroring ``frontier._rows_for``'s sort key; open rows
        keep their creation date. ``timestamp_label`` says which one it is.
        """
        if self.task.status in TERMINAL_TASK_STATUSES:
            return self.task.resolved_at or self.task.created_at
        return self.task.created_at

    @property
    def timestamp_label(self) -> str:
        """Which date :attr:`timestamp` is — the two differ, so rows say so."""
        if self.task.status in TERMINAL_TASK_STATUSES and self.task.resolved_at:
            return "resolved"
        return "created"


@dataclass(frozen=True)
class EpicRollup:
    """One open epic's progress chip in the dashboard's epic strip.

    Built from ``lithos_task_children(recursive=True, include_closed=True)``:
    ``done``/``total`` count only WORKABLE (``task``-typed) descendants —
    nested epics and gates are structure, not units of work, and counting a
    sub-epic would double-count its own children. ``total`` excludes cancelled
    descendants (work that will never be done must not hold the bar under 100%
    forever); ``cancelled`` keeps that count visible rather than silent.
    ``descendant_ids`` holds EVERY descendant id whatever its type — a gate or
    sub-epic is still part of the initiative — and is what the ``?epic=``
    scope filters the sections by. On a strip assembled by ``frontier`` it is
    populated only for the ``selected`` epic: nothing reads another epic's set,
    and the subtree reads include closed tasks, so keeping them all would hold
    an id for every task ever closed under every epic.

    Counts are whole-subtree facts, so they are deliberately unaffected by the
    tag/agent/since filters applied to the sections.
    """

    task: TaskRecord
    done: int = 0
    total: int = 0
    cancelled: int = 0
    descendant_ids: frozenset[str] = frozenset()
    selected: bool = False

    @property
    def progress_label(self) -> str:
        return f"{self.done}/{self.total}"

    @property
    def percent(self) -> int:
        """Completed share of the non-cancelled subtree, 0-100 (0 when empty)."""
        return round(self.done * 100 / self.total) if self.total else 0


@dataclass(frozen=True)
class TaskFilters:
    """The live ``/tasks`` filter vocabulary, parsed from the query string.

    ``projects`` is multi-valued and matches a task under ``project_convention``
    (§5B.1) — a row matches when ANY selected slug is one of its project slugs.
    ``tags`` compose with AND, ``agent`` matches creator OR claimer, and
    ``since`` windows only the resolved sections. The convention knobs travel on
    the filters so the pure predicates below stay pure.
    """

    statuses: tuple[TaskStatusName, ...]
    tags: tuple[str, ...]
    agent: str
    since: str
    # ``?epic=<id>`` — scope every section to one epic's descendants. Empty
    # means "no epic scope"; an id that is no longer an open epic resolves to
    # no scope at all (``DashboardData.epic_scope``), not an empty board.
    epic: str = ""
    projects: tuple[str, ...] = ()
    project_convention: ProjectConvention = DEFAULT_PROJECT_CONVENTION
    project_tag_key: str = DEFAULT_PROJECT_TAG_KEY


def parse_filters(
    query_items: list[tuple[str, str]],
    default_days: int,
    default_statuses: tuple[TaskStatusName, ...] = TASK_STATUSES,
    *,
    project_convention: ProjectConvention = DEFAULT_PROJECT_CONVENTION,
    project_tag_key: str = DEFAULT_PROJECT_TAG_KEY,
) -> TaskFilters:
    values: dict[str, list[str]] = {}
    for key, value in query_items:
        if key in TAG_FILTER_KEYS:
            # Tags are literal (see ``honored_tags``): a comma is tag content,
            # whitespace is significant, and the empty string is a tag — so
            # nothing here is split, stripped, or dropped for being falsey.
            # Every other key is a constrained vocabulary — statuses are an
            # enum, project slugs are slugs — where the comma form is a
            # documented convenience that cannot collide with a value.
            values.setdefault(key, []).append(value)
            continue
        if not value:
            continue
        values.setdefault(key, []).extend(_split_values(value))

    requested_statuses = set(values.get("status", list(default_statuses)))
    status_items: list[TaskStatusName] = [
        status for status in TASK_STATUSES if status in requested_statuses
    ]
    statuses = tuple(status_items)
    if not statuses:
        statuses = TASK_STATUSES

    # ``claimed_state`` was retired with the graph-native dashboard (T1). Legacy
    # bookmarks that still carry it must degrade gracefully, so it is parsed
    # away (never read) rather than rejected.
    since = normalize_since_input(
        (values.get("since") or [""])[0],
        default_days=default_days,
    )
    return TaskFilters(
        statuses=statuses,
        tags=honored_tags(
            values.get(TAG_FILTER_KEY, []),
            added=values.get(ADD_TAG_FILTER_KEY, []),
        ),
        agent=(values.get("agent") or [""])[0],
        since=since,
        # The epic strip scopes to ONE epic at a time (a chip click), so only
        # the first ``epic`` value is honored.
        epic=(values.get("epic") or [""])[0],
        # Multi-select: ``?project=x&project=y`` (and the comma form) select
        # the union of those projects, not their intersection.
        projects=tuple(values.get("project", [])),
        project_convention=project_convention,
        project_tag_key=project_tag_key,
    )


def note_updated_sort_key(updated: str) -> datetime:
    """Newest-first sort key for a note's ISO ``updated`` timestamp.

    Shared by the real client and the fake so both ``recent_notes`` legs order
    identically. Naive timestamps are treated as UTC (the server normalizes to
    UTC before comparing, ``normalize_datetime``); blank or malformed values
    sort oldest, so unstamped notes sink to the bottom of a recent list instead
    of raising or floating to the top.
    """
    try:
        parsed = datetime.fromisoformat(updated)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def default_since(default_days: int) -> str:
    return lookback_date(default_days).isoformat()


def lookback_date(days: int) -> date:
    """The date ``days`` ago, bounded by :data:`MAX_SINCE_LOOKBACK_DAYS`.

    The single place a day count becomes a window floor, so the ceiling holds
    for every path — the default window, the ``?since=`` filter, and any later
    caller — whatever the configuration says. Clamping into ``[0, MAX]`` also
    means no admitted integer can overflow the date arithmetic (a 500 on the
    dashboard route).
    """
    return (
        datetime.now(UTC) - timedelta(days=min(max(days, 0), MAX_SINCE_LOOKBACK_DAYS))
    ).date()


def normalize_since_input(value: str, *, default_days: int) -> str:
    """Parse the ``?since=`` filter into a BOUNDED ISO date.

    Blank or unparseable input falls back to the default window; a lookback
    longer than :data:`MAX_SINCE_LOOKBACK_DAYS` is clamped to that ceiling
    rather than honored — including when the CONFIGURED default is wider, which
    ``lookback_date`` bounds too. Clamping (rather than snapping back to the
    default) keeps the filter doing what it says as far as it is permitted to,
    and the clamped value is what the filter bar re-renders, so the window
    shown is the window applied.
    """
    value = value.strip()
    if not value:
        return default_since(default_days)
    parsed = parse_date(value)
    if parsed is None:
        return default_since(default_days)
    floor = lookback_date(MAX_SINCE_LOOKBACK_DAYS)
    return parsed.isoformat() if parsed >= floor else floor.isoformat()


def format_display_date(value: str) -> str:
    parsed = parse_date(value)
    return parsed.strftime("%d/%m/%Y") if parsed else value


def format_tag(tag: str) -> str:
    if ":" not in tag:
        return tag
    key, value = tag.split(":", 1)
    return f"{key}: {value}"


def parse_timestamp(value: str) -> datetime | None:
    """Parse an ISO timestamp into an aware UTC datetime, or ``None``.

    Shared by the age-based Needs-attention rules (``attention.py``). A blank or
    malformed value returns ``None`` so a rule never fires on a timestamp it
    could not read — a false "stale"/"expiring" flag is worse than a missed
    one. Naive timestamps are treated as UTC, matching the server's own
    normalization.

    ``OverflowError`` is caught alongside ``ValueError`` because the UTC
    conversion — not the parse — is what rejects an offset timestamp at the
    edge of the datetime domain (``9999-12-31T23:59:59-05:00``). An upstream
    record carrying one must degrade to "unreadable" like any other bad value:
    letting it raise would take the whole dashboard down for every operator
    until the record was fixed.
    """
    try:
        parsed = datetime.fromisoformat(value.strip())
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def parse_date(value: str) -> date | None:
    if "/" in value:
        try:
            return datetime.strptime(value, "%d/%m/%Y").date()
        except ValueError:
            return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def int_stat(stats: dict[str, Any], key: str, *, default: int = 0) -> int:
    value = stats.get(key, default)
    return value if isinstance(value, int) else default


def honored_tags(
    values: Iterable[str], *, added: Iterable[str] = ()
) -> tuple[str, ...]:
    """The ``?tag=`` values the board filters by, in the order written.

    One ``tag`` parameter is one LITERAL tag, verbatim: repeat the parameter
    (``?tag=a&tag=b``) to AND several together. Deliberately no comma-splitting,
    no stripping and no dropping of falsey values, unlike ``project`` and
    ``status``, because a tag is not a constrained vocabulary — the vendored
    Lithos schema types it as a bare ``string`` with no pattern, length,
    ``minLength`` or character exclusions. A comma, surrounding whitespace and
    the empty string are all ordinary tag content, so ``?tag=`` is the
    empty-tag scope and not the absence of a filter. Splitting made the real
    tag ``customer,2`` unselectable, stripping rewrote `` urgent `` into a
    filter that matched a DIFFERENT task, and treating blank as absence showed
    the whole unfiltered board — three ways of quietly answering a question
    nobody asked. An exact-match filter has to name every value the store can
    hold.

    ``added`` is the filter bar's "add a tag" box (``?add_tag=``), which is a
    UI control rather than a filter term: an HTML text input submits on every
    search whether or not it was typed in, so a blank one means "nothing
    typed". It gets its OWN parameter precisely so that ambiguity cannot reach
    ``tag``, whose blank value has to stay literal. It is folded in here and
    never re-emitted — generated URLs carry the canonical ``tag`` list.

    EVERY requested tag is then honoured: dropping a term would widen the board,
    showing rows the operator excluded, so the request SIZE is bounded instead
    (``MAX_FILTER_QUERY_BYTES``), which never removes a predicate.

    Repeats collapse: ``all(tag in task.tags)`` is idempotent, so a duplicate is
    a redundant chip and a wasted comparison per row, never a different result.
    """
    tags: list[str] = []
    for value in values:
        if value not in tags:
            tags.append(value)
    for value in added:
        if value and value not in tags:
            tags.append(value)
    return tuple(tags)


def _split_values(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]
