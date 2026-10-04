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
from pathlib import Path
from typing import Any

import pytest

from lithos_lens.config import EventsConfig, LithosConfig, load_config
from lithos_lens.events import LensEvent
from lithos_lens.fake_dataset import FakeLithosDataset
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
        ready_ids=frozenset(
            {
                "gate-review",
                "timer-elapsed",
                "timer-pending",
                "pred",
                "solo",
                "epic-one",
                "child-one",
            }
        ),
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


SEED_READY = {
    "gate-review",
    "timer-elapsed",
    "timer-pending",
    "pred",
    "solo",
    "epic-one",
    "child-one",
}
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

    # A reopen CLEARS resolved_at, so the stamp it answers is the one it wrote
    # on the `[Reopened]` finding — one stamp per write, not two clock reads.
    (finding,) = await client.list_findings("gate-review")
    assert reopen == TaskReopenResult(
        success=True,
        task_id="gate-review",
        title="Gate review",
        updated_at=finding.created_at,
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
    assert str(passed_through.value) == f"Task '{full_length}' not found."


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
        # kept. One write, one stamp — on the row, on the finding it posted,
        # and on the event alike.
        assert (
            events[2].payload["updated_at"]
            == (await client.task_get("pred")).resolved_at
        )
        (reopen_finding,) = await client.list_findings("gate-review")
        assert events[1].payload["updated_at"] == reopen_finding.created_at
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
