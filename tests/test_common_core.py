"""Common-core integration tests for the FastAPI app."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.lithos_client import LithosHealth
from lithos_lens.logging import JsonFormatter
from lithos_lens.tasks import (
    AgentRecord,
    FindingRecord,
    NoteRecord,
    TaskRecord,
    TaskStatusRecord,
)
from lithos_lens.web import create_app


class RecordingLithosClient:
    def __init__(self, health: LithosHealth) -> None:
        self.health_value: LithosHealth = health
        self.register_calls = 0
        self.closed = False

    async def health(self) -> LithosHealth:
        return self.health_value

    async def register_agent(self) -> bool:
        self.register_calls += 1
        return True

    async def list_tasks(
        self,
        *,
        agent: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
    ) -> list[TaskRecord]:
        return []

    async def task_status(self, task_id: str) -> TaskStatusRecord | None:
        return None

    async def list_findings(
        self, task_id: str, *, since: str | None = None
    ) -> list[FindingRecord]:
        return []

    async def stats(self) -> dict[str, object]:
        return {}

    async def list_agents(self) -> list[AgentRecord]:
        return []

    async def read_note(self, knowledge_id: str) -> NoteRecord | None:
        return None

    async def close(self) -> None:
        self.closed = True


def test_config_loads_common_core_defaults(lithos_lens_config_env: Path) -> None:
    config = load_config(lithos_lens_config_env)

    assert config.environment == "test"
    assert config.lithos.url == "http://lithos.test"
    assert config.lithos.mcp_sse_path == "/sse"
    assert config.lithos.agent_id == "lithos-lens-test"
    assert config.tasks.visible_cap == 50
    assert config.tasks.default_status_groups == ("open", "completed", "cancelled")
    assert config.events.enabled is True
    assert config.llm.enabled is False
    assert config.telemetry.enabled is False
    assert config.ui.default_view == "tasks"


def test_json_formatter_preserves_structured_extra_fields() -> None:
    record = logging.LogRecord(
        name="lithos_lens.web",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg="tasks dashboard filters parsed",
        args=(),
        exc_info=None,
    )
    record.tags = ["project:influx"]
    record.group_counts = {"completed": 0}

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "tasks dashboard filters parsed"
    assert payload["tags"] == ["project:influx"]
    assert payload["group_counts"] == {"completed": 0}


def test_app_degrades_when_lithos_is_unreachable(lithos_lens_config_env: Path) -> None:
    config = load_config(lithos_lens_config_env)
    lithos = RecordingLithosClient("unreachable")
    app = create_app(config, lithos_client_factory=lambda _: lithos)

    with TestClient(app) as client:
        health = client.get("/health")
        tasks = client.get("/tasks")

    assert health.status_code == 200
    assert health.json()["lithos"] == "unreachable"
    assert health.json()["status"] == "degraded"
    assert tasks.status_code == 200
    assert "Lithos is offline or degraded" in tasks.text
    assert lithos.register_calls == 0
    assert lithos.closed is True


def test_startup_auto_registers_when_lithos_is_reachable(
    lithos_lens_config_env: Path,
) -> None:
    config = load_config(lithos_lens_config_env)
    lithos = RecordingLithosClient("ok")
    app = create_app(config, lithos_client_factory=lambda _: lithos)

    with TestClient(app) as client:
        health = client.get("/health")

    assert health.status_code == 200
    assert health.json()["lithos"] == "ok"
    assert lithos.register_calls == 1
    assert lithos.closed is True


def test_static_assets_are_served(lithos_lens_config_env: Path) -> None:
    config = load_config(lithos_lens_config_env)
    app = create_app(
        config, lithos_client_factory=lambda _: RecordingLithosClient("ok")
    )

    with TestClient(app) as client:
        css = client.get("/static/lens.css")
        htmx = client.get("/static/vendor/htmx.min.js")
        tasks_js = client.get("/static/tasks.js")

    assert css.status_code == 200
    assert "--accent" in css.text
    assert htmx.status_code == 200
    assert "htmx" in htmx.text
    assert tasks_js.status_code == 200
    assert "EventSource" in tasks_js.text
