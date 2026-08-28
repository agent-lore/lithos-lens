"""Related-task links on the task detail page: paged, gated and deadlined.

The mechanism half of ``/tasks/{id}``. ``task_detail.py`` assembles the page;
this module owns every read that fans OUT from it — one ``lithos_task_get`` per
related task — and the three bounds that make that fan-out safe on a session
every request shares:

* :data:`DETAIL_PAGE_SIZE` — how much work a render may do. Edge counts are
  agent-controlled, so the page size, not the edge count, decides how many
  lookups happen. :func:`first_page` is the only place that decision is made.
* :data:`DETAIL_FANOUT_CONCURRENCY` — how much of it may be in flight.
* :data:`DETAIL_RENDER_BUDGET_S` — how long it may take, via :func:`until`.

It is a module rather than a section of ``task_detail`` because it is the
SHARED helper: T1-S8 expands the blocker chain one level at a time and must
page each level the same way — :func:`load_blocker_page` is the entry point for
a blocker set at any level, and gets the page size, the gate, the deadline and
the tail (``templates/tasks/paging.html``) by calling it rather than by
copying a number.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from typing import Protocol

from lithos_lens.task_graph import EdgeRecord
from lithos_lens.tasks import SectionState, TaskRecord


class TaskLinkClientProtocol(Protocol):
    """The narrow client surface the link loaders need.

    Declared here rather than imported from ``task_detail`` so the dependency
    runs one way: ``task_detail`` imports this module, never the reverse. Its
    wider ``TaskLinkClientProtocol`` satisfies this structurally, as does the
    real client.
    """

    async def task_get(self, task_id: str) -> TaskRecord: ...

    async def task_edge_list(
        self,
        task_id: str,
        *,
        direction: str = "both",
        types: list[str] | None = None,
    ) -> list[EdgeRecord]: ...


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
# ``templates/tasks/paging.html``), never silently dropped: a truncated
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


# Wall-clock budget for ONE render's reads, from the first fan-out read to the
# last. The count bounds above cap how MUCH work a render does; this caps how
# LONG it may take to fail at it — the two are different properties, and only
# the second is what an operator waiting on the page experiences. Without it a
# Lithos that answers /health but stalls on tool calls (the health probe is a
# separate HTTP endpoint with its own timeout, so it does not notice) turns a
# bounded call count into a minutes-long render: every batch of the gated
# fan-out would wait out ``lithos_client.CALL_TIMEOUT_S`` in turn.
#
# Deliberately larger than that per-call ceiling, so one slow call degrades one
# section rather than the page. Past this the page stops waiting and renders
# what it has: each unfinished section falls to the degraded state it already
# knows how to show (a list says "unavailable", a row says "status unknown"),
# which is more use to an operator than a held request.
DETAIL_RENDER_BUDGET_S = 20.0


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


# The CLOSED server-side type vocabularies, clamped for RENDERING the way
# ``normalizers.normalize_task`` already clamps ``status`` to ``TASK_STATUSES``.
# ``lithos_task_create`` declares both: a task is ``task``/``epic``/``gate``,
# and a gate REQUIRES ``metadata.gate_type`` in
# human/timer/ci/pr/external_task (verified in
# ``tests/contracts/_tools_snapshot.json``).
#
# Both halves are agent-controlled with no credential — ``lithos_task_create``
# takes ``task_type`` as a bare string, and ``lithos_task_update`` merges
# ``metadata`` per key, so ``gate_type`` can be set on any existing task with
# one call that touches nothing else and leaves nothing missing for an operator
# to notice. Clamping only one of them just moves the forgery: a task whose
# ``task_type`` is the literal ``"gate: human"`` would otherwise render the
# byte-identical badge a real human gate does, on the header AND on every
# blocker row — i.e. on the surface an operator reads to decide why a task is
# stuck and whether a human is holding it.
TASK_TYPES = ("task", "epic", "gate")

GATE_TYPES = ("human", "timer", "ci", "pr", "external_task")

# What the badge says for a type outside :data:`TASK_TYPES`. Neutral on
# purpose: it must not be mistakable for a vocabulary value, and it must not be
# the raw string, because a badge cannot be both verbatim and trustworthy.
# ``TaskRecord.task_type`` keeps the raw value either way — normalization does
# not rewrite it, so an unknown future type still round-trips through Lens.
UNKNOWN_TASK_TYPE_BADGE = "unknown type"


def task_type_badge(task: TaskRecord | None) -> str:
    """The header/type badge text: ``task``, ``epic``, or ``gate: human``.

    Every part of this string comes from a closed vocabulary, so no part of it
    is chosen by whoever created the task: the type is clamped to
    :data:`TASK_TYPES` and an unrecognized one reads as
    :data:`UNKNOWN_TASK_TYPE_BADGE`; a gate's kind is clamped to
    :data:`GATE_TYPES` and an unrecognized or missing one leaves a bare
    ``gate``. Autoescaping makes an agent's value safe to RENDER; that is not
    the same as safe to BELIEVE, and this badge sits beside the live status on
    the "why can't this run" surface.
    """
    if task is None:
        return ""
    if task.task_type not in TASK_TYPES:
        return UNKNOWN_TASK_TYPE_BADGE
    if task.task_type != "gate":
        return task.task_type
    gate_type = str(task.metadata.get("gate_type") or "")
    return f"gate: {gate_type}" if gate_type in GATE_TYPES else "gate"


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
    (``templates/tasks/paging.html``) so an operator can see that more
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


def deadline_or_budget(deadline: float | None = None) -> float:
    """The caller's deadline, or a fresh :data:`DETAIL_RENDER_BUDGET_S` one.

    The single place a budget becomes a deadline, so every loader below is
    bounded whether it is called as part of a page render or on its own — T1-S8
    expanding one deeper level of the chain gets a budget by default, exactly
    as it gets the page size and the gate.

    Deadlines are ABSOLUTE, so this cannot be used to escape an outer one: a
    default taken later is always later than the render's, so the render's
    still fires first. Passing one down is what makes the waves SHARE a budget
    instead of each getting its own.
    """
    if deadline is not None:
        return deadline
    return asyncio.get_running_loop().time() + DETAIL_RENDER_BUDGET_S


async def until[T](deadline: float, awaitable: Awaitable[T]) -> T:
    """Await ``awaitable`` until ``deadline``, then give up on it.

    One mechanism for the whole render: an absolute deadline rather than a
    per-await timeout, so the budget covers the SUM of the waves instead of
    being spent again by each of them. Expiry raises ``TimeoutError``, which
    every caller here already handles the way it handles a failed read.
    """
    async with asyncio.timeout_at(deadline):
        return await awaitable


async def until_or[T](deadline: float, awaitable: Awaitable[T], degraded: T) -> T:
    """:func:`until`, answering with ``degraded`` instead of raising.

    For the sections whose loaders never raise: they report their own failure
    as a value, so a timeout has to arrive as one too.
    """
    try:
        return await until(deadline, awaitable)
    except TimeoutError:
        return degraded


def first_page[T](items: Sequence[T]) -> tuple[tuple[T, ...], int]:
    """Split ``items`` into the first page and how many were left off.

    The single place the pagination decision lives: every list on this page
    (and every deeper blocker level T1-S8 expands) reaches its bound through
    here, so the page size and the remainder count cannot drift apart between
    call sites.

    The size is deliberately NOT a parameter — not here and not on the loaders
    below. A bound that a caller can pass its way past is a default, not a
    bound: T1-S8 could then page a deeper level at any size without touching
    :data:`DETAIL_PAGE_SIZE` or failing any test of it. Changing the page size
    means changing the constant, in one place, under review.
    """
    return tuple(items[:DETAIL_PAGE_SIZE]), max(len(items) - DETAIL_PAGE_SIZE, 0)


def last_page[T](items: Sequence[T]) -> tuple[tuple[T, ...], int]:
    """The LAST page of ``items`` (order preserved) and how many precede it.

    :func:`first_page` turned around, for the one list whose interesting end is
    the newest: a findings timeline keeps its most recent entries and collapses
    the older ones (§5.6), where a blocker list keeps the ones it can show
    first. Both reach the same constant through the same function, so there is
    still exactly one page-size decision.
    """
    page, remaining = first_page(tuple(reversed(items)))
    return tuple(reversed(page)), remaining


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


def link_page_from_tasks(tasks: Sequence[TaskRecord]) -> LinkPage:
    """Page a list of ALREADY-LOADED tasks (the children table).

    ``lithos_task_children`` answers with whole records, so this page needs no
    per-row lookup — but it still goes through :func:`first_page`, because the
    number of children is as agent-controlled as the number of blockers and the
    tail must state the remainder either way.
    """
    page, remaining = first_page(tasks)
    return LinkPage(
        links=tuple(TaskLink(task_id=task.id, task=task) for task in page),
        remaining=remaining,
    )


async def load_link_page(
    lithos: TaskLinkClientProtocol,
    edges: Sequence[EdgeRecord],
    *,
    gate: asyncio.Semaphore | None = None,
    deadline: float | None = None,
) -> LinkPage:
    """Resolve the first page of an edge set into links with live status.

    The fan-out this bounds is real work, not bookkeeping: ONE
    ``lithos_task_get`` ROUND TRIP per rendered link, all of them queued on the
    process-wide MCP session this page shares with every other request. That is
    categorically more than the ``lithos_task_edge_list`` call that produced
    ``edges`` — that is a single round trip returning N rows, plus O(N) local
    parsing. So the count is capped at :data:`DETAIL_PAGE_SIZE` (whatever the
    edge count is), the remainder is reported by the tail, and at most
    :data:`DETAIL_FANOUT_CONCURRENCY` of the lookups are in flight at once.

    All three bounds are applied HERE rather than by the caller — the count,
    the gate, and the deadline (:func:`deadline_or_budget` when none is passed
    down) — so a caller cannot get the page without them.

    A failed lookup degrades to an unresolved link rather than failing the
    page: the operator still learns that the blocker exists. A page that runs
    out of budget degrades whole, to the state the section already renders as
    "unavailable": a partial list would read as a complete one.
    """
    return await until_or(
        deadline_or_budget(deadline),
        _resolve_page(
            lithos, edges, gate or asyncio.Semaphore(DETAIL_FANOUT_CONCURRENCY)
        ),
        LinkPage(state=SectionState.ERROR),
    )


async def _resolve_page(
    lithos: TaskLinkClientProtocol,
    edges: Sequence[EdgeRecord],
    gate: asyncio.Semaphore,
) -> LinkPage:
    targets = linked_tasks(edges)
    page, remaining = first_page(tuple(targets))
    links = await asyncio.gather(
        *(_resolve_link(lithos, task_id, targets[task_id], gate) for task_id in page)
    )
    return LinkPage(links=tuple(links), remaining=remaining)


async def load_blocker_page(
    lithos: TaskLinkClientProtocol,
    task_id: str,
    *,
    gate: asyncio.Semaphore | None = None,
    deadline: float | None = None,
) -> LinkPage:
    """One task's level-1 blockers as a bounded page — at ANY level.

    The entry point for a blocker set the caller does not already hold edges
    for: it reads the incoming ``blocks``/``waits_on_gate`` edges and hands
    them to :func:`load_link_page`, so a deeper level of the chain (T1-S8's
    HTMX expander) is paged, bounded and tailed exactly like level 1 — by
    calling this, not by reimplementing it.

    That includes the wall clock. BOTH of its awaits are under one deadline —
    the caller's when a render passes one down, otherwise a fresh budget — so
    expanding a level standalone cannot spend a per-call timeout on the edge
    read and another on the lookups behind it.

    A failed or overrunning read degrades the section to ERROR: an empty
    blocker list would read as "nothing is blocking this task", which is a
    claim this call cannot support.
    """
    deadline = deadline_or_budget(deadline)
    try:
        edges = await until(
            deadline,
            lithos.task_edge_list(
                task_id, direction="incoming", types=list(BLOCKER_EDGE_TYPES)
            ),
        )
    except Exception:
        # TimeoutError included: an overrun is a read that did not answer.
        return LinkPage(state=SectionState.ERROR)
    return await load_link_page(lithos, edges, gate=gate, deadline=deadline)


async def load_parent_chain(
    lithos: TaskLinkClientProtocol,
    task: TaskRecord,
    edges: Sequence[EdgeRecord],
    *,
    gate: asyncio.Semaphore | None = None,
    deadline: float | None = None,
) -> Breadcrumb:
    """Walk incoming ``parent_child`` edges up to the root epic.

    Hierarchy is a single-parent forest (``parent_exists`` upstream), so this
    is a chain rather than a DAG walk: one parent per level, root last. It
    stops — and says so via ``truncated`` — at :data:`PARENT_CHAIN_MAX_DEPTH`,
    on a ``parent_child`` cycle (the visited set; the graph does not forbid
    one), on a failed read, and on the deadline. Each level costs two
    SEQUENTIAL round trips, which is why the depth bound is small and why the
    walk needs the wall-clock bound as well as the depth one.
    """
    return await until_or(
        deadline_or_budget(deadline),
        _walk_parents(
            lithos, task, edges, gate or asyncio.Semaphore(DETAIL_FANOUT_CONCURRENCY)
        ),
        # A walk that ran out of budget stopped early, exactly like one that hit
        # the depth bound: say so rather than implying the task has no parent.
        Breadcrumb(truncated=True),
    )


async def _walk_parents(
    lithos: TaskLinkClientProtocol,
    task: TaskRecord,
    edges: Sequence[EdgeRecord],
    gate: asyncio.Semaphore,
) -> Breadcrumb:
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


async def _resolve_link(
    lithos: TaskLinkClientProtocol,
    task_id: str,
    edge_type: str,
    gate: asyncio.Semaphore,
) -> TaskLink:
    task = await _get_task(lithos, task_id, gate)
    return TaskLink(task_id=task_id, task=task, edge_type=edge_type)


async def _get_task(
    lithos: TaskLinkClientProtocol,
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
