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
``lithos_task_children`` in parallel.

Nearly every set this page renders is agent-sized, and each of those is bounded
by ``task_links.bounded_page`` and reports its remainder through the one tail
template. That covers both shapes the hazard takes: the reads the EDGES imply
(blocker statuses, provenance, the parent walk, and the finding-note titles) —
bounded in ROUND TRIPS, and sharing one limiter and one deadline across the
render — and the child set, which costs one round trip but would otherwise be
rendered in full on every auto-refresh, so it is bounded in ROWS.

There is exactly ONE exception, and it is deliberate rather than an oversight:
the findings TIMELINE. ``resolve_finding_notes`` builds a view for every
finding and ``templates/tasks/findings.html`` renders all of them, so its
response size is O(sum of the agent-written summary lengths) with the row count
agent-chosen. The note-title FAN-OUT behind it is bounded; the row count is
not. That predates T1-S7, which changed neither file, and bounding it is a
product decision this slice does not get to make — nor a free one. The timeline
is the task's audit record, so a page of it needs a choice about which END to
keep, and :attr:`TaskDetailData.reopen_report` is derived from the same set: it
is the only evidence a reopen ever happened, so a page that dropped the wrong
end would silently retract the reopened marker the story requires.

The dependency runs one way: this module imports the records and the graph
helpers, ``tasks.py`` and ``task_links.py`` never import back.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol, cast

from lithos_lens.task_graph import EdgeRecord
from lithos_lens.task_links import (
    BLOCKER_EDGE_TYPES,
    LINK_READ_TIMEOUT_S,
    PROVENANCE_EDGE_TYPE,
    Breadcrumb,
    LinkPage,
    PageTail,
    TaskLinkClient,
    bounded_page,
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
    # ONE limiter for this render's whole fan-out — the neighbour pages AND the
    # finding-note lookups. Per-call-site limiters would let a single render
    # run several pages' worth of slots at once.
    limiter = new_link_limiter()

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
    # ONE ``lithos_task_children`` call, so this costs no round-trip fan-out —
    # but M is agent-chosen just like the edge count, and ``include_closed``
    # maximises it, so what needs bounding here is the RENDER: an epic with a
    # runaway child set would otherwise concatenate M rows into every response,
    # on every auto-refresh. Sliced by the same ``bounded_page``, with
    # ``children_tail`` naming the remainder through the same tail template.
    children: tuple[TaskRecord, ...] = ()
    children_tail: PageTail = PageTail()
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

    # ONE limiter for this render's whole fan-out — the neighbour pages AND the
    # finding-note lookups. Per-call-site limiters would let a single render
    # run several pages' worth of slots at once.
    limiter = new_link_limiter()

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
            lithos, cast(list[FindingRecord], findings_result), limiter=limiter
        )

    children: tuple[TaskRecord, ...] = ()
    children_tail = PageTail()
    children_state = SectionState.OK
    if isinstance(children_result, BaseException):
        children_state = SectionState.ERROR
        errors.append("Could not load child tasks.")
    else:
        children, children_tail = bounded_page(cast(list[TaskRecord], children_result))

    edges: tuple[EdgeRecord, ...] = ()
    relations_state = SectionState.OK
    if isinstance(edges_result, BaseException):
        relations_state = SectionState.ERROR
        errors.append("Could not load task relations.")
    else:
        edges = tuple(cast(list[EdgeRecord], edges_result))

    blockers, discovered_from, spawned, breadcrumb = await _load_relations(
        lithos, task_id, edges, limiter=limiter
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
        children_tail=children_tail,
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
    *,
    limiter: asyncio.Semaphore,
) -> tuple[LinkPage, LinkPage, LinkPage, Breadcrumb]:
    """Turn one edge list into the page's three link pages and its breadcrumb.

    The edge list is one round trip returning N rows, parsed locally in O(N);
    resolving those rows' live statuses is N round trips on the shared MCP
    session, each with its own latency and its own share of the contention.
    That is why every one of these goes through ``task_links.load_link_page``
    rather than gathering the raw edge set, and why the render's limiter is
    threaded in rather than made per call site.
    """
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
    *,
    limiter: asyncio.Semaphore | None = None,
) -> tuple[FindingView, ...]:
    """Attach each finding's document title, through a BOUNDED note fan-out.

    One ``lithos_read`` per DISTINCT knowledge id, and that count is
    agent-chosen — ``lithos_finding_list`` takes no ``limit`` — so this is the
    same unbounded per-render fan-out the blocker chain has, and it used to be
    worse: it ran sequentially, so N reads cost N latencies end to end. It is
    bounded the same way and by the same mechanism: one ``bounded_page`` of
    distinct ids, resolved concurrently under the render's shared limiter, each
    deadlined.

    EVERY finding still renders — the fan-out is bounded, the timeline it feeds
    deliberately is not (see the module docstring's named exception). A finding
    whose id fell outside the page shows the generic link label instead of the
    document title — a link the operator can still follow, not a dropped row —
    and deliberately carries no ``note_error``: that line is reserved for reads
    that actually failed, and claiming a failure here would be false.
    """
    ordered = sorted(findings, key=lambda item: item.created_at)
    # Order-preserving dedup in O(N). A ``not in <list>`` scan reads the same
    # but is Θ(N²) string comparisons over an agent-chosen N, and — having no
    # ``await`` in it — would block the single event loop for the whole worker
    # rather than merely slowing this render: every other in-flight request,
    # including the SSE stream and /health, stalls with it. ``bounded_page``
    # cannot rescue that; it slices the RESULT, so it never sees the scan.
    distinct = list(
        dict.fromkeys(
            finding.knowledge_id for finding in ordered if finding.knowledge_id
        )
    )
    page, _tail = bounded_page(distinct)
    gate = limiter or new_link_limiter()

    async def read(knowledge_id: str) -> Any:
        async with gate:
            return await asyncio.wait_for(
                lithos.read_note(knowledge_id), LINK_READ_TIMEOUT_S
            )

    results = await asyncio.gather(
        *(read(knowledge_id) for knowledge_id in page), return_exceptions=True
    )
    notes: dict[str, NoteRecord | None] = {
        knowledge_id: None
        if isinstance(result, BaseException)
        else cast(NoteRecord | None, result)
        for knowledge_id, result in zip(page, results, strict=True)
    }

    views: list[FindingView] = []
    for finding in ordered:
        if not finding.knowledge_id or finding.knowledge_id not in notes:
            views.append(FindingView(finding=finding))
            continue
        note = notes[finding.knowledge_id]
        views.append(
            FindingView(
                finding=finding,
                note_title=note.title if note else "",
                note_error="" if note else "Could not resolve document title.",
            )
        )
    return tuple(views)
