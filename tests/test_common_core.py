"""Common-core integration tests for the FastAPI app."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.errors import ConfigError
from lithos_lens.knowledge import RelatedNeighborhood, SearchResult
from lithos_lens.lithos_client import LithosHealth, LithosToolError
from lithos_lens.logging import JsonFormatter
from lithos_lens.task_graph import BlockedTaskRecord, EdgeRecord
from lithos_lens.tasks import (
    AgentRecord,
    FindingRecord,
    NoteRecord,
    NoteSummary,
    TaskRecord,
    TaskStatusRecord,
)
from lithos_lens.web import create_app


class RecordingLithosClient:
    def __init__(self, health: LithosHealth) -> None:
        self.health_value: LithosHealth = health
        self.register_calls = 0
        self.startup_calls = 0
        self.closed = False

    async def startup(self) -> None:
        self.startup_calls += 1

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
        with_claims: bool = False,
    ) -> list[TaskRecord]:
        return []

    async def task_ready(
        self,
        *,
        limit: int | None = None,
        with_claims: bool = False,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[TaskRecord]:
        return []

    async def task_blocked(
        self,
        *,
        limit: int | None = None,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[BlockedTaskRecord]:
        return []

    async def task_get(self, task_id: str) -> TaskRecord:
        # Same not-found contract as the concrete client: coded error, not None.
        raise LithosToolError(f"Task '{task_id}' not found.", code="task_not_found")

    async def task_children(
        self,
        task_id: str,
        *,
        recursive: bool = False,
        include_closed: bool = False,
    ) -> list[TaskRecord]:
        return []

    async def task_edge_list(
        self,
        task_id: str,
        *,
        direction: str = "both",
        types: list[str] | None = None,
    ) -> list[EdgeRecord]:
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

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None:
        return None

    async def read_note_by_path(self, path: str) -> NoteRecord | None:
        return None

    async def related(self, knowledge_id: str) -> RelatedNeighborhood:
        return RelatedNeighborhood()

    async def list_notes(
        self,
        *,
        title_contains: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[NoteSummary]:
        return []

    async def recent_notes(
        self,
        *,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[NoteSummary]:
        return []

    async def search_notes(
        self,
        query: str,
        *,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[SearchResult]:
        return []

    async def close(self) -> None:
        self.closed = True


def test_config_loads_common_core_defaults(lithos_lens_config_env: Path) -> None:
    config = load_config(lithos_lens_config_env)

    assert config.environment == "test"
    assert config.lithos.url == "http://lithos.test"
    assert config.lithos.mcp_sse_path == "/sse"
    assert config.lithos.agent_id == "lithos-lens-test"
    assert config.tasks.visible_cap == 50
    assert config.tasks.frontier_limit == 500
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


def test_env_override_sets_tasks_frontier_limit(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITHOS_LENS_TASKS_FRONTIER_LIMIT", "42")

    config = load_config(lithos_lens_config_env)

    assert config.tasks.frontier_limit == 42


@pytest.mark.parametrize("bad", ["0", "-5", "nope", "1.5"])
def test_env_override_tasks_frontier_limit_rejects_junk(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("LITHOS_LENS_TASKS_FRONTIER_LIMIT", bad)

    with pytest.raises(ConfigError, match="LITHOS_LENS_TASKS_FRONTIER_LIMIT"):
        load_config(lithos_lens_config_env)


def test_needs_attention_knobs_default_to_the_shipped_thresholds(
    lithos_lens_config_env: Path,
) -> None:
    """The rules must work out of the box: an operator who never writes a
    [tasks] table still gets the documented thresholds."""
    config = load_config(lithos_lens_config_env)

    assert config.tasks.gate_waiting_attention_hours == 24
    assert config.tasks.claim_expiring_soon_minutes == 10
    assert config.tasks.stale_open_age_days == 7
    assert config.tasks.unclaimed_ready_age_minutes == 60


def test_needs_attention_knobs_read_from_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "lithos-lens.toml"
    config_path.write_text(
        '[lithos-lens]\nenvironment = "test"\n[lithos-lens.tasks]\n'
        "gate_waiting_attention_hours = 4\nclaim_expiring_soon_minutes = 2\n"
        "stale_open_age_days = 3\nunclaimed_ready_age_minutes = 15\n"
    )
    monkeypatch.setenv("LITHOS_LENS_CONFIG", str(config_path))

    config = load_config(config_path)

    assert config.tasks.gate_waiting_attention_hours == 4
    assert config.tasks.claim_expiring_soon_minutes == 2
    assert config.tasks.stale_open_age_days == 3
    assert config.tasks.unclaimed_ready_age_minutes == 15


@pytest.mark.parametrize(
    ("env_var", "attribute"),
    [
        (
            "LITHOS_LENS_TASKS_GATE_WAITING_ATTENTION_HOURS",
            "gate_waiting_attention_hours",
        ),
        (
            "LITHOS_LENS_TASKS_CLAIM_EXPIRING_SOON_MINUTES",
            "claim_expiring_soon_minutes",
        ),
        ("LITHOS_LENS_TASKS_STALE_OPEN_AGE_DAYS", "stale_open_age_days"),
        (
            "LITHOS_LENS_TASKS_UNCLAIMED_READY_AGE_MINUTES",
            "unclaimed_ready_age_minutes",
        ),
    ],
)
def test_env_override_sets_each_needs_attention_knob(
    lithos_lens_config_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_var: str,
    attribute: str,
) -> None:
    """Each knob is independently overridable — they share one apply pass, so
    one override must not reset its neighbours to defaults."""
    monkeypatch.setenv(env_var, "3")

    config = load_config(lithos_lens_config_env)

    assert getattr(config.tasks, attribute) == 3
    untouched = {
        "gate_waiting_attention_hours": 24,
        "claim_expiring_soon_minutes": 10,
        "stale_open_age_days": 7,
        "unclaimed_ready_age_minutes": 60,
    }
    del untouched[attribute]
    for name, default in untouched.items():
        assert getattr(config.tasks, name) == default


@pytest.mark.parametrize(
    ("key", "over_max"),
    [
        ("gate_waiting_attention_hours", 8761),
        ("claim_expiring_soon_minutes", 10081),
        ("stale_open_age_days", 3651),
        ("unclaimed_ready_age_minutes", 10081),
    ],
)
def test_needs_attention_knob_rejects_a_value_over_its_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, over_max: int
) -> None:
    """Each threshold is bounded, not merely positive: an unbounded value
    reaches timedelta() at render time (OverflowError -> 500 on every /tasks),
    so it has to fail at load with the key named."""
    config_path = tmp_path / "lithos-lens.toml"
    config_path.write_text(
        '[lithos-lens]\nenvironment = "test"\n[lithos-lens.tasks]\n'
        f"{key} = {over_max}\n"
    )
    monkeypatch.setenv("LITHOS_LENS_CONFIG", str(config_path))

    with pytest.raises(ConfigError, match=key):
        load_config(config_path)


@pytest.mark.parametrize(
    ("env_var", "over_max"),
    [
        ("LITHOS_LENS_TASKS_GATE_WAITING_ATTENTION_HOURS", 8761),
        ("LITHOS_LENS_TASKS_CLAIM_EXPIRING_SOON_MINUTES", 10081),
        ("LITHOS_LENS_TASKS_STALE_OPEN_AGE_DAYS", 10**12),
        ("LITHOS_LENS_TASKS_UNCLAIMED_READY_AGE_MINUTES", 10081),
    ],
)
def test_env_override_needs_attention_knob_rejects_a_value_over_its_ceiling(
    lithos_lens_config_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_var: str,
    over_max: int,
) -> None:
    """The env path carries the same ceiling as the TOML path — the 10**12 case
    is the one that used to overflow timedelta() at render time."""
    monkeypatch.setenv(env_var, str(over_max))

    with pytest.raises(ConfigError, match=env_var):
        load_config(lithos_lens_config_env)


@pytest.mark.parametrize("bad", ["0", "-5", "nope", "1.5"])
def test_env_override_needs_attention_knob_rejects_junk(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("LITHOS_LENS_TASKS_STALE_OPEN_AGE_DAYS", bad)

    with pytest.raises(ConfigError, match="LITHOS_LENS_TASKS_STALE_OPEN_AGE_DAYS"):
        load_config(lithos_lens_config_env)


def test_visible_cap_in_config_warns_deprecated_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """visible_cap is superseded by frontier_limit (the graph-native dashboard
    has no per-row claim enrichment to cap); a configured value still parses
    but warns ONCE so operators migrate without breakage."""
    import lithos_lens.config as config_module

    monkeypatch.setattr(config_module, "_VISIBLE_CAP_WARNED", False)
    config_path = tmp_path / "lithos-lens.toml"
    config_path.write_text(
        '[lithos-lens]\nenvironment = "test"\n[lithos-lens.tasks]\nvisible_cap = 10\n'
    )
    monkeypatch.setenv("LITHOS_LENS_CONFIG", str(config_path))

    with caplog.at_level("WARNING", logger="lithos_lens.config"):
        first = load_config(config_path)
        load_config(config_path)

    assert first.tasks.visible_cap == 10
    warnings = [
        r for r in caplog.records if "visible_cap is deprecated" in r.getMessage()
    ]
    assert len(warnings) == 1
