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

Every list of RELATED tasks on the page (blockers, provenance both directions,
children) is rendered as a bounded FIRST PAGE plus a tail; see
:data:`DETAIL_PAGE_SIZE` and :func:`load_link_page`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from lithos_lens.task_graph import EdgeRecord
from lithos_lens.tasks import (
    REOPENED_FINDING_PREFIX,
    FindingRecord,
    NoteRecord,
    SectionState,
    TaskRecord,
    TaskStatusRecord,
)

# How many related tasks each detail-page list RENDERS, and — for the lists
# that resolve their rows one ``lithos_task_get`` at a time — how many of those
# lookups a single render may issue.
#
# THE ONE page-size constant for this page. Every list goes through
# :func:`first_page`, so blockers, provenance and children share this bound,
# and T1-S8's deeper blocker levels get it by calling
# :func:`load_blocker_page` rather than by copying a number.
#
# The bound exists because the edge count is agent-controlled: Lithos enforces
# no maximum number of edges on a task, so a buggy agent minting blockers in a
# loop would otherwise turn one page render into one round trip per edge. It is
# a PAGE SIZE, not a claim about how many blockers a task may legitimately
# have — the remainder is counted and stated in the tail (see
# ``templates/tasks/link_list.html``), never silently dropped: a truncated
# blocker list on a "why can't this task run?" page is worse than a slow one.
DETAIL_PAGE_SIZE = 25

# How many of a render's per-task lookups may be in flight at once. The page
# size bounds the TOTAL work; this bounds the CONTENTION — every call on this
# page shares one process-wide MCP session, so a page's worth of concurrent
# ``lithos_task_get`` calls would queue behind each other there anyway. One
# gate is created per render and passed to every loader below, so the bound is
# per rendered page, not per list. Same shape (and same reasoning) as
# ``epic_strip.EPIC_FANOUT_BATCH``.
DETAIL_FANOUT_CONCURRENCY = 8

# How far the parent breadcrumb walks before it stops and says so. Hierarchy is
# a single-parent forest, so the walk is a chain — but its length is
# agent-controlled like everything else in the graph, and each step costs TWO
# sequential round trips (``task_get`` + ``task_edge_list``), so it needs a
# stop. A cycle is caught separately by the visited set.
PARENT_CHAIN_MAX_DEPTH = 10

# Incoming edge types that mean "this task cannot run yet" (§5.5.2): an
# unfinished predecessor, or a gate it waits on.
BLOCKER_EDGE_TYPES = ("blocks", "waits_on_gate")
PARENT_EDGE_TYPE = "parent_child"
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
class TaskLink:
    """One related task rendered on the detail page, with its LIVE status.

    ``task`` is the record a per-link ``lithos_task_get`` resolved — ``None``
    when that lookup failed, which the row renders as the bare id plus "status
    unknown" rather than dropping the link (an unreadable blocker is still a
    reason the task cannot run). ``edge_type`` is the raw graph edge that
    produced the link, so the row can say HOW the two tasks relate; it is empty
    for links that come from a non-edge read (the children table).
    """

    task_id: str
    task: TaskRecord | None = None
    edge_type: str = ""

    @property
    def title(self) -> str:
        return self.task.title if self.task else self.task_id

    @property
    def status(self) -> str:
        return self.task.status if self.task else ""

    @property
    def status_label(self) -> str:
        return self.status or "status unknown"

    @property
    def resolved(self) -> bool:
        """Whether the linked task itself could be read (not its lifecycle)."""
        return self.task is not None

    @property
    def type_badge(self) -> str:
        return task_type_badge(self.task) if self.task else ""

    @property
    def relation_label(self) -> str:
        """The §5.5.2 text-baseline lead-in for a blocker line, else empty."""
        if self.edge_type == "blocks":
            return "blocked by"
        if self.edge_type == "waits_on_gate":
            return "waiting on gate"
        return ""


@dataclass(frozen=True)
class LinkPage:
    """One first-page-plus-tail slice of a related-task list.

    ``remaining`` is how many links the page left off — rendered as the tail
    (``templates/tasks/link_list.html``) so an operator can see that more
    exist and how many, which a silent truncation would not. ``state`` is
    ERROR when the read behind the list failed, so the section says
    "unavailable" instead of showing an empty list as if it were a fact.
    """

    links: tuple[TaskLink, ...] = ()
    remaining: int = 0
    state: SectionState = SectionState.OK

    @property
    def total(self) -> int:
        return len(self.links) + self.remaining

    @property
    def is_empty(self) -> bool:
        return not self.links and not self.remaining


@dataclass(frozen=True)
class Breadcrumb:
    """The parent chain above a task, root first.

    ``truncated`` marks a chain that stopped before the root — the depth bound,
    a ``parent_child`` cycle, or a failed read — so the breadcrumb can render a
    leading ellipsis instead of implying the first entry is the root.
    """

    ancestors: tuple[TaskRecord, ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class TaskDetailData:
    task: TaskRecord | None
    task_status: TaskStatusRecord | None = None
    findings: tuple[FindingView, ...] = ()
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


def task_type_badge(task: TaskRecord | None) -> str:
    """The header/type badge text: ``task``, ``epic``, or ``gate: human``.

    ``task_type`` is a raw server string (an unknown future type survives
    round-trip and is shown verbatim). Gates carry their kind in
    ``metadata.gate_type`` — Lithos requires it on creation — and a gate whose
    metadata omits it still reads as a gate rather than as a plain task.
    """
    if task is None:
        return ""
    if task.task_type != "gate":
        return task.task_type
    gate_type = str(task.metadata.get("gate_type") or "")
    return f"gate: {gate_type}" if gate_type else "gate"


def first_page[T](
    items: Sequence[T], *, page_size: int = DETAIL_PAGE_SIZE
) -> tuple[tuple[T, ...], int]:
    """Split ``items`` into the first page and how many were left off.

    The single place the pagination decision lives: every related-task list on
    this page (and every deeper blocker level T1-S8 expands) reaches its bound
    through here, so the page size and the remainder count cannot drift apart
    between call sites.
    """
    return tuple(items[:page_size]), max(len(items) - page_size, 0)


def select_edges(
    edges: Sequence[EdgeRecord], *, direction: str, types: Sequence[str]
) -> tuple[EdgeRecord, ...]:
    """The edges of ``types`` pointing ``direction`` relative to the focus task.

    Filtering happens Lens-side because the page fetches ``direction="both"``
    once (§5.5) and splits it into the blocker / hierarchy / provenance
    sections, rather than issuing one narrowed ``lithos_task_edge_list`` per
    section.
    """
    return tuple(
        edge for edge in edges if edge.direction == direction and edge.type in types
    )


def linked_tasks(edges: Sequence[EdgeRecord]) -> dict[str, str]:
    """Far endpoint -> the edge type that first named it, in edge order.

    The far endpoint is the OTHER task: the source of an incoming edge, the
    target of an outgoing one. De-duplication is not cosmetic — two edges can
    name the same task (a predecessor that both blocks a task and spawned it),
    and each surviving id costs one ``lithos_task_get``. One pass over a dict
    rather than a membership scan over a list: the edge count is
    agent-controlled, so the local reduction has to stay linear in it even
    though only a page of it is ever fetched.
    """
    targets: dict[str, str] = {}
    for edge in edges:
        far = edge.from_task_id if edge.direction == "incoming" else edge.to_task_id
        if far and far not in targets:
            targets[far] = edge.type
    return targets


def link_page_from_tasks(
    tasks: Sequence[TaskRecord], *, page_size: int = DETAIL_PAGE_SIZE
) -> LinkPage:
    """Page a list of ALREADY-LOADED tasks (the children table).

    ``lithos_task_children`` answers with whole records, so this page needs no
    per-row lookup — but it still goes through :func:`first_page`, because the
    number of children is as agent-controlled as the number of blockers and the
    tail must state the remainder either way.
    """
    page, remaining = first_page(tasks, page_size=page_size)
    return LinkPage(
        links=tuple(TaskLink(task_id=task.id, task=task) for task in page),
        remaining=remaining,
    )


async def load_link_page(
    lithos: TaskDetailClientProtocol,
    edges: Sequence[EdgeRecord],
    *,
    page_size: int = DETAIL_PAGE_SIZE,
    gate: asyncio.Semaphore | None = None,
) -> LinkPage:
    """Resolve the first page of an edge set into links with live status.

    The fan-out this bounds is real work, not bookkeeping: ONE
    ``lithos_task_get`` ROUND TRIP per rendered link, all of them queued on the
    process-wide MCP session this page shares with every other request. That is
    categorically more than the ``lithos_task_edge_list`` call that produced
    ``edges`` — that is a single round trip returning N rows, plus O(N) local
    parsing. So the count is capped at ``page_size`` (whatever the edge count
    is), the remainder is reported by the tail, and at most
    :data:`DETAIL_FANOUT_CONCURRENCY` of the lookups are in flight at once.

    A failed lookup degrades to an unresolved link rather than failing the
    page: the operator still learns that the blocker exists.
    """
    gate = gate or asyncio.Semaphore(DETAIL_FANOUT_CONCURRENCY)
    targets = linked_tasks(edges)
    page, remaining = first_page(tuple(targets), page_size=page_size)
    links = await asyncio.gather(
        *(_resolve_link(lithos, task_id, targets[task_id], gate) for task_id in page)
    )
    return LinkPage(links=tuple(links), remaining=remaining)


async def load_blocker_page(
    lithos: TaskDetailClientProtocol,
    task_id: str,
    *,
    page_size: int = DETAIL_PAGE_SIZE,
    gate: asyncio.Semaphore | None = None,
) -> LinkPage:
    """One task's level-1 blockers as a bounded page — at ANY level.

    The entry point for a blocker set the caller does not already hold edges
    for: it reads the incoming ``blocks``/``waits_on_gate`` edges and hands
    them to :func:`load_link_page`, so a deeper level of the chain (T1-S8's
    HTMX expander) is paged, bounded and tailed exactly like level 1 — by
    calling this, not by reimplementing it.

    A failed edge read degrades the section to ERROR: an empty blocker list
    would read as "nothing is blocking this task", which is a claim this call
    cannot support.
    """
    try:
        edges = await lithos.task_edge_list(
            task_id, direction="incoming", types=list(BLOCKER_EDGE_TYPES)
        )
    except Exception:
        return LinkPage(state=SectionState.ERROR)
    return await load_link_page(lithos, edges, page_size=page_size, gate=gate)


async def load_parent_chain(
    lithos: TaskDetailClientProtocol,
    task: TaskRecord,
    edges: Sequence[EdgeRecord],
    *,
    gate: asyncio.Semaphore | None = None,
) -> Breadcrumb:
    """Walk incoming ``parent_child`` edges up to the root epic.

    Hierarchy is a single-parent forest (``parent_exists`` upstream), so this
    is a chain rather than a DAG walk: one parent per level, root last. It
    stops — and says so via ``truncated`` — at :data:`PARENT_CHAIN_MAX_DEPTH`,
    on a ``parent_child`` cycle (the visited set; the graph does not forbid
    one), and on a failed read. Each level costs two SEQUENTIAL round trips,
    which is why the depth bound is small.
    """
    gate = gate or asyncio.Semaphore(DETAIL_FANOUT_CONCURRENCY)
    ancestors: list[TaskRecord] = []
    seen = {task.id}
    parent_id = _parent_id(edges)
    while parent_id and parent_id not in seen:
        if len(ancestors) >= PARENT_CHAIN_MAX_DEPTH:
            return Breadcrumb(_root_first(ancestors), truncated=True)
        seen.add(parent_id)
        parent = await _get_task(lithos, parent_id, gate)
        if parent is None:
            return Breadcrumb(_root_first(ancestors), truncated=True)
        ancestors.append(parent)
        try:
            async with gate:
                parent_edges = await lithos.task_edge_list(
                    parent.id, direction="incoming", types=[PARENT_EDGE_TYPE]
                )
        except Exception:
            return Breadcrumb(_root_first(ancestors), truncated=True)
        parent_id = _parent_id(parent_edges)
    # A non-empty id here means the loop stopped on the visited set: a cycle.
    return Breadcrumb(_root_first(ancestors), truncated=bool(parent_id))


async def load_task_detail(
    lithos: TaskDetailClientProtocol,
    task_id: str,
) -> TaskDetailData:
    """Load everything ``/tasks/{id}`` renders, in three bounded waves.

    Wave 1 identifies the task (``lithos_task_get``) — an unknown id answers
    the ``task_not_found`` envelope, which becomes the not-found panel rather
    than an HTTP 500 (§5.5); any OTHER failure is reported as a failed read,
    not as a missing task. Wave 2 gathers the four independent per-task reads.
    Wave 3 resolves the links the edge list named, sharing one concurrency gate
    across every list so the whole render — not each list separately — is
    bounded.
    """
    try:
        task = await lithos.task_get(task_id)
    except Exception as exc:
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
        # One gate for the whole render: the lists below are bounded
        # individually by their page size, and jointly by this.
        gate = asyncio.Semaphore(DETAIL_FANOUT_CONCURRENCY)
        blockers, discovered_from, spawned, breadcrumb = await asyncio.gather(
            load_link_page(
                lithos,
                select_edges(edges, direction="incoming", types=BLOCKER_EDGE_TYPES),
                gate=gate,
            ),
            load_link_page(
                lithos,
                select_edges(
                    edges, direction="incoming", types=(PROVENANCE_EDGE_TYPE,)
                ),
                gate=gate,
            ),
            load_link_page(
                lithos,
                select_edges(
                    edges, direction="outgoing", types=(PROVENANCE_EDGE_TYPE,)
                ),
                gate=gate,
            ),
            load_parent_chain(lithos, task, edges, gate=gate),
        )

    return TaskDetailData(
        task=task,
        task_status=task_status,
        findings=finding_views,
        blockers=blockers,
        breadcrumb=breadcrumb,
        children=children,
        discovered_from=discovered_from,
        spawned=spawned,
        status_state=status_state,
        findings_state=findings_state,
        errors=tuple(errors),
    )


async def resolve_finding_notes(
    lithos: TaskDetailClientProtocol,
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


async def _resolve_link(
    lithos: TaskDetailClientProtocol,
    task_id: str,
    edge_type: str,
    gate: asyncio.Semaphore,
) -> TaskLink:
    task = await _get_task(lithos, task_id, gate)
    return TaskLink(task_id=task_id, task=task, edge_type=edge_type)


async def _get_task(
    lithos: TaskDetailClientProtocol,
    task_id: str,
    gate: asyncio.Semaphore,
) -> TaskRecord | None:
    """One gated ``lithos_task_get``; ``None`` for ANY failure.

    Not-found and transport failure are deliberately not told apart here: a
    link Lens cannot read renders the same way either way (the id plus "status
    unknown"), and Foundation cannot inspect the client's error codes.
    """
    try:
        async with gate:
            return await lithos.task_get(task_id)
    except Exception:
        return None


def _parent_id(edges: Sequence[EdgeRecord]) -> str:
    """The parent named by the first incoming ``parent_child`` edge, if any.

    Single-parent forest: a second incoming ``parent_child`` edge would be an
    upstream invariant violation, so the first is taken rather than branching
    the breadcrumb into a tree the page cannot render.
    """
    parents = select_edges(edges, direction="incoming", types=(PARENT_EDGE_TYPE,))
    return parents[0].from_task_id if parents else ""


def _root_first(ancestors: Sequence[TaskRecord]) -> tuple[TaskRecord, ...]:
    """The walk collects parent-first; the breadcrumb reads root-first."""
    return tuple(reversed(ancestors))
