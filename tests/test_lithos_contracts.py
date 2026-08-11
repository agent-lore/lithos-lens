"""Vendored Lithos contract guardrail (issue #31).

Three pipeline PRs in a row (#23/#26/#30) invented a Lithos payload shape and
passed green because the client, the fake, and the tests were authored against
each other. The vendored contracts under ``tests/contracts/`` break that
self-reference, and this module enforces them:

1. **Coverage** (static, mirrors ``test_config_env_prefix.py``): every tool
   name the client passes to ``_call_tool`` has a contract file, every
   contract file names a tool the client actually calls, and every
   ``_call_tool`` invocation uses a string-literal name (a dynamic name would
   be invisible to this guardrail, so it is rejected outright).
2. **Round-trip, both directions of the wire**: each contract's canonical
   request is bound to a real client call whose recorded outbound arguments
   must equal ``request.canonical`` exactly (an invented request argument —
   PR #26's phantom ``agent_id`` — fails here), and the canonical success
   payload fed back through that call must normalize into the full expected
   records, field for field.
3. **Coded errors**: every error envelope a contract documents surfaces as a
   ``LithosToolError`` carrying that envelope's code.

Scope boundary: the round-trip guarantee covers every field the Lens records
expose. Payload fields Lens deliberately does not model (e.g. ``lithos_read``'s
``links`` / ``truncated`` / ``retrieval_count``) are outside it — the contract
still documents them for authoring, but no Lens record carries them.

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
from lithos_lens.knowledge import RelatedNeighborhood, RelatedRef, SearchResult
from lithos_lens.lithos_client import LithosClient, LithosToolError
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord, EdgeRecord
from lithos_lens.tasks import (
    AgentRecord,
    ClaimRecord,
    FindingRecord,
    NoteRecord,
    NoteSummary,
    TaskRecord,
    TaskStatusRecord,
)
from tests.conftest import CONTRACTS_DIR, load_contract

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENT_PY = REPO_ROOT / "src" / "lithos_lens" / "lithos_client.py"


def _call_tool_name_nodes() -> list[ast.expr | None]:
    """The name argument node of every ``self._call_tool(...)`` invocation."""
    tree = ast.parse(CLIENT_PY.read_text(encoding="utf-8"))
    nodes: list[ast.expr | None] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_call_tool"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        ):
            if node.args:
                nodes.append(node.args[0])
            else:
                keyword = next((k for k in node.keywords if k.arg == "name"), None)
                nodes.append(keyword.value if keyword else None)
    return nodes


def _client_tool_names() -> set[str]:
    """Tool-name string literals passed to ``self._call_tool(...)``."""
    return {
        node.value
        for node in _call_tool_name_nodes()
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _contract_tools() -> set[str]:
    return {
        path.stem
        for path in CONTRACTS_DIR.glob("*.json")
        if not path.name.startswith("_")
    }


# ── coverage: client ↔ contracts, both directions ───────────────────────


def test_every_call_tool_name_is_a_string_literal() -> None:
    """A dynamic tool name would be invisible to this guardrail — forbid it."""
    dynamic = [
        ast.dump(node) if node is not None else "<missing name argument>"
        for node in _call_tool_name_nodes()
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str))
    ]
    assert not dynamic, (
        f"_call_tool must be invoked with a string-literal tool name so the "
        f"contract coverage check can see it; found: {dynamic}"
    )


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
    """Every field documented in tests/contracts/README.md, with its type."""
    contract = load_contract(tool)
    assert contract["tool"] == tool, "tool field must equal the filename stem"

    version = contract["lithos_version"]
    assert isinstance(version, str) and version, "lithos_version pin is required"

    source = contract["source"]
    assert isinstance(source, list) and source, "source citation list is required"
    assert all(isinstance(entry, str) and entry for entry in source)

    request = contract["request"]
    assert isinstance(request["canonical"], dict)
    assert isinstance(request.get("notes", ""), str)
    request_variants = request.get("variants", {})
    assert isinstance(request_variants, dict)
    assert all(isinstance(args, dict) for args in request_variants.values())

    responses = contract["responses"]
    assert isinstance(responses["success"], dict)
    response_variants = responses.get("variants", {})
    assert isinstance(response_variants, dict)
    assert all(isinstance(payload, dict) for payload in response_variants.values())
    errors = responses.get("errors", [])
    assert isinstance(errors, list)
    for envelope in errors:
        assert envelope.get("status") == "error"
        assert isinstance(envelope.get("code"), str) and envelope["code"]
        assert isinstance(envelope.get("message"), str) and envelope["message"]

    assert isinstance(contract["observed_divergences"], str)


# ── round-trip: canonical requests out, canonical payloads back ─────────


class _RecordingStubClient(LithosClient):
    """Real client with the transport stubbed: records every outbound
    ``(tool, arguments)`` pair and serves one canned payload back."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(LithosConfig(agent_id="lithos-lens"))
        self._payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _call_tool(  # type: ignore[override]
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return self._payload


def _run(
    payload: dict[str, Any], call: Callable[[LithosClient], Awaitable[Any]]
) -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    client = _RecordingStubClient(payload)

    async def _driver() -> Any:
        try:
            return await call(client)
        finally:
            await client.close()

    return asyncio.run(_driver()), client.calls


# Expected records built field-by-field from the contract JSON. The explicit
# None→"" conversions document the client's normalization policy; a normalizer
# dropping a field (or reading an invented alias) breaks full equality.


def _expected_task(raw: dict[str, Any]) -> TaskRecord:
    claims = None
    if "claims" in raw:
        claims = tuple(
            ClaimRecord(
                agent=claim["agent"],
                aspect=claim["aspect"],
                expires_at=claim["expires_at"],
            )
            for claim in raw["claims"]
        )
    return TaskRecord(
        id=raw["id"],
        title=raw["title"],
        description=raw["description"],
        status=raw["status"],
        created_by=raw["created_by"],
        created_at=raw["created_at"],
        tags=tuple(raw["tags"]),
        metadata=raw["metadata"],
        outcome=raw["outcome"] or "",
        task_type=raw["task_type"],
        resolved_at=raw["resolved_at"] or "",
        claims=claims,
    )


def _check_task_list(result: Any, success: dict[str, Any]) -> None:
    assert result == [_expected_task(raw) for raw in success["tasks"]]


def _check_blocked(result: Any, success: dict[str, Any]) -> None:
    assert result == [
        BlockedTaskRecord(
            task=_expected_task(raw),
            blockers=tuple(
                BlockerRecord(
                    kind=blocker["kind"],
                    task_id=blocker["task_id"],
                    type=blocker["type"],
                    status=blocker["status"],
                    message=blocker["message"],
                )
                for blocker in raw["blockers"]
            ),
        )
        for raw in success["tasks"]
    ]


def _check_task_get(result: Any, success: dict[str, Any]) -> None:
    assert result == _expected_task(success["task"])


def _check_edges(result: Any, success: dict[str, Any]) -> None:
    assert result == [
        EdgeRecord(
            from_task_id=raw["from_task_id"],
            to_task_id=raw["to_task_id"],
            type=raw["type"],
            direction=raw["direction"],
            metadata=raw["metadata"],
            created_by=raw["created_by"],
            created_at=raw["created_at"],
        )
        for raw in success["edges"]
    ]


def _check_task_status(result: Any, success: dict[str, Any]) -> None:
    raw = success["tasks"][0]
    assert result == TaskStatusRecord(
        id=raw["id"],
        title=raw["title"],
        status=raw["status"],
        claims=tuple(
            ClaimRecord(
                agent=claim["agent"],
                aspect=claim["aspect"],
                expires_at=claim["expires_at"],
            )
            for claim in raw["claims"]
        ),
        metadata=raw["metadata"],
    )


def _check_findings(result: Any, success: dict[str, Any]) -> None:
    # task_id is not in the wire payload; the client stamps the requested id.
    assert result == [
        FindingRecord(
            id=raw["id"],
            task_id="influx-ingest-cutover",
            agent=raw["agent"],
            summary=raw["summary"],
            knowledge_id=raw["knowledge_id"],
            created_at=raw["created_at"],
        )
        for raw in success["findings"]
    ]


def _check_agents(result: Any, success: dict[str, Any]) -> None:
    assert result == [
        AgentRecord(
            id=raw["id"],
            name=raw["name"],
            type=raw["type"],
            last_seen_at=raw["last_seen_at"],
        )
        for raw in success["agents"]
    ]


def _expected_note(success: dict[str, Any]) -> NoteRecord:
    # NoteRecord's tags come from metadata.tags (no top-level tags on reads);
    # links/truncated/retrieval_count are deliberately not modeled by Lens.
    return NoteRecord(
        id=success["id"],
        title=success["title"],
        content=success["content"],
        tags=tuple(success["metadata"]["tags"]),
        metadata=success["metadata"],
    )


def _check_note(result: Any, success: dict[str, Any]) -> None:
    assert result == _expected_note(success)


def _check_related(result: Any, success: dict[str, Any]) -> None:
    def _refs(entries: list[dict[str, Any]]) -> tuple[RelatedRef, ...]:
        return tuple(RelatedRef(id=raw["id"], title=raw["title"]) for raw in entries)

    def _edge_ref(raw: dict[str, Any], direction: str) -> RelatedRef:
        # The ref keeps the OPPOSITE endpoint by direction (§6.5).
        return RelatedRef(
            id=raw["to_id"] if direction == "outgoing" else raw["from_id"],
            edge_type=raw["type"],
            weight=raw["weight"],
            direction=direction,
            conflict_state=raw["conflict_state"],
        )

    assert result == RelatedNeighborhood(
        links=_refs(success["links"]["outgoing"]),
        backlinks=_refs(success["links"]["incoming"]),
        sources=_refs(success["provenance"]["sources"]),
        derived=_refs(success["provenance"]["derived"]),
        unresolved=tuple(success["provenance"]["unresolved_sources"]),
        edges=tuple(_edge_ref(raw, "outgoing") for raw in success["edges"]["outgoing"])
        + tuple(_edge_ref(raw, "incoming") for raw in success["edges"]["incoming"]),
    )


def _check_note_summaries(result: Any, success: dict[str, Any]) -> None:
    assert result == [
        NoteSummary(
            id=raw["id"],
            title=raw["title"],
            path=raw["path"],
            updated=raw["updated"],
            tags=tuple(raw["tags"]),
        )
        for raw in success["items"]
    ]


def _check_search_results(result: Any, success: dict[str, Any]) -> None:
    assert result == [
        SearchResult(
            id=raw["id"],
            title=raw["title"],
            path=raw["path"],
            snippet=raw["snippet"],
            updated=raw["updated_at"],
            score=raw["score"],
        )
        for raw in success["results"]
    ]


def _check_register(result: Any, success: dict[str, Any]) -> None:
    assert result is True


def _check_stats(result: Any, success: dict[str, Any]) -> None:
    assert result == success


# tool → (client call, full-field normalization check). Each call is written
# so its OUTBOUND arguments equal the contract's request.canonical exactly —
# test_canonical_request_and_payload_round_trip asserts both directions.
# test_round_trip_table_covers_every_contract forces a new entry here
# whenever a contract (and hence a client tool) is added.
TOOL_SPECS: dict[
    str,
    tuple[
        Callable[[LithosClient], Awaitable[Any]],
        Callable[[Any, dict[str, Any]], None],
    ],
] = {
    "lithos_agent_register": (lambda c: c.register_agent(), _check_register),
    "lithos_task_list": (
        lambda c: c.list_tasks(
            agent="planner",
            status="open",
            tags=["project:influx"],
            since="2026-08-01T00:00:00+00:00",
        ),
        _check_task_list,
    ),
    "lithos_task_ready": (
        lambda c: c.task_ready(limit=500, project="influx", tags=["area:data"]),
        _check_task_list,
    ),
    "lithos_task_blocked": (
        lambda c: c.task_blocked(limit=500, project="influx", tags=["area:data"]),
        _check_blocked,
    ),
    "lithos_task_get": (
        lambda c: c.task_get("influx-ingest-cutover"),
        _check_task_get,
    ),
    "lithos_task_children": (
        lambda c: c.task_children("influx-epic", recursive=True, include_closed=True),
        _check_task_list,
    ),
    "lithos_task_edge_list": (
        lambda c: c.task_edge_list("influx-ingest-cutover", types=["blocks"]),
        _check_edges,
    ),
    "lithos_task_status": (
        lambda c: c.task_status("influx-ingest-cutover"),
        _check_task_status,
    ),
    "lithos_finding_list": (
        lambda c: c.list_findings(
            "influx-ingest-cutover", since="2026-08-01T00:00:00+00:00"
        ),
        _check_findings,
    ),
    "lithos_stats": (lambda c: c.stats(), _check_stats),
    "lithos_agent_list": (lambda c: c.list_agents(), _check_agents),
    "lithos_read": (
        lambda c: c.read_note("11111111-1111-4111-8111-111111111111"),
        _check_note,
    ),
    "lithos_related": (lambda c: c.related("root"), _check_related),
    "lithos_list": (
        lambda c: c.list_notes(
            title_contains="influx", tags=["project:influx"], limit=20
        ),
        _check_note_summaries,
    ),
    "lithos_search": (
        lambda c: c.search_notes("influx", tags=["project:influx"], limit=20),
        _check_search_results,
    ),
}


def test_round_trip_table_covers_every_contract() -> None:
    assert set(TOOL_SPECS) == _contract_tools(), (
        "TOOL_SPECS must have exactly one round-trip entry per contract file — "
        "a new client tool needs its contract AND its round-trip spec."
    )


@pytest.mark.parametrize("tool", sorted(TOOL_SPECS))
def test_canonical_request_and_payload_round_trip(tool: str) -> None:
    call, check = TOOL_SPECS[tool]
    contract = load_contract(tool)
    result, calls = _run(contract["responses"]["success"], lambda c: call(c))
    # Outbound: exactly one call, arguments exactly the vendored canonical
    # request — a phantom or missing argument fails here.
    assert calls == [(tool, contract["request"]["canonical"])]
    # Inbound: the canonical payload normalized into the full expected records.
    check(result, contract["responses"]["success"])


def test_read_note_by_path_sends_the_vendored_variant_request() -> None:
    """The by-path probe (wiki-link resolver) is a distinct request shape,
    vendored as lithos_read's ``by_path`` request variant."""
    contract = load_contract("lithos_read")
    result, calls = _run(
        contract["responses"]["success"],
        lambda c: c.read_note_by_path("plans/influx-migration.md"),
    )
    assert calls == [("lithos_read", contract["request"]["variants"]["by_path"])]
    assert result == _expected_note(contract["responses"]["success"])


def test_recent_notes_sends_the_recent_variant_and_sorts_newest_first() -> None:
    """``recent_notes`` is ``lithos_list``'s second request shape (the
    ``recent``/``recent_tagged`` variants): pages walked via ``offset``,
    because the tool has no ordering parameter (upstream task e0e31654). The
    fixture's ``total`` fits one page, so exactly one request goes out. The
    ``insertion_ordered_recent`` response variant's rows are deliberately out
    of updated-order — the client must sort newest-first and truncate."""
    contract = load_contract("lithos_list")
    payload = contract["responses"]["variants"]["insertion_ordered_recent"]

    result, calls = _run(payload, lambda c: c.recent_notes())
    assert calls == [("lithos_list", contract["request"]["variants"]["recent"])]
    assert [row.id for row in result] == [
        "44444444-4444-4444-8444-444444444444",  # 2026-08-09
        "55555555-5555-4555-8555-555555555555",  # 2026-08-02
        "33333333-3333-4333-8333-333333333333",  # 2026-07-01
    ]

    limited, tagged_calls = _run(
        payload,
        lambda c: c.recent_notes(tags=["project:influx"], limit=1),
    )
    assert tagged_calls == [
        ("lithos_list", contract["request"]["variants"]["recent_tagged"])
    ]
    # The limit truncates AFTER the newest-first sort — never before.
    assert [row.id for row in limited] == ["44444444-4444-4444-8444-444444444444"]


class _PagedStubClient(LithosClient):
    """Recording stub serving a distinct canned payload per request offset."""

    def __init__(self, pages: dict[int, dict[str, Any]]) -> None:
        super().__init__(LithosConfig(agent_id="lithos-lens"))
        self._pages = pages
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _call_tool(  # type: ignore[override]
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return self._pages[arguments["offset"]]


def test_recent_notes_paginates_until_total_so_late_rows_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PR #39 review scenario: the corpus exceeds one fetch page and the
    GLOBALLY newest note sits beyond it. recent_notes must keep walking
    ``offset`` until the response's ``total`` is exhausted, so that note wins
    even with ``limit=1`` — "newest of the first page" is exactly the bug this
    pins out. Rows are the vendored ``insertion_ordered_recent`` fixtures,
    re-paged with a shrunken page size (the wire shape per page is identical;
    only the walk is under test)."""
    import lithos_lens.lithos_client as lithos_client_module

    monkeypatch.setattr(lithos_client_module, "RECENT_NOTES_FETCH_PAGE", 2)
    contract = load_contract("lithos_list")
    fixture_rows = contract["responses"]["variants"]["insertion_ordered_recent"][
        "items"
    ]
    oldest, newest, middle = fixture_rows
    pages = {
        # Page one: older rows only; total says more exist.
        0: {"items": [oldest, middle], "total": 3},
        # Page two: the globally newest row.
        2: {"items": [newest], "total": 3},
    }
    client = _PagedStubClient(pages)

    async def _driver() -> list[Any]:
        try:
            return await client.recent_notes(limit=1)
        finally:
            await client.close()

    result = asyncio.run(_driver())

    assert [(name, args["offset"]) for name, args in client.calls] == [
        ("lithos_list", 0),
        ("lithos_list", 2),
    ]
    assert [row.id for row in result] == [newest["id"]]


def test_recent_notes_advances_past_short_pages_by_page_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstream slices matching ids ``offset:offset+limit`` BEFORE reading
    documents, then skips unreadable ones — so a page can come back short while
    having consumed a full window. The cursor must advance by the requested
    span (offset 0 → 2 with page size 2, never 1), or the next page re-reads
    the overlap. Page two also re-serves a row page one already delivered (a
    concurrent-mutation shift): it must appear exactly once."""
    import lithos_lens.lithos_client as lithos_client_module

    monkeypatch.setattr(lithos_client_module, "RECENT_NOTES_FETCH_PAGE", 2)
    contract = load_contract("lithos_list")
    fixture_rows = contract["responses"]["variants"]["insertion_ordered_recent"][
        "items"
    ]
    oldest, newest, middle = fixture_rows
    pages = {
        # A two-slot window where one document was unreadable: ONE row back,
        # total still counts the full match set.
        0: {"items": [middle], "total": 3},
        # The wrongly-advanced offset 1 is not served: hitting it fails loudly.
        2: {"items": [newest, middle], "total": 3},
    }
    client = _PagedStubClient(pages)

    async def _driver() -> list[Any]:
        try:
            return await client.recent_notes()
        finally:
            await client.close()

    result = asyncio.run(_driver())

    assert [(name, args["offset"]) for name, args in client.calls] == [
        ("lithos_list", 0),
        ("lithos_list", 2),
    ]
    assert [row.id for row in result] == [newest["id"], middle["id"]]


def test_recent_notes_continues_past_an_all_unreadable_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entirely unreadable page returns zero items while ``total`` says more
    matches exist — the walk must continue to the next window, not stop early
    with later pages unread."""
    import lithos_lens.lithos_client as lithos_client_module

    monkeypatch.setattr(lithos_client_module, "RECENT_NOTES_FETCH_PAGE", 2)
    contract = load_contract("lithos_list")
    fixture_rows = contract["responses"]["variants"]["insertion_ordered_recent"][
        "items"
    ]
    _oldest, newest, _middle = fixture_rows
    pages = {
        0: {"items": [], "total": 3},
        2: {"items": [newest], "total": 3},
    }
    client = _PagedStubClient(pages)

    async def _driver() -> list[Any]:
        try:
            return await client.recent_notes(limit=1)
        finally:
            await client.close()

    result = asyncio.run(_driver())

    assert [args["offset"] for _, args in client.calls] == [0, 2]
    assert [row.id for row in result] == [newest["id"]]


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
