"""T1 slice 1 — Lithos client graph reads + data model.

Covers the new normalizers (BlockerRecord, EdgeRecord, TaskRecord graph
fields) and the five new LithosClient graph-read tool methods. The headline
acceptance criterion is that the normalizers round-trip all four blocker kinds
and all four edge types.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lithos_lens.config import LithosConfig
from lithos_lens.lithos_client import LithosClient, LithosToolError
from lithos_lens.tasks import (
    BlockedTaskRecord,
    BlockerRecord,
    EdgeRecord,
    normalize_blocked_task,
    normalize_blocker,
    normalize_edge,
    normalize_task,
)

# --- Normalizers: blocker kinds -------------------------------------------


def test_normalize_blocker_task_kind_round_trips() -> None:
    blocker = normalize_blocker(
        {
            "kind": "task",
            "task_id": "dep-1",
            "title": "Design schema",
            "status": "open",
        }
    )
    assert blocker == BlockerRecord(
        kind="task", task_id="dep-1", title="Design schema", status="open"
    )


def test_normalize_blocker_gate_kind_round_trips() -> None:
    blocker = normalize_blocker(
        {
            "kind": "gate",
            "task_id": "gate-7",
            "title": "Human review",
            "status": "open",
            "gate_type": "human",
        }
    )
    assert blocker == BlockerRecord(
        kind="gate",
        task_id="gate-7",
        title="Human review",
        status="open",
        gate_type="human",
    )


def test_normalize_blocker_unsatisfiable_kind_round_trips() -> None:
    blocker = normalize_blocker(
        {
            "kind": "blocker_unsatisfiable",
            "task_id": "old-spike",
            "title": "Old spike",
            "status": "cancelled",
        }
    )
    assert blocker.kind == "blocker_unsatisfiable"
    assert blocker.title == "Old spike"
    assert blocker.status == "cancelled"


def test_normalize_blocker_cycle_kind_round_trips() -> None:
    blocker = normalize_blocker(
        {
            "kind": "cycle",
            "message": "cycle: A → B → A",
            "members": ["A", "B"],
        }
    )
    assert blocker.kind == "cycle"
    assert blocker.message == "cycle: A → B → A"
    assert blocker.members == ("A", "B")


def test_normalize_blocker_unknown_kind_defaults_to_task() -> None:
    assert normalize_blocker({"kind": "surprise"}).kind == "task"
    assert normalize_blocker({}).kind == "task"


# --- Normalizers: edge types ----------------------------------------------


@pytest.mark.parametrize(
    "edge_type", ["blocks", "parent_child", "discovered_from", "waits_on_gate"]
)
def test_normalize_edge_round_trips_every_type(edge_type: str) -> None:
    edge = normalize_edge(
        {
            "from_task_id": "a",
            "to_task_id": "b",
            "type": edge_type,
            "direction": "incoming",
            "metadata": {"note": "x"},
            "created_by": "planner",
            "created_at": "2026-04-26T10:00:00+00:00",
        }
    )
    assert edge == EdgeRecord(
        from_task_id="a",
        to_task_id="b",
        type=edge_type,  # type: ignore[arg-type]
        direction="incoming",
        metadata={"note": "x"},
        created_by="planner",
        created_at="2026-04-26T10:00:00+00:00",
    )


def test_normalize_edge_unknown_type_defaults_to_blocks() -> None:
    assert normalize_edge({"from_task_id": "a", "to_task_id": "b"}).type == "blocks"


# --- Normalizers: TaskRecord graph fields ---------------------------------


def test_normalize_task_reads_task_type_and_resolved_at() -> None:
    task = normalize_task(
        {
            "id": "epic-1",
            "title": "Auth rework",
            "task_type": "epic",
            "status": "completed",
            "resolved_at": "2026-04-27T09:00:00+00:00",
        }
    )
    assert task.task_type == "epic"
    assert task.resolved_at == "2026-04-27T09:00:00+00:00"


def test_normalize_task_defaults_graph_fields_for_older_payloads() -> None:
    task = normalize_task({"id": "t-1", "title": "Legacy"})
    assert task.task_type == "task"
    assert task.resolved_at == ""


def test_normalize_task_unknown_task_type_falls_back_to_task() -> None:
    assert normalize_task({"id": "t-1", "task_type": "bogus"}).task_type == "task"


def test_normalize_blocked_task_pairs_task_with_its_blockers() -> None:
    record = normalize_blocked_task(
        {
            "id": "t-blocked",
            "title": "Blocked work",
            "task_type": "task",
            "blockers": [
                {"kind": "task", "task_id": "dep-1", "title": "Predecessor"},
                {"kind": "gate", "task_id": "gate-1", "gate_type": "human"},
                "not-a-dict",
            ],
        }
    )
    assert record.task.id == "t-blocked"
    assert [blocker.kind for blocker in record.blockers] == ["task", "gate"]


# --- Client graph-read methods --------------------------------------------


class _StubClient(LithosClient):
    """LithosClient with the MCP transport stubbed out.

    Records each ``(tool, arguments)`` pair and returns a canned payload keyed
    by tool name, so the graph-read methods can be exercised end-to-end without
    a live session.
    """

    def __init__(self, payloads: dict[str, dict[str, Any]]) -> None:
        super().__init__(LithosConfig())
        self._payloads = payloads
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _call_tool(  # type: ignore[override]
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return self._payloads.get(name, {})


def _run(client: _StubClient, coro: Any) -> Any:
    async def _driver() -> Any:
        try:
            return await coro
        finally:
            await client.close()

    return asyncio.run(_driver())


def test_task_ready_sends_limit_and_claims_and_normalizes() -> None:
    client = _StubClient(
        {"lithos_task_ready": {"tasks": [{"id": "r-1", "title": "Ready one"}]}}
    )
    tasks = _run(client, client.task_ready(limit=500, with_claims=True))

    assert [task.id for task in tasks] == ["r-1"]
    name, arguments = client.calls[0]
    assert name == "lithos_task_ready"
    assert arguments == {"limit": 500, "with_claims": True}


def test_task_blocked_returns_blocked_records_with_structured_blockers() -> None:
    client = _StubClient(
        {
            "lithos_task_blocked": {
                "tasks": [
                    {
                        "id": "b-1",
                        "title": "Blocked one",
                        "blockers": [{"kind": "task", "task_id": "dep-1"}],
                    }
                ]
            }
        }
    )
    blocked = _run(client, client.task_blocked(limit=500))

    assert isinstance(blocked[0], BlockedTaskRecord)
    assert blocked[0].task.id == "b-1"
    assert blocked[0].blockers[0].kind == "task"
    assert client.calls[0] == ("lithos_task_blocked", {"limit": 500})


def test_task_get_returns_task_and_supports_task_envelope() -> None:
    client = _StubClient(
        {
            "lithos_task_get": {
                "task": {"id": "t-1", "title": "One", "task_type": "gate"}
            }
        }
    )
    task = _run(client, client.task_get("t-1"))

    assert task is not None
    assert task.id == "t-1"
    assert task.task_type == "gate"
    assert client.calls[0] == ("lithos_task_get", {"task_id": "t-1"})


def test_task_get_raises_with_code_on_not_found_envelope() -> None:
    client = _StubClient(
        {
            "lithos_task_get": {
                "status": "error",
                "code": "task_not_found",
                "message": "no such task",
            }
        }
    )
    with pytest.raises(LithosToolError) as excinfo:
        _run(client, client.task_get("missing"))
    assert excinfo.value.code == "task_not_found"


def test_task_children_sends_recursive_and_include_closed() -> None:
    client = _StubClient(
        {"lithos_task_children": {"tasks": [{"id": "c-1", "title": "Child"}]}}
    )
    children = _run(
        client,
        client.task_children("epic-1", recursive=True, include_closed=True),
    )

    assert [task.id for task in children] == ["c-1"]
    assert client.calls[0] == (
        "lithos_task_children",
        {"task_id": "epic-1", "recursive": True, "include_closed": True},
    )


def test_task_edge_list_sends_direction_and_types_and_normalizes() -> None:
    client = _StubClient(
        {
            "lithos_task_edge_list": {
                "edges": [
                    {
                        "from_task_id": "g-1",
                        "to_task_id": "t-1",
                        "type": "waits_on_gate",
                        "direction": "outgoing",
                    }
                ]
            }
        }
    )
    edges = _run(
        client,
        client.task_edge_list("g-1", direction="outgoing", types=["waits_on_gate"]),
    )

    assert edges[0].type == "waits_on_gate"
    assert client.calls[0] == (
        "lithos_task_edge_list",
        {"task_id": "g-1", "direction": "outgoing", "types": ["waits_on_gate"]},
    )
