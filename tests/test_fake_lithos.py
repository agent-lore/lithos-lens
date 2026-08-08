"""Fake-Lithos app mode: the launchable, server-backed fixture client.

These cover the seam the ``e2e/`` Playwright smoke suite depends on — that the
real application factory can boot a fully browsable app with no Lithos server
behind it — without needing a browser. The Playwright suite itself lives under
``e2e/`` and runs out of band (``make e2e``); here we prove the Python side.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.errors import ConfigError
from lithos_lens.fake_lithos import FakeLithosClient, fake_lithos_enabled
from lithos_lens.lithos_client import LithosClient, LithosToolError
from lithos_lens.main import DEFAULT_PORT, resolve_port
from lithos_lens.web import create_app


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        (" on ", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("maybe", False),
    ],
)
def test_fake_lithos_enabled_reads_env(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", value)
    assert fake_lithos_enabled() is expected


def test_fake_lithos_enabled_defaults_off_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LITHOS_LENS_FAKE_LITHOS", raising=False)
    assert fake_lithos_enabled() is False


def test_create_app_uses_real_client_without_env(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LITHOS_LENS_FAKE_LITHOS", raising=False)
    app = create_app(load_config(lithos_lens_config_env))
    # No lifespan/startup here so we never touch the network — just the wiring.
    assert isinstance(app.state.lens.lithos_client, LithosClient)


def test_create_app_uses_fake_client_when_env_enabled(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    assert isinstance(app.state.lens.lithos_client, FakeLithosClient)


def test_fake_mode_health_is_ok(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["lithos"] == "ok"


def test_fake_mode_dashboard_renders_fixture_tasks(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        response = client.get("/tasks?since=2026-08-01")
    assert response.status_code == 200
    body = response.text
    assert "Cut over Influx ingest path" in body
    assert 'data-task-row data-task-id="influx-ingest-cutover"' in body
    # A completed and a cancelled fixture so all three groups have content.
    assert 'data-task-group="completed"' in body


def test_fake_mode_task_detail_renders(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        response = client.get("/tasks/influx-ingest-cutover")
    assert response.status_code == 200
    assert 'data-task-detail="influx-ingest-cutover"' in response.text
    # The claimed fixture surfaces an active claim on the detail page.
    assert "worker-a" in response.text


def test_fake_mode_note_renders(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        response = client.get("/note/note-influx-plan")
    assert response.status_code == 200
    assert "Influx migration plan" in response.text


def test_resolve_port_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LENS_PORT", raising=False)
    assert resolve_port() == DEFAULT_PORT


def test_resolve_port_honors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LENS_PORT", "8123")
    assert resolve_port() == 8123


@pytest.mark.parametrize("bad", ["0", "70000", "-1", "notaport"])
def test_resolve_port_rejects_invalid(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("LENS_PORT", bad)
    with pytest.raises(ConfigError):
        resolve_port()


@pytest.mark.anyio
async def test_fake_client_task_get_missing_raises_coded_error() -> None:
    client = FakeLithosClient()
    with pytest.raises(LithosToolError) as excinfo:
        await client.task_get("does-not-exist")
    assert excinfo.value.code == "task_not_found"
