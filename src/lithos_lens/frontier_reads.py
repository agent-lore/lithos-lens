"""One generation of the dashboard's related reads, and how a retry replaces it.

The open list, the ready and blocked frontiers and the two resolved windows are
independent Lithos reads, not a snapshot: issued together, they can still
contradict each other (a task in both frontiers, a frontier row the open read
never saw, an id both open and resolved). ``frontier.load_dashboard`` joins
them, and when the join finds such a contradiction below the frontier limit it
retries ALL FIVE reads once. The policy that matters is that the board renders
ONE generation: adopting a fresh open/ready/blocked triple beside the first
generation's resolved windows would be exactly the mixed generation that loses
a row that moved between them (§14).

:class:`ReadGeneration` is that bundle — the five raw results of one issue —
with the views the join reads off it, and :meth:`ReadGeneration.adopt` is the
one place a retry is accepted: whole, and only when every read of it answered.
The join, the sections and the degraded-mode policy stay in ``frontier``; this
module owns what a generation IS and when it is replaced.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Protocol, cast

from lithos_lens.task_graph import BlockedTaskRecord
from lithos_lens.tasks import TaskRecord

# A skewed read could not be re-read (see ``ReadGeneration.adopt``). Reported
# rather than swallowed: the dashboard's affirmative "All systems healthy"
# stripe is gated on this error channel, and a retry triggered by terminal
# overlap alone leaves no other trace — the board would otherwise claim health
# while rendering one task in both an open section and a terminal one.
RETRY_FAILED_ERROR = "Could not re-read the task graph to reconcile a skewed read."

__all__ = [
    "RETRY_FAILED_ERROR",
    "GenerationReader",
    "ReadGeneration",
    "read_generation",
]


class GenerationReader(Protocol):
    """The three reads a generation issues itself; the caller supplies the two
    resolved-window reads, bound to its own window arguments."""

    async def list_tasks(
        self,
        *,
        agent: str | None = ...,
        status: str | None = ...,
        tags: list[str] | None = ...,
        since: str | None = ...,
        resolved_since: str | None = ...,
        with_claims: bool = ...,
    ) -> list[TaskRecord]: ...

    async def task_ready(
        self, *, limit: int, with_claims: bool = ...
    ) -> list[TaskRecord]: ...

    async def task_blocked(self, *, limit: int) -> list[BlockedTaskRecord]: ...


@dataclass(frozen=True)
class ReadGeneration:
    """The five related reads of one issue, raw: each slot holds the rows or
    the exception that read raised. The views below are what the join reads;
    they are derived, never assigned, so no view can outlive its generation.
    """

    open_result: list[TaskRecord] | BaseException
    ready_result: list[TaskRecord] | BaseException
    blocked_result: list[BlockedTaskRecord] | BaseException
    completed_result: list[TaskRecord] | BaseException
    cancelled_result: list[TaskRecord] | BaseException

    @property
    def closed_results(self) -> tuple[list[TaskRecord] | BaseException, ...]:
        """The resolved windows in display order (completed, cancelled)."""
        return (self.completed_result, self.cancelled_result)

    @property
    def complete(self) -> bool:
        """Every read answered — the only generation a retry may hand over."""
        return (
            not any(
                isinstance(result, BaseException)
                for result in (
                    self.open_result,
                    self.ready_result,
                    *self.closed_results,
                )
            )
            and self.blocked_ok
        )

    @property
    def frontier_ok(self) -> bool:
        """Both frontier reads answered, so the board has a classification."""
        return self.blocked_ok and not isinstance(self.ready_result, BaseException)

    @property
    def blocked_ok(self) -> bool:
        """Tracked apart from ``frontier_ok``: the gate waiter counts depend on
        the blocked read alone, so a ready-only outage must not degrade them —
        and "0 waiters" built on a blocked read that never answered would be a
        fabricated fact rather than a degraded one."""
        return not isinstance(self.blocked_result, BaseException)

    @cached_property
    def open_snapshot(self) -> list[TaskRecord]:
        """The whole open list, newest first; empty when the read failed.

        Unfiltered on purpose: it is the authority on which tasks are open and
        it resolves blocker-chip titles for predecessors the sections hide.
        """
        if isinstance(self.open_result, BaseException):
            return []
        return sorted(self.open_result, key=lambda task: task.created_at, reverse=True)

    @property
    def ready_rows(self) -> list[TaskRecord]:
        return [] if isinstance(self.ready_result, BaseException) else self.ready_result

    @property
    def blocked_rows(self) -> list[BlockedTaskRecord]:
        if isinstance(self.blocked_result, BaseException):
            return []
        return self.blocked_result

    @cached_property
    def terminal_index(self) -> dict[str, TaskRecord]:
        """Every resolved row that answered, by id — pre-filter.

        Two uses, one pass: an id in both the open snapshot and here is
        freshness skew worth a retry, whether or not the row survives the
        section filters; and the titles name blockers that resolved inside the
        window (a cancelled predecessor is exactly what the loudest attention
        rule fires on, and it is by definition absent from the open snapshot).
        """
        index: dict[str, TaskRecord] = {}
        for result in self.closed_results:
            if not isinstance(result, BaseException):
                index.update({task.id: task for task in result})
        return index

    def record_failures(self, errors: list[str]) -> None:
        """One error line per open / frontier read that did not answer.

        The resolved windows report theirs where their rows are drawn
        (``frontier._rows_for``), so each failed read is named exactly once.
        """
        if isinstance(self.open_result, BaseException):
            errors.append("Could not load open tasks.")
        if isinstance(self.ready_result, BaseException):
            errors.append("Could not load the ready frontier.")
        if not self.blocked_ok:
            errors.append("Could not load the blocked frontier.")

    def adopt(self, retried: ReadGeneration, *, errors: list[str]) -> ReadGeneration:
        """The generation the board renders after a skew retry.

        The retried generation WHOLE when every read of it answered; otherwise
        this one, unchanged — a complete first generation beats a mix of it
        with a partial second — and recorded, because without the line the
        stripe could call healthy a board whose skew was never resolved.
        """
        if retried.complete:
            return retried
        errors.append(RETRY_FAILED_ERROR)
        return self


async def read_generation(
    lithos: GenerationReader,
    *,
    frontier_limit: int,
    completed: Awaitable[list[TaskRecord]],
    cancelled: Awaitable[list[TaskRecord]],
) -> ReadGeneration:
    """Issue the five reads of one generation together.

    ``completed`` / ``cancelled`` are the caller's resolved-window reads,
    already bound to its ``resolved_since`` / claims arguments, so a retry
    re-asks each read exactly as the first generation did.
    """
    results = await asyncio.gather(
        lithos.list_tasks(status="open", with_claims=True),
        lithos.task_ready(limit=frontier_limit, with_claims=False),
        lithos.task_blocked(limit=frontier_limit),
        completed,
        cancelled,
        return_exceptions=True,
    )
    return ReadGeneration(*cast("tuple[Any, ...]", results))
