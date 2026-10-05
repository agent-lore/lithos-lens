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
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
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
from lithos_lens.tasks import TaskRecord
from lithos_lens.web import create_app
from lithos_lens.write_errors import map_write_error
from lithos_lens.write_funnel import AUDIT_EVENT
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


def test_a_timer_gate_without_ready_at_is_refused_before_any_call(
    client: TestClient, fake: LoggedFake
) -> None:
    data = _form(task_type="gate", gate_type="timer", description="Embargo")
    response = _post(client, data)

    assert response.status_code == 422
    assert _creates(fake) == []
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
    assert _creates(fake) == []
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
    client: TestClient, fake: LoggedFake
) -> None:
    data = _form(project="influx")
    first = _landed_on(_post(client, data))
    second = _post(client, data)

    assert _landed_on(second) == first
    assert len(_creates(fake)) == 1
    page = client.get(second.headers["location"])
    assert "data-receipt-repeated" in page.text


class HeldCreate(LoggedFake):
    """A create that waits, in Lithos, until the test lets it through."""

    def __init__(self) -> None:
        super().__init__(create_dataset())
        self.started = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.release: asyncio.Event | None = None

    async def task_create(self, **kwargs: Any):
        self.loop = asyncio.get_running_loop()
        # One release for every held call, so a second create (the defect this
        # test exists to catch) fails the count rather than hanging the test.
        if self.release is None:
            self.release = asyncio.Event()
        self.started.set()
        await self.release.wait()
        return await super().task_create(**kwargs)


def test_two_concurrent_posts_with_one_request_id_make_one_create(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two POSTs genuinely in flight together: the second arrives while the
    first is inside ``lithos_task_create``. Ordered by barriers, not sleeps:
    the release is sent only once the second submit has reached the
    coordinator, which then has no await before it joins the flight."""
    arrivals: list[str] = []
    second_arrived = threading.Event()

    class SpyCoordinator(create_routes.CreateCoordinator):
        async def submit(self, request_id, create):
            arrivals.append(request_id)
            if len(arrivals) == 2:
                second_arrived.set()
            return await super().submit(request_id, create)

    monkeypatch.setattr(create_routes, "CreateCoordinator", SpyCoordinator)
    fake = HeldCreate()
    data = _form(project="influx")
    answers: dict[str, Any] = {}

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
    assert len(_creates(fake)) == 1
    assert _landed_on(answers["first"]) == _landed_on(answers["second"])


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
        restart = _post(client, {**data, "intent": "restart"})

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
    # Start again: the input kept under a NEW request id, and no call.
    assert restart.status_code == 200
    assert _field_value(restart.text, "description") == "Keep me"
    assert _field_value(restart.text, "project") == "influx"
    assert _request_id(restart.text) != data["request_id"]
    assert len(fake.reads_of("task_create")) == 1


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
