"""T3-W8 — Add a dependency: sentence forms, a confirm step, one upsert or none.

Asserted the way the PRD's Testing Decisions ask: on what a request returns and
on what the fake recorded (its write log and ``LoggedFake``'s method log),
never on internals. The groups follow the slice's acceptance, as narrowed by
the 2026-10-05 clarifications (S1-S4, D1-D10):

- the SENTENCE MODEL maps each of the three sentences to the right ``from`` /
  ``to`` / ``type``, offers the gate sentence only on a gate, and words the
  readiness meaning for each status case;
- the CONFIRM STEP renders both titles and the readiness sentence, resolves a
  prefix, and lists an ambiguous prefix's candidates under the input;
- the WRITE makes one upsert with no metadata and evicts both endpoints;
- an EXISTING relation — in Lithos but not in Lens's cache — is "already
  exists" on both steps, with no upsert;
- a FAILED fresh read makes no upsert;
- each REFUSAL renders its copy and says nothing was changed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterator
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
from lithos_lens.fake_dataset import demo_dataset
from lithos_lens.lithos_client import LithosToolError
from lithos_lens.operator import OPERATOR_COOKIE_NAME
from lithos_lens.relation_sentences import (
    BLOCKED_BY,
    BLOCKS,
    SENTENCES,
    WAITED_ON_BY,
    Relation,
    existing_edge,
    readiness,
    sentence_named,
    sentence_of,
    sentences_for,
)
from lithos_lens.task_graph import EdgeRecord
from lithos_lens.tasks import TaskRecord, TaskStatusName
from lithos_lens.web import create_app
from lithos_lens.write_funnel import AUDIT_EVENT
from tests.conftest import load_contract
from tests.test_complete_gate import LoggedFake

ORIGIN = "http://lens.test"
SAME_ORIGIN = {"Origin": ORIGIN}
OPERATOR = "dave"

#: D9's pair: open, unconnected, untouched by W4-W7.
FOCAL = "loom-docs-tidy"
FOCAL_TITLE = "Tidy the harness docs"
OTHER = "loom-metrics-note"
OTHER_TITLE = "Note the harness metrics gaps"
GATE = "influx-read-swap-approval"
GATE_TITLE = "Approve the Influx read swap"


# ── the sentence model ───────────────────────────────────────────────────


def _task(
    task_id: str, *, status: TaskStatusName = "open", **fields: Any
) -> TaskRecord:
    return TaskRecord(id=task_id, title=task_id.upper(), status=status, **fields)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (BLOCKED_BY, Relation("other", "focal", "blocks")),
        (BLOCKS, Relation("focal", "other", "blocks")),
        (WAITED_ON_BY, Relation("focal", "other", "waits_on_gate")),
    ],
)
def test_each_sentence_produces_its_from_to_and_type(
    key: str, expected: Relation
) -> None:
    gate = _task("focal", task_type="gate")
    sentence = sentence_named(key, gate)
    assert sentence is not None
    assert sentence.relation("focal", "other") == expected
    # And back: the relation is the one sentence on that page that means it.
    assert sentence_of(expected, gate) == sentence


def test_the_gate_sentence_is_offered_only_on_a_gate() -> None:
    labels = {s.key: s.label for s in SENTENCES}
    assert labels == {
        BLOCKED_BY: "This task is blocked by ▁",
        BLOCKS: "This task blocks ▁",
        WAITED_ON_BY: "▁ waits on this gate",
    }
    for task_type in ("task", "epic"):
        task = _task("t", task_type=task_type)
        assert [s.key for s in sentences_for(task)] == [BLOCKED_BY, BLOCKS]
        assert sentence_named(WAITED_ON_BY, task) is None
        # A posted waits_on_gate edge from a non-gate is no sentence it offers.
        assert sentence_of(Relation("t", "x", "waits_on_gate"), task) is None
    # F7: a gate with no metadata.gate_type is still a gate.
    bare_gate = _task("g", task_type="gate")
    assert [s.key for s in sentences_for(bare_gate)] == [
        BLOCKED_BY,
        BLOCKS,
        WAITED_ON_BY,
    ]


def test_a_relation_that_does_not_touch_the_task_or_has_no_sentence_is_refused() -> (
    None
):
    task = _task("focal")
    assert sentence_of(Relation("a", "b", "blocks"), task) is None
    assert sentence_of(Relation("focal", "b", "parent_child"), task) is None
    assert sentence_of(Relation("a", "focal", "discovered_from"), task) is None


def test_a_relation_is_its_from_to_and_type_not_its_endpoints() -> None:
    """Relation identity is ``(from, to, type)``: another relation between the
    same two tasks — another type, or the other direction — is not it."""
    relation = Relation("g", "w", "waits_on_gate")
    same = EdgeRecord(from_task_id="g", to_task_id="w", type="waits_on_gate")
    other_type = EdgeRecord(from_task_id="g", to_task_id="w", type="blocks")
    reversed_ = EdgeRecord(from_task_id="w", to_task_id="g", type="waits_on_gate")
    assert relation.is_edge(same)
    assert not relation.is_edge(other_type)
    assert not relation.is_edge(reversed_)
    assert existing_edge([other_type, reversed_], relation) is None
    assert existing_edge([other_type, same, reversed_], relation) == same


@pytest.mark.parametrize(
    ("blocker_status", "expected"),
    [
        ("open", "“B” will not be ready until “A” completes."),
        (
            "completed",
            "“A” is already completed, so this adds no wait: “B”'s readiness "
            "does not change.",
        ),
        (
            "cancelled",
            "“A” is cancelled, so “B” will be blocked permanently — its blocker "
            "can never be satisfied — until that task is reopened.",
        ),
    ],
)
def test_a_blocks_relation_reads_by_the_blockers_status(
    blocker_status: TaskStatusName, expected: str
) -> None:
    relation = Relation("a", "b", "blocks")
    assert (
        readiness(relation, _task("a", status=blocker_status), _task("b")) == expected
    )


def test_a_resolved_dependent_is_told_it_waits_on_nothing_now() -> None:
    meaning = readiness(
        Relation("a", "b", "blocks"), _task("a"), _task("b", status="completed")
    )
    assert meaning.endswith(
        "“B” is already completed, so it waits on nothing now; the relation "
        "applies if it is reopened."
    )


def test_a_waiter_waits_until_its_gate_resolves() -> None:
    now = datetime(2026, 10, 5, 12, tzinfo=UTC)
    relation = Relation("g", "w", "waits_on_gate")
    waiter = _task("w")

    def gate(status: TaskStatusName = "open", **metadata: str) -> TaskRecord:
        return _task("g", status=status, task_type="gate", metadata=metadata)

    assert readiness(relation, gate(gate_type="human"), waiter, now=now) == (
        "“W” will not be ready until “G” resolves — when it is completed."
    )
    future = (now + timedelta(hours=1)).isoformat()
    past = (now - timedelta(hours=1)).isoformat()
    assert readiness(
        relation, gate(gate_type="timer", ready_at=future), waiter, now=now
    ) == (
        "“W” will not be ready until “G” resolves — when it is completed or its "
        "ready time passes."
    )
    assert readiness(
        relation, gate(gate_type="timer", ready_at=past), waiter, now=now
    ) == (
        "“G” is a timer gate already past its ready time, so “W” does not wait on it."
    )
    assert readiness(relation, gate("completed"), waiter, now=now) == (
        "“G” is already completed, so “W” does not wait on it."
    )
    assert readiness(relation, gate("cancelled"), waiter, now=now).startswith(
        "“G” is cancelled, so “W” will be blocked permanently"
    )


# ── the routes ───────────────────────────────────────────────────────────


@pytest.fixture
def config(lithos_lens_config_env: Path):
    return load_config(lithos_lens_config_env)


@pytest.fixture
def fake() -> LoggedFake:
    return LoggedFake(demo_dataset())


def _client(config, fake: LoggedFake, *, operator: str = OPERATOR) -> TestClient:
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


def _confirm(client: TestClient, task_id: str, relation: str, other: str):
    return client.get(
        f"/tasks/{task_id}/edges/new", params={"relation": relation, "other": other}
    )


def _hidden(html: str) -> dict[str, str]:
    form = re.search(r"<form[^>]*data-relation-confirm-form.*?</form>", html, re.S)
    assert form, "no confirm form"
    return dict(
        re.findall(r'type="hidden" name="([^"]+)" value="([^"]*)"', form.group(0))
    )


def _post(client: TestClient, task_id: str, fields: dict[str, str]):
    return client.post(
        f"/tasks/{task_id}/edges",
        data=fields,
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )


def _upserts(fake: LoggedFake) -> list[dict[str, Any]]:
    return [
        args for tool, args in fake.write_calls if tool == "lithos_task_edge_upsert"
    ]


def _landed(client: TestClient, response) -> str:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert "receipt" in parse_qs(urlsplit(location).query)
    return client.get(location).text


def _receipt(html: str) -> str:
    match = re.search(r'<section class="write-receipt".*?</section>', html, re.S)
    assert match, "no receipt"
    return _text(match.group(0))


def _relation_forms(html: str) -> list[str]:
    return re.findall(r'data-relation-sentence="([^"]+)"', html)


def _insert_as(fake: LoggedFake, agent: str, relation: Relation) -> None:
    """Another agent's edge write, straight to Lithos: Lens hears nothing."""
    asyncio.run(
        fake.task_edge_upsert(
            from_task_id=relation.from_task_id,
            to_task_id=relation.to_task_id,
            edge_type=relation.type,
            agent=agent,
        )
    )
    fake.write_calls.clear()
    fake.method_calls.clear()


def test_an_open_tasks_page_offers_the_two_task_sentences(client: TestClient) -> None:
    page = client.get(f"/tasks/{FOCAL}").text
    assert _relation_forms(page) == [BLOCKED_BY, BLOCKS]
    assert f'action="/tasks/{FOCAL}/edges/new"' in page


def test_a_gates_page_also_offers_waits_on_this_gate(client: TestClient) -> None:
    page = client.get(f"/tasks/{GATE}").text
    assert _relation_forms(page) == [BLOCKED_BY, BLOCKS, WAITED_ON_BY]
    assert "▁ waits on this gate" in page


def test_a_resolved_task_or_no_operator_offers_no_relation_form(config, fake) -> None:
    with _client(config, fake) as client:
        assert _relation_forms(client.get("/tasks/loom-design-done").text) == []
    with _client(config, fake, operator="") as client:
        assert _relation_forms(client.get(f"/tasks/{FOCAL}").text) == []


def test_the_confirm_step_restates_the_relation_with_both_titles_and_readiness(
    client: TestClient, fake: LoggedFake
) -> None:
    response = _confirm(client, FOCAL, BLOCKED_BY, OTHER)

    assert response.status_code == 200
    text = _text(response.text)
    assert f"“{FOCAL_TITLE}”" in text and f"“{OTHER_TITLE}”" in text
    statement = re.search(r"data-relation-statement.*?</p>", response.text, re.S)
    assert statement
    assert _text(statement.group(0)).endswith(
        f"“{FOCAL_TITLE}” {FOCAL[:8]} is blocked by “{OTHER_TITLE}” {OTHER[:8]}"
    )
    assert f"“{FOCAL_TITLE}” will not be ready until “{OTHER_TITLE}” completes." in text
    assert _hidden(response.text) == {
        "from_task_id": OTHER,
        "to_task_id": FOCAL,
        "type": "blocks",
        "expected_status": "open",
        "next": f"/tasks/{FOCAL}",
    }
    assert fake.write_calls == []


def test_the_confirm_step_resolves_a_prefix_and_posts_the_full_id(
    client: TestClient,
) -> None:
    response = _confirm(client, FOCAL, BLOCKS, "loom-me")

    assert response.status_code == 200
    assert _hidden(response.text)["to_task_id"] == OTHER
    assert _hidden(response.text)["from_task_id"] == FOCAL


def test_the_gate_sentence_draws_the_edge_from_the_gate(client: TestClient) -> None:
    response = _confirm(client, GATE, WAITED_ON_BY, OTHER)

    hidden = _hidden(response.text)
    assert (hidden["from_task_id"], hidden["to_task_id"], hidden["type"]) == (
        GATE,
        OTHER,
        "waits_on_gate",
    )
    assert "will not be ready until “Approve the Influx read swap” resolves" in _text(
        response.text
    )


def _says_nothing_changed(html: str, code: str) -> None:
    """A refused check states the W2 claim first, above the kept input."""
    notice = re.search(
        rf'data-relation-problem="{code}">(.*?)</div>\s*</div>', html, re.S
    )
    assert notice, f"no {code} notice"
    assert _text(notice.group(1)).startswith("Nothing was changed.")
    assert "data-relation-confirm-form" not in html


def test_the_gate_sentence_is_not_accepted_on_a_task(client: TestClient) -> None:
    response = _confirm(client, FOCAL, WAITED_ON_BY, OTHER)

    assert response.status_code == 400
    assert 'data-relation-error="relation"' in response.text
    _says_nothing_changed(response.text, "bad_relation")


def test_naming_no_other_task_is_refused_and_says_nothing_changed(
    client: TestClient,
) -> None:
    response = _confirm(client, FOCAL, BLOCKS, "")

    assert response.status_code == 400
    assert 'data-relation-error="other"' in response.text
    _says_nothing_changed(response.text, "bad_form")


def test_the_task_itself_is_refused_on_the_confirm_step(
    client: TestClient, fake: LoggedFake
) -> None:
    response = _confirm(client, FOCAL, BLOCKED_BY, FOCAL)

    assert response.status_code == 422
    _says_nothing_changed(response.text, "self_edge")
    assert "A task can't depend on itself." in _text(response.text)
    assert 'data-relation-error="other"' in response.text
    assert fake.write_calls == []


def test_an_ambiguous_prefix_lists_its_candidates_under_the_input(
    client: TestClient, fake: LoggedFake
) -> None:
    response = _confirm(client, FOCAL, BLOCKS, "loom-d")

    assert response.status_code == 422
    error = re.search(r'data-relation-error="other".*?</div>', response.text, re.S)
    assert error
    assert re.findall(r'data-relation-candidate="([^"]+)"', error.group(0)) == [
        "loom-design-done",
        "loom-docs-tidy",
    ]
    # As text, to retype — not links that choose for the operator, and only
    # under the input: the notice above it carries no second, linked list.
    assert "<a " not in error.group(0)
    assert response.text.count("data-relation-candidate=") == 2
    _says_nothing_changed(response.text, "ambiguous_id_prefix")
    assert "'loom-d' matches more than one task." in _text(response.text)
    assert 'value="loom-d"' in response.text
    assert "data-relation-confirm-form" not in response.text
    assert fake.write_calls == []


@pytest.mark.parametrize(
    ("typed", "code", "copy"),
    [
        (
            "loom-nope",
            "task_not_found",
            "No task matches id prefix 'loom-nope' (task_id).",
        ),
        ("loom", "invalid_input", "task_id 'loom' is too short"),
    ],
)
def test_an_unresolvable_other_task_is_said_under_the_input(
    client: TestClient, typed: str, code: str, copy: str
) -> None:
    response = _confirm(client, FOCAL, BLOCKS, typed)

    assert response.status_code == 422
    error = re.search(r'data-relation-error="other".*?</div>', response.text, re.S)
    assert error and copy in _text(error.group(0))
    _says_nothing_changed(response.text, code)


def test_a_successful_write_is_one_upsert_with_no_metadata_and_evicts_both_ends(
    client: TestClient, fake: LoggedFake
) -> None:
    lens = client.app.state.lens  # type: ignore[attr-defined]
    client.get("/tasks/graph?project=lithos-loom")
    assert lens.graph_cache.get(FOCAL) is not None
    assert lens.graph_cache.get(OTHER) is not None
    fields = _hidden(_confirm(client, FOCAL, BLOCKED_BY, OTHER).text)
    fake.write_calls.clear()
    # The confirm step's own fresh read refilled the focal entry.
    assert lens.graph_cache.get(FOCAL) is not None

    response = _post(client, FOCAL, fields)

    assert response.status_code == 303
    assert _upserts(fake) == [
        {
            "from_task_id": OTHER,
            "to_task_id": FOCAL,
            "type": "blocks",
            "agent": OPERATOR,
        }
    ]
    assert lens.graph_cache.get(FOCAL) is None
    assert lens.graph_cache.get(OTHER) is None
    receipt = _receipt(_landed(client, response))
    assert f"Added dependency: “{OTHER_TITLE}”" in receipt
    assert f"blocks “{FOCAL_TITLE}”" in receipt
    assert "Added as dave." in receipt


@pytest.mark.parametrize(
    ("focal", "key", "upsert", "headline"),
    [
        (
            FOCAL,
            BLOCKED_BY,
            {"from_task_id": OTHER, "to_task_id": FOCAL, "type": "blocks"},
            f"Added dependency: “{OTHER_TITLE}” {OTHER[:8]} blocks “{FOCAL_TITLE}”",
        ),
        (
            FOCAL,
            BLOCKS,
            {"from_task_id": FOCAL, "to_task_id": OTHER, "type": "blocks"},
            f"Added dependency: “{FOCAL_TITLE}” {FOCAL[:8]} blocks “{OTHER_TITLE}”",
        ),
        (
            GATE,
            WAITED_ON_BY,
            {"from_task_id": GATE, "to_task_id": OTHER, "type": "waits_on_gate"},
            f"Added dependency: “{OTHER_TITLE}” {OTHER[:8]} waits on gate "
            f"“{GATE_TITLE}”",
        ),
    ],
    ids=[BLOCKED_BY, BLOCKS, WAITED_ON_BY],
)
def test_each_sentence_writes_its_own_edge_through_the_confirm_form(
    client: TestClient,
    fake: LoggedFake,
    focal: str,
    key: str,
    upsert: dict[str, str],
    headline: str,
) -> None:
    fields = _hidden(_confirm(client, focal, key, OTHER).text)

    response = _post(client, focal, fields)

    assert _upserts(fake) == [{**upsert, "agent": OPERATOR}]
    receipt = _receipt(_landed(client, response))
    assert headline in receipt
    # And Lithos now has exactly that edge, as the focal task's list says.
    edges = asyncio.run(fake.task_edge_list(focal))
    assert any(Relation(**upsert).is_edge(edge) for edge in edges)


def test_another_relation_between_the_same_two_tasks_does_not_stop_this_one(
    client: TestClient, fake: LoggedFake
) -> None:
    """An agent's gate → waiter ``blocks`` edge is not the ``waits_on_gate``
    relation the operator asks for: it is confirmed and written."""
    _insert_as(fake, "worker-b", Relation(GATE, OTHER, "blocks"))

    confirm = _confirm(client, GATE, WAITED_ON_BY, OTHER)

    assert "data-relation-exists" not in confirm.text
    response = _post(client, GATE, _hidden(confirm.text))
    assert _upserts(fake) == [
        {
            "from_task_id": GATE,
            "to_task_id": OTHER,
            "type": "waits_on_gate",
            "agent": OPERATOR,
        }
    ]
    receipt = _receipt(_landed(client, response))
    assert "Added dependency:" in receipt
    assert "already exists" not in receipt


def test_an_edge_in_lithos_but_not_in_the_cache_already_exists_on_both_steps(
    client: TestClient, fake: LoggedFake
) -> None:
    lens = client.app.state.lens  # type: ignore[attr-defined]
    relation = Relation(OTHER, FOCAL, "blocks")
    client.get("/tasks/graph?project=lithos-loom")
    cached = lens.graph_cache.get(FOCAL)
    assert cached is not None and not any(relation.is_edge(e) for e in cached.edges)
    _insert_as(fake, "worker-b", relation)

    confirm = _confirm(client, FOCAL, BLOCKED_BY, OTHER)

    assert confirm.status_code == 200
    exists = re.search(r"data-relation-exists.*?</p>", confirm.text, re.S)
    assert exists
    assert re.search(
        r"This relation already exists \(added by worker-b, \d\d/\d\d/\d{4}\); "
        r"nothing was written\.",
        _text(exists.group(0)),
    )
    assert "data-relation-confirm-form" not in confirm.text
    assert fake.write_calls == []


def test_the_post_re_reads_too_and_a_stale_cache_does_not_let_it_upsert(
    client: TestClient, fake: LoggedFake
) -> None:
    lens = client.app.state.lens  # type: ignore[attr-defined]
    relation = Relation(OTHER, FOCAL, "blocks")
    client.get("/tasks/graph?project=lithos-loom")
    _insert_as(fake, "worker-b", relation)
    cached = lens.graph_cache.get(FOCAL)
    assert cached is not None and not any(relation.is_edge(e) for e in cached.edges)

    response = _post(
        client,
        FOCAL,
        {
            "from_task_id": OTHER,
            "to_task_id": FOCAL,
            "type": "blocks",
            "expected_status": "open",
        },
    )
    receipt = _receipt(_landed(client, response))
    assert "This relation already exists (added by worker-b" in receipt
    assert "nothing was written." in receipt
    assert _upserts(fake) == []


def test_a_seeded_edge_without_stamps_drops_the_parenthesis(
    client: TestClient, fake: LoggedFake
) -> None:
    # The demo's seed edge loom-schema -> loom-transport carries no stamps (F3).
    response = _post(
        client,
        "loom-transport",
        {
            "from_task_id": "loom-schema",
            "to_task_id": "loom-transport",
            "type": "blocks",
            "expected_status": "open",
        },
    )
    receipt = _receipt(_landed(client, response))
    assert "This relation already exists; nothing was written." in receipt
    assert _upserts(fake) == []


def test_a_failed_fresh_read_writes_nothing(
    client: TestClient, fake: LoggedFake
) -> None:
    async def unreadable(*_: Any, **__: Any) -> list[Any]:
        raise LithosToolError("session closed")

    fake.task_edge_list = unreadable  # type: ignore[method-assign]

    response = _post(
        client,
        FOCAL,
        {
            "from_task_id": OTHER,
            "to_task_id": FOCAL,
            "type": "blocks",
            "expected_status": "open",
        },
    )

    assert response.status_code == 503
    text = _text(response.text)
    assert "Nothing was changed." in text
    assert (
        "Lens couldn't check whether this relation exists — nothing was written."
        in text
    )
    assert _upserts(fake) == []
    # And the confirm step offers no form either.
    confirm = _confirm(client, FOCAL, BLOCKED_BY, OTHER)
    assert confirm.status_code == 503
    assert "data-relation-confirm-form" not in confirm.text


def _refusal(client: TestClient, task_id: str, fields: dict[str, str]) -> str:
    response = _post(client, task_id, {"expected_status": "open", **fields})
    assert response.status_code in (409, 422), response.text
    text = _text(response.text)
    assert "Nothing was changed." in text
    return text


def test_a_cycle_is_refused_with_the_upstream_message_verbatim(
    client: TestClient,
) -> None:
    text = _refusal(
        client,
        "loom-schema",
        {
            "from_task_id": "loom-transport",
            "to_task_id": "loom-schema",
            "type": "blocks",
        },
    )
    assert "This dependency would create a cycle." in text
    assert (
        "blocks edge loom-transport -> loom-schema would create a dependency cycle: "
        "loom-transport -> loom-schema -> loom-transport"
    ) in text


def test_a_self_edge_is_refused(client: TestClient) -> None:
    text = _refusal(
        client, FOCAL, {"from_task_id": FOCAL, "to_task_id": FOCAL, "type": "blocks"}
    )
    assert "A task can't depend on itself." in text


def test_a_missing_endpoint_is_refused_with_lithos_message(client: TestClient) -> None:
    text = _refusal(
        client,
        FOCAL,
        {"from_task_id": "loom-gone-away", "to_task_id": FOCAL, "type": "blocks"},
    )
    assert "Lithos couldn't find a task this refers to." in text
    assert "No task matches id prefix 'loom-gone-away' (from_task_id)." in text


@pytest.mark.parametrize(
    ("code", "copy"),
    [
        (
            "not_a_gate",
            "Approve the Influx read swap isn't a gate — only a gate can be waited on.",
        ),
        (
            "invalid_edge_type",
            "Lithos refused this with a code Lens does not recognise.",
        ),
    ],
)
def test_a_contract_refusal_renders_its_row(
    client: TestClient, fake: LoggedFake, code: str, copy: str
) -> None:
    [envelope] = [
        e
        for e in load_contract("lithos_task_edge_upsert")["responses"]["errors"]
        if e["code"] == code
    ]

    async def refused(**_: Any) -> Any:
        raise LithosToolError(envelope["message"], code=code, envelope=envelope)

    fake.task_edge_upsert = refused  # type: ignore[method-assign]
    text = _refusal(
        client,
        GATE,
        {"from_task_id": GATE, "to_task_id": OTHER, "type": "waits_on_gate"},
    )
    assert copy in text


def test_a_relation_the_page_does_not_offer_is_refused_before_lithos(
    client: TestClient, fake: LoggedFake
) -> None:
    text = _refusal(
        client,
        FOCAL,
        {"from_task_id": FOCAL, "to_task_id": OTHER, "type": "waits_on_gate"},
    )
    assert "didn't describe a relation this task's page offers" in text
    assert fake.write_calls == []


def test_every_attempt_is_recorded_with_its_arguments_and_already_exists(
    config,
    fake: LoggedFake,
    spans: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fields = {
        "from_task_id": OTHER,
        "to_task_id": FOCAL,
        "type": "blocks",
        "expected_status": "open",
    }
    with _client(config, fake) as client:
        caplog.set_level(logging.INFO, logger="lithos_lens.write_funnel")
        caplog.clear()
        spans.clear()
        _post(client, FOCAL, fields)
        _post(client, FOCAL, fields)

    lines = [r for r in caplog.records if getattr(r, "lens_event", "") == AUDIT_EVENT]
    assert [(line.result, line.write_already_exists) for line in lines] == [  # type: ignore[attr-defined]
        ("ok", False),
        ("ok", True),
    ]
    for line in lines:
        assert line.action == "edge_upsert"  # type: ignore[attr-defined]
        assert line.arguments == {  # type: ignore[attr-defined]
            "from_task_id": OTHER,
            "to_task_id": FOCAL,
            "type": "blocks",
        }
    written = [
        s for s in spans.get_finished_spans() if s.name == "lens.writes.edge_upsert"
    ]
    assert [dict(s.attributes or {})["lens.write.already_exists"] for s in written] == [
        False,
        True,
    ]
    assert len(_upserts(fake)) == 1


#: A well-formed full id no task has: the resolver passes it through to the
#: tool's own lookup, which is what answers "not found".
UNKNOWN_FULL_ID = "00000000-0000-4000-8000-000000000001"


def test_the_fakes_missing_task_is_the_contracts_envelope(fake: LoggedFake) -> None:
    """The fake's not-found for a full id carries the whole envelope, as
    ``raise_for_error`` gives the real client — not a bare code."""
    [canonical] = [
        e
        for e in load_contract("lithos_task_get")["responses"]["errors"]
        if e["code"] == "task_not_found" and e["message"].startswith("Task '")
    ]
    assert canonical["message"] == "Task 'missing-task' not found."
    with pytest.raises(LithosToolError) as raised:
        asyncio.run(fake.task_get(UNKNOWN_FULL_ID))
    assert raised.value.code == "task_not_found"
    assert dict(raised.value.envelope) == {
        **canonical,
        "message": f"Task '{UNKNOWN_FULL_ID}' not found.",
    }


def test_an_unknown_full_id_is_refused_under_the_input(
    client: TestClient, fake: LoggedFake
) -> None:
    response = _confirm(client, FOCAL, BLOCKED_BY, UNKNOWN_FULL_ID)

    assert response.status_code == 422
    error = re.search(r'data-relation-error="other".*?</div>', response.text, re.S)
    assert error
    assert f"Task '{UNKNOWN_FULL_ID}' not found." in _text(error.group(0))
    _says_nothing_changed(response.text, "task_not_found")
    assert f'value="{UNKNOWN_FULL_ID}"' in response.text
    assert fake.write_calls == []
