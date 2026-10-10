"""Which open gates a scoped board shows: the gate's own match, or a waiter's.

Gates do not carry their waiters' story tags — loom's ``pr`` gates carry none,
its ``human`` gates only ``project:<slug>`` and ``needs-human``, a hand-made
gate whatever the operator typed — so the per-row filter alone drops exactly
the gate an in-scope task is waiting on, and ``?tag=roadmap-x`` read "No open
gates match these filters" over a board full of gated work. The rule (Dave,
2026-10-09): **an open gate is in scope when it passes the filters itself, or
when a task in scope waits on it.** Lens applies it here rather than copying
story tags onto gates, which would miss hand-made gates, go stale on a re-tag,
and risk copying ``trigger:*`` tags.

One map of "who waits on which gate" serves every gate surface of a render —
the Gates section, the Needs-attention gate promotion, the gates tile and the
project strip — so the board agrees with itself. It costs no call on a healthy
render: the blocked frontier (``lithos_task_blocked``) already names each
blocked task's gate blockers. Only when that read is truncated or failed can it
miss a waiter, and then the gates' own outgoing ``waits_on_gate`` edges are
read — the degraded path the Gates section already uses for its counts, under
the same per-render cap (``GATE_WAITER_FANOUT_CAP``), shared with the section's
own reads so one render never issues more than that many. A candidate gate the
cap or a failed read leaves unread may be one an in-scope task waits on, so the
board then says the Gates list may be incomplete rather than under-showing in
silence.

Open gates only: the terminal sections keep the per-row rule.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from lithos_lens.frontier_join import WORKABLE_TASK_TYPE
from lithos_lens.gates import (
    GATE_TASK_TYPE,
    GATE_WAITER_FANOUT_CAP,
    GateEdgeClient,
    blocked_waiter_ids,
    collect_gates,
    read_gate_edges,
)
from lithos_lens.task_graph import BlockedTaskRecord, EdgeRecord
from lithos_lens.tasks import TaskRecord

# Stands in for a gate the cap left unread: as unknown as one whose read failed.
_UNREAD = LookupError("not read: past the per-render edge-read cap")

GATES_INCOMPLETE_ERROR = (
    "The Gates list may be incomplete: the blocked frontier was truncated or "
    "unavailable, and Lens could not verify which other gates the tasks in "
    "this view wait on."
)


@dataclass(frozen=True)
class GateWaits:
    """Who waits on which open gate, as far as this render could learn.

    ``waiting`` maps a gate id to the WORKABLE open task ids waiting on it — the
    blocked frontier's gate relations, plus the edges read on the degraded path.
    ``included`` are the open gates that fail the filters themselves but that a
    task in scope waits on; ``incomplete`` says a candidate gate's waiters could
    not be read, so the list may be short. ``edges`` and ``reads`` hand the
    degraded reads already made — answers and failures alike — to the Gates
    section, which reuses them rather than reading the same gate twice.
    """

    waiting: Mapping[str, frozenset[str]] = field(default_factory=dict)
    included: tuple[TaskRecord, ...] = ()
    incomplete: bool = False
    edges: Mapping[str, Sequence[EdgeRecord] | BaseException] = field(
        default_factory=dict
    )
    reads: int = 0

    @property
    def reads_left(self) -> int:
        """What the per-render edge-read budget still allows the Gates section."""
        return GATE_WAITER_FANOUT_CAP - self.reads

    @property
    def errors(self) -> tuple[str, ...]:
        """The banner line this step contributes: a gap is said, never hidden."""
        return (GATES_INCOMPLETE_ERROR,) if self.incomplete else ()


async def load_gate_waits(
    lithos: GateEdgeClient,
    snapshot: Sequence[TaskRecord],
    *,
    visible: Sequence[TaskRecord],
    blocked: Sequence[BlockedTaskRecord],
    blocked_available: bool,
    blocked_truncated: bool,
    enabled: bool = True,
    fanout_cap: int = GATE_WAITER_FANOUT_CAP,
) -> GateWaits:
    """Learn the gate waits behind this render, and the gates they bring in.

    ``snapshot`` is the WHOLE open list and ``visible`` the rows passing the
    filters. A complete blocked read answers everything with no call. Otherwise
    EVERY degraded edge read of the render is made here, at most ``fanout_cap``
    of them, so the project strip and the Gates section count from one map:
    first the gates that pass on their own (in the section's order — their
    waiter counts), then those a surviving blocked row already ties to the
    scope (the truncated response may have dropped some of their waiters, and
    the strip counts by waiters' projects), then every other open gate in
    snapshot order (the candidates the scope may include). A gate outside the
    filters left unread or unanswered makes the result ``incomplete``.
    ``enabled`` is false when the open side is hidden: no gate surface renders,
    so nothing is learned and nothing is read.
    """
    if not enabled:
        return GateWaits()
    workable = {task.id for task in snapshot if task.task_type == WORKABLE_TASK_TYPE}
    waiting: dict[str, set[str]] = {
        gate_id: waiter_ids & workable
        for gate_id, waiter_ids in blocked_waiter_ids(
            blocked if blocked_available else ()
        ).items()
    }
    visible_ids = {task.id for task in visible}
    edges: Mapping[str, Sequence[EdgeRecord] | BaseException] = {}
    unresolved: list[str] = []
    if not blocked_available or blocked_truncated:
        own = [row.task.id for row in collect_gates(visible)]
        tied = [
            gate.id
            for gate in waited_on_gates(
                snapshot, scoped_ids=visible_ids, waiting=waiting
            )
        ]
        settled = visible_ids | set(tied)
        candidates = [
            task.id
            for task in snapshot
            if task.task_type == GATE_TASK_TYPE and task.id not in settled
        ]
        order = [*own, *tied, *candidates]
        edges = await read_gate_edges(lithos, order[: max(fanout_cap, 0)])
        for gate_id, result in edges.items():
            if not isinstance(result, BaseException):
                waiting.setdefault(gate_id, set()).update(
                    edge.to_task_id for edge in result if edge.to_task_id in workable
                )
        own_ids = set(own)
        unresolved = [
            gate_id
            for gate_id in order
            if gate_id not in own_ids
            and isinstance(edges.get(gate_id, _UNREAD), BaseException)
        ]
    frozen = {gate_id: frozenset(ids) for gate_id, ids in waiting.items()}
    included = waited_on_gates(snapshot, scoped_ids=visible_ids, waiting=frozen)
    return GateWaits(
        waiting=frozen,
        included=tuple(included),
        incomplete=bool(unresolved),
        edges=edges,
        reads=len(edges),
    )


def waited_on_gates(
    snapshot: Sequence[TaskRecord],
    *,
    scoped_ids: Collection[str],
    waiting: Mapping[str, Collection[str]],
) -> list[TaskRecord]:
    """The open gates outside ``scoped_ids`` that a row inside it waits on.

    Pure, and shared by every scope a render evaluates — the board's, and the
    project strip's (every filter but ``project``) — so each surface applies
    the one rule to its own scope. Snapshot order is kept.
    """
    return [
        task
        for task in snapshot
        if task.task_type == GATE_TASK_TYPE
        and task.id not in scoped_ids
        and any(waiter in scoped_ids for waiter in waiting.get(task.id, ()))
    ]
