"""The fake that changes: writes over a frozen seed (T3-W3).

Every action slice's acceptance is "the board afterwards says X", so the fake
has to answer its READS differently once it has been written to. These tests
are that guarantee, stated on the fake alone — no routes exist yet — plus the
refusals each action slice maps to operator copy, the id domain upstream
actually enforces, and the per-instance isolation that stops one test's write
leaking into another's board.

The fixture is hand-built rather than the demo set, and it is built to make a
wrong answer visible: one gate with three waiters, of which ONE also waits on a
predecessor (so "released every direct dependent" is not the same answer as
"released whoever became ready"), two timer gates astride their ``ready_at``,
a predecessor carrying TWO claims, and an epic over a child (so a hierarchy
cycle is reachable, not only a dependency one).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from lithos_lens.config import EventsConfig, LithosConfig, load_config
from lithos_lens.events import LensEvent
from lithos_lens.fake_dataset import FakeLithosDataset, demo_dataset
from lithos_lens.fake_lithos import FakeEventHub, FakeLithosClient
from lithos_lens.lithos_client import LithosToolError
from lithos_lens.state import AppState
from lithos_lens.task_graph import BlockerRecord, EdgeRecord
from lithos_lens.task_writes import (
    TaskCancelResult,
    TaskCompleteResult,
    TaskEdgeUpsertResult,
    TaskReopenResult,
)
from lithos_lens.tasks import ClaimRecord, TaskRecord
from lithos_lens.web import create_app
from tests.conftest import load_contract

pytestmark = pytest.mark.anyio

STAMP = "2026-09-01T09:00:00+00:00"
LATER = "2026-10-01T09:00:00+00:00"
#: Astride a timer gate's wait, far enough either side that no clock drift
#: during a test run can move a case across the boundary.
LONG_PAST = "2000-01-01T00:00:00+00:00"
LONG_AHEAD = "2099-01-01T00:00:00+00:00"


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


def _gate(task_id: str, gate_type: str, **metadata: Any) -> TaskRecord:
    return _task(
        task_id, task_type="gate", metadata={"gate_type": gate_type, **metadata}
    )


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


def _gate_blocker(gate_id: str = "gate-review") -> BlockerRecord:
    return BlockerRecord(
        kind="gate",
        task_id=gate_id,
        type="waits_on_gate",
        status="open",
        message=f"Waiting on human gate {gate_id}.",
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
    """The fixture described in the module docstring.

    ``solo`` is ready from the start and is the control: no write below should
    report it released or re-blocked unless that write targets it.
    """
    return FakeLithosDataset(
        tasks=(
            _gate("gate-review", "human"),
            _gate("timer-elapsed", "timer", ready_at=LONG_PAST),
            _gate("timer-pending", "timer", ready_at=LONG_AHEAD),
            _task("waiter-one"),
            _task("waiter-two"),
            # Waits on the gate AND on `pred`: completing only the gate must
            # neither ready it nor report it.
            _task("waiter-both"),
            _task("pred"),
            _task("dep"),
            _task("solo"),
            _task("epic-one", task_type="epic"),
            _task("child-one"),
        ),
        # Workable tasks only: upstream never puts a gate or an epic on either
        # frontier, so a seed that did would state a verdict no server gives.
        ready_ids=frozenset({"pred", "solo", "child-one"}),
        blocked={
            "waiter-one": (_gate_blocker(),),
            "waiter-two": (_gate_blocker(),),
            "waiter-both": (_gate_blocker(), _task_blocker("pred")),
            "dep": (_task_blocker("pred"),),
        },
        edges=_edge_map(
            _edge("gate-review", "waiter-one", "waits_on_gate"),
            _edge("gate-review", "waiter-two", "waits_on_gate"),
            _edge("gate-review", "waiter-both", "waits_on_gate"),
            _edge("pred", "waiter-both", "blocks"),
            _edge("pred", "dep", "blocks"),
            _edge("epic-one", "child-one", "parent_child", metadata={"note": "seed"}),
        ),
        children={"epic-one": ("child-one",)},
        claims={
            # TWO claims, so "releases every claim" is distinguishable from
            # "releases a claim".
            "pred": (
                ClaimRecord(
                    agent="worker-a", aspect="implementation", expires_at=LATER
                ),
                ClaimRecord(agent="worker-b", aspect="review", expires_at=LATER),
            )
        },
    )


SEED_READY = {"pred", "solo", "child-one"}
SEED_BLOCKED = {"waiter-one", "waiter-two", "waiter-both", "dep"}


def _client() -> FakeLithosClient:
    return FakeLithosClient(dataset=write_dataset())


async def _ready_ids(client: FakeLithosClient) -> set[str]:
    return {task.id for task in await client.task_ready()}


async def _blocked(client: FakeLithosClient) -> dict[str, tuple[BlockerRecord, ...]]:
    return {row.task.id: row.blockers for row in await client.task_blocked()}


async def _all_edges(client: FakeLithosClient) -> dict[str, list[EdgeRecord]]:
    """Every fixture task's edge list — the snapshot a refusal must not move."""
    return {
        task.id: list(await client.task_edge_list(task.id))
        for task in write_dataset().tasks
    }


# ── complete ────────────────────────────────────────────────────────────


async def test_completing_a_gate_moves_the_frontier_and_names_its_waiters() -> None:
    """The W4 acceptance, on the fake: the ready and blocked READS change.

    ``unblocked`` is exactly the waiters that CROSSED — so ``waiter-both``,
    which still waits on ``pred``, is neither reported nor readied, ``dep`` is
    untouched, and ``solo`` (already ready) is not claimed as a release. An
    implementation that returned every direct dependent would fail here.
    """
    client = _client()
    assert await _ready_ids(client) == SEED_READY
    assert set(await _blocked(client)) == SEED_BLOCKED

    result = await client.task_complete(
        "gate-review", agent="dave", outcome="Completed via Lens by dave"
    )

    stored = await client.task_get("gate-review")
    assert result == TaskCompleteResult(
        success=True,
        task_id="gate-review",
        title="Gate review",
        # The stamp the write STORED, not merely a non-empty string: a result
        # carrying some other timestamp would pass a truthiness check.
        updated_at=stored.resolved_at,
        unblocked=("waiter-one", "waiter-two"),
    )
    assert result.updated_at
    ready = await _ready_ids(client)
    assert {"waiter-one", "waiter-two"} <= ready
    assert "waiter-both" not in ready
    # The gate itself leaves the frontier: it is no longer open.
    assert "gate-review" not in ready
    blocked = await _blocked(client)
    assert set(blocked) == {"waiter-both", "dep"}
    # Its remaining blocker is the predecessor, and the satisfied gate is gone.
    assert [row.task_id for row in blocked["waiter-both"]] == ["pred"]
    assert stored.status == "completed"
    assert stored.outcome == "Completed via Lens by dave"
    assert stored.resolved_at


@pytest.mark.parametrize("action", ["complete", "cancel"])
async def test_resolving_a_task_releases_every_claim_on_it(action: str) -> None:
    """Both tools release EVERY claim, not the first one.

    ``pred`` carries two, so a fake that popped one would leave the other
    behind and the board would keep calling the task in progress.
    """
    client = _client()
    before = await client.task_status("pred")
    assert before is not None and len(before.claims) == 2

    if action == "complete":
        await client.task_complete("pred", agent="dave")
    else:
        await client.task_cancel("pred", agent="dave")

    after = await client.task_status("pred")
    assert after is not None and after.claims == ()
    # And the claims are gone from the master list's inline claims too.
    rows = {task.id: task for task in await client.list_tasks(with_claims=True)}
    assert rows["pred"].claims == ()


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
    completion = await client.task_complete(
        "gate-review", agent="dave", outcome="opened by hand"
    )

    reopen = await client.task_reopen("gate-review", agent="dave")

    # A reopen CLEARS resolved_at, and its finding is stamped by its own clock
    # read upstream, so the stamp it answers is checked by what upstream
    # guarantees about it: strictly after the completion's (`_advance_stamp`).
    assert reopen.updated_at > completion.updated_at
    assert reopen == TaskReopenResult(
        success=True,
        task_id="gate-review",
        title="Gate review",
        updated_at=reopen.updated_at,
        reblocked=completion.unblocked,
    )
    assert reopen.updated_at
    # The board is back where it started.
    assert await _ready_ids(client) == SEED_READY
    assert set(await _blocked(client)) == SEED_BLOCKED


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
    # `lithos_task_reopen`'s own wording (a4d2d62), outcome appended only when
    # there was one.
    assert findings[-1].summary == (
        "[Reopened] task reopened (was completed); prior outcome: ship it"
    )
    assert findings[-1].agent == "dave"

    await client.task_cancel("gate-review", agent="dave")
    await client.task_reopen("gate-review", agent="dave")
    assert (await client.list_findings("gate-review"))[-1].summary == (
        "[Reopened] task reopened (was cancelled)"
    )


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

    result = await client.task_cancel("pred", agent="dave", reason="superseded")

    stored = await client.task_get("pred")
    assert result == TaskCancelResult(
        success=True,
        task_id="pred",
        title="Pred",
        updated_at=stored.resolved_at,
    )
    assert result.updated_at
    blocked = await _blocked(client)
    assert [row.kind for row in blocked["dep"]] == ["blocker_unsatisfiable"]
    assert blocked["dep"][0].status == "cancelled"
    assert blocked["dep"][0].task_id == "pred"
    assert "dep" not in await _ready_ids(client)


async def test_a_cancel_reason_never_lands_on_the_task() -> None:
    """It reaches the log and the event only (ROADMAP ledger #6), so a fake
    that stored it would let a test assert a fact the real server does not
    keep."""
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


# ── the id domain every write shares ────────────────────────────────────


async def test_an_id_prefix_must_be_a_full_id_or_at_least_six_characters() -> None:
    """Upstream's id domain, which the fake may not be more generous than.

    Probed live: ``task_id 'zqxj' is too short: pass the full task id or a
    prefix of at least 6 characters.`` A fake that resolved ``pre`` to ``pred``
    would let the create and edge slices pass requests the real server refuses
    — and their tests would encode the wrong boundary.
    """
    client = _client()

    # A full id always resolves, however short it is.
    exact = await client.task_create(title="Full id", agent="dave", depends_on=["pred"])
    assert exact.depends_on == ("pred",)

    # Six characters, matching one task: resolved.
    six = await client.task_create(title="Six", agent="dave", depends_on=["waiter-o"])
    assert six.depends_on == ("waiter-one",)

    # Five characters that are not a full id: refused BEFORE any lookup, even
    # though "waite" would match.
    with pytest.raises(LithosToolError) as short:
        await client.task_create(title="Five", agent="dave", depends_on=["waite"])
    assert short.value.code == "invalid_input"
    assert "at least 6 characters" in str(short.value)
    assert short.value.envelope["code"] == "invalid_input"

    # A searched prefix that matches nothing is the RESOLVER's refusal, with
    # its own message — the same code the tool's own lookup uses, which is why
    # the mapper reads the code and never the text.
    with pytest.raises(LithosToolError) as nomatch:
        await client.task_create(title="None", agent="dave", depends_on=["zqxjqw"])
    assert nomatch.value.code == "task_not_found"
    assert str(nomatch.value) == ("No task matches id prefix 'zqxjqw' (depends_on).")

    # At or above a full id's length the resolver stops searching and the
    # value goes to the tool's own lookup, which answers for itself.
    full_length = "zqxjqw" + "0" * 30
    assert len(full_length) == 36
    with pytest.raises(LithosToolError) as passed_through:
        await client.task_create(title="Full", agent="dave", depends_on=[full_length])
    assert passed_through.value.code == "task_not_found"
    assert str(passed_through.value) == (
        f"depends_on references nonexistent task(s): ['{full_length}']"
    )


async def test_an_ambiguous_prefix_names_at_most_five_candidates() -> None:
    """Upstream caps the candidate list, and quotes the CAPPED count.

    Minted tasks all share the `fake-created-` prefix, so six creates are
    enough to exceed the cap. A fake that returned every match would hand the
    mapper a chooser upstream would never send, and a message disagreeing with
    the list beside it.
    """
    client = _client()
    for index in range(6):
        await client.task_create(title=f"Minted {index}", agent="dave")

    with pytest.raises(LithosToolError) as excinfo:
        await client.task_create(title="Ambiguous", agent="dave", depends_on=["fake-c"])

    candidates = excinfo.value.envelope["candidates"]
    assert len(candidates) == 5
    assert all(set(candidate) == {"id", "title"} for candidate in candidates)
    # The count in the message is the capped one, not the true total of six.
    assert "5 or more matches" in str(excinfo.value)


async def test_an_ambiguous_prefix_names_its_candidates_as_records() -> None:
    """``candidates`` are ``{id, title}`` RECORDS, not id strings.

    The shape is the mapper's input: it renders "which of these did you mean?"
    with the titles. Probed live — a list of bare ids would render a chooser
    with nothing to read in it.
    """
    client = _client()

    with pytest.raises(LithosToolError) as excinfo:
        await client.task_create(
            title="Ambiguous", agent="dave", depends_on=["waiter-"]
        )

    assert excinfo.value.code == "ambiguous_id_prefix"
    assert excinfo.value.envelope["candidates"] == [
        {"id": "waiter-both", "title": "Waiter both"},
        {"id": "waiter-one", "title": "Waiter one"},
        {"id": "waiter-two", "title": "Waiter two"},
    ]
    # The count is the one it returned — a hard-coded "2 or more" would
    # contradict the three candidates beside it.
    assert str(excinfo.value) == (
        "Task id prefix 'waiter-' (depends_on) is ambiguous: 3 or more matches. "
        "Retry with a longer prefix or a full id from candidates."
    )


@pytest.mark.parametrize(
    ("label", "arguments", "code"),
    [
        # Two codes of their OWN, probed against a live Lithos — not
        # `invalid_input`, which is what earlier passes of this slice recorded.
        ("unknown type", {"title": "T", "task_type": "milestone"}, "invalid_task_type"),
        (
            "depends_on in metadata",
            {"title": "T", "metadata": {"depends_on": ["pred"]}},
            "invalid_metadata_key",
        ),
        (
            "blocked_on in metadata",
            {"title": "T", "metadata": {"blocked_on": ["pred"]}},
            "invalid_metadata_key",
        ),
        # The gate rules really are `invalid_input`.
        ("gate with no type", {"title": "T", "task_type": "gate"}, "invalid_input"),
        (
            "timer with no ready_at",
            {"title": "T", "task_type": "gate", "metadata": {"gate_type": "timer"}},
            "invalid_input",
        ),
        (
            "timer with unparseable ready_at",
            {
                "title": "T",
                "task_type": "gate",
                "metadata": {"gate_type": "timer", "ready_at": "soon"},
            },
            "invalid_input",
        ),
        (
            "missing predecessor",
            {"title": "T", "depends_on": ["nope-nope"]},
            "task_not_found",
        ),
        (
            "missing parent",
            {"title": "T", "parent_task_id": "nope-nope"},
            "task_not_found",
        ),
    ],
)
async def test_create_refuses_what_upstream_refuses(
    label: str, arguments: dict[str, Any], code: str
) -> None:
    """Every documented create validation, with the code upstream raises.

    ``metadata.depends_on`` is the one worth naming twice: dependencies are
    first-class edges now, so upstream refuses the old spelling rather than
    ignoring it — and it refuses it with `invalid_metadata_key`, which the
    mapper must tell apart from an ordinary `invalid_input` on a form field.
    """
    client = _client()
    before = {task.id for task in await client.list_tasks()}

    with pytest.raises(LithosToolError) as excinfo:
        await client.task_create(agent="dave", **arguments)

    assert excinfo.value.code == code, label
    assert excinfo.value.envelope["code"] == code
    assert {task.id for task in await client.list_tasks()} == before


async def test_create_has_no_title_rule_of_its_own() -> None:
    """Upstream accepts an empty title and stores it, so the fake must too.

    Lens refuses one in its own form validation (T3 D10), and that is the right
    place for it: a fake that refused it here would have the create slice
    tested against a refusal the server never sends — the wrong code, on the
    wrong field, from the wrong layer.
    """
    client = _client()

    result = await client.task_create(title="", agent="dave")

    assert result.success is True
    assert (await client.task_get(result.task_id)).title == ""


async def test_create_inserts_one_edge_per_repeated_predecessor() -> None:
    """A browser can submit the same predecessor twice.

    Upstream's edge table is unique on (from, to, type) and it dedupes before
    inserting, while the response still echoes the resolved request list — so
    the echo repeats and the graph does not. A fake that inserted twice would
    hand the graph and readiness slices a duplicate edge that cannot exist.
    """
    client = _client()

    result = await client.task_create(
        title="Twice", agent="dave", depends_on=["pred", "pred"]
    )

    # The echo keeps both entries …
    assert result.depends_on == ("pred", "pred")
    # … and exactly one edge was written.
    incoming = [
        edge
        for edge in await client.task_edge_list(result.task_id)
        if edge.type == "blocks"
    ]
    assert len(incoming) == 1
    assert incoming[0].from_task_id == "pred"
    assert [row.task_id for row in (await _blocked(client))[result.task_id]] == ["pred"]


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

    # `success` is DERIVED here: create's payload carries no such flag.
    assert result.success is True
    assert result.task_id
    assert result.title == "Swap reads onto the new store"
    assert result.depends_on == ("pred",)
    assert result.parent_task_id == "epic-one"
    # Readable afterwards, blocked by its predecessor, and a child of the epic.
    created = await client.task_get(result.task_id)
    assert result.updated_at == created.created_at
    assert created.created_by == "dave"
    assert created.tags == ("project:influx",)
    assert created.metadata == {"project": "influx"}
    assert (await _blocked(client))[result.task_id][0].task_id == "pred"
    children = await client.task_children("epic-one")
    assert result.task_id in {child.id for child in children}


# ── edges ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "from_task_id", "to_task_id", "edge_type"),
    [
        # A dependency loop over `blocks` …
        ("blocks", "dep", "pred", "blocks"),
        # … one that closes through a `waits_on_gate` edge, so the walk has to
        # span BOTH blocking types (gate-review already gates waiter-one) …
        ("mixed blocking", "waiter-one", "gate-review", "blocks"),
        # … and a hierarchy that would contain itself. The acceptance says "an
        # edge that closes a cycle", not "a dependency edge": reversing the
        # seeded epic-one -> child-one parentage is refused too.
        ("parent_child", "child-one", "epic-one", "parent_child"),
    ],
)
async def test_every_cycle_closing_edge_is_refused_and_changes_nothing(
    label: str, from_task_id: str, to_task_id: str, edge_type: str
) -> None:
    client = _client()
    before = await _all_edges(client)

    with pytest.raises(LithosToolError) as excinfo:
        await client.task_edge_upsert(
            from_task_id=from_task_id,
            to_task_id=to_task_id,
            edge_type=edge_type,
            agent="dave",
        )

    assert excinfo.value.code == "cycle", label
    # The message names members by full id, and is rendered verbatim.
    assert from_task_id in str(excinfo.value)
    assert to_task_id in str(excinfo.value)
    assert await _all_edges(client) == before
    # A refused hierarchy edge must not have left a child behind either.
    assert {child.id for child in await client.task_children("child-one")} == set()


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

    # The resolved endpoints and their titles come back, same as an insert.
    assert result == TaskEdgeUpsertResult(
        success=True,
        from_task_id="epic-one",
        from_title="Epic one",
        to_task_id="child-one",
        to_title="Child one",
    )
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


@pytest.mark.parametrize(
    "metadata_argument",
    [pytest.param({}, id="omitted"), pytest.param({"metadata": {}}, id="empty")],
)
async def test_re_upserting_without_metadata_clears_the_edges_metadata(
    metadata_argument: dict[str, Any],
) -> None:
    """Replacement means replacement for an empty value too: upstream binds an
    absent ``metadata`` as NULL in ``ON CONFLICT ... DO UPDATE SET metadata =
    excluded.metadata`` and reads NULL back as ``{}`` (lithos
    ``coordination.py`` ``upsert_edge`` / ``_decode_metadata``). A fake that
    kept the old metadata on an empty re-upsert would tell later slices an
    edge still carries what the server dropped."""
    client = _client()

    await client.task_edge_upsert(
        from_task_id="epic-one",
        to_task_id="child-one",
        edge_type="parent_child",
        agent="dave",
        **metadata_argument,
    )

    for endpoint in ("epic-one", "child-one"):
        edges = [
            edge
            for edge in await client.task_edge_list(endpoint)
            if edge.type == "parent_child"
        ]
        assert len(edges) == 1, endpoint
        assert edges[0].metadata == {}, endpoint
        assert edges[0].created_by == "planner", endpoint
        assert edges[0].created_at == STAMP, endpoint


async def test_an_insert_and_a_re_upsert_answer_the_same_payload() -> None:
    """Nothing in the payload says which happened — the residual T3 D11 states.

    Asserted as an equality so a fake that grew a "created" flag of its own
    would fail here rather than quietly handing the edge slice a guarantee the
    real server does not give.
    """
    client = _client()
    arguments = {
        "from_task_id": "pred",
        "to_task_id": "solo",
        "edge_type": "blocks",
        "agent": "dave",
    }

    inserted = await client.task_edge_upsert(**arguments, metadata={"pass": "first"})
    again = await client.task_edge_upsert(**arguments, metadata={"pass": "second"})

    assert inserted == again
    assert inserted == TaskEdgeUpsertResult(
        success=True,
        from_task_id="pred",
        from_title="Pred",
        to_task_id="solo",
        to_title="Solo",
    )
    edges = [
        edge for edge in await client.task_edge_list("solo") if edge.type == "blocks"
    ]
    assert len(edges) == 1
    assert edges[0].metadata == {"pass": "second"}
    assert edges[0].created_by == "dave"


async def test_an_inserted_edge_blocks_its_target() -> None:
    client = _client()

    await client.task_edge_upsert(
        from_task_id="pred", to_task_id="solo", edge_type="blocks", agent="dave"
    )

    assert "solo" not in await _ready_ids(client)
    blockers = (await _blocked(client))["solo"]
    assert [(row.kind, row.task_id) for row in blockers] == [("task", "pred")]
    inserted = [
        edge for edge in await client.task_edge_list("solo") if edge.type == "blocks"
    ]
    assert inserted[0].created_by == "dave"
    assert inserted[0].direction == "incoming"


async def test_an_edge_added_to_a_seed_blocked_waiter_accumulates() -> None:
    """Seed plus overlay, on ONE row: ``waiter-one`` already waits on the gate
    in the seed, and an upsert adds ``solo`` in front of it. The readiness
    oracle must answer from both — so completing the gate leaves ``solo``
    standing and releases only ``waiter-two``, and only completing ``solo``
    as well readies ``waiter-one``. A recomputation that kept just the seed's
    blockers would ready ``waiter-one`` at the gate and report it released."""
    client = _client()

    await client.task_edge_upsert(
        from_task_id="solo", to_task_id="waiter-one", edge_type="blocks", agent="dave"
    )

    blocked = await _blocked(client)
    assert [(row.kind, row.task_id) for row in blocked["waiter-one"]] == [
        ("gate", "gate-review"),
        ("task", "solo"),
    ]

    gate_done = await client.task_complete("gate-review", agent="dave")

    assert gate_done.unblocked == ("waiter-two",)
    ready = await _ready_ids(client)
    assert "waiter-one" not in ready
    assert "waiter-two" in ready
    blocked = await _blocked(client)
    assert [(row.kind, row.task_id) for row in blocked["waiter-one"]] == [
        ("task", "solo")
    ]

    solo_done = await client.task_complete("solo", agent="dave")

    assert solo_done.unblocked == ("waiter-one",)
    assert "waiter-one" in await _ready_ids(client)
    assert "waiter-one" not in await _blocked(client)


@pytest.mark.parametrize(
    ("gate_id", "still_ready"),
    [("timer-elapsed", True), ("timer-pending", False)],
)
async def test_a_timer_gate_blocks_only_until_its_ready_at_passes(
    gate_id: str, still_ready: bool
) -> None:
    """The one blocker whose answer is the clock's, not a status's.

    Upstream resolves a ``timer`` gate by itself once ``metadata.ready_at``
    passes, so a waiter on an ELAPSED open timer gate is ready — treating every
    open gate as unsatisfied would hand the proceed-anyway and edge slices the
    opposite answer from the real server.
    """
    client = _client()

    await client.task_edge_upsert(
        from_task_id=gate_id,
        to_task_id="solo",
        edge_type="waits_on_gate",
        agent="dave",
    )

    assert ("solo" in await _ready_ids(client)) is still_ready
    blocked = await _blocked(client)
    if still_ready:
        assert "solo" not in blocked
    else:
        assert [(row.kind, row.task_id) for row in blocked["solo"]] == [
            ("gate", gate_id)
        ]
        assert "ready_at" in blocked["solo"][0].message


async def test_cancelling_an_elapsed_timer_gate_strands_its_waiter() -> None:
    """Cancellation wins over the clock.

    An elapsed OPEN timer gate stops blocking (the test above), but cancelling
    it must strand its waiter as ``blocker_unsatisfiable`` — that is the
    consequence the cancel confirm page states (T3 D9). Reading `ready_at`
    without the gate's status leaves the waiter ready and the strand invisible.
    """
    client = _client()
    await client.task_edge_upsert(
        from_task_id="timer-elapsed",
        to_task_id="solo",
        edge_type="waits_on_gate",
        agent="dave",
    )
    assert "solo" in await _ready_ids(client), "an elapsed timer gate blocks nobody"

    await client.task_cancel("timer-elapsed", agent="dave")

    assert "solo" not in await _ready_ids(client)
    blockers = (await _blocked(client))["solo"]
    assert [(row.kind, row.task_id, row.status) for row in blockers] == [
        ("blocker_unsatisfiable", "timer-elapsed", "cancelled")
    ]

    # And reopening it hands the waiter back to the clock.
    await client.task_reopen("timer-elapsed", agent="dave")
    assert "solo" in await _ready_ids(client)


@pytest.mark.parametrize(
    ("code", "from_task_id", "to_task_id", "edge_type"),
    [
        ("invalid_edge_type", "pred", "dep", "duplicates"),
        ("self_edge", "pred", "pred", "blocks"),
        ("task_not_found", "no-such-task", "dep", "blocks"),
        ("not_a_gate", "pred", "dep", "waits_on_gate"),
        ("parent_exists", "solo", "child-one", "parent_child"),
        ("ambiguous_id_prefix", "waiter-", "dep", "blocks"),
        ("invalid_input", "abc", "dep", "blocks"),
    ],
)
async def test_every_other_edge_refusal_is_reachable_and_writes_nothing(
    code: str, from_task_id: str, to_task_id: str, edge_type: str
) -> None:
    """Every edge code the mapper has a row for, from one fixture.

    Each refusal is also checked to have changed NOTHING anywhere — every
    fixture task's edge list, not just one endpoint's — because "nothing was
    changed" is the first thing the operator copy promises.
    """
    client = _client()
    before = await _all_edges(client)

    with pytest.raises(LithosToolError) as excinfo:
        await client.task_edge_upsert(
            from_task_id=from_task_id,
            to_task_id=to_task_id,
            edge_type=edge_type,
            agent="dave",
        )

    assert excinfo.value.code == code
    assert excinfo.value.envelope["code"] == code
    assert client.write_calls[-1][0] == "lithos_task_edge_upsert"
    assert await _all_edges(client) == before


async def test_parent_exists_names_the_parent_to_replace() -> None:
    """D11's copy needs the existing parent's id to offer the way to change it."""
    client = _client()
    with pytest.raises(LithosToolError) as excinfo:
        await client.task_edge_upsert(
            from_task_id="solo",
            to_task_id="child-one",
            edge_type="parent_child",
            agent="dave",
        )
    assert "epic-one" in str(excinfo.value)


# ── events ──────────────────────────────────────────────────────────────


class _ObservingHub(FakeEventHub):
    """A hub that reads the store back at the moment an event is published.

    This is how "the write landed before it was announced" is testable: a
    publish-then-mutate fake would record the OLD status here, and the board a
    browser refetches on the event would still show the pre-write state.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.client: FakeLithosClient | None = None
        self.observed: list[tuple[str, str]] = []

    async def publish(self, event: LensEvent) -> None:
        if self.client is not None and event.task_id:
            task = await self.client.task_get(event.task_id)
            self.observed.append((event.type, task.status))
        await super().publish(event)


async def _drain(queue: asyncio.Queue[LensEvent], count: int) -> list[LensEvent]:
    return [await asyncio.wait_for(queue.get(), timeout=0.2) for _ in range(count)]


async def test_each_write_publishes_the_event_body_the_server_would() -> None:
    """Event NAMES are not enough: the bodies are asserted field for field.

    Each one is the upstream payload for that tool — ``task.completed`` carries
    the node-feedback arguments Lens never sends, ``task.cancelled`` is the one
    place the reason survives, ``task.reopened`` names the prior status and
    outcome its own return value just cleared, and ``task.created`` names no
    agent. A consumer written against fake mode has to see the real contract.
    """
    hub = _ObservingHub(EventsConfig(enabled=True), LithosConfig())
    await hub.start()
    client = FakeLithosClient(dataset=write_dataset(), events=hub)
    hub.client = client
    try:
        queue = hub.subscribe()

        completed = await client.task_complete(
            "gate-review", agent="dave", outcome="done"
        )
        reopened = await client.task_reopen("gate-review", agent="dave")
        cancelled = await client.task_cancel("pred", agent="dave", reason="superseded")
        created = await client.task_create(title="Fresh", agent="dave")
        events = await _drain(queue, 4)

        assert [event.type for event in events] == [
            "task.completed",
            "task.reopened",
            "task.cancelled",
            "task.created",
        ]
        assert events[0].payload == {
            "task_id": "gate-review",
            "agent": "dave",
            "outcome": "done",
            "updated_at": completed.updated_at,
            # The literal four-character string, not a JSON null: upstream puts
            # `json.dumps(value)` on the event and nothing decodes it again, so
            # this is what a consumer actually receives for an argument Lens
            # never sends. Asserted as the string so a fake that "tidied" it
            # into None fails here.
            "cited_nodes": "null",
            "misleading_nodes": "null",
            "receipt_id": "null",
        }
        assert all(
            isinstance(events[0].payload[key], str)
            for key in ("cited_nodes", "misleading_nodes", "receipt_id")
        )
        assert events[1].payload == {
            "task_id": "gate-review",
            "agent": "dave",
            "prior_status": "completed",
            "prior_outcome": "done",
            "updated_at": reopened.updated_at,
        }
        assert events[2].payload == {
            "task_id": "pred",
            "agent": "dave",
            "reason": "superseded",
            "updated_at": cancelled.updated_at,
        }
        # Not merely the same values the RESULTS carried: the stamps the store
        # kept. One write, one stamp — on the row and on the event alike.
        assert (
            events[2].payload["updated_at"]
            == (await client.task_get("pred")).resolved_at
        )
        assert events[1].payload["updated_at"] > events[0].payload["updated_at"]
        assert events[3].payload == {
            "task_id": created.task_id,
            "title": "Fresh",
            "updated_at": created.updated_at,
        }
        # Every one of them is a board-moving event, and every one of them was
        # announced only AFTER the store had changed.
        assert all(event.requires_refresh for event in events)
        assert hub.observed == [
            ("task.completed", "completed"),
            ("task.reopened", "open"),
            ("task.cancelled", "cancelled"),
            ("task.created", "open"),
        ]
    finally:
        await hub.stop()


async def test_an_edge_write_publishes_nothing() -> None:
    """Upstream emits no event for an edge write; only the hub mints the
    synthetic ``lens.edge_upserted`` (T3 D11), and that is W8's."""
    hub = FakeEventHub(EventsConfig(enabled=True), LithosConfig())
    await hub.start()
    client = FakeLithosClient(dataset=write_dataset(), events=hub)
    try:
        queue = hub.subscribe()
        await client.task_edge_upsert(
            from_task_id="pred", to_task_id="solo", edge_type="blocks", agent="dave"
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.05)
    finally:
        await hub.stop()


async def test_app_state_wires_the_fake_to_the_hub_the_process_publishes_through(
    lithos_lens_config_env: Path,
) -> None:
    """The PRODUCTION wiring, not a hand-injected hub.

    Every other event test here constructs the client with a hub, which would
    stay green if the fake-mode wiring were deleted. This one takes the client
    the way the app does — through AppState, which adopts the hub and hands it
    to the fake beside the graph cache — and writes through it.
    """
    config = load_config(lithos_lens_config_env)
    hub = FakeEventHub(config.events, config.lithos)
    state = AppState(config, FakeLithosClient(dataset=write_dataset()), events=hub)
    await state.events.start()
    try:
        client = state.lithos_client
        assert isinstance(client, FakeLithosClient)
        assert client.events is state.events, "AppState must wire the fake's hub"
        queue = state.events.subscribe()

        await client.task_complete("gate-review", agent="dave", outcome="done")

        event = await asyncio.wait_for(queue.get(), timeout=0.2)
        assert (event.type, event.task_id) == ("task.completed", "gate-review")
    finally:
        await state.events.stop()


def test_fake_mode_app_gives_its_fake_client_the_app_hub(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And the same wiring through the real entry point, in fake mode."""
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    state = app.state.lens
    assert isinstance(state.lithos_client, FakeLithosClient)
    assert state.lithos_client.events is state.events


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


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (
            lambda c: c.task_complete("nope-nope", agent="dave", outcome="done"),
            (
                "lithos_task_complete",
                {"task_id": "nope-nope", "agent": "dave", "outcome": "done"},
            ),
        ),
        (
            lambda c: c.task_cancel("nope-nope", agent="dave", reason="why"),
            (
                "lithos_task_cancel",
                {"task_id": "nope-nope", "agent": "dave", "reason": "why"},
            ),
        ),
        (
            lambda c: c.task_reopen("solo", agent="dave"),
            ("lithos_task_reopen", {"task_id": "solo", "agent": "dave"}),
        ),
        (
            lambda c: c.task_create(title="T", agent="dave", task_type="milestone"),
            (
                "lithos_task_create",
                {"title": "T", "agent": "dave", "task_type": "milestone"},
            ),
        ),
        (
            lambda c: c.task_edge_upsert(
                from_task_id="pred", to_task_id="pred", edge_type="blocks", agent="dave"
            ),
            (
                "lithos_task_edge_upsert",
                {
                    "from_task_id": "pred",
                    "to_task_id": "pred",
                    "type": "blocks",
                    "agent": "dave",
                },
            ),
        ),
    ],
    ids=["complete", "cancel", "reopen", "create", "edge_upsert"],
)
async def test_a_refused_write_is_logged_by_every_tool(
    call: Callable[[FakeLithosClient], Awaitable[Any]],
    expected: tuple[str, dict[str, Any]],
) -> None:
    """A refused write is still a call the server received, for all five.

    Each client method appends to the log independently, so this is
    parameterised rather than written once: moving any single append after its
    store call would drop that tool's refusals while every other assertion here
    stayed green. Every case is refused on its FIRST attempt, so the log must
    hold EXACTLY the refused call — a log that recorded successes only would be
    empty here, not merely short.
    """
    client = _client()

    with pytest.raises(LithosToolError):
        await call(client)

    assert client.write_calls == [expected]


async def test_two_fakes_over_one_seed_do_not_share_an_overlay() -> None:
    """The isolation the whole overlay design exists for: one test's write must
    not reach another's board, even when both were handed the same frozen
    dataset."""
    seed = write_dataset()
    one = FakeLithosClient(dataset=seed)
    two = FakeLithosClient(dataset=seed)

    await one.task_complete("gate-review", agent="dave", outcome="done")
    await one.task_create(title="Only in one", agent="dave")
    await one.task_edge_upsert(
        from_task_id="pred", to_task_id="solo", edge_type="blocks", agent="dave"
    )

    assert (await two.task_get("gate-review")).status == "open"
    assert await _ready_ids(two) == SEED_READY
    assert {task.id for task in await two.list_tasks()} == {
        task.id for task in seed.tasks
    }
    assert await _all_edges(two) == await _all_edges(FakeLithosClient(dataset=seed))
    assert two.write_calls == []
    # And the seed itself was never touched.
    assert seed.tasks == write_dataset().tasks


async def test_stats_coordination_counters_follow_the_writes() -> None:
    """``lithos_stats`` counts open tasks and live/expired claims afresh on
    every read upstream (``CoordinationService.get_stats``), so the fake's
    counters move with its writes: a resolve drops ``active_tasks`` and every
    claim it released (live or expired), a reopen or a create adds an open
    task, a reopen restores no claim — and the fixture's other statistics are
    left exactly as seeded."""
    seed_stats = {
        "active_tasks": 11,
        "open_claims": 1,
        "expired_claims": 1,
        "agents": 3,
        "documents": 128,
    }
    client = FakeLithosClient(
        dataset=replace(
            write_dataset(),
            claims={
                "pred": (
                    ClaimRecord(agent="a", aspect="impl", expires_at=LONG_AHEAD),
                    ClaimRecord(agent="b", aspect="review", expires_at=LONG_PAST),
                )
            },
            stats=seed_stats,
        )
    )
    assert await client.stats() == seed_stats

    def expected(**moved: int) -> dict[str, Any]:
        return {**seed_stats, **moved}

    await client.task_complete("pred", agent="dave")
    assert await client.stats() == expected(
        active_tasks=10, open_claims=0, expired_claims=0
    )
    await client.task_cancel("solo", agent="dave")
    assert await client.stats() == expected(
        active_tasks=9, open_claims=0, expired_claims=0
    )
    await client.task_reopen("pred", agent="dave")
    assert await client.stats() == expected(
        active_tasks=10, open_claims=0, expired_claims=0
    )
    await client.task_create(title="One", agent="dave")
    await client.task_create(title="Two", agent="dave")
    assert await client.stats() == expected(
        active_tasks=12, open_claims=0, expired_claims=0
    )


async def test_stats_leaves_a_counter_the_seed_omits_omitted() -> None:
    """A fixture that states no ``active_tasks`` gets none back after a write —
    the fake moves the counters a seed vouched for, it does not invent one."""
    live = ClaimRecord(agent="a", aspect="impl", expires_at=LONG_AHEAD)
    client = FakeLithosClient(
        dataset=replace(
            write_dataset(),
            claims={"pred": (live, live)},
            stats={"open_claims": 2, "agents": 3},
        )
    )
    await client.task_complete("gate-review", agent="dave")
    await client.task_complete("pred", agent="dave")
    assert await client.stats() == {"open_claims": 0, "agents": 3}


# ── minted tasks are tasks like any other ─────────────────────────────


async def test_a_minted_task_takes_every_later_write_like_a_seed_row() -> None:
    """A task this fake minted must answer later writes and reads like one it
    was seeded with: completed once it is completed, refused a second
    completion or a cancel, reopenable, and cancellable once reopened.

    Minted rows used to be appended to the effective store RAW, so a write to
    one landed in the overlay and no read ever applied it: the minted task
    read `open` forever, a reopen answered `task_not_resolved`, and a second
    completion succeeded.
    """
    client = _client()
    minted = await client.task_create(title="Minted", agent="dave")

    completion = await client.task_complete(
        minted.task_id, agent="dave", outcome="shipped"
    )

    done = await client.task_get(minted.task_id)
    assert done.status == "completed"
    assert done.outcome == "shipped"
    assert done.resolved_at == completion.updated_at
    assert minted.task_id not in await _ready_ids(client)
    for again in (client.task_complete, client.task_cancel):
        with pytest.raises(LithosToolError) as excinfo:
            await again(minted.task_id, agent="dave")
        assert excinfo.value.code == "task_not_found"

    await client.task_reopen(minted.task_id, agent="dave")
    reopened = await client.task_get(minted.task_id)
    assert (reopened.status, reopened.outcome, reopened.resolved_at) == (
        "open",
        "",
        "",
    )
    assert minted.task_id in await _ready_ids(client)

    cancelled = await client.task_cancel(minted.task_id, agent="dave")
    assert (await client.task_get(minted.task_id)).status == "cancelled"
    assert (await client.task_get(minted.task_id)).resolved_at == cancelled.updated_at
    with pytest.raises(LithosToolError) as excinfo:
        await client.task_cancel(minted.task_id, agent="dave")
    assert excinfo.value.code == "task_not_found"


async def test_completing_a_minted_predecessor_unblocks_its_minted_dependent() -> None:
    """The oracle must see a minted predecessor's status change too, or a
    dependent created against it could never be released."""
    client = _client()
    first = await client.task_create(title="First", agent="dave")
    second = await client.task_create(
        title="Second", agent="dave", depends_on=[first.task_id]
    )
    assert second.task_id in await _blocked(client)

    completion = await client.task_complete(first.task_id, agent="dave")

    assert completion.unblocked == (second.task_id,)
    assert second.task_id in await _ready_ids(client)
    assert second.task_id not in await _blocked(client)
    reopen = await client.task_reopen(first.task_id, agent="dave")
    assert reopen.reblocked == (second.task_id,)


# ── upstream's validation order ────────────────────────────────────────

#: A full-length (36-character) id no fixture holds: the resolver hands it
#: straight through to the calling tool's own existence check.
UNKNOWN_FULL_ID = "3f2b8e1c-9d4a-4c6e-8b7f-2a1d5e9c0b44"


@pytest.mark.parametrize(
    ("label", "arguments", "code", "message"),
    [
        # Both endpoints are resolved before the type or self-edge checks run
        # (probed: upstream answers these two with the resolver's refusal).
        (
            "a too-short self edge",
            {"from_task_id": "x", "to_task_id": "x", "edge_type": "blocks"},
            "invalid_input",
            "from_task_id 'x' is too short: pass the full task id or a prefix of "
            "at least 6 characters.",
        ),
        (
            "an unknown type between too-short ids",
            {"from_task_id": "x", "to_task_id": "y", "edge_type": "duplicates"},
            "invalid_input",
            "from_task_id 'x' is too short: pass the full task id or a prefix of "
            "at least 6 characters.",
        ),
        (
            "a good source and a too-short target",
            {"from_task_id": "pred", "to_task_id": "y", "edge_type": "duplicates"},
            "invalid_input",
            "to_task_id 'y' is too short: pass the full task id or a prefix of "
            "at least 6 characters.",
        ),
        # Then the type, before the self-edge check …
        (
            "an unknown type on a self edge",
            {"from_task_id": "pred", "to_task_id": "pred", "edge_type": "duplicates"},
            "invalid_edge_type",
            "edge type 'duplicates' is not accepted in this phase (accepted: "
            "['blocks', 'discovered_from', 'parent_child', 'waits_on_gate']).",
        ),
        # … and the self-edge check before existence.
        (
            "a self edge on a task that does not exist",
            {
                "from_task_id": UNKNOWN_FULL_ID,
                "to_task_id": UNKNOWN_FULL_ID,
                "edge_type": "blocks",
            },
            "self_edge",
            "An edge cannot connect a task to itself.",
        ),
        (
            "an unknown type between tasks that do not exist",
            {
                "from_task_id": UNKNOWN_FULL_ID,
                "to_task_id": "pred",
                "edge_type": "duplicates",
            },
            "invalid_edge_type",
            "edge type 'duplicates' is not accepted in this phase (accepted: "
            "['blocks', 'discovered_from', 'parent_child', 'waits_on_gate']).",
        ),
    ],
)
async def test_an_edge_write_validates_in_upstreams_order(
    label: str, arguments: dict[str, Any], code: str, message: str
) -> None:
    client = _client()
    before = await _all_edges(client)

    with pytest.raises(LithosToolError) as excinfo:
        await client.task_edge_upsert(agent="dave", **arguments)

    assert (excinfo.value.code, str(excinfo.value)) == (code, message), label
    assert await _all_edges(client) == before


@pytest.mark.parametrize(
    ("label", "arguments", "code", "message"),
    [
        (
            "the parent is resolved before the predecessors",
            {"parent_task_id": "x", "depends_on": ["zqxjqw"]},
            "invalid_input",
            "parent_task_id 'x' is too short: pass the full task id or a prefix "
            "of at least 6 characters.",
        ),
        (
            "ids are resolved before the metadata keys",
            {"depends_on": ["x"], "metadata": {"depends_on": ["pred"]}},
            "invalid_input",
            "depends_on 'x' is too short: pass the full task id or a prefix of "
            "at least 6 characters.",
        ),
        (
            "ids are resolved before the task type",
            {"depends_on": ["x"], "task_type": "milestone"},
            "invalid_input",
            "depends_on 'x' is too short: pass the full task id or a prefix of "
            "at least 6 characters.",
        ),
        (
            "the metadata keys come before the task type",
            {"task_type": "milestone", "metadata": {"depends_on": ["pred"]}},
            "invalid_metadata_key",
            "metadata key(s) ['depends_on'] are no longer accepted: task "
            "dependencies are first-class task edges. Use depends_on on "
            "lithos_task_create, or lithos_task_edge_upsert.",
        ),
        (
            "both forbidden keys, named in upstream's order",
            {"metadata": {"blocked_on": ["pred"], "depends_on": ["pred"]}},
            "invalid_metadata_key",
            "metadata key(s) ['depends_on', 'blocked_on'] are no longer accepted: "
            "task dependencies are first-class task edges. Use depends_on on "
            "lithos_task_create, or lithos_task_edge_upsert.",
        ),
        (
            "the gate rules come before existence",
            {"task_type": "gate", "depends_on": [UNKNOWN_FULL_ID]},
            "invalid_input",
            "a gate task requires metadata.gate_type in ['ci', 'external_task', "
            "'human', 'pr', 'timer'], got None.",
        ),
        (
            "missing predecessors are named together, once each, before the parent",
            {
                "depends_on": [UNKNOWN_FULL_ID, UNKNOWN_FULL_ID],
                "parent_task_id": UNKNOWN_FULL_ID[:-1] + "5",
            },
            "task_not_found",
            f"depends_on references nonexistent task(s): ['{UNKNOWN_FULL_ID}']",
        ),
        (
            "a missing parent",
            {"parent_task_id": UNKNOWN_FULL_ID},
            "task_not_found",
            f"parent_task_id references nonexistent task: {UNKNOWN_FULL_ID}",
        ),
    ],
)
async def test_create_validates_in_upstreams_order(
    label: str, arguments: dict[str, Any], code: str, message: str
) -> None:
    client = _client()
    before = {task.id for task in await client.list_tasks()}

    with pytest.raises(LithosToolError) as excinfo:
        await client.task_create(title="T", agent="dave", **arguments)

    assert (excinfo.value.code, str(excinfo.value)) == (code, message), label
    assert {task.id for task in await client.list_tasks()} == before


# ── a timer gate's ready_at, as upstream stores it ─────────────────────


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        # An offset, and a fraction that is truncated rather than rounded.
        ("2030-01-02T03:04:05.987654-05:00", "2030-01-02T08:04:05+00:00"),
        # A naive value is read as UTC.
        ("2030-01-02T03:04:05", "2030-01-02T03:04:05+00:00"),
        ("2030-01-02T03:04:05Z", "2030-01-02T03:04:05+00:00"),
    ],
)
async def test_a_timer_gates_ready_at_is_stored_in_utc_to_the_second(
    sent: str, stored: str
) -> None:
    """Upstream validates ``ready_at`` by parsing it and stores the parsed
    instant rewritten to UTC at second precision, so a read afterwards shows
    that — never the caller's spelling, which no real server would return."""
    client = _client()

    created = await client.task_create(
        title="Embargo",
        agent="dave",
        task_type="gate",
        metadata={"gate_type": "timer", "ready_at": sent, "note": "kept"},
    )

    gate = await client.task_get(created.task_id)
    assert gate.metadata == {"gate_type": "timer", "ready_at": stored, "note": "kept"}


# ── the vendored envelopes, verbatim ───────────────────────────────────


def influx_dataset(**statuses: str) -> FakeLithosDataset:
    """The ids the contracts' envelopes name, in the relations they need.

    ``influx-ingest-cutover`` blocks ``influx-backfill``, whose parent is
    ``influx-epic``; ``influx-gate-human`` is a human gate. ``statuses``
    overrides a task's status (underscores for hyphens).
    """

    def status(task_id: str) -> str:
        return statuses.get(task_id.replace("-", "_"), "open")

    return FakeLithosDataset(
        tasks=(
            replace(
                _gate("influx-gate-human", "human"), status=status("influx-gate-human")
            ),
            _task("influx-ingest-cutover", status=status("influx-ingest-cutover")),
            _task("influx-backfill", status=status("influx-backfill")),
            _task("influx-epic", task_type="epic", status=status("influx-epic")),
        ),
        ready_ids=frozenset({"influx-gate-human", "influx-ingest-cutover"}),
        blocked={"influx-backfill": (_task_blocker("influx-ingest-cutover"),)},
        edges=_edge_map(
            _edge("influx-ingest-cutover", "influx-backfill", "blocks"),
            _edge("influx-epic", "influx-backfill", "parent_child"),
        ),
        children={"influx-epic": ("influx-backfill",)},
    )


def _edge_call(
    from_task_id: str, to_task_id: str, edge_type: str = "blocks"
) -> Callable[[FakeLithosClient], Awaitable[Any]]:
    return lambda c: c.task_edge_upsert(
        from_task_id=from_task_id,
        to_task_id=to_task_id,
        edge_type=edge_type,
        agent="dave",
    )


def _create_call(**arguments: Any) -> Callable[[FakeLithosClient], Awaitable[Any]]:
    return lambda c: c.task_create(title="T", agent="dave", **arguments)


#: (tool, a phrase that picks ONE vendored envelope, the statuses the fixture
#: needs, the call that must raise it).
VERBATIM_ENVELOPES: list[
    tuple[str, str, dict[str, str], Callable[[FakeLithosClient], Awaitable[Any]]]
] = [
    (
        "lithos_task_complete",
        "not in an open state",
        {"influx_gate_human": "completed"},
        lambda c: c.task_complete("influx-gate-human", agent="dave"),
    ),
    (
        "lithos_task_complete",
        "is too short",
        {},
        lambda c: c.task_complete("infl", agent="dave"),
    ),
    (
        "lithos_task_cancel",
        "already closed",
        {"influx_backfill": "cancelled"},
        lambda c: c.task_cancel("influx-backfill", agent="dave"),
    ),
    (
        "lithos_task_cancel",
        "No task matches",
        {},
        lambda c: c.task_cancel("influx-nope", agent="dave"),
    ),
    (
        "lithos_task_reopen",
        "already open",
        {},
        lambda c: c.task_reopen("influx-gate-human", agent="dave"),
    ),
    (
        "lithos_task_reopen",
        "No task matches",
        {},
        lambda c: c.task_reopen("influx-nope", agent="dave"),
    ),
    (
        "lithos_task_create",
        "depends_on references nonexistent",
        {},
        _create_call(depends_on=[UNKNOWN_FULL_ID]),
    ),
    (
        "lithos_task_create",
        "parent_task_id references nonexistent",
        {},
        _create_call(parent_task_id=UNKNOWN_FULL_ID),
    ),
    (
        "lithos_task_create",
        "is not accepted in this phase",
        {},
        _create_call(task_type="milestone"),
    ),
    (
        "lithos_task_create",
        "['blocked_on']",
        {},
        _create_call(metadata={"blocked_on": ["influx-backfill"]}),
    ),
    (
        "lithos_task_create",
        "requires metadata.gate_type",
        {},
        _create_call(task_type="gate"),
    ),
    (
        "lithos_task_create",
        "got 'soon'",
        {},
        _create_call(
            task_type="gate", metadata={"gate_type": "timer", "ready_at": "soon"}
        ),
    ),
    (
        "lithos_task_create",
        "No task matches",
        {},
        _create_call(depends_on=["influx-nope"]),
    ),
    (
        "lithos_task_edge_upsert",
        "would create a dependency cycle",
        {},
        _edge_call("influx-backfill", "influx-ingest-cutover"),
    ),
    (
        "lithos_task_edge_upsert",
        "would create a hierarchy cycle",
        {},
        _edge_call("influx-backfill", "influx-epic", "parent_child"),
    ),
    (
        "lithos_task_edge_upsert",
        "at most one parent",
        {},
        _edge_call("influx-ingest-cutover", "influx-backfill", "parent_child"),
    ),
    (
        "lithos_task_edge_upsert",
        "requires the from_task",
        {},
        _edge_call("influx-ingest-cutover", "influx-backfill", "waits_on_gate"),
    ),
    (
        "lithos_task_edge_upsert",
        "cannot connect a task to itself",
        {},
        _edge_call("influx-backfill", "influx-backfill"),
    ),
    (
        "lithos_task_edge_upsert",
        "is not accepted in this phase",
        {},
        _edge_call("influx-ingest-cutover", "influx-backfill", "duplicates"),
    ),
    (
        "lithos_task_edge_upsert",
        "to_task_id 'infl' is too short",
        {},
        _edge_call("influx-ingest-cutover", "infl"),
    ),
    (
        "lithos_task_edge_upsert",
        "'influx-nope' (from_task_id)",
        {},
        _edge_call("influx-nope", "influx-backfill"),
    ),
]


@pytest.mark.parametrize(
    ("tool", "phrase", "statuses", "call"),
    VERBATIM_ENVELOPES,
    ids=[f"{tool}:{phrase}" for tool, phrase, _, _ in VERBATIM_ENVELOPES],
)
async def test_the_fake_raises_the_vendored_envelope_verbatim(
    tool: str,
    phrase: str,
    statuses: dict[str, str],
    call: Callable[[FakeLithosClient], Awaitable[Any]],
) -> None:
    """The fake's refusals ARE the contract's, field for field.

    Not "the same code": the whole envelope, message included, compared with
    the one vendored for that scenario. A fake that invented its own wording —
    one shared "not found or not open" for complete and cancel, a composed
    cycle or parent message — passed every code-only assertion and still
    handed the action slices text the server never sends.
    """
    (vendored,) = [
        envelope
        for envelope in load_contract(tool)["responses"]["errors"]
        if phrase in envelope["message"]
    ]
    client = FakeLithosClient(dataset=influx_dataset(**statuses))

    with pytest.raises(LithosToolError) as excinfo:
        await call(client)

    assert excinfo.value.envelope == vendored


# ── the frontiers hold workable tasks only ─────────────────────────────


async def test_gates_and_epics_never_reach_the_ready_frontier() -> None:
    """Upstream's ready frontier holds open ``task``-typed rows only — a gate is
    an external wait and an epic a roll-up — and the limit applies AFTER that
    filter, so neither can take a workable task's slot."""
    client = FakeLithosClient(dataset=FakeLithosDataset())
    gate = await client.task_create(
        title="Gate", agent="dave", task_type="gate", metadata={"gate_type": "human"}
    )
    epic = await client.task_create(title="Epic", agent="dave", task_type="epic")
    work = await client.task_create(title="Work", agent="dave")

    assert {task.id for task in await client.task_ready()} == {work.task_id}
    assert [task.id for task in await client.task_ready(limit=1)] == [work.task_id]
    assert gate.task_id not in await _blocked(client)
    assert epic.task_id not in await _blocked(client)


async def test_gates_and_epics_never_reach_the_blocked_frontier() -> None:
    """Nor the blocked one, whatever unsatisfied edge leads into them."""
    client = _client()
    gate = await client.task_create(
        title="Gate",
        agent="dave",
        task_type="gate",
        metadata={"gate_type": "human"},
        depends_on=["pred"],
    )
    epic = await client.task_create(
        title="Epic", agent="dave", task_type="epic", depends_on=["pred"]
    )
    work = await client.task_create(title="Work", agent="dave", depends_on=["pred"])

    blocked = await _blocked(client)
    assert work.task_id in blocked
    assert gate.task_id not in blocked
    assert epic.task_id not in blocked
    assert {gate.task_id, epic.task_id}.isdisjoint(await _ready_ids(client))


async def test_a_reopened_gate_stays_off_the_ready_frontier() -> None:
    """The demo's human gate is off the ready frontier before any write; a
    complete-then-reopen must leave it there, not promote it."""
    client = FakeLithosClient()
    assert "influx-read-swap-approval" not in await _ready_ids(client)

    await client.task_complete("influx-read-swap-approval", agent="dave")
    await client.task_reopen("influx-read-swap-approval", agent="dave")

    assert "influx-read-swap-approval" not in await _ready_ids(client)
    assert "influx-read-swap-approval" not in await _blocked(client)


# ── a timer gate lapses on the clock, not on a write ───────────────────


def timer_dataset() -> FakeLithosDataset:
    """One timer gate, one waiter on it, as a seed states them before the
    timer's ``ready_at``."""
    return FakeLithosDataset(
        tasks=(
            _gate("timer-x", "timer", ready_at="2030-01-02T00:00:00+00:00"),
            _task("waits-timer"),
        ),
        ready_ids=frozenset(),
        blocked={
            "waits-timer": (
                BlockerRecord(
                    kind="gate",
                    task_id="timer-x",
                    type="waits_on_gate",
                    status="open",
                    message=(
                        "Waiting on timer gate timer-x "
                        "(ready_at=2030-01-02T00:00:00+00:00)."
                    ),
                ),
            )
        },
        edges=_edge_map(_edge("timer-x", "waits-timer", "waits_on_gate")),
    )


async def test_a_seeded_timer_stops_blocking_when_its_ready_at_passes() -> None:
    """Upstream evaluates a timer gate on every read, so its waiter is released
    the moment ``ready_at`` passes — no write involved. The fake used to keep
    the seed's verdict until something wrote near the waiter."""
    now = [datetime(2030, 1, 1, tzinfo=UTC)]
    client = FakeLithosClient(dataset=timer_dataset(), clock=lambda: now[0])
    assert "waits-timer" not in await _ready_ids(client)
    assert "waits-timer" in await _blocked(client)

    now[0] = datetime(2030, 1, 3, tzinfo=UTC)

    assert "waits-timer" in await _ready_ids(client)
    assert "waits-timer" not in await _blocked(client)

    # Completing the lapsed gate reports the waiter, because upstream's
    # `newly_unblocked_by` reports every dependent that is ready NOW — not a
    # before/after difference (lithos a4d2d62 coordination.py).
    completion = await client.task_complete("timer-x", agent="dave")
    assert completion.unblocked == ("waits-timer",)
    assert "waits-timer" in await _ready_ids(client)


# ── longer cycles, through earlier writes ──────────────────────────────


@pytest.mark.parametrize(
    ("label", "earlier", "closing", "message"),
    [
        (
            "a three-task dependency loop through an inserted edge",
            ("dep", "solo", "blocks"),
            ("solo", "pred", "blocks"),
            "blocks edge solo -> pred would create a dependency cycle: "
            "solo -> dep -> pred -> solo",
        ),
        (
            "a dependency loop that runs through a gate edge",
            ("waiter-one", "solo", "blocks"),
            ("solo", "gate-review", "blocks"),
            "blocks edge solo -> gate-review would create a dependency cycle: "
            "solo -> waiter-one -> gate-review -> solo",
        ),
        (
            "a three-level hierarchy that would contain itself",
            ("child-one", "solo", "parent_child"),
            ("solo", "epic-one", "parent_child"),
            "parent_child edge solo -> epic-one would create a hierarchy cycle: "
            "epic-one -> child-one -> solo -> epic-one",
        ),
    ],
)
async def test_a_cycle_through_several_tasks_and_an_earlier_write_is_refused(
    label: str,
    earlier: tuple[str, str, str],
    closing: tuple[str, str, str],
    message: str,
) -> None:
    """The walk must cross intermediate tasks and edges an earlier write
    inserted — a check of only the reversed edge would accept every one of
    these — and render the path the way upstream does."""
    client = _client()
    await client.task_edge_upsert(
        from_task_id=earlier[0],
        to_task_id=earlier[1],
        edge_type=earlier[2],
        agent="dave",
    )
    before = await _all_edges(client)
    children_before = {
        task.id: [child.id for child in await client.task_children(task.id)]
        for task in write_dataset().tasks
    }

    with pytest.raises(LithosToolError) as excinfo:
        await client.task_edge_upsert(
            from_task_id=closing[0],
            to_task_id=closing[1],
            edge_type=closing[2],
            agent="dave",
        )

    assert (excinfo.value.code, str(excinfo.value)) == ("cycle", message), label
    assert await _all_edges(client) == before
    assert {
        task.id: [child.id for child in await client.task_children(task.id)]
        for task in write_dataset().tasks
    } == children_before


# ── no second terminal write, whichever came first ─────────────────────


@pytest.mark.parametrize("first", ["complete", "cancel"])
@pytest.mark.parametrize("second", ["complete", "cancel"])
@pytest.mark.parametrize("minted", [False, True], ids=["seeded", "minted"])
async def test_a_resolved_task_refuses_every_terminal_write(
    first: str, second: str, minted: bool
) -> None:
    """Complete and cancel apply to an OPEN task only, so once either has
    resolved a task — seeded or minted — both answer ``task_not_found`` and
    the task stays exactly as the first write left it."""
    client = _client()
    task_id = (
        (await client.task_create(title="Minted", agent="dave")).task_id
        if minted
        else "pred"
    )
    writes = {
        "complete": lambda: client.task_complete(task_id, agent="dave", outcome="x"),
        "cancel": lambda: client.task_cancel(task_id, agent="dave"),
    }
    await writes[first]()
    resolved = await client.task_get(task_id)

    with pytest.raises(LithosToolError) as excinfo:
        await writes[second]()

    assert excinfo.value.code == "task_not_found"
    after = await client.task_get(task_id)
    assert (after.status, after.outcome, after.resolved_at) == (
        resolved.status,
        resolved.outcome,
        resolved.resolved_at,
    )


# ── edge metadata is per instance too ──────────────────────────────────


async def test_a_metadata_re_upsert_does_not_reach_another_fake() -> None:
    """The edge-metadata overlay is the one a re-upsert writes, so isolation
    has to hold for it too — checked against the seed's own literal value,
    not against another fake built after the write."""
    seed = write_dataset()
    one = FakeLithosClient(dataset=seed)
    two = FakeLithosClient(dataset=seed)

    await one.task_edge_upsert(
        from_task_id="epic-one",
        to_task_id="child-one",
        edge_type="parent_child",
        agent="dave",
        metadata={"added_via": "lithos-lens"},
    )

    def parent_metadata(edges: list[EdgeRecord]) -> list[dict[str, Any]]:
        return [edge.metadata for edge in edges if edge.type == "parent_child"]

    assert parent_metadata(await one.task_edge_list("child-one")) == [
        {"added_via": "lithos-lens"}
    ]
    for client in (two, FakeLithosClient(dataset=seed)):
        for endpoint in ("epic-one", "child-one"):
            assert parent_metadata(await client.task_edge_list(endpoint)) == [
                {"note": "seed"}
            ]
    assert [
        edge.metadata for edge in seed.edges["child-one"] if edge.type == "parent_child"
    ] == [{"note": "seed"}]


async def test_reblocked_is_upstreams_rule_not_a_readiness_difference() -> None:
    """``newly_reblocked_by`` reports each OPEN dependent whose blockers are now
    the reopened task alone — it does not ask whether the dependent was ready
    before. A gate waiting on a predecessor is never ready (gates are off the
    frontier), yet reopening the completed predecessor re-blocks it upstream,
    so the fake reports it too."""
    client = _client()
    gate = await client.task_create(
        title="Gate",
        agent="dave",
        task_type="gate",
        metadata={"gate_type": "human"},
        depends_on=["solo"],
    )
    completion = await client.task_complete("solo", agent="dave")
    # Not unblocked: a gate is never ready, so it is never reported ready.
    assert completion.unblocked == ()

    reopen = await client.task_reopen("solo", agent="dave")

    assert reopen.reblocked == (gate.task_id,)


# ── a write never reuses the stamp it replaces ─────────────────────────


@pytest.mark.parametrize(
    "clock_after_create",
    [
        # The wall clock repeats …
        datetime(2030, 1, 1, tzinfo=UTC),
        # … or runs backward.
        datetime(2029, 6, 1, tzinfo=UTC),
    ],
    ids=["repeated-clock", "backward-clock"],
)
async def test_each_terminal_write_commits_a_strictly_later_stamp(
    clock_after_create: datetime,
) -> None:
    """Upstream's ``_advance_stamp`` (lithos a4d2d62): complete, cancel and
    reopen commit ``max(now, prior + 1µs)``, so a create -> complete -> reopen
    -> cancel run never reuses a stamp, however the clock moves — and the
    result, the event and the stored ``resolved_at`` of each write agree."""
    now = [datetime(2030, 1, 1, tzinfo=UTC)]
    hub = FakeEventHub(EventsConfig(enabled=True), LithosConfig())
    await hub.start()
    client = FakeLithosClient(dataset=write_dataset(), events=hub, clock=lambda: now[0])
    try:
        queue = hub.subscribe()
        created = await client.task_create(title="Minted", agent="dave")
        now[0] = clock_after_create

        completed = await client.task_complete(created.task_id, agent="dave")
        assert (await client.task_get(created.task_id)).resolved_at == (
            completed.updated_at
        )
        reopened = await client.task_reopen(created.task_id, agent="dave")
        cancelled = await client.task_cancel(created.task_id, agent="dave")
        assert (await client.task_get(created.task_id)).resolved_at == (
            cancelled.updated_at
        )
        events = await _drain(queue, 4)
    finally:
        await client.close()
        await hub.stop()

    stamps = [
        created.updated_at,
        completed.updated_at,
        reopened.updated_at,
        cancelled.updated_at,
    ]
    assert stamps == [
        "2030-01-01T00:00:00+00:00",
        "2030-01-01T00:00:00.000001+00:00",
        "2030-01-01T00:00:00.000002+00:00",
        "2030-01-01T00:00:00.000003+00:00",
    ]
    assert [event.payload["updated_at"] for event in events] == stamps


# ── a blocker the seed states without an edge ──────────────────────────


def seed_only_dataset() -> FakeLithosDataset:
    """Blockers the ``blocked`` oracle states with NO edge behind them — a
    fixture may, and the oracle documents honouring them: a predecessor, and a
    timer gate due 2030-01-02."""
    return FakeLithosDataset(
        tasks=(
            _task("lone-pred"),
            _task("lone-waiter"),
            _gate("lone-timer", "timer", ready_at="2030-01-02T00:00:00+00:00"),
            _task("timer-waiter"),
        ),
        ready_ids=frozenset({"lone-pred"}),
        blocked={
            "lone-waiter": (_task_blocker("lone-pred"),),
            "timer-waiter": (
                BlockerRecord(
                    kind="gate",
                    task_id="lone-timer",
                    type="waits_on_gate",
                    status="open",
                    message=(
                        "Waiting on timer gate lone-timer "
                        "(ready_at=2030-01-02T00:00:00+00:00)."
                    ),
                ),
            ),
        },
    )


async def test_completing_a_seed_only_blocker_releases_its_waiter() -> None:
    client = FakeLithosClient(dataset=seed_only_dataset())

    completion = await client.task_complete("lone-pred", agent="dave")

    assert completion.unblocked == ("lone-waiter",)
    assert "lone-waiter" in await _ready_ids(client)
    assert "lone-waiter" not in await _blocked(client)

    reopen = await client.task_reopen("lone-pred", agent="dave")
    assert reopen.reblocked == ("lone-waiter",)
    assert [row.task_id for row in (await _blocked(client))["lone-waiter"]] == [
        "lone-pred"
    ]


async def test_cancelling_a_seed_only_blocker_strands_its_waiter() -> None:
    client = FakeLithosClient(dataset=seed_only_dataset())

    await client.task_cancel("lone-pred", agent="dave")

    (blocker,) = (await _blocked(client))["lone-waiter"]
    assert (blocker.kind, blocker.status) == ("blocker_unsatisfiable", "cancelled")


async def test_a_seed_only_timer_blocker_lapses_on_the_clock() -> None:
    now = [datetime(2030, 1, 1, tzinfo=UTC)]
    client = FakeLithosClient(dataset=seed_only_dataset(), clock=lambda: now[0])
    assert "timer-waiter" in await _blocked(client)

    now[0] = datetime(2030, 1, 3, tzinfo=UTC)

    assert "timer-waiter" in await _ready_ids(client)
    assert "timer-waiter" not in await _blocked(client)


# ── minted ids are new ids ──────────────────────────────────────────────


async def test_create_never_mints_an_id_the_store_already_holds() -> None:
    """Upstream mints a uuid4. The fake's per-instance sequence must therefore
    skip any value a seed already holds — one created by an earlier fake, say —
    rather than shadow that row."""
    client = FakeLithosClient(
        dataset=FakeLithosDataset(
            tasks=(_task("fake-created-1", title="Seed"),),
            ready_ids=frozenset({"fake-created-1"}),
        )
    )

    minted = await client.task_create(title="Minted", agent="dave")

    assert minted.task_id != "fake-created-1"
    assert (await client.task_get("fake-created-1")).title == "Seed"
    assert (await client.task_get(minted.task_id)).title == "Minted"
    ids = [task.id for task in await client.list_tasks()]
    assert sorted(ids) == sorted({"fake-created-1", minted.task_id})

    await client.task_complete(minted.task_id, agent="dave")
    assert (await client.task_get(minted.task_id)).status == "completed"
    assert (await client.task_get("fake-created-1")).status == "open"


# ── a seed-only blocker that was cancelled, reopened ───────────────────


@pytest.mark.parametrize(
    ("blocker", "edge_type", "waiting"),
    [
        (
            _task("lone-pred", status="cancelled"),
            "blocks",
            BlockerRecord(
                kind="task",
                task_id="lone-pred",
                type="blocks",
                status="open",
                message="Waiting on predecessor lone-pred to complete.",
            ),
        ),
        (
            replace(_gate("lone-gate", "human"), status="cancelled"),
            "waits_on_gate",
            BlockerRecord(
                kind="gate",
                task_id="lone-gate",
                type="waits_on_gate",
                status="open",
                message="Waiting on human gate lone-gate.",
            ),
        ),
    ],
    ids=["predecessor", "gate"],
)
async def test_reopening_a_cancelled_seed_only_blocker_un_strands_its_waiter(
    blocker: TaskRecord, edge_type: str, waiting: BlockerRecord
) -> None:
    """A seed that states a waiter stranded by a cancelled blocker — with no
    edge behind it — must read as an ordinary wait once that blocker is
    reopened, exactly as an edge-backed one does; and a cancelled reopen still
    re-blocks nobody."""
    stranded = BlockerRecord(
        kind="blocker_unsatisfiable",
        task_id=blocker.id,
        type=edge_type,
        status="cancelled",
        message="stranded",
    )
    client = FakeLithosClient(
        dataset=FakeLithosDataset(
            tasks=(blocker, _task("lone-waiter")),
            blocked={"lone-waiter": (stranded,)},
        )
    )
    assert (await _blocked(client))["lone-waiter"] == (stranded,)

    reopen = await client.task_reopen(blocker.id, agent="dave")

    assert reopen.reblocked == ()
    assert (await _blocked(client))["lone-waiter"] == (waiting,)


# ── a timer read backward across its ready_at ──────────────────────────


async def test_a_lapsed_timer_blocks_again_when_the_clock_goes_back() -> None:
    """Upstream compares ``ready_at <= now`` on every read, in whichever
    direction the clock has moved. A seed captured after the timer lapsed (its
    waiter ready) must block that waiter again once the clock is before
    ``ready_at`` — with no write in between."""
    now = [datetime(2030, 1, 3, tzinfo=UTC)]
    client = FakeLithosClient(
        dataset=FakeLithosDataset(
            tasks=(
                _gate("timer-x", "timer", ready_at="2030-01-02T00:00:00+00:00"),
                _task("waits-timer"),
            ),
            ready_ids=frozenset({"waits-timer"}),
            edges=_edge_map(_edge("timer-x", "waits-timer", "waits_on_gate")),
        ),
        clock=lambda: now[0],
    )
    assert "waits-timer" in await _ready_ids(client)

    now[0] = datetime(2030, 1, 1, tzinfo=UTC)

    assert "waits-timer" not in await _ready_ids(client)
    assert (await _blocked(client))["waits-timer"] == (
        BlockerRecord(
            kind="gate",
            task_id="timer-x",
            type="waits_on_gate",
            status="open",
            message=(
                "Waiting on timer gate timer-x (ready_at=2030-01-02T00:00:00+00:00)."
            ),
        ),
    )


# ── a stamp under an ordinary, forward-moving clock ────────────────────


async def test_a_terminal_write_under_a_later_clock_commits_the_clock() -> None:
    """The other half of ``_advance_stamp``: when the clock is past the prior
    stamp, the clock IS the stamp — so a seeded task created long ago is
    resolved NOW, and a recent-work read finds it."""
    now = [datetime(2030, 1, 1, tzinfo=UTC)]
    hub = FakeEventHub(EventsConfig(enabled=True), LithosConfig())
    await hub.start()
    client = FakeLithosClient(dataset=write_dataset(), events=hub, clock=lambda: now[0])
    try:
        queue = hub.subscribe()
        completed = await client.task_complete("pred", agent="dave")
        now[0] = datetime(2030, 1, 2, 12, 30, tzinfo=UTC)
        cancelled = await client.task_cancel("solo", agent="dave")
        now[0] = datetime(2030, 1, 3, tzinfo=UTC)
        minted = await client.task_create(title="Minted", agent="dave")
        now[0] = datetime(2030, 1, 4, tzinfo=UTC)
        minted_done = await client.task_complete(minted.task_id, agent="dave")
        events = await _drain(queue, 4)
    finally:
        await client.close()
        await hub.stop()

    assert completed.updated_at == "2030-01-01T00:00:00+00:00"
    assert cancelled.updated_at == "2030-01-02T12:30:00+00:00"
    assert minted.updated_at == "2030-01-03T00:00:00+00:00"
    assert minted_done.updated_at == "2030-01-04T00:00:00+00:00"
    assert [event.payload["updated_at"] for event in events] == [
        "2030-01-01T00:00:00+00:00",
        "2030-01-02T12:30:00+00:00",
        "2030-01-03T00:00:00+00:00",
        "2030-01-04T00:00:00+00:00",
    ]
    assert (await client.task_get("pred")).resolved_at == completed.updated_at
    assert (await client.task_get("solo")).resolved_at == cancelled.updated_at
    window = "2029-12-31T00:00:00+00:00"
    assert "pred" in {
        task.id
        for task in await client.list_tasks(status="completed", resolved_since=window)
    }
    assert "solo" in {
        task.id
        for task in await client.list_tasks(status="cancelled", resolved_since=window)
    }


# ── a recomputed row keeps the seed's blocker order ────────────────────


async def test_a_recomputed_blocked_row_keeps_the_seeds_blocker_order() -> None:
    """A row is recomputed whenever anything — a write, or an open timer gate
    — can move it, and recomputation must not reshuffle what the seed states:
    the demo's ``influx-backfill`` waits on a pending timer, so it is always
    recomputed, yet its chips must read in the fixture's order (the board's
    first chip is the cutover predecessor). Nothing has moved, so the row is
    the seed's, record for record."""
    client = FakeLithosClient()

    (row,) = [r for r in await client.task_blocked() if r.task.id == "influx-backfill"]

    assert row.blockers == demo_dataset().blocked["influx-backfill"]
