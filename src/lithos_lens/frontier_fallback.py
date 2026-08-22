"""The dashboard's no-frontier fallback: detection, flat render, verdicts.

Split out of ``frontier.py`` when the T1 slices pushed it past the 800-line
god-module ceiling. The seam is T1-S12's own subject: ``frontier.py`` owns the
happy path — the seven parallel reads and the ready/blocked join — while
everything that decides *there is no usable frontier right now*, and what to
render instead, lives here.

Two different conditions land in the same flat render and must stay
distinguishable: a server with no frontier TOOLS (pre-0.4, a version story the
caller remembers and re-probes) and a frontier READ that failed (an outage,
reported per render, graph verdict untouched). The error lines below are what
keeps them apart on screen.
"""

from __future__ import annotations

from collections.abc import Awaitable, Sequence
from typing import Any, Protocol, cast

from lithos_lens.task_graph import BlockedTaskRecord
from lithos_lens.tasks import SectionName, SectionRow, TaskRecord

# The three workable sections, in render order (mirrored from ``frontier`` so
# the open-side order can be stated in one place).
WORKABLE_SECTIONS: tuple[SectionName, ...] = ("in_progress", "ready", "blocked")


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

    async def list_tool_names(self) -> set[str]: ...


# The flat-fallback section, rendered INSTEAD of the workable three when the
# server has no frontier tools. Kept separate so the two modes never mix.
FLAT_SECTIONS: tuple[SectionName, ...] = ("open",)

# Every open-side section, in render order (only one mode's are ever filled).
OPEN_SECTIONS: tuple[SectionName, ...] = (
    FLAT_SECTIONS + WORKABLE_SECTIONS + ("claims_unknown", "unclassified")
)

# Version-skew detection (story 27): the names of the two task-graph frontier
# tools, looked for in the server's own ``tools/list``. A pre-0.4 Lithos has
# neither.
READY_TOOL = "lithos_task_ready"
BLOCKED_TOOL = "lithos_task_blocked"

# One line for both flat-fallback paths (a fresh verdict and a cached one), so
# the degraded state is continuously visible to the banner and to log-based
# monitoring rather than only on the render that discovered it.
FRONTIER_UNAVAILABLE_ERROR = "The Lithos ready/blocked frontier tools are unavailable."

# A skewed read could not be re-read. Reported rather than swallowed: the
# dashboard's affirmative "All systems healthy" stripe is gated on this error
# channel, and a retry triggered by terminal overlap alone leaves no other
# trace — the board would otherwise claim health while rendering one task in
# both an open section and a terminal one.
RETRY_FAILED_ERROR = "Could not re-read the task graph to reconcile a skewed read."


async def frontier_tools_absent(lithos: FrontierProbeClient) -> bool:
    """Ask the server whether it actually has the two frontier tools.

    This is the ONLY input to the fallback verdict. Error text is never
    consulted: an MCP error result can quote the failing tool's own payload —
    task titles written by any agent with task-write access, who is strictly
    less privileged than the Lens operator — so a planted string must not be
    able to retire the Ready/Blocked/gate surface. ``tools/list`` is the
    server's structured statement about itself.

    Answers False unless the listing succeeded, was non-empty, and named
    neither tool: a listing Lens could not make (or an empty one) says nothing
    about the server, and absence must never be inferred from a failure. A
    server exposing exactly one of the pair is broken rather than old, and is
    likewise reported as an error instead of silently losing the graph.
    """
    try:
        names = await lithos.list_tool_names()
    except Exception:
        return False
    return bool(names) and not (names & {READY_TOOL, BLOCKED_TOOL})


def flat_open_sections(
    open_tasks: Sequence[TaskRecord],
) -> dict[SectionName, tuple[SectionRow, ...]]:
    """Partition open tasks for the pre-0.4 fallback: one flat ``open`` section.

    Without the frontier there is no honest way to say ready vs blocked, so
    Lens says neither — every open row lands in one list with its claim chips
    (the 0.1.0 dashboard's behavior). No task-type filter either: epics and
    gates do not exist on a server without the task graph. The claims-unknown
    contract is unchanged: a server old enough to lack the frontier tools may
    also ignore ``with_claims``, and a row whose claims came back ``None`` must
    still say "claims unknown" rather than a confident "unclaimed".
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


async def _skipped_frontier_call() -> list[Any]:
    """Stand in for a frontier call not made (the tools are known absent).

    Keeps ``load_dashboard``'s single gather positional: the slot is filled
    with an empty result instead of the response, and the flat branch renders.
    """
    return []


def frontier_reads(
    lithos: FrontierProbeClient,
    *,
    frontier_limit: int,
    graph_available: bool,
) -> tuple[Awaitable[Any], Awaitable[Any]]:
    """The ready/blocked awaitables for one generation of the assembly.

    A server whose tool list recently came back without the frontier tools is
    not asked again until the caller's re-probe window lapses: two guaranteed
    failures per render is pure cost, so the two gather slots are filled with
    placeholders (the fallback still reports itself — see ``resolve_frontier``).
    """
    if not graph_available:
        return _skipped_frontier_call(), _skipped_frontier_call()
    return (
        lithos.task_ready(limit=frontier_limit, with_claims=False),
        lithos.task_blocked(limit=frontier_limit),
    )


def resolve_frontier(
    ready_result: Any,
    blocked_result: Any,
    *,
    graph_available: bool,
    tools_absent: bool,
    errors: list[str],
) -> tuple[bool, bool, list[TaskRecord], list[BlockedTaskRecord]]:
    """Read the two frontier responses into rows, verdicts, and error lines.

    Returns ``(graph_available, frontier_ok, ready_rows, blocked_rows)``.
    ``tools_absent`` is the confirmed ``tools/list`` verdict (see
    ``frontier_tools_absent``); only that drops the load to the flat fallback.
    Every other failure keeps the graph surface and reports itself.

    The fallback is never silent — on the render that discovers it AND on every
    render served from the caller's cached verdict. The same symptom is
    produced by an outage or by the tools being withheld from this client, so
    the condition has to stay continuously visible to the operator and to
    log-based monitoring rather than being dressed up as a benign version
    notice that lapses off the error channel.
    """
    if tools_absent or not graph_available:
        errors.append(FRONTIER_UNAVAILABLE_ERROR)
        return False, False, [], []

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
    return True, frontier_ok, ready_rows, blocked_rows
