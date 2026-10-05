"""T3-W6 — Cancel, with what it strands and whose claims it releases stated first.

Asserted the way the PRD's Testing Decisions ask: on what a request returns and
on what the fake recorded (its write log, and the method log the W4 suite's
``LoggedFake`` keeps), never on internals. The groups are the slice's
acceptance list:

- the CONFIRM PAGE states the direct and transitive counts and names the first
  few, counts a cross-project dependent, renders "≥" with the reason on a
  failed edge read and on a budget hit, lists the active claims by agent, says
  an epic's open children are kept and a gate's waiters become unsatisfiable,
  and labels the reason as not stored;
- a CONFIRMED cancel makes one ``lithos_task_cancel`` as the operator, and the
  board rendered afterwards shows the direct dependent as unsatisfiable;
- with ``confirm_cancel = false`` the POST succeeds without the GET and the
  receipt carries the same facts;
- a task that is no longer open is the conflict page, with no write.

The demo dataset carries the fixtures (clarification F9): the depth-5 chain
``loom-schema`` → … → ``loom-announce`` with the cross-project
``loom-ship`` → ``lens-graph-page``, the cycle, the epics, the gates with
waiters and ``worker-a``'s claim. Claims by several agents, a failed edge read
and a budget hit are test-local.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterator
from dataclasses import replace
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from lithos_lens.cancel_consequences import (
    REASON_DEADLINE,
    load_cancel_consequences,
    walk_downstream,
)
from lithos_lens.config import load_config
from lithos_lens.fake_dataset import demo_dataset
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.graph_cache import GraphCache
from lithos_lens.lithos_client import LithosToolError
from lithos_lens.operator import OPERATOR_COOKIE_NAME
from lithos_lens.tasks import ClaimRecord
from lithos_lens.web import create_app
from lithos_lens.write_funnel import AUDIT_EVENT
from tests.test_complete_gate import LoggedFake

ORIGIN = "http://lens.test"
SAME_ORIGIN = {"Origin": ORIGIN}
OPERATOR = "dave"
CONFIRMATION = {"confirm": "cancel"}
NOT_STORED = "recorded in the event stream only — not stored on the task"

#: The demo's depth-5 chain and what cancelling its head strands.
HEAD = "loom-schema"
DIRECT = ["loom-transport"]
#: Behind, ordered by created_at then id (oldest first).
BEHIND = ["loom-worker", "loom-ship", "loom-announce", "lens-graph-page"]


@pytest.fixture
def config(lithos_lens_config_env: Path):
    return load_config(lithos_lens_config_env)


def _without_confirm(config):
    return replace(config, writes=replace(config.writes, confirm_cancel=False))


def _client(config, fake: FakeLithosClient, *, operator: str = OPERATOR) -> TestClient:
    app = create_app(config, lithos_client_factory=lambda _: fake)
    client = TestClient(app, base_url=ORIGIN)
    if operator:
        client.cookies.set(OPERATOR_COOKIE_NAME, operator)
    return client


@pytest.fixture
def fake() -> LoggedFake:
    return LoggedFake(demo_dataset())


@pytest.fixture
def client(config, fake: LoggedFake) -> Iterator[TestClient]:
    with _client(config, fake) as test_client:
        fake.method_calls.clear()
        fake.write_calls.clear()
        yield test_client


def _post(
    client: TestClient,
    task_id: str,
    *,
    expected_status: str = "open",
    confirmed: bool = True,
    reason: str = "",
    next_url: str = "",
):
    data = {"expected_status": expected_status}
    if confirmed:
        data.update(CONFIRMATION)
    if reason:
        data["reason"] = reason
    if next_url:
        data["next"] = next_url
    return client.post(
        f"/tasks/{task_id}/cancel",
        data=data,
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )


def _text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def _facts(html: str) -> str:
    start = html.index("data-cancel-facts")
    start = html.rindex("<div", 0, start)
    end = html.index('<section class="cancel-facts-part" data-cancel-claims', start)
    return html[start:end]


def _stranded(html: str) -> list[str]:
    return re.findall(r'data-cancel-stranded-task="([^"]+)"', html)


def _behind(html: str) -> list[str]:
    return re.findall(r'data-cancel-behind-task="([^"]+)"', html)


def _receipt(html: str) -> str:
    """The receipt banner, whole: its own sections nest inside it."""
    start = html.index('<section class="write-receipt"')
    depth = 0
    for tag in re.finditer(r"<(/?)section\b", html[start:]):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return html[start : start + tag.end()]
    raise AssertionError("unterminated receipt")


def _attention_rules(board: str, task_id: str) -> set[str]:
    start = board.index(f'id="task-row-{task_id}"')
    row = board[start : board.index("</article>", start)]
    return set(re.findall(r'data-attention-rule="([^"]+)"', row))


# ── the confirm page ─────────────────────────────────────────────────────


def test_the_chain_head_states_the_direct_and_behind_counts_and_names_them(
    client: TestClient, fake: LoggedFake
) -> None:
    response = client.get(f"/tasks/{HEAD}/cancel")

    assert response.status_code == 200
    page = response.text
    assert "Cancelling strands 1 task directly, 4 more behind them." in _text(page)
    assert "permanently blocked until re-routed" in _text(page)
    assert 'data-cancel-exact="yes"' in page
    assert _stranded(page) == DIRECT
    assert _behind(page) == BEHIND
    assert "Implement the run transport" in _text(_facts(page))
    # A read: nothing was written.
    assert fake.write_calls == []


def test_the_focal_tasks_edges_are_re_read_and_the_rest_come_from_the_cache(
    client: TestClient, fake: LoggedFake
) -> None:
    client.get(f"/tasks/{HEAD}/cancel")
    fake.method_calls.clear()

    client.get(f"/tasks/{HEAD}/cancel")

    # F3: the focal entry is evicted and read again; the dependents' entries
    # are still inside the cache TTL and are not.
    assert fake.reads_of("task_edge_list") == [(HEAD,)]


def test_a_cross_project_dependent_is_counted(client: TestClient) -> None:
    page = client.get("/tasks/loom-ship/cancel").text

    # loom-ship (lithos-loom) strands lens-graph-page (lithos-lens) as well.
    assert sorted(_stranded(page)) == ["lens-graph-page", "loom-announce"]
    assert "Cancelling strands 2 tasks directly, 0 more behind them." in _text(page)


def test_a_cycle_counts_its_partner_once_and_never_the_task_itself(
    client: TestClient,
) -> None:
    page = client.get("/tasks/loom-cycle-a/cancel").text

    assert _stranded(page) == ["loom-cycle-b"]
    assert _behind(page) == []
    assert "Cancelling strands 1 task directly, 0 more behind them." in _text(page)


class EdgesDown(LoggedFake):
    """One edge read in the chain fails: the walk cannot see past it."""

    async def task_edge_list(self, task_id: str, **kwargs: Any):
        if task_id == "loom-worker":
            raise LithosToolError("did not answer", code="timeout")
        return await super().task_edge_list(task_id, **kwargs)


def test_a_failed_edge_read_renders_lower_bounds_with_the_reason(config) -> None:
    fake = EdgesDown(demo_dataset())
    with _client(config, fake) as client:
        page = client.get(f"/tasks/{HEAD}/cancel").text

    text = _text(page)
    assert "Cancelling strands ≥ 1 task directly, ≥ 1 more behind them." in text
    assert (
        "Both numbers are lower bounds: Lens couldn't read the dependencies of "
        "1 task (timeout)." in text
    )
    assert 'data-cancel-exact="no"' in page
    # The form is still offered: a partial answer is labelled, not withheld.
    assert "data-cancel-confirm-form" in page


def test_a_walk_over_its_budget_renders_lower_bounds_with_the_reason(config) -> None:
    config = replace(config, graph=replace(config.graph, max_tasks=2))
    with _client(config, LoggedFake(demo_dataset())) as client:
        page = client.get(f"/tasks/{HEAD}/cancel").text

    text = _text(page)
    assert "Cancelling strands ≥ 1 task directly, ≥ 1 more behind them." in text
    assert "lower bounds: the walk stopped at its budget of 2 tasks." in text
    assert _behind(page) == ["loom-worker"]


class OpenListDown(LoggedFake):
    async def list_tasks(self, **kwargs: Any):
        if kwargs.get("status") == "open":
            raise LithosToolError("did not answer", code="timeout")
        return await super().list_tasks(**kwargs)


def test_a_failed_open_list_says_the_consequence_is_unknown_and_still_offers_it(
    config,
) -> None:
    with _client(config, OpenListDown(demo_dataset())) as client:
        page = client.get(f"/tasks/{HEAD}/cancel").text

    text = _text(page)
    assert (
        "Lens couldn't work out what this cancel strands: Lens couldn't read the "
        "list of open tasks." in text
    )
    assert "Cancelling strands" not in text
    assert "data-cancel-confirm-form" in page


class SlowEdges(FakeLithosClient):
    async def task_edge_list(self, task_id: str, **kwargs: Any):
        if task_id != HEAD:
            await asyncio.sleep(1)
        return await super().task_edge_list(task_id, **kwargs)


def test_a_walk_past_its_deadline_states_lower_bounds() -> None:
    fake = SlowEdges(None, dataset=demo_dataset())

    async def walk():
        task = await fake.task_get(HEAD)
        index = {row.id: row for row in await fake.list_tasks(status="open")}
        return await walk_downstream(
            fake,
            task,
            open_index=index,
            cache=GraphCache(),
            fetch_concurrency=4,
            max_nodes=300,
            deadline_s=0.2,
        )

    result = asyncio.run(walk())
    assert [row.id for row in result.direct] == DIRECT
    assert result.behind == ()
    assert not result.exact
    assert result.bound_reasons == (REASON_DEADLINE,)


def test_active_claims_are_listed_by_agent(config) -> None:
    dataset = replace(
        demo_dataset(),
        claims={
            HEAD: (
                ClaimRecord(agent="worker-b", aspect="review"),
                ClaimRecord(agent="agent-zero", aspect="implementation"),
                ClaimRecord(agent="worker-b", aspect="docs"),
            )
        },
    )
    with _client(config, LoggedFake(dataset)) as client:
        page = client.get(f"/tasks/{HEAD}/cancel").text

    assert re.findall(r'data-cancel-claim-agent="([^"]+)"', page) == [
        "agent-zero",
        "worker-b",
    ]
    text = _text(page)
    assert "It releases 3 active claims:" in text
    assert "agent-zero — implementation" in text
    assert "worker-b — docs, review" in text


def test_the_demo_claim_is_named_and_an_unclaimed_task_says_so(
    client: TestClient,
) -> None:
    claimed = _text(client.get("/tasks/influx-ingest-cutover/cancel").text)
    assert "It releases 1 active claim: worker-a — implementation" in claimed

    unclaimed = _text(client.get(f"/tasks/{HEAD}/cancel").text)
    assert "No agent holds an active claim on it." in unclaimed


def test_an_epic_with_open_children_says_they_are_kept(client: TestClient) -> None:
    page = client.get("/tasks/loom-epic/cancel").text

    text = _text(page)
    assert "Its 7 open children are not cancelled with it:" in text
    children = re.findall(r'data-cancel-child="([^"]+)"', page)
    # The first five, oldest first; the rest counted.
    assert len(children) == 5
    assert children[0] == "loom-worker"
    assert "and 2 more" in text


def test_a_task_without_children_says_nothing_about_them(client: TestClient) -> None:
    assert "data-cancel-children" not in client.get(f"/tasks/{HEAD}/cancel").text


def test_a_gate_says_its_waiters_become_unsatisfiable_and_to_complete_it(
    client: TestClient,
) -> None:
    page = client.get("/tasks/influx-read-swap-approval/cancel").text

    text = _text(page)
    assert "Its waiters become unsatisfiable." in text
    assert "Completing the gate, not cancelling it, is how they proceed." in text
    assert _stranded(page) == ["influx-backfill"]
    # An ordinary task carries no gate sentence.
    assert "data-cancel-gate" not in client.get(f"/tasks/{HEAD}/cancel").text


def test_the_reason_field_carries_the_not_stored_note(client: TestClient) -> None:
    page = client.get(f"/tasks/{HEAD}/cancel").text

    form = page[page.index("data-cancel-confirm-form") :]
    form = form[: form.index("</form>")]
    assert 'name="reason"' in form
    assert f"The reason is {NOT_STORED}." in _text(form)
    assert 'name="confirm" value="cancel"' in form
    assert 'name="expected_status" value="open"' in form


def test_the_confirm_page_with_no_identity_states_the_facts_and_offers_no_form(
    config, fake: LoggedFake
) -> None:
    with _client(config, fake, operator="") as client:
        page = client.get(f"/tasks/{HEAD}/cancel").text

    assert "Cancelling strands 1 task directly" in _text(page)
    assert "data-cancel-confirm-form" not in page
    assert "data-cancel-no-operator" in page


# ── a task that is not open ──────────────────────────────────────────────


def test_the_confirm_page_for_a_task_no_longer_open_is_the_conflict_page(
    client: TestClient, fake: LoggedFake
) -> None:
    response = client.get("/tasks/loom-cancelled-pred/cancel")

    assert response.status_code == 409
    text = _text(response.text)
    assert "This task is now cancelled." in text
    assert "strands" not in text
    assert "data-cancel-confirm-form" not in response.text
    assert fake.reads_of("task_edge_list") == []


def test_the_confirm_page_for_a_missing_task_says_it_no_longer_exists(
    client: TestClient,
) -> None:
    response = client.get("/tasks/no-such-task/cancel")

    assert response.status_code == 404
    assert "This task no longer exists." in _text(response.text)


def test_a_post_on_a_task_no_longer_open_is_the_conflict_page_with_no_write(
    client: TestClient, fake: LoggedFake
) -> None:
    response = _post(client, "loom-cancelled-pred")

    assert response.status_code == 409
    assert "This task is now cancelled." in _text(response.text)
    assert fake.write_calls == []


def test_a_form_claiming_the_cancelled_status_is_refused_with_no_write(
    client: TestClient, fake: LoggedFake
) -> None:
    """D8: the pre-check passes (cancelled == cancelled); ``admits`` refuses."""
    response = _post(client, "loom-cancelled-pred", expected_status="cancelled")

    assert response.status_code == 409
    text = _text(response.text)
    assert "Nothing was changed." in text
    assert "Only an open task can be cancelled; this one is cancelled." in text
    assert fake.write_calls == []


# ── the confirmed cancel ─────────────────────────────────────────────────


def test_a_confirmed_cancel_makes_one_call_as_the_operator_and_strands_the_dependent(
    client: TestClient, fake: LoggedFake
) -> None:
    before = client.get("/tasks").text
    assert "unsatisfiable" not in _attention_rules(before, "loom-transport")

    response = _post(client, HEAD, reason="Superseded  by\nthe v2 schema")

    assert response.status_code == 303
    location = response.headers["location"]
    assert urlsplit(location).path == f"/tasks/{HEAD}"
    assert fake.write_calls == [
        (
            "lithos_task_cancel",
            {
                "task_id": HEAD,
                "agent": OPERATOR,
                "reason": "Superseded by the v2 schema",
            },
        )
    ]
    page = client.get(location).text
    receipt = _text(_receipt(page))
    assert "Cancelled “Design the run-record schema”" in receipt
    assert f"Your reason was {NOT_STORED}." in receipt
    # The confirm page stated the facts; the receipt does not repeat them.
    assert "strands" not in receipt and "stranded" not in receipt

    # The board, from fresh reads: the direct dependent can never run now.
    board = client.get("/tasks").text
    assert "unsatisfiable" in _attention_rules(board, "loom-transport")


def test_the_confirm_page_returns_the_operator_where_they_started(
    client: TestClient,
) -> None:
    page = client.get(f"/tasks/{HEAD}/cancel?next=/tasks%3Fproject%3Dlithos-loom").text
    assert 'name="next" value="/tasks?project=lithos-loom"' in page

    response = _post(client, HEAD, next_url="/tasks?project=lithos-loom")
    parts = urlsplit(response.headers["location"])
    assert parts.path == "/tasks"
    assert parse_qs(parts.query)["project"] == ["lithos-loom"]
    assert "receipt" in parse_qs(parts.query)


def test_an_unconfirmed_post_is_sent_to_the_confirm_page_and_writes_nothing(
    client: TestClient, fake: LoggedFake
) -> None:
    response = _post(client, HEAD, confirmed=False, next_url="/tasks")

    assert response.status_code == 303
    parts = urlsplit(response.headers["location"])
    assert parts.path == f"/tasks/{HEAD}/cancel"
    assert parse_qs(parts.query)["next"] == ["/tasks"]
    assert fake.write_calls == []


def test_every_cancel_attempt_is_recorded_once_without_the_reason_text(
    config,
    fake: LoggedFake,
    spans: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with _client(config, fake) as client:
        caplog.set_level(logging.INFO, logger="lithos_lens.write_funnel")
        caplog.clear()
        spans.clear()
        _post(client, HEAD, confirmed=False, reason="secret plan")
        _post(client, HEAD, reason="secret plan")

    lines = [r for r in caplog.records if getattr(r, "lens_event", "") == AUDIT_EVENT]
    assert [(line.result, line.code) for line in lines] == [  # type: ignore[attr-defined]
        ("rejected", "confirmation_required"),
        ("ok", ""),
    ]
    for line in lines:
        assert line.action == "cancel"  # type: ignore[attr-defined]
        assert line.arguments == {"task_id": HEAD, "reason_chars": 11}  # type: ignore[attr-defined]
        assert line.operator == OPERATOR  # type: ignore[attr-defined]
    assert all("secret plan" not in repr(vars(r)) for r in caplog.records)
    written = [s for s in spans.get_finished_spans() if s.name == "lens.writes.cancel"]
    assert len(written) == 2
    for span in written:
        assert "secret plan" not in repr(dict(span.attributes or {}))


# ── confirm_cancel = false ───────────────────────────────────────────────


def test_without_the_confirm_step_the_post_succeeds_and_the_receipt_states_the_facts(
    config, fake: LoggedFake
) -> None:
    with _client(_without_confirm(config), fake) as client:
        fake.method_calls.clear()
        response = _post(client, HEAD, confirmed=False)
        assert response.status_code == 303
        page = client.get(response.headers["location"]).text

    assert fake.write_calls == [
        # No reason typed: none is sent (the contract omits an empty one).
        ("lithos_task_cancel", {"task_id": HEAD, "agent": OPERATOR})
    ]
    receipt = _receipt(page)
    text = _text(receipt)
    assert "This cancel stranded 1 task directly, 4 more behind them." in text
    assert _stranded(receipt) == DIRECT
    assert _behind(receipt) == BEHIND
    assert "No agent held an active claim on it." in text


def test_without_the_confirm_step_claims_are_read_before_the_cancel_releases_them(
    config, fake: LoggedFake
) -> None:
    with _client(_without_confirm(config), fake) as client:
        fake.method_calls.clear()
        response = _post(client, "influx-ingest-cutover", confirmed=False)
        page = client.get(response.headers["location"]).text

    calls = [name for name, _ in fake.method_calls]
    assert calls.index("task_status") < calls.index("task_cancel")
    text = _text(_receipt(page))
    assert "Released 1 active claim: worker-a — implementation" in text
    assert "This cancel stranded 1 task directly, 0 more behind them." in text
    # The claim is gone now: the receipt said what it was before.
    status = asyncio.run(fake.task_status("influx-ingest-cutover"))
    assert status is not None and status.claims == ()


def test_without_the_confirm_step_a_gate_receipt_says_its_waiters_are_unsatisfiable(
    config, fake: LoggedFake
) -> None:
    with _client(_without_confirm(config), fake) as client:
        response = _post(client, "influx-read-swap-approval", confirmed=False)
        page = client.get(response.headers["location"]).text

    text = _text(_receipt(page))
    assert "Its waiters are now unsatisfiable." in text


def test_without_the_confirm_step_a_failed_facts_read_never_fails_the_cancel(
    config,
) -> None:
    fake = OpenListDown(demo_dataset())
    with _client(_without_confirm(config), fake) as client:
        response = _post(client, HEAD, confirmed=False)
        page = client.get(response.headers["location"]).text

    assert response.status_code == 303
    assert len(fake.write_calls) == 1
    text = _text(_receipt(page))
    assert "Cancelled “Design the run-record schema”" in text
    assert "Lens couldn't work out what this cancel stranded" in text


def test_the_confirm_page_still_renders_without_the_confirm_step(config) -> None:
    with _client(_without_confirm(config), LoggedFake(demo_dataset())) as client:
        page = client.get(f"/tasks/{HEAD}/cancel").text

    assert "Cancelling strands 1 task directly, 4 more behind them." in _text(page)


# ── surfaces ─────────────────────────────────────────────────────────────


def _cancel_links(html: str, task_id: str) -> list[str]:
    blocks = re.findall(
        rf'data-cancel-action data-task-id="{re.escape(task_id)}">\s*'
        r'<a class="proceed-anyway-link" href="([^"]*)"',
        html,
    )
    return [unescape(href) for href in blocks]


def _row(board: str, task_id: str) -> str:
    start = board.index(f'id="task-row-{task_id}"')
    return board[start : board.index("</article>", start)]


def test_the_detail_page_links_an_open_task_to_its_confirm_page(
    client: TestClient,
) -> None:
    page = client.get(f"/tasks/{HEAD}").text

    (link,) = _cancel_links(page, HEAD)
    parts = urlsplit(link)
    assert parts.path == f"/tasks/{HEAD}/cancel"
    assert parse_qs(parts.query)["next"] == [f"/tasks/{HEAD}"]
    # A link, not a form: nothing on the page posts a cancel.
    assert f'action="/tasks/{HEAD}/cancel"' not in page


def test_a_resolved_task_offers_no_cancel(client: TestClient) -> None:
    assert "data-cancel-action" not in client.get("/tasks/loom-cancelled-pred").text
    assert "data-cancel-action" not in client.get("/tasks/loom-design-done").text


def test_open_rows_and_gate_rows_carry_the_overflow_menu(client: TestClient) -> None:
    board = client.get("/tasks?since=2026-08-01").text

    for task_id in (HEAD, "influx-read-swap-approval"):
        row = _row(board, task_id)
        assert '<details class="row-menu" data-row-menu' in row
        assert '<summary class="row-menu-toggle"' in row
        (link,) = _cancel_links(row, task_id)
        assert urlsplit(link).path == f"/tasks/{task_id}/cancel"
    # The Cancelled section's rows render through the same template: no menu.
    assert "data-row-menu" not in _row(board, "loom-cancelled-pred")


def test_no_identity_renders_no_cancel_affordance(config, fake: LoggedFake) -> None:
    with _client(config, fake, operator="") as client:
        for url in ("/tasks", f"/tasks/{HEAD}"):
            page = client.get(url).text
            assert "data-cancel-action" not in page
            assert "data-row-menu" not in page


def test_without_the_confirm_step_the_affordance_is_the_direct_form(
    config, fake: LoggedFake
) -> None:
    with _client(_without_confirm(config), fake) as client:
        page = client.get(f"/tasks/{HEAD}").text
        board = client.get("/tasks").text

    for html in (page, _row(board, HEAD)):
        assert _cancel_links(html, HEAD) == []
        form = html[html.index("data-cancel-direct") :]
        form = form[: form.index("</form>")]
        assert 'name="expected_status" value="open"' in form
        assert 'name="confirm"' not in form
        assert NOT_STORED in _text(form)
        assert "hx-post" not in form


# ── the module, directly ─────────────────────────────────────────────────


def test_the_consequence_read_never_raises_on_failed_claims_and_children() -> None:
    class Down(FakeLithosClient):
        async def task_status(self, task_id: str):
            raise LithosToolError("did not answer", code="timeout")

        async def task_children(self, task_id: str, **kwargs: Any):
            raise LithosToolError("did not answer", code="timeout")

    fake = Down(None, dataset=demo_dataset())

    async def read():
        return await load_cancel_consequences(
            fake,
            await fake.task_get("loom-epic"),
            cache=GraphCache(),
            fetch_concurrency=4,
            max_nodes=300,
        )

    consequences = asyncio.run(read())
    assert consequences.claims_unread and consequences.children_unread
    assert consequences.claims == () and consequences.open_children == ()
    assert consequences.walk is not None and consequences.walk.exact
