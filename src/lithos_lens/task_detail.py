"""The task detail page: its view models and the reads that fill them.

Split out of ``tasks.py`` when T1-S3's attention model pushed it past the
800-line god-module ceiling. The seam follows the surfaces: ``tasks.py`` keeps
the records and the DASHBOARD view models, and everything that exists only for
``/tasks/{id}`` — the blocker chain, the hierarchy, the findings timeline, the
reopen report, the per-section degraded states of a single task — lives here.

Since T1-S7 the page is graph-native: one ``lithos_task_get`` (so a deep link
to a deleted task gets a clean not-found envelope instead of the three-list
scan ``find_task`` used to do), then ``lithos_task_status``,
``lithos_task_edge_list(direction="both")``, ``lithos_finding_list`` and
``lithos_task_children`` in parallel. Every read the EDGES then imply — blocker
statuses, provenance, the parent walk — goes through ``task_links``, which is
where their fan-out is bounded.

The dependency runs one way: this module imports the records and the graph
helpers, ``tasks.py`` and ``task_links.py`` never import back.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol, cast

from lithos_lens.task_graph import EdgeRecord
from lithos_lens.task_links import (
    BLOCKER_EDGE_TYPES,
    PROVENANCE_EDGE_TYPE,
    Breadcrumb,
    LinkPage,
    TaskLinkClient,
    gate_type_of,
    incoming_targets,
    load_link_page,
    load_parent_breadcrumb,
    new_link_limiter,
    outgoing_targets,
)
from lithos_lens.tasks import (
    REOPENED_FINDING_PREFIX,
    FindingRecord,
    NoteRecord,
    SectionState,
    TaskRecord,
    TaskStatusRecord,
)

# The Lithos 0.4 error code for a task that does not exist. Matched duck-typed
# off the raised exception's ``code`` attribute because the layering contract
# forbids Foundation importing the client (``LithosToolError`` lives in Core);
# matching on message text would be the worse alternative.
TASK_NOT_FOUND_CODE = "task_not_found"


class TaskDetailClient(TaskLinkClient, Protocol):
    """The subset of the Lithos client the detail page consumes.

    Narrower than it was before T1-S7: with ``find_task`` deleted the page no
    longer reads the task LISTS at all — it addresses the one task directly.
    """

    async def task_status(self, task_id: str) -> TaskStatusRecord | None: ...

    async def task_children(
        self,
        task_id: str,
        *,
        recursive: bool = False,
        include_closed: bool = False,
    ) -> list[TaskRecord]: ...

    async def list_findings(
        self, task_id: str, *, since: str | None = None
    ) -> list[FindingRecord]: ...

    async def read_note(self, knowledge_id: str) -> NoteRecord | None: ...


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
    # The level-1 blocker chain and the two provenance directions. Each is a
    # BOUNDED page of the edge set with a counted tail (``task_links``), never
    # the whole set: the edge count is agent-controlled.
    blockers: LinkPage = LinkPage()
    discovered_from: LinkPage = LinkPage()
    spawned: LinkPage = LinkPage()
    breadcrumb: Breadcrumb = Breadcrumb()
    # Direct children, closed ones included so a child's status is legible.
    # One ``lithos_task_children`` call — one round trip returning N rows, not
    # N round trips — so this is not part of the fan-out bound above.
    children: tuple[TaskRecord, ...] = ()
    status_state: SectionState = SectionState.OK
    findings_state: SectionState = SectionState.OK
    relations_state: SectionState = SectionState.OK
    children_state: SectionState = SectionState.OK
    not_found: bool = False
    errors: tuple[str, ...] = ()

    @property
    def gate_type(self) -> str:
        """This task's gate type, when it is a gate at all."""
        return gate_type_of(self.task) if self.task else ""

    @property
    def has_provenance(self) -> bool:
        return bool(self.discovered_from.total or self.spawned.total)

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
    lithos: TaskDetailClient,
    task_id: str,
) -> TaskDetailData:
    errors: list[str] = []
    try:
        task = await lithos.task_get(task_id)
    except Exception as exc:
        # A missing task is an ANSWER (Lithos replies with the task_not_found
        # envelope), so it renders the not-found panel; anything else is a
        # failed read and must not be reported as "this task does not exist".
        if getattr(exc, "code", "") == TASK_NOT_FOUND_CODE:
            return TaskDetailData(task=None, not_found=True)
        return TaskDetailData(
            task=None, errors=("Could not load this task from Lithos.",)
        )

    (
        status_result,
        edges_result,
        findings_result,
        children_result,
    ) = await asyncio.gather(
        lithos.task_status(task_id),
        lithos.task_edge_list(task_id, direction="both"),
        lithos.list_findings(task_id),
        lithos.task_children(task_id, include_closed=True),
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

    children: tuple[TaskRecord, ...] = ()
    children_state = SectionState.OK
    if isinstance(children_result, BaseException):
        children_state = SectionState.ERROR
        errors.append("Could not load child tasks.")
    else:
        children = tuple(cast(list[TaskRecord], children_result))

    edges: tuple[EdgeRecord, ...] = ()
    relations_state = SectionState.OK
    if isinstance(edges_result, BaseException):
        relations_state = SectionState.ERROR
        errors.append("Could not load task relations.")
    else:
        edges = tuple(cast(list[EdgeRecord], edges_result))

    blockers, discovered_from, spawned, breadcrumb = await _load_relations(
        lithos, task_id, edges
    )

    return TaskDetailData(
        task=task,
        task_status=task_status,
        findings=finding_views,
        blockers=blockers,
        discovered_from=discovered_from,
        spawned=spawned,
        breadcrumb=breadcrumb,
        children=children,
        status_state=status_state,
        findings_state=findings_state,
        relations_state=relations_state,
        children_state=children_state,
        errors=tuple(errors),
    )


async def _load_relations(
    lithos: TaskDetailClient,
    task_id: str,
    edges: tuple[EdgeRecord, ...],
) -> tuple[LinkPage, LinkPage, LinkPage, Breadcrumb]:
    """Turn one edge list into the page's three link pages and its breadcrumb.

    The edge list is one round trip returning N rows; resolving those rows'
    live statuses is N round trips on the shared MCP session, which is why
    every one of these goes through ``task_links.load_link_page`` rather than
    gathering the raw edge set. The three pages share ONE limiter, so the
    bound is on this render's whole fan-out and not on each page separately.
    """
    limiter = new_link_limiter()
    blockers, discovered_from, spawned = await asyncio.gather(
        load_link_page(
            lithos,
            incoming_targets(task_id, edges, BLOCKER_EDGE_TYPES),
            limiter=limiter,
        ),
        load_link_page(
            lithos,
            incoming_targets(task_id, edges, (PROVENANCE_EDGE_TYPE,)),
            limiter=limiter,
        ),
        load_link_page(
            lithos,
            outgoing_targets(task_id, edges, (PROVENANCE_EDGE_TYPE,)),
            limiter=limiter,
        ),
    )
    breadcrumb = await load_parent_breadcrumb(lithos, task_id, edges)
    return blockers, discovered_from, spawned, breadcrumb


async def resolve_finding_notes(
    lithos: TaskDetailClient,
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
