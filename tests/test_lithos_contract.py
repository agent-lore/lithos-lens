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
    assert all(task.claims is not None for task in with_claims)
    without = await client.list_tasks()
    assert all(task.claims is None for task in without)


async def test_task_ready_claims_presence_follows_with_claims(
    client: LithosClientProtocol,
) -> None:
    with_claims = await client.task_ready(with_claims=True)
    assert all(task.claims is not None for task in with_claims)
    # Lens's deliberate False default (divergent from upstream's True) must
    # hold on both legs.
    without = await client.task_ready()
    assert all(task.claims is None for task in without)


# ── shape invariants over whatever rows exist ───────────────────────────


async def test_task_ready_honors_limit(client: LithosClientProtocol) -> None:
    assert len(await client.task_ready(limit=1)) <= 1


async def test_task_blocked_honors_limit_and_rows_carry_blockers(
    client: LithosClientProtocol,
) -> None:
    """Blocked means "open but not ready for a stated reason": every row is a
    BlockedTaskRecord pairing an open task with at least one blocker."""
    blocked = await client.task_blocked()
    for row in blocked:
        assert isinstance(row, BlockedTaskRecord)
        assert row.task.status == "open"
        assert len(row.blockers) >= 1
    assert len(await client.task_blocked(limit=1)) <= 1


async def test_ready_frontier_rows_are_open(client: LithosClientProtocol) -> None:
    assert all(task.status == "open" for task in await client.task_ready())
