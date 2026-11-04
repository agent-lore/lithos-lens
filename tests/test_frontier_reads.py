"""``ReadGeneration``: the five related dashboard reads of one issue, the views
the join reads off it, and the one place a skew retry is accepted.

The load-level behaviour (what the board renders after a retry) is pinned in
``test_frontier.py``; these tests pin the bundle's own contract so that
``frontier.load_dashboard`` can rely on it without re-deriving it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from lithos_lens.frontier_reads import (
    RETRY_FAILED_ERROR,
    ReadGeneration,
    read_generation,
)
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord
from lithos_lens.tasks import TaskRecord


def _task(task_id: str, *, created_at: str = "", status: str = "open") -> TaskRecord:
    return TaskRecord(
        id=task_id,
        title=f"Title {task_id}",
        status=status,  # type: ignore[arg-type]
        task_type="task",
        tags=(),
        created_at=created_at,
        created_by="",
        metadata={},
        claims=(),
    )


def _blocked(task: TaskRecord) -> BlockedTaskRecord:
    return BlockedTaskRecord(
        task=task, blockers=(BlockerRecord(kind="task", task_id="p"),)
    )


def _generation(**slots: Any) -> ReadGeneration:
    fields: dict[str, Any] = {
        "open_result": [],
        "ready_result": [],
        "blocked_result": [],
        "completed_result": [],
        "cancelled_result": [],
    }
    fields.update(slots)
    return ReadGeneration(**fields)


def test_a_generation_whose_every_read_answered_is_complete() -> None:
    generation = _generation(open_result=[_task("a")], ready_result=[_task("a")])
    assert generation.complete
    assert generation.frontier_ok
    assert generation.blocked_ok


def test_any_failed_read_makes_the_generation_incomplete() -> None:
    for slot in (
        "open_result",
        "ready_result",
        "blocked_result",
        "completed_result",
        "cancelled_result",
    ):
        generation = _generation(**{slot: RuntimeError(slot)})
        assert not generation.complete, slot


def test_the_frontier_is_ok_only_when_both_frontier_reads_answered() -> None:
    ready_down = _generation(ready_result=RuntimeError("ready"))
    assert not ready_down.frontier_ok
    # A ready-only outage must not degrade the gate waiter counts, which
    # depend on the blocked read alone.
    assert ready_down.blocked_ok
    blocked_down = _generation(blocked_result=RuntimeError("blocked"))
    assert not blocked_down.frontier_ok
    assert not blocked_down.blocked_ok


def test_views_of_a_failed_read_are_empty_rather_than_the_exception() -> None:
    generation = _generation(
        open_result=RuntimeError("open"),
        ready_result=RuntimeError("ready"),
        blocked_result=RuntimeError("blocked"),
        completed_result=RuntimeError("completed"),
    )
    assert generation.open_snapshot == []
    assert generation.ready_rows == []
    assert generation.blocked_rows == []
    assert generation.terminal_index == {}


def test_the_open_snapshot_is_newest_first_and_whole() -> None:
    older = _task("older", created_at="2026-11-01T00:00:00+00:00")
    newer = _task("newer", created_at="2026-11-03T00:00:00+00:00")
    generation = _generation(open_result=[older, newer])
    assert [task.id for task in generation.open_snapshot] == ["newer", "older"]


def test_the_terminal_index_spans_both_windows_that_answered() -> None:
    done = _task("done", status="completed")
    dropped = _task("dropped", status="cancelled")
    generation = _generation(completed_result=[done], cancelled_result=[dropped])
    assert set(generation.terminal_index) == {"done", "dropped"}
    half = _generation(completed_result=RuntimeError("x"), cancelled_result=[dropped])
    assert set(half.terminal_index) == {"dropped"}


def test_record_failures_names_each_failed_read_once_in_read_order() -> None:
    errors: list[str] = []
    _generation(
        open_result=RuntimeError("open"),
        ready_result=RuntimeError("ready"),
        blocked_result=RuntimeError("blocked"),
        # The resolved windows report where their rows are drawn, not here.
        completed_result=RuntimeError("completed"),
    ).record_failures(errors)
    assert errors == [
        "Could not load open tasks.",
        "Could not load the ready frontier.",
        "Could not load the blocked frontier.",
    ]
    healthy: list[str] = []
    _generation().record_failures(healthy)
    assert healthy == []


def test_adopt_takes_a_complete_retry_whole() -> None:
    first = _generation(open_result=[_task("stale")])
    retried = _generation(
        open_result=[_task("fresh")], completed_result=[_task("stale")]
    )
    errors: list[str] = []
    assert first.adopt(retried, errors=errors) is retried
    assert errors == []


def test_adopt_keeps_the_first_generation_and_says_so_when_any_retry_read_failed() -> (
    None
):
    """A complete first generation beats a mix of it with a partial second,
    and the stripe must not call the board healthy over an unresolved skew."""
    first = _generation(open_result=[_task("stale")])
    for slot in ("open_result", "ready_result", "blocked_result", "cancelled_result"):
        retried = _generation(
            **{"open_result": [_task("fresh")], slot: RuntimeError(slot)}
        )
        errors: list[str] = []
        assert first.adopt(retried, errors=errors) is first, slot
        assert errors == [RETRY_FAILED_ERROR], slot


class _Reader:
    """Records the arguments of each read so a retry can be checked to re-ask
    the generation exactly as the first issue did."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tasks(self, **kwargs: Any) -> list[TaskRecord]:
        self.calls.append(("list_tasks", kwargs))
        return [_task("open-1")]

    async def task_ready(self, **kwargs: Any) -> list[TaskRecord]:
        self.calls.append(("task_ready", kwargs))
        return [_task("open-1")]

    async def task_blocked(self, **kwargs: Any) -> list[BlockedTaskRecord]:
        self.calls.append(("task_blocked", kwargs))
        raise RuntimeError("blocked is down")


def test_read_generation_issues_the_five_reads_together_and_keeps_each_answer() -> None:
    reader = _Reader()

    async def completed() -> list[TaskRecord]:
        return [_task("done", status="completed")]

    async def cancelled() -> list[TaskRecord]:
        raise RuntimeError("cancelled is down")

    generation = asyncio.run(
        read_generation(
            reader, frontier_limit=7, completed=completed(), cancelled=cancelled()
        )
    )

    assert reader.calls == [
        ("list_tasks", {"status": "open", "with_claims": True}),
        ("task_ready", {"limit": 7, "with_claims": False}),
        ("task_blocked", {"limit": 7}),
    ]
    assert [task.id for task in generation.open_snapshot] == ["open-1"]
    assert [task.id for task in generation.ready_rows] == ["open-1"]
    # A read that raised is kept AS the exception in its slot — the bundle
    # never substitutes an answer — and the views read it as empty.
    assert isinstance(generation.blocked_result, RuntimeError)
    assert generation.blocked_rows == []
    assert isinstance(generation.cancelled_result, RuntimeError)
    assert set(generation.terminal_index) == {"done"}
    assert not generation.complete
    assert not generation.frontier_ok
