"""The task detail page: its view models and the reads that fill them.

Split out of ``tasks.py`` when T1-S3's attention model pushed it past the
800-line god-module ceiling. The seam follows the surfaces: ``tasks.py`` keeps
the records and the DASHBOARD view models, and everything that exists only for
``/tasks/{id}`` — the blocker chain, the hierarchy, the provenance links, the
findings timeline, the reopen report, the per-section degraded states of a
single task — lives here.

T1-S7 rebased the page onto the 0.4 task graph: it now loads from
``lithos_task_get`` + ``lithos_task_status`` + ``lithos_task_edge_list`` +
``lithos_task_children`` + ``lithos_finding_list`` instead of scanning the
three status lists for the task. That is why the module sits in the TaskGraph
component (``docs/architecture.toml``) rather than Tasks: it consumes the graph
records. The dependency still runs one way — this module imports the records,
neither ``tasks.py`` nor ``task_graph.py`` imports back.

The mechanism half — resolving related tasks from edges, and the three bounds
that keep that fan-out safe — lives next door in ``task_links.py``, because
T1-S8 expands the blocker chain through the SAME helper. What is left here is
the page: its view models, the findings timeline, and the assembly that gathers
the reads and decides what each section says when one of them does not answer.

Every list on the page — blockers, provenance both directions, children, and
the findings timeline — renders a bounded FIRST PAGE plus a tail
(:data:`~lithos_lens.task_links.DETAIL_PAGE_SIZE`), every per-row lookup runs
under one shared concurrency gate
(:data:`~lithos_lens.task_links.DETAIL_FANOUT_CONCURRENCY`), and the whole set
of reads runs under one wall-clock budget
(:data:`~lithos_lens.task_links.DETAIL_RENDER_BUDGET_S`). Bounded work, bounded
contention, bounded time: an agent-controlled count can make a section
incomplete — which it says — but not a render expensive.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from lithos_lens.task_graph import EdgeRecord
from lithos_lens.task_links import (
    BLOCKER_EDGE_TYPES,
    DETAIL_FANOUT_CONCURRENCY,
    Breadcrumb,
    LinkPage,
    deadline_or_budget,
    last_page,
    link_page_from_tasks,
    load_link_page,
    load_parent_chain,
    select_edges,
    task_type_badge,
    until,
    until_or,
)
from lithos_lens.tasks import (
    REOPENED_FINDING_PREFIX,
    FindingRecord,
    NoteRecord,
    SectionState,
    TaskRecord,
    TaskStatusRecord,
)

PROVENANCE_EDGE_TYPE = "discovered_from"

# Lithos answers a missing task with an error envelope whose ``code`` the
# client re-raises on the exception. Matched structurally rather than by
# importing ``LithosToolError``: this module is Foundation and must not import
# the client (the layering contract in pyproject.toml), the same constraint
# ``epic_strip._is_open_epic`` works under. Telling not-found apart from a
# failed read matters here — one renders the not-found panel, the other must
# not claim a task was deleted because Lithos hiccuped.
TASK_NOT_FOUND_CODE = "task_not_found"


class TaskDetailClientProtocol(Protocol):
    """The subset of the Lithos client this page's loaders consume.

    Declared here (rather than shared with the dashboard's protocol in
    ``tasks.py``) so the graph reads the detail rebase added stay with the
    surface that needs them; the full client surface lives on
    ``lithos_lens.lithos_client.LithosClientProtocol``, which satisfies this
    structurally.
    """

    async def task_get(self, task_id: str) -> TaskRecord: ...

    async def task_status(self, task_id: str) -> TaskStatusRecord | None: ...

    async def task_edge_list(
        self,
        task_id: str,
        *,
        direction: str = "both",
        types: list[str] | None = None,
    ) -> list[EdgeRecord]: ...

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

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None: ...


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
    # The NEWEST page of the findings timeline, oldest-first within the page
    # (§5.6). ``findings_older`` is how many older findings it collapses — the
    # tail states it, exactly like every other list on this page.
    findings: tuple[FindingView, ...] = ()
    findings_older: int = 0
    # Derived from the WHOLE findings list, not from the page above: the marker
    # needs no note lookup, so paging the timeline must not make a reopen
    # invisible on a task with a long history.
    reopen_report: FindingView | None = None
    # Level-1 blockers (§5.5.2 text baseline). T1-S8 hangs its per-level
    # expander off these rows and pages each deeper level the same way.
    blockers: LinkPage = LinkPage()
    breadcrumb: Breadcrumb = Breadcrumb()
    children: LinkPage = LinkPage()
    # ``discovered_from`` provenance, both directions: what this task was
    # discovered while working on, and the follow-ons it spawned.
    discovered_from: LinkPage = LinkPage()
    spawned: LinkPage = LinkPage()
    status_state: SectionState = SectionState.OK
    findings_state: SectionState = SectionState.OK
    not_found: bool = False
    errors: tuple[str, ...] = ()

    @property
    def type_badge(self) -> str:
        return task_type_badge(self.task)

    @property
    def blocked(self) -> bool:
        return not self.blockers.is_empty

    @property
    def resolution(self) -> bool:
        """Whether there is anything to report under Resolution."""
        return bool(self.task and (self.task.resolved_at or self.task.outcome))


def latest_reopen_report(findings: Sequence[FindingRecord]) -> FindingView | None:
    """The most recent reopen report on a task, if any.

    The ``[Reopened]`` finding is the only durable record of a reopen — and an
    unauthenticated one, so the view carries the REPORTING AGENT and the header
    attributes the claim to them instead of stating a lifecycle reversal as
    fact (see :data:`~lithos_lens.tasks.REOPENED_FINDING_PREFIX`).

    Scanned over EVERY finding rather than over the rendered page: the marker
    costs no round trip, so the page bound must not turn a long timeline into a
    false negative. A findings load failure yields None ("unknown"), never a
    false negative claim.
    """
    for finding in sorted(findings, key=lambda item: item.created_at, reverse=True):
        view = FindingView(finding=finding)
        if view.is_reopen:
            return view
    return None


async def load_task_detail(
    lithos: TaskDetailClientProtocol,
    task_id: str,
) -> TaskDetailData:
    """Load everything ``/tasks/{id}`` renders, in three waves.

    Wave 1 identifies the task (``lithos_task_get``) — an unknown id answers
    the ``task_not_found`` envelope, which becomes the not-found panel rather
    than an HTTP 500 (§5.5); any OTHER failure is reported as a failed read,
    not as a missing task. Wave 2 gathers the four independent per-task reads.
    Wave 3 resolves what those reads named: the link lists, the breadcrumb and
    the timeline's note titles.

    EVERY per-row fan-out in wave 3 is bounded, because every list it feeds is
    paged by :data:`DETAIL_PAGE_SIZE` before anything is looked up — blockers,
    provenance both ways, children, and the findings timeline. Nothing here
    scales with an agent-controlled count: the ceiling is four pages of
    ``task_get`` plus one page of ``lithos_read``, plus the separately bounded
    parent walk (:data:`PARENT_CHAIN_MAX_DEPTH`). The four wave-2 reads and the
    ``list_findings`` behind the timeline are single round trips whose
    responses can still be long; that cost is O(N) local parsing, not N round
    trips. One gate is shared by every wave-3 lookup, so the render — not each
    list — is what :data:`DETAIL_FANOUT_CONCURRENCY` bounds.
    """
    # ONE deadline for the whole render, taken before the FIRST read: all three
    # waves share it, so the page's wall clock is the budget — not the budget
    # per wave, and not a per-call timeout spent identifying the task PLUS the
    # budget. A read that overruns it lands in the same branch as a read that
    # failed, so every section already knows what to render.
    deadline = deadline_or_budget()
    try:
        task = await until(deadline, lithos.task_get(task_id))
    except Exception as exc:
        # TimeoutError included, and it is NOT a missing task: only the coded
        # envelope may claim that.
        if getattr(exc, "code", "") == TASK_NOT_FOUND_CODE:
            return TaskDetailData(task=None, not_found=True)
        return TaskDetailData(
            task=None, errors=("Could not load this task from Lithos.",)
        )

    errors: list[str] = []
    (
        status_result,
        edges_result,
        findings_result,
        children_result,
    ) = await asyncio.gather(
        until(deadline, lithos.task_status(task_id)),
        until(deadline, lithos.task_edge_list(task_id, direction="both")),
        until(deadline, lithos.list_findings(task_id)),
        until(deadline, lithos.task_children(task_id, include_closed=True)),
        return_exceptions=True,
    )

    task_status: TaskStatusRecord | None = None
    status_state = SectionState.OK
    if isinstance(status_result, BaseException):
        status_state = SectionState.ERROR
        errors.append("Could not load active claims.")
    else:
        task_status = cast(TaskStatusRecord | None, status_result)

    # One gate for the whole render: the lists below are bounded individually
    # by their page size, and jointly by this.
    gate = asyncio.Semaphore(DETAIL_FANOUT_CONCURRENCY)

    finding_views: tuple[FindingView, ...] = ()
    findings_older = 0
    reopen_report: FindingView | None = None
    findings_state = SectionState.OK
    if isinstance(findings_result, BaseException):
        findings_state = SectionState.ERROR
        errors.append("Could not load findings.")
    else:
        findings = cast(list[FindingRecord], findings_result)
        finding_views, findings_older = await resolve_finding_notes(
            lithos, findings, gate=gate, deadline=deadline
        )
        reopen_report = latest_reopen_report(findings)

    if isinstance(children_result, BaseException):
        errors.append("Could not load child tasks.")
        children = LinkPage(state=SectionState.ERROR)
    else:
        children = link_page_from_tasks(cast(list[TaskRecord], children_result))

    # One edge list feeds three sections and the breadcrumb, so a failed edge
    # read degrades all four together — each to ERROR rather than to an empty
    # list, which would read as "nothing blocks this, and it came from nowhere".
    blockers = discovered_from = spawned = LinkPage(state=SectionState.ERROR)
    breadcrumb = Breadcrumb()
    if isinstance(edges_result, BaseException):
        errors.append("Could not load task relations.")
    else:
        edges = cast(list[EdgeRecord], edges_result)
        # Each loader applies the render's deadline itself and degrades to the
        # state its section already renders, so one slow list degrades alone
        # instead of taking the sections that did answer down with it.
        blockers, discovered_from, spawned, breadcrumb = await asyncio.gather(
            load_link_page(
                lithos,
                select_edges(edges, direction="incoming", types=BLOCKER_EDGE_TYPES),
                gate=gate,
                deadline=deadline,
            ),
            load_link_page(
                lithos,
                select_edges(
                    edges, direction="incoming", types=(PROVENANCE_EDGE_TYPE,)
                ),
                gate=gate,
                deadline=deadline,
            ),
            load_link_page(
                lithos,
                select_edges(
                    edges, direction="outgoing", types=(PROVENANCE_EDGE_TYPE,)
                ),
                gate=gate,
                deadline=deadline,
            ),
            load_parent_chain(lithos, task, edges, gate=gate, deadline=deadline),
        )

    return TaskDetailData(
        task=task,
        task_status=task_status,
        findings=finding_views,
        findings_older=findings_older,
        reopen_report=reopen_report,
        blockers=blockers,
        breadcrumb=breadcrumb,
        children=children,
        discovered_from=discovered_from,
        spawned=spawned,
        status_state=status_state,
        findings_state=findings_state,
        errors=tuple(errors),
    )


async def load_findings_timeline(
    lithos: TaskDetailClientProtocol,
    task_id: str,
) -> TaskDetailData:
    """Just the findings timeline — what the ``/tasks/{id}/findings`` fragment
    renders, and nothing else.

    ``findings.html`` reads only ``findings``, ``findings_older`` and
    ``findings_state``, so running the full :func:`load_task_detail` for it
    would buy the whole graph fan-out — the edge list, the children, four pages
    of ``task_get`` and the parent walk — and then discard every result
    unrendered. A fragment is the cheapest thing to request and the easiest to
    request in a loop, so it must not be the most expensive thing to serve.

    The loop is real and it is named: ``refreshFindings`` in
    ``static/tasks.js`` requests this endpoint whenever a ``finding.posted``
    event names the open task, and ``lithos_finding_post`` takes
    ``{task_id, agent, summary}`` with no credential — so whoever posts
    findings sets how often this runs, from outside Lens entirely. Which is why
    the event refreshes THIS and NOT the page: the client marks the event
    handled and skips the whole-page reconcile, rather than firing both. What
    this fragment still costs on each run — one ``lithos_finding_list`` plus up
    to :data:`DETAIL_PAGE_SIZE` title reads — is why the client floors its rate
    too (``FINDINGS_MIN_INTERVAL_MS``): cheaper than the page is not the same
    as free, and only a floor stops an event rate from becoming a render rate.

    It carries the same wall-clock budget as the full page, for the same
    reason: the reconcile keeps asking, so a stalled Lithos must not leave a
    request per tick held open.
    """
    deadline = deadline_or_budget()
    try:
        findings = await until(deadline, lithos.list_findings(task_id))
    except (Exception, TimeoutError):
        return TaskDetailData(
            task=None,
            findings_state=SectionState.ERROR,
            errors=("Could not load findings.",),
        )
    views, older = await resolve_finding_notes(lithos, findings, deadline=deadline)
    return TaskDetailData(
        task=None,
        findings=views,
        findings_older=older,
        reopen_report=latest_reopen_report(findings),
    )


async def resolve_finding_notes(
    lithos: TaskDetailClientProtocol,
    findings: Sequence[FindingRecord],
    *,
    gate: asyncio.Semaphore | None = None,
    deadline: float | None = None,
) -> tuple[tuple[FindingView, ...], int]:
    """The newest page of the timeline, with its knowledge-link titles.

    The findings count is agent-controlled in exactly the way the edge count
    is — ``lithos_finding_post`` takes ``{task_id, agent, summary,
    knowledge_id}`` with no credential and no per-task cap, and
    ``lithos_finding_list`` takes no limit — and each DISTINCT
    ``knowledge_id`` costs one ``lithos_read`` ROUND TRIP on the shared MCP
    session. So this list is bounded like every other one on the page: only
    :data:`DETAIL_PAGE_SIZE` findings are rendered (the NEWEST ones — §5.6
    collapses older history, and a reopen keeps its marker either way via
    :func:`latest_reopen_report`), the remainder is returned for the tail to
    state, and the title lookups for that page run concurrently under the
    render's gate instead of one after another.

    Two further economies on the lookups: ids are de-duplicated across the
    page (one read serves every finding citing the same document), and the
    read is ``max_length=1`` — frontmatter comes back complete (§6.3), so a
    title never pulls a whole note body. Same call as the related panel's
    title fan-out (``knowledge._resolve_titles``).

    ``deadline`` is the render's shared budget (:data:`DETAIL_RENDER_BUDGET_S`
    when called on its own). Overrunning it costs the TITLES only — the
    timeline still renders, each unresolved row falling back to the "View
    document" label it already shows for a document it could not read.
    """
    gate = gate or asyncio.Semaphore(DETAIL_FANOUT_CONCURRENCY)
    deadline = deadline_or_budget(deadline)
    page, older = last_page(sorted(findings, key=lambda item: item.created_at))
    # dict.fromkeys: de-duplicate the cited documents, keep first-cited order.
    cited = tuple(dict.fromkeys(f.knowledge_id for f in page if f.knowledge_id))
    # Only the TITLES are given up when the budget runs out: the timeline
    # itself is already in hand, and every row has a label to fall back on.
    titles = await until_or(deadline, _resolve_note_titles(lithos, cited, gate), {})
    views: list[FindingView] = []
    for finding in page:
        if not finding.knowledge_id:
            views.append(FindingView(finding=finding))
            continue
        title = titles.get(finding.knowledge_id, "")
        views.append(
            FindingView(
                finding=finding,
                note_title=title,
                note_error="" if title else "Could not resolve document title.",
            )
        )
    return tuple(views), older


async def _resolve_note_titles(
    lithos: TaskDetailClientProtocol,
    knowledge_ids: Sequence[str],
    gate: asyncio.Semaphore,
) -> dict[str, str]:
    """Title per readable knowledge id; missing when the read failed."""
    titles = await asyncio.gather(
        *(
            _read_note_title(lithos, knowledge_id, gate)
            for knowledge_id in knowledge_ids
        )
    )
    return {
        knowledge_id: title
        for knowledge_id, title in zip(knowledge_ids, titles, strict=True)
        if title
    }


async def _read_note_title(
    lithos: TaskDetailClientProtocol,
    knowledge_id: str,
    gate: asyncio.Semaphore,
) -> str:
    """One gated, body-free title read; empty string for ANY failure.

    A finding may cite a document that was never written, or that the reader
    cannot see — the timeline says so per row (the "View document" fallback)
    rather than failing the section.
    """
    try:
        async with gate:
            note = await lithos.read_note(knowledge_id, max_length=1)
    except Exception:
        return ""
    return note.title if note else ""
