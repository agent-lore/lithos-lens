"""T1 slice 1 — Lithos client graph reads + data model.

Covers the graph normalizers (BlockerRecord, EdgeRecord, TaskRecord graph
fields) and the five LithosClient graph-read tool methods. The headline
acceptance criterion is that the normalizers round-trip the real Lithos
payloads: the blocker fixtures below mirror, branch for branch, the dicts
emitted by ``_compute_blockers`` in lithos ``coordination.py`` — every branch
emits exactly ``{kind, task_id, type, status, message}``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lithos_lens.config import LithosConfig
from lithos_lens.lithos_client import LithosClient, LithosToolError
from lithos_lens.task_graph import (
    BlockedTaskRecord,
    BlockerRecord,
    EdgeRecord,
    normalize_blocked_task,
    normalize_blocker,
    normalize_edge,
)
from lithos_lens.tasks import normalize_task

# --- Normalizers: blocker kinds -------------------------------------------
#
# One fixture per branch of lithos ``_compute_blockers``. Field shapes (and
# message wording) are copied from the Lithos source, not invented.

BLOCKER_TASK = {
    "kind": "task",
    "task_id": "pred-1",
    "type": "blocks",
    "status": "open",
    "message": "Waiting on predecessor pred-1 to complete.",
}

BLOCKER_GATE = {
    "kind": "gate",
    "task_id": "gate-7",
    "type": "waits_on_gate",
    "status": "open",
    "message": "Waiting on pr gate gate-7.",
}

BLOCKER_GATE_TIMER = {
    "kind": "gate",
    "task_id": "gate-9",
    "type": "waits_on_gate",
    "status": "open",
    "message": "Waiting on timer gate gate-9 (ready_at=2026-05-01T00:00:00+00:00).",
}

BLOCKER_UNSATISFIABLE = {
    "kind": "blocker_unsatisfiable",
    "task_id": "old-spike",
    "type": "blocks",
    "status": "cancelled",
    "message": (
        "Blocking predecessor old-spike was cancelled; this task can never "
        "become ready without intervention (complete/re-open the predecessor, "
        "re-route, or cancel this subtree)."
    ),
}

BLOCKER_CYCLE = {
    "kind": "cycle",
    "task_id": "pred-2",
    "type": "blocks",
    "status": "open",
    "message": "dependency cycle: t-1 -> pred-2 -> pred-2",
}

REAL_BLOCKER_FIXTURES = [
    BLOCKER_TASK,
    BLOCKER_GATE,
    BLOCKER_GATE_TIMER,
    BLOCKER_UNSATISFIABLE,
    BLOCKER_CYCLE,
]


@pytest.mark.parametrize("raw", REAL_BLOCKER_FIXTURES, ids=lambda raw: str(raw["kind"]))
def test_normalize_blocker_round_trips_every_compute_blockers_branch(
    raw: dict[str, Any],
) -> None:
    blocker = normalize_blocker(raw)
    assert blocker == BlockerRecord(
        kind=raw["kind"],
        task_id=raw["task_id"],
        type=raw["type"],
        status=raw["status"],
        message=raw["message"],
    )


def test_normalize_blocker_preserves_type_and_message() -> None:
    blocker = normalize_blocker(BLOCKER_UNSATISFIABLE)
    assert blocker.type == "blocks"
    assert blocker.status == "cancelled"
    assert "can never become ready" in blocker.message


def test_normalize_blocker_unknown_kind_survives_round_trip() -> None:
    assert normalize_blocker({"kind": "surprise"}).kind == "surprise"
    assert normalize_blocker({}).kind == ""


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
        type=edge_type,
        direction="incoming",
        metadata={"note": "x"},
        created_by="planner",
        created_at="2026-04-26T10:00:00+00:00",
    )


def test_normalize_edge_unknown_type_survives_round_trip() -> None:
    edge = normalize_edge(
        {"from_task_id": "a", "to_task_id": "b", "type": "brand_new_edge"}
    )
    assert edge.type == "brand_new_edge"


def test_normalize_edge_missing_type_is_empty() -> None:
    assert normalize_edge({"from_task_id": "a", "to_task_id": "b"}).type == ""


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


def test_normalize_task_unknown_task_type_survives_round_trip() -> None:
    assert normalize_task({"id": "t-1", "task_type": "bogus"}).task_type == "bogus"


def test_normalize_blocked_task_pairs_task_with_its_blockers() -> None:
    record = normalize_blocked_task(
        {
            "id": "t-blocked",
            "title": "Blocked work",
            "task_type": "task",
            "blockers": [BLOCKER_TASK, BLOCKER_GATE, "not-a-dict"],
        }
    )
    assert record.task.id == "t-blocked"
    assert [blocker.kind for blocker in record.blockers] == ["task", "gate"]
    assert record.blockers[0].task_id == "pred-1"
    assert record.blockers[1].type == "waits_on_gate"


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


def test_list_tasks_always_sends_with_claims_so_default_false_is_pinned() -> None:
    """Upstream lithos_task_list currently defaults with_claims=False, but the
    flag is sent explicitly so an upstream default flip can't silently invert
    the Lens default (the bug shape task_ready had)."""
    client = _StubClient({"lithos_task_list": {"tasks": []}})
    _run(client, client.list_tasks())

    assert client.calls[0] == ("lithos_task_list", {"with_claims": False})


def test_list_tasks_sends_explicit_true_with_claims() -> None:
    client = _StubClient({"lithos_task_list": {"tasks": []}})
    _run(client, client.list_tasks(status="open", with_claims=True))

    assert client.calls[0] == (
        "lithos_task_list",
        {"status": "open", "with_claims": True},
    )


def test_list_tasks_sends_explicit_false_with_claims() -> None:
    client = _StubClient({"lithos_task_list": {"tasks": []}})
    _run(client, client.list_tasks(status="open", with_claims=False))

    assert client.calls[0] == (
        "lithos_task_list",
        {"status": "open", "with_claims": False},
    )


def test_task_ready_sends_limit_and_claims_and_normalizes() -> None:
    client = _StubClient(
        {"lithos_task_ready": {"tasks": [{"id": "r-1", "title": "Ready one"}]}}
    )
    tasks = _run(client, client.task_ready(limit=500, with_claims=True))

    assert [task.id for task in tasks] == ["r-1"]
    name, arguments = client.calls[0]
    assert name == "lithos_task_ready"
    assert arguments == {"limit": 500, "with_claims": True}


def test_task_ready_always_sends_with_claims_so_default_false_is_honored() -> None:
    """Upstream lithos_task_ready defaults with_claims=True; Lens must send
    its deliberate False default explicitly or it would be silently ignored."""
    client = _StubClient({"lithos_task_ready": {"tasks": []}})
    _run(client, client.task_ready())

    assert client.calls[0] == ("lithos_task_ready", {"with_claims": False})


def test_task_ready_sends_explicit_false_with_claims() -> None:
    client = _StubClient({"lithos_task_ready": {"tasks": []}})
    _run(client, client.task_ready(limit=10, with_claims=False))

    assert client.calls[0] == (
        "lithos_task_ready",
        {"limit": 10, "with_claims": False},
    )


def test_task_ready_propagates_project_and_tags() -> None:
    client = _StubClient({"lithos_task_ready": {"tasks": []}})
    _run(client, client.task_ready(project="influx", tags=["area:docs"]))

    assert client.calls[0] == (
        "lithos_task_ready",
        {"with_claims": False, "project": "influx", "tags": ["area:docs"]},
    )


def test_task_blocked_returns_blocked_records_with_structured_blockers() -> None:
    client = _StubClient(
        {
            "lithos_task_blocked": {
                "tasks": [
                    {
                        "id": "b-1",
                        "title": "Blocked one",
                        "blockers": [BLOCKER_TASK],
                    }
                ]
            }
        }
    )
    blocked = _run(client, client.task_blocked(limit=500))

    assert isinstance(blocked[0], BlockedTaskRecord)
    assert blocked[0].task.id == "b-1"
    assert blocked[0].blockers[0].kind == "task"
    assert blocked[0].blockers[0].message == BLOCKER_TASK["message"]
    assert client.calls[0] == ("lithos_task_blocked", {"limit": 500})


def test_task_blocked_propagates_project_and_tags() -> None:
    client = _StubClient({"lithos_task_blocked": {"tasks": []}})
    _run(client, client.task_blocked(project="influx", tags=["area:docs"]))

    assert client.calls[0] == (
        "lithos_task_blocked",
        {"project": "influx", "tags": ["area:docs"]},
    )


def test_task_get_returns_task_and_supports_task_envelope() -> None:
    client = _StubClient(
        {
            "lithos_task_get": {
                "task": {"id": "t-1", "title": "One", "task_type": "gate"}
            }
        }
    )
    task = _run(client, client.task_get("t-1"))

    assert task.id == "t-1"
    assert task.task_type == "gate"
    assert client.calls[0] == ("lithos_task_get", {"task_id": "t-1"})


def test_task_get_supports_legacy_tasks_list_envelope() -> None:
    client = _StubClient(
        {"lithos_task_get": {"tasks": [{"id": "t-2", "title": "Two"}]}}
    )
    task = _run(client, client.task_get("t-2"))

    assert task.id == "t-2"


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


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"task": None},
        {"task": "not-a-dict"},
        {"tasks": []},
        {"tasks": ["not-a-dict"]},
    ],
    ids=["empty", "task-none", "task-not-dict", "tasks-empty", "tasks-not-dict"],
)
def test_task_get_raises_invalid_response_instead_of_returning_none(
    payload: dict[str, Any],
) -> None:
    """A success payload with no decodable task is a broken response, not an
    absent task — task_get must raise a coded error, never return None."""
    client = _StubClient({"lithos_task_get": payload})
    with pytest.raises(LithosToolError) as excinfo:
        _run(client, client.task_get("t-1"))
    assert excinfo.value.code == "invalid_response"


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
