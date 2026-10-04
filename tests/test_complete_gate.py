"""T3-W4 — the write funnel, receipts, and the direct Complete action.

Asserted the way the PRD's Testing Decisions ask: on what a request returns and
on what the fake recorded (its write log, its tool-call log, and — for the
reads the fake does not log — a wrapper that logs every client method), never
on internals. Each group is the acceptance list it is named after:

- the FUNNEL: the stale pre-check, the unknown outcome, one audit line and one
  span per attempt whatever its ending, the two answer modes, no identity;
- REGISTER-ONCE, end to end through a write;
- COMPLETE: where it is offered and where it is not, what the route refuses,
  what the write records, and what the receipt and the board say afterwards;
- RECEIPTS: shown once, bounded, an unknown id renders nothing.

The dataset is test-local, with every timestamp derived from now (clarification
13): a completed gate must stay inside the board's resolved window however
long after today this suite runs. It carries the one gate type the demo
dataset has none of (``external_task``) without adding rows other tests count.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
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
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.fake_writes import write_error
from lithos_lens.gate_completion import completes_directly, default_outcome
from lithos_lens.lithos_client import LithosToolError
from lithos_lens.operator import OPERATOR_COOKIE_NAME, REFUSAL_REGISTRATION_FAILED
from lithos_lens.receipts import (
    MAX_RECEIPTS,
    RECEIPT_TTL_S,
    ReceiptStore,
    ReceiptTask,
    WriteReceipt,
    receipt_url,
)
from lithos_lens.task_graph import BlockerRecord, EdgeRecord
from lithos_lens.tasks import AgentRecord, TaskRecord
from lithos_lens.web import create_app
from lithos_lens.write_funnel import AUDIT_EVENT, RECONCILE_TRIGGER
from tests.conftest import metric_value

ORIGIN = "http://lens.test"
SAME_ORIGIN = {"Origin": ORIGIN}
HTMX = {**SAME_ORIGIN, "HX-Request": "true"}
OPERATOR = "dave"
NOTHING_CHANGED = "Nothing was changed."


# ── the dataset ──────────────────────────────────────────────────────────


def _ago(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


def _ahead(**delta: float) -> str:
    return (datetime.now(UTC) + timedelta(**delta)).isoformat()


def _task(task_id: str, title: str, **overrides: Any) -> TaskRecord:
    fields: dict[str, Any] = {
        "status": "open",
        "created_by": "planner",
        "created_at": _ago(hours=1),
        "tags": ("project:influx",),
    }
    fields.update(overrides)
    return TaskRecord(id=task_id, title=title, **fields)


def _gate(
    task_id: str, title: str, gate_type: str, *, description: str = "", **meta: Any
) -> TaskRecord:
    created_at = meta.pop("created_at", _ago(hours=1))
    return _task(
        task_id,
        title,
        task_type="gate",
        description=description,
        metadata={"gate_type": gate_type, **meta},
        created_at=created_at,
    )


def _waits(gate_id: str, waiter: str) -> tuple[EdgeRecord, EdgeRecord]:
    edge = EdgeRecord(from_task_id=gate_id, to_task_id=waiter, type="waits_on_gate")
    return replace(edge, direction="outgoing"), replace(edge, direction="incoming")


def _gate_blocker(gate_id: str) -> BlockerRecord:
    return BlockerRecord(
        kind="gate",
        task_id=gate_id,
        type="waits_on_gate",
        status="open",
        message=f"Waiting on gate {gate_id}.",
    )


#: The gates a person resolves, and their waiters (title by id).
WAITERS = {
    "gate-human": {"waiter-a": "Story S7", "waiter-b": "Docs sweep"},
    "gate-external": {"waiter-c": "Mount the new boards"},
    "gate-many": {f"fan-{n}": f"Fan-out task {n}" for n in range(1, 8)},
}


def complete_dataset() -> FakeLithosDataset:
    """Every gate type, an ordinary task, and gates whose waiters move."""
    tasks = [
        _gate(
            "gate-human",
            "Decide: re-develop PR #431?",
            "human",
            description="Complete to re-dispatch the story; cancel to drop it.",
        ),
        _gate("gate-external", "Boards arrive from the vendor", "external_task"),
        _gate("gate-many", "Release the fan-out", "human"),
        # Older than the attention threshold (rule 3), so Needs attention
        # promotes it out of the Gates section into an ordinary row.
        _gate(
            "gate-stale",
            "Approve the release notes",
            "human",
            created_at=_ago(days=3),
        ),
        _gate("gate-timer", "Replica cooldown", "timer", ready_at=_ahead(days=2)),
        _gate("gate-ci", "CI must pass", "ci"),
        _gate("gate-pr", "PR #12 merges", "pr"),
        _task("plain", "An ordinary task"),
    ]
    edges: dict[str, list[EdgeRecord]] = {}
    blocked: dict[str, tuple[BlockerRecord, ...]] = {}
    for gate_id, waiters in WAITERS.items():
        for waiter, title in waiters.items():
            tasks.append(_task(waiter, title))
            outgoing, incoming = _waits(gate_id, waiter)
            edges.setdefault(gate_id, []).append(outgoing)
            edges.setdefault(waiter, []).append(incoming)
            blocked[waiter] = (_gate_blocker(gate_id),)
    return FakeLithosDataset(
        tasks=tuple(tasks),
        ready_ids=frozenset({"plain"}),
        blocked=blocked,
        edges={task_id: tuple(rows) for task_id, rows in edges.items()},
        agents=(AgentRecord(id="agent-zero", name="Agent Zero", type="claude-code"),),
    )


class LoggedFake(FakeLithosClient):
    """The fake, logging EVERY client method a request reaches, by name.

    ``write_calls`` and ``tool_calls`` record the writes and the identity
    calls; the reads (``task_get`` above all, which mints a receipt's titles)
    are logged only here. The ``_EveryCallLogged`` pattern from
    ``tests/test_operator_identity.py``.
    """

    def __init__(self, dataset: FakeLithosDataset | None = None) -> None:
        super().__init__(None, dataset=dataset or complete_dataset())
        self.method_calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattribute__(self, name: str):
        attr = super().__getattribute__(name)
        if name.startswith("_") or not inspect.iscoroutinefunction(attr):
            return attr
        log = super().__getattribute__("method_calls")

        @functools.wraps(attr)
        async def logged(*args, **kwargs):
            log.append((name, args))
            return await attr(*args, **kwargs)

        return logged

    def reads_of(self, method: str) -> list[tuple[Any, ...]]:
        return [args for name, args in self.method_calls if name == method]


@pytest.fixture
def config(lithos_lens_config_env: Path):
    return load_config(lithos_lens_config_env)


@pytest.fixture
def fake() -> LoggedFake:
    return LoggedFake()


@pytest.fixture
def client(config, fake: LoggedFake) -> Iterator[TestClient]:
    app = create_app(config, lithos_client_factory=lambda _: fake)
    with TestClient(app, base_url=ORIGIN) as test_client:
        test_client.cookies.set(OPERATOR_COOKIE_NAME, OPERATOR)
        fake.method_calls.clear()
        fake.tool_calls.clear()
        yield test_client


def _complete(
    client: TestClient,
    task_id: str,
    *,
    expected_status: str = "open",
    note: str = "",
    next_url: str = "",
    headers: dict[str, str] | None = None,
):
    data = {"expected_status": expected_status}
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


def _text(response) -> str:
    """The response's visible text, entity-resolved and whitespace-folded."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", response.text)).split())


def _forms_for(html: str, task_id: str) -> list[str]:
    return re.findall(
        rf'<form class="complete-action"[^>]*data-task-id="{re.escape(task_id)}"',
        html,
    )


def _section(html: str, section: str) -> str:
    """One dashboard section's markup, up to the next section."""
    start = html.index(f'data-task-group="{section}"')
    end = html.find('<article class="task-group"', start)
    return html[start : end if end != -1 else len(html)]


# ── the funnel ───────────────────────────────────────────────────────────


def test_a_stale_expected_status_is_the_conflict_page_and_no_write(
    client: TestClient, fake: LoggedFake
) -> None:
    """An agent completed the gate between the operator's render and POST: the
    pre-check re-reads it, names what it is now, and sends nothing."""
    asyncio.run(fake.task_complete("gate-human", agent="agent-zero"))
    fake.write_calls.clear()

    response = _complete(client, "gate-human", expected_status="open")

    assert response.status_code == 409
    text = _text(response)
    assert "This task is now completed." in text
    assert NOTHING_CHANGED in text
    assert fake.write_calls == []


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            LithosToolError("did not answer within 10s", code="timeout"),
            id="timeout",
        ),
        pytest.param(
            LithosToolError("Lithos MCP session is not available"), id="no-session"
        ),
        # Clarification 6: an unparseable answer is still no answer — Lithos
        # may have applied the write before its reply went wrong.
        pytest.param(
            LithosToolError("non-JSON tool result", code="invalid_response"),
            id="invalid-response",
        ),
        pytest.param(OSError("connection reset"), id="transport"),
    ],
)
def test_no_answer_is_the_unknown_outcome_page_with_the_re_read(
    config, failure: Exception
) -> None:
    """The write APPLIED and the answer was lost: the page must not say it
    failed, and must say what the task is now."""

    class LostAnswer(LoggedFake):
        async def task_complete(self, task_id: str, *, agent: str, outcome: str = ""):
            await super().task_complete(task_id, agent=agent, outcome=outcome)
            raise failure

    fake = LostAnswer()
    app = create_app(config, lithos_client_factory=lambda _: fake)
    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set(OPERATOR_COOKIE_NAME, OPERATOR)
        response = _complete(client, "gate-human")

    assert response.status_code == 200
    text = _text(response)
    assert "The action may or may not have applied." in text
    assert "This task is now completed." in text
    assert NOTHING_CHANGED not in text


def _audit_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if getattr(record, "lens_event", "") == AUDIT_EVENT
    ]


def _write_spans(spans: InMemorySpanExporter) -> list[Any]:
    return [s for s in spans.get_finished_spans() if s.name == "lens.writes.complete"]


class RaceLost(LoggedFake):
    """An agent completes the gate in the window AFTER the pre-check."""

    async def task_complete(self, task_id: str, *, agent: str, outcome: str = ""):
        await FakeLithosClient.task_complete(self, task_id, agent="agent-zero")
        return await super().task_complete(task_id, agent=agent, outcome=outcome)


class InputRefused(LoggedFake):
    """Lithos refuses the write's content with an envelope."""

    async def task_complete(self, task_id: str, *, agent: str, outcome: str = ""):
        raise write_error("invalid_input", "outcome is too long.")


class NoAnswer(LoggedFake):
    async def task_complete(self, task_id: str, *, agent: str, outcome: str = ""):
        raise LithosToolError("did not answer", code="timeout")


class PrecheckDown(LoggedFake):
    async def task_get(self, task_id: str):
        raise LithosToolError("Lithos MCP session is not available")


class RegistrationDown(LoggedFake):
    def __init__(self) -> None:
        super().__init__()
        self.register_operator_fails = True


#: (case, fake, task, form overrides, operator, headers, status, result, code)
ATTEMPTS = [
    ("ok", LoggedFake, "gate-human", {}, OPERATOR, SAME_ORIGIN, 303, "ok", ""),
    (
        "stale",
        LoggedFake,
        "gate-human",
        {"expected_status": "completed"},
        OPERATOR,
        SAME_ORIGIN,
        409,
        "conflict",
        "stale_status",
    ),
    (
        "race-lost",
        RaceLost,
        "gate-human",
        {},
        OPERATOR,
        SAME_ORIGIN,
        409,
        "conflict",
        "task_not_found",
    ),
    (
        "not-a-gate",
        LoggedFake,
        "plain",
        {},
        OPERATOR,
        SAME_ORIGIN,
        409,
        "rejected",
        "not_a_gate",
    ),
    (
        "machine-gate",
        LoggedFake,
        "gate-ci",
        {},
        OPERATOR,
        SAME_ORIGIN,
        409,
        "rejected",
        "gate_type_unsupported",
    ),
    (
        "bad-form",
        LoggedFake,
        "gate-human",
        {"expected_status": ""},
        OPERATOR,
        SAME_ORIGIN,
        400,
        "rejected",
        "bad_form",
    ),
    (
        "precheck-failed",
        PrecheckDown,
        "gate-human",
        {},
        OPERATOR,
        SAME_ORIGIN,
        503,
        "rejected",
        "precheck_failed",
    ),
    (
        "identity-refused",
        LoggedFake,
        "gate-human",
        {},
        "agent-zero",
        SAME_ORIGIN,
        403,
        "rejected",
        "identity_refused",
    ),
    (
        "registration-failed",
        RegistrationDown,
        "gate-human",
        {},
        OPERATOR,
        SAME_ORIGIN,
        503,
        "rejected",
        "registration_failed",
    ),
    (
        "upstream-refusal",
        InputRefused,
        "gate-human",
        {},
        OPERATOR,
        SAME_ORIGIN,
        422,
        "rejected",
        "invalid_input",
    ),
    ("unknown", NoAnswer, "gate-human", {}, OPERATOR, SAME_ORIGIN, 200, "unknown", ""),
    (
        "foreign-origin",
        LoggedFake,
        "gate-human",
        {},
        OPERATOR,
        {"Origin": "http://evil.example"},
        403,
        "refused_origin",
        "",
    ),
    (
        "no-operator",
        LoggedFake,
        "gate-human",
        {},
        "",
        SAME_ORIGIN,
        303,
        "no_operator",
        "",
    ),
]


@pytest.mark.parametrize(
    (
        "fake_class",
        "task_id",
        "form",
        "operator",
        "headers",
        "status",
        "result",
        "code",
    ),
    [pytest.param(*case[1:], id=case[0]) for case in ATTEMPTS],
)
def test_every_attempt_leaves_one_audit_line_one_span_and_one_count(
    config,
    spans: InMemorySpanExporter,
    metric_reader,
    caplog: pytest.LogCaptureFixture,
    fake_class: type[LoggedFake],
    task_id: str,
    form: dict[str, str],
    operator: str,
    headers: dict[str, str],
    status: int,
    result: str,
    code: str,
) -> None:
    """§5C.6: refusals are attempts too — each ending is recorded exactly once,
    with the result the span, the counter and the audit line agree on."""
    fake = fake_class()
    app = create_app(config, lithos_client_factory=lambda _: fake)
    with TestClient(app, base_url=ORIGIN) as client:
        if operator:
            client.cookies.set(OPERATOR_COOKIE_NAME, operator)
        caplog.set_level(logging.INFO, logger="lithos_lens.write_funnel")
        caplog.clear()
        spans.clear()
        response = _complete(client, task_id, headers=headers, **form)

    assert response.status_code == status
    lines = _audit_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert line.result == result  # type: ignore[attr-defined]
    assert line.code == code  # type: ignore[attr-defined]
    assert line.action == "complete"  # type: ignore[attr-defined]
    assert line.task_id == task_id  # type: ignore[attr-defined]
    assert line.operator == (operator if result != "refused_origin" else "")  # type: ignore[attr-defined]
    # The argument summary is lengths, never free text.
    assert line.arguments == {"task_id": task_id, "note_chars": 0}  # type: ignore[attr-defined]
    written = _write_spans(spans)
    assert len(written) == 1
    attributes = dict(written[0].attributes or {})
    assert attributes["lens.write.result"] == result
    assert attributes.get("lens.write.code", "") == code
    assert (
        metric_value(
            metric_reader, "lens_writes_total", action="complete", result=result
        ).value
        == 1
    )
    if result == "ok":
        assert attributes["lens.write.gate_type"] == "human"
        assert attributes["lens.write.override"] is False
        assert attributes["lens.write.operator"] == OPERATOR


def test_the_audit_line_records_the_status_expected_and_observed(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="lithos_lens.write_funnel")
    _complete(client, "gate-human", expected_status="cancelled", note="looks fine")

    (line,) = _audit_lines(caplog)
    assert line.expected_status == "cancelled"  # type: ignore[attr-defined]
    assert line.observed_status == "open"  # type: ignore[attr-defined]
    # The note's LENGTH, never its words.
    assert line.arguments == {"task_id": "gate-human", "note_chars": 10}  # type: ignore[attr-defined]
    assert "looks fine" not in caplog.text


def test_a_plain_post_answers_303_and_the_target_renders_the_receipt(
    client: TestClient, fake: LoggedFake
) -> None:
    response = _complete(client, "gate-human", next_url="/tasks?project=influx")

    assert response.status_code == 303
    target = urlsplit(response.headers["location"])
    assert target.path == "/tasks"
    query = parse_qs(target.query)
    assert query["project"] == ["influx"]
    (receipt_id,) = query["receipt"]

    page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert "data-write-receipt " in page.text
    text = _text(page)
    assert "Completed gate “Decide: re-develop PR #431?”" in text
    assert "Unblocked 2 tasks" in text
    assert "Story S7" in text and "Docs sweep" in text


def test_without_next_the_redirect_lands_on_the_tasks_detail_page(
    client: TestClient,
) -> None:
    response = _complete(client, "gate-human")

    target = urlsplit(response.headers["location"])
    assert target.path == "/tasks/gate-human"
    assert "receipt" in parse_qs(target.query)


def test_a_foreign_next_is_ignored_for_the_tasks_own_page(client: TestClient) -> None:
    response = _complete(client, "gate-human", next_url="//evil.example/steal")

    assert urlsplit(response.headers["location"]).path == "/tasks/gate-human"


def test_an_htmx_post_answers_the_receipt_fragment_with_the_reconcile_trigger(
    client: TestClient, fake: LoggedFake
) -> None:
    response = _complete(client, "gate-human", headers=HTMX)

    assert response.status_code == 200
    assert response.headers["hx-trigger"] == RECONCILE_TRIGGER
    assert response.text.lstrip().startswith('<section class="write-receipt"')
    assert "<html" not in response.text
    assert "Unblocked 2 tasks" in _text(response)
    assert [call[0] for call in fake.write_calls] == ["lithos_task_complete"]


def test_an_htmx_refusal_answers_200_with_its_copy_and_the_trigger(
    client: TestClient, fake: LoggedFake
) -> None:
    """htmx swaps no 4xx body, so a conflict answered 409 would show the
    operator nothing; the fragment carries the full page's copy instead."""
    asyncio.run(fake.task_complete("gate-human", agent="agent-zero"))
    fake.write_calls.clear()

    response = _complete(client, "gate-human", headers=HTMX)

    assert response.status_code == 200
    assert response.headers["hx-trigger"] == RECONCILE_TRIGGER
    text = _text(response)
    assert NOTHING_CHANGED in text
    assert "This task is now completed." in text
    assert "<html" not in response.text
    assert fake.write_calls == []


def test_a_post_without_an_identity_goes_to_the_operator_page_and_writes_nothing(
    client: TestClient, fake: LoggedFake
) -> None:
    client.cookies.clear()

    response = _complete(client, "gate-human", next_url="/tasks?project=influx")

    assert response.status_code == 303
    target = urlsplit(response.headers["location"])
    assert target.path == "/operator"
    # Back to where the operator was — not to the POST's own path.
    assert parse_qs(target.query)["next"] == ["/tasks?project=influx"]
    assert fake.write_calls == []
    assert fake.tool_calls == []

    htmx = _complete(client, "gate-human", headers=HTMX)
    assert htmx.status_code == 200
    assert urlsplit(htmx.headers["hx-redirect"]).path == "/operator"
    assert fake.write_calls == []


def test_without_next_the_operator_page_returns_to_the_tasks_page(
    client: TestClient,
) -> None:
    client.cookies.clear()

    response = _complete(client, "gate-human")

    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["next"] == ["/tasks/gate-human"]


# ── register-once, end to end ────────────────────────────────────────────


def test_the_first_write_registers_the_operator_once_and_the_second_does_not(
    client: TestClient, fake: LoggedFake
) -> None:
    first = _complete(client, "gate-human")
    second = _complete(client, "gate-external")

    assert first.status_code == second.status_code == 303
    registrations = [
        call for call in fake.tool_calls if call[0] == "lithos_agent_register"
    ]
    assert registrations == [
        ("lithos_agent_register", {"id": OPERATOR, "type": "human"})
    ]
    assert [args["agent"] for _, args in fake.write_calls] == [OPERATOR, OPERATOR]


def test_a_failed_registration_refuses_the_write_and_says_nothing_changed(
    config,
) -> None:
    fake = RegistrationDown()
    app = create_app(config, lithos_client_factory=lambda _: fake)
    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set(OPERATOR_COOKIE_NAME, OPERATOR)
        response = _complete(client, "gate-human")

    assert response.status_code == 503
    text = _text(response)
    assert NOTHING_CHANGED in text
    assert REFUSAL_REGISTRATION_FAILED in text
    assert fake.write_calls == []


# ── Complete: where it is offered ────────────────────────────────────────


def test_the_helper_offers_complete_on_open_person_resolved_gates_only() -> None:
    assert completes_directly("gate", "open", "human")
    assert completes_directly("gate", "open", "external_task")
    for gate_type in ("timer", "ci", "pr", "", "mystery"):
        assert not completes_directly("gate", "open", gate_type)
    assert not completes_directly("gate", "completed", "human")
    assert not completes_directly("task", "open", "human")


@pytest.mark.parametrize("gate_id", ["gate-human", "gate-external"])
def test_an_open_person_resolved_gate_offers_one_complete_action_on_every_surface(
    client: TestClient, gate_id: str
) -> None:
    board = client.get("/tasks")
    gates = _section(board.text, "gates")
    assert len(_forms_for(gates, gate_id)) == 1
    assert len(_forms_for(board.text, gate_id)) == 1

    panel = client.get(f"/tasks/{gate_id}?fragment=panel")
    assert len(_forms_for(panel.text, gate_id)) == 1

    detail = client.get(f"/tasks/{gate_id}")
    assert len(_forms_for(detail.text, gate_id)) == 1

    for page in (board, panel, detail):
        form = page.text[page.text.index(_forms_for(page.text, gate_id)[0]) :]
        form = form[: form.index("</form>")]
        # The button says Complete, never Approve; the identity is beside it.
        assert ">Complete</button>" in form
        assert "Approve" not in form
        assert "data-operator-id>dave<" in form
        assert 'name="expected_status" value="open"' in form


def test_the_gate_rows_action_posts_by_htmx_and_the_others_by_plain_form(
    client: TestClient,
) -> None:
    board = client.get("/tasks?project=influx").text
    row_form = board[board.index(_forms_for(board, "gate-human")[0]) :]
    assert 'hx-post="/tasks/gate-human/approve"' in row_form.split(">", 1)[0]
    assert 'hx-target="#write-receipt"' in row_form.split(">", 1)[0]
    # The no-JS path returns to this very board, filters kept.
    assert 'name="next" value="/tasks?project=influx"' in row_form

    detail = client.get("/tasks/gate-human").text
    detail_form = detail[detail.index(_forms_for(detail, "gate-human")[0]) :]
    assert "hx-post" not in detail_form.split(">", 1)[0]


def test_the_gate_description_is_shown_beside_the_action(client: TestClient) -> None:
    detail = client.get("/tasks/gate-human")
    assert "Complete to re-dispatch the story; cancel to drop it." in _text(detail)
    form = detail.text[detail.text.index(_forms_for(detail.text, "gate-human")[0]) :]
    assert "It means what the gate's description says" in form[: form.index("</form>")]


def test_a_human_gate_promoted_into_needs_attention_carries_the_same_action(
    client: TestClient,
) -> None:
    """Clarification 1: the promoted row renders through tasks/row.html, and
    it offers exactly what its Gates-section row would."""
    board = client.get("/tasks").text
    attention = _section(board, "attention")

    assert 'data-task-id="gate-stale"' in attention
    assert len(_forms_for(attention, "gate-stale")) == 1
    assert len(_forms_for(board, "gate-stale")) == 1


@pytest.mark.parametrize(
    "task_id", ["plain", "gate-timer", "gate-ci", "gate-pr", "waiter-a"]
)
def test_no_complete_action_on_ordinary_tasks_or_machine_owned_gates(
    client: TestClient, task_id: str
) -> None:
    for url in ("/tasks", f"/tasks/{task_id}?fragment=panel", f"/tasks/{task_id}"):
        assert _forms_for(client.get(url).text, task_id) == []


def test_no_identity_renders_no_action_and_only_the_chromes_one_prompt(
    client: TestClient,
) -> None:
    client.cookies.clear()
    board = client.get("/tasks").text

    assert 'class="complete-action"' not in board
    assert board.count("choose an operator to act") == 1


# ── Complete: what the route refuses ─────────────────────────────────────


def test_completing_a_plain_task_by_url_is_409_and_writes_nothing(
    client: TestClient, fake: LoggedFake
) -> None:
    response = _complete(client, "plain")

    assert response.status_code == 409
    text = _text(response)
    assert "This task isn't a gate — only gates can be completed here." in text
    assert NOTHING_CHANGED in text
    assert fake.write_calls == []


@pytest.mark.parametrize("gate_id", ["gate-timer", "gate-ci", "gate-pr"])
def test_a_machine_owned_gate_posted_to_the_route_is_refused_with_no_write(
    client: TestClient, fake: LoggedFake, gate_id: str
) -> None:
    response = _complete(client, gate_id)

    assert response.status_code == 409
    assert "is resolved by whatever watches it" in _text(response)
    assert NOTHING_CHANGED in _text(response)
    assert fake.write_calls == []


def test_a_missing_task_is_the_conflict_page(
    client: TestClient, fake: LoggedFake
) -> None:
    response = _complete(client, "no-such-task")

    assert response.status_code == 409
    assert "This task no longer exists." in _text(response)
    assert fake.write_calls == []


# ── Complete: what it records, and what the board says afterwards ────────


def test_the_note_becomes_the_outcome(client: TestClient, fake: LoggedFake) -> None:
    _complete(client, "gate-human", note="  Re-run it:\n the flake is fixed  ")

    assert fake.write_calls == [
        (
            "lithos_task_complete",
            {
                "task_id": "gate-human",
                "agent": OPERATOR,
                # One line: whitespace runs folded, ends trimmed.
                "outcome": "Re-run it: the flake is fixed",
            },
        )
    ]


def test_with_no_note_the_outcome_names_the_operator(
    client: TestClient, fake: LoggedFake
) -> None:
    _complete(client, "gate-external")

    ((_, arguments),) = fake.write_calls
    assert arguments["outcome"] == "Completed via Lens by dave"
    assert arguments["outcome"] == default_outcome(OPERATOR)


def test_the_receipt_names_the_first_five_released_titles_and_counts_the_rest(
    client: TestClient, fake: LoggedFake
) -> None:
    """Clarification 5: titles are read at mint time, at most five of them —
    one ``task_get`` for the pre-check plus one per named release."""
    # The fragment IS the receipt: the detail page under a banner would list
    # every waiter anyway, in its own Blocks section.
    response = _complete(client, "gate-many", headers=HTMX)
    reads = fake.reads_of("task_get")

    assert len(reads) == 1 + 5
    page = _text(response)
    assert "Unblocked 7 tasks" in page
    for n in range(1, 6):
        assert f"Fan-out task {n}" in page
    assert "Fan-out task 6" not in page
    assert "and 2 more" in page


def test_a_release_whose_title_cannot_be_read_shows_its_short_id_alone(
    config,
) -> None:
    class TitleUnreadable(LoggedFake):
        async def task_get(self, task_id: str):
            if task_id == "waiter-b":
                raise LithosToolError("Lithos MCP session is not available")
            return await super().task_get(task_id)

    fake = TitleUnreadable()
    app = create_app(config, lithos_client_factory=lambda _: fake)
    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set(OPERATOR_COOKIE_NAME, OPERATOR)
        response = _complete(client, "gate-human", headers=HTMX)

    released = re.findall(
        r'<li data-receipt-released-task="([^"]+)">(.*?)</li>', response.text, re.S
    )
    by_id = {task_id: body for task_id, body in released}
    assert "Story S7" in by_id["waiter-a"]
    assert "<a " not in by_id["waiter-b"]
    assert 'title="waiter-b"' in by_id["waiter-b"]


def test_after_the_write_the_board_shows_the_waiters_ready(
    client: TestClient,
) -> None:
    before = client.get("/tasks").text
    assert 'data-task-id="waiter-a"' not in _section(before, "ready")

    location = _complete(client, "gate-human").headers["location"]
    after = client.get(location.replace("/tasks/gate-human", "/tasks")).text

    ready = _section(after, "ready")
    assert 'data-task-id="waiter-a"' in ready
    assert 'data-task-id="waiter-b"' in ready
    assert _forms_for(after, "gate-human") == []


# ── receipts ─────────────────────────────────────────────────────────────


def test_an_unknown_receipt_id_renders_no_banner_and_no_error(
    client: TestClient,
) -> None:
    for url in (
        "/tasks?receipt=nope",
        "/tasks/plain?receipt=nope",
        "/knowledge?receipt=nope",
    ):
        response = client.get(url)
        assert response.status_code == 200
        assert "data-write-receipt " not in response.text


def test_a_receipt_is_shown_once(client: TestClient) -> None:
    location = _complete(client, "gate-human").headers["location"]

    assert "data-write-receipt " in client.get(location).text
    assert "data-write-receipt " not in client.get(location).text


def test_any_read_page_accepts_a_receipt(client: TestClient) -> None:
    location = _complete(client, "gate-human", next_url="/knowledge").headers[
        "location"
    ]

    assert urlsplit(location).path == "/knowledge"
    assert "Unblocked 2 tasks" in _text(client.get(location))


def _receipt(title: str = "Gate") -> WriteReceipt:
    return WriteReceipt(
        action="complete", task=ReceiptTask("g", title), operator=OPERATOR
    )


def test_an_expired_receipt_renders_nothing() -> None:
    now = [1000.0]
    store = ReceiptStore(clock=lambda: now[0])
    receipt_id = store.put(_receipt())

    now[0] += RECEIPT_TTL_S + 1
    assert store.take(receipt_id) is None


def test_a_receipt_inside_its_ttl_is_taken_once() -> None:
    now = [1000.0]
    store = ReceiptStore(clock=lambda: now[0])
    receipt_id = store.put(_receipt())

    now[0] += RECEIPT_TTL_S - 1
    assert store.take(receipt_id) == _receipt()
    assert store.take(receipt_id) is None


def test_the_store_is_bounded_and_drops_the_oldest_first() -> None:
    store = ReceiptStore()
    first = store.put(_receipt("first"))
    later = [store.put(_receipt(str(n))) for n in range(MAX_RECEIPTS)]

    assert len(store) == MAX_RECEIPTS
    assert store.take(first) is None
    assert store.take(later[-1]) == _receipt(str(MAX_RECEIPTS - 1))


@pytest.mark.parametrize(
    ("next_url", "expected"),
    [
        ("/tasks", "/tasks?receipt=R"),
        ("/tasks?project=influx", "/tasks?project=influx&receipt=R"),
        # An earlier write's receipt is REPLACED, never doubled.
        ("/tasks?receipt=old&project=a+b", "/tasks?project=a+b&receipt=R"),
        (
            "/tasks/x?selected=y#task-group-gates",
            "/tasks/x?selected=y&receipt=R#task-group-gates",
        ),
    ],
)
def test_the_receipt_id_is_merged_into_next_with_a_url_parser(
    next_url: str, expected: str
) -> None:
    assert receipt_url(next_url, "R") == expected
