"""The task detail page: its view models and the reads that fill them.

Split out of ``tasks.py`` when T1-S3's attention model pushed it past the
800-line god-module ceiling. The seam follows the surfaces: ``tasks.py`` keeps
the records and the DASHBOARD view models, and everything that exists only for
``/tasks/{id}`` — the findings timeline, the reopen report, the per-section
degraded states of a single task — lives here.

The dependency runs one way: this module imports the records, ``tasks.py``
never imports back.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast

from lithos_lens.tasks import (
    REOPENED_FINDING_PREFIX,
    TASK_STATUSES,
    FindingRecord,
    NoteRecord,
    SectionState,
    TaskLithosClientProtocol,
    TaskRecord,
    TaskStatusRecord,
)


@dataclass(frozen=True)
class FindingView:
    finding: FindingRecord
    note_title: str = ""
    note_error: str = ""

    @property
    def link_label(self) -> str:
        return self.note_title or "View document"

    @property
    def is_reopen(self) -> bool:
        """True for a ``[Reopened]`` finding — a reopen REPORT, not a verdict.

        ``lithos_task_reopen`` posts this finding and leaves no other trace (it
        clears ``resolved_at`` and ``outcome``), but any client can post the
        same prefix under any agent name; see the trust note on
        :data:`REOPENED_FINDING_PREFIX`. Both markers it drives are worded as
        attributed reports for that reason.
        """
        return self.finding.summary.lstrip().startswith(REOPENED_FINDING_PREFIX)


@dataclass(frozen=True)
class TaskDetailData:
    task: TaskRecord | None
    task_status: TaskStatusRecord | None = None
    findings: tuple[FindingView, ...] = ()
    status_state: SectionState = SectionState.OK
    findings_state: SectionState = SectionState.OK
    not_found: bool = False
    errors: tuple[str, ...] = ()

    @property
    def reopen_report(self) -> FindingView | None:
        """The most recent reopen report on this task, if any.

        Derived from the findings timeline (see ``FindingView.is_reopen``),
        which is the only durable record of a reopen — and an unauthenticated
        one, so the view carries the REPORTING AGENT and the header attributes
        the claim to them instead of stating a lifecycle reversal as fact.
        ``findings`` is ordered oldest-first by ``resolve_finding_notes``, so
        the last match is the latest report. A findings load failure yields
        None ("unknown"), never a false negative claim.
        """
        return next((view for view in reversed(self.findings) if view.is_reopen), None)


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
