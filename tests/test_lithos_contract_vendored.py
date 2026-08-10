"""Vendored Lithos tool contracts (`lithos_lens.lithos_contract`).

These are the guards that turn the three invented-payload-shape escapes
(#23/#26/#30) into reds:

* the argument guard rejects a tool call carrying an argument the tool does
  not accept (and an unregistered tool name) — the #23/#26 class;
* the envelope helper reads a list payload through the *vendored* container
  key, so the invented ``notes``/``documents``/``results`` aliases (#30) can no
  longer be re-guessed at a call site.

The registry-shape tests pin the exact keys and argument sets, so changing a
vendored shape is a deliberate, reviewed edit here rather than an inline
invention scattered across ``lithos_client.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from lithos_lens.config import LithosConfig
from lithos_lens.lithos_client import LithosClient
from lithos_lens.lithos_contract import (
    CONTRACTS,
    LithosContractError,
    check_arguments,
    contract,
    envelope_rows,
)

# Every tool name LithosClient can emit; each must be vendored so the argument
# guard never rejects a legitimate call and the registry stays complete.
CLIENT_TOOLS = {
    "lithos_agent_register",
    "lithos_agent_list",
    "lithos_stats",
    "lithos_task_list",
    "lithos_task_ready",
    "lithos_task_blocked",
    "lithos_task_get",
    "lithos_task_children",
    "lithos_task_edge_list",
    "lithos_task_status",
    "lithos_finding_list",
    "lithos_read",
    "lithos_related",
    "lithos_list",
}


# ── registry shape ──────────────────────────────────────────────────────


def test_every_client_tool_is_vendored_with_provenance() -> None:
    for name in CLIENT_TOOLS:
        assert name in CONTRACTS, f"{name} is called by the client but not vendored"
        assert contract(name).source, f"{name} must cite an upstream source"


def test_contract_raises_for_unregistered_tool() -> None:
    with pytest.raises(LithosContractError):
        contract("lithos_made_up_tool")


@pytest.mark.parametrize(
    ("name", "rows_key"),
    [
        ("lithos_task_list", "tasks"),
        ("lithos_task_ready", "tasks"),
        ("lithos_task_blocked", "tasks"),
        ("lithos_task_children", "tasks"),
        ("lithos_task_status", "tasks"),
        ("lithos_task_edge_list", "edges"),
        ("lithos_finding_list", "findings"),
        ("lithos_agent_list", "agents"),
        ("lithos_list", "items"),
    ],
)
def test_list_tool_envelope_keys_are_pinned(name: str, rows_key: str) -> None:
    assert contract(name).rows_key == rows_key


def test_lithos_list_container_key_is_items_not_an_invented_alias() -> None:
    """The #30 escape: rows were read from invented notes/documents/results
    aliases. `items` is the one real key; no vendored tool uses the aliases."""
    assert contract("lithos_list").rows_key == "items"
    used_keys = {c.rows_key for c in CONTRACTS.values()}
    assert used_keys.isdisjoint({"notes", "documents", "results"})


def test_related_accepts_exactly_the_vendored_argument_set() -> None:
    """The #26 escape sent invented arguments; FastMCP rejects any it does not
    accept. The vendored set is exactly id/include/depth/namespace."""
    assert contract("lithos_related").arguments == frozenset(
        {"id", "include", "depth", "namespace"}
    )


# ── check_arguments (unit) ──────────────────────────────────────────────


def test_check_arguments_accepts_a_subset() -> None:
    check_arguments("lithos_related", {"id": "n-1", "depth": 1})


def test_check_arguments_rejects_an_unexpected_argument() -> None:
    with pytest.raises(LithosContractError) as excinfo:
        check_arguments("lithos_related", {"id": "n-1", "flat": True})
    assert "flat" in str(excinfo.value)


def test_check_arguments_rejects_an_unregistered_tool() -> None:
    with pytest.raises(LithosContractError):
        check_arguments("lithos_made_up_tool", {})


# ── envelope_rows (unit) ────────────────────────────────────────────────


def test_envelope_rows_reads_the_vendored_key() -> None:
    rows = envelope_rows("lithos_list", {"items": [{"id": "n-1"}], "total": 1})
    assert rows == [{"id": "n-1"}]


def test_envelope_rows_ignores_invented_alias_keys() -> None:
    # Same rows filed under keys lithos_list never emits: read nothing.
    payload = {"notes": [{"id": "n-1"}], "documents": [{"id": "n-2"}]}
    assert envelope_rows("lithos_list", payload) == []


def test_envelope_rows_empty_for_missing_or_non_list() -> None:
    assert envelope_rows("lithos_task_list", {}) == []
    assert envelope_rows("lithos_task_list", {"tasks": "oops"}) == []


def test_envelope_rows_rejects_a_non_list_tool() -> None:
    with pytest.raises(LithosContractError):
        envelope_rows("lithos_read", {"id": "n-1"})


# ── client wiring (the real _call_tool path) ────────────────────────────


class _RecordingClient(LithosClient):
    """Records each ``(tool, arguments)`` and returns a canned payload, so the
    public methods run end-to-end without a session. Overriding ``_call_tool``
    bypasses the argument guard — used only for envelope/wiring assertions."""

    def __init__(self, payloads: dict[str, dict[str, Any]]) -> None:
        super().__init__(LithosConfig())
        self._payloads = payloads
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _call_tool(  # type: ignore[override]
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return self._payloads.get(name, {})


class _OneshotClient(LithosClient):
    """Feeds a canned raw MCP result through the REAL ``_call_tool`` (the
    no-startup one-shot branch), so the argument guard actually runs."""

    def __init__(self, result: Any) -> None:
        super().__init__(LithosConfig())
        self._result = result

    async def _call_tool_oneshot(  # type: ignore[override]
        self, name: str, arguments: dict[str, Any]
    ) -> Any:
        return self._result


def _tool_result(text: str) -> Any:
    return SimpleNamespace(content=[SimpleNamespace(text=text)], isError=False)


def _run(client: LithosClient, coro: Any) -> Any:
    async def _driver() -> Any:
        try:
            return await coro
        finally:
            await client.close()

    return asyncio.run(_driver())


def test_list_notes_reads_the_items_envelope() -> None:
    client = _RecordingClient(
        {"lithos_list": {"items": [{"id": "n-1", "title": "Note"}], "total": 1}}
    )
    notes = _run(client, client.list_notes(title_contains="No"))
    assert [note.id for note in notes] == ["n-1"]


def test_list_notes_drops_invented_alias_envelopes() -> None:
    """Rows filed under keys lithos_list never emits yield nothing — the client
    binds to the vendored `items` key, not a guessed alias (#30)."""
    client = _RecordingClient(
        {"lithos_list": {"notes": [{"id": "n-1"}], "documents": [{"id": "n-2"}]}}
    )
    assert _run(client, client.list_notes()) == []


def test_related_sends_only_vendored_arguments() -> None:
    client = _RecordingClient({"lithos_related": {}})
    _run(client, client.related("n-1"))
    _name, arguments = client.calls[0]
    assert set(arguments) <= contract("lithos_related").arguments
    assert arguments == {"id": "n-1", "depth": 1}


def test_call_tool_rejects_an_out_of_contract_argument() -> None:
    client = _OneshotClient(_tool_result("{}"))
    with pytest.raises(LithosContractError):
        _run(client, client._call_tool("lithos_related", {"id": "n-1", "flat": True}))


def test_call_tool_rejects_an_unregistered_tool() -> None:
    client = _OneshotClient(_tool_result("{}"))
    with pytest.raises(LithosContractError):
        _run(client, client._call_tool("lithos_made_up_tool", {}))


def test_call_tool_accepts_in_contract_arguments() -> None:
    client = _OneshotClient(_tool_result('{"id": "n-1"}'))
    payload = _run(
        client, client._call_tool("lithos_related", {"id": "n-1", "depth": 1})
    )
    assert payload == {"id": "n-1"}
