"""Tasks dashboard data loading and normalization."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Protocol, cast

TaskStatusName = Literal["open", "completed", "cancelled"]
ClaimedState = Literal["any", "known_claimed", "known_unclaimed"]

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
class EnrichedTask:
    task: TaskRecord
    task_status: TaskStatusRecord | None = None
    claim_error: str = ""

    @property
    def claims(self) -> tuple[ClaimRecord, ...]:
        return self.task_status.claims if self.task_status else ()

    @property
    def claim_state(self) -> str:
        if self.task.status != "open":
            return "not_applicable"
        if self.task_status is None:
            return "unknown"
        return "known_claimed" if self.task_status.claims else "known_unclaimed"


@dataclass(frozen=True)
class TaskFilters:
    statuses: tuple[TaskStatusName, ...]
    claimed_state: ClaimedState
    tags: tuple[str, ...]
    agent: str
    since: str


@dataclass(frozen=True)
class TaskSummary:
    open_tasks: int = 0
    open_claims: int = 0
    claimed_open_tasks: int = 0
    unclaimed_open_tasks: int = 0
    unknown_claim_open_tasks: int = 0
    recent_completed: int = 0
    recent_cancelled: int = 0
    agents: int = 0


@dataclass(frozen=True)
class DashboardData:
    filters: TaskFilters
    summary: TaskSummary
    groups: dict[TaskStatusName, tuple[EnrichedTask, ...]]
    agents: tuple[AgentRecord, ...]
    visible_cap: int
    open_total: int
    claim_cap_exceeded: bool = False
    claim_filter_limited: bool = False
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
    async def list_tasks(
        self,
        *,
        agent: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
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

    claimed_state_raw = (values.get("claimed_state") or ["any"])[0]
    claimed_state: ClaimedState = (
        claimed_state_raw
        if claimed_state_raw in {"any", "known_claimed", "known_unclaimed"}
        else "any"
    )  # type: ignore[assignment]

    since = normalize_since_input(
        (values.get("since") or [""])[0],
        default_days=default_days,
    )
    return TaskFilters(
        statuses=statuses,
        claimed_state=claimed_state,
        tags=tuple(values.get("tag", [])),
        agent=(values.get("agent") or [""])[0],
        since=since,
    )


async def load_dashboard(
    lithos: TaskLithosClientProtocol,
    *,
    filters: TaskFilters,
    visible_cap: int,
) -> DashboardData:
    errors: list[str] = []
    query_tags = list(filters.tags) or None
    query_agent = filters.agent or None

    async def load_group(status: TaskStatusName) -> list[TaskRecord]:
        since = filters.since
        return await lithos.list_tasks(
            agent=query_agent,
            status=status,
            tags=query_tags,
            since=since,
        )

    group_results = await asyncio.gather(
        *(load_group(status) for status in TASK_STATUSES),
        return_exceptions=True,
    )
    raw_groups: dict[TaskStatusName, list[TaskRecord]] = {}
    for status, result in zip(TASK_STATUSES, group_results, strict=True):
        if isinstance(result, BaseException):
            errors.append(f"Could not load {status} tasks.")
            raw_groups[status] = []
        else:
            task_records = cast(list[TaskRecord], result)
            task_records = [
                task
                for task in task_records
                if _matches_filters(task, filters=filters, status=status)
            ]
            raw_groups[status] = sorted(
                task_records, key=lambda task: task.created_at, reverse=True
            )

    open_total = len(raw_groups["open"])
    enriched_open = await _enrich_open_tasks(
        lithos, raw_groups["open"], visible_cap, errors
    )
    filtered_open = _apply_claim_filter(enriched_open, filters.claimed_state)

    stats_result, agents_result = await asyncio.gather(
        lithos.stats(),
        lithos.list_agents(),
        return_exceptions=True,
    )
    stats: dict[str, Any] = {}
    if isinstance(stats_result, BaseException):
        errors.append("Could not load Lithos stats.")
    else:
        stats = cast(dict[str, Any], stats_result)

    agents: tuple[AgentRecord, ...] = ()
    if isinstance(agents_result, BaseException):
        errors.append("Could not load agent list.")
    else:
        agents = tuple(cast(list[AgentRecord], agents_result))

    groups: dict[TaskStatusName, tuple[EnrichedTask, ...]] = {
        "open": tuple(filtered_open) if "open" in filters.statuses else (),
        "completed": tuple(EnrichedTask(task) for task in raw_groups["completed"])
        if "completed" in filters.statuses
        else (),
        "cancelled": tuple(EnrichedTask(task) for task in raw_groups["cancelled"])
        if "cancelled" in filters.statuses
        else (),
    }
    known_claimed = sum(
        1 for row in enriched_open if row.claim_state == "known_claimed"
    )
    known_unclaimed = sum(
        1 for row in enriched_open if row.claim_state == "known_unclaimed"
    )
    summary = TaskSummary(
        open_tasks=open_total,
        open_claims=_int_stat(stats, "open_claims"),
        claimed_open_tasks=known_claimed,
        unclaimed_open_tasks=known_unclaimed,
        unknown_claim_open_tasks=max(open_total - known_claimed - known_unclaimed, 0),
        recent_completed=len(raw_groups["completed"]),
        recent_cancelled=len(raw_groups["cancelled"]),
        agents=_int_stat(stats, "agents", default=len(agents)),
    )
    return DashboardData(
        filters=filters,
        summary=summary,
        groups=groups,
        agents=agents,
        visible_cap=visible_cap,
        open_total=open_total,
        claim_cap_exceeded=open_total > visible_cap,
        claim_filter_limited=open_total > visible_cap
        and filters.claimed_state != "any",
        errors=tuple(errors),
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


async def _enrich_open_tasks(
    lithos: TaskLithosClientProtocol,
    open_tasks: list[TaskRecord],
    visible_cap: int,
    errors: list[str],
) -> list[EnrichedTask]:
    visible = open_tasks[:visible_cap]
    results = await asyncio.gather(
        *(lithos.task_status(task.id) for task in visible),
        return_exceptions=True,
    )
    enriched: list[EnrichedTask] = []
    for task, result in zip(visible, results, strict=True):
        if isinstance(result, BaseException):
            errors.append(f"Could not load claims for task {task.id}.")
            enriched.append(EnrichedTask(task=task, claim_error="Claims unavailable."))
        else:
            enriched.append(
                EnrichedTask(
                    task=task,
                    task_status=cast(TaskStatusRecord | None, result),
                )
            )
    enriched.extend(EnrichedTask(task=task) for task in open_tasks[visible_cap:])
    return enriched


def _apply_claim_filter(
    tasks: list[EnrichedTask],
    claimed_state: ClaimedState,
) -> list[EnrichedTask]:
    if claimed_state == "any":
        return tasks
    return [task for task in tasks if task.claim_state == claimed_state]


def _matches_filters(
    task: TaskRecord,
    *,
    filters: TaskFilters,
    status: TaskStatusName,
) -> bool:
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


def _int_stat(stats: dict[str, Any], key: str, *, default: int = 0) -> int:
    value = stats.get(key, default)
    return value if isinstance(value, int) else default


def _split_values(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]
