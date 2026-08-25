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
from types import SimpleNamespace
from typing import Any

import pytest

from lithos_lens import lithos_client
from lithos_lens.config import LithosConfig
from lithos_lens.lithos_client import LithosClient, LithosToolError
from lithos_lens.normalizers import normalize_task
from lithos_lens.task_graph import (
    BlockedTaskRecord,
    BlockerRecord,
    EdgeRecord,
    normalize_blocked_task,
    normalize_blocker,
    normalize_edge,
)
from tests.conftest import load_contract

# --- Normalizers: blocker kinds -------------------------------------------
#
# One fixture per branch of lithos ``_compute_blockers``, loaded from the
# vendored contract (tests/contracts/lithos_task_blocked.json — the
# authoritative payload shapes; see tests/contracts/README.md).

_BLOCKER_KINDS = load_contract("lithos_task_blocked")["responses"]["variants"][
    "blocker_kinds"
]
BLOCKER_TASK = _BLOCKER_KINDS["task"]
BLOCKER_GATE = _BLOCKER_KINDS["gate"]
BLOCKER_UNSATISFIABLE = _BLOCKER_KINDS["blocker_unsatisfiable"]

REAL_BLOCKER_FIXTURES = list(_BLOCKER_KINDS.values())


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


def test_normalize_task_defaults_graph_fields_when_the_payload_omits_them() -> None:
    """A malformed payload degrades; it is not a supported server shape.

    Both fields are part of the 0.4 contract, and 0.4 is a floor rather than a
    detected capability (REQUIREMENTS §5) — so this pins boundary robustness,
    not back-compat. See ``normalize_task`` for why the default is "task".
    """
    task = normalize_task({"id": "t-1", "title": "Malformed"})
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


def _run(client: LithosClient, coro: Any) -> Any:
    async def _driver() -> Any:
        try:
            return await coro
        finally:
            await client.close()

    return asyncio.run(_driver())


# --- MCP result decoding ---------------------------------------------------


class _CannedResultClient(LithosClient):
    """LithosClient that feeds a canned raw MCP result through the REAL
    decode path (via the no-startup one-shot branch of ``_call_tool``)."""

    def __init__(self, result: Any) -> None:
        super().__init__(LithosConfig())
        self._result = result

    async def _call_tool_oneshot(  # type: ignore[override]
        self, name: str, arguments: dict[str, Any]
    ) -> Any:
        return self._result


def _tool_result(text: str | None, *, is_error: bool = False) -> Any:
    content = [SimpleNamespace(text=text)] if text is not None else []
    return SimpleNamespace(content=content, isError=is_error)


def test_mcp_error_result_raises_coded_tool_error() -> None:
    """An MCP-level tool error (isError=true — e.g. the live server's FastMCP
    output-schema validation rejecting its own error envelope) carries plain
    text, not a Lithos envelope. It must surface as a coded LithosToolError,
    never a leaked JSONDecodeError."""
    client = _CannedResultClient(
        _tool_result(
            "Output validation error: 'error' is not of type 'array'",
            is_error=True,
        )
    )
    with pytest.raises(LithosToolError) as excinfo:
        _run(client, client.stats())
    assert excinfo.value.code == "tool_error"
    assert "Output validation error" in str(excinfo.value)


def test_non_json_success_result_raises_invalid_response() -> None:
    client = _CannedResultClient(_tool_result("<html>bad gateway</html>"))
    with pytest.raises(LithosToolError) as excinfo:
        _run(client, client.stats())
    assert excinfo.value.code == "invalid_response"


def test_non_dict_json_result_raises_invalid_response() -> None:
    client = _CannedResultClient(_tool_result("[1, 2]"))
    with pytest.raises(LithosToolError) as excinfo:
        _run(client, client.stats())
    assert excinfo.value.code == "invalid_response"


def test_empty_content_result_decodes_to_empty_payload() -> None:
    client = _CannedResultClient(_tool_result(None))
    assert _run(client, client.stats()) == {}


def test_json_dict_result_decodes_to_payload() -> None:
    client = _CannedResultClient(_tool_result('{"open_claims": 3}'))
    assert _run(client, client.stats()) == {"open_claims": 3}


class _HangingClient(LithosClient):
    """A client whose tool call never answers (the wedged-session case)."""

    def __init__(self) -> None:
        super().__init__(LithosConfig())
        self.started = 0

    async def _call_tool_oneshot(  # type: ignore[override]
        self, name: str, arguments: dict[str, Any]
    ) -> Any:
        self.started += 1
        await asyncio.Event().wait()


def test_a_tool_call_that_never_answers_times_out_instead_of_wedging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounding the CALL COUNT does not bound the render DURATION: nothing
    under ``_call_tool`` imposes a deadline, so one unanswered call would
    otherwise hold its request task — and, on a fan-out surface, one of the
    render's concurrency slots — forever. It must fail as an ordinary coded
    read error, which every caller already degrades gracefully."""
    monkeypatch.setattr(lithos_client, "CALL_TIMEOUT_S", 0.05)
    client = _HangingClient()

    with pytest.raises(LithosToolError) as excinfo:
        _run(client, client.stats())

    assert client.started == 1
    assert excinfo.value.code == "timeout"


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


def test_list_tasks_sends_resolved_since_for_the_terminal_window() -> None:
    """The Completed/Cancelled window is a resolved_since push (T1-S10):
    upstream filters resolved_at >= value and drops NULL-resolved rows, which
    is what makes "created months ago, finished yesterday" recent work."""
    client = _StubClient({"lithos_task_list": {"tasks": []}})
    _run(
        client,
        client.list_tasks(
            status="completed", resolved_since="2026-07-01T00:00:00+00:00"
        ),
    )

    assert client.calls[0] == (
        "lithos_task_list",
        {
            "with_claims": False,
            "status": "completed",
            "resolved_since": "2026-07-01T00:00:00+00:00",
        },
    )


def test_list_tasks_omits_resolved_since_when_not_windowed() -> None:
    """An unset window must not leak a null argument: the master open read is
    deliberately unwindowed."""
    client = _StubClient({"lithos_task_list": {"tasks": []}})
    _run(client, client.list_tasks(status="open", with_claims=True))

    assert client.calls[0] == (
        "lithos_task_list",
        {"status": "open", "with_claims": True},
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
        {"task": {}},
        {"task": {"id": ""}},
        {"task": {"id": 7}},
        {"tasks": []},
        {"tasks": ["not-a-dict"]},
        {"tasks": {"id": "t-1"}},
        {"tasks": 7},
        {"tasks": True},
    ],
    ids=[
        "empty",
        "task-none",
        "task-not-dict",
        "task-missing-id",
        "task-empty-id",
        "task-non-string-id",
        "tasks-empty",
        "tasks-entry-not-dict",
        "tasks-container-dict",
        "tasks-container-int",
        "tasks-container-bool",
    ],
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
