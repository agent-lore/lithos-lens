"""T2 slice A2 — the pure graph topology: SCC, layers, chain, hierarchy.

Table-driven where the rule has cases (edge classification, layering, the
chain's bound) and fixture-driven where the rule has a shape (the depth-5 DAG,
the cross-scope cycle). Every case names the D-number it pins, because the
interesting assertions here are the ones that look wrong until you read the
decision: a cyclic condensation counting ONCE, a completed three-chain losing
to an open two-chain, and a disconnected unknown edge lowering the bound of a
chain it is nowhere near.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Any

import pytest

from lithos_lens.graph_layout import (
    DependencyEdge,
    build_topology,
    classify_dependency_edges,
    hierarchy_rows,
    longest_blocking_chain,
)
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord, EdgeRecord
from lithos_lens.tasks import TaskRecord, TaskStatusName

# Creation order is the tie-break everything in this module falls back on, so
# fixtures space their tasks a day apart in the order their ids read.
_CREATED = {
    letter: f"2026-08-{index + 1:02d}T09:00:00+00:00"
    for index, letter in enumerate("ABCDEFGHIJ")
}


def _task(
    task_id: str,
    *,
    status: TaskStatusName = "open",
    created_at: str = "",
    task_type: str = "task",
) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        title=f"Title {task_id}",
        status=status,
        task_type=task_type,
        created_at=created_at or _CREATED.get(task_id, f"2026-08-01T09:00:0{0}+00:00"),
    )


def _tasks(*specs: str) -> list[TaskRecord]:
    """``"A"`` / ``"B:completed"`` shorthand for a node set."""
    built = []
    for spec in specs:
        task_id, _, status = spec.partition(":")
        built.append(_task(task_id, status=status or "open"))  # type: ignore[arg-type]
    return built


def _dated(*specs: str) -> list[TaskRecord]:
    """``"A@3"`` is task A created on 2026-08-03, open.

    ``_tasks`` dates A, B, C... in id order, which cannot tell the required
    ``(created_at, id)`` apart from a plain id sort. These fixtures pull the
    two APART — the winner's id sorts later than the loser's — and repeating a
    day pins the id fallback.
    """
    built = []
    for spec in specs:
        task_id, _, day = spec.partition("@")
        stamp = f"2026-08-{int(day):02d}T09:00:00+00:00"
        built.append(_task(task_id, created_at=stamp))
    return built


def _edge(spec: str, edge_type: str = "blocks") -> EdgeRecord:
    """``"A>B"`` is "A blocks B" — both dependency types point blocker -> blocked."""
    predecessor, _, dependent = spec.partition(">")
    return EdgeRecord(
        from_task_id=predecessor,
        to_task_id=dependent,
        type=edge_type,
        created_at="2026-08-01T09:00:00+00:00",
    )


def _edges(*specs: str) -> list[EdgeRecord]:
    return [_edge(spec) for spec in specs]


def _cycle_flag(
    task: TaskRecord, message: str = "cycle: A -> B -> A"
) -> BlockedTaskRecord:
    return BlockedTaskRecord(
        task=task,
        blockers=(BlockerRecord(kind="cycle", task_id=task.id, message=message),),
    )


def _layer_ids(topology) -> list[list[str]]:
    return [list(layer) for layer in topology.layers]


# --------------------------------------------------------------------------
# D6 — dependency edge state, classified from BOTH endpoints
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("predecessor", "dependent", "state", "reason"),
    [
        ("open", "open", "active", ""),
        # A cancelled predecessor blocks forever: T1's unsatisfiable case.
        ("cancelled", "open", "active", ""),
        ("completed", "open", "inactive", "satisfied"),
        ("open", "completed", "inactive", "dependent_resolved"),
        ("open", "cancelled", "inactive", "dependent_resolved"),
        # Satisfied takes precedence: the dependency was MET, whatever the
        # dependent then did.
        ("completed", "completed", "inactive", "satisfied"),
        ("completed", "cancelled", "inactive", "satisfied"),
    ],
)
def test_edge_state_from_both_endpoints(
    predecessor: str, dependent: str, state: str, reason: str
) -> None:
    edges = classify_dependency_edges(
        _edges("A>B"), _tasks(f"A:{predecessor}", f"B:{dependent}")
    )
    assert edges == (
        DependencyEdge("A", "B", "blocks", state, reason),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("unknown", ["A", "B"])
def test_unknown_status_endpoint_makes_the_edge_unknown(unknown: str) -> None:
    """D2: a ghost whose ``task_get`` failed unclassifies its edges either way."""
    edges = classify_dependency_edges(
        _edges("A>B"), _tasks("A", "B"), unknown_status=[unknown]
    )
    assert [(edge.state, edge.reason) for edge in edges] == [("unknown", "")]


def test_only_dependency_edge_types_are_classified() -> None:
    edges = classify_dependency_edges(
        [
            _edge("A>B", "blocks"),
            _edge("A>B", "waits_on_gate"),
            _edge("A>B", "parent_child"),
            _edge("A>B", "discovered_from"),
            # The cache fetches direction="both", so an in-scope edge arrives twice.
            _edge("A>B", "blocks"),
        ],
        _tasks("A", "B"),
    )
    assert [edge.type for edge in edges] == ["blocks", "waits_on_gate"]


# --------------------------------------------------------------------------
# D4 — cycles and condensed layering
# --------------------------------------------------------------------------


def test_cycle_and_its_dependent_each_get_a_layer() -> None:
    """A -> B -> A with C blocked by B: [{A,B} cycle], [C blocked-via-cycle]."""
    topology = build_topology(_tasks("A", "B", "C"), _edges("A>B", "B>A", "B>C"))

    assert _layer_ids(topology) == [["A"], ["C"]]
    (cycle,) = topology.cycles
    assert cycle.members == ("A", "B")
    assert cycle.path == ("A", "B", "A")
    assert cycle.scc is True
    assert topology.condensation_of("B") is topology.condensations[0]
    assert topology.condensations[0].blocked_via_cycle is False
    assert topology.condensations[1].blocked_via_cycle is True
    # A cycle has no in-degree-zero member, so it contributes its own root.
    assert topology.roots == ("A",)


def test_dag_of_depth_four_yields_four_layers() -> None:
    topology = build_topology(_tasks("A", "B", "C", "D"), _edges("A>B", "B>C", "C>D"))

    assert _layer_ids(topology) == [["A"], ["B"], ["C"], ["D"]]
    assert topology.cycles == ()
    assert topology.roots == ("A",)


def test_ghost_with_only_outgoing_edges_is_in_layer_zero() -> None:
    """D5: a one-hop ghost predecessor (a node record like any other) is a root."""
    topology = build_topology(_tasks("A", "B", "C"), _edges("A>B", "B>C"))

    assert _layer_ids(topology) == [["A"], ["B"], ["C"]]
    assert topology.roots == ("A",)


def test_edge_endpoint_with_no_record_is_a_node_of_unknown_status() -> None:
    """Assuming a status would either inflate the chain or hide a live blocker."""
    topology = build_topology(_tasks("B"), _edges("A>B"))

    assert topology.nodes == ("A", "B")
    assert [edge.state for edge in topology.edges] == ["unknown"]


@pytest.mark.parametrize(
    ("label", "specs", "path"),
    [
        # A self-loop closes on itself: the path is ``(A, A)``, not ``(A,)``.
        ("self-loop", ("A>A",), ("A", "A")),
        ("two-cycle", ("A>B", "B>A"), ("A", "B", "A")),
        ("three-cycle", ("A>B", "B>C", "C>A"), ("A", "B", "C", "A")),
    ],
)
def test_every_cycle_shape_is_one_condensation(
    label: str, specs: tuple[str, ...], path: tuple[str, ...]
) -> None:
    members = sorted({end for spec in specs for end in spec.split(">")})
    topology = build_topology(_tasks(*members), _edges(*specs))

    (cycle,) = topology.cycles
    assert cycle.members == tuple(members)
    assert cycle.scc is True
    assert cycle.path == path
    # One condensation, so one layer, however many members it holds.
    assert _layer_ids(topology) == [["A"]]


def test_scc_renders_identically_under_reversed_edge_order() -> None:
    """D4: the render is a function of the graph, not of the fetch order.

    Compared WHOLE, not field by field: ``Topology.edges`` is emitted to the
    payload like everything else, so an arrival-ordered edge list would make
    two renders of one graph unequal even with identical cycles and layers.

    A and B are joined by BOTH dependency types, which is the only pair the
    endpoint order cannot separate — drop the type from the edge sort and a
    stable sort leaves that pair in fetch order, so reversing the input
    reverses those two rows while every other field still matches.
    """
    tasks = _tasks("A", "B", "C")
    edges = [_edge("A>B"), _edge("A>B", "waits_on_gate"), _edge("B>C"), _edge("C>A")]
    forward = build_topology(tasks, edges)
    reversed_input = build_topology(list(reversed(tasks)), list(reversed(edges)))

    assert forward == reversed_input
    assert forward.cycles[0].members == ("A", "B", "C")
    assert forward.cycles[0].path == ("A", "B", "C", "A")
    # Edges come back in ``(created_at, id)`` order of predecessor, then
    # dependent, then type — whichever end of the fetch they arrived from.
    assert [
        (edge.from_task_id, edge.to_task_id, edge.type) for edge in forward.edges
    ] == [
        ("A", "B", "blocks"),
        ("A", "B", "waits_on_gate"),
        ("B", "C", "blocks"),
        ("C", "A", "blocks"),
    ]


def test_scc_members_and_path_follow_created_at_before_id() -> None:
    """D4's order is ``(created_at, id)`` all the way down.

    A forks to B and C, both of which close back on it. C is named later and
    created EARLIER, so it leads the member list and the DFS tries it first —
    an id sort would answer ``("A", "B", ...)`` to both.
    """
    tasks = _dated("A@1", "B@3", "C@2")
    specs = ("A>B", "A>C", "B>A", "C>A")
    topology = build_topology(tasks, _edges(*specs))
    reversed_input = build_topology(
        list(reversed(tasks)), list(reversed(_edges(*specs)))
    )

    (cycle,) = topology.cycles
    assert cycle.members == ("A", "C", "B")
    assert cycle.path == ("A", "C", "A")
    assert topology == reversed_input
    # The edge order is the members' order too: C before B, created before it.
    assert [(edge.from_task_id, edge.to_task_id) for edge in topology.edges] == [
        ("A", "C"),
        ("A", "B"),
        ("C", "A"),
        ("B", "A"),
    ]


def test_lithos_flagged_member_with_no_scc_is_condensed_alone_and_marked() -> None:
    """D4: the cross-scope cycle Lens cannot see is still layered and marked."""
    tasks = _tasks("A", "C")
    topology = build_topology(
        tasks,
        # A's cycle closes through ghosts whose edges Lens never fetches.
        _edges("A>C"),
        blocked=[_cycle_flag(tasks[0], "cycle: A -> ghost -> A")],
    )

    (cycle,) = topology.cycles
    assert cycle.members == ("A",)
    assert cycle.scc is False
    assert cycle.flagged is True
    assert cycle.path == ()
    assert cycle.message == "cycle: A -> ghost -> A"
    assert _layer_ids(topology) == [["A"], ["C"]]
    downstream = topology.condensation_of("C")
    assert downstream is not None and downstream.blocked_via_cycle is True
    assert topology.roots == ("A",)


def test_lithos_flag_on_an_scc_member_keeps_the_drawable_shape() -> None:
    tasks = _tasks("A", "B")
    topology = build_topology(
        tasks, _edges("A>B", "B>A"), blocked=[_cycle_flag(tasks[1])]
    )

    (cycle,) = topology.cycles
    assert (cycle.scc, cycle.flagged) == (True, True)
    assert cycle.members == ("A", "B")
    assert cycle.path == ("A", "B", "A")


def test_layers_use_every_dependency_edge_whatever_its_state() -> None:
    """D6: layers describe the planned sequence, so a satisfied edge still orders."""
    topology = build_topology(
        _tasks("A:completed", "B", "C"), _edges("A>B", "B>C"), unknown_status=["C"]
    )

    assert _layer_ids(topology) == [["A"], ["B"], ["C"]]
    assert [edge.state for edge in topology.edges] == ["inactive", "unknown"]


def test_roots_list_every_in_degree_zero_condensation() -> None:
    topology = build_topology(_tasks("A", "B", "C", "D"), _edges("A>C", "B>C", "C>D"))

    assert topology.roots == ("A", "B")


def test_a_cyclic_condensation_is_a_root_even_with_an_incoming_edge() -> None:
    """D4: a cycle contributes a root of its own on top of the in-degree-zero
    ones. Here C feeds the cycle, so ``{A, B}`` is NOT in-degree-zero — and a
    layout given no root inside it draws the cycle from wherever it starts."""
    topology = build_topology(_tasks("A", "B", "C"), _edges("C>A", "A>B", "B>A"))

    (cycle,) = topology.cycles
    assert cycle.members == ("A", "B")
    assert _layer_ids(topology) == [["C"], ["A"]]
    assert topology.roots == ("C", "A")


# --------------------------------------------------------------------------
# D7 — the longest blocking chain
# --------------------------------------------------------------------------


def _depth_five() -> tuple[list[TaskRecord], list[EdgeRecord]]:
    """A -> B -> C -> D -> E, with a shorter F -> C branch beside it."""
    return (
        _tasks("A", "B", "C", "D", "E", "F"),
        _edges("A>B", "B>C", "C>D", "D>E", "F>C"),
    )


def test_longest_chain_of_the_depth_five_fixture() -> None:
    topology = build_topology(*_depth_five())
    chain = longest_blocking_chain(topology)

    assert chain.nodes == ("A", "B", "C", "D", "E")
    assert chain.length == 5
    assert chain.bound == "exact"


def test_chain_through_a_given_node_is_the_known_sub_chain() -> None:
    tasks, edges = _depth_five()
    topology = build_topology(tasks, edges)

    # C sits in layer 2; the chain through it is the whole five, and the chain
    # through the short branch F reaches C's descendants but not A and B.
    assert longest_blocking_chain(topology, through="C").nodes == (
        "A",
        "B",
        "C",
        "D",
        "E",
    )
    assert longest_blocking_chain(topology, through="F").nodes == ("F", "C", "D", "E")
    assert longest_blocking_chain(topology, through="E").nodes == (
        "A",
        "B",
        "C",
        "D",
        "E",
    )


def test_chain_through_an_unknown_id_is_empty_not_an_error() -> None:
    topology = build_topology(*_depth_five())

    assert longest_blocking_chain(topology, through="nope").nodes == ()


@pytest.mark.parametrize(
    ("label", "specs", "expected"),
    [
        # C is named after B and created BEFORE it, so the C branch wins: an
        # implementation that sorted on id alone would answer ("A", "B", "D").
        ("created_at wins", ("A@1", "B@3", "C@2", "D@5", "E@4"), ("A", "C", "E")),
        # Same day on both branches: the id is the documented fallback, so the
        # answer is pinned rather than left to the order the edges arrived in.
        ("id breaks the draw", ("A@1", "B@2", "C@2", "D@3", "E@3"), ("A", "B", "D")),
    ],
)
def test_chain_ties_break_on_created_at_then_id(
    label: str, specs: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    """Two equal-length branches: the smaller ``(created_at, id)`` wins each step."""
    topology = build_topology(_dated(*specs), _edges("A>B", "A>C", "B>D", "C>E"))

    assert longest_blocking_chain(topology).nodes == expected


def test_a_cyclic_condensation_counts_once_in_the_chain() -> None:
    topology = build_topology(
        _tasks("A", "B", "C", "D"), _edges("A>B", "B>C", "C>B", "C>D")
    )

    chain = longest_blocking_chain(topology)
    # B and C are one condensation, represented by B: A -> {B,C} -> D is 3.
    assert chain.nodes == ("A", "B", "D")
    assert chain.length == 3
    # And the chain says what each of those three HOLDS, because nothing else
    # in the render does: a client tracing it has to know the step into the
    # condensation may land on C.
    assert chain.members == (("A",), ("B", "C"), ("D",))
    # Focus resolves by MEMBERSHIP, so the member that is not the
    # representative names the same condensation and traces the same chain.
    for member in ("B", "C"):
        focused = longest_blocking_chain(topology, through=member)
        assert (focused.nodes, focused.length) == (chain.nodes, 3)


def test_chain_condenses_the_active_projection_not_the_all_edge_one() -> None:
    """D7: a cycle whose loop closes through a completed task is still live.

    ``A(open) -> B(open) -> C(completed) -> A`` is ONE all-edge SCC — which is
    what the layers must draw — but only ``A -> B`` is active, so the chain is
    the open two-chain and not a single collapsed node.
    """
    topology = build_topology(
        _tasks("A", "B", "C:completed"), _edges("A>B", "B>C", "C>A")
    )

    # The display condensation still holds all three: cycles/layers are D4's.
    assert _layer_ids(topology) == [["A"]]
    assert topology.cycles[0].members == ("A", "B", "C")

    chain = longest_blocking_chain(topology)
    assert chain.nodes == ("A", "B")
    assert chain.length == 2
    assert chain.bound == "exact"
    # …and the chain's own membership is the ACTIVE partition, not that one
    # (round-9 correctness f-001). A client mapping these ids through a node's
    # display cycle would call `A -> B` internal to a condensation and mark
    # completed C as on the chain.
    assert chain.members == (("A",), ("B",))


def test_focusing_a_node_on_the_longest_chain_keeps_that_chain() -> None:
    """Equal prefixes tie-break the way the chain reads: FORWARD, and on
    ``(created_at, id)``.

    ``B -> C -> F`` and ``A -> D -> F`` are equal-length, and B was created
    first, so B's branch is the global answer — an id sort would take A's.
    Focusing F must not switch branches, which it does if the tie-break starts
    from the step nearest the focus: D is the smaller immediate predecessor.
    """
    topology = build_topology(
        _dated("A@2", "B@1", "C@4", "D@3", "F@5"),
        _edges("B>C", "C>F", "A>D", "D>F"),
    )

    assert longest_blocking_chain(topology).nodes == ("B", "C", "F")
    assert longest_blocking_chain(topology, through="F").nodes == ("B", "C", "F")


def test_ghosts_count_toward_the_chain() -> None:
    """D7: a chain may start at a ghost, so the scope's ghost records count."""
    topology = build_topology(_tasks("A", "B", "C"), _edges("A>B", "B>C"))

    assert longest_blocking_chain(topology).nodes == ("A", "B", "C")


def test_completed_three_chain_loses_to_the_open_two_chain() -> None:
    """D7: inactive edges are not in the projection, so they never dominate."""
    topology = build_topology(
        _tasks("A:completed", "B:completed", "C:completed", "D", "E"),
        _edges("A>B", "B>C", "D>E"),
    )

    chain = longest_blocking_chain(topology)
    assert chain.nodes == ("D", "E")
    assert chain.bound == "exact"


def test_incomplete_node_flags_the_chain_as_a_lower_bound() -> None:
    tasks, edges = _depth_five()
    topology = build_topology(tasks, edges, incomplete=["D"])

    chain = longest_blocking_chain(topology)
    assert chain.nodes == ("A", "B", "C", "D", "E")
    assert chain.bound == "lower_bound"


def test_incomplete_node_off_the_chain_still_lowers_the_bound() -> None:
    """D7: the bound is a property of the SCOPE, not of the selected chain.

    G is disconnected — nowhere near the five the chain returns — and its
    unread edges could still have been the longer chain, exactly as a
    disconnected ``unknown`` edge could.
    """
    tasks, edges = _depth_five()
    topology = build_topology([*tasks, _task("G")], edges, incomplete=["G"])

    chain = longest_blocking_chain(topology)
    assert chain.nodes == ("A", "B", "C", "D", "E")
    assert chain.bound == "lower_bound"


def test_unknown_ghost_blocker_lowers_the_bound_and_stays_off_the_chain() -> None:
    """D6: an unknown edge is counted in neither direction, so it is not walked."""
    topology = build_topology(
        _tasks("A", "B"), _edges("G>A", "A>B"), unknown_status=["G"]
    )

    chain = longest_blocking_chain(topology)
    assert chain.nodes == ("A", "B")
    assert chain.bound == "lower_bound"
    assert "G" not in chain.nodes


def test_disconnected_unknown_edge_lowers_the_global_bound() -> None:
    """A known chain of 1 beside an unknown X -> Y reports ">= 1", not "1"."""
    topology = build_topology(_tasks("A", "X"), _edges("X>Y"), unknown_status=["Y"])

    chain = longest_blocking_chain(topology)
    assert chain.length == 1
    assert chain.bound == "lower_bound"


def test_empty_scope_has_no_chain() -> None:
    chain = longest_blocking_chain(build_topology([], []))

    assert (chain.nodes, chain.length, chain.bound) == ((), 0, "exact")


def test_epic_fixture_completed_predecessor_leaves_the_chain_and_the_ancestry() -> None:
    """An epic graph keeps closed children; open A -> completed B is not blocking."""
    tasks = _tasks("A", "B:completed", "C", "D")
    edges = _edges("A>B", "C>D")
    topology = build_topology(tasks, edges)

    assert longest_blocking_chain(topology).nodes == ("C", "D")
    assert longest_blocking_chain(topology, through="A").nodes == ("A",)

    # Reopening B (the fake's own move) makes the edge active again.
    reopened = build_topology(_tasks("A", "B", "C", "D"), edges)
    assert longest_blocking_chain(reopened, through="A").nodes == ("A", "B")


# --------------------------------------------------------------------------
# The hierarchy tree
# --------------------------------------------------------------------------


def test_hierarchy_tree_is_indented_and_ordered_by_created_at() -> None:
    rows = hierarchy_rows(
        _tasks("A", "B", "C", "D", "E"),
        [
            _edge("A>C", "parent_child"),
            _edge("A>B", "parent_child"),
            _edge("B>D", "parent_child"),
            # Not hierarchy: must not shape the tree.
            _edge("D>E", "blocks"),
        ],
    )

    assert [(row.task_id, row.depth) for row in rows] == [
        ("A", 0),
        ("B", 1),
        ("D", 2),
        ("C", 1),
        ("E", 0),
    ]
    assert [row.task_id for row in rows if row.has_children] == ["A", "B"]


def test_hierarchy_ignores_a_parent_outside_the_scope() -> None:
    rows = hierarchy_rows(_tasks("B"), [_edge("A>B", "parent_child")])

    assert [(row.task_id, row.depth) for row in rows] == [("B", 0)]


def test_hierarchy_walk_refuses_to_revisit_a_malformed_loop() -> None:
    rows = hierarchy_rows(
        _tasks("A", "B"),
        [_edge("A>B", "parent_child"), _edge("B>A", "parent_child")],
    )

    # Every parented node, no root: the walk still terminates and shows both.
    assert sorted(row.task_id for row in rows) == ["A", "B"]


# ── security: bounded representative-path walk (security/f-001) ────────


def _exponential_scc(depth: int) -> tuple[list[TaskRecord], list[EdgeRecord]]:
    """One SCC whose closing edge sits behind ``2**depth`` dead-end descents.

    ``s -> m``, then ``m`` branches into a trap lattice (``t_i`` forking through
    ``u_i``/``v_i`` into ``t_{i+1}``, the last one pointing back at ``m``) and
    into ``w -> s``, the only edge that closes the cycle. ``created_at`` is
    assigned so ``s`` is the smallest member — hence the walk's start — and
    ``w`` the largest, so the sorted adjacency offers ``m`` the trap first.
    """
    order = ["s", "m"]
    edges = [_edge("s>m"), _edge("m>t0"), _edge(f"t{depth}>m")]
    for index in range(depth):
        order += [f"t{index}", f"u{index}", f"v{index}"]
        edges += _edges(
            f"t{index}>u{index}",
            f"t{index}>v{index}",
            f"u{index}>t{index + 1}",
            f"v{index}>t{index + 1}",
        )
    order += [f"t{depth}", "w"]
    edges += [_edge("m>w"), _edge("w>s")]
    base = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    tasks = [
        _task(task_id, created_at=(base + timedelta(minutes=index)).isoformat())
        for index, task_id in enumerate(order)
    ]
    return tasks, edges


# Budget for one ``build_topology``, in executed lines of the module under
# test per node-plus-edge. The bounded walk costs about 64 (11.3k lines for
# this fixture's 177 elements), so 400 leaves six times the headroom for
# ordinary refactoring while still being LINEAR — which is the whole claim.
_LINES_PER_ELEMENT = 400


class _LineBudget:
    """Trips as soon as ``graph_layout`` executes more than ``limit`` lines.

    The structural oracle for the bounded walk. Exhaustive backtracking is not
    "slower" than the fix, it is a different complexity class, so the
    assertion is on the WORK DONE and not on the clock: a descheduled
    container cannot fail it, and a reintroduced blowup fails it in
    milliseconds rather than hanging the suite for the ``2**24`` descents it
    would otherwise attempt.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.lines = 0
        self._previous: Any = None

    def __enter__(self) -> _LineBudget:
        self._previous = sys.gettrace()
        sys.settrace(self)
        return self

    def __exit__(self, *_: object) -> None:
        sys.settrace(self._previous)

    # ``Any`` returns, not ``object``: sys.settrace's own signature is
    # recursive (a trace function returns a trace function), which is not
    # expressible here, and ``object`` would not be assignable to it.
    def __call__(self, frame: FrameType, event: str, arg: object) -> Any:
        """Global hook: ask for line events from the module under test only."""
        if frame.f_globals.get("__name__") == "lithos_lens.graph_layout":
            return self._count
        return None

    def _count(self, frame: FrameType, event: str, arg: object) -> Any:
        self.lines += 1
        if self.lines > self.limit:
            raise AssertionError(
                f"graph_layout executed over {self.limit} lines on a "
                "graph of this size: the representative-path walk is no "
                "longer bounded by the component"
            )
        return self._count


def test_representative_path_walk_is_bounded_by_the_component_size() -> None:
    """Regression for the exhaustive-backtracking DoS: un-marking a node when
    the walk backtracked made the path search enumerate simple paths, so an
    agent-written cycle of ~80 tasks (well inside ``[graph].max_tasks``) burned
    minutes of event loop per render. Keeping the visited mark bounds it at one
    expansion per member. Black-box through ``build_topology``, the entry point
    a page scope actually reaches, asserting the answer is unchanged AND that
    the work stayed linear — the trap costs nothing to skip, only to explore."""
    tasks, edges = _exponential_scc(24)
    budget = _LineBudget(_LINES_PER_ELEMENT * (len(tasks) + len(edges)))

    with budget:
        topology = build_topology(tasks, edges)

    (cycle,) = topology.cycles
    assert cycle.path == ("s", "m", "w", "s")
    assert cycle.members[0] == "s"
    assert len(cycle.members) == len(tasks)
    assert budget.lines <= budget.limit
