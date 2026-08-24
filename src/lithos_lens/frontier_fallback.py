"""The dashboard's no-frontier fallback: the flat render and its verdict.

Split out of ``frontier.py`` when the T1 slices pushed it past the 800-line
god-module ceiling. The seam is T1-S12's own subject: ``frontier.py`` owns the
happy path — the seven parallel reads and the ready/blocked join — while
everything that decides *there is no usable frontier right now*, and what to
render instead, lives here.

One condition produces the flat render: a frontier read that did not answer.
That covers an outage and, since the pre-0.4 detection was withdrawn (2026-08-24,
REQUIREMENTS §4.1), a server that never had those tools — Lens reports the
failed read rather than diagnosing a version it has no reason to guess at.
"""

from __future__ import annotations

from collections.abc import Awaitable, Sequence
from typing import Any, Protocol, cast

from lithos_lens.task_graph import BlockedTaskRecord
from lithos_lens.tasks import SectionName, SectionRow, TaskRecord


class FrontierProbeClient(Protocol):
    """The narrow client surface this module needs.

    Declared here rather than imported from ``frontier`` so the dependency runs
    one way: ``frontier`` imports the fallback, never the reverse. Its wider
    ``FrontierLithosClient`` satisfies this structurally.
    """

    async def task_ready(
        self,
        *,
        limit: int | None = None,
        with_claims: bool = False,
    ) -> list[TaskRecord]: ...

    async def task_blocked(
        self, *, limit: int | None = None
    ) -> list[BlockedTaskRecord]: ...


# A skewed read could not be re-read. Reported rather than swallowed: the
# dashboard's affirmative "All systems healthy" stripe is gated on this error
# channel, and a retry triggered by terminal overlap alone leaves no other
# trace — the board would otherwise claim health while rendering one task in
# both an open section and a terminal one.
RETRY_FAILED_ERROR = "Could not re-read the task graph to reconcile a skewed read."


def flat_open_sections(
    open_tasks: Sequence[TaskRecord],
) -> dict[SectionName, tuple[SectionRow, ...]]:
    """Partition open tasks with no usable frontier: one flat ``open`` section.

    Without the frontier there is no honest way to say ready vs blocked, so
    Lens says neither — every open row lands in one list with its claim chips
    (the 0.1.0 dashboard's behavior). No task-type filter either: the epic and
    gate treatments are graph features, and this is the board without one. The
    claims-unknown contract is unchanged: a row whose claims came back ``None``
    must still say "claims unknown" rather than a confident "unclaimed".
    """
    return {
        "open": tuple(
            SectionRow(
                task=task,
                claims=task.claims or (),
                claims_unknown=task.claims is None,
            )
            for task in open_tasks
        ),
        "in_progress": (),
        "ready": (),
        "blocked": (),
        "claims_unknown": (),
        "unclassified": (),
    }


def frontier_reads(
    lithos: FrontierProbeClient,
    *,
    frontier_limit: int,
) -> tuple[Awaitable[Any], Awaitable[Any]]:
    """The ready/blocked awaitables for one generation of the assembly."""
    return (
        lithos.task_ready(limit=frontier_limit, with_claims=False),
        lithos.task_blocked(limit=frontier_limit),
    )


def resolve_frontier(
    ready_result: Any,
    blocked_result: Any,
    *,
    errors: list[str],
) -> tuple[bool, list[TaskRecord], list[BlockedTaskRecord]]:
    """Read the two frontier responses into rows and error lines.

    Returns ``(frontier_ok, ready_rows, blocked_rows)``. A read that did not
    answer is reported as the failed read it is — including on a server that
    never had the tool, which Lens does not try to tell apart (the pre-0.4
    detection was withdrawn; see the module docstring).
    """
    frontier_ok = True
    ready_rows: list[TaskRecord] = []
    blocked_rows: list[BlockedTaskRecord] = []
    if isinstance(ready_result, BaseException):
        errors.append("Could not load the ready frontier.")
        frontier_ok = False
    else:
        ready_rows = cast(list[TaskRecord], ready_result)
    if isinstance(blocked_result, BaseException):
        errors.append("Could not load the blocked frontier.")
        frontier_ok = False
    else:
        blocked_rows = cast(list[BlockedTaskRecord], blocked_result)
    return frontier_ok, ready_rows, blocked_rows
