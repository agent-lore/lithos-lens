"""T1 slice 4 — the Gates section (``gates.py``).

Gates are open tasks Lithos keeps out of both workable frontiers, so this
module is a collector rather than a join: it reads gates off the master open
list, counts what each one blocks, and hands the board a self-refresh instant
for the timer gates. The tests below pin the three things that make that
honest — the waiter count is never narrowed, it always says which source it
came from, and the fan-out behind it is bounded — plus the hostile-metadata
paths, because every field a gate row renders is peer-written.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from lithos_lens.gates import (
    GATE_WAITER_FANOUT_CAP,
    GateWaiterState,
    attach_gate_waiters,
    collect_gates,
    group_gates,
    load_gates,
    next_gate_ready_at,
)
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord, EdgeRecord
from lithos_lens.tasks import TaskRecord

_NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


def _gate(
    task_id: str,
    *,
    gate_type: str = "human",
    created_at: str = "2026-08-01T09:00:00+00:00",
    ready_at: str = "",
    metadata: dict[str, Any] | None = None,
) -> TaskRecord:
    meta: dict[str, Any] = {"gate_type": gate_type}
    if ready_at:
        meta["ready_at"] = ready_at
    meta.update(metadata or {})
    return TaskRecord(
        id=task_id,
        title=f"Gate {task_id}",
        status="open",
        task_type="gate",
        created_at=created_at,
        metadata=meta,
    )


def _task(task_id: str) -> TaskRecord:
    return TaskRecord(id=task_id, title=f"Task {task_id}", status="open")


def _waits(waiter: TaskRecord, *gate_ids: str) -> BlockedTaskRecord:
    return BlockedTaskRecord(
        task=waiter,
        blockers=tuple(
            BlockerRecord(kind="gate", task_id=gate_id, type="waits_on_gate")
            for gate_id in gate_ids
        ),
    )


def _index(*tasks: TaskRecord) -> Mapping[str, TaskRecord]:
    return {task.id: task for task in tasks}


class _EdgeFake:
    """Counts ``task_edge_list`` calls and the peak concurrency they reach."""

    def __init__(
        self,
        edges: dict[str, list[EdgeRecord]] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self._edges = edges or {}
        self._fail = fail
        self._inflight = 0
        self.calls: list[dict[str, Any]] = []
        self.max_inflight = 0

    async def task_edge_list(
        self,
        task_id: str,
        *,
        direction: str = "both",
        types: list[str] | None = None,
    ) -> list[EdgeRecord]:
        self.calls.append({"task_id": task_id, "direction": direction, "types": types})
        self._inflight += 1
        self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            # Yield, so concurrent calls actually overlap and the peak above is
            # a real measurement rather than an artefact of serial execution.
            await asyncio.sleep(0)
            if self._fail:
                raise RuntimeError(f"edges unavailable for {task_id}")
            return list(self._edges.get(task_id, []))
        finally:
            self._inflight -= 1


# --- ordering and grouping (criterion 1) ---------------------------------


def test_gate_rows_lead_with_human_gates_then_group_oldest_first() -> None:
    """§5.2.3: human gates first — they are the work waiting on a PERSON — then
    the remaining types, each group ordered oldest-first and the groups
    themselves placed by their oldest member."""
    rows = collect_gates(
        [
            _gate("ci-new", gate_type="ci", created_at="2026-08-05T09:00:00+00:00"),
            _gate("human-new", created_at="2026-08-04T09:00:00+00:00"),
            _gate(
                "timer-old", gate_type="timer", created_at="2026-08-02T09:00:00+00:00"
            ),
            _gate("human-old", created_at="2026-08-03T09:00:00+00:00"),
            _task("not-a-gate"),  # a workable task is not a gate row
        ]
    )

    assert [row.task.id for row in rows] == [
        "human-old",
        "human-new",
        "timer-old",
        "ci-new",
    ]
    groups = group_gates(rows)
    assert [
        (group.gate_type, [row.task.id for row in group.rows]) for group in groups
    ] == [
        ("human", ["human-old", "human-new"]),
        # The timer group leads ci because its oldest gate has waited longer.
        ("timer", ["timer-old"]),
        ("ci", ["ci-new"]),
    ]


def test_a_gate_with_no_created_at_sorts_last_rather_than_oldest() -> None:
    """`normalize_task` defaults a missing `created_at` to `""`, and the empty
    string sorts BEFORE every real timestamp — so a gate the server never
    stamped would read as the one that has waited longest.

    Two places it lands, and the second is the one that matters. Within a group
    the unstamped row leads. Across groups a group is placed by its oldest
    member, so ONE unstamped gate promotes its whole type ahead of a type that
    has genuinely been waiting since January. "Oldest first" is the section's
    only ordering claim, and this is a field a peer writes; unknown must sort
    last, not first.
    """
    rows = collect_gates(
        [
            _gate("ci-jan", gate_type="ci", created_at="2026-01-01T09:00:00+00:00"),
            _gate("pr-aug", gate_type="pr", created_at="2026-08-28T09:00:00+00:00"),
            _gate("pr-unstamped", gate_type="pr", created_at=""),
            _gate("human-mar", created_at="2026-03-01T09:00:00+00:00"),
            _gate("human-unstamped", created_at=""),
        ]
    )

    # Within its group: known stamps first, oldest to newest, unknown last.
    assert [row.task.id for row in rows] == [
        "human-mar",
        "human-unstamped",
        "ci-jan",
        "pr-aug",
        "pr-unstamped",
    ]
    groups = group_gates(rows)
    assert [
        (group.gate_type, [row.task.id for row in group.rows]) for group in groups
    ] == [
        ("human", ["human-mar", "human-unstamped"]),
        # ci leads pr on January vs August. The unstamped pr gate must not
        # place its group by an absent stamp and jump the queue.
        ("ci", ["ci-jan"]),
        ("pr", ["pr-aug", "pr-unstamped"]),
    ]


def test_a_group_of_only_unstamped_gates_still_sorts_last() -> None:
    """The degenerate case the fix must not special-case its way past: with no
    known stamp anywhere in a group there is nothing to place it by, and it
    belongs after every group that does know when it started waiting."""
    groups = group_gates(
        collect_gates(
            [
                _gate("pr-unstamped", gate_type="pr", created_at=""),
                _gate("ci-jan", gate_type="ci", created_at="2026-01-01T09:00:00+00:00"),
            ]
        )
    )

    assert [group.gate_type for group in groups] == ["ci", "pr"]


def test_workable_tasks_and_already_placed_gates_are_not_collected() -> None:
    """Single placement (criterion 7): a gate the severity rules promoted into
    Needs attention must not ALSO render here."""
    rows = collect_gates(
        [_gate("promoted"), _gate("kept"), _task("work")],
        placed_ids=frozenset({"promoted"}),
    )

    assert [row.task.id for row in rows] == ["kept"]


# --- waiter counts and their honesty (criteria 2 and 4) ------------------


@pytest.mark.parametrize(
    ("waiter_ids", "expected"),
    [(("w1",), "blocks 1 task"), (("w1", "w2", "w3"), "blocks 3 tasks")],
)
def test_waiter_label_counts_outgoing_waits_on_gate_edges(
    waiter_ids: tuple[str, ...], expected: str
) -> None:
    """The count comes from the tasks Lithos reports as waiting on the gate,
    and the label is singular at one."""
    gate = _gate("g")
    waiters = [_task(waiter_id) for waiter_id in waiter_ids]
    rows = asyncio.run(
        attach_gate_waiters(
            _EdgeFake(),
            collect_gates([gate]),
            index=_index(gate, *waiters),
            blocked=[_waits(waiter, "g") for waiter in waiters],
            blocked_available=True,
            blocked_truncated=False,
        )
    )

    assert rows[0].waiters_label == expected
    assert rows[0].waiters_state is GateWaiterState.KNOWN
    assert [waiter.id for waiter in rows[0].waiters] == sorted(waiter_ids)


def test_truncated_blocked_read_says_at_least_rather_than_a_flat_count() -> None:
    """A truncated source cannot support an exact count, and the edge fallback
    it uses is peer-asserted — so the row says which one it got instead of
    presenting either as the whole picture."""
    gate = _gate("g")
    waiter = _task("w1")
    fake = _EdgeFake(
        {"g": [EdgeRecord(from_task_id="g", to_task_id="w1", type="waits_on_gate")]}
    )
    rows = asyncio.run(
        attach_gate_waiters(
            fake,
            collect_gates([gate]),
            index=_index(gate, waiter),
            blocked=[],
            blocked_available=True,
            blocked_truncated=True,
        )
    )

    assert rows[0].waiters_state is GateWaiterState.UNVERIFIED
    assert rows[0].waiters_label == "blocks 1 task (unverified)"

    # …and when the edge read fails too, the truncated blocked response is all
    # that is left: "at least N", never a confident N.
    rows = asyncio.run(
        attach_gate_waiters(
            _EdgeFake(fail=True),
            collect_gates([gate]),
            index=_index(gate, waiter),
            blocked=[_waits(waiter, "g")],
            blocked_available=True,
            blocked_truncated=True,
        )
    )
    assert rows[0].waiters_state is GateWaiterState.PARTIAL
    assert rows[0].waiters_label == "blocks at least 1 task"


def test_failed_waiter_sources_report_unavailable_never_a_number() -> None:
    """Blocked read down AND the edge read down: the row must say the count is
    unavailable. A "blocks 0 tasks" here would be a fabricated fact — the one
    reading an operator would act on."""
    gate = _gate("g")
    rows = asyncio.run(
        attach_gate_waiters(
            _EdgeFake(fail=True),
            collect_gates([gate]),
            index=_index(gate),
            blocked=[],
            blocked_available=False,
            blocked_truncated=False,
        )
    )

    assert rows[0].waiters_state is GateWaiterState.UNKNOWN
    assert rows[0].waiters_unknown is True
    assert rows[0].waiters_label == "waiter count unavailable"
    assert "blocks" not in rows[0].waiters_label
    # The empty body is qualified by the same source: only a COMPLETE read may
    # claim that nothing waits.
    assert "did not answer" in rows[0].waiters_empty_note


def test_empty_waiter_note_distinguishes_complete_from_truncated_sources() -> None:
    gate = _gate("g")
    complete = asyncio.run(
        attach_gate_waiters(
            _EdgeFake(),
            collect_gates([gate]),
            index=_index(gate),
            blocked=[],
            blocked_available=True,
            blocked_truncated=False,
        )
    )
    truncated = asyncio.run(
        attach_gate_waiters(
            _EdgeFake(fail=True),
            collect_gates([gate]),
            index=_index(gate),
            blocked=[],
            blocked_available=True,
            blocked_truncated=True,
        )
    )

    assert complete[0].waiters_empty_note == "No open tasks are waiting on this gate."
    assert "more may exist" in truncated[0].waiters_empty_note


# --- the fan-out bound (criterion 5) -------------------------------------


def test_healthy_path_issues_no_edge_reads_at_any_gate_count() -> None:
    """The blocked frontier is COMPUTED by Lithos and, when complete, already
    names every waiter — so the normal render costs zero extra calls however
    many gates a peer creates."""
    gates = [_gate(f"g{index:03d}") for index in range(200)]
    fake = _EdgeFake()
    rows = asyncio.run(
        attach_gate_waiters(
            fake,
            collect_gates(gates),
            index=_index(*gates),
            blocked=[],
            blocked_available=True,
            blocked_truncated=False,
        )
    )

    assert fake.calls == []
    assert len(rows) == 200
    assert all(row.waiters_state is GateWaiterState.KNOWN for row in rows)


@pytest.mark.parametrize("blocked_available", [True, False])
def test_degraded_paths_cap_the_edge_fanout_and_its_concurrency(
    blocked_available: bool,
) -> None:
    """Both degraded paths run a per-gate read, and the gate count is
    peer-controlled, so both are bounded twice: at most
    ``GATE_WAITER_FANOUT_CAP`` reads per render, at most 8 in flight."""
    gates = [_gate(f"g{index:03d}") for index in range(200)]
    fake = _EdgeFake()
    rows = asyncio.run(
        attach_gate_waiters(
            fake,
            collect_gates(gates),
            index=_index(*gates),
            blocked=[],
            blocked_available=blocked_available,
            blocked_truncated=True,
        )
    )

    assert len(fake.calls) == GATE_WAITER_FANOUT_CAP == 25
    assert fake.max_inflight <= 8
    assert fake.calls[0] == {
        "task_id": "g000",
        "direction": "outgoing",
        "types": ["waits_on_gate"],
    }
    # Gates past the cap degrade to the blocked-response fallback rather than
    # silently reporting an unread zero as fact.
    overflow = rows[GATE_WAITER_FANOUT_CAP]
    expected = GateWaiterState.PARTIAL if blocked_available else GateWaiterState.UNKNOWN
    assert overflow.waiters_state is expected


# --- hostile gate data (criterion 6) -------------------------------------


def test_unknown_gate_type_falls_back_to_a_vocabulary_slug() -> None:
    """``metadata`` is peer-writable, so the raw type never reaches a class
    list: a value outside KNOWN_GATE_TYPES collapses to ``unknown``, and the
    badge shows the raw string as escaped, length-capped TEXT."""
    hostile = collect_gates([_gate("g", gate_type='human" data-x="1')])[0]

    assert hostile.type_slug == "unknown"
    assert hostile.is_human is False
    assert hostile.type_label == 'human" data-x="1'

    long_type = collect_gates([_gate("g", gate_type="x" * 500)])[0]
    assert long_type.type_slug == "unknown"
    assert len(long_type.type_label) == 80


def test_overflowing_ready_at_renders_verbatim_and_drives_no_countdown() -> None:
    """The UTC shift of an in-range extreme raises OverflowError; one such value
    in one gate's metadata must not 500 every dashboard render."""
    overflowing = collect_gates(
        [_gate("g", gate_type="timer", ready_at="9999-12-31T23:59:59-01:00")]
    )[0]
    malformed = collect_gates([_gate("m", gate_type="timer", ready_at="soon")])[0]

    assert overflowing.ready_at == "9999-12-31T23:59:59-01:00"
    assert malformed.ready_at == "soon"
    assert next_gate_ready_at([overflowing, malformed], now=_NOW) == ""


def test_fabricated_waits_on_gate_edge_does_not_inflate_the_count() -> None:
    """``lithos_task_edge_upsert`` will link a gate to ANY existing task, so on
    the degraded paths every edge target is checked against what Lithos itself
    said elsewhere in this render. Everything the computed source could never
    report as waiting is dropped, and a repeated edge counts once."""
    gate = _gate("g")
    real = _task("real")
    epic = TaskRecord(id="an-epic", title="Epic", status="open", task_type="epic")
    other_gate = _gate("other-gate")
    fake = _EdgeFake(
        {
            "g": [
                EdgeRecord(from_task_id="g", to_task_id="real", type="waits_on_gate"),
                # A repeated edge must not double-count the same waiter.
                EdgeRecord(from_task_id="g", to_task_id="real", type="waits_on_gate"),
                # Names nothing in the open snapshot.
                EdgeRecord(from_task_id="g", to_task_id="ghost", type="waits_on_gate"),
                EdgeRecord(from_task_id="g", to_task_id="", type="waits_on_gate"),
                # Not workable: the frontier reads return ``task``-typed rows
                # only, so neither is a unit of work that can be waiting.
                EdgeRecord(
                    from_task_id="g", to_task_id="an-epic", type="waits_on_gate"
                ),
                EdgeRecord(
                    from_task_id="g", to_task_id="other-gate", type="waits_on_gate"
                ),
                # The edge outlives the wait: a finished waiter still has one.
                EdgeRecord(from_task_id="g", to_task_id="done", type="waits_on_gate"),
            ]
        }
    )
    rows = asyncio.run(
        attach_gate_waiters(
            fake,
            collect_gates([gate]),
            index=_index(gate, real, epic, other_gate),
            blocked=[],
            blocked_available=True,
            blocked_truncated=True,
        )
    )

    assert [waiter.id for waiter in rows[0].waiters] == ["real"]
    assert rows[0].waiters_label == "blocks 1 task (unverified)"


def test_non_workable_edge_targets_are_dropped_on_the_failed_blocked_path_too() -> None:
    """The same admissions apply when the blocked read is DOWN, not just
    truncated — that path reaches the identical fan-out."""
    gate = _gate("g")
    epic = TaskRecord(id="an-epic", title="Epic", status="open", task_type="epic")
    work = _task("work")
    fake = _EdgeFake(
        {
            "g": [
                EdgeRecord(
                    from_task_id="g", to_task_id="an-epic", type="waits_on_gate"
                ),
                EdgeRecord(from_task_id="g", to_task_id="work", type="waits_on_gate"),
            ]
        }
    )
    rows = asyncio.run(
        attach_gate_waiters(
            fake,
            collect_gates([gate]),
            index=_index(gate, epic, work),
            blocked=[],
            blocked_available=False,
            blocked_truncated=False,
        )
    )

    assert [waiter.id for waiter in rows[0].waiters] == ["work"]
    assert rows[0].waiters_label == "blocks 1 task (unverified)"


def test_no_frontier_response_is_used_to_refute_an_edge_asserted_waiter() -> None:
    """Regression (f-002): an accepted ``waits_on_gate`` edge CONSTITUTES the
    wait (``lithos_task_edge_upsert`` contract), and the edge list is read
    strictly after the frontier responses — so a task an earlier read called
    ready, or one absent from an earlier blocked response, is still a waiter.
    Dropping it would silently under-report the gate."""
    gate = _gate("g")
    victim = _task("victim")
    fake = _EdgeFake(
        {"g": [EdgeRecord(from_task_id="g", to_task_id="victim", type="waits_on_gate")]}
    )

    for blocked_available in (True, False):
        rows = asyncio.run(
            attach_gate_waiters(
                fake,
                collect_gates([gate]),
                index=_index(gate, victim),
                # An earlier blocked response that named no waiter at all.
                blocked=[],
                blocked_available=blocked_available,
                blocked_truncated=True,
            )
        )

        assert [waiter.id for waiter in rows[0].waiters] == ["victim"]
        assert rows[0].waiters_label == "blocks 1 task (unverified)"


def test_advisory_metadata_is_bounded_on_every_axis() -> None:
    """Key length, value length and pair count are all peer-controlled, so all
    three are capped; the remainder is COUNTED for the detail-page link rather
    than silently dropped."""
    row = collect_gates(
        [
            _gate(
                "g",
                metadata={
                    "a" * 300: "short",
                    "b": "v" * 300,
                    "c": {"nested": ["deep"] * 100},
                    "d": ["x"] * 100,
                    "e": "kept out of the row",
                    "f": "kept out of the row",
                },
            )
        ]
    )[0]

    assert len(row.advisory) == 3
    assert row.advisory_more == 3
    keys = [key for key, _ in row.advisory]
    values = [value for _, value in row.advisory]
    assert len(keys[0]) == 80
    assert len(values[1]) == 80
    # Containers are labelled, never stringified: str() on a megabyte-deep
    # value would allocate the whole thing on every render for 80 bytes.
    assert values[2] == "{…}"


def test_chrome_metadata_is_bounded_like_every_other_axis() -> None:
    """``gate_type`` and ``ready_at`` are peer-written like any other metadata
    key, and ``ready_at`` reaches the markup TWICE (``datetime=`` and
    ``data-gate-ready-at=``, neither truncated) before a once-a-second browser
    loop re-reads it — so both are capped, and a container is labelled rather
    than materialised to keep 80 bytes of it."""
    long_value = collect_gates(
        [
            _gate(
                "g",
                gate_type="timer",
                metadata={"ready_at": "x" * 1_000_000},
            )
        ]
    )[0]
    # Same 80-character bound the advisory chips get.
    assert len(long_value.ready_at) <= 80

    container = collect_gates(
        [
            TaskRecord(
                id="c",
                title="Gate c",
                status="open",
                task_type="gate",
                metadata={
                    "gate_type": {"nested": ["y" * 100] * 10_000},
                    "ready_at": [["z" * 100] * 10_000],
                },
            )
        ]
    )[0]
    assert container.ready_at == "[…]"
    assert container.gate_type == "{…}"
    assert container.type_slug == "unknown"
    # Still no countdown and no 500 — an unreadable stamp schedules nothing.
    assert next_gate_ready_at([long_value, container], now=_NOW) == ""


def test_chrome_metadata_keys_are_not_repeated_as_advisory_chips() -> None:
    row = collect_gates(
        [
            _gate(
                "g",
                gate_type="timer",
                ready_at="2026-09-01T00:00:00+00:00",
                metadata={"project": "influx", "repo": "lens"},
            )
        ]
    )[0]

    assert row.advisory == (("repo", "lens"),)
    assert row.advisory_more == 0


# --- the timer self-refresh instant (criterion 8) -------------------------


def test_next_ready_at_is_the_earliest_future_timer_gate() -> None:
    """One refresh, scheduled at min(ready_at) over the VISIBLE open timer
    gates: Lithos emits no event when a timer lapses, so this instant is the
    board's only way to notice."""
    soon = _gate(
        "soon", gate_type="timer", ready_at=(_NOW + timedelta(hours=1)).isoformat()
    )
    later = _gate(
        "later", gate_type="timer", ready_at=(_NOW + timedelta(days=3)).isoformat()
    )
    # A human gate carrying a ready_at is not a timer gate; it must not be the
    # instant the board wakes up for.
    human = _gate("human", ready_at=(_NOW + timedelta(minutes=1)).isoformat())

    assert (
        next_gate_ready_at(collect_gates([later, human, soon]), now=_NOW)
        == (_NOW + timedelta(hours=1)).isoformat()
    )


def test_lapsed_timer_gate_schedules_no_refresh() -> None:
    """A lapsed gate stays open until someone completes it, so a past instant
    would have the browser refresh on a loop against an attribute that never
    changes again."""
    lapsed = _gate(
        "lapsed", gate_type="timer", ready_at=(_NOW - timedelta(hours=1)).isoformat()
    )

    assert next_gate_ready_at(collect_gates([lapsed]), now=_NOW) == ""


def test_naive_ready_at_is_normalized_to_utc_for_the_browser() -> None:
    """The countdown is computed in the browser, where a naive stamp would be
    read as LOCAL time and count down to the wrong instant."""
    row = collect_gates(
        [_gate("g", gate_type="timer", ready_at="2026-08-29T18:00:00")]
    )[0]

    assert row.ready_at == "2026-08-29T18:00:00+00:00"


# --- the assembled section ------------------------------------------------


def test_load_gates_reports_unavailable_counts_as_a_page_error() -> None:
    gate = _gate("g")
    section = asyncio.run(
        load_gates(
            _EdgeFake(fail=True),
            visible_open=[gate],
            index=_index(gate),
            blocked=[],
            blocked_available=False,
            blocked_truncated=False,
            now=_NOW,
        )
    )

    assert section.count == 1
    assert section.errors == (
        "Could not load waiter counts for some gates; "
        "their counts are shown as unavailable.",
    )


def test_load_gates_on_a_gateless_board_is_empty_and_reads_nothing() -> None:
    fake = _EdgeFake()
    section = asyncio.run(
        load_gates(
            fake,
            visible_open=[_task("work")],
            index=_index(_task("work")),
            blocked=[],
            blocked_available=False,
            blocked_truncated=True,
            now=_NOW,
        )
    )

    assert section.groups == ()
    assert section.count == 0
    assert section.next_ready_at == ""
    assert fake.calls == []
