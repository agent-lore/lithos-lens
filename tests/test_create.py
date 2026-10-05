"""T3-W7 — Create a task, epic or gate: one form, both project conventions,
de-duplicated on a request id.

Asserted the way the PRD's Testing Decisions ask: on what a request returns and
on what the fake recorded (its write log, and the method log the W4 suite's
``LoggedFake`` keeps), never on internals. The groups:

- the FORM MODEL (``create_form``) — validation, both conventions, the gate
  rules, and where an upstream refusal is placed — tested without a browser;
- the COORDINATOR (``create_coordinator``) directly (D17): two concurrent
  submits make one create; a create that times out and lands later stays
  remembered as unknown; a created id lands on its task; a refused id is
  forgotten;
- the ROUTES: the form and its pre-fill, the affordances, the create through
  the funnel and what it records, the refusals, and de-duplication end to end
  — concurrent POSTs, a resubmit, and the unknown outcome with Start again.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Iterator
from dataclasses import replace
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

import lithos_lens.create_routes as create_routes
from lithos_lens.config import load_config
from lithos_lens.create_coordinator import (
    CreateCoordinator,
    Created,
    OutcomeUnknown,
    Refused,
)
from lithos_lens.create_form import (
    CREATABLE_GATE_TYPES,
    CreateInput,
    new_request_id,
    place_problem,
    validate,
)
from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.fake_writes import write_error
from lithos_lens.gates import KNOWN_GATE_TYPES
from lithos_lens.lithos_client import LithosToolError
from lithos_lens.operator import OPERATOR_COOKIE_NAME
from lithos_lens.tasks import AgentRecord, TaskRecord
from lithos_lens.web import create_app
from lithos_lens.write_errors import map_write_error
from lithos_lens.write_funnel import AUDIT_EVENT
from tests.conftest import metric_points, metric_value
from tests.test_complete_gate import LoggedFake

ORIGIN = "http://lens.test"
SAME_ORIGIN = {"Origin": ORIGIN}
OPERATOR = "dave"

EPIC = "influx-epic"
PREDECESSOR = "influx-ingest-cutover"
#: Two ids sharing the six-character prefix ``506155`` (the contract's own).
TWIN_A = "50615540-daf2-4622-afd7-787efcd6af62"
TWIN_B = "5061554c-c2eb-43ff-9f9a-f45d3376a597"

NOT_VISIBLE = "Lens could not confirm this task was created — it is not visible yet."


def _task(task_id: str, title: str, **overrides: Any) -> TaskRecord:
    fields: dict[str, Any] = {
        "status": "open",
        "created_by": "planner",
        "created_at": "2026-09-30T09:00:00+00:00",
        "tags": ("project:influx",),
    }
    fields.update(overrides)
    return TaskRecord(id=task_id, title=title, **fields)


def create_dataset() -> FakeLithosDataset:
    return FakeLithosDataset(
        tasks=(
            _task(EPIC, "Influx store migration", task_type="epic"),
            _task(PREDECESSOR, "Cut ingest over"),
            _task(TWIN_A, "Rebuild the Influx ingest path"),
            _task(TWIN_B, "Retire the old Influx writer"),
            _task(
                "loom-epic",
                "Loom lifecycle",
                task_type="epic",
                tags=(),
                metadata={"project": "lithos-loom"},
            ),
            _task(
                "done-epic",
                "Shipped epic",
                task_type="epic",
                status="completed",
                resolved_at="2026-10-01T09:00:00+00:00",
            ),
        ),
        ready_ids=frozenset({EPIC, PREDECESSOR, TWIN_A, TWIN_B, "loom-epic"}),
    )


@pytest.fixture
def config(lithos_lens_config_env: Path):
    return load_config(lithos_lens_config_env)


@pytest.fixture
def fake() -> LoggedFake:
    return LoggedFake(create_dataset())


def _client(config, fake: FakeLithosClient, *, operator: str = OPERATOR) -> TestClient:
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


def _text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def _request_id(html: str) -> str:
    match = re.search(r'name="request_id" value="([^"]*)"', html)
    assert match, "the form carries no request id"
    return match.group(1)


def _field_value(html: str, name: str) -> str:
    match = re.search(rf'<input[^>]*name="{name}"[^>]*value="([^"]*)"', html)
    if match:
        return unescape(match.group(1))
    match = re.search(rf'<textarea[^>]*name="{name}"[^>]*>(.*?)</textarea>', html, re.S)
    assert match, f"no field {name}"
    return unescape(match.group(1))


def _error(html: str, name: str) -> str:
    match = re.search(
        rf'<div class="create-field-error"[^>]*data-create-error="{name}">(.*?)</div>',
        html,
        re.S,
    )
    return _text(match.group(1)) if match else ""


def _form(**fields: str) -> dict[str, str]:
    data = {"title": "Swap reads onto the new store", "task_type": "task"}
    data.update(fields)
    data.setdefault("request_id", new_request_id())
    return data


def _post(client: TestClient, data: dict[str, str], **kwargs: Any):
    return client.post(
        "/tasks/new", data=data, headers=SAME_ORIGIN, follow_redirects=False, **kwargs
    )


def _creates(fake: FakeLithosClient) -> list[dict[str, Any]]:
    return [args for tool, args in fake.write_calls if tool == "lithos_task_create"]


def _write_counts(reader: InMemoryMetricReader) -> dict[tuple[str, str], int]:
    """Every ``lens_writes_total`` label set and its value — so an extra label
    (a dedup marker, say) shows up as a set of its own."""
    counts: dict[tuple[str, str], int] = {}
    for point in metric_points(reader, "lens_writes_total"):
        labels = dict(point.attributes or {})
        assert set(labels) == {"action", "result"}, labels
        counts[(str(labels["action"]), str(labels["result"]))] = point.value
    return counts


def _create_spans(spans: InMemorySpanExporter) -> list[dict[str, Any]]:
    return [
        dict(span.attributes or {})
        for span in spans.get_finished_spans()
        if span.name == "lens.writes.create"
    ]


def _receipt_id(response) -> str:
    return parse_qs(urlsplit(response.headers["location"]).query)["receipt"][0]


class _Controls(HTMLParser):
    """The successful controls of ONE form on a page, as a browser posts them."""

    def __init__(self, marker: str) -> None:
        super().__init__()
        self.marker = marker
        self.inside = False
        self.controls: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form":
            self.inside = self.marker in values
        elif tag == "input" and self.inside and values.get("name"):
            self.controls.append((values["name"] or "", values.get("value") or ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.inside = False


def _form_controls(html: str, marker: str) -> dict[str, str]:
    parser = _Controls(marker)
    parser.feed(html)
    controls = dict(parser.controls)
    assert len(controls) == len(parser.controls), "a control is posted twice"
    return controls


def _selected(html: str, name: str) -> str:
    select = re.search(rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>', html, re.S)
    assert select, f"no select {name}"
    chosen = re.findall(r'<option value="([^"]*)" selected>', select.group(1))
    assert len(chosen) == 1, chosen
    return chosen[0]


def _landed_on(response) -> str:
    assert response.status_code == 303, response.text
    location = urlsplit(response.headers["location"])
    assert location.path.startswith("/tasks/")
    assert "receipt" in parse_qs(location.query)
    return location.path.removeprefix("/tasks/")


# ── the form model ──────────────────────────────────────────────────────


def _typed(**fields: str) -> CreateInput:
    return CreateInput(**{"title": "A title", "request_id": new_request_id(), **fields})


def test_the_project_is_written_under_both_conventions_with_the_configured_key() -> (
    None
):
    typed = _typed(project="influx", tags="area:data\nproj:influx")

    request = validate(typed, project_tag_key="proj").request

    assert request is not None
    assert request.metadata["project"] == "influx"
    # The configured key, and no duplicate when the operator typed it too.
    assert request.tags == ("area:data", "proj:influx")
    assert request.metadata["lens_request_id"] == typed.request_id


def test_metadata_is_the_project_the_request_id_and_the_gate_nothing_else() -> None:
    gate = validate(
        _typed(task_type="gate", gate_type="timer", ready_at="2026-10-09T09:00"),
        project_tag_key="project",
    ).request
    plain = validate(
        # The no-JS form posts the gate fieldset whatever type was chosen.
        _typed(task_type="task", gate_type="timer", ready_at="2026-10-09T09:00"),
        project_tag_key="project",
    ).request

    assert gate is not None and plain is not None
    # A datetime-local value is read as UTC (D10), stored as ISO with offset.
    assert dict(gate.metadata) == {
        "gate_type": "timer",
        "ready_at": "2026-10-09T09:00:00+00:00",
        "lens_request_id": gate.request_id,
    }
    assert dict(plain.metadata) == {"lens_request_id": plain.request_id}
    assert plain.tags == ()


def test_a_past_ready_at_is_allowed_it_is_simply_already_ready() -> None:
    request = validate(
        _typed(task_type="gate", gate_type="timer", ready_at="2020-01-01T00:00"),
        project_tag_key="project",
    ).request
    assert request is not None


@pytest.mark.parametrize(
    ("fields", "field"),
    [
        pytest.param({"title": "   "}, "title", id="blank-title"),
        pytest.param({"task_type": "milestone"}, "task_type", id="unknown-type"),
        pytest.param({"project": "Influx Store"}, "project", id="project-not-a-slug"),
        pytest.param(
            {"task_type": "gate", "gate_type": "timer"},
            "ready_at",
            id="timer-no-ready-at",
        ),
        pytest.param(
            {"task_type": "gate", "gate_type": "timer", "ready_at": "soon"},
            "ready_at",
            id="timer-unparseable",
        ),
        pytest.param({"task_type": "gate", "gate_type": "ci"}, "gate_type", id="ci"),
        pytest.param({"task_type": "gate", "gate_type": "pr"}, "gate_type", id="pr"),
        pytest.param(
            {"task_type": "gate", "gate_type": "bogus"}, "gate_type", id="unknown-gate"
        ),
        pytest.param({"task_type": "gate"}, "gate_type", id="no-gate-type"),
    ],
)
def test_lens_refuses_before_any_call(fields: dict[str, str], field: str) -> None:
    validated = validate(_typed(**fields), project_tag_key="project")
    assert validated.request is None
    assert field in validated.errors


def test_the_creatable_gate_types_are_a_policy_subset_of_the_known_ones() -> None:
    assert set(CREATABLE_GATE_TYPES) <= KNOWN_GATE_TYPES
    assert "ci" not in CREATABLE_GATE_TYPES and "pr" not in CREATABLE_GATE_TYPES


def test_tags_and_predecessors_are_one_per_line() -> None:
    request = validate(
        _typed(tags="a, b\r\n\n c \na, b", predecessors=f"{PREDECESSOR}\n506155\n"),
        project_tag_key="project",
    ).request
    assert request is not None
    # A comma can be part of a tag; blanks and repeats drop, order holds.
    assert request.tags == ("a, b", "c")
    assert request.depends_on == (PREDECESSOR, "506155")


def test_the_audit_summary_carries_no_free_text() -> None:
    request = validate(
        _typed(title="Secret plan", description="Nobody may read this"),
        project_tag_key="project",
    ).request
    assert request is not None
    summary = request.arguments()
    assert "Secret plan" not in repr(summary)
    assert "Nobody may read this" not in repr(summary)
    assert summary["title_chars"] == len("Secret plan")
    assert summary["description_chars"] == len("Nobody may read this")


@pytest.mark.parametrize(
    ("message", "field"),
    [
        ("parent_task_id 'infl' is too short: pass the full task id.", "parent"),
        ("No task matches id prefix 'influx-nope' (depends_on).", "predecessors"),
        (
            "a 'timer' gate requires a parseable metadata.ready_at (ISO datetime), "
            "got 'soon'.",
            "ready_at",
        ),
        (
            "a gate task requires metadata.gate_type in ['ci', 'external_task', "
            "'human', 'pr', 'timer'], got None.",
            "gate_type",
        ),
    ],
)
def test_an_upstream_refusal_lands_on_the_input_it_names(
    message: str, field: str
) -> None:
    problem = map_write_error(
        "create", {"status": "error", "code": "invalid_input", "message": message}
    )
    if "No task matches" in message:
        problem = map_write_error(
            "create", {"status": "error", "code": "task_not_found", "message": message}
        )

    _, errors = place_problem(_typed(), problem)

    assert list(errors) == [field]
    assert errors[field].message == message


def test_ambiguous_candidates_go_under_the_field_the_prefix_was_typed_in() -> None:
    problem = map_write_error(
        "create",
        {
            "status": "error",
            "code": "ambiguous_id_prefix",
            "message": "Task id prefix '506155' (depends_on) is ambiguous.",
            "candidates": [
                {"id": TWIN_A, "title": "Rebuild the Influx ingest path"},
                {"id": TWIN_B, "title": "Retire the old Influx writer"},
            ],
        },
    )

    notice, errors = place_problem(
        _typed(parent=EPIC, predecessors=f"{PREDECESSOR}\n506155"), problem
    )

    assert list(errors) == ["predecessors"]
    assert [ref.task_id for ref in errors["predecessors"].candidates] == [
        TWIN_A,
        TWIN_B,
    ]
    # Not also offered as links away from the form.
    assert notice.candidates == ()


# ── the coordinator (D17) ───────────────────────────────────────────────


def test_two_concurrent_submits_make_one_create() -> None:
    async def scenario() -> tuple[list[Any], int]:
        coordinator = CreateCoordinator()
        calls = 0
        started = asyncio.Event()
        release = asyncio.Event()

        async def create():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return Created(task_id="new-1", title="T")

        first = asyncio.create_task(coordinator.submit("r1", create))
        await started.wait()
        second = asyncio.create_task(coordinator.submit("r1", create))
        await asyncio.sleep(0)  # the second reaches the in-flight entry
        release.set()
        return [await first, await second], calls

    (first, second), calls = asyncio.run(scenario())
    assert calls == 1
    assert first.outcome == second.outcome == Created(task_id="new-1", title="T")
    assert (first.dedup, second.dedup) == ("", "joined")


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(Refused(envelope={"code": "invalid_input"}), id="refused"),
        pytest.param(OutcomeUnknown(), id="unknown"),
    ],
)
def test_a_joined_waiter_receives_the_first_calls_outcome_whatever_it_is(
    outcome: Any,
) -> None:
    """In flight, a refusal and an unknown outcome are shared like a success;
    once settled, they part: a refused id may be sent again (nothing was
    created), an unknown one never."""

    async def scenario() -> tuple[list[Any], int, Any, int]:
        coordinator = CreateCoordinator()
        calls = 0
        started, release = asyncio.Event(), asyncio.Event()

        async def held():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return outcome

        first = asyncio.create_task(coordinator.submit("r1", held))
        await started.wait()
        second = asyncio.create_task(coordinator.submit("r1", held))
        await asyncio.sleep(0)
        release.set()
        settled = [await first, await second]
        in_flight_calls = calls

        async def corrected():
            nonlocal calls
            calls += 1
            return Created(task_id="new-1", title="T")

        after = await coordinator.submit("r1", corrected)
        return settled, in_flight_calls, after, calls

    (first, second), in_flight_calls, after, calls = asyncio.run(scenario())
    assert in_flight_calls == 1
    assert first.outcome == second.outcome == outcome
    assert (first.dedup, second.dedup) == ("", "joined")
    if isinstance(outcome, Refused):
        assert calls == 2 and after.outcome == Created(task_id="new-1", title="T")
    else:
        assert calls == 1
        assert after.outcome == OutcomeUnknown() and after.dedup == "remembered"


def test_a_waiter_going_away_does_not_cancel_the_create() -> None:
    async def scenario() -> Any:
        coordinator = CreateCoordinator()
        started, release = asyncio.Event(), asyncio.Event()

        async def create():
            started.set()
            await release.wait()
            return Created(task_id="new-1", title="T")

        leader = asyncio.create_task(coordinator.submit("r1", create))
        await started.wait()
        leader.cancel()
        await asyncio.sleep(0)
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return coordinator.remembered("r1")

    assert asyncio.run(scenario()) == Created(task_id="new-1", title="T")


def test_a_create_that_times_out_and_lands_later_is_never_sent_again() -> None:
    async def scenario() -> tuple[Any, Any, int]:
        coordinator = CreateCoordinator()
        calls = 0

        async def create():
            nonlocal calls
            calls += 1
            return OutcomeUnknown()

        first = await coordinator.submit("r1", create)
        # ...upstream lands it now; a lookup would still prove nothing.
        second = await coordinator.submit("r1", create)
        return first, second, calls

    first, second, calls = asyncio.run(scenario())
    assert calls == 1
    assert first.outcome == second.outcome == OutcomeUnknown()
    assert second.dedup == "remembered"


def test_a_remembered_created_id_lands_on_its_task() -> None:
    async def scenario() -> tuple[Any, int]:
        coordinator = CreateCoordinator()
        calls = 0

        async def create():
            nonlocal calls
            calls += 1
            return Created(task_id="new-1", title="T")

        await coordinator.submit("r1", create)
        return await coordinator.submit("r1", create), calls

    settled, calls = asyncio.run(scenario())
    assert calls == 1
    assert settled.outcome == Created(task_id="new-1", title="T")
    assert settled.dedup == "remembered"


def test_a_refused_id_is_not_remembered() -> None:
    async def scenario() -> tuple[Any, int]:
        coordinator = CreateCoordinator()
        outcomes = [Refused(envelope={"code": "invalid_input"}), Created("new-1", "T")]
        calls = 0

        async def create():
            nonlocal calls
            calls += 1
            return outcomes.pop(0)

        await coordinator.submit("r1", create)
        assert coordinator.remembered("r1") is None
        return await coordinator.submit("r1", create), calls

    settled, calls = asyncio.run(scenario())
    assert calls == 2
    assert settled.outcome == Created("new-1", "T") and settled.dedup == ""


def test_the_map_is_bounded_oldest_settled_first() -> None:
    async def scenario() -> CreateCoordinator:
        coordinator = CreateCoordinator(max_remembered=2)
        for request_id in ("r1", "r2", "r3"):

            async def create(request_id: str = request_id):
                return Created(task_id=f"t-{request_id}", title="T")

            await coordinator.submit(request_id, create)
        return coordinator

    coordinator = asyncio.run(scenario())
    assert len(coordinator) == 2
    assert coordinator.remembered("r1") is None
    assert coordinator.remembered("r3") == Created("t-r3", "T")


# ── the form page and its affordances ──────────────────────────────────


def test_the_form_has_the_gate_fieldset_and_a_server_minted_request_id(
    client: TestClient, fake: LoggedFake
) -> None:
    page = client.get("/tasks/new")

    assert page.status_code == 200
    assert re.fullmatch(r"[0-9a-f]{32}", _request_id(page.text))
    assert "data-create-gate" in page.text
    assert "only for gates" in page.text
    for gate_type in ("human", "external_task", "timer"):
        assert f'<option value="{gate_type}"' in page.text
    assert '<option value="ci"' not in page.text
    # The one read: open tasks for the project datalist, both conventions.
    assert [name for name, _ in fake.method_calls] == ["list_tasks"]
    assert '<option value="influx">' in page.text
    assert '<option value="lithos-loom">' in page.text
    # The toggle is its own script, never tasks.js (which opens the stream).
    assert "create_form.js" in page.text
    assert "tasks.js" not in page.text
    # Each render mints its own id.
    assert _request_id(client.get("/tasks/new").text) != _request_id(page.text)


def test_query_prefills_project_and_parent(client: TestClient) -> None:
    page = client.get(f"/tasks/new?project=influx&parent={EPIC}")

    assert _field_value(page.text, "project") == "influx"
    assert _field_value(page.text, "parent") == EPIC


def test_a_failed_project_read_still_renders_the_form(config) -> None:
    class ListFails(LoggedFake):
        async def list_tasks(self, **kwargs: Any):
            raise LithosToolError("did not answer", code="timeout")

    with _client(config, ListFails(create_dataset())) as client:
        page = client.get("/tasks/new")

    assert page.status_code == 200
    assert "data-create-form" in page.text
    assert "data-create-projects" not in page.text


def test_no_identity_renders_only_the_choose_link_and_reads_nothing(
    config, fake: LoggedFake
) -> None:
    with _client(config, fake, operator="") as client:
        fake.method_calls.clear()
        page = client.get("/tasks/new?project=influx")
        reads = list(fake.method_calls)

    assert page.status_code == 200
    assert "data-create-form" not in page.text
    assert "data-create-no-operator" in page.text
    assert reads == []


def test_new_task_on_the_dashboard_carries_the_one_selected_project(
    client: TestClient,
) -> None:
    one = client.get("/tasks?project=influx")
    two = client.get("/tasks?project=influx&project=lithos-loom")

    assert 'href="/tasks/new?project=influx" data-new-task-link' in one.text
    assert 'href="/tasks/new" data-new-task-link' in two.text


def test_affordances_need_an_identity(config, fake: LoggedFake) -> None:
    with _client(config, fake, operator="") as client:
        board = client.get("/tasks?project=influx")
        epic = client.get(f"/tasks/{EPIC}")

    assert "data-new-task-link" not in board.text
    assert "data-add-child-link" not in epic.text


def test_add_child_is_on_open_epics_and_carries_the_parent(client: TestClient) -> None:
    epic = client.get(f"/tasks/{EPIC}")
    done = client.get("/tasks/done-epic")
    task = client.get(f"/tasks/{PREDECESSOR}")

    assert f'href="/tasks/new?parent={EPIC}" data-add-child-link' in epic.text
    assert "data-add-child-link" not in done.text
    assert "data-add-child-link" not in task.text
    form = client.get(f"/tasks/new?parent={EPIC}")
    assert _field_value(form.text, "parent") == EPIC


# ── a create, through the funnel ────────────────────────────────────────


def test_a_create_lands_on_the_new_task_with_both_conventions(config) -> None:
    config = replace(config, tasks=replace(config.tasks, project_tag_key="proj"))
    fake = LoggedFake(create_dataset())
    with _client(config, fake) as client:
        fake.write_calls.clear()
        data = _form(
            description="Dual-write first, then swap.",
            project="influx",
            tags="area:data",
        )
        response = _post(client, data)
        task_id = _landed_on(response)
        page = client.get(response.headers["location"])

    [call] = _creates(fake)
    assert call["agent"] == OPERATOR
    assert call["task_type"] == "task"
    assert call["metadata"] == {
        "project": "influx",
        "lens_request_id": data["request_id"],
    }
    assert call["tags"] == ["area:data", "proj:influx"]
    text = _text(page.text)
    assert "Created task “Swap reads onto the new store”" in text
    assert "Created as dave in project influx." in text
    assert f'data-task-detail="{task_id}"' in page.text


def test_depends_on_and_parent_produce_edges_shown_on_the_new_page(
    client: TestClient, fake: LoggedFake
) -> None:
    lens = client.app.state.lens  # type: ignore[attr-defined]
    # Warm the parent's and the predecessor's cached edges, as a graph page
    # would have; the create must drop them (D14).
    client.get("/tasks/graph?project=influx")
    assert lens.graph_cache.get(EPIC) is not None
    assert lens.graph_cache.get(PREDECESSOR) is not None

    response = _post(
        client, _form(parent="influx-ep", predecessors=f"{PREDECESSOR[:8]}\n")
    )
    task_id = _landed_on(response)

    [call] = _creates(fake)
    assert call["parent_task_id"] == "influx-ep"
    assert call["depends_on"] == [PREDECESSOR[:8]]
    assert lens.graph_cache.get(EPIC) is None
    assert lens.graph_cache.get(PREDECESSOR) is None
    page = client.get(f"/tasks/{task_id}")
    breadcrumb = re.search(r"data-parent-breadcrumb.*?</nav>", page.text, re.S)
    assert breadcrumb and "Influx store migration" in breadcrumb.group(0)
    chain = re.search(r"<section data-blocker-chain>.*?</section>", page.text, re.S)
    assert chain and "Cut ingest over" in chain.group(0)


@pytest.mark.parametrize(
    ("fields", "metadata"),
    [
        pytest.param({"task_type": "epic"}, {}, id="epic"),
        pytest.param(
            {"task_type": "gate", "gate_type": "human"},
            {"gate_type": "human"},
            id="human-gate",
        ),
        pytest.param(
            {"task_type": "gate", "gate_type": "external_task"},
            {"gate_type": "external_task"},
            id="external-task-gate",
        ),
        pytest.param(
            {"task_type": "gate", "gate_type": "timer", "ready_at": "2026-10-09T09:00"},
            {"gate_type": "timer", "ready_at": "2026-10-09T09:00:00+00:00"},
            id="timer-gate",
        ),
    ],
)
def test_each_type_is_created_as_chosen_with_its_markdown_description(
    client: TestClient,
    fake: LoggedFake,
    fields: dict[str, str],
    metadata: dict[str, str],
) -> None:
    description = "Dual-write **first**, then swap.\n\n- one\n- two"
    data = _form(description=description, **fields)

    response = _post(client, data)
    task_id = _landed_on(response)
    page = client.get(response.headers["location"])

    [call] = _creates(fake)
    assert call["task_type"] == fields["task_type"]
    assert call["description"] == description
    assert call["metadata"] == {**metadata, "lens_request_id": data["request_id"]}
    assert f'data-task-detail="{task_id}"' in page.text
    assert f'data-task-type="{fields["task_type"]}"' in page.text
    if "gate_type" in metadata:
        assert f'data-gate-type="{metadata["gate_type"]}"' in page.text
    # The description is stored as typed and rendered as Markdown.
    assert "<strong>first</strong>" in page.text
    assert "<li>two</li>" in page.text
    assert f"Created {fields['task_type']} “{data['title']}”" in _text(page.text)


def test_a_timer_gate_without_ready_at_is_refused_before_any_call(
    client: TestClient, fake: LoggedFake
) -> None:
    data = _form(task_type="gate", gate_type="timer", description="Embargo")
    response = _post(client, data)

    assert response.status_code == 422
    # Not one Lithos call of any kind — not even the datalist's read.
    assert fake.method_calls == []
    assert "A timer gate needs the date and time" in _error(response.text, "ready_at")
    # Input kept, and the SAME request id: nothing was created.
    assert _field_value(response.text, "title") == data["title"]
    assert _field_value(response.text, "description") == "Embargo"
    assert _request_id(response.text) == data["request_id"]


@pytest.mark.parametrize("gate_type", ["ci", "pr", "bogus"])
def test_a_gate_type_a_person_may_not_create_is_refused_before_any_call(
    client: TestClient, fake: LoggedFake, gate_type: str
) -> None:
    response = _post(client, _form(task_type="gate", gate_type=gate_type))

    assert response.status_code == 422
    assert fake.method_calls == []
    assert _error(response.text, "gate_type")


def test_an_upstream_invalid_input_re_renders_with_the_message_on_its_field(
    client: TestClient, fake: LoggedFake
) -> None:
    data = _form(parent="infl", tags="area:data\nmilestone:t3")
    response = _post(client, data)

    assert response.status_code == 422
    assert len(_creates(fake)) == 1
    assert "Nothing was changed." in _text(response.text)
    assert "parent_task_id 'infl' is too short" in _error(response.text, "parent")
    assert _field_value(response.text, "parent") == "infl"
    assert _field_value(response.text, "tags") == "area:data\nmilestone:t3"
    assert _request_id(response.text) == data["request_id"]

    # A refused id is forgotten: the corrected submit, same id, creates.
    corrected = _post(client, {**data, "parent": EPIC})
    _landed_on(corrected)
    assert len(_creates(fake)) == 2


def test_an_ambiguous_parent_prefix_lists_the_candidates_under_the_parent(
    client: TestClient,
) -> None:
    response = _post(client, _form(parent="506155"))

    assert response.status_code == 422
    error = _error(response.text, "parent")
    assert "'506155' matches more than one task" in error
    assert TWIN_A in error and "Rebuild the Influx ingest path" in error
    assert TWIN_B in error and "Retire the old Influx writer" in error
    # Text, not links away from the form.
    assert f'href="/tasks/{TWIN_A}"' not in response.text
    assert _field_value(response.text, "parent") == "506155"


def test_a_post_without_a_valid_request_id_is_bad_form(
    client: TestClient, fake: LoggedFake
) -> None:
    response = _post(client, _form(request_id="not-an-id"))

    assert response.status_code == 400
    assert _creates(fake) == []


def test_a_post_without_identity_goes_to_the_operator_page_unreplayed(
    config, fake: LoggedFake
) -> None:
    with _client(config, fake, operator="") as client:
        response = _post(client, _form(project="influx", parent=EPIC))

    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/operator"
    assert parse_qs(location.query)["next"] == [
        f"/tasks/new?project=influx&parent={EPIC}"
    ]
    assert _creates(fake) == []


def test_a_cross_origin_post_is_refused(client: TestClient, fake: LoggedFake) -> None:
    response = client.post(
        "/tasks/new",
        data=_form(),
        headers={"Origin": "http://elsewhere.test"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert _creates(fake) == []


def _audit(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "lens_event", "") == AUDIT_EVENT]


def test_one_audit_line_and_span_per_attempt_by_ids_and_lengths(
    spans: InMemorySpanExporter,
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data = _form(description="Nobody may read this", project="influx")
    caplog.set_level(logging.INFO)
    spans.clear()

    task_id = _landed_on(_post(client, data))
    again = _landed_on(_post(client, data))

    assert again == task_id
    first, second = (vars(record) for record in _audit(caplog))
    assert first["action"] == "create"
    assert first["result"] == "ok"
    assert first["task_id"] == task_id
    assert first["write_request_id"] == data["request_id"]
    assert "Nobody may read this" not in repr(first["arguments"])
    assert first["arguments"]["description_chars"] == len("Nobody may read this")
    assert "write_dedup" not in first
    assert second["result"] == "ok" and second["write_dedup"] == "remembered"
    created = [
        dict(s.attributes or {})
        for s in spans.get_finished_spans()
        if s.name == "lens.writes.create"
    ]
    assert [s["lens.write.task_id"] for s in created] == [task_id, task_id]
    assert created[1]["lens.write.dedup"] == "remembered"


# ── de-duplication, end to end ──────────────────────────────────────────


def test_a_resubmit_after_the_first_finished_creates_nothing(
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
    client: TestClient,
    fake: LoggedFake,
) -> None:
    data = _form(project="influx")
    spans.clear()
    first = _post(client, data)
    # The first tab's receipt is shown (and consumed) before the resubmit.
    first_page = client.get(first.headers["location"])
    second = _post(client, data)

    assert _landed_on(second) == _landed_on(first)
    assert len(_creates(fake)) == 1
    assert _receipt_id(first) != _receipt_id(second)
    second_page = client.get(second.headers["location"])
    for page in (first_page, second_page):
        assert 'data-receipt-action="create"' in page.text
    assert "data-receipt-repeated" not in first_page.text
    assert "data-receipt-repeated" in second_page.text
    # D1/D2: both are attempts, both `ok`; the dedup marker is on the span,
    # never a counter label.
    task_id = _landed_on(first)
    recorded = _create_spans(spans)
    assert [span["lens.write.result"] for span in recorded] == ["ok", "ok"]
    assert [span["lens.write.task_id"] for span in recorded] == [task_id, task_id]
    assert [span["lens.write.request_id"] for span in recorded] == [
        data["request_id"]
    ] * 2
    assert [span.get("lens.write.dedup", "") for span in recorded] == [
        "",
        "remembered",
    ]
    assert _write_counts(metric_reader) == {("create", "ok"): 2}


def test_a_resubmit_after_an_operator_switch_names_who_created_the_task(
    client: TestClient, fake: LoggedFake, caplog: pytest.LogCaptureFixture
) -> None:
    """The label is switched on the operator page between the submit and its
    resubmit: the receipt still names who CREATED the task; the resubmit's
    own attempt is recorded under the identity that sent it."""
    data = _form(project="influx")
    first = _post(client, data)
    client.get(first.headers["location"])
    switched = client.post(
        "/operator",
        data={"operator": "dave-alt"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert switched.status_code == 303
    caplog.set_level(logging.INFO)
    second = _post(client, data)

    [call] = _creates(fake)
    assert call["agent"] == OPERATOR
    assert _landed_on(second) == _landed_on(first)
    receipt = _text(client.get(second.headers["location"]).text)
    assert f"Created as {OPERATOR} in project influx." in receipt
    assert "Created as dave-alt" not in receipt
    [line] = [vars(record) for record in _audit(caplog)]
    assert line["operator"] == "dave-alt"
    assert line["write_dedup"] == "remembered"


def test_a_resubmit_of_a_changed_form_reports_the_task_that_exists(
    client: TestClient, fake: LoggedFake
) -> None:
    """The back button, the form edited, the same request id sent again: one
    task, and the second receipt states THAT task — not the edited form."""
    data = _form(project="influx")
    first = _post(client, data)
    client.get(first.headers["location"])
    second = _post(client, {**data, "task_type": "epic", "project": "lithos-loom"})

    assert _landed_on(second) == _landed_on(first)
    assert len(_creates(fake)) == 1
    receipt = _text(client.get(second.headers["location"]).text)
    assert "Created task “Swap reads onto the new store”" in receipt
    assert "in project influx." in receipt
    assert "epic" not in receipt.split("Created as")[0]
    assert "lithos-loom" not in receipt


class HeldCreate(LoggedFake):
    """A create that waits, in Lithos, until the test lets it through — then
    applies (``created``), is refused by the fake's own rules (``refused``,
    driven by the form), or times out while still landing upstream
    (``unknown``: it lands once ``land`` is set)."""

    def __init__(self, mode: str = "created") -> None:
        super().__init__(create_dataset())
        self.mode = mode
        self.started = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.release: asyncio.Event | None = None
        self.land: asyncio.Event | None = None
        self.landed = threading.Event()
        self.landing: asyncio.Task[Any] | None = None

    async def task_create(self, **kwargs: Any):
        self.loop = asyncio.get_running_loop()
        # One release for every held call, so a second create (the defect this
        # test exists to catch) fails the count rather than hanging the test.
        if self.release is None:
            self.release = asyncio.Event()
        self.started.set()
        await self.release.wait()
        if self.mode == "unknown":
            self.land = asyncio.Event()
            self.landing = asyncio.create_task(self._land(kwargs))
            raise LithosToolError("did not answer within 10s", code="timeout")
        return await super().task_create(**kwargs)

    async def _land(self, kwargs: dict[str, Any]) -> None:
        assert self.land is not None
        await self.land.wait()
        await FakeLithosClient.task_create(self, **kwargs)
        self.landed.set()


@pytest.mark.parametrize("mode", ["created", "refused", "unknown"])
def test_two_concurrent_posts_with_one_request_id_make_one_create(
    config,
    monkeypatch: pytest.MonkeyPatch,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
    caplog: pytest.LogCaptureFixture,
    mode: str,
) -> None:
    """Two POSTs genuinely in flight together: the second arrives while the
    first is inside ``lithos_task_create``, and receives THAT call's outcome
    whatever it is. Ordered by barriers, not sleeps: the release is sent only
    once the second submit has reached the coordinator, which then has no
    await before it joins the flight."""
    arrivals: list[str] = []
    second_arrived = threading.Event()

    class SpyCoordinator(create_routes.CreateCoordinator):
        async def submit(self, request_id, create):
            arrivals.append(request_id)
            if len(arrivals) == 2:
                second_arrived.set()
            return await super().submit(request_id, create)

    monkeypatch.setattr(create_routes, "CreateCoordinator", SpyCoordinator)
    fake = HeldCreate(mode)
    # `refused`: the fake refuses a too-short parent prefix after the hold.
    data = _form(project="influx", parent="infl" if mode == "refused" else "")
    answers: dict[str, Any] = {}
    caplog.set_level(logging.INFO)

    with _client(config, fake) as client:

        def post(name: str) -> None:
            answers[name] = _post(client, data)

        first = threading.Thread(target=post, args=("first",))
        first.start()
        assert fake.started.wait(10)
        second = threading.Thread(target=post, args=("second",))
        second.start()
        assert second_arrived.wait(10)
        assert fake.loop is not None and fake.release is not None
        fake.loop.call_soon_threadsafe(fake.release.set)
        first.join(10)
        second.join(10)
        assert len(fake.reads_of("task_create")) == 1

        if mode == "created":
            # Each tab gets its OWN receipt (a receipt is consumed once), and
            # both render; the joined one says it made no second task.
            assert _landed_on(answers["first"]) == _landed_on(answers["second"])
            assert _receipt_id(answers["first"]) != _receipt_id(answers["second"])
            leader = client.get(answers["first"].headers["location"])
            joined = client.get(answers["second"].headers["location"])
            for page in (leader, joined):
                assert 'data-receipt-action="create"' in page.text
            assert "data-receipt-repeated" not in leader.text
            assert "data-receipt-repeated" in joined.text
        elif mode == "refused":
            for name in ("first", "second"):
                assert answers[name].status_code == 422
                assert "is too short" in _error(answers[name].text, "parent")
                assert _request_id(answers[name].text) == data["request_id"]
            # Nothing was created, so the corrected form, same id, may create.
            _landed_on(_post(client, {**data, "parent": EPIC}))
            assert len(fake.reads_of("task_create")) == 2
        else:
            for name in ("first", "second"):
                assert answers[name].status_code == 200
                assert NOT_VISIBLE in _text(answers[name].text)
            assert fake.land is not None
            fake.loop.call_soon_threadsafe(fake.land.set)
            assert fake.landed.wait(10)
            later = _post(client, data)
            assert NOT_VISIBLE in _text(later.text)
            # Landed upstream, and still never sent again under this id.
            assert len(fake.reads_of("task_create")) == 1

    expected = {"created": "ok", "refused": "rejected", "unknown": "unknown"}[mode]
    lines = [vars(record) for record in _audit(caplog)][:2]
    assert [line["result"] for line in lines] == [expected, expected]
    assert sorted(line.get("write_dedup", "") for line in lines) == ["", "joined"]
    recorded = _create_spans(spans)[:2]
    assert [span["lens.write.result"] for span in recorded] == [expected] * 2
    assert [span["lens.write.request_id"] for span in recorded] == [
        data["request_id"]
    ] * 2
    assert sorted(str(span.get("lens.write.dedup", "")) for span in recorded) == [
        "",
        "joined",
    ]
    if mode == "created":
        task_ids = {span["lens.write.task_id"] for span in recorded}
        assert task_ids == {_landed_on(answers["first"])}
    # Every attempt counted once, by action and result only. The follow-up
    # submit each mode makes (a corrected create; a POST after the landing)
    # is an attempt too.
    assert (
        _write_counts(metric_reader)
        == {
            "created": {("create", "ok"): 2},
            "refused": {("create", "rejected"): 2, ("create", "ok"): 1},
            "unknown": {("create", "unknown"): 3},
        }[mode]
    )


class LandsLater(LoggedFake):
    """The call times out in Lens while Lithos is still to complete it."""

    def __init__(self) -> None:
        super().__init__(create_dataset())
        self.loop: asyncio.AbstractEventLoop | None = None
        self.land: asyncio.Event | None = None
        self.landed = threading.Event()
        self.landing: asyncio.Task[Any] | None = None

    async def task_create(self, **kwargs: Any):
        self.loop = asyncio.get_running_loop()
        self.land = asyncio.Event()
        self.landing = asyncio.create_task(self._land(kwargs))
        raise LithosToolError("did not answer within 10s", code="timeout")

    async def _land(self, kwargs: dict[str, Any]) -> None:
        assert self.land is not None
        await self.land.wait()
        await FakeLithosClient.task_create(self, **kwargs)
        self.landed.set()


def test_a_create_that_times_out_is_not_visible_yet_and_never_sent_again(
    config,
) -> None:
    fake = LandsLater()
    data = _form(project="influx", description="Keep me")
    with _client(config, fake) as client:
        first = _post(client, data)
        second = _post(client, data)
        assert fake.loop is not None and fake.land is not None
        fake.loop.call_soon_threadsafe(fake.land.set)
        assert fake.landed.wait(10)
        third = _post(client, data)
        board = client.get("/tasks?project=influx")

    for page in (first, second, third):
        assert page.status_code == 200
        text = _text(page.text)
        assert NOT_VISIBLE in text
        assert "not created" not in text.lower()
        assert "data-create-restart" in page.text
        assert 'href="/tasks?project=influx" data-create-board-link' in page.text
    # ONE call under the id, ever — before and after it landed.
    assert len(fake.reads_of("task_create")) == 1
    assert "Swap reads onto the new store" in board.text


def test_start_again_posts_the_rendered_input_back_under_a_new_id(
    config,
    spans: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Start again is submitted exactly as the "not visible yet" page renders
    it — its own hidden controls — so what survives is what the page carried,
    not what the test remembers typing (D7)."""
    fake = LandsLater()
    data = _form(
        title="Wait for the embargo to lift",
        task_type="gate",
        description="Lift at **09:00**.\n\nThen ship.",
        project="influx",
        tags="area:data\nmilestone:t3, late",
        parent=EPIC,
        predecessors=f"{PREDECESSOR}\n{TWIN_A}",
        gate_type="timer",
        ready_at="2026-10-09T09:00",
    )
    caplog.set_level(logging.INFO)
    with _client(config, fake) as client:
        unknown = _post(client, data)
        assert NOT_VISIBLE in _text(unknown.text)
        controls = _form_controls(unknown.text, "data-create-restart")
        attempts = len(_audit(caplog))
        spans.clear()
        fake.method_calls.clear()
        restart = _post(client, controls)
        calls = list(fake.method_calls)

    assert controls["intent"] == "restart"
    assert "request_id" not in controls
    assert restart.status_code == 200
    for name in ("title", "description", "project", "tags", "parent", "predecessors"):
        assert _field_value(restart.text, name) == data[name], name
    assert _field_value(restart.text, "ready_at") == data["ready_at"]
    assert _selected(restart.text, "task_type") == "gate"
    assert _selected(restart.text, "gate_type") == "timer"
    fresh = _request_id(restart.text)
    assert re.fullmatch(r"[0-9a-f]{32}", fresh) and fresh != data["request_id"]
    # No call of any kind, and not an attempt: no audit line, no span.
    assert calls == []
    assert len(_audit(caplog)) == attempts
    assert [
        s for s in spans.get_finished_spans() if s.name == "lens.writes.create"
    ] == []


def test_an_upstream_refusal_is_answered_on_the_form_not_a_standalone_page(
    config,
) -> None:
    class TypeRefused(LoggedFake):
        async def task_create(self, **kwargs: Any):
            raise write_error("invalid_task_type", "task_type 'x' is not accepted.")

    with _client(config, TypeRefused(create_dataset())) as client:
        response = _post(client, _form())

    assert response.status_code == 422
    assert "data-create-form" in response.text
    assert "invalid_task_type" in response.text  # the unmapped-code path


class _NoAnswer(LoggedFake):
    async def task_create(self, **kwargs: Any):
        raise LithosToolError("did not answer within 10s", code="timeout")


#: One create attempt per row, and how it must be recorded:
#: (id, form overrides, operator, headers, fake, result, code, status).
_ATTEMPTS = [
    ("ok", {}, OPERATOR, SAME_ORIGIN, LoggedFake, "ok", "", 303),
    (
        "invalid-form",
        {"task_type": "gate", "gate_type": "timer"},
        OPERATOR,
        SAME_ORIGIN,
        LoggedFake,
        "rejected",
        "invalid_form",
        422,
    ),
    (
        "bad-form",
        {"request_id": "not-an-id"},
        OPERATOR,
        SAME_ORIGIN,
        LoggedFake,
        "rejected",
        "bad_form",
        400,
    ),
    ("no-operator", {}, "", SAME_ORIGIN, LoggedFake, "no_operator", "", 303),
    (
        "foreign-origin",
        {},
        OPERATOR,
        {"Origin": "http://elsewhere.test"},
        LoggedFake,
        "refused_origin",
        "",
        403,
    ),
    (
        "identity-refused",
        {},
        "agent-zero",
        SAME_ORIGIN,
        LoggedFake,
        "rejected",
        "identity_refused",
        403,
    ),
    (
        "registration-failed",
        {},
        OPERATOR,
        SAME_ORIGIN,
        LoggedFake,
        "rejected",
        "registration_failed",
        503,
    ),
    (
        "upstream-refusal",
        {"parent": "infl"},
        OPERATOR,
        SAME_ORIGIN,
        LoggedFake,
        "rejected",
        "invalid_input",
        422,
    ),
    ("no-answer", {}, OPERATOR, SAME_ORIGIN, _NoAnswer, "unknown", "", 200),
]


@pytest.mark.parametrize(
    ("overrides", "operator", "headers", "fake_type", "result", "code", "status"),
    [pytest.param(*row[1:], id=row[0]) for row in _ATTEMPTS],
)
def test_every_create_attempt_is_recorded_once(
    config,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
    caplog: pytest.LogCaptureFixture,
    overrides: dict[str, str],
    operator: str,
    headers: dict[str, str],
    fake_type: type[LoggedFake],
    result: str,
    code: str,
    status: int,
) -> None:
    """D1/D2: one audit line, one span and one counter increment per create
    attempt, whatever its ending, carrying the request id."""
    dataset = replace(
        create_dataset(), agents=(AgentRecord(id="agent-zero", type="claude-code"),)
    )
    fake = fake_type(dataset)
    fake.register_operator_fails = code == "registration_failed"
    data = _form(**overrides)
    caplog.set_level(logging.INFO)
    with _client(config, fake, operator=operator) as client:
        spans.clear()
        response = client.post(
            "/tasks/new", data=data, headers=headers, follow_redirects=False
        )

    assert response.status_code == status
    [line] = [vars(record) for record in _audit(caplog)]
    assert (line["action"], line["result"], line["code"]) == ("create", result, code)
    assert line["write_request_id"] == data["request_id"]
    assert "write_dedup" not in line
    [span] = [
        dict(s.attributes or {})
        for s in spans.get_finished_spans()
        if s.name == "lens.writes.create"
    ]
    assert span["lens.write.result"] == result
    assert span.get("lens.write.code", "") == code
    assert span["lens.write.request_id"] == data["request_id"]
    assert (
        metric_value(
            metric_reader, "lens_writes_total", action="create", result=result
        ).value
        == 1
    )
