"""K1-S5 — Produced-by-task chip.

A note's ``metadata.source`` is validated via ``lithos_task_get`` into a chip
linking to ``/tasks/{id}``. The chip renders only on a successful lookup; an
absent source, an invalid source, or a client without ``task_get`` all render
nothing rather than a dead link. ``note_type == "task_record"`` gets a distinct
chip style.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.knowledge_produced_by import ProducedByTask, load_produced_by
from lithos_lens.lithos_client import LithosToolError
from lithos_lens.tasks import NoteRecord, TaskRecord
from lithos_lens.web import create_app
from tests.test_tasks_mvp import TaskFakeLithosClient


def _run(coro):
    return asyncio.run(coro)


class _ProducedByFake:
    """Minimal client exposing only ``task_get`` for the chip orchestrator."""

    def __init__(self, tasks: dict[str, TaskRecord] | None = None) -> None:
        self.tasks = tasks or {}
        self.get_calls: list[str] = []

    async def task_get(self, task_id: str) -> TaskRecord:
        self.get_calls.append(task_id)
        task = self.tasks.get(task_id)
        if task is None:
            raise LithosToolError(f"Task '{task_id}' not found.", code="task_not_found")
        return task


class _NoTaskGetFake:
    """A client that never gained the ``task_get`` method (T1 not landed)."""


def _note(**metadata: object) -> NoteRecord:
    return NoteRecord(id="n1", title="A note", content="", metadata=dict(metadata))


# ── load_produced_by (pure orchestrator) ───────────────────────────────


def test_produced_by_renders_chip_for_valid_source() -> None:
    fake = _ProducedByFake({"task-7": TaskRecord(id="task-7", title="Ship the thing")})
    chip = _run(load_produced_by(fake, _note(source="task-7")))
    assert chip == ProducedByTask(task_id="task-7", title="Ship the thing")
    assert fake.get_calls == ["task-7"]


def test_produced_by_none_when_no_source() -> None:
    fake = _ProducedByFake({"task-7": TaskRecord(id="task-7", title="X")})
    assert _run(load_produced_by(fake, _note())) is None
    # No source means no validation call at all.
    assert fake.get_calls == []


def test_produced_by_none_for_blank_source() -> None:
    fake = _ProducedByFake()
    assert _run(load_produced_by(fake, _note(source="   "))) is None
    assert fake.get_calls == []


def test_produced_by_none_for_non_string_source() -> None:
    fake = _ProducedByFake()
    assert _run(load_produced_by(fake, _note(source=123))) is None
    assert fake.get_calls == []


def test_produced_by_none_when_source_unknown() -> None:
    """An invalid/stale source id (task_not_found) renders no chip, not a dead
    link."""
    fake = _ProducedByFake()  # no tasks → task_get raises task_not_found
    assert _run(load_produced_by(fake, _note(source="gone"))) is None
    assert fake.get_calls == ["gone"]


def test_produced_by_none_when_task_get_unavailable() -> None:
    """K1 does not hard-depend on T1: a client without ``task_get`` degrades to
    no chip."""
    assert _run(load_produced_by(_NoTaskGetFake(), _note(source="task-7"))) is None


def test_produced_by_source_is_stripped_before_lookup() -> None:
    fake = _ProducedByFake({"task-7": TaskRecord(id="task-7", title="X")})
    chip = _run(load_produced_by(fake, _note(source="  task-7  ")))
    assert chip is not None
    assert fake.get_calls == ["task-7"]


def test_produced_by_marks_task_record_notes() -> None:
    fake = _ProducedByFake({"task-7": TaskRecord(id="task-7", title="X")})
    chip = _run(load_produced_by(fake, _note(source="task-7", note_type="task_record")))
    assert chip is not None
    assert chip.is_task_record is True


def test_produced_by_ordinary_note_is_not_task_record() -> None:
    fake = _ProducedByFake({"task-7": TaskRecord(id="task-7", title="X")})
    chip = _run(load_produced_by(fake, _note(source="task-7", note_type="reference")))
    assert chip is not None
    assert chip.is_task_record is False


def test_produced_by_url_quotes_reserved_characters() -> None:
    """Task ids are arbitrary strings; ``?``/``#`` must be percent-encoded so the
    href targets the whole validated id, not a truncated prefix."""
    chip = ProducedByTask(task_id="task?42#x", title="X")
    assert chip.url == "/tasks/task%3F42%23x"
    assert chip.url == f"/tasks/{quote('task?42#x', safe='')}"


def test_produced_by_url_encodes_slash_without_splitting_path() -> None:
    """A ``/`` is percent-encoded (``safe=''``) so the href never splits into a
    ``/tasks/parent/child`` subpath. Note this is about URL *containment*, not
    routability: the single-segment ``/tasks/{task_id}`` route cannot address a
    slash-bearing id at all (ASGI decodes ``%2F`` back to a separator), which is
    why ``load_produced_by`` never builds a chip for one — see the next test."""
    chip = ProducedByTask(task_id="parent/child", title="X")
    assert chip.url == "/tasks/parent%2Fchild"
    assert "/tasks/parent/child" not in chip.url


def test_produced_by_none_for_unroutable_slash_id() -> None:
    """A task id containing a literal ``/`` validates via ``task_get`` but is
    not addressable by the single-segment ``/tasks/{task_id}`` route (ASGI
    percent-decodes ``%2F`` back to a separator before routing, so the href
    would 404). The loader refuses the chip: no chip beats a dead link."""
    fake = _ProducedByFake(
        {"parent/child": TaskRecord(id="parent/child", title="Nested id task")}
    )
    assert _run(load_produced_by(fake, _note(source="parent/child"))) is None
    # Validation ran — the refusal is about routability, not lookup failure.
    assert fake.get_calls == ["parent/child"]


def test_produced_by_none_for_dot_segment_ids() -> None:
    """``quote(..., safe="")`` leaves periods unescaped, and clients apply RFC
    3986 dot-segment normalization before the request goes out: ``/tasks/.``
    collapses to ``/tasks/`` and ``/tasks/..`` to ``/``, so neither href can
    reach the detail route even though the id validated. Same class as the
    slash refusal: no chip beats a dead link."""
    for dot_id in (".", ".."):
        fake = _ProducedByFake({dot_id: TaskRecord(id=dot_id, title="Dot task")})
        assert _run(load_produced_by(fake, _note(source=dot_id))) is None
        assert fake.get_calls == [dot_id]


def test_produced_by_none_when_returned_id_differs_from_source() -> None:
    """Upstream ``lithos_task_get`` is an exact-id lookup, so a success payload
    naming a different id than the requested source is a nonconforming
    response — rendering it would link a task the note never named. The loader
    refuses the chip rather than trust ``task.id`` over ``metadata.source``."""

    class _MismatchFake:
        async def task_get(self, task_id: str) -> TaskRecord:
            return TaskRecord(id="different-task", title="Not the one asked for")

    chip = _run(load_produced_by(_MismatchFake(), _note(source="expected-task")))
    assert chip is None


def test_produced_by_none_for_empty_returned_id() -> None:
    """A nonconforming client answering an empty id yields no chip (the real
    ``LithosClient.task_get`` already rejects these as invalid_response, but the
    loader takes any duck-typed client and must not render ``/tasks/``). The
    id-mismatch guard catches this today; ``_is_routable_task_id`` backstops it
    even if the guards are ever reordered."""

    class _EmptyIdFake:
        async def task_get(self, task_id: str) -> TaskRecord:
            return TaskRecord(id="", title="Idless task")

    assert _run(load_produced_by(_EmptyIdFake(), _note(source="some-task"))) is None


# ── /note/{id} rendering ───────────────────────────────────────────────


def _client(config_path: Path, fake: TaskFakeLithosClient) -> TestClient:
    config = load_config(config_path)
    app = create_app(config, lithos_client_factory=lambda _: fake)
    return TestClient(app)


def test_note_page_shows_produced_by_chip(lithos_lens_config_env: Path) -> None:
    fake = TaskFakeLithosClient()
    fake.notes["prod-note"] = NoteRecord(
        id="prod-note",
        title="Produced note",
        content="Body.",
        metadata={"source": "open-claimed"},
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/prod-note")

    assert response.status_code == 200
    assert 'href="/tasks/open-claimed"' in response.text
    assert "Produced by task:" in response.text
    # The producing task's title is validated (fetched), not just echoed.
    assert "Claimed open task" in response.text


def test_note_page_task_record_gets_distinct_chip_style(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()
    fake.notes["record-note"] = NoteRecord(
        id="record-note",
        title="Task record",
        content="Body.",
        metadata={"source": "open-claimed", "note_type": "task_record"},
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/record-note")

    assert response.status_code == 200
    assert "produced-by-chip-record" in response.text
    assert "Task record:" in response.text


def test_note_page_chip_href_is_url_encoded(lithos_lens_config_env: Path) -> None:
    """A validated task id carrying URL-reserved characters is percent-encoded in
    the chip href, so the link targets the whole id rather than a prefix cut at
    ``?``/``#``."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        TaskRecord(
            id="task?42",
            title="Reserved id task",
            status="open",
            created_by="planner",
            created_at="2026-04-26T10:00:00+00:00",
        )
    )
    fake.notes["reserved-note"] = NoteRecord(
        id="reserved-note",
        title="Reserved note",
        content="Body.",
        metadata={"source": "task?42"},
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/reserved-note")

    assert response.status_code == 200
    assert 'href="/tasks/task%3F42"' in response.text
    assert 'href="/tasks/task?42"' not in response.text


def test_note_page_chip_href_round_trips_to_the_same_task(
    lithos_lens_config_env: Path,
) -> None:
    """End-to-end link/route contract: the chip href for a reserved-char task id
    is followable and resolves to the SAME task ``load_produced_by`` validated —
    not a 404 or a different id. Exercises the real ``/tasks/{task_id}`` route."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        TaskRecord(
            id="weird?id#frag",
            title="Weird reserved id task",
            status="open",
            created_by="planner",
            created_at="2026-04-26T10:00:00+00:00",
        )
    )
    fake.notes["rt-note"] = NoteRecord(
        id="rt-note",
        title="Round-trip note",
        content="Body.",
        metadata={"source": "weird?id#frag"},
    )

    with _client(lithos_lens_config_env, fake) as client:
        note_response = client.get("/note/rt-note")
        assert note_response.status_code == 200
        href = re.search(
            r'class="produced-by-chip[^"]*"\s+href="([^"]+)"', note_response.text
        )
        assert href is not None
        # Follow the chip's own href through the real detail route.
        task_response = client.get(href.group(1))

    assert task_response.status_code == 200
    assert "Weird reserved id task" in task_response.text


def test_note_page_no_chip_without_source(lithos_lens_config_env: Path) -> None:
    fake = TaskFakeLithosClient()
    fake.notes["plain-note"] = NoteRecord(
        id="plain-note", title="Plain note", content="Body."
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/plain-note")

    assert response.status_code == 200
    assert "produced-by-chip" not in response.text


def test_note_page_no_chip_for_invalid_source(lithos_lens_config_env: Path) -> None:
    fake = TaskFakeLithosClient()
    fake.notes["stale-note"] = NoteRecord(
        id="stale-note",
        title="Stale note",
        content="Body.",
        metadata={"source": "does-not-exist"},
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/stale-note")

    assert response.status_code == 200
    # The note still renders; only the dead-link chip is suppressed.
    assert "Stale note" in response.text
    assert "produced-by-chip" not in response.text


def test_note_page_no_chip_for_slash_bearing_task_id(
    lithos_lens_config_env: Path,
) -> None:
    """A source that validates to a slash-bearing task id renders no chip: its
    href (``/tasks/parent%2Fchild``) would 404 against the real single-segment
    detail route, and a validated-but-dead link is exactly what the chip
    contract forbids."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        TaskRecord(
            id="parent/child",
            title="Nested id task",
            status="open",
            created_by="planner",
            created_at="2026-04-26T10:00:00+00:00",
        )
    )
    fake.notes["nested-note"] = NoteRecord(
        id="nested-note",
        title="Nested note",
        content="Body.",
        metadata={"source": "parent/child"},
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/nested-note")
        assert response.status_code == 200
        # The note renders, the chip does not…
        assert "Nested note" in response.text
        assert "produced-by-chip" not in response.text
        # …and the href it would have carried really is unroutable.
        assert client.get("/tasks/parent%2Fchild").status_code == 404


def test_note_page_no_chip_for_dot_segment_task_id(
    lithos_lens_config_env: Path,
) -> None:
    """A source validating to the ``.`` task id renders no chip: the would-be
    href ``/tasks/.`` dot-segment-normalizes to ``/tasks/`` before any request
    is made, so it can never reach the detail route for that task."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        TaskRecord(
            id=".",
            title="Dot id task",
            status="open",
            created_by="planner",
            created_at="2026-04-26T10:00:00+00:00",
        )
    )
    fake.notes["dot-note"] = NoteRecord(
        id="dot-note",
        title="Dot note",
        content="Body.",
        metadata={"source": "."},
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/dot-note")

    assert response.status_code == 200
    assert "Dot note" in response.text
    assert "produced-by-chip" not in response.text
