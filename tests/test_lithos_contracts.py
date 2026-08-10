"""Vendored Lithos contract guardrail (issue #31).

Three pipeline PRs in a row (#23/#26/#30) invented a Lithos payload shape and
passed green because the client, the fake, and the tests were authored against
each other. The vendored contracts under ``tests/contracts/`` break that
self-reference, and this module enforces them:

1. **Coverage** (static, mirrors ``test_config_env_prefix.py``): every tool
   name the client passes to ``_call_tool`` has a contract file, and every
   contract file names a tool the client actually calls — checked by AST so
   the check itself cannot drift with import-time behavior.
2. **Round-trip**: every contract's canonical success payload feeds through
   the real client method and must normalize into the expected records —
   an invented shape in either the contract or a normalizer fails here.
3. **Coded errors**: every error envelope a contract documents surfaces as a
   ``LithosToolError`` carrying that envelope's code.

``health()`` is out of scope: it probes the plain HTTP ``/health`` endpoint,
not an MCP tool. Underscore-prefixed files in the contracts dir (e.g. the
advisory ``_tools_snapshot.json``) are not contracts.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from lithos_lens.config import LithosConfig
from lithos_lens.lithos_client import LithosClient, LithosToolError
from tests.conftest import CONTRACTS_DIR, load_contract

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENT_PY = REPO_ROOT / "src" / "lithos_lens" / "lithos_client.py"

REQUIRED_KEYS = ("tool", "lithos_version", "source", "request", "responses")


def _client_tool_names() -> set[str]:
    """Tool-name literals passed to ``self._call_tool(...)`` (by AST)."""
    tree = ast.parse(CLIENT_PY.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_call_tool"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
    return names


def _contract_tools() -> set[str]:
    return {
        path.stem
        for path in CONTRACTS_DIR.glob("*.json")
        if not path.name.startswith("_")
    }


# ── coverage: client ↔ contracts, both directions ───────────────────────


def test_every_client_tool_has_a_contract() -> None:
    missing = _client_tool_names() - _contract_tools()
    assert not missing, (
        f"Lithos tools called by lithos_client.py without a vendored contract: "
        f"{sorted(missing)}. Add tests/contracts/<tool>.json (transcribed from "
        f"the Lithos source — see tests/contracts/README.md) in the same PR."
    )


def test_every_contract_names_a_called_tool() -> None:
    orphans = _contract_tools() - _client_tool_names()
    assert not orphans, (
        f"Contract files without a matching _call_tool user: {sorted(orphans)}. "
        f"Remove them or wire up the client method."
    )


@pytest.mark.parametrize("tool", sorted(_contract_tools()))
def test_contract_file_is_well_formed(tool: str) -> None:
    contract = load_contract(tool)
    for key in REQUIRED_KEYS:
        assert key in contract, f"{tool}.json is missing required key {key!r}"
    assert contract["tool"] == tool, "tool field must equal the filename stem"
    assert contract["source"], "source citation is required"
    assert isinstance(contract["request"].get("canonical"), dict)
    assert isinstance(contract["responses"].get("success"), dict)
    for envelope in contract["responses"].get("errors", []):
        assert envelope.get("status") == "error"
        assert envelope.get("code"), "error envelopes must carry a code"


# ── round-trip: canonical payloads through the real client ──────────────


class _ContractStubClient(LithosClient):
    """Real client with the transport stubbed to serve one canned payload."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(LithosConfig(agent_id="lithos-lens"))
        self._payload = payload

    async def _call_tool(  # type: ignore[override]
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return self._payload


def _run(
    payload: dict[str, Any], call: Callable[[LithosClient], Awaitable[Any]]
) -> Any:
    client = _ContractStubClient(payload)

    async def _driver() -> Any:
        try:
            return await call(client)
        finally:
            await client.close()

    return asyncio.run(_driver())


def _check_task_list(result: Any, success: dict[str, Any]) -> None:
    assert [t.id for t in result] == [t["id"] for t in success["tasks"]]


def _check_blocked(result: Any, success: dict[str, Any]) -> None:
    assert [row.task.id for row in result] == [t["id"] for t in success["tasks"]]
    assert [b.kind for b in result[0].blockers] == [
        b["kind"] for b in success["tasks"][0]["blockers"]
    ]


# tool → (client call, assertion that the canonical payload survived
# normalization). test_round_trip_table_covers_every_contract forces a new
# entry here whenever a contract (and hence a client tool) is added.
TOOL_SPECS: dict[
    str,
    tuple[
        Callable[[LithosClient], Awaitable[Any]], Callable[[Any, dict[str, Any]], None]
    ],
] = {
    "lithos_agent_register": (
        lambda c: c.register_agent(),
        lambda result, success: None if result is True else pytest.fail(str(result)),
    ),
    "lithos_task_list": (lambda c: c.list_tasks(), _check_task_list),
    "lithos_task_ready": (lambda c: c.task_ready(), _check_task_list),
    "lithos_task_blocked": (lambda c: c.task_blocked(), _check_blocked),
    "lithos_task_get": (
        lambda c: c.task_get("influx-ingest-cutover"),
        lambda result, success: _assert_eq(
            (result.id, result.title, result.task_type),
            (
                success["task"]["id"],
                success["task"]["title"],
                success["task"]["task_type"],
            ),
        ),
    ),
    "lithos_task_children": (
        lambda c: c.task_children("influx-epic"),
        _check_task_list,
    ),
    "lithos_task_edge_list": (
        lambda c: c.task_edge_list("influx-ingest-cutover"),
        lambda result, success: _assert_eq(
            [(e.from_task_id, e.to_task_id, e.type, e.direction) for e in result],
            [
                (e["from_task_id"], e["to_task_id"], e["type"], e["direction"])
                for e in success["edges"]
            ],
        ),
    ),
    "lithos_task_status": (
        lambda c: c.task_status("influx-ingest-cutover"),
        lambda result, success: _assert_eq(
            (result.id, [claim.agent for claim in result.claims]),
            (
                success["tasks"][0]["id"],
                [claim["agent"] for claim in success["tasks"][0]["claims"]],
            ),
        ),
    ),
    "lithos_finding_list": (
        lambda c: c.list_findings("influx-ingest-cutover"),
        lambda result, success: _assert_eq(
            [(f.id, f.knowledge_id) for f in result],
            [(f["id"], f["knowledge_id"]) for f in success["findings"]],
        ),
    ),
    "lithos_stats": (
        lambda c: c.stats(),
        lambda result, success: _assert_eq(result, success),
    ),
    "lithos_agent_list": (
        lambda c: c.list_agents(),
        lambda result, success: _assert_eq(
            [a.id for a in result], [a["id"] for a in success["agents"]]
        ),
    ),
    "lithos_read": (
        lambda c: c.read_note("11111111-1111-4111-8111-111111111111"),
        lambda result, success: _assert_eq(
            (result.id, result.title), (success["id"], success["title"])
        ),
    ),
    "lithos_related": (
        lambda c: c.related("root"),
        lambda result, success: _assert_eq(
            [ref.id for ref in result.links],
            [ref["id"] for ref in success["links"]["outgoing"]],
        ),
    ),
    "lithos_list": (
        lambda c: c.list_notes(),
        lambda result, success: _assert_eq(
            [note.id for note in result], [item["id"] for item in success["items"]]
        ),
    ),
}


def _assert_eq(actual: Any, expected: Any) -> None:
    assert actual == expected


def test_round_trip_table_covers_every_contract() -> None:
    assert set(TOOL_SPECS) == _contract_tools(), (
        "TOOL_SPECS must have exactly one round-trip entry per contract file — "
        "a new client tool needs its contract AND its round-trip spec."
    )


@pytest.mark.parametrize("tool", sorted(TOOL_SPECS))
def test_canonical_success_payload_round_trips(tool: str) -> None:
    call, check = TOOL_SPECS[tool]
    success = load_contract(tool)["responses"]["success"]
    result = _run(success, lambda c: call(c))
    check(result, success)


def _error_cases() -> list[tuple[str, dict[str, Any]]]:
    cases = []
    for tool in sorted(_contract_tools()):
        # register_agent deliberately swallows envelopes (returns False).
        if tool == "lithos_agent_register":
            continue
        for envelope in load_contract(tool)["responses"].get("errors", []):
            cases.append((tool, envelope))
    return cases


@pytest.mark.parametrize(
    ("tool", "envelope"),
    _error_cases(),
    ids=[f"{tool}-{envelope['code']}" for tool, envelope in _error_cases()],
)
def test_error_envelopes_surface_as_coded_errors(
    tool: str, envelope: dict[str, Any]
) -> None:
    call, _ = TOOL_SPECS[tool]
    with pytest.raises(LithosToolError) as excinfo:
        _run(envelope, lambda c: call(c))
    assert excinfo.value.code == envelope["code"]
