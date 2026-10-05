"""T3-W5 — Reopen: completed and cancelled tasks, worded differently, and the
Reopen gate follow-up on a completion's receipt.

Asserted the way the PRD's Testing Decisions ask: on what a request returns and
on what the fake recorded, never on internals. The groups are the slice's
acceptance list:

- a COMPLETED task: the receipt names the re-blocked dependents, and the board
  read afterwards shows them blocked again;
- a CANCELLED predecessor: the receipt names the dependents now waiting, says
  nothing is re-blocked, and their blocker is no longer unsatisfiable;
- the two inline copies beside the button differ, each matching its case;
- an already-open task is the conflict page on both of its paths;
- Reopen gate on the completion receipt, and the round trip that leaves the
  board as it started;
- one audit line and one span per attempt.

Two datasets: W4's test-local one (``gate-human`` → ``waiter-a``,
``waiter-b``), every timestamp derived from now, for the completed case and the
round trip; and the demo dataset's ``loom-cancelled-pred`` →
``loom-blocked-forever`` for the cancelled case (clarification 8).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from lithos_lens.config import load_config
from lithos_lens.fake_dataset import FakeLithosDataset, demo_dataset
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.lithos_client import LithosToolError
from lithos_lens.operator import OPERATOR_COOKIE_NAME
from lithos_lens.task_graph import EdgeRecord
from lithos_lens.task_links import LINK_PAGE_SIZE
from lithos_lens.tasks import TaskRecord
from lithos_lens.web import create_app
from lithos_lens.write_funnel import AUDIT_EVENT
from lithos_lens.write_routes import offers_reopen
from tests.conftest import metric_value
from tests.test_complete_gate import LoggedFake

ORIGIN = "http://lens.test"
SAME_ORIGIN = {"Origin": ORIGIN}
HTMX = {**SAME_ORIGIN, "HX-Request": "true"}
OPERATOR = "dave"
NOTHING_CHANGED = "Nothing was changed."
RECONCILE_EVENT = "lens:reconcile"

COMPLETED_COPY = (
    "Reopening puts this task back to open. Tasks that became ready when it "
    "completed will be blocked again."
)
CANCELLED_COPY = (
    "Reopening returns this task to open. Its dependents stop being permanently "
    "blocked and wait on it again."
)
OUTCOME_KEPT = "Lithos keeps it in the [Reopened] finding it records."
REOPEN_GATE_LIMITS = (
    "Re-blocks its waiters, but does not recall anything their agents started "
    "in between."
)

#: The demo dataset's stranded pair (``fake_graph_dataset``).
CANCELLED_PRED = "loom-cancelled-pred"
STRANDED = "loom-blocked-forever"
STRANDED_TITLE = "Migrate the legacy run archive"


@pytest.fixture
def config(lithos_lens_config_env: Path):
    return load_config(lithos_lens_config_env)


@pytest.fixture
def fake() -> LoggedFake:
    return LoggedFake()


def _client(config, fake: FakeLithosClient, *, operator: str = OPERATOR):
    app = create_app(config, lithos_client_factory=lambda _: fake)
    client = TestClient(app, base_url=ORIGIN)
    if operator:
        client.cookies.set(OPERATOR_COOKIE_NAME, operator)
    return client


@pytest.fixture
def client(config, fake: LoggedFake) -> Iterator[TestClient]:
    with _client(config, fake) as test_client:
        fake.method_calls.clear()
        fake.write_calls.clear()
        yield test_client


@pytest.fixture
def demo_fake() -> LoggedFake:
    return LoggedFake(demo_dataset())


@pytest.fixture
def demo_client(config, demo_fake: LoggedFake) -> Iterator[TestClient]:
    with _client(config, demo_fake) as test_client:
        demo_fake.method_calls.clear()
        demo_fake.write_calls.clear()
        yield test_client


def _reopen(
    client: TestClient,
    task_id: str,
    *,
    expected_status: str,
    next_url: str = "",
    headers: dict[str, str] | None = None,
):
    data = {"expected_status": expected_status}
    if next_url:
        data["next"] = next_url
    return client.post(
        f"/tasks/{task_id}/reopen",
        data=data,
        headers=SAME_ORIGIN if headers is None else headers,
        follow_redirects=False,
    )


def _text(html: str) -> str:
    """Visible text, entity-resolved and whitespace-folded."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def _form(html: str, hook: str, task_id: str) -> str:
    """The one form carrying ``hook`` for ``task_id``, open tag to close."""
    found = re.findall(
        rf'<form [^>]*{hook}[^>]*data-task-id="{re.escape(task_id)}".*?</form>',
        html,
        re.S,
    )
    assert len(found) == 1, f"expected one {hook} form for {task_id}"
    return found[0]


def _hidden(form: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', form)
    assert match is not None
    return unescape(match.group(1))


def _receipt(html: str) -> str:
    start = html.index('<section class="write-receipt"')
    return html[start : html.index("</section>", start)]


def _named(receipt: str) -> list[str]:
    return re.findall(r'data-receipt-released-task="([^"]+)"', receipt)


def _sections(board: str) -> dict[str, set[str]]:
    """Each open-board section's task ids — the board's verdict on every task."""
    found: dict[str, set[str]] = {}
    for section in ("attention", "gates", "ready", "blocked"):
        start = board.find(f'data-task-group="{section}"')
        if start == -1:
            continue
        end = board.find('<article class="task-group"', start)
        markup = board[start : end if end != -1 else len(board)]
        found[section] = set(
            re.findall(r'data-task-row[^>]*data-task-id="([^"]+)"', markup)
        ) | set(re.findall(r'data-gate-row[^>]*data-task-id="([^"]+)"', markup))
    return found


def _blocker_kinds(fake: FakeLithosClient, task_id: str) -> set[str]:
    rows = asyncio.run(fake.task_blocked())
    return {
        blocker.kind
        for row in rows
        if row.task.id == task_id
        for blocker in row.blockers
    }


# ── where Reopen is offered ──────────────────────────────────────────────


def test_the_helper_offers_reopen_on_completed_and_cancelled_tasks_only() -> None:
    for task_type in ("task", "epic", "gate"):
        task = TaskRecord(id="t", title="T", task_type=task_type)
        assert offers_reopen(replace(task, status="completed"))
        assert offers_reopen(replace(task, status="cancelled"))
        assert not offers_reopen(replace(task, status="open"))
    assert offers_reopen(None) is False


def test_the_two_inline_copies_differ_and_each_matches_its_case(
    demo_client: TestClient,
) -> None:
    """Clarification 1: no confirm page — "before the write" is the copy
    rendered beside the Reopen button, worded by the status the page read."""
    completed = _form(
        demo_client.get("/tasks/loom-design-done").text,
        "data-reopen-action",
        "loom-design-done",
    )
    cancelled = _form(
        demo_client.get(f"/tasks/{CANCELLED_PRED}").text,
        "data-reopen-action",
        CANCELLED_PRED,
    )

    assert COMPLETED_COPY in _text(completed)
    assert CANCELLED_COPY not in _text(completed)
    assert CANCELLED_COPY in _text(cancelled)
    assert COMPLETED_COPY not in _text(cancelled)
    for form, status in ((completed, "completed"), (cancelled, "cancelled")):
        assert ">Reopen</button>" in form
        assert _hidden(form, "expected_status") == status
        assert "data-operator-id>dave<" in form
        # Both carry an outcome, so both say where Lithos keeps it.
        assert OUTCOME_KEPT in _text(form)


def test_without_an_outcome_the_copy_says_nothing_about_one(
    client: TestClient, fake: LoggedFake
) -> None:
    asyncio.run(fake.task_cancel("plain", agent="agent-zero"))

    form = _form(client.get("/tasks/plain").text, "data-reopen-action", "plain")

    assert CANCELLED_COPY in _text(form)
    assert "outcome" not in _text(form)


def test_a_completed_gate_offers_reopen_on_its_detail_page(
    client: TestClient, fake: LoggedFake
) -> None:
    """D8 excludes no type: a gate is reopened from its page like any task."""
    asyncio.run(fake.task_complete("gate-human", agent="agent-zero", outcome="ok"))

    form = _form(
        client.get("/tasks/gate-human").text, "data-reopen-action", "gate-human"
    )

    assert 'action="/tasks/gate-human/reopen"' in form
    assert _hidden(form, "next") == "/tasks/gate-human"


def test_reopen_is_not_offered_on_open_tasks_rows_or_the_panel(
    demo_client: TestClient,
) -> None:
    assert "data-reopen-action" not in demo_client.get(f"/tasks/{STRANDED}").text
    board = demo_client.get("/tasks?status=completed&status=cancelled").text
    assert "data-reopen-action" not in board
    panel = demo_client.get(f"/tasks/{CANCELLED_PRED}?fragment=panel").text
    assert "data-reopen-action" not in panel


def test_no_identity_renders_no_reopen(config, demo_fake: LoggedFake) -> None:
    with _client(config, demo_fake, operator="") as client:
        page = client.get(f"/tasks/{CANCELLED_PRED}").text

    assert "data-reopen-action" not in page
    assert page.count("choose an operator to act") == 1


# ── a completed task ─────────────────────────────────────────────────────


def test_reopening_a_completed_task_names_the_re_blocked_and_blocks_them_again(
    client: TestClient, fake: LoggedFake
) -> None:
    asyncio.run(fake.task_complete("gate-human", agent="agent-zero", outcome="Go"))
    fake.write_calls.clear()
    assert {"waiter-a", "waiter-b"} <= _sections(client.get("/tasks").text)["ready"]

    response = _reopen(
        client, "gate-human", expected_status="completed", next_url="/tasks"
    )

    assert response.status_code == 303
    assert fake.write_calls == [
        ("lithos_task_reopen", {"task_id": "gate-human", "agent": OPERATOR})
    ]
    page = client.get(response.headers["location"]).text
    receipt = _receipt(page)
    text = _text(receipt)
    assert "Reopened “Decide: re-develop PR #431?”" in text
    assert "Re-blocked 2 dependents:" in text
    assert "Story S7" in text and "Docs sweep" in text
    assert _named(receipt) == ["waiter-a", "waiter-b"]
    assert "waiting on this again" not in text
    # The outcome Lithos cleared, where it is kept, and that it is what Lens
    # READ before the write rather than a claim about what Lithos reopened.
    assert "Lens read it as completed just before reopening." in text
    assert (
        "Lithos keeps the outcome it cleared in its [Reopened] finding; "
        "Lens read it as “Go”." in text
    )

    # The page under the receipt is rendered from fresh reads.
    board = _sections(page)
    assert {"waiter-a", "waiter-b"} <= board["blocked"]
    assert not {"waiter-a", "waiter-b"} & board["ready"]
    assert "gate-human" in board["gates"]


def test_reopening_a_completed_task_nothing_waits_on_says_so(
    client: TestClient, fake: LoggedFake
) -> None:
    asyncio.run(fake.task_complete("plain", agent="agent-zero"))

    response = _reopen(client, "plain", expected_status="completed", headers=HTMX)

    assert (
        "Re-blocked no dependents — nothing that waits on this task had become "
        "ready." in _text(response.text)
    )


# ── a cancelled predecessor ──────────────────────────────────────────────


def test_reopening_a_cancelled_predecessor_names_the_dependents_now_waiting(
    demo_client: TestClient, demo_fake: LoggedFake
) -> None:
    assert _blocker_kinds(demo_fake, STRANDED) == {"blocker_unsatisfiable"}

    response = _reopen(demo_client, CANCELLED_PRED, expected_status="cancelled")

    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).path == f"/tasks/{CANCELLED_PRED}"
    assert demo_fake.write_calls == [
        ("lithos_task_reopen", {"task_id": CANCELLED_PRED, "agent": OPERATOR})
    ]
    page = demo_client.get(response.headers["location"]).text
    receipt = _receipt(page)
    text = _text(receipt)
    assert "1 dependent is waiting on this again:" in text
    assert STRANDED_TITLE in text
    assert _named(receipt) == [STRANDED]
    assert 'data-receipt-case="waiting"' in receipt
    # Nothing is reported as re-blocked: Lithos's reblocked[] is empty here.
    assert "Re-blocked" not in text
    assert "Superseded by the new transport." in text

    # Un-stranded: it waits on an open predecessor again, not on a dead one.
    assert _blocker_kinds(demo_fake, STRANDED) == {"task"}
    detail = demo_client.get(f"/tasks/{STRANDED}").text
    assert "can never become ready" not in detail


def test_reopening_a_cancelled_task_nothing_depends_on_says_so(
    client: TestClient, fake: LoggedFake
) -> None:
    asyncio.run(fake.task_cancel("plain", agent="agent-zero"))

    response = _reopen(client, "plain", expected_status="cancelled", headers=HTMX)

    text = _text(response.text)
    assert "No dependents are waiting on this again — nothing depends on it." in text
    assert "Re-blocked" not in text


def test_an_unreadable_dependent_makes_the_count_a_lower_bound(config) -> None:
    class WaiterUnreadable(LoggedFake):
        async def task_get(self, task_id: str):
            if task_id == "waiter-b":
                raise LithosToolError("Lithos MCP session is not available")
            return await super().task_get(task_id)

    fake = WaiterUnreadable()
    asyncio.run(fake.task_cancel("gate-human", agent="agent-zero"))
    with _client(config, fake) as client:
        response = _reopen(
            client, "gate-human", expected_status="cancelled", headers=HTMX
        )

    text = _text(response.text)
    assert "At least 1 dependent is waiting on this again:" in text
    assert _named(response.text) == ["waiter-a"]


#: How a dependent of the cancelled root depends on it: the edge types, one
#: edge each, in the order the root's edge list reports them.
BOTH = ("blocks", "waits_on_gate")


def _root_dataset(
    dependents: list[tuple[str, str, tuple[str, ...]]],
) -> FakeLithosDataset:
    """A cancelled gate ``root`` and its dependents, stamped from now.

    Each dependent is ``(id, status, edge types)``; a dependent with both
    types is one TASK reached by two edges, which Lithos permits (an edge is
    identified by ``(from, to, type)``).
    """
    now = datetime.now(UTC)
    root = TaskRecord(
        id="root",
        title="Cancelled root",
        status="cancelled",
        task_type="gate",
        metadata={"gate_type": "human"},
        created_by="planner",
        created_at=(now - timedelta(hours=2)).isoformat(),
        resolved_at=(now - timedelta(hours=1)).isoformat(),
    )
    tasks = [root]
    edges: dict[str, list[EdgeRecord]] = {"root": []}
    for task_id, status, types in dependents:
        tasks.append(
            TaskRecord(
                id=task_id,
                title=f"Dependent {task_id}",
                status=status,  # type: ignore[arg-type]
                created_by="planner",
                created_at=(now - timedelta(hours=1)).isoformat(),
                resolved_at=""
                if status == "open"
                else (now - timedelta(minutes=30)).isoformat(),
            )
        )
        for edge_type in types:
            edges["root"].append(EdgeRecord("root", task_id, edge_type, "outgoing"))
            edges.setdefault(task_id, []).append(
                EdgeRecord("root", task_id, edge_type, "incoming")
            )
    return FakeLithosDataset(
        tasks=tuple(tasks),
        edges={task_id: tuple(rows) for task_id, rows in edges.items()},
    )


def _fan_dataset(count: int, types: tuple[str, ...] = ("blocks",)):
    return _root_dataset([(f"dep-{n:02d}", "open", types) for n in range(count)])


def test_the_waiting_count_is_open_dependent_tasks_not_links_or_resolved_ones(
    config,
) -> None:
    """Only OPEN dependents wait on the reopened task, and one reached by both
    a ``blocks`` and a ``waits_on_gate`` edge is one task, named once."""
    fake = LoggedFake(
        _root_dataset(
            [
                ("open-a", "open", BOTH),
                ("open-b", "open", ("waits_on_gate",)),
                ("done-c", "completed", ("blocks",)),
                ("gone-d", "cancelled", ("waits_on_gate",)),
            ]
        )
    )
    with _client(config, fake) as client:
        fake.write_calls.clear()
        response = _reopen(client, "root", expected_status="cancelled", headers=HTMX)

    text = _text(response.text)
    assert "2 dependents are waiting on this again:" in text
    assert _named(response.text) == ["open-a", "open-b"]
    assert 'data-receipt-exact="yes"' in response.text
    assert fake.write_calls == [
        ("lithos_task_reopen", {"task_id": "root", "agent": OPERATOR})
    ]


def test_two_edges_per_dependent_never_push_the_count_past_the_tasks(
    config,
) -> None:
    """Twenty tasks, forty edges: more LINKS than one page, but every task
    fits on it — so the count is exact, and never "at least" more than there
    are."""
    fake = LoggedFake(_fan_dataset(20, BOTH))
    with _client(config, fake) as client:
        response = _reopen(client, "root", expected_status="cancelled", headers=HTMX)

    text = _text(response.text)
    assert "20 dependents are waiting on this again:" in text
    assert "At least" not in text
    assert "and 15 more" in text
    named = _named(response.text)
    assert len(named) == len(set(named)) == 5


def test_the_dependents_are_read_after_the_reopen_and_not_after_a_refusal(
    config,
) -> None:
    """Clarification 4: the read follows the SUCCESSFUL write — what it counts
    is the state the reopen left — and a refused reopen makes no such read."""
    fake = LoggedFake(demo_dataset())
    with _client(config, fake) as client:
        fake.method_calls.clear()
        _reopen(client, CANCELLED_PRED, expected_status="cancelled", headers=HTMX)
    called = [name for name, _ in fake.method_calls]
    assert called.index("task_reopen") < called.index("task_edge_list")

    refused = ReopenedMeanwhile(demo_dataset())
    with _client(config, refused) as client:
        refused.method_calls.clear()
        response = _reopen(
            client, CANCELLED_PRED, expected_status="cancelled", headers=HTMX
        )
    assert "This task is already open." in _text(response.text)
    called = [name for name, _ in refused.method_calls]
    assert called.count("task_reopen") == 1
    assert "task_edge_list" not in called


class ResolvedAgainMeanwhile(LoggedFake):
    """An agent reopens the task and resolves it AGAIN in the window after the
    pre-check — so the operator's reopen succeeds, on a state Lens never read.
    """

    def __init__(self, then: str) -> None:
        super().__init__()
        self.then = then

    async def task_reopen(self, task_id: str, *, agent: str):
        await FakeLithosClient.task_reopen(self, task_id, agent="agent-zero")
        if self.then == "complete":
            await FakeLithosClient.task_complete(
                self, task_id, agent="agent-zero", outcome="New completion"
            )
        else:
            await FakeLithosClient.task_cancel(self, task_id, agent="agent-zero")
        return await FakeLithosClient.task_reopen(self, task_id, agent=agent)


def _operators_finding(fake: FakeLithosClient, task_id: str) -> str:
    """The ``[Reopened]`` finding the OPERATOR's reopen made Lithos record."""
    findings = asyncio.run(fake.list_findings(task_id))
    return [f.summary for f in findings if f.agent == OPERATOR][-1]


def test_a_task_completed_again_meanwhile_keeps_the_re_blocked_it_returned(
    config,
) -> None:
    """Read as cancelled; reopened and COMPLETED by an agent before the call.
    Lithos's ``reblocked`` proves the completed case: the receipt names those
    tasks, says the read was stale, and quotes no outcome from it."""
    fake = ResolvedAgainMeanwhile("complete")
    asyncio.run(fake.task_cancel("gate-human", agent="agent-zero"))
    with _client(config, fake) as client:
        response = _reopen(
            client, "gate-human", expected_status="cancelled", headers=HTMX
        )

    assert response.status_code == 200
    text = _text(response.text)
    assert "Re-blocked 2 dependents:" in text
    assert _named(response.text) == ["waiter-a", "waiter-b"]
    assert "waiting on this again" not in text
    assert (
        "Lens read it as cancelled just before reopening, but Lithos re-blocked "
        "dependents, so it had been completed again by the time this reopen "
        "applied." in text
    )
    # What Lithos recorded agrees with the receipt's case, not the stale read.
    assert _operators_finding(fake, "gate-human") == (
        "[Reopened] task reopened (was completed); prior outcome: New completion"
    )
    assert "New completion" not in text


def test_a_task_cancelled_meanwhile_is_not_claimed_as_completed(config) -> None:
    """Read as completed (outcome "Go"); reopened and CANCELLED by an agent
    before the call. Nothing in Lithos's answer says so, so the receipt states
    only what it knows: Lithos re-blocked no one, and the status and outcome are
    what Lens read before the write."""
    fake = ResolvedAgainMeanwhile("cancel")
    asyncio.run(fake.task_complete("gate-human", agent="agent-zero", outcome="Go"))
    with _client(config, fake) as client:
        response = _reopen(
            client, "gate-human", expected_status="completed", headers=HTMX
        )

    text = _text(response.text)
    assert "Re-blocked no dependents" in text
    assert "Lens read it as completed just before reopening." in text
    assert "Lens read it as “Go”." in text
    assert "was cancelled" in _operators_finding(fake, "gate-human")
    # Never stated as the reopened state: no unqualified "it was completed",
    # and the outcome is not presented as what Lithos's finding holds.
    assert "it was completed" not in text
    assert "finding: “Go”" not in text


def test_more_dependents_than_one_page_is_a_lower_bound(config) -> None:
    fake = LoggedFake(_fan_dataset(LINK_PAGE_SIZE + 5))
    with _client(config, fake) as client:
        response = _reopen(client, "root", expected_status="cancelled", headers=HTMX)

    text = _text(response.text)
    assert f"At least {LINK_PAGE_SIZE} dependents are waiting on this again:" in text
    assert f"and {LINK_PAGE_SIZE - 5} more" in text
    assert 'data-receipt-exact="no"' in response.text


def test_a_failed_dependents_read_still_reports_the_reopen(
    config, caplog: pytest.LogCaptureFixture
) -> None:
    """Clarification 4: the read runs after a reopen that APPLIED, so its
    failure must not turn the write into an unknown outcome or a refusal."""

    class EdgesDown(LoggedFake):
        async def task_edge_list(self, task_id: str, **kwargs: Any):
            raise LithosToolError("Lithos MCP session is not available")

    fake = EdgesDown(demo_dataset())
    with _client(config, fake) as client:
        caplog.set_level(logging.INFO, logger="lithos_lens.write_funnel")
        response = _reopen(client, CANCELLED_PRED, expected_status="cancelled")
        page = client.get(response.headers["location"]).text

    assert response.status_code == 303
    text = _text(_receipt(page))
    assert "Reopened “Port the legacy run bridge”" in text
    assert "Lens couldn't read its dependents just now." in text
    assert "waiting on this again" not in text
    (line,) = [r for r in caplog.records if getattr(r, "lens_event", "") == AUDIT_EVENT]
    assert line.result == "ok"  # type: ignore[attr-defined]


# ── an already-open task ─────────────────────────────────────────────────


def test_a_stale_form_for_an_open_task_is_the_conflict_page_and_no_call(
    client: TestClient, fake: LoggedFake
) -> None:
    response = _reopen(client, "gate-human", expected_status="completed")

    assert response.status_code == 409
    text = _text(response.text)
    assert "This task is now open." in text
    assert NOTHING_CHANGED in text
    assert fake.write_calls == []
    assert fake.reads_of("task_reopen") == []


class ReopenedMeanwhile(LoggedFake):
    """Someone reopens the task in the window AFTER the pre-check."""

    async def task_reopen(self, task_id: str, *, agent: str):
        await FakeLithosClient.task_reopen(self, task_id, agent="agent-zero")
        return await FakeLithosClient.task_reopen(self, task_id, agent=agent)


def test_a_task_reopened_meanwhile_is_refused_by_lithos_onto_the_conflict_page(
    config,
) -> None:
    fake = ReopenedMeanwhile()
    asyncio.run(fake.task_complete("gate-human", agent="agent-zero"))
    with _client(config, fake) as client:
        fake.method_calls.clear()
        response = _reopen(client, "gate-human", expected_status="completed")

    assert response.status_code == 409
    text = _text(response.text)
    assert "This task is already open." in text
    assert NOTHING_CHANGED in text
    # The operator's call was placed once, and refused.
    assert len(fake.reads_of("task_reopen")) == 1


# ── Reopen gate, on the completion receipt ───────────────────────────────


def test_the_completion_receipt_offers_reopen_gate_with_its_limits(
    client: TestClient,
) -> None:
    response = client.post(
        "/tasks/gate-human/approve",
        data={"expected_status": "open", "next": "/tasks?project=influx"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    page = client.get(response.headers["location"]).text

    form = _form(_receipt(page), "data-reopen-gate", "gate-human")
    assert 'action="/tasks/gate-human/reopen"' in form
    assert ">Reopen gate</button>" in form
    assert "Undo" not in form
    assert REOPEN_GATE_LIMITS in _text(form)
    assert _hidden(form, "expected_status") == "completed"
    assert _hidden(form, "next") == "/tasks?project=influx"


def test_an_htmx_receipts_reopen_gate_returns_to_the_board_not_the_post(
    client: TestClient,
) -> None:
    """Clarification 6: the fragment is rendered against the POST, whose path
    has no page; the form carries the funnel's checked ``back_to``."""
    response = client.post(
        "/tasks/gate-human/approve",
        data={"expected_status": "open", "next": "/tasks?project=influx"},
        headers=HTMX,
    )

    form = _form(response.text, "data-reopen-gate", "gate-human")
    assert _hidden(form, "next") == "/tasks?project=influx"


def test_the_detail_page_can_show_both_reopen_forms_each_with_its_own_hook(
    client: TestClient,
) -> None:
    response = client.post(
        "/tasks/gate-human/approve",
        data={"expected_status": "open"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    page = client.get(response.headers["location"]).text

    assert _form(page, "data-reopen-gate", "gate-human")
    assert _form(page, "data-reopen-action", "gate-human")


def test_completing_then_reopening_leaves_the_board_as_it_started(
    client: TestClient, fake: LoggedFake
) -> None:
    """The acceptance's round trip, through the forms the pages render: the
    second receipt names the same tasks the first did."""
    before = _sections(client.get("/tasks").text)

    completed = client.post(
        "/tasks/gate-human/approve",
        data={"expected_status": "open", "next": "/tasks"},
        headers=HTMX,
    )
    first = _named(_receipt(completed.text))
    assert first == ["waiter-a", "waiter-b"]
    form = _form(completed.text, "data-reopen-gate", "gate-human")

    reopened = client.post(
        "/tasks/gate-human/reopen",
        data={
            "expected_status": _hidden(form, "expected_status"),
            "next": _hidden(form, "next"),
        },
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert reopened.status_code == 303
    assert urlsplit(reopened.headers["location"]).path == "/tasks"
    assert "receipt" in parse_qs(urlsplit(reopened.headers["location"]).query)
    page = client.get(reopened.headers["location"]).text

    assert _named(_receipt(page)) == first
    assert "Re-blocked 2 dependents:" in _text(_receipt(page))
    assert _sections(page) == before
    assert [name for name, _ in fake.write_calls] == [
        "lithos_task_complete",
        "lithos_task_reopen",
    ]


# ── the record: one audit line and one span per attempt ──────────────────


class _Case:
    def __init__(
        self,
        case: str,
        *,
        task_id: str,
        expected_status: str,
        result: str,
        code: str = "",
        fake: type[LoggedFake] = LoggedFake,
        prepare: str = "",
        operator: str = OPERATOR,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.case = case
        self.task_id = task_id
        self.expected_status = expected_status
        self.result = result
        self.code = code
        self.fake = fake
        self.prepare = prepare
        self.operator = operator
        self.headers = headers if headers is not None else SAME_ORIGIN


CASES = [
    _Case(
        "completed",
        task_id="gate-human",
        expected_status="completed",
        result="ok",
        prepare="complete",
    ),
    _Case(
        "cancelled",
        task_id="gate-human",
        expected_status="cancelled",
        result="ok",
        prepare="cancel",
    ),
    _Case(
        "stale",
        task_id="gate-human",
        expected_status="completed",
        result="conflict",
        code="stale_status",
    ),
    _Case(
        "reopened-meanwhile",
        task_id="gate-human",
        expected_status="completed",
        result="conflict",
        code="task_not_resolved",
        fake=ReopenedMeanwhile,
        prepare="complete",
    ),
    _Case(
        "no-operator",
        task_id="gate-human",
        expected_status="completed",
        result="no_operator",
        operator="",
        prepare="complete",
    ),
    _Case(
        "foreign-origin",
        task_id="gate-human",
        expected_status="completed",
        result="refused_origin",
        prepare="complete",
        headers={"Origin": "http://evil.example"},
    ),
]


@pytest.mark.parametrize("attempt", [pytest.param(c, id=c.case) for c in CASES])
def test_every_reopen_attempt_is_recorded_once(
    config,
    spans: InMemorySpanExporter,
    metric_reader,
    caplog: pytest.LogCaptureFixture,
    attempt: _Case,
) -> None:
    fake = attempt.fake()
    if attempt.prepare == "complete":
        asyncio.run(fake.task_complete(attempt.task_id, agent="agent-zero"))
    elif attempt.prepare == "cancel":
        asyncio.run(fake.task_cancel(attempt.task_id, agent="agent-zero"))
    with _client(config, fake, operator=attempt.operator) as client:
        caplog.set_level(logging.INFO, logger="lithos_lens.write_funnel")
        caplog.clear()
        spans.clear()
        _reopen(
            client,
            attempt.task_id,
            expected_status=attempt.expected_status,
            headers=attempt.headers,
        )

    lines = [r for r in caplog.records if getattr(r, "lens_event", "") == AUDIT_EVENT]
    assert len(lines) == 1
    line = lines[0]
    assert line.action == "reopen"  # type: ignore[attr-defined]
    assert line.result == attempt.result  # type: ignore[attr-defined]
    assert line.code == attempt.code  # type: ignore[attr-defined]
    assert line.arguments == {"task_id": attempt.task_id}  # type: ignore[attr-defined]
    written = [s for s in spans.get_finished_spans() if s.name == "lens.writes.reopen"]
    assert len(written) == 1
    attributes = dict(written[0].attributes or {})
    assert attributes["lens.write.result"] == attempt.result
    assert attributes.get("lens.write.code", "") == attempt.code
    # Nothing complete-specific (clarification 7).
    assert "lens.write.gate_type" not in attributes
    assert "lens.write.override" not in attributes
    assert (
        metric_value(
            metric_reader, "lens_writes_total", action="reopen", result=attempt.result
        ).value
        == 1
    )
    if attempt.result == "ok":
        # The prior status is the audit line's observed status, and the
        # answer is Lithos's, with reblocked as a list.
        assert line.observed_status == attempt.expected_status  # type: ignore[attr-defined]
        assert line.envelope["reblocked"] == (  # type: ignore[attr-defined]
            ["waiter-a", "waiter-b"] if attempt.case == "completed" else []
        )
