"""Lithos's cycle verdict for one graph page: the scoped blocked reads (D4).

The division of labour is asymmetric on purpose. ``graph_layout`` computes the
SHAPE of a cycle from the fetched edges; **membership** is Lithos's, read from
``lithos_task_blocked`` — a ``kind="cycle"`` blocker — because a cycle that
closes through two out-of-scope tasks (``A(in) -> B(ghost) -> C(ghost) -> A``)
is invisible to a scope that never fetches a ghost's edges (D5).

The read is **scoped**, one pair per project in the coverage set, and the
coverage set is the point of this module:

- every §5B.1 project among the in-scope tasks, *and* among the DOWNSTREAM
  ghosts — they appear in the impact count (D10), so their sole-blocker fact
  has to be readable from the same reads;
- a read pair is ``project=<slug>`` plus, under the ``"both"`` convention,
  ``tags=["<project_tag_key>:<slug>"]``, unioned (§5B.7's pattern), each at
  ``frontier_limit``, with ``len == limit`` treated as truncation — the
  dashboard's rule;
- a task with **no project** under either convention is reachable by no scoped
  read. It is marked ``cycle status unknown`` and counted, and Lens issues no
  unscoped read to cover it: that read is global, capped and corpus-size
  dependent, so the coverage claim would stop meaning anything.

The coverage set is COMPLETE or the scope is refused. It is derived from task
TAGS, so whoever writes a task chooses how many projects one render would read
— but "every project in the coverage set" is D4's contract, so a scope too
large to honour it is turned away by ``graph_page`` before any read is issued
(:data:`~lithos_lens.graph_scope.REFUSAL_COVERAGE`) rather than read in part.
:data:`CYCLE_READ_BUDGET_S` then bounds the phase in TIME, and a read the
deadline left unissued is reported as never made — never as "no cycle", and
never as an upstream failure it was not.

Everything here degrades towards *unknown*, never towards *no cycle* — but the
degradation is per TASK, not per project or per response. A task any response
RETURNED is answered for, even by a response that went on to truncate. A task
no response returned is cycle-free only if some read that could actually have
matched it answered in full; a truncated read, a failed read and a read that
was never made establish nothing about the tasks they did not name, because
absence from a response Lens knows to be partial reads on the page as "this
task is fine".
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from lithos_lens.graph_cache import graph_fanout_gate
from lithos_lens.graph_layout import CYCLE_BLOCKER_KIND
from lithos_lens.graph_scope import TaskGraphScope
from lithos_lens.task_filtering import task_projects
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord
from lithos_lens.task_links import BLOCKER_EDGE_TYPES, LINK_READ_TIMEOUT_S
from lithos_lens.tasks import (
    DEFAULT_PROJECT_CONVENTION,
    DEFAULT_PROJECT_TAG_KEY,
    ProjectConvention,
    TaskRecord,
)

#: Which half of a read pair a call is. Kept as data rather than as two code
#: paths so the call log a test asserts on and the code issuing the calls are
#: the same list.
READ_BY_PROJECT = "project"
READ_BY_TAG = "tags"

#: The deadline on the whole cycle-read phase — the same internal safety net
#: :data:`~lithos_lens.graph_fanout.GHOST_RESOLUTION_BUDGET_S` is, not a dial an
#: operator tunes. It covers the waits for the two gates as well as the calls,
#: which the per-call :data:`~lithos_lens.task_links.LINK_READ_TIMEOUT_S` by
#: construction cannot: that timeout starts only once both gates are held, so a
#: render queued behind a busy fan-out is exactly the case it does not bound.
CYCLE_READ_BUDGET_S = 10.0


@dataclass(frozen=True)
class ProjectRead:
    """One half of a project's read pair, and what came back."""

    project: str
    by: str
    rows: tuple[BlockedTaskRecord, ...] = ()
    truncated: bool = False
    error: str = ""
    #: The call was never ISSUED — the phase deadline expired while this half
    #: of the pair was still queued behind the fan-out gate. Distinct from
    #: ``error`` on purpose: a failure is Lithos's answer to a question Lens
    #: asked, and counting an unasked question as one would put a read in the
    #: telemetry that never reached the server and tell the operator upstream
    #: broke when it was Lens that ran out of time.
    unmade: bool = False

    @property
    def ok(self) -> bool:
        """Answered in full — the only outcome that can establish ABSENCE.

        Rows a response DID return are answers whatever this says: a task named
        by a truncated response is still one Lithos reported on. This flag is
        about what the response's silence means, which is nothing unless the
        read was complete (and matched the task — see ``_signal``).
        """
        return not self.error and not self.truncated and not self.unmade


@dataclass(frozen=True)
class CycleSignal:
    """What Lithos said about cycles in this scope, and where it went quiet.

    ``flagged`` is the verdict itself (task id -> the blocker's own message);
    ``verdicts`` is one row per task, its blockers MERGED across every read that
    named it, handed to ``build_topology`` so a flagged member with no fetched
    component is still condensed alone and layered. ``unknown`` names the
    in-scope tasks whose cycle status this page cannot claim either way.
    """

    #: D4's coverage set, whole: every project this render planned to read.
    #: A scope Lens could not plan a complete pair for never gets this far —
    #: ``graph_page`` refuses it before the first call.
    coverage: tuple[str, ...] = ()
    reads: tuple[ProjectRead, ...] = ()
    #: Every row any read returned, merged. NOT this page's cycle authority: a
    #: project-scoped read legitimately names tasks this graph never fetched,
    #: and ghosts whose own edges it never read. Kept whole because a
    #: downstream ghost's sole-blocker fact is why its project is in the
    #: coverage set at all (D10) — see ``verdicts`` for what may be believed.
    blocked: tuple[BlockedTaskRecord, ...] = ()
    #: The subset of ``blocked`` naming an IN-SCOPE, non-ghost task: the only
    #: rows this page may condense, flag or propagate ``blocked via cycle``
    #: from. A ghost's row would invent a cycle for a node whose edges Lens
    #: never fetched, and then blame in-scope work for it.
    verdicts: tuple[BlockedTaskRecord, ...] = ()
    flagged: Mapping[str, str] = field(default_factory=dict)
    #: task id -> the tasks Lithos's ``kind="cycle"`` blockers NAME as its
    #: partners. The page needs the endpoint, not just the message: a cycle
    #: Lens cannot shape is only "outside this scope" when the partner is, and
    #: a stale edge cache makes the missing shape prove nothing on its own.
    cycle_partners: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    unknown: frozenset[str] = frozenset()
    projectless: tuple[str, ...] = ()

    @property
    def truncated_projects(self) -> tuple[str, ...]:
        return _distinct(read.project for read in self.reads if read.truncated)

    @property
    def failed_projects(self) -> tuple[str, ...]:
        """Projects whose read was ISSUED and did not answer."""
        return _distinct(read.project for read in self.reads if read.error)

    @property
    def unmade_projects(self) -> tuple[str, ...]:
        """Projects a read was planned for and never issued (the deadline)."""
        return _distinct(read.project for read in self.reads if read.unmade)

    @property
    def complete(self) -> bool:
        """Whether every in-scope task's cycle status is known."""
        return not self.unknown


class CycleSignalClient(Protocol):
    """The one client method this module needs."""

    async def task_blocked(
        self,
        *,
        limit: int | None = None,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[BlockedTaskRecord]: ...


def coverage_projects(
    scope: TaskGraphScope,
    *,
    convention: ProjectConvention = DEFAULT_PROJECT_CONVENTION,
    tag_key: str = DEFAULT_PROJECT_TAG_KEY,
) -> tuple[str, ...]:
    """Every project the page must read, sorted (D4).

    In-scope tasks plus DOWNSTREAM ghosts — the far endpoints of outgoing
    dependency edges. An upstream ghost is a blocker, and nothing on this page
    claims anything about ITS blockers; a downstream one is counted in impact,
    so its project is read.

    Slugs are read under **both** conventions whatever the matching posture, as
    §5B.1 requires of any project enumeration: a project invisible to its own
    coverage read would leave its tasks silently unknown.
    """
    in_scope = {node.id for node in scope.nodes if not node.ghost}
    downstream = {
        edge.to_task_id
        for edge in scope.edges
        if edge.type in BLOCKER_EDGE_TYPES
        and edge.from_task_id in in_scope
        and edge.to_task_id not in in_scope
    }
    covered = [
        node
        for node in scope.nodes
        if not node.ghost or (node.ghost and node.id in downstream)
    ]
    slugs: set[str] = set()
    for node in covered:
        slugs.update(task_projects(node.task, convention="both", tag_key=tag_key))
    return tuple(sorted(slugs))


async def load_cycle_signal(
    lithos: CycleSignalClient,
    scope: TaskGraphScope,
    *,
    frontier_limit: int,
    convention: ProjectConvention = DEFAULT_PROJECT_CONVENTION,
    tag_key: str = DEFAULT_PROJECT_TAG_KEY,
    fetch_concurrency: int = 16,
) -> CycleSignal:
    """Run one ``lithos_task_blocked`` read PLAN per project in the coverage set.

    A plan is a pair of calls under the default ``"both"`` posture — one
    ``project=`` (metadata) and one ``tags=`` (tag convention) — and a single
    call under a single-convention posture, so the fan-out is two calls per
    covered project by default, not one. ``_read_kinds`` decides which, and the
    call log a test asserts on is exactly this plan.

    The plan is the WHOLE coverage set — D4 admits no sampling, and a scope
    whose set is too large to read is refused by ``graph_page`` before this
    function is reached. What bounds the phase here is TIME
    (:data:`CYCLE_READ_BUDGET_S`), because neither gate below does: a semaphore
    limits how many calls run at once, not how long the queue behind it takes,
    and the per-call timeout starts only once both gates are held.

    A read the deadline catches BEFORE the call is issued comes back
    ``unmade``, not ``error``. Both leave their tasks ``cycle status unknown``
    — that much is the same claim — but only one of them is a read Lithos ever
    saw, and the counter and the banner both say which.
    """
    projects = coverage_projects(scope, convention=convention, tag_key=tag_key)
    limiter = asyncio.Semaphore(max(fetch_concurrency, 1))
    plan = [(project, by) for project in projects for by in _read_kinds(convention)]
    deadline = asyncio.get_running_loop().time() + CYCLE_READ_BUDGET_S

    async def read(project: str, by: str) -> ProjectRead:
        issued = False
        try:
            # Deadline OUTSIDE the gates: a render held behind a busy fan-out
            # gate is exactly the case the per-call timeout cannot see.
            async with asyncio.timeout_at(deadline):
                async with graph_fanout_gate(), limiter:
                    # Set on the line before the call, so it is true of
                    # exactly the halves that reached ``task_blocked`` — a
                    # deadline that fires mid-call is a read Lithos received
                    # and did not answer, which is a failure and counts.
                    issued = True
                    rows = await asyncio.wait_for(
                        lithos.task_blocked(
                            limit=frontier_limit,
                            project=project if by == READ_BY_PROJECT else None,
                            tags=[f"{tag_key}:{project}"]
                            if by == READ_BY_TAG
                            else None,
                        ),
                        LINK_READ_TIMEOUT_S,
                    )
        except Exception as exc:  # noqa: BLE001 - every failure is "unknown"
            if not issued:
                return ProjectRead(project=project, by=by, unmade=True)
            return ProjectRead(project=project, by=by, error=_reason(exc))
        return ProjectRead(
            project=project,
            by=by,
            rows=tuple(rows),
            # The dashboard's rule: a response that exactly fills the limit is
            # assumed truncated, because Lithos does not say.
            truncated=len(rows) >= frontier_limit > 0,
        )

    reads = tuple(await asyncio.gather(*(read(*call) for call in plan)))
    return _signal(scope, projects, reads, tag_key=tag_key)


def _signal(
    scope: TaskGraphScope,
    projects: Sequence[str],
    reads: Sequence[ProjectRead],
    *,
    tag_key: str,
) -> CycleSignal:
    """Fold the reads into the verdict and the coverage it does not have.

    A scoped read answers about a PROJECT, not about this graph, so it returns
    rows for tasks the page never fetched and for ghosts whose edges it never
    read. Those rows are kept (``blocked``) and are not authority
    (``verdicts``): condensing a ghost's flagged row would draw a cycle for a
    node Lens has no edges for and mark the real work below it "blocked via
    cycle" on the strength of it.

    The fold is per TASK, not per response. §5B.7's pattern unions the pair,
    and the two calls are independent reads rather than one snapshot: a task
    can arrive in the ``tags=`` response carrying the ``kind="cycle"`` blocker
    that the ``project=`` response — read a moment earlier — did not have yet.
    Keeping whichever row landed first would drop Lithos's verdict on the
    floor while the message rendered beside it, so blockers are merged across
    every row that names the task.

    Coverage is per task too, and per READ rather than per project. A task
    PRESENT in any response has been answered about, whatever else that
    response truncated. A task ABSENT from every response is known cycle-free
    only when some read that COULD have returned it answered in full — which
    is a question about the convention that read expresses (§5B.1):
    ``project=<slug>`` is the metadata convention, ``tags=["<key>:<slug>"]``
    the tag one. An empty response from a filter the task cannot match is not
    coverage — under a single-convention posture that is the whole hazard,
    because the other half of the pair is never issued at all.
    """

    rows: dict[str, TaskRecord] = {}
    blockers: dict[str, dict[tuple[str, str, str, str], BlockerRecord]] = {}
    for read in reads:
        for row in read.rows:
            rows.setdefault(row.task.id, row.task)
            merged = blockers.setdefault(row.task.id, {})
            for blocker in row.blockers:
                # First one wins, so the merged row keeps the message of
                # whichever read answered first — the same arbitrary-but-stable
                # choice the fold makes about everything else here.
                merged.setdefault(_blocker_identity(blocker), blocker)
    blocked = tuple(
        BlockedTaskRecord(task=task, blockers=tuple(blockers[task_id].values()))
        for task_id, task in rows.items()
    )
    in_scope = {node.id for node in scope.nodes if not node.ghost}
    verdicts = tuple(record for record in blocked if record.task.id in in_scope)
    cycle_blockers = {
        record.task.id: tuple(
            blocker for blocker in record.blockers if blocker.kind == CYCLE_BLOCKER_KIND
        )
        for record in verdicts
    }
    flagged = {
        task_id: blockers[0].message
        for task_id, blockers in cycle_blockers.items()
        if blockers
    }
    partners = {
        task_id: tuple(blocker.task_id for blocker in blockers if blocker.task_id)
        for task_id, blockers in cycle_blockers.items()
        if blockers
    }

    unknown: set[str] = set()
    projectless: list[str] = []
    for node in scope.nodes:
        if node.ghost or node.id in rows:
            continue
        if not task_projects(node.task, convention="both", tag_key=tag_key):
            # No scoped read can reach it, and Lens does not issue the unscoped
            # one (D4). Unknown, and said so rather than implied.
            projectless.append(node.id)
            unknown.add(node.id)
        elif not any(read_covers(read, node.task, tag_key=tag_key) for read in reads):
            unknown.add(node.id)

    return CycleSignal(
        coverage=tuple(projects),
        reads=tuple(reads),
        blocked=blocked,
        verdicts=verdicts,
        flagged=flagged,
        cycle_partners=partners,
        unknown=frozenset(unknown),
        projectless=tuple(projectless),
    )


def _blocker_identity(blocker: BlockerRecord) -> tuple[str, str, str, str]:
    """What makes two rows from two reads the SAME blocker.

    The pair of reads is two independent samples, not one snapshot, and
    ``message`` is presentation text sampled with them: a gate's carries its
    ``ready_at``, a cycle's the path it was detected through, and either can be
    rewritten between the two calls while the blocking FACT stands still.
    Deduplicating on the whole record would then keep both copies, and the one
    consumer that reads the merged tuple's LENGTH — ``graph_impact``'s
    sole-blocker count — would see two blockers where Lithos reported one and
    withhold M's "immediately" from a dependent this task alone is blocking
    (round-1 correctness f-002).

    So identity is the four fields the answer is actually computed from — the
    same four ``graph_impact.impact_fingerprint`` folds into its digest, and
    the same four ``read_covers`` and the cycle markers read.
    """
    return (blocker.kind, blocker.task_id, blocker.type, blocker.status)


def read_covers(read: ProjectRead, task: TaskRecord, *, tag_key: str) -> bool:
    """Whether ``read`` would have returned ``task`` had it been blocked (D4).

    Coverage belongs to the READ, not to the project: ``project=<slug>`` is
    §5B.1's metadata convention and ``tags=["<key>:<slug>"]`` the tag one, so a
    complete response from a filter this task cannot match establishes nothing
    about it. And only a complete response establishes ABSENCE at all — a
    truncated, failed or unissued read is silence, never "not blocked".
    """
    if not read.ok:
        return False
    convention: ProjectConvention = "metadata" if read.by == READ_BY_PROJECT else "tag"
    return read.project in task_projects(task, convention=convention, tag_key=tag_key)


def blocked_coverage(signal: CycleSignal, task: TaskRecord, *, tag_key: str) -> bool:
    """Whether some complete read could have returned ``task``.

    The question the cycle markers ask of an in-scope task, asked here of any
    task — which is what the downstream impact count needs (D10): a DEPENDENT
    may be a ghost, and its sole-blocker fact is exactly why its project is in
    the coverage set.
    """
    return any(read_covers(read, task, tag_key=tag_key) for read in signal.reads)


def _read_kinds(convention: ProjectConvention) -> tuple[str, ...]:
    """The halves of a read pair this convention needs (§5B.7).

    Under ``"both"`` the two reads are unioned; under a single-convention
    posture only the matching one is issued, because the other would scope by a
    convention this deployment does not honour.
    """
    kinds: list[str] = []
    if convention in ("metadata", "both"):
        kinds.append(READ_BY_PROJECT)
    if convention in ("tag", "both"):
        kinds.append(READ_BY_TAG)
    return tuple(kinds)


def _reason(exc: BaseException) -> str:
    """The Lithos error code when there is one, else the exception type."""
    code = getattr(exc, "code", "")
    return code if isinstance(code, str) and code else type(exc).__name__


def _distinct(values: Iterable[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return tuple(seen)
