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
    read_gate_edges,
)
from lithos_lens.task_graph import BlockedTaskRecord, EdgeRecord
from lithos_lens.tasks import TaskRecord

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
    degraded reads already made to the Gates section, which reuses them rather
    than reading the same gate twice.
    """

    waiting: Mapping[str, frozenset[str]] = field(default_factory=dict)
    included: tuple[TaskRecord, ...] = ()
    incomplete: bool = False
    edges: Mapping[str, Sequence[EdgeRecord]] = field(default_factory=dict)
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
    the open gates that fail the filters and that no surviving blocked row
    already ties to the scope are candidates: they are read in snapshot order,
    at most ``fanout_cap`` minus the gates already in scope (those keep first
    claim on the budget, for their own waiter counts in the Gates section).
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
    edges: dict[str, Sequence[EdgeRecord]] = {}
    reads = 0
    unresolved: list[str] = []
    if not blocked_available or blocked_truncated:
        in_scope = waited_on_gates(snapshot, scoped_ids=visible_ids, waiting=waiting)
        settled = visible_ids | {gate.id for gate in in_scope}
        candidates = [
            task.id
            for task in snapshot
            if task.task_type == GATE_TASK_TYPE and task.id not in settled
        ]
        own = sum(1 for task in visible if task.task_type == GATE_TASK_TYPE)
        budget = max(fanout_cap - own - len(in_scope), 0)
        results = await read_gate_edges(lithos, candidates[:budget])
        reads = len(results)
        for gate_id, result in results.items():
            if isinstance(result, BaseException):
                unresolved.append(gate_id)
                continue
            edges[gate_id] = result
            waiting.setdefault(gate_id, set()).update(
                edge.to_task_id for edge in result if edge.to_task_id in workable
            )
        unresolved.extend(candidates[budget:])
    frozen = {gate_id: frozenset(ids) for gate_id, ids in waiting.items()}
    included = waited_on_gates(snapshot, scoped_ids=visible_ids, waiting=frozen)
    return GateWaits(
        waiting=frozen,
        included=tuple(included),
        incomplete=bool(unresolved),
        edges=edges,
        reads=reads,
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
