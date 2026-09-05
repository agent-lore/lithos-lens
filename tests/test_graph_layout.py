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
    ("label", "specs"),
    [
        ("self-loop", ("A>A",)),
        ("two-cycle", ("A>B", "B>A")),
        ("three-cycle", ("A>B", "B>C", "C>A")),
    ],
)
def test_every_cycle_shape_is_one_condensation(
    label: str, specs: tuple[str, ...]
) -> None:
    members = sorted({end for spec in specs for end in spec.split(">")})
    topology = build_topology(_tasks(*members), _edges(*specs))

    (cycle,) = topology.cycles
    assert cycle.members == tuple(members)
    assert cycle.scc is True
    # One condensation, so one layer, however many members it holds.
    assert _layer_ids(topology) == [["A"]]


def test_scc_renders_identically_under_reversed_edge_order() -> None:
    tasks = _tasks("A", "B", "C")
    specs = ("A>B", "B>C", "C>A")
    forward = build_topology(tasks, _edges(*specs))
    reversed_input = build_topology(
        list(reversed(tasks)), list(reversed(_edges(*specs)))
    )

    assert forward.cycles == reversed_input.cycles
    assert forward.cycles[0].members == ("A", "B", "C")
    assert forward.cycles[0].path == ("A", "B", "C", "A")
    assert forward.layers == reversed_input.layers


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


def test_chain_ties_break_on_created_at_then_id() -> None:
    """Two equal-length branches: the smaller ``(created_at, id)`` wins each step."""
    topology = build_topology(
        _tasks("A", "B", "C", "D", "E"), _edges("A>B", "A>C", "B>D", "C>E")
    )

    assert longest_blocking_chain(topology).nodes == ("A", "B", "D")


def test_a_cyclic_condensation_counts_once_in_the_chain() -> None:
    topology = build_topology(
        _tasks("A", "B", "C", "D"), _edges("A>B", "B>C", "C>B", "C>D")
    )

    chain = longest_blocking_chain(topology)
    # B and C are one condensation, represented by B: A -> {B,C} -> D is 3.
    assert chain.nodes == ("A", "B", "D")
    assert chain.length == 3


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
