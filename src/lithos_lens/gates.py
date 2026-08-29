"""The Gates section: open gates, their waiters, and the timer self-refresh.

Gates (``task_type="gate"``) are open tasks Lithos keeps out of both workable
frontiers, so they never reach the ready/blocked join in ``frontier.py`` — they
are collected straight off the master open list into their own section
(REQUIREMENTS §5.2.3), grouped by gate type with human gates first.

Everything here treats gate data as what it is: peer-written. The waiter count
prefers the Lithos-COMPUTED blocked frontier and only falls back to the gate's
own ``waits_on_gate`` edges (bounded fan-out, labelled unverified) when that
frontier cannot back the number; advisory metadata is bounded on every axis
before it reaches a row; and an unparseable or overflowing ``ready_at`` drives
no countdown instead of raising.
"""

from __future__ import annotations

import asyncio
import heapq
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from lithos_lens.frontier_join import WORKABLE_TASK_TYPE
from lithos_lens.task_graph import BlockedTaskRecord, EdgeRecord
from lithos_lens.tasks import TaskRecord, parse_timestamp

# Gates are open tasks too, but they gate rather than get worked: Lithos keeps
# them out of both frontiers, so they are collected off the master open list
# into their own section.
GATE_TASK_TYPE = "gate"

# The edge type linking a gate to the tasks waiting on it (gate -> waiter).
WAITS_ON_GATE_EDGE = "waits_on_gate"

# Gate types Lithos accepts in ``metadata.gate_type`` on a ``gate`` task. The
# raw server string is kept (an unknown future type still renders its badge
# TEXT); only ``human`` (the work waiting on a person, sorted first) and
# ``timer`` (the countdown + self-refresh) get dedicated dashboard treatment.
KNOWN_GATE_TYPES = frozenset({"human", "timer", "ci", "pr", "external_task"})
HUMAN_GATE_TYPE = "human"
TIMER_GATE_TYPE = "timer"

# Gate metadata keys that already have dedicated row chrome, so they are not
# repeated as advisory chips: the gate type badge, the timer countdown, and
# the project convention key (rendered as the row's project chip and driving
# the project filter, §5B.1).
_GATE_CHROME_KEYS = frozenset({"gate_type", "ready_at", "project"})

# Advisory keys/values are summarized on the row; the full table is the detail
# page. Both axes are bounded because gate ``metadata`` is peer-written.
_ADVISORY_VALUE_CAP = 80
_ADVISORY_ROW_LIMIT = 3

# Bounds on the degraded per-gate waiter fan-out. Internal constants,
# deliberately NOT public config (the pattern ``knowledge.RELATED_RENDER_CAP``
# sets): the PRD specifies no gate dial, and these are safety nets against a
# peer-chosen gate count, not operator knobs.
#
# ``GATE_WAITER_FANOUT_CAP`` bounds calls per render and matches the repo's
# other per-render fan-out page (``task_links`` renders 25 children/blockers),
# so one dashboard costs at most one detail page's worth of extra reads;
# ``_GATE_EDGE_CONCURRENCY`` bounds how many are in flight, because
# LithosClient holds ONE MCP session for the whole process and a burst here
# would contend with every other surface.
GATE_WAITER_FANOUT_CAP = 25
_GATE_EDGE_CONCURRENCY = 8

WAITERS_UNAVAILABLE_ERROR = (
    "Could not load waiter counts for some gates; "
    "their counts are shown as unavailable."
)


class GateEdgeClient(Protocol):
    """The one client method the degraded waiter fan-out needs.

    Narrower than ``frontier.FrontierLithosClient`` on purpose: this module
    reads edges and nothing else.
    """

    async def task_edge_list(
        self,
        task_id: str,
        *,
        direction: str = "both",
        types: list[str] | None = None,
    ) -> list[EdgeRecord]: ...


class GateWaiterState(StrEnum):
    """How far a gate's "blocks N tasks" count can be trusted.

    The two waiter sources differ in authority, so the row says which one it
    got: ``KNOWN`` is the Lithos-computed blocked frontier (it derives blockers
    itself, and the response was complete); ``UNVERIFIED`` is the gate's own
    ``waits_on_gate`` edges — complete, but an edge is a peer-written
    *assertion* that a task waits, which nothing cross-checked because the
    blocked frontier was truncated or unavailable; ``PARTIAL`` is a truncated
    blocked response ("at least N"); ``UNKNOWN`` is neither source available —
    rendered as unavailable, never as a confident zero.
    """

    KNOWN = "known"
    UNVERIFIED = "unverified"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class GateRow:
    """An open gate rendered in the dashboard's Gates section (§5.2.3).

    Gates are open tasks Lithos excludes from both workable frontiers, so they
    are collected straight off the master open list rather than joined onto it.
    ``gate_type`` is the raw ``metadata.gate_type`` (see KNOWN_GATE_TYPES) and
    decides grouping and ordering; ``type_label`` is the length-capped badge
    TEXT and ``type_slug`` the closed vocabulary the MARKUP (class name, data
    attribute) is built from, so a hostile or malformed server value cannot
    inject extra CSS classes or selector hooks.

    ``waiters`` are the open tasks waiting on this gate ("blocks N tasks",
    expandable on the row) and ``waiters_state`` records how far that count can
    be trusted (see :class:`GateWaiterState`) — a degraded read is labelled,
    never rendered as a confident zero. ``advisory`` is the gate's remaining
    metadata — type-specific advisory keys Lithos does not interpret —
    summarized verbatim on the row, with ``advisory_more`` counting the keys
    left for the detail page's full table.

    ``ready_at`` is a timer gate's ``metadata.ready_at`` normalized to a UTC
    ISO stamp — the browser countdown parses it, and a naive stamp read as
    local time there would count down to the wrong instant. An unparseable
    value passes through verbatim rather than being dropped.
    """

    task: TaskRecord
    gate_type: str = ""
    ready_at: str = ""
    waiters: tuple[TaskRecord, ...] = ()
    waiters_state: GateWaiterState = GateWaiterState.KNOWN
    advisory: tuple[tuple[str, str], ...] = ()
    advisory_more: int = 0

    @property
    def waiting(self) -> int:
        return len(self.waiters)

    @property
    def waiters_unknown(self) -> bool:
        return self.waiters_state is GateWaiterState.UNKNOWN

    @property
    def waiters_label(self) -> str:
        """The waiter count as the row states it, qualified by its source.

        Kept out of the template so the honesty of each degraded phrasing is
        unit-testable: an unavailable count must never read as "blocks 0".
        """
        if self.waiters_state is GateWaiterState.UNKNOWN:
            return "waiter count unavailable"
        tasks = f"{self.waiting} task{'' if self.waiting == 1 else 's'}"
        if self.waiters_state is GateWaiterState.PARTIAL:
            return f"blocks at least {tasks}"
        if self.waiters_state is GateWaiterState.UNVERIFIED:
            return f"blocks {tasks} (unverified)"
        return f"blocks {tasks}"

    @property
    def waiters_empty_note(self) -> str:
        """What an EMPTY waiter list means — which the source decides.

        Only a complete source can say "nothing waits on this gate". A
        truncated one (``PARTIAL``) saw part of the picture, so it can say only
        that no waiter appeared in the part it read; a missing one
        (``UNKNOWN``) cannot say anything. Kept beside ``waiters_label`` so
        both phrasings stay unit-testable.

        The only remedy offered for a truncated read is the limit. The board's
        project/tag/agent filters are NOT one: the waiter sources are fetched
        unfiltered on purpose (``load_dashboard`` pushes no filter to
        ``task_blocked``, so a gate's count stays whole whatever the board is
        scoped to), which means narrowing them cannot change what Lithos
        returned or make the truncated source complete.
        """
        if self.waiters_state is GateWaiterState.UNKNOWN:
            return (
                "Lithos did not answer which tasks wait on this gate. Retry on refresh."
            )
        if self.waiters_state is GateWaiterState.PARTIAL:
            return (
                "No waiter appeared in the part of the blocked frontier Lens "
                "could read; more may exist. Raise [tasks].frontier_limit to "
                "see the rest."
            )
        return "No open tasks are waiting on this gate."

    @property
    def is_human(self) -> bool:
        return self.gate_type == HUMAN_GATE_TYPE

    @property
    def is_timer(self) -> bool:
        return self.gate_type == TIMER_GATE_TYPE

    @property
    def type_label(self) -> str:
        """The badge TEXT: the raw type as text, "gate" when unset.

        Already bounded — ``collect_gates`` passes ``gate_type`` through
        ``_chrome_text`` — so this only supplies the wording for a gate whose
        ``metadata.gate_type`` is missing.
        """
        return self.gate_type or "gate"

    @property
    def type_slug(self) -> str:
        """The gate type as a markup-safe token from the known vocabulary.

        Anything outside KNOWN_GATE_TYPES (including a value carrying spaces or
        quotes) collapses to ``unknown``: ``metadata`` is peer-writable, and
        interpolating it into a class list would let a ``ci`` gate wear the
        human-gate accent — or hide itself with a borrowed utility class.
        """
        return self.gate_type if self.gate_type in KNOWN_GATE_TYPES else "unknown"


@dataclass(frozen=True)
class GateGroup:
    """One gate-type group in the Gates section (§5.2.3: grouped by gate type).

    ``gate_type`` is the raw shared type of the group's rows (``label`` for
    display, ``type_slug`` for markup); ``rows`` are its gates, oldest first.
    """

    gate_type: str
    rows: tuple[GateRow, ...]

    @property
    def label(self) -> str:
        return self.gate_type or "unspecified"

    @property
    def type_slug(self) -> str:
        return self.rows[0].type_slug if self.rows else "unknown"

    @property
    def is_human(self) -> bool:
        return self.gate_type == HUMAN_GATE_TYPE


@dataclass(frozen=True)
class GateSection:
    """The assembled Gates section, as ``load_gates`` hands it to the board.

    ``groups`` is the rendered structure (human first); ``next_ready_at`` is
    the one-shot self-refresh instant; ``errors`` are the banner lines this
    section contributes to the page's shared error list.
    """

    groups: tuple[GateGroup, ...] = ()
    next_ready_at: str = ""
    errors: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return sum(len(group.rows) for group in self.groups)


async def load_gates(
    lithos: GateEdgeClient,
    *,
    visible_open: Sequence[TaskRecord],
    index: Mapping[str, TaskRecord],
    blocked: Sequence[BlockedTaskRecord],
    blocked_available: bool,
    blocked_truncated: bool,
    placed_ids: frozenset[str] = frozenset(),
    now: datetime,
) -> GateSection:
    """Assemble the Gates section from the reads ``load_dashboard`` already has.

    ``visible_open`` is the filtered open snapshot the sections render, so the
    board's scope decides WHICH gates appear; ``index`` is the WHOLE open
    snapshot, so a gate's waiter count is never narrowed by that same scope.
    ``placed_ids`` are rows some other section already rendered (Needs
    attention promotes a long-waiting human gate out of here) — the
    single-placement rule, enforced at the one point that can see both.
    """
    gates = collect_gates(visible_open, placed_ids=placed_ids)
    if not gates:
        return GateSection()
    gates = await attach_gate_waiters(
        lithos,
        gates,
        index=index,
        blocked=blocked,
        blocked_available=blocked_available,
        blocked_truncated=blocked_truncated,
    )
    return GateSection(
        groups=group_gates(gates),
        next_ready_at=next_gate_ready_at(gates, now=now),
        errors=(
            (WAITERS_UNAVAILABLE_ERROR,)
            if any(gate.waiters_unknown for gate in gates)
            else ()
        ),
    )


def collect_gates(
    open_tasks: Sequence[TaskRecord],
    *,
    placed_ids: frozenset[str] = frozenset(),
) -> tuple[GateRow, ...]:
    """Collect the open gates off the master open list (§5.2.3).

    Pure and waiter-free: waiter identities need the blocked frontier (or, on
    the degraded paths, a per-gate edge read), which ``attach_gate_waiters``
    supplies. Rows come back human-first then oldest first; ``group_gates``
    turns that order into the rendered type groups.
    """
    rows: list[GateRow] = []
    for task in open_tasks:
        if task.task_type != GATE_TASK_TYPE or task.id in placed_ids:
            continue
        advisory, advisory_more = _advisory_metadata(task.metadata)
        rows.append(
            GateRow(
                task=task,
                gate_type=_chrome_text(task.metadata.get("gate_type")),
                ready_at=_normalized_instant(
                    _chrome_text(task.metadata.get("ready_at"))
                ),
                advisory=advisory,
                advisory_more=advisory_more,
            )
        )
    return tuple(sorted(rows, key=lambda row: (not row.is_human, _age_key(row.task))))


def _age_key(task: TaskRecord) -> tuple[bool, str]:
    """Sort key for "oldest first" that puts an UNKNOWN stamp last.

    ``normalize_task`` defaults a missing ``created_at`` to ``""``, and the
    empty string sorts before every real timestamp — so sorting on the raw
    field reads a gate the server never stamped as the one that has waited
    longest. ``created_at`` is peer-written like every other field this section
    renders, and "oldest first" is the only ordering claim it makes, so an
    absent stamp must not be able to take the front of the queue.
    """
    return (not task.created_at, task.created_at)


def group_gates(gates: Sequence[GateRow]) -> tuple[GateGroup, ...]:
    """Group gate rows by gate type — human first, oldest first within a group.

    Ordering between the non-human groups follows the same "oldest first"
    principle as the rows: a group is placed by its oldest gate, so the type
    that has been waiting longest leads. Grouping is by the RAW type, so an
    unknown type stays its own group rather than being merged into a bucket.
    """
    groups: dict[str, list[GateRow]] = {}
    for gate in gates:
        groups.setdefault(gate.gate_type, []).append(gate)
    ordered = [
        GateGroup(
            gate_type=gate_type,
            rows=tuple(sorted(rows, key=lambda row: _age_key(row.task))),
        )
        for gate_type, rows in groups.items()
    ]
    # `rows[0]` is now the group's oldest KNOWN stamp when it has one, so a
    # group with a mix places by the gate that really has been waiting; a group
    # with none at all still sorts last, which is the honest answer.
    ordered.sort(key=lambda group: (not group.is_human, _age_key(group.rows[0].task)))
    return tuple(ordered)


async def attach_gate_waiters(
    lithos: GateEdgeClient,
    gates: Sequence[GateRow],
    *,
    index: Mapping[str, TaskRecord],
    blocked: Sequence[BlockedTaskRecord],
    blocked_available: bool,
    blocked_truncated: bool,
    fanout_cap: int = GATE_WAITER_FANOUT_CAP,
) -> tuple[GateRow, ...]:
    """Fill each gate's waiter list, preferring the Lithos-computed source.

    Two sources exist and they are not equally trustworthy. The blocked
    frontier is *computed* by Lithos (it derives each task's blockers itself),
    so when that response came back and did not hit its limit it is both
    complete and authoritative — waiters are read straight off it and NO
    per-gate call is made at all. A ``waits_on_gate`` edge, by contrast, is a
    peer-written assertion that some task waits — though per the
    ``lithos_task_edge_upsert`` contract an ACCEPTED one constitutes the wait
    rather than merely claiming it. It is unbounded (so it is the only source
    that survives frontier truncation) and nothing independently confirms it,
    which is why edge-derived counts render as ``UNVERIFIED``; see
    ``_resolve_asserted_waiters`` for the two admissions applied to it and for
    why no frontier response is used to refute it.

    So the per-gate edge fan-out happens ONLY on the degraded paths (blocked
    truncated or unavailable), and even then it is bounded twice: at most
    ``fanout_cap`` gates get a read, and no more than
    ``_GATE_EDGE_CONCURRENCY`` are in flight — the gate count comes off the
    unbounded open list, i.e. it is peer-controlled, and this runs on every
    dashboard render. Gates past the cap, and gates whose read fails, degrade
    to the blocked-response fallback (``PARTIAL`` — "at least N"); when that
    source is unavailable too the row reports ``UNKNOWN`` rather than an
    invented zero.
    """
    if not gates:
        return ()
    waiting = _blocked_waiter_ids(blocked)

    def from_blocked(gate: GateRow, state: GateWaiterState) -> GateRow:
        if not blocked_available:
            return replace(gate, waiters=(), waiters_state=GateWaiterState.UNKNOWN)
        return replace(
            gate,
            waiters=_resolve_waiters(sorted(waiting.get(gate.task.id, ())), index),
            waiters_state=state,
        )

    if blocked_available and not blocked_truncated:
        return tuple(from_blocked(gate, GateWaiterState.KNOWN) for gate in gates)

    fanned, overflow = gates[:fanout_cap], gates[fanout_cap:]
    limit = asyncio.Semaphore(_GATE_EDGE_CONCURRENCY)

    async def read_edges(gate: GateRow) -> list[EdgeRecord]:
        async with limit:
            return await lithos.task_edge_list(
                gate.task.id, direction="outgoing", types=[WAITS_ON_GATE_EDGE]
            )

    results = await asyncio.gather(
        *(read_edges(gate) for gate in fanned), return_exceptions=True
    )
    attached: list[GateRow] = []
    for gate, result in zip(fanned, results, strict=True):
        if isinstance(result, BaseException):
            attached.append(from_blocked(gate, GateWaiterState.PARTIAL))
            continue
        # Order by the edge list, de-duped: a repeated edge must not
        # double-count the same waiter.
        waiter_ids = dict.fromkeys(
            edge.to_task_id for edge in result if edge.to_task_id
        )
        attached.append(
            replace(
                gate,
                waiters=_resolve_asserted_waiters(waiter_ids, index=index),
                waiters_state=GateWaiterState.UNVERIFIED,
            )
        )
    attached.extend(from_blocked(gate, GateWaiterState.PARTIAL) for gate in overflow)
    return tuple(attached)


def next_gate_ready_at(gates: Sequence[GateRow], *, now: datetime) -> str:
    """The earliest still-FUTURE timer-gate ``ready_at``, or "" when none.

    Lithos emits no event when a timer gate lapses (the gate simply stops
    blocking at query time), so the dashboard schedules its own one-shot
    refresh at this instant. Only future stamps qualify: a lapsed timer gate
    stays open until someone completes it, and handing the browser a past
    instant would have it refresh on a loop against an attribute that never
    changes.
    """
    upcoming = [
        parsed
        for gate in gates
        if gate.is_timer and (parsed := parse_timestamp(gate.ready_at)) and parsed > now
    ]
    return min(upcoming).isoformat() if upcoming else ""


def _resolve_waiters(
    waiter_ids: Iterable[str],
    index: Mapping[str, TaskRecord],
) -> tuple[TaskRecord, ...]:
    """Resolve COMPUTED waiter ids against the master open snapshot.

    These come off ``lithos_task_blocked``, which derives blockers itself, so
    the only thing to check is that the row is still visible: an id absent from
    the snapshot (it closed between the two independent reads) is dropped
    rather than surfaced as a row the operator cannot see.
    """
    return tuple(
        task for task_id in waiter_ids if (task := index.get(task_id)) is not None
    )


def _resolve_asserted_waiters(
    waiter_ids: Iterable[str],
    *,
    index: Mapping[str, TaskRecord],
) -> tuple[TaskRecord, ...]:
    """Resolve EDGE-asserted waiter ids into the rows the section can show.

    On the degraded paths this is the one waiter source nothing computed, and
    the ``lithos_task_edge_upsert`` contract is what makes it usable anyway: a
    ``waits_on_gate`` edge means "*to_task is not ready until the gate
    from_task is resolved*", so an accepted edge does not merely ASSERT the
    wait — it constitutes it. An open workable task the edge names is a waiter
    by definition, and Lens is in no position to overrule that.

    Two admissions remain, and neither is a refutation of the edge:

    - the id must be in the open snapshot. This is a renderability bound, not a
      verdict: the count and the expandable list under it must describe the
      same set (the list IS the count's evidence), and Lens cannot draw a row
      for a task it holds no record of — nor let an id naming nothing inflate
      the number;
    - the row must be WORKABLE. ``lithos_task_ready`` / ``lithos_task_blocked``
      return ``task``-typed rows only, so "blocks N tasks" counts the same
      domain the computed source does; an epic or another gate is structure,
      never a unit of work waiting to be picked up.

    There is deliberately NO cross-check against the ready frontier. It reads
    as though it would work — ready is Lithos saying nothing blocks a task —
    but the two reads are different generations in the WRONG order: ``ready``
    is fetched in the opening gather and the edge list strictly later, so an
    edge created in between is the newer fact and the ready response cannot
    speak to it. Excluding on it silently dropped a real waiter. Anything
    sound would need a same-or-later authoritative read per waiter, which is
    exactly the unbounded fan-out ``GATE_WAITER_FANOUT_CAP`` exists to prevent
    — so the row states its source instead (``UNVERIFIED``) and counts what
    the edge says.
    """
    return tuple(
        task
        for task_id in waiter_ids
        if (task := index.get(task_id)) is not None
        and task.task_type == WORKABLE_TASK_TYPE
    )


def _blocked_waiter_ids(
    blocked: Sequence[BlockedTaskRecord],
) -> dict[str, set[str]]:
    """Waiter ids per blocking node id, read off the blocked frontier.

    Only gate ids are ever looked up, so plain predecessors in the same map are
    harmless.
    """
    waiting: dict[str, set[str]] = {}
    for record in blocked:
        for blocker in record.blockers:
            if blocker.task_id:
                waiting.setdefault(blocker.task_id, set()).add(record.task.id)
    return waiting


def _advisory_metadata(
    metadata: Mapping[str, Any],
) -> tuple[tuple[tuple[str, str], ...], int]:
    """The gate's advisory metadata for the row, plus the count left over.

    Type-specific keys (``provider``, ``repo``, ``pr_number``,
    ``approval_required_from``, …) are advisory — Lithos does not interpret
    them and neither does Lens, so they are rendered verbatim, key-sorted for a
    stable row. ``metadata`` is peer-written and unbounded in every direction,
    so the row summary is bounded in every direction too: at most
    ``_ADVISORY_ROW_LIMIT`` pairs are built (the rest are counted for the
    "+N more" link), and both key and value are length-capped. The full table
    is the detail page's job.
    """
    keys = [key for key in metadata if key not in _GATE_CHROME_KEYS]
    # nsmallest, not sorted(): the row needs the first few keys in a stable
    # order, and fully sorting a peer-sized metadata dict on every render is
    # work the row never uses.
    shown = tuple(
        (_summarize_text(key), _summarize_value(metadata[key]))
        for key in heapq.nsmallest(_ADVISORY_ROW_LIMIT, keys)
    )
    return shown, len(keys) - len(shown)


def _chrome_text(value: Any) -> str:
    """A chrome metadata value (``gate_type``, ``ready_at``) as bounded text.

    ``metadata`` is peer-written whatever the key, so the two keys that drive
    dedicated row chrome get the same bounding contract as the advisory chips
    — applied BEFORE the value is interpreted, since both end up in the markup
    (the badge; the countdown's two attributes). ``_summarize_value`` is reused
    rather than ``str()`` so that a container is labelled instead of
    materialised: neither key can be a container meaningfully, and stringifying
    a nested megabyte to keep 80 bytes of it is the allocation this module
    refuses everywhere else. A falsy value is the absent case and reads as "".
    """
    return _summarize_value(value) if value else ""


def _summarize_text(text: str) -> str:
    if len(text) <= _ADVISORY_VALUE_CAP:
        return text
    return text[: _ADVISORY_VALUE_CAP - 1] + "…"


def _summarize_value(value: Any) -> str:
    """One advisory value, summarized without materializing the whole object.

    Scalars are stringified and capped; a container is NOT stringified at all —
    ``str()`` on a multi-megabyte nested value would allocate the whole thing on
    every render just to keep 80 bytes. A pathologically long integer is
    refused for the same reason (CPython caps int->str conversion and raises).
    """
    if isinstance(value, str):
        return _summarize_text(value)
    if isinstance(value, dict):
        return "{…}"
    if isinstance(value, list | tuple):
        return "[…]"
    try:
        return _summarize_text(str(value))
    except ValueError:
        # int -> str beyond CPython's digit limit; the detail page can show it.
        return "…"


def _normalized_instant(value: str) -> str:
    """Normalize a gate's ``ready_at`` to a UTC ISO stamp for the browser.

    An unparseable value (including one whose UTC shift overflows the datetime
    domain, ``9999-12-31T23:59:59-01:00``) passes through as bounded TEXT: the
    countdown ignores it, but the raw metadata stays visible rather than
    silently disappearing — and the page renders instead of 500ing.

    Bounded, not verbatim, because this is the one gate field that reaches the
    row markup TWICE (``datetime=`` and ``data-gate-ready-at=``, neither of
    them the truncated visible text) and is then re-read by the browser's
    once-a-second countdown loop. A real instant is ~25 characters; anything
    longer is not one, and the summarised form says so without pasting a
    peer-sized string into every render of every open tab. Callers pass a value
    that ``_chrome_text`` has already bounded — this keeps the guarantee local
    to the function that publishes the attribute.
    """
    parsed = parse_timestamp(value)
    return parsed.isoformat() if parsed is not None else _summarize_text(value)
