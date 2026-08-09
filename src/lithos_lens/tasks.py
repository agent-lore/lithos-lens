"""Tasks dashboard data loading and normalization."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Protocol, cast

TaskStatusName = Literal["open", "completed", "cancelled"]
# Sections of the graph-native dashboard. The three workable sections are
# computed by joining the master open list against the Lithos ready/blocked
# frontier (see ``frontier.py``); ``unclassified`` only fills under frontier
# truncation. Completed/cancelled window recently-resolved work.
SectionName = Literal[
    "in_progress", "ready", "blocked", "unclassified", "completed", "cancelled"
]
# Known task types from the Lithos 0.4 task graph: "task" (workable), "epic",
# "gate". Transport records carry the raw server string so an unknown future
# type survives round-trip; only a MISSING task_type defaults to "task" (legacy
# payloads predate the field).
KNOWN_TASK_TYPES = frozenset({"task", "epic", "gate"})

TASK_STATUSES: tuple[TaskStatusName, ...] = ("open", "completed", "cancelled")


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
    """

    task: TaskRecord
    claims: tuple[ClaimRecord, ...] = ()
    blockers: tuple[BlockerChip, ...] = ()
    claimed_but_blocked: bool = False
    claims_unknown: bool = False
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


@dataclass(frozen=True)
class TaskFilters:
    statuses: tuple[TaskStatusName, ...]
    tags: tuple[str, ...]
    agent: str
    since: str


@dataclass(frozen=True)
class TaskSummary:
    in_progress: int = 0
    ready: int = 0
    blocked: int = 0
    unclassified: int = 0
    open_total: int = 0
    open_claims: int = 0
    recent_completed: int = 0
    recent_cancelled: int = 0
    agents: int = 0


@dataclass(frozen=True)
class DashboardData:
    filters: TaskFilters
    summary: TaskSummary
    sections: dict[SectionName, tuple[SectionRow, ...]]
    agents: tuple[AgentRecord, ...]
    frontier_limit: int
    open_total: int
    reconciliation_pending: bool = False
    truncated: bool = False
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class FindingView:
    finding: FindingRecord
    note_title: str = ""
    note_error: str = ""

    @property
    def link_label(self) -> str:
        return self.note_title or "View document"


@dataclass(frozen=True)
class TaskDetailData:
    task: TaskRecord | None
    task_status: TaskStatusRecord | None = None
    findings: tuple[FindingView, ...] = ()
    status_state: SectionState = SectionState.OK
    findings_state: SectionState = SectionState.OK
    not_found: bool = False
    errors: tuple[str, ...] = ()


class TaskLithosClientProtocol(Protocol):
    """The subset of the Lithos client this module's loaders consume.

    The full client surface (including the task-graph reads) lives on
    ``lithos_lens.lithos_client.LithosClientProtocol``; graph view models
    belong to ``lithos_lens.task_graph``.
    """

    async def list_tasks(
        self,
        *,
        agent: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
        with_claims: bool = False,
    ) -> list[TaskRecord]: ...

    async def task_status(self, task_id: str) -> TaskStatusRecord | None: ...

    async def list_findings(
        self, task_id: str, *, since: str | None = None
    ) -> list[FindingRecord]: ...

    async def stats(self) -> dict[str, Any]: ...

    async def list_agents(self) -> list[AgentRecord]: ...

    async def read_note(self, knowledge_id: str) -> NoteRecord | None: ...


def parse_filters(
    query_items: list[tuple[str, str]],
    default_days: int,
    default_statuses: tuple[TaskStatusName, ...] = TASK_STATUSES,
) -> TaskFilters:
    values: dict[str, list[str]] = {}
    for key, value in query_items:
        if value:
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
        tags=tuple(values.get("tag", [])),
        agent=(values.get("agent") or [""])[0],
        since=since,
    )


async def load_task_detail(
    lithos: TaskLithosClientProtocol,
    task_id: str,
) -> TaskDetailData:
    errors: list[str] = []
    task = await find_task(lithos, task_id)
    if task is None:
        return TaskDetailData(task=None, not_found=True)

    status_result, findings_result = await asyncio.gather(
        lithos.task_status(task_id),
        lithos.list_findings(task_id),
        return_exceptions=True,
    )

    task_status: TaskStatusRecord | None = None
    status_state = SectionState.OK
    if isinstance(status_result, BaseException):
        status_state = SectionState.ERROR
        errors.append("Could not load active claims.")
    else:
        task_status = cast(TaskStatusRecord | None, status_result)

    finding_views: tuple[FindingView, ...] = ()
    findings_state = SectionState.OK
    if isinstance(findings_result, BaseException):
        findings_state = SectionState.ERROR
        errors.append("Could not load findings.")
    else:
        finding_views = await resolve_finding_notes(
            lithos, cast(list[FindingRecord], findings_result)
        )

    return TaskDetailData(
        task=task,
        task_status=task_status,
        findings=finding_views,
        status_state=status_state,
        findings_state=findings_state,
        errors=tuple(errors),
    )


async def find_task(
    lithos: TaskLithosClientProtocol,
    task_id: str,
) -> TaskRecord | None:
    results = await asyncio.gather(
        *(lithos.list_tasks(status=status) for status in TASK_STATUSES),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            continue
        for task in cast(list[TaskRecord], result):
            if task.id == task_id:
                return task
    return None


async def resolve_finding_notes(
    lithos: TaskLithosClientProtocol,
    findings: list[FindingRecord],
) -> tuple[FindingView, ...]:
    cache: dict[str, NoteRecord | None] = {}
    views: list[FindingView] = []
    for finding in sorted(findings, key=lambda item: item.created_at):
        if not finding.knowledge_id:
            views.append(FindingView(finding=finding))
            continue
        if finding.knowledge_id not in cache:
            try:
                cache[finding.knowledge_id] = await lithos.read_note(
                    finding.knowledge_id
                )
            except Exception:
                cache[finding.knowledge_id] = None
        note = cache[finding.knowledge_id]
        views.append(
            FindingView(
                finding=finding,
                note_title=note.title if note else "",
                note_error="" if note else "Could not resolve document title.",
            )
        )
    return tuple(views)


def normalize_task(raw: dict[str, Any]) -> TaskRecord:
    status_raw = str(raw.get("status") or "open")
    status: TaskStatusName = status_raw if status_raw in TASK_STATUSES else "open"  # type: ignore[assignment]
    # Raw passthrough: only a MISSING/empty task_type defaults to "task"
    # (legacy payloads); an unknown explicit value survives round-trip.
    task_type = str(raw.get("task_type") or "task")
    claims: tuple[ClaimRecord, ...] | None = None
    if "claims" in raw and raw["claims"] is not None:
        claims = tuple(
            ClaimRecord(
                agent=str(claim.get("agent") or ""),
                aspect=str(claim.get("aspect") or ""),
                expires_at=str(claim.get("expires_at") or ""),
            )
            for claim in raw["claims"]
            if isinstance(claim, dict)
        )
    return TaskRecord(
        id=str(raw.get("id") or ""),
        title=str(raw.get("title") or "Untitled task"),
        description=str(raw.get("description") or ""),
        status=status,
        created_by=str(raw.get("created_by") or raw.get("agent") or ""),
        created_at=str(raw.get("created_at") or ""),
        tags=tuple(str(tag) for tag in raw.get("tags") or []),
        metadata=dict(raw.get("metadata") or {}),
        outcome=str(raw.get("outcome") or ""),
        completed_at=str(raw.get("completed_at") or ""),
        task_type=task_type,
        resolved_at=str(raw.get("resolved_at") or ""),
        claims=claims,
    )


def normalize_task_status(raw: dict[str, Any]) -> TaskStatusRecord:
    return TaskStatusRecord(
        id=str(raw.get("id") or ""),
        title=str(raw.get("title") or ""),
        status=str(raw.get("status") or ""),
        claims=tuple(
            ClaimRecord(
                agent=str(claim.get("agent") or ""),
                aspect=str(claim.get("aspect") or ""),
                expires_at=str(claim.get("expires_at") or ""),
            )
            for claim in raw.get("claims") or []
            if isinstance(claim, dict)
        ),
        metadata=dict(raw.get("metadata") or {}),
    )


def normalize_finding(raw: dict[str, Any], task_id: str) -> FindingRecord:
    return FindingRecord(
        id=str(raw.get("id") or ""),
        task_id=str(raw.get("task_id") or task_id),
        agent=str(raw.get("agent") or ""),
        summary=str(raw.get("summary") or ""),
        knowledge_id=str(raw.get("knowledge_id") or ""),
        created_at=str(raw.get("created_at") or ""),
    )


def normalize_agent(raw: dict[str, Any]) -> AgentRecord:
    return AgentRecord(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        type=str(raw.get("type") or ""),
        last_seen_at=str(raw.get("last_seen_at") or ""),
    )


def normalize_note(raw: dict[str, Any]) -> NoteRecord:
    metadata = dict(raw.get("metadata") or {})
    tags = raw.get("tags") or metadata.get("tags") or []
    return NoteRecord(
        id=str(raw.get("id") or ""),
        title=str(raw.get("title") or "Untitled document"),
        content=str(raw.get("content") or ""),
        tags=tuple(str(tag) for tag in tags),
        metadata=metadata,
    )


def normalize_note_summary(raw: dict[str, Any]) -> NoteSummary:
    metadata = dict(raw.get("metadata") or {})
    tags = raw.get("tags") or metadata.get("tags") or []
    return NoteSummary(
        id=str(raw.get("id") or ""),
        title=str(raw.get("title") or ""),
        path=str(raw.get("path") or ""),
        updated=str(raw.get("updated") or raw.get("updated_at") or ""),
        tags=tuple(str(tag) for tag in tags),
    )


def default_since(default_days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=default_days)).date().isoformat()


def normalize_since_input(value: str, *, default_days: int) -> str:
    value = value.strip()
    if not value:
        return default_since(default_days)
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else default_since(default_days)


def format_display_date(value: str) -> str:
    parsed = parse_date(value)
    return parsed.strftime("%d/%m/%Y") if parsed else value


def format_tag(tag: str) -> str:
    if ":" not in tag:
        return tag
    key, value = tag.split(":", 1)
    return f"{key}: {value}"


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


def matches_filters(
    task: TaskRecord,
    *,
    filters: TaskFilters,
    status: TaskStatusName,
) -> bool:
    """Client-side filter predicate shared by the dashboard sections.

    Public because the frontier join (``frontier.py``) re-applies it over the
    joined snapshot; the guardrail forbids reaching for another module's
    privates.
    """
    if task.status != status:
        return False
    if filters.agent and task.created_by != filters.agent:
        return False
    if filters.tags and not all(tag in task.tags for tag in filters.tags):
        return False
    if status in {"completed", "cancelled"} and filters.since:
        task_date = parse_date(task.created_at)
        since_date = parse_date(filters.since)
        if task_date is not None and since_date is not None and task_date < since_date:
            return False
    return True


def int_stat(stats: dict[str, Any], key: str, *, default: int = 0) -> int:
    value = stats.get(key, default)
    return value if isinstance(value, int) else default


def _split_values(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]
