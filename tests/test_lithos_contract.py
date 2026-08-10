"""Fake↔real Lithos client contract matrix (issue #28).

The fake's contract fidelity used to be maintained by hand, finding by
finding (``task_not_found``, ``doc_not_found`` for reads AND related,
``with_claims`` semantics, edge scoping…). This suite runs the same
assertions against both :class:`FakeLithosClient` and the real
:class:`LithosClient`, so a whole class of drift fails here as a matrix red
instead of being re-fixed instance by instance.

The real leg is guarded on ``LITHOS_URL`` (host/CI only, per the
hermetic-gate rule): without it the leg skips and the unit run stays fully
hermetic. With it, the suite dials that server, so only contracts that hold
regardless of server data are asserted here — coded not-found envelopes for
ids nothing should ever create, claims-presence semantics, limit bounds, and
shape invariants over whatever rows come back. Anything pinned to specific
demo fixture rows lives in ``test_fake_lithos.py``.

Behavior sources for the encoded contracts: lithos ``tools/tasks.py``
(task_get task_not_found; task_status empty-tasks-on-missing; task_children
empty-on-missing; task_edge_list invalid_input on a bad direction) and
``tools/read_search.py`` (lithos_read / lithos_related doc_not_found).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from lithos_lens.config import LithosConfig
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.lithos_client import (
    LithosClient,
    LithosClientProtocol,
    LithosToolError,
)
from lithos_lens.task_graph import BlockedTaskRecord
from tests.conftest import CONTRACTS_DIR, load_contract

pytestmark = pytest.mark.anyio

# An id no real deployment should ever hold — the not-found legs of the
# matrix depend on it being absent on the LITHOS_URL server too.
MISSING_ID = "lithos-lens-contract-suite-no-such-id"

REAL_URL_ENV = "LITHOS_URL"


@pytest.fixture(params=["fake", "real"])
async def client(request: pytest.FixtureRequest) -> AsyncIterator[LithosClientProtocol]:
    """One client per matrix leg; the real leg skips unless LITHOS_URL is set."""
    if request.param == "real":
        url = os.environ.get(REAL_URL_ENV, "").strip()
        if not url:
            pytest.skip(
                f"real-Lithos contract leg needs {REAL_URL_ENV} "
                "(host/CI only; unit runs stay hermetic)"
            )
        real = LithosClient(LithosConfig(url=url))
        await real.startup()
        try:
            yield real
        finally:
            await real.close()
    else:
        fake = FakeLithosClient()
        try:
            yield fake
        finally:
            await fake.close()


# ── coded error envelopes ───────────────────────────────────────────────


async def test_task_get_missing_raises_coded_task_not_found(
    client: LithosClientProtocol,
) -> None:
    with pytest.raises(LithosToolError) as excinfo:
        await client.task_get(MISSING_ID)
    assert excinfo.value.code == "task_not_found"


async def test_read_note_missing_raises_coded_doc_not_found(
    client: LithosClientProtocol,
) -> None:
    with pytest.raises(LithosToolError) as excinfo:
        await client.read_note(MISSING_ID)
    assert excinfo.value.code == "doc_not_found"


async def test_related_missing_raises_coded_doc_not_found(
    client: LithosClientProtocol,
) -> None:
    with pytest.raises(LithosToolError) as excinfo:
        await client.related(MISSING_ID)
    assert excinfo.value.code == "doc_not_found"


async def test_task_edge_list_invalid_direction_raises_coded_invalid_input(
    client: LithosClientProtocol,
) -> None:
    with pytest.raises(LithosToolError) as excinfo:
        await client.task_edge_list(MISSING_ID, direction="sideways")
    assert excinfo.value.code == "invalid_input"


async def test_list_findings_malformed_since_raises_a_coded_error(
    client: LithosClientProtocol,
) -> None:
    """A malformed `since` answers a coded error, never data.

    Upstream intends invalid_input (the datetime.fromisoformat guard in
    lithos_finding_list, checked before the task id) and the fake speaks it.
    The running server, however, trips FastMCP's output-schema validation
    before that envelope can leave — lithos_finding_list's return annotation
    (dict[str, list[...]]) forbids the envelope's string fields — which the
    client surfaces as code="tool_error". Until that upstream bug is fixed,
    the strongest matrix-wide contract is: one of the two codes, and never a
    silent full/empty findings list (or a leaked JSONDecodeError)."""
    with pytest.raises(LithosToolError) as excinfo:
        await client.list_findings(MISSING_ID, since="not-a-timestamp")
    assert excinfo.value.code in {"invalid_input", "tool_error"}


# ── soft-missing reads (no envelope upstream) ───────────────────────────


async def test_task_status_missing_returns_none(
    client: LithosClientProtocol,
) -> None:
    """Upstream lithos_task_status answers a missing task with an empty tasks
    list (historical behaviour), which both clients surface as None."""
    assert await client.task_status(MISSING_ID) is None


async def test_task_children_missing_returns_empty(
    client: LithosClientProtocol,
) -> None:
    assert await client.task_children(MISSING_ID) == []


async def test_task_edge_list_missing_returns_empty(
    client: LithosClientProtocol,
) -> None:
    assert await client.task_edge_list(MISSING_ID) == []


async def test_list_findings_missing_returns_empty(
    client: LithosClientProtocol,
) -> None:
    assert await client.list_findings(MISSING_ID) == []


# ── with_claims semantics ───────────────────────────────────────────────


async def test_list_tasks_claims_presence_follows_with_claims(
    client: LithosClientProtocol,
) -> None:
    """claims is None iff claims were not requested; a tuple (possibly empty)
    when with_claims=True. The silent-default-inversion class of bug (PR #23,
    issue #24) turns exactly this contract red."""
    with_claims = await client.list_tasks(with_claims=True)
    _require_rows_on_fake_leg(client, with_claims)
    assert all(task.claims is not None for task in with_claims)
    without = await client.list_tasks()
    assert all(task.claims is None for task in without)


async def test_task_ready_claims_presence_follows_with_claims(
    client: LithosClientProtocol,
) -> None:
    with_claims = await client.task_ready(with_claims=True)
    _require_rows_on_fake_leg(client, with_claims)
    assert all(task.claims is not None for task in with_claims)
    # Lens's deliberate False default (divergent from upstream's True) must
    # hold on both legs.
    without = await client.task_ready()
    assert all(task.claims is None for task in without)


# ── shape invariants over whatever rows exist ───────────────────────────


async def test_task_ready_limit_returns_the_unlimited_prefix(
    client: LithosClientProtocol,
) -> None:
    unlimited = await client.task_ready()
    limited = await client.task_ready(limit=1)
    assert [task.id for task in limited] == [task.id for task in unlimited][:1]


async def test_task_blocked_limit_and_rows_carry_blockers(
    client: LithosClientProtocol,
) -> None:
    """Blocked means "open but not ready for a stated reason": every row is a
    BlockedTaskRecord pairing an open task with at least one blocker."""
    blocked = await client.task_blocked()
    _require_rows_on_fake_leg(client, blocked)
    for row in blocked:
        assert isinstance(row, BlockedTaskRecord)
        assert row.task.status == "open"
        assert len(row.blockers) >= 1
    limited = await client.task_blocked(limit=1)
    assert [row.task.id for row in limited] == [row.task.id for row in blocked][:1]


async def test_ready_frontier_rows_are_open(client: LithosClientProtocol) -> None:
    ready = await client.task_ready()
    _require_rows_on_fake_leg(client, ready)
    assert all(task.status == "open" for task in ready)


# ── vendored-contract verification (issue #31) ──────────────────────────


async def test_vendored_contracts_match_live_tool_schemas() -> None:
    """Verify the vendored contracts themselves against the live server.

    The contracts under tests/contracts/ are the authoring-time reference for
    hermetic agents, so they need their own truth check: every contract tool
    must exist on the live server, every vendored request (canonical + named
    variants) must use only arguments the live inputSchema declares, and must
    supply everything the schema marks required. Response-shape-vs-live is
    data-dependent and deferred to the seeded-instance run (Lithos task
    c144b363).
    """
    url = os.environ.get(REAL_URL_ENV, "").strip()
    if not url:
        pytest.skip(
            f"vendored-contract verification needs {REAL_URL_ENV} "
            "(host/CI only; unit runs stay hermetic)"
        )

    from mcp import ClientSession
    from mcp.client.sse import sse_client

    config = LithosConfig(url=url)
    endpoint = f"{config.url.rstrip('/')}/{config.mcp_sse_path.strip('/')}"
    async with (
        sse_client(endpoint) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
    live_tools = {tool.name: tool for tool in listed.tools}

    problems: list[str] = []
    for path in sorted(CONTRACTS_DIR.glob("*.json")):
        if path.name.startswith("_"):
            continue
        contract = load_contract(path.stem)
        tool = contract["tool"]
        if tool not in live_tools:
            problems.append(f"{tool}: not present on the live server")
            continue
        schema = live_tools[tool].inputSchema or {}
        properties = set(schema.get("properties", {}))
        required = set(schema.get("required", []))
        requests = {
            "canonical": contract["request"]["canonical"],
            **contract["request"].get("variants", {}),
        }
        for name, args in requests.items():
            unknown = set(args) - properties
            if unknown:
                problems.append(
                    f"{tool}[{name}]: args not in live inputSchema: {sorted(unknown)}"
                )
            missing = required - set(args)
            if missing:
                problems.append(
                    f"{tool}[{name}]: live-required args missing: {sorted(missing)}"
                )
    assert not problems, (
        "vendored contracts diverge from live tools/list:\n" + "\n".join(problems)
    )


def _require_rows_on_fake_leg(client: LithosClientProtocol, rows: object) -> None:
    """Fail a for-all assertion that would pass vacuously on the fake leg.

    The fake leg always serves the demo dataset, which stocks every surface
    these tests sweep — an empty read there means the assertion exercised
    nothing. The real leg has no such guarantee (an empty but healthy server
    is a legitimate state), so it may run the for-alls over zero rows."""
    if isinstance(client, FakeLithosClient):
        assert rows, "demo dataset should make this assertion non-vacuous"
