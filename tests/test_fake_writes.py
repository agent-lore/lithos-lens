"""The fake that changes: writes over a frozen seed (T3-W3).

Every action slice's acceptance is "the board afterwards says X", so the fake
has to answer its READS differently once it has been written to. These tests
are that guarantee, stated on the fake alone — no routes exist yet — plus the
refusals each action slice maps to operator copy, and the per-instance
isolation that stops one test's write leaking into another's board.

The fixture is deliberately small and hand-built rather than the demo set: the
claims here are about one gate with two waiters and one predecessor with one
dependent, and a fixture with exactly that in it makes a wrong answer obvious.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from lithos_lens.config import EventsConfig, LithosConfig
from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.fake_lithos import FakeEventHub, FakeLithosClient
from lithos_lens.lithos_client import LithosToolError
from lithos_lens.task_graph import BlockerRecord, EdgeRecord
from lithos_lens.tasks import ClaimRecord, TaskRecord

pytestmark = pytest.mark.anyio

STAMP = "2026-09-01T09:00:00+00:00"
LATER = "2026-10-01T09:00:00+00:00"


def _task(task_id: str, **overrides: Any) -> TaskRecord:
    defaults: dict[str, Any] = {
        "title": task_id.replace("-", " ").capitalize(),
        "status": "open",
        "created_by": "planner",
        "created_at": STAMP,
        "tags": ("project:influx",),
    }
    defaults.update(overrides)
    return TaskRecord(id=task_id, **defaults)


def _edge(
    from_task_id: str,
    to_task_id: str,
    edge_type: str,
    *,
    created_by: str = "planner",
    metadata: dict[str, Any] | None = None,
) -> EdgeRecord:
    return EdgeRecord(
        from_task_id=from_task_id,
        to_task_id=to_task_id,
        type=edge_type,
        metadata=dict(metadata or {}),
        created_by=created_by,
        created_at=STAMP,
    )


def _edge_map(*edges: EdgeRecord) -> dict[str, tuple[EdgeRecord, ...]]:
    """The dataset's per-task edge map: one entry per endpoint, with direction."""
    by_task: dict[str, list[EdgeRecord]] = {}
    for edge in edges:
        by_task.setdefault(edge.from_task_id, []).append(
            replace(edge, direction="outgoing")
        )
        by_task.setdefault(edge.to_task_id, []).append(
            replace(edge, direction="incoming")
        )
    return {task_id: tuple(rows) for task_id, rows in by_task.items()}


def _gate_blocker() -> BlockerRecord:
    return BlockerRecord(
        kind="gate",
        task_id="gate-review",
        type="waits_on_gate",
        status="open",
        message="Waiting on human gate gate-review.",
    )


def _task_blocker(task_id: str) -> BlockerRecord:
    return BlockerRecord(
        kind="task",
        task_id=task_id,
        type="blocks",
        status="open",
        message=f"Waiting on predecessor {task_id} to complete.",
    )


def write_dataset() -> FakeLithosDataset:
    """One gate with two waiters, one predecessor with one dependent, an epic.

    ``solo`` is ready from the start and is the control: no write below should
    ever report it as released or re-blocked.
    """
    return FakeLithosDataset(
        tasks=(
            _task("gate-review", task_type="gate", metadata={"gate_type": "human"}),
            _task("waiter-one"),
            _task("waiter-two"),
            _task("pred"),
            _task("dep"),
            _task("solo"),
            _task("epic-one", task_type="epic"),
            _task("child-one"),
        ),
        ready_ids=frozenset({"gate-review", "pred", "solo", "epic-one", "child-one"}),
        blocked={
            "waiter-one": (_gate_blocker(),),
            "waiter-two": (_gate_blocker(),),
            "dep": (_task_blocker("pred"),),
        },
        edges=_edge_map(
            _edge("gate-review", "waiter-one", "waits_on_gate"),
            _edge("gate-review", "waiter-two", "waits_on_gate"),
            _edge("pred", "dep", "blocks"),
            _edge("epic-one", "child-one", "parent_child", metadata={"note": "seed"}),
        ),
        children={"epic-one": ("child-one",)},
        claims={
            "pred": (
                ClaimRecord(
                    agent="worker-a", aspect="implementation", expires_at=LATER
                ),
            )
        },
    )


def _client() -> FakeLithosClient:
    return FakeLithosClient(dataset=write_dataset())


async def _ready_ids(client: FakeLithosClient) -> set[str]:
    return {task.id for task in await client.task_ready()}


async def _blocked(client: FakeLithosClient) -> dict[str, tuple[BlockerRecord, ...]]:
    return {row.task.id: row.blockers for row in await client.task_blocked()}


# ── complete ────────────────────────────────────────────────────────────


async def test_completing_a_gate_moves_the_frontier_and_names_its_waiters() -> None:
    """The W4 acceptance, on the fake: the ready and blocked READS change.

    ``unblocked`` is exactly the waiters that crossed — not every dependent
    (``dep`` still waits on ``pred``) and not whatever was already ready
    (``solo``).
    """
    client = _client()
    assert await _ready_ids(client) == {
        "gate-review",
        "pred",
        "solo",
        "epic-one",
        "child-one",
    }
    assert set(await _blocked(client)) == {"waiter-one", "waiter-two", "dep"}

    result = await client.task_complete(
        "gate-review", agent="dave", outcome="Completed via Lens by dave"
    )

    assert result.success is True
    assert sorted(result.unblocked) == ["waiter-one", "waiter-two"]
    # The payload names the task it resolved and the stamp it wrote, exactly as
    # the vendored contract's canonical success payload does.
    assert (result.task_id, result.title) == ("gate-review", "Gate review")
    assert result.updated_at
    ready = await _ready_ids(client)
    assert {"waiter-one", "waiter-two"} <= ready
    # The gate itself leaves the frontier: it is no longer open.
    assert "gate-review" not in ready
    assert set(await _blocked(client)) == {"dep"}
    gate = await client.task_get("gate-review")
    assert gate.status == "completed"
    assert gate.outcome == "Completed via Lens by dave"
    assert gate.resolved_at


async def test_completing_releases_every_claim_on_the_task() -> None:
    client = _client()
    before = await client.task_status("pred")
    assert before is not None and len(before.claims) == 1

    await client.task_complete("pred", agent="dave")

    after = await client.task_status("pred")
    assert after is not None and after.claims == ()


async def test_completing_a_non_open_task_answers_task_not_found() -> None:
    """One code for two facts: "no such task" and "not open" are the same code.

    Which is why the write funnel re-reads the task to tell the operator which
    of the two happened (T3 D6).
    """
    client = _client()
    await client.task_complete("gate-review", agent="dave")

    with pytest.raises(LithosToolError) as not_open:
        await client.task_complete("gate-review", agent="dave")
    assert not_open.value.code == "task_not_found"

    with pytest.raises(LithosToolError) as missing:
        await client.task_complete("no-such-task", agent="dave")
    assert missing.value.code == "task_not_found"


# ── reopen ──────────────────────────────────────────────────────────────


async def test_reopening_a_completed_gate_re_blocks_the_same_waiters() -> None:
    client = _client()
    before = await _ready_ids(client)
    completion = await client.task_complete(
        "gate-review", agent="dave", outcome="opened by hand"
    )

    reopen = await client.task_reopen("gate-review", agent="dave")

    assert sorted(reopen.reblocked) == sorted(completion.unblocked)
    # The board is back where it started.
    assert await _ready_ids(client) == before
    assert set(await _blocked(client)) == {"waiter-one", "waiter-two", "dep"}


async def test_reopening_clears_the_outcome_and_posts_a_reopened_finding() -> None:
    """Upstream clears ``outcome``/``resolved_at`` and records the reopen as a
    ``[Reopened]`` finding — which is then the only surviving evidence of what
    the outcome had said."""
    client = _client()
    await client.task_complete("gate-review", agent="dave", outcome="ship it")

    await client.task_reopen("gate-review", agent="dave")

    gate = await client.task_get("gate-review")
    assert gate.status == "open"
    assert gate.outcome == ""
    assert gate.resolved_at == ""
    findings = await client.list_findings("gate-review")
    assert findings[-1].summary.startswith("[Reopened]")
    assert "ship it" in findings[-1].summary
    assert findings[-1].agent == "dave"


async def test_reopening_a_cancelled_predecessor_re_blocks_nobody() -> None:
    """Its dependents were stranded, not ready — so there is nobody to re-block.

    The count the cancelled-case receipt states comes from the task's own
    outgoing edges instead (T3 D8); here the point is that ``reblocked`` is
    empty and the dependent is waiting again rather than stranded.
    """
    client = _client()
    await client.task_cancel("pred", agent="dave")

    result = await client.task_reopen("pred", agent="dave")

    assert result.reblocked == ()
    assert [row.kind for row in (await _blocked(client))["dep"]] == ["task"]


async def test_reopening_an_open_task_answers_task_not_resolved() -> None:
    client = _client()
    with pytest.raises(LithosToolError) as excinfo:
        await client.task_reopen("solo", agent="dave")
    assert excinfo.value.code == "task_not_resolved"


# ── cancel ──────────────────────────────────────────────────────────────


async def test_cancelling_a_predecessor_strands_its_dependent() -> None:
    """The Needs-attention rule-1 shape: the blocker becomes unsatisfiable."""
    client = _client()

    await client.task_cancel("pred", agent="dave", reason="superseded")

    blocked = await _blocked(client)
    assert [row.kind for row in blocked["dep"]] == ["blocker_unsatisfiable"]
    assert blocked["dep"][0].status == "cancelled"
    assert blocked["dep"][0].task_id == "pred"
    assert "dep" not in await _ready_ids(client)


async def test_a_cancel_reason_never_lands_on_the_task() -> None:
    """It reaches the log and the event only (ROADMAP ledger #6), so a fake
    that stored it would let a test assert a fact the server does not keep."""
    client = _client()

    await client.task_cancel("pred", agent="dave", reason="superseded")

    cancelled = await client.task_get("pred")
    assert cancelled.status == "cancelled"
    assert cancelled.outcome == ""
    assert "superseded" not in str(cancelled.metadata)


async def test_cancelling_a_non_open_task_answers_task_not_found() -> None:
    client = _client()
    await client.task_cancel("pred", agent="dave")
    with pytest.raises(LithosToolError) as excinfo:
        await client.task_cancel("pred", agent="dave")
    assert excinfo.value.code == "task_not_found"


# ── create ──────────────────────────────────────────────────────────────


async def test_create_mints_a_task_and_resolves_its_links() -> None:
    client = _client()

    result = await client.task_create(
        title="Swap reads onto the new store",
        agent="dave",
        task_type="task",
        tags=["project:influx"],
        metadata={"project": "influx"},
        depends_on=["pred"],
        parent_task_id="epic-one",
    )

    assert result.success is True
    assert result.task_id
    assert result.title == "Swap reads onto the new store"
    assert result.updated_at
    assert result.depends_on == ("pred",)
    assert result.parent_task_id == "epic-one"
    # Readable afterwards, blocked by its predecessor, and a child of the epic.
    created = await client.task_get(result.task_id)
    assert created.created_by == "dave"
    assert created.tags == ("project:influx",)
    assert (await _blocked(client))[result.task_id][0].task_id == "pred"
    children = await client.task_children("epic-one")
    assert result.task_id in {child.id for child in children}


async def test_create_resolves_an_id_prefix_and_refuses_an_ambiguous_one() -> None:
    client = _client()
    exact = await client.task_create(title="Prefixed", agent="dave", depends_on=["pre"])
    assert exact.depends_on == ("pred",)

    with pytest.raises(LithosToolError) as excinfo:
        await client.task_create(title="Ambiguous", agent="dave", depends_on=["waiter"])

    assert excinfo.value.code == "ambiguous_id_prefix"
    # The field the whole envelope exists for: the mapper offers these as
    # choices.
    assert excinfo.value.envelope["candidates"] == ["waiter-one", "waiter-two"]


async def test_create_refuses_an_unknown_predecessor_and_a_bad_gate() -> None:
    client = _client()
    with pytest.raises(LithosToolError) as missing:
        await client.task_create(title="Orphan", agent="dave", depends_on=["nope"])
    assert missing.value.code == "task_not_found"

    with pytest.raises(LithosToolError) as untyped:
        await client.task_create(title="Gate", agent="dave", task_type="gate")
    assert untyped.value.code == "invalid_input"

    with pytest.raises(LithosToolError) as timer:
        await client.task_create(
            title="Gate",
            agent="dave",
            task_type="gate",
            metadata={"gate_type": "timer"},
        )
    assert timer.value.code == "invalid_input"

    with pytest.raises(LithosToolError) as blank:
        await client.task_create(title="   ", agent="dave")
    assert blank.value.code == "invalid_input"


# ── edges ───────────────────────────────────────────────────────────────


async def test_an_edge_that_closes_a_cycle_is_refused_and_changes_nothing() -> None:
    client = _client()
    before = await client.task_edge_list("dep")

    with pytest.raises(LithosToolError) as excinfo:
        await client.task_edge_upsert(
            from_task_id="dep", to_task_id="pred", edge_type="blocks", agent="dave"
        )

    assert excinfo.value.code == "cycle"
    # The message names members by full id, and is rendered verbatim.
    assert "pred" in str(excinfo.value) and "dep" in str(excinfo.value)
    assert await client.task_edge_list("dep") == before


async def test_re_upserting_an_edge_replaces_metadata_and_keeps_created_by() -> None:
    """The upsert's defining behaviour, and the reason ``created_by`` is
    evidence of who INSERTED an edge (T3 D11)."""
    client = _client()

    result = await client.task_edge_upsert(
        from_task_id="epic-one",
        to_task_id="child-one",
        edge_type="parent_child",
        agent="dave",
        metadata={"added_via": "lithos-lens"},
    )

    assert result.success is True
    edges = [
        edge
        for edge in await client.task_edge_list("child-one")
        if edge.type == "parent_child"
    ]
    # One edge, not two: the upsert landed on the existing one.
    assert len(edges) == 1
    assert edges[0].metadata == {"added_via": "lithos-lens"}
    assert edges[0].created_by == "planner"
    assert edges[0].created_at == STAMP


async def test_an_inserted_edge_blocks_its_target_and_is_read_back() -> None:
    client = _client()

    await client.task_edge_upsert(
        from_task_id="pred",
        to_task_id="solo",
        edge_type="blocks",
        agent="dave",
        metadata={"added_via": "lithos-lens"},
    )

    assert "solo" not in await _ready_ids(client)
    blockers = (await _blocked(client))["solo"]
    assert [(row.kind, row.task_id) for row in blockers] == [("task", "pred")]
    inserted = [
        edge for edge in await client.task_edge_list("solo") if edge.type == "blocks"
    ]
    assert inserted[0].created_by == "dave"
    assert inserted[0].direction == "incoming"


async def test_every_other_edge_refusal_is_reachable() -> None:
    """Every edge code the mapper has a row for, from one fixture.

    Each tuple is ``(code, from, to, type)``: the arguments that provoke the
    refusal, so a code that stopped being reachable (or started answering a
    different one) fails on the row that names it.
    """
    client = _client()
    cases = [
        ("invalid_edge_type", "pred", "dep", "duplicates"),
        ("self_edge", "pred", "pred", "blocks"),
        ("task_not_found", "no-such-task", "dep", "blocks"),
        ("not_a_gate", "pred", "dep", "waits_on_gate"),
        ("parent_exists", "solo", "child-one", "parent_child"),
        ("ambiguous_id_prefix", "waiter", "dep", "blocks"),
    ]
    for code, from_task_id, to_task_id, edge_type in cases:
        with pytest.raises(LithosToolError) as excinfo:
            await client.task_edge_upsert(
                from_task_id=from_task_id,
                to_task_id=to_task_id,
                edge_type=edge_type,
                agent="dave",
            )
        assert excinfo.value.code == code, (from_task_id, to_task_id, edge_type)
        assert excinfo.value.envelope["code"] == code
        # Every refusal says nothing was changed, so nothing may be.
        assert client.write_calls[-1][0] == "lithos_task_edge_upsert"
    # parent_exists names the parent the operator has to replace (T3 D11).
    with pytest.raises(LithosToolError) as parented:
        await client.task_edge_upsert(
            from_task_id="solo",
            to_task_id="child-one",
            edge_type="parent_child",
            agent="dave",
        )
    assert "epic-one" in str(parented.value)
    assert await client.task_edge_list("dep") == list(write_dataset().edges["dep"])


# ── events ──────────────────────────────────────────────────────────────


async def test_a_completion_publishes_task_completed_and_an_edge_write_nothing() -> (
    None
):
    """Writes announce themselves exactly as the real server does — and an edge
    write does not, because upstream emits no event for one."""
    hub = FakeEventHub(EventsConfig(enabled=True), LithosConfig())
    await hub.start()
    client = FakeLithosClient(dataset=write_dataset(), events=hub)
    try:
        queue = hub.subscribe()

        await client.task_complete("gate-review", agent="dave", outcome="done")
        event = await asyncio.wait_for(queue.get(), timeout=0.1)
        assert event.type == "task.completed"
        assert event.task_id == "gate-review"
        assert event.payload["outcome"] == "done"
        assert event.requires_refresh is True

        await client.task_edge_upsert(
            from_task_id="pred", to_task_id="solo", edge_type="blocks", agent="dave"
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.05)
    finally:
        await hub.stop()


async def test_each_write_publishes_the_event_the_server_would() -> None:
    hub = FakeEventHub(EventsConfig(enabled=True), LithosConfig())
    await hub.start()
    client = FakeLithosClient(dataset=write_dataset(), events=hub)
    try:
        queue = hub.subscribe()
        await client.task_cancel("pred", agent="dave", reason="superseded")
        await client.task_reopen("pred", agent="dave")
        await client.task_create(title="Fresh", agent="dave")
        types = [
            (await asyncio.wait_for(queue.get(), timeout=0.1)).type for _ in range(3)
        ]
        assert types == ["task.cancelled", "task.reopened", "task.created"]
    finally:
        await hub.stop()


async def test_a_write_without_a_hub_still_applies() -> None:
    """Most unit tests construct the fake with no hub; the write must land."""
    client = _client()
    assert client.events is None
    await client.task_complete("gate-review", agent="dave")
    assert (await client.task_get("gate-review")).status == "completed"


# ── the call log, and per-instance isolation ────────────────────────────


async def test_the_write_log_records_every_attempt_in_the_wire_shape() -> None:
    """All five tools, refusals included: a refused write is still a call the
    server received.

    The arguments are the ones the real client would have sent — both project
    conventions on a create, for instance — because later slices assert on
    exactly that, and both sides build them with the same builder.
    """
    client = _client()
    await client.task_complete("gate-review", agent="dave", outcome="done")
    with pytest.raises(LithosToolError):
        await client.task_complete("gate-review", agent="dave")
    await client.task_cancel("pred", agent="dave", reason="superseded")
    await client.task_reopen("pred", agent="dave")
    await client.task_create(
        title="Fresh",
        agent="dave",
        task_type="gate",
        tags=["project:influx"],
        metadata={"project": "influx", "gate_type": "human"},
    )
    await client.task_edge_upsert(
        from_task_id="pred", to_task_id="solo", edge_type="blocks", agent="dave"
    )

    assert client.write_calls == [
        (
            "lithos_task_complete",
            {"task_id": "gate-review", "agent": "dave", "outcome": "done"},
        ),
        ("lithos_task_complete", {"task_id": "gate-review", "agent": "dave"}),
        (
            "lithos_task_cancel",
            {"task_id": "pred", "agent": "dave", "reason": "superseded"},
        ),
        ("lithos_task_reopen", {"task_id": "pred", "agent": "dave"}),
        (
            "lithos_task_create",
            {
                "title": "Fresh",
                "agent": "dave",
                "task_type": "gate",
                "tags": ["project:influx"],
                "metadata": {"project": "influx", "gate_type": "human"},
            },
        ),
        (
            "lithos_task_edge_upsert",
            {
                "from_task_id": "pred",
                "to_task_id": "solo",
                "type": "blocks",
                "agent": "dave",
            },
        ),
    ]


async def test_two_fakes_over_one_seed_do_not_share_an_overlay() -> None:
    """The isolation the whole overlay design exists for: one test's write must
    not reach another's board, even when both were handed the same frozen
    dataset."""
    seed = write_dataset()
    one = FakeLithosClient(dataset=seed)
    two = FakeLithosClient(dataset=seed)

    await one.task_complete("gate-review", agent="dave", outcome="done")
    await one.task_create(title="Only in one", agent="dave")

    assert (await two.task_get("gate-review")).status == "open"
    assert await _ready_ids(two) == {
        "gate-review",
        "pred",
        "solo",
        "epic-one",
        "child-one",
    }
    assert {task.id for task in await two.list_tasks()} == {
        task.id for task in seed.tasks
    }
    assert two.write_calls == []
    # And the seed itself was never touched.
    assert seed.tasks == write_dataset().tasks
