"""K1-S5 — Produced-by-task chip.

A note's ``metadata.source`` is validated via ``lithos_task_get`` into a chip
linking to ``/tasks/{id}``. The chip renders only on a successful lookup; an
absent source, an invalid source, or a client without ``task_get`` all render
nothing rather than a dead link. ``note_type == "task_record"`` gets a distinct
chip style.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.knowledge import ProducedByTask, load_produced_by
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
