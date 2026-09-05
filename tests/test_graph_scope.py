"""T2 slice A1 — scope assembly (``graph_scope.py``).

A scope is what one graph page renders, and nearly every rule in it is a
decision about what Lens may CLAIM: which edges still block, which far
endpoints are worth showing, which tasks are genuinely edge-less, and how
much of the picture was actually read. So the tests below are mostly about
the claims — an edge dropped as history, a ghost shown with no status rather
than hidden, a task that is not isolated because its edge read failed — and
about the reads those claims cost, asserted on the client's call log.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from lithos_lens.config import (
    DEFAULT_GRAPH_FETCH_CONCURRENCY as CONFIG_FETCH_CONCURRENCY,
)
from lithos_lens.config import DEFAULT_GRAPH_MAX_TASKS as CONFIG_MAX_TASKS
from lithos_lens.config import EventsConfig, LithosConfig
from lithos_lens.events import EventHub, LensEvent
from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.fake_graph_dataset import edge_index
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.graph_cache import GraphCache
from lithos_lens.graph_scope import (
    COMPLETENESS_EDGES_UNKNOWN,
    COMPLETENESS_STATUS_UNKNOWN,
    EDGE_ACTIVE,
    EDGE_INACTIVE,
    EDGE_UNKNOWN,
    GHOST_CONTEXT,
    GHOST_DEPENDENCY,
    REASON_DEPENDENT_RESOLVED,
    REASON_SATISFIED,
    UNKNOWN_STATUS,
    GraphScopeLimits,
    dependency_edge_state,
    load_epic_scope,
    load_project_scope,
)
from lithos_lens.tasks import TaskRecord, TaskStatusName

pytestmark = pytest.mark.anyio

_T0 = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
PROJECT = "loom"


class StepClock:
    def __init__(self, start: datetime = _T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def task(
    task_id: str,
    *,
    status: TaskStatusName = "open",
    project: str = PROJECT,
    created_at: str = "",
    task_type: str = "task",
) -> TaskRecord:
    """One fixture task, in ``project`` under the tag convention."""
    return TaskRecord(
        id=task_id,
        title=task_id.replace("-", " ").title(),
        status=status,
        task_type=task_type,
        created_by="planner",
        created_at=created_at or f"2026-09-01T00:00:{len(task_id):02d}+00:00",
        tags=(f"project:{project}",) if project else (),
        resolved_at="2026-09-02T00:00:00+00:00" if status != "open" else "",
    )


def dataset(
    tasks: Sequence[TaskRecord],
    edges: Sequence[tuple[str, str, str]] = (),
    children: dict[str, tuple[str, ...]] | None = None,
) -> FakeLithosDataset:
    return FakeLithosDataset(
        tasks=tuple(tasks),
        edges=edge_index(tuple(edges)),
        children=children or {},
    )


class RecordingClient:
    """A ``FakeLithosClient`` with a call log and injectable read failures.

    The call log is what several acceptance criteria are stated in — "no
    ``task_get`` for the excluded child", "each task's edges fetched exactly
    once" — because the cost and the bound are the behaviour, not a detail
    behind it.
    """

    def __init__(
        self,
        data: FakeLithosDataset,
        *,
        edge_failures: set[str] | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self._client = FakeLithosClient(dataset=data)
        self._edge_failures = edge_failures or set()
        self._gate = gate
        self.edge_calls: list[str] = []
        self.get_calls: list[str] = []
        self.children_calls: list[str] = []

    async def task_get(self, task_id: str) -> TaskRecord:
        self.get_calls.append(task_id)
        return await self._client.task_get(task_id)

    async def task_children(
        self,
        task_id: str,
        *,
        recursive: bool = False,
        include_closed: bool = False,
    ) -> list[TaskRecord]:
        self.children_calls.append(task_id)
        return await self._client.task_children(
            task_id, recursive=recursive, include_closed=include_closed
        )

    async def task_edge_list(
        self,
        task_id: str,
        *,
        direction: str = "both",
        types: list[str] | None = None,
    ):
        self.edge_calls.append(task_id)
        if self._gate is not None:
            await self._gate.wait()
        if task_id in self._edge_failures:
            raise RuntimeError(f"edge_list failed for {task_id}")
        return await self._client.task_edge_list(
            task_id, direction=direction, types=types
        )


async def project_scope(
    client: RecordingClient,
    master: Sequence[TaskRecord],
    *,
    cache: GraphCache | None = None,
    limits: GraphScopeLimits | None = None,
    include_resolved: bool = False,
):
    return await load_project_scope(
        client,
        project=PROJECT,
        master=master,
        cache=cache or GraphCache(clock=StepClock()),
        limits=limits,
        include_resolved=include_resolved,
    )


def state_of(scope, from_id: str, to_id: str) -> tuple[str, str]:
    edge = next(
        e for e in scope.edges if (e.from_task_id, e.to_task_id) == (from_id, to_id)
    )
    return edge.state, edge.reason


# ── Limits, and the config they mirror ─────────────────────────────────


def test_scope_limits_mirror_the_config_defaults() -> None:
    """Same pinning as the cache's TTL: one decision, two spellings."""
    limits = GraphScopeLimits()
    assert (limits.max_tasks, limits.fetch_concurrency) == (
        CONFIG_MAX_TASKS,
        CONFIG_FETCH_CONCURRENCY,
    )


# ── The cache seam: fan-out and invalidation ───────────────────────────


async def test_two_concurrent_scopes_fetch_each_task_s_edges_exactly_once() -> None:
    """The point of a per-TASK cache: overlapping scopes share their reads."""
    tasks = [task("a"), task("b"), task("c")]
    gate = asyncio.Event()
    client = RecordingClient(
        dataset(tasks, [("a", "b", "blocks"), ("b", "c", "blocks")]), gate=gate
    )
    cache = GraphCache(clock=StepClock())

    first = asyncio.create_task(project_scope(client, tasks, cache=cache))
    second = asyncio.create_task(project_scope(client, tasks, cache=cache))
    await asyncio.sleep(0)
    gate.set()
    left, right = await asyncio.gather(first, second)

    assert sorted(client.edge_calls) == ["a", "b", "c"]
    assert left.node_ids == right.node_ids == ("a", "b", "c")


async def test_a_task_event_evicts_that_node_and_the_next_scope_refetches_it() -> None:
    """Eviction is per task, so a second render re-reads one node, not all."""
    tasks = [task("a"), task("b")]
    client = RecordingClient(dataset(tasks, [("a", "b", "blocks")]))
    cache = GraphCache(clock=StepClock())
    hub = EventHub(EventsConfig(enabled=False), LithosConfig(), graph_cache=cache)
    await project_scope(client, tasks, cache=cache)
    assert sorted(client.edge_calls) == ["a", "b"]

    await hub.publish(LensEvent(id="e1", type="task.updated", task_id="b"))
    await project_scope(client, tasks, cache=cache)

    assert sorted(client.edge_calls) == ["a", "b", "b"]


# ── The size guard ─────────────────────────────────────────────────────


async def test_a_scope_one_over_max_tasks_is_refused_with_the_count() -> None:
    """Ghosts count: three in-scope tasks plus one ghost breaches a guard of 3."""
    tasks = [task("a"), task("b"), task("c")]
    client = RecordingClient(
        dataset(
            [*tasks, task("far", project="other")],
            [("a", "b", "blocks"), ("b", "c", "blocks"), ("c", "far", "blocks")],
        )
    )

    scope = await project_scope(client, tasks, limits=GraphScopeLimits(max_tasks=3))

    assert scope.refused
    assert scope.refusal is not None
    assert (scope.refusal.count, scope.refusal.ghosts_counted) == (4, True)
    assert scope.nodes == ()


async def test_an_oversized_task_set_is_refused_before_the_fan_out() -> None:
    """The answer cannot change, so it is not worth one read per node."""
    tasks = [task("a"), task("b")]
    client = RecordingClient(dataset(tasks))

    scope = await project_scope(client, tasks, limits=GraphScopeLimits(max_tasks=1))

    assert scope.refusal is not None
    assert (scope.refusal.count, scope.refusal.ghosts_counted) == (2, False)
    assert client.edge_calls == []


# ── Membership and the resolved toggle ─────────────────────────────────


async def test_a_project_scope_is_open_only_until_include_resolved() -> None:
    master = [task("a"), task("done", status="completed"), task("x", project="other")]
    client = RecordingClient(dataset(master, [("done", "a", "blocks")]))

    default = await project_scope(client, master)
    widened = await project_scope(client, master, include_resolved=True)

    assert default.node_ids == ("a",)
    assert widened.node_ids == ("a", "done")


async def test_an_epic_scope_includes_closed_children_by_default() -> None:
    epic = task("epic", task_type="epic")
    children = {"epic": ("open-child", "done-child")}
    tasks = [epic, task("open-child"), task("done-child", status="completed")]
    client = RecordingClient(dataset(tasks, children=children))

    scope = await load_epic_scope(
        client, epic_id="epic", master=tasks, cache=GraphCache(clock=StepClock())
    )

    assert set(scope.node_ids) == {"epic", "open-child", "done-child"}


async def test_include_resolved_0_on_an_epic_hides_a_completed_child_unread() -> None:
    """Fixture (a): the excluded child is not re-fetched as a context ghost.

    The epic's ``parent_child`` edge to it survives upstream — completion
    never deletes hierarchy — so the only thing stopping ``include_resolved=0``
    from silently re-admitting it is the rule that context is added UPSTREAM
    only (D6). Asserted on the call log, because a re-admission would show up
    first as a read.
    """
    epic = task("epic", task_type="epic")
    tasks = [epic, task("open-child"), task("done-child", status="completed")]
    client = RecordingClient(
        dataset(
            tasks,
            [
                ("epic", "open-child", "parent_child"),
                ("epic", "done-child", "parent_child"),
            ],
            children={"epic": ("open-child", "done-child")},
        )
    )

    scope = await load_epic_scope(
        client,
        epic_id="epic",
        master=tasks,
        cache=GraphCache(clock=StepClock()),
        include_resolved=False,
    )

    assert set(scope.node_ids) == {"epic", "open-child"}
    assert "done-child" not in client.get_calls
    assert [(e.from_task_id, e.to_task_id) for e in scope.edges] == [
        ("epic", "open-child")
    ]


# ── Ghosts: dependency, context, and one hop ───────────────────────────


async def test_a_completed_far_predecessor_is_dropped_and_a_cancelled_one_ghosted() -> (
    None
):
    """Both are out of scope; only one of them still stops anything.

    A completed predecessor's edge is history the default view did not ask
    for. A cancelled one blocks FOREVER, and a graph that hid it would
    contradict the dashboard, which flags exactly that as unsatisfiable.
    """
    dependent = task("a")
    master = [dependent]
    client = RecordingClient(
        dataset(
            [
                dependent,
                task("done-pred", status="completed", project="other"),
                task("dead-pred", status="cancelled", project="other"),
            ],
            [("done-pred", "a", "blocks"), ("dead-pred", "a", "blocks")],
        )
    )

    scope = await project_scope(client, master)

    assert set(scope.node_ids) == {"a", "dead-pred"}
    ghost = scope.node("dead-pred")
    assert ghost is not None and ghost.ghost and ghost.ghost_kind == GHOST_DEPENDENCY
    assert state_of(scope, "dead-pred", "a") == (EDGE_ACTIVE, "")


async def test_a_ghost_s_own_edges_are_never_fetched() -> None:
    """One hop, leaf-only: the fan-out is bounded by the scope (D5)."""
    master = [task("a")]
    client = RecordingClient(
        dataset(
            [task("a"), task("far", project="other"), task("beyond", project="other")],
            [("far", "a", "blocks"), ("beyond", "far", "blocks")],
        )
    )

    scope = await project_scope(client, master)

    assert client.edge_calls == ["a"]
    assert "beyond" not in scope.node_ids


async def test_an_open_far_endpoint_is_ghosted_from_the_master_list_unread() -> None:
    """An open ghost's title and status are already in hand (D5)."""
    far = task("far", project="other")
    master = [task("a"), far]
    client = RecordingClient(dataset([*master], [("far", "a", "blocks")]))

    scope = await project_scope(client, master)

    assert client.get_calls == []
    ghost = scope.node("far")
    assert ghost is not None and ghost.ghost and ghost.status == "open"


async def test_a_ghost_whose_task_get_fails_is_shown_unknown_in_both_directions() -> (
    None
):
    """Hiding a possibly-live blocker is the wrong way to err (D2).

    Lens cannot tell a completed far endpoint (drop) from a cancelled one
    (show), so it shows the node with no status claim and classes every
    dependency edge touching it ``unknown`` — neither active nor inactive —
    whichever end of the edge it sits on.
    """
    master = [task("a")]
    # Neither ghost exists in the dataset, so `task_get` answers with the
    # coded not-found envelope the real client would.
    client = RecordingClient(
        dataset(
            [task("a")], [("ghost-up", "a", "blocks"), ("a", "ghost-down", "blocks")]
        )
    )

    scope = await project_scope(client, master)

    for ghost_id in ("ghost-up", "ghost-down"):
        ghost = scope.node(ghost_id)
        assert ghost is not None, f"{ghost_id} was dropped"
        assert ghost.status == UNKNOWN_STATUS
        assert ghost.completeness == COMPLETENESS_STATUS_UNKNOWN
    assert state_of(scope, "ghost-up", "a") == (EDGE_UNKNOWN, "")
    assert state_of(scope, "a", "ghost-down") == (EDGE_UNKNOWN, "")


async def test_an_open_child_keeps_its_completed_parent_as_a_context_ghost() -> None:
    """Fixture (b): the parent is one hop, and its own parent is not read.

    ``task_get`` carries neither a parent id nor edges, and a ghost's edge
    list is never read — so the grandparent is unreachable by construction,
    which is what makes "one hop" a bound rather than a promise.
    """
    child = task("child")
    master = [child]
    client = RecordingClient(
        dataset(
            [
                child,
                task("epic", status="completed", task_type="epic"),
                task("grandparent", task_type="epic"),
            ],
            [
                ("epic", "child", "parent_child"),
                ("grandparent", "epic", "parent_child"),
            ],
        )
    )

    scope = await project_scope(client, master)

    ghost = scope.node("epic")
    assert ghost is not None and ghost.ghost and ghost.ghost_kind == GHOST_CONTEXT
    assert ghost.status == "completed"
    assert client.get_calls == ["epic"]
    assert client.edge_calls == ["child"]
    assert "grandparent" not in scope.node_ids


async def test_an_out_of_set_discovered_from_source_is_a_context_ghost() -> None:
    """Provenance is upstream by definition, so it survives the open-only filter.

    Resolved on the DEFAULT request rather than when the overlay is toggled:
    the overlays are client-side state over a static payload, so the node has
    to be in the payload before the toggle can show it.
    """
    follow_on = task("follow-on")
    master = [follow_on]
    client = RecordingClient(
        dataset(
            [follow_on, task("source", status="completed")],
            [("source", "follow-on", "discovered_from")],
        )
    )

    scope = await project_scope(client, master)

    ghost = scope.node("source")
    assert ghost is not None and ghost.ghost_kind == GHOST_CONTEXT
    assert "source" in client.get_calls


async def test_an_out_of_set_follow_on_is_not_ghosted() -> None:
    """Context is added upstream only: a follow-on the filter excluded stays out."""
    source = task("source")
    master = [source]
    client = RecordingClient(
        dataset(
            [source, task("follow-on", status="completed")],
            [("source", "follow-on", "discovered_from")],
        )
    )

    scope = await project_scope(client, master)

    assert scope.node_ids == ("source",)
    assert scope.edges == ()
    assert client.get_calls == []


# ── Edge states ────────────────────────────────────────────────────────


async def test_an_open_to_completed_edge_is_inactive_dependent_resolved() -> None:
    """An edge into a resolved dependent constrains nothing now (D6)."""
    master = [task("a"), task("done", status="completed")]
    client = RecordingClient(dataset(master, [("a", "done", "blocks")]))

    scope = await project_scope(client, master, include_resolved=True)

    assert state_of(scope, "a", "done") == (EDGE_INACTIVE, REASON_DEPENDENT_RESOLVED)
    assert scope.active_edges == ()


async def test_a_completed_predecessor_is_satisfied_whatever_the_dependent_did() -> (
    None
):
    """`satisfied` takes precedence: the dependency WAS met either way."""
    master = [
        task("done-pred", status="completed"),
        task("open-dep"),
        task("done-dep", status="completed"),
    ]
    client = RecordingClient(
        dataset(
            master,
            [("done-pred", "open-dep", "blocks"), ("done-pred", "done-dep", "blocks")],
        )
    )

    scope = await project_scope(client, master, include_resolved=True)

    assert state_of(scope, "done-pred", "open-dep") == (
        EDGE_INACTIVE,
        REASON_SATISFIED,
    )
    assert state_of(scope, "done-pred", "done-dep") == (
        EDGE_INACTIVE,
        REASON_SATISFIED,
    )


async def test_the_active_projection_keeps_open_and_cancelled_predecessors() -> None:
    """A cancelled predecessor blocks forever; a completed one blocks nothing."""
    master = [task("open-pred"), task("dead-pred", status="cancelled"), task("dep")]
    client = RecordingClient(
        dataset(
            master, [("open-pred", "dep", "blocks"), ("dead-pred", "dep", "blocks")]
        )
    )

    scope = await project_scope(client, master, include_resolved=True)

    assert {(e.from_task_id, e.to_task_id) for e in scope.active_edges} == {
        ("open-pred", "dep"),
        ("dead-pred", "dep"),
    }


@pytest.mark.parametrize(
    ("predecessor", "dependent", "expected"),
    [
        ("open", "open", (EDGE_ACTIVE, "")),
        ("cancelled", "open", (EDGE_ACTIVE, "")),
        ("completed", "open", (EDGE_INACTIVE, REASON_SATISFIED)),
        ("completed", "cancelled", (EDGE_INACTIVE, REASON_SATISFIED)),
        ("open", "completed", (EDGE_INACTIVE, REASON_DEPENDENT_RESOLVED)),
        ("open", "cancelled", (EDGE_INACTIVE, REASON_DEPENDENT_RESOLVED)),
        ("open", UNKNOWN_STATUS, (EDGE_UNKNOWN, "")),
        (UNKNOWN_STATUS, "open", (EDGE_UNKNOWN, "")),
    ],
)
def test_dependency_edge_state_reads_both_endpoints(
    predecessor: str, dependent: str, expected: tuple[str, str]
) -> None:
    assert dependency_edge_state(predecessor, dependent) == expected


# ── Isolation and completeness ─────────────────────────────────────────


async def test_a_task_with_only_a_parent_child_edge_is_isolated() -> None:
    """Hierarchy is not a dependency, so it does not rescue a task from the fold."""
    master = [task("epic", task_type="epic"), task("child"), task("blocker")]
    client = RecordingClient(
        dataset(
            master, [("epic", "child", "parent_child"), ("blocker", "epic", "blocks")]
        )
    )

    scope = await project_scope(client, master)

    assert scope.isolated == ("child",)


async def test_a_task_whose_edge_read_failed_is_incomplete_and_never_isolated() -> None:
    """ "No dependency edges" is a claim, and a failed read is not evidence for it.

    ``as_of`` is the OLDEST contributing entry, so a graph assembled mostly
    from warm entries cannot present itself as current — and the node that
    contributed nothing contributes no timestamp either.
    """
    clock = StepClock()
    cache = GraphCache(clock=clock)
    # `b` has no edges anyone else reports, so its own failed read is the ONLY
    # thing between it and the isolated fold — which is the point: an edge it
    # does have would be invisible in exactly this state.
    master = [task("a"), task("b"), task("c")]
    warm = RecordingClient(dataset(master, [("a", "c", "blocks")]))
    await project_scope(warm, [master[0]], cache=cache)
    warmed_at = clock.now

    clock.advance(seconds=5)
    client = RecordingClient(
        dataset(master, [("a", "c", "blocks")]), edge_failures={"b"}
    )
    scope = await project_scope(client, master, cache=cache)

    assert scope.incomplete == {"b": "RuntimeError"}
    assert scope.isolated == ()
    node = scope.node("b")
    assert node is not None and node.completeness == COMPLETENESS_EDGES_UNKNOWN
    assert scope.as_of == warmed_at


async def test_the_payload_carries_roots_and_per_node_completeness() -> None:
    master = [task("a"), task("b"), task("loner")]
    client = RecordingClient(dataset(master, [("a", "b", "blocks")]))

    scope = await project_scope(client, master)

    assert set(scope.roots) == {"a", "loner"}
    assert {node.id: node.completeness for node in scope.nodes} == {
        "a": "ok",
        "b": "ok",
        "loner": "ok",
    }
