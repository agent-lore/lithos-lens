"""T3-W4b — Proceed anyway: completing a timer, CI or PR gate behind a confirm.

Asserted the way the PRD's Testing Decisions ask: on what a request returns and
on what the fake recorded (its write log, and the method log the W4 suite's
``LoggedFake`` keeps), never on internals. One group per acceptance line:

- WHERE the link is offered: a timer, CI, PR or unknown-type gate shows the
  Proceed anyway link and no direct Complete form, on the row, the panel and
  the detail page; a person-resolved gate keeps its direct form;
- the UNCONFIRMED POST is sent to the confirm page and writes nothing;
- the CONFIRM PAGE names what would otherwise resolve the gate and the waiters
  it releases, with the gate row's own read labels;
- the CONFIRMED POST completes the gate, records the override, and its default
  outcome says "early" and names the gate type.

The dataset is test-local, every timestamp derived from now, so a pending timer
stays pending and a lapsed one stays lapsed however long after today this runs.
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
from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.gate_completion import default_outcome, proceeds_anyway
from lithos_lens.lithos_client import LithosToolError
from lithos_lens.operator import OPERATOR_COOKIE_NAME
from lithos_lens.task_graph import BlockerRecord, EdgeRecord
from lithos_lens.tasks import TaskRecord
from lithos_lens.web import create_app
from lithos_lens.write_funnel import AUDIT_EVENT
from tests.test_complete_gate import LoggedFake

ORIGIN = "http://lens.test"
SAME_ORIGIN = {"Origin": ORIGIN}
HTMX = {**SAME_ORIGIN, "HX-Request": "true"}
OPERATOR = "dave"
PR_URL = "https://example.invalid/agent-lore/influx/pull/431"
#: The value the confirm page's form posts as ``confirm``, spelled literally:
#: it is the contract between that form and the route.
CONFIRMATION = {"confirm": "proceed-anyway"}

#: Every machine-owned gate, and its waiters (title by id).
WAITERS = {
    "gate-timer": {"wait-t1": "Promote the replica", "wait-t2": "Reindex search"},
    "gate-lapsed": {},
    "gate-ci": {"wait-c1": "Ship the build"},
    "gate-pr": {"wait-p1": "Story S7: PR #431"},
    "gate-mystery": {"wait-m1": "Whatever comes next"},
    "gate-human": {"wait-h1": "Docs sweep"},
}
MACHINE_GATES = ["gate-timer", "gate-lapsed", "gate-ci", "gate-pr", "gate-mystery"]


def _ago(**delta: float) -> datetime:
    return datetime.now(UTC) - timedelta(**delta)


def _ahead(**delta: float) -> datetime:
    return datetime.now(UTC) + timedelta(**delta)


#: Fixed for the run, so the page's stamp can be asserted against it.
TIMER_READY_AT = _ahead(days=2).replace(microsecond=0)
LAPSED_READY_AT = _ago(hours=3).replace(microsecond=0)


def _task(task_id: str, title: str, **overrides: Any) -> TaskRecord:
    fields: dict[str, Any] = {
        "status": "open",
        "created_by": "planner",
        "created_at": _ago(hours=1).isoformat(),
        "tags": ("project:influx",),
    }
    fields.update(overrides)
    return TaskRecord(id=task_id, title=title, **fields)


def _gate(
    task_id: str, title: str, gate_type: str, *, description: str = "", **meta: Any
) -> TaskRecord:
    return _task(
        task_id,
        title,
        task_type="gate",
        description=description,
        metadata={"gate_type": gate_type, **meta},
    )


def proceed_dataset(*, pr_url: str = PR_URL) -> FakeLithosDataset:
    """One gate of every machine-owned kind, a human gate, and their waiters.

    The lapsed timer has no blocked record: upstream stops counting a timer
    gate as a blocker once its ``ready_at`` has passed.
    """
    tasks = [
        _gate(
            "gate-timer",
            "Replica cooldown",
            "timer",
            ready_at=TIMER_READY_AT.isoformat(),
        ),
        _gate(
            "gate-lapsed",
            "Cache warm-up",
            "timer",
            ready_at=LAPSED_READY_AT.isoformat(),
        ),
        _gate(
            "gate-ci",
            "CI must pass",
            "ci",
            description="Resolved by the **build watcher** when `main` is green.",
        ),
        _gate("gate-pr", "PR #431 merges", "pr", pr_url=pr_url),
        _gate(
            "gate-mystery",
            "Quarterly review",
            "mystery",
            description="Closed by the compliance bot after sign-off.",
        ),
        _gate("gate-human", "Decide: ship it?", "human"),
        _task("plain", "An ordinary task"),
    ]
    edges: dict[str, list[EdgeRecord]] = {}
    blocked: dict[str, tuple[BlockerRecord, ...]] = {}
    for gate_id, waiters in WAITERS.items():
        for waiter, title in waiters.items():
            tasks.append(_task(waiter, title))
            edge = EdgeRecord(
                from_task_id=gate_id, to_task_id=waiter, type="waits_on_gate"
            )
            edges.setdefault(gate_id, []).append(replace(edge, direction="outgoing"))
            edges.setdefault(waiter, []).append(replace(edge, direction="incoming"))
            blocked[waiter] = (
                BlockerRecord(
                    kind="gate",
                    task_id=gate_id,
                    type="waits_on_gate",
                    status="open",
                    message=f"Waiting on gate {gate_id}.",
                ),
            )
    return FakeLithosDataset(
        tasks=tuple(tasks),
        ready_ids=frozenset({"plain"}),
        blocked=blocked,
        edges={task_id: tuple(rows) for task_id, rows in edges.items()},
    )


@pytest.fixture
def config(lithos_lens_config_env: Path):
    return load_config(lithos_lens_config_env)


@pytest.fixture
def fake() -> LoggedFake:
    return LoggedFake(proceed_dataset())


def _client(config, fake: LoggedFake) -> TestClient:
    app = create_app(config, lithos_client_factory=lambda _: fake)
    return TestClient(app, base_url=ORIGIN)


@pytest.fixture
def client(config, fake: LoggedFake) -> Iterator[TestClient]:
    with _client(config, fake) as test_client:
        test_client.cookies.set(OPERATOR_COOKIE_NAME, OPERATOR)
        fake.method_calls.clear()
        fake.tool_calls.clear()
        fake.write_calls.clear()
        yield test_client


def _post(
    client: TestClient,
    task_id: str,
    *,
    confirmed: bool = False,
    note: str = "",
    next_url: str = "",
    headers: dict[str, str] | None = None,
):
    data = {"expected_status": "open"}
    if confirmed:
        data.update(CONFIRMATION)
    if note:
        data["note"] = note
    if next_url:
        data["next"] = next_url
    return client.post(
        f"/tasks/{task_id}/approve",
        data=data,
        headers=SAME_ORIGIN if headers is None else headers,
        follow_redirects=False,
    )


def _text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def _complete_forms(html: str, task_id: str) -> list[str]:
    """The DIRECT Complete forms for ``task_id`` (W4's partial)."""
    return re.findall(
        rf'<form class="complete-action"[^>]*data-task-id="{re.escape(task_id)}"',
        html,
    )


def _proceed_links(html: str, task_id: str) -> list[str]:
    """The hrefs of ``task_id``'s Proceed anyway links."""
    blocks = re.findall(
        rf'data-proceed-anyway data-task-id="{re.escape(task_id)}">\s*'
        r'<a class="proceed-anyway-link" href="([^"]*)"',
        html,
    )
    return [unescape(href) for href in blocks]


def _link_target(href: str) -> tuple[str, str]:
    """A link's path and the ``next`` it carries."""
    parts = urlsplit(href)
    return parts.path, parse_qs(parts.query)["next"][0]


def _section(html: str, section: str) -> str:
    start = html.index(f'data-task-group="{section}"')
    end = html.find('<article class="task-group"', start)
    return html[start : end if end != -1 else len(html)]


def _page_section(html: str, marker: str) -> str:
    start = html.index(marker)
    return html[start : html.index("</section>", start)]


# ── where the link is offered ─────────────────────────────────────────────


@pytest.mark.parametrize("gate_id", MACHINE_GATES)
def test_a_machine_owned_gate_shows_the_link_and_no_direct_form_on_every_surface(
    client: TestClient, gate_id: str
) -> None:
    board = client.get("/tasks?project=influx").text
    gates = _section(board, "gates")
    (row_link,) = _proceed_links(gates, gate_id)
    assert _link_target(row_link) == (
        f"/tasks/{gate_id}/approve",
        "/tasks?project=influx",
    )
    assert len(_proceed_links(board, gate_id)) == 1

    panel = client.get(f"/tasks/{gate_id}?fragment=panel").text
    detail = client.get(f"/tasks/{gate_id}").text
    assert len(_proceed_links(panel, gate_id)) == 1
    (detail_link,) = _proceed_links(detail, gate_id)
    assert _link_target(detail_link) == (
        f"/tasks/{gate_id}/approve",
        f"/tasks/{gate_id}",
    )

    for page in (board, panel, detail):
        assert _complete_forms(page, gate_id) == []
        # The link is a link: no form on these surfaces carries the confirm.
        assert 'name="confirm"' not in page


def test_a_person_resolved_gate_keeps_its_direct_form_and_gets_no_link(
    client: TestClient,
) -> None:
    for url in ("/tasks", "/tasks/gate-human?fragment=panel", "/tasks/gate-human"):
        page = client.get(url).text
        assert len(_complete_forms(page, "gate-human")) == 1
        assert _proceed_links(page, "gate-human") == []


def test_no_identity_renders_no_link(client: TestClient) -> None:
    client.cookies.clear()
    for url in ("/tasks", "/tasks/gate-ci?fragment=panel", "/tasks/gate-ci"):
        assert "data-proceed-anyway-link" not in client.get(url).text


def test_the_helper_sends_every_open_gate_but_the_person_resolved_ones_here() -> None:
    for gate_type in ("timer", "ci", "pr", "", "mystery"):
        assert proceeds_anyway("gate", "open", gate_type)
    for gate_type in ("human", "external_task"):
        assert not proceeds_anyway("gate", "open", gate_type)
    assert not proceeds_anyway("gate", "completed", "timer")
    assert not proceeds_anyway("task", "open", "")


# ── the unconfirmed POST ──────────────────────────────────────────────────


@pytest.mark.parametrize("gate_id", MACHINE_GATES)
def test_an_unconfirmed_post_redirects_to_the_confirm_page_and_writes_nothing(
    client: TestClient, fake: LoggedFake, gate_id: str
) -> None:
    response = _post(client, gate_id, next_url="/tasks?project=influx")

    assert response.status_code == 303
    target = urlsplit(response.headers["location"])
    assert target.path == f"/tasks/{gate_id}/approve"
    # The return trip survives the detour, so the confirmed POST lands there.
    assert parse_qs(target.query)["next"] == ["/tasks?project=influx"]
    assert fake.write_calls == []

    htmx = _post(client, gate_id, headers=HTMX)
    assert htmx.status_code == 200
    assert urlsplit(htmx.headers["hx-redirect"]).path == f"/tasks/{gate_id}/approve"
    assert fake.write_calls == []


def test_a_confirmation_value_other_than_the_pages_is_no_confirmation(
    client: TestClient, fake: LoggedFake
) -> None:
    response = client.post(
        "/tasks/gate-ci/approve",
        data={"expected_status": "open", "confirm": "yes"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).path == "/tasks/gate-ci/approve"
    assert fake.write_calls == []


def test_a_human_gate_still_completes_directly_with_no_confirm_page(
    client: TestClient, fake: LoggedFake
) -> None:
    response = _post(client, "gate-human")

    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).path == "/tasks/gate-human"
    assert fake.write_calls == [
        (
            "lithos_task_complete",
            {
                "task_id": "gate-human",
                "agent": OPERATOR,
                "outcome": "Completed via Lens by dave",
            },
        )
    ]


# ── the confirm page ──────────────────────────────────────────────────────


def test_a_timer_gates_page_names_its_ready_at_and_the_waiters_it_releases(
    client: TestClient, fake: LoggedFake
) -> None:
    page = client.get("/tasks/gate-timer/approve?next=/tasks")

    assert page.status_code == 200
    resolver = _page_section(page.text, "data-proceed-anyway-resolver")
    assert f'datetime="{TIMER_READY_AT.isoformat()}"' in resolver
    stamp = TIMER_READY_AT.isoformat()[:16].replace("T", " ")
    assert f"This timer gate resolves itself at {stamp}" in _text(resolver)
    assert "no longer blocks" not in page.text

    waiters = _page_section(page.text, "data-proceed-anyway-waiters")
    assert "This gate blocks 2 tasks." in _text(waiters)
    assert "Promote the replica" in waiters and "Reindex search" in waiters
    assert "Whatever watches this gate will find it closed." in _text(page.text)
    assert fake.write_calls == []


def test_a_timer_already_past_ready_at_says_the_gate_no_longer_blocks_anything(
    client: TestClient,
) -> None:
    page = client.get("/tasks/gate-lapsed/approve")

    assert page.status_code == 200
    text = _text(_page_section(page.text, "data-proceed-anyway-resolver"))
    assert "The gate no longer blocks anything; completing it only closes it." in text
    assert LAPSED_READY_AT.isoformat()[:16].replace("T", " ") in text


def test_a_pr_gates_page_names_its_pr_link_and_its_waiter(client: TestClient) -> None:
    page = client.get("/tasks/gate-pr/approve")

    resolver = _page_section(page.text, "data-proceed-anyway-resolver")
    assert f'<a href="{PR_URL}"' in resolver
    waiters = _page_section(page.text, "data-proceed-anyway-waiters")
    assert "Story S7: PR #431" in waiters
    # It names the PR and the waiters and leaves the rest to the operator:
    # nothing on the page describes how the gate's author reacts.
    assert "loom" not in _text(page.text).lower()


def test_a_pr_url_that_is_not_a_web_link_is_shown_and_not_linked(config) -> None:
    fake = LoggedFake(proceed_dataset(pr_url="javascript:alert(1)"))
    with _client(config, fake) as client:
        client.cookies.set(OPERATOR_COOKIE_NAME, OPERATOR)
        page = client.get("/tasks/gate-pr/approve").text

    resolver = _page_section(page, "data-proceed-anyway-resolver")
    assert "javascript:alert(1)" in _text(resolver)
    assert 'href="javascript:' not in page


@pytest.mark.parametrize(
    ("gate_id", "description"),
    [
        ("gate-ci", "Resolved by the build watcher when main is green."),
        ("gate-mystery", "Closed by the compliance bot after sign-off."),
    ],
)
def test_a_ci_or_unknown_gates_page_shows_its_own_description(
    client: TestClient, gate_id: str, description: str
) -> None:
    page = client.get(f"/tasks/{gate_id}/approve")

    assert page.status_code == 200
    resolver = _text(_page_section(page.text, "data-proceed-anyway-resolver"))
    assert "is resolved by whatever watches it. Its description says:" in resolver
    assert description in resolver


def test_the_page_carries_the_one_form_with_the_confirmation(
    client: TestClient,
) -> None:
    page = client.get("/tasks/gate-ci/approve?next=/tasks?project=influx").text

    form = page[page.index('<form class="complete-action proceed-anyway-form"') :]
    form = form[: form.index("</form>")]
    assert 'action="/tasks/gate-ci/approve"' in form
    assert 'name="confirm" value="proceed-anyway"' in form
    assert 'name="expected_status" value="open"' in form
    assert 'name="next" value="/tasks?project=influx"' in form
    assert ">Complete anyway</button>" in form
    assert "Approve" not in form


def test_a_foreign_next_on_the_page_falls_back_to_the_gates_own_page(
    client: TestClient,
) -> None:
    page = client.get("/tasks/gate-ci/approve?next=//evil.example/x").text
    assert 'name="next" value="/tasks/gate-ci"' in page


def test_without_an_identity_the_page_states_the_facts_and_offers_no_form(
    client: TestClient,
) -> None:
    client.cookies.clear()
    page = client.get("/tasks/gate-ci/approve").text

    assert "data-proceed-anyway-waiters" in page
    assert "data-proceed-anyway-form" not in page
    assert "data-proceed-anyway-no-operator" in page


class BlockedDown(LoggedFake):
    """The blocked frontier does not answer: the row's degraded path, the
    gate's own ``waits_on_gate`` edges, labelled unverified."""

    async def task_blocked(self, **kwargs: Any):
        raise LithosToolError("did not answer", code="timeout")


class EdgesDown(LoggedFake):
    """The blocked frontier is truncated AND the edge read fails: the row's
    "at least N" from the part of the frontier that was read."""

    async def task_edge_list(self, task_id: str, **kwargs: Any):
        raise LithosToolError("did not answer", code="timeout")


class OpenListDown(LoggedFake):
    """The open list does not answer: nothing to resolve waiters against."""

    async def list_tasks(self, **kwargs: Any):
        if kwargs.get("status") == "open":
            raise LithosToolError("did not answer", code="timeout")
        return await super().list_tasks(**kwargs)


@pytest.mark.parametrize(
    ("fake_type", "frontier_limit", "label"),
    [
        pytest.param(BlockedDown, None, "blocks 2 tasks (unverified)", id="unverified"),
        pytest.param(EdgesDown, 1, "blocks at least 1 task", id="at-least"),
    ],
)
def test_the_page_carries_the_gate_rows_read_labels(
    config, fake_type: type[LoggedFake], frontier_limit: int | None, label: str
) -> None:
    """The page's count is the row's — the same read, the same qualifier."""
    if frontier_limit is not None:
        config = replace(
            config, tasks=replace(config.tasks, frontier_limit=frontier_limit)
        )
    fake = fake_type(proceed_dataset())
    with _client(config, fake) as client:
        client.cookies.set(OPERATOR_COOKIE_NAME, OPERATOR)
        page = client.get("/tasks/gate-timer/approve").text
        board = client.get("/tasks").text

    waiters = _page_section(page, "data-proceed-anyway-waiters")
    assert f"This gate {label}." in _text(waiters)
    row = board[board.index('id="task-row-gate-timer"') :]
    row = row[: row.index("</article>")]
    assert f">{label}</summary>" in row


def test_a_failed_open_list_reads_as_unavailable_never_as_zero(config) -> None:
    fake = OpenListDown(proceed_dataset())
    with _client(config, fake) as client:
        client.cookies.set(OPERATOR_COOKIE_NAME, OPERATOR)
        page = client.get("/tasks/gate-timer/approve").text

    text = _text(_page_section(page, "data-proceed-anyway-waiters"))
    assert "waiter count unavailable" in text
    assert "blocks 0" not in text


@pytest.mark.parametrize(
    ("task_id", "status", "copy"),
    [
        ("plain", 409, "This task isn't a gate — only gates can be completed here."),
        ("no-such-task", 404, "This task no longer exists."),
    ],
)
def test_a_task_with_nothing_to_confirm_says_why_and_offers_no_form(
    client: TestClient, task_id: str, status: int, copy: str
) -> None:
    page = client.get(f"/tasks/{task_id}/approve")

    assert page.status_code == status
    assert copy in _text(page.text)
    assert "data-proceed-anyway-form" not in page.text


def test_a_gate_completed_since_says_there_is_nothing_to_complete(
    client: TestClient, fake: LoggedFake
) -> None:
    asyncio.run(fake.task_complete("gate-ci", agent="agent-zero"))

    page = client.get("/tasks/gate-ci/approve")

    assert page.status_code == 409
    assert "This gate is now completed — there is nothing to complete." in _text(
        page.text
    )
    assert "data-proceed-anyway-form" not in page.text


def test_a_person_resolved_gates_confirm_url_goes_to_its_own_page(
    client: TestClient,
) -> None:
    page = client.get("/tasks/gate-human/approve", follow_redirects=False)

    assert page.status_code == 303
    assert page.headers["location"] == "/tasks/gate-human"


# ── the confirmed POST ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("gate_id", "gate_type"),
    [
        ("gate-timer", "timer"),
        ("gate-ci", "ci"),
        ("gate-pr", "pr"),
        ("gate-mystery", "mystery"),
    ],
)
def test_a_confirmed_post_completes_the_gate_and_the_outcome_says_early(
    client: TestClient, fake: LoggedFake, gate_id: str, gate_type: str
) -> None:
    response = _post(client, gate_id, confirmed=True)

    assert response.status_code == 303
    outcome = (
        f"Completed early via Lens by dave — proceed anyway; "
        f"{gate_type} gate had not resolved"
    )
    assert fake.write_calls == [
        (
            "lithos_task_complete",
            {"task_id": gate_id, "agent": OPERATOR, "outcome": outcome},
        )
    ]
    assert default_outcome(OPERATOR, gate_type) == outcome
    assert asyncio.run(fake.task_get(gate_id)).status == "completed"


def test_with_a_note_the_note_is_the_outcome(
    client: TestClient, fake: LoggedFake
) -> None:
    _post(
        client, "gate-pr", confirmed=True, note="  Merged by hand;\n watcher is down "
    )

    ((_, arguments),) = fake.write_calls
    assert arguments["outcome"] == "Merged by hand; watcher is down"


def test_an_untyped_gates_default_outcome_names_it_untyped() -> None:
    assert default_outcome("dave", "") == (
        "Completed early via Lens by dave — proceed anyway; untyped gate had not "
        "resolved"
    )


def test_the_confirmed_post_returns_to_next_with_the_unblocked_receipt(
    client: TestClient,
) -> None:
    page = client.get("/tasks/gate-timer/approve?next=/tasks?project=influx").text
    next_url = unescape(re.search(r'name="next" value="([^"]*)"', page).group(1))  # type: ignore[union-attr]

    response = _post(client, "gate-timer", confirmed=True, next_url=next_url)

    target = urlsplit(response.headers["location"])
    assert target.path == "/tasks"
    assert parse_qs(target.query)["project"] == ["influx"]
    landed = _text(client.get(response.headers["location"]).text)
    assert "Unblocked 2 tasks" in landed
    assert "Promote the replica" in landed and "Reindex search" in landed


def test_the_audit_line_and_the_span_record_the_override(
    client: TestClient,
    spans: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="lithos_lens.write_funnel")
    spans.clear()

    _post(client, "gate-pr", confirmed=True)

    (line,) = [
        record
        for record in caplog.records
        if getattr(record, "lens_event", "") == AUDIT_EVENT
    ]
    assert line.result == "ok"  # type: ignore[attr-defined]
    assert line.action == "complete"  # type: ignore[attr-defined]
    assert line.write_gate_type == "pr"  # type: ignore[attr-defined]
    assert line.write_override is True  # type: ignore[attr-defined]
    (span,) = [
        s for s in spans.get_finished_spans() if s.name == "lens.writes.complete"
    ]
    attributes = dict(span.attributes or {})
    assert attributes["lens.write.result"] == "ok"
    assert attributes["lens.write.gate_type"] == "pr"
    assert attributes["lens.write.override"] is True


def test_a_direct_completion_records_no_override(
    client: TestClient,
    spans: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="lithos_lens.write_funnel")
    spans.clear()

    _post(client, "gate-human")

    (line,) = [
        record
        for record in caplog.records
        if getattr(record, "lens_event", "") == AUDIT_EVENT
    ]
    assert line.write_override is False  # type: ignore[attr-defined]
    (span,) = [
        s for s in spans.get_finished_spans() if s.name == "lens.writes.complete"
    ]
    assert dict(span.attributes or {})["lens.write.override"] is False
